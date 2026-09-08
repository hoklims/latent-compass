"""HOK-243 — the durable pre-action strategic decision memory store.

A store is a local SQLite database bound at creation to exactly one host, one
agent family, one store id and one epoch. It records opt-in strategic decisions
and the events that revoke or redact them. It never executes anything, never
selects a route or contacts a network. Store creation/opening refuses any
pre-existing symlink or reparse redirection in its root.

Physically separate per family
------------------------------
The binding is written once and never updated. A native append must match the
host, family, store id and epoch it was bound to, or it is refused as a
provenance mismatch. A Codex store and a Claude store are different files with
different genesis seals; nothing synchronises them, and nothing ever will
implicitly. The only crossing is an explicit sealed envelope — see
:meth:`DecisionMemoryStore.export_transfer` and
:meth:`DecisionMemoryStore.import_transfer` — whose imported content is
``FOREIGN_READ_ONLY``: readable, never native, never revisable here, and never
authority-bearing.

Append-only, with an event chain
--------------------------------
There is no update and no delete in the write path. A revision, a revocation and
a tombstone are all *appends*. Each event links to its predecessor::

    chain_seal(0) = seal(format_version, store_id, host_id, agent_family, epoch)
    chain_seal(n) = seal(seq, event_id, event_kind, decision_id, revision,
                         origin_kind, transfer_seal, content_seal,
                         chain_seal(n-1), appended_at)

``chain_seal(0)`` is recomputed from the binding at verification time, never read
back from storage, so relabelling ``host_id`` breaks the chain instead of
renaming the store's history. A durable anchor — the event count, the tail seal
and the epoch status — is written inside the same transaction as each append and
checked by :meth:`DecisionMemoryStore.verify`, because a chain alone cannot
notice that its last events were removed.

**What this does not prove.** A hash chain plus an anchor detects tampering by
anyone who cannot rewrite both. It establishes no authenticity and no chronology:
an administrator with write access can recompute the chain and the anchor
together from a forged history and produce a store that verifies perfectly. The
same is true of a transfer envelope — its seal proves internal consistency, never
who issued it or when. Detecting that needs an external anchor this package does
not have. The limit is intrinsic to the threat model, not an implementation gap.

Atomicity and compare-and-set
-----------------------------
Every append runs inside one ``BEGIN IMMEDIATE`` transaction that re-reads the
binding, the epoch status, the generation and the tail. Admission, contract
validation, clock validation and receipt construction all happen **before** the
commit, so no failure can occur after a row is durable. An interruption before
commit leaves no trace: no row, no burned sequence number, an unchanged anchor.
Every write accepts an optional ``expected_generation``; when supplied and stale,
the append is refused without touching record count, generation or root seal.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Final

from pydantic import Field, ValidationError

from latent_compass.canonical import seal
from latent_compass.confined_io import plan_confined_target
from latent_compass.contracts import (
    DECISION_MEMORY_CONTRACT_VERSION,
    DECISION_MEMORY_FORMAT_VERSION,
    SUPPORTED_DECISION_MEMORY_FORMATS,
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.admission import (
    admit_strategic_decision,
    admit_transfer_envelope,
    screen_persisted_text,
)
from latent_compass.decision_memory.contracts import (
    MAX_REVISION,
    ActiveDecision,
    DecisionBinding,
    DecisionOriginKind,
    DecisionTombstone,
    DecisionTransferEnvelope,
    EventKind,
    RevocationEvent,
    StrategicDecisionRecord,
    build_transfer_envelope,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import (
    ContractViolation,
    DecisionMemoryViolation,
    DecisionRevisionConflict,
    EpochClosed,
    IntegrityError,
    LedgerError,
    ProvenanceMismatch,
    StoreAlreadyExists,
    StoreNotFound,
    TransferRefused,
)
from latent_compass.ledger import Clock, EpochStatus, utc_now

__all__ = [
    "DECISION_DATABASE_FILENAME",
    "MAX_DECISION_PAGE_SIZE",
    "DecisionAppendReceipt",
    "DecisionIntegrityFinding",
    "DecisionIntegrityKind",
    "DecisionIntegrityReport",
    "DecisionMemoryStore",
    "DecisionStoreBinding",
    "DecisionStoreStatus",
    "TransferReceipt",
]

DECISION_DATABASE_FILENAME: Final = "decision-memory.sqlite3"
MAX_DECISION_PAGE_SIZE: Final = 1000

GENESIS_SEAL_DOMAIN: Final = "decision-memory.genesis.v1"
CHAIN_SEAL_DOMAIN: Final = "decision-memory.chain.v1"
ANCHOR_SEAL_DOMAIN: Final = "decision-memory.anchor.v1"

_BINDING_KEYS: Final = (
    "decision_memory_format_version",
    "store_id",
    "host_id",
    "agent_family",
    "epoch",
    "epoch_status",
    "created_at",
)

_SCHEMA: Final[tuple[str, ...]] = (
    """
CREATE TABLE store_meta (
    key   TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT;
""",
    """
CREATE TABLE events (
    seq             INTEGER PRIMARY KEY NOT NULL,
    event_id        TEXT NOT NULL UNIQUE,
    event_kind      TEXT NOT NULL,
    decision_id     TEXT NOT NULL,
    revision        INTEGER,
    origin_kind     TEXT NOT NULL,
    transfer_seal   TEXT,
    content_seal    TEXT NOT NULL,
    prev_chain_seal TEXT NOT NULL,
    chain_seal      TEXT NOT NULL UNIQUE,
    payload         TEXT,
    redacted        INTEGER NOT NULL DEFAULT 0,
    appended_at     TEXT NOT NULL
) STRICT;
""",
    """
CREATE TABLE imports (
    transfer_seal      TEXT PRIMARY KEY NOT NULL,
    source_record_seal TEXT NOT NULL UNIQUE,
    event_id           TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    imported_at        TEXT NOT NULL
) STRICT;
""",
)

BoundedSeal = Annotated[str, Field(min_length=1, max_length=200)]
EventId = Annotated[str, Field(min_length=3, max_length=200)]


class _Stamp(StrictModel):
    """Validates a clock's output against the canonical timestamp contract."""

    at: Timestamp


def _stamp(clock: Clock) -> str:
    try:
        produced = clock()
    except Exception as exc:
        raise ContractViolation(
            "the clock raised instead of producing a timestamp",
            detail={"cause": type(exc).__name__},
        ) from exc
    return validate_contract(
        _Stamp, {"at": produced}, error=ContractViolation, context="clock output"
    ).at


def _require_timestamp(value: str, *, what: str) -> str:
    """Refuse a caller-supplied instant that is not a canonical timestamp."""
    return validate_contract(_Stamp, {"at": value}, error=ContractViolation, context=what).at


class DecisionIntegrityKind(StrEnum):
    """What kind of defect was found. Never collapsed into a single boolean."""

    SEQUENCE_INVALID = "SEQUENCE_INVALID"
    CHAIN_BROKEN = "CHAIN_BROKEN"
    EVENT_KIND_UNKNOWN = "EVENT_KIND_UNKNOWN"
    ORIGIN_KIND_UNKNOWN = "ORIGIN_KIND_UNKNOWN"
    EVENT_IDENTITY_MISMATCH = "EVENT_IDENTITY_MISMATCH"
    IMPORT_MISMATCH = "IMPORT_MISMATCH"
    PAYLOAD_ALTERED = "PAYLOAD_ALTERED"
    PAYLOAD_UNPARSEABLE = "PAYLOAD_UNPARSEABLE"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_PRESENT_ON_REDACTED = "PAYLOAD_PRESENT_ON_REDACTED"
    REDACTED_WITHOUT_TOMBSTONE = "REDACTED_WITHOUT_TOMBSTONE"
    TOMBSTONE_ON_UNREDACTED = "TOMBSTONE_ON_UNREDACTED"
    ORPHAN_EVENT = "ORPHAN_EVENT"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    REVISION_LINK_BROKEN = "REVISION_LINK_BROKEN"
    ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
    ANCHOR_MISSING = "ANCHOR_MISSING"


class DecisionStoreBinding(StrictModel):
    """The identity a decision memory store is bound to at creation, and never after."""

    decision_memory_format_version: str = Field(min_length=5, max_length=20)
    store_id: Identifier
    host_id: Identifier
    agent_family: AgentFamily
    epoch: Identifier
    epoch_status: EpochStatus
    created_at: Timestamp

    def binding_ref(self) -> DecisionBinding:
        """The four fields a record must match to be native here."""
        return DecisionBinding(
            host_id=self.host_id,
            agent_family=self.agent_family,
            store_id=self.store_id,
            epoch=self.epoch,
        )

    def genesis_seal(self) -> str:
        """Recomputed from identity, never read back from storage."""
        return seal(
            GENESIS_SEAL_DOMAIN,
            {
                "decision_memory_format_version": self.decision_memory_format_version,
                "store_id": self.store_id,
                "host_id": self.host_id,
                "agent_family": self.agent_family.value,
                "epoch": self.epoch,
            },
        )


class DecisionAppendReceipt(StrictModel):
    """Proof that one event was accepted, and where it landed."""

    seq: int = Field(ge=1)
    event_kind: EventKind
    event_id: EventId
    decision_id: Identifier
    revision: int | None = Field(default=None, ge=1, le=MAX_REVISION)
    origin_kind: DecisionOriginKind
    content_seal: BoundedSeal
    chain_seal: BoundedSeal
    root_seal: BoundedSeal
    generation: int = Field(ge=1)
    record_count: int = Field(ge=0)
    appended_at: Timestamp


class DecisionIntegrityFinding(StrictModel):
    """One defect, located and named."""

    seq: int | None = Field(default=None, ge=1)
    event_id: str | None = Field(default=None)
    kind: DecisionIntegrityKind
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class DecisionIntegrityReport(StrictModel):
    """Result of a full chain walk over every durable event."""

    ok: bool
    generation: int = Field(ge=0)
    record_count: int = Field(ge=0)
    revoked_count: int = Field(ge=0)
    redacted_count: int = Field(ge=0)
    foreign_count: int = Field(ge=0)
    root_seal: BoundedSeal
    findings: tuple[DecisionIntegrityFinding, ...] = Field(default=())


class DecisionStoreStatus(StrictModel):
    """The counters a caller needs to build a compare-and-set append."""

    binding: DecisionStoreBinding
    generation: int = Field(ge=0)
    record_count: int = Field(ge=0)
    root_seal: BoundedSeal


class TransferReceipt(StrictModel):
    """Proof that one foreign record was imported, and what it was bound to."""

    transfer_seal: Seal
    source_record_seal: Seal
    source_binding: DecisionBinding
    destination_binding: DecisionBinding
    append: DecisionAppendReceipt


def _chain_seal(
    *,
    seq: int,
    event_id: str,
    event_kind: str,
    decision_id: str,
    revision: int | None,
    origin_kind: str,
    transfer_seal: str | None,
    content_seal: str,
    prev_chain_seal: str,
    appended_at: str,
) -> str:
    return seal(
        CHAIN_SEAL_DOMAIN,
        {
            "seq": seq,
            "event_id": event_id,
            "event_kind": event_kind,
            "decision_id": decision_id,
            "revision": revision,
            "origin_kind": origin_kind,
            "transfer_seal": transfer_seal,
            "content_seal": content_seal,
            "prev_chain_seal": prev_chain_seal,
            "appended_at": appended_at,
        },
    )


def _anchor_seal(*, genesis: str, generation: int, tail: str, epoch_status: str) -> str:
    return seal(
        ANCHOR_SEAL_DOMAIN,
        {
            "genesis": genesis,
            "generation": generation,
            "tail": tail,
            "epoch_status": epoch_status,
        },
    )


def _record_event_id(decision_id: str, revision: int) -> str:
    return f"{decision_id}:r{revision}"


def _revocation_event_id(decision_id: str) -> str:
    return f"{decision_id}:revoked"


def _tombstone_event_id(decision_id: str, revision: int) -> str:
    return f"{decision_id}:r{revision}:tombstone"


def _validated_store_root(root: Path | str) -> Path:
    """Reject lexical escapes and any existing symlink/reparse component.

    SQLite's stdlib API accepts a pathname rather than an already-confined file
    handle. This check therefore protects admission against pre-existing
    redirection; callers must also keep the local root free from concurrent
    adversarial namespace mutation for the store lifetime.
    """
    absolute = Path(root).absolute()
    plan_confined_target(
        absolute,
        absolute / DECISION_DATABASE_FILENAME,
        what="decision memory database",
    )
    try:
        resolved = absolute.resolve(strict=False)
    except OSError as exc:
        raise ContractViolation(
            "decision memory root cannot be resolved safely",
            detail={"root": str(absolute), "cause": exc.strerror},
        ) from exc
    if resolved != absolute:
        raise ContractViolation(
            "decision memory root must not contain a symlink or reparse point",
            detail={"root": str(absolute), "reason": "reparse_root"},
        )
    return absolute


def _validated_database_path(root: Path) -> Path:
    """Reject a database entry that is itself a symlink or reparse point."""
    database = root / DECISION_DATABASE_FILENAME
    try:
        resolved = database.resolve(strict=False)
    except OSError as exc:
        raise ContractViolation(
            "decision memory database cannot be resolved safely",
            detail={"path": str(database), "cause": exc.strerror},
        ) from exc
    if resolved != database:
        raise ContractViolation(
            "decision memory database must not be a symlink or reparse point",
            detail={"path": str(database), "reason": "reparse_database"},
        )
    return database


class DecisionMemoryStore:
    """A local, single-host, single-family, append-only decision memory."""

    def __init__(self, root: Path, connection: sqlite3.Connection) -> None:
        self.root = Path(root)
        self._conn = connection

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path | str,
        *,
        store_id: str,
        host_id: str,
        agent_family: AgentFamily,
        epoch: str,
        clock: Clock = utc_now,
    ) -> DecisionMemoryStore:
        """Create a store bound to one host, one agent family and one epoch.

        The database file is claimed with ``O_CREAT | O_EXCL``, which is the
        mutual exclusion: a concurrent loser never gets the file, so it can
        never delete the winner's store on its way out.
        """
        screen_persisted_text(store_id=store_id, host_id=host_id, epoch=epoch)
        root_path = _validated_store_root(root)
        database = _validated_database_path(root_path)
        binding = validate_contract(
            DecisionStoreBinding,
            {
                "decision_memory_format_version": DECISION_MEMORY_FORMAT_VERSION,
                "store_id": store_id,
                "host_id": host_id,
                "agent_family": agent_family,
                "epoch": epoch,
                "epoch_status": EpochStatus.OPEN,
                "created_at": _stamp(clock),
            },
            error=ContractViolation,
            context="decision memory store binding",
        )
        genesis = binding.genesis_seal()
        meta = {key: str(value) for key, value in binding.model_dump(mode="json").items()}
        meta["anchor_seal"] = _anchor_seal(
            genesis=genesis, generation=0, tail=genesis, epoch_status=EpochStatus.OPEN.value
        )
        meta["anchor_generation"] = "0"
        meta["anchor_tail"] = genesis

        root_path.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(database), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise StoreAlreadyExists(
                f"a decision memory already exists at {database}",
                detail={"root": str(root_path)},
            ) from exc
        except OSError as exc:
            raise LedgerError(
                f"cannot create a decision memory at {database}",
                detail={"root": str(root_path), "cause": exc.strerror},
            ) from exc
        os.close(descriptor)

        connection: sqlite3.Connection | None = None
        try:
            connection = cls._connect(database)
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO store_meta (key, value) VALUES (?, ?)", sorted(meta.items())
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection is not None:
                connection.close()
            # Safe: this process created the file, proven by the exclusive open.
            database.unlink(missing_ok=True)
            raise
        return cls(root_path, connection)

    @classmethod
    def open(cls, root: Path | str) -> DecisionMemoryStore:
        """Open an existing store, refusing an unknown store format."""
        root_path = _validated_store_root(root)
        database = _validated_database_path(root_path)
        if not database.exists():
            raise StoreNotFound(
                f"no decision memory at {database}", detail={"root": str(root_path)}
            )
        connection = cls._connect(database)
        store = cls(root_path, connection)
        try:
            with _sqlite_errors(root_path):
                check_contract_version(
                    store._meta("decision_memory_format_version"),
                    SUPPORTED_DECISION_MEMORY_FORMATS,
                    "decision memory store",
                )
        except BaseException:
            connection.close()
            raise
        return store

    @staticmethod
    def _connect(database: Path) -> sqlite3.Connection:
        with _sqlite_errors(database.parent):
            connection = sqlite3.connect(database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DecisionMemoryStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- binding and counters ----------------------------------------------

    def _meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise LedgerError(f"store metadata is missing {key!r}", detail={"key": key})
        return str(row["value"])

    def _binding_from(self, connection: sqlite3.Connection) -> DecisionStoreBinding:
        rows = connection.execute(
            "SELECT key, value FROM store_meta WHERE key IN "
            "('decision_memory_format_version','store_id','host_id','agent_family',"
            " 'epoch','epoch_status','created_at')"
        ).fetchall()
        payload = {row["key"]: row["value"] for row in rows}
        missing = sorted(set(_BINDING_KEYS) - set(payload))
        if missing:
            raise LedgerError("store metadata is incomplete", detail={"missing": missing})
        return validate_contract(
            DecisionStoreBinding,
            payload,
            error=ContractViolation,
            context="decision memory store binding",
        )

    def binding(self) -> DecisionStoreBinding:
        """The immutable identity this store is bound to."""
        with _sqlite_errors(self.root):
            return self._binding_from(self._conn)

    @staticmethod
    def _generation_of(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COUNT(*) AS total FROM events").fetchone()
        return int(row["total"])

    @staticmethod
    def _record_count_of(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM events WHERE event_kind = ?",
            (EventKind.RECORD.value,),
        ).fetchone()
        return int(row["total"])

    def generation(self) -> int:
        """Total durable events. The value an ``expected_generation`` must match."""
        with _sqlite_errors(self.root):
            return self._generation_of(self._conn)

    def record_count(self) -> int:
        """Number of durable record events, revisions included."""
        with _sqlite_errors(self.root):
            return self._record_count_of(self._conn)

    def _root_seal_of(self, connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT chain_seal FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return self._binding_from(connection).genesis_seal()
        return str(row["chain_seal"])

    def root_seal(self) -> str:
        """Seal of the last chain link, or the genesis seal for an empty store."""
        with _sqlite_errors(self.root):
            return self._root_seal_of(self._conn)

    def status(self) -> DecisionStoreStatus:
        """Binding plus the three counters a compare-and-set append needs."""
        with _sqlite_errors(self.root):
            return DecisionStoreStatus(
                binding=self._binding_from(self._conn),
                generation=self._generation_of(self._conn),
                record_count=self._record_count_of(self._conn),
                root_seal=self._root_seal_of(self._conn),
            )

    # -- write path --------------------------------------------------------

    @contextmanager
    def _append_transaction(
        self, *, expected_generation: int | None, what: str
    ) -> Iterator[DecisionStoreBinding]:
        """Open the one transaction every durable write shares.

        Verification, binding, epoch status and the generation guard all run
        inside it, so a concurrent write cannot be overtaken by a check made
        before it. Anything that raises rolls the whole attempt back.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            report = self._verify_unlocked()
            if not report.ok:
                raise IntegrityError(
                    f"refusing to {what} in a store that fails integrity verification",
                    detail={
                        "findings": [finding.canonical_payload() for finding in report.findings]
                    },
                )
            binding = self._binding_from(self._conn)
            if binding.epoch_status is EpochStatus.ABANDONED:
                raise EpochClosed(
                    "the epoch of this decision memory was abandoned; no further writes",
                    detail={"store_id": binding.store_id, "epoch": binding.epoch},
                )
            if expected_generation is not None:
                observed = self._generation_of(self._conn)
                if expected_generation != observed:
                    raise DecisionRevisionConflict(
                        "the store is not at the generation this write expected",
                        detail={
                            "reason": "generation_mismatch",
                            "expected_generation": expected_generation,
                            "observed_generation": observed,
                        },
                    )
            yield binding
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _insert_event(
        self,
        binding: DecisionStoreBinding,
        *,
        event_id: str,
        event_kind: EventKind,
        decision_id: str,
        revision: int | None,
        origin_kind: DecisionOriginKind,
        content_seal: str,
        payload: str,
        appended_at: str,
        transfer_seal: str | None = None,
    ) -> DecisionAppendReceipt:
        """Link, insert and re-anchor one event. Everything fallible runs first."""
        tail = self._conn.execute(
            "SELECT seq, chain_seal FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if tail is None:
            seq = 1
            prev_chain_seal = binding.genesis_seal()
        else:
            seq = int(tail["seq"]) + 1
            prev_chain_seal = str(tail["chain_seal"])

        chain_seal = _chain_seal(
            seq=seq,
            event_id=event_id,
            event_kind=event_kind.value,
            decision_id=decision_id,
            revision=revision,
            origin_kind=origin_kind.value,
            transfer_seal=transfer_seal,
            content_seal=content_seal,
            prev_chain_seal=prev_chain_seal,
            appended_at=appended_at,
        )
        # Built before the insert: nothing that can fail runs after the commit.
        receipt = DecisionAppendReceipt(
            seq=seq,
            event_kind=event_kind,
            event_id=event_id,
            decision_id=decision_id,
            revision=revision,
            origin_kind=origin_kind,
            content_seal=content_seal,
            chain_seal=chain_seal,
            root_seal=chain_seal,
            generation=seq,
            record_count=self._record_count_of(self._conn)
            + (1 if event_kind is EventKind.RECORD else 0),
            appended_at=appended_at,
        )
        try:
            self._conn.execute(
                "INSERT INTO events "
                "(seq, event_id, event_kind, decision_id, revision, origin_kind, transfer_seal, "
                " content_seal, prev_chain_seal, chain_seal, payload, redacted, appended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    seq,
                    event_id,
                    event_kind.value,
                    decision_id,
                    revision,
                    origin_kind.value,
                    transfer_seal,
                    content_seal,
                    prev_chain_seal,
                    chain_seal,
                    payload,
                    appended_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DecisionRevisionConflict(
                f"event {event_id!r} is already present in this decision memory",
                detail={"reason": "duplicate_event", "event_id": event_id},
            ) from exc
        self._write_anchor(
            self._conn,
            genesis=binding.genesis_seal(),
            generation=seq,
            tail=chain_seal,
            epoch_status=binding.epoch_status,
        )
        return receipt

    @staticmethod
    def _write_anchor(
        connection: sqlite3.Connection,
        *,
        genesis: str,
        generation: int,
        tail: str,
        epoch_status: EpochStatus,
    ) -> None:
        values = {
            "anchor_generation": str(generation),
            "anchor_tail": tail,
            "anchor_seal": _anchor_seal(
                genesis=genesis, generation=generation, tail=tail, epoch_status=epoch_status.value
            ),
        }
        connection.executemany(
            "INSERT INTO store_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            sorted(values.items()),
        )

    @staticmethod
    def _require_native_binding(record: StrategicDecisionRecord, binding: DecisionBinding) -> None:
        for field, expected, received in (
            ("host_id", binding.host_id, record.binding.host_id),
            ("agent_family", binding.agent_family.value, record.binding.agent_family.value),
            ("store_id", binding.store_id, record.binding.store_id),
            ("epoch", binding.epoch, record.binding.epoch),
        ):
            if expected != received:
                raise ProvenanceMismatch(
                    f"decision record {field} does not match this decision memory",
                    detail={
                        "field": field,
                        "store": expected,
                        "record": received,
                        "decision_id": record.decision_id,
                    },
                )

    def _head_record_row(self, decision_id: str) -> sqlite3.Row | None:
        head: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM events WHERE event_kind = ? AND decision_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (EventKind.RECORD.value, decision_id),
        ).fetchone()
        return head

    def _revocation_row(self, decision_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM events WHERE event_kind = ? AND decision_id = ?",
            (EventKind.REVOCATION.value, decision_id),
        ).fetchone()
        return row

    def append_decision(
        self,
        payload: object,
        *,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> DecisionAppendReceipt:
        """Admit and append one record — an initial revision or a revision of one.

        ``payload`` is raw JSON-shaped data on purpose: admission is the only
        supported construction path, and it runs here, before any transaction is
        opened, so a refused payload cannot move record count, generation or root
        seal.

        A revision must extend the *exact* current head: revision ``n`` requires
        a head at ``n - 1`` whose record seal is exactly the one it names. A
        stale revision, a fork off an earlier revision, a replayed initial
        revision, a revision of a revoked decision and a revision of a foreign
        decision are all refused, and none of them changes the store.
        """
        record = admit_strategic_decision(payload)
        appended_at = _stamp(clock)
        document = json.dumps(
            record.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        content_seal = record.record_seal()

        with (
            _sqlite_errors(self.root),
            self._append_transaction(
                expected_generation=expected_generation, what="append a decision"
            ) as binding,
        ):
            self._require_native_binding(record, binding.binding_ref())
            self._require_extends_head(record)
            # Returning here still commits: the context manager resumes past its
            # yield on a clean exit, which is where COMMIT runs.
            return self._insert_event(
                binding,
                event_id=_record_event_id(record.decision_id, record.revision),
                event_kind=EventKind.RECORD,
                decision_id=record.decision_id,
                revision=record.revision,
                origin_kind=DecisionOriginKind.NATIVE,
                content_seal=content_seal,
                payload=document,
                appended_at=appended_at,
            )

    def _require_extends_head(self, record: StrategicDecisionRecord) -> None:
        """Refuse anything that is not an exact extension of the current head."""
        head = self._head_record_row(record.decision_id)
        if head is not None and str(head["origin_kind"]) != DecisionOriginKind.NATIVE.value:
            raise DecisionRevisionConflict(
                "an imported decision is read-only here and cannot be revised",
                detail={
                    "reason": "foreign_decision",
                    "decision_id": record.decision_id,
                    "origin_kind": str(head["origin_kind"]),
                },
            )
        if self._revocation_row(record.decision_id) is not None:
            raise DecisionRevisionConflict(
                "a revoked decision cannot be revised",
                detail={"reason": "revoked_decision", "decision_id": record.decision_id},
            )
        if record.revision == 1:
            if head is not None:
                raise DecisionRevisionConflict(
                    "initial history is never replaced: this decision already exists",
                    detail={
                        "reason": "initial_revision_exists",
                        "decision_id": record.decision_id,
                        "head_revision": int(head["revision"]),
                    },
                )
            return
        if head is None:
            raise DecisionRevisionConflict(
                "a revision was offered for a decision that has no initial revision",
                detail={
                    "reason": "no_prior_revision",
                    "decision_id": record.decision_id,
                    "revision": record.revision,
                },
            )
        head_revision = int(head["revision"])
        if record.revision != head_revision + 1:
            raise DecisionRevisionConflict(
                "a revision must extend the current head by exactly one",
                detail={
                    "reason": "stale_or_forked_revision",
                    "decision_id": record.decision_id,
                    "offered_revision": record.revision,
                    "head_revision": head_revision,
                },
            )
        if record.supersedes_revision_seal != str(head["content_seal"]):
            raise DecisionRevisionConflict(
                "a revision must name the seal of the exact revision it supersedes",
                detail={
                    "reason": "superseded_seal_mismatch",
                    "decision_id": record.decision_id,
                    "expected": str(head["content_seal"]),
                    "offered": record.supersedes_revision_seal,
                },
            )

    def revoke(
        self,
        decision_id: str,
        *,
        reason: str,
        revoked_by: str,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> DecisionAppendReceipt:
        """Append the durable statement that a decision no longer holds.

        Revocation removes nothing. The whole revision history stays readable and
        replayable; the decision simply stops appearing in the active view, and a
        further revision of it is refused.
        """
        screen_persisted_text(decision_id=decision_id, reason=reason, revoked_by=revoked_by)
        revoked_at = _stamp(clock)
        with (
            _sqlite_errors(self.root),
            self._append_transaction(
                expected_generation=expected_generation, what="revoke a decision"
            ) as binding,
        ):
            head = self._head_record_row(decision_id)
            if head is None:
                raise StoreNotFound(
                    f"no decision {decision_id!r} in this decision memory",
                    detail={"decision_id": decision_id},
                )
            if self._revocation_row(decision_id) is not None:
                raise DecisionRevisionConflict(
                    "this decision is already revoked",
                    detail={"reason": "already_revoked", "decision_id": decision_id},
                )
            event = RevocationEvent(
                contract_version=DECISION_MEMORY_CONTRACT_VERSION,
                decision_id=decision_id,
                revoked_revision=int(head["revision"]),
                revoked_revision_seal=str(head["content_seal"]),
                revoked_by=revoked_by,
                reason=reason,
                revoked_at=revoked_at,
            )
            return self._insert_event(
                binding,
                event_id=_revocation_event_id(decision_id),
                event_kind=EventKind.REVOCATION,
                decision_id=decision_id,
                revision=event.revoked_revision,
                origin_kind=DecisionOriginKind(str(head["origin_kind"])),
                content_seal=event.event_seal(),
                payload=json.dumps(
                    event.canonical_payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                appended_at=revoked_at,
            )

    def tombstone(
        self,
        decision_id: str,
        revision: int,
        *,
        reason: str,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> DecisionAppendReceipt:
        """Drop one revision's payload, preserving the row, its seal and the chain.

        The redaction is itself a durable appended event, so the remaining
        history is not silently rewritten: ``verify`` reports the revision as
        redacted and names the tombstone that explains it. What is lost is the
        ability to read that revision back. Physical erasure of the bytes remains
        an explicit operator act with operator tools, and is not performed here.
        """
        screen_persisted_text(decision_id=decision_id, reason=reason)
        created_at = _stamp(clock)
        with (
            _sqlite_errors(self.root),
            self._append_transaction(
                expected_generation=expected_generation, what="redact a decision revision"
            ) as binding,
        ):
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_kind = ? AND decision_id = ? AND revision = ?",
                (EventKind.RECORD.value, decision_id, revision),
            ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"no revision {revision} of decision {decision_id!r}",
                    detail={"decision_id": decision_id, "revision": revision},
                )
            if int(row["redacted"]) == 1:
                raise DecisionRevisionConflict(
                    "this revision is already redacted",
                    detail={
                        "reason": "already_redacted",
                        "decision_id": decision_id,
                        "revision": revision,
                    },
                )
            stone = DecisionTombstone(
                contract_version=DECISION_MEMORY_CONTRACT_VERSION,
                decision_id=decision_id,
                revision=revision,
                redacted_record_seal=str(row["content_seal"]),
                reason=reason,
                created_at=created_at,
            )
            self._conn.execute(
                "UPDATE events SET payload = NULL, redacted = 1 WHERE event_id = ?",
                (str(row["event_id"]),),
            )
            return self._insert_event(
                binding,
                event_id=_tombstone_event_id(decision_id, revision),
                event_kind=EventKind.TOMBSTONE,
                decision_id=decision_id,
                revision=revision,
                origin_kind=DecisionOriginKind(str(row["origin_kind"])),
                content_seal=stone.event_seal(),
                payload=json.dumps(
                    stone.canonical_payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                appended_at=created_at,
            )

    def abandon_epoch(self, *, reason: str, clock: Clock = utc_now) -> DecisionStoreBinding:
        """Close a verifying epoch. Everything recorded survives; no further writes.

        Abandonment rewrites the durable anchor to include the closed status. It
        must therefore refuse a corrupt store first: otherwise a truncated suffix
        or divergent anchor could be replaced by a new anchor that agrees with the
        damaged history and the act of closing would erase the evidence.
        """
        screen_persisted_text(reason=reason)
        stamped = _stamp(clock)
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to abandon an epoch whose decision memory fails integrity "
                        "verification: re-anchoring would launder the defect",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                binding = self._binding_from(self._conn)
                if binding.epoch_status is EpochStatus.ABANDONED:
                    raise EpochClosed(
                        "the epoch is already abandoned", detail={"epoch": binding.epoch}
                    )
                self._conn.execute(
                    "UPDATE store_meta SET value = ? WHERE key = 'epoch_status'",
                    (EpochStatus.ABANDONED.value,),
                )
                self._conn.execute(
                    "INSERT INTO store_meta (key, value) "
                    "VALUES ('epoch_abandoned_reason', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"{stamped} {reason}",),
                )
                genesis = binding.genesis_seal()
                self._write_anchor(
                    self._conn,
                    genesis=genesis,
                    generation=self._generation_of(self._conn),
                    tail=self._root_seal_of(self._conn),
                    epoch_status=EpochStatus.ABANDONED,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.binding()

    # -- read path ---------------------------------------------------------

    @staticmethod
    def _active_from_row(row: sqlite3.Row, *, as_of: str, revoked: bool) -> ActiveDecision | None:
        """Project one head row into the active view, or ``None`` if excluded.

        Revocation and expiry both exclude. A tombstoned head is *not* excluded:
        it stays in the active state and reports that its payload was removed,
        because silently dropping it would rewrite what the store says happened.
        """
        if revoked:
            return None
        origin_kind = DecisionOriginKind(str(row["origin_kind"]))
        if int(row["redacted"]) == 1:
            return ActiveDecision(
                decision_id=str(row["decision_id"]),
                revision=int(row["revision"]),
                origin_kind=origin_kind,
                record_seal=str(row["content_seal"]),
                redacted=True,
            )
        record = validate_contract(
            StrategicDecisionRecord,
            _decode_payload(row),
            error=DecisionMemoryViolation,
            context="stored strategic decision record",
        )
        if as_of > record.expires_at:
            return None
        return ActiveDecision(
            decision_id=record.decision_id,
            revision=record.revision,
            origin_kind=origin_kind,
            record_seal=str(row["content_seal"]),
            redacted=False,
            captured_at=record.captured_at,
            review_due_at=record.review_due_at,
            expires_at=record.expires_at,
            review_overdue=as_of > record.review_due_at,
            record=record,
        )

    def get_active(self, decision_id: str, *, as_of: str | None = None) -> ActiveDecision:
        """The head revision of one decision, as of an explicit instant.

        Refuses with :class:`~latent_compass.errors.StoreNotFound` when the
        decision is unknown, revoked, or expired at ``as_of``: an excluded
        decision is not served as if it still held.
        """
        instant = _require_timestamp(as_of if as_of is not None else utc_now(), what="as_of")
        with _sqlite_errors(self.root):
            row = self._head_record_row(decision_id)
            if row is None:
                raise StoreNotFound(
                    f"no decision {decision_id!r} in this decision memory",
                    detail={"decision_id": decision_id},
                )
            revoked = self._revocation_row(decision_id) is not None
            active = self._active_from_row(row, as_of=instant, revoked=revoked)
        if active is None:
            raise StoreNotFound(
                f"decision {decision_id!r} is not active as of {instant}",
                detail={
                    "decision_id": decision_id,
                    "as_of": instant,
                    "reason": "revoked" if revoked else "expired",
                },
            )
        return active

    def list_active(
        self,
        *,
        as_of: str | None = None,
        origin_kind: DecisionOriginKind = DecisionOriginKind.NATIVE,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ActiveDecision, ...]:
        """Bounded active state. Refuses an unbounded page rather than serving one.

        Native and imported decisions are listed separately and never merged:
        imported content is read-only reference material, and folding it into the
        native view would make it look like local memory.
        """
        if limit < 1 or limit > MAX_DECISION_PAGE_SIZE:
            raise ContractViolation(
                f"limit must be between 1 and {MAX_DECISION_PAGE_SIZE}",
                detail={"limit": limit, "max": MAX_DECISION_PAGE_SIZE},
            )
        if offset < 0:
            raise ContractViolation("offset must not be negative", detail={"offset": offset})
        instant = _require_timestamp(as_of if as_of is not None else utc_now(), what="as_of")
        with _sqlite_errors(self.root):
            revoked_ids = {
                str(row["decision_id"])
                for row in self._conn.execute(
                    "SELECT decision_id FROM events WHERE event_kind = ?",
                    (EventKind.REVOCATION.value,),
                )
            }
            rows = self._conn.execute(
                "SELECT * FROM events AS head WHERE head.event_kind = ? AND head.origin_kind = ? "
                "AND head.revision = (SELECT MAX(peer.revision) FROM events AS peer "
                "                     WHERE peer.event_kind = head.event_kind "
                "                       AND peer.decision_id = head.decision_id) "
                "ORDER BY head.decision_id ASC",
                (EventKind.RECORD.value, origin_kind.value),
            ).fetchall()
            active: list[ActiveDecision] = []
            for row in rows:
                projected = self._active_from_row(
                    row, as_of=instant, revoked=str(row["decision_id"]) in revoked_ids
                )
                if projected is not None:
                    active.append(projected)
        return tuple(active[offset : offset + limit])

    # -- integrity ---------------------------------------------------------

    def verify(self) -> DecisionIntegrityReport:
        """Walk the whole event chain and report every defect found, by kind."""
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN")
            try:
                return self._verify_unlocked()
            finally:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")

    def _verify_unlocked(self) -> DecisionIntegrityReport:
        findings: list[DecisionIntegrityFinding] = []
        binding = self._binding_from(self._conn)
        native = binding.binding_ref().canonical_payload()
        genesis = binding.genesis_seal()
        previous = genesis
        expected_seq = 1
        generation = 0
        record_count = 0
        revoked_count = 0
        redacted_count = 0
        foreign_count = 0

        record_seals = {
            (str(row["decision_id"]), int(row["revision"])): str(row["content_seal"])
            for row in self._conn.execute(
                "SELECT decision_id, revision, content_seal FROM events WHERE event_kind = ?",
                (EventKind.RECORD.value,),
            )
        }
        record_origins = {
            (str(row["decision_id"]), int(row["revision"])): str(row["origin_kind"])
            for row in self._conn.execute(
                "SELECT decision_id, revision, origin_kind FROM events WHERE event_kind = ?",
                (EventKind.RECORD.value,),
            )
        }
        imports_by_event = {
            str(row["event_id"]): (
                str(row["source_record_seal"]),
                str(row["transfer_seal"]),
                str(row["imported_at"]),
            )
            for row in self._conn.execute(
                "SELECT event_id, source_record_seal, transfer_seal, imported_at FROM imports"
            )
        }
        redacted_flags = {
            (str(row["decision_id"]), int(row["revision"])): int(row["redacted"]) == 1
            for row in self._conn.execute(
                "SELECT decision_id, revision, redacted FROM events WHERE event_kind = ?",
                (EventKind.RECORD.value,),
            )
        }
        tombstoned = {
            (str(row["decision_id"]), int(row["revision"]))
            for row in self._conn.execute(
                "SELECT decision_id, revision FROM events WHERE event_kind = ?",
                (EventKind.TOMBSTONE.value,),
            )
        }

        for row in self._conn.execute("SELECT * FROM events ORDER BY seq ASC"):
            generation += 1
            seq = int(row["seq"])
            event_id = str(row["event_id"])
            if seq != expected_seq:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.SEQUENCE_INVALID,
                        detail=f"expected sequence {expected_seq}, found {seq}",
                    )
                )
            expected_seq = seq + 1

            if str(row["prev_chain_seal"]) != previous:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.CHAIN_BROKEN,
                        detail="recorded predecessor seal does not match the previous link",
                    )
                )
            recomputed = _chain_seal(
                seq=seq,
                event_id=event_id,
                event_kind=str(row["event_kind"]),
                decision_id=str(row["decision_id"]),
                revision=int(row["revision"]) if row["revision"] is not None else None,
                origin_kind=str(row["origin_kind"]),
                transfer_seal=str(row["transfer_seal"])
                if row["transfer_seal"] is not None
                else None,
                content_seal=str(row["content_seal"]),
                prev_chain_seal=str(row["prev_chain_seal"]),
                appended_at=str(row["appended_at"]),
            )
            if recomputed != str(row["chain_seal"]):
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.CHAIN_BROKEN,
                        detail="stored chain seal does not reproduce from this row",
                    )
                )
            previous = str(row["chain_seal"])

            try:
                kind = EventKind(str(row["event_kind"]))
            except ValueError:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.EVENT_KIND_UNKNOWN,
                        detail=f"unknown event kind {str(row['event_kind'])[:64]!r}",
                    )
                )
                continue
            if kind is EventKind.RECORD:
                record_count += 1
                try:
                    origin = DecisionOriginKind(str(row["origin_kind"]))
                except ValueError:
                    findings.append(
                        DecisionIntegrityFinding(
                            seq=seq,
                            event_id=event_id,
                            kind=DecisionIntegrityKind.ORIGIN_KIND_UNKNOWN,
                            detail="record declares an unknown origin kind",
                        )
                    )
                    origin = None
                if origin is DecisionOriginKind.FOREIGN_READ_ONLY:
                    foreign_count += 1
                    expected_import = (
                        str(row["content_seal"]),
                        str(row["transfer_seal"]),
                        str(row["appended_at"]),
                    )
                    if imports_by_event.get(event_id) != expected_import:
                        findings.append(
                            DecisionIntegrityFinding(
                                seq=seq,
                                event_id=event_id,
                                kind=DecisionIntegrityKind.IMPORT_MISMATCH,
                                detail="foreign record is not linked to matching import metadata",
                            )
                        )
                elif origin is DecisionOriginKind.NATIVE and (
                    event_id in imports_by_event or row["transfer_seal"] is not None
                ):
                    findings.append(
                        DecisionIntegrityFinding(
                            seq=seq,
                            event_id=event_id,
                            kind=DecisionIntegrityKind.IMPORT_MISMATCH,
                            detail="native record unexpectedly has import metadata",
                        )
                    )
                if int(row["redacted"]) == 1:
                    redacted_count += 1
                findings.extend(
                    self._record_findings(
                        row, native=native, record_seals=record_seals, tombstoned=tombstoned
                    )
                )
                continue
            if kind is EventKind.REVOCATION:
                revoked_count += 1
            findings.extend(
                self._referencing_event_findings(
                    row,
                    kind=kind,
                    record_seals=record_seals,
                    record_origins=record_origins,
                    redacted_flags=redacted_flags,
                )
            )

        recorded_event_ids = {
            str(row["event_id"]) for row in self._conn.execute("SELECT event_id FROM events")
        }
        for imported_event_id in sorted(set(imports_by_event) - recorded_event_ids):
            findings.append(
                DecisionIntegrityFinding(
                    event_id=imported_event_id,
                    kind=DecisionIntegrityKind.IMPORT_MISMATCH,
                    detail="import metadata refers to an event that is not recorded",
                )
            )

        findings.extend(
            self._anchor_findings(
                genesis=genesis, generation=generation, tail=previous, binding=binding
            )
        )
        return DecisionIntegrityReport(
            ok=not findings,
            generation=generation,
            record_count=record_count,
            revoked_count=revoked_count,
            redacted_count=redacted_count,
            foreign_count=foreign_count,
            root_seal=previous,
            findings=tuple(findings),
        )

    @staticmethod
    def _record_findings(
        row: sqlite3.Row,
        *,
        native: dict[str, object],
        record_seals: dict[tuple[str, int], str],
        tombstoned: set[tuple[str, int]],
    ) -> list[DecisionIntegrityFinding]:
        seq = int(row["seq"])
        event_id = str(row["event_id"])
        decision_id = str(row["decision_id"])
        revision = int(row["revision"])
        findings: list[DecisionIntegrityFinding] = []

        if int(row["redacted"]) == 1:
            if row["payload"] is not None:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.PAYLOAD_PRESENT_ON_REDACTED,
                        detail="row is marked redacted but still carries a payload",
                    )
                )
            if (decision_id, revision) not in tombstoned:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.REDACTED_WITHOUT_TOMBSTONE,
                        detail="payload removed with no tombstone event explaining it",
                    )
                )
            return findings

        if (decision_id, revision) in tombstoned:
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.TOMBSTONE_ON_UNREDACTED,
                    detail="a tombstone event exists but the revision still exposes its payload",
                )
            )
        if row["payload"] is None:
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_MISSING,
                    detail="payload absent on a row that is not marked redacted",
                )
            )
            return findings
        try:
            decoded = _decode_payload(row)
            record = validate_contract(
                StrategicDecisionRecord,
                decoded,
                error=DecisionMemoryViolation,
                context="stored strategic decision record",
            )
        except ContractViolation as exc:
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_UNPARSEABLE,
                    detail=exc.message[:400],
                )
            )
            return findings
        if record.record_seal() != str(row["content_seal"]):
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_ALTERED,
                    detail="payload does not reproduce its recorded content seal",
                )
            )
            return findings
        if (
            record.decision_id != decision_id
            or record.revision != revision
            or event_id != _record_event_id(decision_id, revision)
        ):
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.EVENT_IDENTITY_MISMATCH,
                    detail="record row identity does not match its sealed payload",
                )
            )
        if (
            str(row["origin_kind"]) == DecisionOriginKind.NATIVE.value
            and record.binding.canonical_payload() != native
        ):
            findings.append(
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.BINDING_MISMATCH,
                    detail="stored native record no longer matches the store binding",
                )
            )
        if record.revision > 1 and str(row["origin_kind"]) == DecisionOriginKind.NATIVE.value:
            expected = record_seals.get((decision_id, record.revision - 1))
            if expected is None or record.supersedes_revision_seal != expected:
                findings.append(
                    DecisionIntegrityFinding(
                        seq=seq,
                        event_id=event_id,
                        kind=DecisionIntegrityKind.REVISION_LINK_BROKEN,
                        detail="revision does not link to the seal of its predecessor",
                    )
                )
        return findings

    @staticmethod
    def _referencing_event_findings(
        row: sqlite3.Row,
        *,
        kind: EventKind,
        record_seals: dict[tuple[str, int], str],
        record_origins: dict[tuple[str, int], str],
        redacted_flags: dict[tuple[str, int], bool],
    ) -> list[DecisionIntegrityFinding]:
        seq = int(row["seq"])
        event_id = str(row["event_id"])
        key = (str(row["decision_id"]), int(row["revision"]))
        if row["payload"] is None:
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_MISSING,
                    detail=f"{kind.value} event carries no payload",
                )
            ]
        try:
            decoded = _decode_payload(row)
            event = (
                validate_contract(
                    RevocationEvent,
                    decoded,
                    error=DecisionMemoryViolation,
                    context="stored revocation event",
                )
                if kind is EventKind.REVOCATION
                else validate_contract(
                    DecisionTombstone,
                    decoded,
                    error=DecisionMemoryViolation,
                    context="stored tombstone event",
                )
            )
        except ContractViolation as exc:
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_UNPARSEABLE,
                    detail=exc.message[:400],
                )
            ]
        if event.event_seal() != str(row["content_seal"]):
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.PAYLOAD_ALTERED,
                    detail="payload does not reproduce its recorded content seal",
                )
            ]
        if isinstance(event, RevocationEvent):
            expected_event_id = _revocation_event_id(event.decision_id)
            payload_revision = event.revoked_revision
        else:
            expected_event_id = _tombstone_event_id(event.decision_id, event.revision)
            payload_revision = event.revision
        if (
            event.decision_id != key[0]
            or payload_revision != key[1]
            or event_id != expected_event_id
        ):
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.EVENT_IDENTITY_MISMATCH,
                    detail=f"{kind.value} row identity does not match its sealed payload",
                )
            ]
        if key not in record_seals:
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.ORPHAN_EVENT,
                    detail=f"{kind.value} event refers to a revision that is not recorded",
                )
            ]
        if str(row["origin_kind"]) != record_origins.get(key):
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.EVENT_IDENTITY_MISMATCH,
                    detail=f"{kind.value} provenance differs from its target revision",
                )
            ]
        if kind is EventKind.TOMBSTONE and not redacted_flags.get(key, False):
            return [
                DecisionIntegrityFinding(
                    seq=seq,
                    event_id=event_id,
                    kind=DecisionIntegrityKind.TOMBSTONE_ON_UNREDACTED,
                    detail="tombstone event refers to a revision that still exposes its payload",
                )
            ]
        return []

    def _anchor_findings(
        self, *, genesis: str, generation: int, tail: str, binding: DecisionStoreBinding
    ) -> list[DecisionIntegrityFinding]:
        """Detect a truncated suffix, which a chain alone cannot see."""
        rows = {
            str(row["key"]): str(row["value"])
            for row in self._conn.execute(
                "SELECT key, value FROM store_meta "
                "WHERE key IN ('anchor_generation','anchor_tail','anchor_seal')"
            )
        }
        if set(rows) != {"anchor_generation", "anchor_tail", "anchor_seal"}:
            return [
                DecisionIntegrityFinding(
                    kind=DecisionIntegrityKind.ANCHOR_MISSING,
                    detail="the durable anchor is absent; a truncated suffix would be invisible",
                )
            ]
        expected = _anchor_seal(
            genesis=genesis,
            generation=int(rows["anchor_generation"])
            if rows["anchor_generation"].isdigit()
            else -1,
            tail=rows["anchor_tail"],
            epoch_status=binding.epoch_status.value,
        )
        findings: list[DecisionIntegrityFinding] = []
        if expected != rows["anchor_seal"]:
            findings.append(
                DecisionIntegrityFinding(
                    kind=DecisionIntegrityKind.ANCHOR_MISMATCH,
                    detail="the anchor does not reproduce from the binding it claims to anchor",
                )
            )
        if rows["anchor_generation"] != str(generation) or rows["anchor_tail"] != tail:
            findings.append(
                DecisionIntegrityFinding(
                    kind=DecisionIntegrityKind.ANCHOR_MISMATCH,
                    detail=(
                        f"anchor records generation {rows['anchor_generation']} ending "
                        f"{rows['anchor_tail'][:23]}…, store holds {generation} ending "
                        f"{tail[:23]}…"
                    ),
                )
            )
        return findings

    # -- explicit transfer -------------------------------------------------

    def export_transfer(
        self,
        decision_id: str,
        *,
        destination: DecisionBinding,
        exported_by: str,
        clock: Clock = utc_now,
    ) -> DecisionTransferEnvelope:
        """Seal the head revision of one native decision for one named destination.

        This writes nothing. It refuses a store that does not verify, a decision
        that is unknown, revoked, redacted or itself imported, and a destination
        equal to this store's own binding.

        The resulting seal proves that the envelope is internally consistent with
        the record and root seal it names. It proves nothing about who produced
        it or when: local seals are a consistency proof, never an authenticity or
        chronology proof.
        """
        screen_persisted_text(
            decision_id=decision_id,
            exported_by=exported_by,
            destination_host_id=destination.host_id,
            destination_store_id=destination.store_id,
            destination_epoch=destination.epoch,
        )
        exported_at = _stamp(clock)
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to export from a store that fails integrity verification",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                row = self._head_record_row(decision_id)
                if row is None:
                    raise StoreNotFound(
                        f"no decision {decision_id!r} in this decision memory",
                        detail={"decision_id": decision_id},
                    )
                if str(row["origin_kind"]) != DecisionOriginKind.NATIVE.value:
                    raise TransferRefused(
                        "imported content is read-only and is never re-exported from here",
                        detail={"reason": "foreign_decision", "decision_id": decision_id},
                    )
                if self._revocation_row(decision_id) is not None:
                    raise TransferRefused(
                        "a revoked decision is not transferable",
                        detail={"reason": "revoked_decision", "decision_id": decision_id},
                    )
                if int(row["redacted"]) == 1:
                    raise TransferRefused(
                        "a redacted revision has no payload to transfer",
                        detail={"reason": "redacted_revision", "decision_id": decision_id},
                    )
                record = admit_strategic_decision(_decode_payload(row))
                root_seal = report.root_seal
            finally:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
        return build_transfer_envelope(
            record=record,
            source_root_seal=root_seal,
            destination_binding=destination,
            exported_at=exported_at,
            exported_by=exported_by,
        )

    def import_transfer(
        self,
        payload: object,
        *,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> TransferReceipt:
        """Admit one sealed envelope as ``FOREIGN_READ_ONLY`` content.

        Every one of these fails closed, before or during the transaction, and
        none of them changes record count, generation or root seal:

        * an envelope whose seal does not reproduce — tampering;
        * an envelope addressed to a different host, family, store or epoch;
        * an envelope already imported here, or one carrying a source record
          already imported under another envelope — replay;
        * a decision identity that already exists natively here — collision;
        * a carried record that would not pass native admission.

        Imported content is never native. It cannot be revised here, it is never
        re-exported, and it is listed separately from the native active view.
        """
        envelope = admit_transfer_envelope(payload)
        imported_at = _stamp(clock)
        record = envelope.record
        document = json.dumps(
            record.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with (
            _sqlite_errors(self.root),
            self._append_transaction(
                expected_generation=expected_generation, what="import a foreign decision"
            ) as binding,
        ):
            local = binding.binding_ref()
            if envelope.destination_binding != local:
                raise TransferRefused(
                    "this envelope is addressed to a different decision memory",
                    detail={
                        "reason": "destination_mismatch",
                        "destination": envelope.destination_binding.canonical_payload(),
                        "store": local.canonical_payload(),
                    },
                )
            self._require_no_native_collision(record.decision_id)
            self._require_not_replayed(envelope)
            event_id = _record_event_id(record.decision_id, record.revision)
            receipt = self._insert_event(
                binding,
                event_id=event_id,
                event_kind=EventKind.RECORD,
                decision_id=record.decision_id,
                revision=record.revision,
                origin_kind=DecisionOriginKind.FOREIGN_READ_ONLY,
                transfer_seal=envelope.transfer_seal,
                content_seal=envelope.source_record_seal,
                payload=document,
                appended_at=imported_at,
            )
            self._conn.execute(
                "INSERT INTO imports "
                "(transfer_seal, source_record_seal, event_id, imported_at) "
                "VALUES (?, ?, ?, ?)",
                (envelope.transfer_seal, envelope.source_record_seal, event_id, imported_at),
            )
        return TransferReceipt(
            transfer_seal=envelope.transfer_seal,
            source_record_seal=envelope.source_record_seal,
            source_binding=envelope.source_binding,
            destination_binding=envelope.destination_binding,
            append=receipt,
        )

    def _require_no_native_collision(self, decision_id: str) -> None:
        row = self._conn.execute(
            "SELECT origin_kind FROM events WHERE event_kind = ? AND decision_id = ? LIMIT 1",
            (EventKind.RECORD.value, decision_id),
        ).fetchone()
        if row is None:
            return
        raise TransferRefused(
            "a decision with this identity already exists here and is never shadowed",
            detail={
                "reason": "identity_collision",
                "decision_id": decision_id,
                "existing_origin_kind": str(row["origin_kind"]),
            },
        )

    def _require_not_replayed(self, envelope: DecisionTransferEnvelope) -> None:
        row = self._conn.execute(
            "SELECT transfer_seal FROM imports WHERE transfer_seal = ? OR source_record_seal = ?",
            (envelope.transfer_seal, envelope.source_record_seal),
        ).fetchone()
        if row is not None:
            raise TransferRefused(
                "this transfer has already been imported here",
                detail={
                    "reason": "replayed_transfer",
                    "transfer_seal": envelope.transfer_seal,
                    "source_record_seal": envelope.source_record_seal,
                },
            )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _decode_payload(row: sqlite3.Row) -> object:
    """Decode a stored payload, refusing bad bytes with a typed error."""
    raw = row["payload"]
    try:
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8")
        return json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DecisionMemoryViolation(
            "stored payload is not decodable JSON",
            detail={"event_id": str(row["event_id"]), "cause": type(exc).__name__},
        ) from exc


@contextmanager
def _sqlite_errors(root: Path) -> Iterator[None]:
    """Translate raw SQLite and pydantic failures into the typed vocabulary.

    The order matters. ``OperationalError`` and ``IntegrityError`` are both
    ``DatabaseError`` subclasses but mean "the operation could not run", not
    "the file is corrupt", so they map to a store error. The bare
    ``DatabaseError`` — "file is not a database", "disk image is malformed" — is
    the corruption case and maps to an integrity error.
    """
    try:
        yield
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        raise LedgerError(
            "the decision memory database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(
            "the decision memory database is unreadable or not a decision memory",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.Error as exc:
        raise LedgerError(
            "the decision memory database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except ValidationError as exc:
        raise ContractViolation(
            "stored decision memory data does not satisfy its contract",
            detail={"root": str(root)},
        ) from exc

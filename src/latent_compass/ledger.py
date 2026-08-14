"""HOK-187 — the shadow-only episode ledger.

A store is a local SQLite database bound at creation to exactly one host, one
agent family and one epoch. It records episodes and nothing else. It never
executes a recommendation, never contacts a network, and never touches any file
outside its own root.

Append-only at the contract level
---------------------------------
There is no update and no delete in the write path. The only mutation offered
is :meth:`LedgerStore.tombstone`, which drops a payload while preserving the
row, its content seal, its chain link and the root seal — and which refuses
outright on a store that does not verify, so a redaction can never launder a
corruption. See :mod:`latent_compass.governance`.

Integrity chain and anchor
--------------------------
Each row links to its predecessor::

    chain_seal(0) = seal(ledger_format_version, store_id, host_id, agent_family, epoch)
    chain_seal(n) = seal(seq, episode_id, content_seal, chain_seal(n-1), appended_at)

``chain_seal(0)`` is **recomputed from the binding at verification time**, never
read back from storage, so relabelling ``host_id`` in ``store_meta`` breaks the
chain instead of renaming the store's history.

A chain alone cannot notice that its *last* rows were removed: the shorter
chain still verifies. A durable **anchor** — the expected episode count, the
expected tail seal and the epoch status — is therefore written inside the same
transaction as each append, and checked by :meth:`LedgerStore.verify`.

**What this does not prove.** A hash chain plus an anchor detects tampering by
anyone who cannot rewrite *both*. It establishes no authenticity: an
administrator with write access can recompute the chain and the anchor together
from a forged history and produce a store that verifies perfectly. Detecting
that requires an external anchor this package does not have — a co-signed
digest, a remote witness, or genuinely append-only storage. This limit is
intrinsic to the threat model, not an implementation gap.

Atomicity
---------
Every append runs inside a single ``BEGIN IMMEDIATE`` transaction that also
re-reads the binding, the epoch status and the tail. Validation of the episode,
of the clock output and of the receipt all happen **before** the commit, so no
failure can occur after a row is durable. An interruption before commit leaves
no trace: no row, no burned sequence number, an unchanged root seal.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Final

from pydantic import Field, ValidationError

from latent_compass.canonical import seal
from latent_compass.contracts import (
    LEDGER_FORMAT_VERSION,
    SUPPORTED_LEDGER_FORMATS,
    Identifier,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import (
    AgentFamily,
    Episode,
    episode_content_seal,
    load_episode,
)
from latent_compass.errors import (
    ContractViolation,
    DuplicateEpisode,
    EpochClosed,
    IntegrityError,
    LedgerError,
    ProvenanceMismatch,
    StoreAlreadyExists,
    StoreNotFound,
)
from latent_compass.governance import DeletionMode, Tombstone

__all__ = [
    "DATABASE_FILENAME",
    "MAX_PAGE_SIZE",
    "AppendReceipt",
    "Clock",
    "EpochStatus",
    "ExportDocument",
    "IntegrityFinding",
    "IntegrityKind",
    "IntegrityReport",
    "LedgerRecord",
    "LedgerStore",
    "ReplayReport",
    "StoreBinding",
    "utc_now",
]

DATABASE_FILENAME: Final = "ledger.sqlite3"
MAX_PAGE_SIZE: Final = 1000

GENESIS_SEAL_DOMAIN: Final = "ledger.genesis"
CHAIN_SEAL_DOMAIN: Final = "ledger.chain"
REPLAY_SEAL_DOMAIN: Final = "ledger.replay"
EXPORT_SEAL_DOMAIN: Final = "ledger.export"
ANCHOR_SEAL_DOMAIN: Final = "ledger.anchor"

EXPORT_FORMAT_VERSION: Final = "1.0.0"

#: A callable returning an ISO-8601 UTC timestamp ending in ``Z``. Injectable so
#: that determinism proofs do not depend on wall-clock time. Its output is
#: validated before use: an injected clock is untrusted input like any other.
Clock = Callable[[], str]

_BINDING_KEYS: Final = (
    "ledger_format_version",
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
CREATE TABLE episodes (
    seq             INTEGER PRIMARY KEY NOT NULL,
    episode_id      TEXT NOT NULL UNIQUE,
    content_seal    TEXT NOT NULL,
    prev_chain_seal TEXT NOT NULL,
    chain_seal      TEXT NOT NULL UNIQUE,
    payload         TEXT,
    redacted        INTEGER NOT NULL DEFAULT 0,
    appended_at     TEXT NOT NULL
) STRICT;
""",
    """
CREATE TABLE tombstones (
    episode_id TEXT PRIMARY KEY NOT NULL REFERENCES episodes(episode_id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    mode       TEXT NOT NULL
) STRICT;
""",
)


def utc_now() -> str:
    """Current time as an ISO-8601 UTC timestamp, second precision, ``Z``."""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Stamp(StrictModel):
    """Validates a clock's output against the canonical timestamp contract."""

    at: Timestamp


def _stamp(clock: Clock) -> str:
    """Call ``clock`` and refuse anything that is not a canonical timestamp."""
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


class EpochStatus(StrEnum):
    OPEN = "open"
    ABANDONED = "abandoned"


class IntegrityKind(StrEnum):
    """What kind of defect was found. Never collapsed into a single boolean."""

    SEQUENCE_INVALID = "SEQUENCE_INVALID"
    CHAIN_BROKEN = "CHAIN_BROKEN"
    PAYLOAD_ALTERED = "PAYLOAD_ALTERED"
    PAYLOAD_UNPARSEABLE = "PAYLOAD_UNPARSEABLE"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_PRESENT_ON_REDACTED = "PAYLOAD_PRESENT_ON_REDACTED"
    REDACTED_WITHOUT_TOMBSTONE = "REDACTED_WITHOUT_TOMBSTONE"
    TOMBSTONE_INVALID = "TOMBSTONE_INVALID"
    TOMBSTONE_ON_UNREDACTED = "TOMBSTONE_ON_UNREDACTED"
    ORPHAN_TOMBSTONE = "ORPHAN_TOMBSTONE"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
    ANCHOR_MISSING = "ANCHOR_MISSING"


class StoreBinding(StrictModel):
    """The identity a store is bound to at creation, and never after."""

    ledger_format_version: str = Field(min_length=5, max_length=20)
    store_id: Identifier
    host_id: Identifier
    agent_family: AgentFamily
    epoch: Identifier
    epoch_status: EpochStatus
    created_at: Timestamp

    def genesis_seal(self) -> str:
        """Recomputed from identity, never read back from storage."""
        return seal(
            GENESIS_SEAL_DOMAIN,
            {
                "ledger_format_version": self.ledger_format_version,
                "store_id": self.store_id,
                "host_id": self.host_id,
                "agent_family": self.agent_family.value,
                "epoch": self.epoch,
            },
        )


class AppendReceipt(StrictModel):
    """Proof that one episode was accepted, and where it landed."""

    seq: int = Field(ge=1)
    episode_id: Identifier
    content_seal: Annotated[str, Field(min_length=1, max_length=200)]
    chain_seal: Annotated[str, Field(min_length=1, max_length=200)]
    root_seal: Annotated[str, Field(min_length=1, max_length=200)]
    appended_at: Timestamp


class LedgerRecord(StrictModel):
    """One stored row, with its episode when it has not been redacted."""

    seq: int = Field(ge=1)
    episode_id: Identifier
    content_seal: Annotated[str, Field(min_length=1, max_length=200)]
    prev_chain_seal: Annotated[str, Field(min_length=1, max_length=200)]
    chain_seal: Annotated[str, Field(min_length=1, max_length=200)]
    appended_at: Timestamp
    redacted: bool
    episode: Episode | None = Field(default=None)
    tombstone: Tombstone | None = Field(default=None)


class IntegrityFinding(StrictModel):
    """One defect, located and named."""

    seq: int | None = Field(default=None, ge=1)
    episode_id: str | None = Field(default=None)
    kind: IntegrityKind
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class IntegrityReport(StrictModel):
    """Result of a full chain walk."""

    ok: bool
    checked: int = Field(ge=0)
    redacted: int = Field(ge=0)
    root_seal: Annotated[str, Field(min_length=1, max_length=200)]
    findings: tuple[IntegrityFinding, ...] = Field(default=())


class ReplayReport(StrictModel):
    """Result of a deterministic replay from genesis."""

    store_id: Identifier
    epoch: Identifier
    episode_count: int = Field(ge=0)
    redacted_count: int = Field(ge=0)
    replayed_episode_ids: tuple[Identifier, ...] = Field(default=())
    redacted_episode_ids: tuple[Identifier, ...] = Field(default=())
    root_seal: Annotated[str, Field(min_length=1, max_length=200)]
    replay_digest: Annotated[str, Field(min_length=1, max_length=200)]


class ExportDocument(StrictModel):
    """A sealed, self-describing snapshot of a store."""

    export_format_version: str = Field(default=EXPORT_FORMAT_VERSION, min_length=5, max_length=20)
    store: StoreBinding
    root_seal: Annotated[str, Field(min_length=1, max_length=200)]
    episode_count: int = Field(ge=0)
    records: tuple[dict[str, object], ...] = Field(default=())
    tombstones: tuple[Tombstone, ...] = Field(default=())
    export_seal: Annotated[str, Field(min_length=1, max_length=200)]


def _chain_seal(
    *, seq: int, episode_id: str, content_seal: str, prev_chain_seal: str, appended_at: str
) -> str:
    return seal(
        CHAIN_SEAL_DOMAIN,
        {
            "seq": seq,
            "episode_id": episode_id,
            "content_seal": content_seal,
            "prev_chain_seal": prev_chain_seal,
            "appended_at": appended_at,
        },
    )


def _anchor_seal(*, genesis: str, count: int, tail: str, epoch_status: str) -> str:
    return seal(
        ANCHOR_SEAL_DOMAIN,
        {"genesis": genesis, "count": count, "tail": tail, "epoch_status": epoch_status},
    )


class LedgerStore:
    """A local, single-host, append-only episode store."""

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
    ) -> LedgerStore:
        """Create a store bound to one host, one agent family and one epoch.

        The database file is claimed with ``O_CREAT | O_EXCL``, which is the
        mutual exclusion: a concurrent loser never gets the file, so it can
        never delete the winner's store on its way out. Only the process that
        actually created the file will unlink it on failure.
        """
        root_path = Path(root)
        database = root_path / DATABASE_FILENAME
        binding = validate_contract(
            StoreBinding,
            {
                "ledger_format_version": LEDGER_FORMAT_VERSION,
                "store_id": store_id,
                "host_id": host_id,
                "agent_family": agent_family,
                "epoch": epoch,
                "epoch_status": EpochStatus.OPEN,
                "created_at": _stamp(clock),
            },
            error=ContractViolation,
            context="store binding",
        )
        genesis = binding.genesis_seal()
        meta = {key: str(value) for key, value in binding.model_dump(mode="json").items()}
        meta["anchor_seal"] = _anchor_seal(
            genesis=genesis, count=0, tail=genesis, epoch_status=EpochStatus.OPEN.value
        )
        meta["anchor_count"] = "0"
        meta["anchor_tail"] = genesis

        root_path.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(database), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise StoreAlreadyExists(
                f"a ledger already exists at {database}", detail={"root": str(root_path)}
            ) from exc
        except OSError as exc:
            raise LedgerError(
                f"cannot create a ledger at {database}",
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
            # Safe: this process is the one that created the file, proven by the
            # exclusive open above.
            database.unlink(missing_ok=True)
            raise
        return cls(root_path, connection)

    @classmethod
    def open(cls, root: Path | str) -> LedgerStore:
        """Open an existing store, refusing an unknown ledger format."""
        root_path = Path(root)
        database = root_path / DATABASE_FILENAME
        if not database.exists():
            raise StoreNotFound(f"no ledger at {database}", detail={"root": str(root_path)})
        connection = cls._connect(database)
        store = cls(root_path, connection)
        try:
            with _sqlite_errors(root_path):
                check_contract_version(
                    store._meta("ledger_format_version"), SUPPORTED_LEDGER_FORMATS, "ledger"
                )
        except BaseException:
            connection.close()
            raise
        return store

    @staticmethod
    def _connect(database: Path) -> sqlite3.Connection:
        # The pragmas touch the file, so a non-database or a truncated file
        # fails here rather than on the first query. Same classification as
        # `_sqlite_errors`: "could not run" is a store error, "this is not a
        # readable database" is an integrity error.
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

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- binding -----------------------------------------------------------

    def _meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise LedgerError(f"store metadata is missing {key!r}", detail={"key": key})
        return str(row["value"])

    def _binding_from(self, connection: sqlite3.Connection) -> StoreBinding:
        rows = connection.execute(
            "SELECT key, value FROM store_meta WHERE key IN "
            "('ledger_format_version','store_id','host_id','agent_family','epoch',"
            " 'epoch_status','created_at')"
        ).fetchall()
        payload = {row["key"]: row["value"] for row in rows}
        missing = sorted(set(_BINDING_KEYS) - set(payload))
        if missing:
            raise LedgerError("store metadata is incomplete", detail={"missing": missing})
        return validate_contract(
            StoreBinding, payload, error=ContractViolation, context="store binding"
        )

    def binding(self) -> StoreBinding:
        """The immutable identity this store is bound to."""
        with _sqlite_errors(self.root):
            return self._binding_from(self._conn)

    def root_seal(self) -> str:
        """Seal of the last chain link, or the genesis seal for an empty store."""
        with _sqlite_errors(self.root):
            row = self._conn.execute(
                "SELECT chain_seal FROM episodes ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return self._binding_from(self._conn).genesis_seal()
            return str(row["chain_seal"])

    # -- write path --------------------------------------------------------

    @staticmethod
    def _check_admissible(episode: Episode, binding: StoreBinding) -> None:
        if binding.epoch_status is EpochStatus.ABANDONED:
            raise EpochClosed(
                "the epoch of this store was abandoned; no further appends are accepted",
                detail={"store_id": binding.store_id, "epoch": binding.epoch},
            )
        provenance = episode.provenance
        for field, expected, received in (
            ("host_id", binding.host_id, provenance.host_id),
            ("agent_family", binding.agent_family.value, provenance.agent_family.value),
            ("store_id", binding.store_id, provenance.store_id),
            ("epoch", binding.epoch, provenance.epoch),
        ):
            if expected != received:
                raise ProvenanceMismatch(
                    f"episode {field} does not match this store",
                    detail={
                        "field": field,
                        "store": expected,
                        "episode": received,
                        "episode_id": episode.episode_id,
                    },
                )

    def append(
        self,
        episode: Episode,
        *,
        clock: Clock = utc_now,
    ) -> AppendReceipt:
        """Append one episode atomically.

        The episode is **revalidated from its own canonical payload** first:
        ``Episode`` instances can be produced by ``model_copy(update=...)``,
        which bypasses validation entirely, so an in-memory instance is not
        proof of a valid episode.

        The clock output, the chain link and the whole receipt are all
        constructed and validated *before* the commit, so there is no failure
        mode that leaves a durable row behind a raised exception. Binding, epoch
        status and tail are re-read **inside** the transaction, so a concurrent
        ``abandon_epoch`` cannot be overtaken by a check made before it.

        """
        revalidated = load_episode(episode.canonical_payload())
        content_seal = revalidated.content_seal()
        payload = json.dumps(
            revalidated.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        appended_at = _stamp(clock)

        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to append to a store that fails integrity verification",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                binding = self._binding_from(self._conn)
                self._check_admissible(revalidated, binding)

                tail = self._conn.execute(
                    "SELECT seq, chain_seal FROM episodes ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if tail is None:
                    seq = 1
                    prev_chain_seal = binding.genesis_seal()
                else:
                    seq = int(tail["seq"]) + 1
                    prev_chain_seal = str(tail["chain_seal"])

                chain_seal = _chain_seal(
                    seq=seq,
                    episode_id=revalidated.episode_id,
                    content_seal=content_seal,
                    prev_chain_seal=prev_chain_seal,
                    appended_at=appended_at,
                )
                # Built before the commit: nothing that can fail runs after it.
                receipt = AppendReceipt(
                    seq=seq,
                    episode_id=revalidated.episode_id,
                    content_seal=content_seal,
                    chain_seal=chain_seal,
                    root_seal=chain_seal,
                    appended_at=appended_at,
                )
                try:
                    self._conn.execute(
                        "INSERT INTO episodes "
                        "(seq, episode_id, content_seal, prev_chain_seal, chain_seal, "
                        " payload, redacted, appended_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            seq,
                            revalidated.episode_id,
                            content_seal,
                            prev_chain_seal,
                            chain_seal,
                            payload,
                            appended_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateEpisode(
                        f"episode {revalidated.episode_id!r} is already present in this store",
                        detail={"episode_id": revalidated.episode_id},
                    ) from exc

                self._write_anchor(
                    self._conn,
                    genesis=binding.genesis_seal(),
                    count=seq,
                    tail=chain_seal,
                    epoch_status=binding.epoch_status,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return receipt

    @staticmethod
    def _write_anchor(
        connection: sqlite3.Connection,
        *,
        genesis: str,
        count: int,
        tail: str,
        epoch_status: EpochStatus,
    ) -> None:
        values = {
            "anchor_count": str(count),
            "anchor_tail": tail,
            "anchor_seal": _anchor_seal(
                genesis=genesis, count=count, tail=tail, epoch_status=epoch_status.value
            ),
        }
        connection.executemany(
            "INSERT INTO store_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            sorted(values.items()),
        )

    def tombstone(self, episode_id: str, *, reason: str, clock: Clock = utc_now) -> Tombstone:
        """Redact one payload while preserving the row, the chain and the root seal.

        Refused outright on a store that does not verify: a redaction must never
        be usable to make an existing corruption disappear.
        """
        created_at = _stamp(clock)
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to redact a store that fails integrity verification",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                row = self._conn.execute(
                    "SELECT redacted FROM episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                if row is None:
                    raise StoreNotFound(
                        f"no episode {episode_id!r} in this store",
                        detail={"episode_id": episode_id},
                    )
                if int(row["redacted"]) == 1:
                    raise LedgerError(
                        f"episode {episode_id!r} is already redacted",
                        detail={"episode_id": episode_id},
                    )
                record = Tombstone(
                    episode_id=episode_id,
                    reason=reason,
                    created_at=created_at,
                    mode=DeletionMode.TOMBSTONE,
                )
                self._conn.execute(
                    "UPDATE episodes SET payload = NULL, redacted = 1 WHERE episode_id = ?",
                    (episode_id,),
                )
                self._conn.execute(
                    "INSERT INTO tombstones (episode_id, reason, created_at, mode) "
                    "VALUES (?, ?, ?, ?)",
                    (episode_id, reason, record.created_at, record.mode.value),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return record

    def abandon_epoch(self, *, reason: str, clock: Clock = utc_now) -> StoreBinding:
        """Close the epoch. Existing rows survive; no further appends are accepted."""
        stamped = _stamp(clock)
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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
                tail = self._conn.execute(
                    "SELECT chain_seal FROM episodes ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                count = int(
                    self._conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
                )
                genesis = binding.genesis_seal()
                self._write_anchor(
                    self._conn,
                    genesis=genesis,
                    count=count,
                    tail=str(tail["chain_seal"]) if tail is not None else genesis,
                    epoch_status=EpochStatus.ABANDONED,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self.binding()

    # -- read path ---------------------------------------------------------

    def _record(self, row: sqlite3.Row) -> LedgerRecord:
        redacted = int(row["redacted"]) == 1
        episode: Episode | None = None
        tombstone: Tombstone | None = None
        if redacted:
            stone = self._conn.execute(
                "SELECT episode_id, reason, created_at, mode FROM tombstones WHERE episode_id = ?",
                (row["episode_id"],),
            ).fetchone()
            if stone is not None:
                tombstone = validate_contract(
                    Tombstone, dict(stone), error=ContractViolation, context="tombstone"
                )
        elif row["payload"] is not None:
            episode = load_episode(_decode_payload(row))
        return LedgerRecord(
            seq=int(row["seq"]),
            episode_id=str(row["episode_id"]),
            content_seal=str(row["content_seal"]),
            prev_chain_seal=str(row["prev_chain_seal"]),
            chain_seal=str(row["chain_seal"]),
            appended_at=str(row["appended_at"]),
            redacted=redacted,
            episode=episode,
            tombstone=tombstone,
        )

    def get(self, episode_id: str) -> LedgerRecord:
        with _sqlite_errors(self.root):
            row = self._conn.execute(
                "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"no episode {episode_id!r} in this store", detail={"episode_id": episode_id}
                )
            return self._record(row)

    def list_records(self, *, limit: int = 50, offset: int = 0) -> tuple[LedgerRecord, ...]:
        """Bounded read. Refuses an unbounded page rather than serving one."""
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise ContractViolation(
                f"limit must be between 1 and {MAX_PAGE_SIZE}",
                detail={"limit": limit, "max": MAX_PAGE_SIZE},
            )
        if offset < 0:
            raise ContractViolation("offset must not be negative", detail={"offset": offset})
        with _sqlite_errors(self.root):
            rows = self._conn.execute(
                "SELECT * FROM episodes ORDER BY seq ASC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return tuple(self._record(row) for row in rows)

    def count(self) -> int:
        with _sqlite_errors(self.root):
            row = self._conn.execute("SELECT COUNT(*) AS total FROM episodes").fetchone()
            return int(row["total"])

    def _iter_rows(self) -> Iterator[sqlite3.Row]:
        yield from self._conn.execute("SELECT * FROM episodes ORDER BY seq ASC")

    # -- integrity ---------------------------------------------------------

    def verify(self) -> IntegrityReport:
        """Walk the whole chain and report every defect found, by kind."""
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN")
            try:
                return self._verify_unlocked()
            finally:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")

    def _verify_unlocked(self) -> IntegrityReport:
        findings: list[IntegrityFinding] = []
        binding = self._binding_from(self._conn)
        genesis = binding.genesis_seal()
        previous = genesis
        expected_seq = 1
        checked = 0
        redacted_count = 0
        tombstone_rows = tuple(
            self._conn.execute(
                "SELECT episode_id, reason, created_at, mode FROM tombstones ORDER BY episode_id"
            )
        )
        tombstone_ids = {str(row["episode_id"]) for row in tombstone_rows}
        tombstoned: set[str] = set()
        for row in tombstone_rows:
            episode_id = str(row["episode_id"])
            try:
                validate_contract(
                    Tombstone, dict(row), error=ContractViolation, context="tombstone"
                )
            except ContractViolation as exc:
                findings.append(
                    IntegrityFinding(
                        episode_id=episode_id,
                        kind=IntegrityKind.TOMBSTONE_INVALID,
                        detail=exc.message[:400],
                    )
                )
            else:
                tombstoned.add(episode_id)
        seen: set[str] = set()

        for row in self._iter_rows():
            checked += 1
            seq = int(row["seq"])
            episode_id = str(row["episode_id"])
            seen.add(episode_id)
            redacted = int(row["redacted"]) == 1

            if seq != expected_seq:
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.SEQUENCE_INVALID,
                        detail=f"expected sequence {expected_seq}, found {seq}",
                    )
                )
            expected_seq = seq + 1

            if str(row["prev_chain_seal"]) != previous:
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.CHAIN_BROKEN,
                        detail="recorded predecessor seal does not match the previous link",
                    )
                )
            recomputed = _chain_seal(
                seq=seq,
                episode_id=episode_id,
                content_seal=str(row["content_seal"]),
                prev_chain_seal=str(row["prev_chain_seal"]),
                appended_at=str(row["appended_at"]),
            )
            if recomputed != str(row["chain_seal"]):
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.CHAIN_BROKEN,
                        detail="stored chain seal does not reproduce from this row",
                    )
                )
            previous = str(row["chain_seal"])

            if redacted:
                redacted_count += 1
                if row["payload"] is not None:
                    findings.append(
                        IntegrityFinding(
                            seq=seq,
                            episode_id=episode_id,
                            kind=IntegrityKind.PAYLOAD_PRESENT_ON_REDACTED,
                            detail="row is marked redacted but still carries a payload",
                        )
                    )
                if episode_id not in tombstoned:
                    findings.append(
                        IntegrityFinding(
                            seq=seq,
                            episode_id=episode_id,
                            kind=IntegrityKind.REDACTED_WITHOUT_TOMBSTONE,
                            detail="payload removed with no tombstone explaining it",
                        )
                    )
                continue

            if episode_id in tombstoned:
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.TOMBSTONE_ON_UNREDACTED,
                        detail="a tombstone exists but the episode still exposes its payload",
                    )
                )

            if row["payload"] is None:
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.PAYLOAD_MISSING,
                        detail="payload absent on a row that is not marked redacted",
                    )
                )
                continue
            try:
                decoded = _decode_payload(row)
            except ContractViolation as exc:
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.PAYLOAD_UNPARSEABLE,
                        detail=exc.message[:400],
                    )
                )
                continue
            if episode_content_seal(decoded) != str(row["content_seal"]):
                findings.append(
                    IntegrityFinding(
                        seq=seq,
                        episode_id=episode_id,
                        kind=IntegrityKind.PAYLOAD_ALTERED,
                        detail="payload does not reproduce its recorded content seal",
                    )
                )
                continue
            findings.extend(self._provenance_findings(seq, episode_id, decoded, binding))

        for orphan in sorted(tombstone_ids - seen):
            findings.append(
                IntegrityFinding(
                    episode_id=orphan,
                    kind=IntegrityKind.ORPHAN_TOMBSTONE,
                    detail="tombstone refers to an episode that is not in the ledger",
                )
            )

        findings.extend(
            self._anchor_findings(genesis=genesis, count=checked, tail=previous, binding=binding)
        )
        return IntegrityReport(
            ok=not findings,
            checked=checked,
            redacted=redacted_count,
            root_seal=previous,
            findings=tuple(findings),
        )

    @staticmethod
    def _provenance_findings(
        seq: int, episode_id: str, decoded: object, binding: StoreBinding
    ) -> list[IntegrityFinding]:
        """Every stored payload must still belong to this store's identity."""
        if not isinstance(decoded, dict):  # pragma: no cover - seal check already passed
            return []
        provenance = decoded.get("provenance")
        if not isinstance(provenance, dict):  # pragma: no cover - schema guarantees it
            return []
        mismatches = [
            field
            for field, expected in (
                ("host_id", binding.host_id),
                ("agent_family", binding.agent_family.value),
                ("store_id", binding.store_id),
                ("epoch", binding.epoch),
            )
            if provenance.get(field) != expected
        ]
        if not mismatches:
            return []
        return [
            IntegrityFinding(
                seq=seq,
                episode_id=episode_id,
                kind=IntegrityKind.PROVENANCE_MISMATCH,
                detail="stored episode provenance no longer matches the store binding: "
                + ", ".join(mismatches),
            )
        ]

    def _anchor_findings(
        self, *, genesis: str, count: int, tail: str, binding: StoreBinding
    ) -> list[IntegrityFinding]:
        """Detect a truncated suffix, which a chain alone cannot see."""
        rows = {
            str(row["key"]): str(row["value"])
            for row in self._conn.execute(
                "SELECT key, value FROM store_meta "
                "WHERE key IN ('anchor_count','anchor_tail','anchor_seal')"
            )
        }
        if set(rows) != {"anchor_count", "anchor_tail", "anchor_seal"}:
            return [
                IntegrityFinding(
                    kind=IntegrityKind.ANCHOR_MISSING,
                    detail="the durable anchor is absent; a truncated suffix would be invisible",
                )
            ]
        expected = _anchor_seal(
            genesis=genesis,
            count=int(rows["anchor_count"]) if rows["anchor_count"].isdigit() else -1,
            tail=rows["anchor_tail"],
            epoch_status=binding.epoch_status.value,
        )
        findings: list[IntegrityFinding] = []
        if expected != rows["anchor_seal"]:
            findings.append(
                IntegrityFinding(
                    kind=IntegrityKind.ANCHOR_MISMATCH,
                    detail="the anchor does not reproduce from the binding it claims to anchor",
                )
            )
        if rows["anchor_count"] != str(count) or rows["anchor_tail"] != tail:
            findings.append(
                IntegrityFinding(
                    kind=IntegrityKind.ANCHOR_MISMATCH,
                    detail=(
                        f"anchor records {rows['anchor_count']} episode(s) ending "
                        f"{rows['anchor_tail'][:23]}…, ledger holds {count} ending {tail[:23]}…"
                    ),
                )
            )
        return findings

    def replay(self) -> ReplayReport:
        """Replay from genesis, refusing to replay a store that does not verify."""
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to replay a store that fails integrity verification",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                binding = self._binding_from(self._conn)
                replayed: list[str] = []
                redacted: list[str] = []
                links: list[dict[str, object]] = []
                previous = binding.genesis_seal()

                for row in self._iter_rows():
                    episode_id = str(row["episode_id"])
                    if int(row["redacted"]) == 1:
                        redacted.append(episode_id)
                    else:
                        load_episode(_decode_payload(row))
                        replayed.append(episode_id)
                    links.append(
                        {
                            "seq": int(row["seq"]),
                            "episode_id": episode_id,
                            "content_seal": str(row["content_seal"]),
                            "redacted": int(row["redacted"]) == 1,
                        }
                    )
                    previous = str(row["chain_seal"])

                return ReplayReport(
                    store_id=binding.store_id,
                    epoch=binding.epoch,
                    episode_count=len(links),
                    redacted_count=len(redacted),
                    replayed_episode_ids=tuple(replayed),
                    redacted_episode_ids=tuple(redacted),
                    root_seal=previous,
                    replay_digest=seal(
                        REPLAY_SEAL_DOMAIN,
                        {
                            "store_id": binding.store_id,
                            "epoch": binding.epoch,
                            "genesis_seal": binding.genesis_seal(),
                            "links": links,
                        },
                    ),
                )
            finally:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")

    # -- export ------------------------------------------------------------

    def export(self) -> ExportDocument:
        """Verify, then produce a sealed snapshot of one consistent state.

        The verification and the read run inside a single ``BEGIN IMMEDIATE``
        transaction, so a concurrent append cannot land between "this store
        verifies" and "these are its rows". A store that does not verify
        produces **no** export seal at all.
        """
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to export a store that fails integrity verification",
                        detail={
                            "findings": [finding.canonical_payload() for finding in report.findings]
                        },
                    )
                binding = self._binding_from(self._conn)
                root_seal = report.root_seal
                records: list[dict[str, object]] = []
                for row in self._iter_rows():
                    payload = row["payload"]
                    records.append(
                        {
                            "seq": int(row["seq"]),
                            "episode_id": str(row["episode_id"]),
                            "content_seal": str(row["content_seal"]),
                            "prev_chain_seal": str(row["prev_chain_seal"]),
                            "chain_seal": str(row["chain_seal"]),
                            "appended_at": str(row["appended_at"]),
                            "redacted": int(row["redacted"]) == 1,
                            "payload": _decode_payload(row) if payload is not None else None,
                        }
                    )
                tombstones = tuple(
                    validate_contract(
                        Tombstone, dict(row), error=ContractViolation, context="tombstone"
                    )
                    for row in self._conn.execute(
                        "SELECT episode_id, reason, created_at, mode FROM tombstones "
                        "ORDER BY episode_id ASC"
                    )
                )
            finally:
                self._conn.execute("ROLLBACK")

        body = {
            "export_format_version": EXPORT_FORMAT_VERSION,
            "store": binding.canonical_payload(),
            "root_seal": root_seal,
            "episode_count": len(records),
            "records": records,
            "tombstones": [stone.canonical_payload() for stone in tombstones],
        }
        return ExportDocument(
            store=binding,
            root_seal=root_seal,
            episode_count=len(records),
            records=tuple(records),
            tombstones=tombstones,
            export_seal=seal(EXPORT_SEAL_DOMAIN, body),
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
        raise ContractViolation(
            "stored payload is not decodable JSON",
            detail={"episode_id": str(row["episode_id"]), "cause": type(exc).__name__},
        ) from exc


@contextmanager
def _sqlite_errors(root: Path) -> Iterator[None]:
    """Translate raw SQLite and pydantic failures into the typed vocabulary.

    Without this, a truncated or non-SQLite file surfaces
    ``sqlite3.DatabaseError`` straight out of the API, which no documented exit
    code covers and which reaches the user as a traceback.

    The order matters. ``OperationalError`` and ``IntegrityError`` are both
    ``DatabaseError`` subclasses but mean "the operation could not run", not
    "the file is corrupt", so they map to a store error. The bare
    ``DatabaseError`` — "file is not a database", "disk image is malformed" —
    is the corruption case and maps to an integrity error.
    """
    try:
        yield
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        raise LedgerError(
            "the ledger database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(
            "the ledger database is unreadable or not a ledger",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.Error as exc:
        raise LedgerError(
            "the ledger database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except ValidationError as exc:
        raise ContractViolation(
            "stored ledger data does not satisfy its contract", detail={"root": str(root)}
        ) from exc

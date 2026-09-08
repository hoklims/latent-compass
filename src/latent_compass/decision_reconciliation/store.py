"""HOK-244 — the durable post-action reconciliation journal.

A journal is a local SQLite database, separate from both the episode ledger and
the decision memory, bound at creation to exactly one host, one agent family, one
store id and one epoch. It records observations about decisions that live in
*another* store, and it writes nothing back to them. It never executes anything,
never selects a route, never authorises anything and contacts no network. Creation
and opening refuse any pre-existing symlink or reparse redirection in the root.

Adjacent, never inside
----------------------
The pre-action record stays exactly where HOK-243 put it, byte-identical, with its
seal untouched. A reconciliation names it — binding, decision id, pre-action
revision, record seal, projection seal — and that reference is the whole coupling.
No decision gains an outcome field, and the HOK-243 event chain gains no event
kind. The two stores can be verified, backed up, retained and destroyed
independently, which is the point: an observation about a decision must not be
able to invalidate the decision's own history.

Append-only, with an event chain
--------------------------------
There is no update and no delete in the write path; a correction and a
disagreement are both *appends*. Each entry links to its predecessor::

    chain_seal(0) = seal(format_version, store_id, host_id, agent_family, epoch)
    chain_seal(n) = seal(seq, entry_id, reconciliation_id, revision, decision_id,
                         decision_revision, preimage_seal, record_seal, content_seal,
                         chain_seal(n-1), appended_at)

``chain_seal(0)`` is recomputed from the binding at verification time, never read
back from storage, so relabelling ``host_id`` breaks the chain instead of renaming
the journal's history. Every seal domain is distinct from HOK-243's, so a decision
record and a reconciliation cannot collide or be relabelled into one another. A
durable anchor — the entry count, the tail seal and the epoch status — is written
inside the same transaction as each append and checked by
:meth:`ReconciliationJournal.verify`, because a chain alone cannot notice that its
last entries were removed.

**What this does not prove.** A hash chain plus an anchor detects tampering by
anyone who cannot rewrite both. It establishes no authenticity and no chronology:
an administrator with write access can recompute the chain and the anchor together
from a forged history and produce a journal that verifies perfectly. Worse for
this contract than for HOK-243, an observation's ``observed_at``, its
``source_digest`` and its stated ``confidence`` are all asserted by the producer
and witnessed by nothing here. The journal proves that *these observations were
recorded and have not been altered since*, never that they are true.

One reconciliation per preimage
-------------------------------
An exact pre-action revision is reconciled by exactly one reconciliation identity.
A second identity over the same domain-separated full preimage-reference seal is refused,
so a later observer cannot open a parallel narrative: a correction or a
disagreement must append as a revision of the existing reconciliation, naming the
head seal it extends and saying why. A revision may never re-point at a different
preimage.

Atomicity and compare-and-set
-----------------------------
Every append runs inside one ``BEGIN IMMEDIATE`` transaction that re-reads the
binding, the epoch status, the generation and the tail. Admission, contract
validation, mandatory exact preimage verification, clock validation and receipt
construction all happen **before** the commit, so no failure can occur after a row
is durable. An interruption before commit leaves no trace: no row, no burned
sequence number, an unchanged anchor. Every write accepts an optional
``expected_generation``; when supplied and stale, the append is refused without
touching entry count, generation or root seal.
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
    RECONCILIATION_FORMAT_VERSION,
    SUPPORTED_RECONCILIATION_FORMATS,
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.admission import screen_persisted_text
from latent_compass.decision_memory.contracts import StrategicDecisionRecord
from latent_compass.decision_reconciliation.admission import admit_reconciliation
from latent_compass.decision_reconciliation.contracts import (
    MAX_RECONCILIATION_REVISION,
    ReconciliationBinding,
    ReconciliationRecord,
    preimage_reference_seal,
)
from latent_compass.decision_reconciliation.replay import (
    ReconciliationReplay,
    replay_reconciliation,
    require_preimage_match,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import (
    ContractViolation,
    EpochClosed,
    IntegrityError,
    LedgerError,
    ProvenanceMismatch,
    ReconciliationRevisionConflict,
    ReconciliationViolation,
    StoreAlreadyExists,
    StoreNotFound,
)
from latent_compass.ledger import Clock, EpochStatus, utc_now

__all__ = [
    "MAX_RECONCILIATION_PAGE_SIZE",
    "RECONCILIATION_DATABASE_FILENAME",
    "ReconciliationAppendReceipt",
    "ReconciliationHead",
    "ReconciliationIntegrityFinding",
    "ReconciliationIntegrityKind",
    "ReconciliationIntegrityReport",
    "ReconciliationJournal",
    "ReconciliationJournalBinding",
    "ReconciliationJournalStatus",
]

RECONCILIATION_DATABASE_FILENAME: Final = "reconciliation-journal.sqlite3"
MAX_RECONCILIATION_PAGE_SIZE: Final = 1000

GENESIS_SEAL_DOMAIN: Final = "decision-reconciliation.genesis.v1"
CHAIN_SEAL_DOMAIN: Final = "decision-reconciliation.chain.v1"
ANCHOR_SEAL_DOMAIN: Final = "decision-reconciliation.anchor.v1"

_BINDING_KEYS: Final = (
    "reconciliation_format_version",
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
CREATE TABLE reconciliations (
    seq               INTEGER PRIMARY KEY NOT NULL,
    entry_id          TEXT NOT NULL UNIQUE,
    reconciliation_id TEXT NOT NULL,
    revision          INTEGER NOT NULL,
    decision_id       TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,
    preimage_seal      TEXT NOT NULL,
    record_seal       TEXT NOT NULL,
    content_seal      TEXT NOT NULL,
    prev_chain_seal   TEXT NOT NULL,
    chain_seal        TEXT NOT NULL UNIQUE,
    payload           TEXT NOT NULL,
    appended_at       TEXT NOT NULL
) STRICT;
""",
)

BoundedSeal = Annotated[str, Field(min_length=1, max_length=200)]
EntryId = Annotated[str, Field(min_length=3, max_length=200)]


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


class ReconciliationIntegrityKind(StrEnum):
    """What kind of defect was found. Never collapsed into a single boolean."""

    SEQUENCE_INVALID = "SEQUENCE_INVALID"
    CHAIN_BROKEN = "CHAIN_BROKEN"
    ENTRY_IDENTITY_MISMATCH = "ENTRY_IDENTITY_MISMATCH"
    PREIMAGE_MISMATCH = "PREIMAGE_MISMATCH"
    PAYLOAD_ALTERED = "PAYLOAD_ALTERED"
    PAYLOAD_UNPARSEABLE = "PAYLOAD_UNPARSEABLE"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    REVISION_LINK_BROKEN = "REVISION_LINK_BROKEN"
    PREIMAGE_FORKED = "PREIMAGE_FORKED"
    ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
    ANCHOR_MISSING = "ANCHOR_MISSING"


class ReconciliationJournalBinding(StrictModel):
    """The identity a journal is bound to at creation, and never after."""

    reconciliation_format_version: str = Field(min_length=5, max_length=20)
    store_id: Identifier
    host_id: Identifier
    agent_family: AgentFamily
    epoch: Identifier
    epoch_status: EpochStatus
    created_at: Timestamp

    def binding_ref(self) -> ReconciliationBinding:
        """The four fields a reconciliation must match to belong here."""
        return ReconciliationBinding(
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
                "reconciliation_format_version": self.reconciliation_format_version,
                "store_id": self.store_id,
                "host_id": self.host_id,
                "agent_family": self.agent_family.value,
                "epoch": self.epoch,
            },
        )


class ReconciliationAppendReceipt(StrictModel):
    """Proof that one reconciliation revision was accepted, and where it landed."""

    seq: int = Field(ge=1)
    entry_id: EntryId
    reconciliation_id: Identifier
    revision: int = Field(ge=1, le=MAX_RECONCILIATION_REVISION)
    decision_id: Identifier
    decision_revision: int = Field(ge=1)
    preimage_seal: Seal
    record_seal: Seal
    content_seal: BoundedSeal
    chain_seal: BoundedSeal
    root_seal: BoundedSeal
    generation: int = Field(ge=1)
    entry_count: int = Field(ge=0)
    preimage_verified: bool
    appended_at: Timestamp


class ReconciliationIntegrityFinding(StrictModel):
    """One defect, located and named."""

    seq: int | None = Field(default=None, ge=1)
    entry_id: str | None = Field(default=None)
    kind: ReconciliationIntegrityKind
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class ReconciliationIntegrityReport(StrictModel):
    """Result of a full chain walk over every durable entry."""

    ok: bool
    generation: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    reconciliation_count: int = Field(ge=0)
    root_seal: BoundedSeal
    findings: tuple[ReconciliationIntegrityFinding, ...] = Field(default=())


class ReconciliationJournalStatus(StrictModel):
    """The counters a caller needs to build a compare-and-set append."""

    binding: ReconciliationJournalBinding
    generation: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    reconciliation_count: int = Field(ge=0)
    root_seal: BoundedSeal


class ReconciliationHead(StrictModel):
    """The head revision of one reconciliation, with its position in the journal."""

    reconciliation_id: Identifier
    revision: int = Field(ge=1, le=MAX_RECONCILIATION_REVISION)
    seq: int = Field(ge=1)
    content_seal: BoundedSeal
    appended_at: Timestamp
    record: ReconciliationRecord


def _chain_seal(
    *,
    seq: int,
    entry_id: str,
    reconciliation_id: str,
    revision: int,
    decision_id: str,
    decision_revision: int,
    preimage_seal: str,
    record_seal: str,
    content_seal: str,
    prev_chain_seal: str,
    appended_at: str,
) -> str:
    return seal(
        CHAIN_SEAL_DOMAIN,
        {
            "seq": seq,
            "entry_id": entry_id,
            "reconciliation_id": reconciliation_id,
            "revision": revision,
            "decision_id": decision_id,
            "decision_revision": decision_revision,
            "preimage_seal": preimage_seal,
            "record_seal": record_seal,
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


def _entry_id(reconciliation_id: str, revision: int) -> str:
    return f"{reconciliation_id}:r{revision}"


def _validated_journal_root(root: Path | str) -> Path:
    """Reject lexical escapes and any existing symlink/reparse component.

    SQLite's stdlib API accepts a pathname rather than an already-confined file
    handle. This check therefore protects admission against pre-existing
    redirection; callers must also keep the local root free from concurrent
    adversarial namespace mutation for the journal lifetime.
    """
    absolute = Path(root).absolute()
    plan_confined_target(
        absolute,
        absolute / RECONCILIATION_DATABASE_FILENAME,
        what="reconciliation journal database",
    )
    try:
        resolved = absolute.resolve(strict=False)
    except OSError as exc:
        raise ContractViolation(
            "reconciliation journal root cannot be resolved safely",
            detail={"root": str(absolute), "cause": exc.strerror},
        ) from exc
    if resolved != absolute:
        raise ContractViolation(
            "reconciliation journal root must not contain a symlink or reparse point",
            detail={"root": str(absolute), "reason": "reparse_root"},
        )
    return absolute


def _validated_database_path(root: Path) -> Path:
    """Reject a database entry that is itself a symlink or reparse point."""
    database = root / RECONCILIATION_DATABASE_FILENAME
    try:
        resolved = database.resolve(strict=False)
    except OSError as exc:
        raise ContractViolation(
            "reconciliation journal database cannot be resolved safely",
            detail={"path": str(database), "cause": exc.strerror},
        ) from exc
    if resolved != database:
        raise ContractViolation(
            "reconciliation journal database must not be a symlink or reparse point",
            detail={"path": str(database), "reason": "reparse_database"},
        )
    return database


class ReconciliationJournal:
    """A local, single-host, single-family, append-only reconciliation journal."""

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
    ) -> ReconciliationJournal:
        """Create a journal bound to one host, one agent family and one epoch.

        The database file is claimed with ``O_CREAT | O_EXCL``, which is the
        mutual exclusion: a concurrent loser never gets the file, so it can never
        delete the winner's journal on its way out.
        """
        screen_persisted_text(store_id=store_id, host_id=host_id, epoch=epoch)
        root_path = _validated_journal_root(root)
        database = _validated_database_path(root_path)
        binding = validate_contract(
            ReconciliationJournalBinding,
            {
                "reconciliation_format_version": RECONCILIATION_FORMAT_VERSION,
                "store_id": store_id,
                "host_id": host_id,
                "agent_family": agent_family,
                "epoch": epoch,
                "epoch_status": EpochStatus.OPEN,
                "created_at": _stamp(clock),
            },
            error=ContractViolation,
            context="reconciliation journal binding",
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
                f"a reconciliation journal already exists at {database}",
                detail={"root": str(root_path)},
            ) from exc
        except OSError as exc:
            raise LedgerError(
                f"cannot create a reconciliation journal at {database}",
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
    def open(cls, root: Path | str) -> ReconciliationJournal:
        """Open an existing journal, refusing an unknown journal format."""
        root_path = _validated_journal_root(root)
        database = _validated_database_path(root_path)
        if not database.exists():
            raise StoreNotFound(
                f"no reconciliation journal at {database}", detail={"root": str(root_path)}
            )
        connection = cls._connect(database)
        journal = cls(root_path, connection)
        try:
            with _sqlite_errors(root_path):
                check_contract_version(
                    journal._meta("reconciliation_format_version"),
                    SUPPORTED_RECONCILIATION_FORMATS,
                    "reconciliation journal",
                )
        except BaseException:
            connection.close()
            raise
        return journal

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

    def __enter__(self) -> ReconciliationJournal:
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
            raise LedgerError(f"journal metadata is missing {key!r}", detail={"key": key})
        return str(row["value"])

    def _binding_from(self, connection: sqlite3.Connection) -> ReconciliationJournalBinding:
        rows = connection.execute(
            "SELECT key, value FROM store_meta WHERE key IN "
            "('reconciliation_format_version','store_id','host_id','agent_family',"
            " 'epoch','epoch_status','created_at')"
        ).fetchall()
        payload = {row["key"]: row["value"] for row in rows}
        missing = sorted(set(_BINDING_KEYS) - set(payload))
        if missing:
            raise LedgerError("journal metadata is incomplete", detail={"missing": missing})
        return validate_contract(
            ReconciliationJournalBinding,
            payload,
            error=ContractViolation,
            context="reconciliation journal binding",
        )

    def binding(self) -> ReconciliationJournalBinding:
        """The immutable identity this journal is bound to."""
        with _sqlite_errors(self.root):
            return self._binding_from(self._conn)

    @staticmethod
    def _generation_of(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COUNT(*) AS total FROM reconciliations").fetchone()
        return int(row["total"])

    @staticmethod
    def _reconciliation_count_of(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(DISTINCT reconciliation_id) AS total FROM reconciliations"
        ).fetchone()
        return int(row["total"])

    def generation(self) -> int:
        """Total durable entries. The value an ``expected_generation`` must match."""
        with _sqlite_errors(self.root):
            return self._generation_of(self._conn)

    def entry_count(self) -> int:
        """Number of durable entries, revisions included."""
        return self.generation()

    def reconciliation_count(self) -> int:
        """Number of distinct reconciliation identities."""
        with _sqlite_errors(self.root):
            return self._reconciliation_count_of(self._conn)

    def _root_seal_of(self, connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT chain_seal FROM reconciliations ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return self._binding_from(connection).genesis_seal()
        return str(row["chain_seal"])

    def root_seal(self) -> str:
        """Seal of the last chain link, or the genesis seal for an empty journal."""
        with _sqlite_errors(self.root):
            return self._root_seal_of(self._conn)

    def status(self) -> ReconciliationJournalStatus:
        """Binding plus the counters a compare-and-set append needs."""
        with _sqlite_errors(self.root):
            return ReconciliationJournalStatus(
                binding=self._binding_from(self._conn),
                generation=self._generation_of(self._conn),
                entry_count=self._generation_of(self._conn),
                reconciliation_count=self._reconciliation_count_of(self._conn),
                root_seal=self._root_seal_of(self._conn),
            )

    # -- write path --------------------------------------------------------

    @contextmanager
    def _append_transaction(
        self, *, expected_generation: int | None, what: str
    ) -> Iterator[ReconciliationJournalBinding]:
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
                    f"refusing to {what} in a journal that fails integrity verification",
                    detail={
                        "findings": [finding.canonical_payload() for finding in report.findings]
                    },
                )
            binding = self._binding_from(self._conn)
            if binding.epoch_status is EpochStatus.ABANDONED:
                raise EpochClosed(
                    "the epoch of this reconciliation journal was abandoned; no further writes",
                    detail={"store_id": binding.store_id, "epoch": binding.epoch},
                )
            if expected_generation is not None:
                observed = self._generation_of(self._conn)
                if expected_generation != observed:
                    raise ReconciliationRevisionConflict(
                        "the journal is not at the generation this write expected",
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

    @staticmethod
    def _require_journal_binding(
        record: ReconciliationRecord, binding: ReconciliationBinding
    ) -> None:
        for field, expected, received in (
            ("host_id", binding.host_id, record.binding.host_id),
            ("agent_family", binding.agent_family.value, record.binding.agent_family.value),
            ("store_id", binding.store_id, record.binding.store_id),
            ("epoch", binding.epoch, record.binding.epoch),
        ):
            if expected != received:
                raise ProvenanceMismatch(
                    f"reconciliation {field} does not match this journal",
                    detail={
                        "field": field,
                        "journal": expected,
                        "reconciliation": received,
                        "reconciliation_id": record.reconciliation_id,
                    },
                )
        preimage_binding = record.preimage.decision_binding
        for field, expected, received in (
            ("host_id", binding.host_id, preimage_binding.host_id),
            ("agent_family", binding.agent_family.value, preimage_binding.agent_family.value),
            ("store_id", binding.store_id, preimage_binding.store_id),
            ("epoch", binding.epoch, preimage_binding.epoch),
        ):
            if expected != received:
                raise ProvenanceMismatch(
                    f"pre-action decision {field} does not match this journal",
                    detail={
                        "field": field,
                        "journal": expected,
                        "preimage": received,
                        "reconciliation_id": record.reconciliation_id,
                    },
                )

    def _head_row(self, reconciliation_id: str) -> sqlite3.Row | None:
        head: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (reconciliation_id,),
        ).fetchone()
        return head

    def append_reconciliation(
        self,
        payload: object,
        *,
        decision_record: StrategicDecisionRecord,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> ReconciliationAppendReceipt:
        """Admit and append one reconciliation — the initial one or a revision of it.

        ``payload`` is raw JSON-shaped data on purpose: admission is the only
        supported construction path, and it runs here, before any transaction is
        opened, so a refused payload cannot move entry count, generation or root
        seal.

        ``decision_record`` is required verification. The declared preimage must
        match it exactly — binding, decision
        id, pre-action revision, record seal and projection seal — and any executed
        direction must be one of that record's candidates; a divergence is refused
        as a :class:`~latent_compass.errors.PreimageMismatch`. The decision record is only *read*:
        nothing is ever written back to the decision memory.

        A revision must extend the *exact* current head: revision ``n`` requires a
        head at ``n - 1`` whose content seal is exactly the one it names, and it
        must carry the identical preimage. A stale revision, a fork past the head,
        a replayed initial revision, a revision that re-points at another decision
        and a second identity over an already reconciled preimage are all refused,
        and none of them changes the journal.
        """
        record = admit_reconciliation(payload)
        require_preimage_match(record, decision_record)
        appended_at = _stamp(clock)
        document = json.dumps(
            record.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        content_seal = record.record_seal()

        with (
            _sqlite_errors(self.root),
            self._append_transaction(
                expected_generation=expected_generation, what="append a reconciliation"
            ) as binding,
        ):
            self._require_journal_binding(record, binding.binding_ref())
            self._require_extends_head(record)
            # Returning here still commits: the context manager resumes past its
            # yield on a clean exit, which is where COMMIT runs.
            return self._insert_entry(
                binding,
                record=record,
                content_seal=content_seal,
                payload=document,
                appended_at=appended_at,
                preimage_verified=True,
            )

    def _require_extends_head(self, record: ReconciliationRecord) -> None:
        """Refuse anything that is not an exact extension of the current head."""
        head = self._head_row(record.reconciliation_id)
        if record.revision == 1:
            if head is not None:
                raise ReconciliationRevisionConflict(
                    "initial history is never replaced: this reconciliation already exists",
                    detail={
                        "reason": "initial_revision_exists",
                        "reconciliation_id": record.reconciliation_id,
                        "head_revision": int(head["revision"]),
                    },
                )
            self._require_preimage_unclaimed(record)
            return
        if head is None:
            raise ReconciliationRevisionConflict(
                "a revision was offered for a reconciliation that has no initial revision",
                detail={
                    "reason": "no_prior_revision",
                    "reconciliation_id": record.reconciliation_id,
                    "revision": record.revision,
                },
            )
        head_revision = int(head["revision"])
        if record.revision != head_revision + 1:
            raise ReconciliationRevisionConflict(
                "a revision must extend the current head by exactly one",
                detail={
                    "reason": "stale_or_forked_revision",
                    "reconciliation_id": record.reconciliation_id,
                    "offered_revision": record.revision,
                    "head_revision": head_revision,
                },
            )
        if record.supersedes_revision_seal != str(head["content_seal"]):
            raise ReconciliationRevisionConflict(
                "a revision must name the seal of the exact revision it supersedes",
                detail={
                    "reason": "superseded_seal_mismatch",
                    "reconciliation_id": record.reconciliation_id,
                    "expected": str(head["content_seal"]),
                    "offered": record.supersedes_revision_seal,
                },
            )
        head_preimage = _decode_record(head).preimage
        if record.preimage.canonical_payload() != head_preimage.canonical_payload():
            raise ReconciliationRevisionConflict(
                "a revision reconciles the same preimage as the revision it supersedes",
                detail={
                    "reason": "preimage_repointed",
                    "reconciliation_id": record.reconciliation_id,
                    "head_preimage": head_preimage.canonical_payload(),
                    "offered_preimage": record.preimage.canonical_payload(),
                },
            )

    def _require_preimage_unclaimed(self, record: ReconciliationRecord) -> None:
        """One pre-action revision is reconciled by exactly one identity.

        A second identity over the same preimage would be a parallel narrative
        with no defined head. A later observer corrects or disagrees by appending
        a revision to the existing reconciliation, which is visible and ordered.
        """
        row = self._conn.execute(
            "SELECT reconciliation_id FROM reconciliations WHERE preimage_seal = ? LIMIT 1",
            (preimage_reference_seal(record.preimage),),
        ).fetchone()
        if row is not None:
            raise ReconciliationRevisionConflict(
                "this pre-action revision is already reconciled; append a correction or a "
                "disagreement to that reconciliation instead",
                detail={
                    "reason": "preimage_already_reconciled",
                    "decision_id": record.preimage.decision_id,
                    "decision_revision": record.preimage.decision_revision,
                    "existing_reconciliation_id": str(row["reconciliation_id"]),
                },
            )

    def _insert_entry(
        self,
        binding: ReconciliationJournalBinding,
        *,
        record: ReconciliationRecord,
        content_seal: str,
        payload: str,
        appended_at: str,
        preimage_verified: bool,
    ) -> ReconciliationAppendReceipt:
        """Link, insert and re-anchor one entry. Everything fallible runs first."""
        tail = self._conn.execute(
            "SELECT seq, chain_seal FROM reconciliations ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if tail is None:
            seq = 1
            prev_chain_seal = binding.genesis_seal()
        else:
            seq = int(tail["seq"]) + 1
            prev_chain_seal = str(tail["chain_seal"])

        entry_id = _entry_id(record.reconciliation_id, record.revision)
        preimage_seal = preimage_reference_seal(record.preimage)
        chain_seal = _chain_seal(
            seq=seq,
            entry_id=entry_id,
            reconciliation_id=record.reconciliation_id,
            revision=record.revision,
            decision_id=record.preimage.decision_id,
            decision_revision=record.preimage.decision_revision,
            preimage_seal=preimage_seal,
            record_seal=record.preimage.record_seal,
            content_seal=content_seal,
            prev_chain_seal=prev_chain_seal,
            appended_at=appended_at,
        )
        # Built before the insert: nothing that can fail runs after the commit.
        receipt = ReconciliationAppendReceipt(
            seq=seq,
            entry_id=entry_id,
            reconciliation_id=record.reconciliation_id,
            revision=record.revision,
            decision_id=record.preimage.decision_id,
            decision_revision=record.preimage.decision_revision,
            preimage_seal=preimage_seal,
            record_seal=record.preimage.record_seal,
            content_seal=content_seal,
            chain_seal=chain_seal,
            root_seal=chain_seal,
            generation=seq,
            entry_count=seq,
            preimage_verified=preimage_verified,
            appended_at=appended_at,
        )
        try:
            self._conn.execute(
                "INSERT INTO reconciliations "
                "(seq, entry_id, reconciliation_id, revision, decision_id, decision_revision, "
                " preimage_seal, record_seal, content_seal, prev_chain_seal, chain_seal, "
                " payload, appended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    entry_id,
                    record.reconciliation_id,
                    record.revision,
                    record.preimage.decision_id,
                    record.preimage.decision_revision,
                    preimage_seal,
                    record.preimage.record_seal,
                    content_seal,
                    prev_chain_seal,
                    chain_seal,
                    payload,
                    appended_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ReconciliationRevisionConflict(
                f"entry {entry_id!r} is already present in this reconciliation journal",
                detail={"reason": "duplicate_entry", "entry_id": entry_id},
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

    def abandon_epoch(self, *, reason: str, clock: Clock = utc_now) -> ReconciliationJournalBinding:
        """Close the epoch. Everything recorded survives; no further writes.

        Refuses on a journal that fails verification, and that refusal is
        load-bearing rather than defensive: abandoning re-writes the durable
        anchor from the *current* entry count and tail, so on a truncated journal
        it would replace the anchor that proves entries are missing with one that
        agrees with what is left. An operator who needs to stop writing to a
        corrupt journal already has that — every append refuses too.
        """
        screen_persisted_text(reason=reason)
        stamped = _stamp(clock)
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                report = self._verify_unlocked()
                if not report.ok:
                    raise IntegrityError(
                        "refusing to abandon the epoch of a journal that fails integrity "
                        "verification: re-anchoring it would launder the defect",
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
                self._write_anchor(
                    self._conn,
                    genesis=binding.genesis_seal(),
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

    def get(self, reconciliation_id: str, *, revision: int | None = None) -> ReconciliationHead:
        """One reconciliation revision — the named one, or the head by default."""
        with _sqlite_errors(self.root):
            if revision is None:
                row = self._head_row(reconciliation_id)
            else:
                row = self._conn.execute(
                    "SELECT * FROM reconciliations WHERE reconciliation_id = ? AND revision = ?",
                    (reconciliation_id, revision),
                ).fetchone()
            if row is None:
                raise StoreNotFound(
                    f"no reconciliation {reconciliation_id!r}"
                    + (f" at revision {revision}" if revision is not None else "")
                    + " in this journal",
                    detail={"reconciliation_id": reconciliation_id, "revision": revision},
                )
            return _head_from_row(row)

    def revisions(self, reconciliation_id: str) -> tuple[ReconciliationHead, ...]:
        """Every revision of one reconciliation, in append order. Never collapsed."""
        with _sqlite_errors(self.root):
            rows = self._conn.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id = ? ORDER BY revision ASC",
                (reconciliation_id,),
            ).fetchall()
            if not rows:
                raise StoreNotFound(
                    f"no reconciliation {reconciliation_id!r} in this journal",
                    detail={"reconciliation_id": reconciliation_id},
                )
            return tuple(_head_from_row(row) for row in rows)

    def list_heads(self, *, limit: int = 50, offset: int = 0) -> tuple[ReconciliationHead, ...]:
        """Bounded listing of head revisions. Refuses an unbounded page."""
        if limit < 1 or limit > MAX_RECONCILIATION_PAGE_SIZE:
            raise ContractViolation(
                f"limit must be between 1 and {MAX_RECONCILIATION_PAGE_SIZE}",
                detail={"limit": limit, "max": MAX_RECONCILIATION_PAGE_SIZE},
            )
        if offset < 0:
            raise ContractViolation("offset must not be negative", detail={"offset": offset})
        with _sqlite_errors(self.root):
            rows = self._conn.execute(
                "SELECT * FROM reconciliations AS head "
                "WHERE head.revision = (SELECT MAX(peer.revision) FROM reconciliations AS peer "
                "                       WHERE peer.reconciliation_id = head.reconciliation_id) "
                "ORDER BY head.reconciliation_id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return tuple(_head_from_row(row) for row in rows)

    def replay(
        self,
        reconciliation_id: str,
        decision_record: StrategicDecisionRecord,
        *,
        revision: int | None = None,
    ) -> ReconciliationReplay:
        """Replay one stored reconciliation against its supplied preimage.

        Reads. Produces the per-dimension comparison and nothing else — no score,
        no ranking, no causal claim, no authorization, and no write to either
        store.
        """
        head = self.get(reconciliation_id, revision=revision)
        return replay_reconciliation(head.record, decision_record)

    # -- integrity ---------------------------------------------------------

    def verify(self) -> ReconciliationIntegrityReport:
        """Walk the whole entry chain and report every defect found, by kind."""
        with _sqlite_errors(self.root):
            self._conn.execute("BEGIN")
            try:
                return self._verify_unlocked()
            finally:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")

    def _verify_unlocked(self) -> ReconciliationIntegrityReport:
        findings: list[ReconciliationIntegrityFinding] = []
        binding = self._binding_from(self._conn)
        journal = binding.binding_ref().canonical_payload()
        genesis = binding.genesis_seal()
        previous = genesis
        expected_seq = 1
        generation = 0

        content_seals = {
            (str(row["reconciliation_id"]), int(row["revision"])): str(row["content_seal"])
            for row in self._conn.execute(
                "SELECT reconciliation_id, revision, content_seal FROM reconciliations"
            )
        }
        preimage_owners: dict[str, set[str]] = {}
        for row in self._conn.execute(
            "SELECT preimage_seal, reconciliation_id FROM reconciliations"
        ):
            key = str(row["preimage_seal"])
            preimage_owners.setdefault(key, set()).add(str(row["reconciliation_id"]))

        for row in self._conn.execute("SELECT * FROM reconciliations ORDER BY seq ASC"):
            generation += 1
            seq = int(row["seq"])
            entry_id = str(row["entry_id"])
            if seq != expected_seq:
                findings.append(
                    ReconciliationIntegrityFinding(
                        seq=seq,
                        entry_id=entry_id,
                        kind=ReconciliationIntegrityKind.SEQUENCE_INVALID,
                        detail=f"expected sequence {expected_seq}, found {seq}",
                    )
                )
            expected_seq = seq + 1

            if str(row["prev_chain_seal"]) != previous:
                findings.append(
                    ReconciliationIntegrityFinding(
                        seq=seq,
                        entry_id=entry_id,
                        kind=ReconciliationIntegrityKind.CHAIN_BROKEN,
                        detail="recorded predecessor seal does not match the previous link",
                    )
                )
            recomputed = _chain_seal(
                seq=seq,
                entry_id=entry_id,
                reconciliation_id=str(row["reconciliation_id"]),
                revision=int(row["revision"]),
                decision_id=str(row["decision_id"]),
                decision_revision=int(row["decision_revision"]),
                preimage_seal=str(row["preimage_seal"]),
                record_seal=str(row["record_seal"]),
                content_seal=str(row["content_seal"]),
                prev_chain_seal=str(row["prev_chain_seal"]),
                appended_at=str(row["appended_at"]),
            )
            if recomputed != str(row["chain_seal"]):
                findings.append(
                    ReconciliationIntegrityFinding(
                        seq=seq,
                        entry_id=entry_id,
                        kind=ReconciliationIntegrityKind.CHAIN_BROKEN,
                        detail="stored chain seal does not reproduce from this row",
                    )
                )
            previous = str(row["chain_seal"])
            findings.extend(self._entry_findings(row, journal=journal, content_seals=content_seals))

        for key, owners in sorted(preimage_owners.items()):
            if len(owners) > 1:
                findings.append(
                    ReconciliationIntegrityFinding(
                        kind=ReconciliationIntegrityKind.PREIMAGE_FORKED,
                        detail=(
                            f"preimage {key} is claimed by {len(owners)} reconciliation identities"
                        ),
                    )
                )

        findings.extend(
            self._anchor_findings(
                genesis=genesis, generation=generation, tail=previous, binding=binding
            )
        )
        return ReconciliationIntegrityReport(
            ok=not findings,
            generation=generation,
            entry_count=generation,
            reconciliation_count=self._reconciliation_count_of(self._conn),
            root_seal=previous,
            findings=tuple(findings),
        )

    @staticmethod
    def _entry_findings(
        row: sqlite3.Row,
        *,
        journal: dict[str, object],
        content_seals: dict[tuple[str, int], str],
    ) -> list[ReconciliationIntegrityFinding]:
        seq = int(row["seq"])
        entry_id = str(row["entry_id"])
        reconciliation_id = str(row["reconciliation_id"])
        revision = int(row["revision"])
        findings: list[ReconciliationIntegrityFinding] = []
        try:
            record = _decode_record(row)
        except ContractViolation as exc:
            return [
                ReconciliationIntegrityFinding(
                    seq=seq,
                    entry_id=entry_id,
                    kind=ReconciliationIntegrityKind.PAYLOAD_UNPARSEABLE,
                    detail=exc.message[:400],
                )
            ]
        if record.record_seal() != str(row["content_seal"]):
            return [
                ReconciliationIntegrityFinding(
                    seq=seq,
                    entry_id=entry_id,
                    kind=ReconciliationIntegrityKind.PAYLOAD_ALTERED,
                    detail="payload does not reproduce its recorded content seal",
                )
            ]
        if (
            record.reconciliation_id != reconciliation_id
            or record.revision != revision
            or entry_id != _entry_id(reconciliation_id, revision)
        ):
            findings.append(
                ReconciliationIntegrityFinding(
                    seq=seq,
                    entry_id=entry_id,
                    kind=ReconciliationIntegrityKind.ENTRY_IDENTITY_MISMATCH,
                    detail="entry row identity does not match its sealed payload",
                )
            )
        if (
            record.preimage.decision_id != str(row["decision_id"])
            or record.preimage.decision_revision != int(row["decision_revision"])
            or preimage_reference_seal(record.preimage) != str(row["preimage_seal"])
            or record.preimage.record_seal != str(row["record_seal"])
        ):
            findings.append(
                ReconciliationIntegrityFinding(
                    seq=seq,
                    entry_id=entry_id,
                    kind=ReconciliationIntegrityKind.PREIMAGE_MISMATCH,
                    detail="indexed preimage does not match the sealed payload's preimage",
                )
            )
        if record.binding.canonical_payload() != journal:
            findings.append(
                ReconciliationIntegrityFinding(
                    seq=seq,
                    entry_id=entry_id,
                    kind=ReconciliationIntegrityKind.BINDING_MISMATCH,
                    detail="stored reconciliation no longer matches the journal binding",
                )
            )
        if record.revision > 1:
            expected = content_seals.get((reconciliation_id, record.revision - 1))
            if expected is None or record.supersedes_revision_seal != expected:
                findings.append(
                    ReconciliationIntegrityFinding(
                        seq=seq,
                        entry_id=entry_id,
                        kind=ReconciliationIntegrityKind.REVISION_LINK_BROKEN,
                        detail="revision does not link to the seal of its predecessor",
                    )
                )
        return findings

    def _anchor_findings(
        self,
        *,
        genesis: str,
        generation: int,
        tail: str,
        binding: ReconciliationJournalBinding,
    ) -> list[ReconciliationIntegrityFinding]:
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
                ReconciliationIntegrityFinding(
                    kind=ReconciliationIntegrityKind.ANCHOR_MISSING,
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
        findings: list[ReconciliationIntegrityFinding] = []
        if expected != rows["anchor_seal"]:
            findings.append(
                ReconciliationIntegrityFinding(
                    kind=ReconciliationIntegrityKind.ANCHOR_MISMATCH,
                    detail="the anchor does not reproduce from the binding it claims to anchor",
                )
            )
        if rows["anchor_generation"] != str(generation) or rows["anchor_tail"] != tail:
            findings.append(
                ReconciliationIntegrityFinding(
                    kind=ReconciliationIntegrityKind.ANCHOR_MISMATCH,
                    detail=(
                        f"anchor records generation {rows['anchor_generation']} ending "
                        f"{rows['anchor_tail'][:23]}…, journal holds {generation} ending "
                        f"{tail[:23]}…"
                    ),
                )
            )
        return findings


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _decode_payload(row: sqlite3.Row) -> object:
    """Decode a stored payload, refusing bad bytes with a typed error."""
    raw = row["payload"]
    try:
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8")
        return json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReconciliationViolation(
            "stored payload is not decodable JSON",
            detail={"entry_id": str(row["entry_id"]), "cause": type(exc).__name__},
        ) from exc


def _decode_record(row: sqlite3.Row) -> ReconciliationRecord:
    return validate_contract(
        ReconciliationRecord,
        _decode_payload(row),
        error=ReconciliationViolation,
        context="stored reconciliation record",
    )


def _head_from_row(row: sqlite3.Row) -> ReconciliationHead:
    return ReconciliationHead(
        reconciliation_id=str(row["reconciliation_id"]),
        revision=int(row["revision"]),
        seq=int(row["seq"]),
        content_seal=str(row["content_seal"]),
        appended_at=str(row["appended_at"]),
        record=_decode_record(row),
    )


@contextmanager
def _sqlite_errors(root: Path) -> Iterator[None]:
    """Translate raw SQLite and pydantic failures into the typed vocabulary.

    The order matters. ``OperationalError`` and ``IntegrityError`` are both
    ``DatabaseError`` subclasses but mean "the operation could not run", not "the
    file is corrupt", so they map to a store error. The bare ``DatabaseError`` —
    "file is not a database", "disk image is malformed" — is the corruption case
    and maps to an integrity error.
    """
    try:
        yield
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        raise LedgerError(
            "the reconciliation journal database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(
            "the reconciliation journal database is unreadable or not a journal",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except sqlite3.Error as exc:
        raise LedgerError(
            "the reconciliation journal database rejected an operation",
            detail={"root": str(root), "cause": str(exc)},
        ) from exc
    except ValidationError as exc:
        raise ContractViolation(
            "stored reconciliation data does not satisfy its contract",
            detail={"root": str(root)},
        ) from exc

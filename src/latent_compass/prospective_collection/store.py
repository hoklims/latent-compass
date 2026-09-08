"""Adjacent append-only HOK-252 journal bound to one immutable sealed plan."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, Field

from latent_compass.canonical import seal
from latent_compass.confined_io import plan_confined_target
from latent_compass.contracts import (
    PROSPECTIVE_COLLECTION_FORMAT_VERSION,
    SUPPORTED_PROSPECTIVE_COLLECTION_FORMATS,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory import StrategicDecisionRecord, admit_strategic_decision
from latent_compass.decision_memory.admission import screen_persisted_text
from latent_compass.decision_reconciliation import (
    DecisionPreimageReference,
    ExecutionState,
    ReconciliationJournal,
    ReconciliationRecord,
    admit_reconciliation,
)
from latent_compass.decision_reconciliation.contracts import preimage_reference_seal
from latent_compass.decision_reconciliation.replay import require_preimage_match
from latent_compass.errors import (
    IntegrityError,
    ProspectiveCollectionViolation,
    StoreAlreadyExists,
    StoreNotFound,
)
from latent_compass.ledger import Clock, utc_now
from latent_compass.prospective_collection.contracts import (
    AbortReason,
    CaseState,
    PlanState,
    ProspectiveCollectionPlan,
)

if TYPE_CHECKING:
    from latent_compass.prospective_collection.report import (
        CollectionDiagnosticReport,
        CollectionManifest,
    )

__all__ = [
    "PROSPECTIVE_DATABASE_FILENAME",
    "CollectionCase",
    "CollectionReceipt",
    "CollectionStatus",
    "ProspectiveCollectionJournal",
]

PROSPECTIVE_DATABASE_FILENAME: Final = "prospective-collection.sqlite3"
GENESIS_DOMAIN: Final = "prospective-collection.genesis.v1"
EVENT_DOMAIN: Final = "prospective-collection.event.v1"
ANCHOR_DOMAIN: Final = "prospective-collection.anchor.v1"


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("reason must not contain control characters")
    return value


Reason = Annotated[
    str,
    Field(min_length=1, max_length=500),
    AfterValidator(_no_control_characters),
]

_SCHEMA: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL) STRICT",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY NOT NULL,
        event_kind TEXT NOT NULL,
        case_id TEXT,
        case_state TEXT,
        payload TEXT NOT NULL,
        content_seal TEXT NOT NULL,
        prev_chain_seal TEXT NOT NULL,
        chain_seal TEXT NOT NULL UNIQUE,
        appended_at TEXT NOT NULL
    ) STRICT""",
)


class _Stamp(StrictModel):
    at: Timestamp


def _stamp(clock: Clock) -> str:
    try:
        value = clock()
    except Exception as exc:
        raise ProspectiveCollectionViolation(
            "the collection clock raised", detail={"cause": type(exc).__name__}
        ) from exc
    return validate_contract(
        _Stamp, {"at": value}, error=ProspectiveCollectionViolation, context="collection clock"
    ).at


class CollectionStatus(StrictModel):
    plan_id: str
    plan_seal: Seal
    state: PlanState
    generation: int = Field(ge=0)
    enrolled_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    root_seal: Seal


class CollectionIntegrityReport(StrictModel):
    ok: bool
    generation: int = Field(ge=0)
    root_seal: Seal
    findings: tuple[str, ...]


class CollectionReceipt(StrictModel):
    seq: int = Field(ge=1)
    case_id: str
    state: CaseState
    generation: int = Field(ge=1)
    chain_seal: Seal
    appended_at: Timestamp


class CollectionCase(StrictModel):
    case_id: str
    state: CaseState
    stratum: str
    decision_id: str
    decision_revision: int = Field(ge=1)
    preimage_seal: Seal
    decision_record_seal: Seal
    decision_record: dict[str, object]
    decision_authority: str
    enrolled_at: Timestamp
    terminal_at: Timestamp | None
    terminal_reason: Reason | None
    reconciliation_id: str | None
    reconciliation_revision: int | None
    reconciliation_record_seal: Seal | None
    reconciliation: dict[str, object] | None
    reconciliation_source: ReconciliationSourceProof | None


class ReconciliationSourceProof(StrictModel):
    journal_root_seal: Seal
    journal_generation: int = Field(ge=1)
    entry_seq: int = Field(ge=1)
    entry_chain_seal: Seal
    entry_content_seal: Seal


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _anchor_seal(*, plan_seal: str, generation: int, tail: str, state: PlanState) -> str:
    return seal(
        ANCHOR_DOMAIN,
        {
            "plan_seal": plan_seal,
            "generation": generation,
            "tail": tail,
            "state": state.value,
        },
    )


def _validated_root(root: Path | str) -> Path:
    absolute = Path(root).absolute()
    plan_confined_target(
        absolute,
        absolute / PROSPECTIVE_DATABASE_FILENAME,
        what="prospective collection database",
    )
    try:
        resolved = absolute.resolve(strict=False)
        database = (absolute / PROSPECTIVE_DATABASE_FILENAME).resolve(strict=False)
    except OSError as exc:
        raise ProspectiveCollectionViolation(
            "prospective collection root cannot be resolved safely",
            detail={"root": str(absolute), "cause": exc.strerror},
        ) from exc
    if resolved != absolute or database != absolute / PROSPECTIVE_DATABASE_FILENAME:
        raise ProspectiveCollectionViolation(
            "prospective collection root must not contain a symlink or reparse point",
            detail={"root": str(absolute)},
        )
    return absolute


def _verified_reconciliation_source(
    source: ReconciliationJournal,
    *,
    reconciliation_id: str,
    revision: int | None,
) -> tuple[ReconciliationRecord, ReconciliationSourceProof]:
    connection = source._conn  # noqa: SLF001 - read-only cross-store proof snapshot
    if connection.in_transaction:
        raise ProspectiveCollectionViolation(
            "reconciliation source is already inside a transaction"
        )
    connection.execute("BEGIN")
    try:
        report = source._verify_unlocked()  # noqa: SLF001 - same snapshot as the proof row
        if not report.ok:
            raise IntegrityError(
                "reconciliation source journal fails integrity verification",
                detail={"findings": [item.canonical_payload() for item in report.findings]},
            )
        if revision is None:
            row = connection.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (reconciliation_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id=? AND revision=?",
                (reconciliation_id, revision),
            ).fetchone()
        if row is None:
            raise ProspectiveCollectionViolation(
                "named reconciliation revision is absent from the verified source journal"
            )
        try:
            record = admit_reconciliation(json.loads(str(row["payload"])))
        except Exception as exc:
            raise IntegrityError("verified reconciliation row cannot be admitted") from exc
        if record.record_seal() != str(row["content_seal"]):
            raise IntegrityError("reconciliation source content seal does not reproduce")
        root_seal = source._root_seal_of(connection)  # noqa: SLF001
        generation = source._generation_of(connection)  # noqa: SLF001
        entry_seq = int(row["seq"])
        entry_chain_seal = str(row["chain_seal"])
        if entry_seq != generation or entry_chain_seal != root_seal:
            raise ProspectiveCollectionViolation(
                "prospective reconciliation must link the current verified source tail"
            )
        proof = ReconciliationSourceProof(
            journal_root_seal=root_seal,
            journal_generation=generation,
            entry_seq=entry_seq,
            entry_chain_seal=entry_chain_seal,
            entry_content_seal=str(row["content_seal"]),
        )
        return record, proof
    finally:
        connection.execute("ROLLBACK")


class ProspectiveCollectionJournal:
    def __init__(self, root: Path, connection: sqlite3.Connection) -> None:
        self.root = root
        self._conn = connection

    @classmethod
    def create(
        cls, root: Path | str, *, plan: ProspectiveCollectionPlan, clock: Clock = utc_now
    ) -> ProspectiveCollectionJournal:
        from latent_compass.prospective_collection.admission import admit_prospective_plan

        plan = admit_prospective_plan(plan.canonical_payload())
        created_at = _stamp(clock)
        if created_at < plan.plan_created_at or created_at > plan.collection_not_before:
            raise ProspectiveCollectionViolation(
                "journal creation must occur inside the sealed pre-collection window"
            )
        root_path = _validated_root(root)
        root_path.mkdir(parents=True, exist_ok=True)
        database = root_path / PROSPECTIVE_DATABASE_FILENAME
        try:
            descriptor = os.open(str(database), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise StoreAlreadyExists("a prospective collection journal already exists") from exc
        os.close(descriptor)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            genesis = seal(
                GENESIS_DOMAIN,
                {
                    "format_version": PROSPECTIVE_COLLECTION_FORMAT_VERSION,
                    "plan_id": plan.plan_id,
                    "plan_seal": plan.plan_seal(),
                },
            )
            meta = {
                "format_version": PROSPECTIVE_COLLECTION_FORMAT_VERSION,
                "plan": _json(plan.canonical_payload()),
                "plan_seal": plan.plan_seal(),
                "state": PlanState.SEALED.value,
                "created_at": created_at,
                "genesis": genesis,
                "anchor_generation": "0",
                "anchor_tail": genesis,
                "anchor_state": PlanState.SEALED.value,
                "anchor_seal": _anchor_seal(
                    plan_seal=plan.plan_seal(),
                    generation=0,
                    tail=genesis,
                    state=PlanState.SEALED,
                ),
            }
            connection.executemany(
                "INSERT INTO meta (key,value) VALUES (?,?)", sorted(meta.items())
            )
            connection.execute("COMMIT")
            return cls(root_path, connection)
        except BaseException:
            if connection is not None:
                connection.close()
            database.unlink(missing_ok=True)
            raise

    @classmethod
    def open(cls, root: Path | str) -> ProspectiveCollectionJournal:
        root_path = _validated_root(root)
        database = root_path / PROSPECTIVE_DATABASE_FILENAME
        if not database.exists():
            raise StoreNotFound("no prospective collection journal at this root")
        connection = sqlite3.connect(database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        journal = cls(root_path, connection)
        try:
            check_contract_version(
                journal._meta("format_version"),
                SUPPORTED_PROSPECTIVE_COLLECTION_FORMATS,
                "prospective collection journal",
            )
        except BaseException:
            connection.close()
            raise
        return journal

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ProspectiveCollectionJournal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise IntegrityError("prospective collection metadata is incomplete")
        return str(row["value"])

    def plan(self) -> ProspectiveCollectionPlan:
        from latent_compass.prospective_collection.admission import admit_prospective_plan

        return admit_prospective_plan(json.loads(self._meta("plan")))

    def _generation(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def _root_seal(self) -> str:
        row = self._conn.execute(
            "SELECT chain_seal FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return self._meta("genesis") if row is None else str(row["chain_seal"])

    def _cases(self) -> tuple[CollectionCase, ...]:
        cases: dict[str, CollectionCase] = {}
        for row in self._conn.execute(
            "SELECT event_kind,case_id,payload FROM events WHERE case_id IS NOT NULL ORDER BY seq"
        ):
            payload = json.loads(str(row["payload"]))
            case_id = str(row["case_id"])
            if row["event_kind"] == "ENROLL":
                cases[case_id] = validate_contract(
                    CollectionCase,
                    payload,
                    error=ProspectiveCollectionViolation,
                    context="prospective collection case",
                )
            else:
                previous = cases[case_id].canonical_payload()
                previous.update(payload)
                cases[case_id] = validate_contract(
                    CollectionCase,
                    previous,
                    error=ProspectiveCollectionViolation,
                    context="prospective collection case",
                )
        return tuple(cases[key] for key in sorted(cases))

    def status(self) -> CollectionStatus:
        cases = self._cases()
        return CollectionStatus(
            plan_id=self.plan().plan_id,
            plan_seal=self._meta("plan_seal"),
            state=PlanState(self._meta("state")),
            generation=self._generation(),
            enrolled_count=len(cases),
            terminal_count=sum(case.state is not CaseState.ENROLLED for case in cases),
            root_seal=self._root_seal(),
        )

    def _append(
        self,
        *,
        event_kind: str,
        payload: dict[str, object],
        appended_at: str,
        case_id: str | None = None,
        case_state: CaseState | None = None,
        expected_generation: int | None = None,
        new_plan_state: PlanState | None = None,
        validate_unlocked: Callable[[], None] | None = None,
    ) -> tuple[int, str]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if expected_generation is not None and expected_generation != self._generation():
                raise ProspectiveCollectionViolation(
                    "collection generation changed before append",
                    detail={"expected": expected_generation, "observed": self._generation()},
                )
            integrity = self._verify_unlocked()
            if not integrity.ok:
                raise IntegrityError(
                    "refusing to append to a prospective collection journal that fails integrity",
                    detail={"findings": list(integrity.findings)},
                )
            if validate_unlocked is not None:
                validate_unlocked()
            latest = self._conn.execute(
                "SELECT appended_at FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if latest is not None and appended_at < str(latest["appended_at"]):
                raise ProspectiveCollectionViolation(
                    "journal append timestamps must be globally monotonic"
                )
            seq = self._generation() + 1
            previous = self._root_seal()
            content = seal("prospective-collection.content.v1", payload)
            chain = seal(
                EVENT_DOMAIN,
                {
                    "seq": seq,
                    "event_kind": event_kind,
                    "case_id": case_id,
                    "case_state": case_state.value if case_state is not None else None,
                    "content_seal": content,
                    "prev_chain_seal": previous,
                    "appended_at": appended_at,
                },
            )
            self._conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    event_kind,
                    case_id,
                    case_state.value if case_state is not None else None,
                    _json(payload),
                    content,
                    previous,
                    chain,
                    appended_at,
                ),
            )
            if new_plan_state is not None:
                self._conn.execute(
                    "UPDATE meta SET value=? WHERE key='state'", (new_plan_state.value,)
                )
            anchored_state = new_plan_state or PlanState(self._meta("state"))
            anchor = {
                "anchor_generation": str(seq),
                "anchor_tail": chain,
                "anchor_state": anchored_state.value,
                "anchor_seal": _anchor_seal(
                    plan_seal=self._meta("plan_seal"),
                    generation=seq,
                    tail=chain,
                    state=anchored_state,
                ),
            }
            self._conn.executemany(
                "INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                sorted(anchor.items()),
            )
            self._conn.execute("COMMIT")
            return seq, chain
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def start(
        self, *, clock: Clock = utc_now, expected_generation: int | None = None
    ) -> CollectionStatus:
        now = _stamp(clock)
        plan = self.plan()
        if PlanState(self._meta("state")) is not PlanState.SEALED:
            raise ProspectiveCollectionViolation("only a sealed plan can start collection")
        if now < plan.collection_not_before:
            raise ProspectiveCollectionViolation("collection cannot start before its sealed date")
        if now > plan.hard_calendar_end:
            raise ProspectiveCollectionViolation("collection cannot start after its hard end")

        def validate_start() -> None:
            if PlanState(self._meta("state")) is not PlanState.SEALED:
                raise ProspectiveCollectionViolation("only a sealed plan can start collection")
            if now < plan.collection_not_before or now > plan.hard_calendar_end:
                raise ProspectiveCollectionViolation(
                    "collection start falls outside the sealed calendar"
                )

        self._append(
            event_kind="PLAN_STATE",
            payload={"state": PlanState.COLLECTING.value},
            appended_at=now,
            expected_generation=expected_generation,
            new_plan_state=PlanState.COLLECTING,
            validate_unlocked=validate_start,
        )
        return self.status()

    def enroll(
        self,
        decision: StrategicDecisionRecord,
        *,
        stratum: str,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> CollectionReceipt:
        now = _stamp(clock)
        plan = self.plan()
        if PlanState(self._meta("state")) is not PlanState.COLLECTING:
            raise ProspectiveCollectionViolation("enrollment requires a collecting plan")
        if decision.binding != plan.source_binding.decision:
            raise ProspectiveCollectionViolation(
                "decision belongs to the wrong sealed source binding"
            )
        if (
            decision.captured_at < plan.collection_not_before
            or decision.captured_at > plan.hard_calendar_end
        ):
            raise ProspectiveCollectionViolation(
                "pre-plan, backfilled, or post-window decision refused"
            )
        if now < decision.captured_at or now > plan.hard_calendar_end:
            raise ProspectiveCollectionViolation(
                "enrollment timestamp falls outside the prospective window"
            )
        if stratum not in plan.declared_strata:
            raise ProspectiveCollectionViolation("case stratum was not declared before collection")
        if decision.decision_authority not in plan.independence_policy.decision_producer_identities:
            raise ProspectiveCollectionViolation("decision producer identity was not preregistered")
        if len(self._cases()) >= plan.max_enrollments:
            raise ProspectiveCollectionViolation("sealed maximum enrollment has been reached")
        from latent_compass.decision_reconciliation import DecisionPreimageReference

        preimage_model = DecisionPreimageReference(
            decision_binding=decision.binding,
            decision_id=decision.decision_id,
            decision_revision=decision.revision,
            record_seal=decision.record_seal(),
            projection_seal=decision.projection.projection_seal(),
        )
        preimage_seal = preimage_reference_seal(preimage_model)
        case_id = f"case:{decision.decision_id}:r{decision.revision}"
        if any(
            case.case_id == case_id or case.preimage_seal == preimage_seal for case in self._cases()
        ):
            raise ProspectiveCollectionViolation("this exact decision preimage is already enrolled")
        case = CollectionCase(
            case_id=case_id,
            state=CaseState.ENROLLED,
            stratum=stratum,
            decision_id=decision.decision_id,
            decision_revision=decision.revision,
            preimage_seal=preimage_seal,
            decision_record_seal=decision.record_seal(),
            decision_record=decision.canonical_payload(),
            decision_authority=decision.decision_authority,
            enrolled_at=now,
            terminal_at=None,
            terminal_reason=None,
            reconciliation_id=None,
            reconciliation_revision=None,
            reconciliation_record_seal=None,
            reconciliation=None,
            reconciliation_source=None,
        )

        def validate_enrollment() -> None:
            if PlanState(self._meta("state")) is not PlanState.COLLECTING:
                raise ProspectiveCollectionViolation("enrollment requires a collecting plan")
            cases = self._cases()
            if len(cases) >= plan.max_enrollments:
                raise ProspectiveCollectionViolation("sealed maximum enrollment has been reached")
            if any(
                item.case_id == case_id or item.preimage_seal == preimage_seal for item in cases
            ):
                raise ProspectiveCollectionViolation(
                    "this exact decision preimage is already enrolled"
                )
            if now < decision.captured_at or now > plan.hard_calendar_end:
                raise ProspectiveCollectionViolation(
                    "enrollment timestamp falls outside the prospective window"
                )

        seq, chain = self._append(
            event_kind="ENROLL",
            case_id=case_id,
            case_state=CaseState.ENROLLED,
            payload=case.canonical_payload(),
            appended_at=now,
            expected_generation=expected_generation,
            validate_unlocked=validate_enrollment,
        )
        return CollectionReceipt(
            seq=seq,
            case_id=case_id,
            state=CaseState.ENROLLED,
            generation=seq,
            chain_seal=chain,
            appended_at=now,
        )

    def _mark_terminal(
        self,
        case_id: str,
        *,
        state: CaseState,
        reason: str,
        expected_generation: int | None,
        clock: Clock,
    ) -> CollectionReceipt:
        if state not in (CaseState.CANCELLED, CaseState.LOST_TO_FOLLOWUP):
            raise ProspectiveCollectionViolation("unsupported non-reconciliation terminal state")
        screen_persisted_text(reason=reason)
        validated_reason = validate_contract(
            _ReasonPayload,
            {"reason": reason},
            error=ProspectiveCollectionViolation,
            context="case terminal reason",
        ).reason
        now = _stamp(clock)
        payload: dict[str, object] = {
            "state": state.value,
            "terminal_at": now,
            "terminal_reason": validated_reason,
        }

        def validate_terminal() -> None:
            if PlanState(self._meta("state")) is not PlanState.COLLECTING:
                raise ProspectiveCollectionViolation("terminalization requires a collecting plan")
            current = next((item for item in self._cases() if item.case_id == case_id), None)
            if current is None or current.state is not CaseState.ENROLLED:
                raise ProspectiveCollectionViolation("case is absent or already terminal")
            plan = self.plan()
            if now < current.enrolled_at or now > plan.hard_calendar_end:
                raise ProspectiveCollectionViolation(
                    "terminal timestamp falls outside the enrolled case window"
                )

        seq, chain = self._append(
            event_kind="TERMINAL",
            case_id=case_id,
            case_state=state,
            payload=payload,
            appended_at=now,
            expected_generation=expected_generation,
            validate_unlocked=validate_terminal,
        )
        return CollectionReceipt(
            seq=seq,
            case_id=case_id,
            state=state,
            generation=seq,
            chain_seal=chain,
            appended_at=now,
        )

    def mark_cancelled(
        self,
        case_id: str,
        *,
        reason: str,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> CollectionReceipt:
        return self._mark_terminal(
            case_id,
            state=CaseState.CANCELLED,
            reason=reason,
            expected_generation=expected_generation,
            clock=clock,
        )

    def mark_lost_to_followup(
        self,
        case_id: str,
        *,
        reason: str,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> CollectionReceipt:
        return self._mark_terminal(
            case_id,
            state=CaseState.LOST_TO_FOLLOWUP,
            reason=reason,
            expected_generation=expected_generation,
            clock=clock,
        )

    def close_collection(
        self, *, expected_generation: int | None = None, clock: Clock = utc_now
    ) -> CollectionStatus:
        now = _stamp(clock)
        if PlanState(self._meta("state")) is not PlanState.COLLECTING:
            raise ProspectiveCollectionViolation("only a collecting plan can close")
        plan = self.plan()
        cases = self._cases()
        required = plan.power.required_enrollments
        if required is None:
            raise ProspectiveCollectionViolation("power is not established")
        sufficient = (
            len(cases) >= required
            and all(case.state is not CaseState.ENROLLED for case in cases)
            and now >= plan.minimum_calendar_end
        )
        if sufficient:
            state = PlanState.CLOSED_SUFFICIENT
        elif now >= plan.hard_calendar_end:
            state = PlanState.CLOSED_INSUFFICIENT
        else:
            raise ProspectiveCollectionViolation(
                "collection cannot close before its preregistered calendar condition"
            )

        def validate_closure() -> None:
            if PlanState(self._meta("state")) is not PlanState.COLLECTING:
                raise ProspectiveCollectionViolation("only a collecting plan can close")
            current_cases = self._cases()
            current_sufficient = (
                len(current_cases) >= required
                and all(item.state is not CaseState.ENROLLED for item in current_cases)
                and now >= plan.minimum_calendar_end
            )
            expected_state = (
                PlanState.CLOSED_SUFFICIENT
                if current_sufficient
                else PlanState.CLOSED_INSUFFICIENT
                if now >= plan.hard_calendar_end
                else None
            )
            if expected_state is None or expected_state is not state:
                raise ProspectiveCollectionViolation(
                    "collection state changed before the closure append"
                )

        self._append(
            event_kind="PLAN_STATE",
            payload={"state": state.value},
            appended_at=now,
            expected_generation=expected_generation,
            new_plan_state=state,
            validate_unlocked=validate_closure,
        )
        return self.status()

    def abort(
        self,
        *,
        reason: AbortReason,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> CollectionStatus:
        if PlanState(self._meta("state")) not in (PlanState.SEALED, PlanState.COLLECTING):
            raise ProspectiveCollectionViolation("a closed collection cannot be aborted")
        now = _stamp(clock)

        def validate_abort() -> None:
            if PlanState(self._meta("state")) not in (PlanState.SEALED, PlanState.COLLECTING):
                raise ProspectiveCollectionViolation("a closed collection cannot be aborted")

        self._append(
            event_kind="PLAN_STATE",
            payload={"state": PlanState.ABORTED.value, "reason": reason.value},
            appended_at=now,
            expected_generation=expected_generation,
            new_plan_state=PlanState.ABORTED,
            validate_unlocked=validate_abort,
        )
        return self.status()

    def manifest(self) -> CollectionManifest:
        from latent_compass.prospective_collection.report import build_collection_manifest

        self._require_publishable()
        return build_collection_manifest(self.plan(), self.status(), self._cases())

    def report(self) -> CollectionDiagnosticReport:
        from latent_compass.prospective_collection.report import build_collection_report

        manifest = self.manifest()
        return build_collection_report(self.plan(), manifest, self._cases())

    def _require_publishable(self) -> None:
        report = self.verify()
        if not report.ok:
            raise IntegrityError(
                "refusing collection publication from a journal that fails integrity",
                detail={"findings": list(report.findings)},
            )
        state = PlanState(self._meta("state"))
        if state not in (PlanState.CLOSED_SUFFICIENT, PlanState.CLOSED_INSUFFICIENT):
            raise ProspectiveCollectionViolation(
                "collection publication requires a verified terminal non-aborted state"
            )

    def verify(self) -> CollectionIntegrityReport:
        self._conn.execute("BEGIN")
        try:
            return self._verify_unlocked()
        finally:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

    def _verify_unlocked(self) -> CollectionIntegrityReport:
        findings: list[str] = []
        plan: ProspectiveCollectionPlan | None = None
        try:
            plan = self.plan()
            if plan.plan_seal() != self._meta("plan_seal"):
                findings.append("PLAN_SEAL_MISMATCH")
        except Exception:
            findings.append("PLAN_UNREADABLE")
        previous = self._meta("genesis")
        replayed_state = PlanState.SEALED
        expected_seq = 1
        generation = 0
        previous_appended_at: str | None = None
        cases: dict[str, CollectionCase] = {}
        decisions: dict[str, StrategicDecisionRecord] = {}
        preimages: set[str] = set()
        for row in self._conn.execute("SELECT * FROM events ORDER BY seq"):
            generation += 1
            seq = int(row["seq"])
            if seq != expected_seq:
                findings.append("SEQUENCE_INVALID")
            appended_at = str(row["appended_at"])
            try:
                _Stamp(at=appended_at)
            except Exception:
                findings.append("CHRONOLOGY_INVALID")
            if previous_appended_at is not None and appended_at < previous_appended_at:
                findings.append("CHRONOLOGY_INVALID")
            previous_appended_at = appended_at

            payload: dict[str, object] | None = None
            try:
                decoded = json.loads(str(row["payload"]))
                if type(decoded) is not dict:
                    raise TypeError("event payload is not an object")
                payload = decoded
            except (json.JSONDecodeError, TypeError, ValueError):
                findings.append("PAYLOAD_UNPARSEABLE")

            event_kind = str(row["event_kind"])
            case_id = None if row["case_id"] is None else str(row["case_id"])
            case_state = None if row["case_state"] is None else str(row["case_state"])
            if payload is not None and event_kind == "PLAN_STATE":
                try:
                    raw_state = payload["state"]
                    if not isinstance(raw_state, str):
                        raise TypeError("plan state is not text")
                    target = PlanState(raw_state)
                except (KeyError, ValueError, TypeError):
                    findings.append("PLAN_STATE_INVALID")
                else:
                    legal = (
                        replayed_state is PlanState.SEALED
                        and target in (PlanState.COLLECTING, PlanState.ABORTED)
                    ) or (
                        replayed_state is PlanState.COLLECTING
                        and target
                        in (
                            PlanState.CLOSED_SUFFICIENT,
                            PlanState.CLOSED_INSUFFICIENT,
                            PlanState.ABORTED,
                        )
                    )
                    if not legal or case_id is not None or case_state is not None:
                        findings.append("SEMANTIC_TRANSITION_INVALID")
                    else:
                        exact = True
                        required = plan.power.required_enrollments if plan is not None else None
                        sufficient = (
                            required is not None
                            and len(cases) >= required
                            and all(item.state is not CaseState.ENROLLED for item in cases.values())
                            and plan is not None
                            and appended_at >= plan.minimum_calendar_end
                        )
                        if target is PlanState.COLLECTING:
                            exact = (
                                set(payload) == {"state"}
                                and plan is not None
                                and plan.collection_not_before
                                <= appended_at
                                <= plan.hard_calendar_end
                            )
                        elif target is PlanState.CLOSED_SUFFICIENT:
                            exact = set(payload) == {"state"} and sufficient
                        elif target is PlanState.CLOSED_INSUFFICIENT:
                            exact = (
                                set(payload) == {"state"}
                                and plan is not None
                                and appended_at >= plan.hard_calendar_end
                                and not sufficient
                            )
                        elif target is PlanState.ABORTED:
                            try:
                                reason = payload["reason"]
                                if not isinstance(reason, str):
                                    raise TypeError
                                AbortReason(reason)
                            except (KeyError, TypeError, ValueError):
                                exact = False
                            else:
                                exact = set(payload) == {"state", "reason"}
                        if not exact:
                            findings.append("SEMANTIC_TRANSITION_INVALID")
                        else:
                            replayed_state = target
            elif payload is not None and event_kind == "ENROLL":
                try:
                    enrolled = validate_contract(
                        CollectionCase,
                        payload,
                        error=ProspectiveCollectionViolation,
                        context="persisted prospective enrollment",
                    )
                except Exception:
                    findings.append("PAYLOAD_UNPARSEABLE")
                else:
                    valid = True
                    if (
                        replayed_state is not PlanState.COLLECTING
                        or enrolled.state is not CaseState.ENROLLED
                        or case_id != enrolled.case_id
                        or case_state != CaseState.ENROLLED.value
                        or enrolled.enrolled_at != appended_at
                    ):
                        findings.append("SEMANTIC_TRANSITION_INVALID")
                        valid = False
                    if enrolled.case_id in cases:
                        findings.append("DUPLICATE_CASE")
                        valid = False
                    if enrolled.preimage_seal in preimages:
                        findings.append("DUPLICATE_PREIMAGE")
                        valid = False
                    decision: StrategicDecisionRecord | None = None
                    try:
                        decision = admit_strategic_decision(enrolled.decision_record)
                        expected_preimage = DecisionPreimageReference(
                            decision_binding=decision.binding,
                            decision_id=decision.decision_id,
                            decision_revision=decision.revision,
                            record_seal=decision.record_seal(),
                            projection_seal=decision.projection.projection_seal(),
                        )
                        if (
                            plan is None
                            or decision.binding != plan.source_binding.decision
                            or decision.record_seal() != enrolled.decision_record_seal
                            or preimage_reference_seal(expected_preimage) != enrolled.preimage_seal
                            or decision.decision_id != enrolled.decision_id
                            or decision.revision != enrolled.decision_revision
                            or decision.decision_authority != enrolled.decision_authority
                            or decision.decision_authority
                            not in plan.independence_policy.decision_producer_identities
                            or decision.captured_at < plan.collection_not_before
                            or decision.captured_at > enrolled.enrolled_at
                            or enrolled.stratum not in plan.declared_strata
                            or len(cases) >= plan.max_enrollments
                        ):
                            raise ValueError("enrollment does not reproduce from its decision")
                    except Exception:
                        findings.append("ENROLLMENT_INVALID")
                        valid = False
                    if plan is not None and not (
                        plan.collection_not_before <= enrolled.enrolled_at <= plan.hard_calendar_end
                    ):
                        findings.append("CHRONOLOGY_INVALID")
                        valid = False
                    if valid:
                        cases[enrolled.case_id] = enrolled
                        if decision is not None:
                            decisions[enrolled.case_id] = decision
                        preimages.add(enrolled.preimage_seal)
            elif payload is not None and event_kind == "TERMINAL":
                current = cases.get(case_id or "")
                try:
                    if case_state is None:
                        raise TypeError("case state is absent")
                    target_state = CaseState(case_state)
                except (TypeError, ValueError):
                    target_state = CaseState.ENROLLED
                    findings.append("SEMANTIC_TRANSITION_INVALID")
                if (
                    current is None
                    or current.state is not CaseState.ENROLLED
                    or target_state is CaseState.ENROLLED
                ):
                    findings.append("SEMANTIC_TRANSITION_INVALID")
                else:
                    merged = current.canonical_payload()
                    merged.update(payload)
                    try:
                        terminal = validate_contract(
                            CollectionCase,
                            merged,
                            error=ProspectiveCollectionViolation,
                            context="persisted prospective terminal",
                        )
                    except Exception:
                        findings.append("PAYLOAD_UNPARSEABLE")
                    else:
                        semantic_valid = True
                        if (
                            terminal.state is not target_state
                            or terminal.terminal_at != appended_at
                            or terminal.terminal_at < current.enrolled_at
                            or (plan is not None and terminal.terminal_at > plan.hard_calendar_end)
                        ):
                            findings.append("CHRONOLOGY_INVALID")
                            semantic_valid = False
                        decision = decisions.get(current.case_id)
                        if target_state in (CaseState.RECONCILED, CaseState.ABSTAINED):
                            try:
                                if (
                                    terminal.reconciliation is None
                                    or terminal.reconciliation_source is None
                                ):
                                    raise ValueError("durable reconciliation proof is absent")
                                if decision is None or plan is None:
                                    raise ValueError("decision or plan is absent")
                                reconciliation = admit_reconciliation(terminal.reconciliation)
                                require_preimage_match(reconciliation, decision)
                                source = terminal.reconciliation_source
                                identities = set(
                                    plan.independence_policy.decision_producer_identities
                                )
                                producers = {reconciliation.reconciled_by}
                                producers.update(
                                    item.provenance.producer
                                    for item in reconciliation.observations
                                    if item.provenance is not None
                                )
                                expected_state = (
                                    CaseState.RECONCILED
                                    if reconciliation.execution_state is ExecutionState.EXECUTED
                                    else CaseState.ABSTAINED
                                )
                                if (
                                    reconciliation.binding != plan.source_binding.reconciliation
                                    or reconciliation.record_seal()
                                    != terminal.reconciliation_record_seal
                                    or reconciliation.reconciliation_id
                                    != terminal.reconciliation_id
                                    or reconciliation.revision != terminal.reconciliation_revision
                                    or source.entry_content_seal != reconciliation.record_seal()
                                    or source.entry_seq != source.journal_generation
                                    or source.entry_chain_seal != source.journal_root_seal
                                    or identities & producers
                                    or target_state is not expected_state
                                ):
                                    raise ValueError("embedded reconciliation does not reproduce")
                            except Exception:
                                findings.append("RECONCILIATION_INVALID")
                                semantic_valid = False
                        elif any(
                            value is not None
                            for value in (
                                terminal.reconciliation_id,
                                terminal.reconciliation_revision,
                                terminal.reconciliation_record_seal,
                                terminal.reconciliation,
                                terminal.reconciliation_source,
                            )
                        ):
                            findings.append("SEMANTIC_TRANSITION_INVALID")
                            semantic_valid = False
                        try:
                            if terminal.terminal_reason is None:
                                raise ValueError("terminal reason missing")
                            screen_persisted_text(reason=terminal.terminal_reason)
                        except Exception:
                            findings.append("PAYLOAD_UNPARSEABLE")
                            semantic_valid = False
                        if semantic_valid:
                            cases[current.case_id] = terminal
            elif payload is not None:
                findings.append("SEMANTIC_TRANSITION_INVALID")

            if payload is not None:
                content = seal("prospective-collection.content.v1", payload)
                if content != str(row["content_seal"]):
                    findings.append("CONTENT_SEAL_MISMATCH")
            chain = seal(
                EVENT_DOMAIN,
                {
                    "seq": seq,
                    "event_kind": event_kind,
                    "case_id": row["case_id"],
                    "case_state": row["case_state"],
                    "content_seal": str(row["content_seal"]),
                    "prev_chain_seal": str(row["prev_chain_seal"]),
                    "appended_at": str(row["appended_at"]),
                },
            )
            if str(row["prev_chain_seal"]) != previous or str(row["chain_seal"]) != chain:
                findings.append("CHAIN_BROKEN")
            previous = str(row["chain_seal"])
            expected_seq = seq + 1
        try:
            anchored_generation = int(self._meta("anchor_generation"))
            anchored_tail = self._meta("anchor_tail")
            anchored_state = PlanState(self._meta("anchor_state"))
            anchored_seal = self._meta("anchor_seal")
            expected_anchor = _anchor_seal(
                plan_seal=self._meta("plan_seal"),
                generation=generation,
                tail=previous,
                state=replayed_state,
            )
            if (
                anchored_generation != generation
                or anchored_tail != previous
                or anchored_state is not replayed_state
                or anchored_seal != expected_anchor
                or PlanState(self._meta("state")) is not replayed_state
            ):
                findings.append("ANCHOR_MISMATCH")
        except Exception:
            findings.append("ANCHOR_MISSING")
        return CollectionIntegrityReport(
            ok=not findings,
            generation=generation,
            root_seal=previous,
            findings=tuple(findings),
        )

    def reconcile(
        self,
        case_id: str,
        *,
        decision_record: StrategicDecisionRecord,
        source_journal: ReconciliationJournal,
        reconciliation_id: str,
        reconciliation_revision: int | None = None,
        expected_generation: int | None = None,
        clock: Clock = utc_now,
    ) -> CollectionReceipt:
        reconciliation, source_proof = _verified_reconciliation_source(
            source_journal,
            reconciliation_id=reconciliation_id,
            revision=reconciliation_revision,
        )
        now = _stamp(clock)
        plan = self.plan()
        if PlanState(self._meta("state")) is not PlanState.COLLECTING:
            raise ProspectiveCollectionViolation("reconciliation requires a collecting plan")
        case = next((item for item in self._cases() if item.case_id == case_id), None)
        if case is None or case.state is not CaseState.ENROLLED:
            raise ProspectiveCollectionViolation("case is absent or already terminal")
        if decision_record.record_seal() != case.decision_record_seal:
            raise ProspectiveCollectionViolation("supplied decision is not the enrolled preimage")
        if (
            reconciliation.reconciled_at < case.enrolled_at
            or reconciliation.reconciled_at > now
            or now > plan.hard_calendar_end
        ):
            raise ProspectiveCollectionViolation(
                "reconciliation chronology falls outside the prospective collection window"
            )
        try:
            require_preimage_match(reconciliation, decision_record)
        except Exception as exc:
            raise ProspectiveCollectionViolation(
                "reconciliation does not match the enrolled preimage"
            ) from exc
        if reconciliation.binding != plan.source_binding.reconciliation:
            raise ProspectiveCollectionViolation(
                "reconciliation belongs to the wrong sealed source"
            )
        identities = set(plan.independence_policy.decision_producer_identities)
        producers = {reconciliation.reconciled_by}
        producers.update(
            observation.provenance.producer
            for observation in reconciliation.observations
            if observation.provenance is not None
        )
        overlap = sorted(identities & producers)
        if overlap:
            raise ProspectiveCollectionViolation(
                "reconciliation producer is not syntactically independent",
                detail={"overlap": overlap, "claim": "syntactic_only"},
            )
        state = (
            CaseState.RECONCILED
            if reconciliation.execution_state is ExecutionState.EXECUTED
            else CaseState.ABSTAINED
        )
        payload: dict[str, object] = {
            "state": state.value,
            "terminal_at": now,
            "terminal_reason": (
                "execution observed" if state is CaseState.RECONCILED else "not executed"
            ),
            "reconciliation_id": reconciliation.reconciliation_id,
            "reconciliation_revision": reconciliation.revision,
            "reconciliation_record_seal": reconciliation.record_seal(),
            "reconciliation": reconciliation.canonical_payload(),
            "reconciliation_source": source_proof.canonical_payload(),
        }

        def validate_reconciliation_transition() -> None:
            if PlanState(self._meta("state")) is not PlanState.COLLECTING:
                raise ProspectiveCollectionViolation("reconciliation requires a collecting plan")
            current = next((item for item in self._cases() if item.case_id == case_id), None)
            if current is None or current.state is not CaseState.ENROLLED:
                raise ProspectiveCollectionViolation("case is absent or already terminal")
            if decision_record.record_seal() != current.decision_record_seal:
                raise ProspectiveCollectionViolation(
                    "supplied decision is not the enrolled preimage"
                )
            if (
                reconciliation.reconciled_at < current.enrolled_at
                or reconciliation.reconciled_at > now
                or now > plan.hard_calendar_end
            ):
                raise ProspectiveCollectionViolation(
                    "reconciliation chronology falls outside the collection window"
                )

        seq, chain = self._append(
            event_kind="TERMINAL",
            case_id=case_id,
            case_state=state,
            payload=payload,
            appended_at=now,
            expected_generation=expected_generation,
            validate_unlocked=validate_reconciliation_transition,
        )
        return CollectionReceipt(
            seq=seq,
            case_id=case_id,
            state=state,
            generation=seq,
            chain_seal=chain,
            appended_at=now,
        )


class _ReasonPayload(StrictModel):
    reason: Reason

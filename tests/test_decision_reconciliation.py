"""HOK-244 — proofs that the reconciliation journal observes without judging.

Three families of proof run here, and each one exists because the corresponding
failure would be invisible otherwise.

*Separation.* Every refusal proof asserts the same counters afterwards **and** that
the HOK-243 decision record is byte-identical after the journal has been written
to. A journal that records correctly but perturbs the store it references has
already broken the invariant this whole design exists to hold.

*Honesty about unknowns.* An unknown must never acquire a value, on any path — not
through the contract, not through a revision, not through replay. Four separate
tests attack that from four directions, because "the field happened to be None"
passes trivially.

*Absence of authority.* The package must contain no verb, name or flag that
selects, scores, ranks, promotes or authorises. That is asserted against the real
public surface and the real parser, not against a docstring.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import (
    AUTHORITY_REFERENCE,
    DECISION_CAPTURED_AT,
    DECISION_ID,
    EPOCH,
    EXECUTED_DIRECTION_ID,
    HOST_ID,
    OBSERVED_AT,
    RECONCILED_AT,
    RECONCILIATION_ID,
    STORE_ID,
    observation_set,
    observed_dimension,
    preimage_payload,
    reconciliation_payload,
    strategic_decision_payload,
    unknown_dimension,
)
from latent_compass.decision_memory import (
    DecisionMemoryStore,
    StrategicDecisionRecord,
    admit_strategic_decision,
)
from latent_compass.decision_reconciliation import (
    ComparisonLinkage,
    ObservationStatus,
    ReconciliationAppendReceipt,
    ReconciliationJournal,
    UnknownReason,
    admit_reconciliation,
    reconciliation_limits,
    replay_reconciliation,
)
from latent_compass.decision_reconciliation.store import (
    RECONCILIATION_DATABASE_FILENAME,
    ReconciliationIntegrityKind,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import (
    ContractViolation,
    EpochClosed,
    IntegrityError,
    LedgerError,
    PreimageMismatch,
    ProvenanceMismatch,
    ReconciliationRevisionConflict,
    ReconciliationViolation,
    SensitiveContentRefused,
    StoreNotFound,
    UnsupportedContractVersion,
)
from latent_compass.pairwise_capture import DIMENSION_ORDER

Clock = Callable[[], str]

UNKNOWN_REASONS = ("ABSENT", "LATE", "AMBIGUOUS", "DISPUTED")


def raw(journal: ReconciliationJournal) -> sqlite3.Connection:
    """The journal's own connection.

    Reaching past the API is the point: the threat model is an adversary with
    write access to the database file, and every corruption proof below has to
    write the way that adversary would.
    """
    return journal._conn  # noqa: SLF001 - deliberate: this is the threat model


def tamper(journal: ReconciliationJournal, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = raw(journal)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(sql, params)
    connection.execute("COMMIT")


def snapshot(journal: ReconciliationJournal) -> tuple[int, int, str]:
    """The three counters a refusal must never move."""
    return journal.entry_count(), journal.generation(), journal.root_seal()


def append_initial(
    journal: ReconciliationJournal, clock: Clock, **overrides: Any
) -> tuple[str, int]:
    payload = reconciliation_payload(**overrides)
    receipt = append_record(journal, payload, clock=clock)
    return receipt.content_seal, receipt.generation


def decision_for_reconciliation(payload: dict[str, Any]) -> StrategicDecisionRecord:
    """Reproduce the exact HOK-243 preimage named by a valid test payload."""
    preimage = cast(dict[str, Any], payload["preimage"])
    binding = cast(dict[str, Any], preimage["decision_binding"])
    return admit_strategic_decision(
        strategic_decision_payload(
            cast(str, preimage["decision_id"]),
            revision=cast(int, preimage["decision_revision"]),
            host_id=cast(str, binding["host_id"]),
            store_id=cast(str, binding["store_id"]),
            epoch=cast(str, binding["epoch"]),
            agent_family=cast(str, binding["agent_family"]),
        )
    )


def append_record(
    journal: ReconciliationJournal,
    payload: dict[str, Any],
    *,
    clock: Clock,
    decision_record: StrategicDecisionRecord | None = None,
    **kwargs: Any,
) -> ReconciliationAppendReceipt:
    """Test helper that always supplies the required exact pre-action record."""
    return journal.append_reconciliation(
        payload,
        decision_record=decision_record or decision_for_reconciliation(payload),
        clock=clock,
        **kwargs,
    )


def with_extra_field(name: str, value: object) -> dict[str, Any]:
    """A valid payload carrying exactly one additional, forbidden field name."""
    payload = reconciliation_payload()
    payload[name] = value
    return payload


def with_binding_field(name: str, value: str) -> dict[str, Any]:
    """A valid payload whose journal binding names one foreign identity field."""
    payload = reconciliation_payload()
    payload["binding"] = {**payload["binding"], name: value}
    return payload


def correction(head_seal: str, **overrides: Any) -> dict[str, Any]:
    """The canonical shape of an appended correction."""
    payload: dict[str, Any] = {
        "revision": 2,
        "revision_kind": "CORRECTION",
        "revision_reason": "the cost source was restated by its producer",
        "supersedes_revision_seal": head_seal,
    }
    payload.update(overrides)
    return reconciliation_payload(**payload)


# -- admission and the five dimensions --------------------------------------


def test_a_complete_reconciliation_is_admitted_and_seals_reproducibly() -> None:
    record = admit_reconciliation(reconciliation_payload())
    again = admit_reconciliation(reconciliation_payload())
    assert record.record_seal() == again.record_seal()
    assert record.record_seal().startswith("sha256:")


def test_the_reconciliation_seal_is_domain_separated_from_the_decision_seal() -> None:
    """The two contracts must not be able to produce a colliding seal."""
    record = admit_reconciliation(reconciliation_payload())
    decision = admit_strategic_decision(strategic_decision_payload())
    assert record.record_seal() != decision.record_seal()
    assert record.record_seal() != decision.projection.projection_seal()


def test_exactly_the_five_dimensions_are_carried_in_canonical_order() -> None:
    record = admit_reconciliation(reconciliation_payload())
    assert tuple(item.dimension for item in record.observations) == DIMENSION_ORDER


@pytest.mark.parametrize(
    "observations",
    [
        pytest.param(observation_set()[:4], id="one-dimension-missing"),
        pytest.param([*observation_set(), observed_dimension("SUCCESS")], id="six-entries"),
        pytest.param(
            [observed_dimension(name) for name in ("VIOLATION", "SUCCESS")] + observation_set()[2:],
            id="out-of-canonical-order",
        ),
        pytest.param(
            [observed_dimension("SUCCESS")] * 5,
            id="one-dimension-repeated",
        ),
    ],
)
def test_an_incomplete_or_reordered_dimension_set_is_refused(
    observations: list[dict[str, Any]],
) -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


@pytest.mark.parametrize("reason", UNKNOWN_REASONS)
def test_every_explicit_unknown_reason_is_admissible_and_carries_no_value(reason: str) -> None:
    """All four reasons are first-class. None of them may acquire a value."""
    record = admit_reconciliation(
        reconciliation_payload(observations=observation_set(unknowns={"COST": reason}))
    )
    cost = next(item for item in record.observations if item.dimension.value == "COST")
    assert cost.status is ObservationStatus.UNKNOWN
    assert cost.unknown_reason is UnknownReason(reason)
    assert (cost.value, cost.statement, cost.provenance) == (None, None, None)


@pytest.mark.parametrize("reason", UNKNOWN_REASONS)
@pytest.mark.parametrize(
    "smuggled",
    [
        pytest.param({"value": {"kind": "FLOAT", "float_value": 0.0}}, id="value"),
        pytest.param({"statement": "cost was probably fine"}, id="statement"),
        pytest.param(
            {
                "provenance": {
                    "source_id": "source-cost",
                    "source_digest": "sha256:" + "c" * 64,
                    "observed_at": OBSERVED_AT,
                    "producer": "observer-agent-v1",
                    "confidence": 0.5,
                }
            },
            id="provenance",
        ),
    ],
)
def test_an_unknown_dimension_never_acquires_a_value(reason: str, smuggled: dict[str, Any]) -> None:
    """ABSENT, LATE, AMBIGUOUS and DISPUTED are facts, not placeholders."""
    observations = observation_set()
    observations[2] = unknown_dimension("COST", reason, **smuggled)
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


@pytest.mark.parametrize("field", ["statement", "value", "provenance"])
def test_an_observed_dimension_requires_its_whole_provenance(field: str) -> None:
    observations = observation_set()
    dropped = observed_dimension("COST")
    dropped[field] = None
    observations[2] = dropped
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


@pytest.mark.parametrize("field", ["source_id", "source_digest", "observed_at", "producer"])
def test_observed_provenance_is_complete_or_the_observation_is_refused(field: str) -> None:
    observations = observation_set()
    provenance = dict(observed_dimension("COST")["provenance"])
    del provenance[field]
    observations[2] = observed_dimension("COST", provenance=provenance)
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "0.9"])
def test_confidence_must_be_a_real_number_in_the_unit_interval(confidence: object) -> None:
    observations = observation_set()
    out_of_range = observed_dimension("COST")
    out_of_range["provenance"] = {**out_of_range["provenance"], "confidence": confidence}
    observations[2] = out_of_range
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


def test_an_observation_may_not_postdate_the_record_that_reports_it() -> None:
    observations = observation_set()
    observations[2] = observed_dimension("COST", observed_at="2027-01-01T00:00:00Z")
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


def test_an_unknown_reason_outside_the_closed_set_is_refused() -> None:
    observations = observation_set()
    observations[2] = unknown_dimension("COST", "PROBABLY_FINE")
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=observations))


# -- authorization, execution and the counterfactual refusal -----------------


def test_authorization_and_execution_are_independent_axes() -> None:
    """An execution without authorisation is exactly what an audit needs to see."""
    record = admit_reconciliation(
        reconciliation_payload(authorization_state="NOT_AUTHORIZED", authorization_reference=None)
    )
    assert record.authorization_state.value == "NOT_AUTHORIZED"
    assert record.execution_state.value == "EXECUTED"
    assert record.executed_direction_id == EXECUTED_DIRECTION_ID


def test_authorized_elsewhere_must_name_the_authority_it_defers_to() -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(authorization_reference=None))


@pytest.mark.parametrize("state", ["NOT_AUTHORIZED", "AUTHORIZATION_UNKNOWN"])
def test_only_authorized_elsewhere_names_an_external_authority(state: str) -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(
            reconciliation_payload(
                authorization_state=state, authorization_reference=AUTHORITY_REFERENCE
            )
        )


@pytest.mark.parametrize("state", ["NOT_EXECUTED", "EXECUTION_UNKNOWN"])
def test_no_observation_exists_for_an_unexecuted_candidate(state: str) -> None:
    """The counterfactual is unrepresentable, not merely discouraged."""
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(
            reconciliation_payload(
                execution_state=state,
                executed_direction_id=None,
                observations=observation_set(),
            )
        )


@pytest.mark.parametrize("state", ["NOT_EXECUTED", "EXECUTION_UNKNOWN"])
def test_an_unexecuted_reconciliation_names_no_direction_and_is_admissible(state: str) -> None:
    record = admit_reconciliation(
        reconciliation_payload(execution_state=state, executed_direction_id=None, observations=[])
    )
    assert record.observations == ()
    assert record.executed_direction_id is None


@pytest.mark.parametrize("state", ["NOT_EXECUTED", "EXECUTION_UNKNOWN"])
def test_only_an_executed_reconciliation_names_a_direction(state: str) -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(
            reconciliation_payload(
                execution_state=state,
                executed_direction_id=EXECUTED_DIRECTION_ID,
                observations=[],
            )
        )


def test_an_executed_reconciliation_must_name_a_direction_and_all_five_dimensions() -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(executed_direction_id=None))
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(observations=[]))


# -- forbidden vocabulary, bounds and credentials ----------------------------


@pytest.mark.parametrize(
    "name",
    ["score", "rank", "ranking", "reward", "preference", "winner", "verdict", "outcome"],
)
def test_a_judgment_vocabulary_field_name_is_refused_at_the_top_level(name: str) -> None:
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(with_extra_field(name, 1))


@pytest.mark.parametrize(
    "name", ["promote", "promotion", "authorization", "execution", "argv", "command", "capability"]
)
def test_an_authority_vocabulary_field_name_is_refused(name: str) -> None:
    """``authorization_state`` is admissible; a bare authority blob is not."""
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(with_extra_field(name, "anything"))


@pytest.mark.parametrize("name", ["counterfactual", "causal_effect", "uplift", "attribution"])
def test_a_causal_claim_field_name_is_refused(name: str) -> None:
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(with_extra_field(name, 0.4))


@pytest.mark.parametrize("name", ["holdout", "holdout_corpus_seal", "split"])
def test_holdout_metadata_is_refused(name: str) -> None:
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(with_extra_field(name, "HOLDOUT"))


def test_a_forbidden_field_name_is_refused_at_any_depth() -> None:
    observations = observation_set()
    observations[0] = observed_dimension("SUCCESS", statement="ok")
    observations[0]["provenance"] = {**observations[0]["provenance"], "score": 0.9}
    with pytest.raises(SensitiveContentRefused) as refusal:
        admit_reconciliation(reconciliation_payload(observations=observations))
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["count"] == 1


@pytest.mark.parametrize(
    "field", ["sensitivity", "impact_class_absent_is_not_a_thing_here", "contract_version"]
)
def test_a_missing_or_wrong_classification_is_refused_rather_than_assumed(field: str) -> None:
    if field == "sensitivity":
        with pytest.raises(SensitiveContentRefused):
            admit_reconciliation(reconciliation_payload(sensitivity="UNKNOWN"))
        payload = reconciliation_payload()
        del payload["sensitivity"]
        with pytest.raises(SensitiveContentRefused):
            admit_reconciliation(payload)
        return
    if field == "contract_version":
        payload = reconciliation_payload()
        del payload["contract_version"]
        with pytest.raises(ReconciliationViolation):
            admit_reconciliation(payload)
        return
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(reconciliation_payload(sensitivity="HOLDOUT"))


def test_an_unknown_contract_version_is_refused_as_such() -> None:
    with pytest.raises(UnsupportedContractVersion):
        admit_reconciliation(reconciliation_payload(contract_version="9.0.0"))


@pytest.mark.parametrize(
    "needle",
    [
        "AKIA" + "Z" * 16,
        "ghp_" + "b" * 36,
        "sk-" + "c" * 32,
        "xoxb-" + "1" * 12,
        # Split so this file does not match the repository's own secret scanner,
        # exactly as tests/test_decision_memory.py does.
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "Authorization: Bearer " + "d" * 24,
        "api_key = " + "e" * 20,
    ],
)
def test_a_known_credential_shape_is_refused_in_reconciliation_text(needle: str) -> None:
    observations = observation_set()
    observations[0] = observed_dimension("SUCCESS", statement=needle)
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(reconciliation_payload(observations=observations))


def test_the_refusal_never_echoes_the_material_it_refused() -> None:
    needle = "AKIA" + "Q" * 16
    observations = observation_set()
    observations[0] = observed_dimension("SUCCESS", statement=needle)
    with pytest.raises(SensitiveContentRefused) as refusal:
        admit_reconciliation(reconciliation_payload(observations=observations))
    rendered = json.dumps(refusal.value.as_dict())
    assert needle not in rendered
    assert "aws_access_key_id" in rendered


def test_oversized_text_is_refused_before_validation() -> None:
    observations = observation_set()
    observations[0] = observed_dimension("SUCCESS", statement="x" * 2001)
    with pytest.raises(SensitiveContentRefused):
        admit_reconciliation(reconciliation_payload(observations=observations))


def test_deep_nesting_is_a_typed_refusal_before_canonicalisation() -> None:
    nested: dict[str, Any] = {"leaf": True}
    for _ in range(64):
        nested = {"nested": nested}
    with pytest.raises(ReconciliationViolation) as refusal:
        admit_reconciliation(reconciliation_payload(unexpected=nested))
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["max_nesting_depth"] == 32


@pytest.mark.parametrize(
    "hidden",
    [
        pytest.param(({"score": 1},), id="forbidden-field"),
        pytest.param(("AKIA" + "T" * 16,), id="credential"),
        pytest.param(("x" * 2001,), id="oversized-text"),
        pytest.param(tuple({"nested": {"leaf": True}} for _ in range(40)), id="nested"),
    ],
)
def test_non_json_tuples_cannot_hide_screened_material(hidden: tuple[object, ...]) -> None:
    with pytest.raises(ReconciliationViolation) as refusal:
        admit_reconciliation(reconciliation_payload(unexpected=hidden))
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["received_type"] == "tuple"


def test_a_payload_that_is_not_a_json_object_is_refused() -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(["not", "an", "object"])


def test_named_credentials_are_refused_on_every_durable_metadata_surface(
    journal: ReconciliationJournal, fixed_clock: Clock, tmp_path: Path
) -> None:
    needle = "AKIA" + "Y" * 16
    fresh_root = tmp_path / "credential-journal"
    for store_id, host_id, epoch in (
        (needle, "host-test", "LC-E1"),
        ("store-test", needle, "LC-E1"),
        ("store-test", "host-test", needle),
    ):
        with pytest.raises(SensitiveContentRefused):
            ReconciliationJournal.create(
                fresh_root,
                store_id=store_id,
                host_id=host_id,
                agent_family=AgentFamily.CODEX,
                epoch=epoch,
            )
        assert not fresh_root.exists()

    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(SensitiveContentRefused):
        journal.abandon_epoch(reason=needle, clock=fixed_clock)
    assert snapshot(journal) == before
    assert journal.verify().ok


# -- confinement -------------------------------------------------------------


def test_a_journal_root_symlink_is_refused_before_any_database_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractViolation) as refusal:
        ReconciliationJournal.create(
            redirected,
            store_id="store-test",
            host_id="host-test",
            agent_family=AgentFamily.CODEX,
            epoch="LC-E1",
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "reparse_root"
    assert not (outside / RECONCILIATION_DATABASE_FILENAME).exists()


def test_a_database_file_symlink_is_refused_on_open(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside-journal"
    outside = ReconciliationJournal.create(
        outside_root,
        store_id="store-outside",
        host_id="host-test",
        agent_family=AgentFamily.CODEX,
        epoch="LC-E1",
    )
    outside.close()
    root = tmp_path / "real-root"
    root.mkdir()
    (root / RECONCILIATION_DATABASE_FILENAME).symlink_to(
        outside_root / RECONCILIATION_DATABASE_FILENAME
    )

    with pytest.raises(ContractViolation) as refusal:
        ReconciliationJournal.open(root)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "reparse_database"


# -- append, revisions and compare-and-set -----------------------------------


def test_the_initial_reconciliation_lands_and_becomes_the_head(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    seal, generation = append_initial(journal, fixed_clock)
    assert generation == 1
    assert journal.verify().ok

    head = journal.get(RECONCILIATION_ID)
    assert (head.revision, head.content_seal) == (1, seal)
    assert head.record.preimage.decision_id == DECISION_ID
    # The reconciliation carries no route, score, verdict or authority to carry.
    assert set(head.record.canonical_payload()) == {
        "authorization_reference",
        "authorization_state",
        "binding",
        "contract_version",
        "executed_direction_id",
        "execution_state",
        "observations",
        "preimage",
        "reconciled_at",
        "reconciled_by",
        "reconciliation_id",
        "revision",
        "revision_kind",
        "revision_reason",
        "sensitivity",
        "supersedes_revision_seal",
    }


def test_the_journal_is_a_separate_file_from_the_decision_memory(
    journal: ReconciliationJournal,
    journal_root: Path,
    decision_root: Path,
    fixed_clock: Clock,
) -> None:
    store = DecisionMemoryStore.create(
        decision_root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
        clock=fixed_clock,
    )
    with store:
        store.append_decision(strategic_decision_payload(), clock=fixed_clock)
        decision_bytes = (decision_root / "decision-memory.sqlite3").read_bytes()
        decision_root_seal = store.root_seal()

    append_initial(journal, fixed_clock)
    assert journal.root_seal() != decision_root_seal

    # The decision memory is byte-identical: the journal wrote nothing to it.
    assert (decision_root / "decision-memory.sqlite3").read_bytes() == decision_bytes
    assert (journal_root / RECONCILIATION_DATABASE_FILENAME).is_file()
    with DecisionMemoryStore.open(decision_root) as reopened:
        assert reopened.verify().ok
        assert reopened.root_seal() == decision_root_seal


@pytest.mark.parametrize("kind", ["CORRECTION", "DISAGREEMENT"])
def test_a_correction_and_a_disagreement_both_append(
    journal: ReconciliationJournal, fixed_clock: Clock, kind: str
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    receipt = append_record(
        journal,
        correction(
            head_seal,
            revision_kind=kind,
            observations=observation_set(unknowns={"COST": "DISPUTED"}),
        ),
        clock=fixed_clock,
    )
    assert (receipt.revision, receipt.generation, receipt.entry_count) == (2, 2, 2)
    assert journal.verify().ok
    # Both revisions remain readable: an appended disagreement removes nothing.
    history = journal.revisions(RECONCILIATION_ID)
    assert [item.revision for item in history] == [1, 2]
    assert history[0].content_seal == head_seal
    assert journal.get(RECONCILIATION_ID).revision == 2
    assert journal.get(RECONCILIATION_ID, revision=1).content_seal == head_seal


def test_a_correction_must_say_why_it_was_appended(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(ReconciliationViolation):
        append_record(journal, correction(head_seal, revision_reason=None), clock=fixed_clock)
    assert snapshot(journal) == before


def test_an_initial_revision_corrects_nothing_and_names_no_seal() -> None:
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(revision_reason="unasked for"))
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(reconciliation_payload(supersedes_revision_seal="sha256:" + "f" * 64))


@pytest.mark.parametrize("kind", ["INITIAL", "CORRECTION"])
def test_the_revision_number_and_the_revision_kind_must_agree(kind: str) -> None:
    if kind == "INITIAL":
        payload = reconciliation_payload(
            revision=2,
            revision_kind="INITIAL",
            revision_reason="x",
            supersedes_revision_seal="sha256:" + "f" * 64,
        )
    else:
        payload = reconciliation_payload(revision=1, revision_kind="CORRECTION")
    with pytest.raises(ReconciliationViolation):
        admit_reconciliation(payload)


@pytest.mark.parametrize(
    ("revision", "use_head_seal"),
    [
        (1, False),  # replayed initial reconciliation
        (2, False),  # right revision, wrong predecessor seal
        (3, True),  # fork past the head
    ],
)
def test_a_stale_or_forked_revision_is_refused_without_moving_the_journal(
    journal: ReconciliationJournal, fixed_clock: Clock, revision: int, use_head_seal: bool
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    before = snapshot(journal)
    offered = head_seal if use_head_seal else "sha256:" + "f" * 64

    with pytest.raises(ReconciliationRevisionConflict):
        append_record(
            journal,
            reconciliation_payload(
                revision=revision,
                revision_kind="INITIAL" if revision == 1 else "CORRECTION",
                revision_reason=None if revision == 1 else "restated",
                supersedes_revision_seal=None if revision == 1 else offered,
            ),
            clock=fixed_clock,
        )

    assert snapshot(journal) == before
    assert journal.verify().ok


def test_a_revision_may_not_re_point_at_another_preimage(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """A correction corrects observations, never which decision they were about."""
    head_seal, _ = append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(ReconciliationRevisionConflict) as refusal:
        append_record(
            journal,
            correction(head_seal, preimage=preimage_payload(decision_id="decision-strategic-0002")),
            clock=fixed_clock,
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "preimage_repointed"
    assert snapshot(journal) == before


def test_a_revision_without_an_initial_reconciliation_is_refused(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    before = snapshot(journal)
    with pytest.raises(ReconciliationRevisionConflict):
        append_record(journal, correction("sha256:" + "f" * 64), clock=fixed_clock)
    assert snapshot(journal) == before


def test_one_preimage_is_reconciled_by_exactly_one_identity(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """A second narrative over the same decision revision has no defined head."""
    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(ReconciliationRevisionConflict) as refusal:
        append_record(journal, reconciliation_payload("reconciliation-0002"), clock=fixed_clock)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "preimage_already_reconciled"
    assert snapshot(journal) == before
    assert journal.verify().ok


def test_a_different_preimage_gets_its_own_reconciliation(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    append_record(
        journal,
        reconciliation_payload(
            "reconciliation-0002",
            preimage=preimage_payload(decision_id="decision-strategic-0002"),
        ),
        clock=fixed_clock,
    )
    assert journal.reconciliation_count() == 2
    assert journal.verify().ok


def test_preimage_identity_uses_the_full_reference_not_only_id_and_revision(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    alternate_payload = strategic_decision_payload()
    alternate_projection = cast(dict[str, Any], alternate_payload["projection"])
    alternate_projection["objective_summary"] = "A distinct sealed pre-action capture."
    alternate = admit_strategic_decision(alternate_payload)
    alternate_preimage = preimage_payload(
        record_seal=alternate.record_seal(),
        projection_seal=alternate.projection.projection_seal(),
    )

    receipt = append_record(
        journal,
        reconciliation_payload("reconciliation-0002", preimage=alternate_preimage),
        decision_record=alternate,
        clock=fixed_clock,
    )

    first_preimage_seal = str(
        raw(journal)
        .execute("SELECT preimage_seal FROM reconciliations WHERE seq = 1")
        .fetchone()["preimage_seal"]
    )
    assert receipt.preimage_seal != first_preimage_seal
    assert journal.reconciliation_count() == 2
    assert journal.verify().ok


def test_expected_generation_is_a_compare_and_set(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(ReconciliationRevisionConflict) as refusal:
        append_record(
            journal,
            reconciliation_payload(
                "reconciliation-0002",
                preimage=preimage_payload(decision_id="decision-strategic-0002"),
            ),
            expected_generation=7,
            clock=fixed_clock,
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "generation_mismatch"
    assert snapshot(journal) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_id", "host-beta"),
        ("store_id", "store-beta"),
        ("epoch", "LC-OTHER"),
        ("agent_family", "codex"),
    ],
)
def test_a_reconciliation_bound_elsewhere_is_refused(
    journal: ReconciliationJournal, fixed_clock: Clock, field: str, value: str
) -> None:
    before = snapshot(journal)
    with pytest.raises(ProvenanceMismatch):
        append_record(journal, with_binding_field(field, value), clock=fixed_clock)
    assert snapshot(journal) == before


def test_an_abandoned_epoch_accepts_no_further_write(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    journal.abandon_epoch(reason="collection complete", clock=fixed_clock)
    before = snapshot(journal)
    with pytest.raises(EpochClosed):
        append_record(
            journal,
            reconciliation_payload(
                "reconciliation-0002",
                preimage=preimage_payload(decision_id="decision-strategic-0002"),
            ),
            clock=fixed_clock,
        )
    assert snapshot(journal) == before
    assert journal.verify().ok
    with pytest.raises(EpochClosed):
        journal.abandon_epoch(reason="again", clock=fixed_clock)


def test_codex_and_claude_journals_are_separate_files(
    journal: ReconciliationJournal, tmp_path: Path, fixed_clock: Clock
) -> None:
    codex_root = tmp_path / "codex-journal"
    codex = ReconciliationJournal.create(
        codex_root,
        store_id="store-codex",
        host_id=HOST_ID,
        agent_family=AgentFamily.CODEX,
        epoch=EPOCH,
        clock=fixed_clock,
    )
    try:
        assert codex.root_seal() != journal.root_seal(), "genesis seals must differ"
        append_initial(journal, fixed_clock)
        assert codex.generation() == 0, "nothing synchronises the two journals"
        with pytest.raises(ProvenanceMismatch):
            append_record(codex, reconciliation_payload(), clock=fixed_clock)
    finally:
        codex.close()


# -- mandatory exact preimage verification -----------------------------------


def test_every_append_is_verified_against_the_required_exact_preimage(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    verified = append_record(
        journal, reconciliation_payload(), decision_record=fixture_decision, clock=fixed_clock
    )
    assert verified.preimage_verified is True

    before = snapshot(journal)
    with pytest.raises(TypeError):
        journal.append_reconciliation(  # type: ignore[call-arg]
            reconciliation_payload("reconciliation-0002"), clock=fixed_clock
        )
    assert snapshot(journal) == before


def test_decision_family_crossing_is_refused_in_both_directions(
    journal: ReconciliationJournal, fixed_clock: Clock, tmp_path: Path
) -> None:
    codex_decision = admit_strategic_decision(strategic_decision_payload(agent_family="codex"))
    codex_preimage = preimage_payload(agent_family="codex")
    before = snapshot(journal)
    with pytest.raises(ProvenanceMismatch):
        append_record(
            journal,
            reconciliation_payload(preimage=codex_preimage),
            decision_record=codex_decision,
            clock=fixed_clock,
        )
    assert snapshot(journal) == before

    codex = ReconciliationJournal.create(
        tmp_path / "codex-crossing",
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CODEX,
        epoch=EPOCH,
        clock=fixed_clock,
    )
    try:
        with pytest.raises(ProvenanceMismatch):
            append_record(codex, reconciliation_payload(), clock=fixed_clock)
        assert codex.generation() == 0
    finally:
        codex.close()


@pytest.mark.parametrize(
    "preimage",
    [
        pytest.param(preimage_payload(decision_id="decision-strategic-0002"), id="decision-id"),
        pytest.param(preimage_payload(decision_revision=2), id="decision-revision"),
        pytest.param(preimage_payload(record_seal="sha256:" + "a" * 64), id="record-seal"),
        pytest.param(preimage_payload(projection_seal="sha256:" + "b" * 64), id="projection-seal"),
        pytest.param(preimage_payload(host_id="host-beta"), id="binding-host"),
        pytest.param(preimage_payload(store_id="store-beta"), id="binding-store"),
        pytest.param(preimage_payload(epoch="LC-OTHER"), id="binding-epoch"),
        pytest.param(preimage_payload(agent_family="codex"), id="binding-family"),
    ],
)
def test_a_wrong_preimage_is_refused_against_the_supplied_record(
    journal: ReconciliationJournal,
    fixed_clock: Clock,
    fixture_decision: StrategicDecisionRecord,
    preimage: dict[str, Any],
) -> None:
    before = snapshot(journal)
    with pytest.raises(PreimageMismatch):
        append_record(
            journal,
            reconciliation_payload(preimage=preimage),
            decision_record=fixture_decision,
            clock=fixed_clock,
        )
    assert snapshot(journal) == before
    assert journal.verify().ok


@pytest.mark.parametrize("case", ["reconciliation", "observation", "not-executed"])
def test_post_action_chronology_cannot_precede_the_exact_decision(
    journal: ReconciliationJournal,
    fixed_clock: Clock,
    fixture_decision: StrategicDecisionRecord,
    case: str,
) -> None:
    earlier = "2026-08-16T08:59:59Z"
    payload = reconciliation_payload()
    if case == "reconciliation":
        payload["reconciled_at"] = earlier
        payload["observations"] = [
            observed_dimension(dimension.value, observed_at=earlier)
            for dimension in DIMENSION_ORDER
        ]
    elif case == "observation":
        payload["observations"] = [
            observed_dimension(
                dimension.value,
                observed_at=earlier if dimension.value == "COST" else OBSERVED_AT,
            )
            for dimension in DIMENSION_ORDER
        ]
    else:
        payload.update(
            reconciled_at=earlier,
            execution_state="NOT_EXECUTED",
            executed_direction_id=None,
            observations=[],
        )
    # Each standalone record is internally valid. The missing fact is in its
    # exact decision preimage, so both admission to the journal and replay must
    # enforce the cross-record chronology before doing anything else.
    record = admit_reconciliation(payload)
    before = snapshot(journal)
    decision_before = fixture_decision.canonical_payload()
    with pytest.raises(PreimageMismatch, match="predates the pre-action capture"):
        replay_reconciliation(record, fixture_decision)
    with pytest.raises(PreimageMismatch, match="predates the pre-action capture"):
        append_record(journal, payload, decision_record=fixture_decision, clock=fixed_clock)
    assert snapshot(journal) == before
    assert fixture_decision.canonical_payload() == decision_before
    assert journal.verify().ok


def test_post_action_chronology_accepts_the_capture_time_boundary(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    payload = reconciliation_payload(
        reconciled_at=DECISION_CAPTURED_AT,
        observations=[
            observed_dimension(dimension.value, observed_at=DECISION_CAPTURED_AT)
            for dimension in DIMENSION_ORDER
        ],
    )
    receipt = append_record(journal, payload, decision_record=fixture_decision, clock=fixed_clock)
    assert receipt.preimage_verified
    assert (
        len(replay_reconciliation(admit_reconciliation(payload), fixture_decision).comparisons) == 5
    )
    assert journal.verify().ok


def test_an_executed_direction_must_be_a_candidate_of_the_referenced_preimage(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    before = snapshot(journal)
    with pytest.raises(PreimageMismatch) as refusal:
        append_record(
            journal,
            reconciliation_payload(executed_direction_id="direction-invented"),
            decision_record=fixture_decision,
            clock=fixed_clock,
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "executed_direction_not_a_candidate"
    assert snapshot(journal) == before


def test_verification_never_mutates_the_decision_record(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    before = fixture_decision.canonical_payload()
    seal_before = fixture_decision.record_seal()
    append_record(
        journal, reconciliation_payload(), decision_record=fixture_decision, clock=fixed_clock
    )
    assert fixture_decision.canonical_payload() == before
    assert fixture_decision.record_seal() == seal_before


# -- deterministic replay ----------------------------------------------------


def test_replay_places_the_exact_pre_action_facts_beside_the_observation(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    append_initial(journal, fixed_clock)
    report = journal.replay(RECONCILIATION_ID, fixture_decision)

    assert tuple(item.dimension for item in report.comparisons) == DIMENSION_ORDER
    assert report.executed_direction_id == EXECUTED_DIRECTION_ID
    candidate = next(
        item
        for item in fixture_decision.projection.candidates
        if item.direction_id == EXECUTED_DIRECTION_ID
    )
    expected = {item.dimension: item for item in candidate.evidence}
    for comparison in report.comparisons:
        evidence = expected[comparison.dimension]
        # Verbatim, not summarised: the pre-action side is the sealed side.
        assert comparison.pre_action_facts == evidence.facts
        assert comparison.pre_action_availability is evidence.availability
        assert comparison.linkage is ComparisonLinkage.BOTH_PRESENT


def test_replay_is_deterministic(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    append_initial(journal, fixed_clock)
    first = journal.replay(RECONCILIATION_ID, fixture_decision)
    second = journal.replay(RECONCILIATION_ID, fixture_decision)
    assert first.replay_seal == second.replay_seal
    assert first.canonical_payload() == second.canonical_payload()
    assert first.replay_seal != first.reconciliation_record_seal


def test_replay_marks_an_unknown_dimension_as_pre_action_only(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    """The pre-action side survives; the post-action side stays honestly empty."""
    append_initial(
        journal, fixed_clock, observations=observation_set(unknowns={"INFORMATION": "AMBIGUOUS"})
    )
    report = journal.replay(RECONCILIATION_ID, fixture_decision)
    comparison = next(item for item in report.comparisons if item.dimension.value == "INFORMATION")
    assert comparison.linkage is ComparisonLinkage.PRE_ACTION_ONLY
    assert comparison.observation.unknown_reason is UnknownReason.AMBIGUOUS
    assert comparison.observation.value is None
    assert comparison.pre_action_facts, "the pre-action evidence is still shown"


def test_replay_of_an_unexecuted_reconciliation_produces_no_comparison(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    append_initial(
        journal,
        fixed_clock,
        execution_state="NOT_EXECUTED",
        executed_direction_id=None,
        observations=[],
    )
    report = journal.replay(RECONCILIATION_ID, fixture_decision)
    assert report.comparisons == ()
    assert report.executed_direction_id is None


def test_replay_refuses_a_record_that_is_not_the_named_preimage(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    other = admit_strategic_decision(strategic_decision_payload("decision-strategic-0002"))
    with pytest.raises(PreimageMismatch):
        journal.replay(RECONCILIATION_ID, other)


def test_replay_carries_no_score_ranking_or_verdict(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    """The report may say what exists on each side; never which side wins."""
    append_initial(journal, fixed_clock)
    rendered = json.dumps(journal.replay(RECONCILIATION_ID, fixture_decision).canonical_payload())
    for forbidden in (
        '"score"',
        '"scores"',
        '"rank"',
        '"ranking"',
        '"verdict"',
        '"winner"',
        '"agreement"',
        '"preference"',
        '"reward"',
        '"causal',
        '"promot',
        '"authorized"',
    ):
        assert forbidden not in rendered, f"replay leaked {forbidden}"


def test_replay_of_a_named_revision_reads_that_revision(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    append_record(
        journal,
        correction(head_seal, observations=observation_set(unknowns={"COST": "DISPUTED"})),
        clock=fixed_clock,
    )
    first = journal.replay(RECONCILIATION_ID, fixture_decision, revision=1)
    second = journal.replay(RECONCILIATION_ID, fixture_decision)
    assert (first.revision, second.revision) == (1, 2)
    assert first.replay_seal != second.replay_seal
    cost = next(item for item in second.comparisons if item.dimension.value == "COST")
    assert cost.observation.unknown_reason is UnknownReason.DISPUTED


def test_replay_never_invents_a_comparison_for_an_unexecuted_candidate(
    journal: ReconciliationJournal, fixed_clock: Clock, fixture_decision: StrategicDecisionRecord
) -> None:
    """Only the executed candidate appears; the sibling is absent, not scored."""
    append_initial(journal, fixed_clock)
    report = journal.replay(RECONCILIATION_ID, fixture_decision)
    rendered = json.dumps(report.canonical_payload())
    other = next(
        item.direction_id
        for item in fixture_decision.projection.candidates
        if item.direction_id != EXECUTED_DIRECTION_ID
    )
    assert other not in rendered


def test_the_pure_replay_function_needs_no_store(
    fixture_decision: StrategicDecisionRecord,
) -> None:
    record = admit_reconciliation(reconciliation_payload())
    report = replay_reconciliation(record, fixture_decision)
    assert report.reconciliation_record_seal == record.record_seal()
    assert len(report.comparisons) == 5


# -- reads are bounded -------------------------------------------------------


def test_reads_are_bounded(journal: ReconciliationJournal, fixed_clock: Clock) -> None:
    append_initial(journal, fixed_clock)
    with pytest.raises(ContractViolation):
        journal.list_heads(limit=0)
    with pytest.raises(ContractViolation):
        journal.list_heads(limit=1001)
    with pytest.raises(ContractViolation):
        journal.list_heads(offset=-1)
    assert len(journal.list_heads(limit=1)) == 1


def test_an_unknown_reconciliation_is_refused_rather_than_served_empty(
    journal: ReconciliationJournal,
) -> None:
    with pytest.raises(StoreNotFound):
        journal.get("reconciliation-absent")
    with pytest.raises(StoreNotFound):
        journal.revisions("reconciliation-absent")


def test_list_returns_only_head_revisions(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    append_record(journal, correction(head_seal), clock=fixed_clock)
    heads = journal.list_heads()
    assert [(item.reconciliation_id, item.revision) for item in heads] == [(RECONCILIATION_ID, 2)]


# -- integrity, interruption and concurrency ---------------------------------


def test_an_altered_stored_payload_is_detected(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    altered = reconciliation_payload(reconciled_by="observer-impostor")
    tamper(
        journal,
        "UPDATE reconciliations SET payload = ? WHERE entry_id = ?",
        (json.dumps(altered, sort_keys=True), f"{RECONCILIATION_ID}:r1"),
    )
    report = journal.verify()
    assert not report.ok
    assert ReconciliationIntegrityKind.PAYLOAD_ALTERED in {
        finding.kind for finding in report.findings
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("entry_id", "reconciliation-9999:r1"),
        ("reconciliation_id", "reconciliation-9999"),
        ("revision", 9),
        ("decision_id", "decision-strategic-9999"),
        ("decision_revision", 9),
        ("record_seal", "sha256:" + "a" * 64),
        ("content_seal", "sha256:" + "b" * 64),
        ("prev_chain_seal", "sha256:" + "c" * 64),
        ("chain_seal", "sha256:" + "d" * 64),
        ("appended_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_tampering_with_any_semantic_column_breaks_integrity(
    journal: ReconciliationJournal, fixed_clock: Clock, column: str, value: object
) -> None:
    """Every column is inside the chain seal, so none of them is a free edit."""
    append_initial(journal, fixed_clock)
    tamper(
        journal,
        f"UPDATE reconciliations SET {column} = ? WHERE seq = 1",  # noqa: S608 - closed test data
        (value,),
    )
    report = journal.verify()
    assert not report.ok
    assert ReconciliationIntegrityKind.CHAIN_BROKEN in {finding.kind for finding in report.findings}


def test_relabelling_the_indexed_preimage_is_reported_as_such(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """The chain notices, and so does the preimage cross-check."""
    append_initial(journal, fixed_clock)
    tamper(
        journal,
        "UPDATE reconciliations SET decision_id = 'decision-strategic-9999' WHERE seq = 1",
    )
    kinds = {finding.kind for finding in journal.verify().findings}
    assert ReconciliationIntegrityKind.CHAIN_BROKEN in kinds
    assert ReconciliationIntegrityKind.PREIMAGE_MISMATCH in kinds


def test_a_truncated_suffix_is_detected_by_the_anchor(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    head_seal, _ = append_initial(journal, fixed_clock)
    append_record(journal, correction(head_seal), clock=fixed_clock)
    tamper(journal, "DELETE FROM reconciliations WHERE seq = 2")
    report = journal.verify()
    assert not report.ok
    assert ReconciliationIntegrityKind.ANCHOR_MISMATCH in {
        finding.kind for finding in report.findings
    }


def test_a_relabelled_anchor_does_not_reproduce(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    tamper(journal, "UPDATE store_meta SET value = '9' WHERE key = 'anchor_generation'")
    assert not journal.verify().ok
    tamper(journal, "DELETE FROM store_meta WHERE key = 'anchor_seal'")
    assert ReconciliationIntegrityKind.ANCHOR_MISSING in {
        finding.kind for finding in journal.verify().findings
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("host_id", "host-beta"),
        ("store_id", "store-beta"),
        ("epoch", "LC-OTHER"),
        ("agent_family", "codex"),
    ],
)
def test_relabelling_the_binding_breaks_the_chain_instead_of_renaming_history(
    journal: ReconciliationJournal, fixed_clock: Clock, key: str, value: str
) -> None:
    append_initial(journal, fixed_clock)
    tamper(journal, f"UPDATE store_meta SET value = ? WHERE key = '{key}'", (value,))  # noqa: S608
    report = journal.verify()
    assert not report.ok
    kinds = {finding.kind for finding in report.findings}
    assert ReconciliationIntegrityKind.CHAIN_BROKEN in kinds
    assert ReconciliationIntegrityKind.BINDING_MISMATCH in kinds


def test_a_forked_preimage_is_reported_even_if_each_chain_link_reproduces(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """Two identities over one decision revision cannot be introduced by the API.

    An adversary with write access can still insert one, and ``verify`` must name
    it rather than serving two competing heads as if both were authoritative.
    """
    append_initial(journal, fixed_clock)
    append_record(
        journal,
        reconciliation_payload(
            "reconciliation-0002",
            preimage=preimage_payload(decision_id="decision-strategic-0002"),
        ),
        clock=fixed_clock,
    )
    tamper(
        journal,
        "UPDATE reconciliations SET preimage_seal = "
        "(SELECT preimage_seal FROM reconciliations WHERE seq = 1) WHERE seq = 2",
    )
    kinds = {finding.kind for finding in journal.verify().findings}
    assert ReconciliationIntegrityKind.PREIMAGE_FORKED in kinds


def test_an_unparseable_payload_is_reported_not_raised(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    tamper(journal, "UPDATE reconciliations SET payload = '{not json' WHERE seq = 1")
    report = journal.verify()
    assert not report.ok
    assert ReconciliationIntegrityKind.PAYLOAD_UNPARSEABLE in {
        finding.kind for finding in report.findings
    }


def test_a_journal_that_does_not_verify_refuses_every_further_write(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    tamper(
        journal,
        "UPDATE reconciliations SET appended_at = '2020-01-01T00:00:00Z' WHERE seq = 1",
    )
    with pytest.raises(IntegrityError):
        append_record(
            journal,
            reconciliation_payload(
                "reconciliation-0002",
                preimage=preimage_payload(decision_id="decision-strategic-0002"),
            ),
            clock=fixed_clock,
        )
    # Abandoning re-anchors, so on a corrupt journal it would replace the anchor
    # that proves the defect with one that agrees with the tampered content.
    with pytest.raises(IntegrityError):
        journal.abandon_epoch(reason="x", clock=fixed_clock)


def test_abandoning_a_truncated_journal_cannot_launder_the_anchor(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """The one place a legitimate write could erase the proof of a deletion.

    ``abandon_epoch`` re-anchors from the current count and tail. Allowed on a
    truncated journal, it would replace the anchor that says two entries existed
    with one that agrees there is only one. It refuses, and the defect survives.
    """
    head_seal, _ = append_initial(journal, fixed_clock)
    append_record(journal, correction(head_seal), clock=fixed_clock)
    tamper(journal, "DELETE FROM reconciliations WHERE seq = 2")
    anchor_before = dict(
        raw(journal)
        .execute("SELECT key, value FROM store_meta WHERE key LIKE 'anchor_%'")
        .fetchall()
    )

    with pytest.raises(IntegrityError):
        journal.abandon_epoch(reason="closing a broken journal", clock=fixed_clock)

    anchor_after = dict(
        raw(journal)
        .execute("SELECT key, value FROM store_meta WHERE key LIKE 'anchor_%'")
        .fetchall()
    )
    assert anchor_after == anchor_before
    assert ReconciliationIntegrityKind.ANCHOR_MISMATCH in {
        finding.kind for finding in journal.verify().findings
    }


def test_an_interrupted_append_leaves_no_partially_accepted_reconciliation(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """The row exists inside the transaction, then the transaction dies.

    Aborting on the *anchor update*, after the insert, is what makes this a proof
    of atomicity rather than a proof that validation ran first.
    """
    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    raw(journal).execute(
        """
        CREATE TEMP TRIGGER interrupt_anchor_update
        BEFORE UPDATE ON store_meta
        WHEN OLD.key = 'anchor_generation'
         AND (SELECT COUNT(*) FROM reconciliations) = 2
        BEGIN
            SELECT RAISE(ABORT, 'power loss after entry insert');
        END
        """
    )
    second = reconciliation_payload(
        "reconciliation-0002",
        preimage=preimage_payload(decision_id="decision-strategic-0002"),
    )

    with pytest.raises(LedgerError, match="rejected an operation"):
        append_record(journal, second, clock=fixed_clock)

    assert snapshot(journal) == before
    assert journal.verify().ok
    with pytest.raises(StoreNotFound):
        journal.get("reconciliation-0002")

    raw(journal).execute("DROP TRIGGER interrupt_anchor_update")
    receipt = append_record(journal, second, clock=fixed_clock)
    assert receipt.seq == 2, "the interrupted attempt must not have burned a sequence number"


def test_a_concurrent_writer_is_refused_rather_than_interleaved(
    journal: ReconciliationJournal, journal_root: Path, fixed_clock: Clock
) -> None:
    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    rival = ReconciliationJournal.open(journal_root)
    try:
        raw(journal).execute("BEGIN IMMEDIATE")
        with pytest.raises(LedgerError):
            append_record(
                rival,
                reconciliation_payload(
                    "reconciliation-0002",
                    preimage=preimage_payload(decision_id="decision-strategic-0002"),
                ),
                clock=fixed_clock,
            )
        raw(journal).execute("ROLLBACK")
        assert snapshot(journal) == before
    finally:
        rival.close()


def test_a_colliding_entry_identity_is_refused(
    journal: ReconciliationJournal, journal_root: Path, fixed_clock: Clock
) -> None:
    """Two writers racing the same revision: the loser is refused, not merged."""
    append_initial(journal, fixed_clock)
    before = snapshot(journal)
    with pytest.raises(ReconciliationRevisionConflict):
        append_record(journal, reconciliation_payload(), clock=fixed_clock)
    assert snapshot(journal) == before
    assert (journal_root / RECONCILIATION_DATABASE_FILENAME).is_file()


def test_an_unreadable_database_is_an_integrity_error_not_a_traceback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-a-journal"
    root.mkdir()
    (root / RECONCILIATION_DATABASE_FILENAME).write_bytes(b"this is not a database at all")
    with pytest.raises((IntegrityError, LedgerError, ContractViolation)):
        ReconciliationJournal.open(root)


# -- absence of authority, routing and promotion -----------------------------


def test_the_public_surface_names_no_authority_routing_or_promotion_verb() -> None:
    import latent_compass.decision_reconciliation as package

    forbidden = (
        "select",
        "decide",
        "authorize",
        "authorise",
        "promote",
        "promotion",
        "learn",
        "train",
        "rank",
        "score",
        "route",
        "reward",
        "capability",
        "grant",
    )
    for name in package.__all__:
        lowered = name.lower()
        for verb in forbidden:
            assert verb not in lowered, f"{name} exposes a {verb!r} surface"


def test_the_journal_exposes_no_method_that_acts_on_the_system() -> None:
    forbidden = ("select", "decide", "authorize", "promote", "learn", "execute", "apply", "run")
    for name in dir(ReconciliationJournal):
        if name.startswith("_"):
            continue
        for verb in forbidden:
            assert verb not in name.lower(), f"ReconciliationJournal.{name} looks like an action"


def test_the_limits_document_states_what_is_not_produced() -> None:
    limits = reconciliation_limits()
    assert limits["produces_scalar_score"] is False
    assert limits["produces_ranking"] is False
    assert limits["produces_causal_claim"] is False
    assert limits["grants_authority"] is False
    assert limits["universal_secret_detection"] is False
    assert limits["observation_dimensions"] == [item.value for item in DIMENSION_ORDER]
    assert sorted(cast("list[str]", limits["unknown_reasons"])) == sorted(UNKNOWN_REASONS)


def test_the_authority_boundary_is_untouched_by_this_contract() -> None:
    """HOK-244 grants no lifecycle authority and must be absent from the boundary."""
    from latent_compass.authority import authority_boundary_snapshot

    rendered = json.dumps(authority_boundary_snapshot()).lower()
    for needle in ("reconcil", "observation", "unknown_reason", "executed"):
        assert needle not in rendered, f"the authority boundary mentions {needle!r}"


def test_the_decision_memory_contract_gained_no_outcome_field() -> None:
    """The whole design rests on this: HOK-243's record shape is unchanged."""
    record = admit_strategic_decision(strategic_decision_payload())
    assert set(record.canonical_payload()) == {
        "binding",
        "captured_at",
        "contract_version",
        "decision_authority",
        "decision_id",
        "expires_at",
        "impact_class",
        "projection",
        "review_due_at",
        "revision",
        "sensitivity",
        "supersedes_revision_seal",
    }


def test_the_decision_memory_event_kinds_gained_no_reconciliation_kind() -> None:
    from latent_compass.decision_memory import EventKind

    assert sorted(kind.value for kind in EventKind) == ["RECORD", "REVOCATION", "TOMBSTONE"]


def test_reconciled_at_and_the_journal_clock_are_separate_facts(
    journal: ReconciliationJournal, fixed_clock: Clock
) -> None:
    """The record says when it was reconciled; the journal says when it landed."""
    append_initial(journal, fixed_clock)
    head = journal.get(RECONCILIATION_ID)
    assert head.record.reconciled_at == RECONCILED_AT
    assert head.appended_at == fixed_clock()
    assert head.record.reconciled_at != head.appended_at

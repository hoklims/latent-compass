"""HOK-252 — prospective shadow collection stays preregistered and diagnostic."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from time import perf_counter

import pytest

import latent_compass
from conftest import (
    EPOCH,
    HOST_ID,
    STORE_ID,
    observation_set,
    reconciliation_payload,
    strategic_decision_payload,
)
from latent_compass.decision_memory import admit_strategic_decision
from latent_compass.decision_memory.contracts import StrategicDecisionRecord
from latent_compass.decision_reconciliation import (
    ReconciliationJournal,
    ReconciliationRecord,
    admit_reconciliation,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import IntegrityError, LatentCompassError, ProspectiveCollectionViolation
from latent_compass.prospective_collection import ProspectiveCollectionPlan
from latent_compass.prospective_collection.contracts import _binomial_tail

Clock = Callable[[], str]


def clock_at(timestamp: str) -> Clock:
    return lambda: timestamp


def plan_payload(**overrides: object) -> dict[str, object]:
    power = latent_compass.plan_exact_one_sided_binomial(
        p0=0.5,
        p1=0.75,
        alpha=0.05,
        target_power=0.8,
        max_enrollments=35,
        clustering_inflation=1.5,
    )
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "plan_id": "prospective-plan-001",
        "source_binding": {
            "decision": {
                "host_id": HOST_ID,
                "agent_family": "claude",
                "store_id": STORE_ID,
                "epoch": EPOCH,
            },
            "reconciliation": {
                "host_id": HOST_ID,
                "agent_family": "claude",
                "store_id": STORE_ID,
                "epoch": EPOCH,
            },
        },
        "population": "Every qualifying HOK-243 strategic decision captured in the window.",
        "declared_strata": ["maintenance", "feature"],
        "hard_exclusions": [item.value for item in latent_compass.HardExclusion],
        "plan_created_at": "2026-08-15T08:00:00Z",
        "collection_not_before": "2026-08-16T08:00:00Z",
        "minimum_calendar_end": "2026-08-21T08:00:00Z",
        "hard_calendar_end": "2026-08-31T08:00:00Z",
        "max_enrollments": 35,
        "outcome_dependent_interim_looks": 0,
        "independence_policy": {"decision_producer_identities": ["operator-alpha"]},
        "power": power.canonical_payload(),
        "stop_priority": [item.value for item in latent_compass.StopCondition],
    }
    payload.update(overrides)
    return payload


def admitted_plan() -> ProspectiveCollectionPlan:
    return latent_compass.admit_prospective_plan(plan_payload())


def fixture_decision() -> StrategicDecisionRecord:
    return admit_strategic_decision(strategic_decision_payload())


def fixture_reconciliation(
    *, producer: str = "observer-agent-v1", unknowns: dict[str, str] | None = None
) -> ReconciliationRecord:
    observations = observation_set(unknowns)
    for observation in observations:
        provenance = observation["provenance"]
        if isinstance(provenance, dict):
            provenance["producer"] = producer
    return admit_reconciliation(
        reconciliation_payload(
            store_id=STORE_ID,
            observations=observations,
        )
    )


def durable_reconciliation_source(
    root: Path,
    decision: StrategicDecisionRecord,
    reconciliation: ReconciliationRecord,
) -> ReconciliationJournal:
    source = ReconciliationJournal.create(
        root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
        clock=clock_at("2026-08-15T08:00:00Z"),
    )
    source.append_reconciliation(
        reconciliation.canonical_payload(),
        decision_record=decision,
        clock=clock_at("2026-08-20T10:00:00Z"),
    )
    return source


def one_case_plan() -> ProspectiveCollectionPlan:
    power = latent_compass.plan_exact_one_sided_binomial(
        p0=0.01,
        p1=0.99,
        alpha=0.05,
        target_power=0.8,
        max_enrollments=1,
        clustering_inflation=1.0,
    )
    return latent_compass.admit_prospective_plan(
        plan_payload(max_enrollments=1, power=power.canonical_payload())
    )


def three_case_plan() -> ProspectiveCollectionPlan:
    power = latent_compass.plan_exact_one_sided_binomial(
        p0=0.01,
        p1=0.99,
        alpha=0.05,
        target_power=0.8,
        max_enrollments=3,
        clustering_inflation=1.0,
    )
    return latent_compass.admit_prospective_plan(
        plan_payload(max_enrollments=3, power=power.canonical_payload())
    )


def test_exact_planner_finds_the_smallest_inflated_enrollment_target() -> None:
    planner = getattr(latent_compass, "plan_exact_one_sided_binomial", None)
    assert callable(planner), "the HOK-252 exact planner must be public"

    result = planner(
        p0=0.5,
        p1=0.75,
        alpha=0.05,
        target_power=0.8,
        max_enrollments=35,
        clustering_inflation=1.5,
    )

    assert result.status.value == "ESTABLISHED"
    assert result.independent_eligible_count == 23
    assert result.required_enrollments == 35
    assert result.critical_eligible_count == 16


def test_exact_planner_returns_a_typed_no_solution_result() -> None:
    planner = getattr(latent_compass, "plan_exact_one_sided_binomial", None)
    assert callable(planner), "the HOK-252 exact planner must be public"

    result = planner(
        p0=0.5,
        p1=0.51,
        alpha=0.001,
        target_power=0.999,
        max_enrollments=5,
        clustering_inflation=1.0,
    )

    assert result.status.value == "POWER_NOT_ESTABLISHED"
    assert result.required_enrollments is None
    assert result.critical_eligible_count is None


def test_planner_honours_decimal_inflation_at_the_exact_raw_boundary() -> None:
    result = latent_compass.plan_exact_one_sided_binomial(
        p0=0.5,
        p1=0.7,
        alpha=0.05,
        target_power=0.7,
        max_enrollments=33,
        clustering_inflation=1.1,
    )
    assert result.independent_eligible_count == 30
    assert result.required_enrollments == 33
    assert result.critical_eligible_count == 20
    assert result.achieved_power == pytest.approx(0.7303704173856143)

    no_room = latent_compass.plan_exact_one_sided_binomial(
        p0=0.5,
        p1=0.7,
        alpha=0.05,
        target_power=0.7,
        max_enrollments=32,
        clustering_inflation=1.1,
    )
    assert no_room.status.value == "POWER_NOT_ESTABLISHED"


def test_exact_tail_remains_finite_at_large_n() -> None:
    tail = _binomial_tail(2000, 1000, 0.5)
    assert 0.5 < tail < 0.52
    assert tail == pytest.approx(0.508919505572927)
    assert _binomial_tail(10_000, 5_000, 0.5) == pytest.approx(0.5039893230623885)


def test_maximum_planner_search_stays_inside_the_local_budget() -> None:
    started = perf_counter()
    result = latent_compass.plan_exact_one_sided_binomial(
        p0=0.5,
        p1=0.5001,
        alpha=0.0001,
        target_power=0.9999,
        max_enrollments=10_000,
        clustering_inflation=1.0,
    )
    elapsed = perf_counter() - started
    assert result.status.value == "POWER_NOT_ESTABLISHED"
    assert elapsed < 1.0, f"exact max search exceeded 1s: {elapsed:.3f}s"


def test_plan_admission_refuses_non_json_and_forbidden_training_surface() -> None:
    admit = getattr(latent_compass, "admit_prospective_plan", None)
    assert callable(admit), "the plan admission boundary must be public"
    with pytest.raises(ProspectiveCollectionViolation):
        admit({**plan_payload(), "declared_strata": ("maintenance", "feature")})
    with pytest.raises(ProspectiveCollectionViolation):
        admit({**plan_payload(), "training_label": "positive"})


def test_power_not_established_cannot_be_sealed_as_a_startable_plan() -> None:
    no_power = latent_compass.plan_exact_one_sided_binomial(
        p0=0.5,
        p1=0.51,
        alpha=0.001,
        target_power=0.999,
        max_enrollments=5,
        clustering_inflation=1.0,
    )
    payload = plan_payload(max_enrollments=5, power=no_power.canonical_payload())
    with pytest.raises(ProspectiveCollectionViolation):
        latent_compass.admit_prospective_plan(payload)


def test_plan_refuses_a_forged_or_nonminimal_power_result() -> None:
    payload = plan_payload()
    power = payload["power"]
    assert isinstance(power, dict)
    forged = dict(power)
    forged["independent_eligible_count"] = 22
    payload["power"] = forged
    with pytest.raises(ProspectiveCollectionViolation):
        latent_compass.admit_prospective_plan(payload)


def test_power_result_rejects_a_nonlocal_nonminimal_solution() -> None:
    inputs = {
        "method": "EXACT_ONE_SIDED_BINOMIAL_V1",
        "p0": 0.1,
        "p1": 0.2,
        "alpha": 0.01,
        "target_power": 0.1957484036385649,
        "max_enrollments": 100,
        "clustering_inflation": 1.0,
    }
    forged = {
        "method": "EXACT_ONE_SIDED_BINOMIAL_V1",
        "status": "ESTABLISHED",
        "inputs": inputs,
        "independent_eligible_count": 29,
        "required_enrollments": 29,
        "critical_eligible_count": 8,
        "achieved_alpha": 0.0062466085092110855,
        "achieved_power": 0.20972708066450663,
    }
    payload = plan_payload(max_enrollments=100, power=forged)
    with pytest.raises(ProspectiveCollectionViolation):
        latent_compass.admit_prospective_plan(payload)


def test_journal_refuses_backfill_and_wrong_binding_without_moving_generation(
    tmp_path: Path,
) -> None:
    journal_type = getattr(latent_compass, "ProspectiveCollectionJournal", None)
    assert journal_type is not None, "the adjacent collection journal must be public"
    journal = journal_type.create(
        tmp_path / "prospective",
        plan=admitted_plan(),
        clock=lambda: "2026-08-15T08:00:00Z",
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        before = journal.status()
        backfill = admit_strategic_decision(
            strategic_decision_payload(captured_at="2026-08-15T07:59:59Z")
        )
        enrollment_clock = clock_at("2026-08-16T10:00:00Z")
        with pytest.raises(ProspectiveCollectionViolation, match="pre-plan, backfilled"):
            journal.enroll(backfill, stratum="maintenance", clock=enrollment_clock)
        wrong = admit_strategic_decision(strategic_decision_payload(store_id="wrong-store-001"))
        with pytest.raises(ProspectiveCollectionViolation, match="wrong sealed source binding"):
            journal.enroll(wrong, stratum="maintenance", clock=enrollment_clock)
        assert journal.status() == before
    finally:
        journal.close()


def test_journal_creation_cannot_predate_or_postdate_the_sealed_plan_window(
    tmp_path: Path,
) -> None:
    for suffix, timestamp in (
        ("before", "2026-08-15T07:59:59Z"),
        ("after", "2026-08-16T08:00:01Z"),
    ):
        with pytest.raises(ProspectiveCollectionViolation):
            latent_compass.ProspectiveCollectionJournal.create(
                tmp_path / suffix,
                plan=admitted_plan(),
                clock=clock_at(timestamp),
            )


def test_reconciliation_requires_exact_preimage_binding_and_independent_producers(
    tmp_path: Path,
) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=admitted_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        decision = fixture_decision()
        receipt = journal.enroll(
            decision, stratum="maintenance", clock=lambda: "2026-08-16T09:00:00Z"
        )
        before = journal.status()
        dependent = fixture_reconciliation(producer="operator-alpha")
        with (
            durable_reconciliation_source(
                tmp_path / "dependent-source", decision, dependent
            ) as dependent_source,
            pytest.raises(ProspectiveCollectionViolation),
        ):
            journal.reconcile(
                receipt.case_id,
                decision_record=decision,
                source_journal=dependent_source,
                reconciliation_id=dependent.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        assert journal.status() == before
        reconciliation = fixture_reconciliation()
        with durable_reconciliation_source(
            tmp_path / "accepted-source", decision, reconciliation
        ) as accepted_source:
            accepted = journal.reconcile(
                receipt.case_id,
                decision_record=decision,
                source_journal=accepted_source,
                reconciliation_id=reconciliation.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        assert accepted.state.value == "RECONCILED"
    finally:
        journal.close()


def test_close_has_no_favourable_early_stop_and_uses_calendar_rules(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    close_collection = getattr(journal, "close_collection", None)
    assert callable(close_collection), "calendar-only closure must be implemented"
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        decision = fixture_decision()
        case = journal.enroll(decision, stratum="maintenance", clock=lambda: "2026-08-16T09:00:00Z")
        reconciliation = fixture_reconciliation()
        with durable_reconciliation_source(tmp_path / "source", decision, reconciliation) as source:
            journal.reconcile(
                case.case_id,
                decision_record=decision,
                source_journal=source,
                reconciliation_id=reconciliation.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        with pytest.raises(ProspectiveCollectionViolation):
            close_collection(clock=lambda: "2026-08-20T11:00:00Z")
        closed = close_collection(clock=lambda: "2026-08-21T08:00:00Z")
        assert closed.state.value == "CLOSED_SUFFICIENT"
    finally:
        journal.close()


def test_hard_end_closes_insufficient_and_retains_nonterminal_denominator(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        journal.enroll(
            fixture_decision(),
            stratum="feature",
            clock=lambda: "2026-08-16T09:00:00Z",
        )
        closed = journal.close_collection(clock=lambda: "2026-08-31T08:00:00Z")
        assert closed.state.value == "CLOSED_INSUFFICIENT"
        manifest = journal.manifest()
        assert manifest.enrolled_count == 1
        assert manifest.cases[0].state.value == "ENROLLED"
        assert manifest.cases[0].eligible_for_corpus is False
    finally:
        journal.close()


def test_manifest_and_report_are_deterministic_and_only_diagnostic(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    report = getattr(journal, "report", None)
    assert callable(report), "the narrow collection diagnostic must be implemented"
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        decision = fixture_decision()
        case = journal.enroll(decision, stratum="maintenance", clock=lambda: "2026-08-16T09:00:00Z")
        reconciliation = fixture_reconciliation(unknowns={"COST": "LATE"})
        with durable_reconciliation_source(tmp_path / "source", decision, reconciliation) as source:
            journal.reconcile(
                case.case_id,
                decision_record=decision,
                source_journal=source,
                reconciliation_id=reconciliation.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        journal.close_collection(clock=lambda: "2026-08-21T08:00:00Z")
        first_manifest = journal.manifest()
        second_manifest = journal.manifest()
        assert first_manifest.manifest_seal == second_manifest.manifest_seal
        assert first_manifest.eligible_count == 1
        diagnostic = report()
        assert diagnostic.eligibility_rate == 1.0
        assert diagnostic.missingness["COST"]["LATE"] == 1
        assert diagnostic.selection_imbalance_diagnostic.maximum_stratum_gap == 1.0
        forbidden = {"score", "causal_effect", "rank", "winner", "routing"}
        assert forbidden.isdisjoint(diagnostic.canonical_payload())
    finally:
        journal.close()


def test_truncation_breaks_the_durable_anchor_and_blocks_future_appends(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        journal.enroll(
            fixture_decision(),
            stratum="maintenance",
            clock=lambda: "2026-08-16T09:00:00Z",
        )
        journal._conn.execute(  # noqa: SLF001 - deliberate corruption threat model
            "DELETE FROM events WHERE seq=(SELECT MAX(seq) FROM events)"
        )
        report = journal.verify()
        assert report.ok is False
        assert "ANCHOR_MISMATCH" in report.findings
        before = journal.status().generation
        with pytest.raises(IntegrityError):
            journal.mark_lost_to_followup(
                "case:decision-strategic-0001:r1",
                reason="source stopped responding",
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        assert journal.status().generation == before
    finally:
        journal.close()


def test_future_dated_reconciliation_and_stale_cas_leave_case_enrolled(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        decision = fixture_decision()
        case = journal.enroll(decision, stratum="maintenance", clock=lambda: "2026-08-16T09:00:00Z")
        generation = journal.status().generation
        with pytest.raises(ProspectiveCollectionViolation):
            journal.mark_cancelled(
                case.case_id,
                reason="cancelled externally",
                expected_generation=generation - 1,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        reconciliation = fixture_reconciliation()
        with (
            durable_reconciliation_source(tmp_path / "source", decision, reconciliation) as source,
            pytest.raises(ProspectiveCollectionViolation),
        ):
            journal.reconcile(
                case.case_id,
                decision_record=decision,
                source_journal=source,
                reconciliation_id=reconciliation.reconciliation_id,
                expected_generation=generation,
                clock=lambda: "2026-08-19T10:00:00Z",
            )
        assert journal.status().enrolled_count == 1
        assert journal.status().terminal_count == 0
    finally:
        journal.close()


def test_abstention_cancellation_and_loss_remain_in_the_denominator(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=three_case_plan(), clock=lambda: "2026-08-15T08:00:00Z"
    )
    try:
        journal.start(clock=lambda: "2026-08-16T08:00:00Z")
        decisions = [
            admit_strategic_decision(strategic_decision_payload(decision_id))
            for decision_id in (
                "decision-strategic-0001",
                "decision-strategic-0002",
                "decision-strategic-0003",
            )
        ]
        cases = [
            journal.enroll(
                decision,
                stratum="maintenance",
                clock=lambda: "2026-08-16T09:00:00Z",
            )
            for decision in decisions
        ]
        abstention = admit_reconciliation(
            reconciliation_payload(
                store_id=STORE_ID,
                execution_state="NOT_EXECUTED",
                executed_direction_id=None,
                observations=[],
            )
        )
        with durable_reconciliation_source(tmp_path / "source", decisions[0], abstention) as source:
            journal.reconcile(
                cases[0].case_id,
                decision_record=decisions[0],
                source_journal=source,
                reconciliation_id=abstention.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
        journal.mark_cancelled(
            cases[1].case_id,
            reason="cancelled outside the package",
            clock=lambda: "2026-08-20T10:00:00Z",
        )
        journal.mark_lost_to_followup(
            cases[2].case_id,
            reason="no reconciliation arrived",
            clock=lambda: "2026-08-20T10:00:00Z",
        )
        journal.close_collection(clock=lambda: "2026-08-21T08:00:00Z")
        manifest = journal.manifest()
        diagnostic = journal.report()
        assert manifest.enrolled_count == 3
        assert manifest.eligible_count == 0
        assert diagnostic.abstention_or_nonexecution_rate == pytest.approx(1 / 3)
        assert diagnostic.cancellation_rate == pytest.approx(1 / 3)
        assert diagnostic.lost_to_followup_rate == pytest.approx(1 / 3)
    finally:
        journal.close()


def test_publication_refuses_collecting_aborted_and_corrupt_journals(tmp_path: Path) -> None:
    root = tmp_path / "prospective"
    journal = latent_compass.ProspectiveCollectionJournal.create(
        root, plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
        with pytest.raises(ProspectiveCollectionViolation):
            journal.manifest()
        journal.abort(
            reason=latent_compass.AbortReason.SECURITY_FAILURE,
            clock=clock_at("2026-08-16T09:00:00Z"),
        )
        with pytest.raises(ProspectiveCollectionViolation):
            journal.report()
    finally:
        journal.close()

    corrupt = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "corrupt", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        corrupt.start(clock=clock_at("2026-08-16T08:00:00Z"))
        corrupt._conn.execute(  # noqa: SLF001 - deliberate corruption threat model
            "UPDATE events SET payload='{' WHERE seq=1"
        )
        report = corrupt.verify()
        assert report.ok is False
        assert "PAYLOAD_UNPARSEABLE" in report.findings
        with pytest.raises(IntegrityError):
            corrupt.close_collection(clock=clock_at("2026-08-31T08:00:00Z"))
    finally:
        corrupt.close()


@pytest.mark.parametrize(
    "reason",
    [
        "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "contains\x00control",
        "x" * 600,
    ],
)
def test_terminal_reason_security_refusal_is_pretransaction_and_does_not_echo(
    tmp_path: Path, reason: str
) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
        case = journal.enroll(
            fixture_decision(), stratum="maintenance", clock=clock_at("2026-08-16T09:00:00Z")
        )
        before = journal.status()
        with pytest.raises(LatentCompassError) as refusal:
            journal.mark_cancelled(
                case.case_id, reason=reason, clock=clock_at("2026-08-20T10:00:00Z")
            )
        assert reason not in str(refusal.value)
        assert journal.status() == before
    finally:
        journal.close()


def test_terminal_chronology_cannot_predate_enrollment_or_exceed_hard_end(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
        case = journal.enroll(
            fixture_decision(), stratum="maintenance", clock=clock_at("2026-08-16T09:00:00Z")
        )
        for timestamp in ("2026-08-16T08:59:59Z", "2026-08-31T08:00:01Z"):
            with pytest.raises(ProspectiveCollectionViolation):
                journal.mark_lost_to_followup(
                    case.case_id,
                    reason="source unavailable",
                    clock=clock_at(timestamp),
                )
        assert journal.status().terminal_count == 0
    finally:
        journal.close()


def test_concurrent_duplicate_preimage_has_exactly_one_accepted_append(tmp_path: Path) -> None:
    root = tmp_path / "prospective"
    journal = latent_compass.ProspectiveCollectionJournal.create(
        root, plan=three_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
    journal.close()
    barrier = Barrier(2)

    def attempt() -> str:
        opened = latent_compass.ProspectiveCollectionJournal.open(root)
        try:
            barrier.wait()
            opened.enroll(
                fixture_decision(),
                stratum="maintenance",
                clock=clock_at("2026-08-16T09:00:00Z"),
            )
            return "accepted"
        except ProspectiveCollectionViolation:
            return "refused"
        finally:
            opened.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: attempt(), range(2)))
    assert outcomes == ["accepted", "refused"]
    with latent_compass.ProspectiveCollectionJournal.open(root) as reopened:
        assert reopened.status().enrolled_count == 1
        assert reopened.verify().ok is True


def test_verify_reports_duplicate_preimages_and_invalid_transitions(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=three_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
        journal.enroll(
            fixture_decision(),
            stratum="maintenance",
            clock=clock_at("2026-08-16T09:00:00Z"),
        )
        persisted = journal._cases()[0]  # noqa: SLF001 - hostile semantic append
        journal._append(  # noqa: SLF001 - bypass public transition guards deliberately
            event_kind="ENROLL",
            case_id=persisted.case_id,
            case_state=latent_compass.CaseState.ENROLLED,
            payload=persisted.canonical_payload(),
            appended_at="2026-08-16T09:00:01Z",
        )
        findings = journal.verify().findings
        assert "DUPLICATE_CASE" in findings
        assert "DUPLICATE_PREIMAGE" in findings
    finally:
        journal.close()

    invalid = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "invalid", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        invalid.start(clock=clock_at("2026-08-16T08:00:00Z"))
        invalid._append(  # noqa: SLF001 - impossible terminal transition corruption
            event_kind="TERMINAL",
            case_id="case:absent-decision:r1",
            case_state=latent_compass.CaseState.CANCELLED,
            payload={
                "state": "CANCELLED",
                "terminal_at": "2026-08-16T09:00:00Z",
                "terminal_reason": "forged terminal",
            },
            appended_at="2026-08-16T09:00:00Z",
        )
        assert "SEMANTIC_TRANSITION_INVALID" in invalid.verify().findings
    finally:
        invalid.close()


def test_global_append_chronology_is_monotonic_across_cases(tmp_path: Path) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=three_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
        first = admit_strategic_decision(strategic_decision_payload("decision-strategic-0001"))
        second = admit_strategic_decision(strategic_decision_payload("decision-strategic-0002"))
        journal.enroll(first, stratum="maintenance", clock=clock_at("2026-08-16T10:00:00Z"))
        with pytest.raises(ProspectiveCollectionViolation, match="globally monotonic"):
            journal.enroll(
                second,
                stratum="maintenance",
                clock=clock_at("2026-08-16T09:30:00Z"),
            )
        assert journal.status().enrolled_count == 1
    finally:
        journal.close()


def test_verified_reconciliation_source_tamper_and_stale_root_are_refused(
    tmp_path: Path,
) -> None:
    journal = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "prospective", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    journal.start(clock=clock_at("2026-08-16T08:00:00Z"))
    decision = fixture_decision()
    case = journal.enroll(decision, stratum="maintenance", clock=clock_at("2026-08-16T09:00:00Z"))
    reconciliation = fixture_reconciliation()
    source = durable_reconciliation_source(tmp_path / "source", decision, reconciliation)
    try:
        source._conn.execute(  # noqa: SLF001 - stale durable root threat model
            "UPDATE store_meta SET value=? WHERE key='anchor_tail'",
            ("sha256:" + "0" * 64,),
        )
        before = journal.status()
        with pytest.raises(IntegrityError):
            journal.reconcile(
                case.case_id,
                decision_record=decision,
                source_journal=source,
                reconciliation_id=reconciliation.reconciliation_id,
                clock=clock_at("2026-08-20T10:00:00Z"),
            )
        assert journal.status() == before
    finally:
        source.close()
        journal.close()


def test_verify_refuses_forged_sufficient_close_and_reconciliation_payload(
    tmp_path: Path,
) -> None:
    empty = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "empty", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    try:
        empty.start(clock=clock_at("2026-08-16T08:00:00Z"))
        empty._append(  # noqa: SLF001 - hostile zero-case close
            event_kind="PLAN_STATE",
            payload={"state": "CLOSED_SUFFICIENT"},
            appended_at="2026-08-21T08:00:00Z",
            new_plan_state=latent_compass.PlanState.CLOSED_SUFFICIENT,
        )
        assert "SEMANTIC_TRANSITION_INVALID" in empty.verify().findings
        with pytest.raises(IntegrityError):
            empty.manifest()
    finally:
        empty.close()

    forged = latent_compass.ProspectiveCollectionJournal.create(
        tmp_path / "forged", plan=one_case_plan(), clock=clock_at("2026-08-15T08:00:00Z")
    )
    source = None
    try:
        forged.start(clock=clock_at("2026-08-16T08:00:00Z"))
        decision = fixture_decision()
        enrolled = forged.enroll(
            decision,
            stratum="maintenance",
            clock=clock_at("2026-08-16T09:00:00Z"),
        )
        reconciliation = fixture_reconciliation()
        source = durable_reconciliation_source(tmp_path / "forged-source", decision, reconciliation)
        source_row = source._conn.execute(  # noqa: SLF001 - hostile proof construction
            "SELECT * FROM reconciliations ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert source_row is not None
        forged._append(  # noqa: SLF001 - hostile embedded reconciliation
            event_kind="TERMINAL",
            case_id=enrolled.case_id,
            case_state=latent_compass.CaseState.RECONCILED,
            payload={
                "state": "RECONCILED",
                "terminal_at": "2026-08-20T10:00:00Z",
                "terminal_reason": "execution observed",
                "reconciliation_id": reconciliation.reconciliation_id,
                "reconciliation_revision": reconciliation.revision,
                "reconciliation_record_seal": reconciliation.record_seal(),
                "reconciliation": reconciliation.canonical_payload(),
                "reconciliation_source": {
                    "journal_root_seal": "sha256:" + "2" * 64,
                    "journal_generation": 1,
                    "entry_seq": 1,
                    "entry_chain_seal": str(source_row["chain_seal"]),
                    "entry_content_seal": str(source_row["content_seal"]),
                },
            },
            appended_at="2026-08-20T10:00:00Z",
        )
        assert "RECONCILIATION_INVALID" in forged.verify().findings
    finally:
        if source is not None:
            source.close()
        forged.close()

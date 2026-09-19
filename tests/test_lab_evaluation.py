"""HOK-804 — paired evaluation reporting stays exact, closed and non-causal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from latent_compass.errors import LatentCompassError
from latent_compass.lab.evaluation import (
    Arm,
    LabEvaluationProtocol,
    LabEvaluationReport,
    LabEvaluationViolation,
    LabTrial,
    ReportStatus,
    admit_lab_evaluation_protocol,
    admit_lab_trial,
    build_evaluation_report,
)

DIGEST_A = "sha256:" + "a" * 64
FROZEN_AT = "2026-09-01T00:00:00Z"
RECORDED_AT = "2026-09-10T00:00:00Z"
PAIR_TASK = {"pair-1": "task-1", "pair-2": "task-2"}


def protocol_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "protocol_id": "protocol-one",
        "candidate_identity": "candidate-one",
        "config_identity": "config-one",
        "source_identity": "source-one",
        "expected_arms": ["EXISTING_STACK", "SOURCE_ONLY", "CONTROLLER_PLUS_SOURCE"],
        "task_ids": ["task-1", "task-2"],
        "pair_ids": ["pair-1", "pair-2"],
        "pair_task_bindings": [
            {"pair_id": "pair-1", "task_id": "task-1"},
            {"pair_id": "pair-2", "task_id": "task-2"},
        ],
        "min_cost": 0,
        "max_cost": 100,
        "min_latency_ms": 0,
        "max_latency_ms": 1000,
        "non_inferiority_margin": 0.05,
        "cost_target": 20,
        "origin": "SYNTHETIC",
        "frozen_at": FROZEN_AT,
    }
    payload.update(overrides)
    return payload


def admitted_protocol(**overrides: object) -> LabEvaluationProtocol:
    return admit_lab_evaluation_protocol(protocol_payload(**overrides))


def trial_payload(
    *,
    pair_id: str,
    arm: str,
    outcome_status: str,
    protocol: LabEvaluationProtocol,
    trial_id: str | None = None,
    critical_violation: bool = False,
    total_cost: int | None = None,
    latency_ms: int | None = None,
    candidate_generation: str = "gen-1",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "protocol_seal": protocol.protocol_seal(),
        "trial_id": trial_id or f"trial-{pair_id}-{arm.lower()}",
        "pair_id": pair_id,
        "task_id": PAIR_TASK[pair_id],
        "arm": arm,
        "candidate_generation": candidate_generation,
        "outcome_status": outcome_status,
        "critical_violation": critical_violation,
        "total_cost": total_cost,
        "latency_ms": latency_ms,
        "producer_evidence_digest": DIGEST_A,
        "producer": "producer-one",
        "trial_recorded_at": RECORDED_AT,
    }
    payload.update(overrides)
    return payload


def full_trial_set(protocol: LabEvaluationProtocol) -> tuple[LabTrial, ...]:
    rows = [
        ("pair-1", "EXISTING_STACK", "FAILURE", 10, 100),
        ("pair-1", "SOURCE_ONLY", "SUCCESS", 8, 90),
        ("pair-1", "CONTROLLER_PLUS_SOURCE", "SUCCESS", 12, 110),
        ("pair-2", "EXISTING_STACK", "SUCCESS", None, None),
        ("pair-2", "SOURCE_ONLY", "FAILURE", 5, 95),
        ("pair-2", "CONTROLLER_PLUS_SOURCE", "SUCCESS", 6, 105),
    ]
    return tuple(
        admit_lab_trial(
            trial_payload(
                pair_id=pair_id,
                arm=arm,
                outcome_status=status,
                protocol=protocol,
                total_cost=cost,
                latency_ms=latency,
            )
        )
        for pair_id, arm, status, cost, latency in rows
    )


def test_a_complete_trial_set_produces_exact_descriptive_counts_and_deltas() -> None:
    protocol = admitted_protocol()
    trials = full_trial_set(protocol)
    report = build_evaluation_report(protocol, trials)

    assert report.status is ReportStatus.DESCRIPTIVE_ONLY
    assert report.empirical_claim is False
    assert report.causal_claim is False

    by_arm = {item.arm: item for item in report.per_arm}
    assert by_arm[Arm.EXISTING_STACK].successes == 1
    assert by_arm[Arm.EXISTING_STACK].failures == 1
    assert by_arm[Arm.EXISTING_STACK].denominator == 2
    assert by_arm[Arm.EXISTING_STACK].unknown_cost_count == 1
    assert by_arm[Arm.EXISTING_STACK].unknown_latency_count == 1
    assert by_arm[Arm.EXISTING_STACK].total_cost == 10
    assert by_arm[Arm.EXISTING_STACK].p95_latency_ms == 100
    assert by_arm[Arm.SOURCE_ONLY].successes == 1
    assert by_arm[Arm.SOURCE_ONLY].failures == 1
    assert by_arm[Arm.CONTROLLER_PLUS_SOURCE].successes == 2
    assert by_arm[Arm.CONTROLLER_PLUS_SOURCE].failures == 0

    deltas = {item.comparison_arm: item for item in report.paired_deltas}
    source_only_delta = deltas[Arm.SOURCE_ONLY]
    assert source_only_delta.pairs_compared == 2
    assert source_only_delta.successes_gained == 1
    assert source_only_delta.successes_lost == 1
    assert source_only_delta.net_delta == 0
    controller_delta = deltas[Arm.CONTROLLER_PLUS_SOURCE]
    assert controller_delta.successes_gained == 1
    assert controller_delta.successes_lost == 0
    assert controller_delta.net_delta == 1


def test_a_wholly_missing_trial_set_is_insufficient_evidence_not_dropped() -> None:
    protocol = admitted_protocol()
    trials = tuple(
        admit_lab_trial(
            trial_payload(
                pair_id=pair_id, arm=arm.value, outcome_status="MISSING", protocol=protocol
            )
        )
        for pair_id in protocol.pair_ids
        for arm in Arm
    )
    report = build_evaluation_report(protocol, trials)
    assert report.status is ReportStatus.INSUFFICIENT_EVIDENCE
    for tally in report.per_arm:
        assert tally.missing == 2
        assert tally.denominator == 2
        assert tally.successes == 0
        assert tally.failures == 0
    for delta in report.paired_deltas:
        assert delta.pairs_incomplete == 2
        assert delta.pairs_compared == 0
        assert delta.successes_gained == 0
        assert delta.successes_lost == 0


def test_a_missing_baseline_never_counts_as_a_paired_gain_or_loss() -> None:
    protocol = admitted_protocol()
    rows = [
        ("pair-1", "EXISTING_STACK", "MISSING", None, None),
        ("pair-1", "SOURCE_ONLY", "SUCCESS", 8, 90),
        ("pair-1", "CONTROLLER_PLUS_SOURCE", "SUCCESS", 12, 110),
        ("pair-2", "EXISTING_STACK", "SUCCESS", None, None),
        ("pair-2", "SOURCE_ONLY", "FAILURE", 5, 95),
        ("pair-2", "CONTROLLER_PLUS_SOURCE", "SUCCESS", 6, 105),
    ]
    trials = tuple(
        admit_lab_trial(
            trial_payload(
                pair_id=pair_id,
                arm=arm,
                outcome_status=status,
                protocol=protocol,
                total_cost=cost,
                latency_ms=latency,
            )
        )
        for pair_id, arm, status, cost, latency in rows
    )
    report = build_evaluation_report(protocol, trials)
    by_arm = {item.arm: item for item in report.per_arm}
    # The missing baseline still lands in the per-arm denominator...
    assert by_arm[Arm.EXISTING_STACK].missing == 1
    assert by_arm[Arm.EXISTING_STACK].denominator == 2

    deltas = {item.comparison_arm: item for item in report.paired_deltas}
    source_only = deltas[Arm.SOURCE_ONLY]
    # ...but pair-1's SOURCE_ONLY SUCCESS against a MISSING baseline is never a
    # gain, and pair-2's baseline SUCCESS against a FAILURE is still a loss.
    assert source_only.pairs_incomplete == 1
    assert source_only.pairs_compared == 1
    assert source_only.successes_gained == 0
    assert source_only.successes_lost == 1
    assert source_only.net_delta == -1


def test_a_trial_bound_to_a_different_protocol_seal_is_refused() -> None:
    protocol = admitted_protocol()
    other_protocol = admitted_protocol(protocol_id="protocol-two")
    trials = list(full_trial_set(protocol))
    trials[0] = admit_lab_trial(
        trial_payload(
            pair_id="pair-1",
            arm="EXISTING_STACK",
            outcome_status="FAILURE",
            protocol=other_protocol,
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, tuple(trials))


def test_a_duplicate_trial_id_is_refused() -> None:
    protocol = admitted_protocol()
    trials = list(full_trial_set(protocol))
    duplicate = admit_lab_trial(
        trial_payload(
            pair_id="pair-1",
            arm="EXISTING_STACK",
            outcome_status="FAILURE",
            protocol=protocol,
            trial_id=trials[0].trial_id,
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, (*trials, duplicate))


def test_a_trial_naming_an_unplanned_pair_is_refused() -> None:
    protocol = admitted_protocol()
    trials = list(full_trial_set(protocol))
    with pytest.raises(LabEvaluationViolation):
        admit_lab_trial(
            trial_payload(
                pair_id="pair-1",
                arm="EXISTING_STACK",
                outcome_status="FAILURE",
                protocol=protocol,
                trial_id="unplanned-trial",
                pair_id_override=None,
            )
        )
    # Construct the payload directly to bypass the local PAIR_TASK lookup and
    # exercise the report-time refusal, not the fixture helper's own KeyError.
    unplanned = admit_lab_trial(
        {
            "contract_version": "1.0.0",
            "protocol_seal": protocol.protocol_seal(),
            "trial_id": "unplanned-trial",
            "pair_id": "pair-unplanned",
            "task_id": "task-1",
            "arm": "EXISTING_STACK",
            "candidate_generation": "gen-1",
            "outcome_status": "SUCCESS",
            "critical_violation": False,
            "total_cost": None,
            "latency_ms": None,
            "producer_evidence_digest": DIGEST_A,
            "producer": "producer-one",
            "trial_recorded_at": RECORDED_AT,
        }
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, (*trials, unplanned))


def test_a_trial_pairing_a_baseline_task_with_a_mismatched_comparison_task_is_refused() -> None:
    """pair-1 is bound to task-1; a comparison trial claiming task-2 is refused."""
    protocol = admitted_protocol()
    trials = list(full_trial_set(protocol))
    trials[1] = admit_lab_trial(
        trial_payload(
            pair_id="pair-1",
            arm="SOURCE_ONLY",
            outcome_status="SUCCESS",
            protocol=protocol,
            total_cost=8,
            latency_ms=90,
            task_id="task-2",
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, tuple(trials))


def test_an_incomplete_trial_set_dropping_a_slot_is_refused() -> None:
    protocol = admitted_protocol()
    trials = full_trial_set(protocol)[:-1]
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, trials)


def test_a_duplicated_pair_arm_slot_is_refused_even_with_a_distinct_trial_id() -> None:
    protocol = admitted_protocol()
    trials = full_trial_set(protocol)[:-1]
    duplicate_slot = admit_lab_trial(
        trial_payload(
            pair_id="pair-1",
            arm="EXISTING_STACK",
            outcome_status="FAILURE",
            protocol=protocol,
            trial_id="a-second-trial-for-the-same-slot",
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, (*trials, duplicate_slot))


def test_mixed_candidate_generations_are_refused() -> None:
    protocol = admitted_protocol()
    trials = list(full_trial_set(protocol))
    trials[-1] = admit_lab_trial(
        trial_payload(
            pair_id="pair-2",
            arm="CONTROLLER_PLUS_SOURCE",
            outcome_status="SUCCESS",
            protocol=protocol,
            candidate_generation="gen-2",
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, tuple(trials))


def test_a_cost_outside_the_protocol_bounds_is_refused() -> None:
    protocol = admitted_protocol()
    trials = list(full_trial_set(protocol))
    trials[0] = admit_lab_trial(
        trial_payload(
            pair_id="pair-1",
            arm="EXISTING_STACK",
            outcome_status="FAILURE",
            protocol=protocol,
            total_cost=999,
        )
    )
    with pytest.raises(LabEvaluationViolation):
        build_evaluation_report(protocol, tuple(trials))


def test_a_missing_trial_carries_no_cost_or_latency() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_trial(
            trial_payload(
                pair_id="pair-1",
                arm="EXISTING_STACK",
                outcome_status="MISSING",
                protocol=admitted_protocol(),
                total_cost=5,
            )
        )


def test_a_protocol_refuses_pair_task_bindings_missing_a_declared_pair() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_evaluation_protocol(
            protocol_payload(pair_task_bindings=[{"pair_id": "pair-1", "task_id": "task-1"}])
        )


def test_a_protocol_refuses_a_pair_task_binding_naming_an_undeclared_task() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_evaluation_protocol(
            protocol_payload(
                pair_task_bindings=[
                    {"pair_id": "pair-1", "task_id": "task-1"},
                    {"pair_id": "pair-2", "task_id": "task-never-declared"},
                ]
            )
        )


def test_a_protocol_binds_every_declared_pair_to_exactly_one_task() -> None:
    """Prove the capable half: two distinct, fully-covered pairs are accepted."""
    protocol = admitted_protocol()
    assert protocol.task_for("pair-1") == "task-1"
    assert protocol.task_for("pair-2") == "task-2"


def test_a_protocol_refuses_expected_arms_out_of_canonical_order() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_evaluation_protocol(
            protocol_payload(
                expected_arms=["SOURCE_ONLY", "EXISTING_STACK", "CONTROLLER_PLUS_SOURCE"]
            )
        )


def test_a_protocol_refuses_a_non_finite_margin() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_evaluation_protocol(protocol_payload(non_inferiority_margin=float("nan")))


def test_a_protocol_refuses_an_injected_field() -> None:
    with pytest.raises(LatentCompassError):
        admit_lab_evaluation_protocol(protocol_payload(approved=True))


def test_a_report_refuses_a_forged_report_seal() -> None:
    protocol = admitted_protocol()
    trials = full_trial_set(protocol)
    genuine = build_evaluation_report(protocol, trials)
    with pytest.raises(ValidationError):
        LabEvaluationReport(
            contract_version=genuine.contract_version,
            protocol_id=genuine.protocol_id,
            protocol_seal=genuine.protocol_seal,
            status=genuine.status,
            non_inferiority_margin_declared=genuine.non_inferiority_margin_declared,
            cost_target_declared=genuine.cost_target_declared,
            per_arm=genuine.per_arm,
            paired_deltas=genuine.paired_deltas,
            report_seal="sha256:" + "0" * 64,
        )


def test_the_report_never_carries_an_approved_or_power_flag() -> None:
    protocol = admitted_protocol()
    report = build_evaluation_report(protocol, full_trial_set(protocol))
    dumped = report.canonical_payload()
    for forbidden in ("approved", "causal_gain", "power_passed"):
        assert forbidden not in dumped

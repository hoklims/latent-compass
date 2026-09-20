"""HOK-804 — paired evaluation reporting tooling, not its empirical closure.

This module builds a :class:`LabEvaluationReport` from a frozen
:class:`LabEvaluationProtocol` and the closed set of :class:`LabTrial` rows it
admits. Everything it computes is an exact descriptive count or delta over the
supplied trials: no invented statistical test, no p-value, no
``approved``/``causal_gain``/``power_passed`` flag exists anywhere in this
module. The declared non-inferiority margin and cost target are carried
through for prospective planning only — they are never treated as met by a
report built here, however the counts land. The real HOK-246, HOK-247 and any
replacement experiment require independent evidence this module does not and
cannot supply.

A report's :attr:`~LabEvaluationReport.status` is one of two values:
``DESCRIPTIVE_ONLY`` when at least one trial produced a non-missing outcome,
and ``INSUFFICIENT_EVIDENCE`` when every trial in the closed set was
``MISSING``. Neither value is a causal, powered or calibration claim.

Each :class:`PairedDelta` compares only the pairs where *both* the baseline
and the comparison arm produced a non-``MISSING`` outcome: a missing baseline
paired with an observed comparison success is never counted as a gain, nor the
reverse as a loss. ``pairs_incomplete`` names every pair excluded from a
comparison this way, so the exclusion is visible rather than silently folded
into the delta. Every declared pair id names exactly one task id, fixed by the
protocol's ``pair_task_bindings`` before any trial is admitted, so a trial
cannot pair a baseline task with an unrelated comparison task under the same
pair id.
"""

from __future__ import annotations

from enum import StrEnum
from math import ceil
from typing import Final, Literal, Self

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    UnitInterval,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import ContractViolation

__all__ = [
    "LAB_EVALUATION_CONTRACT_VERSION",
    "QUANTILE_CONVENTION",
    "SUPPORTED_LAB_EVALUATION_VERSIONS",
    "Arm",
    "ArmTally",
    "LabEvaluationProtocol",
    "LabEvaluationReport",
    "LabEvaluationViolation",
    "LabTrial",
    "OutcomeStatus",
    "PairTaskBinding",
    "PairedDelta",
    "ReportStatus",
    "TrialOrigin",
    "admit_lab_evaluation_protocol",
    "admit_lab_trial",
    "build_evaluation_report",
]

LAB_EVALUATION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_LAB_EVALUATION_VERSIONS: Final = frozenset({LAB_EVALUATION_CONTRACT_VERSION})

PROTOCOL_SEAL_DOMAIN: Final = "lab.evaluation.protocol.v1"
REPORT_SEAL_DOMAIN: Final = "lab.evaluation.report.v1"

#: Nearest-rank, ceiling convention over the ascending-sorted known latencies:
#: index ``ceil(0.95 * n) - 1``, clamped to ``[0, n - 1]``. Stated exactly once
#: here and referenced by every report, rather than left to be inferred from
#: the arithmetic.
QUANTILE_CONVENTION: Final = (
    "p95 = nearest-rank ceiling over ascending-sorted known latencies: "
    "index = clamp(ceil(0.95 * n) - 1, 0, n - 1)"
)


class LabEvaluationViolation(ContractViolation):
    """A HOK-804 evaluation protocol, trial or report rule was broken."""

    code = "lab_evaluation_violation"


class Arm(StrEnum):
    EXISTING_STACK = "EXISTING_STACK"
    SOURCE_ONLY = "SOURCE_ONLY"
    CONTROLLER_PLUS_SOURCE = "CONTROLLER_PLUS_SOURCE"


ARM_ORDER: Final = tuple(Arm)


class OutcomeStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ABSTAINED = "ABSTAINED"
    MISSING = "MISSING"


class TrialOrigin(StrEnum):
    SYNTHETIC = "SYNTHETIC"
    REAL_DECLARED = "REAL_DECLARED"


class ReportStatus(StrEnum):
    DESCRIPTIVE_ONLY = "DESCRIPTIVE_ONLY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class PairTaskBinding(StrictModel):
    """One protocol pair id bound to exactly one task id, fixed before any trial."""

    pair_id: Identifier
    task_id: Identifier


class LabEvaluationProtocol(StrictModel):
    """A frozen paired-evaluation design. Every trial names it by seal."""

    contract_version: str
    protocol_id: Identifier
    candidate_identity: Identifier
    config_identity: Identifier
    source_identity: Identifier
    expected_arms: tuple[Arm, ...]
    task_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    pair_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    pair_task_bindings: tuple[PairTaskBinding, ...] = Field(min_length=1, max_length=4096)
    min_cost: int = Field(ge=0)
    max_cost: int = Field(ge=0)
    min_latency_ms: int = Field(ge=0)
    max_latency_ms: int = Field(ge=0)
    non_inferiority_margin: UnitInterval
    cost_target: int = Field(ge=0)
    origin: TrialOrigin
    frozen_at: Timestamp

    @model_validator(mode="after")
    def _coherent_protocol(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_EVALUATION_VERSIONS, "lab evaluation protocol"
        )
        if self.expected_arms != ARM_ORDER:
            raise ValueError("expected_arms must declare every arm exactly, in canonical order")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task_ids must be unique")
        if len(set(self.pair_ids)) != len(self.pair_ids):
            raise ValueError("pair_ids must be unique")
        if self.max_cost < self.min_cost:
            raise ValueError("max_cost must not be below min_cost")
        if self.max_latency_ms < self.min_latency_ms:
            raise ValueError("max_latency_ms must not be below min_latency_ms")
        binding_pair_ids = [item.pair_id for item in self.pair_task_bindings]
        if len(set(binding_pair_ids)) != len(binding_pair_ids):
            raise ValueError("pair_task_bindings must bind each pair id at most once")
        if set(binding_pair_ids) != set(self.pair_ids):
            raise ValueError("pair_task_bindings must bind exactly the declared pair_ids")
        for item in self.pair_task_bindings:
            if item.task_id not in self.task_ids:
                raise ValueError(f"{item.pair_id} is bound to undeclared task id {item.task_id!r}")
        return self

    def protocol_seal(self) -> str:
        return seal(PROTOCOL_SEAL_DOMAIN, self.canonical_payload())

    def task_for(self, pair_id: str) -> str:
        """The one task id bound to ``pair_id`` before any trial was admitted."""
        for item in self.pair_task_bindings:
            if item.pair_id == pair_id:
                return item.task_id
        raise KeyError(pair_id)


class LabTrial(StrictModel):
    """One arm of one pair, bound to the protocol seal it was run under."""

    contract_version: str
    protocol_seal: Seal
    trial_id: Identifier
    pair_id: Identifier
    task_id: Identifier
    arm: Arm
    candidate_generation: Identifier
    outcome_status: OutcomeStatus
    critical_violation: bool
    total_cost: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    producer_evidence_digest: Seal
    producer: Identifier
    trial_recorded_at: Timestamp

    @model_validator(mode="after")
    def _coherent_trial(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_EVALUATION_VERSIONS, "lab trial"
        )
        if self.outcome_status in (OutcomeStatus.MISSING, OutcomeStatus.ABSTAINED) and (
            self.total_cost is not None or self.latency_ms is not None
        ):
            raise ValueError(
                f"{self.outcome_status.value} carries no measured cost or latency: "
                "nothing ran to measure"
            )
        return self


class ArmTally(StrictModel):
    """Exact per-arm denominators and descriptive counts. No inference."""

    arm: Arm
    denominator: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    abstained: int = Field(ge=0)
    missing: int = Field(ge=0)
    critical_violations: int = Field(ge=0)
    unknown_cost_count: int = Field(ge=0)
    unknown_latency_count: int = Field(ge=0)
    total_cost: int = Field(ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exact_denominator(self) -> Self:
        if self.successes + self.failures + self.abstained + self.missing != self.denominator:
            raise ValueError("successes, failures, abstained and missing must sum to denominator")
        return self


class PairedDelta(StrictModel):
    """An exact paired comparison between two arms. A count, never a test.

    ``pairs_compared`` counts only pairs where both arms produced a
    non-``MISSING`` outcome; ``pairs_incomplete`` names every pair excluded
    from the comparison because at least one side was ``MISSING``. Neither a
    missing baseline nor a missing comparison ever contributes a gain or a
    loss.
    """

    comparison_arm: Arm
    baseline_arm: Arm
    pairs_compared: int = Field(ge=0)
    pairs_incomplete: int = Field(ge=0)
    successes_gained: int = Field(ge=0)
    successes_lost: int = Field(ge=0)
    net_delta: int

    @model_validator(mode="after")
    def _coherent_delta(self) -> Self:
        if self.comparison_arm is self.baseline_arm:
            raise ValueError("a paired delta compares two distinct arms")
        if self.net_delta != self.successes_gained - self.successes_lost:
            raise ValueError("net_delta must equal successes_gained minus successes_lost")
        if self.successes_gained + self.successes_lost > self.pairs_compared:
            raise ValueError("successes_gained and successes_lost cannot exceed pairs_compared")
        return self


class LabEvaluationReport(StrictModel):
    """The one descriptive report :func:`build_evaluation_report` can return."""

    contract_version: str
    protocol_id: Identifier
    protocol_seal: Seal
    status: ReportStatus
    quantile_convention: str = Field(default=QUANTILE_CONVENTION, min_length=1, max_length=200)
    non_inferiority_margin_declared: UnitInterval
    cost_target_declared: int = Field(ge=0)
    per_arm: tuple[ArmTally, ...]
    paired_deltas: tuple[PairedDelta, ...]
    empirical_claim: Literal[False] = False
    causal_claim: Literal[False] = False
    report_seal: Seal

    @model_validator(mode="after")
    def _coherent_report(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_EVALUATION_VERSIONS, "lab evaluation report"
        )
        if tuple(item.arm for item in self.per_arm) != ARM_ORDER:
            raise ValueError("per_arm must report every arm exactly, in canonical order")
        if self.quantile_convention != QUANTILE_CONVENTION:
            raise ValueError("quantile_convention must state the one convention this build uses")
        body = self.canonical_payload()
        body.pop("report_seal")
        if seal(REPORT_SEAL_DOMAIN, body) != self.report_seal:
            raise ValueError("report seal does not reproduce from its own body")
        return self


def admit_lab_evaluation_protocol(payload: object) -> LabEvaluationProtocol:
    return validate_contract(
        LabEvaluationProtocol,
        payload,
        error=LabEvaluationViolation,
        context="lab evaluation protocol",
    )


def admit_lab_trial(payload: object) -> LabTrial:
    return validate_contract(LabTrial, payload, error=LabEvaluationViolation, context="lab trial")


def _p95(known_latencies: list[int]) -> int | None:
    if not known_latencies:
        return None
    ordered = sorted(known_latencies)
    index = min(max(ceil(0.95 * len(ordered)) - 1, 0), len(ordered) - 1)
    return ordered[index]


def build_evaluation_report(
    protocol: LabEvaluationProtocol, trials: tuple[LabTrial, ...]
) -> LabEvaluationReport:
    """Build the exact descriptive report over a closed, complete trial set.

    Refuses a trial bound to a different protocol seal, a duplicate trial id,
    a trial naming a pair the protocol did not declare, a trial whose task id
    does not match the one bound to its pair id, an incomplete trial set (a
    missing arm for a declared pair must be submitted as an explicit
    ``MISSING`` outcome, never dropped), mixed candidate generations, and any
    cost or latency outside the protocol's declared bounds.
    """
    expected_protocol_seal = protocol.protocol_seal()
    seen_trial_ids: set[str] = set()
    for trial in trials:
        if trial.protocol_seal != expected_protocol_seal:
            raise LabEvaluationViolation(
                "a trial is bound to a different protocol seal",
                detail={"trial_id": trial.trial_id, "protocol_seal": trial.protocol_seal},
            )
        if trial.trial_id in seen_trial_ids:
            raise LabEvaluationViolation("duplicate trial id", detail={"trial_id": trial.trial_id})
        seen_trial_ids.add(trial.trial_id)
        if trial.pair_id not in protocol.pair_ids:
            raise LabEvaluationViolation(
                "trial names a pair the protocol did not declare",
                detail={"trial_id": trial.trial_id, "pair_id": trial.pair_id},
            )
        expected_task_id = protocol.task_for(trial.pair_id)
        if trial.task_id != expected_task_id:
            raise LabEvaluationViolation(
                "trial task id does not match the task bound to its pair id before trials",
                detail={
                    "trial_id": trial.trial_id,
                    "pair_id": trial.pair_id,
                    "task_id": trial.task_id,
                    "expected_task_id": expected_task_id,
                },
            )
        cost_in_bounds = (
            trial.total_cost is None or protocol.min_cost <= trial.total_cost <= protocol.max_cost
        )
        if not cost_in_bounds:
            raise LabEvaluationViolation(
                "trial cost lies outside the protocol's declared bounds",
                detail={"trial_id": trial.trial_id, "total_cost": trial.total_cost},
            )
        if trial.latency_ms is not None and not (
            protocol.min_latency_ms <= trial.latency_ms <= protocol.max_latency_ms
        ):
            raise LabEvaluationViolation(
                "trial latency lies outside the protocol's declared bounds",
                detail={"trial_id": trial.trial_id, "latency_ms": trial.latency_ms},
            )

    generations = {trial.candidate_generation for trial in trials}
    if len(generations) > 1:
        raise LabEvaluationViolation(
            "trials mix candidate generations",
            detail={"candidate_generations": sorted(generations)},
        )

    expected_keys = {
        (pair_id, arm) for pair_id in protocol.pair_ids for arm in protocol.expected_arms
    }
    supplied_keys = {(trial.pair_id, trial.arm) for trial in trials}
    if expected_keys != supplied_keys or len(trials) != len(expected_keys):
        missing = expected_keys - supplied_keys
        unplanned = supplied_keys - expected_keys
        raise LabEvaluationViolation(
            "trial set is incomplete, duplicated or names an unplanned pair/arm slot",
            detail={
                "missing": sorted(f"{pair}:{arm.value}" for pair, arm in missing),
                "unplanned": sorted(f"{pair}:{arm.value}" for pair, arm in unplanned),
                "expected_count": len(expected_keys),
                "supplied_count": len(trials),
            },
        )

    per_arm: list[ArmTally] = []
    by_arm: dict[Arm, dict[str, OutcomeStatus]] = {arm: {} for arm in protocol.expected_arms}
    for trial in trials:
        by_arm[trial.arm][trial.pair_id] = trial.outcome_status

    for arm in ARM_ORDER:
        arm_trials = [trial for trial in trials if trial.arm is arm]
        statuses = [trial.outcome_status for trial in arm_trials]
        known_costs = [trial.total_cost for trial in arm_trials if trial.total_cost is not None]
        known_latencies = [trial.latency_ms for trial in arm_trials if trial.latency_ms is not None]
        per_arm.append(
            ArmTally(
                arm=arm,
                denominator=len(protocol.pair_ids),
                successes=statuses.count(OutcomeStatus.SUCCESS),
                failures=statuses.count(OutcomeStatus.FAILURE),
                abstained=statuses.count(OutcomeStatus.ABSTAINED),
                missing=statuses.count(OutcomeStatus.MISSING),
                critical_violations=sum(trial.critical_violation for trial in arm_trials),
                unknown_cost_count=sum(trial.total_cost is None for trial in arm_trials),
                unknown_latency_count=sum(trial.latency_ms is None for trial in arm_trials),
                total_cost=sum(known_costs),
                p95_latency_ms=_p95(known_latencies),
            )
        )

    paired_deltas: list[PairedDelta] = []
    for comparison_arm in (Arm.SOURCE_ONLY, Arm.CONTROLLER_PLUS_SOURCE):
        gained = 0
        lost = 0
        incomplete = 0
        for pair_id in protocol.pair_ids:
            baseline_status = by_arm[Arm.EXISTING_STACK][pair_id]
            comparison_status = by_arm[comparison_arm][pair_id]
            if OutcomeStatus.MISSING in (baseline_status, comparison_status):
                incomplete += 1
                continue
            baseline_success = baseline_status is OutcomeStatus.SUCCESS
            comparison_success = comparison_status is OutcomeStatus.SUCCESS
            if comparison_success and not baseline_success:
                gained += 1
            elif baseline_success and not comparison_success:
                lost += 1
        paired_deltas.append(
            PairedDelta(
                comparison_arm=comparison_arm,
                baseline_arm=Arm.EXISTING_STACK,
                pairs_compared=len(protocol.pair_ids) - incomplete,
                pairs_incomplete=incomplete,
                successes_gained=gained,
                successes_lost=lost,
                net_delta=gained - lost,
            )
        )

    any_non_missing = any(item.missing < item.denominator for item in per_arm)
    status = (
        ReportStatus.DESCRIPTIVE_ONLY if any_non_missing else ReportStatus.INSUFFICIENT_EVIDENCE
    )

    body: dict[str, object] = {
        "contract_version": LAB_EVALUATION_CONTRACT_VERSION,
        "protocol_id": protocol.protocol_id,
        "protocol_seal": expected_protocol_seal,
        "status": status.value,
        "quantile_convention": QUANTILE_CONVENTION,
        "non_inferiority_margin_declared": protocol.non_inferiority_margin,
        "cost_target_declared": protocol.cost_target,
        "per_arm": [item.canonical_payload() for item in per_arm],
        "paired_deltas": [item.canonical_payload() for item in paired_deltas],
        "empirical_claim": False,
        "causal_claim": False,
    }
    return LabEvaluationReport(
        contract_version=LAB_EVALUATION_CONTRACT_VERSION,
        protocol_id=protocol.protocol_id,
        protocol_seal=expected_protocol_seal,
        status=status,
        non_inferiority_margin_declared=protocol.non_inferiority_margin,
        cost_target_declared=protocol.cost_target,
        per_arm=tuple(per_arm),
        paired_deltas=tuple(paired_deltas),
        report_seal=seal(REPORT_SEAL_DOMAIN, body),
    )

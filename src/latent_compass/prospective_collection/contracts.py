"""Immutable HOK-252 prospective shadow-collection contracts and exact planner."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from math import exp, lgamma, log, log1p
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    SUPPORTED_PROSPECTIVE_COLLECTION_VERSIONS,
    Identifier,
    StrictModel,
    Timestamp,
    UnitInterval,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory import DecisionBinding
from latent_compass.decision_reconciliation import ReconciliationBinding
from latent_compass.errors import ProspectiveCollectionViolation

__all__ = [
    "AbortReason",
    "CaseState",
    "ExactBinomialInputs",
    "ExactBinomialResult",
    "HardExclusion",
    "PlanState",
    "PowerStatus",
    "ProducerIndependencePolicy",
    "ProspectiveCollectionPlan",
    "ProspectiveSourceBinding",
    "StopCondition",
    "plan_exact_one_sided_binomial",
]

PLANNER_METHOD: Final = "EXACT_ONE_SIDED_BINOMIAL_V1"
PLAN_SEAL_DOMAIN: Final = "prospective-collection.plan.v1"
MAX_POWER_SEARCH: Final = 10_000

PopulationText = Annotated[str, Field(min_length=1, max_length=1000)]
Stratum = Annotated[str, Field(min_length=1, max_length=128)]


class PowerStatus(StrEnum):
    ESTABLISHED = "ESTABLISHED"
    POWER_NOT_ESTABLISHED = "POWER_NOT_ESTABLISHED"


class PlanState(StrEnum):
    SEALED = "SEALED"
    COLLECTING = "COLLECTING"
    CLOSED_SUFFICIENT = "CLOSED_SUFFICIENT"
    CLOSED_INSUFFICIENT = "CLOSED_INSUFFICIENT"
    ABORTED = "ABORTED"


class CaseState(StrEnum):
    ENROLLED = "ENROLLED"
    RECONCILED = "RECONCILED"
    ABSTAINED = "ABSTAINED"
    CANCELLED = "CANCELLED"
    LOST_TO_FOLLOWUP = "LOST_TO_FOLLOWUP"


class AbortReason(StrEnum):
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    SECURITY_FAILURE = "SECURITY_FAILURE"


class HardExclusion(StrEnum):
    PRE_PLAN_OR_BACKFILL = "PRE_PLAN_OR_BACKFILL"
    WRONG_BINDING = "WRONG_BINDING"
    HOLDOUT = "HOLDOUT"
    CANCELLED = "CANCELLED"
    INVALID_PREIMAGE = "INVALID_PREIMAGE"
    NON_INDEPENDENT_RECONCILIATION = "NON_INDEPENDENT_RECONCILIATION"


class StopCondition(StrEnum):
    INTEGRITY_OR_SECURITY_FAILURE = "INTEGRITY_OR_SECURITY_FAILURE"
    SUFFICIENT_AFTER_MINIMUM_CALENDAR = "SUFFICIENT_AFTER_MINIMUM_CALENDAR"
    INSUFFICIENT_AT_HARD_CALENDAR_END = "INSUFFICIENT_AT_HARD_CALENDAR_END"


class ExactBinomialInputs(StrictModel):
    method: str
    p0: UnitInterval
    p1: UnitInterval
    alpha: UnitInterval
    target_power: UnitInterval
    max_enrollments: int = Field(ge=1, le=MAX_POWER_SEARCH)
    clustering_inflation: float = Field(ge=1.0, le=100.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _valid_test(self) -> Self:
        if self.method != PLANNER_METHOD:
            raise ValueError(f"method must be {PLANNER_METHOD}")
        if not self.p1 > self.p0:
            raise ValueError("p1 must be strictly greater than p0")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must lie strictly between zero and one")
        if not 0.0 < self.target_power < 1.0:
            raise ValueError("target_power must lie strictly between zero and one")
        return self


class ExactBinomialResult(StrictModel):
    method: str
    status: PowerStatus
    inputs: ExactBinomialInputs
    independent_eligible_count: int | None = Field(default=None, ge=1)
    required_enrollments: int | None = Field(default=None, ge=1)
    critical_eligible_count: int | None = Field(default=None, ge=1)
    achieved_alpha: float | None = Field(default=None, ge=0.0, le=1.0)
    achieved_power: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _status_matches_result(self) -> Self:
        if self.method != PLANNER_METHOD or self.method != self.inputs.method:
            raise ValueError("power result method does not match its inputs")
        values = (
            self.independent_eligible_count,
            self.required_enrollments,
            self.critical_eligible_count,
            self.achieved_alpha,
            self.achieved_power,
        )
        if self.status is PowerStatus.ESTABLISHED and any(item is None for item in values):
            raise ValueError("an established power result carries every result field")
        if self.status is PowerStatus.POWER_NOT_ESTABLISHED and any(
            item is not None for item in values
        ):
            raise ValueError("POWER_NOT_ESTABLISHED carries no invented result")
        solution = _solve_values(self.inputs)
        if self.status is PowerStatus.POWER_NOT_ESTABLISHED:
            if solution is not None:
                raise ValueError("POWER_NOT_ESTABLISHED contradicts the exact planner")
            return self
        if solution is None or values != solution:
            raise ValueError("power result is not the exact minimal deterministic solution")
        return self


class ProspectiveSourceBinding(StrictModel):
    decision: DecisionBinding
    reconciliation: ReconciliationBinding

    @model_validator(mode="after")
    def _same_durable_identity(self) -> Self:
        for field in ("host_id", "agent_family", "store_id", "epoch"):
            if getattr(self.decision, field) != getattr(self.reconciliation, field):
                raise ValueError(f"decision and reconciliation {field} must match")
        return self


class ProducerIndependencePolicy(StrictModel):
    decision_producer_identities: tuple[Identifier, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.decision_producer_identities)) != len(self.decision_producer_identities):
            raise ValueError("decision producer identities must be unique")
        return self


class ProspectiveCollectionPlan(StrictModel):
    contract_version: str
    plan_id: Identifier
    source_binding: ProspectiveSourceBinding
    population: PopulationText
    declared_strata: tuple[Stratum, ...] = Field(min_length=1, max_length=64)
    hard_exclusions: tuple[HardExclusion, ...]
    plan_created_at: Timestamp
    collection_not_before: Timestamp
    minimum_calendar_end: Timestamp
    hard_calendar_end: Timestamp
    max_enrollments: int = Field(ge=1, le=MAX_POWER_SEARCH)
    outcome_dependent_interim_looks: int = Field(ge=0, le=0)
    independence_policy: ProducerIndependencePolicy
    power: ExactBinomialResult
    stop_priority: tuple[StopCondition, ...]

    @model_validator(mode="after")
    def _coherent_plan(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_PROSPECTIVE_COLLECTION_VERSIONS,
            "prospective collection plan",
        )
        if self.power.status is not PowerStatus.ESTABLISHED:
            raise ValueError("collection cannot start while power is not established")
        if self.power.inputs.max_enrollments != self.max_enrollments:
            raise ValueError("plan and power result must name the same max_enrollments")
        if not (
            self.plan_created_at
            <= self.collection_not_before
            <= self.minimum_calendar_end
            <= self.hard_calendar_end
        ):
            raise ValueError("plan and collection calendar must be monotonic")
        if len(set(self.declared_strata)) != len(self.declared_strata):
            raise ValueError("declared strata must be unique")
        required_exclusions = tuple(HardExclusion)
        if self.hard_exclusions != required_exclusions:
            raise ValueError("the plan must declare every hard exclusion in canonical order")
        if self.stop_priority != tuple(StopCondition):
            raise ValueError("stop priority must be complete and deterministic")
        return self

    def plan_seal(self) -> str:
        return seal(PLAN_SEAL_DOMAIN, self.canonical_payload())


def _binomial_tail(n: int, threshold: int, probability: float) -> float:
    if threshold <= 0:
        return 1.0
    if threshold > n:
        return 0.0
    if probability == 0.0:
        return 0.0
    if probability == 1.0:
        return 1.0

    return _binomial_tails(n, probability)[threshold]


def _binomial_tails(n: int, probability: float) -> list[float]:
    if probability == 0.0:
        return [1.0, *([0.0] * (n + 1))]
    if probability == 1.0:
        return [1.0] * (n + 1) + [0.0]
    mode = min(n, int((n + 1) * probability))
    probabilities = [0.0] * (n + 1)
    probabilities[mode] = exp(
        lgamma(n + 1)
        - lgamma(mode + 1)
        - lgamma(n - mode + 1)
        + mode * log(probability)
        + (n - mode) * log1p(-probability)
    )
    for successes in range(mode, 0, -1):
        probabilities[successes - 1] = (
            probabilities[successes]
            * successes
            / (n - successes + 1)
            * (1.0 - probability)
            / probability
        )
    for successes in range(mode, n):
        probabilities[successes + 1] = (
            probabilities[successes]
            * (n - successes)
            / (successes + 1)
            * probability
            / (1.0 - probability)
        )
    tails = [0.0] * (n + 2)
    running = 0.0
    compensation = 0.0
    for successes in range(n, -1, -1):
        increment = probabilities[successes] - compensation
        updated = running + increment
        compensation = (updated - running) - increment
        running = updated
        tails[successes] = min(1.0, running)
    return tails


def _inflated_enrollments(n: int, inflation: float) -> int:
    product = Decimal(n) * Decimal(str(inflation))
    return int(product.to_integral_value(rounding=ROUND_CEILING))


def _binomial_pmf(n: int, successes: int, probability: float) -> float:
    if successes < 0 or successes > n:
        return 0.0
    if probability == 0.0:
        return 1.0 if successes == 0 else 0.0
    if probability == 1.0:
        return 1.0 if successes == n else 0.0
    return exp(
        lgamma(n + 1)
        - lgamma(successes + 1)
        - lgamma(n - successes + 1)
        + successes * log(probability)
        + (n - successes) * log1p(-probability)
    )


def _solve_values(
    inputs: ExactBinomialInputs,
) -> tuple[int, int, int, float, float] | None:
    critical = 1
    null_tail = inputs.p0
    alternative_tail = inputs.p1
    for n in range(1, inputs.max_enrollments + 1):
        required = _inflated_enrollments(n, inputs.clustering_inflation)
        if required > inputs.max_enrollments:
            break
        if n > 1:
            null_tail += inputs.p0 * _binomial_pmf(n - 1, critical - 1, inputs.p0)
            alternative_tail += inputs.p1 * _binomial_pmf(n - 1, critical - 1, inputs.p1)
        while critical <= n and null_tail > inputs.alpha:
            null_tail -= _binomial_pmf(n, critical, inputs.p0)
            alternative_tail -= _binomial_pmf(n, critical, inputs.p1)
            critical += 1
        if critical > n:
            continue
        if alternative_tail >= inputs.target_power:
            null_tails = _binomial_tails(n, inputs.p0)
            exact_critical = next(
                (k for k in range(1, n + 1) if null_tails[k] <= inputs.alpha), None
            )
            if exact_critical is None:
                continue
            achieved_power = _binomial_tail(n, exact_critical, inputs.p1)
            if achieved_power < inputs.target_power:
                continue
            return (
                n,
                required,
                exact_critical,
                null_tails[exact_critical],
                achieved_power,
            )
    return None


def plan_exact_one_sided_binomial(
    *,
    p0: float,
    p1: float,
    alpha: float,
    target_power: float,
    max_enrollments: int,
    clustering_inflation: float,
) -> ExactBinomialResult:
    """Find the smallest independent denominator, then inflate enrollment exactly once."""
    inputs = validate_contract(
        ExactBinomialInputs,
        {
            "method": PLANNER_METHOD,
            "p0": p0,
            "p1": p1,
            "alpha": alpha,
            "target_power": target_power,
            "max_enrollments": max_enrollments,
            "clustering_inflation": clustering_inflation,
        },
        error=ProspectiveCollectionViolation,
        context="exact one-sided binomial inputs",
    )
    solution = _solve_values(inputs)
    if solution is not None:
        independent, required, critical, achieved_alpha, achieved_power = solution
        return ExactBinomialResult(
            method=PLANNER_METHOD,
            status=PowerStatus.ESTABLISHED,
            inputs=inputs,
            independent_eligible_count=independent,
            required_enrollments=required,
            critical_eligible_count=critical,
            achieved_alpha=achieved_alpha,
            achieved_power=achieved_power,
        )
    return ExactBinomialResult(
        method=PLANNER_METHOD,
        status=PowerStatus.POWER_NOT_ESTABLISHED,
        inputs=inputs,
    )


def admit_plan(payload: object) -> ProspectiveCollectionPlan:
    return validate_contract(
        ProspectiveCollectionPlan,
        payload,
        error=ProspectiveCollectionViolation,
        context="prospective collection plan",
    )

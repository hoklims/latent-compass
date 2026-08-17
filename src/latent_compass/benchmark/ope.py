"""HOK-188 — off-policy evaluation from logged bandit feedback.

The corpus records what a logging policy did and what happened next. It does not
record what would have happened had a baseline chosen differently. That is the
whole problem, and inverse propensity scoring is the estimator this benchmark
pre-registers for it.

The estimator
-------------
For a deterministic target policy ``pi`` and a logged action ``a`` drawn with
probability ``mu(a|x)``::

    w_i  = pi(a_i|x_i) / mu(a_i|x_i)      # 1/mu when the target agrees, else 0
    IPS  = mean_i( w_i * y_i )            # primary
    SNIPS = sum_i(w_i * y_i) / sum_i(w_i) # sensitivity diagnostic only

``IPS`` is unbiased under overlap and reports the scale of the outcome.
``SNIPS`` has lower variance and is self-normalising, but is biased in finite
samples, so it is carried as a diagnostic and never promoted to the primary
value. When ``sum(w) == 0`` — the target agreed with the logging policy on no
eligible case at all — ``SNIPS`` is *undefined*, and it is reported as undefined
rather than as zero.

No case is dropped in silence
-----------------------------
Every case reaches exactly one :class:`CaseEligibility` state, and
:class:`SupportDiagnostics` refuses to validate unless the states account for
the whole corpus. A case that is censored, unobservable, incompletely observed
or out of support is *named*; it never simply fails to appear.

The support caveat this benchmark cannot escape
-----------------------------------------------
IPS is only meaningful where the logging policy could have produced the target
action. A near-deterministic logging policy gives most of its mass to one
direction, so a baseline that reproduces that direction inherits a large
effective sample while every other baseline collapses towards zero non-zero
weights. That is a property of the *data*, not evidence about the baselines, and
``effective_sample_size`` is reported precisely so the reader can see it. This
is why no comparison in this package is stated as a ranking.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Final

from pydantic import Field, model_validator

from latent_compass.benchmark.budget import BudgetReceipt
from latent_compass.benchmark.corpus import BenchmarkCase
from latent_compass.canonical import seal
from latent_compass.contracts import FiniteFloat, Identifier, StrictModel, UnitInterval
from latent_compass.episode import Observability
from latent_compass.errors import BenchmarkViolation

__all__ = [
    "CaseEligibility",
    "ChannelEstimate",
    "OutcomeChannel",
    "SupportDiagnostics",
    "TrialRecord",
    "classify",
    "estimate_channel",
    "quantile",
    "reward",
    "summarise_support",
    "trial_binding_seal",
]

TRIAL_SEAL_DOMAIN: Final = "benchmark.trial"


class CaseEligibility(StrEnum):
    """Why a case does or does not enter the estimator. Exhaustive by design."""

    ELIGIBLE = "ELIGIBLE"
    """Outcome observed by the cutoff, complete, and the target action supported."""

    UNOBSERVABLE = "UNOBSERVABLE"
    """The outcome was never observable at all — observability ``NONE``."""

    CENSORED_IMMATURE = "CENSORED_IMMATURE"
    """Observable, but not observed by this case's cutoff."""

    INCOMPLETE_OUTCOME = "INCOMPLETE_OUTCOME"
    """Observed, but missing a field the estimator needs. Never defaulted to zero."""

    OUT_OF_SUPPORT = "OUT_OF_SUPPORT"
    """The logging policy's probability of the target action is below the floor."""


class OutcomeChannel(StrEnum):
    """The observed quantity an estimate is taken over."""

    SUCCESS = "SUCCESS"
    VIOLATIONS = "VIOLATIONS"
    COST = "COST"
    INFORMATION_GAIN = "INFORMATION_GAIN"
    REVERSIBILITY = "REVERSIBILITY"
    BRIER = "BRIER"
    """Squared error of the baseline's own confidence against observed success."""


class TrialRecord(StrictModel):
    """One baseline, on one case, at one seed. The raw receipt of the run."""

    baseline_id: Identifier
    algorithm_version: Identifier
    case_id: Identifier
    seed: int
    distribution_group: Identifier
    selected_direction_id: Identifier
    confidence: UnitInterval
    budget: BudgetReceipt
    eligibility: CaseEligibility
    logged_direction_id: Identifier
    logged_propensity: FiniteFloat = Field(gt=0.0, le=1.0)
    logging_propensity_of_target_action: FiniteFloat = Field(gt=0.0, le=1.0)
    weight: FiniteFloat = Field(ge=0.0)
    binding_seal: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _weight_reproduces_from_the_record(self) -> TrialRecord:
        if self.eligibility is not CaseEligibility.ELIGIBLE:
            if self.weight != 0.0:
                raise ValueError("an ineligible trial must carry a zero weight")
            return self
        agrees = self.selected_direction_id == self.logged_direction_id
        expected = (1.0 / self.logged_propensity) if agrees else 0.0
        if not math.isclose(self.weight, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("the importance weight does not reproduce from the record")
        return self

    def grid_key(self) -> tuple[str, str, int]:
        """The coordinate this record occupies in the baseline x case x seed grid."""
        return (self.baseline_id, self.case_id, self.seed)


def trial_binding_seal(
    *,
    spec_seal: str,
    corpus_seal: str,
    baseline_id: str,
    algorithm_version: str,
    case_id: str,
    seed: int,
    selected_direction_id: str,
) -> str:
    """Seal binding one selection to the spec, corpus, baseline, case and seed.

    A receipt lifted from another run, another corpus or another seed does not
    reproduce this seal, so a copied result is detectable without re-deriving
    the whole report.
    """
    return seal(
        TRIAL_SEAL_DOMAIN,
        {
            "spec_seal": spec_seal,
            "corpus_seal": corpus_seal,
            "baseline_id": baseline_id,
            "algorithm_version": algorithm_version,
            "case_id": case_id,
            "seed": seed,
            "selected_direction_id": selected_direction_id,
        },
    )


def classify(
    case: BenchmarkCase, *, target_direction_id: str, minimum_propensity: float
) -> CaseEligibility:
    """Place a case in exactly one eligibility state.

    Order matters and is pre-registered: an unobservable outcome is reported as
    unobservable rather than as censored, because "we could never have seen it"
    and "we have not seen it yet" are different facts about the corpus.
    """
    outcome = case.episode.outcome
    if outcome is None or outcome.observability is Observability.NONE:
        return CaseEligibility.UNOBSERVABLE
    if not case.outcome_is_mature():
        return CaseEligibility.CENSORED_IMMATURE
    if outcome.success is None or outcome.violations is None:
        return CaseEligibility.INCOMPLETE_OUTCOME
    if case.logged_propensity(target_direction_id) < minimum_propensity:
        return CaseEligibility.OUT_OF_SUPPORT
    return CaseEligibility.ELIGIBLE


def reward(case: BenchmarkCase, channel: OutcomeChannel, *, confidence: float) -> float:
    """The observed scalar for ``channel`` on an eligible case.

    Only ever called on an ``ELIGIBLE`` case, where ``success`` and
    ``violations`` are both known to be present; the explicit refusal below is
    what makes that a checked precondition rather than an assumption.
    """
    outcome = case.episode.outcome
    if outcome is None or outcome.success is None or outcome.violations is None:
        raise BenchmarkViolation(
            "an outcome channel was requested for a case with no complete outcome",
            detail={"case_id": case.case_id, "channel": channel.value},
        )
    economics = case.episode.economics
    match channel:
        case OutcomeChannel.SUCCESS:
            return 1.0 if outcome.success else 0.0
        case OutcomeChannel.VIOLATIONS:
            return float(outcome.violations)
        case OutcomeChannel.COST:
            return economics.cost
        case OutcomeChannel.INFORMATION_GAIN:
            return economics.information_gain
        case OutcomeChannel.REVERSIBILITY:
            return economics.reversibility
        case OutcomeChannel.BRIER:
            observed = 1.0 if outcome.success else 0.0
            return (confidence - observed) ** 2


def quantile(values: list[float], probability: float) -> float:
    """Deterministic nearest-rank quantile over an ascending copy of ``values``.

    Nearest-rank rather than an interpolating definition: interpolation invents
    a value that no case produced, and every number in this report has to be
    traceable to a case.
    """
    if not values:
        raise BenchmarkViolation(
            "a quantile was requested over an empty sample",
            detail={"probability": probability},
        )
    ordered = sorted(values)
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


class SupportDiagnostics(StrictModel):
    """What the estimator was actually able to use, and what it could not."""

    total_cases: int = Field(ge=1)
    eligible_cases: int = Field(ge=0)
    unobservable_cases: int = Field(ge=0)
    censored_immature_cases: int = Field(ge=0)
    incomplete_outcome_cases: int = Field(ge=0)
    out_of_support_cases: int = Field(ge=0)
    support_rate: FiniteFloat = Field(ge=0.0, le=1.0)
    nonzero_weight_cases: int = Field(ge=0)
    weight_sum: FiniteFloat = Field(ge=0.0)
    weight_max: FiniteFloat = Field(ge=0.0)
    weight_p50: FiniteFloat = Field(ge=0.0)
    weight_p90: FiniteFloat = Field(ge=0.0)
    weight_p99: FiniteFloat = Field(ge=0.0)
    effective_sample_size: FiniteFloat = Field(ge=0.0)

    @model_validator(mode="after")
    def _every_case_is_accounted_for(self) -> SupportDiagnostics:
        counted = (
            self.eligible_cases
            + self.unobservable_cases
            + self.censored_immature_cases
            + self.incomplete_outcome_cases
            + self.out_of_support_cases
        )
        if counted != self.total_cases:
            raise ValueError(
                "eligibility states do not account for every case: "
                f"{counted} classified against {self.total_cases} present"
            )
        if self.nonzero_weight_cases > self.eligible_cases:
            raise ValueError("more non-zero weights than eligible cases")
        expected_rate = self.eligible_cases / self.total_cases
        if not math.isclose(self.support_rate, expected_rate, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("support_rate does not reproduce from the counts")
        return self


def summarise_support(records: tuple[TrialRecord, ...]) -> SupportDiagnostics:
    """Summarise one baseline's trials at one seed."""
    if not records:
        raise BenchmarkViolation(
            "support diagnostics were requested over an empty trial set", detail={}
        )
    counts = dict.fromkeys(CaseEligibility, 0)
    for record in records:
        counts[record.eligibility] += 1

    weights = [
        record.weight for record in records if record.eligibility is CaseEligibility.ELIGIBLE
    ]
    weight_sum = math.fsum(weights)
    squared_sum = math.fsum(weight * weight for weight in weights)
    ess = (weight_sum * weight_sum / squared_sum) if squared_sum > 0.0 else 0.0
    # An eligible set is never empty here only because the caller refuses a run
    # with no eligible case; quantiles over an empty sample still refuse loudly.
    sample = weights if weights else [0.0]

    return SupportDiagnostics(
        total_cases=len(records),
        eligible_cases=counts[CaseEligibility.ELIGIBLE],
        unobservable_cases=counts[CaseEligibility.UNOBSERVABLE],
        censored_immature_cases=counts[CaseEligibility.CENSORED_IMMATURE],
        incomplete_outcome_cases=counts[CaseEligibility.INCOMPLETE_OUTCOME],
        out_of_support_cases=counts[CaseEligibility.OUT_OF_SUPPORT],
        support_rate=counts[CaseEligibility.ELIGIBLE] / len(records),
        nonzero_weight_cases=sum(1 for weight in weights if weight > 0.0),
        weight_sum=weight_sum,
        weight_max=max(sample),
        weight_p50=quantile(sample, 0.50),
        weight_p90=quantile(sample, 0.90),
        weight_p99=quantile(sample, 0.99),
        effective_sample_size=ess,
    )


class ChannelEstimate(StrictModel):
    """The primary IPS value for one channel, with its SNIPS diagnostic."""

    channel: OutcomeChannel
    eligible_cases: int = Field(ge=1)
    ips: FiniteFloat
    snips: FiniteFloat | None = Field(default=None)
    snips_defined: bool

    @model_validator(mode="after")
    def _undefined_snips_is_stated_not_zeroed(self) -> ChannelEstimate:
        if self.snips_defined is (self.snips is None):
            raise ValueError("snips_defined must agree with whether a SNIPS value is present")
        return self


def estimate_channel(
    channel: OutcomeChannel, weighted: list[tuple[float, float]]
) -> ChannelEstimate:
    """Compute IPS and SNIPS from ``(weight, outcome)`` pairs over eligible cases."""
    if not weighted:
        raise BenchmarkViolation(
            "an estimate was requested with no eligible case",
            detail={"channel": channel.value},
        )
    products = [weight * value for weight, value in weighted]
    weight_sum = math.fsum(weight for weight, _ in weighted)
    ips = math.fsum(products) / len(weighted)
    if weight_sum > 0.0:
        return ChannelEstimate(
            channel=channel,
            eligible_cases=len(weighted),
            ips=ips,
            snips=math.fsum(products) / weight_sum,
            snips_defined=True,
        )
    return ChannelEstimate(
        channel=channel, eligible_cases=len(weighted), ips=ips, snips=None, snips_defined=False
    )

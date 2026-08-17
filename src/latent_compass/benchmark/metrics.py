"""HOK-188 — the eight metric families, computed per seed.

Every family is derived from the same eligible trial set, so the vector is
internally consistent: a reader can ask "why is cost high here?" and reach the
same cases that produced the success number.

Per-seed first, aggregated later
--------------------------------
Nothing here aggregates across seeds. The raw per-seed vector is what reaches
the report; HOK-181's median-over-seeds runs afterwards, inside
:func:`~latent_compass.protocol.score_measurements`, from the sealed
:class:`~latent_compass.protocol.MeasurementSet`. Collapsing seeds earlier would
put an aggregation rule in two places, and the pre-registered one is the
protocol's.

The families
------------
Six are IPS values over an observed channel. Two are shaped differently and are
pre-registered as such:

``TAIL``
    A nearest-rank quantile of the per-case weighted cost contributions
    ``w_i * cost_i``, at the quantile fixed in the spec. Long-tail behaviour of
    cost, on the same weighting as every other estimate, lower is better.

``DRIFT``
    The largest absolute gap between the nominal population's success estimate
    and any single shift group's. Lower is better. It refuses rather than
    reports when a stratum has no eligible case, because a gap measured against
    nothing is not a small gap.

There is no global mean and no composite score. Eight numbers stay eight
numbers; a scalar champion would hide exactly the trade-off the vector exists to
expose.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from latent_compass.benchmark.corpus import NOMINAL_GROUP, BenchmarkCase, DistributionKind
from latent_compass.benchmark.ope import (
    CaseEligibility,
    ChannelEstimate,
    OutcomeChannel,
    SupportDiagnostics,
    TrialRecord,
    estimate_channel,
    quantile,
    reward,
    summarise_support,
)
from latent_compass.benchmark.spec import BENCHMARK_METRIC_NAMES, BenchmarkSpec
from latent_compass.contracts import FiniteFloat, Identifier, StrictModel
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import MetricFamily

__all__ = [
    "IPS_CHANNELS",
    "DriftGroupEstimate",
    "MetricValue",
    "SeedEvaluation",
    "evaluate_seed",
]

#: Which observed channel each IPS-shaped family is taken over.
IPS_CHANNELS: dict[MetricFamily, OutcomeChannel] = {
    MetricFamily.SUCCESS: OutcomeChannel.SUCCESS,
    MetricFamily.VIOLATION: OutcomeChannel.VIOLATIONS,
    MetricFamily.COST: OutcomeChannel.COST,
    MetricFamily.INFORMATION: OutcomeChannel.INFORMATION_GAIN,
    MetricFamily.REVERSIBILITY: OutcomeChannel.REVERSIBILITY,
    MetricFamily.CALIBRATION: OutcomeChannel.BRIER,
}


class MetricValue(StrictModel):
    """One family's value for one baseline at one seed."""

    family: MetricFamily
    metric: Identifier
    value: FiniteFloat


class DriftGroupEstimate(StrictModel):
    """The success estimate for one distribution stratum."""

    distribution_group: Identifier
    kind: DistributionKind
    eligible_cases: int = Field(ge=1)
    ips_success: FiniteFloat
    gap_to_nominal: FiniteFloat = Field(ge=0.0)


class SeedEvaluation(StrictModel):
    """Everything one baseline produced at one seed."""

    seed: int
    diagnostics: SupportDiagnostics
    estimates: tuple[ChannelEstimate, ...] = Field(min_length=6, max_length=6)
    drift_groups: tuple[DriftGroupEstimate, ...] = Field(min_length=2, max_length=64)
    metrics: tuple[MetricValue, ...] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def _the_vector_is_complete_and_unduplicated(self) -> SeedEvaluation:
        families = [value.family for value in self.metrics]
        if sorted(families) != sorted(MetricFamily):
            raise ValueError("the metric vector must carry each of the eight families exactly once")
        channels = [estimate.channel for estimate in self.estimates]
        if len(set(channels)) != len(channels):
            raise ValueError("channel estimates must be unique")
        groups = [group.distribution_group for group in self.drift_groups]
        if len(set(groups)) != len(groups):
            raise ValueError("drift groups must be unique")
        if NOMINAL_GROUP not in groups:
            raise ValueError("drift needs the nominal population to compare against")
        return self

    def measurement_values(self) -> dict[str, float]:
        """The vector as ``metric name -> value``, ready for a MeasurementSet."""
        return {value.metric: value.value for value in self.metrics}


def _weighted_pairs(
    channel: OutcomeChannel,
    records: tuple[TrialRecord, ...],
    cases: dict[str, BenchmarkCase],
) -> list[tuple[float, float]]:
    return [
        (record.weight, reward(cases[record.case_id], channel, confidence=record.confidence))
        for record in records
        if record.eligibility is CaseEligibility.ELIGIBLE
    ]


def _drift(
    records: tuple[TrialRecord, ...], cases: dict[str, BenchmarkCase]
) -> tuple[tuple[DriftGroupEstimate, ...], float]:
    """Success estimates per stratum, and the largest gap to nominal."""
    eligible = [record for record in records if record.eligibility is CaseEligibility.ELIGIBLE]
    by_group: dict[str, list[TrialRecord]] = {}
    for record in eligible:
        by_group.setdefault(record.distribution_group, []).append(record)

    declared = {case.distribution_group for case in cases.values()}
    empty = sorted(declared - set(by_group))
    if empty:
        raise BenchmarkViolation(
            "a distribution group has no eligible case, so DRIFT cannot be measured over it",
            detail={"empty_groups": empty, "measured_groups": sorted(by_group)},
        )
    if NOMINAL_GROUP not in by_group:
        raise BenchmarkViolation(
            "there is no eligible nominal case to compare shift groups against",
            detail={"measured_groups": sorted(by_group)},
        )

    nominal_ips = estimate_channel(
        OutcomeChannel.SUCCESS,
        _weighted_pairs(OutcomeChannel.SUCCESS, tuple(by_group[NOMINAL_GROUP]), cases),
    ).ips

    estimates: list[DriftGroupEstimate] = []
    largest_gap = 0.0
    for group in sorted(by_group):
        group_records = tuple(by_group[group])
        group_ips = estimate_channel(
            OutcomeChannel.SUCCESS, _weighted_pairs(OutcomeChannel.SUCCESS, group_records, cases)
        ).ips
        gap = abs(group_ips - nominal_ips)
        kind = DistributionKind.NOMINAL if group == NOMINAL_GROUP else DistributionKind.SHIFT
        if kind is DistributionKind.SHIFT:
            largest_gap = max(largest_gap, gap)
        estimates.append(
            DriftGroupEstimate(
                distribution_group=group,
                kind=kind,
                eligible_cases=len(group_records),
                ips_success=group_ips,
                gap_to_nominal=gap,
            )
        )
    return tuple(estimates), largest_gap


def evaluate_seed(
    *,
    seed: int,
    records: tuple[TrialRecord, ...],
    cases: dict[str, BenchmarkCase],
    spec: BenchmarkSpec,
) -> SeedEvaluation:
    """Derive the eight-family vector for one baseline at one seed.

    Refuses rather than reports when no case is eligible: an eight-family vector
    computed over nothing would validate, seal and mean nothing.
    """
    if not records:
        raise BenchmarkViolation(
            "no trial records for this baseline and seed", detail={"seed": seed}
        )
    eligible = tuple(record for record in records if record.eligibility is CaseEligibility.ELIGIBLE)
    if not eligible:
        raise BenchmarkViolation(
            "no eligible case at this seed; the metric vector would be empty",
            detail={
                "seed": seed,
                "baseline_id": records[0].baseline_id,
                "total_cases": len(records),
            },
        )

    estimates = tuple(
        estimate_channel(channel, _weighted_pairs(channel, records, cases))
        for channel in sorted(OutcomeChannel)
    )
    by_channel = {estimate.channel: estimate for estimate in estimates}

    drift_groups, drift_gap = _drift(records, cases)
    tail = quantile(
        [
            record.weight
            * reward(cases[record.case_id], OutcomeChannel.COST, confidence=record.confidence)
            for record in eligible
        ],
        spec.tail_quantile,
    )

    values = [
        MetricValue(
            family=family,
            metric=BENCHMARK_METRIC_NAMES[family],
            value=by_channel[channel].ips,
        )
        for family, channel in sorted(IPS_CHANNELS.items())
    ]
    values.append(
        MetricValue(
            family=MetricFamily.TAIL,
            metric=BENCHMARK_METRIC_NAMES[MetricFamily.TAIL],
            value=tail,
        )
    )
    values.append(
        MetricValue(
            family=MetricFamily.DRIFT,
            metric=BENCHMARK_METRIC_NAMES[MetricFamily.DRIFT],
            value=drift_gap,
        )
    )

    return SeedEvaluation(
        seed=seed,
        diagnostics=summarise_support(records),
        estimates=estimates,
        drift_groups=drift_groups,
        metrics=tuple(sorted(values, key=lambda value: value.metric)),
    )

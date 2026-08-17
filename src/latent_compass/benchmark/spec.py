"""HOK-188 — the benchmark specification.

The spec is the pre-registered *plan of the run*: which protocol governs it,
which corpus it is bound to, which split it may execute, which seeds, which four
baselines at which pinned versions, and the one budget every baseline receives.
It is sealed, and its seal reaches the report.

The spec binds; it does not restate
-----------------------------------
Thresholds, metric families and directions live in the HOK-181
:class:`~latent_compass.protocol.Preregistration` and nowhere else. This module
*checks* that the pre-registration fixes exactly the eight metrics the benchmark
knows how to produce, and refuses otherwise. Restating a threshold here would
create two places to change it, and a benchmark whose thresholds can drift
against its own pre-registration is not pre-registered.

Split confinement is a spec property
------------------------------------
``executed_split`` is validated to ``VALIDATION`` by the model itself. A spec
naming ``HOLDOUT`` is refused during validation — before a corpus directory has
been resolved, before a manifest has been consulted, and therefore before any
data file could have been opened. HOK-190 owns the later pairwise-ranker
evaluation, including its final holdout comparison.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, model_validator

from latent_compass.benchmark.baselines import BASELINES, BaselineId
from latent_compass.benchmark.budget import BudgetGrant
from latent_compass.benchmark.corpus import CorpusManifest
from latent_compass.canonical import seal
from latent_compass.contracts import (
    SUPPORTED_BENCHMARK_VERSIONS,
    Identifier,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import (
    BaselineKind,
    MetricDirection,
    MetricFamily,
    Preregistration,
    Split,
)

__all__ = [
    "BENCHMARK_METRIC_DIRECTIONS",
    "BENCHMARK_METRIC_NAMES",
    "BaselineBinding",
    "BenchmarkSpec",
    "load_benchmark_spec",
    "require_spec_matches_manifest",
    "require_spec_matches_protocol",
]

SPEC_SEAL_DOMAIN: Final = "benchmark.spec"

#: The metric name this benchmark produces for each family. Fixed here, checked
#: against the pre-registration: a protocol that names its success metric
#: something else is a protocol this benchmark cannot fill in, and saying so is
#: better than filling in a metric the protocol did not ask for.
BENCHMARK_METRIC_NAMES: Final[dict[MetricFamily, str]] = {
    MetricFamily.SUCCESS: "ips-success",
    MetricFamily.VIOLATION: "ips-violations",
    MetricFamily.COST: "ips-cost",
    MetricFamily.INFORMATION: "ips-information-gain",
    MetricFamily.REVERSIBILITY: "ips-reversibility",
    MetricFamily.CALIBRATION: "ips-brier",
    MetricFamily.TAIL: "tail-weighted-cost-quantile",
    MetricFamily.DRIFT: "drift-max-success-gap",
}

#: The direction each family is scored in. Pre-registered, never inferred from
#: the numbers that come out.
BENCHMARK_METRIC_DIRECTIONS: Final[dict[MetricFamily, MetricDirection]] = {
    MetricFamily.SUCCESS: MetricDirection.HIGHER_IS_BETTER,
    MetricFamily.VIOLATION: MetricDirection.LOWER_IS_BETTER,
    MetricFamily.COST: MetricDirection.LOWER_IS_BETTER,
    MetricFamily.INFORMATION: MetricDirection.HIGHER_IS_BETTER,
    MetricFamily.REVERSIBILITY: MetricDirection.HIGHER_IS_BETTER,
    MetricFamily.CALIBRATION: MetricDirection.LOWER_IS_BETTER,
    MetricFamily.TAIL: MetricDirection.LOWER_IS_BETTER,
    MetricFamily.DRIFT: MetricDirection.LOWER_IS_BETTER,
}


class BaselineBinding(StrictModel):
    """One baseline, pinned to the version the run is allowed to use."""

    baseline_id: BaselineId
    algorithm_version: Identifier
    kind: BaselineKind


class BenchmarkSpec(StrictModel):
    """The sealed plan of one offline benchmark run."""

    contract_version: str = Field(min_length=5, max_length=20)
    benchmark_id: Identifier
    protocol_id: Identifier
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    corpus_id: Identifier
    corpus_version: Identifier
    executed_split: Split
    train_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    validation_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    holdout_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    seeds: tuple[int, ...] = Field(min_length=2, max_length=64)
    baselines: tuple[BaselineBinding, ...] = Field(min_length=4, max_length=4)
    budget: BudgetGrant
    minimum_propensity: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    tail_quantile: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _spec_invariants(self) -> BenchmarkSpec:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")

        # Refused here, in the model, so the refusal happens during validation —
        # before any corpus path exists, let alone is opened.
        if self.executed_split is not Split.VALIDATION:
            raise ValueError(
                f"HOK-188 executes {Split.VALIDATION.value} only; "
                f"{self.executed_split.value} is refused. HOK-190 owns the later "
                "pairwise-ranker evaluation and its final holdout comparison."
            )

        seals = (self.train_corpus_seal, self.validation_corpus_seal, self.holdout_corpus_seal)
        if len(set(seals)) != len(seals):
            raise ValueError("the three split corpus seals must be distinct")

        if list(self.seeds) != sorted(self.seeds) or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique and listed in ascending order")

        declared = [binding.baseline_id for binding in self.baselines]
        if len(set(declared)) != len(declared):
            raise ValueError("baseline identities must be unique")
        if set(declared) != set(BaselineId):
            missing = sorted(identity.value for identity in set(BaselineId) - set(declared))
            raise ValueError(f"the four pre-registered baselines are mandatory; missing {missing}")

        for binding in self.baselines:
            registered = BASELINES[binding.baseline_id]
            if binding.algorithm_version != registered.algorithm_version:
                raise ValueError(
                    f"baseline {binding.baseline_id.value!r} is pinned to "
                    f"{binding.algorithm_version!r} but this build implements "
                    f"{registered.algorithm_version!r}"
                )
            if binding.kind is not registered.kind:
                raise ValueError(
                    f"baseline {binding.baseline_id.value!r} is declared "
                    f"{binding.kind.value} but is registered {registered.kind.value}"
                )
        return self

    def spec_seal(self) -> str:
        """Seal over the whole plan. Any edit moves it."""
        return seal(SPEC_SEAL_DOMAIN, self.canonical_payload())

    def corpus_seal_for(self, split: Split) -> str:
        """The seal this spec binds to ``split``."""
        return {
            Split.TRAIN: self.train_corpus_seal,
            Split.VALIDATION: self.validation_corpus_seal,
            Split.HOLDOUT: self.holdout_corpus_seal,
        }[split]

    def binding(self, baseline_id: BaselineId) -> BaselineBinding:
        for candidate in self.baselines:
            if candidate.baseline_id is baseline_id:
                return candidate
        raise BenchmarkViolation(  # pragma: no cover - forbidden by the validator
            "baseline is not bound in this spec", detail={"baseline_id": baseline_id.value}
        )

    def ordered_baselines(self) -> tuple[BaselineBinding, ...]:
        """Bindings in canonical identity order, so run order is not a variable."""
        return tuple(sorted(self.baselines, key=lambda binding: binding.baseline_id.value))


def load_benchmark_spec(payload: object) -> BenchmarkSpec:
    """Validate an untrusted payload into a :class:`BenchmarkSpec`."""
    if not isinstance(payload, dict):
        raise BenchmarkViolation(
            "benchmark spec payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise BenchmarkViolation(
            "benchmark spec declares no contract_version",
            detail={"contract": "benchmark", "reason": "absent"},
        )
    return validate_contract(
        BenchmarkSpec, payload, error=BenchmarkViolation, context="benchmark spec"
    )


def require_spec_matches_protocol(spec: BenchmarkSpec, protocol: Preregistration) -> None:
    """Bind the run to the pre-registration that governs it.

    Refuses on identity, seal, epoch, seeds, corpus seals, the eight metric
    families and the four baseline names. Every one of these is a way for a run
    to be scored under rules other than the ones it was registered under.
    """
    problems: list[dict[str, object]] = []

    current_seal = protocol.protocol_seal()
    if spec.protocol_id != protocol.protocol_id:
        problems.append(
            {"field": "protocol_id", "spec": spec.protocol_id, "protocol": protocol.protocol_id}
        )
    if spec.protocol_seal != current_seal:
        problems.append(
            {
                "field": "protocol_seal",
                "spec": spec.protocol_seal,
                "protocol": current_seal,
                "meaning": "the protocol changed after this benchmark spec was written",
            }
        )
    if spec.epoch != protocol.epoch:
        problems.append({"field": "epoch", "spec": spec.epoch, "protocol": protocol.epoch})
    if tuple(spec.seeds) != tuple(protocol.seeds):
        problems.append(
            {"field": "seeds", "spec": list(spec.seeds), "protocol": list(protocol.seeds)}
        )

    for split in sorted(Split):
        declared = spec.corpus_seal_for(split)
        registered = protocol.split_spec(split).corpus_seal
        if declared != registered:
            problems.append(
                {
                    "field": "corpus_seal",
                    "split": split.value,
                    "spec": declared,
                    "protocol": registered,
                }
            )

    by_name = {metric.name: metric for metric in protocol.metrics}
    expected_metric_names = set(BENCHMARK_METRIC_NAMES.values())
    extra_metric_names = sorted(set(by_name) - expected_metric_names)
    if extra_metric_names:
        problems.append(
            {
                "field": "metrics",
                "reason": "the benchmark protocol must declare exactly the eight benchmark metrics",
                "extra": extra_metric_names,
            }
        )
    for family in sorted(MetricFamily):
        name = BENCHMARK_METRIC_NAMES[family]
        metric = by_name.get(name)
        if metric is None:
            problems.append(
                {
                    "field": "metric",
                    "family": family.value,
                    "expected_name": name,
                    "reason": "the pre-registration does not declare this metric",
                }
            )
            continue
        if metric.family is not family:
            problems.append(
                {
                    "field": "metric_family",
                    "metric": name,
                    "expected": family.value,
                    "protocol": metric.family.value,
                }
            )
        expected_direction = BENCHMARK_METRIC_DIRECTIONS[family]
        if metric.direction is not expected_direction:
            problems.append(
                {
                    "field": "metric_direction",
                    "metric": name,
                    "expected": expected_direction.value,
                    "protocol": metric.direction.value,
                }
            )

    declared_names = {baseline.name for baseline in protocol.baselines}
    expected_names = {identity.value for identity in BaselineId}
    if declared_names != expected_names:
        problems.append(
            {
                "field": "baselines",
                "expected": sorted(expected_names),
                "protocol": sorted(declared_names),
            }
        )
    else:
        for baseline in protocol.baselines:
            registered_kind = BASELINES[BaselineId(baseline.name)].kind
            if baseline.kind is not registered_kind:
                problems.append(
                    {
                        "field": "baseline_kind",
                        "baseline": baseline.name,
                        "expected": registered_kind.value,
                        "protocol": baseline.kind.value,
                    }
                )

    if problems:
        raise BenchmarkViolation(
            "the benchmark spec does not match its pre-registration", detail={"problems": problems}
        )


def require_spec_matches_manifest(spec: BenchmarkSpec, manifest: CorpusManifest) -> None:
    """Bind the run to the corpus identity the manifest establishes."""
    problems: list[dict[str, object]] = []
    if spec.corpus_id != manifest.corpus_id:
        problems.append(
            {"field": "corpus_id", "spec": spec.corpus_id, "manifest": manifest.corpus_id}
        )
    if spec.corpus_version != manifest.corpus_version:
        problems.append(
            {
                "field": "corpus_version",
                "spec": spec.corpus_version,
                "manifest": manifest.corpus_version,
            }
        )
    for split in sorted(Split):
        declared = spec.corpus_seal_for(split)
        entry = manifest.entry(split)
        if declared != entry.corpus_seal:
            problems.append(
                {
                    "field": "corpus_seal",
                    "split": split.value,
                    "spec": declared,
                    "manifest": entry.corpus_seal,
                }
            )
    if problems:
        raise BenchmarkViolation(
            "the benchmark spec does not match the corpus manifest",
            detail={"problems": problems},
        )

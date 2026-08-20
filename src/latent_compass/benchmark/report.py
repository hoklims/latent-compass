"""HOK-188 — the sealed benchmark report.

The report is the whole run, not a summary of it: every raw receipt, every
support diagnostic, every per-seed vector, and the diagnostic score each
baseline's vector produces under the HOK-181 rules. A reader who distrusts the
conclusion can rebuild it from the same document.

Determinism is structural
-------------------------
Nothing time-varying, host-varying or path-varying enters the sealed body. No
wall clock, no corpus directory, no temporary path, no iteration order: cases,
seeds and baselines are all carried in canonical order. Two runs of the same
spec against the same corpus therefore produce the same canonical payload and
the same ``report_seal``, on any host.

The grid is validated, not assumed
----------------------------------
:class:`BenchmarkReport` refuses unless every baseline carries **exactly** the
declared case set crossed with the declared seed set — no missing coordinate, no
extra one, no duplicate. A run that quietly skipped an unfavourable seed does
not validate, so it can never be sealed.

What this report is not
-----------------------
It is not evidence for the authority boundary. ``authority.py`` does not import
this module, and the measurement and verdict summaries carry an extra strict
``BENCHMARK_DIAGNOSTIC_ONLY`` scope. Passing either summary directly to HOK-181's
strict evidence loader is refused. This scope is a type-safety rail, not a
non-forgeable provenance attestation; claim-bearing authority remains blocked
until the persistent evidence contract gains a trusted external anchor.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from latent_compass.benchmark.baselines import BaselineId
from latent_compass.benchmark.budget import BudgetGrant, BudgetLedgerEntry
from latent_compass.benchmark.metrics import SeedEvaluation
from latent_compass.benchmark.ope import TrialRecord
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
    HoldoutPurpose,
    Measurement,
    MeasurementSet,
    MetricVerdict,
    Preregistration,
    Split,
    Verdict,
    load_measurement_set,
    score_measurements,
)
from latent_compass.vocabulary import ContinueKill

__all__ = [
    "UNSEALED",
    "BaselineReport",
    "BenchmarkMeasurementSet",
    "BenchmarkReport",
    "BenchmarkVerdict",
    "load_benchmark_report",
    "recompute_report_seal",
    "score_benchmark_measurements",
    "seal_report",
]

REPORT_SEAL_DOMAIN: Final = "benchmark.report"

#: The placeholder a report carries while its own seal is being computed. It is
#: never written to disk: :func:`seal_report` replaces it in the same expression
#: that computes the real value.
UNSEALED: Final = "sha256:" + "0" * 64
BENCHMARK_EVIDENCE_SCOPE: Final = "BENCHMARK_DIAGNOSTIC_ONLY"


class BenchmarkMeasurementSet(StrictModel):
    """A diagnostic metric vector that is rejected as direct authority evidence.

    The fields intentionally mirror the protocol measurement contract so the
    benchmark can rescore them deterministically. ``evidence_scope`` makes the
    serialized shape incompatible with HOK-181's strict ``MeasurementSet``;
    the authority boundary therefore refuses the report object directly. The
    marker is removable and deliberately makes no cryptographic provenance claim.
    """

    evidence_scope: Literal["BENCHMARK_DIAGNOSTIC_ONLY"] = BENCHMARK_EVIDENCE_SCOPE
    contract_version: str = Field(min_length=5, max_length=20)
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    purpose: HoldoutPurpose
    split: Split
    measurements: tuple[Measurement, ...] = Field(min_length=1, max_length=4096)


class BenchmarkVerdict(StrictModel):
    """The diagnostic score of one baseline, scoped out of lifecycle authority."""

    evidence_scope: Literal["BENCHMARK_DIAGNOSTIC_ONLY"] = BENCHMARK_EVIDENCE_SCOPE
    contract_version: str = Field(min_length=5, max_length=20)
    decision: ContinueKill
    protocol_id: Identifier
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    split: Split
    purpose: HoldoutPurpose
    metrics: tuple[MetricVerdict, ...] = Field(min_length=1)
    failing_metrics: tuple[Identifier, ...] = Field(default=())
    verdict_seal: Annotated[str, Field(min_length=1, max_length=200)]


def _protocol_measurements(measurements: BenchmarkMeasurementSet) -> MeasurementSet:
    payload = measurements.canonical_payload()
    payload.pop("evidence_scope")
    return load_measurement_set(payload)


def _benchmark_verdict(verdict: Verdict) -> BenchmarkVerdict:
    return validate_contract(
        BenchmarkVerdict,
        {"evidence_scope": BENCHMARK_EVIDENCE_SCOPE, **verdict.canonical_payload()},
        error=BenchmarkViolation,
        context="benchmark verdict",
    )


def score_benchmark_measurements(
    protocol: Preregistration, measurements: BenchmarkMeasurementSet
) -> BenchmarkVerdict:
    """Rescore a diagnostic vector while preserving its non-authoritative scope."""
    return _benchmark_verdict(score_measurements(protocol, _protocol_measurements(measurements)))


class BaselineReport(StrictModel):
    """One baseline's complete result on the executed split."""

    baseline_id: BaselineId
    algorithm_version: Identifier
    kind: BaselineKind
    trials: tuple[TrialRecord, ...] = Field(min_length=1, max_length=262144)
    seed_evaluations: tuple[SeedEvaluation, ...] = Field(min_length=2, max_length=64)
    measurement_set: BenchmarkMeasurementSet
    verdict: BenchmarkVerdict

    @model_validator(mode="after")
    def _baseline_report_is_internally_consistent(self) -> BaselineReport:
        for trial in self.trials:
            if trial.baseline_id != self.baseline_id.value:
                raise ValueError(
                    f"a {trial.baseline_id!r} trial appears in the "
                    f"{self.baseline_id.value!r} report"
                )
            if trial.algorithm_version != self.algorithm_version:
                raise ValueError("a trial carries a different algorithm version")

        seeds = [evaluation.seed for evaluation in self.seed_evaluations]
        if seeds != sorted(seeds) or len(set(seeds)) != len(seeds):
            raise ValueError("seed evaluations must be unique and ascending")

        # The measurement set is what HOK-181 scores. If it disagrees with the
        # per-seed vectors carried right beside it, one of the two is decorative.
        expected = {
            (value.metric, evaluation.seed): value.value
            for evaluation in self.seed_evaluations
            for value in evaluation.metrics
        }
        supplied = {
            (measurement.metric, measurement.seed): measurement.value
            for measurement in self.measurement_set.measurements
        }
        if supplied != expected:
            raise ValueError(
                "the measurement set does not reproduce from the per-seed metric vectors"
            )

        if self.measurement_set.split is not Split.VALIDATION:
            raise ValueError("a HOK-188 measurement set is on VALIDATION")
        if self.measurement_set.purpose is not HoldoutPurpose.SELECTION:
            raise ValueError("a HOK-188 measurement set is scored for SELECTION")
        if self.verdict.split is not Split.VALIDATION:
            raise ValueError("a HOK-188 verdict is on VALIDATION")
        if self.verdict.corpus_seal != self.measurement_set.corpus_seal:
            raise ValueError("the verdict and its measurements disagree about the corpus")
        return self

    def grid(self) -> set[tuple[str, int]]:
        """The ``(case_id, seed)`` coordinates this baseline actually covered."""
        return {(trial.case_id, trial.seed) for trial in self.trials}


class BenchmarkReport(StrictModel):
    """The sealed record of one offline benchmark run."""

    contract_version: str = Field(min_length=5, max_length=20)
    benchmark_id: Identifier
    spec_seal: Annotated[str, Field(min_length=1, max_length=200)]
    baseline_registry_seal: Annotated[str, Field(min_length=1, max_length=200)]
    protocol_id: Identifier
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    executed_split: Split
    holdout_executed: bool
    corpus_id: Identifier
    corpus_version: Identifier
    train_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    validation_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    holdout_corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    budget: BudgetGrant
    budget_ledger: tuple[BudgetLedgerEntry, ...] = Field(min_length=4, max_length=4)
    seeds: tuple[int, ...] = Field(min_length=2, max_length=64)
    case_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    baselines: tuple[BaselineReport, ...] = Field(min_length=4, max_length=4)
    report_seal: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _report_invariants(self) -> BenchmarkReport:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")

        if self.executed_split is not Split.VALIDATION:
            raise ValueError("HOK-188 executes VALIDATION only")
        if self.holdout_executed:
            raise ValueError("a HOK-188 report may not claim the holdout was executed")

        if list(self.seeds) != sorted(self.seeds) or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique and ascending")
        if list(self.case_ids) != sorted(self.case_ids) or len(set(self.case_ids)) != len(
            self.case_ids
        ):
            raise ValueError("case ids must be unique and ascending")

        identities = [report.baseline_id for report in self.baselines]
        if set(identities) != set(BaselineId):
            raise ValueError("the report must cover exactly the four pre-registered baselines")
        if identities != sorted(identities, key=lambda identity: identity.value):
            raise ValueError("baseline reports must be in canonical identity order")

        expected_grid = {(case_id, seed) for case_id in self.case_ids for seed in self.seeds}
        for report in self.baselines:
            trials = [(trial.case_id, trial.seed) for trial in report.trials]
            if len(trials) != len(set(trials)):
                duplicates = sorted({key for key in trials if trials.count(key) > 1})
                raise ValueError(
                    f"baseline {report.baseline_id.value!r} repeats grid coordinates: {duplicates}"
                )
            covered = set(trials)
            missing = sorted(expected_grid - covered)
            extra = sorted(covered - expected_grid)
            if missing or extra:
                raise ValueError(
                    f"baseline {report.baseline_id.value!r} does not cover the declared grid; "
                    f"missing {missing}, extra {extra}"
                )
            if [evaluation.seed for evaluation in report.seed_evaluations] != list(self.seeds):
                raise ValueError(
                    f"baseline {report.baseline_id.value!r} does not evaluate every declared seed"
                )
            if report.measurement_set.corpus_seal != self.validation_corpus_seal:
                raise ValueError(
                    f"baseline {report.baseline_id.value!r} measured a different corpus"
                )
            if report.measurement_set.protocol_seal != self.protocol_seal:
                raise ValueError(
                    f"baseline {report.baseline_id.value!r} was scored under a different protocol"
                )

        ledger_ids = [entry.baseline_id for entry in self.budget_ledger]
        if sorted(ledger_ids) != sorted(identity.value for identity in BaselineId):
            raise ValueError("the budget ledger must cover exactly the four baselines")
        if ledger_ids != sorted(ledger_ids):
            raise ValueError("budget ledger entries must be in canonical identity order")
        for entry in self.budget_ledger:
            if entry.max_candidate_inspections_on_a_case > (
                self.budget.max_candidate_inspections_per_case
            ):
                raise ValueError(
                    f"baseline {entry.baseline_id!r} exceeded the common inspection cap"
                )
            if entry.max_random_draws_on_a_case > self.budget.max_random_draws_per_case:
                raise ValueError(f"baseline {entry.baseline_id!r} exceeded the common draw cap")
        return self

    def sealed_body(self) -> dict[str, object]:
        """Everything the seal covers: the report minus the seal itself."""
        payload = self.canonical_payload()
        payload.pop("report_seal", None)
        return payload


def recompute_report_seal(report: BenchmarkReport) -> str:
    """Recompute a report's seal from its own contents.

    This detects an edit after sealing. It does **not** prove the report was
    produced by running anything — a forged report sealed consistently passes
    here and fails :func:`~latent_compass.benchmark.runner.verify_report`, which
    re-executes the four baselines and compares the whole payload.
    """
    return seal(REPORT_SEAL_DOMAIN, report.sealed_body())


def seal_report(report: BenchmarkReport) -> BenchmarkReport:
    """Return ``report`` carrying its own recomputed seal."""
    return load_benchmark_report(
        report.model_copy(update={"report_seal": recompute_report_seal(report)}).canonical_payload()
    )


def load_benchmark_report(payload: object) -> BenchmarkReport:
    """Validate an untrusted payload into a :class:`BenchmarkReport`."""
    if not isinstance(payload, dict):
        raise BenchmarkViolation(
            "benchmark report payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise BenchmarkViolation(
            "benchmark report declares no contract_version",
            detail={"contract": "benchmark", "reason": "absent"},
        )
    return validate_contract(
        BenchmarkReport, payload, error=BenchmarkViolation, context="benchmark report"
    )

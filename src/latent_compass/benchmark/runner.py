"""HOK-188 — the offline benchmark runner and its verifier.

Runs the four pre-registered baselines over the ``VALIDATION`` split of a sealed
corpus, under one common budget, and produces a sealed vector report. It trains
nothing, fits nothing, tunes nothing and adapts nothing: a baseline is a pure
function and the run is a nested loop over a fixed grid.

The holdout is never opened here
--------------------------------
The runner resolves and reads exactly one path — the ``VALIDATION`` entry of the
manifest. The holdout's seal reaches the report from the manifest, which was
established by the separate corpus-authoring path. Three independent guards
stand in front of a holdout read, and the first two fire before a filesystem
path has even been constructed:

1. :class:`~latent_compass.benchmark.spec.BenchmarkSpec` refuses any
   ``executed_split`` other than ``VALIDATION`` during model validation;
2. :func:`run_benchmark` re-checks the split before resolving any path;
3. only ``manifest.entry(Split.VALIDATION)`` is ever resolved.

There is no holdout-ledger option on this path, and there is nothing here that
could consume one. HOK-190 owns the later ranker evaluation and its final
validation-then-holdout comparison.
"""

from __future__ import annotations

from pathlib import Path

from latent_compass.benchmark.baselines import (
    BASELINES,
    BaselineId,
    SelectionContext,
    baseline_registry_seal,
    select,
)
from latent_compass.benchmark.budget import (
    BudgetLedgerEntry,
    CaseBudgetMeter,
    require_case_count_within_budget,
)
from latent_compass.benchmark.corpus import (
    BenchmarkCase,
    CorpusManifest,
    read_split_file,
    require_manifest_matches_protocol,
    split_corpus_seal,
)
from latent_compass.benchmark.metrics import SeedEvaluation, evaluate_seed
from latent_compass.benchmark.ope import (
    CaseEligibility,
    TrialRecord,
    classify,
    trial_binding_seal,
)
from latent_compass.benchmark.report import (
    UNSEALED,
    BaselineReport,
    BenchmarkMeasurementSet,
    BenchmarkReport,
    recompute_report_seal,
    score_benchmark_measurements,
    seal_report,
)
from latent_compass.benchmark.spec import (
    BenchmarkSpec,
    require_spec_matches_manifest,
    require_spec_matches_protocol,
)
from latent_compass.contracts import BENCHMARK_CONTRACT_VERSION, PROTOCOL_CONTRACT_VERSION
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import (
    HoldoutPurpose,
    Measurement,
    Preregistration,
    Split,
)

__all__ = ["run_benchmark", "verify_report"]


def _load_validation_cases(
    spec: BenchmarkSpec, manifest: CorpusManifest, corpus_dir: Path
) -> tuple[BenchmarkCase, ...]:
    """Resolve and read the one split this benchmark is allowed to execute."""
    if spec.executed_split is not Split.VALIDATION:  # pragma: no cover - refused by the spec model
        raise BenchmarkViolation(
            "HOK-188 executes VALIDATION only; refusing before resolving any corpus path",
            detail={"requested": spec.executed_split.value},
        )

    entry = manifest.entry(Split.VALIDATION)
    case_file = read_split_file(entry.resolve(corpus_dir), expected_split=Split.VALIDATION)
    cases = case_file.ordered_cases()

    # The manifest's seal is a claim about the file. Recomputing it here is what
    # turns "the corpus was not edited since the manifest was built" from an
    # assumption into a check, and it costs one pass over data already in memory.
    recomputed = split_corpus_seal(cases)
    if recomputed != spec.validation_corpus_seal:
        raise BenchmarkViolation(
            "the validation corpus does not seal to the value the spec was written against",
            detail={
                "expected": spec.validation_corpus_seal,
                "recomputed": recomputed,
                "meaning": "the corpus changed after this benchmark spec was written",
            },
        )
    return cases


def _run_one_baseline(
    baseline_id: BaselineId,
    *,
    spec: BenchmarkSpec,
    protocol: Preregistration,
    cases: tuple[BenchmarkCase, ...],
    context: SelectionContext,
) -> tuple[BaselineReport, BudgetLedgerEntry]:
    binding = spec.binding(baseline_id)
    algorithm = BASELINES[baseline_id]
    by_id = {case.case_id: case for case in cases}

    trials: list[TrialRecord] = []
    for seed in spec.seeds:
        for case in cases:
            meter = CaseBudgetMeter(
                spec.budget, baseline_id=baseline_id.value, case_id=case.case_id, seed=seed
            )
            choice = select(
                baseline_id, case.public_view(), seed=seed, context=context, meter=meter
            )
            logged_direction = case.logged_direction_id()
            logged_propensity = case.logged_propensity(logged_direction)
            eligibility = classify(
                case,
                target_direction_id=choice.direction_id,
                minimum_propensity=spec.minimum_propensity,
            )
            agrees = choice.direction_id == logged_direction
            weight = (
                (1.0 / logged_propensity)
                if (eligibility is CaseEligibility.ELIGIBLE and agrees)
                else 0.0
            )
            trials.append(
                TrialRecord(
                    baseline_id=baseline_id.value,
                    algorithm_version=algorithm.algorithm_version,
                    case_id=case.case_id,
                    seed=seed,
                    distribution_group=case.distribution_group,
                    selected_direction_id=choice.direction_id,
                    confidence=choice.confidence,
                    budget=meter.receipt(),
                    eligibility=eligibility,
                    logged_direction_id=logged_direction,
                    logged_propensity=logged_propensity,
                    logging_propensity_of_target_action=case.logged_propensity(choice.direction_id),
                    weight=weight,
                    binding_seal=trial_binding_seal(
                        spec_seal=context.spec_seal,
                        corpus_seal=context.corpus_seal,
                        baseline_id=baseline_id.value,
                        algorithm_version=algorithm.algorithm_version,
                        case_id=case.case_id,
                        seed=seed,
                        selected_direction_id=choice.direction_id,
                    ),
                )
            )

    unsupported = sorted(
        {
            (trial.case_id, trial.seed)
            for trial in trials
            if trial.eligibility is CaseEligibility.OUT_OF_SUPPORT
        }
    )
    if unsupported:
        raise BenchmarkViolation(
            "the baseline lacks common support on the fixed analysis population",
            detail={
                "baseline_id": baseline_id.value,
                "minimum_propensity": spec.minimum_propensity,
                "unsupported_case_seed_coordinates": unsupported,
                "meaning": "a successful report must compare every baseline on one population",
            },
        )

    evaluations: list[SeedEvaluation] = []
    for seed in spec.seeds:
        seed_trials = tuple(trial for trial in trials if trial.seed == seed)
        evaluations.append(evaluate_seed(seed=seed, records=seed_trials, cases=by_id, spec=spec))

    measurement_set = BenchmarkMeasurementSet(
        contract_version=PROTOCOL_CONTRACT_VERSION,
        protocol_seal=protocol.protocol_seal(),
        corpus_seal=spec.validation_corpus_seal,
        epoch=protocol.epoch,
        purpose=HoldoutPurpose.SELECTION,
        split=Split.VALIDATION,
        measurements=tuple(
            Measurement(
                metric=value.metric, split=Split.VALIDATION, seed=evaluation.seed, value=value.value
            )
            for evaluation in evaluations
            for value in evaluation.metrics
        ),
    )
    verdict = score_benchmark_measurements(protocol, measurement_set)

    ledger = BudgetLedgerEntry(
        baseline_id=baseline_id.value,
        total_candidate_inspections=sum(trial.budget.candidate_inspections for trial in trials),
        total_random_draws=sum(trial.budget.random_draws for trial in trials),
        max_candidate_inspections_on_a_case=max(
            trial.budget.candidate_inspections for trial in trials
        ),
        max_random_draws_on_a_case=max(trial.budget.random_draws for trial in trials),
    )

    report = BaselineReport(
        baseline_id=baseline_id,
        algorithm_version=algorithm.algorithm_version,
        kind=binding.kind,
        trials=tuple(sorted(trials, key=lambda trial: (trial.case_id, trial.seed))),
        seed_evaluations=tuple(evaluations),
        measurement_set=measurement_set,
        verdict=verdict,
    )
    return report, ledger


def run_benchmark(
    *,
    spec: BenchmarkSpec,
    protocol: Preregistration,
    manifest: CorpusManifest,
    corpus_dir: Path,
) -> BenchmarkReport:
    """Execute the four baselines on ``VALIDATION`` and seal the result.

    Every binding is checked before any corpus file is opened: spec against
    pre-registration, spec against manifest, manifest against pre-registration.
    A run that would have been scored under rules it was not registered under
    refuses instead, and a refused run produces no report at all — there is no
    partial artefact to be mistaken for a completed one, because the report is
    constructed once, at the end, from values that all already exist.
    """
    require_spec_matches_protocol(spec, protocol)
    require_spec_matches_manifest(spec, manifest)
    require_manifest_matches_protocol(manifest, protocol)

    cases = _load_validation_cases(spec, manifest, corpus_dir)
    require_case_count_within_budget(spec.budget, len(cases), split=Split.VALIDATION.value)

    context = SelectionContext(spec_seal=spec.spec_seal(), corpus_seal=spec.validation_corpus_seal)

    baseline_reports = []
    ledger_entries = []
    for binding in spec.ordered_baselines():
        report, ledger = _run_one_baseline(
            binding.baseline_id, spec=spec, protocol=protocol, cases=cases, context=context
        )
        baseline_reports.append(report)
        ledger_entries.append(ledger)

    unsealed = BenchmarkReport(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        benchmark_id=spec.benchmark_id,
        spec_seal=context.spec_seal,
        baseline_registry_seal=baseline_registry_seal(),
        protocol_id=protocol.protocol_id,
        protocol_seal=protocol.protocol_seal(),
        epoch=protocol.epoch,
        executed_split=Split.VALIDATION,
        holdout_executed=False,
        corpus_id=spec.corpus_id,
        corpus_version=spec.corpus_version,
        train_corpus_seal=spec.train_corpus_seal,
        validation_corpus_seal=spec.validation_corpus_seal,
        holdout_corpus_seal=spec.holdout_corpus_seal,
        budget=spec.budget,
        budget_ledger=tuple(sorted(ledger_entries, key=lambda entry: entry.baseline_id)),
        seeds=tuple(spec.seeds),
        case_ids=tuple(sorted(case.case_id for case in cases)),
        baselines=tuple(baseline_reports),
        report_seal=UNSEALED,
    )
    return seal_report(unsealed)


def _summarise_difference(expected: dict[str, object], received: dict[str, object]) -> list[str]:
    """Name the top-level fields that differ, so a mismatch is locatable."""
    keys = sorted(set(expected) | set(received))
    return [key for key in keys if expected.get(key) != received.get(key)]


def verify_report(
    report: BenchmarkReport,
    *,
    spec: BenchmarkSpec,
    protocol: Preregistration,
    manifest: CorpusManifest,
    corpus_dir: Path,
) -> dict[str, object]:
    """Re-derive the report from the raw artefacts and compare it whole.

    Recomputing the carried seal is necessary but not sufficient: a forged
    report sealed consistently passes that check. So the four baselines are
    re-executed against the real corpus and the **entire** canonical payload is
    compared, field by field, with the document under test.
    """
    carried = report.report_seal
    recomputed_seal = recompute_report_seal(report)
    if carried != recomputed_seal:
        raise BenchmarkViolation(
            "the report seal does not reproduce from the report's own contents",
            detail={"carried": carried, "recomputed": recomputed_seal},
        )

    if report.spec_seal != spec.spec_seal():
        raise BenchmarkViolation(
            "the report was produced under a different benchmark spec",
            detail={"report": report.spec_seal, "spec": spec.spec_seal()},
        )

    rerun = run_benchmark(spec=spec, protocol=protocol, manifest=manifest, corpus_dir=corpus_dir)
    if rerun.sealed_body() != report.sealed_body():
        raise BenchmarkViolation(
            "the report does not reproduce from the artefacts it names",
            detail={
                "differing_fields": _summarise_difference(
                    rerun.sealed_body(), report.sealed_body()
                ),
                "expected_report_seal": rerun.report_seal,
                "received_report_seal": carried,
            },
        )
    return {
        "verified": True,
        "benchmark_id": report.benchmark_id,
        "report_seal": carried,
        "spec_seal": report.spec_seal,
        "validation_corpus_seal": report.validation_corpus_seal,
        "baselines": [item.baseline_id.value for item in report.baselines],
        "reexecuted": True,
    }

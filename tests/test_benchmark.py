"""HOK-188 — the offline benchmark contracts, baselines, estimator and report.

The adversarial invariants from the tranche risk packet live in
``test_benchmark_risk_packet.py``. This module covers the contracts those
invariants rest on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import BENCHMARK_CORPUS, read_corpus_json
from latent_compass.benchmark import (
    BASELINES,
    BENCHMARK_METRIC_NAMES,
    BaselineId,
    CaseEligibility,
    OutcomeChannel,
    PublicCaseView,
    SelectionContext,
    load_benchmark_spec,
    load_case_file,
    load_corpus_manifest,
    run_benchmark,
    select,
    split_corpus_seal,
    verify_manifest,
    verify_report,
)
from latent_compass.benchmark.budget import BudgetGrant, CaseBudgetMeter
from latent_compass.benchmark.corpus import BenchmarkCase, build_manifest
from latent_compass.benchmark.ope import (
    estimate_channel,
    quantile,
    reward,
    summarise_support,
    weighted_quantile,
)
from latent_compass.benchmark.report import load_benchmark_report, recompute_report_seal
from latent_compass.benchmark.spec import require_spec_matches_protocol
from latent_compass.contracts import BENCHMARK_CONTRACT_VERSION
from latent_compass.errors import BenchmarkViolation, BudgetExceeded, UnsupportedContractVersion
from latent_compass.protocol import BaselineKind, HoldoutPurpose, MetricFamily, Split
from latent_compass.vocabulary import ContinueKill

DISCRIMINATING_VIEW: dict[str, Any] = {
    "case_id": "case-discriminating",
    "state": {
        "levels": [{"depth": 0, "name": "L4-invariants", "summary_digest": "digest-d"}],
        "branch_point": {"node_id": "node-discriminating", "depth": 0, "parent_node_id": None},
    },
    "candidates": [
        {"direction_id": "dir-a", "propensity": 0.25, "prior_uncertainty": 0.4},
        {"direction_id": "dir-b", "propensity": 0.125, "prior_uncertainty": 0.05},
        {"direction_id": "dir-c", "propensity": 0.125, "prior_uncertainty": 0.3},
        {"direction_id": "dir-d", "propensity": 0.5, "prior_uncertainty": 0.2},
    ],
}

#: At this seed the four baselines select four different directions on the view
#: above. Pinned, not searched for at runtime: a test that hunts for a
#: discriminating seed would keep passing after an algorithm silently changed.
DISCRIMINATING_SEED = 1

GRANT = BudgetGrant(max_cases=64, max_candidate_inspections_per_case=4, max_random_draws_per_case=1)
CONTEXT = SelectionContext(spec_seal="sha256:spec-fixture", corpus_seal="sha256:corpus-fixture")


def discriminating_view() -> PublicCaseView:
    return PublicCaseView.model_validate_json(json.dumps(DISCRIMINATING_VIEW))


def run_baseline(baseline_id: BaselineId, view: PublicCaseView, seed: int) -> Any:
    meter = CaseBudgetMeter(GRANT, baseline_id=baseline_id.value, case_id=view.case_id, seed=seed)
    return select(baseline_id, view, seed=seed, context=CONTEXT, meter=meter), meter.receipt()


# --------------------------------------------------------------------------- #
# The corpus contract
# --------------------------------------------------------------------------- #


def test_the_committed_corpus_manifest_reproduces_from_the_real_files(
    benchmark_corpus_dir: Path, benchmark_manifest: Any
) -> None:
    assert verify_manifest(benchmark_corpus_dir, benchmark_manifest)["verified"] is True


def test_a_case_with_no_logged_direction_is_refused(benchmark_corpus_dir: Path) -> None:
    """Off-policy evaluation is undefined without a logged action."""
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    case = payload["cases"][0]
    case["episode"]["decision"] = {
        "advisory": {
            "contract_version": "1.0.0",
            "kind": "ABSTAIN",
            "confidence": 0.4,
            "uncertainty": 0.5,
            "rationale": "the logging policy abstained",
            "issued_by": "latent_compass",
        }
    }
    with pytest.raises(BenchmarkViolation) as refusal:
        load_case_file(payload)
    assert "logged direction" in json.dumps(refusal.value.as_dict())


def test_a_nominal_case_may_not_sit_in_a_shift_group(benchmark_corpus_dir: Path) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    payload["cases"][0]["distribution_group"] = "shift.invented"
    with pytest.raises(BenchmarkViolation):
        load_case_file(payload)


def test_a_case_file_holding_another_splits_case_is_refused(benchmark_corpus_dir: Path) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    payload["cases"][0]["split"] = "HOLDOUT"
    with pytest.raises(BenchmarkViolation):
        load_case_file(payload)


def test_a_corpus_contract_version_is_declared_never_defaulted(
    benchmark_corpus_dir: Path,
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    del payload["contract_version"]
    with pytest.raises(BenchmarkViolation) as refusal:
        load_case_file(payload)
    assert refusal.value.as_dict()["detail"] == {"contract": "benchmark", "reason": "absent"}


def test_a_future_corpus_contract_version_is_refused_as_future(
    benchmark_corpus_dir: Path,
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    payload["contract_version"] = "9.0.0"
    with pytest.raises(UnsupportedContractVersion) as refusal:
        load_case_file(payload)
    assert refusal.value.detail == {
        "contract": "benchmark",
        "version": "9.0.0",
        "reason": "future",
        "supported": [BENCHMARK_CONTRACT_VERSION],
    }


def test_a_split_path_may_not_escape_the_corpus_directory(
    benchmark_corpus_dir: Path, mutable_corpus: Path
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "manifest.json")
    for entry in payload["splits"]:
        if entry["split"] == "VALIDATION":
            entry["path"] = "../elsewhere.json"
    with pytest.raises(BenchmarkViolation):
        load_corpus_manifest(payload)
    assert not (mutable_corpus.parent / "elsewhere.json").exists()


def test_a_validation_split_with_no_shift_group_is_refused(tmp_path: Path) -> None:
    """DRIFT measured against nothing is not a small drift."""
    source = read_corpus_json(BENCHMARK_CORPUS, "validation.json")
    for case in source["cases"]:
        case["distribution_kind"] = "NOMINAL"
        case["distribution_group"] = "nominal"
    _write_split_bundle(tmp_path, validation=source)
    with pytest.raises(BenchmarkViolation) as refusal:
        build_manifest(
            tmp_path,
            corpus_id="lc-shiftless",
            corpus_version="v1.0.0",
            provenance=_provenance(),
            relative_paths={
                Split.TRAIN: "train.json",
                Split.VALIDATION: "validation.json",
                Split.HOLDOUT: "holdout.json",
            },
        )
    assert "no shift group" in refusal.value.message


# --------------------------------------------------------------------------- #
# The public view is the whole boundary
# --------------------------------------------------------------------------- #


def test_the_public_view_carries_no_label_field(benchmark_corpus_dir: Path) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    case = load_case_file(payload).ordered_cases()[0]
    view = case.public_view().canonical_payload()

    assert set(view) == {"case_id", "state", "candidates"}
    serialised = json.dumps(view)
    for forbidden in (
        "selected_direction_id",
        "outcome",
        "economics",
        "external_verdict",
        "observed",
        "success",
        "violations",
        "reversibility",
        "information_gain",
    ):
        assert forbidden not in serialised, f"the public view leaks {forbidden!r}"


def test_changing_a_label_does_not_change_the_public_view(
    benchmark_corpus_dir: Path,
) -> None:
    """The projection is built from three sources, not filtered from the episode."""
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    before = load_case_file(payload).ordered_cases()[0].public_view().canonical_payload()

    first = payload["cases"][0]
    first["episode"]["outcome"]["success"] = not first["episode"]["outcome"]["success"]
    first["episode"]["economics"]["cost"] = 999.0
    after = load_case_file(payload).ordered_cases()[0].public_view().canonical_payload()

    assert before == after


# --------------------------------------------------------------------------- #
# The four baselines
# --------------------------------------------------------------------------- #


def test_the_registry_holds_exactly_the_four_pre_registered_identities() -> None:
    assert set(BASELINES) == set(BaselineId)
    assert {BASELINES[identity].kind for identity in BASELINES} == {
        BaselineKind.TRIVIAL,
        BaselineKind.STRONG,
    }


@pytest.mark.parametrize(
    ("baseline_id", "expected_direction", "expected_confidence"),
    [
        (BaselineId.FIXED_CANONICAL, "dir-a", 0.25),
        (BaselineId.LEAST_UNCERTAINTY, "dir-b", 0.95),
        (BaselineId.SEEDED_UNIFORM, "dir-c", 0.25),
        (BaselineId.LOGGED_PROPENSITY_ARBITER, "dir-d", 0.5),
    ],
)
def test_each_baseline_produces_its_pinned_output_on_the_discriminating_case(
    baseline_id: BaselineId, expected_direction: str, expected_confidence: float
) -> None:
    choice, _ = run_baseline(baseline_id, discriminating_view(), DISCRIMINATING_SEED)
    assert choice.direction_id == expected_direction
    assert choice.confidence == pytest.approx(expected_confidence)


def test_the_seeded_baseline_does_not_depend_on_execution_order() -> None:
    """The draw is derived from the case, not from a running random state."""
    view = discriminating_view()
    direct, _ = run_baseline(BaselineId.SEEDED_UNIFORM, view, DISCRIMINATING_SEED)

    for _ in range(5):
        run_baseline(BaselineId.SEEDED_UNIFORM, view, DISCRIMINATING_SEED + 100)
        run_baseline(BaselineId.FIXED_CANONICAL, view, DISCRIMINATING_SEED)
    after_noise, _ = run_baseline(BaselineId.SEEDED_UNIFORM, view, DISCRIMINATING_SEED)

    assert after_noise.direction_id == direct.direction_id


def test_the_least_uncertainty_tie_break_is_canonical() -> None:
    payload = json.loads(json.dumps(DISCRIMINATING_VIEW))
    for candidate in payload["candidates"]:
        candidate["prior_uncertainty"] = 0.2
    view = PublicCaseView.model_validate_json(json.dumps(payload))
    choice, _ = run_baseline(BaselineId.LEAST_UNCERTAINTY, view, DISCRIMINATING_SEED)
    assert choice.direction_id == "dir-a"


def test_the_propensity_arbiter_breaks_a_propensity_tie_on_uncertainty() -> None:
    payload = json.loads(json.dumps(DISCRIMINATING_VIEW))
    payload["candidates"] = [
        {"direction_id": "dir-a", "propensity": 0.5, "prior_uncertainty": 0.4},
        {"direction_id": "dir-b", "propensity": 0.5, "prior_uncertainty": 0.1},
    ]
    view = PublicCaseView.model_validate_json(json.dumps(payload))
    choice, _ = run_baseline(BaselineId.LOGGED_PROPENSITY_ARBITER, view, DISCRIMINATING_SEED)
    assert choice.direction_id == "dir-b"


def test_only_the_seeded_baseline_spends_a_random_draw() -> None:
    for baseline_id in BaselineId:
        _, receipt = run_baseline(baseline_id, discriminating_view(), DISCRIMINATING_SEED)
        expected = 1 if baseline_id is BaselineId.SEEDED_UNIFORM else 0
        assert receipt.random_draws == expected
        assert receipt.candidate_inspections == 4


# --------------------------------------------------------------------------- #
# Off-policy evaluation
# --------------------------------------------------------------------------- #


def test_snips_is_reported_undefined_rather_than_zero_when_no_weight_is_non_zero() -> None:
    estimate = estimate_channel(OutcomeChannel.SUCCESS, [(0.0, 1.0), (0.0, 0.0)])
    assert estimate.ips == 0.0
    assert estimate.snips is None
    assert estimate.snips_defined is False


def test_ips_and_snips_reproduce_from_the_pairs() -> None:
    estimate = estimate_channel(OutcomeChannel.SUCCESS, [(2.0, 1.0), (0.0, 0.0), (4.0, 0.5)])
    assert estimate.ips == pytest.approx((2.0 * 1.0 + 0.0 + 4.0 * 0.5) / 3)
    assert estimate.snips == pytest.approx((2.0 + 2.0) / 6.0)


def test_trial_names_the_logging_propensity_of_the_target_action(
    benchmark_report: Any,
) -> None:
    trial = benchmark_report.baselines[0].trials[0]
    payload = trial.canonical_payload()
    assert "logging_propensity_of_target_action" in payload
    assert "target_propensity" not in payload


def test_an_estimate_over_no_eligible_case_refuses() -> None:
    with pytest.raises(BenchmarkViolation):
        estimate_channel(OutcomeChannel.COST, [])


def test_the_quantile_is_nearest_rank_and_invents_no_value() -> None:
    sample = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert quantile(sample, 0.9) == 10.0
    assert quantile(sample, 0.5) == 3.0
    assert quantile(sample, 0.01) == 1.0
    with pytest.raises(BenchmarkViolation):
        quantile([], 0.5)


def test_the_weighted_cost_quantile_preserves_units_and_is_scale_invariant() -> None:
    sample = [(2.0, 1.0), (2.0, 10.0)]
    assert weighted_quantile(sample, 0.9) == 10.0
    assert weighted_quantile([(20.0, 1.0), (20.0, 10.0)], 0.9) == 10.0
    with pytest.raises(BenchmarkViolation, match="positive importance mass"):
        weighted_quantile([(0.0, 1.0), (0.0, 10.0)], 0.9)


def test_support_diagnostics_refuse_unless_every_case_is_accounted_for(
    benchmark_report: Any,
) -> None:
    diagnostics = benchmark_report.baselines[0].seed_evaluations[0].diagnostics
    payload = diagnostics.canonical_payload()
    payload["eligible_cases"] = payload["eligible_cases"] - 1
    with pytest.raises(ValueError, match="account for every case"):
        type(diagnostics).model_validate_json(json.dumps(payload))


def test_every_eligibility_state_occurs_in_the_committed_corpus(
    benchmark_report: Any,
) -> None:
    """Successful reports exercise every corpus-derived eligibility state."""
    observed = {
        trial.eligibility for baseline in benchmark_report.baselines for trial in baseline.trials
    }
    assert observed == set(CaseEligibility) - {CaseEligibility.OUT_OF_SUPPORT}


def test_an_immature_outcome_is_named_not_dropped(benchmark_report: Any) -> None:
    baseline = benchmark_report.baselines[0]
    censored = {
        trial.case_id
        for trial in baseline.trials
        if trial.eligibility is CaseEligibility.CENSORED_IMMATURE
    }
    assert censored, "no case exercises the observation cutoff"
    for evaluation in baseline.seed_evaluations:
        diagnostics = evaluation.diagnostics
        assert diagnostics.censored_immature_cases == len(censored)
        assert diagnostics.total_cases == len(benchmark_report.case_ids)


def test_the_effective_sample_size_recomputes_from_the_receipts(
    benchmark_report: Any,
) -> None:
    for baseline in benchmark_report.baselines:
        for evaluation in baseline.seed_evaluations:
            trials = tuple(trial for trial in baseline.trials if trial.seed == evaluation.seed)
            assert summarise_support(trials).canonical_payload() == (
                evaluation.diagnostics.canonical_payload()
            )


# --------------------------------------------------------------------------- #
# Metrics and the HOK-181 hand-off
# --------------------------------------------------------------------------- #


def test_the_vector_carries_all_eight_families_and_no_composite(
    benchmark_report: Any,
) -> None:
    for baseline in benchmark_report.baselines:
        for evaluation in baseline.seed_evaluations:
            families = [value.family for value in evaluation.metrics]
            assert sorted(families) == sorted(MetricFamily)
            assert {value.metric for value in evaluation.metrics} == set(
                BENCHMARK_METRIC_NAMES.values()
            )


def test_tail_metrics_recompute_as_importance_weighted_cost_quantiles(
    benchmark_report: Any, benchmark_spec: Any, benchmark_corpus_dir: Path
) -> None:
    cases = {
        case.case_id: case
        for case in load_case_file(read_corpus_json(benchmark_corpus_dir, "validation.json")).cases
    }
    for baseline in benchmark_report.baselines:
        for evaluation in baseline.seed_evaluations:
            records = [
                trial
                for trial in baseline.trials
                if trial.seed == evaluation.seed and trial.eligibility is CaseEligibility.ELIGIBLE
            ]
            expected = weighted_quantile(
                [
                    (
                        trial.weight,
                        reward(
                            cases[trial.case_id], OutcomeChannel.COST, confidence=trial.confidence
                        ),
                    )
                    for trial in records
                ],
                benchmark_spec.tail_quantile,
            )
            actual = next(
                metric.value for metric in evaluation.metrics if metric.family is MetricFamily.TAIL
            )
            assert actual == expected


def test_per_seed_values_precede_aggregation(benchmark_report: Any) -> None:
    """The measurement set carries raw seeds; the median is the protocol's job."""
    for baseline in benchmark_report.baselines:
        per_seed = {
            (value.metric, evaluation.seed): value.value
            for evaluation in baseline.seed_evaluations
            for value in evaluation.metrics
        }
        supplied = {
            (measurement.metric, measurement.seed): measurement.value
            for measurement in baseline.measurement_set.measurements
        }
        assert supplied == per_seed
        assert len(supplied) == 8 * len(benchmark_report.seeds)


def test_the_derived_measurement_sets_rescore_under_the_protocol(
    benchmark_report: Any, benchmark_protocol: Any
) -> None:
    from latent_compass.benchmark import score_benchmark_measurements

    for baseline in benchmark_report.baselines:
        rescored = score_benchmark_measurements(benchmark_protocol, baseline.measurement_set)
        assert rescored.canonical_payload() == baseline.verdict.canonical_payload()
        assert rescored.decision in (ContinueKill.CONTINUE, ContinueKill.KILL)


def test_a_benchmark_verdict_is_a_selection_verdict_on_validation(
    benchmark_report: Any,
) -> None:
    """It must not be mistaken for HOK-190's later ranker holdout verdict."""
    for baseline in benchmark_report.baselines:
        assert baseline.measurement_set.split is Split.VALIDATION
        assert baseline.measurement_set.purpose is HoldoutPurpose.SELECTION
        assert baseline.verdict.purpose is HoldoutPurpose.SELECTION


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def test_two_independent_runs_produce_the_same_payload_and_seal(
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    first = run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )
    second = run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )
    assert first.canonical_payload() == second.canonical_payload()
    assert first.report_seal == second.report_seal


def test_the_report_seal_recomputes_from_the_report(benchmark_report: Any) -> None:
    assert recompute_report_seal(benchmark_report) == benchmark_report.report_seal


def test_an_edit_after_sealing_is_detected(benchmark_report: Any) -> None:
    payload = benchmark_report.canonical_payload()
    payload["benchmark_id"] = "lc-hok188-forged"
    edited = load_benchmark_report(payload)
    assert recompute_report_seal(edited) != edited.report_seal


def test_a_report_may_not_claim_the_holdout_was_executed(benchmark_report: Any) -> None:
    payload = benchmark_report.canonical_payload()
    payload["holdout_executed"] = True
    with pytest.raises(BenchmarkViolation, match="failed strict contract validation"):
        load_benchmark_report(payload)


def test_the_report_declares_the_holdout_seal_it_never_read(
    benchmark_report: Any, benchmark_manifest: Any
) -> None:
    declared = benchmark_manifest.entry(Split.HOLDOUT).corpus_seal
    assert benchmark_report.holdout_corpus_seal == declared
    assert benchmark_report.executed_split is Split.VALIDATION
    assert benchmark_report.holdout_executed is False


# --------------------------------------------------------------------------- #
# The spec binds the run to its pre-registration
# --------------------------------------------------------------------------- #


def test_a_spec_written_against_a_moved_protocol_is_refused(
    benchmark_spec: Any, benchmark_protocol: Any
) -> None:
    revised = benchmark_protocol.revise(revision=2, epoch="LC-HOK188-E2")
    with pytest.raises(BenchmarkViolation) as refusal:
        require_spec_matches_protocol(benchmark_spec, revised)
    problems = json.dumps(refusal.value.as_dict())
    assert "protocol_seal" in problems


def test_a_spec_pinning_a_baseline_to_an_unimplemented_version_is_refused(
    benchmark_corpus_dir: Path,
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["baselines"][0]["algorithm_version"] = "9.9.9"
    with pytest.raises(BenchmarkViolation):
        load_benchmark_spec(payload)


def test_a_spec_dropping_one_of_the_four_baselines_is_refused(
    benchmark_corpus_dir: Path,
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["baselines"] = payload["baselines"][:3]
    with pytest.raises(BenchmarkViolation):
        load_benchmark_spec(payload)


def test_a_protocol_with_an_extra_metric_is_refused(
    benchmark_spec: Any, benchmark_protocol: Any
) -> None:
    from latent_compass.protocol import load_preregistration

    payload = benchmark_protocol.canonical_payload()
    payload["metrics"].append(
        {
            "name": "undeclared-extra-diagnostic",
            "family": "SUCCESS",
            "direction": "HIGHER_IS_BETTER",
            "threshold": 0.0,
            "required": False,
        }
    )
    expanded = load_preregistration(payload)
    matching_spec = benchmark_spec.model_copy(update={"protocol_seal": expanded.protocol_seal()})

    with pytest.raises(BenchmarkViolation):
        require_spec_matches_protocol(matching_spec, expanded)


def test_protocol_declares_only_sensitivity_outputs_the_report_carries(
    benchmark_protocol: Any,
) -> None:
    assert set(benchmark_protocol.sensitivity_analyses) == {
        "report SNIPS beside IPS for every observed channel",
        "report support, weight quantiles and effective sample size",
    }


def test_a_corpus_edited_after_the_spec_was_written_refuses_the_run(
    benchmark_spec: Any, benchmark_protocol: Any, mutable_corpus: Path
) -> None:
    payload = read_corpus_json(mutable_corpus, "validation.json")
    payload["cases"][0]["episode"]["economics"]["cost"] = 42.0
    (mutable_corpus / "validation.json").write_text(json.dumps(payload), encoding="utf-8")
    manifest = load_corpus_manifest(read_corpus_json(mutable_corpus, "manifest.json"))

    with pytest.raises(BenchmarkViolation) as refusal:
        run_benchmark(
            spec=benchmark_spec,
            protocol=benchmark_protocol,
            manifest=manifest,
            corpus_dir=mutable_corpus,
        )
    assert "does not seal to the value" in refusal.value.message


def test_a_mutated_case_moves_the_split_seal(benchmark_corpus_dir: Path) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    before = split_corpus_seal(load_case_file(payload).cases)
    payload["cases"][3]["ambiguous"] = not payload["cases"][3]["ambiguous"]
    assert split_corpus_seal(load_case_file(payload).cases) != before


def test_permuting_the_case_order_changes_nothing(
    benchmark_spec: Any, benchmark_protocol: Any, mutable_corpus: Path
) -> None:
    """File order is not an input. Canonical case order is."""
    payload = read_corpus_json(mutable_corpus, "validation.json")
    before = split_corpus_seal(load_case_file(payload).cases)
    payload["cases"] = list(reversed(payload["cases"]))
    (mutable_corpus / "validation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert split_corpus_seal(load_case_file(payload).cases) == before

    manifest = load_corpus_manifest(read_corpus_json(mutable_corpus, "manifest.json"))
    permuted = run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=manifest,
        corpus_dir=mutable_corpus,
    )
    assert permuted.report_seal == _reference_report_seal(
        benchmark_spec, benchmark_protocol, manifest, mutable_corpus
    )


def _reference_report_seal(spec: Any, protocol: Any, manifest: Any, corpus_dir: Path) -> str:
    return run_benchmark(
        spec=spec, protocol=protocol, manifest=manifest, corpus_dir=corpus_dir
    ).report_seal


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def test_verification_re_executes_rather_than_trusting_the_seal(
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    result = verify_report(
        benchmark_report,
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )
    assert result == {
        "verified": True,
        "benchmark_id": benchmark_report.benchmark_id,
        "report_seal": benchmark_report.report_seal,
        "spec_seal": benchmark_report.spec_seal,
        "validation_corpus_seal": benchmark_report.validation_corpus_seal,
        "baselines": [item.baseline_id.value for item in benchmark_report.baselines],
        "reexecuted": True,
    }


def test_a_self_consistent_forged_report_is_caught_by_re_execution(
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    """A forger who re-seals honestly still has to have run the baselines."""
    payload = benchmark_report.canonical_payload()
    payload["baselines"][0]["trials"][0]["confidence"] = 0.123456
    forged = load_benchmark_report(payload)
    resealed = load_benchmark_report(
        {**forged.canonical_payload(), "report_seal": recompute_report_seal(forged)}
    )

    # The forgery is internally coherent: its own seal reproduces.
    assert recompute_report_seal(resealed) == resealed.report_seal

    with pytest.raises(BenchmarkViolation) as refusal:
        verify_report(
            resealed,
            spec=benchmark_spec,
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )
    assert "does not reproduce from the artefacts" in refusal.value.message
    assert "baselines" in json.dumps(refusal.value.as_dict())


def test_a_budget_smaller_than_the_candidate_set_refuses_and_seals_nothing(
    benchmark_corpus_dir: Path, benchmark_protocol: Any, benchmark_manifest: Any
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["budget"]["max_candidate_inspections_per_case"] = 2
    with pytest.raises(BudgetExceeded) as refusal:
        run_benchmark(
            spec=load_benchmark_spec(payload),
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )
    assert refusal.value.detail is not None
    assert refusal.value.as_dict()["error"] == "budget_exceeded"


def _provenance() -> Any:
    from latent_compass.benchmark import CorpusProvenance

    return CorpusProvenance(
        origin="test fixture",
        licence="Apache-2.0",
        synthetic=True,
        description="a corpus built inside a test to exercise one refusal",
    )


def _write_split_bundle(
    target: Path,
    *,
    validation: dict[str, Any] | None = None,
    holdout: dict[str, Any] | None = None,
) -> None:
    """Copy the committed corpus into ``target``, replacing named splits."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("train.json", "validation.json", "holdout.json"):
        (target / name).write_text(
            (BENCHMARK_CORPUS / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    if validation is not None:
        (target / "validation.json").write_text(json.dumps(validation), encoding="utf-8")
    if holdout is not None:
        (target / "holdout.json").write_text(json.dumps(holdout), encoding="utf-8")


@pytest.fixture
def benchmark_report(
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> Any:
    """One real run of the committed corpus, shared by the read-only assertions."""
    return run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )


def test_a_benchmark_case_carries_the_episode_unwidened(benchmark_corpus_dir: Path) -> None:
    """The benchmark adds metadata beside an episode; it does not extend one."""
    payload = read_corpus_json(benchmark_corpus_dir, "validation.json")
    case: BenchmarkCase = load_case_file(payload).ordered_cases()[0]
    assert set(case.canonical_payload()) == {
        "contract_version",
        "case_id",
        "split",
        "distribution_kind",
        "distribution_group",
        "ambiguous",
        "observation_cutoff",
        "observation_horizon_seconds",
        "episode",
    }
    payload["cases"][0]["episode"]["extra_field"] = "smuggled"
    with pytest.raises(BenchmarkViolation):
        load_case_file(payload)

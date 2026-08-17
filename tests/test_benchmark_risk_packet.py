"""HOK-188 — the tranche risk packet, exercised as hostile regressions.

One module per invariant section. Each section states the scenario it is
defending against, drives it, and — where the invariant could be silently
weakened rather than removed — proves the assertion actually fails when the
invariant is taken away. An oracle that cannot fail is not an oracle.
"""

from __future__ import annotations

import builtins
import contextlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import BENCHMARK_CORPUS, read_corpus_json
from latent_compass.benchmark import (
    BaselineId,
    load_benchmark_spec,
    load_corpus_manifest,
    run_benchmark,
    select,
)
from latent_compass.benchmark import baselines as baselines_module
from latent_compass.benchmark.budget import CaseBudgetMeter
from latent_compass.benchmark.corpus import build_manifest, verify_manifest
from latent_compass.benchmark.ope import trial_binding_seal
from latent_compass.benchmark.report import load_benchmark_report
from latent_compass.canonical import canonical_bytes
from latent_compass.errors import AuthorityRefusal, BenchmarkViolation, BudgetExceeded
from latent_compass.protocol import Split, load_measurement_set, load_verdict
from latent_compass.vocabulary import Actor, LifecycleState
from test_benchmark import (
    CONTEXT,
    DISCRIMINATING_SEED,
    GRANT,
    discriminating_view,
    run_baseline,
)


@pytest.fixture
def report(
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> Any:
    return run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )


def refusal_text(error: BenchmarkViolation) -> str:
    return json.dumps(error.as_dict())


# =========================================================================== #
# Invariant 1 — seals and disjointness derive from the real cases
#
# Hostile scenario: two splits share a case, or a corpus is edited while its
# manifest keeps declaring the old seal.
# =========================================================================== #


def _corpus_with(target: Path, **replacements: dict[str, Any]) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("train.json", "validation.json", "holdout.json"):
        (target / name).write_text(
            (BENCHMARK_CORPUS / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    for name, payload in replacements.items():
        (target / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def _provenance() -> Any:
    from latent_compass.benchmark import CorpusProvenance

    return CorpusProvenance(
        origin="test fixture",
        licence="Apache-2.0",
        synthetic=True,
        description="built inside a test to drive one hostile scenario",
    )


RELATIVE_PATHS = {
    Split.TRAIN: "train.json",
    Split.VALIDATION: "validation.json",
    Split.HOLDOUT: "holdout.json",
}


def test_two_splits_sharing_a_case_id_are_refused_before_any_baseline_runs(
    tmp_path: Path,
) -> None:
    holdout = read_corpus_json(BENCHMARK_CORPUS, "holdout.json")
    validation = read_corpus_json(BENCHMARK_CORPUS, "validation.json")
    holdout["cases"][0]["case_id"] = validation["cases"][0]["case_id"]
    corpus = _corpus_with(tmp_path / "corpus", holdout=holdout)

    with pytest.raises(BenchmarkViolation) as refusal:
        build_manifest(
            corpus,
            corpus_id="lc-overlapping",
            corpus_version="v1.0.0",
            provenance=_provenance(),
            relative_paths=RELATIVE_PATHS,
        )
    assert "not disjoint" in refusal.value.message
    assert "case_id" in refusal_text(refusal.value)
    assert sorted(path.name for path in corpus.iterdir()) == [
        "holdout.json",
        "train.json",
        "validation.json",
    ], "a refused manifest build wrote something"


def test_two_splits_sharing_an_episode_id_are_refused(tmp_path: Path) -> None:
    """A renamed case wrapping the same episode is still the same evidence."""
    holdout = read_corpus_json(BENCHMARK_CORPUS, "holdout.json")
    validation = read_corpus_json(BENCHMARK_CORPUS, "validation.json")
    holdout["cases"][0]["episode"]["episode_id"] = validation["cases"][0]["episode"]["episode_id"]
    corpus = _corpus_with(tmp_path / "corpus", holdout=holdout)

    with pytest.raises(BenchmarkViolation) as refusal:
        build_manifest(
            corpus,
            corpus_id="lc-overlapping",
            corpus_version="v1.0.0",
            provenance=_provenance(),
            relative_paths=RELATIVE_PATHS,
        )
    assert "episode_id" in refusal_text(refusal.value)


def test_a_declared_seal_over_an_edited_corpus_is_refused(mutable_corpus: Path) -> None:
    """The manifest is recomputed from the files; it is never believed."""
    payload = read_corpus_json(mutable_corpus, "validation.json")
    payload["cases"][2]["ambiguous"] = not payload["cases"][2]["ambiguous"]
    (mutable_corpus / "validation.json").write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_corpus_manifest(read_corpus_json(mutable_corpus, "manifest.json"))
    with pytest.raises(BenchmarkViolation) as refusal:
        verify_manifest(mutable_corpus, manifest)
    assert "does not reproduce from the corpus files" in refusal.value.message
    assert "corpus_seal" in refusal_text(refusal.value)


def test_the_committed_manifest_still_verifies_when_nothing_was_edited(
    mutable_corpus: Path,
) -> None:
    """The negative above must fail for the edit, not for the copying."""
    manifest = load_corpus_manifest(read_corpus_json(mutable_corpus, "manifest.json"))
    assert verify_manifest(mutable_corpus, manifest)["verified"] is True


# =========================================================================== #
# Invariant 2 — the baseline x case x seed grid is exhaustive and matched
#
# Hostile scenario: an unfavourable seed is omitted, duplicated or renamed.
# =========================================================================== #


def test_the_grid_is_exactly_the_declared_product(report: Any) -> None:
    expected = {(case_id, seed) for case_id in report.case_ids for seed in report.seeds}
    for baseline in report.baselines:
        assert baseline.grid() == expected
        assert len(baseline.trials) == len(expected)


def test_a_report_missing_one_grid_coordinate_is_refused(report: Any) -> None:
    payload = report.canonical_payload()
    dropped = payload["baselines"][0]["trials"].pop(3)
    with pytest.raises(BenchmarkViolation) as refusal:
        load_benchmark_report(payload)
    text = refusal_text(refusal.value)
    assert "does not cover the declared grid" in text
    assert dropped["case_id"] in text


def test_a_report_repeating_a_grid_coordinate_is_refused(report: Any) -> None:
    payload = report.canonical_payload()
    trials = payload["baselines"][0]["trials"]
    trials.append(json.loads(json.dumps(trials[0])))
    with pytest.raises(BenchmarkViolation) as refusal:
        load_benchmark_report(payload)
    assert "repeats grid coordinates" in refusal_text(refusal.value)


def test_a_report_carrying_an_undeclared_seed_is_refused(report: Any) -> None:
    """Renaming a seed shows up as one missing coordinate and one extra."""
    payload = report.canonical_payload()
    payload["baselines"][0]["trials"][0]["seed"] = 99
    with pytest.raises(BenchmarkViolation) as refusal:
        load_benchmark_report(payload)
    text = refusal_text(refusal.value)
    assert "does not cover the declared grid" in text
    assert "99" in text


def test_a_baseline_skipping_a_seed_evaluation_is_refused(report: Any) -> None:
    payload = report.canonical_payload()
    payload["baselines"][0]["seed_evaluations"].pop()
    with pytest.raises(BenchmarkViolation) as refusal:
        load_benchmark_report(payload)
    assert "does not reproduce" in refusal_text(refusal.value) or "every declared seed" in (
        refusal_text(refusal.value)
    )


def test_the_unmodified_report_validates(report: Any) -> None:
    """The grid refusals above must fire for the edit, not for the round-trip."""
    assert load_benchmark_report(report.canonical_payload()).report_seal == report.report_seal


# =========================================================================== #
# Invariant 3 — the budget is genuinely common
#
# Hostile scenario: one baseline inspects more candidates, or receives a
# different cap.
# =========================================================================== #


def test_every_baseline_is_granted_a_byte_identical_budget(
    report: Any, benchmark_spec: Any
) -> None:
    granted = canonical_bytes(benchmark_spec.budget.canonical_payload())
    assert canonical_bytes(report.budget.canonical_payload()) == granted
    # There is exactly one grant object in the run; the ledger is measured
    # consumption against it, never a second allowance.
    for entry in report.budget_ledger:
        assert entry.max_candidate_inspections_on_a_case <= (
            report.budget.max_candidate_inspections_per_case
        )
        assert entry.max_random_draws_on_a_case <= report.budget.max_random_draws_per_case


def test_consumption_may_differ_while_the_grant_does_not(report: Any) -> None:
    by_id = {entry.baseline_id: entry for entry in report.budget_ledger}
    inspections = {entry.total_candidate_inspections for entry in report.budget_ledger}
    assert len(inspections) == 1, "the four baselines look at the same candidates"
    assert by_id[BaselineId.SEEDED_UNIFORM.value].total_random_draws > 0
    for identity in BaselineId:
        if identity is not BaselineId.SEEDED_UNIFORM:
            assert by_id[identity.value].total_random_draws == 0


def test_the_first_inspection_past_the_cap_refuses_and_seals_nothing(
    benchmark_corpus_dir: Path, benchmark_protocol: Any, benchmark_manifest: Any
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["budget"]["max_candidate_inspections_per_case"] = 1

    with pytest.raises(BudgetExceeded) as refusal:
        run_benchmark(
            spec=load_benchmark_spec(payload),
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["cap"] == 1
    assert detail["requested"] == 2, "the refusal must fire on the operation, not on a tally"


def test_raising_the_cap_removes_the_refusal(
    benchmark_corpus_dir: Path, benchmark_protocol: Any, benchmark_manifest: Any
) -> None:
    """Falsifies the test above: the cap is what refuses, not something else."""
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["budget"]["max_candidate_inspections_per_case"] = 4
    produced = run_benchmark(
        spec=load_benchmark_spec(payload),
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )
    assert produced.report_seal.startswith("sha256:")


def test_a_corpus_larger_than_the_case_cap_refuses_before_any_baseline_runs(
    benchmark_corpus_dir: Path, benchmark_protocol: Any, benchmark_manifest: Any
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["budget"]["max_cases"] = 4
    with pytest.raises(BudgetExceeded) as refusal:
        run_benchmark(
            spec=load_benchmark_spec(payload),
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )
    assert refusal.value.detail == {"split": "VALIDATION", "cap": 4, "case_count": 16}


# =========================================================================== #
# Invariant 4 — the four baselines are distinct and were really invoked
#
# Hostile scenario: two identities point at one behaviour, or a result is
# copied from one baseline to another without invoking it.
# =========================================================================== #


def test_the_four_baselines_disagree_on_the_discriminating_case() -> None:
    view = discriminating_view()
    chosen = {
        identity: run_baseline(identity, view, DISCRIMINATING_SEED)[0].direction_id
        for identity in BaselineId
    }
    assert chosen == {
        BaselineId.FIXED_CANONICAL: "dir-a",
        BaselineId.LEAST_UNCERTAINTY: "dir-b",
        BaselineId.SEEDED_UNIFORM: "dir-c",
        BaselineId.LOGGED_PROPENSITY_ARBITER: "dir-d",
    }
    assert len(set(chosen.values())) == 4


def test_aliasing_two_baselines_breaks_the_distinctness_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removes the invariant and proves the assertion above stops holding."""
    selectors = dict(baselines_module._SELECTORS)  # noqa: SLF001 - the point of the test
    selectors[BaselineId.LEAST_UNCERTAINTY] = selectors[BaselineId.FIXED_CANONICAL]
    monkeypatch.setattr(baselines_module, "_SELECTORS", selectors)

    view = discriminating_view()
    chosen = {
        run_baseline(identity, view, DISCRIMINATING_SEED)[0].direction_id for identity in BaselineId
    }
    assert len(chosen) < 4, "aliasing two baselines must be visible in their outputs"


def test_the_baselines_disagree_across_the_real_corpus(report: Any) -> None:
    """Not merely on one crafted case: they diverge on the committed corpus too."""
    selections = {
        baseline.baseline_id: tuple(
            trial.selected_direction_id
            for trial in sorted(baseline.trials, key=lambda item: (item.case_id, item.seed))
        )
        for baseline in report.baselines
    }
    assert len(set(selections.values())) == 4, "two baselines produced identical selections"


def test_a_receipt_lifted_from_another_baseline_does_not_reproduce_its_binding(
    report: Any,
) -> None:
    """A copied result is detectable without re-deriving the whole report."""
    donor = next(item for item in report.baselines if item.baseline_id is BaselineId.SEEDED_UNIFORM)
    trial = donor.trials[0]
    assert trial.binding_seal == trial_binding_seal(
        spec_seal=report.spec_seal,
        corpus_seal=report.validation_corpus_seal,
        baseline_id=trial.baseline_id,
        algorithm_version=trial.algorithm_version,
        case_id=trial.case_id,
        seed=trial.seed,
        selected_direction_id=trial.selected_direction_id,
    )
    for changed in (
        {"baseline_id": BaselineId.FIXED_CANONICAL.value},
        {"algorithm_version": "9.9.9"},
        {"seed": trial.seed + 1},
        {"corpus_seal": "sha256:another-corpus"},
    ):
        assert (
            trial_binding_seal(
                **{
                    "spec_seal": report.spec_seal,
                    "corpus_seal": report.validation_corpus_seal,
                    "baseline_id": trial.baseline_id,
                    "algorithm_version": trial.algorithm_version,
                    "case_id": trial.case_id,
                    "seed": trial.seed,
                    "selected_direction_id": trial.selected_direction_id,
                    **changed,
                }
            )
            != trial.binding_seal
        )


def test_a_baseline_answering_outside_the_candidate_set_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latent_compass.benchmark.baselines import BaselineChoice

    def rogue(view: Any, seed: int, context: Any, meter: Any) -> BaselineChoice:
        return BaselineChoice(direction_id="dir-not-a-candidate", confidence=1.0)

    selectors = dict(baselines_module._SELECTORS)  # noqa: SLF001 - the point of the test
    selectors[BaselineId.FIXED_CANONICAL] = rogue
    monkeypatch.setattr(baselines_module, "_SELECTORS", selectors)

    meter = CaseBudgetMeter(
        GRANT, baseline_id="fixed-canonical", case_id="case-discriminating", seed=1
    )
    with pytest.raises(BenchmarkViolation, match="not a candidate"):
        select(
            BaselineId.FIXED_CANONICAL,
            discriminating_view(),
            seed=DISCRIMINATING_SEED,
            context=CONTEXT,
            meter=meter,
        )


# =========================================================================== #
# Invariant 5 — the holdout is unreachable from the runner
#
# Hostile scenario: the runner opens or loads the holdout, for selection or
# for exploration.
# =========================================================================== #


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path this process opens, by any of the three routes.

    Reading the holdout through ``Path.read_text``, ``Path.open`` or the bare
    builtin ``open`` all land here, so the assertion is about the file system,
    not about which helper the runner happens to call today.
    """
    seen: list[str] = []
    original_read_text = Path.read_text
    original_path_open = Path.open
    original_builtin_open = builtins.open

    def note(candidate: object) -> None:
        # A file descriptor, a memory-mapped name or a closed handle is not a
        # path; it cannot be the holdout either, so it is simply not recorded.
        with contextlib.suppress(TypeError, ValueError, OSError):
            seen.append(str(Path(candidate).resolve()))  # type: ignore[arg-type]

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        note(self)
        return original_read_text(self, *args, **kwargs)

    def spy_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        note(self)
        return original_path_open(self, *args, **kwargs)

    def spy_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        note(file)
        return original_builtin_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "open", spy_path_open)
    monkeypatch.setattr(builtins, "open", spy_builtin_open)
    return seen


def test_the_runner_never_opens_the_holdout_file(
    opened_paths: list[str],
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    holdout = str((benchmark_corpus_dir / "holdout.json").resolve())
    validation = str((benchmark_corpus_dir / "validation.json").resolve())

    produced = run_benchmark(
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
    )

    assert validation in opened_paths, "the spy did not observe the read it should have"
    assert holdout not in opened_paths, "the runner opened the holdout"
    assert produced.holdout_executed is False
    assert produced.holdout_corpus_seal == benchmark_manifest.entry(Split.HOLDOUT).corpus_seal


def test_a_spec_naming_the_holdout_is_refused_before_any_corpus_file_is_opened(
    opened_paths: list[str], benchmark_corpus_dir: Path
) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    del opened_paths[:]
    payload["executed_split"] = "HOLDOUT"

    with pytest.raises(BenchmarkViolation) as refusal:
        load_benchmark_spec(payload)
    assert "VALIDATION" in refusal_text(refusal.value)

    corpus_files = {
        str((benchmark_corpus_dir / name).resolve())
        for name in ("train.json", "validation.json", "holdout.json")
    }
    assert not corpus_files & set(opened_paths), "a refused spec still touched corpus data"


def test_a_spec_naming_train_is_refused_too(benchmark_corpus_dir: Path) -> None:
    payload = read_corpus_json(benchmark_corpus_dir, "spec.json")
    payload["executed_split"] = "TRAIN"
    with pytest.raises(BenchmarkViolation):
        load_benchmark_spec(payload)


def test_no_benchmark_module_imports_the_authority_or_ledger_layers() -> None:
    """The benchmark must not become a second route to an authorisation."""
    package = Path(__file__).resolve().parents[1] / "src" / "latent_compass" / "benchmark"
    modules = sorted(package.glob("*.py"))
    assert len(modules) >= 8, f"the dependency scan only examined {len(modules)} modules"
    for module in modules:
        text = module.read_text(encoding="utf-8")
        assert "latent_compass.authority" not in text, f"{module.name} imports authority"
        assert "latent_compass.ledger" not in text, f"{module.name} imports ledger"

    authority = (package.parent / "authority.py").read_text(encoding="utf-8")
    assert "latent_compass.benchmark" not in authority, "authority imports the benchmark"


def test_benchmark_score_objects_are_rejected_as_direct_transition_evidence(
    report: Any, benchmark_protocol: Any
) -> None:
    """A benchmark report is diagnostic evidence, never transition evidence."""
    from latent_compass.authority import authorize_transition

    continuing = next(item for item in report.baselines if item.verdict.decision == "CONTINUE")

    with pytest.raises(AuthorityRefusal):
        authorize_transition(
            from_state=LifecycleState.SHADOW,
            to_state=LifecycleState.OFFLINE_VERIFIED,
            actor=Actor.EXTERNAL_JUDGE,
            protocol=benchmark_protocol,
            measurements=continuing.measurement_set,
            verdict=continuing.verdict,
        )


def test_benchmark_scores_cannot_be_laundered_by_removing_the_scope_marker(
    report: Any, benchmark_protocol: Any
) -> None:
    """Structural validity is not trusted provenance for a lifecycle move."""
    from latent_compass.authority import authorize_transition

    continuing = next(item for item in report.baselines if item.verdict.decision == "CONTINUE")
    measurement_payload = continuing.measurement_set.canonical_payload()
    verdict_payload = continuing.verdict.canonical_payload()
    measurement_payload.pop("evidence_scope")
    verdict_payload.pop("evidence_scope")

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.SHADOW,
            to_state=LifecycleState.OFFLINE_VERIFIED,
            actor=Actor.EXTERNAL_JUDGE,
            protocol=benchmark_protocol,
            measurements=load_measurement_set(measurement_payload),
            verdict=load_verdict(verdict_payload),
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "untrusted_evidence"


# =========================================================================== #
# Additional oracles named in the packet
# =========================================================================== #


def test_a_failure_part_way_through_produces_no_report_at_all(
    monkeypatch: pytest.MonkeyPatch,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    """The report is constructed once, at the end, from values that all exist."""
    from latent_compass.benchmark import runner as runner_module
    from latent_compass.benchmark.metrics import evaluate_seed

    calls = {"count": 0}

    def failing(**kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] > 4:
            raise BenchmarkViolation("simulated failure part way through the run", detail={})
        return evaluate_seed(**kwargs)

    monkeypatch.setattr(runner_module, "evaluate_seed", failing)

    with pytest.raises(BenchmarkViolation, match="simulated failure"):
        run_benchmark(
            spec=benchmark_spec,
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )
    assert calls["count"] > 1, "the run must have got part way in for this to mean anything"


def test_no_case_is_dropped_between_the_corpus_and_the_diagnostics(report: Any) -> None:
    """Every declared case reaches exactly one eligibility state, per seed."""
    for baseline in report.baselines:
        for evaluation in baseline.seed_evaluations:
            diagnostics = evaluation.diagnostics
            assert diagnostics.total_cases == len(report.case_ids)
            counted = (
                diagnostics.eligible_cases
                + diagnostics.unobservable_cases
                + diagnostics.censored_immature_cases
                + diagnostics.incomplete_outcome_cases
                + diagnostics.out_of_support_cases
            )
            assert counted == len(report.case_ids)

            seen = {trial.case_id for trial in baseline.trials if trial.seed == evaluation.seed}
            assert seen == set(report.case_ids)


def test_an_out_of_support_case_refuses_instead_of_changing_one_denominator(
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    stricter = benchmark_spec.model_copy(update={"minimum_propensity": 0.02})

    with pytest.raises(BenchmarkViolation) as refusal:
        run_benchmark(
            spec=stricter,
            protocol=benchmark_protocol,
            manifest=benchmark_manifest,
            corpus_dir=benchmark_corpus_dir,
        )

    detail = cast(dict[str, Any], refusal.value.as_dict()["detail"])
    assert detail["baseline_id"] == BaselineId.FIXED_CANONICAL.value
    assert any(
        coordinate[0] == "case-val-09" for coordinate in detail["unsupported_case_seed_coordinates"]
    )


def test_every_successful_baseline_uses_the_same_analysis_population(report: Any) -> None:
    """A successful comparison must not change its estimand by baseline."""
    for seed in report.seeds:
        eligible_counts = {
            evaluation.diagnostics.eligible_cases
            for baseline in report.baselines
            for evaluation in baseline.seed_evaluations
            if evaluation.seed == seed
        }
        out_of_support_counts = {
            evaluation.diagnostics.out_of_support_cases
            for baseline in report.baselines
            for evaluation in baseline.seed_evaluations
            if evaluation.seed == seed
        }
        assert len(eligible_counts) == 1
        assert out_of_support_counts == {0}

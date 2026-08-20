"""Pre-HOK-190 prerequisite — freeze selection before final holdout access."""

from __future__ import annotations

import builtins
import contextlib
from pathlib import Path
from typing import Any

import pytest

from latent_compass.benchmark import (
    BaselineId,
    create_holdout_plan,
    load_holdout_plan,
    recompute_holdout_plan_seal,
    run_benchmark,
)
from latent_compass.benchmark.report import load_benchmark_report, recompute_report_seal
from latent_compass.errors import BenchmarkViolation


@pytest.fixture
def benchmark_report(
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


def _create_plan(
    *,
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
    baseline_id: BaselineId = BaselineId.LEAST_UNCERTAINTY,
) -> Any:
    return create_holdout_plan(
        benchmark_report,
        spec=benchmark_spec,
        protocol=benchmark_protocol,
        manifest=benchmark_manifest,
        corpus_dir=benchmark_corpus_dir,
        selected_baseline_id=baseline_id,
    )


def test_plan_replays_validation_without_opening_holdout(
    monkeypatch: pytest.MonkeyPatch,
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    holdout = (benchmark_corpus_dir / "holdout.json").resolve()
    validation = (benchmark_corpus_dir / "validation.json").resolve()
    opened: list[Path] = []
    original_read_text = Path.read_text
    original_open = Path.open
    original_builtin_open = builtins.open

    def note(candidate: object) -> None:
        with contextlib.suppress(TypeError, ValueError, OSError):
            opened.append(Path(candidate).resolve())  # type: ignore[arg-type]

    def is_holdout(candidate: object) -> bool:
        with contextlib.suppress(TypeError, ValueError, OSError):
            return Path(candidate).resolve() == holdout  # type: ignore[arg-type]
        return False

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        note(path)
        return original_read_text(path, *args, **kwargs)

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        note(path)
        if path.resolve() == holdout:
            raise AssertionError("the phase-1 planner opened the real holdout")
        return original_open(path, *args, **kwargs)

    def guarded_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        note(file)
        if is_holdout(file):
            raise AssertionError("the phase-1 planner opened the real holdout")
        return original_builtin_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_builtin_open)

    plan = _create_plan(
        benchmark_report=benchmark_report,
        benchmark_spec=benchmark_spec,
        benchmark_protocol=benchmark_protocol,
        benchmark_manifest=benchmark_manifest,
        benchmark_corpus_dir=benchmark_corpus_dir,
    )

    assert holdout not in opened
    assert validation in opened
    assert plan.scope == "FINAL_HOLDOUT_PLAN_ONLY"
    assert plan.holdout_executed is False


def test_plan_binds_every_pre_holdout_identity_and_is_deterministic(
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    first = _create_plan(
        benchmark_report=benchmark_report,
        benchmark_spec=benchmark_spec,
        benchmark_protocol=benchmark_protocol,
        benchmark_manifest=benchmark_manifest,
        benchmark_corpus_dir=benchmark_corpus_dir,
    )
    second = _create_plan(
        benchmark_report=benchmark_report,
        benchmark_spec=benchmark_spec,
        benchmark_protocol=benchmark_protocol,
        benchmark_manifest=benchmark_manifest,
        benchmark_corpus_dir=benchmark_corpus_dir,
    )

    assert first == second
    assert first.plan_seal == recompute_holdout_plan_seal(first)
    assert first.benchmark_id == benchmark_report.benchmark_id
    assert first.spec_seal == benchmark_spec.spec_seal()
    assert first.validation_report_seal == benchmark_report.report_seal
    assert first.protocol_id == benchmark_protocol.protocol_id
    assert first.protocol_seal == benchmark_protocol.protocol_seal()
    assert first.epoch == benchmark_protocol.epoch
    assert first.corpus_id == benchmark_spec.corpus_id
    assert first.corpus_version == benchmark_spec.corpus_version
    assert first.validation_corpus_seal == benchmark_spec.validation_corpus_seal
    assert first.holdout_corpus_seal == benchmark_spec.holdout_corpus_seal
    assert first.baseline_registry_seal == benchmark_report.baseline_registry_seal
    assert first.selected_baseline_id is BaselineId.LEAST_UNCERTAINTY
    assert first.selected_algorithm_version == "1.0.0"
    assert first.selection_mode == "EXTERNAL_EXPLICIT"


def test_plan_requires_an_explicit_validation_continue(
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    with pytest.raises(BenchmarkViolation, match="CONTINUE"):
        _create_plan(
            benchmark_report=benchmark_report,
            benchmark_spec=benchmark_spec,
            benchmark_protocol=benchmark_protocol,
            benchmark_manifest=benchmark_manifest,
            benchmark_corpus_dir=benchmark_corpus_dir,
            baseline_id=BaselineId.FIXED_CANONICAL,
        )


def test_plan_refuses_a_modified_report_even_when_resealed(
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    payload = benchmark_report.canonical_payload()
    payload["baselines"][0]["trials"][0]["confidence"] = 0.123456
    forged = load_benchmark_report(payload)
    resealed = load_benchmark_report(
        {**forged.canonical_payload(), "report_seal": recompute_report_seal(forged)}
    )

    with pytest.raises(BenchmarkViolation, match="does not reproduce from the artefacts"):
        _create_plan(
            benchmark_report=resealed,
            benchmark_spec=benchmark_spec,
            benchmark_protocol=benchmark_protocol,
            benchmark_manifest=benchmark_manifest,
            benchmark_corpus_dir=benchmark_corpus_dir,
        )


@pytest.mark.parametrize("extra_field", ["authority", "attestation"])
def test_plan_payload_is_strict_and_cannot_carry_claims(
    extra_field: str,
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    plan = _create_plan(
        benchmark_report=benchmark_report,
        benchmark_spec=benchmark_spec,
        benchmark_protocol=benchmark_protocol,
        benchmark_manifest=benchmark_manifest,
        benchmark_corpus_dir=benchmark_corpus_dir,
    )
    payload = plan.canonical_payload()
    payload[extra_field] = "PROMOTED"

    with pytest.raises(BenchmarkViolation):
        load_holdout_plan(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("selected_algorithm_version", "9.9.9"),
        ("plan_seal", "sha256:" + "f" * 64),
    ],
)
def test_plan_loader_refuses_a_stale_or_false_seal(
    field: str,
    value: str,
    benchmark_report: Any,
    benchmark_spec: Any,
    benchmark_protocol: Any,
    benchmark_manifest: Any,
    benchmark_corpus_dir: Path,
) -> None:
    plan = _create_plan(
        benchmark_report=benchmark_report,
        benchmark_spec=benchmark_spec,
        benchmark_protocol=benchmark_protocol,
        benchmark_manifest=benchmark_manifest,
        benchmark_corpus_dir=benchmark_corpus_dir,
    )
    payload = plan.canonical_payload()
    payload[field] = value

    with pytest.raises(BenchmarkViolation, match="seal does not reproduce"):
        load_holdout_plan(payload)

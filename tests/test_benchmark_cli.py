"""HOK-188 — the benchmark command line, its confinement and its exit codes."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from conftest import BENCHMARK_CORPUS, read_corpus_json
from latent_compass.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, build_parser, main


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def corpus_args(corpus: Path) -> list[str]:
    return [
        "--spec",
        str(corpus / "spec.json"),
        "--protocol",
        str(corpus / "protocol.json"),
        "--manifest",
        str(corpus / "manifest.json"),
        "--corpus-dir",
        str(corpus),
    ]


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #


def test_manifest_build_derives_a_manifest_that_then_verifies(
    tmp_path: Path, mutable_corpus: Path
) -> None:
    root = tmp_path / "out"
    destination = root / "rebuilt-manifest.json"
    code, out, _ = run(
        "benchmark",
        "manifest",
        "build",
        "--root",
        str(root),
        "--corpus-dir",
        str(mutable_corpus),
        "--corpus-id",
        "lc-synthetic-bench",
        "--corpus-version",
        "v1.0.0",
        "--train",
        "train.json",
        "--validation",
        "validation.json",
        "--holdout",
        "holdout.json",
        "--origin",
        "rebuilt inside a test",
        "--licence",
        "Apache-2.0",
        "--description",
        "a rebuild of the committed synthetic corpus manifest",
        "--synthetic",
        "--out",
        str(destination),
    )
    assert code == EXIT_OK
    assert out["manifest_written_to"] == str(destination.resolve())

    committed = read_corpus_json(mutable_corpus, "manifest.json")
    rebuilt = json.loads(destination.read_text(encoding="utf-8"))
    assert {entry["split"]: entry["corpus_seal"] for entry in rebuilt["splits"]} == {
        entry["split"]: entry["corpus_seal"] for entry in committed["splits"]
    }

    code, _, _ = run(
        "benchmark",
        "manifest",
        "verify",
        "--corpus-dir",
        str(mutable_corpus),
        "--manifest",
        str(destination),
    )
    assert code == EXIT_OK


def test_manifest_build_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, mutable_corpus: Path
) -> None:
    root = tmp_path / "out"
    root.mkdir()
    destination = root / "manifest.json"
    destination.write_text("{}", encoding="utf-8")

    code, _, err = run(
        "benchmark",
        "manifest",
        "build",
        "--root",
        str(root),
        "--corpus-dir",
        str(mutable_corpus),
        "--corpus-id",
        "lc-synthetic-bench",
        "--corpus-version",
        "v1.0.0",
        "--train",
        "train.json",
        "--validation",
        "validation.json",
        "--holdout",
        "holdout.json",
        "--origin",
        "x",
        "--licence",
        "Apache-2.0",
        "--description",
        "y",
        "--out",
        str(destination),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert destination.read_text(encoding="utf-8") == "{}"


def test_manifest_verify_refuses_an_edited_corpus(mutable_corpus: Path) -> None:
    payload = read_corpus_json(mutable_corpus, "validation.json")
    payload["cases"][0]["ambiguous"] = not payload["cases"][0]["ambiguous"]
    (mutable_corpus / "validation.json").write_text(json.dumps(payload), encoding="utf-8")

    code, _, err = run(
        "benchmark",
        "manifest",
        "verify",
        "--corpus-dir",
        str(mutable_corpus),
        "--manifest",
        str(mutable_corpus / "manifest.json"),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "benchmark_violation"


# --------------------------------------------------------------------------- #
# spec
# --------------------------------------------------------------------------- #


def test_spec_validate_reports_the_seals_and_the_executed_split() -> None:
    code, out, _ = run(
        "benchmark",
        "spec",
        "validate",
        "--spec",
        str(BENCHMARK_CORPUS / "spec.json"),
        "--protocol",
        str(BENCHMARK_CORPUS / "protocol.json"),
        "--manifest",
        str(BENCHMARK_CORPUS / "manifest.json"),
    )
    assert code == EXIT_OK
    assert out["executed_split"] == "VALIDATION"
    assert out["holdout_executed"] is False
    assert out["spec_seal"].startswith("sha256:")
    assert out["holdout_corpus_seal"] != out["validation_corpus_seal"]


def test_spec_validate_refuses_a_spec_bound_to_another_corpus(
    tmp_path: Path, mutable_corpus: Path
) -> None:
    payload = read_corpus_json(mutable_corpus, "spec.json")
    payload["validation_corpus_seal"] = "sha256:" + "b" * 64
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(payload), encoding="utf-8")

    code, _, err = run(
        "benchmark",
        "spec",
        "validate",
        "--spec",
        str(spec_file),
        "--protocol",
        str(mutable_corpus / "protocol.json"),
        "--manifest",
        str(mutable_corpus / "manifest.json"),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "benchmark_violation"


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_emits_a_sealed_report_on_stdout() -> None:
    code, out, _ = run("benchmark", "run", *corpus_args(BENCHMARK_CORPUS))
    assert code == EXIT_OK
    report = out["report"]
    assert report["executed_split"] == "VALIDATION"
    assert report["holdout_executed"] is False
    assert len(report["baselines"]) == 4
    assert report["report_seal"].startswith("sha256:")


def test_run_writes_the_report_inside_the_named_root(tmp_path: Path) -> None:
    root = tmp_path / "run"
    destination = root / "report.json"
    code, out, _ = run(
        "benchmark",
        "run",
        *corpus_args(BENCHMARK_CORPUS),
        "--root",
        str(root),
        "--out",
        str(destination),
    )
    assert code == EXIT_OK
    assert out["report_written_to"] == str(destination.resolve())
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["report_seal"] == out["report_seal"]


def test_run_refuses_an_out_without_a_root(tmp_path: Path) -> None:
    code, _, err = run(
        "benchmark", "run", *corpus_args(BENCHMARK_CORPUS), "--out", str(tmp_path / "report.json")
    )
    assert code == EXIT_REFUSED
    assert err["detail"] == {"requires": "--root", "for": "--out"}
    assert not (tmp_path / "report.json").exists()


def test_run_refuses_a_destination_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "run"
    escape = tmp_path / "elsewhere.json"
    code, _, err = run(
        "benchmark",
        "run",
        *corpus_args(BENCHMARK_CORPUS),
        "--root",
        str(root),
        "--out",
        str(escape),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert not escape.exists()


def test_a_refused_run_writes_no_report(tmp_path: Path, mutable_corpus: Path) -> None:
    """A budget refusal must not leave a partial artefact behind."""
    payload = read_corpus_json(mutable_corpus, "spec.json")
    payload["budget"]["max_candidate_inspections_per_case"] = 1
    spec_file = mutable_corpus / "tight-spec.json"
    spec_file.write_text(json.dumps(payload), encoding="utf-8")

    root = tmp_path / "run"
    destination = root / "report.json"
    code, _, err = run(
        "benchmark",
        "run",
        "--spec",
        str(spec_file),
        "--protocol",
        str(mutable_corpus / "protocol.json"),
        "--manifest",
        str(mutable_corpus / "manifest.json"),
        "--corpus-dir",
        str(mutable_corpus),
        "--root",
        str(root),
        "--out",
        str(destination),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "budget_exceeded"
    assert not destination.exists()


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def test_verify_accepts_a_report_it_can_re_execute(tmp_path: Path) -> None:
    root = tmp_path / "run"
    destination = root / "report.json"
    assert (
        run(
            "benchmark",
            "run",
            *corpus_args(BENCHMARK_CORPUS),
            "--root",
            str(root),
            "--out",
            str(destination),
        )[0]
        == EXIT_OK
    )

    code, out, _ = run(
        "benchmark", "verify", "--report", str(destination), *corpus_args(BENCHMARK_CORPUS)
    )
    assert code == EXIT_OK
    assert out["verification"]["verified"] is True
    assert out["verification"]["reexecuted"] is True


def test_verify_refuses_a_report_whose_seal_was_left_stale(tmp_path: Path) -> None:
    root = tmp_path / "run"
    destination = root / "report.json"
    run(
        "benchmark",
        "run",
        *corpus_args(BENCHMARK_CORPUS),
        "--root",
        str(root),
        "--out",
        str(destination),
    )
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["benchmark_id"] = "lc-hok188-forged"
    destination.write_text(json.dumps(payload), encoding="utf-8")

    code, _, err = run(
        "benchmark", "verify", "--report", str(destination), *corpus_args(BENCHMARK_CORPUS)
    )
    assert code == EXIT_REFUSED
    assert "does not reproduce from the report" in err["message"]


# --------------------------------------------------------------------------- #
# Pre-HOK-190 prerequisite: freeze selection, do not execute the holdout
# --------------------------------------------------------------------------- #


def test_holdout_plan_writes_a_sealed_pre_execution_checkpoint(tmp_path: Path) -> None:
    run_root = tmp_path / "validation"
    report = run_root / "report.json"
    assert (
        run(
            "benchmark",
            "run",
            *corpus_args(BENCHMARK_CORPUS),
            "--root",
            str(run_root),
            "--out",
            str(report),
        )[0]
        == EXIT_OK
    )

    plan_root = tmp_path / "holdout-plan"
    destination = plan_root / "plan.json"
    code, out, _ = run(
        "benchmark",
        "holdout",
        "plan",
        "--report",
        str(report),
        *corpus_args(BENCHMARK_CORPUS),
        "--baseline",
        "least-uncertainty",
        "--root",
        str(plan_root),
        "--out",
        str(destination),
    )

    assert code == EXIT_OK
    assert out["plan_written_to"] == str(destination.resolve())
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["scope"] == "FINAL_HOLDOUT_PLAN_ONLY"
    assert written["selected_baseline_id"] == "least-uncertainty"
    assert written["selection_mode"] == "EXTERNAL_EXPLICIT"
    assert written["holdout_executed"] is False
    assert written["plan_seal"] == out["plan_seal"]


def test_holdout_plan_refuses_a_kill_baseline_and_writes_nothing(tmp_path: Path) -> None:
    run_root = tmp_path / "validation"
    report = run_root / "report.json"
    run(
        "benchmark",
        "run",
        *corpus_args(BENCHMARK_CORPUS),
        "--root",
        str(run_root),
        "--out",
        str(report),
    )
    plan_root = tmp_path / "holdout-plan"
    destination = plan_root / "plan.json"

    code, _, err = run(
        "benchmark",
        "holdout",
        "plan",
        "--report",
        str(report),
        *corpus_args(BENCHMARK_CORPUS),
        "--baseline",
        "fixed-canonical",
        "--root",
        str(plan_root),
        "--out",
        str(destination),
    )

    assert code == EXIT_REFUSED
    assert "CONTINUE" in err["message"]
    assert not destination.exists()


def test_holdout_plan_refuses_a_destination_outside_its_root(tmp_path: Path) -> None:
    run_root = tmp_path / "validation"
    report = run_root / "report.json"
    run(
        "benchmark",
        "run",
        *corpus_args(BENCHMARK_CORPUS),
        "--root",
        str(run_root),
        "--out",
        str(report),
    )
    root = tmp_path / "plan-root"
    escape = tmp_path / "escape.json"

    code, _, err = run(
        "benchmark",
        "holdout",
        "plan",
        "--report",
        str(report),
        *corpus_args(BENCHMARK_CORPUS),
        "--baseline",
        "least-uncertainty",
        "--root",
        str(root),
        "--out",
        str(escape),
    )

    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert not escape.exists()


# --------------------------------------------------------------------------- #
# There is no holdout route on this surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["benchmark", "run", "--holdout-ledger", "usage.json"],
        ["benchmark", "verify", "--holdout-ledger", "usage.json"],
        ["benchmark", "run", "--split", "HOLDOUT"],
        ["benchmark", "run", "--holdout", "holdout.json"],
    ],
)
def test_the_benchmark_surface_rejects_any_holdout_option(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_status:
        build_parser().parse_args(argv)
    assert exit_status.value.code == EXIT_USAGE


def _subcommands(parser: Any) -> dict[str, Any]:
    """The sub-parsers registered directly under ``parser``."""
    for action in parser._actions:  # noqa: SLF001 - parser introspection
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            return choices
    return {}


def test_no_benchmark_subcommand_declares_a_holdout_ledger_option() -> None:
    """Stated over the parser itself, so a future subcommand cannot add one quietly."""
    stack = [_subcommands(build_parser())["benchmark"]]
    inspected = 0
    while stack:
        current = stack.pop()
        inspected += 1
        for action in current._actions:  # noqa: SLF001 - parser introspection
            assert "--holdout-ledger" not in action.option_strings
            assert "--holdout-usage" not in action.option_strings
        stack.extend(_subcommands(current).values())
    assert inspected >= 6, f"the parser scan only examined {inspected} parsers"

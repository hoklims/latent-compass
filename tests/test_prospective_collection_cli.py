"""HOK-252 operator CLI stays local, preregistered and diagnostic."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from pathlib import Path

import pytest

from conftest import (
    EPOCH,
    HOST_ID,
    STORE_ID,
    observation_set,
    reconciliation_payload,
    strategic_decision_payload,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, build_parser, main
from latent_compass.decision_memory import admit_strategic_decision
from latent_compass.decision_reconciliation import ReconciliationJournal
from latent_compass.episode import AgentFamily


def run(*argv: str) -> tuple[int, object, object]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def power_payload(*, max_enrollments: int = 1) -> dict[str, object]:
    return {
        "method": "EXACT_ONE_SIDED_BINOMIAL_V1",
        "status": "ESTABLISHED",
        "inputs": {
            "method": "EXACT_ONE_SIDED_BINOMIAL_V1",
            "p0": 0.1,
            "p1": 0.9,
            "alpha": 0.2,
            "target_power": 0.8,
            "max_enrollments": max_enrollments,
            "clustering_inflation": 1.0,
        },
        "independent_eligible_count": 1,
        "required_enrollments": 1,
        "critical_eligible_count": 1,
        "achieved_alpha": 0.1,
        "achieved_power": 0.9,
    }


def plan_payload(*, max_enrollments: int = 1) -> dict[str, object]:
    return {
        "contract_version": "1.0.0",
        "plan_id": "prospective-plan-cli",
        "source_binding": {
            "decision": {
                "host_id": HOST_ID,
                "agent_family": "claude",
                "store_id": STORE_ID,
                "epoch": EPOCH,
            },
            "reconciliation": {
                "host_id": HOST_ID,
                "agent_family": "claude",
                "store_id": STORE_ID,
                "epoch": EPOCH,
            },
        },
        "population": "Every qualifying HOK-243 decision in the sealed window.",
        "declared_strata": ["maintenance"],
        "hard_exclusions": [
            "PRE_PLAN_OR_BACKFILL",
            "WRONG_BINDING",
            "HOLDOUT",
            "CANCELLED",
            "INVALID_PREIMAGE",
            "NON_INDEPENDENT_RECONCILIATION",
        ],
        "plan_created_at": "2026-08-15T08:00:00Z",
        "collection_not_before": "2026-08-16T08:00:00Z",
        "minimum_calendar_end": "2026-08-21T08:00:00Z",
        "hard_calendar_end": "2026-08-31T08:00:00Z",
        "max_enrollments": max_enrollments,
        "outcome_dependent_interim_looks": 0,
        "independence_policy": {"decision_producer_identities": ["operator-alpha"]},
        "power": power_payload(max_enrollments=max_enrollments),
        "stop_priority": [
            "INTEGRITY_OR_SECURITY_FAILURE",
            "SUFFICIENT_AFTER_MINIMUM_CALENDAR",
            "INSUFFICIENT_AT_HARD_CALENDAR_END",
        ],
    }


def set_clock(monkeypatch: pytest.MonkeyPatch, timestamp: str) -> None:
    monkeypatch.setattr("latent_compass.cli.utc_now", lambda: timestamp)


def initialise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "shadow"
    plan = write_json(tmp_path / "plan.json", plan_payload())
    set_clock(monkeypatch, "2026-08-15T08:00:00Z")
    code, out, err = run("shadow", "init", "--root", str(root), "--plan", str(plan))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["created"]["state"] == "SEALED"
    return root, plan


def create_source(
    root: Path,
    decision_payload: dict[str, object],
    reconciliation: dict[str, object] | None = None,
) -> ReconciliationJournal:
    decision = admit_strategic_decision(decision_payload)
    source = ReconciliationJournal.create(
        root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
        clock=lambda: "2026-08-15T08:00:00Z",
    )
    source.append_reconciliation(
        reconciliation or reconciliation_payload(),
        decision_record=decision,
        clock=lambda: "2026-08-20T10:00:00Z",
    )
    return source


def test_power_validate_init_and_status_are_practical_json_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out, err = run(
        "shadow",
        "power",
        "--p0",
        "0.1",
        "--p1",
        "0.9",
        "--alpha",
        "0.2",
        "--target-power",
        "0.8",
        "--max-enrollments",
        "1",
        "--clustering-inflation",
        "1.0",
    )
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["power"]["required_enrollments"] == 1

    root, plan = initialise(tmp_path, monkeypatch)
    code, out, err = run("shadow", "validate-plan", "--plan", str(plan))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["plan_seal"].startswith("sha256:")
    code, out, err = run("shadow", "status", "--root", str(root))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["status"]["state"] == "SEALED"
    code, out, err = run("shadow", "limits")
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["limits"]["influences_routing"] is False


def test_cli_lifecycle_closes_sufficient_and_publishes_deterministic_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    decision_payload = strategic_decision_payload()
    decision_file = write_json(tmp_path / "decision.json", decision_payload)
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    assert run("shadow", "start", "--root", str(root))[0] == EXIT_OK
    set_clock(monkeypatch, "2026-08-16T09:00:00Z")
    code, out, err = run(
        "shadow",
        "enroll",
        "--root",
        str(root),
        "--decision-record",
        str(decision_file),
        "--stratum",
        "maintenance",
    )
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    case_id = out["enrolled"]["case_id"]

    source = create_source(tmp_path / "source", decision_payload)
    source.close()
    set_clock(monkeypatch, "2026-08-20T10:00:00Z")
    code, _, err = run(
        "shadow",
        "reconcile",
        "--root",
        str(root),
        "--case-id",
        str(case_id),
        "--decision-record",
        str(decision_file),
        "--source-root",
        str(tmp_path / "source"),
        "--reconciliation-id",
        "reconciliation-0001",
    )
    assert code == EXIT_OK, err

    set_clock(monkeypatch, "2026-08-21T08:00:00Z")
    code, out, err = run("shadow", "close", "--root", str(root))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["status"]["state"] == "CLOSED_SUFFICIENT"
    code, out, err = run("shadow", "verify", "--root", str(root))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["integrity"]["ok"] is True

    manifest = root / "artifacts" / "manifest.json"
    report = root / "artifacts" / "report.json"
    first_stdout, second_stdout = StringIO(), StringIO()
    assert (
        main(["shadow", "manifest", "--root", str(root)], stdout=first_stdout, stderr=StringIO())
        == EXIT_OK
    )
    assert (
        main(["shadow", "manifest", "--root", str(root)], stdout=second_stdout, stderr=StringIO())
        == EXIT_OK
    )
    assert first_stdout.getvalue().encode() == second_stdout.getvalue().encode()
    assert run("shadow", "manifest", "--root", str(root), "--out", str(manifest))[0] == EXIT_OK
    assert run("shadow", "report", "--root", str(root), "--out", str(report))[0] == EXIT_OK
    first_manifest = manifest.read_bytes()
    first_report = report.read_bytes()
    assert json.loads(first_manifest)["collection_state"] == "CLOSED_SUFFICIENT"
    assert json.loads(first_report)["selection_imbalance_diagnostic"]["label"] == (
        "selection_imbalance_diagnostic"
    )
    assert first_manifest.endswith(b"\n")
    assert first_report.endswith(b"\n")
    assert run("shadow", "manifest", "--root", str(root), "--out", str(manifest))[0] == (
        EXIT_REFUSED
    )
    assert manifest.read_bytes() == first_manifest
    assert report.read_bytes() == first_report


def test_cli_lifecycle_can_close_insufficient_but_cannot_publish_while_collecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    assert run("shadow", "start", "--root", str(root))[0] == EXIT_OK
    assert run("shadow", "manifest", "--root", str(root))[0] == EXIT_REFUSED
    assert run("shadow", "report", "--root", str(root))[0] == EXIT_REFUSED
    outside = tmp_path / "outside.json"
    assert run("shadow", "manifest", "--root", str(root), "--out", str(outside))[0] == EXIT_REFUSED
    assert not outside.exists()
    set_clock(monkeypatch, "2026-08-31T08:00:00Z")
    code, out, err = run("shadow", "close", "--root", str(root))
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["status"]["state"] == "CLOSED_INSUFFICIENT"
    assert run("shadow", "manifest", "--root", str(root))[0] == EXIT_OK
    assert run("shadow", "report", "--root", str(root))[0] == EXIT_OK
    assert run("shadow", "manifest", "--root", str(root), "--out", str(outside))[0] == EXIT_REFUSED
    assert not outside.exists()


def test_terminal_command_keeps_the_case_in_the_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    decision = write_json(tmp_path / "decision.json", strategic_decision_payload())
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    assert run("shadow", "start", "--root", str(root))[0] == EXIT_OK
    set_clock(monkeypatch, "2026-08-16T09:00:00Z")
    _, enrolled, _ = run(
        "shadow",
        "enroll",
        "--root",
        str(root),
        "--decision-record",
        str(decision),
        "--stratum",
        "maintenance",
    )
    assert isinstance(enrolled, dict)
    set_clock(monkeypatch, "2026-08-20T10:00:00Z")
    code, _, err = run(
        "shadow",
        "terminal",
        "--root",
        str(root),
        "--case-id",
        str(enrolled["enrolled"]["case_id"]),
        "--state",
        "lost-to-followup",
        "--reason",
        "observer unavailable",
    )
    assert code == EXIT_OK, err
    code, out, _ = run("shadow", "status", "--root", str(root))
    assert code == EXIT_OK
    assert isinstance(out, dict)
    assert out["status"]["enrolled_count"] == 1
    assert out["status"]["terminal_count"] == 1


def test_invalid_bounded_plan_input_creates_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "never-created"
    payload = plan_payload()
    payload["unexpected"] = {
        "nested": {"credential": "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz1234567890"}
    }
    plan = write_json(tmp_path / "invalid-plan.json", payload)
    set_clock(monkeypatch, "2026-08-15T08:00:00Z")
    code, _, err = run("shadow", "init", "--root", str(root), "--plan", str(plan))
    assert code == EXIT_REFUSED
    assert isinstance(err, dict)
    assert err["error"] == "prospective_collection_violation"
    assert not root.exists()


@pytest.mark.parametrize(
    ("reason", "start_first"),
    [("integrity-failure", False), ("security-failure", True)],
)
def test_abort_is_terminal_cas_guarded_and_never_publishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    start_first: bool,
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    if start_first:
        assert run("shadow", "start", "--root", str(root))[0] == EXIT_OK
    before = run("shadow", "status", "--root", str(root))[1]
    assert isinstance(before, dict)
    generation = before["status"]["generation"]
    code, _, _ = run(
        "shadow",
        "abort",
        "--root",
        str(root),
        "--reason",
        reason,
        "--expected-generation",
        str(generation + 1),
    )
    assert code == EXIT_REFUSED
    assert run("shadow", "status", "--root", str(root))[1] == before

    code, out, err = run(
        "shadow",
        "abort",
        "--root",
        str(root),
        "--reason",
        reason,
        "--expected-generation",
        str(generation),
    )
    assert code == EXIT_OK, err
    assert isinstance(out, dict)
    assert out["status"]["state"] == "ABORTED"
    assert run("shadow", "status", "--root", str(root))[1] == out
    assert run("shadow", "manifest", "--root", str(root))[0] == EXIT_REFUSED
    assert run("shadow", "report", "--root", str(root))[0] == EXIT_REFUSED
    assert run("shadow", "abort", "--root", str(root), "--reason", reason)[0] == EXIT_REFUSED
    assert run("shadow", "start", "--root", str(root))[0] == EXIT_REFUSED
    decision = write_json(tmp_path / "after-abort.json", strategic_decision_payload())
    assert (
        run(
            "shadow",
            "enroll",
            "--root",
            str(root),
            "--decision-record",
            str(decision),
            "--stratum",
            "maintenance",
        )[0]
        == EXIT_REFUSED
    )
    assert (
        run(
            "shadow",
            "terminal",
            "--root",
            str(root),
            "--case-id",
            "case-after-abort",
            "--state",
            "cancelled",
            "--reason",
            "must refuse",
        )[0]
        == EXIT_REFUSED
    )
    source_root = tmp_path / "after-abort-source"
    source = create_source(source_root, strategic_decision_payload())
    source.close()
    assert (
        run(
            "shadow",
            "reconcile",
            "--root",
            str(root),
            "--case-id",
            "case-after-abort",
            "--decision-record",
            str(decision),
            "--source-root",
            str(source_root),
            "--reconciliation-id",
            "reconciliation-0001",
        )[0]
        == EXIT_REFUSED
    )
    assert run("shadow", "close", "--root", str(root))[0] == EXIT_REFUSED


@pytest.mark.parametrize(
    ("decision_overrides", "expected_fragment"),
    [
        ({"captured_at": "2026-08-15T07:00:00Z"}, "window"),
        (
            {
                "binding": {
                    "host_id": HOST_ID,
                    "agent_family": "claude",
                    "store_id": "wrong-store",
                    "epoch": EPOCH,
                }
            },
            "wrong sealed source",
        ),
    ],
)
def test_invalid_backfill_and_binding_enrollment_write_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision_overrides: dict[str, object],
    expected_fragment: str,
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    assert run("shadow", "start", "--root", str(root))[0] == EXIT_OK
    payload = strategic_decision_payload()
    payload.update(decision_overrides)
    decision = write_json(tmp_path / "invalid-decision.json", payload)
    before = run("shadow", "status", "--root", str(root))[1]
    set_clock(monkeypatch, "2026-08-16T09:00:00Z")
    code, _, err = run(
        "shadow",
        "enroll",
        "--root",
        str(root),
        "--decision-record",
        str(decision),
        "--stratum",
        "maintenance",
    )
    assert code == EXIT_REFUSED
    assert expected_fragment in json.dumps(err)
    assert run("shadow", "status", "--root", str(root))[1] == before


def test_terminal_source_and_producer_refusals_are_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = initialise(tmp_path, monkeypatch)
    decision_payload = strategic_decision_payload()
    decision_file = write_json(tmp_path / "decision.json", decision_payload)
    set_clock(monkeypatch, "2026-08-16T08:00:00Z")
    run("shadow", "start", "--root", str(root))
    set_clock(monkeypatch, "2026-08-16T09:00:00Z")
    _, enrolled, _ = run(
        "shadow",
        "enroll",
        "--root",
        str(root),
        "--decision-record",
        str(decision_file),
        "--stratum",
        "maintenance",
    )
    assert isinstance(enrolled, dict)
    case_id = str(enrolled["enrolled"]["case_id"])

    dependent_observations = observation_set()
    for observation in dependent_observations:
        provenance = observation["provenance"]
        assert isinstance(provenance, dict)
        provenance["producer"] = "operator-alpha"
    dependent = reconciliation_payload(
        reconciled_by="operator-alpha", observations=dependent_observations
    )
    source = create_source(tmp_path / "dependent-source", decision_payload, dependent)
    source.close()
    before = run("shadow", "status", "--root", str(root))[1]
    set_clock(monkeypatch, "2026-08-20T10:00:00Z")
    code, _, _ = run(
        "shadow",
        "reconcile",
        "--root",
        str(root),
        "--case-id",
        case_id,
        "--decision-record",
        str(decision_file),
        "--source-root",
        str(tmp_path / "dependent-source"),
        "--reconciliation-id",
        "reconciliation-0001",
    )
    assert code == EXIT_REFUSED
    assert run("shadow", "status", "--root", str(root))[1] == before

    source = create_source(tmp_path / "tampered-source", decision_payload)
    source._conn.execute(  # noqa: SLF001 - hostile source fixture
        "UPDATE store_meta SET value=? WHERE key='anchor_tail'", ("sha256:" + "0" * 64,)
    )
    source.close()
    code, _, _ = run(
        "shadow",
        "reconcile",
        "--root",
        str(root),
        "--case-id",
        case_id,
        "--decision-record",
        str(decision_file),
        "--source-root",
        str(tmp_path / "tampered-source"),
        "--reconciliation-id",
        "reconciliation-0001",
    )
    assert code == EXIT_INTEGRITY
    assert run("shadow", "status", "--root", str(root))[1] == before


def test_shadow_parser_has_required_arguments_and_no_forbidden_surface() -> None:
    parser = build_parser()
    top = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    shadow = top.choices["shadow"]
    group = next(
        action
        for action in shadow._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    assert set(group.choices) == {
        "power",
        "validate-plan",
        "init",
        "start",
        "enroll",
        "reconcile",
        "terminal",
        "status",
        "close",
        "abort",
        "verify",
        "manifest",
        "report",
        "limits",
    }
    surface = " ".join(
        option
        for command in group.choices.values()
        for action in command._actions  # noqa: SLF001
        for option in action.option_strings
    )
    for forbidden in ("authority", "route", "holdout", "train", "score", "promote"):
        assert forbidden not in surface.lower()
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(["shadow", "init"])
    assert missing.value.code == 2


def test_shadow_docs_state_the_offline_and_real_collection_boundaries() -> None:
    root = Path(__file__).parents[1]
    guide = (root / "docs" / "prospective-shadow-collection.md").read_text(encoding="utf-8")
    adr = (root / "docs" / "adr" / "0010-prospective-shadow-collection.md").read_text(
        encoding="utf-8"
    )
    template = json.loads(
        (root / "docs" / "examples" / "prospective-plan.template.json").read_text(encoding="utf-8")
    )
    combined = guide + adr
    for claim in (
        "OFFLINE_VERIFIED",
        "POWER_NOT_ESTABLISHED",
        "real elapsed collection",
        "no existing or backfilled cases",
        "local consistency",
    ):
        assert claim in combined
    assert template["outcome_dependent_interim_looks"] == 0
    assert template["power"]["status"] == "POWER_NOT_ESTABLISHED"
    assert template["power"]["required_enrollments"] is None

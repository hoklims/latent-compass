"""HOK-244 — the reconciliation command line, end to end and at its refusals.

The surface is closed: there is no subcommand that selects a route, scores a
candidate, promotes anything or authorises anything, and the parser proves it by
refusing the verbs and flags that would express one. Exit codes are asserted, not
inferred: a caller branches on them without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    EPOCH,
    EXECUTED_DIRECTION_ID,
    HOST_ID,
    RECONCILIATION_ID,
    STORE_ID,
    observation_set,
    observed_dimension,
    preimage_payload,
    reconciliation_payload,
    strategic_decision_payload,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, EXIT_STORE, EXIT_USAGE, main
from latent_compass.decision_reconciliation.store import RECONCILIATION_DATABASE_FILENAME


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return (
        code,
        json.loads(out.getvalue() or "null"),
        json.loads(err.getvalue() or "null"),
    )


def init_journal(root: Path, *, store_id: str = STORE_ID, family: str = "claude") -> Path:
    code, _, err = run(
        "reconcile",
        "init",
        "--root",
        str(root),
        "--store-id",
        store_id,
        "--host-id",
        HOST_ID,
        "--agent-family",
        family,
        "--epoch",
        EPOCH,
    )
    assert code == EXIT_OK, err
    return root


@pytest.fixture
def journal_cli_root(tmp_path: Path) -> Path:
    return init_journal(tmp_path / "journal")


@pytest.fixture
def decision_file(tmp_path: Path) -> Path:
    return write_json(tmp_path / "decision.json", strategic_decision_payload())


def append(
    root: Path,
    payload: dict[str, Any],
    name: str = "reconciliation.json",
    *,
    decision_record: Path | None = None,
) -> tuple[int, Any, Any]:
    document = write_json(root.parent / name, payload)
    if decision_record is None:
        decision_record = write_json(root.parent / "decision.json", strategic_decision_payload())
    argv = ["reconcile", "append", "--root", str(root), "--reconciliation", str(document)]
    argv += ["--decision-record", str(decision_record)]
    return run(*argv)


def test_the_lifecycle_runs_end_to_end(
    journal_cli_root: Path, decision_file: Path, tmp_path: Path
) -> None:
    code, out, err = append(
        journal_cli_root, reconciliation_payload(), decision_record=decision_file
    )
    assert code == EXIT_OK, err
    seal = out["appended"]["content_seal"]
    assert out["appended"]["generation"] == 1
    assert out["appended"]["preimage_verified"] is True

    revision = write_json(
        tmp_path / "correction.json",
        reconciliation_payload(
            revision=2,
            revision_kind="CORRECTION",
            revision_reason="the cost source was restated",
            supersedes_revision_seal=seal,
            observations=observation_set(unknowns={"COST": "DISPUTED"}),
        ),
    )
    code, out, err = run(
        "reconcile",
        "revise",
        "--root",
        str(journal_cli_root),
        "--reconciliation",
        str(revision),
        "--decision-record",
        str(decision_file),
        "--expected-generation",
        "1",
    )
    assert code == EXIT_OK, err
    assert out["appended"]["revision"] == 2

    code, out, _ = run("reconcile", "status", "--root", str(journal_cli_root))
    assert code == EXIT_OK
    assert (out["status"]["generation"], out["status"]["entry_count"]) == (2, 2)
    assert out["status"]["reconciliation_count"] == 1

    code, out, _ = run("reconcile", "verify", "--root", str(journal_cli_root))
    assert code == EXIT_OK
    assert out["integrity"]["ok"] is True

    code, out, _ = run("reconcile", "list", "--root", str(journal_cli_root))
    assert code == EXIT_OK
    assert [head["revision"] for head in out["heads"]] == [2]

    code, out, _ = run(
        "reconcile",
        "show",
        "--root",
        str(journal_cli_root),
        "--reconciliation-id",
        RECONCILIATION_ID,
    )
    assert code == EXIT_OK
    assert out["reconciliation"]["revision"] == 2
    assert [item["revision"] for item in out["revision_seals"]] == [1, 2]

    code, out, err = run(
        "reconcile",
        "replay",
        "--root",
        str(journal_cli_root),
        "--reconciliation-id",
        RECONCILIATION_ID,
        "--decision-record",
        str(decision_file),
    )
    assert code == EXIT_OK, err
    dimensions = [item["dimension"] for item in out["replay"]["comparisons"]]
    assert dimensions == ["SUCCESS", "VIOLATION", "COST", "INFORMATION", "REVERSIBILITY"]
    cost = next(item for item in out["replay"]["comparisons"] if item["dimension"] == "COST")
    assert cost["linkage"] == "PRE_ACTION_ONLY"
    assert cost["observation"]["unknown_reason"] == "DISPUTED"
    assert cost["observation"]["value"] is None

    code, out, err = run(
        "reconcile", "abandon-epoch", "--root", str(journal_cli_root), "--reason", "collected"
    )
    assert code == EXIT_OK, err
    assert out["binding"]["epoch_status"] == "abandoned"


def test_append_and_revise_mean_what_their_names_say(
    journal_cli_root: Path, tmp_path: Path
) -> None:
    code, _, err = append(
        journal_cli_root,
        reconciliation_payload(
            revision=2,
            revision_kind="CORRECTION",
            revision_reason="x",
            supersedes_revision_seal="sha256:" + "f" * 64,
        ),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"

    document = write_json(tmp_path / "initial.json", reconciliation_payload())
    decision = write_json(tmp_path / "decision-for-revise.json", strategic_decision_payload())
    code, _, err = run(
        "reconcile",
        "revise",
        "--root",
        str(journal_cli_root),
        "--reconciliation",
        str(document),
        "--decision-record",
        str(decision),
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["forbidden_revision"] == 1


def test_a_wrong_preimage_is_refused_through_the_cli(
    journal_cli_root: Path, decision_file: Path
) -> None:
    code, _, err = append(
        journal_cli_root,
        reconciliation_payload(preimage=preimage_payload(record_seal="sha256:" + "a" * 64)),
        decision_record=decision_file,
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "preimage_mismatch"
    assert err["detail"]["field"] == "record_seal"


def test_an_executed_direction_outside_the_preimage_is_refused_through_the_cli(
    journal_cli_root: Path, decision_file: Path
) -> None:
    code, _, err = append(
        journal_cli_root,
        reconciliation_payload(executed_direction_id="direction-invented"),
        decision_record=decision_file,
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["reason"] == "executed_direction_not_a_candidate"


def test_append_requires_the_exact_pre_action_record(
    journal_cli_root: Path, tmp_path: Path
) -> None:
    document = write_json(tmp_path / "missing-preimage.json", reconciliation_payload())
    with pytest.raises(SystemExit) as exit_code:
        run(
            "reconcile",
            "append",
            "--root",
            str(journal_cli_root),
            "--reconciliation",
            str(document),
        )
    assert exit_code.value.code == EXIT_USAGE


def test_replay_requires_the_preimage_it_compares_against(journal_cli_root: Path) -> None:
    append(journal_cli_root, reconciliation_payload())
    with pytest.raises(SystemExit) as exit_code:
        run(
            "reconcile",
            "replay",
            "--root",
            str(journal_cli_root),
            "--reconciliation-id",
            RECONCILIATION_ID,
        )
    assert exit_code.value.code == EXIT_USAGE


def test_replay_refuses_a_record_that_is_not_the_named_preimage(
    journal_cli_root: Path, tmp_path: Path
) -> None:
    append(journal_cli_root, reconciliation_payload())
    other = write_json(
        tmp_path / "other-decision.json", strategic_decision_payload("decision-strategic-0002")
    )
    code, _, err = run(
        "reconcile",
        "replay",
        "--root",
        str(journal_cli_root),
        "--reconciliation-id",
        RECONCILIATION_ID,
        "--decision-record",
        str(other),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "preimage_mismatch"


def test_an_unexecuted_reconciliation_replays_with_no_comparison(
    journal_cli_root: Path, decision_file: Path
) -> None:
    code, _, err = append(
        journal_cli_root,
        reconciliation_payload(
            execution_state="NOT_EXECUTED", executed_direction_id=None, observations=[]
        ),
        decision_record=decision_file,
    )
    assert code == EXIT_OK, err
    code, out, _ = run(
        "reconcile",
        "replay",
        "--root",
        str(journal_cli_root),
        "--reconciliation-id",
        RECONCILIATION_ID,
        "--decision-record",
        str(decision_file),
    )
    assert code == EXIT_OK
    assert out["replay"]["comparisons"] == []
    assert out["replay"]["executed_direction_id"] is None


def test_a_credential_shape_is_refused_through_the_cli(journal_cli_root: Path) -> None:
    observations = observation_set()
    observations[0] = observed_dimension("SUCCESS", statement="AKIA" + "W" * 16)
    code, _, err = append(journal_cli_root, reconciliation_payload(observations=observations))
    assert code == EXIT_REFUSED
    assert err["error"] == "sensitive_content_refused"
    assert "AKIA" not in json.dumps(err)


def test_deep_nesting_is_a_typed_refusal_through_the_cli(journal_cli_root: Path) -> None:
    nested: dict[str, Any] = {"leaf": True}
    for _ in range(64):
        nested = {"nested": nested}
    code, _, err = append(journal_cli_root, reconciliation_payload(unexpected=nested))
    assert code == EXIT_REFUSED
    assert err["error"] == "reconciliation_violation"
    assert err["detail"]["max_nesting_depth"] == 32


def test_a_credential_in_the_journal_identity_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "credential-journal"
    code, _, err = run(
        "reconcile",
        "init",
        "--root",
        str(root),
        "--store-id",
        STORE_ID,
        "--host-id",
        "AKIA" + "V" * 16,
        "--agent-family",
        "claude",
        "--epoch",
        EPOCH,
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "sensitive_content_refused"
    assert not root.exists()


def test_invalid_json_and_a_missing_journal_are_typed_errors(
    tmp_path: Path, journal_cli_root: Path, decision_file: Path
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    code, _, err = run(
        "reconcile",
        "append",
        "--root",
        str(journal_cli_root),
        "--reconciliation",
        str(broken),
        "--decision-record",
        str(decision_file),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"

    code, _, err = run("reconcile", "status", "--root", str(tmp_path / "absent"))
    assert code == EXIT_STORE
    assert err["error"] == "store_not_found"


def test_verify_exits_with_the_integrity_code_on_a_tampered_journal(
    journal_cli_root: Path,
) -> None:
    append(journal_cli_root, reconciliation_payload())
    connection = sqlite3.connect(journal_cli_root / RECONCILIATION_DATABASE_FILENAME)
    try:
        connection.execute("UPDATE reconciliations SET revision = 9 WHERE seq = 1")
        connection.commit()
    finally:
        connection.close()

    code, out, _ = run("reconcile", "verify", "--root", str(journal_cli_root))
    assert code == EXIT_INTEGRITY
    assert out["integrity"]["ok"] is False
    assert "CHAIN_BROKEN" in {finding["kind"] for finding in out["integrity"]["findings"]}


def test_a_bounded_page_is_enforced(journal_cli_root: Path) -> None:
    append(journal_cli_root, reconciliation_payload())
    code, _, err = run("reconcile", "list", "--root", str(journal_cli_root), "--limit", "5000")
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"


def test_limits_publishes_what_is_refused_and_what_is_not_produced() -> None:
    code, out, _ = run("reconcile", "limits")
    assert code == EXIT_OK
    admission = out["admission"]
    assert admission["universal_secret_detection"] is False
    assert admission["produces_scalar_score"] is False
    assert admission["produces_ranking"] is False
    assert admission["produces_causal_claim"] is False
    assert admission["grants_authority"] is False
    assert admission["unknown_reasons"] == ["ABSENT", "LATE", "AMBIGUOUS", "DISPUTED"]
    for forbidden in ("score", "rank", "verdict", "promote", "counterfactual", "holdout"):
        assert forbidden in admission["forbidden_field_names"]


@pytest.mark.parametrize(
    "verb", ["select", "decide", "authorize", "authorise", "promote", "learn", "train", "score"]
)
def test_the_reconcile_namespace_has_no_decision_taking_verb(verb: str) -> None:
    with pytest.raises(SystemExit) as exit_code:
        run("reconcile", verb, "--root", ".")
    assert exit_code.value.code == EXIT_USAGE


@pytest.mark.parametrize(
    "flag",
    [
        "--selected-direction-id",
        "--score",
        "--rank",
        "--promote",
        "--authorize",
        "--verdict",
        "--reward",
    ],
)
def test_the_append_command_refuses_a_decision_taking_flag(
    journal_cli_root: Path, tmp_path: Path, flag: str
) -> None:
    document = write_json(tmp_path / "reconciliation.json", reconciliation_payload())
    with pytest.raises(SystemExit) as exit_code:
        run(
            "reconcile",
            "append",
            "--root",
            str(journal_cli_root),
            "--reconciliation",
            str(document),
            flag,
            EXECUTED_DIRECTION_ID,
        )
    assert exit_code.value.code == EXIT_USAGE


def test_the_reconcile_surface_lists_only_evidence_verbs() -> None:
    from latent_compass.cli import build_parser

    parser = build_parser()
    groups = [
        action
        for action in parser._actions  # noqa: SLF001 - argparse exposes no public reader
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    reconcile = groups[0].choices["reconcile"]
    reconcile_groups = [
        action
        for action in reconcile._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    assert sorted(reconcile_groups[0].choices) == [
        "abandon-epoch",
        "append",
        "init",
        "limits",
        "list",
        "replay",
        "revise",
        "show",
        "status",
        "verify",
    ]


def test_the_journal_cli_never_writes_to_the_decision_memory(
    journal_cli_root: Path, decision_file: Path
) -> None:
    """The pre-action file is read as evidence and left byte-identical."""
    before = decision_file.read_bytes()
    code, _, err = append(journal_cli_root, reconciliation_payload(), decision_record=decision_file)
    assert code == EXIT_OK, err
    run(
        "reconcile",
        "replay",
        "--root",
        str(journal_cli_root),
        "--reconciliation-id",
        RECONCILIATION_ID,
        "--decision-record",
        str(decision_file),
    )
    assert decision_file.read_bytes() == before

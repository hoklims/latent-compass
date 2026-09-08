"""HOK-243 — the decision memory command line, end to end and at its refusals.

The surface is closed: there is no subcommand that selects a route, records an
outcome or authorises anything, and the parser proves it by refusing the flags
that would express one.
"""

from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    DECISION_ID,
    EPOCH,
    HOST_ID,
    STORE_ID,
    strategic_decision_payload,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, EXIT_STORE, main

BEFORE_REVIEW = "2026-09-01T00:00:00Z"
AFTER_EXPIRY = "2027-09-01T00:00:00Z"
CODEX_STORE_ID = "store-codex"


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return (
        code,
        json.loads(out.getvalue() or "null"),
        json.loads(err.getvalue() or "null"),
    )


def init_memory(root: Path, *, store_id: str = STORE_ID, family: str = "claude") -> Path:
    code, _, err = run(
        "memory",
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
def memory_root(tmp_path: Path) -> Path:
    return init_memory(tmp_path / "memory")


def append(root: Path, payload: dict[str, Any], name: str = "record.json") -> tuple[int, Any, Any]:
    record = write_json(root.parent / name, payload)
    return run("memory", "append", "--root", str(root), "--record", str(record))


def test_the_lifecycle_runs_end_to_end(memory_root: Path, tmp_path: Path) -> None:
    code, out, err = append(memory_root, strategic_decision_payload())
    assert code == EXIT_OK, err
    seal = out["appended"]["content_seal"]
    assert out["appended"]["origin_kind"] == "NATIVE"
    assert out["appended"]["generation"] == 1

    revision = write_json(
        tmp_path / "revision.json",
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal),
    )
    code, out, err = run(
        "memory",
        "revise",
        "--root",
        str(memory_root),
        "--record",
        str(revision),
        "--expected-generation",
        "1",
    )
    assert code == EXIT_OK, err
    assert out["appended"]["revision"] == 2

    code, out, _ = run("memory", "status", "--root", str(memory_root))
    assert code == EXIT_OK
    assert (out["status"]["generation"], out["status"]["record_count"]) == (2, 2)

    code, out, _ = run("memory", "verify", "--root", str(memory_root))
    assert code == EXIT_OK
    assert out["integrity"]["ok"] is True

    code, out, _ = run("memory", "list", "--root", str(memory_root), "--as-of", BEFORE_REVIEW)
    assert code == EXIT_OK
    assert [entry["revision"] for entry in out["active"]] == [2]

    code, out, _ = run(
        "memory",
        "show",
        "--root",
        str(memory_root),
        "--decision-id",
        DECISION_ID,
        "--as-of",
        BEFORE_REVIEW,
    )
    assert code == EXIT_OK
    assert out["active"]["record"]["decision_id"] == DECISION_ID


def test_append_and_revise_mean_what_their_names_say(memory_root: Path, tmp_path: Path) -> None:
    code, out, err = append(memory_root, strategic_decision_payload())
    assert code == EXIT_OK
    seal = out["appended"]["content_seal"]

    # A revision offered to `append` is refused rather than silently accepted.
    forward = write_json(
        tmp_path / "forward.json",
        strategic_decision_payload(revision=2, supersedes_revision_seal=seal),
    )
    code, _, err = run("memory", "append", "--root", str(memory_root), "--record", str(forward))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"

    # And an initial record offered to `revise` is refused too.
    initial = write_json(tmp_path / "initial.json", strategic_decision_payload("decision-x-0002"))
    code, _, err = run("memory", "revise", "--root", str(memory_root), "--record", str(initial))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("sensitivity", "HOLDOUT", "sensitive_content_refused"),
        ("sensitivity", "UNKNOWN", "sensitive_content_refused"),
        ("impact_class", "ROUTINE", "sensitive_content_refused"),
        ("verdict", "PASS", "sensitive_content_refused"),
    ],
)
def test_an_inadmissible_record_is_refused_with_a_stable_code(
    memory_root: Path, field: str, value: str, code: str
) -> None:
    payload = strategic_decision_payload()
    payload[field] = value
    exit_code, _, err = append(memory_root, payload)
    assert exit_code == EXIT_REFUSED
    assert err["error"] == code

    status_code, out, _ = run("memory", "status", "--root", str(memory_root))
    assert status_code == EXIT_OK
    assert (out["status"]["generation"], out["status"]["record_count"]) == (0, 0)


def test_revocation_and_expiry_both_empty_the_active_view(memory_root: Path) -> None:
    assert append(memory_root, strategic_decision_payload())[0] == EXIT_OK

    code, out, _ = run("memory", "list", "--root", str(memory_root), "--as-of", AFTER_EXPIRY)
    assert (code, out["active"]) == (EXIT_OK, [])

    code, out, err = run(
        "memory",
        "revoke",
        "--root",
        str(memory_root),
        "--decision-id",
        DECISION_ID,
        "--reason",
        "superseded by a wider programme",
        "--revoked-by",
        "operator-alpha",
    )
    assert code == EXIT_OK, err
    assert out["revoked"]["event_kind"] == "REVOCATION"

    code, out, _ = run("memory", "list", "--root", str(memory_root), "--as-of", BEFORE_REVIEW)
    assert (code, out["active"]) == (EXIT_OK, [])

    code, _, err = run(
        "memory",
        "show",
        "--root",
        str(memory_root),
        "--decision-id",
        DECISION_ID,
        "--as-of",
        BEFORE_REVIEW,
    )
    assert code == EXIT_STORE
    assert err["detail"]["reason"] == "revoked"


def test_a_tombstone_removes_the_payload_and_keeps_the_store_verifiable(
    memory_root: Path,
) -> None:
    assert append(memory_root, strategic_decision_payload())[0] == EXIT_OK
    code, out, err = run(
        "memory",
        "tombstone",
        "--root",
        str(memory_root),
        "--decision-id",
        DECISION_ID,
        "--revision",
        "1",
        "--reason",
        "subject erasure request",
    )
    assert code == EXIT_OK, err
    assert out["tombstoned"]["event_kind"] == "TOMBSTONE"

    code, out, _ = run("memory", "verify", "--root", str(memory_root))
    assert code == EXIT_OK
    assert out["integrity"]["redacted_count"] == 1

    code, out, _ = run(
        "memory",
        "show",
        "--root",
        str(memory_root),
        "--decision-id",
        DECISION_ID,
        "--as-of",
        BEFORE_REVIEW,
    )
    assert (out["active"]["redacted"], out["active"]["record"]) == (True, None)


def test_a_transfer_crosses_only_to_the_store_it_names(tmp_path: Path) -> None:
    source = init_memory(tmp_path / "claude-memory")
    target = init_memory(tmp_path / "codex-memory", store_id=CODEX_STORE_ID, family="codex")
    assert append(source, strategic_decision_payload())[0] == EXIT_OK

    envelope_path = source / "transfer.json"
    code, out, err = run(
        "memory",
        "export-transfer",
        "--root",
        str(source),
        "--decision-id",
        DECISION_ID,
        "--to-host-id",
        HOST_ID,
        "--to-agent-family",
        "codex",
        "--to-store-id",
        CODEX_STORE_ID,
        "--to-epoch",
        EPOCH,
        "--exported-by",
        "operator-alpha",
        "--out",
        str(envelope_path),
    )
    assert code == EXIT_OK, err
    assert out["destination_binding"]["store_id"] == CODEX_STORE_ID

    code, out, err = run(
        "memory", "import-transfer", "--root", str(target), "--envelope", str(envelope_path)
    )
    assert code == EXIT_OK, err
    assert out["imported"]["append"]["origin_kind"] == "FOREIGN_READ_ONLY"

    # Native and imported are listed separately, never merged.
    code, out, _ = run("memory", "list", "--root", str(target), "--as-of", BEFORE_REVIEW)
    assert (code, out["active"]) == (EXIT_OK, [])
    code, out, _ = run(
        "memory",
        "list",
        "--root",
        str(target),
        "--as-of",
        BEFORE_REVIEW,
        "--origin",
        "FOREIGN_READ_ONLY",
    )
    assert [entry["decision_id"] for entry in out["active"]] == [DECISION_ID]

    # Replaying the same envelope is refused, and the store does not move.
    code, _, err = run(
        "memory", "import-transfer", "--root", str(target), "--envelope", str(envelope_path)
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "transfer_refused"

    # And it never lands in a store it was not addressed to.
    other = init_memory(tmp_path / "other-memory", store_id="store-other", family="codex")
    code, _, err = run(
        "memory", "import-transfer", "--root", str(other), "--envelope", str(envelope_path)
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["reason"] == "destination_mismatch"


def test_a_tampered_store_reports_integrity_through_the_exit_code(memory_root: Path) -> None:
    assert append(memory_root, strategic_decision_payload())[0] == EXIT_OK
    database = memory_root / "decision-memory.sqlite3"
    assert database.is_file()

    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("DELETE FROM events WHERE seq = 1")
    finally:
        connection.close()

    code, out, _ = run("memory", "verify", "--root", str(memory_root))
    assert code == EXIT_INTEGRITY
    assert out["integrity"]["ok"] is False


def test_the_memory_surface_offers_no_way_to_select_or_authorise(memory_root: Path) -> None:
    """The parser is the proof: these flags do not exist and cannot be added."""
    for argv in (
        ("memory", "append", "--root", str(memory_root), "--selected-direction-id", "alpha"),
        ("memory", "show", "--root", str(memory_root), "--authorize"),
        ("memory", "list", "--root", str(memory_root), "--outcome", "success"),
    ):
        with pytest.raises(SystemExit) as exit_code:
            main(list(argv), stdout=StringIO(), stderr=StringIO())
        assert exit_code.value.code == 2


def test_admission_limits_are_machine_readable() -> None:
    code, out, _ = run("memory", "limits")
    assert code == EXIT_OK
    limits = out["admission"]
    assert limits["universal_secret_detection"] is False
    assert limits["required_sensitivity"] == "NON_SENSITIVE"
    assert "verdict" in limits["forbidden_field_names"]
    assert "private_key_block" in limits["credential_shapes"]


@pytest.mark.parametrize("depth", [33, 5000])
def test_deep_json_is_a_typed_cli_refusal(memory_root: Path, tmp_path: Path, depth: int) -> None:
    document = tmp_path / "deep.json"
    document.write_text(
        '{"revision":1,"nested":' + '{"nested":' * depth + "0" + "}" * (depth + 1),
        encoding="utf-8",
    )
    before = run("memory", "status", "--root", str(memory_root))
    code, _, err = run("memory", "append", "--root", str(memory_root), "--record", str(document))
    assert code == EXIT_REFUSED
    # CPython builds differ in how deep the JSON parser can go. If parsing
    # succeeds, admission must still report the exact application depth bound.
    if err["detail"].get("reason") == "RecursionError":
        assert err["error"] == "contract_violation"
        assert depth == 5000
    else:
        assert err["error"] == "decision_memory_violation"
        assert err["detail"]["max_nesting_depth"] == 32
    assert run("memory", "status", "--root", str(memory_root)) == before
    assert run("memory", "verify", "--root", str(memory_root))[0] == EXIT_OK

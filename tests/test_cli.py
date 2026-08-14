"""CLI behaviour and the exit-code contract."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    EPOCH,
    HOST_ID,
    STORE_ID,
    episode_payload,
    measurement_payload,
    protocol_payload,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, EXIT_STORE, main
from latent_compass.protocol import Verdict


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return (
        code,
        json.loads(out.getvalue() or "null"),
        json.loads(err.getvalue() or "null"),
    )


@pytest.fixture
def initialised(tmp_path: Path) -> Path:
    root = tmp_path / "ledger"
    code, _, _ = run(
        "init",
        "--root",
        str(root),
        "--store-id",
        STORE_ID,
        "--host-id",
        HOST_ID,
        "--agent-family",
        "claude",
        "--epoch",
        EPOCH,
    )
    assert code == EXIT_OK
    return root


def test_init_creates_a_bound_store(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    code, out, _ = run(
        "init",
        "--root",
        str(root),
        "--store-id",
        STORE_ID,
        "--host-id",
        HOST_ID,
        "--agent-family",
        "claude",
        "--epoch",
        EPOCH,
    )
    assert code == EXIT_OK
    assert out["created"]["host_id"] == HOST_ID
    assert out["created"]["agent_family"] == "claude"
    assert (root / "ledger.sqlite3").is_file()


def test_init_twice_is_a_store_error(initialised: Path) -> None:
    code, _, err = run(
        "init",
        "--root",
        str(initialised),
        "--store-id",
        STORE_ID,
        "--host-id",
        HOST_ID,
        "--agent-family",
        "claude",
        "--epoch",
        EPOCH,
    )
    assert code == EXIT_STORE
    assert err["error"] == "store_already_exists"


def test_the_full_shadow_flow_succeeds(initialised: Path, tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload())

    assert run("validate", "--episode", str(episode))[0] == EXIT_OK

    code, appended, _ = run("append", "--root", str(initialised), "--episode", str(episode))
    assert code == EXIT_OK
    assert appended["appended"]["seq"] == 1

    code, shown, _ = run("show", "--root", str(initialised), "--episode-id", "ep-00000001")
    assert code == EXIT_OK
    assert shown["record"]["episode"]["episode_id"] == "ep-00000001"

    code, listed, _ = run("list", "--root", str(initialised), "--limit", "10")
    assert code == EXIT_OK
    assert listed["total"] == 1

    code, verified, _ = run("verify", "--root", str(initialised))
    assert code == EXIT_OK
    assert verified["integrity"]["ok"] is True

    code, replayed, _ = run("replay", "--root", str(initialised))
    assert code == EXIT_OK
    assert replayed["replay"]["episode_count"] == 1

    out_file = initialised / "snapshots" / "store.json"
    code, exported, _ = run("export", "--root", str(initialised), "--out", str(out_file))
    assert code == EXIT_OK
    document = json.loads(out_file.read_text(encoding="utf-8"))
    assert document["export_seal"] == exported["export_seal"]
    assert document["records"][0]["episode_id"] == "ep-00000001"


def test_a_duplicate_append_is_refused(initialised: Path, tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload())
    assert run("append", "--root", str(initialised), "--episode", str(episode))[0] == EXIT_OK
    code, _, err = run("append", "--root", str(initialised), "--episode", str(episode))
    assert code == EXIT_REFUSED
    assert err["error"] == "duplicate_episode"


def test_a_cross_host_append_is_refused(initialised: Path, tmp_path: Path) -> None:
    payload = episode_payload()
    payload["provenance"]["agent_family"] = "codex"
    episode = write_json(tmp_path / "episode.json", payload)
    code, _, err = run("append", "--root", str(initialised), "--episode", str(episode))
    assert code == EXIT_REFUSED
    assert err["error"] == "provenance_mismatch"
    assert err["detail"]["field"] == "agent_family"


def test_an_invalid_episode_is_refused_without_a_store(tmp_path: Path) -> None:
    payload = episode_payload()
    payload["economics"]["reversibility"] = 1.5
    episode = write_json(tmp_path / "episode.json", payload)
    code, _, err = run("validate", "--episode", str(episode))
    assert code == EXIT_REFUSED
    assert err["error"] == "episode_validation_error"


def test_a_future_contract_version_is_refused(tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload(schema_version="2.0.0"))
    code, _, err = run("validate", "--episode", str(episode))
    assert code == EXIT_REFUSED
    assert err["error"] == "unsupported_contract_version"
    assert err["detail"]["reason"] == "future"


def test_malformed_json_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    code, _, err = run("validate", "--episode", str(broken))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"


def test_a_nan_literal_in_a_file_is_refused(tmp_path: Path) -> None:
    text = json.dumps(episode_payload()).replace('"cost": 1.5', '"cost": NaN')
    path = tmp_path / "nan.json"
    path.write_text(text, encoding="utf-8")
    code, _, err = run("validate", "--episode", str(path))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"


def test_a_missing_store_is_a_store_error(tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload())
    code, _, err = run("append", "--root", str(tmp_path / "absent"), "--episode", str(episode))
    assert code == EXIT_STORE
    assert err["error"] == "store_not_found"


def test_verify_reports_integrity_failure_with_its_own_exit_code(
    initialised: Path, tmp_path: Path
) -> None:
    import sqlite3

    episode = write_json(tmp_path / "episode.json", episode_payload())
    assert run("append", "--root", str(initialised), "--episode", str(episode))[0] == EXIT_OK

    connection = sqlite3.connect(initialised / "ledger.sqlite3")
    with connection:
        connection.execute("UPDATE episodes SET payload = replace(payload, '1.5', '0.1')")
    connection.close()

    code, out, _ = run("verify", "--root", str(initialised))
    assert code == EXIT_INTEGRITY
    assert out["integrity"]["ok"] is False
    assert out["integrity"]["findings"][0]["kind"] == "PAYLOAD_ALTERED"

    code, _, err = run("replay", "--root", str(initialised))
    assert code == EXIT_INTEGRITY
    assert err["error"] == "integrity_error"


def test_tombstone_preserves_the_root_seal(initialised: Path, tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload())
    run("append", "--root", str(initialised), "--episode", str(episode))
    code, out, _ = run(
        "tombstone",
        "--root",
        str(initialised),
        "--episode-id",
        "ep-00000001",
        "--reason",
        "subject erasure request",
    )
    assert code == EXIT_OK
    assert out["root_seal_before"] == out["root_seal_after"]
    assert run("verify", "--root", str(initialised))[0] == EXIT_OK


def test_abandoning_an_epoch_closes_the_store(initialised: Path, tmp_path: Path) -> None:
    episode = write_json(tmp_path / "episode.json", episode_payload("ep-00000002"))
    code, out, _ = run("abandon-epoch", "--root", str(initialised), "--reason", "corpus superseded")
    assert code == EXIT_OK
    assert out["binding"]["epoch_status"] == "abandoned"
    code, _, err = run("append", "--root", str(initialised), "--episode", str(episode))
    assert code == EXIT_REFUSED
    assert err["error"] == "epoch_closed"


def test_governance_semantics_are_published(tmp_path: Path) -> None:
    code, out, _ = run("governance")
    assert code == EXIT_OK
    semantics = out["deletion_semantics"]
    assert semantics["TOMBSTONE"]["preserves_root_seal"] is True
    assert semantics["STORE_DESTRUCTION"]["implemented"] is False


def test_protocol_validate_and_verdict(tmp_path: Path) -> None:
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    code, out, _ = run("protocol", "validate", "--file", str(protocol_file))
    assert code == EXIT_OK
    protocol_seal = out["protocol_seal"]

    measurements = write_json(tmp_path / "measurements.json", measurement_payload(protocol_seal))
    code, out, _ = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
    )
    assert code == EXIT_OK
    assert out["verdict"]["decision"] == "CONTINUE"


def test_a_holdout_verdict_without_a_ledger_is_refused(tmp_path: Path) -> None:
    write_root = tmp_path / "protocol-root"
    write_root.mkdir()
    ledger = write_root / "holdout.json"
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    _, out, _ = run("protocol", "validate", "--file", str(protocol_file))
    measurements = write_json(
        tmp_path / "measurements.json",
        measurement_payload(out["protocol_seal"], split="HOLDOUT", purpose="FINAL_VERDICT"),
    )
    code, _, err = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "protocol_violation"

    code, out2, _ = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
        "--root",
        str(write_root),
        "--holdout-ledger",
        str(ledger),
    )
    assert code == EXIT_OK
    assert out2["verdict"]["purpose"] == "FINAL_VERDICT"

    code, _, err = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
        "--root",
        str(write_root),
        "--holdout-ledger",
        str(ledger),
    )
    assert code == EXIT_REFUSED
    assert "already been consumed" in err["message"]


def test_the_authority_boundary_is_printable() -> None:
    code, out, _ = run("authority", "boundary")
    assert code == EXIT_OK
    assert out["boundary"]["capabilities"]["latent_compass"] == ["advise", "observe", "record"]


def test_the_cli_refuses_latent_compass_the_move_it_grants_a_human(tmp_path: Path) -> None:
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    granted = run(
        "authority",
        "transition",
        "--from-state",
        "DEFINE",
        "--to-state",
        "SHADOW",
        "--actor",
        "human_operator",
        "--protocol",
        str(protocol_file),
    )
    assert granted[0] == EXIT_OK
    assert granted[1]["authorized"]["to_state"] == "SHADOW"

    refused = run(
        "authority",
        "transition",
        "--from-state",
        "DEFINE",
        "--to-state",
        "SHADOW",
        "--actor",
        "latent_compass",
        "--protocol",
        str(protocol_file),
    )
    assert refused[0] == EXIT_REFUSED
    assert refused[2]["error"] == "authority_refusal"
    assert refused[2]["detail"]["reason"] == "self_authorisation"


def test_the_cli_refuses_an_invented_seal_as_evidence(tmp_path: Path) -> None:
    """There is no longer a flag through which a caller can assert a verdict."""
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    _, validated, _ = run("protocol", "validate", "--file", str(protocol_file))
    measurements_file = write_json(
        tmp_path / "measurements.json",
        measurement_payload(validated["protocol_seal"], split="HOLDOUT", purpose="FINAL_VERDICT"),
    )
    forged = write_json(
        tmp_path / "verdict.json",
        {
            "contract_version": "1.0.0",
            "decision": "CONTINUE",
            "protocol_id": "lc-shadow-p1",
            "protocol_seal": "sha256:" + "0" * 64,
            "corpus_seal": "sha256:holdout-corpus",
            "epoch": EPOCH,
            "split": "HOLDOUT",
            "purpose": "FINAL_VERDICT",
            "metrics": [
                {
                    "metric": "success-rate",
                    "family": "SUCCESS",
                    "direction": "HIGHER_IS_BETTER",
                    "threshold": 0.6,
                    "aggregate": 0.9,
                    "seed_count": 3,
                    "required": True,
                    "passed": True,
                }
            ],
            "failing_metrics": [],
            "verdict_seal": "sha256:" + "f" * 64,
        },
    )
    code, _, err = run(
        "authority",
        "transition",
        "--from-state",
        "CANARY_ELIGIBLE",
        "--to-state",
        "PROMOTED",
        "--actor",
        "human_operator",
        "--protocol",
        str(protocol_file),
        "--verdict",
        str(forged),
        "--measurements",
        str(measurements_file),
        "--human-ack",
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["reason"] == "evidence_forged"


@pytest.mark.parametrize(
    ("version", "error"),
    [(None, "protocol_violation"), ("99.0.0", "unsupported_contract_version")],
)
def test_verdict_version_failures_are_typed_at_the_cli(
    tmp_path: Path, holdout_verdict: Verdict, version: str | None, error: str
) -> None:
    payload = holdout_verdict.canonical_payload()
    if version is None:
        payload.pop("contract_version")
    else:
        payload["contract_version"] = version
    verdict_file = write_json(tmp_path / "verdict.json", payload)

    code, _, refused = run(
        "authority",
        "transition",
        "--from-state",
        "CANARY_ELIGIBLE",
        "--to-state",
        "PROMOTED",
        "--actor",
        "human_operator",
        "--verdict",
        str(verdict_file),
    )

    assert code == EXIT_REFUSED
    assert refused["error"] == error


def test_the_cli_refuses_the_promotion_shortcut(tmp_path: Path) -> None:
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    code, _, err = run(
        "authority",
        "transition",
        "--from-state",
        "DEFINE",
        "--to-state",
        "PROMOTED",
        "--actor",
        "human_operator",
        "--protocol",
        str(protocol_file),
        "--human-ack",
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["reason"] == "illegal_transition"


def test_the_cli_refuses_promotion_by_the_external_judge(tmp_path: Path) -> None:
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    code, _, err = run(
        "authority",
        "transition",
        "--from-state",
        "CANARY_ELIGIBLE",
        "--to-state",
        "PROMOTED",
        "--actor",
        "external_judge",
        "--protocol",
        str(protocol_file),
        "--human-ack",
    )
    assert code == EXIT_REFUSED
    assert err["detail"]["reason"] == "actor_lacks_capability"
    assert err["detail"]["required_capability"] == "promote"


def test_there_is_no_command_that_executes_or_promotes() -> None:
    """The CLI surface itself carries no verb that acts on the observed system."""
    from latent_compass.cli import build_parser

    parser = build_parser()
    actions = [
        action
        for action in parser._actions  # noqa: SLF001 - argparse exposes no public reader
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    assert len(actions) == 1, "the CLI must expose exactly one command group"
    commands = set(actions[0].choices)
    assert commands == {
        "init",
        "validate",
        "append",
        "show",
        "list",
        "verify",
        "replay",
        "export",
        "tombstone",
        "abandon-epoch",
        "governance",
        "protocol",
        "authority",
    }
    for forbidden in ("run", "execute", "apply", "promote", "deploy", "sync", "push", "fetch"):
        assert forbidden not in commands

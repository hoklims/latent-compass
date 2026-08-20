"""CLI behaviour and the exit-code contract."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
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
    pairwise_projection_payload,
    protocol_payload,
    write_json,
)
from latent_compass.cli import EXIT_INTEGRITY, EXIT_OK, EXIT_REFUSED, EXIT_STORE, main
from latent_compass.confined_io import write_new_file
from latent_compass.errors import ContractViolation
from latent_compass.pairwise_capture import load_judgeable_projection
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


def test_pairwise_capture_publishes_one_canonical_pre_action_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "capture-root"
    root.mkdir()
    source = write_json(tmp_path / "projection.json", pairwise_projection_payload())
    destination = root / "captures" / "decision-0001.json"

    code, out, err = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(destination),
    )

    assert code == EXIT_OK
    assert err is None
    assert out["captured_to"] == str(destination.resolve())
    assert out["decision_point_id"] == "decision-0001"
    assert out["candidate_count"] == 2
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document == load_judgeable_projection(pairwise_projection_payload()).canonical_payload()
    assert out["projection_seal"].startswith("sha256:")
    assert destination.read_bytes().endswith(b"\n")

    second_root = tmp_path / "second-capture-root"
    second_root.mkdir()
    second_destination = second_root / "same-decision.json"
    second = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(second_root),
        "--out",
        str(second_destination),
    )
    assert second[0] == EXIT_OK
    assert second[1]["projection_seal"] == out["projection_seal"]
    assert second_destination.read_bytes() == destination.read_bytes()


def test_pairwise_capture_refuses_contamination_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "capture-root"
    root.mkdir()
    payload = pairwise_projection_payload()
    payload["selected_direction_id"] = "direction-alpha"
    source = write_json(tmp_path / "contaminated.json", payload)
    destination = root / "captures" / "decision-0001.json"

    code, _, err = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(destination),
    )

    assert code == EXIT_REFUSED
    assert err["error"] == "pairwise_capture_violation"
    assert not destination.exists()
    assert not destination.parent.exists()


def test_pairwise_capture_refuses_overwrite_and_escape(tmp_path: Path) -> None:
    root = tmp_path / "capture-root"
    root.mkdir()
    source = write_json(tmp_path / "projection.json", pairwise_projection_payload())
    destination = root / "decision-0001.json"
    destination.write_text("sentinel", encoding="utf-8")

    overwrite = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(destination),
    )
    assert overwrite[0] == EXIT_REFUSED
    assert "refusing to overwrite" in overwrite[2]["message"]
    assert destination.read_text(encoding="utf-8") == "sentinel"

    outside = tmp_path / "outside.json"
    escape = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(outside),
    )
    assert escape[0] == EXIT_REFUSED
    assert not outside.exists()


def test_pairwise_capture_refuses_parent_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import latent_compass.cli as cli

    root = tmp_path / "capture-root"
    parent = root / "captures"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    source = write_json(tmp_path / "projection.json", pairwise_projection_payload())
    destination = parent / "decision-0001.json"
    original_read_json = cli._read_json  # noqa: SLF001

    def swap_parent_before_read(path: Path) -> object:
        shutil.rmtree(parent)
        parent.symlink_to(outside, target_is_directory=True)
        return original_read_json(path)

    monkeypatch.setattr(cli, "_read_json", swap_parent_before_read)
    code, _, err = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(destination),
    )

    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"
    assert not (outside / destination.name).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows parent handles pin namespace entries")
def test_confined_writer_blocks_parent_swap_while_handle_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import latent_compass.confined_io as confined_io

    root = tmp_path / "root"
    parent = root / "nested"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = confined_io._open_or_create_directory_windows  # noqa: SLF001
    swap_was_blocked = False

    def open_then_try_swap(parent_handle: int, name: str, path: Path, *, what: str) -> int:
        nonlocal swap_was_blocked
        handle = original(parent_handle, name, path, what=what)
        try:
            path.rename(outside / "stolen")
        except OSError:
            swap_was_blocked = True
        return handle

    monkeypatch.setattr(confined_io, "_open_or_create_directory_windows", open_then_try_swap)
    destination = parent / "result.json"
    write_new_file(root, destination, b'{"ok":true}\n', what="test destination")

    assert swap_was_blocked
    assert destination.read_bytes() == b'{"ok":true}\n'
    assert not (outside / "stolen" / destination.name).exists()


def test_confined_writer_refuses_root_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import latent_compass.confined_io as confined_io

    authority_parent = tmp_path / "authority"
    root = authority_parent / "root"
    root.mkdir(parents=True)
    moved_authority = tmp_path / "moved-authority"
    outside = tmp_path / "outside"
    (outside / "root").mkdir(parents=True)
    destination = root / "result.json"
    backend_name = "_write_windows" if os.name == "nt" else "_write_posix"
    original_backend = getattr(confined_io, backend_name)

    def swap_ancestor_before_open(
        backend_root: Path, relative: Path, data: bytes, *, what: str
    ) -> None:
        authority_parent.rename(moved_authority)
        authority_parent.symlink_to(outside, target_is_directory=True)
        original_backend(backend_root, relative, data, what=what)

    monkeypatch.setattr(confined_io, backend_name, swap_ancestor_before_open)
    with pytest.raises(ContractViolation):
        write_new_file(root, destination, b"must stay confined", what="test destination")

    assert not (outside / "root" / destination.name).exists()


def test_confined_writer_refuses_a_preexisting_dangling_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    destination = root / "result.json"
    try:
        destination.symlink_to(tmp_path / "missing.json")
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(ContractViolation, match="refusing to overwrite"):
        write_new_file(root, destination, b"attacker controlled", what="test destination")
    assert not (tmp_path / "missing.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path grammar")
@pytest.mark.parametrize(
    ("component", "reason"),
    [
        ("CON", "reserved_dos_name"),
        ("con.txt", "reserved_dos_name"),
        ("PRN.json", "reserved_dos_name"),
        ("COM9", "reserved_dos_name"),
        ("LPT1.log", "reserved_dos_name"),
        ("COM¹", "reserved_dos_name"),
        ("LPT³.txt", "reserved_dos_name"),
        ("result.json:payload", "invalid_or_stream_character"),
        ("trailing.", "trailing_dot_or_space"),
        ("trailing ", "trailing_dot_or_space"),
        ("control\x01name", "control_character"),
    ],
)
def test_confined_writer_refuses_ambiguous_windows_components(
    tmp_path: Path, component: str, reason: str
) -> None:
    root = tmp_path / "root"
    destination = root / component

    with pytest.raises(ContractViolation) as refusal:
        write_new_file(root, destination, b"must not exist", what="test destination")

    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == reason
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
@pytest.mark.parametrize("reserved_name", ["CON", "COM¹", "LPT³.txt"])
def test_pairwise_capture_refuses_reserved_name_as_a_typed_contract_violation(
    tmp_path: Path, reserved_name: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = write_json(tmp_path / "projection.json", pairwise_projection_payload())

    code, _, error = run(
        "pairwise",
        "capture",
        "--projection",
        str(source),
        "--root",
        str(root),
        "--out",
        str(root / reserved_name),
    )

    assert code == EXIT_REFUSED
    assert error["error"] == "contract_violation"
    assert error["detail"]["reason"] == "reserved_dos_name"
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows local-volume policy")
@pytest.mark.parametrize(
    "root",
    [Path(r"\\server\share\root"), Path(r"\\?\C:\latent-compass-root")],
)
def test_confined_writer_refuses_unc_and_device_namespaces(root: Path) -> None:
    with pytest.raises(ContractViolation) as refusal:
        write_new_file(root, root / "result.json", b"must not exist", what="test destination")

    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "unc_or_device_namespace"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-on-close cleanup")
def test_confined_writer_cleanup_failure_cannot_leave_payload_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import latent_compass.confined_io as confined_io

    root = tmp_path / "root"
    root.mkdir()
    destination = root / "winner.json"
    destination.write_bytes(b"winner")

    def cleanup_denied(_handle: int) -> None:
        raise PermissionError("simulated cleanup refusal")

    monkeypatch.setattr(confined_io, "_dispose_windows_file", cleanup_denied)
    with pytest.raises(ContractViolation) as refusal:
        write_new_file(root, destination, b"losing payload", what="test destination")

    assert destination.read_bytes() == b"winner"
    assert not list(root.glob(".lc-*.tmp"))
    assert any("FILE_DELETE_ON_CLOSE remains active" in note for note in refusal.value.__notes__)


def test_confined_writer_preserves_one_concurrent_winner(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    destination = root / "winner.json"
    payloads = [f'{{"winner":{index}}}\n'.encode() for index in range(8)]

    def publish(payload: bytes) -> bool:
        try:
            write_new_file(root, destination, payload, what="test destination")
        except ContractViolation:
            return False
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        outcomes = list(pool.map(publish, payloads))

    assert outcomes.count(True) == 1
    assert destination.read_bytes() in payloads
    assert not list(root.glob(".lc-*.tmp"))


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

    write_root = tmp_path / "new-protocol-root"
    destination = write_root / "nested" / "verdict.json"
    code, written, _ = run(
        "protocol",
        "verdict",
        "--protocol",
        str(protocol_file),
        "--measurements",
        str(measurements),
        "--root",
        str(write_root),
        "--out",
        str(destination),
    )
    assert code == EXIT_OK
    expected = (
        json.dumps(written["verdict"], indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    assert destination.read_bytes() == expected


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
        "REJECTED",
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
        "benchmark",
        "pairwise",
    }
    for forbidden in ("run", "execute", "apply", "promote", "deploy", "sync", "push", "fetch"):
        assert forbidden not in commands


def test_the_only_run_verb_acts_on_a_corpus_and_not_on_a_system() -> None:
    """``benchmark run`` executes baselines over recorded cases, nothing else.

    It is the one imperative verb on the whole surface, so it is worth stating
    what it can reach: four file paths and a corpus directory. There is no
    target, no host, no endpoint and no holdout ledger among its options.
    """
    from latent_compass.cli import build_parser

    parser = build_parser()
    groups = [
        action
        for action in parser._actions  # noqa: SLF001 - argparse exposes no public reader
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    benchmark = groups[0].choices["benchmark"]
    benchmark_groups = [
        action
        for action in benchmark._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    run_parser = benchmark_groups[0].choices["run"]
    options = {
        option
        for action in run_parser._actions  # noqa: SLF001
        for option in action.option_strings
    }
    assert options == {
        "-h",
        "--help",
        "--spec",
        "--protocol",
        "--manifest",
        "--corpus-dir",
        "--root",
        "--out",
    }


def test_pairwise_capture_has_only_local_pre_action_file_inputs() -> None:
    from latent_compass.cli import build_parser

    parser = build_parser()
    groups = [
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    pairwise = groups[0].choices["pairwise"]
    pairwise_groups = [
        action
        for action in pairwise._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    capture = pairwise_groups[0].choices["capture"]
    options = {
        option
        for action in capture._actions  # noqa: SLF001
        for option in action.option_strings
    }
    assert options == {"-h", "--help", "--projection", "--root", "--out"}

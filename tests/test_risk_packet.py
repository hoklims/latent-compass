"""The cross-cutting risk packet, exercised as behaviour rather than prose.

One section per invariant. Each hostile scenario is run against the real CLI or
the real store, and each oracle is chosen so that the naive implementation —
the one that merely looks correct — fails it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    EPOCH,
    HOST_ID,
    STORE_ID,
    episode_payload,
    protocol_payload,
    snapshot_tree,
    write_json,
)
from latent_compass.cli import EXIT_OK, EXIT_REFUSED, main
from latent_compass.episode import AgentFamily, load_episode
from latent_compass.errors import ContractViolation, ProvenanceMismatch
from latent_compass.ledger import LedgerStore

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "latent_compass"


def source_files() -> list[Path]:
    """Every module in the package, with a guard against scanning nothing.

    A ``rglob`` over a mistyped root returns an empty list, and every "the
    source contains no X" assertion below would then pass vacuously. The guard
    is what makes those assertions evidence.
    """
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    names = {path.name for path in files}
    assert names >= {
        "__init__.py",
        "authority.py",
        "canonical.py",
        "cli.py",
        "contracts.py",
        "episode.py",
        "errors.py",
        "governance.py",
        "ledger.py",
        "protocol.py",
    }, f"source scan found only {sorted(names)}"
    return files


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def full_flow(root: Path, episode: Path) -> None:
    """Init, append, read, verify, replay, export — the whole supported surface."""
    assert (
        run(
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
        )[0]
        == EXIT_OK
    )
    assert run("append", "--root", str(root), "--episode", str(episode))[0] == EXIT_OK
    assert run("show", "--root", str(root), "--episode-id", "ep-00000001")[0] == EXIT_OK
    assert run("list", "--root", str(root))[0] == EXIT_OK
    assert run("verify", "--root", str(root))[0] == EXIT_OK
    assert run("replay", "--root", str(root))[0] == EXIT_OK
    assert run("export", "--root", str(root))[0] == EXIT_OK


# ---------------------------------------------------------------------------
# Invariant 1 — latent compass stays purely advisory
# ---------------------------------------------------------------------------


def test_a_full_cli_flow_writes_only_inside_the_named_ledger_root(tmp_path: Path) -> None:
    """Containment, measured against the whole workspace rather than asserted."""
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "operator-notes.txt").write_text("do not touch", encoding="utf-8")
    (sentinel / "nested").mkdir()
    (sentinel / "nested" / "config.json").write_text('{"a": 1}', encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    episode = write_json(workspace / "episode.json", episode_payload())
    root = tmp_path / "ledger"

    sentinel_before = snapshot_tree(sentinel)
    assert len(sentinel_before) == 2
    before = snapshot_tree(tmp_path)
    full_flow(root, episode)
    after = snapshot_tree(tmp_path)

    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed, "the flow must actually have written something"
    outside = {path for path in changed if not path.startswith("ledger/")}
    assert outside == set(), f"the flow touched paths outside the ledger root: {sorted(outside)}"
    assert snapshot_tree(sentinel) == sentinel_before


def test_the_package_neither_opens_a_socket_nor_spawns_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traps are proved live before the flow runs, so the test cannot be vacuous."""
    tripped: list[str] = []

    def trap(name: str) -> Any:
        def _trap(*_args: object, **_kwargs: object) -> Any:
            tripped.append(name)
            raise AssertionError(f"latent-compass reached for {name}")

        return _trap

    monkeypatch.setattr(socket, "socket", trap("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", trap("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", trap("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", trap("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", trap("subprocess.run"))
    monkeypatch.setattr(os, "system", trap("os.system"))

    traps: tuple[tuple[str, Any], ...] = (
        ("socket.socket", socket.socket),
        ("subprocess.Popen", subprocess.Popen),
        ("os.system", os.system),
    )
    for name, call in traps:
        with pytest.raises(AssertionError, match="reached for"):
            call()
        assert tripped[-1] == name
    tripped.clear()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    episode = write_json(workspace / "episode.json", episode_payload())
    full_flow(tmp_path / "ledger", episode)
    assert tripped == []


@pytest.mark.parametrize(
    "forbidden",
    [
        "import socket",
        "import subprocess",
        "import urllib",
        "import http",
        "import requests",
        "import httpx",
        "urlopen",
        "Popen(",
    ],
)
def test_the_source_tree_imports_no_networking_or_process_machinery(forbidden: str) -> None:
    for path in source_files():
        assert forbidden not in path.read_text(encoding="utf-8"), (
            f"{path.name} references {forbidden}"
        )


def test_the_source_tree_references_no_external_judge_installation() -> None:
    """No connector, path or import reaching the deterministic judge.

    Needles are assembled at runtime so this module does not contain the
    literals it forbids, which would make the repository-wide leak scan in
    ``test_open_source.py`` match this file rather than a real leak.
    """
    judge = "sem" + "ctx"
    needles = (
        "c:\\" + judge,
        "c:/" + judge,
        f"import {judge}",
        f"from {judge}",
        "mainten" + "ence",
    )
    for path in source_files():
        text = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            assert needle not in text, f"{path.name} references {needle!r}"


def test_an_episode_cannot_ask_for_execution(tmp_path: Path) -> None:
    hostile = episode_payload()
    hostile["execute"] = {"command": "rm -rf /"}
    path = write_json(tmp_path / "hostile.json", hostile)
    code, _, err = run("validate", "--episode", str(path))
    assert code == EXIT_REFUSED
    assert err["error"] == "episode_validation_error"


# ---------------------------------------------------------------------------
# Invariant 2 — ambiguity fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        (
            "chained uncertainty",
            lambda p: p["decision"]["advisory"].update({"uncertainty": 0.95, "confidence": 0.99}),
        ),
        ("nan cost", lambda p: p["economics"].update({"cost": float("nan")})),
        (
            "infinite information gain",
            lambda p: p["economics"].update({"information_gain": float("inf")}),
        ),
        ("out of range reversibility", lambda p: p["economics"].update({"reversibility": 1.4})),
        ("negative cost", lambda p: p["economics"].update({"cost": -1.0})),
        ("future version", lambda p: p.update({"schema_version": "2.0.0"})),
        ("injected action", lambda p: p.update({"action": "promote"})),
        (
            "injected nested authority",
            lambda p: p["decision"]["advisory"].update({"authority": "execute"}),
        ),
        ("string for float", lambda p: p["economics"].update({"cost": "1.5"})),
    ],
)
def test_ambiguous_input_produces_no_append_and_no_executable_advice(
    tmp_path: Path, label: str, mutate: Any
) -> None:
    store = LedgerStore.create(
        tmp_path / "ledger",
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
    )
    try:
        before_seal, before_count = store.root_seal(), store.count()
        payload = episode_payload()
        mutate(payload)
        with pytest.raises(ContractViolation):
            store.append(load_episode(payload))
        assert (store.root_seal(), store.count()) == (before_seal, before_count), label
        assert store.verify().ok
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Invariant 3 — host stores stay separate
# ---------------------------------------------------------------------------


def test_a_refused_cross_host_import_leaves_both_stores_bit_identical(tmp_path: Path) -> None:
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude = LedgerStore.create(
        claude_root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
    )
    codex = LedgerStore.create(
        codex_root,
        store_id="store-codex",
        host_id="host-codex",
        agent_family=AgentFamily.CODEX,
        epoch=EPOCH,
    )
    try:
        claude.append(load_episode(episode_payload()))
        codex_payload = episode_payload(
            "ep-00000050", host_id="host-codex", store_id="store-codex", agent_family="codex"
        )
        codex.append(load_episode(codex_payload))

        state = {
            "claude": (claude.root_seal(), claude.count()),
            "codex": (codex.root_seal(), codex.count()),
        }

        with pytest.raises(ProvenanceMismatch):
            codex.append(load_episode(episode_payload("ep-00000051")))
        with pytest.raises(ProvenanceMismatch):
            claude.append(load_episode(codex_payload | {"episode_id": "ep-00000052"}))

        assert (claude.root_seal(), claude.count()) == state["claude"]
        assert (codex.root_seal(), codex.count()) == state["codex"]
        assert claude.root_seal() != codex.root_seal()
        assert claude.verify().ok
        assert codex.verify().ok
    finally:
        claude.close()
        codex.close()


def test_an_episode_without_a_host_identity_cannot_be_recorded(tmp_path: Path) -> None:
    store = LedgerStore.create(
        tmp_path / "ledger",
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
    )
    try:
        payload = episode_payload()
        del payload["provenance"]["host_id"]
        with pytest.raises(ContractViolation):
            store.append(load_episode(payload))
        assert store.count() == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Invariant 4 — the ledger stays atomic and replayable
# ---------------------------------------------------------------------------


def test_the_same_logical_content_replays_and_exports_identically(tmp_path: Path) -> None:
    def build(root: Path) -> tuple[str, str, str]:
        store = LedgerStore.create(
            root,
            store_id=STORE_ID,
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            epoch=EPOCH,
            clock=lambda: "2026-08-14T12:00:00Z",
        )
        try:
            for index in (1, 2, 3):
                store.append(
                    load_episode(episode_payload(f"ep-0000000{index}")),
                    clock=lambda: "2026-08-14T12:00:00Z",
                )
            replay = store.replay()
            return replay.root_seal, replay.replay_digest, store.export().export_seal
        finally:
            store.close()

    assert build(tmp_path / "a") == build(tmp_path / "b")


# ---------------------------------------------------------------------------
# Invariant 5 — governance and pre-registration stay unavoidable
# ---------------------------------------------------------------------------


def test_the_promotion_shortcut_is_refused_at_every_entry_point(tmp_path: Path) -> None:
    from latent_compass.authority import (
        Actor,
        LifecycleState,
        authorize_transition,
    )
    from latent_compass.errors import AuthorityRefusal
    from latent_compass.protocol import load_preregistration

    protocol = load_preregistration(protocol_payload())
    with pytest.raises(AuthorityRefusal):
        authorize_transition(
            from_state=LifecycleState.DEFINE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            human_acknowledged=True,
        )
    protocol_file = write_json(tmp_path / "protocol.json", protocol_payload())
    assert (
        run(
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
        )[0]
        == EXIT_REFUSED
    )


def test_the_package_runs_on_the_declared_python() -> None:
    assert sys.version_info[:2] == (3, 13)

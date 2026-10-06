from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Literal

import pytest

from latent_compass.canonical import seal
from latent_compass.errors import ContractViolation
from latent_compass.shadow_harness import (
    DEFAULT_CONFIG_NAME,
    ShadowHarnessConfig,
    default_store_root,
    load_shadow_config,
    main,
    process_hook_event,
)

NOW = "2026-09-20T00:00:00Z"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    return root


def _config(root: Path, *, host: str = "codex") -> ShadowHarnessConfig:
    family = "codex" if host == "codex" else "claude"
    return load_shadow_config(
        {
            "contract_version": "1.0.0",
            "enabled": True,
            "host_id": f"{host}-local",
            "agent_family": family,
            "projects": [
                {
                    "root": str(root),
                    "alias": "latent-compass-shadow",
                    "capabilities": [
                        {"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0},
                        {"capability_id": "functions.exec", "kind": "TOOL", "cost_ceiling": 0},
                    ],
                }
            ],
        }
    )


def _payload(root: Path, *, tool_name: str = "functions.exec") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(root),
        "session_id": "session-secret-identifier",
        "turn_id": "turn-secret-identifier",
        "model": "gpt-local",
        "permission_mode": "default",
        "tool_name": tool_name,
        "prompt": "PROMPT-MUST-NOT-BE-STORED",
        "tool_input": {"command": "SECRET-COMMAND-MUST-NOT-BE-STORED"},
        "tool_response": "SECRET-OUTPUT-MUST-NOT-BE-STORED",
        "transcript_path": "C:/private/transcript.jsonl",
    }


def _prime_source(
    root: Path,
    *,
    host: Literal["codex", "claude"],
    config: ShadowHarnessConfig,
    store: Path,
    marker: str,
) -> str:
    digest = seal("test.shadow.source.v1", {"marker": marker})
    payload = _payload(root)
    payload.update(
        {
            "hook_event_name": "SessionStart",
            "source_declaration_digest": digest,
            "source_observed_at": NOW,
        }
    )
    refreshed = process_hook_event(
        payload,
        host=host,
        config=config,
        store_root=store,
        now=NOW,
    )
    assert refreshed is not None
    return digest


def test_store_roots_are_host_local_and_separate(tmp_path: Path) -> None:
    assert default_store_root("codex", tmp_path) == tmp_path / ".codex" / "latent-compass-shadow"
    assert default_store_root("claude", tmp_path) == tmp_path / ".claude" / "latent-compass-shadow"
    assert default_store_root("codex", tmp_path) != default_store_root("claude", tmp_path)


@pytest.mark.parametrize("alias", ["CON", "foo:bar"])
def test_project_alias_must_be_a_portable_storage_component(repository: Path, alias: str) -> None:
    payload = _config(repository).canonical_payload()
    projects = payload["projects"]
    assert isinstance(projects, list)
    projects[0]["alias"] = alias

    with pytest.raises(ContractViolation):
        load_shadow_config(payload)


def test_project_aliases_are_unique_under_windows_case_identity(
    repository: Path, tmp_path: Path
) -> None:
    payload = _config(repository).canonical_payload()
    projects = payload["projects"]
    assert isinstance(projects, list)
    projects[0]["alias"] = "Foo"
    projects.append({**projects[0], "root": str(tmp_path / "second"), "alias": "foo"})

    with pytest.raises(ContractViolation):
        load_shadow_config(payload)


def test_known_tool_yields_non_authoritative_advice_without_sensitive_content(
    repository: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    config = _config(repository)
    _prime_source(repository, host="codex", config=config, store=store, marker="one")

    record = process_hook_event(
        _payload(repository), host="codex", config=config, store_root=store, now=NOW
    )

    assert record is not None
    decision = record["route_decision"]
    assert isinstance(decision, dict)
    assert decision["verdict"] == "ADVICE"
    assert decision["execution_authority"] is False
    assert decision["empirical_claim"] is False
    persisted = next((store / "events").rglob("*.json")).read_text(encoding="utf-8")
    assert "PROMPT-MUST-NOT-BE-STORED" not in persisted
    assert "SECRET-COMMAND-MUST-NOT-BE-STORED" not in persisted
    assert "SECRET-OUTPUT-MUST-NOT-BE-STORED" not in persisted
    assert "transcript.jsonl" not in persisted
    assert "session-secret-identifier" not in persisted
    assert "turn-secret-identifier" not in persisted
    assert '"tool_name":"functions.exec"' in persisted


def test_nested_project_routes_to_most_specific_registration(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    capabilities = [{"capability_id": "functions.exec", "kind": "TOOL", "cost_ceiling": 0}]
    config = load_shadow_config(
        {
            "contract_version": "1.0.0",
            "enabled": True,
            "host_id": "codex-local",
            "agent_family": "codex",
            "projects": [
                {"root": str(parent), "alias": "parent", "capabilities": capabilities},
                {"root": str(child), "alias": "child", "capabilities": capabilities},
            ],
        }
    )
    store = tmp_path / "store"
    _prime_source(child, host="codex", config=config, store=store, marker="child")

    record = process_hook_event(
        _payload(child), host="codex", config=config, store_root=store, now=NOW
    )

    assert record is not None
    assert record["project_alias"] == "child"
    assert next((store / "events" / "child").rglob("*.json")).is_file()
    assert not (store / "events" / "parent").exists()


def test_unknown_tool_abstains_without_affecting_the_host(repository: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    config = _config(repository)
    _prime_source(repository, host="codex", config=config, store=store, marker="one")
    record = process_hook_event(
        _payload(repository, tool_name="unconfigured.tool"),
        host="codex",
        config=config,
        store_root=store,
        now=NOW,
    )

    assert record is not None
    decision = record["route_decision"]
    assert isinstance(decision, dict)
    assert decision["verdict"] == "ABSTAIN"
    assert decision["abstain_reason"] == "CAPABILITY_MISSING"
    assert decision["execution_authority"] is False


def test_source_declaration_changes_when_tracked_content_changes(
    repository: Path, tmp_path: Path
) -> None:
    config = _config(repository)
    store = tmp_path / "store"
    first_digest = _prime_source(repository, host="codex", config=config, store=store, marker="one")
    first = process_hook_event(
        _payload(repository),
        host="codex",
        config=config,
        store_root=store,
        now=NOW,
    )
    (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
    second_digest = seal("test.shadow.source.v1", {"marker": "two"})
    refresh_payload = _payload(repository)
    refresh_payload.update(
        {
            "hook_event_name": "PostToolUse",
            "source_declaration_digest": second_digest,
            "source_observed_at": "2026-09-20T00:00:01Z",
        }
    )
    refreshed = process_hook_event(
        refresh_payload,
        host="codex",
        config=config,
        store_root=store,
        now="2026-09-20T00:00:01Z",
    )
    second = process_hook_event(
        _payload(repository),
        host="codex",
        config=config,
        store_root=store,
        now="2026-09-20T00:00:02Z",
    )

    assert first is not None
    assert refreshed is not None
    assert second is not None
    assert first["source_declaration_digest"] == first_digest
    assert second["source_declaration_digest"] == second_digest
    assert second["source_observed_at"] == "2026-09-20T00:00:01Z"


def test_hook_entrypoint_is_silent_and_fail_open_on_invalid_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_path = default_store_root("codex", home) / DEFAULT_CONFIG_NAME
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    out, err = StringIO(), StringIO()

    code = main(
        ["--host", "codex", "--home", str(home), "--now", NOW],
        stdin=StringIO("not-json"),
        stdout=out,
        stderr=err,
    )

    assert code == 0
    assert out.getvalue() == ""
    assert "not-json" not in err.getvalue()


def test_hook_entrypoint_ignores_projects_not_in_its_allowlist(
    repository: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = default_store_root("codex", home)
    store.mkdir(parents=True)
    store.joinpath(DEFAULT_CONFIG_NAME).write_text(
        json.dumps(_config(repository).canonical_payload()), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = _payload(elsewhere)

    code = main(
        ["--host", "codex", "--home", str(home), "--now", NOW],
        stdin=StringIO(json.dumps(payload)),
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert code == 0
    assert not (store / "events").exists()


def test_collector_refuses_linked_source_cache_parent_without_outside_read_or_write(
    repository: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside-source"
    outside.mkdir()
    outside_cache = outside / "latent-compass-shadow.json"
    outside_cache.write_text(
        json.dumps(
            {
                "source_declaration_digest": seal("test.shadow.source.v1", {"outside": True}),
                "source_observed_at": NOW,
            }
        ),
        encoding="utf-8",
    )
    (store / "source").symlink_to(outside, target_is_directory=True)
    before = outside_cache.read_bytes()

    with pytest.raises(ContractViolation):
        process_hook_event(
            _payload(repository),
            host="codex",
            config=_config(repository),
            store_root=store,
            now=NOW,
        )

    assert outside_cache.read_bytes() == before
    assert not (store / "events").exists()


def test_collector_refuses_linked_events_parent_without_outside_write(
    repository: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    config = _config(repository)
    _prime_source(repository, host="codex", config=config, store=store, marker="one")
    outside = tmp_path / "outside-events"
    outside.mkdir()
    (store / "events").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractViolation):
        process_hook_event(
            _payload(repository),
            host="codex",
            config=config,
            store_root=store,
            now=NOW,
        )

    assert list(outside.iterdir()) == []


def test_hook_entrypoint_stays_fail_open_when_event_store_is_linked(
    repository: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = default_store_root("codex", home)
    config = _config(repository)
    store.mkdir(parents=True)
    (store / DEFAULT_CONFIG_NAME).write_text(
        json.dumps(config.canonical_payload()), encoding="utf-8"
    )
    _prime_source(repository, host="codex", config=config, store=store, marker="one")
    outside = tmp_path / "outside-events"
    outside.mkdir()
    (store / "events").symlink_to(outside, target_is_directory=True)
    stderr = StringIO()

    code = main(
        ["--host", "codex", "--home", str(home), "--now", NOW],
        stdin=StringIO(json.dumps(_payload(repository))),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 0
    assert "fail-open" in stderr.getvalue()
    assert list(outside.iterdir()) == []


def test_hook_entrypoint_fails_open_for_linked_config_leaf(
    repository: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = default_store_root("codex", home)
    store.mkdir(parents=True)
    outside = tmp_path / "outside-config.json"
    outside.write_text(json.dumps(_config(repository).canonical_payload()), encoding="utf-8")
    (store / DEFAULT_CONFIG_NAME).symlink_to(outside)
    stderr = StringIO()

    code = main(
        ["--host", "codex", "--home", str(home), "--now", NOW],
        stdin=StringIO(json.dumps(_payload(repository))),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 0
    assert "fail-open" in stderr.getvalue()
    assert not (store / "events").exists()

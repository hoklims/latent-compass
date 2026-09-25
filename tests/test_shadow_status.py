from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

import latent_compass.shadow_status as shadow_status
from latent_compass.canonical import seal
from latent_compass.shadow_status import Host, inspect_host, main, render_text


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _install_fixture(
    home: Path,
    project: Path,
    *,
    host: str = "codex",
    wrapper_path: Path | None = None,
) -> Path:
    settings = home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")
    runtime = home / "runtime" / "python.exe"
    wrapper = wrapper_path or home / "runtime" / "latent-compass-shadow-hook.py"
    command = (
        f"& '{runtime}' '{wrapper}' --host {host}"
        if host == "codex"
        else f'"{runtime}" "{wrapper}" --host {host}'
    )
    matchers = {
        "codex": {
            "SessionStart": "startup|resume|clear",
            "PreToolUse": (
                "^(?:apply_patch|functions\\.exec|functions\\.wait|"
                "view_image|web\\.run|write_stdin)$"
            ),
            "PostToolUse": (
                "Write|Edit|MultiEdit|NotebookEdit|apply_patch|ApplyPatch|functions\\.exec"
            ),
        },
        "claude": {
            "SessionStart": "startup|resume",
            "PreToolUse": (
                "^(?:Agent|Bash|Edit|Glob|Grep|MultiEdit|NotebookEdit|"
                "Read|WebFetch|WebSearch|Write)$"
            ),
            "PostToolUse": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        },
    }
    hooks = {}
    for event, matcher in matchers[host].items():
        hooks[event] = [
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "async": True,
                        "timeout": 10,
                    }
                ],
            }
        ]
    _write_json(settings, {"hooks": hooks})
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"fixture")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("# fixture\n", encoding="utf-8")
    store = home / f".{host}" / "latent-compass-shadow"
    _write_json(
        store / "ownership.json",
        {"schema_version": 1, "host": host, "command": command},
    )
    _write_json(
        store / "config.json",
        {
            "contract_version": "1.0.0",
            "enabled": True,
            "host_id": f"{host}-local",
            "agent_family": host,
            "projects": [
                {
                    "root": str(project),
                    "alias": "project-alpha",
                    "capabilities": [{"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0}],
                    "remaining_budget": 0,
                }
            ],
        },
    )
    return store


def _event(store: Path, *, verdict: str, observed_at: str, session: str) -> None:
    destination = store / "events" / "project-alpha" / session / f"{observed_at[-3:-1]}.json"
    record: dict[str, object] = {
        "contract_version": "1.0.0",
        "host": "codex",
        "host_id": "codex-local",
        "project_alias": "project-alpha",
        "hook_event_name": "PreToolUse",
        "observed_at": observed_at,
        "session_seal": f"sha256:{session:0<64}",
        "turn_seal": f"sha256:{'3':0<64}",
        "model": "gpt-local",
        "permission_mode": "default",
        "tool_name": "Read",
        "source_declaration_digest": f"sha256:{'4':0<64}",
        "source_observed_at": observed_at,
        "route_decision": {
            "verdict": verdict,
            "execution_authority": False,
            "empirical_claim": False,
        },
        "shadow_only": True,
        "host_influenced": False,
        "content_recorded": False,
    }
    record["record_seal"] = seal("shadow.harness.record.v1", record)
    _write_json(destination, record)


def test_status_reports_registered_project_and_sanitised_counts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    _event(store, verdict="ADVICE", observed_at="2026-09-20T10:00:00Z", session="1")
    _event(store, verdict="ABSTAIN", observed_at="2026-09-20T11:00:00Z", session="2")

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "OBSERVING"
    assert report["hooks_present"] == 3
    assert report["runtime_present"] is True
    assert report["project_registered"] is True
    assert report["project_alias"] == "project-alpha"
    assert report["event_count"] == 2
    assert report["session_count"] == 2
    assert report["last_observed_at"] == "2026-09-20T11:00:00Z"
    assert report["verdicts"] == {"ABSTAIN": 1, "ADVICE": 1}
    rendered = render_text(
        {
            "project_root": str(project),
            "hosts": [report],
            "shadow_only": True,
            "execution_authority": False,
            "content_recorded": False,
        }
    )
    assert "Authority: none" in rendered


def test_status_distinguishes_unregistered_and_no_observations(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _install_fixture(home, project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert inspect_host(home=home, host="codex", project_root=project)["status"] == (
        "NO_OBSERVATIONS"
    )
    assert inspect_host(home=home, host="codex", project_root=elsewhere)["status"] == (
        "PROJECT_NOT_REGISTERED"
    )


def test_invalid_event_degrades_without_exposing_its_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    bad = store / "events" / "project-alpha" / "session" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("SECRET INVALID CONTENT", encoding="utf-8")

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "DEGRADED"
    assert report["invalid_event_count"] == 1
    assert "SECRET" not in json.dumps(report)


def test_sensitive_value_in_consumed_field_is_rejected_not_displayed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    _event(store, verdict="SECRET-VERDICT", observed_at="2026-09-20T10:00:00Z", session="1")

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "DEGRADED"
    assert report["event_count"] == 0
    assert "SECRET" not in render_text({"project_root": str(project), "hosts": [report]})


def test_impossible_timestamp_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    _event(store, verdict="ADVICE", observed_at="2026-99-20T10:00:00Z", session="1")

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "DEGRADED"
    assert report["event_count"] == 0


def test_inert_marker_commands_do_not_count_as_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _install_fixture(home, project)
    settings = home / ".codex" / "hooks.json"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    runtime = home / "runtime" / "python.exe"
    wrapper = home / "runtime" / "latent-compass-shadow-hook.py"
    for groups in payload["hooks"].values():
        groups[0]["hooks"][0]["command"] = f"echo '{runtime}' '{wrapper}' --host codex"
    _write_json(settings, payload)

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "HOOKS_MISSING"
    assert report["hooks_present"] == 0


def test_missing_wrapper_or_host_suffix_cannot_look_active(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _install_fixture(home, project)
    wrapper = home / "runtime" / "latent-compass-shadow-hook.py"
    wrapper.unlink()

    missing = inspect_host(home=home, host="codex", project_root=project)
    assert missing["status"] == "RUNTIME_MISSING"

    wrapper.write_text("# fixture\n", encoding="utf-8")
    settings = home / ".codex" / "hooks.json"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    for groups in payload["hooks"].values():
        groups[0]["hooks"][0]["command"] += "-evil"
    _write_json(settings, payload)
    suffix = inspect_host(home=home, host="codex", project_root=project)
    assert suffix["status"] == "HOOKS_MISSING"


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_status_accepts_exact_owned_custom_wrapper(tmp_path: Path, host: str) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    custom_wrapper = tmp_path / "custom" / f"{host}-observer.py"
    _install_fixture(home, project, host=host, wrapper_path=custom_wrapper)

    report = inspect_host(home=home, host=cast(Host, host), project_root=project)

    assert report["status"] == "NO_OBSERVATIONS"
    assert report["hooks_present"] == 3
    assert report["wrapper_present"] is True


def test_status_rejects_foreign_command_with_owned_wrapper_basename(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _install_fixture(home, project)
    settings = home / ".codex" / "hooks.json"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    foreign = tmp_path / "foreign" / "latent-compass-shadow-hook.py"
    foreign.parent.mkdir()
    foreign.write_text("# foreign\n", encoding="utf-8")
    for groups in payload["hooks"].values():
        groups[0]["hooks"][0]["command"] = f"& '{Path(__file__)}' '{foreign}' --host codex"
    _write_json(settings, payload)

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "HOOKS_MISSING"
    assert report["hooks_present"] == 0


def test_configuration_and_records_are_bound_to_the_reported_host(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    config_path = store / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent_family"] = "claude"
    _write_json(config_path, config)
    assert inspect_host(home=home, host="codex", project_root=project)["status"] == (
        "SHADOW_CONFIGURATION_INVALID"
    )

    config["agent_family"] = "codex"
    _write_json(config_path, config)
    _event(store, verdict="ADVICE", observed_at="2026-09-20T10:00:00Z", session="1")
    event = next((store / "events").rglob("*.json"))
    record = json.loads(event.read_text(encoding="utf-8"))
    record["host"] = "claude"
    unsigned = {key: value for key, value in record.items() if key != "record_seal"}
    record["record_seal"] = seal("shadow.harness.record.v1", unsigned)
    _write_json(event, record)
    report = inspect_host(home=home, host="codex", project_root=project)
    assert report["status"] == "DEGRADED"
    assert report["event_count"] == 0


def test_oversized_event_is_bounded_and_degrades(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    event = store / "events" / "project-alpha" / "session" / "huge.json"
    event.parent.mkdir(parents=True)
    event.write_bytes(b"x" * (1_048_576 + 1))

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "DEGRADED"
    assert report["invalid_event_count"] == 1


def test_total_discovery_is_bounded_and_marked_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = _install_fixture(home, project)
    events = store / "events" / "project-alpha" / "session"
    events.mkdir(parents=True)
    for index in range(5):
        (events / f"noise-{index}.txt").write_text("noise", encoding="utf-8")
    monkeypatch.setattr(shadow_status, "MAX_EVENT_FILES", 3)

    report = inspect_host(home=home, host="codex", project_root=project)

    assert report["status"] == "DEGRADED"
    assert report["truncated"] is True


def test_cli_supports_human_and_json_output(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _install_fixture(home, project, host="claude")

    human = StringIO()
    assert (
        main(
            ["--host", "claude", "--home", str(home), "--project-root", str(project)],
            stdout=human,
        )
        == 0
    )
    assert "Claude: NO_OBSERVATIONS" in human.getvalue()

    machine = StringIO()
    assert (
        main(
            [
                "--host",
                "claude",
                "--home",
                str(home),
                "--project-root",
                str(project),
                "--json",
            ],
            stdout=machine,
        )
        == 0
    )
    payload = json.loads(machine.getvalue())
    assert payload["content_recorded"] is False
    assert payload["execution_authority"] is False
    assert payload["host_influenced"] is False

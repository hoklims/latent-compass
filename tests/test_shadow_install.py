from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import latent_compass.shadow_harness as shadow_harness
from latent_compass.shadow_harness import load_shadow_config
from latent_compass.shadow_install import (
    _command,
    _merged_host_config,
    _without_project,
    host_status,
    install_shadow_hooks,
    main,
    remove_shadow_hooks,
)
from latent_compass.shadow_status import Host


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _commands(path: Path, event: str) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in payload["hooks"][event]
        for hook in group.get("hooks", [])
        if "latent-compass-shadow-hook.py" in hook.get("command", "")
        or "latent_compass.shadow_hook" in hook.get("command", "")
    ]


def _shadow_handlers(path: Path, event: str) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        hook
        for group in payload["hooks"][event]
        for hook in group.get("hooks", [])
        if "latent-compass-shadow-hook.py" in hook.get("command", "")
        or "latent_compass.shadow_hook" in hook.get("command", "")
    ]


def test_installer_is_idempotent_and_keeps_host_stores_separate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime" / "python.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"fixture")
    hook_script = tmp_path / "runtime" / "latent-compass-shadow-hook.py"
    hook_script.write_text("# fixture\n", encoding="utf-8")
    codex_hooks = home / ".codex" / "hooks.json"
    claude_settings = home / ".claude" / "settings.json"
    _write(codex_hooks, {"hooks": {"PreToolUse": []}})
    _write(claude_settings, {"permissions": {"deny": []}, "hooks": {"PreToolUse": []}})

    first = install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        hook_script=hook_script,
        project_root=project,
        backup_tag="test",
    )
    second = install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        hook_script=hook_script,
        project_root=project,
        backup_tag="test-two",
    )

    assert first["changed"] is True
    assert second["changed"] is False
    for event in ("SessionStart", "PreToolUse", "PostToolUse"):
        assert len(_commands(codex_hooks, event)) == 1
        assert len(_commands(claude_settings, event)) == 1
        for path in (codex_hooks, claude_settings):
            assert _shadow_handlers(path, event) == [
                {
                    "type": "command",
                    "command": _commands(path, event)[0],
                    "async": True,
                    "timeout": 10,
                }
            ]
    codex_config = json.loads(
        (home / ".codex" / "latent-compass-shadow" / "config.json").read_text("utf-8")
    )
    claude_config = json.loads(
        (home / ".claude" / "latent-compass-shadow" / "config.json").read_text("utf-8")
    )
    assert codex_config["agent_family"] == "codex"
    assert claude_config["agent_family"] == "claude"
    assert codex_config["projects"][0]["root"] == claude_config["projects"][0]["root"]
    assert codex_config["projects"][0]["alias"] == claude_config["projects"][0]["alias"]
    assert (
        codex_config["projects"][0]["capabilities"] != claude_config["projects"][0]["capabilities"]
    )
    assert codex_config["host_id"] != claude_config["host_id"]
    assert (home / ".codex" / "hooks.json.bak-latent-compass-test").is_file()
    assert (home / ".claude" / "settings.json.bak-latent-compass-test").is_file()


def test_removal_only_removes_latent_compass_entries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime" / "python.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"fixture")
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "existing"}]}
            ]
        }
    }
    _write(home / ".codex" / "hooks.json", existing)
    _write(home / ".claude" / "settings.json", existing)
    install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        project_root=project,
        backup_tag="one",
    )

    result = remove_shadow_hooks(home=home, backup_tag="remove")

    assert result["changed"] is True
    for path in (home / ".codex" / "hooks.json", home / ".claude" / "settings.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
        assert payload["hooks"].get("SessionStart", []) == []
        assert payload["hooks"].get("PostToolUse", []) == []


def test_dry_run_reports_exact_plan_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    stdout = StringIO()

    code = main(
        [
            "install",
            "--host",
            "codex",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--project-alias",
            "project-alpha",
            "--dry-run",
            "--json",
        ],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 0
    assert report["dry_run"] is True
    assert report["changed"] is True
    assert [item["action"] for item in report["files"]] == [
        "create",
        "update",
        "create",
        "create",
    ]
    assert hooks.read_bytes() == before
    assert not (home / ".codex" / "latent-compass-shadow").exists()


def test_install_derives_valid_stable_alias_for_arbitrary_directory_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "projet avec espace é"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    stdout = StringIO()

    code = main(
        [
            "install",
            "--host",
            "codex",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--json",
        ],
        stdout=stdout,
    )

    assert code == 0
    config = json.loads(
        (home / ".codex" / "latent-compass-shadow" / "config.json").read_text("utf-8")
    )
    alias = config["projects"][0]["alias"]
    assert alias.startswith("projet-avec-espace-")
    assert len(alias) <= 127


def test_preflight_refuses_all_hosts_before_any_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    codex = home / ".codex" / "hooks.json"
    claude = home / ".claude" / "settings.json"
    _write(codex, {"hooks": {"PreToolUse": []}})
    claude.parent.mkdir(parents=True)
    claude.write_text("not-json", encoding="utf-8")
    before = codex.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="project-alpha",
        backup_tag="collision",
    )

    assert result["conflicts"]
    assert codex.read_bytes() == before
    assert not (home / ".codex" / "latent-compass-shadow").exists()


def test_install_preserves_ownership_shaped_foreign_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    foreign_command = _command(
        "codex",
        Path(sys.executable),
        tmp_path / "foreign" / "latent-compass-shadow-hook.py",
    )
    foreign_group = {
        "matcher": (
            "^(?:apply_patch|functions\\.exec|functions\\.wait|view_image|web\\.run|write_stdin)$"
        ),
        "hooks": [
            {
                "type": "command",
                "command": foreign_command,
                "async": True,
                "timeout": 10,
            }
        ],
    }
    _write(
        hooks,
        {"hooks": {"PreToolUse": [foreign_group]}},
    )

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="project-alpha",
        backup_tag="collision",
        hosts=("codex",),
    )

    assert result["conflicts"] == []
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    assert payload["hooks"]["PreToolUse"][0] == foreign_group
    assert len(payload["hooks"]["PreToolUse"]) == 2
    assert _commands(hooks, "PreToolUse") == [
        foreign_command,
        payload["hooks"]["PreToolUse"][1]["hooks"][0]["command"],
    ]

    removed = remove_shadow_hooks(
        home=home,
        backup_tag="remove",
        hosts=("codex",),
    )

    assert removed["conflicts"] == []
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    assert payload["hooks"]["PreToolUse"] == [foreign_group]


def test_apply_refuses_backup_collision_before_any_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    backup = hooks.with_name("hooks.json.bak-latent-compass-fixed")
    backup.write_text("existing backup\n", encoding="utf-8")
    before = hooks.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="project-alpha",
        backup_tag="fixed",
        hosts=("codex",),
    )

    assert result["dry_run"] is False
    assert result["conflicts"] == [{"code": "backup_collision", "path": str(backup)}]
    assert hooks.read_bytes() == before
    assert not (home / ".codex" / "latent-compass-shadow").exists()


def test_dry_run_reports_backup_collision_before_apply(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    backup = hooks.with_name("hooks.json.bak-latent-compass-fixed")
    backup.write_text("existing backup\n", encoding="utf-8")
    before = hooks.read_bytes()
    stdout = StringIO()

    code = main(
        [
            "install",
            "--host",
            "codex",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--project-alias",
            "project",
            "--backup-tag",
            "fixed",
            "--dry-run",
            "--json",
        ],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"] == [{"code": "backup_collision", "path": str(backup)}]
    assert hooks.read_bytes() == before
    assert not (home / ".codex" / "latent-compass-shadow").exists()


def test_project_removal_preserves_other_registration_and_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    runtime = Path(sys.executable)
    install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        project_root=first,
        project_alias="first",
        backup_tag="first",
        hosts=("codex",),
    )
    install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        project_root=second,
        project_alias="second",
        backup_tag="second",
        hosts=("codex",),
    )

    result = remove_shadow_hooks(
        home=home,
        backup_tag="remove-first",
        hosts=("codex",),
        project_alias="first",
    )

    assert result["changed"] is True
    config = json.loads(
        (home / ".codex" / "latent-compass-shadow" / "config.json").read_text("utf-8")
    )
    assert [project["alias"] for project in config["projects"]] == ["second"]
    for event in ("SessionStart", "PreToolUse", "PostToolUse"):
        assert len(_commands(hooks, event)) == 1


def test_removal_uses_persisted_ownership_for_custom_wrapper(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    custom_wrapper = tmp_path / "custom" / "latent-compass-shadow-hook.py"
    custom_wrapper.parent.mkdir()
    custom_wrapper.write_text("# custom fixture\n", encoding="utf-8")

    installed = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_wrapper,
        project_root=project,
        project_alias="custom",
        backup_tag="custom",
        hosts=("codex",),
    )
    removed = remove_shadow_hooks(
        home=home,
        backup_tag="remove-custom",
        hosts=("codex",),
    )

    assert installed["conflicts"] == []
    assert removed["conflicts"] == []
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    for event in ("SessionStart", "PreToolUse", "PostToolUse"):
        assert payload["hooks"][event] == []
    assert not (home / ".codex" / "latent-compass-shadow" / "ownership.json").exists()


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_interpreter_change_migrates_one_owned_hook_per_event(tmp_path: Path, host: Host) -> None:
    home = tmp_path / host
    project = tmp_path / "project"
    project.mkdir()
    settings = home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")
    _write(settings, {"hooks": {"PreToolUse": []}})
    runtime_one = tmp_path / "runtime-one" / "python.exe"
    runtime_two = tmp_path / "runtime-two" / "python.exe"
    for runtime in (runtime_one, runtime_two):
        runtime.parent.mkdir()
        runtime.write_bytes(b"fixture")

    first = install_shadow_hooks(
        home=home,
        runtime_python=runtime_one,
        project_root=project,
        project_alias="project",
        backup_tag="runtime-one",
        hosts=(host,),
    )
    second = install_shadow_hooks(
        home=home,
        runtime_python=runtime_two,
        project_root=project,
        project_alias="project",
        backup_tag="runtime-two",
        hosts=(host,),
    )

    assert first["conflicts"] == []
    assert second["conflicts"] == []
    for event in ("SessionStart", "PreToolUse", "PostToolUse"):
        commands = _commands(settings, event)
        assert len(commands) == 1
        assert str(runtime_two) in commands[0] or runtime_two.as_posix() in commands[0]
        assert str(runtime_one) not in commands[0]


def test_interpreter_change_refuses_ambiguous_owned_groups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    first_runtime = tmp_path / "first" / "python.exe"
    second_runtime = tmp_path / "second" / "python.exe"
    for runtime in (first_runtime, second_runtime):
        runtime.parent.mkdir()
        runtime.write_bytes(b"fixture")
    install_shadow_hooks(
        home=home,
        runtime_python=first_runtime,
        project_root=project,
        backup_tag="first",
        hosts=("codex",),
    )
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    payload["hooks"]["PreToolUse"].append(payload["hooks"]["PreToolUse"][-1])
    _write(hooks, payload)
    before = hooks.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=second_runtime,
        project_root=project,
        backup_tag="second",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts
    assert "ambiguous Latent Compass hook ownership" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == before


def test_removal_refuses_standard_wrapper_groups_without_ownership_manifest(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="install",
        hosts=("codex",),
    )
    store = home / ".codex" / "latent-compass-shadow"
    (store / "ownership.json").unlink()
    hooks_before = hooks.read_bytes()
    config_before = (store / "config.json").read_bytes()

    result = remove_shadow_hooks(
        home=home,
        backup_tag="remove",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts
    assert "ownership manifest is required" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == hooks_before
    assert (store / "config.json").read_bytes() == config_before


def test_status_keeps_loading_and_approval_unknown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="project-alpha",
        backup_tag="status",
        hosts=("codex",),
    )

    report = host_status(home=home, project_root=project, hosts=("codex",))
    states = cast(dict[str, dict[str, Any]], report["states"])
    assert states["codex"] == {
        "installed": True,
        "configured": True,
        "loaded": "UNKNOWN",
        "approved": "UNKNOWN",
        "observed": False,
    }


def test_status_reports_missing_host_paths_as_not_installed_or_configured(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    report = host_status(home=tmp_path / "missing-home", project_root=project, hosts=("codex",))

    states = cast(dict[str, dict[str, Any]], report["states"])
    assert states["codex"] == {
        "installed": False,
        "configured": False,
        "loaded": "UNKNOWN",
        "approved": "UNKNOWN",
        "observed": False,
    }


def test_posix_host_commands_quote_paths_for_both_hosts() -> None:
    runtime = Path("/opt/Latent Compass/bin/python")
    wrapper = Path("/tmp/host wrapper.py")

    assert _command("codex", runtime, wrapper, platform="posix") == (
        "'/opt/Latent Compass/bin/python' '/tmp/host wrapper.py' --host codex"
    )
    assert _command("claude", runtime, wrapper, platform="posix") == (
        "'/opt/Latent Compass/bin/python' '/tmp/host wrapper.py' --host claude"
    )


def test_posix_project_removal_preserves_case_distinct_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shadow_harness, "_PLATFORM", "posix")
    upper = tmp_path / "Foo"
    lower = tmp_path / "foo"
    config = load_shadow_config(
        {
            "contract_version": "1.0.0",
            "enabled": True,
            "host_id": "codex-local",
            "agent_family": "codex",
            "projects": [
                {
                    "root": str(upper),
                    "alias": "upper",
                    "capabilities": [{"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0}],
                    "remaining_budget": 0,
                },
                {
                    "root": str(lower),
                    "alias": "lower",
                    "capabilities": [{"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0}],
                    "remaining_budget": 0,
                },
            ],
        }
    )

    result = _without_project(
        config,
        project_root=upper,
        project_alias=None,
        platform="posix",
    )

    assert result is not None
    projects = cast(list[dict[str, object]], result["projects"])
    assert [project["alias"] for project in projects] == ["lower"]


def test_posix_registration_accepts_case_distinct_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shadow_harness, "_PLATFORM", "posix")
    upper = tmp_path / "Foo"
    lower = tmp_path / "foo"
    config_path = tmp_path / "config.json"
    _write(
        config_path,
        {
            "contract_version": "1.0.0",
            "enabled": True,
            "host_id": "codex-local",
            "agent_family": "codex",
            "projects": [
                {
                    "root": str(upper),
                    "alias": "upper",
                    "capabilities": [{"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0}],
                    "remaining_budget": 0,
                }
            ],
        },
    )

    result = _merged_host_config(
        path=config_path,
        host="codex",
        project_root=lower,
        project_alias="lower",
        platform="posix",
    )

    projects = cast(list[dict[str, object]], result["projects"])
    assert [project["alias"] for project in projects] == ["upper", "lower"]

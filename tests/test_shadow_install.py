from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any, cast

from latent_compass.shadow_install import (
    _command,
    host_status,
    install_shadow_hooks,
    main,
    remove_shadow_hooks,
)


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
    hook_script = tmp_path / "runtime" / "latent-compass-shadow-hook.py"
    hook_script.write_text("# fixture\n", encoding="utf-8")
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
        hook_script=hook_script,
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
    assert [item["action"] for item in report["files"]] == ["create", "update", "create"]
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


def test_preflight_refuses_unowned_hook_marker_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(
        hooks,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "foreign",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "foreign latent-compass-shadow-hook.py",
                            }
                        ],
                    }
                ]
            }
        },
    )
    before = hooks.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="project-alpha",
        backup_tag="collision",
        hosts=("codex",),
    )

    assert result["conflicts"] == [
        {
            "code": "configuration_collision",
            "path": str(hooks),
            "detail": "existing Latent Compass hook marker collides in PreToolUse",
        }
    ]
    assert hooks.read_bytes() == before


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


def test_posix_host_commands_quote_paths_for_both_hosts() -> None:
    runtime = Path("/opt/Latent Compass/bin/python")
    wrapper = Path("/tmp/host wrapper.py")

    assert _command("codex", runtime, wrapper, platform="posix") == (
        "'/opt/Latent Compass/bin/python' '/tmp/host wrapper.py' --host codex"
    )
    assert _command("claude", runtime, wrapper, platform="posix") == (
        "'/opt/Latent Compass/bin/python' '/tmp/host wrapper.py' --host claude"
    )

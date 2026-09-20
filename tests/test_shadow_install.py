from __future__ import annotations

import json
from pathlib import Path

from latent_compass.shadow_install import install_shadow_hooks, remove_shadow_hooks


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
    ]


def _shadow_handlers(path: Path, event: str) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        hook
        for group in payload["hooks"][event]
        for hook in group.get("hooks", [])
        if "latent-compass-shadow-hook.py" in hook.get("command", "")
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

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import latent_compass.shadow_harness as shadow_harness
import latent_compass.shadow_install as shadow_install
from latent_compass.confined_io import read_confined_file as confined_read
from latent_compass.confined_io import replace_file as confined_replace
from latent_compass.confined_io import write_new_file as confined_write
from latent_compass.errors import ContractViolation
from latent_compass.shadow_harness import load_shadow_config
from latent_compass.shadow_install import (
    _EXPECTED_UNSET,
    _assert_snapshot,
    _atomic_json,
    _atomic_text,
    _backup,
    _begin_transaction,
    _command,
    _command_references_wrapper,
    _default_project_alias,
    _exclusive_json,
    _ExpectedUnset,
    _merged_host_config,
    _packaged_hook_text,
    _path_entry_exists,
    _recover_pending_transaction,
    _regular_file_bytes_or_none,
    _remove_confined,
    _transaction_snapshot,
    _without_project,
    host_status,
    install_shadow_hooks,
    main,
    plan_install_shadow_hooks,
    plan_recover_shadow_hooks,
    plan_remove_shadow_hooks,
    recover_shadow_hooks,
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


def test_unchanged_install_skips_transaction_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        project_alias="same",
        backup_tag="first",
        hosts=("codex",),
    )

    def unexpected_transaction(**_kwargs: object) -> Path:
        raise AssertionError("unchanged install must not create a journal")

    monkeypatch.setattr(shadow_install, "_begin_transaction", unexpected_transaction)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="same",
        backup_tag="second",
        hosts=("codex",),
    )

    assert result["changed"] is False
    assert result["conflicts"] == []
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_unchanged_remove_skips_transaction_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        project_alias="kept",
        backup_tag="first",
        hosts=("codex",),
    )

    def unexpected_transaction(**_kwargs: object) -> Path:
        raise AssertionError("unchanged remove must not create a journal")

    monkeypatch.setattr(shadow_install, "_begin_transaction", unexpected_transaction)
    result = remove_shadow_hooks(
        home=home,
        backup_tag="remove-missing",
        hosts=("codex",),
        project_alias="missing",
    )

    assert result["changed"] is False
    assert result["conflicts"] == []
    assert not (home / ".latent-compass-shadow.pending.json").exists()


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
    assert report["project_root"] == str(project.resolve())
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


@pytest.mark.parametrize("dangling", [False, True])
def test_install_preview_refuses_symlinked_host_settings(tmp_path: Path, dangling: bool) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    settings = home / ".codex" / "hooks.json"
    settings.parent.mkdir(parents=True)
    outside = tmp_path / "outside-hooks.json"
    if not dangling:
        _write(outside, {"hooks": {"PreToolUse": []}})
    try:
        settings.symlink_to(outside)
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")
    outside_before = outside.read_bytes() if outside.is_file() else None

    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="preview",
    )

    assert plan["conflicts"]
    assert settings.is_symlink()
    assert (outside.read_bytes() if outside.is_file() else None) == outside_before


def test_install_preview_refuses_host_file_swapped_to_symlink_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    settings = home / ".codex" / "hooks.json"
    _write(settings, {"hooks": {"PreToolUse": []}})
    outside = tmp_path / "outside-hooks.json"
    outside.write_bytes(b'{"outside":true}\n')
    outside_before = outside.read_bytes()
    original_reader = confined_read
    swapped = False

    def swap_then_read(root: Path, target: Path, *, max_bytes: int, what: str) -> bytes:
        nonlocal swapped
        if target == settings and not swapped:
            swapped = True
            settings.unlink()
            settings.symlink_to(outside)
        return original_reader(root, target, max_bytes=max_bytes, what=what)

    monkeypatch.setattr("latent_compass.shadow_install.read_confined_file", swap_then_read)
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="preview",
    )

    assert cast(list[dict[str, object]], plan["conflicts"])[0]["code"] == (
        "configuration_collision"
    )
    assert settings.is_symlink()
    assert outside.read_bytes() == outside_before


def test_install_and_remove_preview_refuse_symlinked_host_parent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-codex"
    hooks = outside / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    try:
        (home / ".codex").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink support is required for this security witness: {exc}")
    project = tmp_path / "project"
    project.mkdir()
    before = hooks.read_bytes()

    install_plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="preview",
    )
    remove_plan = shadow_install.plan_remove_shadow_hooks(
        home=home,
        hosts=("codex",),
        backup_tag="preview",
    )

    assert install_plan["conflicts"]
    assert remove_plan["conflicts"]
    assert hooks.read_bytes() == before
    assert not (outside / "latent-compass-shadow").exists()


@pytest.mark.parametrize("replacement", ["symlink", "dangling", "directory"])
def test_install_preview_refuses_non_regular_managed_wrapper(
    tmp_path: Path, replacement: str
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    wrapper.parent.mkdir(parents=True)
    if replacement == "directory":
        wrapper.mkdir()
    else:
        outside = tmp_path / "outside-wrapper.py"
        if replacement == "symlink":
            outside.write_text("# foreign\n", encoding="utf-8")
        try:
            wrapper.symlink_to(outside)
        except OSError as exc:
            pytest.fail(f"file symlink support is required for this security witness: {exc}")

    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="preview",
    )

    assert plan["conflicts"]
    assert _path_entry_exists(wrapper)


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


def test_apply_refuses_dangling_backup_symlink_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    backup = hooks.with_name("hooks.json.bak-latent-compass-fixed")
    try:
        backup.symlink_to(tmp_path / "missing-backup.json")
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")
    before = hooks.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="fixed",
        hosts=("codex",),
    )

    assert result["conflicts"] == [{"code": "backup_collision", "path": str(backup)}]
    assert hooks.read_bytes() == before
    assert backup.is_symlink()


def test_backup_race_preserves_third_party_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "hooks.json"
    source.write_bytes(b"source")
    destination = source.with_name("hooks.json.bak-latent-compass-race")
    third_party = b"third-party"
    real_writer = confined_write
    injected = False

    def race_writer(root: Path, target: Path, data: bytes, *, what: str) -> Path:
        nonlocal injected
        if target == destination and not injected:
            injected = True
            destination.write_bytes(third_party)
        return real_writer(root, target, data, what=what)

    monkeypatch.setattr("latent_compass.shadow_install.write_new_file", race_writer)

    with pytest.raises(ValueError, match="already exists"):
        _backup(source, "race")

    assert destination.read_bytes() == third_party


def test_backup_publication_failure_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "hooks.json"
    source.write_bytes(b"source")
    destination = source.with_name("hooks.json.bak-latent-compass-race")

    def fail_before_publication(root: Path, target: Path, data: bytes, *, what: str) -> Path:
        assert root == tmp_path
        assert target == destination
        assert data == b"source"
        assert what == "host transaction backup"
        raise OSError("injected publication failure")

    monkeypatch.setattr("latent_compass.shadow_install.write_new_file", fail_before_publication)

    with pytest.raises(OSError, match="injected publication failure"):
        _backup(source, "race")

    assert not destination.exists()


def test_backup_source_swap_to_symlink_is_refused_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "hooks.json"
    source.write_bytes(b"source")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    real_reader = confined_read

    def swap_then_read(root: Path, target: Path, *, max_bytes: int, what: str) -> bytes:
        source.unlink()
        source.symlink_to(outside)
        return real_reader(root, target, max_bytes=max_bytes, what=what)

    monkeypatch.setattr(shadow_install, "read_confined_file", swap_then_read)

    with pytest.raises(ContractViolation):
        _backup(source, "race", home=tmp_path)

    assert source.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_install_rejects_backup_bytes_that_do_not_match_transaction_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    original_reader = confined_read

    def inject_foreign_backup_bytes(
        root: Path, target: Path, *, max_bytes: int, what: str
    ) -> bytes:
        if target == hooks and what == "host transaction backup source":
            return b'{"foreign":true}\n'
        return original_reader(root, target, max_bytes=max_bytes, what=what)

    monkeypatch.setattr(
        "latent_compass.shadow_install.read_confined_file", inject_foreign_backup_bytes
    )
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="aba",
        hosts=("codex",),
    )

    journal = home / ".latent-compass-shadow.pending.json"
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert "backup source" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == before
    assert not hooks.with_name("hooks.json.bak-latent-compass-aba").exists()
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert not journal.exists()


def test_atomic_write_ignores_predictable_symlink_trap(tmp_path: Path) -> None:
    target = tmp_path / "ownership.json"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    predictable = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        predictable.symlink_to(outside)
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")

    _atomic_text(target, "owned\n")

    assert target.read_bytes() == b"owned\n"
    assert outside.read_bytes() == b"outside"
    assert predictable.is_symlink()


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
    assert report["project_root"] == str(project.resolve())
    assert report["conflicts"] == [{"code": "backup_collision", "path": str(backup)}]
    assert hooks.read_bytes() == before
    assert not (home / ".codex" / "latent-compass-shadow").exists()


def test_install_refuses_unowned_runtime_wrapper_before_backup_or_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# foreign wrapper\n", encoding="utf-8")
    before = wrapper.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="collision",
        hosts=("codex",),
    )

    assert result["conflicts"] == [{"code": "wrapper_collision", "path": str(wrapper)}]
    assert wrapper.read_bytes() == before
    assert not wrapper.with_name(f"{wrapper.name}.bak-latent-compass-collision").exists()
    assert not (wrapper.parents[1] / "config.json").exists()
    assert not (wrapper.parents[1] / "ownership.json").exists()


def test_install_refuses_changed_owned_wrapper_content(tmp_path: Path) -> None:
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
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    wrapper.write_text("# replaced by foreign content\n", encoding="utf-8")
    before = wrapper.read_bytes()

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="reinstall",
        hosts=("codex",),
    )

    assert result["conflicts"] == [{"code": "wrapper_integrity_collision", "path": str(wrapper)}]
    assert wrapper.read_bytes() == before
    assert not wrapper.with_name(f"{wrapper.name}.bak-latent-compass-reinstall").exists()


def test_final_removal_deletes_managed_wrapper_and_allows_reinstall(tmp_path: Path) -> None:
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
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )

    removed = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))
    assert not wrapper.exists()
    reinstalled = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="reinstall",
        hosts=("codex",),
    )

    assert removed["conflicts"] == []
    assert reinstalled["conflicts"] == []
    assert wrapper.is_file()
    for event in ("SessionStart", "PreToolUse", "PostToolUse"):
        assert len(_commands(hooks, event)) == 1


@pytest.mark.parametrize("embedded_host", ["codex", "claude", "other"])
def test_final_removal_refuses_foreign_reference_to_managed_wrapper(
    tmp_path: Path, embedded_host: str
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
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    foreign_runtime = tmp_path / "foreign" / "python.exe"
    foreign_runtime.parent.mkdir()
    foreign_runtime.write_bytes(b"foreign")
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    foreign_command = (
        _command(cast(Host, embedded_host), foreign_runtime, wrapper, platform="nt")
        if embedded_host in {"codex", "claude"}
        else f'"{foreign_runtime}" "{wrapper}" --host other'
    )
    payload["hooks"]["PreToolUse"].append(
        {
            "matcher": "foreign",
            "hooks": [
                {
                    "type": "command",
                    "command": foreign_command + f' --home "{home}"',
                    "async": True,
                    "timeout": 10,
                }
            ],
        }
    )
    _write(hooks, payload)
    store = home / ".codex" / "latent-compass-shadow"
    before = {path: path.read_bytes() for path in (hooks, wrapper, store / "config.json")}

    result = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts
    assert "still referenced by a foreign hook" in str(conflicts[0]["detail"])
    assert all(path.read_bytes() == content for path, content in before.items())


def test_install_refuses_foreign_reference_to_managed_wrapper(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    foreign_runtime = tmp_path / "foreign" / "python.exe"
    foreign_runtime.parent.mkdir()
    foreign_runtime.write_bytes(b"foreign")
    foreign_command = f'"{foreign_runtime}" "{wrapper}" --extra'
    _write(
        hooks,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "foreign",
                        "hooks": [{"type": "command", "command": foreign_command}],
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
        backup_tag="install",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts
    assert "referenced by a foreign hook" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == before
    assert not wrapper.exists()


@pytest.mark.parametrize(
    ("command_template", "expected"),
    [
        ('python -u "{wrapper}" --host other', True),
        ('env python "{wrapper}" --extra', True),
        ('python "{wrapper}" --extra', True),
        ('echo ok # "{wrapper}"', False),
    ],
)
def test_bounded_reference_scan_handles_interpreter_forms_and_comments(
    tmp_path: Path, command_template: str, expected: bool
) -> None:
    wrapper = tmp_path / "Windows Style" / "latent-compass-shadow-hook.py"
    command = command_template.format(wrapper=wrapper)

    assert _command_references_wrapper(command, wrapper) is expected


def test_default_backup_tags_allow_rapid_install_remove_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime:
        @classmethod
        def now(cls, *, tz: object) -> datetime:
            assert tz is UTC
            return datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(shadow_install, "datetime", FrozenDatetime)
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    install_output = StringIO()
    remove_output = StringIO()

    install_code = main(
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
        stdout=install_output,
    )
    remove_code = main(
        ["remove", "--host", "codex", "--home", str(home), "--json"],
        stdout=remove_output,
    )

    assert install_code == 0
    assert remove_code == 0
    assert json.loads(install_output.getvalue())["conflicts"] == []
    assert json.loads(remove_output.getvalue())["conflicts"] == []


def test_install_rolls_back_all_files_when_late_atomic_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(
        hooks,
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "foreign", "hooks": [{"type": "command", "command": "keep"}]}
                ]
            }
        },
    )
    before = hooks.read_bytes()
    real_atomic_json = _atomic_json

    def fail_on_config(path: Path, payload: object, **kwargs: Any) -> None:
        if path.name == "config.json":
            raise OSError("injected config write failure")
        real_atomic_json(path, payload, **kwargs)

    monkeypatch.setattr(shadow_install, "_atomic_json", fail_on_config)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="fault",
        hosts=("codex",),
    )

    store = home / ".codex" / "latent-compass-shadow"
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == before
    assert not (store / "config.json").exists()
    assert not (store / "ownership.json").exists()
    assert not (store / "runtime" / "latent-compass-shadow-hook.py").exists()
    backup = hooks.with_name("hooks.json.bak-latent-compass-fault")
    assert backup.read_bytes() == before


def test_install_rolls_back_when_writer_fails_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    real_replace = confined_replace
    injected = False

    def replace_then_fail(root: Path, target: Path, data: bytes, *, what: str) -> Path:
        nonlocal injected
        result = real_replace(root, target, data, what=what)
        if target == hooks and not injected:
            injected = True
            raise OSError("injected failure after atomic publication")
        return result

    monkeypatch.setattr("latent_compass.shadow_install.replace_file", replace_then_fail)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="post-publication",
        hosts=("codex",),
    )

    store = home / ".codex" / "latent-compass-shadow"
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == before
    assert not (store / "config.json").exists()
    assert not (store / "ownership.json").exists()
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert not journal.exists()


def test_install_preserves_journal_when_backup_fails_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    real_backup = _backup
    injected = False

    def backup_then_fail(
        path: Path,
        tag: str,
        *,
        home: Path | None = None,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> Path:
        nonlocal injected
        destination = real_backup(path, tag, home=home, expected=expected)
        if path == hooks and not injected:
            injected = True
            raise OSError("injected failure after backup publication")
        return destination

    monkeypatch.setattr(shadow_install, "_backup", backup_then_fail)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="backup-published",
        hosts=("codex",),
    )

    journal = home / ".latent-compass-shadow.pending.json"
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == before
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert not journal.exists()


def test_install_preserves_journal_when_published_bytes_change_before_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    third_party = b'{"third_party":true}\n'
    real_atomic_json = _atomic_json

    def publish_substitute_then_fail(path: Path, payload: object, **kwargs: Any) -> None:
        real_atomic_json(path, payload, **kwargs)
        if path == config:
            path.write_bytes(third_party)
            raise OSError("injected post-publication observation failure")

    monkeypatch.setattr(shadow_install, "_atomic_json", publish_substitute_then_fail)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="post-observation",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert config.read_bytes() == third_party
    assert (home / ".latent-compass-shadow.pending.json").is_file()


def test_concurrent_pending_journal_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    real_exclusive = _exclusive_json
    rival = {
        "schema_version": 1,
        "operation": "install",
        "backup_tag": "rival",
        "entries": [
            {
                "path": str(home / ".codex" / "hooks.json"),
                "before_sha256": "sha256:" + "1" * 64,
                "after_sha256": "sha256:" + "2" * 64,
                "backup_path": str(home / ".codex" / "hooks.json.bak-latent-compass-rival"),
            }
        ],
    }
    injected = False

    def race_journal(path: Path, payload: object, *, home: Path | None = None) -> None:
        nonlocal injected
        if not injected:
            injected = True
            real_exclusive(path, rival, home=home)
        real_exclusive(path, payload, home=home)

    monkeypatch.setattr(shadow_install, "_exclusive_json", race_journal)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="ours",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    journal = home / ".latent-compass-shadow.pending.json"
    assert conflicts[0]["code"] == "apply_failed"
    assert json.loads(journal.read_text(encoding="utf-8")) == rival


@pytest.mark.parametrize("operation", ["install", "remove"])
def test_apply_preserves_journal_replaced_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    if operation == "remove":
        install_shadow_hooks(
            home=home,
            runtime_python=Path(sys.executable),
            project_root=project,
            backup_tag="install",
            hosts=("codex",),
        )
    journal = home / ".latent-compass-shadow.pending.json"
    foreign = b'{"foreign":true}\n'
    real_remove = _remove_confined
    swapped = False

    def swap_before_cleanup(
        selected_home: Path,
        path: Path,
        *,
        what: str,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> None:
        nonlocal swapped
        if path == journal and not swapped:
            swapped = True
            path.write_bytes(foreign)
        real_remove(selected_home, path, what=what, expected=expected)

    monkeypatch.setattr(shadow_install, "_remove_confined", swap_before_cleanup)
    result = (
        install_shadow_hooks(
            home=home,
            runtime_python=Path(sys.executable),
            project_root=project,
            project_alias="fresh",
            backup_tag="apply",
            hosts=("codex",),
        )
        if operation == "install"
        else remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert swapped is True
    assert journal.read_bytes() == foreign


def test_recovery_preserves_journal_replaced_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="recover",
    )
    journal, _journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="recover",
        plan=plan,
    )
    foreign = b'{"foreign":true}\n'
    real_remove = _remove_confined
    swapped = False

    def swap_before_cleanup(
        selected_home: Path,
        path: Path,
        *,
        what: str,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> None:
        nonlocal swapped
        if path == journal and not swapped:
            swapped = True
            path.write_bytes(foreign)
        real_remove(selected_home, path, what=what, expected=expected)

    monkeypatch.setattr(shadow_install, "_remove_confined", swap_before_cleanup)
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert swapped is True
    assert journal.read_bytes() == foreign


def test_pending_journal_is_complete_before_exclusive_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".latent-compass-shadow.pending.json"
    original_writer = confined_write

    def interrupt_before_publication(
        root: Path, destination: Path, data: bytes, *, what: str
    ) -> Path:
        assert json.loads(data.decode("utf-8")) == {"operation": "install"}
        assert root == tmp_path
        assert destination == path
        assert what == "host transaction journal"
        assert not path.exists()
        raise OSError("interrupted before journal publication")

    monkeypatch.setattr(
        "latent_compass.shadow_install.write_new_file", interrupt_before_publication
    )
    with pytest.raises(OSError, match="interrupted before journal publication"):
        _exclusive_json(path, {"operation": "install"})
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr("latent_compass.shadow_install.write_new_file", original_writer)
    _exclusive_json(path, {"operation": "install"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"operation": "install"}


def test_remove_rolls_back_all_files_when_late_backup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    wrapper = store / "runtime" / "latent-compass-shadow-hook.py"
    paths = (hooks, store / "config.json", store / "ownership.json", wrapper)
    before = {path: path.read_bytes() for path in paths}
    real_backup = _backup

    def fail_on_config(
        path: Path,
        tag: str,
        *,
        home: Path | None = None,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> Path:
        if path.name == "config.json":
            raise OSError("injected config backup failure")
        return real_backup(path, tag, home=home, expected=expected)

    monkeypatch.setattr(shadow_install, "_backup", fail_on_config)

    result = remove_shadow_hooks(home=home, backup_tag="fault", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert all(path.read_bytes() == content for path, content in before.items())
    backup = hooks.with_name("hooks.json.bak-latent-compass-fault")
    assert backup.read_bytes() == before[hooks]


def test_remove_rolls_back_when_delete_fails_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    paths = (
        hooks,
        store / "config.json",
        store / "ownership.json",
        store / "runtime" / "latent-compass-shadow-hook.py",
    )
    before = {path: path.read_bytes() for path in paths}
    real_remove = _remove_confined
    injected = False

    def remove_then_fail(
        selected_home: Path,
        path: Path,
        *,
        what: str,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> None:
        nonlocal injected
        real_remove(selected_home, path, what=what, expected=expected)
        if path.name == "config.json" and not injected:
            injected = True
            raise OSError("injected failure after confined removal")

    monkeypatch.setattr(shadow_install, "_remove_confined", remove_then_fail)
    result = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert all(path.read_bytes() == content for path, content in before.items())
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert not journal.exists()


def test_remove_preserves_journal_when_backup_fails_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    before = hooks.read_bytes()
    real_backup = _backup
    injected = False

    def backup_then_fail(
        path: Path,
        tag: str,
        *,
        home: Path | None = None,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> Path:
        nonlocal injected
        destination = real_backup(path, tag, home=home, expected=expected)
        if path == hooks and not injected:
            injected = True
            raise OSError("injected failure after backup publication")
        return destination

    monkeypatch.setattr(shadow_install, "_backup", backup_then_fail)
    result = remove_shadow_hooks(
        home=home,
        backup_tag="remove-backup-published",
        hosts=("codex",),
    )

    journal = home / ".latent-compass-shadow.pending.json"
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == before
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert not journal.exists()


def test_remove_preserves_file_changed_after_backup_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    original = config.read_bytes()
    foreign = b'{"foreign_after_backup":true}\n'
    real_backup = _backup
    swapped = False

    def backup_then_swap(
        path: Path,
        tag: str,
        *,
        home: Path | None = None,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> Path:
        nonlocal swapped
        destination = real_backup(path, tag, home=home, expected=expected)
        if path == config and not swapped:
            swapped = True
            path.write_bytes(foreign)
        return destination

    monkeypatch.setattr(shadow_install, "_backup", backup_then_swap)
    result = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert swapped is True
    assert config.read_bytes() == foreign
    assert config.with_name("config.json.bak-latent-compass-remove").read_bytes() == original
    assert (home / ".latent-compass-shadow.pending.json").is_file()


def test_install_refuses_concurrent_foreign_hook_before_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    concurrent = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "foreign", "hooks": [{"type": "command", "command": "keep"}]}
            ]
        }
    }
    real_snapshot = _transaction_snapshot

    def inject_then_validate(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        _write(hooks, concurrent)
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", inject_then_validate)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="concurrent",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "concurrent_change"
    assert json.loads(hooks.read_text(encoding="utf-8")) == concurrent
    assert not (home / ".codex" / "latent-compass-shadow" / "config.json").exists()
    assert not list(home.rglob("*.bak-latent-compass-concurrent"))


def test_install_rollback_preserves_concurrent_change_to_unwritten_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    hooks_before = hooks.read_bytes()
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    third_party = b'{"third_party":true}\n'
    real_assert = _assert_snapshot

    def inject_on_config(selected_home: Path, path: Path, expected: bytes | None) -> None:
        if path == config:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(third_party)
        real_assert(selected_home, path, expected)

    monkeypatch.setattr(shadow_install, "_assert_snapshot", inject_on_config)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="concurrent-late",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == third_party
    assert (home / ".latent-compass-shadow.pending.json").is_file()


def test_install_preserves_leaf_created_after_snapshot_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    third_party = b'{"third_party":true}\n'
    original_writer = confined_write

    def inject_leaf_then_publish(root: Path, target: Path, data: bytes, *, what: str) -> Path:
        if target == config and not config.exists():
            config.write_bytes(third_party)
        return original_writer(root, target, data, what=what)

    monkeypatch.setattr("latent_compass.shadow_install.write_new_file", inject_leaf_then_publish)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="leaf-create-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert config.read_bytes() == third_party


def test_install_preserves_leaf_changed_before_final_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    third_party = b'{"hooks":{"PreToolUse":[{"foreign":true}]}}\n'
    original_reader = confined_read
    injected = False

    def inject_leaf_then_read(root: Path, target: Path, *, max_bytes: int, what: str) -> bytes:
        nonlocal injected
        if target == hooks and what == "managed host file before replacement" and not injected:
            injected = True
            hooks.write_bytes(third_party)
        return original_reader(root, target, max_bytes=max_bytes, what=what)

    monkeypatch.setattr("latent_compass.shadow_install.read_confined_file", inject_leaf_then_read)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="leaf-update-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == third_party


def test_install_rerun_recovers_durable_pending_wrapper_creation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, _journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())

    install_arguments = [
        "install",
        "--host",
        "codex",
        "--home",
        str(home),
        "--project-root",
        str(project),
        "--backup-tag",
        "retry",
        "--json",
    ]
    blocked_output = StringIO()
    blocked_code = main([*install_arguments, "--dry-run"], stdout=blocked_output)
    recovery_preview_output = StringIO()
    recovery_preview_code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=recovery_preview_output,
    )
    recovery_apply_output = StringIO()
    recovery_apply_code = main(
        ["recover", "--home", str(home), "--json"],
        stdout=recovery_apply_output,
    )
    install_preview_output = StringIO()
    install_preview_code = main([*install_arguments, "--dry-run"], stdout=install_preview_output)
    install_apply_output = StringIO()
    install_apply_code = main(install_arguments, stdout=install_apply_output)
    blocked = json.loads(blocked_output.getvalue())
    recovery_preview = json.loads(recovery_preview_output.getvalue())
    recovery_apply = json.loads(recovery_apply_output.getvalue())
    install_preview = json.loads(install_preview_output.getvalue())
    install_apply = json.loads(install_apply_output.getvalue())

    assert blocked_code == 3
    assert blocked["conflicts"][0]["code"] == "recovery_required"
    assert blocked["next_steps"] == [
        f'latent-compass host recover --home "{home}" --dry-run --json'
    ]
    assert recovery_preview_code == recovery_apply_code == 0
    assert recovery_preview["files"] == recovery_apply["files"]
    assert recovery_apply["recovery"]["completed"] is True
    assert install_preview_code == install_apply_code == 0
    assert install_preview["files"] == install_apply["files"]
    assert wrapper.is_file()
    assert not journal.exists()


def test_recovery_preflights_all_backups_before_first_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, _journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())
    payloads = cast(dict[str, dict[str, object]], plan["_payloads"])
    _atomic_json(hooks, payloads["codex"]["settings"])

    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "backup is missing" in str(conflicts[0]["detail"])
    assert wrapper.is_file()
    assert journal.is_file()


def test_recovery_preserves_created_file_changed_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, _journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())
    foreign = b"# foreign after recovery preflight\n"
    real_remove = _remove_confined
    swapped = False

    def swap_before_delete(
        selected_home: Path,
        path: Path,
        *,
        what: str,
        expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
    ) -> None:
        nonlocal swapped
        if path == wrapper and not swapped:
            swapped = True
            path.write_bytes(foreign)
        real_remove(selected_home, path, what=what, expected=expected)

    monkeypatch.setattr(shadow_install, "_remove_confined", swap_before_delete)
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert swapped is True
    assert wrapper.read_bytes() == foreign
    assert journal.is_file()


def test_recovery_reports_backup_vanishing_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    original = b'{"hooks":{"PreToolUse":[]}}'
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(original)
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    payloads = cast(dict[str, dict[str, object]], plan["_payloads"])
    _atomic_json(hooks, payloads["codex"]["settings"])
    backup = hooks.with_name("hooks.json.bak-latent-compass-crash")
    backup.write_bytes(original)
    real_reader = _regular_file_bytes_or_none
    backup_reads = 0

    def vanish_on_second_backup_read(path: Path, *, home: Path | None = None) -> bytes | None:
        nonlocal backup_reads
        if path == backup:
            backup_reads += 1
            if backup_reads == 3:
                backup.unlink()
        return real_reader(path, home=home)

    monkeypatch.setattr(shadow_install, "_regular_file_bytes_or_none", vanish_on_second_backup_read)

    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "vanished after preflight" in str(conflicts[0]["detail"])
    assert (home / ".latent-compass-shadow.pending.json").is_file()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_recovery_refuses_non_regular_target_entry(tmp_path: Path, replacement: str) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    snapshots = _transaction_snapshot(home, plan)
    _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    if replacement == "directory":
        wrapper.mkdir()
    else:
        outside = tmp_path / "outside-wrapper.py"
        outside.write_text(_packaged_hook_text(), encoding="utf-8")
        try:
            wrapper.symlink_to(outside)
        except OSError as exc:
            pytest.fail(f"file symlink support is required for this security witness: {exc}")

    recovery = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert _path_entry_exists(wrapper)


def test_recovery_refuses_dangling_backup_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="crash",
    )
    _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    payloads = cast(dict[str, dict[str, object]], plan["_payloads"])
    _atomic_json(hooks, payloads["codex"]["settings"])
    backup = hooks.with_name("hooks.json.bak-latent-compass-crash")
    try:
        backup.symlink_to(tmp_path / "missing-backup.json")
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")

    recovery = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert backup.is_symlink()


def test_recover_refuses_symlinked_journal_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version":1,"entries":[]}\n', encoding="utf-8")
    journal = home / ".latent-compass-shadow.pending.json"
    try:
        journal.symlink_to(outside)
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")
    before = outside.read_bytes()
    stdout = StringIO()

    code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
    assert outside.read_bytes() == before


def test_recover_refuses_journal_swapped_to_symlink_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_text('{"schema_version":1,"entries":[]}\n', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"outside":true}\n')
    outside_before = outside.read_bytes()
    original_reader = confined_read
    swapped = False

    def swap_then_read(root: Path, target: Path, *, max_bytes: int, what: str) -> bytes:
        nonlocal swapped
        if target == journal and not swapped:
            swapped = True
            journal.unlink()
            journal.symlink_to(outside)
        return original_reader(root, target, max_bytes=max_bytes, what=what)

    monkeypatch.setattr("latent_compass.shadow_install.read_confined_file", swap_then_read)
    report = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], report["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert journal.is_symlink()
    assert outside.read_bytes() == outside_before


def test_recover_refuses_dangling_symlinked_journal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    journal = home / ".latent-compass-shadow.pending.json"
    try:
        journal.symlink_to(tmp_path / "missing.json")
    except OSError as exc:
        pytest.fail(f"file symlink support is required for this security witness: {exc}")
    stdout = StringIO()

    code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
    assert journal.is_symlink()


def test_recover_refuses_directory_at_journal_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    journal = home / ".latent-compass-shadow.pending.json"
    journal.mkdir(parents=True)
    stdout = StringIO()

    code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
    assert journal.is_dir()


@pytest.mark.parametrize("raw", ["not-json", "[]", "null", '"text"'])
def test_malformed_journal_returns_json_conflict_for_every_operation(
    tmp_path: Path, raw: str
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_text(raw, encoding="utf-8")
    commands = (
        [
            "install",
            "--host",
            "codex",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--dry-run",
            "--json",
        ],
        ["remove", "--host", "codex", "--home", str(home), "--dry-run", "--json"],
        ["recover", "--home", str(home), "--dry-run", "--json"],
    )

    for command in commands:
        stdout = StringIO()
        code = main(command, stdout=stdout)
        report = json.loads(stdout.getvalue())
        assert code == 3
        assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
        assert journal.read_text(encoding="utf-8") == raw


def test_deeply_nested_journal_returns_structured_invalid_conflict(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    journal = home / ".latent-compass-shadow.pending.json"
    raw = "[" * 10_000 + "]" * 10_000
    journal.write_text(raw, encoding="utf-8")
    before = journal.read_bytes()

    preview = plan_recover_shadow_hooks(home=home)
    apply_conflicts = _recover_pending_transaction(home)

    preview_conflicts = cast(list[dict[str, object]], preview["conflicts"])
    assert preview_conflicts[0]["code"] == "pending_transaction_invalid"
    assert apply_conflicts[0]["code"] == "pending_transaction_invalid"
    assert journal.read_bytes() == before


def test_recovery_preview_preflights_replacement_journal_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="swap",
    )
    journal, _journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="swap",
        plan=plan,
    )
    current_digest = f"sha256:{hashlib.sha256(hooks.read_bytes()).hexdigest()}"
    replacement = {
        "schema_version": 1,
        "operation": "remove",
        "backup_tag": "replacement",
        "entries": [
            {
                "path": str(hooks),
                "before_sha256": "sha256:" + "1" * 64,
                "after_sha256": current_digest,
                "backup_path": str(hooks.with_name("hooks.json.bak-latent-compass-replacement")),
            }
        ],
    }
    real_reader = _regular_file_bytes_or_none
    swapped = False

    def replace_before_journal_read(path: Path, *, home: Path | None = None) -> bytes | None:
        nonlocal swapped
        if path == journal and not swapped:
            swapped = True
            journal.write_text(json.dumps(replacement), encoding="utf-8")
        return real_reader(path, home=home)

    monkeypatch.setattr(
        "latent_compass.shadow_install._regular_file_bytes_or_none",
        replace_before_journal_read,
    )
    report = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], report["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "backup is missing" in str(conflicts[0]["detail"])
    assert swapped is True
    assert journal.is_file()


@pytest.mark.parametrize("defect", ["empty", "operation", "duplicate", "unknown-path"])
def test_recover_rejects_semantically_invalid_journal_entries(tmp_path: Path, defect: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    managed = home / ".codex" / "hooks.json"
    unknown = home / "unmanaged.json"
    digest = "sha256:" + "1" * 64
    entry = {
        "path": str(unknown if defect == "unknown-path" else managed),
        "before_sha256": None,
        "after_sha256": digest,
        "backup_path": None,
    }
    entries = [] if defect == "empty" else [entry]
    if defect == "duplicate":
        entries.append(dict(entry))
    payload = {
        "schema_version": 1,
        "operation": "unknown" if defect == "operation" else "install",
        "backup_tag": "test",
        "entries": entries,
    }
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    before = journal.read_bytes()
    stdout = StringIO()

    code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
    assert journal.read_bytes() == before


def test_recover_validates_digest_syntax_before_current_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".codex" / "hooks.json"
    backup = target.with_name("hooks.json.bak-latent-compass-test")
    payload = {
        "schema_version": 1,
        "operation": "remove",
        "backup_tag": "test",
        "entries": [
            {
                "path": str(target),
                "before_sha256": "not-a-digest",
                "after_sha256": None,
                "backup_path": str(backup),
            }
        ],
    }
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    before = journal.read_bytes()
    stdout = StringIO()

    code = main(
        ["recover", "--home", str(home), "--dry-run", "--json"],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert report["conflicts"][0]["code"] == "pending_transaction_invalid"
    assert journal.read_bytes() == before


def test_recover_requires_string_backup_path_before_current_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    before_digest = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
    payload = {
        "schema_version": 1,
        "operation": "remove",
        "backup_tag": "test",
        "entries": [
            {
                "path": str(target),
                "before_sha256": before_digest,
                "after_sha256": None,
                "backup_path": 42,
            }
        ],
    }
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    journal_before = journal.read_bytes()

    preview = plan_recover_shadow_hooks(home=home)
    applied = recover_shadow_hooks(home=home)

    for report in (preview, applied):
        conflicts = cast(list[dict[str, object]], report["conflicts"])
        assert conflicts[0]["code"] == "pending_transaction_invalid"
        assert "must be a string" in str(conflicts[0]["detail"])
    assert target.read_bytes() == b"before"
    assert journal.read_bytes() == journal_before


def test_rollback_does_not_overwrite_concurrent_change_to_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    third_party = b'{"hooks":{"PreToolUse":[{"foreign":true}]}}\n'
    real_atomic_json = _atomic_json

    def fail_after_foreign_change(path: Path, payload: object, **kwargs: Any) -> None:
        if path.name == "config.json":
            hooks.write_bytes(third_party)
            raise OSError("injected failure after concurrent hook change")
        real_atomic_json(path, payload, **kwargs)

    monkeypatch.setattr(shadow_install, "_atomic_json", fail_after_foreign_change)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="rollback-conflict",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert hooks.read_bytes() == third_party
    assert (home / ".latent-compass-shadow.pending.json").is_file()
    assert hooks.with_name("hooks.json.bak-latent-compass-rollback-conflict").is_file()
    dry_output = StringIO()
    dry_code = main(
        [
            "install",
            "--host",
            "codex",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--dry-run",
            "--json",
        ],
        stdout=dry_output,
    )
    dry_report = json.loads(dry_output.getvalue())
    assert dry_code == 3
    assert dry_report["conflicts"][0]["code"] == "pending_transaction_conflict"
    assert hooks.read_bytes() == third_party


def test_rollback_preserves_symlink_substitution_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    outside = tmp_path / "outside-hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    real_atomic_json = _atomic_json

    def fail_after_symlink_substitution(path: Path, payload: object, **kwargs: Any) -> None:
        if path.name == "config.json":
            written = hooks.read_bytes()
            hooks.unlink()
            outside.write_bytes(written)
            hooks.symlink_to(outside)
            raise OSError("injected failure after symlink substitution")
        real_atomic_json(path, payload, **kwargs)

    monkeypatch.setattr(shadow_install, "_atomic_json", fail_after_symlink_substitution)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="symlink-conflict",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert hooks.is_symlink()
    assert outside.is_file()
    assert (home / ".latent-compass-shadow.pending.json").is_file()
    assert hooks.with_name("hooks.json.bak-latent-compass-symlink-conflict").is_file()


def test_remove_dry_run_reports_backup_collision_before_apply(tmp_path: Path) -> None:
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
    backup = hooks.with_name("hooks.json.bak-latent-compass-fixed")
    backup.write_text("existing backup\n", encoding="utf-8")
    store = home / ".codex" / "latent-compass-shadow"
    before = {
        path: path.read_bytes() for path in (hooks, store / "config.json", store / "ownership.json")
    }
    stdout = StringIO()

    code = main(
        [
            "remove",
            "--host",
            "codex",
            "--home",
            str(home),
            "--backup-tag",
            "fixed",
            "--dry-run",
            "--json",
        ],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert code == 3
    assert {item["path"] for item in report["conflicts"]} >= {str(backup)}
    assert all(path.read_bytes() == content for path, content in before.items())


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


def test_removal_refuses_manifest_wrapper_outside_home_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    hooks_before = hooks.read_bytes()
    original_reader = _regular_file_bytes_or_none

    def refuse_outside_read(path: Path, *, home: Path | None = None) -> bytes | None:
        if path == custom_wrapper:
            pytest.fail("manifest-supplied wrapper outside home must not be read")
        return original_reader(path, home=home)

    monkeypatch.setattr(
        "latent_compass.shadow_install._regular_file_bytes_or_none", refuse_outside_read
    )
    removed = remove_shadow_hooks(
        home=home,
        backup_tag="remove-custom",
        hosts=("codex",),
    )

    assert installed["conflicts"] == []
    conflicts = cast(list[dict[str, object]], removed["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert "escapes the selected home" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == hooks_before
    assert custom_wrapper.read_text(encoding="utf-8") == "# custom fixture\n"


def test_codex_removal_refuses_manifest_wrapper_in_claude_store_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    private = home / ".claude" / "private.txt"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private")
    ownership = home / ".codex" / "latent-compass-shadow" / "ownership.json"
    _write(
        ownership,
        {
            "schema_version": 2,
            "host": "codex",
            "command": _command("codex", Path(sys.executable), private),
            "wrapper_digest": f"sha256:{hashlib.sha256(private.read_bytes()).hexdigest()}",
        },
    )
    private_before = private.read_bytes()
    original_reader = _regular_file_bytes_or_none

    def refuse_cross_host_read(path: Path, *, home: Path | None = None) -> bytes | None:
        if path == private:
            pytest.fail("Codex removal must not read a Claude profile file")
        return original_reader(path, home=home)

    monkeypatch.setattr(
        "latent_compass.shadow_install._regular_file_bytes_or_none", refuse_cross_host_read
    )
    plan = plan_remove_shadow_hooks(home=home, hosts=("codex",), backup_tag="remove")

    conflicts = cast(list[dict[str, object]], plan["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert "escapes the selected home" in str(conflicts[0]["detail"])
    assert private.read_bytes() == private_before


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


def test_windows_status_keeps_installation_but_reports_observation_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        project_alias="project-alpha",
        backup_tag="status",
        hosts=("codex",),
    )
    events = home / ".codex" / "latent-compass-shadow" / "events" / "project-alpha"
    events.mkdir(parents=True)
    monkeypatch.setattr("latent_compass.confined_io._is_windows_runtime", lambda: True)

    report = host_status(home=home, project_root=project, hosts=("codex",))
    states = cast(dict[str, dict[str, Any]], report["states"])

    assert states["codex"] == {
        "installed": True,
        "configured": True,
        "loaded": "UNKNOWN",
        "approved": "UNKNOWN",
        "observed": "UNKNOWN",
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


def test_posix_default_aliases_preserve_case_distinct_roots(tmp_path: Path) -> None:
    upper = tmp_path / "A" / "foo"
    lower = tmp_path / "a" / "foo"

    assert _default_project_alias(upper, platform="posix") != _default_project_alias(
        lower, platform="posix"
    )
    assert _default_project_alias(upper, platform="nt") == _default_project_alias(
        lower, platform="nt"
    )

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import latent_compass.confined_io as confined_io
import latent_compass.shadow_harness as shadow_harness
import latent_compass.shadow_install as shadow_install
from latent_compass.confined_io import read_confined_file as confined_read
from latent_compass.confined_io import replace_file as confined_replace
from latent_compass.confined_io import write_new_file as confined_write
from latent_compass.errors import ContractViolation
from latent_compass.shadow_harness import host_command_home_is_eligible, load_shadow_config
from latent_compass.shadow_install import (
    _atomic_bytes,
    _atomic_text,
    _backup,
    _begin_transaction,
    _bytes_digest,
    _command,
    _command_references_wrapper,
    _confirm_create_publication,
    _default_project_alias,
    _exclusive_json,
    _json_bytes,
    _merged_host_config,
    _observe_recovery_targets,
    _packaged_hook_text,
    _path_entry_exists,
    _pending_recovery_preview,
    _pending_recovery_session,
    _recover_pending_transaction,
    _regular_file_bytes_or_none,
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
    codex_hooks = home / ".codex" / "hooks.json"
    claude_settings = home / ".claude" / "settings.json"
    _write(codex_hooks, {"hooks": {"PreToolUse": []}})
    _write(claude_settings, {"permissions": {"deny": []}, "hooks": {"PreToolUse": []}})

    first = install_shadow_hooks(
        home=home,
        runtime_python=runtime,
        project_root=project,
        backup_tag="test",
    )
    second = install_shadow_hooks(
        home=home,
        runtime_python=runtime,
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
    journal = home / ".latent-compass-shadow.pending.json"
    assert not journal.exists()


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
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "backup_collision"
    assert conflicts[0]["path"] == str(backup)
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

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "backup_collision"
    assert conflicts[0]["path"] == str(backup)
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

    with pytest.raises(ValueError, match="refusing"):
        _backup(source, "race", home=tmp_path)

    assert source.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_install_structures_backup_source_link_refusal_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    real_replace = confined_io.ConfinedFileLease.replace
    refused = False

    def refuse_backup(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal refused
        if ".bak-latent-compass-" in lease.path.name:
            refused = True
            raise ContractViolation("injected backup confinement refusal")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", refuse_backup)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="linked-source",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert "backup confinement refusal" in str(conflicts[0]["detail"])
    assert refused is True
    assert hooks.read_bytes() == before
    assert not (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    ).exists()
    assert (home / ".latent-compass-shadow.pending.json").is_file()


def test_install_rejects_backup_bytes_that_do_not_match_transaction_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    real_replace = confined_io.ConfinedFileLease.replace
    backed_up: bytes | None = None

    def capture_backup(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal backed_up
        if lease.path.name == "hooks.json.bak-latent-compass-aba":
            backed_up = data
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", capture_backup)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="aba",
        hosts=("codex",),
    )

    assert result["conflicts"] == []
    assert backed_up == before
    assert hooks.with_name("hooks.json.bak-latent-compass-aba").read_bytes() == before


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
    real_replace = confined_io.ConfinedFileLease.replace

    def fail_on_config(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path.name == "config.json":
            raise OSError("injected config write failure")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", fail_on_config)

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


def test_rollback_revocation_preserves_later_same_byte_peer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    real_replace = confined_io.ConfinedFileLease.replace

    def fail_on_config(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path.name == "config.json":
            raise OSError("injected config write failure")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", fail_on_config)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="revoke-rollback",
        hosts=("codex",),
    )

    assert cast(list[dict[str, object]], result["conflicts"])[0]["code"] == "apply_failed"
    journal = home / ".latent-compass-shadow.pending.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    wrapper_entry = next(item for item in payload["entries"] if item["path"] == str(wrapper))
    assert wrapper_entry["publication_state"] == "revoked"
    peer = _packaged_hook_text().encode("utf-8")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes(peer)

    recovery = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert wrapper.read_bytes() == peer
    assert journal.exists()


def test_rollback_revocation_refusal_is_structured_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    journal = home / ".latent-compass-shadow.pending.json"
    real_replace = confined_io.ConfinedFileLease.replace

    def fail_config_and_revocation(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path == hooks:
            raise OSError("injected settings failure")
        if lease.path == journal and b'"publication_state": "revoked"' in data:
            raise ContractViolation("injected revocation refusal")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", fail_config_and_revocation)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="revocation-refusal",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert wrapper.is_file()
    assert journal.is_file()


def test_install_rolls_back_when_writer_fails_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    before = hooks.read_bytes()
    real_replace = confined_io.ConfinedFileLease.replace
    injected = False

    def replace_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal injected
        real_replace(lease, data)
        if lease.path == hooks and not injected:
            injected = True
            raise OSError("injected failure after atomic publication")

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", replace_then_fail)
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
    assert hooks.read_bytes() != before
    assert not (store / "config.json").exists()
    assert not (store / "ownership.json").exists()
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert hooks.read_bytes() == before
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
    real_replace = confined_io.ConfinedFileLease.replace
    injected = False

    def backup_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal injected
        real_replace(lease, data)
        if lease.path.name == "hooks.json.bak-latent-compass-backup-published" and not injected:
            injected = True
            raise OSError("injected failure after backup publication")

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", backup_then_fail)
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
    real_replace = confined_io.ConfinedFileLease.replace
    substitution_blocked = False

    def publish_substitute_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal substitution_blocked
        real_replace(lease, data)
        if lease.path == config:
            try:
                config.write_bytes(third_party)
            except PermissionError:
                substitution_blocked = True
            raise OSError("injected post-publication observation failure")

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", publish_substitute_then_fail)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="post-observation",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed"]
    if os.name == "nt":
        assert substitution_blocked is True
        assert config.is_file()
        assert config.read_bytes() != third_party
    else:
        assert substitution_blocked is False
        assert config.read_bytes() == third_party
    assert (home / ".latent-compass-shadow.pending.json").is_file()


def test_concurrent_pending_journal_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    journal = home / ".latent-compass-shadow.pending.json"
    real_exclusive = _exclusive_json
    real_replace = confined_io.ConfinedFileLease.replace
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

    def race_journal(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal injected
        if lease.path == journal and not injected:
            injected = True
            real_exclusive(journal, rival, home=home)
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", race_journal)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="ours",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
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
    real_remove = confined_io.ConfinedFileLease.remove
    swapped = False

    def refuse_changed_cleanup(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal swapped
        if lease.path == journal and not swapped:
            swapped = True
            raise ContractViolation("injected journal identity replacement")
        real_remove(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", refuse_changed_cleanup)
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
    assert journal.is_file()


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
    real_remove = confined_io.ConfinedFileLease.remove
    swapped = False

    def refuse_changed_cleanup(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal swapped
        if lease.path == journal and not swapped:
            swapped = True
            raise ContractViolation("injected journal identity replacement")
        real_remove(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", refuse_changed_cleanup)
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert swapped is True
    assert journal.is_file()


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
    real_replace = confined_io.ConfinedFileLease.replace

    def fail_on_config_backup(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path.name == "config.json.bak-latent-compass-fault":
            raise OSError("injected config backup failure")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", fail_on_config_backup)

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
    real_remove = confined_io.ConfinedFileLease.remove
    injected = False

    def remove_then_fail(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal injected
        real_remove(lease)
        if lease.path.name == "config.json" and not injected:
            injected = True
            raise OSError("injected failure after confined removal")

    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", remove_then_fail)
    result = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert not (store / "config.json").exists()
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    recovery = recover_shadow_hooks(home=home)
    assert recovery["conflicts"] == []
    assert all(path.read_bytes() == content for path, content in before.items())
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
    real_replace = confined_io.ConfinedFileLease.replace
    injected = False

    def backup_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal injected
        real_replace(lease, data)
        if (
            lease.path.name == "hooks.json.bak-latent-compass-remove-backup-published"
            and not injected
        ):
            injected = True
            raise OSError("injected failure after backup publication")

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", backup_then_fail)
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
    real_remove = confined_io.ConfinedFileLease.remove
    swapped = False

    def refuse_changed_delete(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal swapped
        if lease.path == config and not swapped:
            swapped = True
            raise ContractViolation("injected target identity change")
        real_remove(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", refuse_changed_delete)
    result = remove_shadow_hooks(home=home, backup_tag="remove", hosts=("codex",))

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed"]
    assert swapped is True
    assert config.read_bytes() == original
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
    replacement_blocked = False

    def inject_then_validate(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal replacement_blocked
        try:
            _write(hooks, concurrent)
        except PermissionError:
            replacement_blocked = True
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
    if replacement_blocked:
        assert conflicts == []
        installed = json.loads(hooks.read_text(encoding="utf-8"))
        assert installed != concurrent
        assert installed["hooks"]["PreToolUse"]
        assert (home / ".codex" / "latent-compass-shadow" / "config.json").is_file()
        assert list(home.rglob("*.bak-latent-compass-concurrent"))
    else:
        assert conflicts[0]["code"] == "concurrent_change"
        assert json.loads(hooks.read_text(encoding="utf-8")) == concurrent
        assert not (home / ".codex" / "latent-compass-shadow" / "config.json").exists()
        assert not list(home.rglob("*.bak-latent-compass-concurrent"))


def test_install_binds_payload_and_digest_to_one_planning_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    original = hooks.read_bytes()
    real_lease = confined_io.lease_confined_file
    hook_observations = 0

    def count_hook_observation(
        root: Path,
        target: Path,
        *,
        max_bytes: int,
        what: str,
        allow_absent: bool = False,
        create_parents: bool = False,
    ) -> confined_io.ConfinedFileLease:
        nonlocal hook_observations
        if target == hooks:
            hook_observations += 1
        return real_lease(
            root,
            target,
            max_bytes=max_bytes,
            what=what,
            allow_absent=allow_absent,
            create_parents=create_parents,
        )

    monkeypatch.setattr(shadow_install, "lease_confined_file", count_hook_observation)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="single-read",
        hosts=("codex",),
    )

    assert result["conflicts"] == []
    assert hook_observations == 1
    assert hooks.read_bytes() != original
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_retains_target_identity_from_plan_through_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    peer = tmp_path / "peer-hooks.json"
    peer.write_bytes(hooks.read_bytes())
    real_snapshot = _transaction_snapshot
    replacement_blocked = False

    def replace_before_snapshot(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal replacement_blocked
        try:
            peer.replace(hooks)
        except PermissionError:
            replacement_blocked = True
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", replace_before_snapshot)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="identity-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    if os.name == "nt":
        assert replacement_blocked is True
        assert conflicts == []
    else:
        assert conflicts[0]["code"] == "concurrent_change"
    assert hooks.is_file()


def test_install_revalidates_backup_absence_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    backup = hooks.with_name("hooks.json.bak-latent-compass-backup-race")
    real_snapshot = _transaction_snapshot

    def create_backup_before_snapshot(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        backup.write_bytes(b"peer-backup")
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", create_backup_before_snapshot)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="backup-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "concurrent_change"
    assert backup.read_bytes() == b"peer-backup"
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_refuses_peer_journal_appearing_at_transaction_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    hooks_before = hooks.read_bytes()
    journal = home / ".latent-compass-shadow.pending.json"
    peer_journal: bytes | None = None
    real_replace = confined_io.ConfinedFileLease.replace

    def create_same_byte_peer_before_publish(
        lease: confined_io.ConfinedFileLease, data: bytes
    ) -> None:
        nonlocal peer_journal
        if lease.path == journal and lease.content is None and peer_journal is None:
            peer_journal = data
            journal.write_bytes(data)
        real_replace(lease, data)

    monkeypatch.setattr(
        confined_io.ConfinedFileLease, "replace", create_same_byte_peer_before_publish
    )
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="peer-journal",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert "after observation" in str(conflicts[0]["detail"])
    assert peer_journal is not None
    assert journal.read_bytes() == peer_journal
    assert hooks.read_bytes() == hooks_before
    assert not (home / ".codex" / "latent-compass-shadow" / "config.json").exists()
    assert not list(home.rglob("*.bak-latent-compass-peer-journal"))


def test_recovery_closes_target_lease_when_backup_observation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir(parents=True)
    before = b"before"
    target.write_bytes(before)
    backup_tag = "lease-cleanup"
    backup = target.with_name(f"hooks.json.bak-latent-compass-{backup_tag}")
    backup.mkdir()
    journal_raw = _json_bytes(
        {
            "schema_version": 1,
            "operation": "install",
            "backup_tag": backup_tag,
            "entries": [
                {
                    "path": str(target),
                    "before_sha256": _bytes_digest(before),
                    "after_sha256": _bytes_digest(b"after"),
                    "backup_path": str(backup),
                    "publication_state": "not_applicable",
                }
            ],
        }
    )
    real_lease = confined_io.lease_confined_file
    captured: list[confined_io.ConfinedFileLease] = []

    def capture_target_lease(
        root: Path,
        path: Path,
        *,
        max_bytes: int,
        what: str,
        allow_absent: bool = False,
    ) -> confined_io.ConfinedFileLease:
        lease = real_lease(
            root,
            path,
            max_bytes=max_bytes,
            what=what,
            allow_absent=allow_absent,
        )
        if path == target:
            captured.append(lease)
        return lease

    monkeypatch.setattr(shadow_install, "lease_confined_file", capture_target_lease)

    with pytest.raises((OSError, ContractViolation)):
        _observe_recovery_targets(home, journal_raw)

    assert len(captured) == 1
    with pytest.raises(RuntimeError, match="lease is closed"):
        captured[0].assert_current()
    peer = tmp_path / "peer-hooks.json"
    peer.write_bytes(b"peer")
    peer.replace(target)
    assert target.read_bytes() == b"peer"


def test_fresh_store_rollback_preserves_same_byte_peer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    wrapper = (
        home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
    )
    peer = tmp_path / "peer-wrapper.py"
    real_replace = confined_io.ConfinedFileLease.replace
    peer_identity: tuple[int, int] | None = None
    substitution_blocked = False

    def substitute_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal peer_identity, substitution_blocked
        if lease.path == hooks:
            raise OSError("injected late settings failure")
        real_replace(lease, data)
        if lease.path == wrapper and peer_identity is None:
            peer.write_bytes(data)
            peer_stat = peer.stat()
            peer_identity = (peer_stat.st_dev, peer_stat.st_ino)
            try:
                peer.replace(wrapper)
            except PermissionError:
                substitution_blocked = True

    with monkeypatch.context() as fault:
        fault.setattr(confined_io.ConfinedFileLease, "replace", substitute_then_fail)
        result = install_shadow_hooks(
            home=home,
            runtime_python=Path(sys.executable),
            project_root=project,
            backup_tag="fresh-peer",
            hosts=("codex",),
        )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert peer_identity is not None
    if os.name == "nt":
        assert substitution_blocked is True
        assert [item["code"] for item in conflicts] == ["apply_failed"]
        assert not wrapper.exists()
    else:
        assert substitution_blocked is False
        assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
        assert wrapper.is_file()
        wrapper_stat = wrapper.stat()
        assert (wrapper_stat.st_dev, wrapper_stat.st_ino) == peer_identity
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    wrapper_entry = next(
        entry for entry in journal_payload["entries"] if entry["path"] == str(wrapper)
    )
    assert wrapper_entry["publication_state"] == "revoked"

    recovery = recover_shadow_hooks(home=home)
    recovery_conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    if os.name == "nt":
        assert recovery_conflicts == []
        assert not journal.exists()
        assert peer.read_bytes() == _packaged_hook_text().encode("utf-8")
    else:
        assert recovery_conflicts[0]["code"] == "pending_transaction_conflict"
        assert "unconfirmed" in str(recovery_conflicts[0]["detail"])
        assert wrapper.is_file()
        wrapper_stat = wrapper.stat()
        assert (wrapper_stat.st_dev, wrapper_stat.st_ino) == peer_identity
        assert journal.is_file()


@pytest.mark.skipif(os.name != "nt", reason="native Windows publication semantics")
def test_windows_uncertain_journal_publication_returns_structured_rollback_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    journal = home / ".latent-compass-shadow.pending.json"
    real_writer = confined_io._write_windows_at  # noqa: SLF001 - native backend witness
    journal_publications = 0

    def fail_later_journal_publication(
        lease: confined_io.ConfinedFileLease,
        data: bytes,
        *,
        replace: bool,
        before_publish: Callable[[], None] | None = None,
        retain_published_handle: bool = False,
    ) -> int | None:
        nonlocal journal_publications
        fail_after_publish = False
        if lease.path == journal:
            journal_publications += 1
            fail_after_publish = journal_publications == 4
        published_handle = real_writer(
            lease,
            data,
            replace=replace,
            before_publish=before_publish,
            retain_published_handle=(False if fail_after_publish else retain_published_handle),
        )
        if fail_after_publish:
            raise OSError("injected journal failure after publication")
        return published_handle

    monkeypatch.setattr(confined_io, "_write_windows_at", fail_later_journal_publication)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="uncertain-journal",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert journal_publications == 4
    assert [item["code"] for item in conflicts] == ["apply_failed", "rollback_conflict"]
    assert "journal failure after publication" in str(conflicts[0]["detail"])
    assert journal.is_file()


def test_deep_host_json_returns_structured_install_and_status_refusal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    deep_json = '{"hooks":' + "[" * 10_000 + "0" + "]" * 10_000 + "}"
    hooks.write_text(deep_json, encoding="utf-8")

    install = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="deep-json",
        hosts=("codex",),
    )
    install_conflicts = cast(list[dict[str, object]], install["conflicts"])
    assert install_conflicts[0]["code"] == "configuration_collision"
    assert not (home / ".latent-compass-shadow.pending.json").exists()
    status = host_status(home=home, project_root=project, hosts=("codex",), dry_run=False)
    state = cast(dict[str, dict[str, object]], status["states"])["codex"]
    assert state["installed"] is False
    assert state["configured"] is False
    snapshots = cast(list[dict[str, object]], status["hosts"])
    assert snapshots[0]["status"] == "HOST_CONFIGURATION_INVALID"

    peer = tmp_path / "peer-hooks.json"
    peer.write_text(deep_json, encoding="utf-8")
    peer.replace(hooks)
    assert hooks.read_text(encoding="utf-8") == deep_json


def test_serialization_depth_refusal_closes_planning_leases_with_valid_control(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    depth = 65
    nested = "[" * depth + "0" + "]" * depth
    raw = '{"hooks":{"PreToolUse":[]},"deep":' + nested + "}"
    assert isinstance(json.loads(raw), dict)
    hooks.write_text(raw, encoding="utf-8")

    refused = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="serialize-depth",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], refused["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert not (home / ".latent-compass-shadow.pending.json").exists()
    peer = tmp_path / "peer-hooks.json"
    peer.write_text(raw, encoding="utf-8")
    peer.replace(hooks)
    status = host_status(home=home, project_root=project, hosts=("codex",), dry_run=False)
    snapshots = cast(list[dict[str, object]], status["hosts"])
    assert isinstance(snapshots[0]["status"], str)

    control_home = tmp_path / "control-home"
    control_hooks = control_home / ".codex" / "hooks.json"
    control_hooks.parent.mkdir(parents=True)
    control_nested = "[" * 32 + "0" + "]" * 32
    control_hooks.write_text(
        '{"hooks":{"PreToolUse":[]},"deep":' + control_nested + "}",
        encoding="utf-8",
    )
    accepted = install_shadow_hooks(
        home=control_home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="serialize-control",
        hosts=("codex",),
    )
    assert accepted["conflicts"] == []


@pytest.mark.parametrize(
    ("literal", "value"),
    [("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))],
)
def test_nonfinite_host_json_is_refused_in_parse_serialize_and_status(
    tmp_path: Path, literal: str, value: float
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    raw = '{"hooks":{"PreToolUse":[]},"foreign_number":' + literal + "}"
    hooks.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="numbers must be finite"):
        _json_bytes({"foreign_number": value})
    refused = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="nonfinite",
        hosts=("codex",),
    )
    conflicts = cast(list[dict[str, object]], refused["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert f"constant {literal} is not permitted" in str(conflicts[0]["detail"])
    assert not (home / ".latent-compass-shadow.pending.json").exists()
    status = host_status(home=home, project_root=project, hosts=("codex",))
    snapshots = cast(list[dict[str, object]], status["hosts"])
    assert snapshots[0]["status"] == "HOST_CONFIGURATION_INVALID"
    peer = tmp_path / "peer-hooks.json"
    peer.write_text(raw, encoding="utf-8")
    peer.replace(hooks)
    assert hooks.read_text(encoding="utf-8") == raw


def test_finite_host_json_number_remains_supported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    finite = 1.7976931348623157e308
    raw = _json_bytes({"hooks": {"PreToolUse": []}, "foreign_number": finite})
    hooks.write_bytes(raw)

    accepted = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="finite",
        hosts=("codex",),
    )

    assert accepted["conflicts"] == []


def test_serialized_host_growth_is_bounded_before_journal_with_adjacent_control(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def host_payload(width: int) -> tuple[bytes, bytes]:
        wide = ",".join("0" for _ in range(width))
        nested = "[" * 62 + "[" + wide + "]" + "]" * 62
        compact = ('{"hooks":{"PreToolUse":[]},"foreign":' + nested + "}").encode()
        payload = json.loads(compact.decode())
        return compact, _json_bytes(payload)

    oversized_input, oversized_after = host_payload(9_000)
    assert len(oversized_input) < 1_048_576
    assert 1_048_576 < len(oversized_after) < 8 * 1_048_576
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(oversized_input)
    before = hooks.read_bytes()

    preview = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="growth",
    )
    applied = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="growth",
        hosts=("codex",),
    )

    for report in (preview, applied):
        conflicts = cast(list[dict[str, object]], report["conflicts"])
        assert conflicts[0]["code"] == "configuration_collision"
        assert "supported bound" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == before
    assert not (home / ".latent-compass-shadow.pending.json").exists()

    control_input, control_after = host_payload(5_000)
    assert len(control_after) < 1_048_576
    control_home = tmp_path / "control-home"
    control_hooks = control_home / ".codex" / "hooks.json"
    control_hooks.parent.mkdir(parents=True)
    control_hooks.write_bytes(control_input)
    control = install_shadow_hooks(
        home=control_home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="growth-control",
        hosts=("codex",),
    )
    assert control["conflicts"] == []
    status = host_status(home=control_home, project_root=project, hosts=("codex",))
    states = cast(dict[str, dict[str, object]], status["states"])
    assert states["codex"]["configured"] is True
    recovery = plan_recover_shadow_hooks(home=control_home)
    assert recovery["conflicts"] == []
    assert recovery["recovery"] == {"pending": False, "files": []}


@pytest.mark.parametrize("hidden", ["NaN", "Infinity", "-Infinity"])
def test_duplicate_host_keys_cannot_hide_nonfinite_or_deep_values(
    tmp_path: Path, hidden: str
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    raw = '{"hooks":{"PreToolUse":[]},"hidden":' + hidden + ',"hidden":0}'
    hooks.write_text(raw, encoding="utf-8")

    refused = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="duplicate",
        hosts=("codex",),
    )
    conflicts = cast(list[dict[str, object]], refused["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert "not permitted" in str(conflicts[0]["detail"])
    status = host_status(home=home, project_root=project, hosts=("codex",))
    snapshots = cast(list[dict[str, object]], status["hosts"])
    assert snapshots[0]["status"] == "HOST_CONFIGURATION_INVALID"
    assert not (home / ".latent-compass-shadow.pending.json").exists()

    deep = "[" * 65 + "0" + "]" * 65
    deep_raw = '{"hooks":{"PreToolUse":[]},"hidden":' + deep + ',"hidden":0}'
    hooks.write_text(deep_raw, encoding="utf-8")
    deep_refused = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="duplicate-deep",
        hosts=("codex",),
    )
    deep_conflicts = cast(list[dict[str, object]], deep_refused["conflicts"])
    assert deep_conflicts[0]["code"] == "configuration_collision"
    assert "duplicate key" in str(deep_conflicts[0]["detail"])
    assert not (home / ".latent-compass-shadow.pending.json").exists()


@pytest.mark.parametrize("target_kind", ["ownership", "registration"])
@pytest.mark.parametrize("hidden", ["NaN", "Infinity", "-Infinity", "deep"])
def test_managed_registration_json_rejects_hidden_duplicate_values_everywhere(
    tmp_path: Path, target_kind: str, hidden: str
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    installed = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="strict-json",
        backup_tag="strict-initial",
        hosts=("codex",),
    )
    assert installed["conflicts"] == []
    store = home / ".codex" / "latent-compass-shadow"
    target = store / ("ownership.json" if target_kind == "ownership" else "config.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, separators=(",", ":"))
    duplicate_key = "schema_version" if target_kind == "ownership" else "contract_version"
    assert canonical.startswith(f'{{"{duplicate_key}":')
    hidden_value = "[" * 65 + "0" + "]" * 65 if hidden == "deep" else hidden
    poisoned = f'{{"{duplicate_key}":' + hidden_value + "," + canonical[1:]
    target.write_text(poisoned, encoding="utf-8")
    before = target.read_bytes()

    reinstall = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="strict-json",
        backup_tag="strict-reinstall",
        hosts=("codex",),
    )
    removed = remove_shadow_hooks(
        home=home,
        project_root=project,
        project_alias="strict-json",
        backup_tag="strict-remove",
        hosts=("codex",),
    )
    status = host_status(home=home, project_root=project, hosts=("codex",))

    for report in (reinstall, removed):
        conflicts = cast(list[dict[str, object]], report["conflicts"])
        assert conflicts[0]["code"] == "configuration_collision"
    snapshots = cast(list[dict[str, object]], status["hosts"])
    expected_status = (
        "HOST_CONFIGURATION_INVALID"
        if target_kind == "ownership"
        else "SHADOW_CONFIGURATION_INVALID"
    )
    assert snapshots[0]["status"] == expected_status
    assert target.read_bytes() == before
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_revalidates_custom_hook_script_dependency_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    custom_hook = home / ".codex" / "latent-compass-shadow" / "custom-hook.py"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_bytes(b"custom-a")
    real_snapshot = _transaction_snapshot
    replacement_blocked = False

    def replace_custom_hook_before_validation(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal replacement_blocked
        try:
            custom_hook.write_bytes(b"custom-b")
        except PermissionError:
            replacement_blocked = True
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(
        shadow_install, "_transaction_snapshot", replace_custom_hook_before_validation
    )
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_hook,
        project_root=project,
        backup_tag="custom-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    if replacement_blocked:
        assert conflicts == []
        assert (custom_hook.parent / "ownership.json").is_file()
    else:
        assert conflicts[0]["code"] == "concurrent_change"
        assert not (custom_hook.parent / "ownership.json").exists()
    assert custom_hook.read_bytes() == (b"custom-a" if replacement_blocked else b"custom-b")


def test_custom_hook_inside_selected_store_has_complete_lifecycle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    store = home / ".codex" / "latent-compass-shadow"
    custom_hook = store / "runtime" / "custom-hook.py"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_bytes(b"custom-owned-by-operator")

    installed = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_hook,
        project_root=project,
        project_alias="custom",
        backup_tag="custom-install",
        hosts=("codex",),
    )
    status = host_status(
        home=home,
        project_root=project,
        hosts=("codex",),
    )
    removed = remove_shadow_hooks(
        home=home,
        project_root=project,
        project_alias="custom",
        backup_tag="custom-remove",
        hosts=("codex",),
    )

    assert installed["conflicts"] == []
    states = cast(dict[str, dict[str, object]], status["states"])
    assert states["codex"]["installed"] is True
    assert states["codex"]["configured"] is True
    assert removed["conflicts"] == []
    assert custom_hook.read_bytes() == b"custom-owned-by-operator"
    assert not (store / "ownership.json").exists()


def test_reinstall_refuses_edited_owned_custom_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    store = home / ".codex" / "latent-compass-shadow"
    custom_hook = store / "runtime" / "custom-hook.py"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_bytes(b"custom-a")
    first = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_hook,
        project_root=project,
        backup_tag="custom-first",
        hosts=("codex",),
    )
    ownership = store / "ownership.json"
    ownership_before = ownership.read_bytes()
    hooks_before = hooks.read_bytes()
    custom_hook.write_bytes(b"custom-b")

    second = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_hook,
        project_root=project,
        backup_tag="custom-second",
        hosts=("codex",),
    )

    assert first["conflicts"] == []
    conflicts = cast(list[dict[str, object]], second["conflicts"])
    assert conflicts[0]["code"] == "wrapper_integrity_collision"
    assert ownership.read_bytes() == ownership_before
    assert hooks.read_bytes() == hooks_before
    assert custom_hook.read_bytes() == b"custom-b"


def test_legacy_implicit_home_is_consistent_for_default_home_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_home = tmp_path / "default-home"
    monkeypatch.setattr(Path, "home", lambda: default_home)
    project = tmp_path / "project"
    project.mkdir()
    settings = default_home / ".codex" / "hooks.json"
    _write(settings, {"hooks": {"PreToolUse": []}})
    installed = install_shadow_hooks(
        home=default_home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="legacy-default",
        backup_tag="legacy-default",
        hosts=("codex",),
    )
    assert installed["conflicts"] == []
    store = default_home / ".codex" / "latent-compass-shadow"
    wrapper = store / "runtime" / "latent-compass-shadow-hook.py"
    ownership_path = store / "ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    previous_command = str(ownership["command"])
    legacy_command = _command("codex", Path(sys.executable), wrapper, home=None)
    ownership["command"] = legacy_command
    _write(ownership_path, ownership)
    settings_payload = json.loads(settings.read_text(encoding="utf-8"))
    for groups in settings_payload["hooks"].values():
        for group in groups:
            for handler in group.get("hooks", []):
                if handler.get("command") == previous_command:
                    handler["command"] = legacy_command
    _write(settings, settings_payload)

    status = host_status(home=default_home, project_root=project, hosts=("codex",))
    states = cast(dict[str, dict[str, object]], status["states"])
    install_plan = plan_install_shadow_hooks(
        home=default_home,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="legacy-default",
        hosts=("codex",),
    )
    remove_plan = plan_remove_shadow_hooks(
        home=default_home,
        project_root=project,
        project_alias="legacy-default",
        hosts=("codex",),
    )

    assert states["codex"]["installed"] is True
    assert states["codex"]["configured"] is True
    assert install_plan["conflicts"] == []
    assert remove_plan["conflicts"] == []
    assert host_command_home_is_eligible(None, default_home) is True
    assert host_command_home_is_eligible(None, tmp_path / "other-home") is False
    assert host_command_home_is_eligible(Path("relative-home"), default_home) is False
    assert host_command_home_is_eligible(default_home, default_home) is True
    assert host_command_home_is_eligible(tmp_path / "other-home", default_home) is False


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize(
    "malformed",
    ["invalid_json", "duplicate", "string", "object", "number", "boolean", "null"],
)
def test_partial_and_final_remove_refuse_malformed_settings_before_registration_mutation(
    tmp_path: Path, host: str, malformed: str
) -> None:
    selected_host = cast(Host, host)
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    settings = home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")
    _write(settings, {"hooks": {"PreToolUse": []}})
    for project, alias in ((first, "first"), (second, "second")):
        installed = install_shadow_hooks(
            home=home,
            runtime_python=Path(sys.executable),
            project_root=project,
            project_alias=alias,
            backup_tag=f"install-{alias}",
            hosts=(selected_host,),
        )
        assert installed["conflicts"] == []
    store = home / f".{host}" / "latent-compass-shadow"
    config = store / "config.json"
    config_before = config.read_bytes()
    if malformed == "invalid_json":
        poisoned = "not-json"
    elif malformed == "duplicate":
        poisoned = '{"hooks":{},"hidden":NaN,"hidden":0}'
    elif malformed in {"string", "object", "number", "boolean", "null"}:
        payload = json.loads(settings.read_text(encoding="utf-8"))
        invalid_event_values: dict[str, object] = {
            "string": "invalid",
            "object": {},
            "number": 1,
            "boolean": False,
            "null": None,
        }
        payload["hooks"]["Unrelated"] = invalid_event_values[malformed]
        poisoned = json.dumps(payload)
    else:
        raise AssertionError(f"unsupported malformed case: {malformed}")
    settings.write_text(poisoned, encoding="utf-8")
    settings_before = settings.read_bytes()

    partial = remove_shadow_hooks(
        home=home,
        project_root=first,
        project_alias="first",
        backup_tag="remove-partial",
        hosts=(selected_host,),
    )
    final = remove_shadow_hooks(
        home=home,
        backup_tag="remove-final",
        hosts=(selected_host,),
    )
    status = host_status(home=home, project_root=first, hosts=(selected_host,))

    for report in (partial, final):
        conflicts = cast(list[dict[str, object]], report["conflicts"])
        assert conflicts[0]["code"] == "configuration_collision"
    snapshots = cast(list[dict[str, object]], status["hosts"])
    states = cast(dict[str, dict[str, object]], status["states"])
    assert snapshots[0]["status"] == "HOST_CONFIGURATION_INVALID"
    assert states[host]["installed"] is False
    assert states[host]["configured"] is False
    assert config.read_bytes() == config_before
    assert settings.read_bytes() == settings_before
    assert not (home / ".latent-compass-shadow.pending.json").exists()


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_two_project_remove_valid_settings_preserves_then_removes_registration(
    tmp_path: Path, host: str
) -> None:
    selected_host = cast(Host, host)
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    settings = home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")
    _write(settings, {"hooks": {"PreToolUse": []}})
    for project, alias in ((first, "first"), (second, "second")):
        installed = install_shadow_hooks(
            home=home,
            runtime_python=Path(sys.executable),
            project_root=project,
            project_alias=alias,
            backup_tag=f"install-{alias}",
            hosts=(selected_host,),
        )
        assert installed["conflicts"] == []

    partial = remove_shadow_hooks(
        home=home,
        project_root=first,
        project_alias="first",
        backup_tag="remove-first",
        hosts=(selected_host,),
    )
    remaining = host_status(home=home, project_root=second, hosts=(selected_host,))
    final = remove_shadow_hooks(
        home=home,
        project_root=second,
        project_alias="second",
        backup_tag="remove-second",
        hosts=(selected_host,),
    )

    assert partial["conflicts"] == []
    states = cast(dict[str, dict[str, object]], remaining["states"])
    assert states[host]["installed"] is True
    assert states[host]["configured"] is True
    assert final["conflicts"] == []


def test_installed_relative_custom_command_uses_selected_home_from_other_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_home = tmp_path / "selected-home"
    process_home = tmp_path / "process-home"
    process_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    settings = selected_home / ".claude" / "settings.json"
    _write(settings, {"hooks": {"PreToolUse": []}})
    store = selected_home / ".claude" / "latent-compass-shadow"
    custom_hook = store / "runtime" / "custom-hook.py"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_text(_packaged_hook_text(), encoding="utf-8")
    monkeypatch.chdir(store)

    installed = install_shadow_hooks(
        home=selected_home,
        runtime_python=Path(sys.executable),
        hook_script=Path("runtime/custom-hook.py"),
        project_root=project,
        project_alias="selected-home",
        backup_tag="selected-home",
        hosts=("claude",),
    )
    ownership = json.loads((store / "ownership.json").read_text(encoding="utf-8"))
    command = str(ownership["command"])
    arguments = shlex.split(command)
    assert Path(arguments[1]) == custom_hook
    assert arguments[4] == "--home"
    assert Path(arguments[5]) == selected_home
    source_cache = store / "source" / "selected-home.json"
    source_cache.parent.mkdir()
    source_cache.write_text(
        json.dumps(
            {
                "owner": "latent-compass-shadow",
                "source_declaration_digest": "sha256:" + "1" * 64,
                "source_observed_at": "2026-09-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    environment = {**os.environ, "HOME": str(process_home), "USERPROFILE": str(process_home)}
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(project),
        "session_id": "session",
        "turn_id": "turn",
        "tool_name": "Read",
    }

    completed = subprocess.run(  # noqa: S603 - exact installed argv under test
        arguments,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=project,
        env=environment,
        check=False,
        timeout=10,
    )

    assert installed["conflicts"] == []
    assert completed.returncode == 0
    assert list((store / "events" / "selected-home").rglob("*.json"))
    assert not (process_home / ".claude" / "latent-compass-shadow").exists()


def test_relative_home_freezes_absolute_wrapper_and_transaction_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    home_argument = Path("relative-home")
    selected_home = tmp_path / home_argument
    project = tmp_path / "project"
    project.mkdir()
    settings = selected_home / ".claude" / "settings.json"
    _write(settings, {"hooks": {"PreToolUse": []}})

    installed = install_shadow_hooks(
        home=home_argument,
        runtime_python=Path(sys.executable),
        project_root=project,
        project_alias="relative-home",
        backup_tag="relative-home",
        hosts=("claude",),
    )
    store = selected_home / ".claude" / "latent-compass-shadow"
    ownership = json.loads((store / "ownership.json").read_text(encoding="utf-8"))
    arguments = shlex.split(str(ownership["command"]))
    assert Path(arguments[1]).is_absolute()
    assert Path(arguments[5]) == selected_home
    files = cast(list[dict[str, object]], installed["files"])
    assert all(Path(str(item["path"])).is_absolute() for item in files)
    assert not (tmp_path / ".latent-compass-shadow.pending.json").exists()


def test_relative_runtime_is_serialized_as_lexical_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    _write(settings, {"hooks": {"PreToolUse": []}})
    runtime = tmp_path / "tools" / "python"
    runtime.parent.mkdir()
    runtime.write_bytes(b"runtime-fixture")

    installed = install_shadow_hooks(
        home=home,
        runtime_python=Path("tools/python"),
        project_root=project,
        project_alias="relative-runtime",
        backup_tag="relative-runtime",
        hosts=("claude",),
    )
    ownership = json.loads(
        (home / ".claude" / "latent-compass-shadow" / "ownership.json").read_text(encoding="utf-8")
    )
    arguments = shlex.split(str(ownership["command"]))
    monkeypatch.chdir(project)

    assert installed["conflicts"] == []
    assert Path(arguments[0]) == runtime
    assert Path(arguments[0]).is_file()


def test_remove_revalidates_custom_hook_dependency_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    store = home / ".codex" / "latent-compass-shadow"
    custom_hook = store / "runtime" / "custom-hook.py"
    custom_hook.parent.mkdir(parents=True)
    custom_hook.write_bytes(b"custom-a")
    installed = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom_hook,
        project_root=project,
        project_alias="custom",
        backup_tag="custom-install",
        hosts=("codex",),
    )
    ownership = store / "ownership.json"
    hooks_before = hooks.read_bytes()
    real_snapshot = _transaction_snapshot
    replacement_blocked = False

    def replace_custom_hook_before_validation(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal replacement_blocked
        try:
            custom_hook.write_bytes(b"custom-b")
        except PermissionError:
            replacement_blocked = True
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(
        shadow_install, "_transaction_snapshot", replace_custom_hook_before_validation
    )
    removed = remove_shadow_hooks(
        home=home,
        project_root=project,
        project_alias="custom",
        backup_tag="custom-remove",
        hosts=("codex",),
    )

    assert installed["conflicts"] == []
    conflicts = cast(list[dict[str, object]], removed["conflicts"])
    if replacement_blocked:
        assert conflicts == []
        assert not ownership.exists()
        assert hooks.read_bytes() != hooks_before
    else:
        assert conflicts[0]["code"] == "concurrent_change"
        assert ownership.is_file()
        assert hooks.read_bytes() == hooks_before
    assert custom_hook.read_bytes() == (b"custom-a" if replacement_blocked else b"custom-b")


def test_install_validates_unchanged_dependencies_before_journal(
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
        backup_tag="first",
        hosts=("codex",),
    )
    foreign = b'{"hooks":{"PreToolUse":[{"matcher":"foreign","hooks":[]}]}}\n'
    real_snapshot = _transaction_snapshot
    replacement_blocked = False

    def mutate_before_validation(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal replacement_blocked
        try:
            hooks.write_bytes(foreign)
        except PermissionError:
            replacement_blocked = True
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", mutate_before_validation)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="second",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    if replacement_blocked:
        assert conflicts == []
        assert hooks.read_bytes() != foreign
    else:
        assert conflicts[0]["code"] == "concurrent_change"
        assert hooks.read_bytes() == foreign
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_revalidates_project_root_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    real_snapshot = _transaction_snapshot

    def remove_project_before_snapshot(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        project.rmdir()
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", remove_project_before_snapshot)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="project-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "concurrent_change"
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_revalidates_absent_target_parent_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    store = home / ".codex" / "latent-compass-shadow"
    outside = tmp_path / "outside-store"
    outside.mkdir()
    real_snapshot = _transaction_snapshot

    def link_store_before_snapshot(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        store.symlink_to(outside, target_is_directory=True)
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", link_store_before_snapshot)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="parent-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "concurrent_change"
    assert list(outside.iterdir()) == []
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_custom_install_rejects_non_array_hook_event_before_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"Unrelated": "invalid"}})
    custom = home / ".codex" / "latent-compass-shadow" / "custom.py"
    custom.parent.mkdir(parents=True)
    custom.write_text("# custom\n", encoding="utf-8")

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        hook_script=custom,
        project_root=project,
        backup_tag="malformed-hooks",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert not (custom.parent / "ownership.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows filename rules")
def test_windows_backup_name_is_rejected_during_preflight(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})

    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="bad:tag",
        hosts=("codex",),
    )

    assert plan["conflicts"]
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_exclusive_create_refuses_peer_same_bytes_before_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    real_snapshot = _transaction_snapshot
    peer_bytes: bytes | None = None

    def peer_create_before_validation(
        selected_home: Path, plan: dict[str, object]
    ) -> dict[Path, bytes | None]:
        nonlocal peer_bytes
        operations = cast(list[dict[str, Any]], plan["_operations"])
        peer_bytes = next(item["after"] for item in operations if item["path"] == config)
        assert isinstance(peer_bytes, bytes)
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(peer_bytes)
        return real_snapshot(selected_home, plan)

    monkeypatch.setattr(shadow_install, "_transaction_snapshot", peer_create_before_validation)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="peer",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "concurrent_change"
    assert peer_bytes is not None
    assert config.read_bytes() == peer_bytes
    assert not (home / ".latent-compass-shadow.pending.json").exists()


def test_install_collision_after_journal_preserves_peer_and_retains_uncertain_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    hooks_before = hooks.read_bytes()
    foreign_hooks = b'{"hooks":{"PreToolUse":[{"matcher":"foreign","hooks":[]}]}}'
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    real_replace = confined_io.ConfinedFileLease.replace
    peer_bytes: bytes | None = None
    hook_substitution_blocked = False

    def peer_create_during_apply(
        lease: confined_io.ConfinedFileLease,
        data: bytes,
    ) -> None:
        nonlocal peer_bytes, hook_substitution_blocked
        if lease.path == config and lease.content is None and peer_bytes is None:
            peer_bytes = data
            config.write_bytes(data)
            try:
                hooks.write_bytes(foreign_hooks)
            except PermissionError:
                hook_substitution_blocked = True
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", peer_create_during_apply)
    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="peer-after-journal",
        hosts=("codex",),
    )
    conflicts = cast(list[dict[str, object]], result["conflicts"])
    expected_codes = ["apply_failed"] if os.name == "nt" else ["apply_failed", "rollback_conflict"]
    assert [item["code"] for item in conflicts] == expected_codes
    assert peer_bytes is not None
    assert config.read_bytes() == peer_bytes
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.is_file()
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    config_entry = next(
        entry for entry in journal_payload["entries"] if entry["path"] == str(config)
    )
    assert config_entry["publication_state"] == "attempted"
    assert hooks.read_bytes() == (hooks_before if os.name == "nt" else foreign_hooks)
    assert hook_substitution_blocked is (os.name == "nt")
    recovery = recover_shadow_hooks(home=home)
    recovery_conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert recovery_conflicts[0]["code"] == "pending_transaction_conflict"
    assert config.read_bytes() == peer_bytes
    assert hooks.read_bytes() == (hooks_before if os.name == "nt" else foreign_hooks)
    assert journal.exists()


def test_recovery_preserves_unconfirmed_future_create_after_interruption(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write(home / ".codex" / "hooks.json", {"hooks": {"PreToolUse": []}})
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        hosts=("codex",),
        backup_tag="interrupted",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, journal_content = _begin_transaction(
        home=home, operation="install", backup_tag="interrupted", plan=plan
    )
    operations = cast(list[dict[str, Any]], plan["_operations"])
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    wrapper_after = next(item["after"] for item in operations if item["path"] == wrapper)
    assert isinstance(wrapper_after, bytes)
    _atomic_bytes(wrapper, wrapper_after, home=home, expected=None)
    _confirm_create_publication(home, journal, journal_content, wrapper)
    config = home / ".codex" / "latent-compass-shadow" / "config.json"
    config_after = next(item["after"] for item in operations if item["path"] == config)
    assert isinstance(config_after, bytes)
    config.write_bytes(config_after)

    recovery = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "unconfirmed" in str(conflicts[0]["detail"])
    assert config.read_bytes() == config_after
    assert journal.is_file()


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
    real_replace = confined_io.ConfinedFileLease.replace

    def inject_on_config(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path == config and lease.content is None and not config.exists():
            config.write_bytes(third_party)
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", inject_on_config)

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
    journal = home / ".latent-compass-shadow.pending.json"
    assert journal.exists()
    recovery = recover_shadow_hooks(home=home)
    recovery_conflicts = cast(list[dict[str, object]], recovery["conflicts"])
    assert recovery_conflicts[0]["code"] == "pending_transaction_conflict"
    assert config.read_bytes() == third_party


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
    real_replace = confined_io.ConfinedFileLease.replace

    def inject_leaf_then_publish(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path == config and lease.content is None and not config.exists():
            config.write_bytes(third_party)
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", inject_leaf_then_publish)

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
    before = hooks.read_bytes()
    real_replace = confined_io.ConfinedFileLease.replace

    def refuse_changed_target(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        if lease.path == hooks:
            raise ContractViolation("injected final identity change")
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", refuse_changed_target)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="leaf-update-race",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    assert hooks.read_bytes() == before


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
    journal, journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())
    _confirm_create_publication(home, journal, journal_content, wrapper)

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
    journal, journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())
    _confirm_create_publication(home, journal, journal_content, wrapper)
    operations = cast(list[dict[str, Any]], plan["_operations"])
    settings_after = next(item["after"] for item in operations if item["path"] == hooks)
    assert isinstance(settings_after, bytes)
    _atomic_bytes(hooks, settings_after)

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
    journal, journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="crash",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    _atomic_text(wrapper, _packaged_hook_text())
    _confirm_create_publication(home, journal, journal_content, wrapper)
    real_remove = confined_io.ConfinedFileLease.remove
    swapped = False

    def refuse_changed_delete(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal swapped
        if lease.path == wrapper and not swapped:
            swapped = True
            raise ContractViolation("injected target identity replacement")
        real_remove(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", refuse_changed_delete)
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert swapped is True
    assert wrapper.is_file()
    assert journal.is_file()


def test_recovery_revokes_before_identity_check_and_never_deletes_same_byte_peer(
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
        backup_tag="identity-revoke",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, journal_content = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="identity-revoke",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    owned = _packaged_hook_text().encode("utf-8")
    _atomic_bytes(wrapper, owned, home=home, expected=None)
    _confirm_create_publication(home, journal, journal_content, wrapper)
    peer = tmp_path / "peer-wrapper.py"
    peer.write_bytes(owned)
    peer_stat = peer.stat()
    peer_identity = (peer_stat.st_dev, peer_stat.st_ino)
    replacement_blocked = False
    real_session = _pending_recovery_session

    def observe_then_substitute(selected_home: Path) -> tuple[Any, ...]:
        nonlocal replacement_blocked
        session = real_session(selected_home)
        try:
            peer.replace(wrapper)
        except PermissionError:
            replacement_blocked = True
        return session

    with monkeypatch.context() as fault:
        fault.setattr(shadow_install, "_pending_recovery_session", observe_then_substitute)
        first = recover_shadow_hooks(home=home)
    second = recover_shadow_hooks(home=home)

    if os.name == "nt":
        assert replacement_blocked is True
        assert first["conflicts"] == []
        assert second["conflicts"] == []
        assert not wrapper.exists()
        assert peer.read_bytes() == owned
        assert not journal.exists()
    else:
        assert replacement_blocked is False
        for report in (first, second):
            conflicts = cast(list[dict[str, object]], report["conflicts"])
            assert conflicts[0]["code"] == "pending_transaction_conflict"
        first_conflicts = cast(list[dict[str, object]], first["conflicts"])
        second_conflicts = cast(list[dict[str, object]], second["conflicts"])
        assert "changed after preflight" in str(first_conflicts[0]["detail"])
        assert "unconfirmed" in str(second_conflicts[0]["detail"])
        wrapper_stat = wrapper.stat()
        assert (wrapper_stat.st_dev, wrapper_stat.st_ino) == peer_identity
        payload = json.loads(journal.read_text(encoding="utf-8"))
        entry = next(item for item in payload["entries"] if item["path"] == str(wrapper))
        assert entry["publication_state"] == "revoked"
        assert journal.is_file()


def test_recovery_revokes_create_authority_before_delete(
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
        backup_tag="revoke",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, journal_content = _begin_transaction(
        home=home, operation="install", backup_tag="revoke", plan=plan
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    wrapper_after = next(
        item["after"]
        for item in cast(list[dict[str, Any]], plan["_operations"])
        if item["path"] == wrapper
    )
    assert isinstance(wrapper_after, bytes)
    _atomic_bytes(wrapper, wrapper_after)
    _confirm_create_publication(home, journal, journal_content, wrapper)
    real_remove = confined_io.ConfinedFileLease.remove
    real_replace = confined_io.ConfinedFileLease.replace
    revoked_before_delete = False

    def capture_revocation(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal revoked_before_delete
        if lease.path == journal and b'"publication_state": "revoked"' in data:
            revoked_before_delete = True
        real_replace(lease, data)

    def assert_revoked_then_remove(lease: confined_io.ConfinedFileLease) -> None:
        if lease.path == wrapper:
            assert revoked_before_delete is True
        real_remove(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", capture_revocation)
    monkeypatch.setattr(confined_io.ConfinedFileLease, "remove", assert_revoked_then_remove)
    result = recover_shadow_hooks(home=home)

    assert result["conflicts"] == []
    assert revoked_before_delete is True
    assert not wrapper.exists()
    assert not journal.exists()


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
    operations = cast(list[dict[str, Any]], plan["_operations"])
    settings_after = next(item["after"] for item in operations if item["path"] == hooks)
    assert isinstance(settings_after, bytes)
    _atomic_bytes(hooks, settings_after)
    backup = hooks.with_name("hooks.json.bak-latent-compass-crash")
    backup.write_bytes(original)
    real_assert_current = confined_io.ConfinedFileLease.assert_current
    backup_checks = 0

    def refuse_changed_backup(lease: confined_io.ConfinedFileLease) -> None:
        nonlocal backup_checks
        if lease.path == backup:
            backup_checks += 1
            raise ContractViolation("injected backup identity replacement")
        real_assert_current(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "assert_current", refuse_changed_backup)

    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "changed after preflight" in str(conflicts[0]["detail"])
    assert backup_checks == 1
    assert (home / ".latent-compass-shadow.pending.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows post-publication recovery")
def test_windows_post_rename_flush_failure_keeps_recoverable_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import latent_compass.confined_io as confined_io

    home = tmp_path / "home"
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir(parents=True)
    before = b'{"hooks":{"PreToolUse":[]}}'
    after = b'{"hooks":{"PreToolUse":[{"matcher":"owned","hooks":[]}]}}'
    target.write_bytes(before)
    backup_tag = "post-rename"
    backup = target.with_name(f"hooks.json.bak-latent-compass-{backup_tag}")
    backup.write_bytes(before)
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_bytes(
        _json_bytes(
            {
                "schema_version": 1,
                "operation": "install",
                "backup_tag": backup_tag,
                "entries": [
                    {
                        "path": str(target),
                        "before_sha256": _bytes_digest(before),
                        "after_sha256": _bytes_digest(after),
                        "backup_path": str(backup),
                    }
                ],
            }
        )
    )
    kernel32 = getattr(confined_io, "_kernel32")  # noqa: B009 - absent on POSIX type paths
    real_flush = kernel32.FlushFileBuffers
    flush_calls = 0

    def fail_second_flush(handle: int) -> int:
        nonlocal flush_calls
        flush_calls += 1
        return 0 if flush_calls == 2 else real_flush(handle)

    with monkeypatch.context() as fault:
        fault.setattr(kernel32, "FlushFileBuffers", fail_second_flush)
        with pytest.raises(OSError, match=r"hooks\.json"):
            confined_replace(home, target, after, what="managed host file")

    assert target.read_bytes() == after
    assert backup.read_bytes() == before
    assert journal.is_file()

    recovered = recover_shadow_hooks(home=home)

    assert recovered["conflicts"] == []
    assert target.read_bytes() == before
    assert not journal.exists()


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
    operations = cast(list[dict[str, Any]], plan["_operations"])
    settings_after = next(item["after"] for item in operations if item["path"] == hooks)
    assert isinstance(settings_after, bytes)
    _atomic_bytes(hooks, settings_after)
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


def test_recover_refuses_redirected_home_when_journal_is_absent(tmp_path: Path) -> None:
    outside = tmp_path / "outside-home"
    outside.mkdir()
    home = tmp_path / "home"
    try:
        home.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink support is required for this security witness: {exc}")

    report = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], report["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert list(outside.iterdir()) == []


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
    assert swapped is False
    assert journal.is_file()
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


def test_recovery_refuses_journal_created_after_empty_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b"peer-owned")
    journal = home / ".latent-compass-shadow.pending.json"
    peer_journal = _json_bytes(
        {
            "schema_version": 1,
            "operation": "install",
            "backup_tag": "peer",
            "entries": [
                {
                    "path": str(hooks),
                    "before_sha256": None,
                    "after_sha256": _bytes_digest(hooks.read_bytes()),
                    "backup_path": None,
                }
            ],
        }
    )
    real_session = _pending_recovery_session

    def preview_then_create(selected_home: Path) -> tuple[Any, ...]:
        preview = real_session(selected_home)
        journal.write_bytes(peer_journal)
        return preview

    monkeypatch.setattr(shadow_install, "_pending_recovery_session", preview_then_create)
    report = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], report["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "changed after preview" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == b"peer-owned"
    assert journal.read_bytes() == peer_journal


def test_recovery_description_uses_preflight_target_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    state_x = b"transaction-after"
    hooks.write_bytes(state_x)
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_bytes(
        _json_bytes(
            {
                "schema_version": 1,
                "operation": "install",
                "backup_tag": "observe-once",
                "entries": [
                    {
                        "path": str(hooks),
                        "before_sha256": None,
                        "after_sha256": _bytes_digest(state_x),
                        "backup_path": None,
                        "publication_confirmed": True,
                    }
                ],
            }
        )
    )
    real_lease = confined_io.lease_confined_file
    target_reads = 0

    def count_target_lease(
        root: Path,
        target: Path,
        *,
        max_bytes: int,
        what: str,
        allow_absent: bool = False,
    ) -> confined_io.ConfinedFileLease:
        nonlocal target_reads
        if target == hooks:
            target_reads += 1
        return real_lease(
            root,
            target,
            max_bytes=max_bytes,
            what=what,
            allow_absent=allow_absent,
        )

    monkeypatch.setattr(shadow_install, "lease_confined_file", count_target_lease)
    conflicts, recovery, _journal_raw, _observations = _pending_recovery_preview(home)

    assert conflicts == []
    recovery_files = cast(list[dict[str, object]], recovery["files"])
    assert recovery_files[0]["before_sha256"] == _bytes_digest(state_x)
    assert target_reads == 1
    assert hooks.read_bytes() == state_x
    assert journal.is_file()

    applied = recover_shadow_hooks(home=home)

    assert applied["conflicts"] == []
    assert not hooks.exists()
    assert not journal.exists()


def test_legacy_confirmed_revocation_is_canonical_before_failed_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    after = b"transaction-after"
    hooks.write_bytes(after)
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_bytes(
        _json_bytes(
            {
                "schema_version": 1,
                "operation": "install",
                "backup_tag": "legacy-revoke",
                "entries": [
                    {
                        "path": str(hooks),
                        "before_sha256": None,
                        "after_sha256": _bytes_digest(after),
                        "backup_path": None,
                        "publication_confirmed": True,
                    }
                ],
            }
        )
    )
    real_remove = confined_io.ConfinedFileLease.remove

    def fail_owned_delete(lease: confined_io.ConfinedFileLease) -> None:
        if lease.path == hooks:
            raise OSError("injected delete failure after revocation")
        real_remove(lease)

    with monkeypatch.context() as fault:
        fault.setattr(confined_io.ConfinedFileLease, "remove", fail_owned_delete)
        failed = recover_shadow_hooks(home=home)

    failed_conflicts = cast(list[dict[str, object]], failed["conflicts"])
    assert failed_conflicts[0]["code"] == "pending_transaction_invalid"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["publication_state"] == "revoked"
    assert "publication_confirmed" not in entry
    assert hooks.read_bytes() == after

    preview = plan_recover_shadow_hooks(home=home)
    preview_conflicts = cast(list[dict[str, object]], preview["conflicts"])
    assert preview_conflicts[0]["code"] == "pending_transaction_conflict"
    assert hooks.read_bytes() == after
    assert journal.is_file()


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
    real_lease = confined_io.lease_confined_file
    swapped = False

    def replace_before_journal_lease(
        root: Path,
        target: Path,
        *,
        max_bytes: int,
        what: str,
        allow_absent: bool = False,
    ) -> confined_io.ConfinedFileLease:
        nonlocal swapped
        if target == journal and not swapped:
            swapped = True
            journal.write_text(json.dumps(replacement), encoding="utf-8")
        return real_lease(
            root,
            target,
            max_bytes=max_bytes,
            what=what,
            allow_absent=allow_absent,
        )

    monkeypatch.setattr(shadow_install, "lease_confined_file", replace_before_journal_lease)
    report = plan_recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], report["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "backup is missing" in str(conflicts[0]["detail"])
    assert swapped is True
    assert journal.is_file()


def test_recovery_refuses_journal_replacement_before_any_target_mutation(
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
        backup_tag="preview-a",
    )
    snapshots = _transaction_snapshot(home, plan)
    journal, journal_a = _begin_transaction(
        home=home,
        operation="install",
        backup_tag="preview-a",
        plan=plan,
    )
    wrapper = next(path for path in snapshots if path.name == "latent-compass-shadow-hook.py")
    wrapper_after = next(
        item["after"]
        for item in cast(list[dict[str, Any]], plan["_operations"])
        if item["path"] == wrapper
    )
    assert isinstance(wrapper_after, bytes)
    _atomic_bytes(wrapper, wrapper_after)
    _confirm_create_publication(home, journal, journal_a, wrapper)
    journal_before = journal.read_bytes()
    real_assert_current = confined_io.ConfinedFileLease.assert_current

    def refuse_replaced_journal(lease: confined_io.ConfinedFileLease) -> None:
        if lease.path == journal:
            raise ContractViolation("injected same-byte journal identity replacement")
        real_assert_current(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "assert_current", refuse_replaced_journal)
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "pending_transaction_conflict"
    assert "changed after preview" in str(conflicts[0]["detail"])
    assert wrapper.read_bytes() == wrapper_after
    assert journal.read_bytes() == journal_before


@pytest.mark.parametrize("replacement", ["none", "different", "same-bytes"])
def test_recovery_revalidates_journal_after_backup_inputs_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    home = tmp_path / "home"
    hooks = home / ".codex" / "hooks.json"
    before = b'{"hooks":{"PreToolUse":[]}}\n'
    after = b'{"hooks":{"PreToolUse":[{"hooks":[]}]}}\n'
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(before)
    backup_tag = "recovery-boundary"
    backup = _backup(hooks, backup_tag, home=home, expected=before)
    hooks.write_bytes(after)
    journal = home / ".latent-compass-shadow.pending.json"
    payload = {
        "schema_version": 1,
        "operation": "install",
        "backup_tag": backup_tag,
        "entries": [
            {
                "path": str(hooks),
                "before_sha256": _bytes_digest(before),
                "after_sha256": _bytes_digest(after),
                "backup_path": str(backup),
                "publication_state": "not_applicable",
            }
        ],
    }
    _exclusive_json(journal, payload)
    journal_a = journal.read_bytes()
    journal_b = (
        journal_a
        if replacement == "same-bytes"
        else _json_bytes({**payload, "operation": "remove"})
    )
    real_assert_current = confined_io.ConfinedFileLease.assert_current
    peer_path: Path | None = None
    replacement_blocked = False
    swapped = False
    peer_identity: tuple[int, int] | None = None

    def swap_journal_during_backup_revalidation(
        lease: confined_io.ConfinedFileLease,
    ) -> None:
        nonlocal peer_identity, peer_path, replacement_blocked, swapped
        if lease.path == backup and replacement != "none" and not swapped:
            peer = tmp_path / f"peer-{replacement}.json"
            peer_path = peer
            peer.write_bytes(journal_b)
            peer_info = peer.stat()
            peer_identity = (peer_info.st_dev, peer_info.st_ino)
            try:
                peer.replace(journal)
            except PermissionError:
                if os.name != "nt":
                    raise
                replacement_blocked = True
            else:
                swapped = True
        real_assert_current(lease)

    monkeypatch.setattr(
        confined_io.ConfinedFileLease,
        "assert_current",
        swap_journal_during_backup_revalidation,
    )
    result = recover_shadow_hooks(home=home)

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert backup.read_bytes() == before
    if replacement == "none":
        assert conflicts == []
        assert hooks.read_bytes() == before
        assert not journal.exists()
    elif os.name == "nt":
        assert conflicts == []
        assert replacement_blocked is True
        assert swapped is False
        assert hooks.read_bytes() == before
        assert not journal.exists()
        assert peer_path is not None
        assert peer_path.read_bytes() == journal_b
    else:
        assert conflicts[0]["code"] == "pending_transaction_conflict", conflicts
        assert "changed before recovery mutation" in str(conflicts[0]["detail"])
        assert swapped is True
        assert peer_identity is not None
        journal_info = journal.stat()
        assert (journal_info.st_dev, journal_info.st_ino) == peer_identity
        assert journal.read_bytes() == journal_b
        assert hooks.read_bytes() == after


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


@pytest.mark.parametrize(
    ("schema_version", "extra_field"),
    [(True, False), (1.0, False), (1, True)],
)
def test_recover_requires_exact_journal_schema_without_mutation(
    tmp_path: Path, schema_version: object, extra_field: bool
) -> None:
    home = tmp_path / "home"
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"transaction-after")
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "operation": "install",
        "backup_tag": "strict-schema",
        "entries": [
            {
                "path": str(target),
                "before_sha256": None,
                "after_sha256": _bytes_digest(target.read_bytes()),
                "backup_path": None,
            }
        ],
    }
    if extra_field:
        payload["unexpected"] = "must-refuse"
    journal = home / ".latent-compass-shadow.pending.json"
    journal.write_bytes(_json_bytes(payload))
    journal_before = journal.read_bytes()

    preview = plan_recover_shadow_hooks(home=home)
    applied = recover_shadow_hooks(home=home)

    for report in (preview, applied):
        conflicts = cast(list[dict[str, object]], report["conflicts"])
        assert conflicts[0]["code"] == "pending_transaction_invalid"
    assert target.read_bytes() == b"transaction-after"
    assert journal.read_bytes() == journal_before


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
    before = hooks.read_bytes()
    real_replace = confined_io.ConfinedFileLease.replace
    real_assert = confined_io.ConfinedFileLease.assert_current
    published = False

    def publish_then_fail(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal published
        real_replace(lease, data)
        if lease.path == hooks and not published:
            published = True
            raise OSError("injected failure after settings publication")

    def refuse_rollback(lease: confined_io.ConfinedFileLease) -> None:
        if lease.path == hooks and published:
            raise ContractViolation("injected concurrent identity change")
        real_assert(lease)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", publish_then_fail)
    monkeypatch.setattr(confined_io.ConfinedFileLease, "assert_current", refuse_rollback)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="rollback-conflict",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert [item["code"] for item in conflicts] == ["apply_failed"]
    assert hooks.read_bytes() != before
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
    assert dry_report["conflicts"][0]["code"] == "recovery_required"
    assert hooks.read_bytes() != before


def test_rollback_preserves_symlink_substitution_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    outside = tmp_path / "outside-hooks.json"
    _write(hooks, {"hooks": {"PreToolUse": []}})
    real_replace = confined_io.ConfinedFileLease.replace
    substitution_blocked = False

    def substitute_before_replace(lease: confined_io.ConfinedFileLease, data: bytes) -> None:
        nonlocal substitution_blocked
        if lease.path == hooks:
            try:
                written = hooks.read_bytes()
                hooks.unlink()
                outside.write_bytes(written)
                hooks.symlink_to(outside)
            except PermissionError:
                substitution_blocked = True
                raise ContractViolation("host handle blocked symlink substitution") from None
        real_replace(lease, data)

    monkeypatch.setattr(confined_io.ConfinedFileLease, "replace", substitute_before_replace)

    result = install_shadow_hooks(
        home=home,
        runtime_python=Path(sys.executable),
        project_root=project,
        backup_tag="symlink-conflict",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "apply_failed"
    if substitution_blocked:
        assert hooks.is_file()
        assert not outside.exists()
    else:
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


def test_install_refuses_custom_wrapper_outside_selected_host_store(
    tmp_path: Path,
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
    conflicts = cast(list[dict[str, object]], installed["conflicts"])
    assert conflicts[0]["code"] == "hook_script_location_unsupported"
    assert "inside the root" in str(conflicts[0]["detail"])
    assert json.loads(hooks.read_text(encoding="utf-8")) == {"hooks": {"PreToolUse": []}}
    assert custom_wrapper.read_text(encoding="utf-8") == "# custom fixture\n"
    assert not (home / ".codex" / "latent-compass-shadow" / "ownership.json").exists()


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
            "command": _command("codex", Path(sys.executable), private, home=home),
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


def test_removal_refuses_unowned_config_without_owned_hook_groups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    hooks = home / ".codex" / "hooks.json"
    foreign_settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "foreign", "hooks": [{"type": "command", "command": "keep"}]}
            ]
        }
    }
    _write(hooks, foreign_settings)
    store = home / ".codex" / "latent-compass-shadow"
    config = store / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "enabled": True,
                "host_id": "codex-local",
                "agent_family": "codex",
                "projects": [
                    {
                        "root": str(project),
                        "alias": "unowned",
                        "capabilities": [
                            {"capability_id": "Read", "kind": "TOOL", "cost_ceiling": 0}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()

    result = remove_shadow_hooks(
        home=home,
        project_root=project,
        project_alias="unowned",
        backup_tag="unowned-remove",
        hosts=("codex",),
    )

    conflicts = cast(list[dict[str, object]], result["conflicts"])
    assert conflicts[0]["code"] == "configuration_collision"
    assert "ownership manifest is required" in str(conflicts[0]["detail"])
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before
    assert not (home / ".latent-compass-shadow.pending.json").exists()


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

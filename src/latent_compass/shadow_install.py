"""Reversible installer for the passive Codex and Claude shadow hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from latent_compass.canonical import canonical_text, seal

__all__ = ["install_shadow_hooks", "main", "remove_shadow_hooks"]

_HOOK_MARKER: Final = "latent-compass-shadow-hook.py"
_LEGACY_HOOK_MARKER: Final = "latent_compass.shadow_harness"
_CODEX_CAPABILITIES: Final = (
    "apply_patch",
    "functions.exec",
    "functions.wait",
    "view_image",
    "web.run",
    "write_stdin",
)
_CLAUDE_CAPABILITIES: Final = (
    "Agent",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "WebFetch",
    "WebSearch",
    "Write",
)
_HOOK_EVENTS: Final = {
    "codex": {
        "SessionStart": "startup|resume|clear",
        "PreToolUse": (
            "^(?:apply_patch|functions\\.exec|functions\\.wait|view_image|web\\.run|write_stdin)$"
        ),
        "PostToolUse": (
            "Write|Edit|MultiEdit|NotebookEdit|apply_patch|ApplyPatch|functions\\.exec"
        ),
    },
    "claude": {
        "SessionStart": "startup|resume",
        "PreToolUse": (
            "^(?:Agent|Bash|Edit|Glob|Grep|MultiEdit|NotebookEdit|Read|WebFetch|WebSearch|Write)$"
        ),
        "PostToolUse": "Write|Edit|MultiEdit|NotebookEdit|Bash",
    },
}


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError(f"{path.name} must contain a hooks object")
    return payload


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _backup(path: Path, tag: str) -> Path:
    destination = path.with_name(f"{path.name}.bak-latent-compass-{tag}")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite backup {destination.name}")
    shutil.copy2(path, destination)
    return destination


def _command(host: str, runtime_python: Path, hook_script: Path) -> str:
    if host == "codex":
        return f"& '{runtime_python}' '{hook_script}' --host codex"
    return f'"{runtime_python.as_posix()}" "{hook_script.as_posix()}" --host claude'


def _without_shadow_groups(groups: object) -> list[object]:
    if not isinstance(groups, list):
        raise ValueError("PreToolUse hooks must be an array")
    retained: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            retained.append(group)
            continue
        handlers = group.get("hooks")
        commands = (
            [handler.get("command", "") for handler in handlers if isinstance(handler, dict)]
            if isinstance(handlers, list)
            else []
        )
        if any(
            _HOOK_MARKER in command or _LEGACY_HOOK_MARKER in command
            for command in commands
            if isinstance(command, str)
        ):
            continue
        retained.append(group)
    return retained


def _install_one(path: Path, *, host: str, command: str, backup_tag: str) -> bool:
    payload = _read_json(path)
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    before = canonical_text(payload)
    for event, matcher in _HOOK_EVENTS[host].items():
        groups = _without_shadow_groups(hooks.get(event, []))
        group: dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "async": True,
                    "timeout": 10,
                }
            ]
        }
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        hooks[event] = groups
    if canonical_text(payload) == before:
        return False
    _backup(path, backup_tag)
    _atomic_json(path, payload)
    return True


def _remove_one(path: Path, *, backup_tag: str) -> bool:
    payload = _read_json(path)
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    before = canonical_text(payload)
    for event in _HOOK_EVENTS["codex"]:
        if event in hooks:
            hooks[event] = _without_shadow_groups(hooks[event])
    if canonical_text(payload) == before:
        return False
    _backup(path, backup_tag)
    _atomic_json(path, payload)
    return True


def _host_config(*, host: str, project_root: Path) -> dict[str, object]:
    capabilities = _CODEX_CAPABILITIES if host == "codex" else _CLAUDE_CAPABILITIES
    return {
        "contract_version": "1.0.0",
        "enabled": True,
        "host_id": f"{host}-local",
        "agent_family": host,
        "projects": [
            {
                "root": str(project_root.resolve()),
                "alias": "latent-compass-shadow",
                "capabilities": [
                    {"capability_id": capability, "kind": "TOOL", "cost_ceiling": 0}
                    for capability in capabilities
                ],
                "remaining_budget": 0,
            }
        ],
    }


def install_shadow_hooks(
    *,
    home: Path,
    runtime_python: Path,
    hook_script: Path,
    project_root: Path,
    backup_tag: str,
) -> dict[str, object]:
    """Install idempotent async hooks plus separate host-local configurations."""
    if not runtime_python.is_file():
        raise FileNotFoundError("runtime Python does not exist")
    if not hook_script.is_file():
        raise FileNotFoundError("host hook script does not exist")
    if not project_root.is_dir():
        raise FileNotFoundError("project root does not exist")
    targets = {
        "codex": home / ".codex" / "hooks.json",
        "claude": home / ".claude" / "settings.json",
    }
    if not all(path.is_file() for path in targets.values()):
        raise FileNotFoundError("both Codex hooks.json and Claude settings.json must exist")
    changed: dict[str, bool] = {}
    for host, path in targets.items():
        changed[host] = _install_one(
            path,
            host=host,
            command=_command(host, runtime_python, hook_script),
            backup_tag=backup_tag,
        )
        store = home / f".{host}" / "latent-compass-shadow"
        store.mkdir(parents=True, exist_ok=True)
        _atomic_json(store / "config.json", _host_config(host=host, project_root=project_root))
    return {
        "schema_version": 1,
        "operation": "install",
        "changed": any(changed.values()),
        "hosts": changed,
        "runtime_digest": seal("shadow.install.runtime.v1", runtime_python.read_bytes().hex()),
    }


def remove_shadow_hooks(*, home: Path, backup_tag: str) -> dict[str, object]:
    """Remove only Latent Compass hook groups; retain runtime and evidence stores."""
    targets = {
        "codex": home / ".codex" / "hooks.json",
        "claude": home / ".claude" / "settings.json",
    }
    changed = {
        host: _remove_one(path, backup_tag=backup_tag)
        for host, path in targets.items()
        if path.is_file()
    }
    return {
        "schema_version": 1,
        "operation": "remove",
        "changed": any(changed.values()),
        "hosts": changed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install passive Latent Compass shadow hooks")
    sub = parser.add_subparsers(dest="operation", required=True)
    install = sub.add_parser("install")
    install.add_argument("--runtime-python", type=Path, required=True)
    install.add_argument("--hook-script", type=Path, required=True)
    install.add_argument("--project-root", type=Path, required=True)
    install.add_argument("--home", type=Path, default=Path.home())
    install.add_argument("--backup-tag", default=None)
    remove = sub.add_parser("remove")
    remove.add_argument("--home", type=Path, default=Path.home())
    remove.add_argument("--backup-tag", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tag = args.backup_tag or datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    if args.operation == "install":
        result = install_shadow_hooks(
            home=args.home,
            runtime_python=args.runtime_python,
            hook_script=args.hook_script,
            project_root=args.project_root,
            backup_tag=tag,
        )
    else:
        result = remove_shadow_hooks(home=args.home, backup_tag=tag)
    sys.stdout.write(canonical_text(result) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

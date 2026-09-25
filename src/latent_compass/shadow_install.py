"""Reversible installer for passive Codex and Claude shadow hooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Final, TextIO, cast

from latent_compass import __version__
from latent_compass.canonical import canonical_text, seal
from latent_compass.shadow_harness import ShadowHarnessConfig, load_shadow_config
from latent_compass.shadow_status import Host, inspect_hosts, render_text

__all__ = [
    "configure_host_parser",
    "host_status",
    "install_shadow_hooks",
    "main",
    "plan_install_shadow_hooks",
    "plan_remove_shadow_hooks",
    "remove_shadow_hooks",
    "run_host_namespace",
]

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
_OWNERSHIP_NAME: Final = "ownership.json"


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError(f"{path.name} must contain a hooks object")
    return payload


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _backup(path: Path, tag: str) -> Path:
    destination = _backup_destination(path, tag)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite backup {destination.name}")
    shutil.copy2(path, destination)
    return destination


def _backup_destination(path: Path, tag: str) -> Path:
    return path.with_name(f"{path.name}.bak-latent-compass-{tag}")


def _backup_conflicts(plan: dict[str, object], tag: str) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    for item in cast(list[dict[str, object]], plan["files"]):
        if item["action"] not in {"update", "delete"}:
            continue
        path = Path(str(item["path"]))
        destination = _backup_destination(path, tag)
        if destination.exists():
            conflicts.append({"code": "backup_collision", "path": str(destination)})
    return conflicts


def _command(
    host: Host, runtime_python: Path, hook_script: Path, *, platform: str = os.name
) -> str:
    if platform == "nt" and host == "codex":
        runtime = str(runtime_python).replace("'", "''")
        wrapper = str(hook_script).replace("'", "''")
        return f"& '{runtime}' '{wrapper}' --host codex"
    if platform == "nt":
        return f'"{runtime_python.as_posix()}" "{hook_script.as_posix()}" --host {host}'
    return (
        f"{shlex.quote(runtime_python.as_posix())} "
        f"{shlex.quote(hook_script.as_posix())} --host {host}"
    )


def _packaged_hook_text() -> str:
    resource = files("latent_compass").joinpath("_assets/shadow_hook.py.txt")
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    source = (
        Path(__file__).resolve().parents[2] / "examples" / "_latent_compass_shadow_hook_impl.py"
    )
    return source.read_text(encoding="utf-8")


def _shadow_group(*, event: str, host: Host, command: str) -> dict[str, object]:
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
    matcher = _HOOK_EVENTS[host][event]
    if matcher is not None:
        group["matcher"] = matcher
    return group


def _owned_command(command: object, *, host: Host, wrapper: Path) -> bool:
    if not isinstance(command, str):
        return False
    parsed = _parse_owned_command(command, host)
    if parsed is None:
        return False
    _, candidate_wrapper = parsed
    return candidate_wrapper.resolve(strict=False) == wrapper.resolve(strict=False)


def _parse_owned_command(command: str, host: Host) -> tuple[Path, Path] | None:
    if host == "codex":
        match = re.fullmatch(r"^& '((?:[^']|'')+)' '((?:[^']|'')+)' --host codex$", command)
        if match is not None:
            return Path(match.group(1).replace("''", "'")), Path(match.group(2).replace("''", "'"))
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if len(arguments) != 4 or arguments[2:] != ["--host", host]:
        return None
    return Path(arguments[0]), Path(arguments[1])


def _without_shadow_groups(
    groups: object,
    *,
    event: str,
    host: Host,
    command: str | None = None,
    wrapper: Path | None = None,
) -> list[object]:
    if not isinstance(groups, list):
        raise ValueError("hook event entries must be an array")
    retained: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            retained.append(group)
            continue
        handlers = group.get("hooks")
        candidate_command = (
            handlers[0].get("command")
            if isinstance(handlers, list) and len(handlers) == 1 and isinstance(handlers[0], dict)
            else None
        )
        expected_command = command
        if (
            expected_command is None
            and wrapper is not None
            and _owned_command(candidate_command, host=host, wrapper=wrapper)
        ):
            expected_command = cast(str, candidate_command)
        if expected_command is not None and group == _shadow_group(
            event=event, host=host, command=expected_command
        ):
            continue
        retained.append(group)
    return retained


def _ownership_payload(*, host: Host, command: str) -> dict[str, object]:
    return {"schema_version": 1, "host": host, "command": command}


def _owned_command_from_manifest(path: Path, *, host: Host) -> str | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "host", "command"}
        or payload.get("schema_version") != 1
        or payload.get("host") != host
        or not isinstance(payload.get("command"), str)
        or _parse_owned_command(cast(str, payload["command"]), host) is None
    ):
        raise ValueError("invalid Latent Compass hook ownership manifest")
    return cast(str, payload["command"])


def _discover_owned_command(payload: dict[str, object], *, host: Host, wrapper: Path) -> str | None:
    hooks = cast(dict[str, object], payload["hooks"])
    candidates: dict[str, set[str]] = {}
    occurrences: dict[tuple[str, str], int] = {}
    for event, groups_raw in hooks.items():
        if not isinstance(groups_raw, list):
            raise ValueError(f"{event} hook entries must be an array")
        for group in groups_raw:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            command = (
                handlers[0].get("command")
                if isinstance(handlers, list)
                and len(handlers) == 1
                and isinstance(handlers[0], dict)
                else None
            )
            if not _owned_command(command, host=host, wrapper=wrapper):
                continue
            assert isinstance(command, str)
            if event not in _HOOK_EVENTS[host] or group != _shadow_group(
                event=event, host=host, command=command
            ):
                raise ValueError(f"ambiguous Latent Compass hook ownership in {event}")
            key = (command, event)
            occurrences[key] = occurrences.get(key, 0) + 1
            if occurrences[key] > 1:
                raise ValueError(f"ambiguous Latent Compass hook ownership in {event}")
            candidates.setdefault(command, set()).add(event)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError("ambiguous Latent Compass hook ownership commands")
    command, events = next(iter(candidates.items()))
    if events != set(_HOOK_EVENTS[host]):
        raise ValueError("incomplete Latent Compass hook ownership evidence")
    return command


def _validate_existing_shadow_groups(
    payload: dict[str, object], host: Host, *, command: str
) -> None:
    hooks = cast(dict[str, object], payload["hooks"])
    occurrences: dict[str, int] = {}
    for event, groups_raw in hooks.items():
        if not isinstance(groups_raw, list):
            raise ValueError(f"{event} hook entries must be an array")
        for group in groups_raw:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            owned_handlers = [
                handler
                for handler in handlers
                if isinstance(handler, dict) and handler.get("command") == command
            ]
            if not owned_handlers:
                continue
            if event not in _HOOK_EVENTS[host] or group != _shadow_group(
                event=event, host=host, command=command
            ):
                raise ValueError(f"existing Latent Compass hook marker collides in {event}")
            occurrences[event] = occurrences.get(event, 0) + 1
            if occurrences[event] > 1:
                raise ValueError(f"ambiguous Latent Compass hook ownership in {event}")


def _planned_host_payload(
    path: Path,
    *,
    host: Host,
    command: str,
    owned_command: str | None,
    owned_wrapper: Path | None,
) -> dict[str, object]:
    payload = _read_json(path)
    if owned_command is None and owned_wrapper is not None:
        owned_command = _discover_owned_command(payload, host=host, wrapper=owned_wrapper)
    if owned_command is not None:
        _validate_existing_shadow_groups(payload, host, command=owned_command)
    if command != owned_command:
        hooks = cast(dict[str, object], payload["hooks"])
        for groups_raw in hooks.values():
            if not isinstance(groups_raw, list):
                continue
            for group in groups_raw:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if isinstance(handlers, list) and any(
                    isinstance(handler, dict) and handler.get("command") == command
                    for handler in handlers
                ):
                    raise ValueError(
                        "target hook command already exists without ownership evidence"
                    )
    hooks = cast(dict[str, object], payload["hooks"])
    for event in _HOOK_EVENTS[host]:
        groups = _without_shadow_groups(
            hooks.get(event, []), event=event, host=host, command=owned_command
        )
        groups.append(_shadow_group(event=event, host=host, command=command))
        hooks[event] = groups
    return payload


def _remove_host_payload(
    path: Path,
    *,
    host: Host,
    owned_command: str | None,
    owned_wrapper: Path | None,
) -> dict[str, object]:
    payload = _read_json(path)
    if owned_command is None and owned_wrapper is not None:
        discovered = _discover_owned_command(payload, host=host, wrapper=owned_wrapper)
        if discovered is not None:
            raise ValueError(
                "ownership manifest is required before removing legacy hook definitions"
            )
    if owned_command is not None:
        _validate_existing_shadow_groups(payload, host, command=owned_command)
    hooks = cast(dict[str, object], payload["hooks"])
    for event in set(_HOOK_EVENTS["codex"]) | set(_HOOK_EVENTS["claude"]):
        if event in hooks:
            hooks[event] = _without_shadow_groups(
                hooks[event], event=event, host=host, command=owned_command
            )
    return payload


def _project_payload(*, host: Host, project_root: Path, project_alias: str) -> dict[str, object]:
    capabilities = _CODEX_CAPABILITIES if host == "codex" else _CLAUDE_CAPABILITIES
    return {
        "root": str(project_root.resolve()),
        "alias": project_alias,
        "capabilities": [
            {"capability_id": capability, "kind": "TOOL", "cost_ceiling": 0}
            for capability in capabilities
        ],
        "remaining_budget": 0,
    }


def _path_identity(path: Path, *, platform: str = os.name) -> str:
    resolved = str(path.resolve(strict=False))
    return resolved.casefold() if platform == "nt" else resolved


def _default_project_alias(project_root: Path) -> str:
    resolved = project_root.resolve()
    readable = re.sub(r"[^A-Za-z0-9._:-]+", "-", resolved.name).strip("._:-")
    if not readable or not readable[0].isalnum():
        readable = "project"
    digest = seal("shadow.install.project-alias.v1", str(resolved).casefold())[7:15]
    return f"{readable[:110]}-{digest}"


def _new_host_config(*, host: Host, project: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": "1.0.0",
        "enabled": True,
        "host_id": f"{host}-local",
        "agent_family": host,
        "projects": [project],
    }


def _merged_host_config(
    *,
    path: Path,
    host: Host,
    project_root: Path,
    project_alias: str,
    platform: str = os.name,
) -> dict[str, object]:
    project = _project_payload(host=host, project_root=project_root, project_alias=project_alias)
    if not path.is_file():
        payload = _new_host_config(host=host, project=project)
        load_shadow_config(payload)
        return payload
    current = load_shadow_config(json.loads(path.read_text(encoding="utf-8"))).canonical_payload()
    projects = cast(list[object], current["projects"])
    resolved_root = _path_identity(project_root, platform=platform)
    for existing_raw in projects:
        existing = cast(dict[str, object], existing_raw)
        same_root = _path_identity(Path(str(existing["root"])), platform=platform) == resolved_root
        same_alias = existing["alias"] == project_alias
        if same_root and not same_alias:
            raise ValueError("project root is already registered under another alias")
        if same_alias and not same_root:
            raise ValueError("project alias is already registered for another root")
        if same_root and same_alias:
            return current
    projects.append(project)
    load_shadow_config(current)
    return current


def _without_project(
    config: ShadowHarnessConfig,
    *,
    project_root: Path | None,
    project_alias: str | None,
    platform: str = os.name,
) -> dict[str, object] | None:
    current = config.canonical_payload()
    if project_root is None and project_alias is None:
        return None
    resolved_root = (
        _path_identity(project_root, platform=platform) if project_root is not None else None
    )
    projects = cast(list[dict[str, object]], current["projects"])
    retained = [
        project
        for project in projects
        if not (
            (
                resolved_root is None
                or _path_identity(Path(str(project["root"])), platform=platform) == resolved_root
            )
            and (project_alias is None or project["alias"] == project_alias)
        )
    ]
    if len(retained) == len(projects):
        return current
    if not retained:
        return None
    current["projects"] = retained
    load_shadow_config(current)
    return current


def _target_paths(home: Path, hosts: tuple[Host, ...]) -> dict[Host, tuple[Path, Path]]:
    return {
        host: (
            home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json"),
            home / f".{host}" / "latent-compass-shadow" / "config.json",
        )
        for host in hosts
    }


def _file_plan(path: Path, payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        action = "delete" if path.is_file() else "unchanged"
    else:
        after = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        action = "unchanged" if before == after else ("update" if before is not None else "create")
    return {"path": str(path), "action": action}


def _text_file_plan(path: Path, content: str) -> dict[str, object]:
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    action = "unchanged" if before == content else ("update" if before is not None else "create")
    return {"path": str(path), "action": action}


def plan_install_shadow_hooks(
    *,
    home: Path,
    runtime_python: Path,
    project_root: Path,
    project_alias: str = "latent-compass-shadow",
    hosts: tuple[Host, ...],
    hook_script: Path | None = None,
    backup_tag: str | None = None,
) -> dict[str, object]:
    """Preflight every selected host and return the complete no-write plan."""
    conflicts: list[dict[str, str]] = []
    if not runtime_python.is_file():
        conflicts.append({"code": "runtime_missing", "path": str(runtime_python)})
    if not project_root.is_dir():
        conflicts.append({"code": "project_missing", "path": str(project_root)})
    if hook_script is not None and not hook_script.is_file():
        conflicts.append({"code": "hook_script_missing", "path": str(hook_script)})
    if not hosts or len(set(hosts)) != len(hosts):
        conflicts.append({"code": "invalid_hosts", "path": ""})
    files: list[dict[str, object]] = []
    payloads: dict[str, dict[str, object]] = {}
    packaged_hook: str | None = None
    if hook_script is None:
        try:
            packaged_hook = _packaged_hook_text()
        except (OSError, UnicodeDecodeError) as exc:
            conflicts.append({"code": "hook_resource_missing", "path": "", "detail": str(exc)})
    for host, (settings_path, config_path) in _target_paths(home, hosts).items():
        if not settings_path.is_file():
            conflicts.append({"code": "host_configuration_missing", "path": str(settings_path)})
            continue
        ownership_path = config_path.with_name(_OWNERSHIP_NAME)
        try:
            installed_hook = (
                hook_script
                if hook_script is not None
                else config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
            )
            command = _command(host, runtime_python, installed_hook)
            owned_command = _owned_command_from_manifest(ownership_path, host=host)
            if (
                packaged_hook is not None
                and installed_hook.is_file()
                and not _owned_command(owned_command, host=host, wrapper=installed_hook)
            ):
                conflicts.append({"code": "wrapper_collision", "path": str(installed_hook)})
                continue
            settings = _planned_host_payload(
                settings_path,
                host=host,
                command=command,
                owned_command=owned_command,
                owned_wrapper=(
                    config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
                    if owned_command is None and hook_script is None
                    else None
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            conflicts.append(
                {
                    "code": "configuration_collision",
                    "path": str(settings_path),
                    "detail": str(exc),
                }
            )
            continue
        try:
            config = _merged_host_config(
                path=config_path,
                host=host,
                project_root=project_root,
                project_alias=project_alias,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            conflicts.append(
                {"code": "configuration_collision", "path": str(config_path), "detail": str(exc)}
            )
            continue
        if packaged_hook is not None:
            files.append(_text_file_plan(installed_hook, packaged_hook))
        ownership = _ownership_payload(host=host, command=command)
        files.extend(
            (
                _file_plan(settings_path, settings),
                _file_plan(config_path, config),
                _file_plan(ownership_path, ownership),
            )
        )
        payloads[host] = {"settings": settings, "config": config, "ownership": ownership}
        if packaged_hook is not None:
            payloads[host]["wrapper_text"] = packaged_hook
    changed = any(item["action"] != "unchanged" for item in files)
    plan: dict[str, object] = {
        "schema_version": 1,
        "operation": "install",
        "version": __version__,
        "dry_run": True,
        "changed": changed,
        "hosts": list(hosts),
        "files": files,
        "conflicts": conflicts,
        "states": host_status(home=home, project_root=project_root, hosts=hosts, dry_run=True)[
            "states"
        ],
        "next_steps": ["Review and approve the exact Codex hook definition with /hooks before use."]
        if "codex" in hosts
        else [],
        "_payloads": payloads,
    }
    if backup_tag is not None:
        conflicts.extend(_backup_conflicts(plan, backup_tag))
    return plan


def install_shadow_hooks(
    *,
    home: Path,
    runtime_python: Path,
    project_root: Path,
    project_alias: str = "latent-compass-shadow",
    backup_tag: str,
    hosts: tuple[Host, ...] = ("codex", "claude"),
    hook_script: Path | None = None,
) -> dict[str, object]:
    """Install only after every selected host passes a shared preflight."""
    plan = plan_install_shadow_hooks(
        home=home,
        runtime_python=runtime_python,
        project_root=project_root,
        project_alias=project_alias,
        hosts=hosts,
        hook_script=hook_script,
        backup_tag=backup_tag,
    )
    if plan["conflicts"]:
        plan["dry_run"] = False
        return plan
    payloads = cast(dict[str, dict[str, object]], plan.pop("_payloads"))
    for host, (settings_path, config_path) in _target_paths(home, hosts).items():
        wrapper_text = payloads[host].get("wrapper_text")
        if isinstance(wrapper_text, str):
            wrapper_path = config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
            if _text_file_plan(wrapper_path, wrapper_text)["action"] != "unchanged":
                wrapper_path.parent.mkdir(parents=True, exist_ok=True)
                if wrapper_path.is_file():
                    _backup(wrapper_path, backup_tag)
                wrapper_path.write_text(wrapper_text, encoding="utf-8", newline="\n")
        ownership_path = config_path.with_name(_OWNERSHIP_NAME)
        for path, key in (
            (settings_path, "settings"),
            (config_path, "config"),
            (ownership_path, "ownership"),
        ):
            proposed = cast(dict[str, object], payloads[host][key])
            if _file_plan(path, proposed)["action"] == "unchanged":
                continue
            if path.is_file():
                _backup(path, backup_tag)
            _atomic_json(path, proposed)
    plan["dry_run"] = False
    plan["states"] = host_status(home=home, project_root=project_root, hosts=hosts, dry_run=False)[
        "states"
    ]
    return plan


def plan_remove_shadow_hooks(
    *,
    home: Path,
    hosts: tuple[Host, ...],
    project_root: Path | None = None,
    project_alias: str | None = None,
    backup_tag: str | None = None,
) -> dict[str, object]:
    """Plan project-level removal while retaining every unrelated registration."""
    conflicts: list[dict[str, str]] = []
    files: list[dict[str, object]] = []
    payloads: dict[str, dict[str, object] | None] = {}
    for host, (settings_path, config_path) in _target_paths(home, hosts).items():
        ownership_path = config_path.with_name(_OWNERSHIP_NAME)
        try:
            owned_command = _owned_command_from_manifest(ownership_path, host=host)
            if config_path.is_file():
                config = load_shadow_config(json.loads(config_path.read_text(encoding="utf-8")))
                next_config = _without_project(
                    config, project_root=project_root, project_alias=project_alias
                )
            else:
                next_config = None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            conflicts.append(
                {"code": "configuration_collision", "path": str(config_path), "detail": str(exc)}
            )
            continue
        remove_hooks = next_config is None
        try:
            settings = (
                _remove_host_payload(
                    settings_path,
                    host=host,
                    owned_command=owned_command,
                    owned_wrapper=(
                        config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
                        if owned_command is None
                        else None
                    ),
                )
                if settings_path.is_file() and remove_hooks
                else None
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            conflicts.append(
                {
                    "code": "configuration_collision",
                    "path": str(settings_path),
                    "detail": str(exc),
                }
            )
            continue
        if settings_path.is_file() and remove_hooks:
            files.append(_file_plan(settings_path, settings))
            payloads[f"{host}:settings"] = settings
        files.append(_file_plan(config_path, next_config))
        payloads[f"{host}:config"] = next_config
        if remove_hooks:
            files.append(_file_plan(ownership_path, None))
            payloads[f"{host}:ownership"] = None
    plan: dict[str, object] = {
        "schema_version": 1,
        "operation": "remove",
        "version": __version__,
        "dry_run": True,
        "changed": any(item["action"] != "unchanged" for item in files),
        "hosts": list(hosts),
        "files": files,
        "conflicts": conflicts,
        "next_steps": [],
        "_payloads": payloads,
    }
    if backup_tag is not None:
        conflicts.extend(_backup_conflicts(plan, backup_tag))
    return plan


def remove_shadow_hooks(
    *,
    home: Path,
    backup_tag: str,
    hosts: tuple[Host, ...] = ("codex", "claude"),
    project_root: Path | None = None,
    project_alias: str | None = None,
) -> dict[str, object]:
    plan = plan_remove_shadow_hooks(
        home=home,
        hosts=hosts,
        project_root=project_root,
        project_alias=project_alias,
        backup_tag=backup_tag,
    )
    if plan["conflicts"]:
        plan["dry_run"] = False
        return plan
    payloads = cast(dict[str, dict[str, object] | None], plan.pop("_payloads"))
    for key, payload in payloads.items():
        host_name, kind = key.split(":", 1)
        host = cast(Host, host_name)
        settings_path, config_path = _target_paths(home, (host,))[host]
        path = (
            settings_path
            if kind == "settings"
            else config_path.with_name(_OWNERSHIP_NAME)
            if kind == "ownership"
            else config_path
        )
        if _file_plan(path, payload)["action"] == "unchanged":
            continue
        if path.is_file():
            _backup(path, backup_tag)
        if payload is None:
            if path.is_file():
                path.unlink()
        else:
            _atomic_json(path, payload)
    plan["dry_run"] = False
    return plan


def host_status(
    *,
    home: Path,
    project_root: Path,
    hosts: tuple[Host, ...],
    dry_run: bool = False,
) -> dict[str, object]:
    report = inspect_hosts(home=home, project_root=project_root, hosts=hosts)
    snapshots = cast(list[dict[str, object]], report["hosts"])
    report["operation"] = "status"
    report["version"] = __version__
    report["dry_run"] = dry_run
    report["states"] = {
        str(snapshot["host"]): {
            "installed": (
                snapshot["hooks_present"] == len(_HOOK_EVENTS[cast(Host, snapshot["host"])])
                and snapshot["runtime_present"] is True
                and snapshot["wrapper_present"] is True
            ),
            "configured": bool(snapshot["project_registered"]),
            "loaded": "UNKNOWN",
            "approved": snapshot["hook_trust"],
            "observed": bool(snapshot["event_count"]),
        }
        for snapshot in snapshots
    }
    return report


def _public_plan(plan: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def configure_host_parser(parser: argparse.ArgumentParser) -> None:
    """Attach the public host command surface to an argparse parser."""
    sub = parser.add_subparsers(dest="host_operation", required=True)
    install = sub.add_parser("install")
    install.add_argument("--runtime-python", type=Path, default=Path(sys.executable))
    install.add_argument("--hook-script", type=Path, default=None, help=argparse.SUPPRESS)
    install.add_argument("--project-root", type=Path, default=Path.cwd())
    install.add_argument("--project-alias")
    remove = sub.add_parser("remove")
    remove.add_argument("--project-root", type=Path)
    remove.add_argument("--project-alias")
    status = sub.add_parser("status")
    status.add_argument("--project-root", type=Path, default=Path.cwd())
    for command in (install, remove, status):
        command.add_argument("--host", action="append", choices=("codex", "claude"))
        command.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--json", action="store_true")
    install.add_argument("--backup-tag", default=None, help=argparse.SUPPRESS)
    remove.add_argument("--backup-tag", default=None, help=argparse.SUPPRESS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage passive Latent Compass host hooks")
    configure_host_parser(parser)
    return parser


def run_host_namespace(
    args: argparse.Namespace,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    hosts = cast(tuple[Host, ...], tuple(args.host or ("codex", "claude")))
    tag = getattr(args, "backup_tag", None) or datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    if args.host_operation == "install":
        alias = args.project_alias or _default_project_alias(args.project_root)
        result = (
            plan_install_shadow_hooks(
                home=args.home,
                runtime_python=args.runtime_python,
                project_root=args.project_root,
                project_alias=alias,
                hosts=hosts,
                hook_script=args.hook_script,
                backup_tag=tag,
            )
            if args.dry_run
            else install_shadow_hooks(
                home=args.home,
                runtime_python=args.runtime_python,
                project_root=args.project_root,
                project_alias=alias,
                backup_tag=tag,
                hosts=hosts,
                hook_script=args.hook_script,
            )
        )
    elif args.host_operation == "remove":
        result = (
            plan_remove_shadow_hooks(
                home=args.home,
                hosts=hosts,
                project_root=args.project_root,
                project_alias=args.project_alias,
                backup_tag=tag,
            )
            if args.dry_run
            else remove_shadow_hooks(
                home=args.home,
                backup_tag=tag,
                hosts=hosts,
                project_root=args.project_root,
                project_alias=args.project_alias,
            )
        )
    else:
        result = host_status(
            home=args.home,
            project_root=args.project_root,
            hosts=hosts,
            dry_run=args.dry_run,
        )
    public = _public_plan(result)
    if args.json or args.host_operation != "status":
        stdout.write(canonical_text(public) + "\n")
    else:
        stdout.write(render_text(public))
    conflicts = public.get("conflicts", [])
    if isinstance(conflicts, list) and conflicts:
        if not args.json:
            stderr.write("Latent Compass host preflight refused the operation.\n")
        return 3
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    return run_host_namespace(_parser().parse_args(argv), stdout=stdout, stderr=stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

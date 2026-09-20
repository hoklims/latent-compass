"""Human-readable, privacy-minimised status for passive host observations."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, TextIO

from latent_compass import __version__
from latent_compass.canonical import seal
from latent_compass.shadow_harness import DEFAULT_CONFIG_NAME, ShadowProject, load_shadow_config

Host = Literal["codex", "claude"]
HOSTS: Final[tuple[Host, ...]] = ("codex", "claude")
EXPECTED_EVENTS: Final = frozenset({"SessionStart", "PreToolUse", "PostToolUse"})
MAX_EVENT_FILES: Final = 10_000
MAX_EVENT_BYTES: Final = 1_048_576
_HOOK_MARKERS: Final = ("latent-compass-shadow-hook.py", "latent_compass.shadow_harness")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SEAL = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERDICTS: Final = frozenset({"ADVICE", "ABSTAIN", "ESCALATE"})
_RECORD_FIELDS: Final = frozenset(
    {
        "contract_version",
        "host",
        "host_id",
        "project_alias",
        "hook_event_name",
        "observed_at",
        "session_seal",
        "turn_seal",
        "model",
        "permission_mode",
        "tool_name",
        "source_declaration_digest",
        "source_observed_at",
        "route_decision",
        "shadow_only",
        "host_influenced",
        "content_recorded",
        "record_seal",
    }
)


def _parse_hook_command(command: str, host: Host) -> tuple[Path, Path] | None:
    pattern = (
        r"^& '([^']+)' '([^']+)' --host codex$"
        if host == "codex"
        else r'^"([^"]+)" "([^"]+)" --host claude$'
    )
    match = re.fullmatch(pattern, command)
    if match is None:
        return None
    runtime, wrapper = Path(match.group(1)), Path(match.group(2))
    if wrapper.name != "latent-compass-shadow-hook.py":
        return None
    return runtime, wrapper


def _host_settings(home: Path, host: Host) -> Path:
    return home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")


def _store_root(home: Path, host: Host) -> Path:
    return home / f".{host}" / "latent-compass-shadow"


def _hook_state(path: Path, host: Host) -> dict[str, object]:
    if not path.is_file():
        return {"configuration_present": False, "events": [], "runtime_present": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "configuration_present": True,
            "configuration_valid": False,
            "events": [],
            "runtime_present": None,
        }
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return {
            "configuration_present": True,
            "configuration_valid": False,
            "events": [],
            "runtime_present": None,
        }
    events: set[str] = set()
    runtime_states: list[bool] = []
    wrapper_states: list[bool] = []
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            continue
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                command = handler.get("command") if isinstance(handler, dict) else None
                if not (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and handler.get("async") is True
                    and isinstance(command, str)
                    and any(marker in command for marker in _HOOK_MARKERS)
                ):
                    continue
                parsed = _parse_hook_command(command, host)
                if parsed is None:
                    continue
                runtime, wrapper = parsed
                events.add(event)
                runtime_states.append(runtime.is_file())
                wrapper_states.append(wrapper.is_file())
    runtime_present = all(runtime_states) if runtime_states else None
    wrapper_present = all(wrapper_states) if wrapper_states else None
    return {
        "configuration_present": True,
        "configuration_valid": True,
        "events": sorted(events),
        "runtime_present": runtime_present,
        "wrapper_present": wrapper_present,
    }


def _project_for_root(
    projects: tuple[ShadowProject, ...], project_root: Path
) -> ShadowProject | None:
    candidate = project_root.resolve(strict=False)
    for project in projects:
        root = Path(project.root).resolve(strict=False)
        if candidate == root or candidate.is_relative_to(root):
            return project
    return None


def _event_summary(store: Path, alias: str, *, host: Host, host_id: str) -> dict[str, object]:
    root = store / "events" / alias
    if not root.is_dir():
        return {
            "event_count": 0,
            "session_count": 0,
            "verdicts": {},
            "last_observed_at": None,
            "invalid_event_count": 0,
            "truncated": False,
        }
    paths, truncated = _bounded_json_paths(root)
    verdicts: Counter[str] = Counter()
    sessions: set[str] = set()
    last_observed_at: str | None = None
    valid = 0
    invalid = 0
    for path in paths:
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_EVENT_BYTES + 1)
            if len(raw) > MAX_EVENT_BYTES:
                invalid += 1
                continue
            record = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant {value}")
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            invalid += 1
            continue
        if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
            invalid += 1
            continue
        record_seal = record.get("record_seal")
        unsigned = {key: value for key, value in record.items() if key != "record_seal"}
        try:
            seal_valid = isinstance(record_seal, str) and record_seal == seal(
                "shadow.harness.record.v1", unsigned
            )
        except (TypeError, ValueError, RecursionError):
            seal_valid = False
        if (
            not seal_valid
            or record.get("content_recorded") is not False
            or record.get("host_influenced") is not False
            or record.get("shadow_only") is not True
            or record.get("contract_version") != "1.0.0"
            or record.get("host") != host
            or record.get("host_id") != host_id
            or record.get("project_alias") != alias
            or record.get("hook_event_name") != "PreToolUse"
        ):
            invalid += 1
            continue
        observed_at = record.get("observed_at")
        session_seal = record.get("session_seal")
        decision = record.get("route_decision")
        verdict = decision.get("verdict") if isinstance(decision, dict) else None
        if (
            not isinstance(observed_at, str)
            or _TIMESTAMP.fullmatch(observed_at) is None
            or not _valid_timestamp(observed_at)
            or not isinstance(session_seal, str)
            or _SEAL.fullmatch(session_seal) is None
            or verdict not in _VERDICTS
            or not isinstance(decision, dict)
            or decision.get("execution_authority") is not False
            or decision.get("empirical_claim") is not False
        ):
            invalid += 1
            continue
        valid += 1
        sessions.add(session_seal)
        last_observed_at = max(last_observed_at or observed_at, observed_at)
        if isinstance(verdict, str):
            verdicts[verdict] += 1
    return {
        "event_count": valid,
        "session_count": len(sessions),
        "verdicts": dict(sorted(verdicts.items())),
        "last_observed_at": last_observed_at,
        "invalid_event_count": invalid,
        "truncated": truncated,
    }


def _bounded_json_paths(root: Path) -> tuple[list[Path], bool]:
    pending = [root]
    paths: list[Path] = []
    visited = 0
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            return sorted(paths, key=lambda item: item.as_posix()), True
        with entries:
            for entry in entries:
                visited += 1
                if visited > MAX_EVENT_FILES:
                    return sorted(paths, key=lambda item: item.as_posix()), True
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".json"):
                        paths.append(Path(entry.path))
                except OSError:
                    return sorted(paths, key=lambda item: item.as_posix()), True
    return sorted(paths, key=lambda item: item.as_posix()), False


def _valid_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def inspect_host(*, home: Path, host: Host, project_root: Path) -> dict[str, object]:
    settings = _hook_state(_host_settings(home, host), host)
    configured_events = settings["events"]
    assert isinstance(configured_events, list)
    store = _store_root(home, host)
    config_path = store / DEFAULT_CONFIG_NAME
    base: dict[str, object] = {
        "host": host,
        "status": "NOT_CONFIGURED",
        "hooks_present": len(set(configured_events) & EXPECTED_EVENTS),
        "runtime_present": settings.get("runtime_present"),
        "wrapper_present": settings.get("wrapper_present"),
        "hook_trust": "UNKNOWN",
        "project_registered": False,
        "project_alias": None,
        "store_present": store.is_dir(),
        "event_count": 0,
        "session_count": 0,
        "verdicts": {},
        "last_observed_at": None,
        "invalid_event_count": 0,
        "truncated": False,
    }
    if settings.get("configuration_valid") is False:
        base["status"] = "HOST_CONFIGURATION_INVALID"
        return base
    if not config_path.is_file():
        return base
    try:
        config = load_shadow_config(json.loads(config_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        base["status"] = "SHADOW_CONFIGURATION_INVALID"
        return base
    if not config.enabled:
        base["status"] = "DISABLED"
        return base
    if config.agent_family.value != host:
        base["status"] = "SHADOW_CONFIGURATION_INVALID"
        return base
    project = _project_for_root(config.projects, project_root)
    if project is None:
        base["status"] = "PROJECT_NOT_REGISTERED"
        return base
    alias = str(project.alias)
    base["project_registered"] = True
    base["project_alias"] = alias
    if base["hooks_present"] != len(EXPECTED_EVENTS):
        base["status"] = "HOOKS_MISSING"
        return base
    if base["runtime_present"] is not True or base["wrapper_present"] is not True:
        base["status"] = "RUNTIME_MISSING"
        return base
    base.update(_event_summary(store, alias, host=host, host_id=str(config.host_id)))
    base["status"] = "OBSERVING" if base["event_count"] else "NO_OBSERVATIONS"
    if base["invalid_event_count"]:
        base["status"] = "DEGRADED"
    if base["truncated"]:
        base["status"] = "DEGRADED"
    return base


def inspect_hosts(
    *, home: Path, project_root: Path, hosts: tuple[Host, ...] = HOSTS
) -> dict[str, object]:
    snapshots = [inspect_host(home=home, host=host, project_root=project_root) for host in hosts]
    return {
        "schema_version": 1,
        "project_root": str(project_root.resolve(strict=False)),
        "hosts": snapshots,
        "shadow_only": True,
        "execution_authority": False,
        "host_influenced": False,
        "content_recorded": False,
    }


def render_text(report: dict[str, object]) -> str:
    hosts = report["hosts"]
    assert isinstance(hosts, list)
    lines = ["Latent Compass — passive observation status", f"Project: {report['project_root']}"]
    for snapshot in hosts:
        assert isinstance(snapshot, dict)
        verdicts = snapshot["verdicts"]
        assert isinstance(verdicts, dict)
        counts = ", ".join(f"{name} {count}" for name, count in verdicts.items()) or "none"
        lines.append(
            f"{str(snapshot['host']).title()}: {snapshot['status']} · "
            f"hooks {snapshot['hooks_present']}/3 · events {snapshot['event_count']} · "
            f"sessions {snapshot['session_count']} · "
            f"last {snapshot['last_observed_at'] or 'never'} · "
            f"verdicts {counts}"
        )
        if snapshot["hook_trust"] == "UNKNOWN":
            lines.append("  Hook trust: UNKNOWN — review the host hook definition separately.")
    lines.append("Authority: none · Host routing influenced: no · Content recorded: no")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show passive Latent Compass usage locally")
    parser.add_argument("--version", action="version", version=f"latent-compass {__version__}")
    parser.add_argument("--host", choices=("all", *HOSTS), default="all")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit the deterministic JSON report")
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = _parser().parse_args(argv)
    hosts: tuple[Host, ...] = HOSTS if args.host == "all" else (args.host,)
    report = inspect_hosts(home=args.home, project_root=args.project_root, hosts=hosts)
    if args.json:
        json.dump(report, stdout, ensure_ascii=False, sort_keys=True, indent=2)
        stdout.write("\n")
    else:
        stdout.write(render_text(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

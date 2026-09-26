"""Human-readable, privacy-minimised status for passive host observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, TextIO

from latent_compass import __version__
from latent_compass.canonical import seal
from latent_compass.confined_io import (
    confined_directory_exists,
    list_confined_json_files,
    read_confined_file,
)
from latent_compass.errors import ContractViolation
from latent_compass.shadow_harness import (
    DEFAULT_CONFIG_NAME,
    ShadowProject,
    decode_host_json,
    host_command_home_is_eligible,
    load_shadow_config,
    validate_host_settings,
)

Host = Literal["codex", "claude"]
HOSTS: Final[tuple[Host, ...]] = ("codex", "claude")
EXPECTED_EVENTS: Final = frozenset({"SessionStart", "PreToolUse", "PostToolUse"})
MAX_EVENT_FILES: Final = 10_000
MAX_EVENT_BYTES: Final = 1_048_576
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


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))  # noqa: PTH100 - must not follow links


def _parse_hook_command(command: str, host: Host) -> tuple[Path, Path, Path | None] | None:
    if host == "codex":
        match = re.fullmatch(
            r"^& '((?:[^']|'')+)' '((?:[^']|'')+)' --host codex"
            r"(?: --home '((?:[^']|'')+)')?$",
            command,
        )
        if match is not None:
            runtime = Path(match.group(1).replace("''", "'"))
            wrapper = Path(match.group(2).replace("''", "'"))
            selected_home = (
                Path(match.group(3).replace("''", "'")) if match.group(3) is not None else None
            )
            return runtime, wrapper, selected_home
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if len(arguments) not in {4, 6} or arguments[2:4] != ["--host", host]:
        return None
    if len(arguments) == 6 and arguments[4] != "--home":
        return None
    runtime, wrapper = Path(arguments[0]), Path(arguments[1])
    selected_home = Path(arguments[5]) if len(arguments) == 6 else None
    return runtime, wrapper, selected_home


def _owned_command(store: Path, host: Host) -> tuple[str | None, str | None, bool]:
    path = store / _OWNERSHIP_NAME
    if not path.is_file():
        return None, None, True
    try:
        raw = read_confined_file(
            store, path, max_bytes=MAX_EVENT_BYTES, what="shadow ownership manifest"
        )
        payload = decode_host_json(raw.decode("utf-8"))
    except (
        OSError,
        ContractViolation,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
        return None, None, False
    valid = (
        isinstance(payload, dict)
        and set(payload) == {"schema_version", "host", "command", "wrapper_digest"}
        and type(payload.get("schema_version")) is int
        and payload.get("schema_version") == 2
        and payload.get("host") == host
        and isinstance(payload.get("command"), str)
        and isinstance(payload.get("wrapper_digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("wrapper_digest"))) is not None
        and (parsed := _parse_hook_command(str(payload.get("command")), host)) is not None
        and host_command_home_is_eligible(parsed[2], store.parents[1])
    )
    if not valid:
        return None, None, False
    assert isinstance(payload, dict)
    return str(payload["command"]), str(payload["wrapper_digest"]), True


def _expected_group(event: str, host: Host, command: str) -> dict[str, object]:
    group: dict[str, object] = {
        "hooks": [{"type": "command", "command": command, "async": True, "timeout": 10}]
    }
    matcher = _HOOK_EVENTS[host][event]
    if matcher is not None:
        group["matcher"] = matcher
    return group


def _wrapper_matches(root: Path, path: Path, expected_digest: str | None) -> bool:
    try:
        content = read_confined_file(
            root,
            path,
            max_bytes=MAX_EVENT_BYTES,
            what="owned shadow wrapper",
        )
        return expected_digest == f"sha256:{hashlib.sha256(content).hexdigest()}"
    except (OSError, ContractViolation):
        return False


def _host_settings(home: Path, host: Host) -> Path:
    return home / f".{host}" / ("hooks.json" if host == "codex" else "settings.json")


def _store_root(home: Path, host: Host) -> Path:
    return home / f".{host}" / "latent-compass-shadow"


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _entry_kind_safe(path: Path, *, directory: bool) -> bool:
    if not os.path.lexists(path):
        return False
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


def _parents_safe(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))  # noqa: PTH100 - resolve would follow links
    for parent in reversed(absolute.parents):
        if not os.path.lexists(parent):
            continue
        if not _entry_kind_safe(parent, directory=True):
            return False
    return True


def _regular_file_safe_or_absent(path: Path) -> bool:
    return _parents_safe(path) and (
        not os.path.lexists(path) or _entry_kind_safe(path, directory=False)
    )


def _lexically_within(root: Path, candidate: Path) -> bool:
    root_absolute = Path(os.path.abspath(root))  # noqa: PTH100 - lexical boundary
    candidate_absolute = Path(os.path.abspath(candidate))  # noqa: PTH100 - lexical boundary
    return candidate_absolute.is_relative_to(root_absolute)


def _host_paths_safe(home: Path, host: Host) -> bool:
    root = Path(os.path.abspath(home))  # noqa: PTH100 - resolve would follow links
    if not _parents_safe(root):
        return False
    if os.path.lexists(root) and not _entry_kind_safe(root, directory=True):
        return False
    host_root = root / f".{host}"
    store = host_root / "latent-compass-shadow"
    settings = _host_settings(root, host)
    for directory in (host_root, store, store / "runtime"):
        if os.path.lexists(directory) and not _entry_kind_safe(directory, directory=True):
            return False
    for leaf in (settings, store / _OWNERSHIP_NAME, store / DEFAULT_CONFIG_NAME):
        if os.path.lexists(leaf) and not _entry_kind_safe(leaf, directory=False):
            return False
    return True


def _hook_state(
    path: Path,
    host: Host,
    owned_command: str | None,
    wrapper_digest: str | None,
    home: Path,
    store: Path,
) -> dict[str, object]:
    if not path.is_file():
        return {"configuration_present": False, "events": [], "runtime_present": None}
    try:
        raw = read_confined_file(home, path, max_bytes=MAX_EVENT_BYTES, what="host hook settings")
        payload = validate_host_settings(decode_host_json(raw.decode("utf-8")))
    except (
        OSError,
        ContractViolation,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
        return {
            "configuration_present": True,
            "configuration_valid": False,
            "events": [],
            "runtime_present": None,
        }
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    events: set[str] = set()
    runtime_states: list[bool] = []
    wrapper_states: list[bool] = []
    for event, groups in hooks.items():
        assert isinstance(event, str)
        assert isinstance(groups, list)
        for group in groups:
            if owned_command is None or event not in _HOOK_EVENTS[host]:
                continue
            if group != _expected_group(event, host, owned_command):
                continue
            if event in events:
                return {
                    "configuration_present": True,
                    "configuration_valid": False,
                    "events": [],
                    "runtime_present": None,
                    "wrapper_present": None,
                }
            parsed = _parse_hook_command(owned_command, host)
            assert parsed is not None
            runtime, wrapper, _selected_home = parsed
            events.add(event)
            try:
                runtime_states.append(runtime.is_file())
            except OSError:
                runtime_states.append(False)
            wrapper_states.append(_wrapper_matches(store, wrapper, wrapper_digest))
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
        if candidate == root:
            return project
    return None


def _event_summary(store: Path, alias: str, *, host: Host, host_id: str) -> dict[str, object]:
    root = store / "events" / alias
    try:
        root_exists = confined_directory_exists(
            store,
            root,
            what="shadow event directory",
        )
    except (OSError, ContractViolation):
        return {"unsafe_event_store": True}
    if not root_exists:
        return {
            "event_count": 0,
            "session_count": 0,
            "verdicts": {},
            "last_observed_at": None,
            "invalid_event_count": 0,
            "truncated": False,
            "unsafe_event_store": False,
        }
    try:
        paths, truncated = list_confined_json_files(
            store,
            root,
            max_entries=MAX_EVENT_FILES,
            what="shadow event directory",
        )
    except ContractViolation as exc:
        if (
            isinstance(exc.detail, dict)
            and exc.detail.get("reason") == "windows_handle_bound_enumeration_unavailable"
        ):
            return {"observation_unknown": True}
        return {"unsafe_event_store": True}
    except OSError:
        return {"unsafe_event_store": True}
    verdicts: Counter[str] = Counter()
    sessions: set[str] = set()
    last_observed_at: str | None = None
    valid = 0
    invalid = 0
    for path in paths:
        try:
            if not _entry_kind_safe(path, directory=False):
                return {"unsafe_event_store": True}
            raw = read_confined_file(
                store,
                path,
                max_bytes=MAX_EVENT_BYTES + 1,
                what="shadow event record",
            )
            if len(raw) > MAX_EVENT_BYTES:
                invalid += 1
                continue
            record = decode_host_json(raw.decode("utf-8"))
        except ContractViolation:
            return {"unsafe_event_store": True}
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
            or not isinstance(decision, dict)
            or not isinstance(verdict, str)
            or verdict not in _VERDICTS
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
        "unsafe_event_store": False,
    }


def _valid_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def inspect_host(*, home: Path, host: Host, project_root: Path) -> dict[str, object]:
    store = _store_root(home, host)
    paths_safe = _host_paths_safe(home, host)
    owned_command, wrapper_digest, ownership_valid = (
        _owned_command(store, host) if paths_safe else (None, None, False)
    )
    if paths_safe and owned_command is not None:
        parsed = _parse_hook_command(owned_command, host)
        if parsed is None:
            paths_safe = False
        else:
            _, wrapper, selected_home = parsed
            if not host_command_home_is_eligible(selected_home, home):
                paths_safe = False
            else:
                paths_safe = _lexically_within(store, wrapper) and _regular_file_safe_or_absent(
                    wrapper
                )
        if not paths_safe:
            ownership_valid = False
    settings = (
        _hook_state(_host_settings(home, host), host, owned_command, wrapper_digest, home, store)
        if paths_safe
        else {
            "configuration_present": False,
            "configuration_valid": False,
            "events": [],
            "runtime_present": None,
            "wrapper_present": None,
        }
    )
    configured_events = settings["events"]
    assert isinstance(configured_events, list)
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
        "store_present": store.is_dir() if paths_safe else False,
        "event_count": 0,
        "session_count": 0,
        "verdicts": {},
        "last_observed_at": None,
        "invalid_event_count": 0,
        "truncated": False,
    }
    if not paths_safe or not ownership_valid or settings.get("configuration_valid") is False:
        base["status"] = "HOST_CONFIGURATION_INVALID"
        return base
    if not config_path.is_file():
        return base
    try:
        raw_config = read_confined_file(
            store,
            config_path,
            max_bytes=MAX_EVENT_BYTES,
            what="shadow host configuration",
        )
        config = load_shadow_config(decode_host_json(raw_config.decode("utf-8")))
    except (
        OSError,
        ContractViolation,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
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
    summary = _event_summary(store, alias, host=host, host_id=str(config.host_id))
    if summary.get("unsafe_event_store") is True:
        base["status"] = "HOST_CONFIGURATION_INVALID"
        return base
    if summary.get("observation_unknown") is True:
        base.update(
            {
                "event_count": None,
                "session_count": None,
                "verdicts": None,
                "last_observed_at": None,
                "invalid_event_count": None,
                "truncated": None,
            }
        )
        base["status"] = "OBSERVATION_UNKNOWN"
        return base
    base.update(summary)
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
        counts = (
            "unknown"
            if verdicts is None
            else ", ".join(f"{name} {count}" for name, count in verdicts.items()) or "none"
            if isinstance(verdicts, dict)
            else "unknown"
        )
        events = "unknown" if snapshot["event_count"] is None else snapshot["event_count"]
        sessions = "unknown" if snapshot["session_count"] is None else snapshot["session_count"]
        last = (
            "unknown"
            if snapshot["status"] == "OBSERVATION_UNKNOWN"
            else snapshot["last_observed_at"] or "never"
        )
        lines.append(
            f"{str(snapshot['host']).title()}: {snapshot['status']} · "
            f"hooks {snapshot['hooks_present']}/3 · events {events} · "
            f"sessions {sessions} · "
            f"last {last} · "
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

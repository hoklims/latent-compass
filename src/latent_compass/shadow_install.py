"""Reversible installer for passive Codex and Claude shadow hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Final, TextIO, TypedDict, cast

from latent_compass import __version__
from latent_compass.canonical import canonical_text, seal
from latent_compass.confined_io import (
    read_confined_file,
    remove_file,
    replace_file,
    write_new_file,
)
from latent_compass.errors import ContractViolation
from latent_compass.shadow_harness import ShadowHarnessConfig, load_shadow_config
from latent_compass.shadow_status import Host, inspect_hosts, render_text

__all__ = [
    "configure_host_parser",
    "host_status",
    "install_shadow_hooks",
    "main",
    "plan_install_shadow_hooks",
    "plan_recover_shadow_hooks",
    "plan_remove_shadow_hooks",
    "recover_shadow_hooks",
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
_PENDING_TRANSACTION_NAME: Final = ".latent-compass-shadow.pending.json"
_MAX_TRANSACTION_FILE_BYTES: Final = 8 * 1_048_576


class _ExpectedUnset:
    pass


class _DefiniteWriteRefusalError(ValueError):
    pass


_EXPECTED_UNSET: Final = _ExpectedUnset()


class _FileOperation(TypedDict):
    path: Path
    action: str
    before: bytes | None
    after: bytes | None


class _ReadObservation(TypedDict):
    path: Path
    root: Path
    content: bytes | None


class _RecoveryObservation(TypedDict):
    path: Path
    before_digest: str | None
    after_digest: str | None
    backup: Path | None
    current: bytes | None
    backup_content: bytes | None


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _file_operation(path: Path, before: bytes | None, after: bytes | None) -> _FileOperation:
    action = (
        "unchanged"
        if before == after
        else "create"
        if before is None
        else "delete"
        if after is None
        else "update"
    )
    return {"path": path, "action": action, "before": before, "after": after}


def _operation_plan(operation: _FileOperation) -> dict[str, object]:
    return {
        "path": str(operation["path"]),
        "action": operation["action"],
        "before_sha256": _bytes_digest(operation["before"]),
        "after_sha256": _bytes_digest(operation["after"]),
    }


def _read_json(
    path: Path,
    *,
    home: Path | None = None,
    raw: bytes | _ExpectedUnset = _EXPECTED_UNSET,
) -> dict[str, object]:
    if isinstance(raw, _ExpectedUnset):
        content = _regular_file_bytes_or_none(path, home=home)
    else:
        content = raw
    if content is None:
        raise FileNotFoundError(path)
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError(f"{path.name} must contain a hooks object")
    return payload


def _atomic_json(
    path: Path,
    payload: object,
    *,
    home: Path | None = None,
    expected: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
) -> None:
    content = _json_bytes(payload)
    _atomic_bytes(path, content, home=home, expected=expected)


def _exclusive_json(path: Path, payload: object, *, home: Path | None = None) -> None:
    content = _json_bytes(payload)
    try:
        write_new_file(
            home if home is not None else path.parent,
            path,
            content,
            what="host transaction journal",
        )
    except ContractViolation as exc:
        raise ValueError(str(exc)) from exc


def _atomic_text(
    path: Path,
    content: str,
    *,
    home: Path | None = None,
    expected: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
) -> None:
    _atomic_bytes(path, content.encode("utf-8"), home=home, expected=expected)


def _atomic_bytes(
    path: Path,
    content: bytes,
    *,
    home: Path | None = None,
    expected: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
) -> None:
    root = home if home is not None else path.parent
    try:
        if isinstance(expected, _ExpectedUnset):
            replace_file(root, path, content, what="managed host file")
        elif expected is None:
            write_new_file(root, path, content, what="managed host file")
        else:
            current = read_confined_file(
                root,
                path,
                max_bytes=_MAX_TRANSACTION_FILE_BYTES,
                what="managed host file before replacement",
            )
            if current != expected:
                raise ValueError(f"concurrent change detected for {path}")
            replace_file(root, path, content, what="managed host file")
    except ContractViolation as exc:
        if expected is None:
            raise _DefiniteWriteRefusalError(str(exc)) from exc
        raise ValueError(str(exc)) from exc


def _remove_confined(
    home: Path,
    path: Path,
    *,
    what: str,
    expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
) -> None:
    try:
        if not isinstance(expected, _ExpectedUnset):
            current = _regular_file_bytes_or_none(path, home=home)
            if current != expected:
                raise ValueError(f"concurrent change detected for {path}")
        remove_file(home, path, what=what)
    except ContractViolation as exc:
        raise ValueError(str(exc)) from exc


def _complete_transaction_journal(home: Path, path: Path, expected: bytes) -> list[dict[str, str]]:
    try:
        _remove_confined(
            home,
            path,
            what="host transaction journal",
            expected=expected,
        )
    except (OSError, ValueError) as exc:
        return [
            {
                "code": "pending_transaction_conflict",
                "path": str(path),
                "detail": f"pending transaction journal changed before cleanup: {exc}",
            }
        ]
    return []


def _prune_unconfirmed_creates(
    home: Path,
    journal_path: Path,
    journal_content: bytes,
    written: dict[Path, bytes | None],
) -> list[dict[str, str]]:
    try:
        payload = json.loads(journal_content.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise ValueError("invalid transaction journal during collision cleanup")
        retained = [
            entry
            for entry in cast(list[dict[str, object]], payload["entries"])
            if not (
                entry.get("before_sha256") is None
                and _lexical_absolute(Path(str(entry.get("path", "")))) not in written
            )
        ]
        if not retained:
            return _complete_transaction_journal(home, journal_path, journal_content)
        replacement = _json_bytes({**payload, "entries": retained})
        _atomic_bytes(journal_path, replacement, home=home, expected=journal_content)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [
            {
                "code": "pending_transaction_conflict",
                "path": str(journal_path),
                "detail": f"could not exclude unconfirmed creates from recovery: {exc}",
            }
        ]
    return []


def _transaction_snapshot(home: Path, plan: dict[str, object]) -> dict[Path, bytes | None]:
    snapshots: dict[Path, bytes | None] = {}
    for operation in cast(list[_FileOperation], plan["_operations"]):
        path = operation["path"]
        current = _regular_file_bytes_or_none(path, home=home)
        if current != operation["before"]:
            raise ValueError(f"concurrent change detected for {path}")
        snapshots[path] = current
    for observation in cast(list[_ReadObservation], plan.get("_observations", [])):
        current = _regular_file_bytes_or_none(observation["path"], home=observation["root"])
        if current != observation["content"]:
            raise ValueError(f"concurrent change detected for {observation['path']}")
    runtime_python = cast(Path | None, plan.get("_runtime_python"))
    if runtime_python is not None:
        try:
            runtime_present = runtime_python.is_file()
        except OSError:
            runtime_present = False
        if not runtime_present:
            raise ValueError(f"runtime disappeared before transaction: {runtime_python}")
    return snapshots


def _bytes_digest(content: bytes | None) -> str | None:
    return None if content is None else f"sha256:{hashlib.sha256(content).hexdigest()}"


def _assert_snapshot(home: Path, path: Path, expected: bytes | None) -> None:
    current = _regular_file_bytes_or_none(path, home=home)
    if current != expected:
        raise ValueError(f"concurrent change detected for {path}")


def _rollback_transaction(
    home: Path,
    snapshots: dict[Path, bytes | None],
    written: dict[Path, bytes | None],
) -> list[Path]:
    unresolved: list[Path] = []
    for path, after_content in written.items():
        try:
            current = _regular_file_bytes_or_none(path, home=home)
            if current != after_content:
                if current == snapshots[path]:
                    continue
                unresolved.append(path)
                continue
            before_content = snapshots[path]
            if before_content is None:
                if _path_entry_exists(path):
                    assert after_content is not None
                    _remove_confined(
                        home,
                        path,
                        what="managed host rollback target",
                        expected=after_content,
                    )
            else:
                _atomic_bytes(path, before_content, home=home, expected=after_content)
        except (OSError, ValueError):
            unresolved.append(path)
    return unresolved


def _apply_frozen_operations(
    *,
    home: Path,
    operations: list[_FileOperation],
    snapshots: dict[Path, bytes | None],
    backup_tag: str,
    written: dict[Path, bytes | None],
    journal_path: Path,
    journal_state: list[bytes],
    attempted_creates: set[Path],
) -> None:
    for operation in operations:
        if operation["action"] == "unchanged":
            continue
        path = operation["path"]
        before = operation["before"]
        after = operation["after"]
        if snapshots[path] != before:
            raise ValueError(f"transaction snapshot changed for {path}")
        _assert_safe_path_under(home, path)
        _assert_safe_path_under(home, _backup_destination(path, backup_tag))
        if before is not None:
            _backup(path, backup_tag, home=home, expected=before)
        if after is None:
            assert before is not None
            _remove_confined(home, path, what="managed host file", expected=before)
        else:
            if before is None:
                journal_state[0] = _set_create_publication_state(
                    home, journal_path, journal_state[0], path, "attempted"
                )
                attempted_creates.add(path)
            _atomic_bytes(path, after, home=home, expected=before)
            if before is None:
                journal_state[0] = _set_create_publication_state(
                    home, journal_path, journal_state[0], path, "confirmed"
                )
        written[path] = after


def _pending_transaction_path(home: Path) -> Path:
    return home / _PENDING_TRANSACTION_NAME


def _set_create_publication_state(
    home: Path,
    journal_path: Path,
    journal_content: bytes,
    path: Path,
    state: str,
) -> bytes:
    payload = json.loads(journal_content.decode("utf-8"))
    assert isinstance(payload, dict)
    entries = cast(list[dict[str, object]], payload["entries"])
    for entry in entries:
        if Path(str(entry["path"])) == path:
            entry["publication_state"] = state
            break
    replacement = _json_bytes(payload)
    _atomic_bytes(journal_path, replacement, home=home, expected=journal_content)
    return replacement


def _confirm_create_publication(
    home: Path, journal_path: Path, journal_content: bytes, path: Path
) -> bytes:
    return _set_create_publication_state(home, journal_path, journal_content, path, "confirmed")


def _path_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _regular_file_bytes_or_none(path: Path, *, home: Path | None = None) -> bytes | None:
    if not _path_entry_exists(path):
        return None
    try:
        return read_confined_file(
            home if home is not None else path.parent,
            path,
            max_bytes=_MAX_TRANSACTION_FILE_BYTES,
            what="host transaction input",
        )
    except ContractViolation as exc:
        raise ValueError(str(exc)) from exc


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))  # noqa: PTH100 - resolve would follow links


def _assert_safe_path_under(home: Path, path: Path) -> Path:
    root = _lexical_absolute(home)
    target = _lexical_absolute(path)
    if not target.is_relative_to(root):
        raise ValueError("transaction path escapes the selected home")
    for ancestor in reversed(root.parents):
        if not _path_entry_exists(ancestor):
            continue
        ancestor_info = ancestor.lstat()
        if stat.S_ISLNK(ancestor_info.st_mode) or _is_reparse_point(ancestor_info):
            raise ValueError(f"selected home ancestor is redirected: {ancestor}")
    if _path_entry_exists(root):
        root_info = root.lstat()
        if (
            stat.S_ISLNK(root_info.st_mode)
            or _is_reparse_point(root_info)
            or not stat.S_ISDIR(root_info.st_mode)
        ):
            raise ValueError("selected home must be a regular directory path")
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current /= part
        if not _path_entry_exists(current):
            continue
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"unsafe transaction path component {current}")
    return target


def _begin_transaction(
    *,
    home: Path,
    operation: str,
    backup_tag: str,
    plan: dict[str, object],
) -> tuple[Path, bytes]:
    entries: list[dict[str, object]] = []
    for item in cast(list[dict[str, object]], plan["files"]):
        if item["action"] == "unchanged":
            continue
        path = Path(str(item["path"]))
        entries.append(
            {
                "path": str(path),
                "before_sha256": item.get("before_sha256"),
                "after_sha256": item.get("after_sha256"),
                "backup_path": (
                    str(_backup_destination(path, backup_tag))
                    if item["action"] in {"update", "delete"}
                    else None
                ),
                "publication_state": (
                    "planned" if item["action"] == "create" else "not_applicable"
                ),
            }
        )
    journal = {
        "schema_version": 1,
        "operation": operation,
        "backup_tag": backup_tag,
        "entries": entries,
    }
    path = _pending_transaction_path(home)
    _assert_safe_path_under(home, path)
    _exclusive_json(path, journal, home=home)
    return path, _json_bytes(journal)


def _decode_pending_transaction(
    home: Path, journal_raw: bytes
) -> list[tuple[Path, str | None, str | None, Path | None]]:
    payload = json.loads(journal_raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pending transaction journal must contain a JSON object")
    entries = payload.get("entries")
    if (
        set(payload) != {"schema_version", "operation", "backup_tag", "entries"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or not isinstance(entries, list)
        or not entries
    ):
        raise ValueError("invalid pending transaction journal")
    operation = payload.get("operation")
    if operation not in {"install", "remove"}:
        raise ValueError("pending transaction operation must be install or remove")
    backup_tag = payload.get("backup_tag")
    if not isinstance(backup_tag, str) or not backup_tag:
        raise ValueError("invalid pending transaction backup tag")
    root = _lexical_absolute(home)
    allowed_paths = {
        _lexical_absolute(path)
        for _, (settings_path, config_path) in _target_paths(home, ("codex", "claude")).items()
        for path in (
            settings_path,
            config_path,
            config_path.with_name(_OWNERSHIP_NAME),
            config_path.parent / "runtime" / "latent-compass-shadow-hook.py",
        )
    }
    seen_paths: set[Path] = set()
    decoded: list[tuple[Path, str | None, str | None, Path | None]] = []
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) not in (
            {"path", "before_sha256", "after_sha256", "backup_path"},
            {
                "path",
                "before_sha256",
                "after_sha256",
                "backup_path",
                "publication_confirmed",
            },
            {
                "path",
                "before_sha256",
                "after_sha256",
                "backup_path",
                "publication_state",
            },
        ):
            raise ValueError("invalid pending transaction entry")
        if "publication_confirmed" in raw and not isinstance(raw["publication_confirmed"], bool):
            raise ValueError("invalid pending transaction publication provenance")
        if raw.get("publication_state") not in {
            None,
            "planned",
            "attempted",
            "confirmed",
            "not_applicable",
        }:
            raise ValueError("invalid pending transaction publication state")
        path = _assert_safe_path_under(root, Path(str(raw.get("path", ""))))
        if path not in allowed_paths:
            raise ValueError("pending transaction path is not a managed host target")
        if path in seen_paths:
            raise ValueError("pending transaction paths must be unique")
        seen_paths.add(path)
        before_digest = cast(str | None, raw.get("before_sha256"))
        after_digest = cast(str | None, raw.get("after_sha256"))
        for digest in (before_digest, after_digest):
            if digest is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise ValueError("invalid pending transaction content digest")
        if before_digest == after_digest:
            raise ValueError("pending transaction entry does not change content")
        backup_raw = raw.get("backup_path")
        if before_digest is not None and not isinstance(backup_raw, str):
            raise ValueError("pending transaction backup path must be a string")
        if before_digest is None and backup_raw is not None:
            raise ValueError("pending transaction backup binding is inconsistent")
        backup = (
            _assert_safe_path_under(root, Path(str(backup_raw)))
            if isinstance(backup_raw, str)
            else None
        )
        expected_backup = _lexical_absolute(_backup_destination(path, backup_tag))
        if backup is not None and backup != expected_backup:
            raise ValueError("pending transaction backup path is not bound to its source")
        decoded.append((path, before_digest, after_digest, backup))
    return decoded


def _observe_recovery_targets(
    home: Path, journal_raw: bytes
) -> tuple[list[dict[str, str]], list[_RecoveryObservation]]:
    observations: list[_RecoveryObservation] = []
    _decode_pending_transaction(home, journal_raw)
    payload = json.loads(journal_raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pending transaction journal must contain a JSON object")
    publication_confirmed = {
        _lexical_absolute(Path(str(entry["path"]))): (
            entry.get("publication_state") == "confirmed"
            or entry.get("publication_confirmed") is True
        )
        for entry in cast(list[dict[str, object]], payload["entries"])
    }
    for path, before_digest, after_digest, backup in _decode_pending_transaction(home, journal_raw):
        current = _regular_file_bytes_or_none(path, home=home)
        current_digest = _bytes_digest(current)
        if current_digest not in {before_digest, after_digest}:
            return [
                {
                    "code": "pending_transaction_conflict",
                    "path": str(path),
                    "detail": "current bytes match neither transaction state; preserve and inspect",
                }
            ], []
        if (
            before_digest is None
            and current_digest == after_digest
            and not publication_confirmed[path]
        ):
            return [
                {
                    "code": "pending_transaction_conflict",
                    "path": str(path),
                    "detail": "create publication is unconfirmed; preserve and inspect",
                }
            ], []
        backup_content = (
            _regular_file_bytes_or_none(backup, home=home) if backup is not None else None
        )
        if backup_content is not None and _bytes_digest(backup_content) != before_digest:
            return [
                {
                    "code": "pending_transaction_conflict",
                    "path": str(backup),
                    "detail": "recovery backup digest mismatch",
                }
            ], []
        if current_digest == after_digest and before_digest is not None and backup_content is None:
            return [
                {
                    "code": "pending_transaction_conflict",
                    "path": str(path),
                    "detail": "required recovery backup is missing",
                }
            ], []
        observations.append(
            {
                "path": path,
                "before_digest": before_digest,
                "after_digest": after_digest,
                "backup": backup,
                "current": current,
                "backup_content": backup_content,
            }
        )
    return [], observations


def _recover_pending_transaction(
    home: Path,
    *,
    apply: bool = True,
    journal_raw: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
    observations: list[_RecoveryObservation] | None = None,
) -> list[dict[str, str]]:
    journal_path = _pending_transaction_path(home)
    try:
        _assert_safe_path_under(home, journal_path)
        current_journal = _regular_file_bytes_or_none(journal_path, home=home)
        if not isinstance(journal_raw, _ExpectedUnset) and current_journal != journal_raw:
            return [
                {
                    "code": "pending_transaction_conflict",
                    "path": str(journal_path),
                    "detail": "pending transaction journal changed after preview",
                }
            ]
        if current_journal is None:
            return []
        expected_journal = current_journal
        if observations is None:
            conflicts, observations = _observe_recovery_targets(home, expected_journal)
            if conflicts:
                return conflicts
        for observation in observations:
            current = _regular_file_bytes_or_none(observation["path"], home=home)
            if current != observation["current"]:
                return [
                    {
                        "code": "pending_transaction_conflict",
                        "path": str(observation["path"]),
                        "detail": "current bytes changed after recovery preflight",
                    }
                ]
            backup = observation["backup"]
            backup_content = (
                _regular_file_bytes_or_none(backup, home=home) if backup is not None else None
            )
            if backup_content != observation["backup_content"]:
                return [
                    {
                        "code": "pending_transaction_conflict",
                        "path": str(backup),
                        "detail": "recovery backup changed after preflight",
                    }
                ]
        if not apply:
            return []
        for observation in observations:
            path = observation["path"]
            current = observation["current"]
            if _bytes_digest(current) != observation["after_digest"]:
                continue
            if observation["before_digest"] is None:
                assert current is not None
                _remove_confined(
                    home,
                    path,
                    what="managed host recovery target",
                    expected=current,
                )
            else:
                backup_content = observation["backup_content"]
                assert backup_content is not None
                _atomic_bytes(path, backup_content, home=home, expected=current)
        return _complete_transaction_journal(home, journal_path, expected_journal)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        return [
            {
                "code": "pending_transaction_invalid",
                "path": str(journal_path),
                "detail": str(exc),
            }
        ]


def _pending_recovery_description(
    observations: list[_RecoveryObservation],
) -> dict[str, object]:
    try:
        if not observations:
            return {"pending": False, "files": []}
        files: list[dict[str, object]] = []
        final_files: list[dict[str, object]] = []
        for observation in observations:
            target = observation["path"]
            before_digest = observation["before_digest"]
            after_digest = observation["after_digest"]
            current = observation["current"]
            current_digest = _bytes_digest(current)
            action = (
                "unchanged"
                if current_digest == before_digest
                else "delete"
                if before_digest is None and current_digest == after_digest
                else "restore"
            )
            files.append(
                {
                    "path": str(target),
                    "action": action,
                    "before_sha256": current_digest,
                    "after_sha256": before_digest,
                }
            )
            final_action = (
                "unchanged"
                if current_digest == after_digest
                else "delete"
                if after_digest is None
                else "create"
                if current_digest is None
                else "update"
            )
            final_files.append(
                {
                    "path": str(target),
                    "action": final_action,
                    "before_sha256": current_digest,
                    "after_sha256": after_digest,
                }
            )
        return {
            "pending": True,
            "action": "restore transaction-owned after-states before apply",
            "files": files,
            "final_files": final_files,
            "then": "replan requested operation",
        }
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ) as exc:
        return {
            "pending": True,
            "files": [],
            "invalid_detail": str(exc),
        }


def _recovery_description(
    home: Path,
    conflicts: list[dict[str, str]],
    observations: list[_RecoveryObservation],
) -> dict[str, object]:
    description = _pending_recovery_description(observations)
    detail = description.pop("invalid_detail", None)
    if isinstance(detail, str):
        conflicts.append(
            {
                "code": "pending_transaction_invalid",
                "path": str(_pending_transaction_path(home)),
                "detail": detail,
            }
        )
    return description


def _pending_recovery_preview(
    home: Path,
) -> tuple[
    list[dict[str, str]],
    dict[str, object],
    bytes | None,
    list[_RecoveryObservation],
]:
    journal_path = _pending_transaction_path(home)
    if not _path_entry_exists(journal_path):
        return [], {"pending": False, "files": []}, None, []
    try:
        _assert_safe_path_under(home, journal_path)
        journal_raw = _regular_file_bytes_or_none(journal_path, home=home)
        if journal_raw is None:
            raise ValueError("pending transaction journal vanished during validation")
    except (OSError, ValueError) as exc:
        conflict = {
            "code": "pending_transaction_invalid",
            "path": str(journal_path),
            "detail": str(exc),
        }
        return [conflict], {"pending": True, "files": []}, None, []
    try:
        conflicts, observations = _observe_recovery_targets(home, journal_raw)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        conflicts = [
            {
                "code": "pending_transaction_invalid",
                "path": str(journal_path),
                "detail": str(exc),
            }
        ]
        observations = []
    recovery = (
        _recovery_description(home, conflicts, observations)
        if not conflicts
        else {"pending": True, "files": []}
    )
    return conflicts, recovery, journal_raw, observations


def _backup(
    path: Path,
    tag: str,
    *,
    home: Path | None = None,
    expected: bytes | _ExpectedUnset = _EXPECTED_UNSET,
) -> Path:
    destination = _backup_destination(path, tag)
    if _path_entry_exists(destination):
        raise FileExistsError(f"refusing to overwrite backup {destination.name}")
    root = home if home is not None else path.parent
    try:
        content = read_confined_file(
            root,
            path,
            max_bytes=_MAX_TRANSACTION_FILE_BYTES,
            what="host transaction backup source",
        )
        if not isinstance(expected, _ExpectedUnset) and content != expected:
            raise ValueError(f"concurrent change detected for backup source {path}")
        write_new_file(root, destination, content, what="host transaction backup")
    except ContractViolation as exc:
        raise ValueError(str(exc)) from exc
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
        if _path_entry_exists(destination):
            conflicts.append({"code": "backup_collision", "path": str(destination)})
    return conflicts


def _command(
    host: Host,
    runtime_python: Path,
    hook_script: Path,
    *,
    home: Path | None = None,
    platform: str = os.name,
) -> str:
    home_arguments = "" if home is None else f" --home '{str(home).replace("'", "''")}'"
    if platform == "nt" and host == "codex":
        runtime = str(runtime_python).replace("'", "''")
        wrapper = str(hook_script).replace("'", "''")
        return f"& '{runtime}' '{wrapper}' --host codex{home_arguments}"
    if platform == "nt":
        suffix = "" if home is None else f' --home "{home.as_posix()}"'
        return f'"{runtime_python.as_posix()}" "{hook_script.as_posix()}" --host {host}{suffix}'
    suffix = "" if home is None else f" --home {shlex.quote(home.as_posix())}"
    return (
        f"{shlex.quote(runtime_python.as_posix())} "
        f"{shlex.quote(hook_script.as_posix())} --host {host}{suffix}"
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
    _, candidate_wrapper, _selected_home = parsed
    return candidate_wrapper.resolve(strict=False) == wrapper.resolve(strict=False)


def _command_references_wrapper(command: object, wrapper: Path) -> bool:
    if not isinstance(command, str) or len(command) > 8192:
        return False
    powershell = re.fullmatch(r"^& '((?:[^']|'')+)' '((?:[^']|'')+)'(?: .*)?$", command)
    if powershell is not None:
        candidate_wrapper = Path(powershell.group(2).replace("''", "'"))
        return candidate_wrapper.resolve(strict=False) == wrapper.resolve(strict=False)
    expected = wrapper.resolve(strict=False)
    for posix in (True, False):
        try:
            arguments = shlex.split(command, comments=True, posix=posix)
        except ValueError:
            continue
        if len(arguments) > 128:
            return False
        for raw_token in arguments:
            token = raw_token.strip("\"'")
            if token in {"&", "env"} or token.startswith("-"):
                continue
            if Path(token).resolve(strict=False) == expected:
                return True
    return False


def _parse_owned_command(command: str, host: Host) -> tuple[Path, Path, Path | None] | None:
    if host == "codex":
        match = re.fullmatch(
            r"^& '((?:[^']|'')+)' '((?:[^']|'')+)' --host codex"
            r"(?: --home '((?:[^']|'')+)')?$",
            command,
        )
        if match is not None:
            selected_home = (
                Path(match.group(3).replace("''", "'")) if match.group(3) is not None else None
            )
            return (
                Path(match.group(1).replace("''", "'")),
                Path(match.group(2).replace("''", "'")),
                selected_home,
            )
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if len(arguments) not in {4, 6} or arguments[2:4] != ["--host", host]:
        return None
    if len(arguments) == 6 and arguments[4] != "--home":
        return None
    return (
        Path(arguments[0]),
        Path(arguments[1]),
        Path(arguments[5]) if len(arguments) == 6 else None,
    )


def _owned_command_bound_to_home(command: str, host: Host, home: Path | None) -> bool:
    if home is None:
        return False
    parsed = _parse_owned_command(command, host)
    if parsed is None:
        return False
    selected_home = parsed[2]
    return (
        selected_home is not None
        and selected_home.is_absolute()
        and _lexical_absolute(selected_home) == _lexical_absolute(home)
    )


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


def _text_digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _ownership_payload(*, host: Host, command: str, wrapper_digest: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "host": host,
        "command": command,
        "wrapper_digest": wrapper_digest,
    }


def _ownership_from_manifest(
    path: Path,
    *,
    host: Host,
    home: Path | None = None,
    raw: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
) -> tuple[str, str] | None:
    if isinstance(raw, _ExpectedUnset):
        content = _regular_file_bytes_or_none(path, home=home)
    else:
        content = raw
    if content is None:
        return None
    payload = json.loads(content.decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "host", "command", "wrapper_digest"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 2
        or payload.get("host") != host
        or not isinstance(payload.get("command"), str)
        or not isinstance(payload.get("wrapper_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload.get("wrapper_digest"))) is None
        or not _owned_command_bound_to_home(cast(str, payload["command"]), host, home)
    ):
        raise ValueError("invalid Latent Compass hook ownership manifest")
    return cast(str, payload["command"]), cast(str, payload["wrapper_digest"])


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
    home: Path | None = None,
    host: Host,
    command: str,
    owned_command: str | None,
    owned_wrapper: Path | None,
    target_wrapper: Path,
    raw: bytes | _ExpectedUnset = _EXPECTED_UNSET,
) -> dict[str, object]:
    payload = _read_json(path, home=home, raw=raw)
    if owned_command is None and owned_wrapper is not None:
        owned_command = _discover_owned_command(payload, host=host, wrapper=owned_wrapper)
    if owned_command is not None:
        _validate_existing_shadow_groups(payload, host, command=owned_command)
    hooks = cast(dict[str, object], payload["hooks"])
    for groups_raw in hooks.values():
        if not isinstance(groups_raw, list):
            continue
        for group in groups_raw:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                candidate = handler.get("command") if isinstance(handler, dict) else None
                if candidate != owned_command and _command_references_wrapper(
                    candidate, target_wrapper
                ):
                    raise ValueError("managed wrapper is referenced by a foreign hook command")
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
    home: Path | None = None,
    host: Host,
    owned_command: str | None,
    owned_wrapper: Path | None,
    wrapper_to_delete: Path | None,
    raw: bytes | _ExpectedUnset = _EXPECTED_UNSET,
) -> dict[str, object]:
    payload = _read_json(path, home=home, raw=raw)
    if wrapper_to_delete is not None:
        hooks = cast(dict[str, object], payload["hooks"])
        for groups_raw in hooks.values():
            if not isinstance(groups_raw, list):
                continue
            for group in groups_raw:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    command = handler.get("command") if isinstance(handler, dict) else None
                    if command != owned_command and _command_references_wrapper(
                        command, wrapper_to_delete
                    ):
                        raise ValueError(
                            "managed wrapper is still referenced by a foreign hook command"
                        )
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


def _default_project_alias(project_root: Path, *, platform: str = os.name) -> str:
    resolved = project_root.resolve()
    readable = re.sub(r"[^A-Za-z0-9._:-]+", "-", resolved.name).strip("._:-")
    if not readable or not readable[0].isalnum():
        readable = "project"
    digest = seal("shadow.install.project-alias.v1", _path_identity(resolved, platform=platform))[
        7:15
    ]
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
    home: Path | None = None,
    host: Host,
    project_root: Path,
    project_alias: str,
    platform: str = os.name,
    raw: bytes | _ExpectedUnset | None = _EXPECTED_UNSET,
) -> dict[str, object]:
    project = _project_payload(host=host, project_root=project_root, project_alias=project_alias)
    if isinstance(raw, _ExpectedUnset):
        content = _regular_file_bytes_or_none(path, home=home)
    else:
        content = raw
    if content is None:
        payload = _new_host_config(host=host, project=project)
        load_shadow_config(payload)
        return payload
    current = load_shadow_config(json.loads(content.decode("utf-8"))).canonical_payload()
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


def _validate_host_paths(
    *, home: Path, settings_path: Path, config_path: Path, backup_tag: str | None
) -> None:
    ownership_path = config_path.with_name(_OWNERSHIP_NAME)
    wrapper_path = config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
    targets = (settings_path, config_path, ownership_path, wrapper_path)
    for target in targets:
        _assert_safe_path_under(home, target)
        if backup_tag is not None:
            _assert_safe_path_under(home, _backup_destination(target, backup_tag))


def _file_plan(
    path: Path, payload: dict[str, object] | None, *, home: Path | None = None
) -> dict[str, object]:
    before_bytes = _regular_file_bytes_or_none(path, home=home)
    after_bytes = None if payload is None else _json_bytes(payload)
    return _operation_plan(_file_operation(path, before_bytes, after_bytes))


def _text_file_plan(path: Path, content: str, *, home: Path | None = None) -> dict[str, object]:
    before_bytes = _regular_file_bytes_or_none(path, home=home)
    return _operation_plan(_file_operation(path, before_bytes, content.encode("utf-8")))


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
    home = _lexical_absolute(home)
    runtime_python = _lexical_absolute(runtime_python)
    if hook_script is not None:
        hook_script = _lexical_absolute(hook_script)
    conflicts: list[dict[str, str]] = []
    pending = _pending_transaction_path(home)
    pending_conflicts, pending_recovery, pending_raw, _pending_observations = (
        _pending_recovery_preview(home)
    )
    conflicts.extend(pending_conflicts)
    if not runtime_python.is_file():
        conflicts.append({"code": "runtime_missing", "path": str(runtime_python)})
    if not project_root.is_dir():
        conflicts.append({"code": "project_missing", "path": str(project_root)})
    if hook_script is not None and not hook_script.is_file():
        conflicts.append({"code": "hook_script_missing", "path": str(hook_script)})
    if not hosts or len(set(hosts)) != len(hosts):
        conflicts.append({"code": "invalid_hosts", "path": ""})
    if pending_raw is not None or pending_conflicts:
        recovery = pending_recovery if not conflicts else {"pending": True, "files": []}
        if not conflicts:
            conflicts.append(
                {
                    "code": "recovery_required",
                    "path": str(pending),
                    "detail": (f'run latent-compass host recover --home "{home}" --dry-run --json'),
                }
            )
        return {
            "schema_version": 1,
            "operation": "install",
            "version": __version__,
            "project_root": str(project_root.resolve()),
            "dry_run": True,
            "changed": False,
            "hosts": list(hosts),
            "files": [],
            "conflicts": conflicts,
            "states": host_status(home=home, project_root=project_root, hosts=hosts, dry_run=True)[
                "states"
            ],
            "next_steps": [f'latent-compass host recover --home "{home}" --dry-run --json'],
            "recovery": recovery,
        }
    files: list[dict[str, object]] = []
    operations: list[_FileOperation] = []
    observations: list[_ReadObservation] = []
    packaged_hook: str | None = None
    if hook_script is None:
        try:
            packaged_hook = _packaged_hook_text()
        except (OSError, UnicodeDecodeError) as exc:
            conflicts.append({"code": "hook_resource_missing", "path": "", "detail": str(exc)})
    for host, (settings_path, config_path) in _target_paths(home, hosts).items():
        try:
            _validate_host_paths(
                home=home,
                settings_path=settings_path,
                config_path=config_path,
                backup_tag=backup_tag,
            )
        except (OSError, ValueError) as exc:
            conflicts.append(
                {
                    "code": "configuration_collision",
                    "path": str(settings_path),
                    "detail": str(exc),
                }
            )
            continue
        ownership_path = config_path.with_name(_OWNERSHIP_NAME)
        try:
            settings_before = _regular_file_bytes_or_none(settings_path, home=home)
            if settings_before is None:
                conflicts.append({"code": "host_configuration_missing", "path": str(settings_path)})
                continue
            ownership_before = _regular_file_bytes_or_none(ownership_path, home=home)
            config_before = _regular_file_bytes_or_none(config_path, home=home)
            installed_hook = (
                hook_script
                if hook_script is not None
                else config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
            )
            managed_wrapper = config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
            if hook_script is not None:
                if _path_identity(installed_hook) in {
                    _path_identity(settings_path),
                    _path_identity(config_path),
                    _path_identity(ownership_path),
                    _path_identity(managed_wrapper),
                }:
                    conflicts.append(
                        {
                            "code": "hook_script_target_overlap",
                            "path": str(installed_hook),
                        }
                    )
                    continue
                try:
                    _assert_safe_path_under(config_path.parent, installed_hook)
                except (OSError, ValueError) as exc:
                    conflicts.append(
                        {
                            "code": "hook_script_location_unsupported",
                            "path": str(installed_hook),
                            "detail": str(exc),
                        }
                    )
                    continue
            wrapper_before = _regular_file_bytes_or_none(
                installed_hook,
                home=home if hook_script is None else config_path.parent,
            )
            if hook_script is not None and not any(
                item["path"] == installed_hook for item in observations
            ):
                observations.append(
                    {
                        "path": installed_hook,
                        "root": config_path.parent,
                        "content": wrapper_before,
                    }
                )
            command = _command(
                host,
                runtime_python,
                installed_hook,
                home=_lexical_absolute(home),
            )
            owned = _ownership_from_manifest(
                ownership_path,
                host=host,
                home=home,
                raw=ownership_before,
            )
            owned_command = owned[0] if owned is not None else None
            owned_digest = owned[1] if owned is not None else None
            if owned_command is not None:
                parsed_owned = _parse_owned_command(owned_command, host)
                if parsed_owned is None:
                    raise ValueError("invalid Latent Compass hook ownership command")
                _assert_safe_path_under(config_path.parent, parsed_owned[1])
            if packaged_hook is not None and wrapper_before is not None:
                if not _owned_command(owned_command, host=host, wrapper=installed_hook):
                    conflicts.append({"code": "wrapper_collision", "path": str(installed_hook)})
                    continue
                if _bytes_digest(wrapper_before) != owned_digest:
                    conflicts.append(
                        {"code": "wrapper_integrity_collision", "path": str(installed_hook)}
                    )
                    continue
            if packaged_hook is None and owned_command is not None:
                if not _owned_command(owned_command, host=host, wrapper=installed_hook):
                    conflicts.append({"code": "wrapper_collision", "path": str(installed_hook)})
                    continue
                if _bytes_digest(wrapper_before) != owned_digest:
                    conflicts.append(
                        {"code": "wrapper_integrity_collision", "path": str(installed_hook)}
                    )
                    continue
            if packaged_hook is not None:
                wrapper_digest = _text_digest(packaged_hook)
            else:
                if wrapper_before is None:
                    raise ValueError("custom hook script is missing")
                custom_digest = _bytes_digest(wrapper_before)
                assert custom_digest is not None
                wrapper_digest = custom_digest
            settings = _planned_host_payload(
                settings_path,
                home=home,
                host=host,
                command=command,
                owned_command=owned_command,
                owned_wrapper=(
                    config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
                    if owned_command is None and hook_script is None
                    else None
                ),
                target_wrapper=installed_hook,
                raw=settings_before,
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
                home=home,
                host=host,
                project_root=project_root,
                project_alias=project_alias,
                raw=config_before,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            conflicts.append(
                {"code": "configuration_collision", "path": str(config_path), "detail": str(exc)}
            )
            continue
        if packaged_hook is not None:
            operations.append(
                _file_operation(installed_hook, wrapper_before, packaged_hook.encode("utf-8"))
            )
        ownership = _ownership_payload(host=host, command=command, wrapper_digest=wrapper_digest)
        operations.extend(
            (
                _file_operation(settings_path, settings_before, _json_bytes(settings)),
                _file_operation(config_path, config_before, _json_bytes(config)),
                _file_operation(ownership_path, ownership_before, _json_bytes(ownership)),
            )
        )
    files = [_operation_plan(operation) for operation in operations]
    changed = any(item["action"] != "unchanged" for item in files)
    plan: dict[str, object] = {
        "schema_version": 1,
        "operation": "install",
        "version": __version__,
        "project_root": str(project_root.resolve()),
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
        "recovery": pending_recovery,
        "_operations": operations,
        "_observations": observations,
        "_runtime_python": runtime_python,
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
    home = _lexical_absolute(home)
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
    try:
        snapshots = _transaction_snapshot(home, plan)
    except (OSError, ValueError) as exc:
        cast(list[dict[str, str]], plan["conflicts"]).append(
            {"code": "concurrent_change", "path": "", "detail": str(exc)}
        )
        plan["dry_run"] = False
        return plan
    if plan["changed"] is False:
        plan.pop("_operations", None)
        plan.pop("_observations", None)
        plan.pop("_runtime_python", None)
        plan["dry_run"] = False
        plan["states"] = host_status(
            home=home, project_root=project_root, hosts=hosts, dry_run=False
        )["states"]
        return plan
    written: dict[Path, bytes | None] = {}
    operations = cast(list[_FileOperation], plan.pop("_operations"))
    plan.pop("_observations", None)
    plan.pop("_runtime_python", None)
    journal_path: Path | None = None
    journal_content: bytes | None = None
    journal_state: list[bytes] = []
    attempted_creates: set[Path] = set()
    try:
        journal_path, journal_content = _begin_transaction(
            home=home, operation="install", backup_tag=backup_tag, plan=plan
        )
        journal_state = [journal_content]
        _apply_frozen_operations(
            home=home,
            operations=operations,
            snapshots=snapshots,
            backup_tag=backup_tag,
            written=written,
            journal_path=journal_path,
            journal_state=journal_state,
            attempted_creates=attempted_creates,
        )
    except (OSError, ValueError) as exc:
        unresolved = _rollback_transaction(home, snapshots, written)
        cast(list[dict[str, str]], plan["conflicts"]).append(
            {"code": "apply_failed", "path": "", "detail": str(exc)}
        )
        for path in unresolved:
            cast(list[dict[str, str]], plan["conflicts"]).append(
                {
                    "code": "rollback_conflict",
                    "path": str(path),
                    "detail": "current bytes changed after this transaction wrote the path",
                }
            )
        active_journal = journal_state[0] if journal_state else journal_content
        if journal_path is not None and active_journal is not None:
            collision_cleanup = (
                _complete_transaction_journal(home, journal_path, active_journal)
                if isinstance(exc, _DefiniteWriteRefusalError) and not unresolved
                else _prune_unconfirmed_creates(
                    home,
                    journal_path,
                    active_journal,
                    {
                        **written,
                        **(
                            dict.fromkeys(attempted_creates)
                            if not isinstance(exc, _DefiniteWriteRefusalError)
                            else {}
                        ),
                    },
                )
            )
            cast(list[dict[str, str]], plan["conflicts"]).extend(collision_cleanup)
        plan["dry_run"] = False
        return plan
    if journal_path is not None:
        assert journal_state
        cast(list[dict[str, str]], plan["conflicts"]).extend(
            _complete_transaction_journal(home, journal_path, journal_state[0])
        )
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
    home = _lexical_absolute(home)
    conflicts: list[dict[str, str]] = []
    pending = _pending_transaction_path(home)
    pending_conflicts, pending_recovery, pending_raw, _pending_observations = (
        _pending_recovery_preview(home)
    )
    conflicts.extend(pending_conflicts)
    if pending_raw is not None or pending_conflicts:
        recovery = pending_recovery if not conflicts else {"pending": True, "files": []}
        if not conflicts:
            conflicts.append(
                {
                    "code": "recovery_required",
                    "path": str(pending),
                    "detail": (f'run latent-compass host recover --home "{home}" --dry-run --json'),
                }
            )
        return {
            "schema_version": 1,
            "operation": "remove",
            "version": __version__,
            "dry_run": True,
            "changed": False,
            "hosts": list(hosts),
            "files": [],
            "conflicts": conflicts,
            "next_steps": [f'latent-compass host recover --home "{home}" --dry-run --json'],
            "recovery": recovery,
        }
    files: list[dict[str, object]] = []
    operations: list[_FileOperation] = []
    observations: list[_ReadObservation] = []
    for host, (settings_path, config_path) in _target_paths(home, hosts).items():
        try:
            _validate_host_paths(
                home=home,
                settings_path=settings_path,
                config_path=config_path,
                backup_tag=backup_tag,
            )
        except (OSError, ValueError) as exc:
            conflicts.append(
                {
                    "code": "configuration_collision",
                    "path": str(settings_path),
                    "detail": str(exc),
                }
            )
            continue
        ownership_path = config_path.with_name(_OWNERSHIP_NAME)
        try:
            settings_before = _regular_file_bytes_or_none(settings_path, home=home)
            ownership_before = _regular_file_bytes_or_none(ownership_path, home=home)
            config_before = _regular_file_bytes_or_none(config_path, home=home)
            managed_wrapper = config_path.parent / "runtime" / "latent-compass-shadow-hook.py"
            managed_wrapper_before = _regular_file_bytes_or_none(managed_wrapper, home=home)
            owned = _ownership_from_manifest(
                ownership_path,
                host=host,
                home=home,
                raw=ownership_before,
            )
            if ownership_before is None:
                if config_before is None:
                    continue
                raise ValueError("ownership manifest is required before removing host registration")
            owned_command = owned[0] if owned is not None else None
            owned_digest = owned[1] if owned is not None else None
            parsed_owned = (
                _parse_owned_command(owned_command, host) if owned_command is not None else None
            )
            owned_wrapper = parsed_owned[1] if parsed_owned is not None else None
            if owned_wrapper is not None:
                _assert_safe_path_under(config_path.parent, owned_wrapper)
                owned_wrapper_before = (
                    managed_wrapper_before
                    if _path_identity(owned_wrapper) == _path_identity(managed_wrapper)
                    else _regular_file_bytes_or_none(owned_wrapper, home=config_path.parent)
                )
                if (
                    owned_wrapper_before is not None
                    and _bytes_digest(owned_wrapper_before) != owned_digest
                ):
                    raise ValueError("owned hook wrapper content does not match its manifest")
                if _path_identity(owned_wrapper) != _path_identity(managed_wrapper):
                    observations.append(
                        {
                            "path": owned_wrapper,
                            "root": config_path.parent,
                            "content": owned_wrapper_before,
                        }
                    )
            if config_before is not None:
                config = load_shadow_config(json.loads(config_before.decode("utf-8")))
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
        delete_managed_wrapper = (
            remove_hooks
            and owned_wrapper is not None
            and _path_identity(owned_wrapper) == _path_identity(managed_wrapper)
        )
        try:
            settings = (
                _remove_host_payload(
                    settings_path,
                    home=home,
                    host=host,
                    owned_command=owned_command,
                    owned_wrapper=(managed_wrapper if owned_command is None else None),
                    wrapper_to_delete=managed_wrapper if delete_managed_wrapper else None,
                    raw=settings_before,
                )
                if settings_before is not None and remove_hooks
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
        if settings_before is not None and remove_hooks:
            assert settings is not None
            operations.append(
                _file_operation(settings_path, settings_before, _json_bytes(settings))
            )
        operations.append(
            _file_operation(
                config_path,
                config_before,
                None if next_config is None else _json_bytes(next_config),
            )
        )
        if remove_hooks:
            operations.append(_file_operation(ownership_path, ownership_before, None))
            if delete_managed_wrapper:
                operations.append(_file_operation(managed_wrapper, managed_wrapper_before, None))
    files = [_operation_plan(operation) for operation in operations]
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
        "recovery": pending_recovery,
        "_operations": operations,
        "_observations": observations,
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
    home = _lexical_absolute(home)
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
    try:
        snapshots = _transaction_snapshot(home, plan)
    except (OSError, ValueError) as exc:
        cast(list[dict[str, str]], plan["conflicts"]).append(
            {"code": "concurrent_change", "path": "", "detail": str(exc)}
        )
        plan["dry_run"] = False
        return plan
    if plan["changed"] is False:
        plan.pop("_operations", None)
        plan.pop("_observations", None)
        plan["dry_run"] = False
        return plan
    written: dict[Path, bytes | None] = {}
    operations = cast(list[_FileOperation], plan.pop("_operations"))
    plan.pop("_observations", None)
    journal_path: Path | None = None
    journal_content: bytes | None = None
    journal_state: list[bytes] = []
    attempted_creates: set[Path] = set()
    try:
        journal_path, journal_content = _begin_transaction(
            home=home, operation="remove", backup_tag=backup_tag, plan=plan
        )
        journal_state = [journal_content]
        _apply_frozen_operations(
            home=home,
            operations=operations,
            snapshots=snapshots,
            backup_tag=backup_tag,
            written=written,
            journal_path=journal_path,
            journal_state=journal_state,
            attempted_creates=attempted_creates,
        )
    except (OSError, ValueError) as exc:
        unresolved = _rollback_transaction(home, snapshots, written)
        cast(list[dict[str, str]], plan["conflicts"]).append(
            {"code": "apply_failed", "path": "", "detail": str(exc)}
        )
        for path in unresolved:
            cast(list[dict[str, str]], plan["conflicts"]).append(
                {
                    "code": "rollback_conflict",
                    "path": str(path),
                    "detail": "current bytes changed after this transaction wrote the path",
                }
            )
        active_journal = journal_state[0] if journal_state else journal_content
        if journal_path is not None and active_journal is not None:
            collision_cleanup = (
                _complete_transaction_journal(home, journal_path, active_journal)
                if isinstance(exc, _DefiniteWriteRefusalError) and not unresolved
                else _prune_unconfirmed_creates(
                    home,
                    journal_path,
                    active_journal,
                    {
                        **written,
                        **(
                            dict.fromkeys(attempted_creates)
                            if not isinstance(exc, _DefiniteWriteRefusalError)
                            else {}
                        ),
                    },
                )
            )
            cast(list[dict[str, str]], plan["conflicts"]).extend(collision_cleanup)
        plan["dry_run"] = False
        return plan
    if journal_path is not None:
        assert journal_state
        cast(list[dict[str, str]], plan["conflicts"]).extend(
            _complete_transaction_journal(home, journal_path, journal_state[0])
        )
    plan["dry_run"] = False
    return plan


def plan_recover_shadow_hooks(*, home: Path) -> dict[str, object]:
    home = _lexical_absolute(home)
    conflicts, recovery, journal_raw, observations = _pending_recovery_preview(home)
    files = cast(list[dict[str, object]], recovery.get("files", []))
    return {
        "schema_version": 1,
        "operation": "recover",
        "version": __version__,
        "dry_run": True,
        "changed": any(item.get("action") != "unchanged" for item in files),
        "hosts": [],
        "files": files,
        "conflicts": conflicts,
        "next_steps": (
            ["Apply recovery, then rerun the original install or remove dry-run."]
            if recovery.get("pending") is True
            else []
        ),
        "recovery": recovery,
        "_journal_raw": journal_raw,
        "_recovery_observations": observations,
    }


def recover_shadow_hooks(*, home: Path) -> dict[str, object]:
    home = _lexical_absolute(home)
    plan = plan_recover_shadow_hooks(home=home)
    journal_raw = cast(bytes | None, plan.pop("_journal_raw"))
    observations = cast(list[_RecoveryObservation], plan.pop("_recovery_observations"))
    if plan["conflicts"]:
        plan["dry_run"] = False
        return plan
    conflicts = _recover_pending_transaction(
        home, journal_raw=journal_raw, observations=observations
    )
    if conflicts:
        cast(list[dict[str, str]], plan["conflicts"]).extend(conflicts)
    plan["dry_run"] = False
    recovery = cast(dict[str, object], plan["recovery"])
    plan["recovery"] = {**recovery, "completed": not conflicts}
    return plan


def host_status(
    *,
    home: Path,
    project_root: Path,
    hosts: tuple[Host, ...],
    dry_run: bool = False,
) -> dict[str, object]:
    home = _lexical_absolute(home)
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
            "observed": (
                "UNKNOWN"
                if snapshot["status"] == "OBSERVATION_UNKNOWN"
                else bool(snapshot["event_count"])
            ),
        }
        for snapshot in snapshots
    }
    return report


def _public_plan(plan: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def _default_backup_tag() -> str:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


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
    recover = sub.add_parser("recover")
    status = sub.add_parser("status")
    status.add_argument("--project-root", type=Path, default=Path.cwd())
    for command in (install, remove, status):
        command.add_argument("--host", action="append", choices=("codex", "claude"))
    for command in (install, remove, recover, status):
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
    hosts = cast(tuple[Host, ...], tuple(getattr(args, "host", None) or ("codex", "claude")))
    tag = getattr(args, "backup_tag", None) or _default_backup_tag()
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
    elif args.host_operation == "recover":
        result = (
            plan_recover_shadow_hooks(home=args.home)
            if args.dry_run
            else recover_shadow_hooks(home=args.home)
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

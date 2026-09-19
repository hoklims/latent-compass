"""HOK-804 — the rehearsal the repository owner authorised, outside the protocol.

REAL SESSIONS, REAL MONEY. Unlike ``lab_host_bench.py`` this script does contact a real
agent: it starts ``codex exec`` sessions under a key the owner dedicated to it. It exists
for the one purpose the preregistration names: sizing the cost and latency ranges, the
spend ceiling and the pair count, which cannot be chosen blind. It is not a trial runner
and it produces no ``LabTrial``.

What it records is duration and token usage, never an outcome. The agent's events are
read for their ``type`` and their ``usage`` alone: nothing the agent said or did is kept,
because a rehearsal that kept it would be a look at results before the freeze. It counts
tokens and fabricates no money cost: the conversion is a decision of the owner.

What bounds it, in this file and not in a flag:

* at most ``APPROVED_MAX_SESSIONS`` sessions **ever** for one output directory — every
  launch is written to a ledger before its process starts, so a crash still counts;
* at most ``APPROVED_SESSION_SECONDS`` per session, the process tree killed on overrun;
* flags may lower both and never raise them.

What isolates it:

* no key file, nothing starts;
* the key travels on standard input to ``codex login --with-api-key`` — never in an
  argument, an environment variable, a log or the report — into a ``CODEX_HOME`` created
  for the run and deleted after it. The owner's own agent home and login are never read,
  copied or written;
* the child processes get a short allow-listed environment whose home and temporary
  directories point inside that isolated home, so nothing of the parent's environment
  reaches the agent;
* each session works on its own copy of one commit, extracted from Git objects into a
  temporary directory. The source repository is only read.

The spend limit of the dedicated key is held by the provider, outside this script, and
its enforcement is not instantaneous: the session ledger is the second guard, not the
first.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

APPROVED_MAX_SESSIONS: Final = 6
APPROVED_SESSION_SECONDS: Final = 900
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2
EXIT_KEY_COPY_LEFT: Final = 3
LEDGER: Final = "sessions.jsonl"
RECORDS: Final = "records.jsonl"
REPORT: Final = "rehearsal-report.json"
USAGE_FIELDS: Final = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
# What a child process inherits. Everything else of the parent's environment stays behind.
PASSED_THROUGH: Final = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL")
KEY_SHAPE: Final = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


class RehearsalRefusedError(Exception):
    """Nothing was started."""


class SessionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    TURN_FAILED = "TURN_FAILED"
    TIMED_OUT = "TIMED_OUT"
    FAILED_TO_START = "FAILED_TO_START"


@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str


@dataclass(frozen=True)
class SessionRecord:
    """Everything the rehearsal keeps about a session. There is no outcome field, on purpose."""

    session_index: int
    task_id: str
    started_at: str
    duration_ms: int
    status: str
    turns: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    diagnostic: str


def load_tasks(path: Path) -> list[Task]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise RehearsalRefusedError("the task file holds no task")
    tasks = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"task_id", "prompt"}:
            raise RehearsalRefusedError("a task is exactly a task_id and a prompt")
        task_id, prompt = item["task_id"], item["prompt"]
        if not isinstance(task_id, str) or not isinstance(prompt, str) or not prompt.strip():
            raise RehearsalRefusedError("a task is exactly a task_id and a prompt")
        tasks.append(Task(task_id=task_id, prompt=prompt))
    if len({task.task_id for task in tasks}) != len(tasks):
        raise RehearsalRefusedError("task ids repeat")
    return tasks


def read_key(path: Path) -> bytes:
    """The dedicated key, as bytes. It is never decoded, printed or stored by this script."""
    if not path.is_file():
        raise RehearsalRefusedError("the dedicated key file does not exist: nothing is started")
    key = path.read_bytes().removeprefix(b"\xef\xbb\xbf").strip()
    if not key:
        raise RehearsalRefusedError("the dedicated key file is empty: nothing is started")
    return key


def child_environment(home: Path) -> dict[str, str]:
    """A short allow-list, with every home and temporary directory inside the isolated home."""
    environment = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    temporary = home / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    for name in ("CODEX_HOME", "HOME", "USERPROFILE"):
        environment[name] = str(home)
    for name in ("TEMP", "TMP", "TMPDIR"):
        environment[name] = str(temporary)
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    return environment


def launched_so_far(out: Path) -> int:
    ledger = out / LEDGER
    return len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0


def read_usage(lines: Iterable[str]) -> tuple[dict[str, int], int, bool]:
    """Usage counters, completed turns, and whether a turn failed. Nothing else is kept."""
    usage = dict.fromkeys(USAGE_FIELDS, 0)
    turns, failed = 0, False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            turns += 1
            reported = event.get("usage")
            if isinstance(reported, dict):
                for name in USAGE_FIELDS:
                    value = reported.get(name)
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[name] += value
        elif kind in ("turn.failed", "error"):
            failed = True
    return usage, turns, failed


def scrub(text: str, key: bytes) -> str:
    """A harness diagnostic, with the key and anything shaped like one removed."""
    cleaned = text.replace(key.decode("utf-8", errors="ignore"), "[key]")
    return KEY_SHAPE.sub("[key]", cleaned).strip()[-300:]


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603 - fixed system tool, numeric argument
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _discard_home(home: Path) -> bool:
    """Delete the isolated home, which holds the only copy of the key this script wrote."""
    shutil.rmtree(home, ignore_errors=True)
    if home.exists():
        # A locked file must not leave the stored key behind: go for it first, then retry.
        for leftover in home.rglob("auth.json"):
            with contextlib.suppress(OSError):
                leftover.unlink()
        shutil.rmtree(home, ignore_errors=True)
    return not any(home.rglob("auth.json")) if home.exists() else True


def _extract(source: Path, ref: str, into: Path) -> None:
    archive = subprocess.run(  # noqa: S603 - fixed arguments, the repository is only read
        ["git", "-C", str(source), "-c", "core.autocrlf=false", "archive", "--format=tar", ref],  # noqa: S607
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")


def _login(codex: Sequence[str], environment: Mapping[str, str], key: bytes) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - resolved command, the key travels on stdin
            [*codex, "login", "--with-api-key"],
            input=key + b"\n",
            env=dict(environment),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RehearsalRefusedError("the agent CLI could not be started for the login") from error
    if completed.returncode != 0:
        # Its output is not echoed: a CLI may repeat the key it was given.
        raise RehearsalRefusedError("the agent CLI refused the dedicated key")


def _run_session(
    codex: Sequence[str],
    environment: Mapping[str, str],
    workdir: Path,
    model: str | None,
    prompt: str,
    seconds: int,
) -> tuple[SessionStatus, int, bytes, bytes]:
    command = [*codex, "exec", "--json", "--color", "never", "--sandbox", "workspace-write"]
    command += ["--skip-git-repo-check", "--ephemeral", "--cd", str(workdir)]
    if model is not None:
        command += ["--model", model]
    command.append("-")  # the prompt travels on stdin: no quoting through a shell shim
    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - resolved command, fixed arguments, no shell
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            cwd=workdir,
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        return SessionStatus.FAILED_TO_START, 0, b"", b""
    try:
        stdout, stderr = process.communicate(input=prompt.encode("utf-8"), timeout=seconds)
        status = SessionStatus.COMPLETED
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate()
        status = SessionStatus.TIMED_OUT
    return status, round((time.monotonic() - started) * 1000), stdout, stderr


def _append(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _spread(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {}
    return {"min": min(values), "median": round(statistics.median(values)), "max": max(values)}


def write_report(
    out: Path, codex_version: str, model: str | None, key_copy_removed: bool
) -> dict[str, Any]:
    records_path = out / RECORDS
    records = (
        [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
        if records_path.is_file()
        else []
    )
    report: dict[str, Any] = {
        "purpose": "rehearsal outside the protocol: durations and token usage, never an outcome",
        "approved_max_sessions": APPROVED_MAX_SESSIONS,
        "approved_session_seconds": APPROVED_SESSION_SECONDS,
        "sessions_launched": launched_so_far(out),
        "key_copy_removed": key_copy_removed,
        "agent_cli": codex_version,
        "model_declared": model,
        "records": records,
        "duration_ms": _spread([record["duration_ms"] for record in records]),
        "tokens": {name: _spread([record[name] for record in records]) for name in USAGE_FIELDS},
    }
    (out / REPORT).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def run_rehearsal(
    *,
    key_file: Path,
    source: Path,
    ref: str,
    tasks: Sequence[Task],
    out: Path,
    codex: Sequence[str],
    model: str | None = None,
    max_sessions: int = APPROVED_MAX_SESSIONS,
    session_seconds: int = APPROVED_SESSION_SECONDS,
) -> dict[str, Any]:
    """Run what is left of the allowance. Refuses, before anything starts, what it cannot bound."""
    if not 1 <= max_sessions <= APPROVED_MAX_SESSIONS:
        raise RehearsalRefusedError("the session count may be lowered, never raised")
    if not 1 <= session_seconds <= APPROVED_SESSION_SECONDS:
        raise RehearsalRefusedError("the session length may be lowered, never raised")
    key = read_key(key_file)
    resolved = shutil.which(codex[0])
    if resolved is None:
        raise RehearsalRefusedError("the agent CLI is not installed on this host")
    command = [resolved, *codex[1:]]
    known = subprocess.run(  # noqa: S603 - fixed arguments, the repository is only read
        ["git", "-C", str(source), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if known.returncode != 0:
        # A mistyped commit must not cost a session of the allowance.
        raise RehearsalRefusedError("the source repository does not hold that commit")
    out.mkdir(parents=True, exist_ok=True)
    allowance = min(max_sessions, APPROVED_MAX_SESSIONS) - launched_so_far(out)
    if allowance <= 0:
        raise RehearsalRefusedError("the approved session allowance is spent")

    home = Path(tempfile.mkdtemp(prefix="lc-rehearsal-home-"))
    version = ""
    try:
        environment = child_environment(home)
        _login(command, environment, key)
        version = subprocess.run(  # noqa: S603 - resolved command, fixed arguments
            [*command, "--version"], env=environment, capture_output=True, check=False
        ).stdout.decode("utf-8", errors="replace")
        for task in list(tasks)[:allowance]:
            index = launched_so_far(out) + 1
            started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Counted before it starts: a crash must not give a session back.
            _append(
                out / LEDGER,
                {"session_index": index, "task_id": task.task_id, "started_at": started_at},
            )
            workdir = Path(tempfile.mkdtemp(prefix="lc-rehearsal-copy-"))
            try:
                _extract(source, ref, workdir)
                status, duration, stdout, stderr = _run_session(
                    command, environment, workdir, model, task.prompt, session_seconds
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            usage, turns, failed = read_usage(stdout.decode("utf-8", errors="replace").splitlines())
            if status is SessionStatus.COMPLETED and (failed or turns == 0):
                status = SessionStatus.TURN_FAILED
            # A diagnostic is kept only when no turn completed: then there is no outcome to leak.
            diagnostic = scrub(stderr.decode("utf-8", errors="replace"), key) if turns == 0 else ""
            record = SessionRecord(
                session_index=index,
                task_id=task.task_id,
                started_at=started_at,
                duration_ms=duration,
                status=status.value,
                turns=turns,
                diagnostic=diagnostic,
                **usage,
            )
            _append(out / RECORDS, asdict(record))
    finally:
        key_copy_removed = _discard_home(home)
    return write_report(out, scrub(version, key), model, key_copy_removed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="repository to copy, read only")
    parser.add_argument("--ref", required=True, help="commit the throwaway copies are taken from")
    parser.add_argument("--tasks", type=Path, required=True, help="JSON list of task_id and prompt")
    parser.add_argument("--out", type=Path, required=True, help="ledger and report directory")
    parser.add_argument("--codex-command", nargs="+", default=["codex"])
    parser.add_argument("--model")
    parser.add_argument("--max-sessions", type=int, default=APPROVED_MAX_SESSIONS)
    parser.add_argument("--session-seconds", type=int, default=APPROVED_SESSION_SECONDS)
    arguments = parser.parse_args(argv)
    try:
        report = run_rehearsal(
            key_file=arguments.key_file,
            source=arguments.source,
            ref=arguments.ref,
            tasks=load_tasks(arguments.tasks),
            out=arguments.out,
            codex=arguments.codex_command,
            model=arguments.model,
            max_sessions=arguments.max_sessions,
            session_seconds=arguments.session_seconds,
        )
    except RehearsalRefusedError as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    statuses = [record["status"] for record in report["records"]]
    print(f"{report['sessions_launched']} of {APPROVED_MAX_SESSIONS} sessions launched: {statuses}")
    if not report["key_copy_removed"]:
        print(
            "WARNING: a copy of the dedicated key may remain under a lc-rehearsal-home-* "
            "temporary directory. Delete it, and revoke the key.",
            file=sys.stderr,
        )
        return EXIT_KEY_COPY_LEFT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

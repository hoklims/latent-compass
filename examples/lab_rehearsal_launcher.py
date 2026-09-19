"""HOK-804 — the rehearsal the repository owner authorised, outside the protocol.

REAL SESSIONS, REAL MONEY. Unlike ``lab_host_bench.py`` this script does contact a real
agent: it starts ``codex exec`` sessions under a key the owner dedicated to it. It exists
for the one purpose the preregistration names: sizing the cost and latency ranges, the
spend ceiling and the pair count, which cannot be chosen blind. It is not a trial runner
and it produces no ``LabTrial``.

What it keeps: per session, a duration, token counts, a harness status (completed, failed
turn, timed out, failed to start) and a diagnostic **label** from a fixed vocabulary. It
keeps no text the agent or the CLI produced — not an answer, not an error message — and no
judgement of success: nobody judges a rehearsal task. The agent's events are read for
their ``type`` and their ``usage`` and dropped. It counts tokens and fabricates no money
cost. A harness status is still a fact about a task, which is why rehearsal tasks belong
to no population.

What bounds it:

* six sessions **per dedicated key file**: the ledger lives beside the key, so another
  output directory does not give the allowance back. A session is written to it after its
  copy is ready and before its process starts: a crash still counts, a failed copy does
  not. A lock beside the key refuses a second run at the same time;
* fifteen minutes per session. On overrun the process tree is killed and the pipes are
  given a bounded time to drain: an orphan that keeps them open is abandoned, not awaited.
  An interruption of this script kills the session before it propagates;
* flags may lower both ceilings and never raise them.

**The ledger is not a spend limit.** It guards against mistakes, not against its owner,
who can delete it. The money bound is the hard limit the owner set on the key's project at
the provider — outside this script, and not instantaneous.

What isolates the key:

* no key file, nothing starts; a key file holding more than one token is refused;
* the key travels on standard input to ``codex login --with-api-key`` — never in an
  argument or an environment variable — into a ``CODEX_HOME`` created beside the key file
  for the run and deleted after it. Whether that deletion succeeded is printed, and
  reported, on every path including an interruption;
* that home's configuration pins the CLI's credential store to a file inside it, and the
  run stops unless the login really left its credentials there: a CLI that put the key
  somewhere this script cannot delete — an OS keyring — is refused before any session;
* the CLI is resolved from an absolute path or from ``PATH`` entries only, never from the
  current directory: the key is not handed to whatever ``codex`` happens to lie there;
* **this script** never reads, copies or writes the owner's own agent home or login.

What it does **not** control, and does not claim: what the agent may read on the machine
under the CLI's ``workspace-write`` sandbox, what the CLI writes outside ``CODEX_HOME``,
and the network. The agent gets an allow-listed environment — homes, temporary
directories and Git's global configuration inside the isolated home, Git's system
configuration and credential prompts off, a ``PATH`` reduced to the tools it needs — and
its own copy of one commit extracted from Git objects. That narrows what it meets by
default; it is not a jail. Do not run this under a harness that prints local variables
in tracebacks: the key is one of them.
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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

APPROVED_MAX_SESSIONS: Final = 6
APPROVED_SESSION_SECONDS: Final = 900
HOUSEKEEPING_SECONDS: Final = 60
ARCHIVE_SECONDS: Final = 300
# How long the pipes of a killed session may take to drain before they are given up.
# Not Final: a test lowers it rather than wait.
DRAIN_SECONDS = 15
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2
EXIT_KEY_COPY_LEFT: Final = 3
RECORDS: Final = "records.jsonl"
REPORT: Final = "rehearsal-report.json"
# Where the CLI documents that a file credential store lives, inside ``CODEX_HOME``.
CREDENTIALS: Final = "auth.json"
USAGE_FIELDS: Final = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
# What a child process inherits. PATH is rebuilt, everything else stays behind.
PASSED_THROUGH: Final = ("PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL")
MODEL_SHAPE: Final = re.compile(r"[A-Za-z0-9._:\-]{1,80}")
VERSION_SHAPE: Final = re.compile(r"[\w .\-]{1,60}")
# A diagnostic is a label, never text: whatever the CLI wrote is classified and dropped.
DIAGNOSES: Final = (
    ("AUTH_REJECTED", re.compile(r"\b401\b|unauthori[sz]ed|(invalid|incorrect)[ _]api[ _]key")),
    ("SPEND_LIMIT", re.compile(r"insufficient_quota|spend[ _]limit|billing|credit balance")),
    ("RATE_LIMITED", re.compile(r"\b429\b|rate[ _]limit")),
    ("NETWORK", re.compile(r"timed? ?out|connection|dns|network|unreachable")),
)


class RehearsalRefusedError(Exception):
    """Nothing was started, or nothing more will be."""


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
    """Everything the rehearsal keeps about a session. No field can hold text or a verdict."""

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
    copy_removed: bool


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
    """The dedicated key, as bytes. This script never decodes, prints or stores it."""
    if not path.is_file():
        raise RehearsalRefusedError("the dedicated key file does not exist: nothing is started")
    key = path.read_bytes().removeprefix(b"\xef\xbb\xbf").strip()
    if not key:
        raise RehearsalRefusedError("the dedicated key file is empty: nothing is started")
    if len(key.split()) != 1:
        raise RehearsalRefusedError("the dedicated key file holds more than one token")
    return key


def ledger_path(key_file: Path) -> Path:
    return key_file.with_name(key_file.name + ".sessions.jsonl")


def lock_path(key_file: Path) -> Path:
    return key_file.with_name(key_file.name + ".lock")


def launched_so_far(key_file: Path) -> int:
    ledger = ledger_path(key_file)
    if not ledger.is_file():
        return 0
    lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    return sum(1 for line in lines if line.strip())


@contextlib.contextmanager
def exclusive(key_file: Path) -> Iterator[None]:
    """One rehearsal at a time per key: two runs counting together could overshoot."""
    lock = lock_path(key_file)
    try:
        lock.open("x", encoding="utf-8").close()
    except FileExistsError as error:
        raise RehearsalRefusedError(
            "a lock lies beside the key file: another rehearsal is running, or one crashed. "
            "Check that no session is running, then remove it"
        ) from error
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def resolve_command(name: str) -> str | None:
    """An absolute path, or a bare name found on PATH — never in the current directory."""
    candidate = Path(name)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    if candidate.name != name:
        return None
    suffixes = [""]
    if sys.platform == "win32":
        extensions = [item for item in os.environ.get("PATHEXT", "").split(os.pathsep) if item]
        if candidate.suffix.upper() not in {item.upper() for item in extensions}:
            suffixes = extensions
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not Path(directory).is_absolute():
            continue
        for suffix in suffixes:
            found = Path(directory, name + suffix)
            if found.is_file() and (sys.platform == "win32" or os.access(found, os.X_OK)):
                return str(found)
    return None


def tool_path(command: str) -> str:
    """The directories of the CLI and of the few tools an agent needs, and nothing else."""
    directories = [str(Path(command).parent), str(Path(sys.executable).parent)]
    for tool in ("git", "node"):
        found = resolve_command(tool)
        if found is not None:
            directories.append(str(Path(found).parent))
    root = os.environ.get("SYSTEMROOT")
    directories += [str(Path(root, "System32")), root] if root else ["/usr/bin", "/bin"]
    return os.pathsep.join(dict.fromkeys(directories))


def child_environment(home: Path, path: str) -> dict[str, str]:
    """An allow-list: homes, temporary directories and Git's configuration inside the home."""
    environment = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    temporary = home / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    gitconfig = home / ".gitconfig"
    gitconfig.touch()
    # The CLI documents this store as its default. Pinned all the same: a keyring would
    # put the key where this script cannot delete it.
    (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
    environment["PATH"] = path
    for name in ("CODEX_HOME", "HOME", "USERPROFILE"):
        environment[name] = str(home)
    for name in ("TEMP", "TMP", "TMPDIR"):
        environment[name] = str(temporary)
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    # A system-wide credential helper is not masked by a redirected home.
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


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


def diagnose(stderr: bytes) -> str:
    """A label for what the CLI wrote on stderr. The text itself is never kept."""
    text = stderr.decode("utf-8", errors="replace").lower()
    if not text.strip():
        return "NONE"
    return next((label for label, pattern in DIAGNOSES if pattern.search(text)), "WITHHELD")


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT")
        tool = str(Path(root, "System32", "taskkill.exe")) if root else "taskkill"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603 - system tool, numeric argument
                [tool, "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=HOUSEKEEPING_SECONDS,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _bounded(
    command: Sequence[str],
    *,
    stdin: bytes,
    environment: Mapping[str, str] | None,
    cwd: Path | None,
    seconds: int,
) -> tuple[bool, int | None, bytes, bytes]:
    """Run to completion or to ``seconds``. Returns whether it overran, and what it wrote."""
    process = subprocess.Popen(  # noqa: S603 - resolved command, fixed arguments, no shell
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if environment is None else dict(environment),
        cwd=cwd,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # An orphan still holds the pipes. It is abandoned, not awaited: the ceiling on
            # a session must hold whatever the session left behind.
            stdout, stderr = b"", b""
            process.poll()  # reap the killed session itself
        return True, process.returncode, stdout, stderr
    except BaseException:
        # An interruption of this script must not leave a paid session running.
        _kill_tree(process)
        raise
    return False, process.returncode, stdout, stderr


def _discard(directory: Path) -> bool:
    """Delete a directory and say whether it is really gone."""
    shutil.rmtree(directory, ignore_errors=True)
    if directory.exists():
        # A locked entry must not shelter the rest: remove what can be, then try again.
        for leftover in sorted(directory.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                if leftover.is_file() or leftover.is_symlink():
                    leftover.unlink()
        shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def _extract(git: str, source: Path, ref: str, into: Path) -> None:
    try:
        overran, code, archive, _ = _bounded(
            [git, "-C", str(source), "-c", "core.autocrlf=false", "archive", "--format=tar", ref],
            stdin=b"",
            environment=None,
            cwd=None,
            seconds=ARCHIVE_SECONDS,
        )
        if overran or code != 0:
            raise RehearsalRefusedError("the copy of the commit could not be made")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(into, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise RehearsalRefusedError("the copy of the commit could not be made") from error


def _login(codex: Sequence[str], environment: Mapping[str, str], key: bytes) -> None:
    try:
        overran, code, _, _ = _bounded(
            [*codex, "login", "--with-api-key"],
            stdin=key + b"\n",
            environment=environment,
            cwd=None,
            seconds=HOUSEKEEPING_SECONDS,
        )
    except OSError as error:
        raise RehearsalRefusedError("the agent CLI could not be started for the login") from error
    if overran or code != 0:
        # Its output is not echoed: a CLI may repeat the key it was given.
        raise RehearsalRefusedError("the agent CLI refused the dedicated key")


def _version(codex: Sequence[str], environment: Mapping[str, str]) -> str:
    try:
        overran, _, stdout, _ = _bounded(
            [*codex, "--version"],
            stdin=b"",
            environment=environment,
            cwd=None,
            seconds=HOUSEKEEPING_SECONDS,
        )
    except OSError:
        return "unknown"
    declared = stdout.decode("utf-8", errors="replace").strip()
    return declared if not overran and VERSION_SHAPE.fullmatch(declared) else "unknown"


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
    out: Path, key_file: Path, codex_version: str, model: str | None, key_copy_removed: bool
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
        "sessions_launched": launched_so_far(key_file),
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
    if model is not None and not MODEL_SHAPE.fullmatch(model):
        raise RehearsalRefusedError("the model name is not a plain identifier")
    key = read_key(key_file)
    resolved, git = resolve_command(codex[0]), resolve_command("git")
    if resolved is None:
        raise RehearsalRefusedError("the agent CLI is not an absolute path and is not on PATH")
    if git is None:
        raise RehearsalRefusedError("git is not on PATH")
    command = [resolved, *codex[1:]]
    verify = [git, "-C", str(source), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]
    overran, code, _, _ = _bounded(
        verify, stdin=b"", environment=None, cwd=None, seconds=HOUSEKEEPING_SECONDS
    )
    if overran or code != 0:
        # A mistyped commit must not cost a session of the allowance.
        raise RehearsalRefusedError("the source repository does not hold that commit")

    with exclusive(key_file):
        allowance = max_sessions - launched_so_far(key_file)
        if allowance <= 0:
            raise RehearsalRefusedError("the approved session allowance is spent")
        out.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix="lc-rehearsal-home-", dir=key_file.parent))
        version, key_copy_removed = "unknown", False
        try:
            environment = child_environment(home, tool_path(resolved))
            _login(command, environment, key)
            if not (home / CREDENTIALS).is_file():
                # The CLI took the key and did not leave it where this script deletes it.
                raise RehearsalRefusedError(
                    "the agent CLI accepted the key and did not store it in the isolated home: "
                    "find where it went, delete it there, and revoke the key"
                )
            version = _version(command, environment)
            session = [*command, "exec", "--json", "--color", "never"]
            session += ["--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral"]
            if model is not None:
                session += ["--model", model]
            for task in list(tasks)[:allowance]:
                workdir = Path(tempfile.mkdtemp(prefix="lc-rehearsal-copy-"))
                status, duration, stdout, stderr = SessionStatus.FAILED_TO_START, 0, b"", b""
                try:
                    # The copy comes first: a copy that cannot be made costs no session.
                    _extract(git, source, ref, workdir)
                    index = launched_so_far(key_file) + 1
                    started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    # Counted before it starts: a crash must not give a session back.
                    _append(
                        ledger_path(key_file),
                        {"session_index": index, "task_id": task.task_id, "started_at": started_at},
                    )
                    started = time.monotonic()
                    # The prompt travels on stdin ("-"): nothing is quoted through a shell shim.
                    with contextlib.suppress(OSError):
                        overran, _, stdout, stderr = _bounded(
                            [*session, "--cd", str(workdir), "-"],
                            stdin=task.prompt.encode("utf-8"),
                            environment=environment,
                            cwd=workdir,
                            seconds=session_seconds,
                        )
                        status = SessionStatus.TIMED_OUT if overran else SessionStatus.COMPLETED
                    duration = round((time.monotonic() - started) * 1000)
                finally:
                    copy_removed = _discard(workdir)
                    if not copy_removed:
                        print(
                            f"WARNING: a session copy could not be removed: {workdir}",
                            file=sys.stderr,
                        )
                usage, turns, failed = read_usage(
                    stdout.decode("utf-8", errors="replace").splitlines()
                )
                if status is SessionStatus.COMPLETED and (failed or turns == 0):
                    status = SessionStatus.TURN_FAILED
                record = SessionRecord(
                    session_index=index,
                    task_id=task.task_id,
                    started_at=started_at,
                    duration_ms=duration,
                    status=status.value,
                    turns=turns,
                    diagnostic="NONE" if status is SessionStatus.COMPLETED else diagnose(stderr),
                    copy_removed=copy_removed,
                    **usage,
                )
                _append(out / RECORDS, asdict(record))
        finally:
            # On every path, an interruption included: say whether the key copy is gone.
            key_copy_removed = _discard(home)
            if not key_copy_removed:
                print(
                    f"WARNING: a copy of the dedicated key may remain under {home}. "
                    "Delete it, and revoke the key.",
                    file=sys.stderr,
                )
    return write_report(out, key_file, version, model, key_copy_removed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="repository to copy, read only")
    parser.add_argument("--ref", required=True, help="commit the throwaway copies are taken from")
    parser.add_argument("--tasks", type=Path, required=True, help="JSON list of task_id and prompt")
    parser.add_argument("--out", type=Path, required=True, help="records and report directory")
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
    return EXIT_OK if report["key_copy_removed"] else EXIT_KEY_COPY_LEFT


if __name__ == "__main__":
    raise SystemExit(main())

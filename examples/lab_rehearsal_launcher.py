"""HOK-804 — the rehearsal the repository owner authorised, outside the protocol.

REAL SESSIONS, REAL MONEY. Unlike ``lab_host_bench.py`` this script does contact a real
agent: it starts ``codex exec`` sessions under a key the owner dedicated to it. It exists
for the one purpose the preregistration names: sizing the cost and latency ranges, the
spend ceiling and the pair count, which cannot be chosen blind. It is not a trial runner
and it produces no ``LabTrial``.

What it keeps: per session, a duration, token counts, a harness status (completed, failed
turn, timed out, failed to start) and a diagnostic **label** from a fixed vocabulary. It
keeps no text the agent or the CLI produced — not an answer, not an error message — with
one exception: the CLI's version, and only when it has the shape of a version. It keeps no
judgement of success: nobody judges a rehearsal task. The agent's events are read for
their ``type`` and their ``usage`` and dropped. It counts tokens and fabricates no money
cost; the report's ranges cover completed sessions only, beside a count per status. A
harness status is still a fact about a task, which is why rehearsal tasks belong to no
population.

What bounds it:

* six sessions **per directory holding the key file**: the ledger lies beside the key
  under a fixed name, so neither another output directory nor a copy of the key file
  beside it gives the allowance back. A session is written to it after its copy is ready
  and before its process starts: a crash still counts, a failed copy does not. A lock
  beside the key refuses a second run at the same time;
* a run stops at the first session that fails — failed to start, failed turn, error exit.
  A CLI that rejects a flag or the key would otherwise burn the allowance in seconds;
* fifteen minutes per session **while this script is alive**, plus at most some forty
  seconds to kill it and drain its pipes. On overrun the process tree is killed. A kill
  that does not take is printed with the process id, and so is the proof that it missed
  something: pipes still held once the tree is dead. What holds them is not awaited and
  not killed — it is recorded, and the run stops there: no session is started while
  something of the last one may still be alive. An interruption of this script — Ctrl+C
  anywhere, Ctrl+Break on Windows, a termination signal on POSIX — kills the session
  before it propagates. A launcher killed outright — its process terminated, which on
  Windows is what any termination request from outside amounts to, or the power cut —
  kills nothing: the session it started runs to its own end;
* flags may lower both ceilings and never raise them.

**The ledger is not a spend limit.** It guards against mistakes — not against its owner,
who can delete it, and not against the agent this script starts, which on a platform
without a sandbox can write the ledger like anything else. The money bound is the hard
limit the owner set on the key's project at the provider — outside this script, and not
instantaneous.

What isolates the key:

* no key file, nothing starts; a file that does not hold exactly one API key — several
  tokens, too long, not the provider's prefix — is refused: a wrong file is not sent;
* the CLI is resolved from an absolute path or from absolute ``PATH`` entries only, never
  from the current directory, and it has to state a version before the key is handed to
  it; ``--cli-version`` pins that version;
* the key travels on standard input to ``codex login --with-api-key`` — never in an
  argument or an environment variable — into a ``CODEX_HOME`` created beside the key file
  for the run and deleted after it. Whether that deletion succeeded is printed on every
  path on which this script still runs, an interruption included, and a second one during
  the deletion included; it is written in the report, and is the exit code, when the run
  reaches its end;
* a run that died without unwinding leaves that home, and the key in it, behind. The next
  run looks for it first, deletes it, says so, and refuses once;
* this script deletes only real directories it made. A link or a junction — under the
  name of a home, of a session copy, or anywhere inside one — is never followed and never
  removed: what lies behind it is not this script's, the owner's own agent home least of
  all. It is named, and the run refuses or reports a failure;
* that home's configuration pins the CLI's credential store to a file inside it, and the
  run stops unless the login really left its credentials there: a CLI that put the key
  somewhere this script cannot delete — an OS keyring — is refused before any session;
* **this script** never reads, copies or writes the owner's own agent home or login;
* **the agent can read the key it runs under**: the CLI keeps it in the home the agent is
  given. That is why the key is dedicated and capped — and why it is to be **revoked when
  the rehearsal is over**.

What it does **not** control, and does not claim. The CLI's ``workspace-write`` policy
lets the agent **read the whole file system** by design. What stops the agent from
**writing** outside its copy is the platform's sandbox: documented for Linux and macOS. On
native Windows the CLI applies **no sandbox unless its configuration asks for one, and
this script does not ask**: the Windows sandbox could not be exercised by the author, and
is not something to switch on blind on the owner's machine. On Windows, the agent can
read and write whatever the account running this script can — the owner's own agent home
included, and this script's ledger and lock. Nor does this script control what the CLI
writes outside ``CODEX_HOME`` — on Windows a redirected home does not move what a program
asks the system for, so anything else the CLI keeps "in the home" may land in the owner's
real profile — or the network. The agent gets an allow-listed
environment — homes, temporary directories and Git's global configuration inside the
isolated home, Git's system configuration and credential prompts off, a ``PATH`` reduced
to the tools it needs — and its own copy of one commit extracted from Git objects. That
narrows what it meets by default; it is not a jail. ``--ephemeral`` asks the CLI not to
persist session files; if the isolated home cannot be deleted, do not read what else it
holds. Do not run this under a harness that prints local variables in tracebacks: the key
is one of them until the login is done, and Python does not erase it from memory.
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
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NamedTuple

APPROVED_MAX_SESSIONS: Final = 6
APPROVED_SESSION_SECONDS: Final = 900
HOUSEKEEPING_SECONDS: Final = 60
ARCHIVE_SECONDS: Final = 300
# How long the pipes of a killed session may take to drain before they are given up, and
# how long a killed process may take to die before that is said. Not Final: tests lower
# them rather than wait.
DRAIN_SECONDS = 15
KILL_SECONDS = 5
TASKKILL_SECONDS: Final = 20
INTERRUPTIONS_ABSORBED: Final = 5
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2
EXIT_KEY_COPY_LEFT: Final = 3
EXIT_INTERRUPTED: Final = 130
RECORDS: Final = "records.jsonl"
REPORT: Final = "rehearsal-report.json"
# Beside the key file, under fixed names: a copy of the key file shares them.
LEDGER: Final = "rehearsal.sessions.jsonl"
LOCK: Final = "rehearsal.lock"
HOME_PREFIX: Final = "lc-rehearsal-home-"
COPY_PREFIX: Final = "lc-rehearsal-copy-"
# Where the CLI documents that a file credential store lives, inside ``CODEX_HOME``.
CREDENTIALS: Final = "auth.json"
KEY_PREFIX: Final = b"sk-"
MAX_KEY_BYTES: Final = 512
USAGE_FIELDS: Final = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
# What a child process inherits. PATH is rebuilt, everything else stays behind.
PASSED_THROUGH: Final = ("PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL")
MODEL_SHAPE: Final = re.compile(r"[A-Za-z0-9._:\-]{1,80}")
# A task id lands in the records and the report: a name, never a path or free text.
TASK_ID_SHAPE: Final = re.compile(r"[A-Za-z0-9._\-]{1,80}")
# The label of a session whose pipes something still held after its process tree was killed.
PIPES_HELD: Final = "PIPES_HELD_AFTER_KILL"
# A name, a space, three numbers. A key has no space: it cannot pass for a version.
VERSION_SHAPE: Final = re.compile(
    r"[A-Za-z][A-Za-z\-]{0,19} \d{1,4}\.\d{1,4}\.\d{1,4}(-[\w.]{1,20})?"
)
# A batch shim hands its command line to the shell, which reads these again.
BATCH_SUFFIXES: Final = (".cmd", ".bat")
BATCH_UNSAFE: Final = re.compile(r'[&|<>^%!"()\r\n]')
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
        if not TASK_ID_SHAPE.fullmatch(task_id):
            raise RehearsalRefusedError("a task_id is a plain name: it is written in the report")
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
    if len(key) > MAX_KEY_BYTES or not key.startswith(KEY_PREFIX):
        # The wrong file — a credential store, say — must not be sent to the CLI whole.
        raise RehearsalRefusedError("the dedicated key file does not look like an API key")
    return key


def ledger_path(key_file: Path) -> Path:
    return key_file.with_name(LEDGER)


def lock_path(key_file: Path) -> Path:
    return key_file.with_name(LOCK)


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
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise RehearsalRefusedError(
            "a lock lies beside the key file: another rehearsal is running, or one died. The "
            "lock names the process that took it. Remove it ONLY once that process and every "
            "agent session are gone — a run that finds no lock deletes what it takes for a dead "
            "run's copy of the key, a live run's included"
        ) from error
    try:
        with handle:
            # For the operator only: which process to look for before removing a stale lock.
            handle.write(f"pid {os.getpid()} since {datetime.now(UTC).isoformat()}\n")
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def resolve_command(name: str) -> str | None:
    """An absolute path, or a bare name found on PATH — never in the current directory."""
    candidate = Path(name)
    runnable = sys.platform == "win32" or os.access(candidate, os.X_OK)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() and runnable else None
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
    temporary, roaming, local = (
        home / "tmp",
        home / "AppData" / "Roaming",
        home / "AppData" / "Local",
    )
    for directory in (temporary, roaming, local):
        directory.mkdir(parents=True, exist_ok=True)
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
    environment["APPDATA"] = str(roaming)
    environment["LOCALAPPDATA"] = str(local)
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


def _kill_tree(process: subprocess.Popen[bytes]) -> bool:
    """Kill the process and its descendants. Says whether the tree kill reported success."""
    confirmed = False
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT")
        tool = str(Path(root, "System32", "taskkill.exe")) if root else "taskkill"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            done = subprocess.run(  # noqa: S603 - system tool, numeric argument
                [tool, "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=TASKKILL_SECONDS,
            )
            # 128: the process was already gone.
            confirmed = done.returncode in (0, 128)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            confirmed = True
        except ProcessLookupError:
            confirmed = True
        except PermissionError:
            confirmed = False
    with contextlib.suppress(OSError):
        process.kill()
    return confirmed


def _kill_and_say(process: subprocess.Popen[bytes]) -> None:
    """A kill that does not take must not pass in silence: a paid session may be alive."""
    confirmed = _kill_tree(process)
    try:
        process.wait(timeout=KILL_SECONDS)
    except subprocess.TimeoutExpired:
        confirmed = False
    if not confirmed:
        print(
            f"WARNING: process {process.pid} or one of its children may still be running "
            "after the kill: a paid session may be alive. Stop it by hand.",
            file=sys.stderr,
        )


class Ran(NamedTuple):
    """What a bounded process did. ``pipes_held``: something outlived the kill of its tree."""

    overran: bool
    returncode: int | None
    stdout: bytes
    stderr: bytes
    pipes_held: bool = False


def _bounded(
    command: Sequence[str],
    *,
    stdin: bytes,
    environment: Mapping[str, str] | None,
    cwd: Path | None,
    seconds: int,
) -> Ran:
    """Run to completion or to ``seconds``. Says whether it overran, and what it wrote."""
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
        _kill_and_say(process)
        try:
            stdout, stderr = process.communicate(timeout=DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # Something that outlived the kill still holds the pipes. It is not awaited —
            # the ceiling on a session must hold whatever the session left behind — and it
            # is not passed over in silence: this is the proof that the kill missed something.
            process.poll()  # reap the killed session itself
            print(
                f"WARNING: something started by process {process.pid} outlived the kill and "
                "still holds its pipes: a paid session may be alive. Stop it by hand.",
                file=sys.stderr,
            )
            return Ran(True, process.returncode, b"", b"", pipes_held=True)
        return Ran(True, process.returncode, stdout, stderr)
    except BaseException:
        # An interruption of this script must not leave a paid session running.
        _kill_and_say(process)
        raise
    return Ran(False, process.returncode, stdout, stderr)


def _is_link(path: Path) -> bool:
    """A symbolic link or a junction: a name for something that lies elsewhere."""
    return path.is_symlink() or path.is_junction()


def _remove_what_can_be(directory: Path) -> None:
    """Best effort, and never through a link or a junction: what lies behind one is not ours."""
    if _is_link(directory):
        return
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                _remove_what_can_be(Path(entry.path))
                Path(entry.path).rmdir()
            else:
                Path(entry.path).unlink()


def _discard(directory: Path) -> bool:
    """Delete a directory this script created, and say whether it is really gone.

    A link or a junction is never followed and never removed: this script creates none, so
    one found under a name of its own was put there by something else. What lies behind it
    is not this script's to delete, and the directory it replaced — the key copy, perhaps —
    is somewhere else: that is reported as a failure, never as a removal.
    """
    if _is_link(directory):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    if directory.exists():
        # A locked entry must not shelter the rest: remove what can be, entry by entry.
        _remove_what_can_be(directory)
        with contextlib.suppress(OSError):
            directory.rmdir()
    return not directory.exists()


def _discard_despite_interruptions(directory: Path) -> tuple[bool, bool]:
    """An impatient second Ctrl+C must not stop the deletion of the key copy half-way.

    Returns whether the directory is gone, and whether an interruption was held back: the
    caller owes it to the operator once the deletion is over.
    """
    held_back = False
    for _ in range(INTERRUPTIONS_ABSORBED):
        try:
            return _discard(directory), held_back
        except KeyboardInterrupt:
            held_back = True
    return _discard(directory), held_back


def sweep_dead_runs(key_file: Path) -> None:
    """A run that died without unwinding left its isolated home behind, and the key in it."""
    leftovers = sorted(key_file.parent.glob(HOME_PREFIX + "*"))
    if not leftovers:
        return
    # Only a real directory can be a home this script made. Anything else under that name —
    # a link, a junction, a file — was put there by something else: it is named, not touched.
    foreign = [str(path) for path in leftovers if _is_link(path) or not path.is_dir()]
    if foreign:
        raise RehearsalRefusedError(
            "beside the key file, under the name of a rehearsal home, lies something this "
            f"script did not make and will not touch: {', '.join(foreign)}. Look at it, and "
            "remove it by hand"
        )
    stuck = [str(path) for path in leftovers if not _discard_despite_interruptions(path)[0]]
    outcome = f"It could NOT be deleted: {', '.join(stuck)}." if stuck else "It is deleted now."
    raise RehearsalRefusedError(
        f"a run that died left a copy of the key beside the key file ({len(leftovers)} found). "
        f"{outcome} Check that no agent session is still running, then run again"
    )


def _extract(git: str, source: Path, ref: str, into: Path) -> None:
    try:
        ran = _bounded(
            [git, "-C", str(source), "-c", "core.autocrlf=false", "archive", "--format=tar", ref],
            stdin=b"",
            environment=None,
            cwd=None,
            seconds=ARCHIVE_SECONDS,
        )
        if ran.overran or ran.returncode != 0:
            raise RehearsalRefusedError("the copy of the commit could not be made")
        with tarfile.open(fileobj=io.BytesIO(ran.stdout)) as tar:
            tar.extractall(into, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise RehearsalRefusedError("the copy of the commit could not be made") from error


def _login(codex: Sequence[str], environment: Mapping[str, str], key: bytes) -> None:
    try:
        ran = _bounded(
            [*codex, "login", "--with-api-key"],
            stdin=key + b"\n",
            environment=environment,
            cwd=None,
            seconds=HOUSEKEEPING_SECONDS,
        )
    except OSError as error:
        raise RehearsalRefusedError("the agent CLI could not be started for the login") from error
    if ran.overran or ran.returncode != 0:
        # Its output is not echoed: a CLI may repeat the key it was given.
        raise RehearsalRefusedError("the agent CLI refused the dedicated key")


def _version(codex: Sequence[str], environment: Mapping[str, str]) -> str:
    try:
        ran = _bounded(
            [*codex, "--version"],
            stdin=b"",
            environment=environment,
            cwd=None,
            seconds=HOUSEKEEPING_SECONDS,
        )
    except OSError:
        return "unknown"
    declared = ran.stdout.decode("utf-8", errors="replace").strip()
    return declared if not ran.overran and VERSION_SHAPE.fullmatch(declared) else "unknown"


def _append(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _spread(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {}
    return {"min": min(values), "median": round(statistics.median(values)), "max": max(values)}


def read_records(out: Path) -> list[dict[str, Any]]:
    """The session records of an output directory, or a refusal if they are not this script's.

    Called before anything starts too: records that cannot be read must not be discovered
    when the report is written, after the sessions have been paid for.
    """
    path = out / RECORDS
    if not path.is_file():
        return []
    expected = {field.name for field in fields(SessionRecord)}
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != expected:
                raise ValueError("not a session record")
            records.append(record)
    except (OSError, ValueError) as error:
        raise RehearsalRefusedError(
            "the output directory holds records this script cannot read: choose another one"
        ) from error
    return records


def write_report(
    out: Path,
    key_file: Path,
    codex_version: str,
    model: str | None,
    key_copy_removed: bool,
    stopped_early: bool,
) -> dict[str, Any]:
    """Everything recorded in ``out``, this run's sessions and any earlier run's."""
    records = read_records(out)
    # A session that did not complete says nothing about what a session costs.
    completed = [record for record in records if record["status"] == SessionStatus.COMPLETED]
    report: dict[str, Any] = {
        "purpose": "rehearsal outside the protocol: durations and token usage, never an outcome",
        "approved_max_sessions": APPROVED_MAX_SESSIONS,
        "approved_session_seconds": APPROVED_SESSION_SECONDS,
        "sessions_launched": launched_so_far(key_file),
        "stopped_early": stopped_early,
        "key_copy_removed": key_copy_removed,
        "agent_cli": codex_version,
        "model_declared": model,
        "records": records,
        "statuses": dict(Counter(record["status"] for record in records)),
        "ranges_cover": "COMPLETED sessions only",
        "duration_ms": _spread([record["duration_ms"] for record in completed]),
        "tokens": {name: _spread([record[name] for record in completed]) for name in USAGE_FIELDS},
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
    expected_version: str | None = None,
    max_sessions: int = APPROVED_MAX_SESSIONS,
    session_seconds: int = APPROVED_SESSION_SECONDS,
) -> dict[str, Any]:
    """Run what is left of the allowance. Refuses, before anything starts, what it cannot bound."""
    if not 1 <= max_sessions <= APPROVED_MAX_SESSIONS:
        raise RehearsalRefusedError("the session count may be lowered, never raised")
    if not 1 <= session_seconds <= APPROVED_SESSION_SECONDS:
        raise RehearsalRefusedError("the session length may be lowered, never raised")
    if model is not None and (
        not MODEL_SHAPE.fullmatch(model) or model.encode().startswith(KEY_PREFIX)
    ):
        # The model name is an argument and a report field: a key pasted there would be both.
        raise RehearsalRefusedError("the model name is not a plain identifier")
    key = read_key(key_file)
    resolved, git = resolve_command(codex[0]), resolve_command("git")
    if resolved is None:
        raise RehearsalRefusedError("the agent CLI is not an absolute path and is not on PATH")
    if git is None:
        raise RehearsalRefusedError("git is not on PATH")
    if Path(resolved).suffix.lower() in BATCH_SUFFIXES and any(
        BATCH_UNSAFE.search(argument) for argument in (tempfile.gettempdir(), *codex[1:])
    ):
        # The copy's path is an argument, and a batch shim lets the shell read it again.
        raise RehearsalRefusedError(
            "an argument, or the temporary directory's path, holds a character a batch shim "
            "would read as a command"
        )
    command = [resolved, *codex[1:]]
    # Records that cannot be read are found now, not once the sessions have been paid for.
    read_records(out)
    verify = [git, "-C", str(source), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]
    checked = _bounded(verify, stdin=b"", environment=None, cwd=None, seconds=HOUSEKEEPING_SECONDS)
    if checked.overran or checked.returncode != 0:
        # A mistyped commit must not cost a session of the allowance.
        raise RehearsalRefusedError("the source repository does not hold that commit")

    with exclusive(key_file):
        # Under the lock, so that a live run's home is never taken for a dead one's.
        sweep_dead_runs(key_file)
        allowance = max_sessions - launched_so_far(key_file)
        if allowance <= 0:
            raise RehearsalRefusedError("the approved session allowance is spent")
        out.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=key_file.parent))
        version, key_copy_removed, stopped = "unknown", False, False
        try:
            environment = child_environment(home, tool_path(resolved))
            # Before the key leaves: which CLI is about to receive it.
            version = _version(command, environment)
            if version == "unknown":
                raise RehearsalRefusedError(
                    "the agent CLI did not state a version: the key is not handed to it"
                )
            if expected_version is not None and expected_version not in version.split():
                raise RehearsalRefusedError(
                    "the agent CLI is not the expected version: the key is not handed to it"
                )
            print(f"agent CLI: {resolved} ({version})", file=sys.stderr)
            _login(command, environment, key)
            del key  # out of any later traceback; Python does not erase it from memory
            if not (home / CREDENTIALS).is_file():
                # The CLI took the key and did not leave it where this script deletes it.
                raise RehearsalRefusedError(
                    "the agent CLI accepted the key and did not store it in the isolated home: "
                    "find where it went, delete it there, and revoke the key"
                )
            session = [*command, "exec", "--json", "--color", "never"]
            session += ["--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral"]
            if model is not None:
                session += ["--model", model]
            for task in list(tasks)[:allowance]:
                workdir = Path(tempfile.mkdtemp(prefix=COPY_PREFIX))
                status, duration = SessionStatus.FAILED_TO_START, 0
                ran = Ran(False, None, b"", b"")
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
                        ran = _bounded(
                            [*session, "--cd", str(workdir), "-"],
                            stdin=task.prompt.encode("utf-8"),
                            environment=environment,
                            cwd=workdir,
                            seconds=session_seconds,
                        )
                        status = SessionStatus.TIMED_OUT if ran.overran else SessionStatus.COMPLETED
                    duration = round((time.monotonic() - started) * 1000)
                finally:
                    copy_removed = _discard(workdir)
                    if not copy_removed:
                        print(
                            f"WARNING: a session copy could not be removed: {workdir}",
                            file=sys.stderr,
                        )
                usage, turns, failed = read_usage(
                    ran.stdout.decode("utf-8", errors="replace").splitlines()
                )
                if status is SessionStatus.COMPLETED and (
                    failed or turns == 0 or ran.returncode != 0
                ):
                    status = SessionStatus.TURN_FAILED
                if status is SessionStatus.COMPLETED:
                    diagnostic = "NONE"
                else:
                    diagnostic = PIPES_HELD if ran.pipes_held else diagnose(ran.stderr)
                record = SessionRecord(
                    session_index=index,
                    task_id=task.task_id,
                    started_at=started_at,
                    duration_ms=duration,
                    status=status.value,
                    turns=turns,
                    diagnostic=diagnostic,
                    copy_removed=copy_removed,
                    **usage,
                )
                _append(out / RECORDS, asdict(record))
                failure = status in (SessionStatus.FAILED_TO_START, SessionStatus.TURN_FAILED)
                if failure or ran.pipes_held:
                    # A CLI that rejects a flag or the key fails every session the same way:
                    # the rest of the allowance is left for a run that can use it. And no
                    # session is started while something of the last one may still be alive.
                    stopped = True
                    break
        finally:
            # On every path this script still runs, an interruption included — and a second
            # one while the key copy is being deleted.
            held_back = False
            try:
                key_copy_removed, held_back = _discard_despite_interruptions(home)
            finally:
                if not key_copy_removed:
                    print(
                        f"WARNING: a copy of the dedicated key may remain under {home} — or, if "
                        "a link has taken that name, wherever the directory was moved. Delete "
                        "it, revoke the key, and do not read what else it holds: the CLI may "
                        "have written the agent's words there.",
                        file=sys.stderr,
                    )
            if held_back:
                # The operator asked to stop while the key copy was being deleted: now it is.
                raise KeyboardInterrupt
    return write_report(out, key_file, version, model, key_copy_removed, stopped)


def _unwind(_signum: int, _frame: object) -> None:
    """A termination request becomes an unwinding: the session killed, the key copy deleted."""
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="repository to copy, read only")
    parser.add_argument("--ref", required=True, help="commit the throwaway copies are taken from")
    parser.add_argument("--tasks", type=Path, required=True, help="JSON list of task_id and prompt")
    parser.add_argument("--out", type=Path, required=True, help="records and report directory")
    parser.add_argument("--codex-command", nargs="+", default=["codex"])
    parser.add_argument("--cli-version", help="refuse an agent CLI that states another version")
    parser.add_argument("--model")
    parser.add_argument("--max-sessions", type=int, default=APPROVED_MAX_SESSIONS)
    parser.add_argument("--session-seconds", type=int, default=APPROVED_SESSION_SECONDS)
    arguments = parser.parse_args(argv)
    # Ctrl+C already unwinds. A termination signal and Ctrl+Break would not.
    names = ("SIGTERM", "SIGBREAK") if threading.current_thread() is threading.main_thread() else ()
    numbers = [getattr(signal, name) for name in names if hasattr(signal, name)]
    previous = {number: signal.signal(number, _unwind) for number in numbers}
    try:
        report = run_rehearsal(
            key_file=arguments.key_file,
            source=arguments.source,
            ref=arguments.ref,
            tasks=load_tasks(arguments.tasks),
            out=arguments.out,
            codex=arguments.codex_command,
            model=arguments.model,
            expected_version=arguments.cli_version,
            max_sessions=arguments.max_sessions,
            session_seconds=arguments.session_seconds,
        )
    except RehearsalRefusedError as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print(
            "interrupted: the session was killed; what was launched stays counted", file=sys.stderr
        )
        return EXIT_INTERRUPTED
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    statuses = [record["status"] for record in report["records"]]
    print(f"{report['sessions_launched']} of {APPROVED_MAX_SESSIONS} sessions launched: {statuses}")
    if report["stopped_early"]:
        print(
            "stopped early — a session failed, or something outlived its kill: "
            "what is left of the allowance is untouched"
        )
    return EXIT_OK if report["key_copy_removed"] else EXIT_KEY_COPY_LEFT


if __name__ == "__main__":
    raise SystemExit(main())

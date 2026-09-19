"""HOK-804 — the rehearsal launcher bounds itself, isolates the key and keeps no text.

A stub stands in for the agent CLI: a real process, started the way the launcher starts the
real one, which logs what it was given and can misbehave on request — overrun, leave a
child or an orphan behind, fail, write on stderr, lie about its version, keep the key
elsewhere. No real agent session runs here and no key exists. These tests prove nothing
about the experiment, which has not run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "examples" / "lab_rehearsal_launcher.py"
KEY = "sk-test-DEDICATED-KEY-0123456789"
ANSWER = "SECRET-ANSWER"
GIT_IDENTITY = ["-c", "user.name=rehearsal-test", "-c", "user.email=rehearsal-test@example.invalid"]
REDIRECTED = ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA")

STUB = f'''
import json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).parent
args = sys.argv[1:]
entry = {{
    "argv": args,
    "env": sorted(os.environ),
    "key_in_env": any("{KEY}" in value for value in os.environ.values()),
    "codex_home": os.environ.get("CODEX_HOME"),
    "redirected": {{name: os.environ.get(name) for name in {REDIRECTED!r}}},
    "redirected_exist": all(Path(os.environ.get(name, "?")).is_dir() for name in {REDIRECTED!r}),
    "git_global_config": os.environ.get("GIT_CONFIG_GLOBAL"),
    "git_system_config_off": os.environ.get("GIT_CONFIG_NOSYSTEM") == "1",
    "path": os.environ.get("PATH", ""),
    "cwd_has_marker": Path(os.getcwd(), "marker.txt").is_file(),
}}


def log():
    with (HERE / "stub-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if args[:1] == ["--version"]:
    log()
    declared = HERE / "version.txt"
    print(declared.read_text(encoding="utf-8") if declared.is_file() else "codex-cli 0.0.0-stub")
    sys.exit(0)
if args[:2] == ["login", "--with-api-key"]:
    key = sys.stdin.read()
    entry["key_on_stdin"] = key.strip() == "{KEY}"
    settings = Path(os.environ["CODEX_HOME"], "config.toml")
    entry["store_pinned_to_a_file"] = (
        settings.is_file() and 'cli_auth_credentials_store = "file"' in settings.read_text()
    )
    if "ELSEWHERE" not in key:
        Path(os.environ["CODEX_HOME"], "auth.json").write_text(key, encoding="utf-8")
    log()
    sys.exit(1 if "REFUSED" in key else 0)
if args[:1] == ["exec"]:
    prompt = sys.stdin.read()
    entry["prompt"] = prompt
    log()
    print(json.dumps({{"type": "thread.started", "thread_id": "stub"}}), flush=True)
    # Every session says something, on both streams: a launcher that kept either would show.
    message = {{"type": "agent_message", "text": "{ANSWER} the agent gave"}}
    print(json.dumps({{"type": "item.completed", "item": message}}), flush=True)
    print("{ANSWER} echoed on the error stream", file=sys.stderr, flush=True)
    if "CHILD" in prompt:
        # A grandchild that shares the pipes: killing the stub alone would leave it running.
        subprocess.Popen([sys.executable, str(HERE / "beat.py")], stdout=sys.stdout, cwd=HERE)
        time.sleep(30)
    if "ORPHAN" in prompt:
        # An intermediate that exits at once: its child has no living parent to be found by.
        subprocess.run([sys.executable, str(HERE / "spawn.py")], stdout=sys.stdout, cwd=HERE)
        time.sleep(30)
    if "SLEEP" in prompt:
        time.sleep(30)
    if "NAP" in prompt:
        time.sleep(2)
    if "FAIL" in prompt:
        print(json.dumps({{"type": "turn.failed", "error": {{"message": "boom"}}}}), flush=True)
        print("401 Unauthorized for {KEY} - {ANSWER} was the last message", file=sys.stderr)
        sys.exit(1)
    usage = {{
        "input_tokens": 3000 if "BIG" in prompt else 1000,
        "cached_input_tokens": 400,
        "output_tokens": 50,
        "reasoning_output_tokens": 7,
    }}
    print(json.dumps({{"type": "turn.completed", "usage": usage}}), flush=True)
    sys.exit(7 if "EXIT-CODE" in prompt else 0)
sys.exit(9)
'''

# Lives for 25 s and leaves a mark every tenth of a second while it does.
BEAT = """
import time
from pathlib import Path

mark = Path(__file__).with_name("heartbeat.txt")
end = time.time() + 25
while time.time() < end:
    with mark.open("a") as handle:
        handle.write("x")
    time.sleep(0.1)
"""

# Starts the beat outside its own process group and exits: the beat is an orphan.
SPAWN = """
import subprocess, sys
from pathlib import Path

here = Path(__file__).parent
subprocess.Popen(
    [sys.executable, str(here / "beat.py")], stdout=sys.stdout, cwd=here, start_new_session=True
)
"""


def _launcher_module() -> ModuleType:
    name = "latent_compass_lab_rehearsal_launcher"
    spec = importlib.util.spec_from_file_location(name, LAUNCHER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


launcher = _launcher_module()

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the copies come from git")


def _git(repository: Path, *arguments: str) -> bytes:
    binary = shutil.which("git")
    assert binary is not None
    return subprocess.run(  # noqa: S603 - resolved binary, fixed arguments
        [binary, "-C", str(repository), *GIT_IDENTITY, *arguments],
        check=True,
        capture_output=True,
    ).stdout


class Rig:
    """A throwaway source repository, a stub agent CLI, a key file and an output directory."""

    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "marker.txt").write_text("committed\n", encoding="utf-8")
        for arguments in (["init", "--quiet"], ["add", "."], ["commit", "--quiet", "-m", "one"]):
            _git(self.source, *arguments)
        self.stub = root / "stub" / "fake_codex.py"
        self.stub.parent.mkdir()
        self.stub.write_text(STUB, encoding="utf-8")
        self.stub.with_name("beat.py").write_text(BEAT, encoding="utf-8")
        self.stub.with_name("spawn.py").write_text(SPAWN, encoding="utf-8")
        self.vault = root / "vault"
        self.vault.mkdir()
        self.key_file = self.vault / "dedicated.key"
        self.key_file.write_text(KEY + "\n", encoding="utf-8")
        self.out = root / "out"

    def run(self, prompts: list[str], **overrides: Any) -> dict[str, Any]:
        tasks = [
            launcher.Task(task_id=f"throwaway-{index}", prompt=prompt)
            for index, prompt in enumerate(prompts, start=1)
        ]
        arguments: dict[str, Any] = {
            "key_file": self.key_file,
            "source": self.source,
            "ref": "HEAD",
            "tasks": tasks,
            "out": self.out,
            "codex": [sys.executable, str(self.stub)],
        }
        arguments.update(overrides)
        report: dict[str, Any] = launcher.run_rehearsal(**arguments)
        return report

    def command_line(
        self, prompts: Sequence[str] = ("look",), out: Path | None = None
    ) -> list[str]:
        tasks = self.source.parent / "tasks.json"
        listed = [
            {"task_id": f"throwaway-{index}", "prompt": prompt}
            for index, prompt in enumerate(prompts, start=1)
        ]
        tasks.write_text(json.dumps(listed), encoding="utf-8")
        arguments = ["--key-file", str(self.key_file), "--source", str(self.source)]
        arguments += ["--ref", "HEAD", "--tasks", str(tasks), "--out", str(out or self.out)]
        return [*arguments, "--codex-command", sys.executable, str(self.stub)]

    def calls(self, kind: str | None = None) -> list[dict[str, Any]]:
        log = self.stub.with_name("stub-log.jsonl")
        if not log.is_file():
            return []
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [call for call in calls if kind is None or call["argv"][:1] == [kind]]

    def launched(self) -> int:
        count: int = launcher.launched_so_far(self.key_file)
        return count

    def isolated_home(self) -> Path:
        return Path(self.calls()[0]["codex_home"])

    def key_copies(self) -> list[Path]:
        return sorted(self.vault.glob("lc-rehearsal-home-*"))

    def copy_of(self, session: dict[str, Any]) -> Path:
        return Path(session["argv"][session["argv"].index("--cd") + 1])

    def everything_written(self) -> str:
        """Every byte the launcher left behind: its records, and what lies beside the key."""
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for folder in (self.out, self.vault)
            for path in folder.rglob("*")
            if path.is_file() and path != self.key_file
        )

    def heartbeats(self) -> int:
        mark = self.stub.with_name("heartbeat.txt")
        return len(mark.read_text(encoding="utf-8")) if mark.is_file() else 0

    def heart_has_stopped(self) -> bool:
        """The grandchild left a mark every tenth of a second while it lived."""
        time.sleep(0.5)
        first = self.heartbeats()
        assert first > 0, "the grandchild never lived: the test proves nothing"
        time.sleep(0.7)
        return self.heartbeats() == first


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_without_the_dedicated_key_file_nothing_is_started(rig: Rig) -> None:
    rig.key_file.unlink()
    with pytest.raises(launcher.RehearsalRefusedError, match="does not exist"):
        rig.run(["look around"])
    assert rig.calls() == []
    assert not rig.out.exists()


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("\n", "empty"),
        (f"{KEY}\n{KEY}\n", "one token"),
        ('{"OPENAI_API_KEY":null,"tokens":{"id_token":"x"}}\n', "does not look like an API key"),
        ("sk-" + "a" * 600 + "\n", "does not look like an API key"),
    ],
    ids=["empty", "two-tokens", "a-credential-store", "too-long"],
)
def test_a_key_file_that_is_not_exactly_one_key_starts_nothing(
    rig: Rig, content: str, reason: str
) -> None:
    rig.key_file.write_text(content, encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match=reason):
        rig.run(["look around"])
    assert rig.calls() == []


def test_a_key_file_saved_by_a_text_editor_is_read_as_the_key(rig: Rig) -> None:
    rig.key_file.write_bytes(b"\xef\xbb\xbf" + KEY.encode() + b"\r\n")
    try:
        rig.run(["look around"])
    except launcher.RehearsalRefusedError:
        pytest.fail("a key file saved by a text editor was refused instead of being read")
    assert rig.calls("login")[0]["key_on_stdin"] is True


@pytest.mark.parametrize(
    "override",
    [{"max_sessions": 7}, {"max_sessions": 0}, {"session_seconds": 901}, {"session_seconds": 0}],
)
def test_the_approved_ceilings_can_be_lowered_and_never_raised(
    rig: Rig, override: dict[str, int]
) -> None:
    assert launcher.APPROVED_MAX_SESSIONS == 6
    assert launcher.APPROVED_SESSION_SECONDS == 900
    with pytest.raises(launcher.RehearsalRefusedError, match="never raised"):
        rig.run(["look around"], **override)
    assert rig.calls() == []


def test_the_allowance_of_six_sessions_belongs_to_the_key_not_to_the_output_directory(
    rig: Rig,
) -> None:
    first = rig.run(["one", "two", "three"])
    assert first["sessions_launched"] == 3
    # The same output directory again: its records add up, the report covers them all.
    second = rig.run(["four"])
    assert second["sessions_launched"] == 4
    assert [record["session_index"] for record in second["records"]] == [1, 2, 3, 4]
    # Neither another output directory nor a copy of the key file beside it starts over.
    beside = rig.key_file.with_name("the-same-key-again.key")
    shutil.copyfile(rig.key_file, beside)
    third = rig.run(
        ["five", "six", "seven", "eight"], out=rig.out.with_name("another-out"), key_file=beside
    )
    assert third["sessions_launched"] == 6
    assert [record["task_id"] for record in third["records"]] == ["throwaway-1", "throwaway-2"]
    with pytest.raises(launcher.RehearsalRefusedError, match="allowance is spent"):
        rig.run(["nine"], out=rig.out.with_name("a-third-out"))
    assert len(rig.calls("exec")) == 6


def test_a_second_rehearsal_at_the_same_time_is_refused(rig: Rig) -> None:
    lock = launcher.lock_path(rig.key_file)
    lock.write_text("", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="lock lies beside the key"):
        rig.run(["look around"])
    assert rig.calls() == []
    # The lock belongs to the other run: a refusal must not take it away.
    assert lock.exists()
    lock.unlink()
    rig.run(["look around"])
    assert not lock.exists()


def test_a_run_holds_the_lock_while_it_lasts_and_a_refused_run_leaves_its_key_copy_alone(
    rig: Rig,
) -> None:
    outcome: dict[str, Any] = {}

    def first_run() -> None:
        outcome["report"] = rig.run(["please take a NAP"])

    thread = threading.Thread(target=first_run)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not rig.calls("exec") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rig.calls("exec"), "the first run never started its session"
        with pytest.raises(launcher.RehearsalRefusedError, match="lock lies beside the key"):
            rig.run(["look around"], out=rig.out.with_name("another-out"))
        # The running run's isolated home is not a dead run's leftover: it was not swept.
        assert rig.isolated_home().exists()
        # The lock tells the operator which process to look for before removing it.
        held = launcher.lock_path(rig.key_file).read_text(encoding="utf-8")
        assert f"pid {os.getpid()} " in held
    finally:
        thread.join(timeout=60)
    assert outcome["report"]["records"][0]["status"] == "COMPLETED"
    assert len(rig.calls("exec")) == 1
    assert not launcher.lock_path(rig.key_file).exists()


def test_the_report_is_written_while_the_lock_is_still_held(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = []
    write_report = launcher.write_report

    def watched(*arguments: Any, **keywords: Any) -> dict[str, Any]:
        held.append(launcher.lock_path(rig.key_file).exists())
        report: dict[str, Any] = write_report(*arguments, **keywords)
        return report

    monkeypatch.setattr(launcher, "write_report", watched)
    rig.run(["look around"])
    # The count it reports is this run's, not that of a run that took the lock meanwhile.
    assert held == [True]
    assert not launcher.lock_path(rig.key_file).exists()


def _link(name: Path, target: Path) -> None:
    """A junction on Windows, which needs no privilege; a symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(name))
    else:
        name.symlink_to(target, target_is_directory=True)


def test_a_link_under_the_name_of_a_home_is_named_and_never_followed(
    rig: Rig, tmp_path: Path
) -> None:
    # What an agent that can write beside the key could plant there: the name of a rehearsal
    # home, leading to the owner's own agent home.
    owners = tmp_path / "the-owners-own-agent-home"
    (owners / "sessions").mkdir(parents=True)
    login = owners / "auth.json"
    login.write_text("the owner's own login", encoding="utf-8")
    planted = rig.vault / "lc-rehearsal-home-planted"
    _link(planted, owners)
    with pytest.raises(launcher.RehearsalRefusedError, match="did not make and will not touch"):
        rig.run(["look around"])
    assert login.is_file(), "the sweep went through the link and deleted the owner's login"
    assert (owners / "sessions").is_dir()
    assert planted.exists()
    assert rig.calls() == []
    # The removal itself refuses a link it is handed, and reports a failure, not a removal.
    assert vars(launcher)["_discard"](planted) is False
    assert login.is_file()
    # A stray file under that name is not this script's either.
    planted.unlink() if sys.platform != "win32" else planted.rmdir()
    stray = rig.vault / "lc-rehearsal-home-a-file"
    stray.write_text("not a home", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="did not make and will not touch"):
        rig.run(["look around"])
    assert stray.read_text(encoding="utf-8") == "not a home"


def test_the_link_policy_holds_for_what_is_written_and_for_a_name_taken_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = tmp_path / "the-owners-own-agent-home"
    owners.mkdir()
    settings = owners / "config.toml"
    settings.write_text("the owner's own settings", encoding="utf-8")
    # Writing: a home that is a link, or no directory at all, gets nothing written into it.
    taken = tmp_path / "lc-rehearsal-home-taken"
    _link(taken, owners)
    for not_a_home in (taken, tmp_path / "never-made"):
        with pytest.raises(launcher.RehearsalRefusedError, match="not the directory this script"):
            launcher.child_environment(not_a_home, "anywhere")
    assert settings.read_text(encoding="utf-8") == "the owner's own settings"
    assert sorted(path.name for path in owners.iterdir()) == ["config.toml"]
    # Deleting: a link that takes the directory's name while it is being removed is neither
    # removed in its stead nor reported as the directory's removal.
    discard = vars(launcher)["_discard"]
    home = tmp_path / "lc-rehearsal-home-swapped"
    home.mkdir()
    (home / "auth.json").write_text(KEY, encoding="utf-8")

    def moved_aside_and_replaced(directory: Path, **_keywords: Any) -> None:
        Path(directory).rename(tmp_path / "moved-aside")
        _link(Path(directory), owners)

    monkeypatch.setattr(shutil, "rmtree", moved_aside_and_replaced)
    assert discard(home) is False, "a link that took the name passed for the removal"
    assert home.exists(), "the link was removed in the directory's stead"
    assert settings.is_file()
    # A link that leads nowhere does not "exist", and is still not the directory's removal.
    dangling = tmp_path / "lc-rehearsal-home-dangling"
    dangling.mkdir()
    nowhere = tmp_path / "nowhere"

    def replaced_by_a_link_to_nowhere(directory: Path, **_keywords: Any) -> None:
        Path(directory).rmdir()
        nowhere.mkdir()
        _link(Path(directory), nowhere)
        nowhere.rmdir()

    monkeypatch.setattr(shutil, "rmtree", replaced_by_a_link_to_nowhere)
    assert discard(dangling) is False, "a link to nowhere passed for the removal"
    # Left as it was found; removed here only so that the test's own directory can be.
    dangling.rmdir() if sys.platform == "win32" else dangling.unlink()


def test_a_link_inside_a_directory_survives_the_real_first_pass_untouched(tmp_path: Path) -> None:
    # No stand-in here: the first pass is the interpreter's own removal, on every normal run.
    outside = tmp_path / "the-owners-own-agent-home"
    outside.mkdir()
    precious = outside / "auth.json"
    precious.write_text("the owner's own login", encoding="utf-8")
    copy = tmp_path / "copy"
    (copy / "deep").mkdir(parents=True)
    _link(copy / "deep" / "way-out", outside)
    assert vars(launcher)["_discard"](copy) is True
    assert not copy.exists()
    assert precious.is_file(), "the removal went through the link and deleted what lay behind it"


def test_a_key_copy_left_by_a_run_that_died_is_deleted_and_said_before_anything_starts(
    rig: Rig,
) -> None:
    dead = rig.vault / "lc-rehearsal-home-left-by-a-dead-run"
    dead.mkdir()
    (dead / "auth.json").write_text(KEY, encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="a run that died left a copy") as said:
        rig.run(["look around"])
    assert "It is deleted now" in str(said.value)
    assert KEY not in str(said.value)
    assert not dead.exists()
    assert rig.calls() == []
    assert not launcher.lock_path(rig.key_file).exists()
    # Said once: the next run goes through.
    rig.run(["look around"])
    assert len(rig.calls("exec")) == 1


def test_a_session_is_counted_before_it_starts_and_a_crash_does_not_give_it_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = vars(launcher)["_bounded"]

    def crash_on_a_session(command: list[str], **keywords: Any) -> Any:
        if "exec" in command:
            raise RuntimeError("the host fell over mid-session")
        return real(command, **keywords)

    monkeypatch.setattr(launcher, "_bounded", crash_on_a_session)
    with pytest.raises(RuntimeError, match="fell over"):
        rig.run(["one"], max_sessions=1)
    assert rig.launched() == 1
    assert not rig.isolated_home().exists()
    assert not launcher.lock_path(rig.key_file).exists()
    monkeypatch.undo()
    with pytest.raises(launcher.RehearsalRefusedError, match="allowance is spent"):
        rig.run(["two"], max_sessions=1)


def test_a_copy_that_cannot_be_made_costs_no_session(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_arguments: Any) -> None:
        raise launcher.RehearsalRefusedError("the copy of the commit could not be made")

    monkeypatch.setattr(launcher, "_extract", refuse)
    with pytest.raises(launcher.RehearsalRefusedError, match="could not be made"):
        rig.run(["one"])
    assert rig.launched() == 0
    assert rig.calls("exec") == []
    assert not rig.isolated_home().exists()


def test_a_mistyped_commit_is_refused_before_it_can_cost_a_session(rig: Rig) -> None:
    with pytest.raises(launcher.RehearsalRefusedError, match="does not hold that commit"):
        rig.run(["one"], ref="no-such-commit")
    assert rig.calls() == []
    assert rig.launched() == 0


def test_a_run_stops_at_the_first_failed_session_and_leaves_the_rest_of_the_allowance(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    report = rig.run(["please FAIL", "look around", "look again"])
    assert [record["status"] for record in report["records"]] == ["TURN_FAILED"]
    assert report["stopped_early"] is True
    assert len(rig.calls("exec")) == 1
    assert rig.launched() == 1
    # A session whose process exits with an error is a failed one, whatever its events said.
    second = rig.run(["finish with an EXIT-CODE", "look around"], out=rig.out.with_name("more"))
    assert [record["status"] for record in second["records"]] == ["TURN_FAILED"]
    assert rig.launched() == 2
    # A run that did not fail says so too.
    assert rig.run(["look around"], out=rig.out.with_name("fine"))["stopped_early"] is False
    # The command line says it to the operator.
    capsys.readouterr()
    arguments = rig.command_line(["please FAIL", "look around"], out=rig.out.with_name("cli"))
    # ...and to a calling script, with an exit code of its own.
    assert launcher.main(arguments) == launcher.EXIT_STOPPED_EARLY
    assert "stopped early" in capsys.readouterr().out
    assert rig.launched() == 4


def test_the_report_holds_durations_and_usage_and_no_text_from_the_agent(rig: Rig) -> None:
    report = rig.run(["look around", "a BIG look", "look again", "please FAIL"])
    *completed, failed = report["records"]
    done = completed[0]
    assert [record["status"] for record in report["records"]] == [*["COMPLETED"] * 3, "TURN_FAILED"]
    assert report["statuses"] == {"COMPLETED": 3, "TURN_FAILED": 1}
    # The ranges cover the completed sessions only: a failed one used nothing and says
    # nothing about what a session costs.
    assert report["tokens"]["input_tokens"] == {"min": 1000, "median": 1000, "max": 3000}
    assert report["tokens"]["reasoning_output_tokens"] == {"min": 7, "median": 7, "max": 7}
    assert set(report["duration_ms"]) == {"min", "median", "max"}
    assert all(record["duration_ms"] >= 0 for record in report["records"])
    assert (done["turns"], failed["turns"], failed["input_tokens"]) == (1, 0, 0)
    # The one string of the CLI's that is kept, and only because it looks like a version.
    assert report["agent_cli"] == "codex-cli 0.0.0-stub"
    # The failing CLI put the agent's last message and the key on stderr: only a label is kept.
    assert (done["diagnostic"], failed["diagnostic"]) == ("NONE", "AUTH_REJECTED")
    written = rig.everything_written()
    assert ANSWER not in written
    assert KEY not in written
    assert set(done) == {
        "session_index",
        "task_id",
        "started_at",
        "duration_ms",
        "status",
        "turns",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "diagnostic",
        "copy_removed",
    }


def test_a_diagnostic_is_a_label_from_a_fixed_vocabulary_never_text() -> None:
    labels = {"NONE", "WITHHELD", *(label for label, _ in launcher.DIAGNOSES)}
    samples = [
        b"",
        b"  \n",
        f"401 Unauthorized for {KEY}".encode(),
        b"insufficient_quota: the project reached its spend limit",
        b"429 Too Many Requests",
        b"connection reset by peer",
        f"{ANSWER} and nothing the vocabulary knows".encode(),
        b"\xff\xfe not even text",
    ]
    assert [launcher.diagnose(sample) for sample in samples] == [
        "NONE",
        "NONE",
        "AUTH_REJECTED",
        "SPEND_LIMIT",
        "RATE_LIMITED",
        "NETWORK",
        "WITHHELD",
        "WITHHELD",
    ]
    assert {launcher.diagnose(sample) for sample in samples} <= labels


def test_the_key_travels_on_stdin_only_and_its_copy_is_deleted(rig: Rig) -> None:
    report = rig.run(["look around"])
    calls = rig.calls()
    assert [call["key_on_stdin"] for call in rig.calls("login")] == [True]
    assert all(KEY not in json.dumps(call["argv"]) for call in calls)
    assert not any(call["key_in_env"] for call in calls)
    assert KEY not in rig.everything_written()
    # The key copy is made beside the key file, not in a shared temporary directory.
    assert rig.isolated_home().parent == rig.vault
    assert not rig.isolated_home().exists()
    assert rig.key_copies() == []
    assert report["key_copy_removed"] is True
    # The CLI was told to keep its credentials in a file of that home before it got the key.
    assert rig.calls("login")[0]["store_pinned_to_a_file"] is True


def test_the_cli_states_a_version_before_it_is_handed_the_key(rig: Rig) -> None:
    version = rig.stub.with_name("version.txt")
    # A CLI that answers with something else than a version — the key, say — gets nothing,
    # and what it answered is kept nowhere.
    version.write_text(KEY, encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="did not state a version"):
        rig.run(["look around"])
    assert [call["argv"][0] for call in rig.calls()] == ["--version"]
    assert KEY not in rig.everything_written()
    assert rig.key_copies() == []
    # A pinned version is the one that has to be stated.
    version.write_text("codex-cli 9.9.9", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="not the expected version"):
        rig.run(["look around"], expected_version="0.116.0")
    assert rig.calls("login") == []
    report = rig.run(["look around"], expected_version="9.9.9")
    assert report["agent_cli"] == "codex-cli 9.9.9"
    assert [call["argv"][0] for call in rig.calls()][-3:] == ["--version", "login", "exec"]


def test_a_cli_that_stores_the_key_outside_the_isolated_home_is_refused_before_any_session(
    rig: Rig,
) -> None:
    rig.key_file.write_text(KEY + "-ELSEWHERE\n", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="revoke the key") as refusal:
        rig.run(["look around"])
    assert KEY not in str(refusal.value)
    assert rig.calls("exec") == []
    assert rig.launched() == 0
    assert not rig.isolated_home().exists()


def test_a_key_copy_that_cannot_be_deleted_is_said_aloud_on_every_path(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = vars(launcher)["_discard"]

    def keep_the_home(directory: Path) -> bool:
        return False if directory.name.startswith("lc-rehearsal-home-") else bool(real(directory))

    def clear_what_the_run_could_not() -> None:
        for leftover in rig.key_copies():
            real(leftover)

    monkeypatch.setattr(launcher, "_discard", keep_the_home)
    report = rig.run(["look around"])
    assert report["key_copy_removed"] is False
    warning = capsys.readouterr().err
    assert "a copy of the dedicated key may remain" in warning
    assert "do not read what else it holds" in warning
    clear_what_the_run_could_not()
    # The command line says it with its exit code.
    assert launcher.main(rig.command_line()) == launcher.EXIT_KEY_COPY_LEFT
    assert "a copy of the dedicated key may remain" in capsys.readouterr().err
    clear_what_the_run_could_not()
    # And when the run dies instead of finishing: no report, the warning all the same.
    monkeypatch.setattr(launcher, "_version", lambda *_arguments: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        rig.run(["look again"])
    assert "a copy of the dedicated key may remain" in capsys.readouterr().err
    clear_what_the_run_could_not()


def test_a_second_interruption_does_not_stop_the_deletion_of_the_key_copy(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = vars(launcher)["_discard"]
    interrupted: list[Path] = []

    def impatient(directory: Path) -> bool:
        if directory.name.startswith("lc-rehearsal-home-") and not interrupted:
            interrupted.append(directory)
            raise KeyboardInterrupt
        return bool(real(directory))

    monkeypatch.setattr(launcher, "_discard", impatient)
    # The interruption is held back while the key copy is deleted, then given to the operator.
    with pytest.raises(KeyboardInterrupt):
        rig.run(["look around"])
    assert interrupted
    assert not interrupted[0].exists(), "the interruption stopped the deletion of the key copy"
    assert rig.key_copies() == []
    assert not launcher.lock_path(rig.key_file).exists()


def test_an_interruption_held_back_hides_neither_a_refusal_nor_what_the_sweep_found(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = vars(launcher)["_discard"]
    interrupted: list[Path] = []

    def impatient(directory: Path) -> bool:
        if directory.name.startswith("lc-rehearsal-home-") and directory not in interrupted:
            interrupted.append(directory)
            raise KeyboardInterrupt
        return bool(real(directory))

    monkeypatch.setattr(launcher, "_discard", impatient)
    # The most important thing this script can say — where did the key go — must not vanish
    # behind the interruption that arrives while the home is being deleted.
    rig.key_file.write_text(KEY + "-ELSEWHERE\n", encoding="utf-8")
    with pytest.raises(KeyboardInterrupt):
        rig.run(["look around"])
    assert "revoke the key" in capsys.readouterr().err
    assert rig.key_copies() == []
    # The same while a dead run's key copy is swept: what was found is said, then it stops.
    dead = rig.vault / "lc-rehearsal-home-left-by-a-dead-run"
    dead.mkdir()
    try:
        with pytest.raises(KeyboardInterrupt):
            rig.run(["look around"])
    except launcher.RehearsalRefusedError:
        pytest.fail("the interruption held back while the sweep deleted was never given back")
    assert "a run that died left a copy" in capsys.readouterr().err
    assert not dead.exists()
    assert not launcher.lock_path(rig.key_file).exists()


def test_a_directory_is_reported_removed_only_when_it_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discard = vars(launcher)["_discard"]
    gone = tmp_path / "gone"
    (gone / "deep").mkdir(parents=True)
    (gone / "deep" / "file.txt").write_text("x", encoding="utf-8")
    assert discard(gone) is True
    assert not gone.exists()
    assert discard(tmp_path / "never-there") is True
    # When the first pass fails, the launcher removes the tree itself, entry by entry...
    stuck = tmp_path / "stuck"
    (stuck / "deep").mkdir(parents=True)
    (stuck / "deep" / "auth.json").write_text("x", encoding="utf-8")
    monkeypatch.setattr(shutil, "rmtree", lambda *_arguments, **_keywords: None)
    assert discard(stuck) is True
    assert not stuck.exists()
    # ...and when nothing could be removed at all, that is not reported as a success.
    (stuck / "deep").mkdir(parents=True)
    monkeypatch.setattr(launcher, "_remove_what_can_be", lambda _directory: None)
    assert discard(stuck) is False


def test_a_removal_never_follows_a_link_out_of_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discard = vars(launcher)["_discard"]
    outside = tmp_path / "the-owners-own-agent-home"
    outside.mkdir()
    precious = outside / "auth.json"
    precious.write_text("the owner's own login", encoding="utf-8")
    copy = tmp_path / "copy"
    copy.mkdir()
    _link(copy / "way-out", outside)
    # The first pass fails, as it does on a locked entry: the launcher walks the tree itself.
    monkeypatch.setattr(shutil, "rmtree", lambda *_arguments, **_keywords: None)
    assert discard(copy) is True
    assert precious.is_file(), "the removal went through the link and deleted what lay behind it"
    assert precious.read_text(encoding="utf-8") == "the owner's own login"


@pytest.mark.skipif(sys.platform != "win32", reason="an open file blocks deletion on Windows only")
def test_a_locked_entry_does_not_shelter_the_key_copy(tmp_path: Path) -> None:
    discard = vars(launcher)["_discard"]
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text("the key copy", encoding="utf-8")
    with (home / "locked.log").open("w", encoding="utf-8"):
        assert discard(home) is False
        assert not (home / "auth.json").exists()
    assert discard(home) is True


def test_the_cli_is_never_resolved_from_the_current_directory(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "downloads"
    hostile.mkdir()
    for name in ("codex", "codex.cmd", "codex.exe"):
        planted = hostile / name
        planted.write_text("echo hostile\n", encoding="utf-8")
        planted.chmod(0o755)
    monkeypatch.chdir(hostile)
    monkeypatch.setenv("PATH", f"{rig.source}{os.pathsep}.{os.pathsep}")
    assert launcher.resolve_command("codex") is None
    assert launcher.resolve_command(str(Path("sub") / "codex")) is None
    with pytest.raises(launcher.RehearsalRefusedError, match="not on PATH"):
        rig.run(["look around"], codex=["codex"])
    assert rig.calls() == []
    # An absolute path is taken as given, and a bare name is found on an absolute PATH entry.
    assert launcher.resolve_command(sys.executable) == sys.executable
    monkeypatch.setenv("PATH", str(hostile))
    found = launcher.resolve_command("codex")
    assert found is not None
    assert Path(found).parent == hostile


def test_a_batch_shim_is_refused_when_the_shell_could_read_the_copy_path_as_a_command(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    shim.chmod(0o755)
    # An extra argument of the operator's is re-read by the shell just the same.
    with pytest.raises(launcher.RehearsalRefusedError, match="batch shim"):
        rig.run(["look around"], codex=[str(shim), "--profile", "a&b"])
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "a&b"))
    with pytest.raises(launcher.RehearsalRefusedError, match="batch shim"):
        rig.run(["look around"], codex=[str(shim)])
    assert rig.calls() == []
    assert rig.key_copies() == []
    if sys.platform != "win32":
        # An absolute path is taken as given only if it can be run at all.
        shim.chmod(0o644)
        assert launcher.resolve_command(str(shim)) is None


def test_a_batch_shim_of_git_is_held_to_the_same_rule_as_one_of_the_cli(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "git.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    resolve = launcher.resolve_command

    def git_behind_a_shim(name: str) -> str | None:
        return str(shim) if name == "git" else resolve(name)

    monkeypatch.setattr(launcher, "resolve_command", git_behind_a_shim)
    # The commit and the source are the operator's, and git is handed both as arguments.
    with pytest.raises(launcher.RehearsalRefusedError, match="batch shim of git"):
        rig.run(["look around"], ref="100%")
    with pytest.raises(launcher.RehearsalRefusedError, match="batch shim of git"):
        rig.run(["look around"], source=rig.source.with_name("a%b"))
    assert rig.calls() == []
    assert rig.key_copies() == []


def test_a_prompt_too_long_to_be_written_under_a_bound_is_refused_before_anything_starts(
    rig: Rig,
) -> None:
    # Writing a prompt to a CLI that never reads it has no timeout on every platform. The bound
    # is counted in bytes, as the pipe counts them.
    for prompt in (
        "x" * (launcher.MAX_PROMPT_BYTES + 1),
        "é" * (launcher.MAX_PROMPT_BYTES // 2 + 1),
    ):
        with pytest.raises(launcher.RehearsalRefusedError, match="prompt is longer"):
            rig.run([prompt])
    assert rig.calls() == []
    assert rig.launched() == 0
    assert rig.run(["x" * launcher.MAX_PROMPT_BYTES])["sessions_launched"] == 1


def test_a_lock_that_cannot_be_taken_is_a_refusal_and_is_left_where_it_lies(rig: Rig) -> None:
    lock = launcher.lock_path(rig.key_file)
    lock.mkdir()
    try:
        with pytest.raises(launcher.RehearsalRefusedError, match="lock"):
            rig.run(["look around"])
    except OSError as error:
        pytest.fail(f"a lock that cannot be taken surfaced as {type(error).__name__}")
    assert rig.calls() == []
    assert lock.is_dir()


def test_the_agent_gets_an_isolated_home_and_none_of_the_parent_environment(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_only = tmp_path / "parent-only-tools"
    parent_only.mkdir()
    separator = os.pathsep
    monkeypatch.setenv("PATH", os.environ["PATH"] + separator + str(parent_only))
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
    monkeypatch.setenv("CODEX_HOME", str(rig.source))
    rig.run(["look around"])
    calls = rig.calls()
    assert [call["argv"][0] for call in calls] == ["--version", "login", "exec"]
    for call in calls:
        home = call["codex_home"]
        assert "OPENAI_API_KEY" not in call["env"]
        assert home != str(rig.source)
        assert Path(home).parent == rig.vault
        # Homes and temporary directories, by value: all of them inside the isolated home,
        # and all of them there.
        assert all(str(value).startswith(home) for value in call["redirected"].values())
        assert call["redirected"]["HOME"] == call["redirected"]["USERPROFILE"] == home
        assert call["redirected_exist"] is True
        assert str(call["git_global_config"]).startswith(home)
        assert call["git_system_config_off"] is True
        assert str(parent_only) not in call["path"]


def test_each_session_works_on_its_own_copy_and_the_source_is_only_read(rig: Rig) -> None:
    before = _git(rig.source, "status", "--porcelain")
    report = rig.run(["look around"])
    session = rig.calls("exec")[0]
    assert session["cwd_has_marker"] is True
    workdir = rig.copy_of(session)
    assert workdir != rig.source
    assert not workdir.exists()
    assert report["records"][0]["copy_removed"] is True
    assert session["argv"][-1] == "-"
    assert session["prompt"] == "look around"
    # The session's own flags, as the launcher passes them and no flag of its own can change:
    # the sandbox policy is the only confinement claimed anywhere, and it is this string.
    argv = session["argv"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--color") + 1] == "never"
    assert {"--ephemeral", "--json", "--skip-git-repo-check"} <= set(argv)
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    after = _git(rig.source, "status", "--porcelain")
    assert before == after == b""


def test_a_session_copy_that_survives_is_said_and_recorded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = vars(launcher)["_discard"]
    kept: list[Path] = []

    def keep_the_copy(directory: Path) -> bool:
        if directory.name.startswith("lc-rehearsal-copy-"):
            kept.append(directory)
            return False
        return bool(real(directory))

    monkeypatch.setattr(launcher, "_discard", keep_the_copy)
    try:
        report = rig.run(["look around"])
        assert report["records"][0]["copy_removed"] is False
        assert "a session copy could not be removed" in capsys.readouterr().err
    finally:
        for directory in kept:
            real(directory)


def test_a_session_that_overruns_is_killed_with_its_children(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "DRAIN_SECONDS", 1)
    started = time.monotonic()
    report = rig.run(["please spawn a CHILD"], session_seconds=3)
    # The stub would sleep for 30 s and its grandchild live for 25: neither is waited for.
    assert time.monotonic() - started < 20
    record = report["records"][0]
    assert (record["status"], record["turns"], record["input_tokens"]) == ("TIMED_OUT", 0, 0)
    assert rig.heart_has_stopped()
    # A session that timed out says nothing about what a session costs.
    assert report["statuses"] == {"TIMED_OUT": 1}
    assert report["duration_ms"] == {}


@pytest.mark.skipif(sys.platform != "win32", reason="the batch shim is this host's process shape")
def test_an_overrun_behind_a_batch_shim_is_killed_with_its_children(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On the host the CLI is a batch shim: the shell, then the CLI, then its children.
    shim = rig.stub.with_name("codex.cmd")
    shim.write_text(f'@echo off\r\n"{sys.executable}" "{rig.stub}" %*\r\n', encoding="utf-8")
    monkeypatch.setattr(launcher, "DRAIN_SECONDS", 1)
    report = rig.run(["please spawn a CHILD"], session_seconds=4, codex=[str(shim)])
    assert report["records"][0]["status"] == "TIMED_OUT"
    assert rig.calls("login")[0]["key_on_stdin"] is True
    assert rig.heart_has_stopped()


def test_what_outlives_the_kill_is_not_awaited_and_not_passed_over_in_silence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "DRAIN_SECONDS", 1)
    started = time.monotonic()
    report = rig.run(["please leave an ORPHAN", "look around"], session_seconds=3)
    # The orphan lives for 25 s and keeps the pipes open: the run must not wait for it.
    assert time.monotonic() - started < 15
    assert len(report["records"]) == 1, "a session was started beside what outlived the kill"
    record = report["records"][0]
    assert (record["status"], record["diagnostic"]) == ("TIMED_OUT", "PIPES_HELD_AFTER_KILL")
    # Pipes still held once the tree is dead prove that the kill missed something: that is
    # said, recorded, and no other session is started while it may still be alive.
    assert "outlived the kill and still holds its pipes" in capsys.readouterr().err
    assert report["stopped_early"] is True
    assert len(rig.calls("exec")) == 1


def test_a_kill_that_does_not_take_is_said_aloud(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_kill, real_discard = vars(launcher)["_kill_tree"], vars(launcher)["_discard"]
    spared: list[subprocess.Popen[bytes]] = []

    def spare(process: subprocess.Popen[bytes]) -> bool:
        spared.append(process)
        return False

    monkeypatch.setattr(launcher, "_kill_tree", spare)
    monkeypatch.setattr(launcher, "DRAIN_SECONDS", 1)
    monkeypatch.setattr(launcher, "KILL_SECONDS", 1)
    try:
        report = rig.run(["please SLEEP"], session_seconds=1)
        assert report["records"][0]["status"] == "TIMED_OUT"
        said = capsys.readouterr().err
        assert f"process {spared[0].pid} or one of its children may still be running" in said
    finally:
        for process in spared:
            real_kill(process)
            process.wait(timeout=20)
        for session in rig.calls("exec"):
            real_discard(rig.copy_of(session))


class _Interrupted(subprocess.Popen[bytes]):
    """A Popen whose wait on a running agent session is interrupted, as by Ctrl+C."""

    sessions: ClassVar[list[subprocess.Popen[bytes]]] = []

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        is_session = isinstance(self.args, list) and "exec" in self.args
        if is_session and self not in _Interrupted.sessions:
            _Interrupted.sessions.append(self)
            # The session gets its prompt and starts working, its child with it...
            assert self.stdin is not None
            self.stdin.write(input or b"")
            self.stdin.close()
            time.sleep(2.5)
            # ...and the operator interrupts the launcher.
            raise KeyboardInterrupt
        return super().communicate(input, timeout)


def test_an_interruption_kills_the_paid_session_and_its_children_before_it_propagates(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Interrupted.sessions.clear()
    monkeypatch.setattr(subprocess, "Popen", _Interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            rig.run(["please spawn a CHILD"])
        monkeypatch.undo()
        (session,) = _Interrupted.sessions
        try:
            session.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("the paid session outlived the interruption")
        assert rig.heart_has_stopped()
        assert rig.launched() == 1
        assert not rig.isolated_home().exists()
        assert not launcher.lock_path(rig.key_file).exists()
    finally:
        for session in _Interrupted.sessions:
            session.kill()
            for pipe in (session.stdin, session.stdout, session.stderr):
                if pipe is not None:
                    pipe.close()
        _Interrupted.sessions.clear()


def test_a_termination_request_unwinds_like_an_interruption(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unwind = vars(launcher)["_unwind"]
    # A closed terminal sends SIGHUP and Ctrl+\ sends SIGQUIT: left alone, either one ends the
    # launcher without unwinding, and the paid session outlives it with nothing left to bound it.
    assert {"SIGTERM", "SIGBREAK", "SIGHUP", "SIGQUIT"} <= set(launcher.UNWOUND_SIGNALS)
    numbers = [getattr(signal, name) for name in launcher.UNWOUND_SIGNALS if hasattr(signal, name)]
    before = [signal.getsignal(number) for number in numbers]
    during: list[Any] = []

    def terminated_mid_run(**_keywords: Any) -> None:
        during.extend(signal.getsignal(number) for number in numbers)
        unwind(numbers[0], None)

    monkeypatch.setattr(launcher, "run_rehearsal", terminated_mid_run)
    assert launcher.main(rig.command_line()) == launcher.EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err
    # While the run lasted, a termination request unwound the stack; afterwards, what was
    # there before is back.
    assert during
    assert all(handler is unwind for handler in during)
    assert [signal.getsignal(number) for number in numbers] == before


def test_a_cli_that_refuses_the_key_stops_the_run_without_echoing_it(rig: Rig) -> None:
    rig.key_file.write_text(KEY + "-REFUSED\n", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError) as refusal:
        rig.run(["look around"])
    assert KEY not in str(refusal.value)
    assert rig.launched() == 0
    assert not rig.isolated_home().exists()


def test_a_model_name_that_is_not_a_plain_identifier_is_refused(rig: Rig) -> None:
    # A shell's characters — or a key pasted in the wrong place: the model name is an
    # argument and a report field.
    for model in ("gpt & calc", KEY):
        with pytest.raises(launcher.RehearsalRefusedError, match="plain identifier"):
            rig.run(["look around"], model=model)
    assert rig.calls() == []
    rig.run(["look around"], model="some-model.v1")
    argv = rig.calls("exec")[0]["argv"]
    assert argv[argv.index("--model") + 1] == "some-model.v1"


def test_the_version_shape_takes_the_real_clis_answer_and_never_a_key() -> None:
    shape = launcher.VERSION_SHAPE
    # What the installed CLI answered to ``--version`` when this was written.
    assert shape.fullmatch("codex-cli 0.116.0")
    assert shape.fullmatch("codex-cli 0.117.0-alpha.3")
    for not_a_version in (
        KEY,
        "sk-" + "a" * 48,
        "codex-cli",
        "0.116.0",
        f"codex-cli 0.116.0 {KEY}",
    ):
        assert shape.fullmatch(not_a_version) is None, not_a_version


def test_records_that_cannot_be_read_are_refused_before_anything_starts(rig: Rig) -> None:
    rig.out.mkdir()
    for content in ("not json\n", '{"status": "COMPLETED"}\n', "[1, 2]\n"):
        (rig.out / launcher.RECORDS).write_text(content, encoding="utf-8")
        with pytest.raises(launcher.RehearsalRefusedError, match="records this script cannot read"):
            rig.run(["look around"])
    assert rig.calls() == []
    assert rig.launched() == 0


def test_the_command_line_refuses_with_its_own_exit_code(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = rig.command_line()
    assert launcher.main([*arguments, "--cli-version", "0.0.0-stub"]) == launcher.EXIT_OK
    output = capsys.readouterr()
    assert "1 of 6 sessions launched" in output.out
    assert ANSWER not in output.out + output.err
    assert launcher.main([*arguments, "--cli-version", "0.116.0"]) == launcher.EXIT_REFUSED
    assert "not the expected version" in capsys.readouterr().err
    absent = str(rig.source.parent / "absent.json")
    unreadable = [absent if item.endswith("tasks.json") else item for item in arguments]
    assert launcher.main(unreadable) == launcher.EXIT_REFUSED
    assert "cannot be read as JSON" in capsys.readouterr().err
    rig.key_file.unlink()
    assert launcher.main(arguments) == launcher.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_a_malformed_task_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    for content in ("[]", '[{"task_id": "a"}]', '[{"task_id": "a", "prompt": " "}]'):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(launcher.RehearsalRefusedError):
            launcher.load_tasks(path)
    # A task id is written in the records and the report: a name, never a path or a sentence.
    for task_id in ("C:/somewhere/private", "a task about someone", ""):
        path.write_text(json.dumps([{"task_id": task_id, "prompt": "x"}]), encoding="utf-8")
        with pytest.raises(launcher.RehearsalRefusedError, match="plain name"):
            launcher.load_tasks(path)
    path.write_text(
        json.dumps([{"task_id": "a", "prompt": "x"}, {"task_id": "a", "prompt": "y"}]),
        encoding="utf-8",
    )
    with pytest.raises(launcher.RehearsalRefusedError, match="repeat"):
        launcher.load_tasks(path)
    # A file that is not JSON, or is not there, is a refusal like any other, not a traceback.
    for content in ("not json", ""):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(launcher.RehearsalRefusedError, match="cannot be read as JSON"):
            launcher.load_tasks(path)
    with pytest.raises(launcher.RehearsalRefusedError, match="cannot be read as JSON"):
        launcher.load_tasks(tmp_path / "absent.json")


def test_the_committed_throwaway_tasks_load_and_fit_the_allowance() -> None:
    tasks = launcher.load_tasks(LAUNCHER_PATH.with_name("lab-rehearsal-tasks.json"))
    assert len(tasks) == launcher.APPROVED_MAX_SESSIONS
    assert all(task.task_id.startswith("rehearsal-") for task in tasks)


def test_the_launcher_and_its_tasks_name_no_private_location() -> None:
    # Both separators, whatever the platform the suite runs on: the paths to keep out are
    # Windows paths, and the suite runs on Linux too. A bare private name cannot be caught
    # here without naming it, which a public test must not do.
    needles = ("users\\", "users/", "appdata\\", "appdata/", "%userprofile%", "$home", "~/", "~\\")
    for path in (LAUNCHER_PATH, LAUNCHER_PATH.with_name("lab-rehearsal-tasks.json")):
        text = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            assert needle not in text, (path.name, needle)
        assert re.search(r"(?<![a-z])[a-z]:[\\/]", text) is None, path.name

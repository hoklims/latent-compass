"""HOK-804 — the rehearsal launcher bounds itself, isolates the key and keeps no outcome.

A stub stands in for the agent CLI: a real process, started the way the launcher starts the
real one, which logs what it was given. No real agent session runs here and no key exists.
These tests prove nothing about the experiment, which has not run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "examples" / "lab_rehearsal_launcher.py"
KEY = "sk-test-DEDICATED-KEY-0123456789"
ANSWER = "SECRET-ANSWER"
GIT_IDENTITY = ["-c", "user.name=rehearsal-test", "-c", "user.email=rehearsal-test@example.invalid"]

STUB = f'''
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
entry = {{
    "argv": args,
    "env": sorted(os.environ),
    "codex_home": os.environ.get("CODEX_HOME"),
    "home": os.environ.get("HOME"),
    "userprofile": os.environ.get("USERPROFILE"),
    "cwd_has_marker": Path(os.getcwd(), "marker.txt").is_file(),
}}


def log():
    with Path(__file__).with_name("stub-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if args[:1] == ["--version"]:
    log()
    print("codex-cli 0.0.0-stub")
    sys.exit(0)
if args[:2] == ["login", "--with-api-key"]:
    key = sys.stdin.read()
    entry["key_on_stdin"] = key.strip() == "{KEY}"
    Path(os.environ["CODEX_HOME"], "auth.json").write_text(key, encoding="utf-8")
    log()
    sys.exit(1 if "REFUSED" in key else 0)
if args[:1] == ["exec"]:
    prompt = sys.stdin.read()
    entry["prompt"] = prompt
    log()
    print(json.dumps({{"type": "thread.started", "thread_id": "stub"}}), flush=True)
    if "SLEEP" in prompt:
        time.sleep(60)
    if "FAIL" in prompt:
        print(json.dumps({{"type": "turn.failed", "error": {{"message": "boom"}}}}), flush=True)
        print("the provider said no to {KEY}", file=sys.stderr)
        sys.exit(1)
    message = {{"type": "agent_message", "text": "{ANSWER} the agent gave"}}
    print(json.dumps({{"type": "item.completed", "item": message}}), flush=True)
    usage = {{
        "input_tokens": 1000,
        "cached_input_tokens": 400,
        "output_tokens": 50,
        "reasoning_output_tokens": 7,
    }}
    print(json.dumps({{"type": "turn.completed", "usage": usage}}), flush=True)
    sys.exit(0)
sys.exit(9)
'''


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
        self.key_file = root / "dedicated.key"
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

    def calls(self) -> list[dict[str, Any]]:
        log = self.stub.with_name("stub-log.jsonl")
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def everything_written(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.out.rglob("*")
            if path.is_file()
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_without_the_dedicated_key_file_nothing_is_started(rig: Rig) -> None:
    rig.key_file.unlink()
    with pytest.raises(launcher.RehearsalRefusedError, match="does not exist"):
        rig.run(["look around"])
    assert rig.calls() == []
    assert not rig.out.exists()


def test_an_empty_key_file_starts_nothing(rig: Rig) -> None:
    rig.key_file.write_text("\n", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError, match="empty"):
        rig.run(["look around"])
    assert rig.calls() == []


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


def test_the_allowance_of_six_sessions_holds_across_invocations(rig: Rig) -> None:
    first = rig.run(["one", "two", "three", "four"])
    assert first["sessions_launched"] == 4
    second = rig.run(["five", "six", "seven", "eight"])
    assert second["sessions_launched"] == 6
    assert [record["task_id"] for record in second["records"]][4:] == [
        "throwaway-1",
        "throwaway-2",
    ]
    with pytest.raises(launcher.RehearsalRefusedError, match="allowance is spent"):
        rig.run(["nine"])
    assert sum(call["argv"][:1] == ["exec"] for call in rig.calls()) == 6


def test_a_session_is_counted_before_it_starts_and_a_crash_does_not_give_it_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*_arguments: Any) -> None:
        raise RuntimeError("the host fell over mid-session")

    monkeypatch.setattr(launcher, "_run_session", crash)
    with pytest.raises(RuntimeError, match="fell over"):
        rig.run(["one"], max_sessions=1)
    assert launcher.launched_so_far(rig.out) == 1
    login = next(call for call in rig.calls() if call["argv"][:1] == ["login"])
    assert not Path(login["codex_home"]).exists()
    monkeypatch.undo()
    with pytest.raises(launcher.RehearsalRefusedError, match="allowance is spent"):
        rig.run(["two"], max_sessions=1)


def test_a_mistyped_commit_is_refused_before_it_can_cost_a_session(rig: Rig) -> None:
    with pytest.raises(launcher.RehearsalRefusedError, match="does not hold that commit"):
        rig.run(["one"], ref="no-such-commit")
    assert rig.calls() == []
    assert launcher.launched_so_far(rig.out) == 0


def test_the_report_holds_durations_and_usage_and_nothing_the_agent_said(rig: Rig) -> None:
    report = rig.run(["look around", "look again"])
    assert [record["status"] for record in report["records"]] == ["COMPLETED", "COMPLETED"]
    assert report["tokens"]["input_tokens"] == {"min": 1000, "median": 1000, "max": 1000}
    assert report["tokens"]["reasoning_output_tokens"]["max"] == 7
    assert all(record["duration_ms"] >= 0 and record["turns"] == 1 for record in report["records"])
    assert ANSWER not in rig.everything_written()
    assert set(report["records"][0]) == {
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
    }


def test_the_key_travels_on_stdin_only_and_its_copy_is_deleted(rig: Rig) -> None:
    report = rig.run(["look around", "please FAIL"])
    calls = rig.calls()
    login = [call for call in calls if call["argv"][:1] == ["login"]]
    assert len(login) == 1
    assert login[0]["key_on_stdin"] is True
    assert all(KEY not in json.dumps(call["argv"]) for call in calls)
    assert KEY not in rig.everything_written()
    # The failing session kept a diagnostic, scrubbed of the key its stderr repeated.
    failed = report["records"][1]
    assert failed["status"] == "TURN_FAILED"
    assert "the provider said no to [key]" in failed["diagnostic"]
    home = Path(login[0]["codex_home"])
    assert not home.exists()
    assert report["key_copy_removed"] is True


def test_the_agent_gets_an_isolated_home_and_none_of_the_parent_environment(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
    monkeypatch.setenv("CODEX_HOME", str(rig.source))
    rig.run(["look around"])
    for call in rig.calls():
        assert "OPENAI_API_KEY" not in call["env"]
        assert call["codex_home"] != str(rig.source)
        assert call["codex_home"] == call["home"] == call["userprofile"]
        assert set(call["env"]) <= {
            *launcher.PASSED_THROUGH,
            *(name.upper() for name in launcher.PASSED_THROUGH),
            "CODEX_HOME",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "APPDATA",
            "LOCALAPPDATA",
        }


def test_each_session_works_on_its_own_copy_and_the_source_is_only_read(rig: Rig) -> None:
    before = _git(rig.source, "status", "--porcelain")
    rig.run(["look around"])
    session = next(call for call in rig.calls() if call["argv"][:1] == ["exec"])
    assert session["cwd_has_marker"] is True
    workdir = Path(session["argv"][session["argv"].index("--cd") + 1])
    assert workdir != rig.source
    assert not workdir.exists()
    assert session["argv"][-1] == "-"
    assert session["prompt"] == "look around"
    after = _git(rig.source, "status", "--porcelain")
    assert before == after == b""


def test_a_session_that_overruns_is_killed_and_recorded_as_timed_out(rig: Rig) -> None:
    started = time.monotonic()
    report = rig.run(["please SLEEP"], session_seconds=1)
    assert time.monotonic() - started < 30
    record = report["records"][0]
    assert record["status"] == "TIMED_OUT"
    assert record["turns"] == 0
    assert record["input_tokens"] == 0


def test_a_cli_that_refuses_the_key_stops_the_run_without_echoing_it(rig: Rig) -> None:
    rig.key_file.write_text(KEY + "-REFUSED\n", encoding="utf-8")
    with pytest.raises(launcher.RehearsalRefusedError) as refusal:
        rig.run(["look around"])
    assert KEY not in str(refusal.value)
    assert launcher.launched_so_far(rig.out) == 0
    login = next(call for call in rig.calls() if call["argv"][:1] == ["login"])
    assert not Path(login["codex_home"]).exists()


def test_the_command_line_refuses_with_its_own_exit_code(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = rig.source.parent / "tasks.json"
    tasks.write_text(json.dumps([{"task_id": "throwaway-1", "prompt": "look"}]), encoding="utf-8")
    rig.key_file.unlink()
    code = launcher.main(
        [
            "--key-file",
            str(rig.key_file),
            "--source",
            str(rig.source),
            "--ref",
            "HEAD",
            "--tasks",
            str(tasks),
            "--out",
            str(rig.out),
            "--codex-command",
            sys.executable,
            str(rig.stub),
        ]
    )
    assert code == launcher.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err
    assert rig.calls() == []


def test_a_malformed_task_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    for content in ("[]", '[{"task_id": "a"}]', '[{"task_id": "a", "prompt": " "}]'):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(launcher.RehearsalRefusedError):
            launcher.load_tasks(path)
    path.write_text(
        json.dumps([{"task_id": "a", "prompt": "x"}, {"task_id": "a", "prompt": "y"}]),
        encoding="utf-8",
    )
    with pytest.raises(launcher.RehearsalRefusedError, match="repeat"):
        launcher.load_tasks(path)


def test_the_committed_throwaway_tasks_load_and_fit_the_allowance() -> None:
    tasks = launcher.load_tasks(LAUNCHER_PATH.with_name("lab-rehearsal-tasks.json"))
    assert len(tasks) == launcher.APPROVED_MAX_SESSIONS
    assert all(task.task_id.startswith("rehearsal-") for task in tasks)


def test_the_launcher_names_no_private_location() -> None:
    text = LAUNCHER_PATH.read_text(encoding="utf-8").lower()
    for needle in ("users" + os.sep, "appdata" + os.sep):
        assert needle not in text

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "examples" / "latent_compass_shadow_hook.py"
NOW = "2026-09-20T00:00:00Z"


def _run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - resolved interpreter/Git, fixed args, no shell
        command,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    _run([git, "-C", str(root), *args])


def _hook(home: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            str(WRAPPER),
            "--host",
            "codex",
            "--home",
            str(home),
            "--now",
            NOW,
        ],
        input_text=json.dumps(payload),
    )


def test_wrapper_refreshes_git_source_and_never_persists_hook_content(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Harness Test")
    _git(root, "config", "user.email", "harness@example.invalid")
    tracked = root / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "fixture")
    home = tmp_path / "home"
    store = home / ".codex" / "latent-compass-shadow"
    store.mkdir(parents=True)
    store.joinpath("config.json").write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "enabled": True,
                "host_id": "codex-local",
                "agent_family": "codex",
                "projects": [
                    {
                        "root": str(root),
                        "alias": "wrapper-test",
                        "capabilities": [
                            {
                                "capability_id": "functions.exec",
                                "kind": "TOOL",
                                "cost_ceiling": 0,
                            }
                        ],
                        "remaining_budget": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    common: dict[str, object] = {
        "cwd": str(root),
        "session_id": "raw-session-must-not-persist",
        "turn_id": "raw-turn-must-not-persist",
        "model": "local-smoke",
        "permission_mode": "default",
        "prompt": "PROMPT-MUST-NOT-PERSIST",
    }
    started = _hook(home, {**common, "hook_event_name": "SessionStart", "source": "startup"})
    assert started.stdout == ""
    first = _hook(
        home,
        {**common, "hook_event_name": "PreToolUse", "tool_name": "functions.exec"},
    )
    assert first.stdout == ""
    first_record = json.loads(next((store / "events").rglob("*.json")).read_text("utf-8"))

    tracked.write_text("two\n", encoding="utf-8")
    refreshed = _hook(
        home,
        {**common, "hook_event_name": "PostToolUse", "tool_name": "functions.exec"},
    )
    assert refreshed.stdout == ""
    second = _hook(
        home,
        {
            **common,
            "hook_event_name": "PreToolUse",
            "tool_name": "functions.exec",
            "turn_id": "second-turn-must-not-persist",
        },
    )
    assert second.stdout == ""
    records = [json.loads(path.read_text("utf-8")) for path in (store / "events").rglob("*.json")]
    second_record = next(
        record for record in records if record["turn_seal"] != first_record["turn_seal"]
    )
    persisted = json.dumps(records)

    assert first_record["source_declaration_digest"] != second_record["source_declaration_digest"]
    assert first_record["route_decision"]["verdict"] == "ADVICE"
    assert "PROMPT-MUST-NOT-PERSIST" not in persisted
    assert "raw-session-must-not-persist" not in persisted
    assert "raw-turn-must-not-persist" not in persisted

"""Exercise the installed wheel's host entry point and packaged hook resource."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - resolved installed tools and disposable inputs
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=input_text,
    )


def _git(repository: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required for the installed-wheel lifecycle smoke")
    _run([executable, "-C", str(repository), *arguments])


def main() -> int:
    executable = shutil.which("latent-compass")
    if executable is None:
        raise RuntimeError("the installed latent-compass entry point is unavailable")
    with tempfile.TemporaryDirectory(prefix="latent-compass-wheel-") as temporary:
        root = Path(temporary)
        home = root / "home"
        project = root / "project"
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "config", "user.name", "Wheel Smoke")
        _git(project, "config", "user.email", "wheel-smoke@example.invalid")
        (project / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        _git(project, "add", "tracked.txt")
        _git(project, "commit", "-qm", "fixture")
        settings = {
            "codex": home / ".codex" / "hooks.json",
            "claude": home / ".claude" / "settings.json",
        }
        for path in settings.values():
            path.parent.mkdir(parents=True)
            path.write_text('{"hooks":{"PreToolUse":[]}}\n', encoding="utf-8")
        install_command = [
            executable,
            "host",
            "install",
            "--home",
            str(home),
            "--project-root",
            str(project),
            "--project-alias",
            "wheel-smoke",
            "--json",
        ]
        dry_run = json.loads(_run([*install_command, "--dry-run"]).stdout)
        assert dry_run["conflicts"] == []
        assert all(not state["installed"] for state in dry_run["states"].values())
        installed = json.loads(_run([*install_command, "--backup-tag", "wheel-smoke"]).stdout)
        assert installed["conflicts"] == []
        for host, tool_name in (("codex", "apply_patch"), ("claude", "Read")):
            wrapper = (
                home
                / f".{host}"
                / "latent-compass-shadow"
                / "runtime"
                / "latent-compass-shadow-hook.py"
            )
            common = {
                "cwd": str(project),
                "session_id": f"{host}-session",
                "turn_id": f"{host}-turn",
                "model": "wheel-smoke",
                "permission_mode": "default",
            }
            _run(
                [sys.executable, str(wrapper), "--host", host, "--home", str(home)],
                input_text=json.dumps(
                    {**common, "hook_event_name": "SessionStart", "source": "startup"}
                ),
            )
            _run(
                [sys.executable, str(wrapper), "--host", host, "--home", str(home)],
                input_text=json.dumps(
                    {**common, "hook_event_name": "PreToolUse", "tool_name": tool_name}
                ),
            )
        status = json.loads(
            _run(
                [
                    executable,
                    "host",
                    "status",
                    "--home",
                    str(home),
                    "--project-root",
                    str(project),
                    "--json",
                ]
            ).stdout
        )
        assert all(
            state["installed"] and state["configured"] for state in status["states"].values()
        )
        assert all(state["observed"] for state in status["states"].values())
        removed = json.loads(
            _run(
                [
                    executable,
                    "host",
                    "remove",
                    "--home",
                    str(home),
                    "--backup-tag",
                    "wheel-remove",
                    "--json",
                ]
            ).stdout
        )
        assert removed["conflicts"] == []
        after = json.loads(
            _run(
                [
                    executable,
                    "host",
                    "status",
                    "--home",
                    str(home),
                    "--project-root",
                    str(project),
                    "--json",
                ]
            ).stdout
        )
        assert all(not state["installed"] for state in after["states"].values())
        assert all(not state["configured"] for state in after["states"].values())
        for path in settings.values():
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert all(groups == [] for groups in payload["hooks"].values())
        wrappers = [
            home
            / f".{host}"
            / "latent-compass-shadow"
            / "runtime"
            / "latent-compass-shadow-hook.py"
            for host in ("codex", "claude")
        ]
        assert all(not wrapper.exists() for wrapper in wrappers)
        reinstalled = json.loads(_run([*install_command, "--backup-tag", "wheel-reinstall"]).stdout)
        assert reinstalled["conflicts"] == []
        assert all(state["installed"] for state in reinstalled["states"].values())
        cleaned = json.loads(
            _run(
                [
                    executable,
                    "host",
                    "remove",
                    "--home",
                    str(home),
                    "--backup-tag",
                    "wheel-cleanup",
                    "--json",
                ]
            ).stdout
        )
        assert cleaned["conflicts"] == []
        assert all(not wrapper.exists() for wrapper in wrappers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

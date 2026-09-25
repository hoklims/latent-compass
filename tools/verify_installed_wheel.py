"""Exercise the installed wheel's host entry point and packaged hook resource."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = shutil.which("latent-compass")
    if executable is None:
        raise RuntimeError("the installed latent-compass entry point is unavailable")
    with tempfile.TemporaryDirectory(prefix="latent-compass-wheel-") as temporary:
        root = Path(temporary)
        home = root / "home"
        project = root / "project"
        project.mkdir()
        hooks = home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"hooks":{"PreToolUse":[]}}\n', encoding="utf-8")
        completed = subprocess.run(  # noqa: S603 - resolved installed entry point
            [
                executable,
                "host",
                "install",
                "--host",
                "codex",
                "--home",
                str(home),
                "--runtime-python",
                sys.executable,
                "--project-root",
                str(project),
                "--project-alias",
                "wheel-smoke",
                "--dry-run",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout)
        report = json.loads(completed.stdout)
        expected_wrapper = (
            home / ".codex" / "latent-compass-shadow" / "runtime" / "latent-compass-shadow-hook.py"
        )
        files = {item["path"]: item["action"] for item in report["files"]}
        assert report["operation"] == "install"
        assert report["dry_run"] is True
        assert report["conflicts"] == []
        assert files[str(expected_wrapper)] == "create"
        assert report["states"]["codex"]["installed"] is False
        assert report["states"]["codex"]["configured"] is False
        assert not (home / ".codex" / "latent-compass-shadow").exists()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

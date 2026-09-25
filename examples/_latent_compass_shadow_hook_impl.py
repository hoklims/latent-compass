"""Host-owned implementation of the passive Latent Compass hook wrapper."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Final, TextIO

from latent_compass.canonical import seal
from latent_compass.shadow_harness import (
    DEFAULT_CONFIG_NAME,
    MAX_HOOK_BYTES,
    ShadowHarnessConfig,
    default_store_root,
    load_shadow_config,
)
from latent_compass.shadow_harness import main as shadow_main

MAX_GIT_BYTES: Final = 8 * 1_048_576


def _git(root: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git_unavailable")
    completed = subprocess.run(  # noqa: S603 - resolved executable, fixed args, no shell
        [git, "-C", str(root), *args], check=True, capture_output=True, timeout=5
    )
    if len(completed.stdout) > MAX_GIT_BYTES:
        raise RuntimeError("git_output_too_large")
    return completed.stdout


def source_declaration(root: Path) -> str:
    """Return a bounded declaration of the current local Git state."""
    head = _git(root, "rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    status = _git(root, "status", "--porcelain=v2", "--branch", "-z")
    unstaged = _git(root, "diff", "--binary", "--no-ext-diff", "--")
    staged = _git(root, "diff", "--cached", "--binary", "--no-ext-diff", "--")
    return seal(
        "shadow.harness.source-declaration.v1",
        {
            "head": head,
            "status_digest": seal("shadow.harness.git-status.v1", status.hex()),
            "unstaged_digest": seal("shadow.harness.git-diff.v1", unstaged.hex()),
            "staged_digest": seal("shadow.harness.git-staged.v1", staged.hex()),
        },
    )


def _allowed_root(config: ShadowHarnessConfig, cwd: object) -> Path | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    candidate = Path(cwd).resolve(strict=False)
    matches: list[Path] = []
    for project in config.projects:
        root = Path(project.root).resolve(strict=False)
        if candidate == root or candidate.is_relative_to(root):
            matches.append(root)
    return max(matches, key=lambda root: len(root.parts), default=None)


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description="Latent Compass passive hook wrapper")
    parser.add_argument("--host", choices=("codex", "claude"), required=True)
    parser.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        raw = stdin.read(MAX_HOOK_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
            raise RuntimeError("hook_payload_too_large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("hook_payload_not_object")
        store = default_store_root(args.host, args.home)
        config = load_shadow_config(json.loads((store / DEFAULT_CONFIG_NAME).read_text("utf-8")))
        if payload.get("hook_event_name") in {"SessionStart", "PostToolUse"}:
            root = _allowed_root(config, payload.get("cwd"))
            if root is not None:
                payload["source_declaration_digest"] = source_declaration(root)
                payload["source_observed_at"] = args.now or datetime.now(tz=UTC).replace(
                    microsecond=0
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return shadow_main(
            ["--host", args.host, *(["--home", str(args.home)] if args.home else [])],
            stdin=StringIO(json.dumps(payload)),
            stdout=stdout,
            stderr=stderr,
        )
    except Exception:
        stderr.write("[latent-compass-shadow] fail-open: wrapper event not recorded\n")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create public audit epochs and fail closed on independent audit receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from re import fullmatch
from typing import Any

SCHEMA = "hoklims/latent-compass:independent-audit/2"
REPOSITORY = "hoklims/latent-compass"
SHA256_PATTERN = r"sha256:[0-9a-f]{64}"
GIT_SHA_PATTERN = r"[0-9a-f]{40}"
POLICY_FILES = (
    "docs/independent-audit.md",
    "tools/independent_audit.py",
    "tests/test_independent_audit.py",
)


class AuditError(ValueError):
    """A receipt cannot authorize the audited candidate."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    git = shutil.which("git")
    if git is None:
        raise AuditError("git is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved executable; caller supplies git arguments
        [git, "-C", str(root), *args], check=True, capture_output=True
    )
    return result.stdout if binary else result.stdout.decode("utf-8", errors="strict").strip()


def _root(repository: Path) -> Path:
    return Path(str(_git(repository, "rev-parse", "--show-toplevel"))).resolve()


def _policy_digest(root: Path) -> str:
    material = []
    for relative in POLICY_FILES:
        path = root / relative
        if not path.is_file():
            raise AuditError(f"public policy file is missing: {relative}")
        material.append({"path": relative, "digest": _digest(path.read_bytes())})
    return _digest(_canonical(material))


def _epoch_digest(epoch: dict[str, Any]) -> str:
    material = {key: value for key, value in epoch.items() if key != "epoch_digest"}
    return _digest(_canonical(material))


def create_epoch(repository: Path, base: str, head: str) -> dict[str, object]:
    root = _root(repository)
    base_sha = str(_git(root, "rev-parse", base))
    head_sha = str(_git(root, "rev-parse", head))
    changed = str(_git(root, "diff", "--name-only", "--find-renames", f"{base_sha}...{head_sha}"))
    paths = sorted(line for line in changed.splitlines() if line)
    files = []
    for relative in paths:
        try:
            content = _git(root, "show", f"{head_sha}:{relative}", binary=True)
            assert isinstance(content, bytes)
            files.append({"path": relative, "kind": "file", "digest": _digest(content)})
        except subprocess.CalledProcessError:
            files.append(
                {
                    "path": relative,
                    "kind": "deleted",
                    "digest": _digest(b"latent-compass:deleted:v1"),
                }
            )
    epoch: dict[str, object] = {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "head_tree": str(_git(root, "rev-parse", f"{head_sha}^{{tree}}")),
        "policy_digest": _policy_digest(root),
        "files": files,
    }
    epoch["epoch_digest"] = _epoch_digest(epoch)
    return epoch


def _required_bool(mapping: dict[str, Any], key: str) -> None:
    if mapping.get(key) is not True:
        raise AuditError(f"{key} must be true")


def gate(epoch: dict[str, Any], receipt: dict[str, Any]) -> dict[str, object]:
    if epoch.get("schema") != SCHEMA or receipt.get("schema") != SCHEMA:
        raise AuditError(f"schema must be {SCHEMA}")
    if epoch.get("repository") != REPOSITORY:
        raise AuditError(f"repository must be {REPOSITORY}")
    for key in ("base_sha", "head_sha", "head_tree"):
        if not isinstance(epoch.get(key), str) or fullmatch(GIT_SHA_PATTERN, epoch[key]) is None:
            raise AuditError(f"epoch {key} must be a full lowercase Git SHA")
    if (
        not isinstance(epoch.get("policy_digest"), str)
        or fullmatch(SHA256_PATTERN, epoch["policy_digest"]) is None
    ):
        raise AuditError("epoch policy_digest must be a SHA-256 digest")
    files = epoch.get("files")
    if not isinstance(files, list) or not files:
        raise AuditError("epoch files must be a non-empty array")
    paths: list[str] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "kind", "digest"}:
            raise AuditError(f"epoch file {index} is malformed")
        path = item.get("path")
        kind = item.get("kind")
        digest = item.get("digest")
        if not isinstance(path, str) or not path or path.startswith(("/", "../")):
            raise AuditError(f"epoch file {index} has an invalid path")
        if kind not in {"file", "deleted"}:
            raise AuditError(f"epoch file {index} has an invalid kind")
        if not isinstance(digest, str) or fullmatch(SHA256_PATTERN, digest) is None:
            raise AuditError(f"epoch file {index} has an invalid digest")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise AuditError("epoch file paths must be unique and sorted")
    expected_epoch_digest = _epoch_digest(epoch)
    if epoch.get("epoch_digest") != expected_epoch_digest:
        raise AuditError("epoch_digest does not match the canonical epoch contents")
    for key in ("epoch_digest", "policy_digest", "head_sha"):
        if receipt.get(key) != epoch.get(key):
            raise AuditError(f"receipt {key} does not match the epoch")
    independence = receipt.get("independence")
    if not isinstance(independence, dict):
        raise AuditError("independence must be an object")
    for key in (
        "not_candidate_author",
        "read_only_candidate",
        "fresh_session",
        "distinct_harness",
        "distinct_account",
        "distinct_environment",
        "distinct_evidence_store",
        "first_pass_before_author_narrative",
    ):
        _required_bool(independence, key)
    claims = receipt.get("claims")
    if not isinstance(claims, list) or not claims:
        raise AuditError("at least one material claim is required")
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not claim.get("claim"):
            raise AuditError(f"claim {index} is malformed")
        invocation_paths = claim.get("invocation_paths")
        if not isinstance(invocation_paths, list) or not invocation_paths:
            raise AuditError(f"claim {index} has no invocation paths")
        witness = claim.get("witness")
        if not isinstance(witness, dict):
            raise AuditError(f"claim {index} has no witness")
        if not isinstance(witness.get("red_exit"), int) or witness["red_exit"] == 0:
            raise AuditError(f"claim {index} has no observed red result")
        if witness.get("green_exit") != 0:
            raise AuditError(f"claim {index} has no observed green result")
        for key in ("mutation", "command", "red_output_digest", "green_output_digest"):
            if not isinstance(witness.get(key), str) or not witness[key]:
                raise AuditError(f"claim {index} witness is missing {key}")
        for key in ("red_output_digest", "green_output_digest"):
            if fullmatch(SHA256_PATTERN, witness[key]) is None:
                raise AuditError(f"claim {index} witness {key} must be a SHA-256 digest")
    if receipt.get("unresolved_blockers") != []:
        raise AuditError("unresolved_blockers must be an empty array")
    if receipt.get("verdict") != "PROOF_ADEQUATE":
        raise AuditError("verdict must be PROOF_ADEQUATE")
    return {
        "schema": SCHEMA,
        "decision": "ALLOW",
        "epoch_digest": epoch["epoch_digest"],
        "head_sha": epoch["head_sha"],
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"{path} must contain an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    epoch_parser = commands.add_parser("epoch")
    epoch_parser.add_argument("--repository", type=Path, default=Path.cwd())
    epoch_parser.add_argument("--base", required=True)
    epoch_parser.add_argument("--head", required=True)
    epoch_parser.add_argument("--output", type=Path)
    gate_parser = commands.add_parser("gate")
    gate_parser.add_argument("--epoch", type=Path, required=True)
    gate_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "epoch":
            result = create_epoch(args.repository, args.base, args.head)
            rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.write_text(rendered, encoding="utf-8", newline="\n")
            else:
                sys.stdout.write(rendered)
        else:
            result = gate(_load(args.epoch), _load(args.receipt))
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (AuditError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"decision": "BLOCK", "error": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

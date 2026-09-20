#!/usr/bin/env python3
"""Create public audit epochs and fail closed on independent audit receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from re import fullmatch
from typing import Any

SCHEMA = "hoklims/latent-compass:independent-audit/3"
REPOSITORY = "hoklims/latent-compass"
SHA256_PATTERN = r"sha256:[0-9a-f]{64}"
GIT_SHA_PATTERN = r"[0-9a-f]{40}"
MAX_MUTATION_BYTES = 8 * 1024 * 1024
REQUIRED_INVOCATION_PATHS = ["local", "pull_request", "main"]
PYTEST_NODE_PATTERN = r"tests/[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_\[\].:-]+"
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


def _policy_digest(root: Path, revision: str) -> str:
    material = []
    for relative in POLICY_FILES:
        try:
            content = _git(root, "show", f"{revision}:{relative}", binary=True)
        except subprocess.CalledProcessError as exc:
            raise AuditError(f"public policy file is missing at {revision}: {relative}") from exc
        assert isinstance(content, bytes)
        material.append({"path": relative, "digest": _digest(content)})
    return _digest(_canonical(material))


def _epoch_digest(epoch: dict[str, Any]) -> str:
    material = {key: value for key, value in epoch.items() if key != "epoch_digest"}
    return _digest(_canonical(material))


def _changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    output = _git(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        f"{base_sha}...{head_sha}",
        binary=True,
    )
    assert isinstance(output, bytes)
    fields = output.decode("utf-8", errors="strict").split("\0")
    fields.pop()
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            paths.extend((fields[index], fields[index + 1]))
            index += 2
        else:
            paths.append(fields[index])
            index += 1
    return sorted(set(paths))


def _run_pytest(root: Path, targets: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src")))
    return subprocess.run(  # noqa: S603 - fixed interpreter/module; targets are argv entries
        [sys.executable, "-m", "pytest", "-q", *targets],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _execute_witness(
    repository: Path,
    head_sha: str,
    targets: list[dict[str, str]],
    pytest_targets: list[str],
    expected_failure: str,
) -> dict[str, object]:
    def run_variant(*, mutate: bool) -> subprocess.CompletedProcess[str]:
        temporary = tempfile.mkdtemp(prefix="latent-compass-audit-")
        worktree = Path(temporary) / "candidate"
        _git(repository, "worktree", "add", "--quiet", "--detach", str(worktree), head_sha)
        try:
            if mutate:
                for target in targets:
                    (worktree / target["path"]).write_text(
                        target["after"], encoding="utf-8", newline=""
                    )
            return _run_pytest(worktree, pytest_targets)
        finally:
            _git(repository, "worktree", "remove", "--force", str(worktree))
            shutil.rmtree(temporary, ignore_errors=True)

    red = run_variant(mutate=True)
    green = run_variant(mutate=False)
    if red.returncode == 0:
        raise AuditError("witness mutation did not make its oracle fail")
    if expected_failure not in (red.stdout + red.stderr):
        raise AuditError("witness red output lacks the expected failure marker")
    if green.returncode != 0:
        raise AuditError("witness restoration did not make its oracle pass")
    return {
        "red_exit": red.returncode,
        "green_exit": green.returncode,
        "red_output_digest": _digest((red.stdout + red.stderr).encode("utf-8")),
        "green_output_digest": _digest((green.stdout + green.stderr).encode("utf-8")),
    }


def create_epoch(repository: Path, base: str, head: str) -> dict[str, object]:
    root = _root(repository)
    base_sha = str(_git(root, "rev-parse", "--verify", f"{base}^{{commit}}"))
    head_sha = str(_git(root, "rev-parse", "--verify", f"{head}^{{commit}}"))
    paths = _changed_paths(root, base_sha, head_sha)
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
        "policy_digest": _policy_digest(root, head_sha),
        "files": files,
    }
    epoch["epoch_digest"] = _epoch_digest(epoch)
    return epoch


def _required_bool(mapping: dict[str, Any], key: str) -> None:
    if mapping.get(key) is not True:
        raise AuditError(f"{key} must be true")


def gate(repository: Path, epoch: dict[str, Any], receipt: dict[str, Any]) -> dict[str, object]:
    root = _root(repository)
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
    try:
        derived_epoch = create_epoch(repository, epoch["base_sha"], epoch["head_sha"])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AuditError("epoch cannot be derived from the repository objects") from exc
    if epoch != derived_epoch:
        raise AuditError("epoch does not match the repository-derived epoch")
    for key in ("epoch_digest", "policy_digest", "head_sha"):
        if receipt.get(key) != epoch.get(key):
            raise AuditError(f"receipt {key} does not match the epoch")
    required_receipt = {
        "schema",
        "epoch_digest",
        "policy_digest",
        "head_sha",
        "independence",
        "claims",
        "unresolved_blockers",
        "verdict",
    }
    if set(receipt) != required_receipt:
        raise AuditError("receipt fields do not match schema 3")
    independence = receipt.get("independence")
    if not isinstance(independence, dict):
        raise AuditError("independence must be an object")
    independence_fields = {
        "not_candidate_author",
        "read_only_candidate",
        "fresh_session",
        "distinct_harness",
        "distinct_account",
        "distinct_environment",
        "distinct_evidence_store",
        "first_pass_before_author_narrative",
    }
    if set(independence) != independence_fields:
        raise AuditError("independence fields do not match schema 3")
    for key in independence_fields:
        _required_bool(independence, key)
    claims = receipt.get("claims")
    if not isinstance(claims, list) or not claims:
        raise AuditError("at least one material claim is required")
    witness_results: list[dict[str, object]] = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {"claim", "invocation_paths", "witness"}:
            raise AuditError(f"claim {index} is malformed")
        if not isinstance(claim.get("claim"), str) or not claim["claim"]:
            raise AuditError(f"claim {index} has no claim text")
        invocation_paths = claim.get("invocation_paths")
        if invocation_paths != REQUIRED_INVOCATION_PATHS:
            raise AuditError(f"claim {index} invocation paths do not match schema 3")
        witness = claim.get("witness")
        if not isinstance(witness, dict) or set(witness) != {
            "expected_failure",
            "mutation",
            "pytest_targets",
            "targets",
        }:
            raise AuditError(f"claim {index} has no witness")
        if not isinstance(witness.get("mutation"), str) or not witness["mutation"]:
            raise AuditError(f"claim {index} witness has no mutation description")
        expected_failure = witness.get("expected_failure")
        if (
            not isinstance(expected_failure, str)
            or fullmatch(PYTEST_NODE_PATTERN, expected_failure) is None
        ):
            raise AuditError(f"claim {index} witness has an invalid expected failure")
        pytest_targets = witness.get("pytest_targets")
        if (
            not isinstance(pytest_targets, list)
            or not pytest_targets
            or not all(
                isinstance(item, str) and fullmatch(PYTEST_NODE_PATTERN, item) is not None
                for item in pytest_targets
            )
        ):
            raise AuditError(f"claim {index} witness has invalid pytest targets")
        targets = witness.get("targets")
        if not isinstance(targets, list) or not targets:
            raise AuditError(f"claim {index} witness has no mutation targets")
        if len(targets) > 64:
            raise AuditError(f"claim {index} witness has too many mutation targets")
        total_bytes = 0
        normalized_targets: list[dict[str, str]] = []
        seen_targets: set[str] = set()
        for target_index, target in enumerate(targets):
            required = {"path", "before", "after", "before_digest", "after_digest"}
            if not isinstance(target, dict) or set(target) != required:
                raise AuditError(f"claim {index} target {target_index} is malformed")
            if not all(isinstance(target[key], str) for key in required):
                raise AuditError(f"claim {index} target {target_index} must contain strings")
            path = target["path"]
            normalized_path = PurePosixPath(path).as_posix()
            if (
                not path
                or path != normalized_path
                or path.startswith("/")
                or "\\" in path
                or any(part in {".", ".."} for part in PurePosixPath(path).parts)
                or path in seen_targets
            ):
                raise AuditError(f"claim {index} target {target_index} has an invalid path")
            seen_targets.add(path)
            before_bytes = target["before"].encode("utf-8")
            after_bytes = target["after"].encode("utf-8")
            total_bytes += len(before_bytes) + len(after_bytes)
            if before_bytes == after_bytes:
                raise AuditError(f"claim {index} target {target_index} does not mutate content")
            if target["before_digest"] != _digest(before_bytes):
                raise AuditError(f"claim {index} target {target_index} before digest is invalid")
            if target["after_digest"] != _digest(after_bytes):
                raise AuditError(f"claim {index} target {target_index} after digest is invalid")
            try:
                committed = _git(root, "show", f"{epoch['head_sha']}:{path}", binary=True)
            except subprocess.CalledProcessError as exc:
                raise AuditError(
                    f"claim {index} target {target_index} is absent from the candidate"
                ) from exc
            assert isinstance(committed, bytes)
            if committed != before_bytes:
                raise AuditError(
                    f"claim {index} target {target_index} baseline differs from the candidate"
                )
            normalized_targets.append({key: target[key] for key in sorted(required)})
        if total_bytes > MAX_MUTATION_BYTES:
            raise AuditError(f"claim {index} witness mutation payload is too large")
        result = _execute_witness(
            root, epoch["head_sha"], normalized_targets, pytest_targets, expected_failure
        )
        witness_results.append({"claim": claim["claim"], **result})
    if receipt.get("unresolved_blockers") != []:
        raise AuditError("unresolved_blockers must be an empty array")
    if receipt.get("verdict") != "PROOF_ADEQUATE":
        raise AuditError("verdict must be PROOF_ADEQUATE")
    return {
        "schema": SCHEMA,
        "decision": "ALLOW",
        "epoch_digest": epoch["epoch_digest"],
        "head_sha": epoch["head_sha"],
        "witnesses": witness_results,
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
    gate_parser.add_argument("--repository", type=Path, default=Path.cwd())
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
            result = gate(args.repository, _load(args.epoch), _load(args.receipt))
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except (AuditError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"decision": "BLOCK", "error": str(exc)}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

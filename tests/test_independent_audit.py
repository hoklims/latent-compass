from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from runpy import run_path
from typing import Any, cast

import pytest

from workflow_assertions import job_lines, step_values

ROOT = Path(__file__).resolve().parents[1]
_TOOL = run_path(str(ROOT / "tools" / "independent_audit.py"))
AuditError = cast(type[ValueError], _TOOL["AuditError"])
create_epoch = cast(Callable[[Path, str, str], dict[str, object]], _TOOL["create_epoch"])
gate = cast(Callable[[Path, dict[str, Any], dict[str, Any]], dict[str, object]], _TOOL["gate"])
epoch_digest = cast(Callable[[dict[str, Any]], str], _TOOL["_epoch_digest"])
digest = cast(Callable[[bytes], str], _TOOL["_digest"])
changed_paths = cast(Callable[[Path, str, str], list[str]], _TOOL["_changed_paths"])


def _git(repository: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for the independent-audit gate tests")
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed test inputs
        [git, "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _write_policy(repository: Path) -> None:
    for relative in (
        "docs/independent-audit.md",
        "tools/independent_audit.py",
        "tests/test_independent_audit.py",
    ):
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())


@pytest.fixture
def audited_repository(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Independent Audit Test")
    _git(repository, "config", "user.email", "audit-test@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8", newline="\n")
    (repository / "old name.txt").write_text("rename me\n", encoding="utf-8", newline="\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    _write_policy(repository)
    (repository / "subject.txt").write_text("candidate\n", encoding="utf-8", newline="\n")
    (repository / "tests" / "test_subject.py").write_text(
        "from pathlib import Path\n\n"
        "def test_subject_is_pristine() -> None:\n"
        "    assert (Path(__file__).parents[1] / 'subject.txt').read_text() == 'candidate\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    (repository / "old name.txt").rename(repository / "new name.txt")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "candidate")
    head_sha = _git(repository, "rev-parse", "HEAD")
    epoch = cast(dict[str, Any], create_epoch(repository, base_sha, head_sha))
    return repository, epoch, _receipt(epoch)


def _receipt(epoch: dict[str, Any]) -> dict[str, Any]:
    before = "candidate\n"
    after = "mutated\n"
    target = {
        "path": "subject.txt",
        "before": before,
        "after": after,
        "before_digest": digest(before.encode("utf-8")),
        "after_digest": digest(after.encode("utf-8")),
    }
    claim = "the gate detects a representative defect"
    return {
        "schema": "hoklims/latent-compass:independent-audit/3",
        "epoch_digest": epoch["epoch_digest"],
        "policy_digest": epoch["policy_digest"],
        "head_sha": epoch["head_sha"],
        "independence": {
            "not_candidate_author": True,
            "read_only_candidate": True,
            "fresh_session": True,
            "distinct_harness": True,
            "distinct_account": True,
            "distinct_environment": True,
            "distinct_evidence_store": True,
            "first_pass_before_author_narrative": True,
        },
        "claims": [
            {
                "claim": claim,
                "invocation_paths": ["local", "pull_request", "main"],
                "witness": {
                    "expected_failure": "tests/test_subject.py::test_subject_is_pristine",
                    "mutation": "replace expected decision",
                    "pytest_targets": ["tests/test_subject.py::test_subject_is_pristine"],
                    "targets": [target],
                },
            }
        ],
        "unresolved_blockers": [],
        "verdict": "PROOF_ADEQUATE",
    }


def test_gate_allows_a_repository_derived_adequate_receipt(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    assert gate(repository, epoch, receipt)["decision"] == "ALLOW"


def test_epoch_inventory_covers_both_sides_of_a_rename(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    _, epoch, _ = audited_repository
    inventory = {item["path"]: item["kind"] for item in epoch["files"]}
    assert inventory["old name.txt"] == "deleted"
    assert inventory["new name.txt"] == "file"


def test_changed_path_parser_is_nul_safe_for_newlines_and_renames() -> None:
    original_git = changed_paths.__globals__["_git"]

    def fake_git(*_args: object, **_kwargs: object) -> bytes:
        return b"R100\0old\nname.txt\0new\nname.txt\0M\0ordinary.txt\0"

    changed_paths.__globals__["_git"] = fake_git
    try:
        assert changed_paths(Path(), "a" * 40, "b" * 40) == [
            "new\nname.txt",
            "old\nname.txt",
            "ordinary.txt",
        ]
    finally:
        changed_paths.__globals__["_git"] = original_git


def test_gate_refuses_nonexistent_git_objects_even_with_a_rehashed_epoch(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    epoch["base_sha"] = "0" * 39 + "1"
    epoch["head_sha"] = "0" * 39 + "2"
    epoch["head_tree"] = "0" * 39 + "3"
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt.update({key: epoch[key] for key in ("epoch_digest", "policy_digest", "head_sha")})
    with pytest.raises(AuditError, match="cannot be derived"):
        gate(repository, epoch, receipt)


def test_gate_refuses_an_incomplete_inventory_even_when_rehashed(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    epoch["files"] = []
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt["epoch_digest"] = epoch["epoch_digest"]
    with pytest.raises(AuditError, match="non-empty array"):
        gate(repository, epoch, receipt)


def test_gate_refuses_an_extra_inventory_entry_even_when_rehashed(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    epoch["files"].append({"path": "extra.txt", "kind": "file", "digest": "sha256:" + "a" * 64})
    epoch["files"] = sorted(epoch["files"], key=lambda item: item["path"])
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt["epoch_digest"] = epoch["epoch_digest"]
    with pytest.raises(AuditError, match="repository-derived epoch"):
        gate(repository, epoch, receipt)


def test_gate_refuses_a_missing_inventory_entry_even_when_rehashed(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    epoch["files"] = [
        item for item in epoch["files"] if item["path"] != "tools/independent_audit.py"
    ]
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt["epoch_digest"] = epoch["epoch_digest"]
    with pytest.raises(AuditError, match="repository-derived epoch"):
        gate(repository, epoch, receipt)


def test_gate_reads_policy_from_the_head_commit_not_the_worktree(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, _ = audited_repository
    policy = repository / "docs" / "independent-audit.md"
    policy.write_text("uncommitted policy edit\n", encoding="utf-8", newline="\n")
    regenerated = create_epoch(repository, epoch["base_sha"], epoch["head_sha"])
    assert regenerated == epoch


@pytest.mark.parametrize("field", ["base_sha", "head_tree", "policy_digest"])
def test_gate_isolates_each_repository_derived_identity(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], field: str
) -> None:
    repository, epoch, receipt = audited_repository
    epoch[field] = "a" * 40 if field != "policy_digest" else "sha256:" + "a" * 64
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt.update({key: epoch[key] for key in ("epoch_digest", "policy_digest", "head_sha")})
    with pytest.raises(AuditError):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize("field", ["red_exit", "green_exit"])
def test_gate_refuses_booleans_as_exit_codes(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    field: str,
) -> None:
    repository, epoch, receipt = audited_repository
    witness = receipt["claims"][0]["witness"]
    witness[field] = True
    with pytest.raises(AuditError, match="has no witness"):
        gate(repository, epoch, receipt)


def test_gate_refuses_placeholder_output_digests(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["witness"]["red_output_digest"] = "sha256:" + "a" * 64
    with pytest.raises(AuditError, match="has no witness"):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("before", "not the candidate\n"),
        ("after", "candidate\n"),
        ("before_digest", "sha256:" + "a" * 64),
        ("after_digest", "sha256:" + "b" * 64),
        ("path", "absent.txt"),
    ],
)
def test_gate_refuses_unbound_mutation_targets(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    field: str,
    value: str,
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["witness"]["targets"][0][field] = value
    with pytest.raises(AuditError):
        gate(repository, epoch, receipt)


def test_gate_refuses_duplicate_mutation_targets(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    target = deepcopy(receipt["claims"][0]["witness"]["targets"][0])
    receipt["claims"][0]["witness"]["targets"].append(target)
    with pytest.raises(AuditError, match="invalid path"):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize("path", ["./subject.txt", "tools/../subject.txt"])
def test_gate_refuses_aliased_mutation_paths(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], path: str
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["witness"]["targets"][0]["path"] = path
    with pytest.raises(AuditError, match="invalid path"):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize(
    "pytest_target",
    ["tests/test_subject.py", "-x", "C:/tmp/test_subject.py::test_subject_is_pristine"],
)
def test_gate_accepts_only_precise_repository_pytest_node_ids(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], pytest_target: str
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["witness"]["pytest_targets"] = [pytest_target]
    with pytest.raises(AuditError, match="invalid pytest targets"):
        gate(repository, epoch, receipt)


def test_gate_requires_the_expected_test_to_appear_in_red_output(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["witness"]["expected_failure"] = "tests/test_subject.py::another_test"
    with pytest.raises(AuditError, match="expected failure marker"):
        gate(repository, epoch, receipt)


def test_gate_requires_exact_invocation_paths(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["claims"][0]["invocation_paths"] = ["fictional"]
    with pytest.raises(AuditError, match="invocation paths"):
        gate(repository, epoch, receipt)


def test_gate_executes_the_mutation_and_oracle_itself(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    result = gate(repository, epoch, receipt)
    witnesses = cast(list[dict[str, object]], result["witnesses"])
    assert witnesses[0]["red_exit"] != 0
    assert witnesses[0]["green_exit"] == 0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("head_sha",), "b" * 40),
        (("verdict",), "PROOF_WEAK"),
        (("unresolved_blockers",), ["still blocked"]),
        (("independence", "first_pass_before_author_narrative"), False),
    ],
)
def test_gate_fails_closed(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    path: tuple[object, ...],
    value: object,
) -> None:
    repository, epoch, original = audited_repository
    receipt = deepcopy(original)
    target: object = receipt
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(AuditError):
        gate(repository, epoch, receipt)


def test_cli_gate_blocks_a_forged_epoch(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    repository, epoch, receipt = audited_repository
    epoch["head_sha"] = "f" * 40
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt["head_sha"] = epoch["head_sha"]
    receipt["epoch_digest"] = epoch["epoch_digest"]
    epoch_path = tmp_path / "epoch.json"
    receipt_path = tmp_path / "receipt.json"
    epoch_path.write_text(json.dumps(epoch), encoding="utf-8", newline="\n")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8", newline="\n")
    completed = subprocess.run(  # noqa: S603 - current interpreter and fixed tool path
        [
            sys.executable,
            str(ROOT / "tools" / "independent_audit.py"),
            "gate",
            "--repository",
            str(repository),
            "--epoch",
            str(epoch_path),
            "--receipt",
            str(receipt_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    assert json.loads(completed.stderr)["decision"] == "BLOCK"


def test_verify_workflow_explicitly_invokes_the_gate_tests() -> None:
    verify = job_lines(ROOT / ".github" / "workflows" / "verify.yml", "verify")

    assert step_values(verify, "run")["Exercise independent audit gate fail-closed tests"] == (
        "uv run --frozen pytest -o addopts='' -q tests/test_independent_audit.py"
    )

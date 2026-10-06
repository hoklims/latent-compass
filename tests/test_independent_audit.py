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

from workflow_assertions import assert_run, job, workflow

ROOT = Path(__file__).resolve().parents[1]
_TOOL = run_path(str(ROOT / "tools" / "independent_audit.py"))
AuditError = cast(type[ValueError], _TOOL["AuditError"])
create_epoch = cast(Callable[[Path, str, str], dict[str, object]], _TOOL["create_epoch"])
gate = cast(Callable[..., dict[str, object]], _TOOL["gate"])
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
        "tools/audit_pytest_reporter.py",
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
        "schema": "hoklims/latent-compass:independent-audit/4",
        "epoch_digest": epoch["epoch_digest"],
        "policy_digest": epoch["policy_digest"],
        "head_sha": epoch["head_sha"],
        "audit_profile": "separate-account",
        "isolation": None,
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


def _isolated_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(receipt)
    result["audit_profile"] = "isolated-session"
    result["independence"] = {
        "not_candidate_author": True,
        "read_only_candidate": True,
        "fresh_session": True,
        "first_pass_before_author_narrative": True,
        "distinct_account": False,
    }
    result["isolation"] = {
        "session_id": "fresh-independent-session",
        "forked": False,
        "sandbox_mode": "read-only",
        "persistent_memory": False,
        "write_tools_enabled": False,
    }
    return result


def test_isolated_profile_requires_explicit_operator_opt_in(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    with pytest.raises(AuditError, match="audit_profile"):
        gate(repository, epoch, _isolated_receipt(receipt))


@pytest.mark.parametrize("unknown_environment", [False, True])
def test_isolated_profile_reports_same_account_and_unknown_environment_honestly(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    unknown_environment: bool,
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    if unknown_environment:
        isolated["isolation"]["persistent_memory"] = None
        isolated["isolation"]["write_tools_enabled"] = None
    result = gate(repository, epoch, isolated, audit_profile="isolated-session")
    assert result["decision"] == "ALLOW"
    assert result["audit_profile"] == "isolated-session"
    assert result["verdict"] == "PROOF_ADEQUATE_WITH_LIMITS"
    assert result["limits"] == ["SAME_ACCOUNT_ISOLATED_REVIEW"] + (
        [
            "AUDITOR_ENVIRONMENT_UNATTESTED:persistent_memory",
            "AUDITOR_ENVIRONMENT_UNATTESTED:write_tools_enabled",
        ]
        if unknown_environment
        else []
    )


@pytest.mark.parametrize(
    ("group", "field", "value"),
    [
        ("independence", "not_candidate_author", False),
        ("independence", "read_only_candidate", False),
        ("independence", "fresh_session", False),
        ("independence", "first_pass_before_author_narrative", False),
        ("independence", "distinct_account", True),
        ("independence", "distinct_account", 0),
        ("isolation", "session_id", " "),
        ("isolation", "session_id", 123),
        ("isolation", "forked", True),
        ("isolation", "forked", None),
        ("isolation", "sandbox_mode", "danger-full-access"),
        ("isolation", "persistent_memory", True),
        ("isolation", "persistent_memory", 0),
        ("isolation", "write_tools_enabled", True),
        ("isolation", "write_tools_enabled", "false"),
    ],
)
def test_isolated_profile_refuses_false_independence_or_violated_isolation(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    group: str,
    field: str,
    value: object,
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    isolated[group][field] = value
    with pytest.raises(AuditError):
        gate(repository, epoch, isolated, audit_profile="isolated-session")


@pytest.mark.parametrize("group", ["independence", "isolation"])
@pytest.mark.parametrize("change", ["missing", "extra", "not-an-object"])
def test_isolated_profile_requires_exact_metadata_fields(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    group: str,
    change: str,
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    if change == "missing":
        isolated[group].pop(next(iter(isolated[group])))
    elif change == "extra":
        isolated[group]["invented"] = False
    else:
        isolated[group] = None
    with pytest.raises(AuditError):
        gate(repository, epoch, isolated, audit_profile="isolated-session")


@pytest.mark.parametrize("field", ["forked", "persistent_memory", "write_tools_enabled"])
def test_isolated_profile_refuses_missing_environment_attestations(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], field: str
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    del isolated["isolation"][field]
    with pytest.raises(AuditError, match="isolation"):
        gate(repository, epoch, isolated, audit_profile="isolated-session")


def test_strict_profile_still_refuses_same_account_and_nonnull_isolation(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["independence"]["distinct_account"] = False
    with pytest.raises(AuditError, match="distinct_account"):
        gate(repository, epoch, receipt)
    receipt["independence"]["distinct_account"] = True
    receipt["isolation"] = _isolated_receipt(receipt)["isolation"]
    with pytest.raises(AuditError, match="isolation"):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize("claimed_profile", ["separate-account", "invented", None])
def test_isolated_profile_cannot_be_forged_or_selected_by_the_receipt(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], claimed_profile: object
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    isolated["audit_profile"] = claimed_profile
    with pytest.raises(AuditError, match="audit_profile"):
        gate(repository, epoch, isolated, audit_profile="isolated-session")


def test_legacy_v3_is_not_relabelled_or_admitted_under_a_v4_epoch(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    receipt["schema"] = "hoklims/latent-compass:independent-audit/3"
    with pytest.raises(AuditError, match="schema"):
        gate(repository, epoch, receipt)


@pytest.mark.parametrize("failure", ["claims", "invocations", "mutation", "verdict", "blockers"])
def test_isolated_profile_preserves_existing_fail_closed_proof_rules(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]], failure: str
) -> None:
    repository, epoch, receipt = audited_repository
    isolated = _isolated_receipt(receipt)
    if failure == "claims":
        isolated["claims"] = []
    elif failure == "invocations":
        isolated["claims"][0]["invocation_paths"] = ["local"]
    elif failure == "mutation":
        isolated["claims"][0]["witness"]["targets"][0]["before"] = "not the candidate"
    elif failure == "verdict":
        isolated["verdict"] = "PROOF_WEAK"
    else:
        isolated["unresolved_blockers"] = ["unresolved"]
    with pytest.raises(AuditError):
        gate(repository, epoch, isolated, audit_profile="isolated-session")


@pytest.mark.parametrize(
    ("profile", "exit_code"), [(None, 1), ("isolated-session", 0), ("invented", 2)]
)
def test_cli_profile_selection_is_external_and_defaults_to_strict(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    profile: str | None,
    exit_code: int,
) -> None:
    repository, epoch, receipt = audited_repository
    epoch_path = tmp_path / "epoch.json"
    receipt_path = tmp_path / "receipt.json"
    epoch_path.write_text(json.dumps(epoch), encoding="utf-8")
    receipt_path.write_text(json.dumps(_isolated_receipt(receipt)), encoding="utf-8")
    arguments = [
        sys.executable,
        str(ROOT / "tools" / "independent_audit.py"),
        "gate",
        "--repository",
        str(repository),
        "--epoch",
        str(epoch_path),
        "--receipt",
        str(receipt_path),
    ]
    if profile is not None:
        arguments += ["--audit-profile", profile]
    result = subprocess.run(  # noqa: S603 - fixed CLI and fixture-only argv
        arguments, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == exit_code
    if profile is None:
        assert json.loads(result.stderr)["decision"] == "BLOCK"
        assert "audit_profile" in json.loads(result.stderr)["error"]
    elif profile == "isolated-session":
        decision = json.loads(result.stdout)
        assert decision["verdict"] == "PROOF_ADEQUATE_WITH_LIMITS"
        assert decision["limits"] == ["SAME_ACCOUNT_ISOLATED_REVIEW"]
    else:
        assert "invalid choice" in result.stderr


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


def _commit_subject_test(
    fixture: tuple[Path, dict[str, Any], dict[str, Any]],
    before: str,
    after: str,
    expected: str = "tests/test_subject.py::test_subject_is_pristine",
    targets: list[str] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repository, old_epoch, receipt = fixture
    relative = "tests/test_subject.py"
    (repository / relative).write_text(before, encoding="utf-8", newline="")
    _git(repository, "add", relative)
    _git(repository, "commit", "--quiet", "-m", "subject oracle fixture")
    epoch = cast(dict[str, Any], create_epoch(repository, old_epoch["base_sha"], "HEAD"))
    for key in ("epoch_digest", "policy_digest", "head_sha"):
        receipt[key] = epoch[key]
    witness = receipt["claims"][0]["witness"]
    witness["expected_failure"] = expected
    witness["pytest_targets"] = targets or [expected]
    witness["targets"] = [
        {
            "path": relative,
            "before": before,
            "after": after,
            "before_digest": digest(before.encode()),
            "after_digest": digest(after.encode()),
        }
    ]
    return repository, epoch, receipt


@pytest.mark.parametrize("mentions_node", [False, True])
def test_gate_refuses_collection_errors_even_when_the_expected_node_is_printed(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    mentions_node: bool,
) -> None:
    before = "def test_subject_is_pristine():\n    assert True\n"
    message = (
        "tests/test_subject.py::test_subject_is_pristine" if mentions_node else "import failed"
    )
    fixture = _commit_subject_test(
        audited_repository,
        before,
        f"raise RuntimeError({message!r})\n" + before,
    )
    with pytest.raises(AuditError):
        gate(*fixture)


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_gate_refuses_fixture_errors_despite_pytest_exit_one(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    phase: str,
) -> None:
    before = "import pytest\n\ndef test_subject_is_pristine():\n    assert True\n"
    body = (
        "    raise RuntimeError('setup failed')\n    yield\n"
        if phase == "setup"
        else "    yield\n    raise RuntimeError('teardown failed')\n"
    )
    after = "import pytest\n\n@pytest.fixture(autouse=True)\ndef bad_fixture():\n" + body + before
    fixture = _commit_subject_test(audited_repository, before, after)
    with pytest.raises(AuditError):
        gate(*fixture)


def test_gate_refuses_an_unrelated_failure_printing_the_expected_node(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = (
        "def test_subject_is_pristine():\n    assert True\n\n"
        "def test_unrelated():\n    assert True\n"
    )
    after = before.replace(
        "def test_unrelated():\n    assert True",
        "def test_unrelated():\n    assert False, "
        "'tests/test_subject.py::test_subject_is_pristine'",
    )
    fixture = _commit_subject_test(
        audited_repository,
        before,
        after,
        targets=[
            "tests/test_subject.py::test_subject_is_pristine",
            "tests/test_subject.py::test_unrelated",
        ],
    )
    with pytest.raises(AuditError):
        gate(*fixture)


@pytest.mark.parametrize("mark", ["skip", "xfail"])
def test_gate_refuses_skip_or_xfail_only_restoration(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    mark: str,
) -> None:
    before = (
        "import pytest\n\n@pytest.mark." + mark + "\n"
        "def test_subject_is_pristine():\n    assert False\n"
    )
    after = "def test_subject_is_pristine():\n    assert False\n"
    fixture = _commit_subject_test(audited_repository, before, after)
    with pytest.raises(AuditError):
        gate(*fixture)


def test_gate_accepts_executed_qualified_and_parameterized_assertion_failures(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = (
        "import pytest\n\nclass TestSubject:\n"
        "    @pytest.mark.parametrize('value', [1, 2])\n"
        "    def test_value(self, value):\n        assert value > 0\n"
    )
    after = before.replace("value > 0", "value < 0")
    fixture = _commit_subject_test(
        audited_repository,
        before,
        after,
        "tests/test_subject.py::TestSubject::test_value",
    )
    assert gate(*fixture)["decision"] == "ALLOW"


@pytest.mark.parametrize("damage", ["missing", "malformed", "stale"])
def test_gate_refuses_execution_reports_damaged_by_a_real_child(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
    damage: str,
) -> None:
    before = "def test_subject_is_pristine():\n    assert True\n"
    actions = {
        "missing": "report.unlink(missing_ok=True)",
        "malformed": "report.write_text('not-json', encoding='utf-8')",
        "stale": (
            "data = json.loads(report.read_text()); data['nonce'] = 'stale'; "
            "report.write_text(json.dumps(data), encoding='utf-8')"
        ),
    }
    after = (
        "import atexit, json\nfrom pathlib import Path\n\n"
        "def test_subject_is_pristine(pytestconfig):\n"
        "    report = Path(pytestconfig.getoption('--latent-audit-report'))\n"
        "    def damage():\n        " + actions[damage] + "\n"
        "    atexit.register(damage)\n    assert False\n"
    )
    fixture = _commit_subject_test(audited_repository, before, after)
    with pytest.raises(AuditError, match="report"):
        gate(*fixture)


def test_gate_accepts_a_specific_parameter_node(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = (
        "import pytest\n\n@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_subject_is_pristine(value):\n    assert value > 0\n"
    )
    fixture = _commit_subject_test(
        audited_repository,
        before,
        before.replace("value > 0", "value < 0"),
        "tests/test_subject.py::test_subject_is_pristine[1]",
    )
    assert gate(*fixture)["decision"] == "ALLOW"


def test_gate_refuses_traversal_before_an_outside_test_can_execute(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, epoch, receipt = audited_repository
    marker = repository.parent / "outside-test-executed"
    outside = repository.parent / "outside.py"
    outside.write_text(
        "from pathlib import Path\n\ndef test_subject_is_pristine():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n    assert False\n",
        encoding="utf-8",
    )
    witness = receipt["claims"][0]["witness"]
    traversal = "tests/" + "../" * 32 + outside.relative_to(outside.anchor).as_posix()
    witness["pytest_targets"] = [traversal + "::test_subject_is_pristine"]
    witness["expected_failure"] = witness["pytest_targets"][0]
    with pytest.raises(AuditError):
        gate(repository, epoch, receipt)
    assert not marker.exists(), "outside test executed before the gate rejected traversal"


def test_gate_refuses_a_symlink_mode_pytest_node_before_execution(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, old_epoch, receipt = audited_repository
    relative = "tests/test_subject.py"
    blob = _git(repository, "rev-parse", f"HEAD:{relative}")
    _git(repository, "update-index", "--cacheinfo", f"120000,{blob},{relative}")
    _git(repository, "commit", "--quiet", "-m", "noneligible node mode")
    epoch = cast(dict[str, Any], create_epoch(repository, old_epoch["base_sha"], "HEAD"))
    for key in ("epoch_digest", "policy_digest", "head_sha"):
        receipt[key] = epoch[key]
    with pytest.raises(AuditError, match="regular candidate blob"):
        gate(repository, epoch, receipt)


def test_gate_accepts_multiple_declared_call_failures_when_expected_leaf_fails(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = (
        "def test_subject_is_pristine():\n    assert True\n\ndef test_second():\n    assert True\n"
    )
    fixture = _commit_subject_test(
        audited_repository,
        before,
        before.replace("assert True", "assert False"),
        targets=[
            "tests/test_subject.py::test_subject_is_pristine",
            "tests/test_subject.py::test_second",
        ],
    )
    assert gate(*fixture)["decision"] == "ALLOW"


def test_gate_refuses_a_class_prefix_as_the_expected_failed_leaf(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = "class TestSubject:\n    def test_value(self):\n        assert True\n"
    fixture = _commit_subject_test(
        audited_repository,
        before,
        before.replace("assert True", "assert False"),
        "tests/test_subject.py::TestSubject",
        ["tests/test_subject.py::TestSubject::test_value"],
    )
    with pytest.raises(AuditError, match="leaf"):
        gate(*fixture)


def test_gate_bootstraps_the_canonical_reporter_not_candidate_modules(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    repository, old_epoch, receipt = audited_repository
    for relative in ("pytest.py", "_latent_audit_reporter.py"):
        (repository / relative).write_text(
            "raise RuntimeError('candidate evaluator module must not execute')\n",
            encoding="utf-8",
        )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "candidate evaluator collision")
    epoch = cast(dict[str, Any], create_epoch(repository, old_epoch["base_sha"], "HEAD"))
    for key in ("epoch_digest", "policy_digest", "head_sha"):
        receipt[key] = epoch[key]
    assert gate(repository, epoch, receipt)["decision"] == "ALLOW"


def test_gate_preserves_a_false_xfail_condition_with_real_executed_passes(
    audited_repository: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    before = (
        "import pytest\n\n@pytest.mark.xfail(False, reason='disabled marker')\n"
        "def test_subject_is_pristine():\n    assert True\n"
    )
    fixture = _commit_subject_test(
        audited_repository,
        before,
        before.replace("assert True", "assert False"),
    )
    assert gate(*fixture)["decision"] == "ALLOW"


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
    verify = job(workflow(ROOT / ".github" / "workflows" / "verify.yml"), "verify")
    assert_run(
        verify,
        "Exercise independent audit gate fail-closed tests",
        "uv run --frozen pytest -o addopts='' -q tests/test_independent_audit.py",
    )

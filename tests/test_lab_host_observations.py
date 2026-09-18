"""HOK-800 — hostile coverage for the shared observation envelope.

Every verification test drives real ``tmp_path`` reads through the confined
reader. No test here runs Git, a language server or a test runner: the host
executor is simulated by hand-built records, which is exactly the trust
boundary under test — what a host declares versus what the lab re-reads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.host_observations import (
    LAB_READER_TOOL_ID,
    Conclusiveness,
    HostObservation,
    HostObservationViolation,
    ObservationMode,
    ObservationStatus,
    admit_host_observation,
    admit_observation_policy,
    conclusiveness_of,
    observation_from_byte_observation,
    observation_from_literal_report,
    question_digest,
    verify_host_observation,
)
from latent_compass.lab.observations import (
    HostBinding,
    SourceSnapshot,
    capture_source_snapshot,
    observe_literal_matches,
    observe_source_file,
)

CAPTURED_AT = "2026-09-19T00:00:00Z"
OBSERVED_AT = "2026-09-19T00:00:05Z"
VERIFIED_AT = "2026-09-19T00:00:09Z"
HOST_ID = "host-alpha"
ROOT_ID = "root-alpha"
HOST = HostBinding(host_id=HOST_ID, agent_family=AgentFamily.CLAUDE)
GIT_HEAD = "6ec94e86f5b6c429fd264708f28d5105b19fbafa"

MODULE_A = "def handler():\n    return retry_policy()\n"
MODULE_B = "def other():\n    return 1\n"
BASE_LIMITS = ["COVERAGE_IS_DECLARED_PATHS_ONLY"]


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(root: Path, name: str, text: str) -> None:
    (root / name).write_bytes(text.encode("utf-8"))


def source_root(tmp_path: Path) -> Path:
    write(tmp_path, "module_a.py", MODULE_A)
    write(tmp_path, "module_b.py", MODULE_B)
    return tmp_path


def capture(root: Path) -> SourceSnapshot:
    return capture_source_snapshot(
        root,
        [Path("module_a.py"), Path("module_b.py")],
        host=HOST,
        root_id=ROOT_ID,
        captured_at=CAPTURED_AT,
        max_bytes_per_file=4096,
        declared_git_head=GIT_HEAD,
    )


def policy(mode: str = "SOURCE_AND_SYMBOLIC", retired: tuple[str, ...] = ()) -> Any:
    return admit_observation_policy(
        {"contract_version": "1.0.0", "mode": mode, "retired_providers": list(retired)}
    )


def location(name: str, text: str, start: int, end: int, **extra: object) -> dict[str, object]:
    return {
        "relative_path": name,
        "byte_digest": digest(text),
        "size_bytes": len(text.encode("utf-8")),
        "line_start": start,
        "line_end": end,
        "generated": False,
        **extra,
    }


def record(**overrides: object) -> dict[str, object]:
    """A conclusive symbol navigation that found one reference in ``module_a.py``."""
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "observation_id": "observation-one",
        "kind": "SYMBOL_NAVIGATION",
        "probe_id": "find-direct-references",
        "question_digest": question_digest("retry_policy"),
        "host": {"host_id": HOST_ID, "agent_family": "claude"},
        "root_id": ROOT_ID,
        "mode": "SOURCE_AND_SYMBOLIC",
        "executed_by": "HOST_EXECUTOR",
        "tool": {"tool_id": "pyright", "version": "1.1.400", "provider_id": "language-server"},
        "providers_used": ["language-server"],
        "declared_git_head": GIT_HEAD,
        "worktree_dirty": False,
        "observed_at": OBSERVED_AT,
        "status": "OBSERVED",
        "covered_paths": ["module_a.py", "module_b.py"],
        "evidence": [location("module_a.py", MODULE_A, 2, 2)],
        "git_diff": None,
        "symbol": {"relation": "REFERENCES", "language": "python"},
        "check": None,
        "duration_ms": 120,
        "observed_cost": 2,
        "resources": {
            "child_processes": 1,
            "files_read": 2,
            "repeated_reads": 0,
            "internal_index_used": True,
            "cache_used": None,
        },
        "limits": [*BASE_LIMITS, "DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED"],
    }
    payload.update(overrides)
    return payload


def git_diff_record(**overrides: object) -> dict[str, object]:
    payload = record(
        kind="GIT_DIFF",
        probe_id="changed-since-base",
        mode="SOURCE_ONLY",
        tool={"tool_id": "git", "version": "2.50.1", "provider_id": "git"},
        providers_used=["git"],
        worktree_dirty=True,
        evidence=[],
        symbol=None,
        git_diff={
            "base_identity": GIT_HEAD,
            "entries": [
                {
                    "relative_path": "module_a.py",
                    "change": "MODIFIED",
                    "post_image_digest": digest(MODULE_A),
                    "post_image_size": len(MODULE_A.encode("utf-8")),
                }
            ],
        },
        resources={
            "child_processes": 1,
            "files_read": 0,
            "repeated_reads": 0,
            "internal_index_used": False,
            "cache_used": False,
        },
        limits=[*BASE_LIMITS, "GIT_IDENTITY_IS_DECLARED"],
    )
    payload.update(overrides)
    return payload


def check_record(**overrides: object) -> dict[str, object]:
    payload = record(
        kind="TARGETED_CHECK",
        probe_id="run-focused-test",
        mode="SOURCE_ONLY",
        tool={"tool_id": "pytest", "version": "8.4.1", "provider_id": "test-runner"},
        providers_used=["test-runner"],
        evidence=[],
        symbol=None,
        check={
            "command_digest": "sha256:" + "c" * 64,
            "verdict": "FAILED",
            "exit_code": 1,
            "log_digest": "sha256:" + "d" * 64,
            "log_bytes": 512,
        },
        resources={
            "child_processes": 1,
            "files_read": 2,
            "repeated_reads": 0,
            "internal_index_used": False,
            "cache_used": False,
        },
        limits=[*BASE_LIMITS, "CHECK_VERDICT_IS_HOST_DECLARED"],
    )
    payload.update(overrides)
    return payload


def file_read_record(**overrides: object) -> dict[str, object]:
    """A host that read ``module_a.py`` and concluded something from it."""
    payload = record(
        kind="FILE_READ",
        probe_id="read-implementation",
        mode="SOURCE_ONLY",
        tool={"tool_id": "host-file-reader", "version": "1.0.0", "provider_id": "source"},
        providers_used=["source"],
        covered_paths=["module_a.py"],
        evidence=[location("module_a.py", MODULE_A, 1, 2)],
        symbol=None,
        interpreted_outcome_id="handler-retries",
        resources={
            "child_processes": 0,
            "files_read": 1,
            "repeated_reads": 0,
            "internal_index_used": False,
            "cache_used": False,
        },
        limits=[*BASE_LIMITS, "OUTCOME_IS_HOST_INTERPRETED"],
    )
    payload.update(overrides)
    return payload


def verify(root: Path, observation: HostObservation, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "snapshot": capture(root),
        "policy": policy(),
        "expected_host": HOST,
        "expected_root_id": ROOT_ID,
        "verified_at": VERIFIED_AT,
    }
    arguments.update(overrides)
    return verify_host_observation(root, observation, **arguments)


def reason_of(violation: HostObservationViolation) -> object:
    assert isinstance(violation.detail, dict)
    return violation.detail["reason"]


def refusal_reason(root: Path, observation: HostObservation, **overrides: Any) -> object:
    with pytest.raises(HostObservationViolation) as refusal:
        verify(root, observation, **overrides)
    return reason_of(refusal.value)


# --- the envelope itself ------------------------------------------------------


def test_every_kind_admits_in_the_one_shared_shape() -> None:
    admitted = [
        admit_host_observation(record()),
        admit_host_observation(git_diff_record()),
        admit_host_observation(check_record()),
    ]
    assert {item.kind.value for item in admitted} == {
        "SYMBOL_NAVIGATION",
        "GIT_DIFF",
        "TARGETED_CHECK",
    }
    # One field set for every kind: what differs is carried in the kind's own detail.
    assert len({frozenset(item.canonical_payload()) for item in admitted}) == 1


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "EMPTY", "evidence": []}, "EMPTY_IS_NOT_ABSENCE"),
        ({"status": "TRUNCATED"}, "OUTPUT_TRUNCATED"),
        ({"limits": [*BASE_LIMITS, "TOOL_INTERNAL_INDEX_USED"]}, "DIRECT_RELATIONS_ONLY"),
        ({"limits": [*BASE_LIMITS, "DIRECT_RELATIONS_ONLY"]}, "TOOL_INTERNAL_INDEX_USED"),
        ({"limits": ["DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED"]}, "COVERAGE_IS"),
        (
            {"evidence": [location("module_a.py", MODULE_A, 2, 2, generated=True)]},
            "GENERATED_CODE_IN_EVIDENCE",
        ),
    ],
)
def test_a_record_that_omits_a_mandatory_limit_is_refused(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(HostObservationViolation) as refusal:
        admit_host_observation(record(**overrides))
    assert expected in str(refusal.value.detail)


def test_an_empty_result_is_admitted_only_with_its_limit_and_no_evidence() -> None:
    limits = [*BASE_LIMITS, "DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED"]
    empty = record(status="EMPTY", evidence=[], limits=[*limits, "EMPTY_IS_NOT_ABSENCE"])
    assert admit_host_observation(empty).status is ObservationStatus.EMPTY

    with pytest.raises(HostObservationViolation):
        admit_host_observation(dict(empty, evidence=[location("module_a.py", MODULE_A, 1, 1)]))


@pytest.mark.parametrize("status", ["TIMEOUT", "TOOL_ABSENT", "UNSUPPORTED_LANGUAGE", "FAILED"])
def test_a_failed_acquisition_is_recorded_and_carries_no_result(status: str) -> None:
    failed = admit_host_observation(record(status=status, evidence=[]))
    assert conclusiveness_of(failed) is Conclusiveness.NONE

    with pytest.raises(HostObservationViolation):
        admit_host_observation(record(status=status))


def test_a_check_that_did_not_run_declares_no_verdict_and_an_error_is_not_a_failure() -> None:
    timed_out = check_record(
        status="TIMEOUT",
        check={
            "command_digest": "sha256:" + "c" * 64,
            "verdict": None,
            "exit_code": None,
            "log_digest": None,
            "log_bytes": None,
        },
    )
    assert conclusiveness_of(admit_host_observation(timed_out)) is Conclusiveness.NONE
    with pytest.raises(HostObservationViolation):
        admit_host_observation(check_record(status="TIMEOUT"))

    errored = check_record()
    assert isinstance(errored["check"], dict)
    errored["check"] = dict(errored["check"], verdict="ERRORED", exit_code=4)
    assert admit_host_observation(errored).check is not None
    for verdict, exit_code in (("PASSED", 1), ("FAILED", 0), ("ERRORED", 0)):
        broken = check_record()
        assert isinstance(broken["check"], dict)
        broken["check"] = dict(broken["check"], verdict=verdict, exit_code=exit_code)
        with pytest.raises(HostObservationViolation):
            admit_host_observation(broken)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"kind": "GIT_DIFF"}, "carries exactly its own detail"),
        ({"symbol": None}, "carries exactly its own detail"),
        ({"check": {"command_digest": "sha256:" + "c" * 64}}, "carries exactly its own detail"),
        ({"covered_paths": ["module_b.py", "module_a.py"]}, "must be sorted"),
        ({"covered_paths": ["module_b.py"]}, "outside the declared coverage"),
        ({"providers_used": ["graphify"]}, "must include the declared tool's own provider"),
        ({"executed_by": "LAB_CONFINED_READER"}, "only reads files and literals"),
        (
            {"status": "UNSUPPORTED_LANGUAGE", "kind": "FILE_READ", "symbol": None, "evidence": []},
            "only a symbol navigation can report an unsupported language",
        ),
        ({"approved": True}, "extra_forbidden"),
    ],
)
def test_an_incoherent_record_is_refused_for_its_own_reason(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(HostObservationViolation) as refusal:
        admit_host_observation(record(**overrides))
    assert expected in str(refusal.value.detail)


def test_source_only_mode_admits_no_symbolic_tool_and_no_unaccounted_index() -> None:
    assert admit_host_observation(git_diff_record()).mode is ObservationMode.SOURCE_ONLY

    # Isolate the kind rule: this symbolic record declares no internal index,
    # so nothing but its kind can refuse it in SOURCE_ONLY mode.
    no_index = {
        "child_processes": 1,
        "files_read": 2,
        "repeated_reads": 0,
        "internal_index_used": False,
        "cache_used": False,
    }
    indexless = record(resources=no_index, limits=[*BASE_LIMITS, "DIRECT_RELATIONS_ONLY"])
    assert admit_host_observation(indexless).mode is ObservationMode.SOURCE_AND_SYMBOLIC
    with pytest.raises(HostObservationViolation) as symbolic:
        admit_host_observation(dict(indexless, mode="SOURCE_ONLY"))
    assert "SOURCE_ONLY mode admits no symbol navigation" in str(symbolic.value.detail)

    unaccounted = git_diff_record()
    assert isinstance(unaccounted["resources"], dict)
    unaccounted["resources"] = dict(unaccounted["resources"], internal_index_used=None)
    with pytest.raises(HostObservationViolation) as unknown_index:
        admit_host_observation(unaccounted)
    assert "requires internal_index_used to be false" in str(unknown_index.value.detail)


def test_an_interpreted_outcome_is_a_flagged_host_reading_of_a_file_and_nothing_else() -> None:
    interpreted = admit_host_observation(file_read_record())
    assert interpreted.interpreted_outcome_id == "handler-retries"

    with pytest.raises(HostObservationViolation) as unflagged:
        admit_host_observation(file_read_record(limits=BASE_LIMITS))
    assert "OUTCOME_IS_HOST_INTERPRETED" in str(unflagged.value.detail)

    symbolic = [
        *BASE_LIMITS,
        "DIRECT_RELATIONS_ONLY",
        "TOOL_INTERNAL_INDEX_USED",
        "OUTCOME_IS_HOST_INTERPRETED",
    ]
    for refused in (
        record(interpreted_outcome_id="handler-retries", limits=symbolic),
        file_read_record(executed_by="LAB_CONFINED_READER"),
        file_read_record(
            status="EMPTY",
            evidence=[],
            limits=[*BASE_LIMITS, "OUTCOME_IS_HOST_INTERPRETED", "EMPTY_IS_NOT_ABSENCE"],
        ),
    ):
        with pytest.raises(HostObservationViolation) as refusal:
            admit_host_observation(refused)
        assert "may carry an interpreted outcome" in str(refusal.value.detail)


def test_the_seal_binds_the_tool_identity_and_every_other_field() -> None:
    original = admit_host_observation(record())
    upgraded = admit_host_observation(
        record(tool={"tool_id": "pyright", "version": "1.1.401", "provider_id": "language-server"})
    )
    assert original.observation_seal() != upgraded.observation_seal()
    assert original.observation_seal() == admit_host_observation(record()).observation_seal()


# --- verification against the snapshot ------------------------------------------


def test_a_conclusive_observation_is_re_read_and_verified(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    observation = admit_host_observation(record())
    report = verify(root, observation, anchor="retry_policy")

    assert report.conclusiveness is Conclusiveness.FULL
    assert report.verified_paths == ("module_a.py", "module_b.py")
    assert report.verified_location_count == 1
    assert report.observation_seal == observation.observation_seal()
    assert report.snapshot_manifest_seal == capture(root).manifest_seal()


def test_a_mutation_between_observation_and_consumption_invalidates_it(tmp_path: Path) -> None:
    """The declared Git HEAD never changes here; only the bytes do."""
    root = source_root(tmp_path)
    snapshot = capture(root)
    observation = admit_host_observation(record())
    assert verify(root, observation, snapshot=snapshot).conclusiveness is Conclusiveness.FULL

    # A covered file that carries no evidence at all is still bound.
    write(root, "module_b.py", MODULE_B + "# edited after the observation\n")
    assert refusal_reason(root, observation, snapshot=snapshot) == "drift_since_snapshot"
    assert observation.declared_git_head == snapshot.declared_git_head


def test_evidence_the_tool_read_from_other_bytes_is_refused(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    stale = record(evidence=[location("module_a.py", "def handler():\n    pass\n", 2, 2)])
    assert refusal_reason(root, admit_host_observation(stale)) == "evidence_digest_mismatch"


def test_a_cited_line_must_exist_and_carry_the_declared_anchor(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    beyond = record(evidence=[location("module_a.py", MODULE_A, 2, 9)])
    assert refusal_reason(root, admit_host_observation(beyond)) == "evidence_line_out_of_range"

    observation = admit_host_observation(record(evidence=[location("module_a.py", MODULE_A, 1, 1)]))
    assert verify(root, observation, anchor="handler").verified_location_count == 1
    reason = refusal_reason(root, observation, anchor="retry_policy")
    assert reason == "anchor_absent_from_evidence"


def test_a_refusal_never_echoes_the_anchor_text(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    observation = admit_host_observation(record(evidence=[location("module_a.py", MODULE_A, 1, 1)]))
    with pytest.raises(HostObservationViolation) as refusal:
        verify(root, observation, anchor="a-private-symbol-name")
    assert "a-private-symbol-name" not in str(refusal.value.as_dict())


def test_a_covered_path_outside_the_snapshot_is_refused(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    write(root, "module_c.py", "x = 1\n")
    outside = record(covered_paths=["module_a.py", "module_c.py"])
    assert refusal_reason(root, admit_host_observation(outside)) == "path_outside_snapshot"


def test_a_git_diff_is_bound_to_the_content_on_disk_not_to_the_declared_head(
    tmp_path: Path,
) -> None:
    root = source_root(tmp_path)
    snapshot = capture(root)
    source_only = policy(mode="SOURCE_ONLY")
    observation = admit_host_observation(git_diff_record())
    report = verify(root, observation, snapshot=snapshot, policy=source_only)
    assert (report.conclusiveness, report.verified_location_count) == (Conclusiveness.FULL, 1)

    forged = git_diff_record()
    assert isinstance(forged["git_diff"], dict)
    forged["git_diff"] = dict(
        forged["git_diff"],
        entries=[
            {
                "relative_path": "module_a.py",
                "change": "MODIFIED",
                "post_image_digest": digest("something else\n"),
                "post_image_size": len(b"something else\n"),
            }
        ],
    )
    reason = refusal_reason(
        root, admit_host_observation(forged), snapshot=snapshot, policy=source_only
    )
    assert reason == "diff_post_image_mismatch"


def test_a_retired_provider_is_refused_with_no_hidden_fallback(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    through_index = admit_host_observation(record(providers_used=["graphify", "language-server"]))
    assert verify(root, through_index).conclusiveness is Conclusiveness.FULL

    retired = policy(retired=("cocoindex-code", "graphify"))
    assert refusal_reason(root, through_index, policy=retired) == "retired_provider_used"
    # The same policy still admits an observation that did not touch the provider.
    clean = admit_host_observation(record())
    assert verify(root, clean, policy=retired).conclusiveness is Conclusiveness.FULL


def test_the_session_mode_is_the_callers_and_a_record_cannot_override_it(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    symbolic = admit_host_observation(record())
    assert refusal_reason(root, symbolic, policy=policy(mode="SOURCE_ONLY")) == "mode_mismatch"


def test_a_non_conclusive_observation_is_admitted_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = source_root(tmp_path)
    snapshot = capture(root)
    failed = admit_host_observation(record(status="TOOL_ABSENT", evidence=[]))

    def unexpected_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a non-conclusive observation reached the filesystem")

    monkeypatch.setattr("latent_compass.lab.host_observations.read_confined_file", unexpected_read)
    # Live trap: the patched reader really is what a conclusive record would call.
    with pytest.raises(pytest.fail.Exception):
        verify(root, admit_host_observation(record()), snapshot=snapshot)

    report = verify(root, failed, snapshot=snapshot)
    assert report.conclusiveness is Conclusiveness.NONE
    assert (report.verified_paths, report.verified_location_count) == ((), 0)


def test_scope_and_chronology_are_checked_before_anything_is_read(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    observation = admit_host_observation(record())
    other_host = HostBinding(host_id=HOST_ID, agent_family=AgentFamily.CODEX)

    assert refusal_reason(root, observation, expected_host=other_host) == "scope_mismatch"
    assert refusal_reason(root, observation, expected_root_id="root-beta") == "scope_mismatch"
    early = admit_host_observation(record(observed_at="2026-09-18T23:59:59Z"))
    assert refusal_reason(root, early) == "observation_predates_snapshot"
    assert (
        refusal_reason(root, observation, verified_at="2026-09-19T00:00:01Z")
        == "observation_postdates_verification"
    )
    with pytest.raises(HostObservationViolation):
        verify(root, observation, verified_at="2026-09-19 00:00:09")


def test_verification_revalidates_a_forged_record(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    forged = admit_host_observation(record()).model_copy(update={"limits": ()})
    with pytest.raises(HostObservationViolation):
        verify(root, forged)


# --- the lab's own reads, in the same envelope ----------------------------------


def test_the_labs_literal_search_wraps_into_the_shared_envelope(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    targets = [Path("module_a.py"), Path("module_b.py")]

    def search(query: str) -> HostObservation:
        report = observe_literal_matches(
            root,
            targets,
            query,
            host=HOST,
            root_id=ROOT_ID,
            observed_at=OBSERVED_AT,
            max_bytes_per_file=4096,
            max_matches_per_file=10,
            declared_git_head=GIT_HEAD,
        )
        return observation_from_literal_report(
            report,
            observation_id="observation-literal",
            probe_id="literal-search",
            mode=ObservationMode.SOURCE_ONLY,
        )

    found = search("retry_policy")
    assert found.status is ObservationStatus.OBSERVED
    assert [(item.relative_path, item.line_start) for item in found.evidence] == [
        ("module_a.py", 2)
    ]
    assert (found.tool.tool_id, found.resources.child_processes) == (LAB_READER_TOOL_ID, 0)
    assert found.question_digest == question_digest("retry_policy")
    source_only = policy(mode="SOURCE_ONLY")
    assert verify(root, found, policy=source_only, anchor="retry_policy").conclusiveness is (
        Conclusiveness.FULL
    )

    missing = search("no-such-identifier")
    assert missing.status is ObservationStatus.EMPTY
    assert "EMPTY_IS_NOT_ABSENCE" in {code.value for code in missing.limits}


def test_a_hosts_empty_literal_search_is_checked_against_the_source_itself(
    tmp_path: Path,
) -> None:
    """The one emptiness the lab can verify: it holds the bytes and the query."""
    root = source_root(tmp_path)
    source_only = policy(mode="SOURCE_ONLY")

    def empty_search(query: str) -> HostObservation:
        return admit_host_observation(
            record(
                kind="LITERAL_SEARCH",
                probe_id="literal-search",
                question_digest=question_digest(query),
                mode="SOURCE_ONLY",
                tool={"tool_id": "ripgrep", "version": "14.1.1", "provider_id": "source"},
                providers_used=["source"],
                status="EMPTY",
                evidence=[],
                symbol=None,
                resources={
                    "child_processes": 1,
                    "files_read": 2,
                    "repeated_reads": 0,
                    "internal_index_used": False,
                    "cache_used": False,
                },
                limits=[*BASE_LIMITS, "EMPTY_IS_NOT_ABSENCE"],
            )
        )

    honest = empty_search("no-such-identifier")
    report = verify(root, honest, policy=source_only, anchor="no-such-identifier")
    assert report.conclusiveness is Conclusiveness.FULL

    false_empty = empty_search("retry_policy")
    reason = refusal_reason(root, false_empty, policy=source_only, anchor="retry_policy")
    assert reason == "empty_contradicted_by_source"


def test_a_binary_file_in_literal_coverage_is_refused_never_called_empty(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    report = observe_literal_matches(
        tmp_path,
        [Path("blob.bin")],
        "anything",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=OBSERVED_AT,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )
    with pytest.raises(HostObservationViolation) as refusal:
        observation_from_literal_report(
            report,
            observation_id="observation-binary",
            probe_id="literal-search",
            mode=ObservationMode.SOURCE_ONLY,
        )
    assert reason_of(refusal.value) == "binary_or_invalid_utf8"


def test_the_labs_file_read_wraps_into_the_shared_envelope(tmp_path: Path) -> None:
    root = source_root(tmp_path)
    (root / "empty.py").write_bytes(b"")

    def read(name: str) -> HostObservation:
        observed = observe_source_file(
            root,
            Path(name),
            host=HOST,
            root_id=ROOT_ID,
            observed_at=OBSERVED_AT,
            max_bytes=4096,
        )
        return observation_from_byte_observation(
            observed,
            observation_id="observation-read",
            probe_id="read-implementation",
            question="what does the handler return?",
            mode=ObservationMode.SOURCE_ONLY,
        )

    whole = read("module_a.py")
    assert whole.status is ObservationStatus.OBSERVED
    assert [(item.line_start, item.line_end) for item in whole.evidence] == [(1, 2)]
    assert read("empty.py").status is ObservationStatus.EMPTY

"""Hostile tests for the HOK-800 bounded source-observation API."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import latent_compass.confined_io as confined_io
from latent_compass.confined_io import read_confined_file
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation
from latent_compass.lab.observations import (
    MAX_MANIFEST_FILES,
    DriftStatus,
    FileRevalidationStatus,
    HostBinding,
    SourceEncoding,
    SourceObservationViolation,
    SourceSnapshot,
    capture_source_snapshot,
    observe_literal_matches,
    observe_source_file,
    revalidate_source_snapshot,
)

HOST = HostBinding(host_id="host-alpha", agent_family=AgentFamily.CLAUDE)
OTHER_HOST = HostBinding(host_id="host-beta", agent_family=AgentFamily.CLAUDE)
ROOT_ID = "root-alpha"
OTHER_ROOT_ID = "root-beta"
WHEN = "2026-09-18T00:00:00Z"
LATER = "2026-09-18T01:00:00Z"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    return location


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:  # pragma: no cover - environment without link privilege
        pytest.skip(f"cannot create a symlink in this environment: {exc}")


def test_observe_source_file_reads_utf8_text_with_stable_multibyte_line_numbers(
    root: Path,
) -> None:
    content = "première ligne\nseconde ligne avec émoji 😀\ntroisième\n".encode()
    _write(root, "src/greeting.py", content)

    observation = observe_source_file(
        root,
        Path("src/greeting.py"),
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes=4096,
    )

    assert observation.encoding is SourceEncoding.UTF8_TEXT
    assert observation.line_count == 3
    assert observation.size_bytes == len(content)
    assert observation.byte_digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert observation.relative_path == "src/greeting.py"


def test_observe_source_file_reports_binary_explicitly_and_grants_text(root: Path) -> None:
    _write(root, "binary.bin", b"\xff\xfe\x00\x01broken-utf8")
    _write(root, "text.txt", b"hello\n")

    binary_observation = observe_source_file(
        root, Path("binary.bin"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
    )
    text_observation = observe_source_file(
        root, Path("text.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
    )

    assert binary_observation.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8
    assert binary_observation.line_count is None
    assert text_observation.encoding is SourceEncoding.UTF8_TEXT
    assert text_observation.line_count == 1


@pytest.mark.parametrize(
    "escape",
    [Path("../outside.txt"), Path("nested/../../outside.txt")],
)
def test_observe_source_file_refuses_relative_traversal_outside_root(
    root: Path, escape: Path
) -> None:
    (root.parent / "outside.txt").write_bytes(b"secret")

    with pytest.raises(ContractViolation):
        observe_source_file(
            root, escape, host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
        )


def test_observe_source_file_refuses_absolute_path_outside_root(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")

    with pytest.raises(ContractViolation):
        observe_source_file(
            root, outside, host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
        )


def test_observe_source_file_refuses_the_root_itself(root: Path) -> None:
    with pytest.raises(ContractViolation):
        observe_source_file(
            root, root, host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
        )


def test_observe_source_file_refuses_oversized_and_grants_file_at_the_limit(root: Path) -> None:
    _write(root, "sized.txt", b"x" * 10)

    observation = observe_source_file(
        root, Path("sized.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=10
    )
    assert observation.size_bytes == 10

    with pytest.raises(ContractViolation):
        observe_source_file(
            root, Path("sized.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=9
        )


def test_observe_source_file_never_creates_a_missing_parent_directory(root: Path) -> None:
    with pytest.raises(ContractViolation):
        observe_source_file(
            root,
            Path("does-not-exist/file.txt"),
            host=HOST,
            root_id=ROOT_ID,
            observed_at=WHEN,
            max_bytes=4096,
        )

    assert not (root / "does-not-exist").exists()


def test_observe_source_file_refuses_a_directory_and_grants_the_sibling_file(root: Path) -> None:
    (root / "a-directory").mkdir()
    _write(root, "a-file.txt", b"content")

    with pytest.raises(ContractViolation):
        observe_source_file(
            root, Path("a-directory"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
        )
    observe_source_file(
        root, Path("a-file.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
    )


def test_read_confined_file_refuses_a_symlinked_file(root: Path) -> None:
    real = root.parent / "real-secret.txt"
    real.write_bytes(b"secret")
    link = root / "linked.txt"
    _symlink_or_skip(link, real)

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "linked.txt", max_bytes=4096, what="test file")


def test_read_confined_file_refuses_a_symlinked_root_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    _symlink_or_skip(linked_parent, real_parent, target_is_directory=True)
    root = linked_parent / "root"
    (real_parent / "root").mkdir()
    _write(real_parent / "root", "file.txt", b"content")

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "file.txt", max_bytes=4096, what="test file")


@pytest.mark.skipif(os.name == "nt", reason="requires os.mkfifo")
def test_read_confined_file_refuses_a_fifo_without_hanging(root: Path) -> None:
    fifo_path = root / "pipe"
    # ``os.mkfifo`` is not in the Windows type stubs; looked up dynamically so
    # this file still type-checks under a Windows mypy run.
    getattr(os, "mkfifo")(fifo_path)  # noqa: B009

    with pytest.raises(ContractViolation):
        read_confined_file(root, fifo_path, max_bytes=4096, what="test fifo")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only reserved device name")
def test_observe_source_file_refuses_a_windows_reserved_name(root: Path) -> None:
    with pytest.raises(ContractViolation):
        observe_source_file(
            root, Path("CON"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only alternate-data-stream syntax")
def test_observe_source_file_refuses_an_alternate_data_stream_path(root: Path) -> None:
    _write(root, "plain.txt", b"content")

    with pytest.raises(ContractViolation):
        observe_source_file(
            root,
            Path("plain.txt:hidden"),
            host=HOST,
            root_id=ROOT_ID,
            observed_at=WHEN,
            max_bytes=4096,
        )
    observe_source_file(
        root, Path("plain.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only UNC path rejection")
def test_observe_source_file_refuses_a_unc_style_target(root: Path) -> None:
    with pytest.raises(ContractViolation):
        observe_source_file(
            root,
            Path(r"\\server\share\file.txt"),
            host=HOST,
            root_id=ROOT_ID,
            observed_at=WHEN,
            max_bytes=4096,
        )


def test_capture_source_snapshot_refuses_a_duplicate_declared_relative_path(root: Path) -> None:
    _write(root, "a.txt", b"aaa")

    with pytest.raises(SourceObservationViolation):
        capture_source_snapshot(
            root,
            [Path("a.txt"), Path("./a.txt")],
            host=HOST,
            root_id=ROOT_ID,
            captured_at=WHEN,
            max_bytes_per_file=4096,
        )


def _snapshot(root: Path) -> SourceSnapshot:
    return capture_source_snapshot(
        root,
        [Path("a.txt"), Path("b.txt")],
        host=HOST,
        root_id=ROOT_ID,
        captured_at=WHEN,
        max_bytes_per_file=4096,
    )


def test_snapshot_revalidation_reports_unchanged_when_nothing_changed(root: Path) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)

    revalidation = revalidate_source_snapshot(
        root, snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER
    )

    assert revalidation.status is DriftStatus.UNCHANGED
    assert {result.status for result in revalidation.results} == {FileRevalidationStatus.UNCHANGED}
    assert revalidation.snapshot_seal == snapshot.manifest_seal()


def test_snapshot_revalidation_detects_a_mutated_file(root: Path) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)
    _write(root, "a.txt", b"mutated")

    revalidation = revalidate_source_snapshot(
        root, snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER
    )

    assert revalidation.status is DriftStatus.DRIFTED
    by_path = {result.relative_path: result for result in revalidation.results}
    assert by_path["a.txt"].status is FileRevalidationStatus.MUTATED
    assert by_path["b.txt"].status is FileRevalidationStatus.UNCHANGED


def test_snapshot_revalidation_detects_a_missing_file(root: Path) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)
    (root / "b.txt").unlink()

    revalidation = revalidate_source_snapshot(
        root, snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER
    )

    assert revalidation.status is DriftStatus.DRIFTED
    by_path = {result.relative_path: result for result in revalidation.results}
    assert by_path["b.txt"].status is FileRevalidationStatus.MISSING
    assert by_path["b.txt"].current_byte_digest is None
    assert by_path["b.txt"].refusal_reason is not None


def test_snapshot_revalidation_detects_addition_and_removal_in_a_redeclared_manifest(
    root: Path,
) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    _write(root, "c.txt", b"ccc")
    snapshot = _snapshot(root)

    revalidation = revalidate_source_snapshot(
        root,
        snapshot,
        host=HOST,
        root_id=ROOT_ID,
        revalidated_at=LATER,
        current_manifest_targets=[Path("a.txt"), Path("c.txt")],
    )

    assert revalidation.status is DriftStatus.DRIFTED
    assert revalidation.added_relative_paths == ("c.txt",)
    assert revalidation.removed_relative_paths == ("b.txt",)
    assert {result.relative_path for result in revalidation.results} == {"a.txt"}


def test_snapshot_revalidation_refuses_a_host_mismatch_and_grants_the_matching_host(
    root: Path,
) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)

    with pytest.raises(SourceObservationViolation):
        revalidate_source_snapshot(
            root, snapshot, host=OTHER_HOST, root_id=ROOT_ID, revalidated_at=LATER
        )
    revalidate_source_snapshot(root, snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER)


def test_snapshot_revalidation_refuses_a_root_id_mismatch(root: Path) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)

    with pytest.raises(SourceObservationViolation):
        revalidate_source_snapshot(
            root, snapshot, host=HOST, root_id=OTHER_ROOT_ID, revalidated_at=LATER
        )


def test_observe_literal_matches_reports_bounded_locations_and_truncation(root: Path) -> None:
    content = "\n".join(f"needle at line {index}" for index in range(1, 6)).encode()
    _write(root, "haystack.py", content)

    report = observe_literal_matches(
        root,
        [Path("haystack.py")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=3,
    )

    result = report.results[0]
    assert result.match_count == 5
    assert result.line_numbers == (1, 2, 3)
    assert result.truncated is True


def test_observe_literal_matches_reports_no_match_only_for_the_file_actually_searched(
    root: Path,
) -> None:
    _write(root, "clean.py", b"nothing interesting here\n")
    _write(root, "unsearched.py", b"needle needle needle\n")

    report = observe_literal_matches(
        root,
        [Path("clean.py")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )

    assert [result.relative_path for result in report.results] == ["clean.py"]
    assert report.results[0].match_count == 0
    assert report.results[0].truncated is False


def test_observe_literal_matches_marks_a_binary_file_as_skipped_not_a_zero_match(
    root: Path,
) -> None:
    _write(root, "binary.bin", b"\xff\xfe\x00needle\x01")

    report = observe_literal_matches(
        root,
        [Path("binary.bin")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )

    result = report.results[0]
    assert result.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8
    assert result.match_count == 0
    assert result.line_numbers == ()
    assert result.byte_digest == "sha256:" + hashlib.sha256(b"\xff\xfe\x00needle\x01").hexdigest()
    assert result.size_bytes == len(b"\xff\xfe\x00needle\x01")


def test_observe_literal_matches_refuses_an_empty_declared_file_list(root: Path) -> None:
    with pytest.raises(SourceObservationViolation):
        observe_literal_matches(
            root,
            [],
            "needle",
            host=HOST,
            root_id=ROOT_ID,
            observed_at=WHEN,
            max_bytes_per_file=4096,
            max_matches_per_file=10,
        )


def test_observe_literal_matches_digest_changes_when_nonmatching_bytes_change_but_locations_do_not(
    root: Path,
) -> None:
    first = b"needle here\nsecond line AAA\n"
    second = b"needle here\nsecond line ZZZ\n"
    _write(root, "haystack.py", first)

    first_report = observe_literal_matches(
        root,
        [Path("haystack.py")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )
    _write(root, "haystack.py", second)
    second_report = observe_literal_matches(
        root,
        [Path("haystack.py")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )

    first_result = first_report.results[0]
    second_result = second_report.results[0]
    assert first_result.line_numbers == second_result.line_numbers == (1,)
    assert first_result.match_count == second_result.match_count == 1
    assert first_result.byte_digest != second_result.byte_digest
    assert first_result.byte_digest == "sha256:" + hashlib.sha256(first).hexdigest()
    assert second_result.byte_digest == "sha256:" + hashlib.sha256(second).hexdigest()


def test_observe_literal_matches_reports_distinct_digests_for_files_with_the_same_locations(
    root: Path,
) -> None:
    _write(root, "one.py", b"needle in file one\n")
    _write(root, "two.py", b"needle in file two\n")

    report = observe_literal_matches(
        root,
        [Path("one.py"), Path("two.py")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )

    by_path = {result.relative_path: result for result in report.results}
    assert by_path["one.py"].line_numbers == by_path["two.py"].line_numbers == (1,)
    assert by_path["one.py"].byte_digest != by_path["two.py"].byte_digest


def test_literal_match_result_identity_matches_snapshot_manifest_for_the_same_file(
    root: Path,
) -> None:
    _write(root, "a.txt", b"needle only here\n")
    snapshot = capture_source_snapshot(
        root,
        [Path("a.txt")],
        host=HOST,
        root_id=ROOT_ID,
        captured_at=WHEN,
        max_bytes_per_file=4096,
    )

    report = observe_literal_matches(
        root,
        [Path("a.txt")],
        "needle",
        host=HOST,
        root_id=ROOT_ID,
        observed_at=WHEN,
        max_bytes_per_file=4096,
        max_matches_per_file=10,
    )

    identity = snapshot.manifest[0]
    result = report.results[0]
    assert result.byte_digest == identity.byte_digest
    assert result.size_bytes == identity.size_bytes


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX os.fstat")
def test_read_confined_file_refuses_a_premature_end_of_file_on_posix(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "short.txt", b"short")
    real_fstat = os.fstat
    calls = {"count": 0}

    def lying_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        calls["count"] += 1
        if calls["count"] == 1:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size + 1000,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                ),
                {"st_mtime_ns": result.st_mtime_ns},
            )
        return result

    monkeypatch.setattr(os, "fstat", lying_fstat)

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "short.txt", max_bytes=4096, what="test file")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX os.fstat")
def test_read_confined_file_refuses_a_file_whose_size_changes_during_the_read_on_posix(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "steady.txt", b"steady content")
    real_fstat = os.fstat
    calls = {"count": 0}

    def lying_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        calls["count"] += 1
        if calls["count"] >= 2:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                ),
                {"st_mtime_ns": result.st_mtime_ns + 1},
            )
        return result

    monkeypatch.setattr(os, "fstat", lying_fstat)

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "steady.txt", max_bytes=4096, what="test file")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only handle metadata race")
def test_read_refuses_windows_size_drift_even_when_write_time_is_unchanged(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(root, "growth.txt", b"before")
    original = cast(
        Callable[[int, Path], int],
        getattr(confined_io, "_file_size_windows"),  # noqa: B009 - Windows-only symbol
    )
    reads = 0

    def changing_size(handle: int, path: Path) -> int:
        nonlocal reads
        reads += 1
        return original(handle, path) + (1 if reads > 1 else 0)

    monkeypatch.setattr(confined_io, "_file_size_windows", changing_size)
    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "growth.txt", max_bytes=4096, what="growth test")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only handle metadata race")
def test_read_confined_file_refuses_a_premature_end_of_file_on_windows(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "short.txt", b"short")
    real_file_size = cast(
        Callable[[int, Path], int],
        getattr(confined_io, "_file_size_windows"),  # noqa: B009 - Windows-only symbol
    )
    calls = {"count": 0}

    def lying_size(handle: int, path: Path) -> int:
        calls["count"] += 1
        size = real_file_size(handle, path)
        if calls["count"] == 1:
            return size + 1000
        return size

    monkeypatch.setattr(confined_io, "_file_size_windows", lying_size)

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "short.txt", max_bytes=4096, what="test file")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only handle metadata race")
def test_read_confined_file_refuses_a_file_whose_write_time_changes_during_the_read_on_windows(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "steady.txt", b"steady content")
    calls = {"count": 0}

    def lying_write_time(handle: int, path: Path) -> int:
        calls["count"] += 1
        return calls["count"]

    monkeypatch.setattr(confined_io, "_file_last_write_time_windows", lying_write_time)

    with pytest.raises(ContractViolation):
        read_confined_file(root, root / "steady.txt", max_bytes=4096, what="test file")


def test_revalidate_source_snapshot_refuses_an_oversized_current_manifest_before_touching_any_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)

    def _explode(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("must not read any file before the manifest size is checked")

    monkeypatch.setattr("latent_compass.lab.observations.read_confined_file", _explode)
    oversized = [Path("a.txt")] * (MAX_MANIFEST_FILES + 1)

    with pytest.raises(SourceObservationViolation):
        revalidate_source_snapshot(
            root,
            snapshot,
            host=HOST,
            root_id=ROOT_ID,
            revalidated_at=LATER,
            current_manifest_targets=oversized,
        )


def test_observe_source_file_refuses_a_host_binding_constructed_to_bypass_validation(
    root: Path,
) -> None:
    _write(root, "a.txt", b"aaa")
    bypassed_host = HostBinding.model_construct(
        host_id="not a valid identifier!!", agent_family=AgentFamily.CLAUDE
    )

    with pytest.raises(SourceObservationViolation):
        observe_source_file(
            root,
            Path("a.txt"),
            host=bypassed_host,
            root_id=ROOT_ID,
            observed_at=WHEN,
            max_bytes=4096,
        )
    observe_source_file(
        root, Path("a.txt"), host=HOST, root_id=ROOT_ID, observed_at=WHEN, max_bytes=4096
    )


def test_revalidate_source_snapshot_refuses_a_snapshot_mutated_past_the_manifest_bound(
    root: Path,
) -> None:
    _write(root, "a.txt", b"aaa")
    _write(root, "b.txt", b"bbb")
    snapshot = _snapshot(root)
    identity = snapshot.manifest[0]
    oversized_manifest = tuple(
        identity.model_copy(update={"relative_path": f"file-{index}.txt"})
        for index in range(MAX_MANIFEST_FILES + 1)
    )
    bypassed_snapshot = snapshot.model_construct(
        **{**snapshot.__dict__, "manifest": oversized_manifest}
    )

    with pytest.raises(SourceObservationViolation):
        revalidate_source_snapshot(
            root, bypassed_snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER
        )
    revalidate_source_snapshot(root, snapshot, host=HOST, root_id=ROOT_ID, revalidated_at=LATER)

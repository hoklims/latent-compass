"""HOK-800 — bounded, read-only, source-only observations.

This module answers one narrow question: *what bytes actually sit at this
relative path, beneath this explicit trusted root, right now?* It never scans
a root, never starts a provider, never touches the network, and never persists
raw source. It only reads files the caller names, bounded in size and count,
through :func:`latent_compass.confined_io.read_confined_file` — the same
handle-relative, non-following containment the writer uses, so a symlink or
Windows reparse point planted anywhere between the root and the target is
refused rather than followed.

What this module is not
------------------------
It is not a CLI command, not a persistence layer, and not a controller. It
does not compare an observation against an empirical outcome, and it does not
authorise anything: reading a file here grants no capability to act on it.

Contract shape
--------------
Every object here is a closed, versioned, frozen pydantic model under
``SOURCE_OBSERVATION_CONTRACT_VERSION``. Three kinds exist:

* :class:`SourceByteObservation` — one file's digest, size, encoding and line
  count, as read at one instant.
* :class:`LiteralMatchReport` — bounded literal-substring line locations,
  counts and truncation flags across an explicit list of files.
* :class:`SourceSnapshot` / :class:`SourceSnapshotRevalidation` — a finite,
  caller-declared manifest of file identities, and a later bounded reread that
  detects mutation, removal, or a change to the declared manifest itself.

Every report is bound to a caller-supplied :class:`HostBinding` and
``root_id``. Revalidating a snapshot under a different host or root is refused
outright, before any file is reread.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass.canonical import seal
from latent_compass.confined_io import plan_confined_target, read_confined_file
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation

__all__ = [
    "MAX_MANIFEST_FILES",
    "MAX_MATCHES_PER_FILE",
    "MAX_QUERY_BYTES",
    "MAX_SOURCE_FILE_BYTES",
    "SOURCE_OBSERVATION_CONTRACT_VERSION",
    "SUPPORTED_SOURCE_OBSERVATION_VERSIONS",
    "DriftStatus",
    "FileRevalidationResult",
    "FileRevalidationStatus",
    "HostBinding",
    "LiteralMatchFileResult",
    "LiteralMatchReport",
    "SourceByteObservation",
    "SourceEncoding",
    "SourceFileIdentity",
    "SourceObservationViolation",
    "SourceSnapshot",
    "SourceSnapshotRevalidation",
    "capture_source_snapshot",
    "observe_literal_matches",
    "observe_source_file",
    "revalidate_source_snapshot",
]

#: Versioned on its own axis: this contract embeds no episode, decision,
#: reconciliation or benchmark shape and must be free to move independently.
SOURCE_OBSERVATION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_SOURCE_OBSERVATION_VERSIONS: Final = frozenset({SOURCE_OBSERVATION_CONTRACT_VERSION})

BYTE_OBSERVATION_SEAL_DOMAIN: Final = "lab.source-byte-observation.v1"
SNAPSHOT_MANIFEST_SEAL_DOMAIN: Final = "lab.source-snapshot.manifest.v1"
LITERAL_MATCH_SEAL_DOMAIN: Final = "lab.literal-match-report.v1"

#: Hard ceilings. The caller-supplied limits below are refused above these,
#: regardless of what the caller asks for; nothing here is negotiable upward.
MAX_SOURCE_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_MANIFEST_FILES: Final = 4096
MAX_MATCHES_PER_FILE: Final = 10_000
MAX_QUERY_BYTES: Final = 4096


class SourceObservationViolation(ContractViolation):
    """A HOK-800 source observation contract rule was broken."""

    code = "source_observation_violation"


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


def _require_relative_posix(value: str) -> str:
    _no_control_characters(value)
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute():
        raise ValueError("relative path must not be absolute")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError("relative path must not contain '.', '..' or an empty segment")
    return value


RelativeSourcePath = Annotated[
    str,
    Field(min_length=1, max_length=4096),
    AfterValidator(_require_relative_posix),
]
DeclaredGitHead = Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")]
RefusalReason = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_no_control_characters)
]


def _sha256_digest(data: bytes) -> str:
    """A plain, non-domain-separated content hash of exact bytes read.

    Deliberately not a :func:`latent_compass.canonical.seal`: a byte digest
    must be reproducible by an independent ``sha256sum`` over the same bytes,
    which a domain-separated seal is not designed to be.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _require_bounded_positive(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise SourceObservationViolation(
            f"{name} must be a positive integer no greater than {maximum}",
            detail={"name": name, "received": repr(value), "maximum": maximum},
        )
    return value


def _confined_candidate(root: Path, target: Path) -> Path:
    """Join ``target`` onto ``root`` the way this module's contract requires.

    :mod:`latent_compass.confined_io` treats a bare relative ``target`` as
    resolving against the process's working directory, matching ordinary path
    semantics for its other callers (see ``cli.py``). This module's contract is
    narrower and explicit: ``target`` is always relative to the named trusted
    ``root``. Joining here keeps that meaning local to this module without
    changing ``confined_io``'s existing contract. An already-absolute
    ``target`` passes through unchanged and is still checked for containment
    below.
    """
    return root / target


def _relative_posix(root: Path, target: Path, *, what: str) -> str:
    """Prove ``target`` lies beneath ``root`` and return its posix-style relative path.

    Uses the same lexical, non-following containment check the confined reader
    itself applies; this only derives the identifier used to label the result,
    it grants no additional trust.
    """
    absolute_root = Path(os.path.abspath(root))  # noqa: PTH100 - must not follow links
    absolute_target = plan_confined_target(
        absolute_root, _confined_candidate(root, target), what=what
    )
    return Path(os.path.relpath(absolute_target, absolute_root)).as_posix()


class SourceEncoding(StrEnum):
    """Whether the exact bytes read decode as UTF-8 text."""

    UTF8_TEXT = "UTF8_TEXT"
    BINARY_OR_INVALID_UTF8 = "BINARY_OR_INVALID_UTF8"


class HostBinding(StrictModel):
    """The caller-declared host and agent family a report is bound to."""

    host_id: Identifier
    agent_family: AgentFamily


class SourceByteObservation(StrictModel):
    """One file's exact-byte digest, size, encoding and line count."""

    contract_version: str = Field(min_length=5, max_length=20)
    host: HostBinding
    root_id: Identifier
    declared_git_head: DeclaredGitHead | None = Field(default=None)
    observed_at: Timestamp
    relative_path: RelativeSourcePath
    byte_digest: Seal
    size_bytes: int = Field(ge=0, le=MAX_SOURCE_FILE_BYTES)
    encoding: SourceEncoding
    line_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_SOURCE_OBSERVATION_VERSIONS,
            "source byte observation",
        )
        if self.encoding is SourceEncoding.UTF8_TEXT and self.line_count is None:
            raise ValueError("a UTF-8 text observation must report a line count")
        if self.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8 and self.line_count is not None:
            raise ValueError("a binary or undecodable observation must not report a line count")
        return self

    def observation_seal(self) -> str:
        """Reproducible seal over the whole observation, in its own domain."""
        return seal(BYTE_OBSERVATION_SEAL_DOMAIN, self.canonical_payload())


class LiteralMatchFileResult(StrictModel):
    """Bounded literal-substring match locations for one declared file.

    ``byte_digest`` and ``size_bytes`` are computed from the exact bytes read
    for matching, for both encodings. A sealed report therefore changes when
    the file's non-matching bytes change even if every match location stays
    the same, and the pair is directly comparable to a
    :class:`SourceFileIdentity` in a :class:`SourceSnapshot` manifest for the
    same relative path.
    """

    relative_path: RelativeSourcePath
    encoding: SourceEncoding
    byte_digest: Seal
    size_bytes: int = Field(ge=0, le=MAX_SOURCE_FILE_BYTES)
    match_count: int = Field(ge=0)
    line_numbers: tuple[int, ...] = Field(max_length=MAX_MATCHES_PER_FILE)
    truncated: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if any(number <= 0 for number in self.line_numbers):
            raise ValueError("line numbers are 1-based and must be positive")
        if self.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8:
            if self.match_count != 0 or self.line_numbers or self.truncated:
                raise ValueError(
                    "a binary or undecodable file reports no matches, no lines and no truncation"
                )
        else:
            if len(self.line_numbers) > self.match_count:
                raise ValueError("line_numbers cannot exceed the reported match_count")
            if not self.truncated and len(self.line_numbers) != self.match_count:
                raise ValueError("an untruncated result must report every matching line")
            if self.truncated and len(self.line_numbers) >= self.match_count:
                raise ValueError("a truncated result must report fewer lines than match_count")
        return self


class LiteralMatchReport(StrictModel):
    """Bounded literal-substring matches across an explicit, finite file list."""

    contract_version: str = Field(min_length=5, max_length=20)
    host: HostBinding
    root_id: Identifier
    declared_git_head: DeclaredGitHead | None = Field(default=None)
    observed_at: Timestamp
    query_digest: Seal
    max_matches_per_file: int = Field(ge=1, le=MAX_MATCHES_PER_FILE)
    results: tuple[LiteralMatchFileResult, ...] = Field(min_length=1, max_length=MAX_MANIFEST_FILES)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_SOURCE_OBSERVATION_VERSIONS, "literal match report"
        )
        paths = [result.relative_path for result in self.results]
        if len(set(paths)) != len(paths):
            raise ValueError("a literal match report must not repeat a relative path")
        return self

    def report_seal(self) -> str:
        return seal(LITERAL_MATCH_SEAL_DOMAIN, self.canonical_payload())


class SourceFileIdentity(StrictModel):
    """One file's declared identity inside a snapshot manifest."""

    relative_path: RelativeSourcePath
    byte_digest: Seal
    size_bytes: int = Field(ge=0, le=MAX_SOURCE_FILE_BYTES)


class SourceSnapshot(StrictModel):
    """A finite, caller-declared manifest of file identities, captured once.

    The manifest is never an implicit whole-root scan: it is exactly the list
    of files the caller named. Nothing here claims anything about a file
    outside this manifest.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    host: HostBinding
    root_id: Identifier
    declared_git_head: DeclaredGitHead | None = Field(default=None)
    captured_at: Timestamp
    max_bytes_per_file: int = Field(ge=1, le=MAX_SOURCE_FILE_BYTES)
    manifest: tuple[SourceFileIdentity, ...] = Field(min_length=1, max_length=MAX_MANIFEST_FILES)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_SOURCE_OBSERVATION_VERSIONS, "source snapshot"
        )
        paths = [identity.relative_path for identity in self.manifest]
        if len(set(paths)) != len(paths):
            raise ValueError("a snapshot manifest must not repeat a relative path")
        if paths != sorted(paths):
            raise ValueError("a snapshot manifest must be sorted by relative_path")
        return self

    def manifest_seal(self) -> str:
        """Digest of the ordered file identities. Not the whole snapshot payload."""
        return seal(
            SNAPSHOT_MANIFEST_SEAL_DOMAIN,
            [identity.canonical_payload() for identity in self.manifest],
        )


class FileRevalidationStatus(StrEnum):
    UNCHANGED = "UNCHANGED"
    MUTATED = "MUTATED"
    MISSING = "MISSING"


class DriftStatus(StrEnum):
    UNCHANGED = "UNCHANGED"
    DRIFTED = "DRIFTED"


class FileRevalidationResult(StrictModel):
    """The outcome of rereading one file declared in both compared manifests."""

    relative_path: RelativeSourcePath
    status: FileRevalidationStatus
    previous_byte_digest: Seal
    current_byte_digest: Seal | None = Field(default=None)
    refusal_reason: RefusalReason | None = Field(default=None)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status is FileRevalidationStatus.MISSING:
            if self.current_byte_digest is not None:
                raise ValueError("a missing file must not report a current digest")
            if self.refusal_reason is None:
                raise ValueError("a missing file must record why it could not be reread")
        else:
            if self.current_byte_digest is None:
                raise ValueError(f"a {self.status.value} file must report a current digest")
            if self.refusal_reason is not None:
                raise ValueError(f"a {self.status.value} file must not record a refusal reason")
        return self


class SourceSnapshotRevalidation(StrictModel):
    """A bounded reread of exactly the files a prior snapshot declared.

    Host and root scope are checked *before* any file is reread: a mismatch is
    refused outright rather than produced as a soft field on the report.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    host: HostBinding
    root_id: Identifier
    snapshot_seal: Seal
    revalidated_at: Timestamp
    results: tuple[FileRevalidationResult, ...] = Field(max_length=MAX_MANIFEST_FILES)
    added_relative_paths: tuple[RelativeSourcePath, ...] = Field(max_length=MAX_MANIFEST_FILES)
    removed_relative_paths: tuple[RelativeSourcePath, ...] = Field(max_length=MAX_MANIFEST_FILES)
    status: DriftStatus

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_SOURCE_OBSERVATION_VERSIONS,
            "source snapshot revalidation",
        )
        drifted = (
            bool(self.added_relative_paths)
            or bool(self.removed_relative_paths)
            or any(result.status is not FileRevalidationStatus.UNCHANGED for result in self.results)
        )
        expected = DriftStatus.DRIFTED if drifted else DriftStatus.UNCHANGED
        if self.status is not expected:
            raise ValueError("status does not match the observed per-file results")
        return self


def _revalidated[ModelT: StrictModel](instance: ModelT, *, context: str) -> ModelT:
    """Re-run full contract validation on a publicly received model instance.

    ``model_construct`` and ``model_copy`` both bypass validation even under
    :data:`~latent_compass.contracts.STRICT_CONFIG`'s
    ``revalidate_instances="always"``: a caller-supplied :class:`HostBinding`
    or :class:`SourceSnapshot` must not be trusted as-is before every field's
    bounds, patterns and cross-field invariants have been checked the same
    way a payload read from disk would be. A normally constructed instance
    round-trips unchanged.
    """
    try:
        payload = instance.model_dump(mode="json")
    except Exception as exc:
        raise SourceObservationViolation(
            f"{context} could not be serialised for revalidation",
            detail={"context": context, "cause": str(exc)},
        ) from exc
    return validate_contract(
        type(instance), payload, error=SourceObservationViolation, context=context
    )


def observe_source_file(
    root: Path,
    target: Path,
    *,
    host: HostBinding,
    root_id: str,
    observed_at: str,
    max_bytes: int,
    declared_git_head: str | None = None,
) -> SourceByteObservation:
    """Read one confined file and report its digest, size, encoding and lines.

    ``declared_git_head`` is asserted by the caller, never verified against an
    actual repository: this module opens no subprocess and no ``.git`` state.
    """
    host = _revalidated(host, context="host binding")
    _require_bounded_positive(max_bytes, name="max_bytes", maximum=MAX_SOURCE_FILE_BYTES)
    relative_path = _relative_posix(root, target, what="source file to observe")
    data = read_confined_file(
        root,
        _confined_candidate(root, target),
        max_bytes=max_bytes,
        what="source file to observe",
    )
    try:
        text: str | None = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        encoding = SourceEncoding.BINARY_OR_INVALID_UTF8
        line_count = None
    else:
        encoding = SourceEncoding.UTF8_TEXT
        line_count = len(text.splitlines())
    payload = {
        "contract_version": SOURCE_OBSERVATION_CONTRACT_VERSION,
        "host": host.canonical_payload(),
        "root_id": root_id,
        "declared_git_head": declared_git_head,
        "observed_at": observed_at,
        "relative_path": relative_path,
        "byte_digest": _sha256_digest(data),
        "size_bytes": len(data),
        "encoding": encoding.value,
        "line_count": line_count,
    }
    return validate_contract(
        SourceByteObservation,
        payload,
        error=SourceObservationViolation,
        context="source byte observation",
    )


def _literal_line_matches(
    text: str, query: str, *, max_matches: int
) -> tuple[int, tuple[int, ...], bool]:
    matching_lines = [
        index for index, line in enumerate(text.splitlines(), start=1) if query in line
    ]
    total = len(matching_lines)
    reported = tuple(matching_lines[:max_matches])
    truncated = total > len(reported)
    return total, reported, truncated


def observe_literal_matches(
    root: Path,
    targets: Sequence[Path],
    query: str,
    *,
    host: HostBinding,
    root_id: str,
    observed_at: str,
    max_bytes_per_file: int,
    max_matches_per_file: int,
    declared_git_head: str | None = None,
) -> LiteralMatchReport:
    """Search an explicit, finite file list for a literal substring.

    Coverage is exactly ``targets``: absence of a match is reported only for
    the files actually read, never as a claim about anything outside them.
    """
    host = _revalidated(host, context="host binding")
    _require_bounded_positive(
        max_bytes_per_file, name="max_bytes_per_file", maximum=MAX_SOURCE_FILE_BYTES
    )
    _require_bounded_positive(
        max_matches_per_file, name="max_matches_per_file", maximum=MAX_MATCHES_PER_FILE
    )
    if not targets:
        raise SourceObservationViolation(
            "a literal match observation must declare at least one file",
            detail={"root_id": root_id},
        )
    if len(targets) > MAX_MANIFEST_FILES:
        raise SourceObservationViolation(
            "a literal match observation declares too many files",
            detail={"declared": len(targets), "maximum": MAX_MANIFEST_FILES},
        )
    query_bytes = query.encode("utf-8")
    if not query_bytes or len(query_bytes) > MAX_QUERY_BYTES:
        raise SourceObservationViolation(
            "a literal match query must be non-empty and bounded",
            detail={"query_bytes": len(query_bytes), "maximum": MAX_QUERY_BYTES},
        )
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for target in targets:
        relative_path = _relative_posix(root, target, what="source file to search")
        if relative_path in seen:
            raise SourceObservationViolation(
                "declared coverage must not repeat a relative path",
                detail={"relative_path": relative_path},
            )
        seen.add(relative_path)
        data = read_confined_file(
            root,
            _confined_candidate(root, target),
            max_bytes=max_bytes_per_file,
            what="source file to search",
        )
        file_digest = _sha256_digest(data)
        file_size = len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            results.append(
                {
                    "relative_path": relative_path,
                    "encoding": SourceEncoding.BINARY_OR_INVALID_UTF8.value,
                    "byte_digest": file_digest,
                    "size_bytes": file_size,
                    "match_count": 0,
                    "line_numbers": [],
                    "truncated": False,
                }
            )
            continue
        total, reported, truncated = _literal_line_matches(
            text, query, max_matches=max_matches_per_file
        )
        results.append(
            {
                "relative_path": relative_path,
                "encoding": SourceEncoding.UTF8_TEXT.value,
                "byte_digest": file_digest,
                "size_bytes": file_size,
                "match_count": total,
                "line_numbers": list(reported),
                "truncated": truncated,
            }
        )
    payload = {
        "contract_version": SOURCE_OBSERVATION_CONTRACT_VERSION,
        "host": host.canonical_payload(),
        "root_id": root_id,
        "declared_git_head": declared_git_head,
        "observed_at": observed_at,
        "query_digest": _sha256_digest(query_bytes),
        "max_matches_per_file": max_matches_per_file,
        "results": results,
    }
    return validate_contract(
        LiteralMatchReport,
        payload,
        error=SourceObservationViolation,
        context="literal match report",
    )


def capture_source_snapshot(
    root: Path,
    targets: Sequence[Path],
    *,
    host: HostBinding,
    root_id: str,
    captured_at: str,
    max_bytes_per_file: int,
    declared_git_head: str | None = None,
) -> SourceSnapshot:
    """Capture identities for an explicit, finite manifest of files."""
    host = _revalidated(host, context="host binding")
    _require_bounded_positive(
        max_bytes_per_file, name="max_bytes_per_file", maximum=MAX_SOURCE_FILE_BYTES
    )
    if not targets:
        raise SourceObservationViolation(
            "a snapshot must declare at least one file", detail={"root_id": root_id}
        )
    if len(targets) > MAX_MANIFEST_FILES:
        raise SourceObservationViolation(
            "a snapshot declares too many files",
            detail={"declared": len(targets), "maximum": MAX_MANIFEST_FILES},
        )
    identities: list[dict[str, object]] = []
    seen: set[str] = set()
    for target in targets:
        observation = observe_source_file(
            root,
            target,
            host=host,
            root_id=root_id,
            observed_at=captured_at,
            max_bytes=max_bytes_per_file,
            declared_git_head=declared_git_head,
        )
        if observation.relative_path in seen:
            raise SourceObservationViolation(
                "a snapshot manifest must not repeat a relative path",
                detail={"relative_path": observation.relative_path},
            )
        seen.add(observation.relative_path)
        identities.append(
            {
                "relative_path": observation.relative_path,
                "byte_digest": observation.byte_digest,
                "size_bytes": observation.size_bytes,
            }
        )
    identities.sort(key=lambda identity: str(identity["relative_path"]))
    payload = {
        "contract_version": SOURCE_OBSERVATION_CONTRACT_VERSION,
        "host": host.canonical_payload(),
        "root_id": root_id,
        "declared_git_head": declared_git_head,
        "captured_at": captured_at,
        "max_bytes_per_file": max_bytes_per_file,
        "manifest": identities,
    }
    return validate_contract(
        SourceSnapshot, payload, error=SourceObservationViolation, context="source snapshot"
    )


def revalidate_source_snapshot(
    root: Path,
    snapshot: SourceSnapshot,
    *,
    host: HostBinding,
    root_id: str,
    revalidated_at: str,
    current_manifest_targets: Sequence[Path] | None = None,
) -> SourceSnapshotRevalidation:
    """Reread exactly the files a snapshot declared and report drift.

    A host or root mismatch is refused before any file is touched. Passing
    ``current_manifest_targets`` compares a freshly declared file list against
    the snapshot's own manifest and reports additions and removals of
    *declared coverage*; it never scans the filesystem to find them.
    """
    host = _revalidated(host, context="host binding")
    snapshot = _revalidated(snapshot, context="source snapshot")
    if host != snapshot.host or root_id != snapshot.root_id:
        raise SourceObservationViolation(
            "revalidation host or root scope does not match the snapshot",
            detail={
                "snapshot_root_id": snapshot.root_id,
                "requested_root_id": root_id,
            },
        )
    if current_manifest_targets is not None and len(current_manifest_targets) > MAX_MANIFEST_FILES:
        raise SourceObservationViolation(
            "a revalidation manifest declares too many files",
            detail={"declared": len(current_manifest_targets), "maximum": MAX_MANIFEST_FILES},
        )
    original_paths = [identity.relative_path for identity in snapshot.manifest]
    if current_manifest_targets is None:
        current_paths = list(original_paths)
    else:
        current_paths = [
            _relative_posix(root, target, what="source file to revalidate")
            for target in current_manifest_targets
        ]
        if len(set(current_paths)) != len(current_paths):
            raise SourceObservationViolation(
                "a revalidation manifest must not repeat a relative path",
                detail={"root_id": root_id},
            )
    original_set = set(original_paths)
    current_set = set(current_paths)
    added = tuple(sorted(current_set - original_set))
    removed = tuple(sorted(original_set - current_set))
    identities_by_path = {identity.relative_path: identity for identity in snapshot.manifest}
    results: list[dict[str, object]] = []
    for relative_path in sorted(original_set & current_set):
        identity = identities_by_path[relative_path]
        try:
            data = read_confined_file(
                root,
                _confined_candidate(root, Path(relative_path)),
                max_bytes=snapshot.max_bytes_per_file,
                what="source file to revalidate",
            )
        except ContractViolation as exc:
            results.append(
                {
                    "relative_path": relative_path,
                    "status": FileRevalidationStatus.MISSING.value,
                    "previous_byte_digest": identity.byte_digest,
                    "current_byte_digest": None,
                    "refusal_reason": exc.message,
                }
            )
            continue
        current_digest = _sha256_digest(data)
        status = (
            FileRevalidationStatus.UNCHANGED
            if current_digest == identity.byte_digest
            else FileRevalidationStatus.MUTATED
        )
        results.append(
            {
                "relative_path": relative_path,
                "status": status.value,
                "previous_byte_digest": identity.byte_digest,
                "current_byte_digest": current_digest,
                "refusal_reason": None,
            }
        )
    drifted = (
        bool(added)
        or bool(removed)
        or any(result["status"] != FileRevalidationStatus.UNCHANGED.value for result in results)
    )
    payload = {
        "contract_version": SOURCE_OBSERVATION_CONTRACT_VERSION,
        "host": host.canonical_payload(),
        "root_id": root_id,
        "snapshot_seal": snapshot.manifest_seal(),
        "revalidated_at": revalidated_at,
        "results": results,
        "added_relative_paths": list(added),
        "removed_relative_paths": list(removed),
        "status": (DriftStatus.DRIFTED if drifted else DriftStatus.UNCHANGED).value,
    }
    return validate_contract(
        SourceSnapshotRevalidation,
        payload,
        error=SourceObservationViolation,
        context="source snapshot revalidation",
    )

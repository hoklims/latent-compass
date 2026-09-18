"""HOK-800 — one observation envelope, admitted and cross-checked, never executed.

ADR 0011 forbids this package from launching a process, so it cannot run Git,
a language server or a test. Those tools belong to the **authorised host
executor**. This module is the admission boundary for what they return, and
the one shape every observation kind shares:

* ``FILE_READ`` and ``LITERAL_SEARCH`` — read by the lab's own confined reader
  (see :mod:`latent_compass.lab.observations`) and wrapped here;
* ``GIT_DIFF``, ``SYMBOL_NAVIGATION`` and ``TARGETED_CHECK`` — executed by the
  host and submitted as a :class:`HostObservation`.

Every observation states the question it answered (as a digest, never as
text), its declared coverage, the evidence it obtained, the identity and
version of the tool that produced it, how long it took, what it cost, which
resources it used, and the limits of what it can show. An empty result always
carries ``EMPTY_IS_NOT_ABSENCE``: nothing found is a statement about the
declared coverage, never about the repository.

What the lab verifies, and what it cannot
------------------------------------------
:func:`verify_host_observation` re-reads, through the confined reader, every
file a conclusive observation covers and every location it cites. It refuses
when those bytes no longer match the episode's captured snapshot, when a cited
line does not exist, or when the declared anchor does not occur in the cited
lines — so an external mutation or a branch change between observation and
consumption invalidates the observation even when the declared Git ``HEAD`` is
identical. It refuses a retired provider, a symbolic observation in
``SOURCE_ONLY`` mode, and any unaccounted internal index.

It cannot authenticate the host. A tool identity, a Git identity, a check
verdict, a duration, a cost and the list of providers used are declarations
the host makes; the mandatory limit codes say so on the record itself. A
timeout, an absent tool, an unsupported language or a tool failure is recorded
as a non-conclusive observation: it produces no outcome, triggers no fallback,
and repairs nothing.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass import __version__
from latent_compass.canonical import seal
from latent_compass.confined_io import read_confined_file
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import ContractViolation
from latent_compass.lab.contracts import MAX_PROBE_COST
from latent_compass.lab.observations import (
    MAX_SOURCE_FILE_BYTES,
    DeclaredGitHead,
    HostBinding,
    LiteralMatchReport,
    RelativeSourcePath,
    SourceByteObservation,
    SourceEncoding,
    SourceSnapshot,
)

__all__ = [
    "HOST_OBSERVATION_CONTRACT_VERSION",
    "LAB_READER_PROVIDER_ID",
    "LAB_READER_TOOL_ID",
    "MAX_COVERED_PATHS",
    "MAX_DIFF_ENTRIES",
    "MAX_EVIDENCE_LOCATIONS",
    "SUPPORTED_HOST_OBSERVATION_VERSIONS",
    "CheckDetail",
    "CheckVerdict",
    "Conclusiveness",
    "DiffChange",
    "EvidenceLocation",
    "ExecutorKind",
    "GitDiffDetail",
    "GitDiffEntry",
    "HostObservation",
    "HostObservationVerification",
    "HostObservationViolation",
    "LimitCode",
    "ObservationKind",
    "ObservationMode",
    "ObservationPolicy",
    "ObservationStatus",
    "ResourceAccounting",
    "SymbolDetail",
    "SymbolRelation",
    "ToolIdentity",
    "admit_host_observation",
    "admit_observation_policy",
    "conclusiveness_of",
    "observation_from_byte_observation",
    "observation_from_literal_report",
    "question_digest",
    "verify_host_observation",
]

#: Its own experimental axis: it embeds the source-observation host binding
#: and path vocabulary, and no model, state, routing or migration shape.
HOST_OBSERVATION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_HOST_OBSERVATION_VERSIONS: Final = frozenset({HOST_OBSERVATION_CONTRACT_VERSION})

HOST_OBSERVATION_SEAL_DOMAIN: Final = "lab.host-observation.v1"
OBSERVATION_POLICY_SEAL_DOMAIN: Final = "lab.host-observation.policy.v1"
VERIFICATION_SEAL_DOMAIN: Final = "lab.host-observation.verification.v1"

#: Hard ceilings, none negotiable upward by a caller or a host.
MAX_COVERED_PATHS: Final = 64
MAX_EVIDENCE_LOCATIONS: Final = 256
MAX_DIFF_ENTRIES: Final = MAX_COVERED_PATHS
MAX_PROVIDERS: Final = 8
MAX_RETIRED_PROVIDERS: Final = 16
MAX_DURATION_MS: Final = 3_600_000
MAX_CHILD_PROCESSES: Final = 1024
MAX_LOG_BYTES: Final = 1024 * 1024
MAX_ANCHOR_BYTES: Final = 4096

#: The identity the lab's own confined reader declares for what it reads.
LAB_READER_TOOL_ID: Final = "lab-confined-reader"
LAB_READER_PROVIDER_ID: Final = "source"


class HostObservationViolation(ContractViolation):
    """A HOK-800 host observation contract or verification rule was broken.

    ``detail["reason"]`` names the exact case for every refusal raised by
    :func:`verify_host_observation` and the two converters.
    """

    code = "host_observation_violation"


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


ToolVersion = Annotated[
    str, Field(min_length=1, max_length=64), AfterValidator(_no_control_characters)
]


class ObservationKind(StrEnum):
    FILE_READ = "FILE_READ"
    LITERAL_SEARCH = "LITERAL_SEARCH"
    GIT_DIFF = "GIT_DIFF"
    SYMBOL_NAVIGATION = "SYMBOL_NAVIGATION"
    TARGETED_CHECK = "TARGETED_CHECK"


class ObservationStatus(StrEnum):
    """How the acquisition ended. Only the first three can carry a result."""

    OBSERVED = "OBSERVED"
    EMPTY = "EMPTY"
    TRUNCATED = "TRUNCATED"
    TIMEOUT = "TIMEOUT"
    TOOL_ABSENT = "TOOL_ABSENT"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    FAILED = "FAILED"


#: Statuses that produced no result at all. They are recorded, accounted for,
#: and never converted into an outcome.
_FAILURE_STATUSES: Final = frozenset(
    {
        ObservationStatus.TIMEOUT,
        ObservationStatus.TOOL_ABSENT,
        ObservationStatus.UNSUPPORTED_LANGUAGE,
        ObservationStatus.FAILED,
    }
)


class ObservationMode(StrEnum):
    """``SOURCE_ONLY`` is the witness mode: no symbolic tool, no internal index."""

    SOURCE_ONLY = "SOURCE_ONLY"
    SOURCE_AND_SYMBOLIC = "SOURCE_AND_SYMBOLIC"


class ExecutorKind(StrEnum):
    LAB_CONFINED_READER = "LAB_CONFINED_READER"
    HOST_EXECUTOR = "HOST_EXECUTOR"


class LimitCode(StrEnum):
    """What an observation cannot show, carried on the sealed record itself."""

    COVERAGE_IS_DECLARED_PATHS_ONLY = "COVERAGE_IS_DECLARED_PATHS_ONLY"
    EMPTY_IS_NOT_ABSENCE = "EMPTY_IS_NOT_ABSENCE"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    GIT_IDENTITY_IS_DECLARED = "GIT_IDENTITY_IS_DECLARED"
    DIRECT_RELATIONS_ONLY = "DIRECT_RELATIONS_ONLY"
    TOOL_INTERNAL_INDEX_USED = "TOOL_INTERNAL_INDEX_USED"
    GENERATED_CODE_IN_EVIDENCE = "GENERATED_CODE_IN_EVIDENCE"
    CHECK_VERDICT_IS_HOST_DECLARED = "CHECK_VERDICT_IS_HOST_DECLARED"
    OUTCOME_IS_HOST_INTERPRETED = "OUTCOME_IS_HOST_INTERPRETED"


class SymbolRelation(StrEnum):
    DEFINITION = "DEFINITION"
    REFERENCES = "REFERENCES"
    IMPLEMENTATIONS = "IMPLEMENTATIONS"
    CALLERS = "CALLERS"


class DiffChange(StrEnum):
    """A deleted path cannot appear: coverage is a subset of captured files."""

    ADDED = "ADDED"
    MODIFIED = "MODIFIED"


class CheckVerdict(StrEnum):
    """``ERRORED`` is an infrastructure or collection error, never a detection."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERRORED = "ERRORED"


class Conclusiveness(StrEnum):
    FULL = "FULL"
    PRESENCE_ONLY = "PRESENCE_ONLY"
    NONE = "NONE"


class ToolIdentity(StrictModel):
    """The tool a host declares it ran. Bound into the sealed record, never verified."""

    tool_id: Identifier
    version: ToolVersion
    provider_id: Identifier


class EvidenceLocation(StrictModel):
    """One cited location: the bytes the tool saw, and where in them."""

    relative_path: RelativeSourcePath
    byte_digest: Seal
    size_bytes: int = Field(ge=0, le=MAX_SOURCE_FILE_BYTES)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    generated: bool = False

    @model_validator(mode="after")
    def _ordered_lines(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not precede line_start")
        return self


class GitDiffEntry(StrictModel):
    """One path the working tree changed against the declared base identity."""

    relative_path: RelativeSourcePath
    change: DiffChange
    post_image_digest: Seal
    post_image_size: int = Field(ge=0, le=MAX_SOURCE_FILE_BYTES)


class GitDiffDetail(StrictModel):
    """A diff of the **working tree** against a declared base identity.

    The post-image is what sits on disk, so a dirty working tree is compared
    by content. ``base_identity`` is a declaration this package cannot verify.
    """

    base_identity: DeclaredGitHead
    entries: tuple[GitDiffEntry, ...] = Field(default=(), max_length=MAX_DIFF_ENTRIES)

    @model_validator(mode="after")
    def _unique_sorted_entries(self) -> Self:
        paths = [entry.relative_path for entry in self.entries]
        if len(set(paths)) != len(paths):
            raise ValueError("a git diff must not repeat a relative path")
        if paths != sorted(paths):
            raise ValueError("git diff entries must be sorted by relative_path")
        return self


class SymbolDetail(StrictModel):
    relation: SymbolRelation
    language: Identifier


class CheckDetail(StrictModel):
    """One targeted check, as the host executor declares it ran.

    Carries digests only: the command line and its log stay with the host, so
    no secret or raw output travels in a sealed record.
    """

    command_digest: Seal
    verdict: CheckVerdict | None = Field(default=None)
    exit_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    log_digest: Seal | None = Field(default=None)
    log_bytes: int | None = Field(default=None, ge=0, le=MAX_LOG_BYTES)

    @model_validator(mode="after")
    def _coherent_check(self) -> Self:
        if (self.verdict is None) != (self.exit_code is None):
            raise ValueError("a check declares a verdict and an exit code together, or neither")
        if self.verdict is CheckVerdict.PASSED and self.exit_code != 0:
            raise ValueError("a PASSED check must declare exit code 0")
        if self.verdict in (CheckVerdict.FAILED, CheckVerdict.ERRORED) and self.exit_code == 0:
            raise ValueError(f"a {self.verdict.value} check must declare a non-zero exit code")
        if (self.log_digest is None) != (self.log_bytes is None):
            raise ValueError("a check log declares its digest and size together, or neither")
        return self


class ResourceAccounting(StrictModel):
    """What the acquisition used. ``None`` means not observed, never zero."""

    child_processes: int = Field(ge=0, le=MAX_CHILD_PROCESSES)
    files_read: int = Field(ge=0)
    repeated_reads: int = Field(ge=0)
    internal_index_used: bool | None = Field(default=None)
    cache_used: bool | None = Field(default=None)


def _required_limits(observation: HostObservation) -> frozenset[LimitCode]:
    required = {LimitCode.COVERAGE_IS_DECLARED_PATHS_ONLY}
    if observation.status is ObservationStatus.EMPTY:
        required.add(LimitCode.EMPTY_IS_NOT_ABSENCE)
    if observation.status is ObservationStatus.TRUNCATED:
        required.add(LimitCode.OUTPUT_TRUNCATED)
    if observation.kind is ObservationKind.GIT_DIFF:
        required.add(LimitCode.GIT_IDENTITY_IS_DECLARED)
    if observation.kind is ObservationKind.TARGETED_CHECK:
        required.add(LimitCode.CHECK_VERDICT_IS_HOST_DECLARED)
    if observation.symbol is not None and (
        observation.symbol.relation is not SymbolRelation.DEFINITION
    ):
        required.add(LimitCode.DIRECT_RELATIONS_ONLY)
    if observation.resources.internal_index_used:
        required.add(LimitCode.TOOL_INTERNAL_INDEX_USED)
    if any(location.generated for location in observation.evidence):
        required.add(LimitCode.GENERATED_CODE_IN_EVIDENCE)
    if observation.interpreted_outcome_id is not None:
        required.add(LimitCode.OUTCOME_IS_HOST_INTERPRETED)
    return frozenset(required)


class HostObservation(StrictModel):
    """One observation of any kind, in the one shape every kind shares."""

    contract_version: str = Field(min_length=5, max_length=20)
    observation_id: Identifier
    kind: ObservationKind
    probe_id: Identifier
    question_digest: Seal
    host: HostBinding
    root_id: Identifier
    mode: ObservationMode
    executed_by: ExecutorKind
    tool: ToolIdentity
    providers_used: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_PROVIDERS)
    declared_git_head: DeclaredGitHead | None = Field(default=None)
    worktree_dirty: bool | None = Field(default=None)
    observed_at: Timestamp
    status: ObservationStatus
    covered_paths: tuple[RelativeSourcePath, ...] = Field(
        min_length=1, max_length=MAX_COVERED_PATHS
    )
    evidence: tuple[EvidenceLocation, ...] = Field(default=(), max_length=MAX_EVIDENCE_LOCATIONS)
    git_diff: GitDiffDetail | None = Field(default=None)
    symbol: SymbolDetail | None = Field(default=None)
    check: CheckDetail | None = Field(default=None)
    #: What the host concluded from reading the cited lines. The lab binds the
    #: bytes that were read; it cannot check the reading itself.
    interpreted_outcome_id: Identifier | None = Field(default=None)
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    observed_cost: int | None = Field(default=None, ge=0, le=MAX_PROBE_COST)
    resources: ResourceAccounting
    limits: tuple[LimitCode, ...] = Field(min_length=1, max_length=len(LimitCode))

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_HOST_OBSERVATION_VERSIONS, "host observation"
        )
        self._check_scope()
        self._check_kind_detail()
        self._check_status()
        self._check_executor_and_mode()
        if len(set(self.limits)) != len(self.limits):
            raise ValueError("a limit code must be listed at most once")
        missing = sorted(code.value for code in _required_limits(self) - set(self.limits))
        if missing:
            raise ValueError(f"observation omits mandatory limit code(s): {missing!r}")
        return self

    def _check_scope(self) -> None:
        if len(set(self.covered_paths)) != len(self.covered_paths):
            raise ValueError("declared coverage must not repeat a relative path")
        if list(self.covered_paths) != sorted(self.covered_paths):
            raise ValueError("declared coverage must be sorted by relative path")
        covered = set(self.covered_paths)
        outside = sorted({item.relative_path for item in self.evidence} - covered)
        if outside:
            raise ValueError(f"evidence cites path(s) outside the declared coverage: {outside!r}")
        if self.git_diff is not None:
            outside = sorted({item.relative_path for item in self.git_diff.entries} - covered)
            if outside:
                raise ValueError(
                    f"git diff names path(s) outside the declared coverage: {outside!r}"
                )
        if len(set(self.providers_used)) != len(self.providers_used):
            raise ValueError("a provider must be listed at most once")
        if self.tool.provider_id not in self.providers_used:
            raise ValueError("providers_used must include the declared tool's own provider")

    def _check_kind_detail(self) -> None:
        expected = {
            ObservationKind.GIT_DIFF: (True, False, False),
            ObservationKind.SYMBOL_NAVIGATION: (False, True, False),
            ObservationKind.TARGETED_CHECK: (False, False, True),
        }.get(self.kind, (False, False, False))
        actual = (self.git_diff is not None, self.symbol is not None, self.check is not None)
        if actual != expected:
            raise ValueError(f"a {self.kind.value} observation carries exactly its own detail")
        if self.kind in (ObservationKind.GIT_DIFF, ObservationKind.TARGETED_CHECK) and (
            self.evidence
        ):
            raise ValueError(f"a {self.kind.value} observation cites no line evidence")
        if self.interpreted_outcome_id is not None and (
            self.kind is not ObservationKind.FILE_READ
            or self.executed_by is not ExecutorKind.HOST_EXECUTOR
            or self.status is not ObservationStatus.OBSERVED
        ):
            raise ValueError(
                "only an OBSERVED file read by the host executor may carry an interpreted outcome"
            )

    def _check_status(self) -> None:
        kind, status = self.kind, self.status
        if status is ObservationStatus.UNSUPPORTED_LANGUAGE and (
            kind is not ObservationKind.SYMBOL_NAVIGATION
        ):
            raise ValueError("only a symbol navigation can report an unsupported language")
        if kind is ObservationKind.TARGETED_CHECK and status in (
            ObservationStatus.EMPTY,
            ObservationStatus.TRUNCATED,
        ):
            raise ValueError("a targeted check either ran to a verdict or did not run")
        if kind is ObservationKind.FILE_READ and status is ObservationStatus.TRUNCATED:
            raise ValueError("a file read is refused above its bound, never truncated")

        has_result = bool(self.evidence) or bool(self.git_diff and self.git_diff.entries)
        has_verdict = self.check is not None and self.check.verdict is not None
        if status in _FAILURE_STATUSES or status is ObservationStatus.EMPTY:
            if has_result or has_verdict:
                raise ValueError(f"a {status.value} observation carries no result")
        elif status is ObservationStatus.OBSERVED:
            if kind is ObservationKind.TARGETED_CHECK:
                if not has_verdict:
                    raise ValueError("an OBSERVED targeted check must declare its verdict")
            elif not has_result:
                raise ValueError("an OBSERVED observation must carry at least one result")

    def _check_executor_and_mode(self) -> None:
        lab_read = self.executed_by is ExecutorKind.LAB_CONFINED_READER
        if lab_read:
            if self.kind not in (ObservationKind.FILE_READ, ObservationKind.LITERAL_SEARCH):
                raise ValueError("the lab's confined reader only reads files and literals")
            if (
                self.resources.child_processes != 0
                or self.resources.internal_index_used is not False
            ):
                raise ValueError("the lab's confined reader spawns nothing and uses no index")
        if self.mode is ObservationMode.SOURCE_ONLY:
            if self.kind is ObservationKind.SYMBOL_NAVIGATION:
                raise ValueError("SOURCE_ONLY mode admits no symbol navigation")
            if self.resources.internal_index_used is not False:
                raise ValueError("SOURCE_ONLY mode requires internal_index_used to be false")

    def observation_seal(self) -> str:
        """Reproducible seal over the whole observation, in its own domain."""
        return seal(HOST_OBSERVATION_SEAL_DOMAIN, self.canonical_payload())


class ObservationPolicy(StrictModel):
    """The caller's session policy. Nothing read from a repository can change it."""

    contract_version: str = Field(min_length=5, max_length=20)
    mode: ObservationMode
    retired_providers: tuple[Identifier, ...] = Field(default=(), max_length=MAX_RETIRED_PROVIDERS)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_HOST_OBSERVATION_VERSIONS, "observation policy"
        )
        if len(set(self.retired_providers)) != len(self.retired_providers):
            raise ValueError("a retired provider must be listed at most once")
        if list(self.retired_providers) != sorted(self.retired_providers):
            raise ValueError("retired providers must be sorted")
        return self

    def policy_seal(self) -> str:
        return seal(OBSERVATION_POLICY_SEAL_DOMAIN, self.canonical_payload())


class HostObservationVerification(StrictModel):
    """What the lab itself re-read and matched for one admitted observation."""

    contract_version: str = Field(min_length=5, max_length=20)
    observation_seal: Seal
    snapshot_manifest_seal: Seal
    policy_seal: Seal
    host: HostBinding
    root_id: Identifier
    verified_at: Timestamp
    status: ObservationStatus
    conclusiveness: Conclusiveness
    verified_paths: tuple[RelativeSourcePath, ...] = Field(max_length=MAX_COVERED_PATHS)
    verified_location_count: int = Field(ge=0, le=MAX_EVIDENCE_LOCATIONS)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_HOST_OBSERVATION_VERSIONS,
            "host observation verification",
        )
        if self.conclusiveness is Conclusiveness.NONE and (
            self.verified_paths or self.verified_location_count
        ):
            raise ValueError("a non-conclusive observation verifies no content")
        return self

    def verification_seal(self) -> str:
        return seal(VERIFICATION_SEAL_DOMAIN, self.canonical_payload())


class _Instant(StrictModel):
    """Validates a caller-supplied instant before it is compared as text."""

    value: Timestamp


def question_digest(question: str) -> str:
    """The plain SHA-256 of a question's UTF-8 bytes, as every record carries it."""
    return "sha256:" + hashlib.sha256(question.encode("utf-8")).hexdigest()


def admit_host_observation(payload: object) -> HostObservation:
    return validate_contract(
        HostObservation, payload, error=HostObservationViolation, context="host observation"
    )


def admit_observation_policy(payload: object) -> ObservationPolicy:
    return validate_contract(
        ObservationPolicy, payload, error=HostObservationViolation, context="observation policy"
    )


def conclusiveness_of(observation: HostObservation) -> Conclusiveness:
    """How much an observation's status lets a consumer conclude.

    ``OBSERVED`` and ``EMPTY`` are conclusive over the declared coverage. A
    ``TRUNCATED`` result proves what it lists and nothing about what it
    omitted. Every other status proves nothing.
    """
    if observation.status in (ObservationStatus.OBSERVED, ObservationStatus.EMPTY):
        return Conclusiveness.FULL
    if observation.status is ObservationStatus.TRUNCATED and (
        observation.evidence or (observation.git_diff and observation.git_diff.entries)
    ):
        return Conclusiveness.PRESENCE_ONLY
    return Conclusiveness.NONE


def _refuse(message: str, reason: str, **detail: object) -> HostObservationViolation:
    return HostObservationViolation(message, detail={"reason": reason, **detail})


def _require_policy(observation: HostObservation, policy: ObservationPolicy) -> None:
    if observation.mode is not policy.mode:
        raise _refuse(
            "observation mode does not match the session policy",
            "mode_mismatch",
            observation_mode=observation.mode.value,
            policy_mode=policy.mode.value,
        )
    retired = sorted(set(observation.providers_used) & set(policy.retired_providers))
    if retired:
        raise _refuse(
            "observation was produced with a retired provider; no hidden fallback is admitted",
            "retired_provider_used",
            retired_providers=retired,
        )


def _read_matching_manifest(
    root: Path, snapshot: SourceSnapshot, relative_path: str, *, observation_id: str
) -> bytes:
    identity = next(
        (item for item in snapshot.manifest if item.relative_path == relative_path), None
    )
    if identity is None:
        raise _refuse(
            "observation covers a path outside the episode's source snapshot",
            "path_outside_snapshot",
            observation_id=observation_id,
            relative_path=relative_path,
        )
    try:
        data = read_confined_file(
            root,
            root / relative_path,
            max_bytes=snapshot.max_bytes_per_file,
            what="observed source file to verify",
        )
    except ContractViolation as exc:
        raise _refuse(
            "a covered file could not be re-read; the observation no longer applies",
            "covered_file_unreadable",
            observation_id=observation_id,
            relative_path=relative_path,
            cause=exc.code,
        ) from exc
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != identity.byte_digest or len(data) != identity.size_bytes:
        raise _refuse(
            "a covered file drifted from the episode's snapshot; the observation no longer applies",
            "drift_since_snapshot",
            observation_id=observation_id,
            relative_path=relative_path,
        )
    return data


def _verify_location(location: EvidenceLocation, data: bytes, *, anchor: str | None) -> None:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if location.byte_digest != digest or location.size_bytes != len(data):
        raise _refuse(
            "the tool saw different bytes than the episode's snapshot binds",
            "evidence_digest_mismatch",
            relative_path=location.relative_path,
        )
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise _refuse(
            "line evidence cites a binary or undecodable file",
            "evidence_not_text",
            relative_path=location.relative_path,
        ) from exc
    if location.line_end > len(lines):
        raise _refuse(
            "evidence cites a line that does not exist in the bytes read",
            "evidence_line_out_of_range",
            relative_path=location.relative_path,
            line_end=location.line_end,
            line_count=len(lines),
        )
    if anchor is not None and not any(
        anchor in line for line in lines[location.line_start - 1 : location.line_end]
    ):
        raise _refuse(
            "the declared anchor does not occur in the cited lines",
            "anchor_absent_from_evidence",
            relative_path=location.relative_path,
            line_start=location.line_start,
            line_end=location.line_end,
        )


def verify_host_observation(
    root: Path,
    observation: HostObservation,
    *,
    snapshot: SourceSnapshot,
    policy: ObservationPolicy,
    expected_host: HostBinding,
    expected_root_id: str,
    verified_at: str,
    anchor: str | None = None,
) -> HostObservationVerification:
    """Cross-check one admitted observation against the episode's snapshot.

    Order of refusal: any public input fails revalidation from its own
    canonical payload; host or root scope differs between the caller, the
    snapshot and the observation; the observation's mode or providers break
    the session policy; the observation predates the snapshot or postdates
    ``verified_at``. A conclusive observation is then re-read through the
    confined reader, file by file: a covered path outside the snapshot, an
    unreadable or drifted file, evidence whose digest is not the snapshot's,
    a cited line beyond the file, or an ``anchor`` absent from the cited
    lines each refuse. ``anchor`` is caller configuration; it is never echoed
    back in a report or an error.

    A non-conclusive observation passes the scope and policy checks and reads
    nothing: it is admitted as a record of an ``UNKNOWN``, nothing more.
    """
    observation = admit_host_observation(observation.canonical_payload())
    snapshot = validate_contract(
        SourceSnapshot,
        snapshot.canonical_payload(),
        error=HostObservationViolation,
        context="source snapshot",
    )
    policy = admit_observation_policy(policy.canonical_payload())
    expected_host = validate_contract(
        HostBinding,
        expected_host.canonical_payload(),
        error=HostObservationViolation,
        context="expected host binding",
    )
    if anchor is not None and not 0 < len(anchor.encode("utf-8")) <= MAX_ANCHOR_BYTES:
        raise _refuse("an anchor must be non-empty and bounded", "anchor_out_of_bounds")

    if (
        observation.host != expected_host
        or snapshot.host != expected_host
        or observation.root_id != expected_root_id
        or snapshot.root_id != expected_root_id
    ):
        raise _refuse(
            "observation, snapshot and caller do not share one host and root scope",
            "scope_mismatch",
            observation_root_id=observation.root_id,
            snapshot_root_id=snapshot.root_id,
            expected_root_id=expected_root_id,
        )
    _require_policy(observation, policy)
    # Canonical ``YYYY-MM-DDTHH:MM:SSZ`` timestamps order lexicographically.
    verified_at = validate_contract(
        _Instant, {"value": verified_at}, error=HostObservationViolation, context="verified_at"
    ).value
    if observation.observed_at < snapshot.captured_at:
        raise _refuse(
            "observation predates the snapshot it would be bound to",
            "observation_predates_snapshot",
        )
    if verified_at < observation.observed_at:
        raise _refuse(
            "observation is dated after its own verification",
            "observation_postdates_verification",
        )

    conclusiveness = conclusiveness_of(observation)
    verified_paths: list[str] = []
    verified_locations = 0
    if conclusiveness is not Conclusiveness.NONE:
        data_by_path = {
            relative_path: _read_matching_manifest(
                root, snapshot, relative_path, observation_id=observation.observation_id
            )
            for relative_path in observation.covered_paths
        }
        verified_paths = sorted(data_by_path)
        # A literal search is the one emptiness the lab can check for itself.
        if (
            anchor is not None
            and observation.kind is ObservationKind.LITERAL_SEARCH
            and observation.status is ObservationStatus.EMPTY
        ):
            needle = anchor.encode("utf-8")
            contradicted = sorted(path for path, data in data_by_path.items() if needle in data)
            if contradicted:
                raise _refuse(
                    "an empty literal search is contradicted by the covered source itself",
                    "empty_contradicted_by_source",
                    relative_paths=contradicted,
                )
        for location in observation.evidence:
            _verify_location(location, data_by_path[location.relative_path], anchor=anchor)
            verified_locations += 1
        if observation.git_diff is not None:
            for entry in observation.git_diff.entries:
                data = data_by_path[entry.relative_path]
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                if entry.post_image_digest != digest or entry.post_image_size != len(data):
                    raise _refuse(
                        "a git diff post-image is not the content on disk",
                        "diff_post_image_mismatch",
                        relative_path=entry.relative_path,
                    )
                verified_locations += 1

    return validate_contract(
        HostObservationVerification,
        {
            "contract_version": HOST_OBSERVATION_CONTRACT_VERSION,
            "observation_seal": observation.observation_seal(),
            "snapshot_manifest_seal": snapshot.manifest_seal(),
            "policy_seal": policy.policy_seal(),
            "host": expected_host.canonical_payload(),
            "root_id": expected_root_id,
            "verified_at": verified_at,
            "status": observation.status.value,
            "conclusiveness": conclusiveness.value,
            "verified_paths": verified_paths,
            "verified_location_count": verified_locations,
        },
        error=HostObservationViolation,
        context="host observation verification",
    )


def _lab_reader_envelope(
    *,
    observation_id: str,
    kind: ObservationKind,
    probe_id: str,
    digest: str,
    host: HostBinding,
    root_id: str,
    mode: ObservationMode,
    declared_git_head: str | None,
    observed_at: str,
    status: ObservationStatus,
    covered_paths: list[str],
    evidence: list[dict[str, object]],
    limits: set[LimitCode],
) -> HostObservation:
    limits.add(LimitCode.COVERAGE_IS_DECLARED_PATHS_ONLY)
    return admit_host_observation(
        {
            "contract_version": HOST_OBSERVATION_CONTRACT_VERSION,
            "observation_id": observation_id,
            "kind": kind.value,
            "probe_id": probe_id,
            "question_digest": digest,
            "host": host.canonical_payload(),
            "root_id": root_id,
            "mode": mode.value,
            "executed_by": ExecutorKind.LAB_CONFINED_READER.value,
            "tool": {
                "tool_id": LAB_READER_TOOL_ID,
                "version": __version__,
                "provider_id": LAB_READER_PROVIDER_ID,
            },
            "providers_used": [LAB_READER_PROVIDER_ID],
            "declared_git_head": declared_git_head,
            "worktree_dirty": None,
            "observed_at": observed_at,
            "status": status.value,
            "covered_paths": sorted(covered_paths),
            "evidence": evidence,
            "git_diff": None,
            "symbol": None,
            "check": None,
            "duration_ms": None,
            "observed_cost": None,
            "resources": {
                "child_processes": 0,
                "files_read": len(covered_paths),
                "repeated_reads": 0,
                "internal_index_used": False,
                "cache_used": False,
            },
            "limits": sorted(code.value for code in limits),
        }
    )


def observation_from_literal_report(
    report: LiteralMatchReport,
    *,
    observation_id: str,
    probe_id: str,
    mode: ObservationMode,
) -> HostObservation:
    """Wrap the lab's own bounded literal search in the shared envelope.

    A binary or undecodable file in the declared coverage is refused: it can
    establish neither a match nor the absence of one.
    """
    report = validate_contract(
        LiteralMatchReport,
        report.canonical_payload(),
        error=HostObservationViolation,
        context="literal match report",
    )
    evidence: list[dict[str, object]] = []
    truncated = False
    for result in report.results:
        if result.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8:
            raise _refuse(
                "a file in the declared coverage is binary or not valid UTF-8",
                "binary_or_invalid_utf8",
                relative_path=result.relative_path,
            )
        truncated = truncated or result.truncated
        evidence.extend(
            {
                "relative_path": result.relative_path,
                "byte_digest": result.byte_digest,
                "size_bytes": result.size_bytes,
                "line_start": line,
                "line_end": line,
                "generated": False,
            }
            for line in result.line_numbers
        )
    if len(evidence) > MAX_EVIDENCE_LOCATIONS:
        evidence = evidence[:MAX_EVIDENCE_LOCATIONS]
        truncated = True
    limits: set[LimitCode] = set()
    if truncated:
        status = ObservationStatus.TRUNCATED
        limits.add(LimitCode.OUTPUT_TRUNCATED)
    elif evidence:
        status = ObservationStatus.OBSERVED
    else:
        status = ObservationStatus.EMPTY
        limits.add(LimitCode.EMPTY_IS_NOT_ABSENCE)
    return _lab_reader_envelope(
        observation_id=observation_id,
        kind=ObservationKind.LITERAL_SEARCH,
        probe_id=probe_id,
        digest=report.query_digest,
        host=report.host,
        root_id=report.root_id,
        mode=mode,
        declared_git_head=report.declared_git_head,
        observed_at=report.observed_at,
        status=status,
        covered_paths=[result.relative_path for result in report.results],
        evidence=evidence,
        limits=limits,
    )


def observation_from_byte_observation(
    observed: SourceByteObservation,
    *,
    observation_id: str,
    probe_id: str,
    question: str,
    mode: ObservationMode,
) -> HostObservation:
    """Wrap the lab's own bounded file read in the shared envelope.

    The whole file is the evidence. A binary file is refused, and a file with
    no line is reported ``EMPTY`` rather than given a location it lacks.
    """
    observed = validate_contract(
        SourceByteObservation,
        observed.canonical_payload(),
        error=HostObservationViolation,
        context="source byte observation",
    )
    if observed.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8 or observed.line_count is None:
        raise _refuse(
            "a binary or undecodable file cannot be cited by line",
            "binary_or_invalid_utf8",
            relative_path=observed.relative_path,
        )
    evidence: list[dict[str, object]] = []
    limits: set[LimitCode] = set()
    if observed.line_count > 0:
        status = ObservationStatus.OBSERVED
        evidence.append(
            {
                "relative_path": observed.relative_path,
                "byte_digest": observed.byte_digest,
                "size_bytes": observed.size_bytes,
                "line_start": 1,
                "line_end": observed.line_count,
                "generated": False,
            }
        )
    else:
        status = ObservationStatus.EMPTY
        limits.add(LimitCode.EMPTY_IS_NOT_ABSENCE)
    return _lab_reader_envelope(
        observation_id=observation_id,
        kind=ObservationKind.FILE_READ,
        probe_id=probe_id,
        digest=question_digest(question),
        host=observed.host,
        root_id=observed.root_id,
        mode=mode,
        declared_git_head=observed.declared_git_head,
        observed_at=observed.observed_at,
        status=status,
        covered_paths=[observed.relative_path],
        evidence=evidence,
        limits=limits,
    )

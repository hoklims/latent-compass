"""HOK-798 — the bridge from bounded literal source reads to the finite model.

This module is the one seam between three independently sealed HOK-798-family
contracts, none of which know about each other: the finite decision
model/state core (:mod:`latent_compass.lab.state`,
:mod:`latent_compass.lab.planner`), the bounded read-only source observer
(``latent_compass.lab.observations``, HOK-800), and lab routing/budgets
(``latent_compass.lab.routing``, HOK-803). It performs no probe selection, no
routing decision and no budget accounting itself — see
:mod:`latent_compass.lab.routing` and the caller in
``examples/lab_source_demo.py`` for that. It is still a lab: no operational
host adapter, no process execution, no index refresh, no deletion.

A :class:`SourceProbeCatalog` declares, once per session, which bounded file
list and literal query answer each of the model's probes, and which of the
probe's own declared outcomes means "matched" versus "not matched". The query
text is configuration a caller supplies; it is never echoed back in any
report or error this module raises, and neither is raw file content — every
public result here carries only ids, digests, sizes and line numbers.

:func:`derive_lab_binding` seals one session's identity from the model, the
captured :class:`~latent_compass.lab.observations.SourceSnapshot` manifest,
its ``root_id`` and the *complete* catalog, so that changing the catalog or
its queries — even against source bytes that happen to be unchanged — never
silently reuses a prior episode's :class:`~latent_compass.lab.state.LabBinding`.
:func:`observe_probe_from_source` is the one function that turns one caller-
selected probe's bounded literal read into a core observation: it revalidates
every public object it is handed, refuses before reading on a wrong scope,
catalog or non-replaying state, performs only that probe's bounded read,
compares every result's same-read byte digest and size against the captured
manifest, refuses on drift, a missing file, binary or undecodable content, or
any mismatch — never converting a binary file into an ``absent`` outcome —
and only then calls :func:`~latent_compass.lab.state.apply_observation`. On
any refusal, no state is produced.

Caller-declared identity (host, agent family, root, Git HEAD) is a
declaration this module checks for internal consistency, never an
independently authenticated fact — the same boundary every module in this
family draws. See ``docs/adr/0011-experimental-active-diagnosis.md`` and
``docs/source-session.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import AgentFamily
from latent_compass.lab.contracts import MAX_PROBES
from latent_compass.lab.errors import (
    LabBindingMismatchError,
    LabContractViolationError,
    LabCrossModelStateError,
    LabRepeatedProbeError,
    LabReplayViolationError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.observations import (
    DriftStatus,
    HostBinding,
    LiteralMatchReport,
    SourceEncoding,
    SourceSnapshot,
    SourceSnapshotRevalidation,
    observe_literal_matches,
    revalidate_source_snapshot,
)
from latent_compass.lab.state import (
    DiagnosisStateRevision,
    LabBinding,
    apply_observation,
    initial_state,
    load_state,
    verify_state_history,
)

__all__ = [
    "MAX_RELATIVE_PATHS_PER_PROBE",
    "SOURCE_SESSION_CONTRACT_VERSION",
    "SUPPORTED_SOURCE_SESSION_VERSIONS",
    "LabSourceSessionViolationError",
    "SourceProbeCatalog",
    "SourceProbeObservationResult",
    "SourceProbeSpec",
    "derive_lab_binding",
    "load_source_probe_catalog",
    "observe_probe_from_source",
]

#: Its own experimental axis: this bridge's catalog and derived binding embed
#: no core state/planner shape, no HOK-800 observation shape and no HOK-803
#: routing shape, and must be free to move independently of all three.
SOURCE_SESSION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_SOURCE_SESSION_VERSIONS: Final = frozenset({SOURCE_SESSION_CONTRACT_VERSION})

SOURCE_SESSION_BINDING_SEAL_DOMAIN: Final = "lab.source-session.binding.v1"
SOURCE_PROBE_CATALOG_SEAL_DOMAIN: Final = "lab.source-session.catalog.v1"

#: A bounded ceiling on one probe's declared file list, not negotiable upward.
MAX_RELATIVE_PATHS_PER_PROBE: Final = 16
_MAX_QUERY_TEXT_LENGTH: Final = 4096
#: One match is enough to decide present/absent; the read stays as small as
#: the outcome it is bounded to answer.
_MAX_MATCHES_PER_PROBE_READ: Final = 1


class LabSourceSessionViolationError(LabContractViolationError):
    """A HOK-798 source-session bridge rule was broken.

    Covers catalog/model/snapshot coherence at :func:`derive_lab_binding`, and
    every refusal :func:`observe_probe_from_source` can raise once reading
    begins: drift against the captured manifest (before or after the read), a
    missing file, binary or undecodable content standing in this probe's
    declared coverage, or a same-read digest/size mismatch against the
    manifest. ``detail["reason"]`` names the exact case. No variant of this
    error ever leaves the caller's prior state changed.
    """

    code = "lab_source_session_violation"


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


def _require_relative_posix_like(value: str) -> str:
    _no_control_characters(value)
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute():
        raise ValueError("relative path must not be absolute")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError("relative path must not contain '.', '..' or an empty segment")
    return value


_CatalogRelativePath = Annotated[
    str,
    Field(min_length=1, max_length=4096),
    AfterValidator(_require_relative_posix_like),
]
_QueryText = Annotated[
    str,
    Field(min_length=1, max_length=_MAX_QUERY_TEXT_LENGTH),
    AfterValidator(_no_control_characters),
]


class SourceProbeSpec(StrictModel):
    """One model probe's bounded source answer: where to look, what to ask.

    ``query`` is configuration this module treats as opaque and never
    reproduces in a report or an error. ``present_outcome_id`` and
    ``absent_outcome_id`` must be two distinct outcomes the *model's own*
    probe declares — checked where the model is available, at
    :func:`derive_lab_binding`, not here.
    """

    probe_id: Identifier
    relative_paths: tuple[_CatalogRelativePath, ...] = Field(
        min_length=1, max_length=MAX_RELATIVE_PATHS_PER_PROBE
    )
    query: _QueryText
    present_outcome_id: Identifier
    absent_outcome_id: Identifier

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.relative_paths)) != len(self.relative_paths):
            raise ValueError(f"probe {self.probe_id!r} declares a duplicate relative path")
        if self.present_outcome_id == self.absent_outcome_id:
            raise ValueError(f"probe {self.probe_id!r} present and absent outcome ids must differ")
        return self


class SourceProbeCatalog(StrictModel):
    """A versioned, session-scoped catalog of every model probe's source answer."""

    contract_version: str = Field(min_length=5, max_length=20)
    probes: tuple[SourceProbeSpec, ...] = Field(min_length=1, max_length=MAX_PROBES)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_SOURCE_SESSION_VERSIONS, "source probe catalog"
        )
        probe_ids = [spec.probe_id for spec in self.probes]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError("source probe catalog declares a duplicate probe id")
        return self

    def spec_by_id(self, probe_id: str) -> SourceProbeSpec | None:
        for spec in self.probes:
            if spec.probe_id == probe_id:
                return spec
        return None

    def catalog_seal(self) -> str:
        """Reproducible seal over the whole catalog, in its own domain."""
        return seal(SOURCE_PROBE_CATALOG_SEAL_DOMAIN, self.canonical_payload())


@dataclass(frozen=True)
class SourceProbeObservationResult:
    """One probe's outcome, its updated state, and non-raw provenance.

    Never carries raw file content or the catalog's query text — only what
    :class:`~latent_compass.lab.observations.LiteralMatchReport` and
    :class:`~latent_compass.lab.observations.SourceSnapshotRevalidation`
    themselves expose: ids, digests, sizes and line numbers.
    """

    state: DiagnosisStateRevision
    probe_id: str
    outcome_id: str
    match_report: LiteralMatchReport
    pre_read_revalidation: SourceSnapshotRevalidation
    post_read_revalidation: SourceSnapshotRevalidation


def load_source_probe_catalog(payload: object) -> SourceProbeCatalog:
    """Validate a raw JSON-shaped payload as a :class:`SourceProbeCatalog`."""
    if not isinstance(payload, dict):
        raise LabSourceSessionViolationError(
            "source probe catalog must be a JSON object",
            detail={"reason": "not_an_object", "received_type": type(payload).__name__},
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabSourceSessionViolationError(
            "source probe catalog must declare contract_version",
            detail={"reason": "contract_version_absent"},
        )
    check_contract_version(version, SUPPORTED_SOURCE_SESSION_VERSIONS, "source probe catalog")
    return validate_contract(
        SourceProbeCatalog,
        payload,
        error=LabSourceSessionViolationError,
        context="source probe catalog",
    )


def _require_catalog_matches_model(model: DiagnosisModel, catalog: SourceProbeCatalog) -> None:
    model_probe_ids = frozenset(probe.id for probe in model.probes)
    catalog_probe_ids = frozenset(spec.probe_id for spec in catalog.probes)
    if model_probe_ids != catalog_probe_ids:
        raise LabSourceSessionViolationError(
            "source probe catalog does not exactly cover the model's declared probes",
            detail={
                "reason": "catalog_probe_coverage_mismatch",
                "missing_probe_ids": sorted(model_probe_ids - catalog_probe_ids),
                "extra_probe_ids": sorted(catalog_probe_ids - model_probe_ids),
            },
        )
    for spec in catalog.probes:
        probe = model.probe_by_id(spec.probe_id)
        assert probe is not None  # guaranteed by the coverage check above
        if (
            spec.present_outcome_id not in probe.outcome_space
            or spec.absent_outcome_id not in probe.outcome_space
        ):
            raise LabSourceSessionViolationError(
                "catalog outcome ids for a probe are not declared by the model's own probe",
                detail={
                    "reason": "catalog_outcome_undeclared",
                    "probe_id": spec.probe_id,
                    "outcome_space": list(probe.outcome_space),
                },
            )


def _require_catalog_paths_within_snapshot(
    snapshot: SourceSnapshot, catalog: SourceProbeCatalog
) -> None:
    manifest_paths = frozenset(identity.relative_path for identity in snapshot.manifest)
    for spec in catalog.probes:
        undeclared = sorted(set(spec.relative_paths) - manifest_paths)
        if undeclared:
            raise LabSourceSessionViolationError(
                "catalog probe names relative path(s) outside the supplied source snapshot",
                detail={
                    "reason": "catalog_path_outside_snapshot",
                    "probe_id": spec.probe_id,
                    "undeclared_relative_paths": undeclared,
                },
            )


def derive_lab_binding(
    model: DiagnosisModel, snapshot: SourceSnapshot, catalog: SourceProbeCatalog
) -> LabBinding:
    """Derive this session's :class:`LabBinding` from the model, snapshot and catalog.

    ``host_id``/``agent_family`` come from ``snapshot.host``.
    ``source_scope_digest`` seals the model's *full* ``model_seal()``, the
    snapshot's *full* ``manifest_seal()``, its ``root_id`` and the *complete*
    catalog's own seal — so a catalog or query change never silently reuses a
    prior episode whose source bytes happen to be unchanged, even though this
    module never scans a whole repository and never reads source itself.

    Refuses before sealing anything when the catalog does not exactly cover
    the model's declared probes, when a catalog probe's declared present/
    absent outcome id is not one the model's own probe declares, or when a
    catalog probe names a relative path outside the supplied snapshot's
    manifest.
    """
    model = load_model(model.canonical_payload())
    snapshot = validate_contract(
        SourceSnapshot,
        snapshot.canonical_payload(),
        error=LabSourceSessionViolationError,
        context="source snapshot",
    )
    catalog = load_source_probe_catalog(catalog.canonical_payload())

    _require_catalog_matches_model(model, catalog)
    _require_catalog_paths_within_snapshot(snapshot, catalog)

    digest = seal(
        SOURCE_SESSION_BINDING_SEAL_DOMAIN,
        {
            "model_seal": model.model_seal(),
            "manifest_seal": snapshot.manifest_seal(),
            "root_id": snapshot.root_id,
            "catalog_seal": catalog.catalog_seal(),
        },
    )
    return LabBinding(
        host_id=snapshot.host.host_id,
        agent_family=snapshot.host.agent_family,
        source_scope_digest=digest,
    )


def _confirm_state_replays(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    binding: LabBinding,
    history: Sequence[DiagnosisStateRevision] | None,
) -> None:
    """Confirm ``state`` replays under ``binding``, through public entry points only.

    Mirrors what :func:`~latent_compass.lab.state.apply_observation` checks
    internally before ever filtering a posterior, so this module refuses a
    tampered or non-replaying state before touching the filesystem for one
    probe's read — using only :func:`~latent_compass.lab.state.initial_state`
    and :func:`~latent_compass.lab.state.verify_state_history`, never that
    module's private replay helper.
    """
    if state.revision == 0:
        expected = initial_state(model, state_id=state.state_id, binding=binding)
        if expected.canonical_payload() != state.canonical_payload():
            raise LabReplayViolationError(
                "diagnosis state does not reproduce the model's declared prior under this "
                "session's derived binding",
                detail={"state_id": state.state_id},
            )
        return
    if not history:
        raise LabReplayViolationError(
            "a diagnosis state above revision 0 requires its full replay history; none was "
            "supplied",
            detail={"state_id": state.state_id, "revision": state.revision},
        )
    verify_state_history(model, binding, history)
    if history[-1].canonical_payload() != state.canonical_payload():
        raise LabReplayViolationError(
            "the supplied history does not end at the presented diagnosis state",
            detail={"state_id": state.state_id, "revision": state.revision},
        )


def observe_probe_from_source(
    model: DiagnosisModel,
    prior: DiagnosisStateRevision,
    *,
    root: Path,
    snapshot: SourceSnapshot,
    catalog: SourceProbeCatalog,
    probe_id: str,
    observation_id: str,
    observed_at: str,
    expected_host_id: str,
    expected_agent_family: AgentFamily,
    expected_root_id: str,
    history: Sequence[DiagnosisStateRevision] | None = None,
) -> SourceProbeObservationResult:
    """Turn one caller-selected probe's bounded literal read into a core observation.

    Order of refusal, every one leaving ``prior`` unchanged: ``model``/
    ``prior``/``snapshot``/``catalog`` fail revalidation from their own
    canonical payloads; ``prior`` was computed against a different sealed
    model; the catalog does not exactly cover the model's probes, names an
    outcome the model does not declare, or names a path outside ``snapshot``
    (all raised by :func:`derive_lab_binding`); ``prior``'s binding does not
    match the binding this session derives from ``model``/``snapshot``/
    ``catalog``; ``prior`` does not replay from ``history``; ``probe_id`` was
    already acquired in this episode or is unknown to the catalog; the
    caller's declared ``expected_host_id``/``expected_agent_family``/
    ``expected_root_id`` do not match what ``snapshot`` itself declares (only
    surfaced once reading begins, by
    :func:`~latent_compass.lab.observations.revalidate_source_snapshot`); the
    snapshot has drifted, before or after the read; any file this probe reads
    is binary or not valid UTF-8 (never treated as ``absent``); or any read
    result's same-read byte digest or size does not match the captured
    manifest.

    Maps *any* match across the probe's declared UTF-8 files to
    ``present_outcome_id`` and zero matches across all of them to
    ``absent_outcome_id``, then calls
    :func:`~latent_compass.lab.state.apply_observation` with the session's own
    derived binding. Returns the updated state alongside the source match
    report and both snapshot revalidations — never raw file content, never
    the catalog's query text.
    """
    model = load_model(model.canonical_payload())
    prior = load_state(prior.canonical_payload())
    snapshot = validate_contract(
        SourceSnapshot,
        snapshot.canonical_payload(),
        error=LabSourceSessionViolationError,
        context="source snapshot",
    )
    catalog = load_source_probe_catalog(catalog.canonical_payload())

    if prior.model_seal != model.model_seal():
        raise LabCrossModelStateError(
            "diagnosis state was computed against a different sealed model",
            detail={
                "state_model_seal": prior.model_seal,
                "presented_model_seal": model.model_seal(),
            },
        )

    expected_binding = derive_lab_binding(model, snapshot, catalog)

    if (
        prior.binding.host_id != expected_binding.host_id
        or prior.binding.agent_family != expected_binding.agent_family
    ):
        raise LabBindingMismatchError(
            "diagnosis state's binding does not match the host/agent-family this session "
            "derives from the snapshot",
            detail={
                "state_host_id": prior.binding.host_id,
                "derived_host_id": expected_binding.host_id,
                "state_agent_family": prior.binding.agent_family.value,
                "derived_agent_family": expected_binding.agent_family.value,
            },
        )
    if prior.binding.source_scope_digest != expected_binding.source_scope_digest:
        raise LabSourceScopeMismatchError(
            "this session's derived source scope (model, manifest, root and catalog) has "
            "drifted from the episode's; a new episode is required",
            detail={
                "episode_source_scope_digest": prior.binding.source_scope_digest,
                "derived_source_scope_digest": expected_binding.source_scope_digest,
            },
        )

    _confirm_state_replays(model, prior, expected_binding, history)

    if probe_id in prior.acquired_probe_ids():
        raise LabRepeatedProbeError(
            "this probe was already acquired in this episode",
            detail={"probe_id": probe_id, "state_id": prior.state_id, "revision": prior.revision},
        )
    spec = catalog.spec_by_id(probe_id)
    if spec is None:
        raise LabUnknownReferenceError(
            "source probe catalog declares no such probe", detail={"probe_id": probe_id}
        )

    expected_host = HostBinding(host_id=expected_host_id, agent_family=expected_agent_family)

    pre_revalidation = revalidate_source_snapshot(
        root,
        snapshot,
        host=expected_host,
        root_id=expected_root_id,
        revalidated_at=observed_at,
    )
    if pre_revalidation.status is DriftStatus.DRIFTED:
        raise LabSourceSessionViolationError(
            "source snapshot drifted before this probe's read began",
            detail={"reason": "pre_read_drift", "probe_id": probe_id},
        )

    targets = [Path(relative_path) for relative_path in spec.relative_paths]
    match_report = observe_literal_matches(
        root,
        targets,
        spec.query,
        host=expected_host,
        root_id=expected_root_id,
        observed_at=observed_at,
        max_bytes_per_file=snapshot.max_bytes_per_file,
        max_matches_per_file=_MAX_MATCHES_PER_PROBE_READ,
        declared_git_head=snapshot.declared_git_head,
    )

    identity_by_path = {identity.relative_path: identity for identity in snapshot.manifest}
    any_match = False
    for result in match_report.results:
        identity = identity_by_path[result.relative_path]
        if result.encoding is SourceEncoding.BINARY_OR_INVALID_UTF8:
            raise LabSourceSessionViolationError(
                "a file in this probe's declared coverage is binary or not valid UTF-8; this "
                "is never mapped to the absent outcome",
                detail={
                    "reason": "binary_or_invalid_utf8",
                    "probe_id": probe_id,
                    "relative_path": result.relative_path,
                },
            )
        if result.byte_digest != identity.byte_digest or result.size_bytes != identity.size_bytes:
            raise LabSourceSessionViolationError(
                "this probe's same-read byte digest or size does not match the captured manifest",
                detail={
                    "reason": "same_read_digest_mismatch",
                    "probe_id": probe_id,
                    "relative_path": result.relative_path,
                },
            )
        if result.match_count > 0:
            any_match = True

    post_revalidation = revalidate_source_snapshot(
        root,
        snapshot,
        host=expected_host,
        root_id=expected_root_id,
        revalidated_at=observed_at,
    )
    if post_revalidation.status is DriftStatus.DRIFTED:
        raise LabSourceSessionViolationError(
            "source snapshot drifted during this probe's read",
            detail={"reason": "post_read_drift", "probe_id": probe_id},
        )

    outcome_id = spec.present_outcome_id if any_match else spec.absent_outcome_id
    updated_state = apply_observation(
        model,
        prior,
        observation_id=observation_id,
        probe_id=probe_id,
        outcome_id=outcome_id,
        observed_at=observed_at,
        expected_binding=expected_binding,
        history=history,
    )
    return SourceProbeObservationResult(
        state=updated_state,
        probe_id=probe_id,
        outcome_id=outcome_id,
        match_report=match_report,
        pre_read_revalidation=pre_revalidation,
        post_read_revalidation=post_revalidation,
    )

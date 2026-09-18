"""HOK-801/802 — the bridge from admitted observations to the finite model.

:mod:`latent_compass.lab.source_session` bridges one observation kind: a
bounded literal read the lab performs itself, with two outcomes. This module
bridges all five kinds of :mod:`latent_compass.lab.host_observations` through
one catalog, one binding and one rule for what an observation may conclude.

A :class:`HostProbeCatalog` declares, once per session, for every probe of the
model: its observation kind, its exact declared coverage, the question it asks,
the **exact tool identity and version** expected to answer it, and how a
result maps onto the probe's own declared outcomes. :func:`derive_host_lab_binding`
seals the model, the snapshot manifest, its root, the complete catalog and the
session's :class:`~latent_compass.lab.host_observations.ObservationPolicy` into
the episode's :class:`~latent_compass.lab.state.LabBinding` — so a tool
upgrade, a changed question, a widened coverage or a relaxed policy never
silently reuses a prior episode.

:func:`apply_host_observation` is the one function that turns an admitted
observation into a core observation. It performs no read of its own beyond
:func:`~latent_compass.lab.host_observations.verify_host_observation`, selects
no probe, routes nothing and accounts for no budget. Its result is one of:

* an **outcome** — the verified result maps onto exactly one declared outcome
  and the state advances by one revision;
* **UNKNOWN** — a timeout, an absent tool, an unsupported language, a tool
  failure, an errored check or a truncated search that found nothing. The
  prior state is returned untouched, the probe is *not* marked acquired, and
  nothing is retried, repaired or routed to another provider here;
* a **refusal** — anything inconsistent with the session or with every
  declared world, raised as a typed error with the prior left unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
from latent_compass.lab.host_observations import (
    CheckVerdict,
    Conclusiveness,
    HostObservation,
    HostObservationVerification,
    ObservationKind,
    ObservationMode,
    ObservationPolicy,
    SymbolRelation,
    ToolIdentity,
    admit_host_observation,
    admit_observation_policy,
    observation_from_literal_report,
    question_digest,
    verify_host_observation,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.observations import (
    HostBinding,
    RelativeSourcePath,
    SourceSnapshot,
    observe_literal_matches,
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
    "HOST_SESSION_CONTRACT_VERSION",
    "MAX_RELATIVE_PATHS_PER_HOST_PROBE",
    "SUPPORTED_HOST_SESSION_VERSIONS",
    "HostProbeCatalog",
    "HostProbeResult",
    "HostProbeSpec",
    "LabHostSessionViolationError",
    "UnknownReason",
    "apply_host_observation",
    "derive_host_lab_binding",
    "load_host_probe_catalog",
    "observe_literal_probe",
]

#: Its own experimental axis, free to move independently of the core, of the
#: observation envelope and of the literal source-session bridge.
HOST_SESSION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_HOST_SESSION_VERSIONS: Final = frozenset({HOST_SESSION_CONTRACT_VERSION})

HOST_SESSION_BINDING_SEAL_DOMAIN: Final = "lab.host-session.binding.v1"
HOST_PROBE_CATALOG_SEAL_DOMAIN: Final = "lab.host-session.catalog.v1"

MAX_RELATIVE_PATHS_PER_HOST_PROBE: Final = 16
_MAX_QUESTION_LENGTH: Final = 4096
_MAX_MATCHES_PER_LITERAL_READ: Final = 16


class LabHostSessionViolationError(LabContractViolationError):
    """A HOK-801/802 host-session bridge rule was broken.

    ``detail["reason"]`` names the exact case. No variant of this error ever
    leaves the caller's prior state changed.
    """

    code = "lab_host_session_violation"


class UnknownReason(StrEnum):
    NON_CONCLUSIVE_STATUS = "NON_CONCLUSIVE_STATUS"
    CHECK_ERRORED = "CHECK_ERRORED"
    TRUNCATION_MAY_HIDE_ANOTHER_OUTCOME = "TRUNCATION_MAY_HIDE_ANOTHER_OUTCOME"


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


_QuestionText = Annotated[
    str,
    Field(min_length=1, max_length=_MAX_QUESTION_LENGTH),
    AfterValidator(_no_control_characters),
]

#: Kinds whose result is *where* something was found, or that nothing was.
_LOCATED_KINDS: Final = frozenset(
    {
        ObservationKind.LITERAL_SEARCH,
        ObservationKind.SYMBOL_NAVIGATION,
        ObservationKind.GIT_DIFF,
    }
)


class HostProbeSpec(StrictModel):
    """How one model probe is answered, and what its answer may conclude.

    ``question`` and ``anchor`` are configuration this module treats as opaque
    and never reproduces in a report or an error. For a located kind, a result
    found in a path maps to ``outcome_by_path[path]`` and a conclusive empty
    result maps to ``empty_outcome_id``. A targeted check maps its declared
    verdict. A file read maps the host's own flagged interpretation.
    """

    probe_id: Identifier
    kind: ObservationKind
    relative_paths: tuple[RelativeSourcePath, ...] = Field(
        min_length=1, max_length=MAX_RELATIVE_PATHS_PER_HOST_PROBE
    )
    question: _QuestionText
    anchor: _QuestionText | None = Field(default=None)
    tool: ToolIdentity
    symbol_relation: SymbolRelation | None = Field(default=None)
    outcome_by_path: dict[RelativeSourcePath, Identifier] = Field(default_factory=dict)
    empty_outcome_id: Identifier | None = Field(default=None)
    passed_outcome_id: Identifier | None = Field(default=None)
    failed_outcome_id: Identifier | None = Field(default=None)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.relative_paths)) != len(self.relative_paths):
            raise ValueError(f"probe {self.probe_id!r} declares a duplicate relative path")
        if list(self.relative_paths) != sorted(self.relative_paths):
            raise ValueError(f"probe {self.probe_id!r} relative paths must be sorted")
        if (self.kind is ObservationKind.SYMBOL_NAVIGATION) != (self.symbol_relation is not None):
            raise ValueError(f"probe {self.probe_id!r}: only a symbol navigation names a relation")
        if self.kind is ObservationKind.LITERAL_SEARCH and self.anchor != self.question:
            raise ValueError(f"probe {self.probe_id!r}: a literal search is anchored on its query")

        located = self.kind in _LOCATED_KINDS
        if located:
            if set(self.outcome_by_path) != set(self.relative_paths):
                raise ValueError(
                    f"probe {self.probe_id!r} must map every declared path, and only those, "
                    "to an outcome"
                )
            if self.empty_outcome_id is None:
                raise ValueError(f"probe {self.probe_id!r} must name its empty outcome")
            if self.empty_outcome_id in set(self.outcome_by_path.values()):
                raise ValueError(
                    f"probe {self.probe_id!r}: the empty outcome must differ from every "
                    "located outcome"
                )
        elif self.outcome_by_path or self.empty_outcome_id is not None:
            raise ValueError(f"probe {self.probe_id!r}: only a located kind maps paths to outcomes")

        checked = self.kind is ObservationKind.TARGETED_CHECK
        verdicts = (self.passed_outcome_id, self.failed_outcome_id)
        if checked:
            if None in verdicts or self.passed_outcome_id == self.failed_outcome_id:
                raise ValueError(
                    f"probe {self.probe_id!r} must name two distinct passed/failed outcomes"
                )
        elif verdicts != (None, None):
            raise ValueError(f"probe {self.probe_id!r}: only a targeted check maps verdicts")
        return self

    def declared_outcomes(self) -> frozenset[str]:
        """Every outcome id this spec can ever produce. Empty for a file read."""
        outcomes = set(self.outcome_by_path.values())
        outcomes.update(
            outcome
            for outcome in (self.empty_outcome_id, self.passed_outcome_id, self.failed_outcome_id)
            if outcome is not None
        )
        return frozenset(outcomes)


class HostProbeCatalog(StrictModel):
    """A versioned, session-scoped catalog covering every probe of one model."""

    contract_version: str = Field(min_length=5, max_length=20)
    probes: tuple[HostProbeSpec, ...] = Field(min_length=1, max_length=MAX_PROBES)
    #: Model probes no source observation can answer — an authored requirement,
    #: an owner's ruling. Declared, so their omission is a statement and not an
    #: accident; this bridge never applies them.
    external_probe_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_PROBES)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_HOST_SESSION_VERSIONS, "host probe catalog"
        )
        probe_ids = [spec.probe_id for spec in self.probes] + list(self.external_probe_ids)
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError("host probe catalog declares a duplicate probe id")
        if list(self.external_probe_ids) != sorted(self.external_probe_ids):
            raise ValueError("external probe ids must be sorted")
        return self

    def spec_by_id(self, probe_id: str) -> HostProbeSpec | None:
        for spec in self.probes:
            if spec.probe_id == probe_id:
                return spec
        return None

    def catalog_seal(self) -> str:
        """Reproducible seal over the whole catalog, tool identities included."""
        return seal(HOST_PROBE_CATALOG_SEAL_DOMAIN, self.canonical_payload())


@dataclass(frozen=True)
class HostProbeResult:
    """One admitted observation's effect on the episode.

    ``outcome_id`` is ``None`` exactly when ``unknown_reason`` is set; ``state``
    is then the caller's own prior, revision unchanged. Never carries question
    or anchor text, nor raw file content.
    """

    state: DiagnosisStateRevision
    probe_id: str
    outcome_id: str | None
    unknown_reason: UnknownReason | None
    verification: HostObservationVerification


def load_host_probe_catalog(payload: object) -> HostProbeCatalog:
    """Validate a raw JSON-shaped payload as a :class:`HostProbeCatalog`."""
    if not isinstance(payload, dict):
        raise LabHostSessionViolationError(
            "host probe catalog must be a JSON object",
            detail={"reason": "not_an_object", "received_type": type(payload).__name__},
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabHostSessionViolationError(
            "host probe catalog must declare contract_version",
            detail={"reason": "contract_version_absent"},
        )
    check_contract_version(version, SUPPORTED_HOST_SESSION_VERSIONS, "host probe catalog")
    return validate_contract(
        HostProbeCatalog,
        payload,
        error=LabHostSessionViolationError,
        context="host probe catalog",
    )


def _refuse(message: str, reason: str, **detail: object) -> LabHostSessionViolationError:
    return LabHostSessionViolationError(message, detail={"reason": reason, **detail})


def _require_catalog_matches_model(model: DiagnosisModel, catalog: HostProbeCatalog) -> None:
    model_probe_ids = frozenset(probe.id for probe in model.probes)
    catalog_probe_ids = frozenset(spec.probe_id for spec in catalog.probes) | frozenset(
        catalog.external_probe_ids
    )
    if model_probe_ids != catalog_probe_ids:
        raise _refuse(
            "host probe catalog does not exactly cover the model's declared probes",
            "catalog_probe_coverage_mismatch",
            missing_probe_ids=sorted(model_probe_ids - catalog_probe_ids),
            extra_probe_ids=sorted(catalog_probe_ids - model_probe_ids),
        )
    for spec in catalog.probes:
        probe = model.probe_by_id(spec.probe_id)
        assert probe is not None  # guaranteed by the coverage check above
        undeclared = sorted(spec.declared_outcomes() - set(probe.outcome_space))
        if undeclared:
            raise _refuse(
                "catalog outcome ids for a probe are not declared by the model's own probe",
                "catalog_outcome_undeclared",
                probe_id=spec.probe_id,
                undeclared_outcome_ids=undeclared,
            )


def _require_catalog_fits_session(
    snapshot: SourceSnapshot, catalog: HostProbeCatalog, policy: ObservationPolicy
) -> None:
    manifest_paths = frozenset(identity.relative_path for identity in snapshot.manifest)
    for spec in catalog.probes:
        outside = sorted(set(spec.relative_paths) - manifest_paths)
        if outside:
            raise _refuse(
                "catalog probe names relative path(s) outside the supplied source snapshot",
                "catalog_path_outside_snapshot",
                probe_id=spec.probe_id,
                undeclared_relative_paths=outside,
            )
        if (
            policy.mode is ObservationMode.SOURCE_ONLY
            and spec.kind is ObservationKind.SYMBOL_NAVIGATION
        ):
            raise _refuse(
                "a SOURCE_ONLY session cannot declare a symbol navigation probe",
                "catalog_kind_outside_mode",
                probe_id=spec.probe_id,
            )
        if spec.tool.provider_id in policy.retired_providers:
            raise _refuse(
                "catalog probe expects a tool from a retired provider",
                "catalog_tool_provider_retired",
                probe_id=spec.probe_id,
            )


def derive_host_lab_binding(
    model: DiagnosisModel,
    snapshot: SourceSnapshot,
    catalog: HostProbeCatalog,
    policy: ObservationPolicy,
) -> LabBinding:
    """Seal one session's :class:`LabBinding` from everything that bounds it.

    ``source_scope_digest`` seals the model's full ``model_seal()``, the
    snapshot's full ``manifest_seal()``, its ``root_id``, the complete catalog
    — every question, coverage, outcome mapping and **tool identity** — and
    the session policy. Refuses before sealing when the catalog does not
    exactly cover the model's probes, names an outcome the model's probe does
    not declare, names a path outside the snapshot, declares a symbol
    navigation in a ``SOURCE_ONLY`` session, or expects a retired provider.
    """
    model = load_model(model.canonical_payload())
    snapshot = validate_contract(
        SourceSnapshot,
        snapshot.canonical_payload(),
        error=LabHostSessionViolationError,
        context="source snapshot",
    )
    catalog = load_host_probe_catalog(catalog.canonical_payload())
    policy = admit_observation_policy(policy.canonical_payload())

    _require_catalog_matches_model(model, catalog)
    _require_catalog_fits_session(snapshot, catalog, policy)

    digest = seal(
        HOST_SESSION_BINDING_SEAL_DOMAIN,
        {
            "model_seal": model.model_seal(),
            "manifest_seal": snapshot.manifest_seal(),
            "root_id": snapshot.root_id,
            "catalog_seal": catalog.catalog_seal(),
            "policy_seal": policy.policy_seal(),
        },
    )
    return LabBinding(
        host_id=snapshot.host.host_id,
        agent_family=snapshot.host.agent_family,
        source_scope_digest=digest,
    )


def observe_literal_probe(
    *,
    root: Path,
    snapshot: SourceSnapshot,
    catalog: HostProbeCatalog,
    policy: ObservationPolicy,
    probe_id: str,
    observation_id: str,
    observed_at: str,
) -> HostObservation:
    """Answer one ``LITERAL_SEARCH`` probe with the lab's own confined reader.

    The only observation this module ever produces itself. Every other kind
    comes from the host executor.
    """
    catalog = load_host_probe_catalog(catalog.canonical_payload())
    policy = admit_observation_policy(policy.canonical_payload())
    spec = catalog.spec_by_id(probe_id)
    if spec is None:
        raise LabUnknownReferenceError(
            "host probe catalog declares no such probe", detail={"probe_id": probe_id}
        )
    if spec.kind is not ObservationKind.LITERAL_SEARCH:
        raise _refuse(
            "the lab's own reader answers literal searches only",
            "probe_is_not_a_literal_search",
            probe_id=probe_id,
            kind=spec.kind.value,
        )
    report = observe_literal_matches(
        root,
        [Path(relative_path) for relative_path in spec.relative_paths],
        spec.question,
        host=snapshot.host,
        root_id=snapshot.root_id,
        observed_at=observed_at,
        max_bytes_per_file=snapshot.max_bytes_per_file,
        max_matches_per_file=_MAX_MATCHES_PER_LITERAL_READ,
        declared_git_head=snapshot.declared_git_head,
    )
    return observation_from_literal_report(
        report, observation_id=observation_id, probe_id=probe_id, mode=policy.mode
    )


def _confirm_state_replays(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    binding: LabBinding,
    history: Sequence[DiagnosisStateRevision] | None,
) -> None:
    """Confirm ``state`` replays under ``binding``, through public entry points only."""
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


def _require_observation_matches_spec(observation: HostObservation, spec: HostProbeSpec) -> None:
    probe_id = spec.probe_id
    if observation.kind is not spec.kind:
        raise _refuse(
            "observation kind is not the kind the catalog declares for this probe",
            "kind_mismatch",
            probe_id=probe_id,
            observation_kind=observation.kind.value,
            catalog_kind=spec.kind.value,
        )
    if observation.question_digest != question_digest(spec.question):
        raise _refuse(
            "observation answers a different question than the catalog's",
            "question_mismatch",
            probe_id=probe_id,
        )
    if observation.tool != spec.tool:
        raise _refuse(
            "observation was produced by a different tool identity or version than the "
            "episode is bound to; a new episode is required",
            "tool_identity_mismatch",
            probe_id=probe_id,
            observed_tool_id=observation.tool.tool_id,
            observed_version=observation.tool.version,
        )
    if observation.covered_paths != spec.relative_paths:
        raise _refuse(
            "observation coverage is not exactly the coverage the catalog declares",
            "coverage_mismatch",
            probe_id=probe_id,
        )
    if spec.symbol_relation is not None and (
        observation.symbol is None or observation.symbol.relation is not spec.symbol_relation
    ):
        raise _refuse(
            "observation navigated a different symbol relation than the catalog declares",
            "symbol_relation_mismatch",
            probe_id=probe_id,
        )


def _outcome_of(
    observation: HostObservation,
    spec: HostProbeSpec,
    model: DiagnosisModel,
    conclusiveness: Conclusiveness,
) -> tuple[str | None, UnknownReason | None]:
    probe_id = spec.probe_id
    if spec.kind is ObservationKind.TARGETED_CHECK:
        assert observation.check is not None  # guaranteed by the envelope's own contract
        verdict = observation.check.verdict
        if verdict is CheckVerdict.PASSED:
            return spec.passed_outcome_id, None
        if verdict is CheckVerdict.FAILED:
            return spec.failed_outcome_id, None
        # An infrastructure or collection error is never counted as a detection.
        return None, UnknownReason.CHECK_ERRORED

    if spec.kind is ObservationKind.FILE_READ:
        interpreted = observation.interpreted_outcome_id
        if interpreted is None:
            raise _refuse(
                "a file read carries no outcome unless the host declares its interpretation",
                "file_read_not_interpreted",
                probe_id=probe_id,
            )
        probe = model.probe_by_id(probe_id)
        assert probe is not None  # guaranteed by the catalog coverage check
        if interpreted not in probe.outcome_space:
            raise LabUnknownReferenceError(
                "interpreted outcome is not declared by the model's own probe",
                detail={"probe_id": probe_id, "outcome_space": list(probe.outcome_space)},
            )
        return interpreted, None

    located = {item.relative_path for item in observation.evidence}
    if observation.git_diff is not None:
        located.update(entry.relative_path for entry in observation.git_diff.entries)
    if not located:
        return spec.empty_outcome_id, None
    if conclusiveness is Conclusiveness.PRESENCE_ONLY and (
        len(set(spec.outcome_by_path.values())) > 1
    ):
        # What a truncated output omitted may sit in a path that answers differently.
        return None, UnknownReason.TRUNCATION_MAY_HIDE_ANOTHER_OUTCOME
    outcomes = sorted({spec.outcome_by_path[relative_path] for relative_path in located})
    if len(outcomes) > 1:
        # No declared world explains results in paths that answer differently.
        raise _refuse(
            "the verified result maps onto several declared outcomes; no declared world "
            "explains it, and it is not resolved by picking one",
            "result_maps_to_several_outcomes",
            probe_id=probe_id,
            outcome_ids=outcomes,
        )
    return outcomes[0], None


def apply_host_observation(
    model: DiagnosisModel,
    prior: DiagnosisStateRevision,
    *,
    root: Path,
    snapshot: SourceSnapshot,
    catalog: HostProbeCatalog,
    policy: ObservationPolicy,
    observation: HostObservation,
    expected_host_id: str,
    expected_agent_family: AgentFamily,
    expected_root_id: str,
    verified_at: str,
    history: Sequence[DiagnosisStateRevision] | None = None,
) -> HostProbeResult:
    """Turn one admitted observation into a core observation, or into UNKNOWN.

    Order of refusal, every one leaving ``prior`` unchanged: a public input
    fails revalidation; ``prior`` was computed against a different sealed
    model; the catalog does not fit the model, the snapshot or the policy (all
    raised by :func:`derive_host_lab_binding`); ``prior``'s binding is not the
    one this session derives; ``prior`` does not replay from ``history``; the
    probe was already acquired or is unknown to the catalog; the observation's
    kind, question, **tool identity**, coverage or symbol relation is not the
    catalog's; verification against the snapshot refuses (drift, foreign
    bytes, a missing line, an absent anchor, a contradicted empty search, a
    retired provider, a wrong mode or scope); or the verified result maps onto
    several declared outcomes.

    A non-conclusive observation and an errored check are **not** refusals:
    they return ``outcome_id=None`` with the prior state untouched, so the
    caller can account for the attempt without the model ever consuming it.
    """
    model = load_model(model.canonical_payload())
    prior = load_state(prior.canonical_payload())
    catalog = load_host_probe_catalog(catalog.canonical_payload())
    policy = admit_observation_policy(policy.canonical_payload())
    observation = admit_host_observation(observation.canonical_payload())

    if prior.model_seal != model.model_seal():
        raise LabCrossModelStateError(
            "diagnosis state was computed against a different sealed model",
            detail={
                "state_model_seal": prior.model_seal,
                "presented_model_seal": model.model_seal(),
            },
        )

    expected_binding = derive_host_lab_binding(model, snapshot, catalog, policy)
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
            "this session's derived scope (model, manifest, root, catalog, tools and policy) "
            "has drifted from the episode's; a new episode is required",
            detail={
                "episode_source_scope_digest": prior.binding.source_scope_digest,
                "derived_source_scope_digest": expected_binding.source_scope_digest,
            },
        )

    _confirm_state_replays(model, prior, expected_binding, history)

    probe_id = observation.probe_id
    if probe_id in prior.acquired_probe_ids():
        raise LabRepeatedProbeError(
            "this probe was already acquired in this episode",
            detail={"probe_id": probe_id, "state_id": prior.state_id, "revision": prior.revision},
        )
    if probe_id in catalog.external_probe_ids:
        raise _refuse(
            "this probe is declared external to source; no source observation answers it",
            "probe_is_external",
            probe_id=probe_id,
        )
    spec = catalog.spec_by_id(probe_id)
    if spec is None:
        raise LabUnknownReferenceError(
            "host probe catalog declares no such probe", detail={"probe_id": probe_id}
        )
    _require_observation_matches_spec(observation, spec)

    verification = verify_host_observation(
        root,
        observation,
        snapshot=snapshot,
        policy=policy,
        expected_host=HostBinding(host_id=expected_host_id, agent_family=expected_agent_family),
        expected_root_id=expected_root_id,
        verified_at=verified_at,
        anchor=spec.anchor,
    )
    if verification.conclusiveness is Conclusiveness.NONE:
        return HostProbeResult(
            state=prior,
            probe_id=probe_id,
            outcome_id=None,
            unknown_reason=UnknownReason.NON_CONCLUSIVE_STATUS,
            verification=verification,
        )

    outcome_id, unknown_reason = _outcome_of(observation, spec, model, verification.conclusiveness)
    if outcome_id is None:
        return HostProbeResult(
            state=prior,
            probe_id=probe_id,
            outcome_id=None,
            unknown_reason=unknown_reason,
            verification=verification,
        )

    updated_state = apply_observation(
        model,
        prior,
        observation_id=observation.observation_id,
        probe_id=probe_id,
        outcome_id=outcome_id,
        observed_at=observation.observed_at,
        expected_binding=expected_binding,
        history=history,
    )
    return HostProbeResult(
        state=updated_state,
        probe_id=probe_id,
        outcome_id=outcome_id,
        unknown_reason=None,
        verification=verification,
    )

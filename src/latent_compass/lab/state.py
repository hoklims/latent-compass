"""HOK-798 — the diagnosis state: exact posterior filtering with replay binding.

A :class:`DiagnosisStateRevision` is one immutable link in an append-only
chain. Revision ``0`` is the prior, unfiltered by any observation. Each later
revision names the exact model it was computed against, the exact prior
revision it extends, and the one observation that produced it, and carries the
posterior that results from **exactly filtering out every world inconsistent
with that observation** — no approximation, no re-normalisation beyond exact
integer weights, because a probe's outcome is a deterministic function of the
world (see :mod:`latent_compass.lab.model`).

The entire observation episode binds one immutable ``source_scope_digest``.
Applying an observation under a different digest, or against a different
sealed model, is refused before any filtering happens. An observation
inconsistent with every remaining world is refused outside the model: it is
never converted into a terminal decision, and the prior state is left
unchanged — nothing is written, nothing is returned but the refusal.

**A stored ``posterior_weights``/``observed_pairs`` pair is never trusted on
sight.** Matching the presented model's seal only proves the state was
computed against *a* model with that seal; it says nothing about whether the
posterior itself replays. Every public entry point below revalidates its
``model``/``state``/``history`` arguments from their own canonical payloads
(closing both a caller-mutated nested field and a ``model_construct`` bypass,
neither of which a frozen pydantic model prevents on its own), then either
cheaply recomputes revision 0 or requires and replays the full history for
any higher revision via :func:`verify_state_history` before doing anything
else with it. A caller-declared ``expected_binding`` is checked against the
state's own binding at this same boundary: replaying an internally
self-consistent history proves it is *a* well-formed episode, never that it
belongs to *this* caller's host, agent family or current source scope.

:func:`verify_state_history` independently recomputes every link from the
model and the recorded observations. A tampered ``posterior_weights``,
``prior_state_seal``, ``acquired`` set or ``binding`` cannot pass it: replay
recomputes what the correct value must have been and compares, rather than
trusting what is stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Self

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import AgentFamily
from latent_compass.lab.contracts import (
    LAB_CONTRACT_VERSION,
    MAX_STATE_REVISION,
    MAX_WORLDS,
    SUPPORTED_LAB_VERSIONS,
)
from latent_compass.lab.errors import (
    LabBindingMismatchError,
    LabContractViolationError,
    LabCrossModelStateError,
    LabImpossibleObservationError,
    LabRepeatedProbeError,
    LabReplayViolationError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.model import DiagnosisModel, EvidenceRequirement, Probe, load_model

__all__ = [
    "STATE_SEAL_DOMAIN",
    "DiagnosisStateRevision",
    "LabBinding",
    "ObservationEvent",
    "apply_observation",
    "initial_state",
    "load_state",
    "verify_state_history",
]

STATE_SEAL_DOMAIN: Final = "lab.state.v1"


class LabBinding(StrictModel):
    """The identity one diagnosis episode belongs to. Immutable once written.

    ``source_scope_digest`` is an opaque, caller-supplied seal identifying the
    bounded source scope the whole episode is evidence about. This package
    never computes it and never reads the source it names — supplying and
    revalidating that digest against real source content is host/adapter
    responsibility explicitly deferred by ADR 0011.
    """

    host_id: Identifier
    agent_family: AgentFamily
    source_scope_digest: Seal


class ObservationEvent(StrictModel):
    """One caller-asserted act of observing a probe's outcome."""

    observation_id: Identifier
    probe_id: Identifier
    outcome_id: Identifier
    observed_at: Timestamp


class DiagnosisStateRevision(StrictModel):
    """One immutable link in one diagnosis episode's append-only chain."""

    contract_version: str = Field(min_length=5, max_length=20)
    state_id: Identifier
    revision: int = Field(ge=0, le=MAX_STATE_REVISION)
    model_seal: Seal
    binding: LabBinding
    prior_state_seal: Seal | None = Field(default=None)
    applied_observation: ObservationEvent | None = Field(default=None)
    observed_pairs: tuple[EvidenceRequirement, ...] = Field(
        default=(), max_length=MAX_STATE_REVISION
    )
    posterior_weights: dict[Identifier, int] = Field(min_length=1, max_length=MAX_WORLDS)

    @model_validator(mode="after")
    def _state_is_coherent(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_VERSIONS, "diagnosis state")

        probe_ids = [pair.probe_id for pair in self.observed_pairs]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError("diagnosis state observed the same probe more than once")

        if not self.posterior_weights:
            raise ValueError("diagnosis state posterior must name at least one world")
        for world_id, weight in self.posterior_weights.items():
            if weight < 1:
                raise ValueError(f"posterior weight for world {world_id!r} must be positive")

        if self.revision == 0:
            if self.prior_state_seal is not None:
                raise ValueError("revision 0 supersedes nothing and must not name a prior seal")
            if self.applied_observation is not None:
                raise ValueError("revision 0 carries no applied observation")
            if self.observed_pairs:
                raise ValueError("revision 0 carries no observed pairs")
        else:
            if self.prior_state_seal is None:
                raise ValueError("a revision above 0 must name the prior state seal it extends")
            if self.applied_observation is None:
                raise ValueError("a revision above 0 must carry the observation that produced it")
            if not self.observed_pairs:
                raise ValueError("a revision above 0 must carry at least one observed pair")
            last = self.observed_pairs[-1]
            if (
                last.probe_id != self.applied_observation.probe_id
                or last.outcome_id != self.applied_observation.outcome_id
            ):
                raise ValueError(
                    "the last observed pair must match the applied observation exactly"
                )
        return self

    def state_seal(self) -> str:
        """Reproducible seal over the whole revision, in its own domain."""
        return seal(STATE_SEAL_DOMAIN, self.canonical_payload())

    def acquired_probe_ids(self) -> frozenset[str]:
        return frozenset(pair.probe_id for pair in self.observed_pairs)

    def satisfied_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((pair.probe_id, pair.outcome_id) for pair in self.observed_pairs)


def load_state(payload: object) -> DiagnosisStateRevision:
    """Validate a raw JSON-shaped payload as a :class:`DiagnosisStateRevision`."""
    if not isinstance(payload, dict):
        raise LabContractViolationError(
            "diagnosis state must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabContractViolationError(
            "diagnosis state must declare contract_version", detail={"reason": "absent"}
        )
    check_contract_version(version, SUPPORTED_LAB_VERSIONS, "diagnosis state")
    return validate_contract(
        DiagnosisStateRevision, payload, error=LabContractViolationError, context="diagnosis state"
    )


def initial_state(
    model: DiagnosisModel, *, state_id: str, binding: LabBinding
) -> DiagnosisStateRevision:
    """Revision 0: the model's declared prior, unfiltered by any observation.

    Revalidates ``model`` from its own canonical payload first: a caller
    holding a live model object may have mutated a nested field (frozen only
    blocks reassigning the field itself, not mutating a dict it points at) or
    constructed it with ``model_construct``, bypassing every bound this
    package declares. Recomputing from the canonical payload forces those
    bounds to run again before this episode's prior is ever derived from it.
    """
    model = load_model(model.canonical_payload())
    payload: dict[str, object] = {
        "contract_version": LAB_CONTRACT_VERSION,
        "state_id": state_id,
        "revision": 0,
        "model_seal": model.model_seal(),
        "binding": binding.canonical_payload(),
        "prior_state_seal": None,
        "applied_observation": None,
        "observed_pairs": [],
        "posterior_weights": {world.id: world.weight for world in model.worlds},
    }
    return validate_contract(
        DiagnosisStateRevision,
        payload,
        error=LabContractViolationError,
        context="initial diagnosis state",
    )


def _checked_transition(probe: Probe, posterior: dict[str, int], outcome_id: str) -> dict[str, int]:
    """The one place a posterior is filtered by an observed outcome.

    Shared by :func:`apply_observation` and :func:`verify_state_history` so
    the two never drift: applying an observation and replaying one are the
    same deterministic transition, computed once.
    """
    return {
        world_id: weight
        for world_id, weight in posterior.items()
        if probe.outcomes.get(world_id) == outcome_id
    }


def _trust_state(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    history: Sequence[DiagnosisStateRevision] | None,
) -> None:
    """Recompute or replay ``state`` before any public entry point trusts it.

    Revision 0 is cheap: it is recomputed directly from ``model`` and the
    state's own declared binding and compared. Any higher revision is never
    cheap — a caller-supplied ``posterior_weights``/``observed_pairs`` pair
    that only matches the presented model's seal is not evidence it replays.
    ``history`` must be the full chain from revision 0 up to and including
    ``state``; it is independently revalidated and passed to
    :func:`verify_state_history`, which refuses an omitted, reordered,
    duplicate-observation, truncated, cross-binding or tampered chain.
    """
    if state.revision == 0 and history is None:
        expected = initial_state(model, state_id=state.state_id, binding=state.binding)
        if state.canonical_payload() != expected.canonical_payload():
            raise LabReplayViolationError(
                "revision 0 does not reproduce the model's declared prior under its own binding",
                detail={"state_id": state.state_id},
            )
        return
    if not history:
        raise LabReplayViolationError(
            "a diagnosis state above revision 0 requires its full replay history; none was "
            "supplied",
            detail={"state_id": state.state_id, "revision": state.revision},
        )
    if len(history) > MAX_STATE_REVISION + 1:
        raise LabReplayViolationError("diagnosis history exceeds the revision bound")
    revalidated_history = [load_state(entry.canonical_payload()) for entry in history]
    if revalidated_history[-1].canonical_payload() != state.canonical_payload():
        raise LabReplayViolationError(
            "the supplied history does not end at the presented diagnosis state; it is "
            "truncated, extended, or for a different episode",
            detail={"state_id": state.state_id, "revision": state.revision},
        )
    verify_state_history(model, state.binding, revalidated_history)


def apply_observation(
    model: DiagnosisModel,
    prior: DiagnosisStateRevision,
    *,
    observation_id: str,
    probe_id: str,
    outcome_id: str,
    observed_at: str,
    expected_binding: LabBinding,
    history: Sequence[DiagnosisStateRevision] | None = None,
) -> DiagnosisStateRevision:
    """Filter ``prior``'s posterior by one observed ``(probe_id, outcome_id)``.

    Refuses, leaving ``prior`` untouched, when: ``model``/``prior`` fail
    revalidation from their own canonical payloads; ``prior`` was computed
    against a different sealed model; ``prior`` does not replay from
    ``history`` (required above revision 0); ``prior``'s binding does not
    match ``expected_binding``'s host, agent family or source scope; the
    probe was already acquired in this episode; the probe or outcome is not
    declared by the model; or the observation is impossible under every
    remaining world.
    """
    model = load_model(model.canonical_payload())
    prior = load_state(prior.canonical_payload())
    expected_binding = validate_contract(
        LabBinding,
        expected_binding.canonical_payload(),
        error=LabContractViolationError,
        context="expected diagnosis binding",
    )

    if prior.model_seal != model.model_seal():
        raise LabCrossModelStateError(
            "diagnosis state was computed against a different sealed model",
            detail={
                "state_model_seal": prior.model_seal,
                "presented_model_seal": model.model_seal(),
            },
        )

    _trust_state(model, prior, history)

    observation = validate_contract(
        ObservationEvent,
        {
            "observation_id": observation_id,
            "probe_id": probe_id,
            "outcome_id": outcome_id,
            "observed_at": observed_at,
        },
        error=LabContractViolationError,
        context="diagnosis observation",
    )
    for entry in history or ():
        if (
            entry.applied_observation is not None
            and entry.applied_observation.observation_id == observation.observation_id
        ):
            raise LabReplayViolationError("observation id was already used in this episode")
    if (
        prior.applied_observation is not None
        and observation.observed_at < prior.applied_observation.observed_at
    ):
        raise LabReplayViolationError("observation time moved backwards")

    if (
        prior.binding.host_id != expected_binding.host_id
        or prior.binding.agent_family != expected_binding.agent_family
    ):
        raise LabBindingMismatchError(
            "diagnosis state's binding does not match the host/agent-family a caller expects "
            "at this consumer boundary",
            detail={
                "state_host_id": prior.binding.host_id,
                "expected_host_id": expected_binding.host_id,
                "state_agent_family": prior.binding.agent_family.value,
                "expected_agent_family": expected_binding.agent_family.value,
            },
        )
    if prior.binding.source_scope_digest != expected_binding.source_scope_digest:
        raise LabSourceScopeMismatchError(
            "source scope has drifted since this episode began; a new episode is required",
            detail={
                "episode_source_scope_digest": prior.binding.source_scope_digest,
                "presented_source_scope_digest": expected_binding.source_scope_digest,
            },
        )
    if probe_id in prior.acquired_probe_ids():
        raise LabRepeatedProbeError(
            "this probe was already acquired in this episode",
            detail={"probe_id": probe_id, "state_id": prior.state_id, "revision": prior.revision},
        )
    probe = model.probe_by_id(probe_id)
    if probe is None:
        raise LabUnknownReferenceError(
            "diagnosis model declares no such probe", detail={"probe_id": probe_id}
        )
    if outcome_id not in probe.outcome_space:
        raise LabUnknownReferenceError(
            "probe declares no such outcome",
            detail={
                "probe_id": probe_id,
                "outcome_id": outcome_id,
                "outcome_space": list(probe.outcome_space),
            },
        )

    filtered = _checked_transition(probe, prior.posterior_weights, outcome_id)
    if not filtered:
        raise LabImpossibleObservationError(
            "no remaining world in the posterior is consistent with this observation; "
            "the prior state is unchanged",
            detail={
                "probe_id": probe_id,
                "outcome_id": outcome_id,
                "state_id": prior.state_id,
                "revision": prior.revision,
            },
        )

    payload: dict[str, object] = {
        "contract_version": LAB_CONTRACT_VERSION,
        "state_id": prior.state_id,
        "revision": prior.revision + 1,
        "model_seal": prior.model_seal,
        "binding": prior.binding.canonical_payload(),
        "prior_state_seal": prior.state_seal(),
        "applied_observation": {
            "observation_id": observation_id,
            "probe_id": probe_id,
            "outcome_id": outcome_id,
            "observed_at": observed_at,
        },
        "observed_pairs": [
            *(pair.canonical_payload() for pair in prior.observed_pairs),
            {"probe_id": probe_id, "outcome_id": outcome_id},
        ],
        "posterior_weights": filtered,
    }
    return validate_contract(
        DiagnosisStateRevision, payload, error=LabContractViolationError, context="diagnosis state"
    )


def verify_state_history(
    model: DiagnosisModel, binding: LabBinding, history: Sequence[DiagnosisStateRevision]
) -> None:
    """Recompute every link of ``history`` from ``model`` and refuse on any drift.

    ``model`` is revalidated from its own canonical payload first, since this
    is itself a public transition boundary a caller may invoke directly, not
    only through :func:`apply_observation`. Every field of every non-initial
    revision is then recomputed independently from the model and the prior
    link, then compared to what is stored. A caller that edited a posterior,
    a seal, an observed pair or the binding in place — without replaying it
    through :func:`apply_observation` — is refused here, because the
    recomputed value will not match the stored one.
    """
    model = load_model(model.canonical_payload())

    if not history:
        raise LabReplayViolationError("diagnosis state history must contain at least one revision")
    if len(history) > MAX_STATE_REVISION + 1:
        raise LabReplayViolationError("diagnosis history exceeds the revision bound")
    history = tuple(load_state(entry.canonical_payload()) for entry in history)
    if history[0].revision != 0:
        raise LabReplayViolationError(
            "diagnosis state history must begin at revision 0",
            detail={"first_revision": history[0].revision},
        )
    expected_initial = initial_state(model, state_id=history[0].state_id, binding=binding)
    if history[0].canonical_payload() != expected_initial.canonical_payload():
        raise LabReplayViolationError(
            "revision 0 does not reproduce the model's declared prior under this binding"
        )

    current = history[0]
    observation_ids: set[str] = set()
    for revision in history[1:]:
        if revision.state_id != current.state_id:
            raise LabReplayViolationError("diagnosis history changed episode identity")
        if revision.revision != current.revision + 1:
            raise LabReplayViolationError(
                "diagnosis state history is not sequential",
                detail={"expected_revision": current.revision + 1, "found": revision.revision},
            )
        if revision.model_seal != model.model_seal():
            raise LabReplayViolationError(
                "diagnosis state history references a different sealed model mid-episode"
            )
        if revision.binding != binding:
            raise LabReplayViolationError("diagnosis state history's binding drifted mid-episode")
        if revision.prior_state_seal != current.state_seal():
            raise LabReplayViolationError(
                "diagnosis state history's seal chain is broken; the prior link does not "
                "reproduce the seal this revision names"
            )
        observation = revision.applied_observation
        assert observation is not None  # guaranteed by _state_is_coherent for revision > 0
        if observation.observation_id in observation_ids:
            raise LabReplayViolationError("diagnosis history reuses an observation id")
        observation_ids.add(observation.observation_id)
        if (
            current.applied_observation is not None
            and observation.observed_at < current.applied_observation.observed_at
        ):
            raise LabReplayViolationError("diagnosis history observation time moved backwards")
        if observation.probe_id in current.acquired_probe_ids():
            raise LabReplayViolationError(
                "diagnosis state history acquires the same probe twice",
                detail={"probe_id": observation.probe_id},
            )
        probe = model.probe_by_id(observation.probe_id)
        if probe is None or observation.outcome_id not in probe.outcome_space:
            raise LabReplayViolationError(
                "diagnosis state history names a probe or outcome the model does not declare",
                detail={"probe_id": observation.probe_id, "outcome_id": observation.outcome_id},
            )
        recomputed_posterior = _checked_transition(
            probe, current.posterior_weights, observation.outcome_id
        )
        if recomputed_posterior != dict(revision.posterior_weights):
            raise LabReplayViolationError(
                "diagnosis state history's posterior does not replay from its recorded observation"
            )
        expected_pairs = (
            *current.observed_pairs,
            EvidenceRequirement(probe_id=observation.probe_id, outcome_id=observation.outcome_id),
        )
        if tuple(revision.observed_pairs) != expected_pairs:
            raise LabReplayViolationError(
                "diagnosis state history's observed pairs do not replay from its recorded "
                "observation"
            )
        current = revision

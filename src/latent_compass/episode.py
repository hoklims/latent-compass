"""HOK-185 — the episode contract.

An episode is one observed branching decision by a coding agent, recorded after
the fact. It is **evidence, never instruction**. No field can express a command,
a target to mutate, or a callback, and extra fields are forbidden, so a crafted
``{"action": "promote"}`` is a loud validation failure rather than an ignored
key.

What an episode must carry
--------------------------
* the hierarchical state and the branch point within it;
* the candidate directions and the propensity with which each was selectable;
* the direction actually chosen, or an explicit abstention;
* the deterministic external verdict *as observed* — never obtained by calling
  the judge, and never written back to it;
* the deferred outcome, with its observability stated rather than assumed;
* cost, information gain, reversibility and uncertainty;
* provenance: host identity, store, epoch, and a reproducible content seal.

Fail-closed posture
-------------------
Every contract version is **declared, never defaulted** — an episode without
``schema_version``, or an advisory without ``contract_version``, is refused
rather than read as "presumably the current one". Strict validation is on:
``"0.5"`` is not accepted where a float is required. Non-finite floats are
refused everywhere. Timestamps have exactly one accepted encoding. A missing
provenance, an unknown or future version at any depth, or a propensity
distribution that does not sum to one all refuse the episode outright. Nothing
is repaired by inference.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    SUPPORTED_EPISODE_VERSIONS,
    Identifier,
    NonNegativeFloat,
    StrictModel,
    Timestamp,
    UnitInterval,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import EpisodeValidationError
from latent_compass.vocabulary import Advisory, AdvisoryKind

__all__ = [
    "PROPENSITY_TOLERANCE",
    "REDACTABLE_PATHS",
    "AgentFamily",
    "BranchPoint",
    "Candidate",
    "Decision",
    "DeferredOutcome",
    "Economics",
    "Episode",
    "ExternalVerdict",
    "ExternalVerdictValue",
    "HierarchicalState",
    "Observability",
    "Provenance",
    "RedactionMark",
    "StateLevel",
    "episode_content_seal",
    "load_episode",
]

EPISODE_SEAL_DOMAIN: Final = "episode.content"

#: Propensities must form a distribution. The tolerance absorbs binary float
#: representation only; it is far too tight to hide a missing candidate.
PROPENSITY_TOLERANCE: Final = 1e-9

Propensity = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]


class AgentFamily(StrEnum):
    """Which agent family produced the episode.

    Stores are bound to exactly one family. Codex episodes and Claude episodes
    never share a store, physically or logically.
    """

    CODEX = "codex"
    CLAUDE = "claude"
    OTHER = "other"


class Observability(StrEnum):
    """How much of the outcome was actually observable."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class ExternalVerdictValue(StrEnum):
    """A deterministic judge's verdict, as observed from outside."""

    PASS = "PASS"  # noqa: S105 - a verdict value, not a credential
    BLOCK = "BLOCK"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class Provenance(StrictModel):
    """Where the episode came from. Absent provenance refuses the episode."""

    host_id: Identifier
    agent_family: AgentFamily
    store_id: Identifier
    epoch: Identifier
    recorded_at: Timestamp
    tool_version: Identifier


class StateLevel(StrictModel):
    """One rung of the hierarchical state, coarse to fine."""

    depth: int = Field(ge=0, le=64)
    name: Annotated[str, Field(min_length=1, max_length=120)]
    summary_digest: Annotated[str, Field(min_length=1, max_length=200)]


class BranchPoint(StrictModel):
    """Where in the hierarchy the decision was taken."""

    node_id: Identifier
    depth: int = Field(ge=0, le=64)
    parent_node_id: Identifier | None = Field(default=None)

    @model_validator(mode="after")
    def _reject_self_parent(self) -> BranchPoint:
        if self.parent_node_id is not None and self.parent_node_id == self.node_id:
            raise ValueError("branch point cannot be its own parent")
        return self


class HierarchicalState(StrictModel):
    """The state the agent was in, as a strictly ordered ladder."""

    levels: tuple[StateLevel, ...] = Field(min_length=1, max_length=64)
    branch_point: BranchPoint

    @model_validator(mode="after")
    def _check_ladder(self) -> HierarchicalState:
        depths = [level.depth for level in self.levels]
        if depths != sorted(depths) or len(set(depths)) != len(depths):
            raise ValueError("state levels must have strictly increasing depths")
        if self.branch_point.depth not in set(depths):
            raise ValueError(
                f"branch point depth {self.branch_point.depth} is not a declared level depth"
            )
        return self


class Candidate(StrictModel):
    """One direction that could have been chosen, and how selectable it was."""

    direction_id: Identifier
    propensity: Propensity
    prior_uncertainty: UnitInterval


class Decision(StrictModel):
    """What was actually done: a direction, or an explicit non-action."""

    advisory: Advisory
    selected_direction_id: Identifier | None = Field(default=None)

    @model_validator(mode="after")
    def _selection_matches_advisory(self) -> Decision:
        if self.advisory.kind is AdvisoryKind.DIRECTION:
            if self.selected_direction_id is None:
                raise ValueError("a DIRECTION decision must name the selected direction")
            if self.selected_direction_id != self.advisory.direction_id:
                raise ValueError("selected direction must match the advisory direction")
        elif self.selected_direction_id is not None:
            raise ValueError(f"a {self.advisory.kind} decision must not select a direction")
        return self


class ExternalVerdict(StrictModel):
    """A deterministic verdict observed from the outside.

    Recording a verdict is the *only* interaction modelled here. This package
    never calls the judge, never appeals a verdict, and never writes one back.
    """

    judge: Identifier
    verdict: ExternalVerdictValue
    observed_at: Timestamp
    evidence_digest: Annotated[str, Field(min_length=1, max_length=200)]


class DeferredOutcome(StrictModel):
    """What happened later, with observability stated rather than assumed."""

    observability: Observability
    observed: bool
    observed_at: Timestamp | None = Field(default=None)
    success: bool | None = Field(default=None)
    violations: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _outcome_consistency(self) -> DeferredOutcome:
        if self.observability is Observability.NONE and self.observed:
            raise ValueError("an unobservable outcome cannot be marked observed")
        if self.observed:
            if self.observed_at is None:
                raise ValueError("an observed outcome must record when it was observed")
            if self.success is None:
                raise ValueError("an observed outcome must record success")
            return self
        # Nothing was observed, so nothing observed may be reported — including
        # a violation count, which is as much an observation as `success` is.
        reported = {
            "observed_at": self.observed_at,
            "success": self.success,
            "violations": self.violations,
        }
        carried = sorted(name for name, value in reported.items() if value is not None)
        if carried:
            raise ValueError(
                "an unobserved outcome must not carry observed results: " + ", ".join(carried)
            )
        return self


class Economics(StrictModel):
    """Cost, information, reversibility and uncertainty of the branch."""

    cost: NonNegativeFloat
    information_gain: NonNegativeFloat
    reversibility: UnitInterval
    uncertainty: UnitInterval


#: The only paths a redaction may declare.
#:
#: Redaction is **structural**: a declared redaction must correspond to a real
#: absence, and only a field that can legitimately be absent can be redacted.
#: The set is therefore exactly the optional fields of the schema. Declaring a
#: redaction over a field that still holds a value is refused, so a redaction
#: marker can never be decoration over present data.
REDACTABLE_PATHS: Final = frozenset(
    {"external_verdict", "outcome", "state.branch_point.parent_node_id"}
)


class RedactionMark(StrictModel):
    """A field withheld at recording time, declared so its absence is visible."""

    path: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _path_is_redactable(self) -> RedactionMark:
        if self.path not in REDACTABLE_PATHS:
            raise ValueError(
                f"{self.path!r} is not a redactable path; "
                f"redactable paths are {sorted(REDACTABLE_PATHS)}"
            )
        return self


class Episode(StrictModel):
    """One recorded branching decision. Evidence, not instruction."""

    schema_version: str = Field(min_length=5, max_length=20)
    episode_id: Identifier
    provenance: Provenance
    state: HierarchicalState
    candidates: tuple[Candidate, ...] = Field(min_length=1, max_length=256)
    decision: Decision
    external_verdict: ExternalVerdict | None = Field(default=None)
    outcome: DeferredOutcome | None = Field(default=None)
    economics: Economics
    redactions: tuple[RedactionMark, ...] = Field(default=())

    def _value_at(self, path: str) -> object:
        current: object = self
        for part in path.split("."):
            if current is None:
                return None
            current = getattr(current, part, None)
        return current

    @model_validator(mode="after")
    def _cross_field_invariants(self) -> Episode:
        check_contract_version(self.schema_version, SUPPORTED_EPISODE_VERSIONS, "episode")

        direction_ids = [candidate.direction_id for candidate in self.candidates]
        if len(set(direction_ids)) != len(direction_ids):
            raise ValueError("candidate direction ids must be unique")

        total = sum(candidate.propensity for candidate in self.candidates)
        if abs(total - 1.0) > PROPENSITY_TOLERANCE:
            raise ValueError(
                "candidate propensities must sum to 1.0 within "
                f"{PROPENSITY_TOLERANCE}, got {total!r}"
            )

        selected = self.decision.selected_direction_id
        if selected is not None and selected not in direction_ids:
            raise ValueError(f"selected direction {selected!r} is not among the candidates")

        redacted_paths = [mark.path for mark in self.redactions]
        if len(set(redacted_paths)) != len(redacted_paths):
            raise ValueError("redaction paths must be unique")
        for mark in self.redactions:
            if self._value_at(mark.path) is not None:
                raise ValueError(
                    f"redaction declares {mark.path!r} withheld, but a value is present; "
                    "a redaction must correspond to a real absence"
                )
        return self

    def content_seal(self) -> str:
        """Reproducible seal over the whole episode."""
        return episode_content_seal(self.canonical_payload())


def episode_content_seal(payload: object) -> str:
    """Seal a canonical episode payload."""
    return seal(EPISODE_SEAL_DOMAIN, payload)


def load_episode(payload: object) -> Episode:
    """Validate an untrusted payload into an :class:`Episode`.

    The declared contract version is resolved *before* structural validation so
    that a future or retired version is refused as such — with the distinct
    ``unsupported_contract_version`` code — rather than surfacing as a
    misleading pile of field errors. An absent version is refused too: no
    version is ever injected on the reader's behalf.
    """
    if not isinstance(payload, dict):
        raise EpisodeValidationError(
            "episode payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "schema_version" not in payload:
        raise EpisodeValidationError(
            "episode declares no schema_version",
            detail={"contract": "episode", "reason": "absent"},
        )
    declared = payload["schema_version"]
    if not isinstance(declared, str):
        raise EpisodeValidationError(
            "schema_version must be a string",
            detail={"received_type": type(declared).__name__},
        )
    check_contract_version(declared, SUPPORTED_EPISODE_VERSIONS, "episode")
    return validate_contract(Episode, payload, error=EpisodeValidationError, context="episode")

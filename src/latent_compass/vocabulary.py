"""Shared vocabulary: actors, capabilities, advisories, lifecycle states.

This module exists to keep the dependency graph acyclic while letting the
authority boundary verify **real** HOK-186 artefacts.

``authority`` must recompute a protocol seal and a verdict seal to decide
anything, so it has to depend on ``protocol``. ``episode`` needs
:class:`Advisory`, and ``protocol`` needs :class:`ContinueKill`. Leaving those
in ``authority`` produced ``authority -> protocol -> episode -> authority``.
Everything both sides need therefore lives here, below both::

    errors -> canonical -> contracts -> vocabulary -> {episode, protocol}
           -> authority -> governance -> ledger -> cli

Nothing in this module decides anything. It names things.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import Field, model_validator

from latent_compass.contracts import (
    AUTHORITY_CONTRACT_VERSION,
    SUPPORTED_AUTHORITY_VERSIONS,
    Identifier,
    StrictModel,
    UnitInterval,
    check_contract_version,
    require_unit_interval,
)

__all__ = [
    "ABSTENTION_UNCERTAINTY_THRESHOLD",
    "CAPABILITIES",
    "FORBIDDEN_FOR_LATENT_COMPASS",
    "Actor",
    "Advisory",
    "AdvisoryKind",
    "Capability",
    "ContinueKill",
    "LifecycleState",
    "may_issue_direction",
]

#: Above this uncertainty a direction must not be issued. Part of the versioned
#: contract: changing it changes the authority boundary seal.
ABSTENTION_UNCERTAINTY_THRESHOLD: Final = 0.35


class Actor(StrEnum):
    """Who is acting. Only these four exist in the model."""

    LATENT_COMPASS = "latent_compass"
    """This system. Validates and records advisories. Never authorises."""

    EXTERNAL_JUDGE = "external_judge"
    """The deterministic authority. Observed from outside; never called from here."""

    HUMAN_OPERATOR = "human_operator"
    """A person. The only actor holding ``promote``."""

    OBSERVED_AGENT = "observed_agent"
    """The coding agent whose episodes are recorded. Unaffected by this system."""


class Capability(StrEnum):
    """Discrete authorities an actor may hold."""

    OBSERVE = "observe"
    ADVISE = "advise"
    RECORD = "record"
    AUTHORIZE_TRANSITION = "authorize_transition"
    EXECUTE = "execute"
    PROMOTE = "promote"
    MUTATE_EXTERNAL_JUDGE = "mutate_external_judge"


# The private table is the single definition; the public name is a read-only
# view of it. A consumer cannot widen the grant through the public name, and a
# consumer who reaches the private one still cannot make Latent Compass
# authorise anything: `authority.authorize_transition` refuses that actor
# unconditionally, before consulting any table.
_CAPABILITIES: Final[dict[Actor, frozenset[Capability]]] = {
    Actor.LATENT_COMPASS: frozenset({Capability.OBSERVE, Capability.ADVISE, Capability.RECORD}),
    Actor.EXTERNAL_JUDGE: frozenset(
        {Capability.OBSERVE, Capability.AUTHORIZE_TRANSITION, Capability.MUTATE_EXTERNAL_JUDGE}
    ),
    Actor.HUMAN_OPERATOR: frozenset(
        {
            Capability.OBSERVE,
            Capability.ADVISE,
            Capability.RECORD,
            Capability.AUTHORIZE_TRANSITION,
            Capability.EXECUTE,
            Capability.PROMOTE,
        }
    ),
    Actor.OBSERVED_AGENT: frozenset({Capability.EXECUTE}),
}

#: The capability grant, as an immutable view.
CAPABILITIES: Final[MappingProxyType[Actor, frozenset[Capability]]] = MappingProxyType(
    _CAPABILITIES
)

#: Capabilities Latent Compass must never hold, in any version.
FORBIDDEN_FOR_LATENT_COMPASS: Final = frozenset(
    {
        Capability.AUTHORIZE_TRANSITION,
        Capability.EXECUTE,
        Capability.PROMOTE,
        Capability.MUTATE_EXTERNAL_JUDGE,
    }
)


class LifecycleState(StrEnum):
    """Where a candidate direction stands in its lifecycle."""

    DEFINE = "DEFINE"
    SHADOW = "SHADOW"
    OFFLINE_VERIFIED = "OFFLINE_VERIFIED"
    CANARY_ELIGIBLE = "CANARY_ELIGIBLE"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class ContinueKill(StrEnum):
    """The only two verdicts a pre-registered protocol may produce."""

    CONTINUE = "CONTINUE"
    KILL = "KILL"


class AdvisoryKind(StrEnum):
    """What an advisory says. None of these is an instruction to act."""

    DIRECTION = "DIRECTION"
    """A ranked opinion supplied by a caller. Still requires another actor to act."""

    ABSTAIN = "ABSTAIN"
    FALLBACK = "FALLBACK"
    ESCALATE = "ESCALATE"


def may_issue_direction(uncertainty: float) -> bool:
    """Whether a direction may be issued at this uncertainty.

    Refuses non-numbers, ``NaN``, infinities and values outside ``[0, 1]``
    instead of answering. A bare comparison would return ``False`` for ``NaN``
    (an answer it has no basis for) and ``True`` for ``-5.0`` (an answer that is
    wrong), so the ambiguous input fails closed with a typed refusal.
    """
    return require_unit_interval(uncertainty, name="uncertainty") <= (
        ABSTENTION_UNCERTAINTY_THRESHOLD
    )


class Advisory(StrictModel):
    """A recorded opinion. The most a direction can ever be.

    Latent Compass **validates and records** advisories supplied by a caller.
    This foundation does not compute confidence, rank candidates or calibrate
    anything; that is HOK-182 and does not exist yet.

    There is no field here that can carry an action, a target to mutate, or a
    command, and extra fields are forbidden, so attempts to add one fail loudly.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    kind: AdvisoryKind
    direction_id: Identifier | None = Field(default=None)
    confidence: UnitInterval
    uncertainty: UnitInterval
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    issued_by: Actor

    @model_validator(mode="after")
    def _enforce_advisory_invariants(self) -> Advisory:
        check_contract_version(self.contract_version, SUPPORTED_AUTHORITY_VERSIONS, "authority")
        if self.issued_by is not Actor.LATENT_COMPASS:
            raise ValueError("an advisory may only be issued by latent_compass")
        if self.kind is AdvisoryKind.DIRECTION:
            if self.direction_id is None:
                raise ValueError("a DIRECTION advisory must name a direction_id")
            if self.uncertainty > ABSTENTION_UNCERTAINTY_THRESHOLD:
                raise ValueError(
                    f"uncertainty {self.uncertainty} exceeds the abstention threshold "
                    f"{ABSTENTION_UNCERTAINTY_THRESHOLD}; abstain, fall back or escalate"
                )
        elif self.direction_id is not None:
            raise ValueError(f"a {self.kind} advisory must not name a direction_id")
        return self


#: Kept for callers that want the current version without hard-coding it.
#: It is never injected into a payload: an advisory with no declared version is
#: refused, not defaulted.
CURRENT_ADVISORY_CONTRACT_VERSION: Final = AUTHORITY_CONTRACT_VERSION

"""HOK-184 — the authority boundary, as executable contract.

Latent Compass **validates and records** advisories supplied by a caller. It
holds no execution, mutation, promotion or final-decision authority over
anything it observes. A deterministic external judge remains the authority on
truth, invariants, impact, gates and proof. This module is where that sentence
stops being prose.

Three mechanisms carry the boundary:

**An unconditional refusal.** :func:`authorize_transition` refuses
:data:`~latent_compass.vocabulary.Actor.LATENT_COMPASS` before it reads any
table. The security property is *not* "the capability check happens first" —
ordering is a property of one implementation, not an invariant. The invariant
is that the refusal is **unconditional and table-independent**: widening the
capability grant, by any means, does not make Latent Compass able to authorise
anything.

**Immutable enforcement tables.** The grant, the transition table and the
evidence requirements are exposed as read-only mappings, so a consumer cannot
widen the boundary by assignment.

**Provenance that fails closed.** Local versions, seals and rescoring can prove
consistency, not origin. Until a composition root supplies a verifier backed by
an external trust root, every evidence-bearing advancement is refused
unconditionally, independently of the evidence-requirement table and before
any evidence or holdout ledger is inspected.

Anti-goals
----------
This module is not a policy engine, a scheduler, or a gate. It cannot admit
anything. Its only positive output is a record stating that some *other* actor
was entitled to make an ungated move, together with the reproduced inputs.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    AUTHORITY_CONTRACT_VERSION,
    Identifier,
    StrictModel,
    check_contract_version,
)
from latent_compass.errors import AuthorityRefusal, ContractViolation
from latent_compass.protocol import (
    HoldoutLedger,
    HoldoutPurpose,
    MeasurementSet,
    Preregistration,
    Split,
    Verdict,
    load_measurement_set,
    load_preregistration,
    load_verdict,
    recompute_verdict_seal,
    score_measurements,
)
from latent_compass.vocabulary import (
    ABSTENTION_UNCERTAINTY_THRESHOLD,
    CAPABILITIES,
    FORBIDDEN_FOR_LATENT_COMPASS,
    Actor,
    Advisory,
    AdvisoryKind,
    Capability,
    ContinueKill,
    LifecycleState,
    may_issue_direction,
)

__all__ = [
    "ABSTENTION_UNCERTAINTY_THRESHOLD",
    "ALLOWED_TRANSITIONS",
    "ANTI_GOALS",
    "CAPABILITIES",
    "EVIDENCE_REQUIREMENTS",
    "FORBIDDEN_FOR_LATENT_COMPASS",
    "ILLEGAL_SHORTCUTS",
    "THREAT_MODEL",
    "Actor",
    "Advisory",
    "AdvisoryKind",
    "Capability",
    "ContinueKill",
    "EvidenceRequirement",
    "LifecycleState",
    "RefusalReason",
    "TransitionAuthorization",
    "authority_boundary_seal",
    "authority_boundary_snapshot",
    "authorize_transition",
    "may_issue_direction",
]

AUTHORITY_SEAL_DOMAIN: Final = "authority.boundary"
TRANSITION_SEAL_DOMAIN: Final = "authority.transition"

_ALLOWED_TRANSITIONS: Final[dict[LifecycleState, frozenset[LifecycleState]]] = {
    LifecycleState.DEFINE: frozenset({LifecycleState.SHADOW, LifecycleState.REJECTED}),
    LifecycleState.SHADOW: frozenset({LifecycleState.OFFLINE_VERIFIED, LifecycleState.REJECTED}),
    LifecycleState.OFFLINE_VERIFIED: frozenset(
        {LifecycleState.CANARY_ELIGIBLE, LifecycleState.REJECTED}
    ),
    LifecycleState.CANARY_ELIGIBLE: frozenset({LifecycleState.PROMOTED, LifecycleState.REJECTED}),
    LifecycleState.PROMOTED: frozenset({LifecycleState.REJECTED}),
    LifecycleState.REJECTED: frozenset(),
}

#: The only moves that exist, as an immutable view.
#:
#: ``REJECTED`` is terminal: a rejected candidate is redefined as a new
#: candidate rather than resurrected, so the record of the rejection survives.
#: ``PROMOTED -> REJECTED`` is the rollback edge.
ALLOWED_TRANSITIONS: Final[MappingProxyType[LifecycleState, frozenset[LifecycleState]]] = (
    MappingProxyType(_ALLOWED_TRANSITIONS)
)

#: Capabilities a target state demands beyond ``authorize_transition``.
_EXTRA_CAPABILITY: Final[dict[LifecycleState, Capability]] = {
    LifecycleState.PROMOTED: Capability.PROMOTE,
}
EXTRA_CAPABILITY: Final[MappingProxyType[LifecycleState, Capability]] = MappingProxyType(
    _EXTRA_CAPABILITY
)


class EvidenceRequirement(StrictModel):
    """What a target state demands before a positive authorisation."""

    protocol: bool = Field(default=False)
    measurements: bool = Field(default=False)
    verdict: bool = Field(default=False)
    final_holdout_verdict: bool = Field(default=False)
    holdout_consumption_receipt: bool = Field(default=False)
    human_acknowledgement: bool = Field(default=False)
    trusted_external_attestation: bool = Field(default=False)


_EVIDENCE_REQUIREMENTS: Final[dict[LifecycleState, EvidenceRequirement]] = {
    LifecycleState.SHADOW: EvidenceRequirement(protocol=True),
    LifecycleState.OFFLINE_VERIFIED: EvidenceRequirement(
        protocol=True,
        measurements=True,
        verdict=True,
        trusted_external_attestation=True,
    ),
    LifecycleState.CANARY_ELIGIBLE: EvidenceRequirement(
        protocol=True,
        measurements=True,
        verdict=True,
        final_holdout_verdict=True,
        holdout_consumption_receipt=True,
        human_acknowledgement=True,
        trusted_external_attestation=True,
    ),
    LifecycleState.PROMOTED: EvidenceRequirement(
        protocol=True,
        measurements=True,
        verdict=True,
        final_holdout_verdict=True,
        holdout_consumption_receipt=True,
        human_acknowledgement=True,
        trusted_external_attestation=True,
    ),
}

#: Evidence each target state demands, as an immutable view. ``REJECTED`` is
#: absent on purpose: refusing is never gated.
EVIDENCE_REQUIREMENTS: Final[MappingProxyType[LifecycleState, EvidenceRequirement]] = (
    MappingProxyType(_EVIDENCE_REQUIREMENTS)
)

#: Named shortcuts a reviewer will look for. Documented so the refusal is
#: legible, not merely implied by the absence of an edge.
ILLEGAL_SHORTCUTS: Final[tuple[tuple[LifecycleState, LifecycleState], ...]] = (
    (LifecycleState.DEFINE, LifecycleState.PROMOTED),
    (LifecycleState.DEFINE, LifecycleState.CANARY_ELIGIBLE),
    (LifecycleState.SHADOW, LifecycleState.PROMOTED),
    (LifecycleState.SHADOW, LifecycleState.CANARY_ELIGIBLE),
    (LifecycleState.OFFLINE_VERIFIED, LifecycleState.PROMOTED),
    (LifecycleState.REJECTED, LifecycleState.SHADOW),
    (LifecycleState.REJECTED, LifecycleState.PROMOTED),
)

ANTI_GOALS: Final[tuple[str, ...]] = (
    "Latent Compass never executes, schedules or applies a recommendation.",
    "Latent Compass never calls, mutates or adjudicates on behalf of the external judge.",
    "Latent Compass never promotes a candidate, and never authorises its own promotion.",
    "Latent Compass never resolves an unknown by inference; unknowns stay unknown.",
    "Latent Compass never emits, ranks or calibrates an advisory in this foundation; "
    "it validates and records advisories supplied to it.",
    "Latent Compass never claims an intelligence gain.",
)

THREAT_MODEL: Final[tuple[str, ...]] = (
    "A crafted episode carries an action or authority field to smuggle intent: "
    "refused by extra-field rejection, not ignored.",
    "A caller asks Latent Compass to authorise a legal move: refused "
    "unconditionally, before any table is read, so widening the grant does not help.",
    "A caller invents self-consistent metrics and the word CONTINUE: refused before "
    "the evidence is inspected, because no trusted external attestation of its origin "
    "exists.",
    "A caller edits a sealed verdict from KILL to CONTINUE: refused, because the "
    "recomputed seal no longer matches the carried one.",
    "The external judge asks for a promotion: refused, because promotion demands "
    "the promote capability, which only a human operator holds.",
    "Chained uncertainty or a non-finite number poses as a confident direction: "
    "refused by finiteness and abstention checks.",
    "An operator edits a threshold after seeing the holdout: forced into a new "
    "protocol version, epoch and seal; the holdout corpus stays spent.",
    "An administrator with write access rewrites the whole ledger: NOT defended "
    "against. A hash chain detects tampering by anyone who cannot rewrite every "
    "row; it does not establish authenticity. See docs/ledger.md.",
)


class RefusalReason(StrEnum):
    """Machine-readable reason attached to every :class:`AuthorityRefusal`."""

    SELF_AUTHORISATION = "self_authorisation"
    ACTOR_LACKS_CAPABILITY = "actor_lacks_capability"
    ILLEGAL_TRANSITION = "illegal_transition"
    TERMINAL_STATE = "terminal_state"
    MISSING_EVIDENCE = "missing_evidence"
    EVIDENCE_FORGED = "evidence_forged"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    EVIDENCE_REJECTS = "evidence_rejects"
    UNTRUSTED_EVIDENCE = "untrusted_evidence"


class TransitionAuthorization(StrictModel):
    """The record produced when some *other* actor was entitled to move.

    This object grants nothing. It states that a move was legal, by whom, and
    on what reproduced evidence. Applying the move is out of scope.
    """

    contract_version: str = Field(default=AUTHORITY_CONTRACT_VERSION)
    from_state: LifecycleState
    to_state: LifecycleState
    actor: Actor
    protocol_seal: str | None = Field(default=None)
    verdict_seal: str | None = Field(default=None)
    measurement_set_seal: str | None = Field(default=None)
    corpus_seal: str | None = Field(default=None)
    protocol_id: Identifier | None = Field(default=None)
    human_acknowledged: bool = Field(default=False)
    authorization_seal: str = Field(min_length=1)

    @model_validator(mode="after")
    def _requires_current_authority_contract(self) -> TransitionAuthorization:
        check_contract_version(
            self.contract_version,
            frozenset({AUTHORITY_CONTRACT_VERSION}),
            "transition authorization",
        )
        return self


def _refuse(message: str, reason: RefusalReason, **detail: object) -> AuthorityRefusal:
    return AuthorityRefusal(message, detail={"reason": reason.value, **detail})


def _verify_evidence(
    *,
    to_state: LifecycleState,
    requirement: EvidenceRequirement,
    protocol: Preregistration | None,
    measurements: MeasurementSet | None,
    verdict: Verdict | None,
    holdout_ledger: HoldoutLedger | None,
    human_acknowledged: bool,
) -> tuple[Preregistration | None, MeasurementSet | None, Verdict | None]:
    """Validate and independently reproduce every supplied evidence artefact."""
    if requirement.protocol and protocol is None:
        raise _refuse(
            f"transition to {to_state.value} requires a pre-registered protocol",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="protocol",
        )
    if requirement.human_acknowledgement and not human_acknowledged:
        raise _refuse(
            f"transition to {to_state.value} requires a human acknowledgement",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="human_acknowledgement",
        )
    if requirement.verdict and verdict is None:
        raise _refuse(
            f"transition to {to_state.value} requires a sealed protocol verdict",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="verdict",
        )
    if requirement.measurements and measurements is None:
        raise _refuse(
            f"transition to {to_state.value} requires the raw measurements",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="measurements",
        )
    try:
        checked_protocol = (
            load_preregistration(protocol.canonical_payload()) if protocol is not None else None
        )
        checked_measurements = (
            load_measurement_set(measurements.canonical_payload())
            if measurements is not None
            else None
        )
        checked_verdict = load_verdict(verdict.canonical_payload()) if verdict is not None else None
    except ContractViolation as exc:
        raise _refuse(
            "supplied evidence violates its declared contract",
            RefusalReason.EVIDENCE_FORGED,
            to_state=to_state.value,
            cause=exc.message,
            evidence_detail=exc.detail,
        ) from exc

    if checked_verdict is None:
        if checked_measurements is not None and checked_protocol is None:
            raise _refuse(
                "measurements cannot be verified without their protocol",
                RefusalReason.MISSING_EVIDENCE,
                to_state=to_state.value,
                missing="protocol",
            )
        return checked_protocol, checked_measurements, None

    if checked_protocol is None:
        raise _refuse(
            "a verdict cannot be verified without its protocol",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="protocol",
        )
    recomputed = recompute_verdict_seal(checked_verdict)
    if recomputed != checked_verdict.verdict_seal:
        raise _refuse(
            "the verdict does not reproduce its own seal",
            RefusalReason.EVIDENCE_FORGED,
            to_state=to_state.value,
            carried_seal=checked_verdict.verdict_seal,
            recomputed_seal=recomputed,
        )
    if checked_measurements is None:
        raise _refuse(
            "a verdict cannot be reproduced without its raw measurements",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="measurements",
        )

    protocol_seal = checked_protocol.protocol_seal()
    if checked_verdict.protocol_seal != protocol_seal:
        raise _refuse(
            "the verdict was produced under a different protocol",
            RefusalReason.EVIDENCE_MISMATCH,
            expected=protocol_seal,
            received=checked_verdict.protocol_seal,
        )
    if checked_verdict.epoch != checked_protocol.epoch:
        raise _refuse(
            "the verdict belongs to a different epoch",
            RefusalReason.EVIDENCE_MISMATCH,
            expected=checked_protocol.epoch,
            received=checked_verdict.epoch,
        )
    expected_corpus = checked_protocol.split_spec(checked_verdict.split).corpus_seal
    if checked_verdict.corpus_seal != expected_corpus:
        raise _refuse(
            "the verdict does not name the pre-registered corpus for its split",
            RefusalReason.EVIDENCE_MISMATCH,
            split=checked_verdict.split.value,
            expected=expected_corpus,
            received=checked_verdict.corpus_seal,
        )

    try:
        expected_verdict = score_measurements(checked_protocol, checked_measurements)
    except ContractViolation as exc:
        raise _refuse(
            "the raw measurements cannot reproduce a valid verdict",
            RefusalReason.EVIDENCE_MISMATCH,
            to_state=to_state.value,
            cause=exc.message,
            evidence_detail=exc.detail,
        ) from exc
    if expected_verdict.canonical_payload() != checked_verdict.canonical_payload():
        raise _refuse(
            "the verdict does not reproduce from the supplied raw measurements",
            RefusalReason.EVIDENCE_MISMATCH,
            to_state=to_state.value,
            expected_verdict_seal=expected_verdict.verdict_seal,
            received_verdict_seal=checked_verdict.verdict_seal,
        )

    if (
        checked_verdict.decision is not ContinueKill.CONTINUE
        and to_state is not LifecycleState.REJECTED
    ):
        raise _refuse(
            "the verdict does not admit this transition",
            RefusalReason.EVIDENCE_REJECTS,
            to_state=to_state.value,
            decision=checked_verdict.decision.value,
        )
    if requirement.final_holdout_verdict and (
        checked_verdict.split is not Split.HOLDOUT
        or checked_verdict.purpose is not HoldoutPurpose.FINAL_VERDICT
    ):
        raise _refuse(
            f"transition to {to_state.value} requires a final verdict on the holdout",
            RefusalReason.MISSING_EVIDENCE,
            to_state=to_state.value,
            missing="final_holdout_verdict",
            split=checked_verdict.split.value,
            purpose=checked_verdict.purpose.value,
        )
    if requirement.holdout_consumption_receipt:
        if holdout_ledger is None:
            raise _refuse(
                "a final holdout verdict requires its durable consumption receipt",
                RefusalReason.MISSING_EVIDENCE,
                to_state=to_state.value,
                missing="holdout_consumption_receipt",
            )
        try:
            holdout_ledger.require_consumption(
                checked_verdict.corpus_seal,
                protocol_seal=protocol_seal,
                verdict_seal=checked_verdict.verdict_seal,
                measurement_set_seal=checked_measurements.measurement_seal(),
                epoch=checked_verdict.epoch,
            )
        except ContractViolation as exc:
            raise _refuse(
                "the final holdout consumption receipt is absent or mismatched",
                RefusalReason.EVIDENCE_MISMATCH,
                to_state=to_state.value,
                cause=exc.message,
                evidence_detail=exc.detail,
            ) from exc
    return checked_protocol, checked_measurements, checked_verdict


def authorize_transition(
    *,
    from_state: LifecycleState,
    to_state: LifecycleState,
    actor: Actor,
    protocol: Preregistration | None = None,
    measurements: MeasurementSet | None = None,
    verdict: Verdict | None = None,
    holdout_ledger: HoldoutLedger | None = None,
    human_acknowledged: bool = False,
) -> TransitionAuthorization:
    """Decide whether ``actor`` may move a candidate ``from_state`` ``to_state``.

    Raises :class:`~latent_compass.errors.AuthorityRefusal` on any refusal.

    The first check is unconditional and reads no table: Latent Compass is
    refused outright. Every later check may consult the capability grant, the
    transition table and the evidence requirements — none of which can rescue
    the refused actor, because that refusal already happened.

    Targets that depend on measurement evidence are refused before evidence or
    holdout-ledger access. Local versions, seals and rescoring could establish
    consistency but not provenance; a future positive path therefore requires a
    trusted external attestation verifier fixed by the composition root.
    """
    if actor is Actor.LATENT_COMPASS:
        # Unconditional, table-independent, and first. Nothing below can undo it.
        raise _refuse(
            "latent_compass never authorises a lifecycle transition",
            RefusalReason.SELF_AUTHORISATION,
            actor=actor.value,
            from_state=from_state.value,
            to_state=to_state.value,
            forbidden_capabilities=sorted(
                capability.value for capability in FORBIDDEN_FOR_LATENT_COMPASS
            ),
        )

    held = CAPABILITIES.get(actor, frozenset())
    if Capability.AUTHORIZE_TRANSITION not in held:
        raise _refuse(
            f"{actor.value} does not hold {Capability.AUTHORIZE_TRANSITION.value}",
            RefusalReason.ACTOR_LACKS_CAPABILITY,
            actor=actor.value,
            required_capability=Capability.AUTHORIZE_TRANSITION.value,
            held_capabilities=sorted(capability.value for capability in held),
            from_state=from_state.value,
            to_state=to_state.value,
        )

    extra = EXTRA_CAPABILITY.get(to_state)
    if extra is not None and extra not in held:
        raise _refuse(
            f"{actor.value} does not hold {extra.value}, required to reach {to_state.value}",
            RefusalReason.ACTOR_LACKS_CAPABILITY,
            actor=actor.value,
            required_capability=extra.value,
            held_capabilities=sorted(capability.value for capability in held),
            from_state=from_state.value,
            to_state=to_state.value,
        )

    allowed = ALLOWED_TRANSITIONS[from_state]
    if not allowed:
        raise _refuse(
            f"{from_state.value} is terminal; no transition is permitted",
            RefusalReason.TERMINAL_STATE,
            from_state=from_state.value,
            to_state=to_state.value,
        )
    if to_state not in allowed:
        raise _refuse(
            f"{from_state.value} -> {to_state.value} is not an allowed transition",
            RefusalReason.ILLEGAL_TRANSITION,
            from_state=from_state.value,
            to_state=to_state.value,
            allowed=sorted(state.value for state in allowed),
        )

    # No table controls this refusal. Until a composition root with a real
    # external trust root exists, every evidence-bearing advancement is
    # unconditionally closed. In particular, mutating _EVIDENCE_REQUIREMENTS
    # cannot turn local consistency into provenance.
    if to_state in (
        LifecycleState.OFFLINE_VERIFIED,
        LifecycleState.CANARY_ELIGIBLE,
        LifecycleState.PROMOTED,
    ):
        raise _refuse(
            "caller-supplied evidence has no trusted external attestation",
            RefusalReason.UNTRUSTED_EVIDENCE,
            from_state=from_state.value,
            to_state=to_state.value,
            missing="trusted_external_attestation",
        )

    requirement = EVIDENCE_REQUIREMENTS.get(to_state, EvidenceRequirement())
    protocol, measurements, verdict = _verify_evidence(
        to_state=to_state,
        requirement=requirement,
        protocol=protocol,
        measurements=measurements,
        verdict=verdict,
        holdout_ledger=holdout_ledger,
        human_acknowledged=human_acknowledged,
    )
    # Checked even where a verdict was not required: a KILL supplied anyway
    # admits nothing but rejection.
    if (
        verdict is not None
        and verdict.decision is ContinueKill.KILL
        and to_state is not LifecycleState.REJECTED
    ):
        raise _refuse(
            "a KILL verdict admits no target other than REJECTED",
            RefusalReason.EVIDENCE_REJECTS,
            from_state=from_state.value,
            to_state=to_state.value,
        )

    protocol_seal = protocol.protocol_seal() if protocol is not None else None
    payload = {
        "contract_version": AUTHORITY_CONTRACT_VERSION,
        "from_state": from_state.value,
        "to_state": to_state.value,
        "actor": actor.value,
        "protocol_seal": protocol_seal,
        "verdict_seal": verdict.verdict_seal if verdict is not None else None,
        "measurement_set_seal": (
            measurements.measurement_seal() if measurements is not None else None
        ),
        "corpus_seal": verdict.corpus_seal if verdict is not None else None,
        "protocol_id": protocol.protocol_id if protocol is not None else None,
        "human_acknowledged": human_acknowledged,
    }
    return TransitionAuthorization(
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        protocol_seal=protocol_seal,
        verdict_seal=verdict.verdict_seal if verdict is not None else None,
        measurement_set_seal=(
            measurements.measurement_seal() if measurements is not None else None
        ),
        corpus_seal=verdict.corpus_seal if verdict is not None else None,
        protocol_id=protocol.protocol_id if protocol is not None else None,
        human_acknowledged=human_acknowledged,
        authorization_seal=seal(TRANSITION_SEAL_DOMAIN, payload),
    )


def authority_boundary_snapshot() -> dict[str, object]:
    """Return the full boundary as JSON-shaped data.

    Sealed by :func:`authority_boundary_seal`, so any widening of the grant —
    an added capability, a new edge, a moved threshold, a relaxed evidence
    requirement — changes an observable value rather than passing silently.
    """
    return {
        "contract_version": AUTHORITY_CONTRACT_VERSION,
        "abstention_uncertainty_threshold": ABSTENTION_UNCERTAINTY_THRESHOLD,
        "actors": sorted(actor.value for actor in Actor),
        "capabilities": {
            actor.value: sorted(capability.value for capability in CAPABILITIES[actor])
            for actor in sorted(CAPABILITIES, key=lambda item: item.value)
        },
        "forbidden_for_latent_compass": sorted(
            capability.value for capability in FORBIDDEN_FOR_LATENT_COMPASS
        ),
        "unconditional_refusals": [Actor.LATENT_COMPASS.value],
        "advisory_kinds": sorted(kind.value for kind in AdvisoryKind),
        "states": sorted(state.value for state in LifecycleState),
        "transitions": {
            state.value: sorted(target.value for target in ALLOWED_TRANSITIONS[state])
            for state in sorted(ALLOWED_TRANSITIONS, key=lambda item: item.value)
        },
        "extra_capability_per_target": {
            state.value: capability.value
            for state, capability in sorted(
                EXTRA_CAPABILITY.items(), key=lambda item: item[0].value
            )
        },
        "evidence_requirements": {
            state.value: requirement.canonical_payload()
            for state, requirement in sorted(
                EVIDENCE_REQUIREMENTS.items(), key=lambda item: item[0].value
            )
        },
        "anti_goals": list(ANTI_GOALS),
        "threat_model": list(THREAT_MODEL),
    }


def authority_boundary_seal() -> str:
    """Seal over :func:`authority_boundary_snapshot`."""
    return seal(AUTHORITY_SEAL_DOMAIN, authority_boundary_snapshot())

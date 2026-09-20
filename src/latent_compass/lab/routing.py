"""HOK-803 — lab routing advice, as pure, non-authoritative computation.

A :class:`RouteRequest` and a :class:`HostCapabilitySnapshot` are **input
declarations, never authenticated facts**. Nothing here observes a host,
calls a tool, dispatches a model or an agent, or opens a process or a
socket. :func:`evaluate_route` is a closed function of its two inputs and a
supplied ``now``: same inputs, same :class:`RouteDecision`, every time.

Three outcomes, and only three
-------------------------------
``ADVICE`` — every declared prerequisite matched, the capability exists, its
observation is current, timing is coherent and budget is sufficient.
``ABSTAIN`` — a missing, stale, incompatible or uncertain capability, an
exhausted budget, a conflicting source, an absent advisor or an engaged kill
switch.
``ESCALATE`` — an explicitly declared missing authority or precondition, or a
required review or Semctx proof check that the caller did not satisfy. A low
declared cost never substitutes for either check: both are read before the
budget is.

None of the three is an executable dispatch instruction. A suggested fallback
observation id is carried as a bare identifier, never as a claim that the
fallback ran. Every :class:`RouteDecision` recomputes its own seal from its
own body, so a caller cannot hand back a mutated decision with the original
seal attached, and that body binds ``request_seal``/``snapshot_seal`` — domain
-separated seals over the *complete* canonical request and snapshot — plus the
plain ``host_id``/``agent_family``/``scope`` context, so a decision minted for
one host, state or model cannot be replayed as if it described another. These
are integrity digests an external consumer can match against its own expected
context; they are never a signature, and no caller-supplied approval boolean
or digest is ever treated as this package's own authorization.

:func:`evaluate_route`, :func:`reserve_budget` and :func:`settle_budget` are
this module's public boundaries: each revalidates every model instance it is
handed, so a ``model_construct``-built or attribute-mutated forgery is refused
exactly as a freshly admitted payload with the same defect would be.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from typing import Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

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
from latent_compass.errors import ContractViolation

__all__ = [
    "LAB_ROUTING_CONTRACT_VERSION",
    "SUPPORTED_LAB_ROUTING_VERSIONS",
    "AbstainReason",
    "BudgetChargeRecord",
    "BudgetPoolState",
    "BudgetReservation",
    "BudgetSettlement",
    "CapabilityKind",
    "CapabilityObservation",
    "CostBasis",
    "EscalateReason",
    "HostCapabilitySnapshot",
    "LabRoutingViolation",
    "RouteDecision",
    "RouteRequest",
    "RouteVerdict",
    "SettlementOutcome",
    "admit_host_capability_snapshot",
    "admit_route_request",
    "evaluate_route",
    "reserve_budget",
    "settle_budget",
]

LAB_ROUTING_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_LAB_ROUTING_VERSIONS: Final = frozenset({LAB_ROUTING_CONTRACT_VERSION})

ROUTE_DECISION_SEAL_DOMAIN: Final = "lab.routing.decision.v1"
ROUTE_REQUEST_SEAL_DOMAIN: Final = "lab.routing.request.v1"
SNAPSHOT_SEAL_DOMAIN: Final = "lab.routing.snapshot.v1"


class LabRoutingViolation(ContractViolation):
    """A HOK-803 lab routing input or result failed its contract."""

    code = "lab_routing_violation"


class CapabilityKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    TOOL = "TOOL"
    MODEL = "MODEL"
    AGENT = "AGENT"


class RouteVerdict(StrEnum):
    ADVICE = "ADVICE"
    ABSTAIN = "ABSTAIN"
    ESCALATE = "ESCALATE"


class AbstainReason(StrEnum):
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CAPABILITY_STALE = "CAPABILITY_STALE"
    CAPABILITY_UNCERTAIN = "CAPABILITY_UNCERTAIN"
    CAPABILITY_INCOMPATIBLE = "CAPABILITY_INCOMPATIBLE"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    TIMING_INCOHERENT = "TIMING_INCOHERENT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ADVISOR_ABSENT = "ADVISOR_ABSENT"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


class EscalateReason(StrEnum):
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    PRECONDITION_MISSING = "PRECONDITION_MISSING"
    REVIEW_REQUIRED_UNSATISFIED = "REVIEW_REQUIRED_UNSATISFIED"
    SEMCTX_PROOF_REQUIRED_UNSATISFIED = "SEMCTX_PROOF_REQUIRED_UNSATISFIED"


class RouteRequest(StrictModel):
    """One immutable declaration of a candidate route, never a command."""

    contract_version: str
    request_digest: Seal
    model_digest: Seal
    state_digest: Seal
    source_digest: Seal
    host_id: Identifier
    agent_family: AgentFamily
    scope: Identifier
    candidate_capability_id: Identifier
    candidate_kind: CapabilityKind
    requested_at: Timestamp
    expiry: Timestamp
    cost_ceiling: int = Field(ge=0)
    remaining_budget: int = Field(ge=0)
    advisor_present: bool
    kill_switch_engaged: bool
    review_required: bool
    review_satisfied: bool
    semctx_proof_required: bool
    semctx_proof_satisfied: bool
    explicit_missing_authority: bool
    explicit_missing_precondition: bool
    fallback_observation_id: Identifier | None = Field(default=None)

    @model_validator(mode="after")
    def _coherent_request(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_ROUTING_VERSIONS, "lab route request"
        )
        if self.expiry < self.requested_at:
            raise ValueError("expiry must not precede requested_at")
        return self


class CapabilityObservation(StrictModel):
    """One capability a host is declared to actually have observed."""

    capability_id: Identifier
    kind: CapabilityKind
    observed_at: Timestamp
    expires_at: Timestamp

    @model_validator(mode="after")
    def _coherent_observation(self) -> Self:
        if self.expires_at < self.observed_at:
            raise ValueError("expires_at must not precede observed_at")
        return self


class HostCapabilitySnapshot(StrictModel):
    """A declared, unauthenticated snapshot of what one host currently offers."""

    contract_version: str
    host_id: Identifier
    agent_family: AgentFamily
    scope: Identifier
    current_source_digest: Seal
    snapshot_taken_at: Timestamp
    observed_capabilities: tuple[CapabilityObservation, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def _coherent_snapshot(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_ROUTING_VERSIONS, "lab host capability snapshot"
        )
        keys = [(item.capability_id, item.kind) for item in self.observed_capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError("a capability id and kind pair must appear at most once")
        for item in self.observed_capabilities:
            if item.observed_at > self.snapshot_taken_at:
                raise ValueError(
                    f"{item.capability_id} was observed after the snapshot it belongs to"
                )
        return self

    def find(self, capability_id: str, kind: CapabilityKind) -> CapabilityObservation | None:
        for item in self.observed_capabilities:
            if item.capability_id == capability_id and item.kind is kind:
                return item
        return None


class RouteDecision(StrictModel):
    """The one non-authoritative result :func:`evaluate_route` can return."""

    contract_version: str
    verdict: RouteVerdict
    abstain_reason: AbstainReason | None = Field(default=None)
    escalate_reason: EscalateReason | None = Field(default=None)
    matched_capability_id: Identifier | None = Field(default=None)
    matched_kind: CapabilityKind | None = Field(default=None)
    suggested_fallback_observation_id: Identifier | None = Field(default=None)
    request_seal: Seal
    snapshot_seal: Seal
    host_id: Identifier
    agent_family: AgentFamily
    scope: Identifier
    lab_only: Literal[True] = True
    experimental: Literal[True] = True
    empirical_claim: Literal[False] = False
    execution_authority: Literal[False] = False
    decision_seal: Seal

    @model_validator(mode="after")
    def _coherent_decision(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_ROUTING_VERSIONS, "lab route decision"
        )
        if self.verdict is RouteVerdict.ADVICE:
            if self.abstain_reason is not None or self.escalate_reason is not None:
                raise ValueError("ADVICE carries no abstain or escalate reason")
            if self.matched_capability_id is None or self.matched_kind is None:
                raise ValueError("ADVICE must name the exact capability it matched")
        elif self.verdict is RouteVerdict.ABSTAIN:
            if self.abstain_reason is None:
                raise ValueError("ABSTAIN must name a reason")
            if self.escalate_reason is not None or self.matched_capability_id is not None:
                raise ValueError("ABSTAIN carries no escalate reason and matches no capability")
        else:
            if self.escalate_reason is None:
                raise ValueError("ESCALATE must name a reason")
            if self.abstain_reason is not None or self.matched_capability_id is not None:
                raise ValueError("ESCALATE carries no abstain reason and matches no capability")
        body = self.canonical_payload()
        body.pop("decision_seal")
        if seal(ROUTE_DECISION_SEAL_DOMAIN, body) != self.decision_seal:
            raise ValueError("decision seal does not reproduce from its own body")
        return self


def admit_route_request(payload: object) -> RouteRequest:
    return validate_contract(
        RouteRequest, payload, error=LabRoutingViolation, context="lab route request"
    )


def admit_host_capability_snapshot(payload: object) -> HostCapabilitySnapshot:
    return validate_contract(
        HostCapabilitySnapshot,
        payload,
        error=LabRoutingViolation,
        context="lab host capability snapshot",
    )


def _revalidated[ModelT: StrictModel](
    model_type: type[ModelT], instance: ModelT, *, context: str
) -> ModelT:
    """Force ``instance`` back through full contract validation.

    Guards a public boundary against a ``model_construct``-built or otherwise
    mutated instance that never ran its own ``model_validator``: pydantic's
    ``revalidate_instances="always"`` config makes this re-run every field and
    model validator exactly as if ``instance`` were untrusted input.
    """
    try:
        return model_type.model_validate(instance)
    except ValidationError as exc:
        raise LabRoutingViolation(
            f"{context} failed strict contract validation",
            detail={
                "context": context,
                "violations": [
                    {
                        "location": ".".join(str(part) for part in item["loc"]),
                        "type": item["type"],
                        "message": item["msg"],
                    }
                    for item in exc.errors(include_url=False)
                ],
            },
        ) from exc


def _instant(value: str) -> datetime:
    """Parse a canonical ``Timestamp`` into a timezone-aware UTC instant.

    Comparisons in this module compare parsed instants, not raw strings, so
    ordering stays correct even if ``Timestamp`` ever grows a second accepted
    UTC encoding.
    """
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _decision(
    *,
    verdict: RouteVerdict,
    request_seal_value: str,
    snapshot_seal_value: str,
    host_id: str,
    agent_family: AgentFamily,
    scope: str,
    abstain_reason: AbstainReason | None = None,
    escalate_reason: EscalateReason | None = None,
    matched_capability_id: str | None = None,
    matched_kind: CapabilityKind | None = None,
    fallback_observation_id: str | None = None,
) -> RouteDecision:
    body: dict[str, object] = {
        "contract_version": LAB_ROUTING_CONTRACT_VERSION,
        "verdict": verdict.value,
        "abstain_reason": abstain_reason.value if abstain_reason is not None else None,
        "escalate_reason": escalate_reason.value if escalate_reason is not None else None,
        "matched_capability_id": matched_capability_id,
        "matched_kind": matched_kind.value if matched_kind is not None else None,
        "suggested_fallback_observation_id": fallback_observation_id,
        "request_seal": request_seal_value,
        "snapshot_seal": snapshot_seal_value,
        "host_id": host_id,
        "agent_family": agent_family.value,
        "scope": scope,
        "lab_only": True,
        "experimental": True,
        "empirical_claim": False,
        "execution_authority": False,
    }
    return RouteDecision(
        contract_version=LAB_ROUTING_CONTRACT_VERSION,
        verdict=verdict,
        abstain_reason=abstain_reason,
        escalate_reason=escalate_reason,
        matched_capability_id=matched_capability_id,
        matched_kind=matched_kind,
        suggested_fallback_observation_id=fallback_observation_id,
        request_seal=request_seal_value,
        snapshot_seal=snapshot_seal_value,
        host_id=host_id,
        agent_family=agent_family,
        scope=scope,
        decision_seal=seal(ROUTE_DECISION_SEAL_DOMAIN, body),
    )


class _NowHolder(StrictModel):
    """Runs the shared timestamp validator over a caller-supplied ``now``."""

    now: Timestamp


def evaluate_route(
    request: RouteRequest, snapshot: HostCapabilitySnapshot, *, now: str
) -> RouteDecision:
    """Decide ADVICE, ABSTAIN or ESCALATE for one declared route, purely.

    Review and Semctx proof requirements are read before the budget: a low
    ``cost_ceiling`` cannot substitute for either. Nothing here authorises,
    dispatches or executes; the returned decision only states which of the
    three outcomes the declared inputs admit.

    ``request`` and ``snapshot`` are revalidated on entry, so a caller cannot
    smuggle a ``model_construct``-built or mutated forgery past this, the
    public evaluation entry point.
    """
    request = _revalidated(RouteRequest, request, context="lab route request")
    snapshot = _revalidated(
        HostCapabilitySnapshot, snapshot, context="lab host capability snapshot"
    )
    now_checked_raw = validate_contract(
        _NowHolder, {"now": now}, error=LabRoutingViolation, context="lab routing clock"
    ).now
    now_checked = _instant(now_checked_raw)
    fallback = request.fallback_observation_id
    request_seal_value = seal(ROUTE_REQUEST_SEAL_DOMAIN, request.canonical_payload())
    snapshot_seal_value = seal(SNAPSHOT_SEAL_DOMAIN, snapshot.canonical_payload())
    decide = partial(
        _decision,
        request_seal_value=request_seal_value,
        snapshot_seal_value=snapshot_seal_value,
        host_id=request.host_id,
        agent_family=request.agent_family,
        scope=request.scope,
    )

    if request.kill_switch_engaged:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.KILL_SWITCH_ENGAGED,
            fallback_observation_id=fallback,
        )
    if not request.advisor_present:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.ADVISOR_ABSENT,
            fallback_observation_id=fallback,
        )
    if request.explicit_missing_authority:
        return decide(
            verdict=RouteVerdict.ESCALATE, escalate_reason=EscalateReason.AUTHORITY_MISSING
        )
    if request.explicit_missing_precondition:
        return decide(
            verdict=RouteVerdict.ESCALATE, escalate_reason=EscalateReason.PRECONDITION_MISSING
        )
    if request.review_required and not request.review_satisfied:
        return decide(
            verdict=RouteVerdict.ESCALATE,
            escalate_reason=EscalateReason.REVIEW_REQUIRED_UNSATISFIED,
        )
    if request.semctx_proof_required and not request.semctx_proof_satisfied:
        return decide(
            verdict=RouteVerdict.ESCALATE,
            escalate_reason=EscalateReason.SEMCTX_PROOF_REQUIRED_UNSATISFIED,
        )
    if (
        request.host_id != snapshot.host_id
        or request.agent_family is not snapshot.agent_family
        or request.scope != snapshot.scope
    ):
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.CAPABILITY_INCOMPATIBLE,
            fallback_observation_id=fallback,
        )
    if request.source_digest != snapshot.current_source_digest:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.SOURCE_CONFLICT,
            fallback_observation_id=fallback,
        )
    if _instant(snapshot.snapshot_taken_at) > now_checked:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.CAPABILITY_UNCERTAIN,
            fallback_observation_id=fallback,
        )
    if now_checked > _instant(request.expiry) or now_checked < _instant(request.requested_at):
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.TIMING_INCOHERENT,
            fallback_observation_id=fallback,
        )
    candidate = snapshot.find(request.candidate_capability_id, request.candidate_kind)
    if candidate is None:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.CAPABILITY_MISSING,
            fallback_observation_id=fallback,
        )
    if now_checked < _instant(candidate.observed_at):
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.CAPABILITY_UNCERTAIN,
            fallback_observation_id=fallback,
        )
    if now_checked > _instant(candidate.expires_at):
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.CAPABILITY_STALE,
            fallback_observation_id=fallback,
        )
    if request.cost_ceiling > request.remaining_budget:
        return decide(
            verdict=RouteVerdict.ABSTAIN,
            abstain_reason=AbstainReason.BUDGET_EXHAUSTED,
            fallback_observation_id=fallback,
        )
    return decide(
        verdict=RouteVerdict.ADVICE,
        matched_capability_id=candidate.capability_id,
        matched_kind=candidate.kind,
    )


class SettlementOutcome(StrEnum):
    """What happened to a reservation. A cancellation still charges a cost."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CostBasis(StrEnum):
    """How a charged cost was determined.

    ``CONSERVATIVE_MAXIMUM`` marks a settlement with no declared actual cost:
    the reservation's own ceiling is charged in full, never zero, so an
    unknown cost cannot be released back into the pool as if it were free.
    """

    ACTUAL = "ACTUAL"
    CONSERVATIVE_MAXIMUM = "CONSERVATIVE_MAXIMUM"


class BudgetReservation(StrictModel):
    """One maximum reserved against the one total budget of a pool."""

    reservation_id: Identifier
    parent_reservation_id: Identifier | None = Field(default=None)
    requested_maximum: int = Field(ge=0)
    reserved_at: Timestamp


class BudgetSettlement(StrictModel):
    """What one reservation actually cost, or the declaration that it is unknown."""

    reservation_id: Identifier
    outcome: SettlementOutcome
    actual_cost: int | None = Field(default=None, ge=0)
    settled_at: Timestamp


class BudgetChargeRecord(StrictModel):
    """The closed charge a settlement produced against the pool."""

    reservation_id: Identifier
    outcome: SettlementOutcome
    charged_cost: int = Field(ge=0)
    cost_basis: CostBasis
    settled_at: Timestamp


class BudgetPoolState(StrictModel):
    """The reservation/settlement ledger for one total budget.

    Immutable: :func:`reserve_budget` and :func:`settle_budget` each return a
    new state rather than mutating this one. There is no merge operation and
    no host field — one pool speaks for exactly one total budget, and this
    module does nothing to combine two of them or to share authority across
    them.
    """

    total_budget: int = Field(ge=0)
    reservations: tuple[BudgetReservation, ...] = Field(default=(), max_length=4096)
    charges: tuple[BudgetChargeRecord, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def _coherent_pool(self) -> Self:
        reservation_ids = [item.reservation_id for item in self.reservations]
        if len(set(reservation_ids)) != len(reservation_ids):
            raise ValueError("a reservation id must be reserved at most once")
        charge_ids = [item.reservation_id for item in self.charges]
        if len(set(charge_ids)) != len(charge_ids):
            raise ValueError("a reservation id must be settled at most once")
        known = set(reservation_ids)
        if not set(charge_ids) <= known:
            raise ValueError("a settlement must reference a reservation that was reserved")
        by_id = {item.reservation_id: item for item in self.reservations}
        for item in self.reservations:
            if item.parent_reservation_id is not None:
                if item.parent_reservation_id not in known:
                    raise ValueError(f"{item.reservation_id} names an unknown parent reservation")
                parent = by_id[item.parent_reservation_id]
                if item.reserved_at < parent.reserved_at:
                    raise ValueError(
                        f"{item.reservation_id} is reserved before its parent "
                        f"{item.parent_reservation_id}"
                    )
        for charge in self.charges:
            reservation = by_id[charge.reservation_id]
            if charge.settled_at < reservation.reserved_at:
                raise ValueError(f"{charge.reservation_id} is settled before it was reserved")
            if (
                charge.cost_basis is CostBasis.CONSERVATIVE_MAXIMUM
                and charge.charged_cost != reservation.requested_maximum
            ):
                raise ValueError(
                    f"{charge.reservation_id} is settled CONSERVATIVE_MAXIMUM but its "
                    "charged cost does not equal its reservation's own maximum"
                )
        return self

    def outstanding(self) -> int:
        """Sum of the maxima of every reservation not yet settled."""
        settled = {item.reservation_id for item in self.charges}
        return sum(
            item.requested_maximum
            for item in self.reservations
            if item.reservation_id not in settled
        )

    def spent(self) -> int:
        """Sum of every settled charge, actual or conservative."""
        return sum(item.charged_cost for item in self.charges)

    def available(self) -> int:
        """What remains to reserve. Explicitly negative on an overrun, never clamped."""
        return self.total_budget - self.outstanding() - self.spent()


def reserve_budget(state: BudgetPoolState, reservation: BudgetReservation) -> BudgetPoolState:
    """Reserve ``reservation`` against ``state``, refusing an overcommit or a replay.

    ``state`` is revalidated on entry, so a ``model_construct``-built or
    mutated pool cannot be used to compute a forged ``available()``.
    """
    state = _revalidated(BudgetPoolState, state, context="lab budget pool state")
    reservation = _revalidated(BudgetReservation, reservation, context="lab budget reservation")
    if reservation.reservation_id in {item.reservation_id for item in state.reservations}:
        raise LabRoutingViolation(
            "reservation id already reserved; replays are refused",
            detail={"reservation_id": reservation.reservation_id},
        )
    if reservation.parent_reservation_id is not None and reservation.parent_reservation_id not in {
        item.reservation_id for item in state.reservations
    }:
        raise LabRoutingViolation(
            "a nested reservation must name an already-reserved parent",
            detail={
                "reservation_id": reservation.reservation_id,
                "parent_reservation_id": reservation.parent_reservation_id,
            },
        )
    available = state.available()
    if reservation.requested_maximum > available:
        raise LabRoutingViolation(
            "reservation would overcommit the total budget",
            detail={
                "reservation_id": reservation.reservation_id,
                "requested_maximum": reservation.requested_maximum,
                "available": available,
            },
        )
    payload = state.canonical_payload()
    payload["reservations"] = [
        *(item.canonical_payload() for item in state.reservations),
        reservation.canonical_payload(),
    ]
    return validate_contract(
        BudgetPoolState,
        payload,
        error=LabRoutingViolation,
        context="lab budget reservation",
    )


def settle_budget(state: BudgetPoolState, settlement: BudgetSettlement) -> BudgetPoolState:
    """Charge ``settlement`` against ``state``, refusing an unknown or replayed reservation.

    An unknown ``actual_cost`` is charged at the reservation's own ceiling —
    never released as zero — and :meth:`BudgetPoolState.available` is left to
    go negative when a declared actual cost exceeds that ceiling, so an
    overrun is a value a caller reads, never a state this function hides.

    ``state`` is revalidated on entry, guarding this public boundary the same
    way :func:`reserve_budget` does.
    """
    state = _revalidated(BudgetPoolState, state, context="lab budget pool state")
    settlement = _revalidated(BudgetSettlement, settlement, context="lab budget settlement")
    known_ids = {item.reservation_id for item in state.reservations}
    if settlement.reservation_id not in known_ids:
        raise LabRoutingViolation(
            "settlement names a reservation that was never reserved",
            detail={"reservation_id": settlement.reservation_id},
        )
    if settlement.reservation_id in {item.reservation_id for item in state.charges}:
        raise LabRoutingViolation(
            "reservation already settled; replays are refused",
            detail={"reservation_id": settlement.reservation_id},
        )
    reservation = next(
        item for item in state.reservations if item.reservation_id == settlement.reservation_id
    )
    if settlement.actual_cost is not None:
        charged_cost = settlement.actual_cost
        basis = CostBasis.ACTUAL
    else:
        charged_cost = reservation.requested_maximum
        basis = CostBasis.CONSERVATIVE_MAXIMUM
    charge = BudgetChargeRecord(
        reservation_id=settlement.reservation_id,
        outcome=settlement.outcome,
        charged_cost=charged_cost,
        cost_basis=basis,
        settled_at=settlement.settled_at,
    )
    payload = state.canonical_payload()
    payload["charges"] = [
        *(item.canonical_payload() for item in state.charges),
        charge.canonical_payload(),
    ]
    return validate_contract(
        BudgetPoolState,
        payload,
        error=LabRoutingViolation,
        context="lab budget settlement",
    )

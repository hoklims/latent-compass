"""HOK-803 — lab routing is pure, closed and never compensates a required gate."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from latent_compass.episode import AgentFamily
from latent_compass.errors import LatentCompassError
from latent_compass.lab.routing import (
    AbstainReason,
    BudgetChargeRecord,
    BudgetPoolState,
    BudgetReservation,
    BudgetSettlement,
    CapabilityKind,
    CapabilityObservation,
    CostBasis,
    EscalateReason,
    HostCapabilitySnapshot,
    LabRoutingViolation,
    RouteDecision,
    RouteRequest,
    RouteVerdict,
    SettlementOutcome,
    admit_host_capability_snapshot,
    admit_route_request,
    evaluate_route,
    reserve_budget,
    settle_budget,
)

HOST_ID = "lab-host-alpha"
SCOPE = "lab-scope-alpha"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
OBSERVED_AT = "2026-09-01T00:00:00Z"
EXPIRES_AT = "2026-09-30T00:00:00Z"
REQUESTED_AT = "2026-09-10T00:00:00Z"
EXPIRY = "2026-09-20T00:00:00Z"
NOW_IN_WINDOW = "2026-09-15T00:00:00Z"


def request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "request_digest": DIGEST_A,
        "model_digest": DIGEST_A,
        "state_digest": DIGEST_A,
        "source_digest": DIGEST_A,
        "host_id": HOST_ID,
        "agent_family": "claude",
        "scope": SCOPE,
        "candidate_capability_id": "capability-one",
        "candidate_kind": "TOOL",
        "requested_at": REQUESTED_AT,
        "expiry": EXPIRY,
        "cost_ceiling": 5,
        "remaining_budget": 10,
        "advisor_present": True,
        "kill_switch_engaged": False,
        "review_required": False,
        "review_satisfied": False,
        "semctx_proof_required": False,
        "semctx_proof_satisfied": False,
        "explicit_missing_authority": False,
        "explicit_missing_precondition": False,
        "fallback_observation_id": None,
    }
    payload.update(overrides)
    return payload


def snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "host_id": HOST_ID,
        "agent_family": "claude",
        "scope": SCOPE,
        "current_source_digest": DIGEST_A,
        "snapshot_taken_at": OBSERVED_AT,
        "observed_capabilities": [
            {
                "capability_id": "capability-one",
                "kind": "TOOL",
                "observed_at": OBSERVED_AT,
                "expires_at": EXPIRES_AT,
            }
        ],
    }
    payload.update(overrides)
    return payload


def admitted_request(**overrides: object) -> RouteRequest:
    return admit_route_request(request_payload(**overrides))


def admitted_snapshot(**overrides: object) -> HostCapabilitySnapshot:
    return admit_host_capability_snapshot(snapshot_payload(**overrides))


def test_a_fully_matching_request_receives_advice() -> None:
    decision = evaluate_route(admitted_request(), admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ADVICE
    assert decision.matched_capability_id == "capability-one"
    assert decision.matched_kind is CapabilityKind.TOOL
    assert decision.lab_only is True
    assert decision.experimental is True
    assert decision.empirical_claim is False
    assert decision.execution_authority is False


def test_an_engaged_kill_switch_abstains_before_anything_else() -> None:
    request = admitted_request(kill_switch_engaged=True, cost_ceiling=0, remaining_budget=0)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.KILL_SWITCH_ENGAGED


def test_an_absent_advisor_abstains() -> None:
    request = admitted_request(advisor_present=False)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.ADVISOR_ABSENT


def test_an_explicit_missing_authority_escalates() -> None:
    request = admitted_request(explicit_missing_authority=True)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ESCALATE
    assert decision.escalate_reason is EscalateReason.AUTHORITY_MISSING


def test_an_explicit_missing_precondition_escalates() -> None:
    request = admitted_request(explicit_missing_precondition=True)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ESCALATE
    assert decision.escalate_reason is EscalateReason.PRECONDITION_MISSING


def test_an_unsatisfied_required_review_escalates_despite_zero_cost() -> None:
    """A low cost ceiling must never compensate a required review."""
    request = admitted_request(
        review_required=True, review_satisfied=False, cost_ceiling=0, remaining_budget=1000
    )
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ESCALATE
    assert decision.escalate_reason is EscalateReason.REVIEW_REQUIRED_UNSATISFIED


def test_a_satisfied_required_review_does_not_escalate() -> None:
    request = admitted_request(review_required=True, review_satisfied=True)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ADVICE


def test_an_unsatisfied_required_semctx_proof_escalates_despite_zero_cost() -> None:
    request = admitted_request(
        semctx_proof_required=True,
        semctx_proof_satisfied=False,
        cost_ceiling=0,
        remaining_budget=1000,
    )
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ESCALATE
    assert decision.escalate_reason is EscalateReason.SEMCTX_PROOF_REQUIRED_UNSATISFIED


def test_a_scope_mismatch_abstains_as_incompatible() -> None:
    decision = evaluate_route(
        admitted_request(), admitted_snapshot(scope="a-different-scope"), now=NOW_IN_WINDOW
    )
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.CAPABILITY_INCOMPATIBLE


def test_a_conflicting_source_digest_abstains() -> None:
    decision = evaluate_route(
        admitted_request(), admitted_snapshot(current_source_digest=DIGEST_B), now=NOW_IN_WINDOW
    )
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.SOURCE_CONFLICT


def test_now_after_expiry_is_timing_incoherent() -> None:
    decision = evaluate_route(admitted_request(), admitted_snapshot(), now="2026-09-25T00:00:00Z")
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.TIMING_INCOHERENT


def test_now_before_requested_at_is_timing_incoherent() -> None:
    decision = evaluate_route(admitted_request(), admitted_snapshot(), now="2026-09-05T00:00:00Z")
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.TIMING_INCOHERENT


def test_a_missing_capability_abstains() -> None:
    request = admitted_request(candidate_capability_id="never-observed")
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.CAPABILITY_MISSING


def test_a_capability_observed_after_now_is_uncertain() -> None:
    request = admitted_request(requested_at="2026-08-15T00:00:00Z")
    decision = evaluate_route(request, admitted_snapshot(), now="2026-08-20T00:00:00Z")
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.CAPABILITY_UNCERTAIN


def test_a_capability_expired_before_now_is_stale() -> None:
    decision = evaluate_route(
        admitted_request(expiry="2026-10-05T00:00:00Z"),
        admitted_snapshot(),
        now="2026-10-01T00:00:00Z",
    )
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.CAPABILITY_STALE


def test_an_exhausted_budget_abstains() -> None:
    request = admitted_request(cost_ceiling=11, remaining_budget=10)
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.BUDGET_EXHAUSTED


def test_a_suggested_fallback_is_carried_on_abstain_but_never_claimed_executed() -> None:
    request = admitted_request(advisor_present=False, fallback_observation_id="fallback-one")
    decision = evaluate_route(request, admitted_snapshot(), now=NOW_IN_WINDOW)
    assert decision.suggested_fallback_observation_id == "fallback-one"
    assert not hasattr(decision, "fallback_executed")


def test_a_route_request_refuses_an_injected_field() -> None:
    with pytest.raises(LatentCompassError):
        admit_route_request(request_payload(execute=True))


def test_a_route_request_refuses_a_bool_where_an_int_is_declared() -> None:
    with pytest.raises(LatentCompassError):
        admit_route_request(request_payload(cost_ceiling=True))


def test_a_snapshot_refuses_a_duplicate_capability_id_and_kind() -> None:
    with pytest.raises(LatentCompassError):
        admit_host_capability_snapshot(
            snapshot_payload(
                observed_capabilities=[
                    {
                        "capability_id": "capability-one",
                        "kind": "TOOL",
                        "observed_at": OBSERVED_AT,
                        "expires_at": EXPIRES_AT,
                    },
                    {
                        "capability_id": "capability-one",
                        "kind": "TOOL",
                        "observed_at": OBSERVED_AT,
                        "expires_at": EXPIRES_AT,
                    },
                ]
            )
        )


def test_a_route_decision_refuses_a_forged_seal() -> None:
    with pytest.raises(ValidationError):
        RouteDecision(
            contract_version="1.0.0",
            verdict=RouteVerdict.ADVICE,
            matched_capability_id="capability-one",
            matched_kind=CapabilityKind.TOOL,
            request_seal=DIGEST_A,
            snapshot_seal=DIGEST_A,
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            scope=SCOPE,
            decision_seal="sha256:" + "0" * 64,
        )


def test_a_route_decision_refuses_advice_without_a_matched_capability() -> None:
    with pytest.raises(ValidationError):
        RouteDecision(
            contract_version="1.0.0",
            verdict=RouteVerdict.ADVICE,
            request_seal=DIGEST_A,
            snapshot_seal=DIGEST_A,
            host_id=HOST_ID,
            agent_family=AgentFamily.CLAUDE,
            scope=SCOPE,
            decision_seal="sha256:" + "0" * 64,
        )


def test_a_snapshot_taken_after_now_abstains_as_uncertain() -> None:
    """The fixture's own regression: a snapshot dated after ``now`` is refused."""
    decision = evaluate_route(
        admitted_request(),
        admitted_snapshot(snapshot_taken_at="2026-09-16T00:00:00Z"),
        now=NOW_IN_WINDOW,
    )
    assert decision.verdict is RouteVerdict.ABSTAIN
    assert decision.abstain_reason is AbstainReason.CAPABILITY_UNCERTAIN


def test_evaluate_route_revalidates_a_model_construct_forged_request() -> None:
    forged = RouteRequest.model_construct(
        contract_version="1.0.0",
        request_digest=DIGEST_A,
        model_digest=DIGEST_A,
        state_digest=DIGEST_A,
        source_digest=DIGEST_A,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        scope=SCOPE,
        candidate_capability_id="capability-one",
        candidate_kind=CapabilityKind.TOOL,
        requested_at=EXPIRY,
        expiry=REQUESTED_AT,  # swapped: expiry precedes requested_at, bypassing admission
        cost_ceiling=5,
        remaining_budget=10,
        advisor_present=True,
        kill_switch_engaged=False,
        review_required=False,
        review_satisfied=False,
        semctx_proof_required=False,
        semctx_proof_satisfied=False,
        explicit_missing_authority=False,
        explicit_missing_precondition=False,
        fallback_observation_id=None,
    )
    with pytest.raises(LabRoutingViolation):
        evaluate_route(forged, admitted_snapshot(), now=NOW_IN_WINDOW)


def test_evaluate_route_revalidates_a_model_construct_forged_snapshot() -> None:
    forged = HostCapabilitySnapshot.model_construct(
        contract_version="1.0.0",
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        scope=SCOPE,
        current_source_digest=DIGEST_A,
        snapshot_taken_at=OBSERVED_AT,
        observed_capabilities=(
            CapabilityObservation(
                capability_id="capability-one",
                kind=CapabilityKind.TOOL,
                observed_at=EXPIRES_AT,  # after snapshot_taken_at, bypassing admission
                expires_at=EXPIRES_AT,
            ),
        ),
    )
    with pytest.raises(LabRoutingViolation):
        evaluate_route(admitted_request(), forged, now=NOW_IN_WINDOW)


def test_a_decision_binds_to_its_own_request_and_snapshot() -> None:
    decision_a = evaluate_route(admitted_request(), admitted_snapshot(), now=NOW_IN_WINDOW)
    decision_b = evaluate_route(
        admitted_request(host_id="lab-host-beta"),
        admitted_snapshot(host_id="lab-host-beta"),
        now=NOW_IN_WINDOW,
    )
    assert decision_a.verdict is RouteVerdict.ADVICE
    assert decision_b.verdict is RouteVerdict.ADVICE
    assert decision_a.request_seal != decision_b.request_seal
    assert decision_a.decision_seal != decision_b.decision_seal
    assert decision_a.host_id != decision_b.host_id


def test_a_decision_seal_refuses_a_swapped_host_id() -> None:
    """The same ADVICE body/seal must not be reusable for an unrelated host."""
    decision = evaluate_route(admitted_request(), admitted_snapshot(), now=NOW_IN_WINDOW)
    with pytest.raises(ValidationError):
        RouteDecision(
            contract_version=decision.contract_version,
            verdict=decision.verdict,
            matched_capability_id=decision.matched_capability_id,
            matched_kind=decision.matched_kind,
            request_seal=decision.request_seal,
            snapshot_seal=decision.snapshot_seal,
            host_id="a-different-host",
            agent_family=decision.agent_family,
            scope=decision.scope,
            decision_seal=decision.decision_seal,
        )


# --- Budget reservation and reconciliation -----------------------------------


def test_a_reservation_within_budget_succeeds_and_available_shrinks() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    assert state.available() == 60


def test_a_reservation_that_would_overcommit_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=90, reserved_at=OBSERVED_AT),
    )
    with pytest.raises(LabRoutingViolation):
        reserve_budget(
            state,
            BudgetReservation(reservation_id="r-2", requested_maximum=20, reserved_at=OBSERVED_AT),
        )
    # The refused attempt changed nothing: the same request still fits after
    # freeing nothing, proving the pool itself was left untouched.
    assert state.available() == 10


def test_a_duplicate_reservation_id_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    reservation = BudgetReservation(
        reservation_id="r-1", requested_maximum=10, reserved_at=OBSERVED_AT
    )
    state = reserve_budget(state, reservation)
    with pytest.raises(LabRoutingViolation):
        reserve_budget(state, reservation)


def test_a_nested_reservation_names_a_known_parent() -> None:
    state = BudgetPoolState(total_budget=100)
    parent = BudgetReservation(
        reservation_id="parent-1", requested_maximum=50, reserved_at=OBSERVED_AT
    )
    state = reserve_budget(state, parent)
    child = BudgetReservation(
        reservation_id="child-1",
        parent_reservation_id="parent-1",
        requested_maximum=10,
        reserved_at=OBSERVED_AT,
    )
    state = reserve_budget(state, child)
    assert state.available() == 40


def test_a_nested_reservation_with_an_unknown_parent_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    child = BudgetReservation(
        reservation_id="child-1",
        parent_reservation_id="never-reserved",
        requested_maximum=10,
        reserved_at=OBSERVED_AT,
    )
    with pytest.raises(LabRoutingViolation):
        reserve_budget(state, child)


def test_settling_an_unknown_reservation_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    with pytest.raises(LabRoutingViolation):
        settle_budget(
            state,
            BudgetSettlement(
                reservation_id="never-reserved",
                outcome=SettlementOutcome.SUCCEEDED,
                actual_cost=5,
                settled_at=OBSERVED_AT,
            ),
        )


def test_settling_a_reservation_twice_is_refused_as_a_replay() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    settlement = BudgetSettlement(
        reservation_id="r-1",
        outcome=SettlementOutcome.SUCCEEDED,
        actual_cost=10,
        settled_at=OBSERVED_AT,
    )
    state = settle_budget(state, settlement)
    with pytest.raises(LabRoutingViolation):
        settle_budget(state, settlement)


def test_a_known_actual_cost_below_the_ceiling_frees_the_unspent_remainder() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    state = settle_budget(
        state,
        BudgetSettlement(
            reservation_id="r-1",
            outcome=SettlementOutcome.SUCCEEDED,
            actual_cost=10,
            settled_at=OBSERVED_AT,
        ),
    )
    assert state.available() == 90
    assert state.charges[0].cost_basis is CostBasis.ACTUAL


def test_an_unknown_actual_cost_is_charged_conservatively_at_the_ceiling() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    state = settle_budget(
        state,
        BudgetSettlement(
            reservation_id="r-1",
            outcome=SettlementOutcome.CANCELLED,
            actual_cost=None,
            settled_at=OBSERVED_AT,
        ),
    )
    # An unknown cost is never released as zero: the full ceiling is charged.
    assert state.available() == 60
    assert state.charges[0].cost_basis is CostBasis.CONSERVATIVE_MAXIMUM
    assert state.charges[0].charged_cost == 40


def test_a_forged_conservative_maximum_charge_below_the_reservation_ceiling_is_refused() -> None:
    reservation = BudgetReservation(
        reservation_id="r-1", requested_maximum=100, reserved_at=OBSERVED_AT
    )
    forged_charge = BudgetChargeRecord(
        reservation_id="r-1",
        outcome=SettlementOutcome.CANCELLED,
        charged_cost=0,
        cost_basis=CostBasis.CONSERVATIVE_MAXIMUM,
        settled_at=OBSERVED_AT,
    )
    with pytest.raises(ValidationError):
        BudgetPoolState(total_budget=100, reservations=(reservation,), charges=(forged_charge,))


def test_reserve_budget_revalidates_a_model_construct_forged_pool_state() -> None:
    """The exact HOK-803 scenario: total 100, reservation max 100, forged charge 0."""
    reservation = BudgetReservation(
        reservation_id="r-1", requested_maximum=100, reserved_at=OBSERVED_AT
    )
    forged_charge = BudgetChargeRecord(
        reservation_id="r-1",
        outcome=SettlementOutcome.CANCELLED,
        charged_cost=0,
        cost_basis=CostBasis.CONSERVATIVE_MAXIMUM,
        settled_at=OBSERVED_AT,
    )
    forged_state = BudgetPoolState.model_construct(
        total_budget=100, reservations=(reservation,), charges=(forged_charge,)
    )
    with pytest.raises(LabRoutingViolation):
        reserve_budget(
            forged_state,
            BudgetReservation(reservation_id="r-2", requested_maximum=100, reserved_at=OBSERVED_AT),
        )


def test_a_settlement_dated_before_its_reservation_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(
            reservation_id="r-1", requested_maximum=40, reserved_at="2026-09-10T00:00:00Z"
        ),
    )
    with pytest.raises(LabRoutingViolation):
        settle_budget(
            state,
            BudgetSettlement(
                reservation_id="r-1",
                outcome=SettlementOutcome.SUCCEEDED,
                actual_cost=10,
                settled_at="2026-09-09T00:00:00Z",
            ),
        )


def test_a_nested_reservation_dated_before_its_parent_is_refused() -> None:
    state = BudgetPoolState(total_budget=100)
    parent = BudgetReservation(
        reservation_id="parent-1", requested_maximum=50, reserved_at="2026-09-10T00:00:00Z"
    )
    state = reserve_budget(state, parent)
    child = BudgetReservation(
        reservation_id="child-1",
        parent_reservation_id="parent-1",
        requested_maximum=10,
        reserved_at="2026-09-09T00:00:00Z",
    )
    with pytest.raises(LabRoutingViolation):
        reserve_budget(state, child)


def test_a_declared_actual_cost_above_the_ceiling_is_an_explicit_overrun() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    state = settle_budget(
        state,
        BudgetSettlement(
            reservation_id="r-1",
            outcome=SettlementOutcome.FAILED,
            actual_cost=70,
            settled_at=OBSERVED_AT,
        ),
    )
    # The overrun is a value the caller reads, never clamped to zero or hidden.
    assert state.available() == 30
    assert state.charges[0].charged_cost == 70


def test_no_new_reservation_is_accepted_after_an_overrun_even_of_size_zero() -> None:
    state = BudgetPoolState(total_budget=100)
    state = reserve_budget(
        state,
        BudgetReservation(reservation_id="r-1", requested_maximum=40, reserved_at=OBSERVED_AT),
    )
    state = settle_budget(
        state,
        BudgetSettlement(
            reservation_id="r-1",
            outcome=SettlementOutcome.FAILED,
            actual_cost=140,
            settled_at=OBSERVED_AT,
        ),
    )
    assert state.available() == -40
    with pytest.raises(LabRoutingViolation):
        reserve_budget(
            state,
            BudgetReservation(reservation_id="r-2", requested_maximum=0, reserved_at=OBSERVED_AT),
        )

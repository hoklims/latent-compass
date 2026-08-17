"""HOK-184 — proofs that the authority boundary holds by construction."""

from __future__ import annotations

import math
from typing import Any, cast

import pytest
from pydantic import ValidationError

from conftest import reseal
from latent_compass import authority as authority_module
from latent_compass import vocabulary
from latent_compass.authority import (
    ABSTENTION_UNCERTAINTY_THRESHOLD,
    ALLOWED_TRANSITIONS,
    ANTI_GOALS,
    CAPABILITIES,
    EVIDENCE_REQUIREMENTS,
    EXTRA_CAPABILITY,
    FORBIDDEN_FOR_LATENT_COMPASS,
    ILLEGAL_SHORTCUTS,
    THREAT_MODEL,
    Actor,
    Advisory,
    AdvisoryKind,
    Capability,
    ContinueKill,
    EvidenceRequirement,
    LifecycleState,
    RefusalReason,
    TransitionAuthorization,
    authority_boundary_seal,
    authority_boundary_snapshot,
    authorize_transition,
    may_issue_direction,
)
from latent_compass.contracts import AUTHORITY_CONTRACT_VERSION, SUPPORTED_AUTHORITY_VERSIONS
from latent_compass.errors import AuthorityRefusal, ContractViolation, UnsupportedContractVersion
from latent_compass.protocol import (
    HoldoutLedger,
    HoldoutPurpose,
    MeasurementSet,
    Preregistration,
    Split,
    Verdict,
)

FORWARD_EDGES = (
    (LifecycleState.DEFINE, LifecycleState.SHADOW),
    (LifecycleState.SHADOW, LifecycleState.OFFLINE_VERIFIED),
    (LifecycleState.OFFLINE_VERIFIED, LifecycleState.CANARY_ELIGIBLE),
    (LifecycleState.CANARY_ELIGIBLE, LifecycleState.PROMOTED),
)


def evidence_for(
    to_state: LifecycleState,
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> dict[str, Any]:
    """The complete, honest evidence a target state demands."""
    requirement = EVIDENCE_REQUIREMENTS.get(to_state)
    if requirement is None:
        return {}
    supplied: dict[str, Any] = {}
    if requirement.protocol:
        supplied["protocol"] = protocol
    if requirement.verdict:
        supplied["verdict"] = holdout_verdict
    if requirement.measurements:
        supplied["measurements"] = holdout_measurements
    if requirement.holdout_consumption_receipt:
        supplied["holdout_ledger"] = holdout_ledger
    if requirement.human_acknowledgement:
        supplied["human_acknowledged"] = True
    return supplied


# -- the grant --------------------------------------------------------------


def test_latent_compass_holds_exactly_three_capabilities() -> None:
    """The grant is stated exhaustively, not merely checked for absences."""
    assert CAPABILITIES[Actor.LATENT_COMPASS] == frozenset(
        {Capability.OBSERVE, Capability.ADVISE, Capability.RECORD}
    )
    assert CAPABILITIES[Actor.LATENT_COMPASS] & FORBIDDEN_FOR_LATENT_COMPASS == frozenset()


def test_fail_closed_authority_is_a_new_major_contract_with_legacy_advisory_reading() -> None:
    assert AUTHORITY_CONTRACT_VERSION == "2.0.0"
    assert frozenset({"1.0.0", "2.0.0"}) == SUPPORTED_AUTHORITY_VERSIONS


def test_promotion_is_a_human_capability_only() -> None:
    holders = {actor for actor, granted in CAPABILITIES.items() if Capability.PROMOTE in granted}
    assert holders == {Actor.HUMAN_OPERATOR}
    assert EXTRA_CAPABILITY[LifecycleState.PROMOTED] is Capability.PROMOTE


def test_structurally_valid_evidence_is_not_trusted_without_external_attestation(
    protocol: Preregistration,
    validation_measurements: MeasurementSet,
    validation_verdict: Verdict,
) -> None:
    """Self-consistent caller-supplied evidence cannot establish its own origin."""
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.SHADOW,
            to_state=LifecycleState.OFFLINE_VERIFIED,
            actor=Actor.EXTERNAL_JUDGE,
            protocol=protocol,
            measurements=validation_measurements,
            verdict=validation_verdict,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value
    assert detail["missing"] == "trusted_external_attestation"


def test_widening_the_evidence_table_does_not_disable_the_attestation_refusal(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Preregistration,
    validation_measurements: MeasurementSet,
    validation_verdict: Verdict,
) -> None:
    monkeypatch.setitem(
        authority_module._EVIDENCE_REQUIREMENTS,  # noqa: SLF001 - compromised table
        LifecycleState.OFFLINE_VERIFIED,
        EvidenceRequirement(protocol=True, measurements=True, verdict=True),
    )

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.SHADOW,
            to_state=LifecycleState.OFFLINE_VERIFIED,
            actor=Actor.EXTERNAL_JUDGE,
            protocol=protocol,
            measurements=validation_measurements,
            verdict=validation_verdict,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value


def test_fail_closed_refusal_happens_before_any_holdout_ledger_access(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    def unexpected_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("a doomed transition accessed the holdout ledger")

    monkeypatch.setattr(HoldoutLedger, "require_consumption", unexpected_access)
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=holdout_verdict,
            holdout_ledger=holdout_ledger,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value


def test_transition_authorization_rejects_an_unsupported_contract_version() -> None:
    with pytest.raises(UnsupportedContractVersion):
        TransitionAuthorization(
            contract_version="9.0.0",
            from_state=LifecycleState.DEFINE,
            to_state=LifecycleState.SHADOW,
            actor=Actor.HUMAN_OPERATOR,
            authorization_seal="sha256:" + "0" * 64,
        )


@pytest.mark.parametrize(
    "table", [CAPABILITIES, ALLOWED_TRANSITIONS, EVIDENCE_REQUIREMENTS, EXTRA_CAPABILITY]
)
def test_the_enforcement_tables_are_not_mutable_by_consumers(table: Any) -> None:
    with pytest.raises(TypeError):
        table[Actor.LATENT_COMPASS] = frozenset()
    with pytest.raises(AttributeError):
        table.clear()


# -- the unconditional refusal ---------------------------------------------


@pytest.mark.parametrize(("from_state", "to_state"), FORWARD_EDGES)
def test_latent_compass_is_refused_and_evidence_moves_fail_closed_without_attestation(
    from_state: LifecycleState,
    to_state: LifecycleState,
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    """Self-authorisation and missing provenance remain distinct refusals."""
    supplied = evidence_for(
        to_state, protocol, holdout_measurements, holdout_ledger, holdout_verdict
    )
    if to_state is LifecycleState.SHADOW:
        granted = authorize_transition(
            from_state=from_state,
            to_state=to_state,
            actor=Actor.HUMAN_OPERATOR,
            **supplied,
        )
        assert granted.to_state is to_state
    else:
        with pytest.raises(AuthorityRefusal) as refusal:
            authorize_transition(
                from_state=from_state,
                to_state=to_state,
                actor=Actor.HUMAN_OPERATOR,
                **supplied,
            )
        detail = refusal.value.detail
        assert isinstance(detail, dict)
        assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=from_state, to_state=to_state, actor=Actor.LATENT_COMPASS, **supplied
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.SELF_AUTHORISATION.value


def test_widening_the_grant_does_not_let_latent_compass_authorise_anything(
    monkeypatch: pytest.MonkeyPatch, protocol: Preregistration
) -> None:
    """The invariant is unconditionality, not check order.

    An implementation whose refusal was merely "latent_compass lacks the
    capability" would authorise here, because the capability is granted. The
    refusal must not consult the table at all.
    """
    monkeypatch.setitem(
        vocabulary._CAPABILITIES,  # noqa: SLF001 - simulating a compromised table
        Actor.LATENT_COMPASS,
        frozenset(Capability),
    )
    assert Capability.AUTHORIZE_TRANSITION in CAPABILITIES[Actor.LATENT_COMPASS]
    assert Capability.PROMOTE in CAPABILITIES[Actor.LATENT_COMPASS]

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.DEFINE,
            to_state=LifecycleState.SHADOW,
            actor=Actor.LATENT_COMPASS,
            protocol=protocol,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.SELF_AUTHORISATION.value


# -- the actor x transition matrix -----------------------------------------


@pytest.mark.parametrize("actor", list(Actor))
@pytest.mark.parametrize(("from_state", "to_state"), FORWARD_EDGES)
def test_the_actor_by_transition_matrix_is_exact(
    actor: Actor,
    from_state: LifecycleState,
    to_state: LifecycleState,
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    """Exactly which actors may make each forward move, and for what reason."""
    supplied = evidence_for(
        to_state, protocol, holdout_measurements, holdout_ledger, holdout_verdict
    )
    may_pass = to_state is LifecycleState.SHADOW and actor in {
        Actor.HUMAN_OPERATOR,
        Actor.EXTERNAL_JUDGE,
    }
    if may_pass:
        granted = authorize_transition(
            from_state=from_state, to_state=to_state, actor=actor, **supplied
        )
        assert granted.actor is actor
        return

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(from_state=from_state, to_state=to_state, actor=actor, **supplied)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    expected = {
        Actor.LATENT_COMPASS: RefusalReason.SELF_AUTHORISATION.value,
        Actor.OBSERVED_AGENT: RefusalReason.ACTOR_LACKS_CAPABILITY.value,
        Actor.EXTERNAL_JUDGE: (
            RefusalReason.ACTOR_LACKS_CAPABILITY.value
            if to_state is LifecycleState.PROMOTED
            else RefusalReason.UNTRUSTED_EVIDENCE.value
        ),
        Actor.HUMAN_OPERATOR: RefusalReason.UNTRUSTED_EVIDENCE.value,
    }[actor]
    assert detail["reason"] == expected


def test_the_external_judge_may_advance_but_never_promote(
    protocol: Preregistration, holdout_verdict: Verdict
) -> None:
    """The judge holds authorize_transition; promotion needs promote as well."""
    advanced = authorize_transition(
        from_state=LifecycleState.DEFINE,
        to_state=LifecycleState.SHADOW,
        actor=Actor.EXTERNAL_JUDGE,
        protocol=protocol,
    )
    assert advanced.actor is Actor.EXTERNAL_JUDGE

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.EXTERNAL_JUDGE,
            protocol=protocol,
            verdict=holdout_verdict,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.ACTOR_LACKS_CAPABILITY.value
    assert detail["required_capability"] == Capability.PROMOTE.value


# -- verified evidence ------------------------------------------------------


def test_a_promotion_refuses_before_inspecting_unattested_evidence(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
    holdout_verdict: Verdict,
) -> None:
    for supplied in (
        {
            "measurements": holdout_measurements,
            "verdict": holdout_verdict,
            "holdout_ledger": holdout_ledger,
            "human_acknowledged": True,
        },
        {
            "protocol": protocol,
            "measurements": holdout_measurements,
            "holdout_ledger": holdout_ledger,
            "human_acknowledged": True,
        },
        {
            "protocol": protocol,
            "verdict": holdout_verdict,
            "holdout_ledger": holdout_ledger,
            "human_acknowledged": True,
        },
        {
            "protocol": protocol,
            "measurements": holdout_measurements,
            "verdict": holdout_verdict,
            "holdout_ledger": holdout_ledger,
        },
    ):
        with pytest.raises(AuthorityRefusal) as refusal:
            authorize_transition(
                from_state=LifecycleState.CANARY_ELIGIBLE,
                to_state=LifecycleState.PROMOTED,
                actor=Actor.HUMAN_OPERATOR,
                **cast(Any, supplied),
            )
        detail = refusal.value.detail
        assert isinstance(detail, dict)
        assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value
        assert detail["missing"] == "trusted_external_attestation"


def test_a_verdict_edited_after_sealing_is_refused(
    protocol: Preregistration,
    failing_holdout_measurements: MeasurementSet,
    failing_holdout_verdict: Verdict,
) -> None:
    """The load-bearing case: KILL rewritten to CONTINUE without re-sealing."""
    laundered = failing_holdout_verdict.model_copy(update={"decision": ContinueKill.CONTINUE})
    assert laundered.decision is ContinueKill.CONTINUE
    assert laundered.verdict_seal == failing_holdout_verdict.verdict_seal

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.REJECTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=failing_holdout_measurements,
            verdict=laundered,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.EVIDENCE_FORGED.value


def test_an_invented_verdict_is_refused(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_verdict: Verdict,
) -> None:
    """A caller cannot write a seal string and be believed."""
    invented = holdout_verdict.model_copy(update={"verdict_seal": "sha256:" + "0" * 64})
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.REJECTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=invented,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.EVIDENCE_FORGED.value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_seal", "sha256:" + "1" * 64),
        ("epoch", "LC-SOME-OTHER-EPOCH"),
        ("corpus_seal", "sha256:not-the-holdout"),
    ],
)
def test_a_correctly_sealed_verdict_from_elsewhere_is_refused(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_verdict: Verdict,
    field: str,
    value: str,
) -> None:
    """Self-consistency is not enough; the verdict must be *this* protocol's."""
    elsewhere = reseal(holdout_verdict, **{field: value})
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.REJECTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=holdout_measurements,
            verdict=elsewhere,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.EVIDENCE_MISMATCH.value


def test_promotion_requires_a_final_holdout_verdict(
    protocol: Preregistration,
    validation_measurements: MeasurementSet,
    validation_verdict: Verdict,
) -> None:
    """A CONTINUE on validation is a real verdict, and still not enough."""
    assert validation_verdict.decision is ContinueKill.CONTINUE
    assert validation_verdict.split is Split.VALIDATION

    with pytest.raises(AuthorityRefusal) as offline_refusal:
        authorize_transition(
            from_state=LifecycleState.SHADOW,
            to_state=LifecycleState.OFFLINE_VERIFIED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=validation_measurements,
            verdict=validation_verdict,
        )
    offline_detail = offline_refusal.value.detail
    assert isinstance(offline_detail, dict)
    assert offline_detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value

    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=validation_measurements,
            verdict=validation_verdict,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value
    assert detail["missing"] == "trusted_external_attestation"


def test_a_kill_verdict_admits_only_rejection(
    protocol: Preregistration,
    failing_holdout_measurements: MeasurementSet,
    failing_holdout_verdict: Verdict,
) -> None:
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=LifecycleState.CANARY_ELIGIBLE,
            to_state=LifecycleState.PROMOTED,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            measurements=failing_holdout_measurements,
            verdict=failing_holdout_verdict,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == RefusalReason.UNTRUSTED_EVIDENCE.value

    allowed = authorize_transition(
        from_state=LifecycleState.CANARY_ELIGIBLE,
        to_state=LifecycleState.REJECTED,
        actor=Actor.HUMAN_OPERATOR,
        protocol=protocol,
        measurements=failing_holdout_measurements,
        verdict=failing_holdout_verdict,
    )
    assert allowed.to_state is LifecycleState.REJECTED


def test_the_authorisation_records_the_verified_shadow_protocol(
    protocol: Preregistration,
) -> None:
    granted = authorize_transition(
        from_state=LifecycleState.DEFINE,
        to_state=LifecycleState.SHADOW,
        actor=Actor.HUMAN_OPERATOR,
        protocol=protocol,
    )
    assert granted.protocol_seal == protocol.protocol_seal()
    assert granted.verdict_seal is None
    assert granted.measurement_set_seal is None
    assert granted.corpus_seal is None
    assert granted.protocol_id == protocol.protocol_id
    assert granted.human_acknowledged is False


# -- the transition table ---------------------------------------------------


@pytest.mark.parametrize(("from_state", "to_state"), ILLEGAL_SHORTCUTS)
def test_named_shortcuts_are_refused_even_for_a_human(
    from_state: LifecycleState,
    to_state: LifecycleState,
    protocol: Preregistration,
    holdout_verdict: Verdict,
) -> None:
    with pytest.raises(AuthorityRefusal) as refusal:
        authorize_transition(
            from_state=from_state,
            to_state=to_state,
            actor=Actor.HUMAN_OPERATOR,
            protocol=protocol,
            verdict=holdout_verdict,
            human_acknowledged=True,
        )
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] in {
        RefusalReason.ILLEGAL_TRANSITION.value,
        RefusalReason.TERMINAL_STATE.value,
    }


def test_rejected_is_terminal_and_rollback_exists() -> None:
    assert ALLOWED_TRANSITIONS[LifecycleState.REJECTED] == frozenset()
    assert LifecycleState.REJECTED in ALLOWED_TRANSITIONS[LifecycleState.PROMOTED]


def test_every_state_can_still_reach_rejected_without_evidence() -> None:
    """Refusing must never be gated; a candidate can always be killed."""
    for state, targets in ALLOWED_TRANSITIONS.items():
        if state is LifecycleState.REJECTED:
            continue
        assert LifecycleState.REJECTED in targets
        granted = authorize_transition(
            from_state=state, to_state=LifecycleState.REJECTED, actor=Actor.HUMAN_OPERATOR
        )
        assert granted.to_state is LifecycleState.REJECTED
    assert LifecycleState.REJECTED not in EVIDENCE_REQUIREMENTS


# -- advisories -------------------------------------------------------------


@pytest.mark.parametrize("uncertainty", [math.nan, math.inf, -math.inf, -5.0, 1.5, "0.2", None])
def test_may_issue_direction_refuses_nonsense(uncertainty: Any) -> None:
    with pytest.raises(ContractViolation):
        may_issue_direction(uncertainty)


def test_may_issue_direction_answers_only_inside_the_unit_interval() -> None:
    assert may_issue_direction(0.0) is True
    assert may_issue_direction(ABSTENTION_UNCERTAINTY_THRESHOLD) is True
    assert may_issue_direction(ABSTENTION_UNCERTAINTY_THRESHOLD + 0.01) is False
    assert may_issue_direction(1.0) is False


def test_high_uncertainty_cannot_be_dressed_as_a_direction() -> None:
    over = ABSTENTION_UNCERTAINTY_THRESHOLD + 0.01
    with pytest.raises(ValidationError, match="abstention threshold"):
        Advisory(
            contract_version="1.0.0",
            kind=AdvisoryKind.DIRECTION,
            direction_id="dir-alpha",
            confidence=0.99,
            uncertainty=over,
            rationale="confident despite knowing nothing",
            issued_by=Actor.LATENT_COMPASS,
        )
    abstained = Advisory(
        contract_version="1.0.0",
        kind=AdvisoryKind.ABSTAIN,
        confidence=0.0,
        uncertainty=over,
        rationale="uncertainty above the abstention threshold",
        issued_by=Actor.LATENT_COMPASS,
    )
    assert abstained.direction_id is None


def test_an_advisory_must_declare_its_contract_version() -> None:
    with pytest.raises(ValidationError, match="contract_version"):
        Advisory.model_validate(
            {
                "kind": "ABSTAIN",
                "confidence": 0.1,
                "uncertainty": 0.5,
                "rationale": "no version declared",
                "issued_by": "latent_compass",
            }
        )


def test_an_advisory_from_the_future_is_refused() -> None:
    with pytest.raises(ValidationError):
        Advisory.model_validate(
            {
                "contract_version": "9.0.0",
                "kind": "ABSTAIN",
                "confidence": 0.1,
                "uncertainty": 0.5,
                "rationale": "written by a newer build",
                "issued_by": "latent_compass",
            }
        )


def test_an_advisory_has_no_field_to_carry_an_action() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Advisory.model_validate(
            {
                "contract_version": "1.0.0",
                "kind": "DIRECTION",
                "direction_id": "dir-alpha",
                "confidence": 0.7,
                "uncertainty": 0.1,
                "rationale": "legitimate looking",
                "issued_by": "latent_compass",
                "action": "promote",
            }
        )


def test_an_advisory_may_only_be_issued_by_latent_compass() -> None:
    with pytest.raises(ValidationError, match="only be issued by latent_compass"):
        Advisory(
            contract_version="1.0.0",
            kind=AdvisoryKind.ABSTAIN,
            confidence=0.1,
            uncertainty=0.5,
            rationale="claiming to speak for a human",
            issued_by=Actor.HUMAN_OPERATOR,
        )


def test_advisory_kinds_never_include_an_executable_verb() -> None:
    assert {kind.value for kind in AdvisoryKind} == {
        "DIRECTION",
        "ABSTAIN",
        "FALLBACK",
        "ESCALATE",
    }


# -- the boundary as data ---------------------------------------------------


def test_boundary_snapshot_is_stable_and_reflects_the_grant() -> None:
    first = authority_boundary_snapshot()
    assert first == authority_boundary_snapshot()
    assert authority_boundary_seal() == authority_boundary_seal()
    capabilities = first["capabilities"]
    transitions = first["transitions"]
    assert isinstance(capabilities, dict)
    assert isinstance(transitions, dict)
    assert capabilities["latent_compass"] == ["advise", "observe", "record"]
    assert first["abstention_uncertainty_threshold"] == ABSTENTION_UNCERTAINTY_THRESHOLD
    assert first["unconditional_refusals"] == ["latent_compass"]
    assert transitions["REJECTED"] == []
    assert first["extra_capability_per_target"] == {"PROMOTED": "promote"}
    requirements = cast(dict[str, dict[str, object]], first["evidence_requirements"])
    assert requirements["SHADOW"]["trusted_external_attestation"] is False
    assert requirements["OFFLINE_VERIFIED"]["trusted_external_attestation"] is True
    assert requirements["CANARY_ELIGIBLE"]["trusted_external_attestation"] is True
    assert requirements["PROMOTED"]["trusted_external_attestation"] is True


def test_widening_the_grant_would_change_the_boundary_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seal is a tripwire, so prove it actually trips."""
    before = authority_boundary_seal()
    monkeypatch.setitem(
        vocabulary._CAPABILITIES,  # noqa: SLF001 - the seal must notice a widened table
        Actor.LATENT_COMPASS,
        CAPABILITIES[Actor.LATENT_COMPASS] | {Capability.PROMOTE},
    )
    assert authority_boundary_seal() != before


def test_anti_goals_and_threat_model_are_populated_and_honest() -> None:
    assert len(ANTI_GOALS) >= 5
    assert len(THREAT_MODEL) >= 5
    joined = " ".join(THREAT_MODEL)
    assert "NOT defended" in joined, "the threat model must name what it does not cover"
    assert "validates and records" in " ".join(ANTI_GOALS)


def test_the_holdout_purpose_vocabulary_is_exact() -> None:
    assert {purpose.value for purpose in HoldoutPurpose} == {
        "FINAL_VERDICT",
        "TRAINING",
        "SELECTION",
        "EXPLORATION",
    }

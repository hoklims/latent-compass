"""Hostile and functional coverage for latent_compass.lab.memory."""

from __future__ import annotations

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.errors import LabMemoryViolationError
from latent_compass.lab.memory import (
    ClaimAssertion,
    FactAssertion,
    FactRevocation,
    MemoryBinding,
    MemoryEvent,
    MemoryEventKind,
    SupportConjunction,
    SupportRevision,
    compute_applicability,
)

SOURCE_SCOPE = "sha256:" + "4" * 64
AS_OF = "2026-09-18T12:00:00Z"


def binding() -> MemoryBinding:
    return MemoryBinding(
        host_id="host-alpha", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )


def fact_event(sequence: int, fact_id: str, *, expires_at: str | None = None) -> MemoryEvent:
    fact = FactAssertion(
        contract_version="1.0.0",
        fact_id=fact_id,
        statement=f"root premise {fact_id}",
        provenance="test-source",
        scope="test-scope",
        asserted_at="2026-09-01T00:00:00Z",
        expires_at=expires_at,
    )
    return MemoryEvent(
        contract_version="1.0.0",
        binding=binding(),
        sequence=sequence,
        recorded_at="2026-09-01T00:00:00Z",
        kind=MemoryEventKind.FACT,
        fact=fact,
    )


def fact_revocation_event(sequence: int, fact_id: str) -> MemoryEvent:
    revocation = FactRevocation(
        contract_version="1.0.0",
        fact_id=fact_id,
        reason="superseded",
        revoked_at="2026-09-05T00:00:00Z",
    )
    return MemoryEvent(
        contract_version="1.0.0",
        binding=binding(),
        sequence=sequence,
        recorded_at="2026-09-05T00:00:00Z",
        kind=MemoryEventKind.FACT_REVOCATION,
        fact_revocation=revocation,
    )


def claim_event(
    sequence: int,
    claim_id: str,
    premise_groups: list[list[str]],
    *,
    expires_at: str | None = None,
) -> MemoryEvent:
    claim = ClaimAssertion(
        contract_version="1.0.0",
        claim_id=claim_id,
        statement=f"derived claim {claim_id}",
        scope="test-scope",
        asserted_at="2026-09-01T00:00:00Z",
        expires_at=expires_at,
        supports=tuple(SupportConjunction(premise_ids=tuple(group)) for group in premise_groups),
    )
    return MemoryEvent(
        contract_version="1.0.0",
        binding=binding(),
        sequence=sequence,
        recorded_at="2026-09-01T00:00:00Z",
        kind=MemoryEventKind.CLAIM,
        claim=claim,
    )


def support_revision_event(
    sequence: int,
    claim_id: str,
    supersedes_seal: str,
    premise_groups: list[list[str]],
) -> MemoryEvent:
    revision = SupportRevision(
        contract_version="1.0.0",
        claim_id=claim_id,
        supersedes_seal=supersedes_seal,
        supports=tuple(SupportConjunction(premise_ids=tuple(group)) for group in premise_groups),
        reason="revised the claim's supports",
        revised_at="2026-09-06T00:00:00Z",
    )
    return MemoryEvent(
        contract_version="1.0.0",
        binding=binding(),
        sequence=sequence,
        recorded_at="2026-09-06T00:00:00Z",
        kind=MemoryEventKind.SUPPORT_REVISION,
        support_revision=revision,
    )


def test_empty_history_returns_empty_applicability() -> None:
    result = compute_applicability([], as_of=AS_OF)
    assert result.active_fact_ids == frozenset()
    assert result.active_claim_ids == frozenset()


def test_invalid_as_of_is_refused_even_for_empty_history() -> None:
    with pytest.raises(LabMemoryViolationError):
        compute_applicability([], as_of="not-a-time")


def test_future_fact_does_not_change_the_past_view() -> None:
    event = fact_event(0, "root")
    assert event.fact is not None
    future = event.model_copy(
        update={
            "recorded_at": "2026-09-20T00:00:00Z",
            "fact": event.fact.model_copy(update={"asserted_at": "2026-09-20T00:00:00Z"}),
        }
    )
    assert compute_applicability([future], as_of=AS_OF).active_fact_ids == frozenset()
    assert compute_applicability([future], as_of="2026-09-20T00:00:00Z").active_fact_ids == {"root"}


def test_future_revocation_preserves_past_applicability() -> None:
    event = fact_revocation_event(1, "root")
    assert event.fact_revocation is not None
    future = event.model_copy(
        update={
            "recorded_at": "2026-09-20T00:00:00Z",
            "fact_revocation": event.fact_revocation.model_copy(
                update={"revoked_at": "2026-09-20T00:00:00Z"}
            ),
        }
    )
    history = [fact_event(0, "root"), future]
    assert compute_applicability(history, as_of=AS_OF).active_fact_ids == {"root"}
    assert compute_applicability(history, as_of="2026-09-20T00:00:00Z").revoked_fact_ids == {"root"}


@pytest.mark.parametrize("fact_first", [True, False])
def test_fact_and_claim_cannot_share_a_premise_id(fact_first: bool) -> None:
    events = (
        [fact_event(0, "same"), claim_event(1, "same", [["same"]])]
        if fact_first
        else [claim_event(0, "same", [["same"]]), fact_event(1, "same")]
    )
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_later_support_revision_cannot_hide_an_invalid_old_reference() -> None:
    claim = claim_event(1, "claim", [["never-declared"]])
    repaired = support_revision_event(2, "claim", claim.event_seal(), [["root"]])
    with pytest.raises(LabMemoryViolationError):
        compute_applicability([fact_event(0, "root"), claim, repaired], as_of=AS_OF)


def test_a_grounded_chain_of_facts_and_claims_is_active() -> None:
    events = [
        fact_event(0, "fact-root"),
        claim_event(1, "claim-a", [["fact-root"]]),
        claim_event(2, "claim-b", [["claim-a"]]),
    ]
    result = compute_applicability(events, as_of=AS_OF)
    assert result.active_fact_ids == frozenset({"fact-root"})
    assert result.active_claim_ids == frozenset({"claim-a", "claim-b"})


def test_a_self_citing_claim_with_no_root_stays_inactive() -> None:
    events = [claim_event(0, "claim-cycle", [["claim-cycle"]])]
    result = compute_applicability(events, as_of=AS_OF)
    assert result.active_claim_ids == frozenset()
    assert result.inactive_claim_ids == frozenset({"claim-cycle"})


def test_a_mutual_cycle_with_no_root_stays_inactive() -> None:
    events = [
        claim_event(0, "claim-x", [["claim-y"]]),
        claim_event(1, "claim-y", [["claim-x"]]),
    ]
    result = compute_applicability(events, as_of=AS_OF)
    assert result.active_claim_ids == frozenset()
    assert result.inactive_claim_ids == frozenset({"claim-x", "claim-y"})


def test_an_independent_support_can_ground_a_cycle_and_removing_it_reverts() -> None:
    cycle_event = claim_event(0, "claim-cycle", [["claim-cycle"]])
    root_fact = fact_event(1, "fact-independent")
    events = [cycle_event, root_fact]

    baseline = compute_applicability(events, as_of=AS_OF)
    assert baseline.active_claim_ids == frozenset()

    revised = support_revision_event(
        2, "claim-cycle", cycle_event.event_seal(), [["claim-cycle"], ["fact-independent"]]
    )
    grounded = compute_applicability([*events, revised], as_of=AS_OF)
    assert grounded.active_claim_ids == frozenset({"claim-cycle"})

    reverted = support_revision_event(3, "claim-cycle", revised.event_seal(), [["claim-cycle"]])
    inactive_again = compute_applicability([*events, revised, reverted], as_of=AS_OF)
    assert inactive_again.active_claim_ids == frozenset()
    # The history is preserved, not rewritten: the grounded read at revision 2
    # is still exactly what it was, even after revision 3 exists.
    assert grounded.active_claim_ids == frozenset({"claim-cycle"})


def test_revoking_the_root_fact_deactivates_every_transitive_dependent() -> None:
    events = [
        fact_event(0, "fact-root"),
        claim_event(1, "claim-a", [["fact-root"]]),
        claim_event(2, "claim-b", [["claim-a"]]),
        fact_revocation_event(3, "fact-root"),
    ]
    result = compute_applicability(events, as_of=AS_OF)
    assert result.revoked_fact_ids == frozenset({"fact-root"})
    assert result.active_claim_ids == frozenset()
    assert result.inactive_claim_ids == frozenset({"claim-a", "claim-b"})


def test_an_alternative_support_preserves_a_conclusion_when_one_premise_disappears() -> None:
    events = [
        fact_event(0, "fact-primary"),
        fact_event(1, "fact-secondary"),
        claim_event(2, "claim-either", [["fact-primary"], ["fact-secondary"]]),
        fact_revocation_event(3, "fact-primary"),
    ]
    result = compute_applicability(events, as_of=AS_OF)
    assert result.active_claim_ids == frozenset({"claim-either"})


def test_expiry_excludes_a_fact_without_marking_it_revoked() -> None:
    events = [fact_event(0, "fact-temporary", expires_at="2026-09-10T00:00:00Z")]
    result = compute_applicability(events, as_of="2026-09-15T00:00:00Z")
    assert result.active_fact_ids == frozenset()
    assert result.revoked_fact_ids == frozenset()


def test_a_fact_not_yet_expired_is_still_active() -> None:
    """Proves the other half: the same fact is active before its expiry."""
    events = [fact_event(0, "fact-temporary", expires_at="2026-09-20T00:00:00Z")]
    result = compute_applicability(events, as_of="2026-09-15T00:00:00Z")
    assert result.active_fact_ids == frozenset({"fact-temporary"})


def test_a_claim_naming_an_unknown_premise_is_refused() -> None:
    events = [claim_event(0, "claim-orphan", [["fact-nonexistent"]])]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_duplicate_fact_id_is_refused() -> None:
    events = [fact_event(0, "fact-dup"), fact_event(1, "fact-dup")]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_duplicate_claim_id_is_refused() -> None:
    events = [
        claim_event(0, "claim-dup", [["claim-dup"]]),
        claim_event(1, "claim-dup", [["claim-dup"]]),
    ]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_revocation_naming_an_unknown_fact_is_refused() -> None:
    events = [fact_revocation_event(0, "fact-nonexistent")]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_binding_drift_mid_history_is_refused() -> None:
    drifted_binding = MemoryBinding(
        host_id="host-beta", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )
    drifted = fact_event(1, "fact-second").model_copy(update={"binding": drifted_binding})
    events = [fact_event(0, "fact-first"), drifted]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_non_sequential_history_is_refused() -> None:
    events = [fact_event(0, "fact-first"), fact_event(2, "fact-second")]
    with pytest.raises(LabMemoryViolationError):
        compute_applicability(events, as_of=AS_OF)


def test_a_support_revision_naming_the_wrong_prior_seal_is_refused() -> None:
    fact = fact_event(0, "fact-root")
    claim = claim_event(1, "claim-a", [["fact-root"]])
    wrong = support_revision_event(2, "claim-a", "sha256:" + "0" * 64, [["fact-root"]])
    with pytest.raises(LabMemoryViolationError):
        compute_applicability([fact, claim, wrong], as_of=AS_OF)


def test_a_support_revision_naming_the_exact_prior_seal_is_granted() -> None:
    """Proves the other half: the correct seal chain is accepted."""
    fact = fact_event(0, "fact-root")
    claim = claim_event(1, "claim-a", [["fact-root"]])
    correct = support_revision_event(2, "claim-a", claim.event_seal(), [["fact-root"]])
    result = compute_applicability([fact, claim, correct], as_of=AS_OF)
    assert result.active_claim_ids == frozenset({"claim-a"})

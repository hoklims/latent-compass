"""HOK-244 — deterministic replay: the pre-action evidence beside what happened.

Replay is a **pure, total, side-effect-free comparison**. It takes one sealed
reconciliation and the one pre-action
:class:`~latent_compass.decision_memory.StrategicDecisionRecord` it names, proves
they are the same preimage, and lays the two out dimension by dimension: the exact
pre-action evidence facts recorded for the *executed* candidate beside the
observed value — or beside the named unknown — for the same dimension.

What replay produces
--------------------
Five :class:`DimensionComparison` entries, in the canonical dimension order, each
carrying the pre-action side verbatim, the post-action side verbatim, and one
:class:`ComparisonLinkage` label saying which of the two sides exist. That label
is *structural*: ``BOTH_PRESENT`` means both sides have content, not that they
agree.

What replay refuses to produce
------------------------------
No scalar score, no aggregate, no ranking, no agreement or disagreement verdict,
no causal claim, no authorization, no promotion, no routing hint and nothing
learned. It compares one executed candidate against its own recorded preimage and
stops. Whether the observation vindicates the decision is a judgment for a reader
with authority this package does not have and does not model.

Replaying an unexecuted reconciliation yields **no comparisons at all**. There is
no pre-action-versus-outcome pair to build for a road not taken, and inventing one
would be the counterfactual this contract exists to refuse.

Determinism
-----------
The same reconciliation and the same decision record produce a byte-identical
report and therefore an identical ``replay_seal``, on any host running this
version. Replay reads no clock, opens no file and consults no store.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    RECONCILIATION_REPLAY_CONTRACT_VERSION,
    SUPPORTED_RECONCILIATION_REPLAY_VERSIONS,
    Identifier,
    Seal,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.contracts import StrategicDecisionRecord
from latent_compass.decision_reconciliation.contracts import (
    MAX_RECONCILIATION_REVISION,
    AuthorizationState,
    DecisionPreimageReference,
    DimensionObservation,
    ExecutionState,
    ObservationStatus,
    ObservationText,
    ReconciliationRecord,
)
from latent_compass.errors import PreimageMismatch, ReconciliationViolation
from latent_compass.pairwise_capture import (
    DIMENSION_ORDER,
    DimensionEvidence,
    EvidenceAvailability,
    JudgeableCandidate,
    JudgmentDimension,
    SemanticFact,
)

__all__ = [
    "ComparisonLinkage",
    "DimensionComparison",
    "ReconciliationReplay",
    "replay_reconciliation",
    "require_preimage_match",
]

REPLAY_SEAL_DOMAIN: Final = "decision-reconciliation.replay.v1"


class ComparisonLinkage(StrEnum):
    """Which of the two sides of one dimension exist. Structural, never evaluative.

    ``BOTH_PRESENT`` says the pre-action evidence was judgeable *and* the
    dimension was observed. It does not say they agree, and nothing in this
    package computes whether they do.
    """

    BOTH_PRESENT = "BOTH_PRESENT"
    PRE_ACTION_ONLY = "PRE_ACTION_ONLY"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"
    NEITHER = "NEITHER"


class DimensionComparison(StrictModel):
    """One dimension: the recorded pre-action evidence beside what was observed."""

    dimension: JudgmentDimension
    pre_action_availability: EvidenceAvailability
    pre_action_facts: tuple[SemanticFact, ...] = Field(default=(), max_length=32)
    pre_action_reason: ObservationText | None = Field(default=None)
    observation: DimensionObservation
    linkage: ComparisonLinkage

    @model_validator(mode="after")
    def _sides_and_linkage_agree(self) -> Self:
        if self.observation.dimension is not self.dimension:
            raise ValueError("a comparison must place the observation of its own dimension")
        pre_action_present = self.pre_action_availability is EvidenceAvailability.PRESENT
        if pre_action_present:
            if not self.pre_action_facts or self.pre_action_reason is not None:
                raise ValueError("PRESENT pre-action evidence carries facts and no reason")
        elif self.pre_action_facts or self.pre_action_reason is None:
            raise ValueError("NOT_JUDGEABLE pre-action evidence carries a reason and no facts")
        observed = self.observation.status is ObservationStatus.OBSERVED
        expected = _linkage(pre_action_present=pre_action_present, observed=observed)
        if self.linkage is not expected:
            raise ValueError(
                f"linkage {self.linkage.value} does not follow from the two sides "
                f"({expected.value} does)"
            )
        return self


class ReconciliationReplay(StrictModel):
    """A deterministic, self-sealing comparison report. Carries no judgment."""

    contract_version: str = Field(min_length=5, max_length=20)
    reconciliation_id: Identifier
    revision: int = Field(ge=1, le=MAX_RECONCILIATION_REVISION)
    reconciliation_record_seal: Seal
    preimage: DecisionPreimageReference
    authorization_state: AuthorizationState
    execution_state: ExecutionState
    executed_direction_id: Identifier | None = Field(default=None)
    comparisons: tuple[DimensionComparison, ...] = Field(default=(), max_length=5)
    replay_seal: Seal

    def sealed_body(self) -> dict[str, object]:
        """The canonical payload minus the seal, which is what the seal covers."""
        body = self.canonical_payload()
        body.pop("replay_seal", None)
        return body

    @model_validator(mode="after")
    def _report_is_coherent_and_sealed(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_RECONCILIATION_REPLAY_VERSIONS,
            "reconciliation replay",
        )
        executed = self.execution_state is ExecutionState.EXECUTED
        if executed:
            if self.executed_direction_id is None:
                raise ValueError("an executed replay names the direction it replayed")
            if tuple(item.dimension for item in self.comparisons) != DIMENSION_ORDER:
                raise ValueError(
                    "an executed replay compares every one of the five dimensions exactly "
                    "once, in canonical order"
                )
        else:
            if self.executed_direction_id is not None:
                raise ValueError("only an executed replay names a direction")
            if self.comparisons:
                raise ValueError(
                    "there is nothing to compare for an unexecuted candidate; a "
                    "counterfactual comparison is not producible here"
                )
        if seal(REPLAY_SEAL_DOMAIN, self.sealed_body()) != self.replay_seal:
            raise ValueError("replay_seal does not reproduce from the report body")
        return self


def _linkage(*, pre_action_present: bool, observed: bool) -> ComparisonLinkage:
    if pre_action_present and observed:
        return ComparisonLinkage.BOTH_PRESENT
    if pre_action_present:
        return ComparisonLinkage.PRE_ACTION_ONLY
    if observed:
        return ComparisonLinkage.OBSERVATION_ONLY
    return ComparisonLinkage.NEITHER


def require_preimage_match(
    record: ReconciliationRecord, decision: StrategicDecisionRecord
) -> JudgeableCandidate | None:
    """Prove the reconciliation is about this exact decision revision.

    Returns the executed candidate when the reconciliation reports one, and
    ``None`` when it reports no execution. Raises
    :class:`~latent_compass.errors.PreimageMismatch` on any divergence — a wrong
    binding, decision id, pre-action revision, record seal or projection seal, and
    an executed direction that is not a candidate of *this* projection, or a
    reconciliation or observation timestamp before the pre-action capture.

    This never mutates ``decision``, and it never writes anything anywhere.
    """
    preimage = record.preimage
    for field, expected, received in (
        (
            "decision_binding",
            decision.binding.canonical_payload(),
            preimage.decision_binding.canonical_payload(),
        ),
        ("decision_id", decision.decision_id, preimage.decision_id),
        ("decision_revision", decision.revision, preimage.decision_revision),
        ("record_seal", decision.record_seal(), preimage.record_seal),
        ("projection_seal", decision.projection.projection_seal(), preimage.projection_seal),
    ):
        if expected != received:
            raise PreimageMismatch(
                f"reconciliation preimage {field} does not match the supplied decision record",
                detail={
                    "field": field,
                    "decision": expected,
                    "reconciliation": received,
                    "reconciliation_id": record.reconciliation_id,
                },
            )
    timestamps = [("reconciled_at", record.reconciled_at)]
    timestamps.extend(
        (f"{observation.dimension.value}.observed_at", observation.provenance.observed_at)
        for observation in record.observations
        if observation.provenance is not None
    )
    for field, timestamp in timestamps:
        if timestamp < decision.captured_at:
            raise PreimageMismatch(
                f"reconciliation {field} predates the pre-action capture",
                detail={
                    "field": field,
                    "captured_at": decision.captured_at,
                    "received": timestamp,
                    "reconciliation_id": record.reconciliation_id,
                },
            )
    if record.executed_direction_id is None:
        return None
    for candidate in decision.projection.candidates:
        if candidate.direction_id == record.executed_direction_id:
            return candidate
    raise PreimageMismatch(
        "the executed direction is not a candidate of the referenced preimage",
        detail={
            "reason": "executed_direction_not_a_candidate",
            "reconciliation_id": record.reconciliation_id,
            "executed_direction_id": record.executed_direction_id,
            "candidate_direction_ids": sorted(
                candidate.direction_id for candidate in decision.projection.candidates
            ),
        },
    )


def replay_reconciliation(
    record: ReconciliationRecord, decision: StrategicDecisionRecord
) -> ReconciliationReplay:
    """Compare one reconciliation against its exact preimage, dimension by dimension.

    Deterministic and pure. Produces no score, no ranking, no agreement verdict
    and no causal claim, and it modifies neither argument.
    """
    executed = require_preimage_match(record, decision)
    comparisons: tuple[DimensionComparison, ...] = ()
    if executed is not None:
        evidence = {item.dimension: item for item in executed.evidence}
        observations = {item.dimension: item for item in record.observations}
        comparisons = tuple(
            _compare(evidence[dimension], observations[dimension]) for dimension in DIMENSION_ORDER
        )
    body: dict[str, object] = {
        "contract_version": RECONCILIATION_REPLAY_CONTRACT_VERSION,
        "reconciliation_id": record.reconciliation_id,
        "revision": record.revision,
        "reconciliation_record_seal": record.record_seal(),
        "preimage": record.preimage.canonical_payload(),
        "authorization_state": record.authorization_state.value,
        "execution_state": record.execution_state.value,
        "executed_direction_id": record.executed_direction_id,
        "comparisons": [comparison.canonical_payload() for comparison in comparisons],
    }
    return validate_contract(
        ReconciliationReplay,
        {**body, "replay_seal": seal(REPLAY_SEAL_DOMAIN, body)},
        error=ReconciliationViolation,
        context="reconciliation replay",
    )


def _compare(
    evidence: DimensionEvidence,
    observation: DimensionObservation,
) -> DimensionComparison:
    """Place one pre-action evidence entry beside one observation, verbatim.

    The facts are carried across by reference, never rebuilt or summarised, so
    what a reader sees on the pre-action side is exactly what was sealed there.
    """
    return DimensionComparison(
        dimension=observation.dimension,
        pre_action_availability=evidence.availability,
        pre_action_facts=evidence.facts,
        pre_action_reason=evidence.reason,
        observation=observation,
        linkage=_linkage(
            pre_action_present=evidence.availability is EvidenceAvailability.PRESENT,
            observed=observation.status is ObservationStatus.OBSERVED,
        ),
    )

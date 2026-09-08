"""HOK-244 — the post-action decision reconciliation contracts.

A reconciliation is **observation about a decision, never a revision of it**. It
names a HOK-243 :class:`~latent_compass.decision_memory.StrategicDecisionRecord`
by its exact seal and writes nothing back to it: the pre-action record stays
byte-identical, keeps its own seal, and never learns that it was reconciled.

What a reconciliation must carry
--------------------------------
* a stable reconciliation identity and a monotonic revision number;
* the seal of the exact revision it supersedes, or nothing at all for the first;
* the journal binding it belongs to — host, agent family, store and epoch;
* the exact pre-action preimage: the decision's ``DecisionBinding``, its
  ``decision_id``, the pre-action revision, that revision's ``record_seal`` and
  the embedded projection's ``projection_seal``;
* reconciliation time, a named reconciling party, an explicit ``NON_SENSITIVE``
  classification;
* an explicit authorization state and an explicit execution state, kept apart
  from every observation;
* exactly the five observation dimensions when — and only when — something was
  executed, each either ``OBSERVED`` with full provenance or ``UNKNOWN`` with a
  named reason.

Three separations this contract exists to hold
----------------------------------------------
**Authorization is not execution.** ``AuthorizationState`` records what an
authority *outside this package* is asserted to have done, and its most
permissive value is named ``AUTHORIZED_ELSEWHERE`` precisely so it can never be
misread as a grant issued here. ``EXECUTED`` alongside ``NOT_AUTHORIZED`` is a
legal, recordable combination — that pairing is exactly the fact an audit needs
and a schema that made it inexpressible would be hiding it.

**Execution is not observation.** Observations exist only for the one candidate
that was executed. When nothing was executed, or when execution is unknown, the
observation tuple is *empty* — there is no field in which to put an outcome for a
road not taken, so a counterfactual cannot be written down even by mistake.

**Observed is not unknown.** A value exists only when the dimension is
``OBSERVED``, and then it carries a source id, a source digest, an observation
instant, a producer and a stated confidence. ``UNKNOWN`` carries no value at all
and must name which of ``ABSENT``, ``LATE``, ``AMBIGUOUS`` or ``DISPUTED`` it is.
An unknown is a first-class recorded fact here, never an absence to be filled in.

What it structurally cannot carry
---------------------------------
No selected route, rank, score, reward, preference, winner, verdict, label,
promotion, causal effect, counterfactual, holdout metadata or execution
authorization grant. ``extra="forbid"`` makes each a loud refusal, and
:mod:`latent_compass.decision_reconciliation.admission` refuses the names again
on the raw payload, at any depth, before any durable write.

Naming a reconciling party is an accountability label. It grants nothing, and
reading a reconciliation grants nothing either.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    RECONCILIATION_CONTRACT_VERSION,
    SUPPORTED_RECONCILIATION_VERSIONS,
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    UnitInterval,
    check_contract_version,
)
from latent_compass.decision_memory.contracts import (
    MAX_REVISION,
    DecisionBinding,
    SensitivityClassification,
)
from latent_compass.episode import AgentFamily
from latent_compass.pairwise_capture import DIMENSION_ORDER, JudgmentDimension, TypedScalar

__all__ = [
    "MAX_RECONCILIATION_REVISION",
    "AuthorizationState",
    "DecisionPreimageReference",
    "DimensionObservation",
    "ExecutionState",
    "ObservationProvenance",
    "ObservationStatus",
    "ObservationText",
    "ReconciliationBinding",
    "ReconciliationRecord",
    "ReconciliationRevisionKind",
    "UnknownReason",
    "build_reconciliation_payload",
    "preimage_reference_seal",
]

RECONCILIATION_SEAL_DOMAIN: Final = "decision-reconciliation.record.v1"
PREIMAGE_REFERENCE_SEAL_DOMAIN: Final = "decision-reconciliation.preimage-reference.v1"

#: A reconciliation corrected four thousand times is a process failure, not an
#: observation. The bound exists so an unbounded revision counter cannot be used
#: to grow a journal without limit.
MAX_RECONCILIATION_REVISION: Final = 4096


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


#: Bounded to the same width as a HOK-234 ``SummaryText``, so a pre-action fact
#: and the observation placed beside it in a replay comparison cannot differ in
#: what they are allowed to say.
ObservationText = Annotated[
    str,
    Field(min_length=1, max_length=1000),
    AfterValidator(_no_control_characters),
]

ReasonText = Annotated[
    str,
    Field(min_length=1, max_length=500),
    AfterValidator(_no_control_characters),
]


class AuthorizationState(StrEnum):
    """What an authority **outside this package** is asserted to have done.

    ``AUTHORIZED_ELSEWHERE`` is named for what it is: a record that some other,
    named authority authorised the action. This package issues no authorisation
    and this field grants none. ``AUTHORIZATION_UNKNOWN`` is a first-class value,
    so "nobody knows whether this was authorised" is something the journal can
    *say* rather than something expressed by omission.
    """

    AUTHORIZED_ELSEWHERE = "AUTHORIZED_ELSEWHERE"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    AUTHORIZATION_UNKNOWN = "AUTHORIZATION_UNKNOWN"


class ExecutionState(StrEnum):
    """Whether one candidate of the preimage was actually executed.

    Held apart from :class:`AuthorizationState` on purpose: an execution without
    authorisation and an authorisation without execution are both real events,
    and collapsing the two axes would make either unrecordable.
    """

    EXECUTED = "EXECUTED"
    NOT_EXECUTED = "NOT_EXECUTED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"


class ObservationStatus(StrEnum):
    """Whether this dimension was observed at all. Never a score."""

    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


class UnknownReason(StrEnum):
    """Why a dimension was not observed. Closed set, no fallback member.

    ``ABSENT`` — no observation exists. ``LATE`` — one is expected but has not
    arrived. ``AMBIGUOUS`` — material exists and does not determine a value.
    ``DISPUTED`` — observers disagree. None of the four may ever acquire a value:
    each is the recorded fact itself, not a placeholder for one.
    """

    ABSENT = "ABSENT"
    LATE = "LATE"
    AMBIGUOUS = "AMBIGUOUS"
    DISPUTED = "DISPUTED"


class ReconciliationRevisionKind(StrEnum):
    """Why this revision exists. A disagreement appends; it never overwrites."""

    INITIAL = "INITIAL"
    CORRECTION = "CORRECTION"
    DISAGREEMENT = "DISAGREEMENT"


class ReconciliationBinding(StrictModel):
    """The journal identity a reconciliation belongs to. Immutable once written.

    A distinct type from :class:`~latent_compass.decision_memory.DecisionBinding`
    even though the four fields coincide: a reconciliation carries both, they mean
    different stores, and a type that let one be passed for the other would make
    a cross-store relabelling a typing accident rather than a refusal.
    """

    host_id: Identifier
    agent_family: AgentFamily
    store_id: Identifier
    epoch: Identifier


class DecisionPreimageReference(StrictModel):
    """The exact pre-action revision this reconciliation is about.

    Every field is part of the identity. Naming the ``record_seal`` alone would
    let a reconciliation float between stores; naming the ``projection_seal`` too
    is what makes "the executed direction was one of *these* candidates" a
    checkable claim rather than an assertion.
    """

    decision_binding: DecisionBinding
    decision_id: Identifier
    decision_revision: int = Field(ge=1, le=MAX_REVISION)
    record_seal: Seal
    projection_seal: Seal


def preimage_reference_seal(preimage: DecisionPreimageReference) -> str:
    """Bind every field that makes a pre-action revision one exact preimage."""
    return seal(PREIMAGE_REFERENCE_SEAL_DOMAIN, preimage.canonical_payload())


class ObservationProvenance(StrictModel):
    """Where one observed value came from, and how sure its producer is.

    ``confidence`` is the producer's stated confidence *in this observation*. It
    is not a calibrated probability, not a quality score, and never a comparison
    between candidates — there is only ever one candidate here to speak about.
    """

    source_id: Identifier
    source_digest: Seal
    observed_at: Timestamp
    producer: Identifier
    confidence: UnitInterval


class DimensionObservation(StrictModel):
    """One of the five dimensions, either observed with provenance or not at all."""

    dimension: JudgmentDimension
    status: ObservationStatus
    statement: ObservationText | None = Field(default=None)
    value: TypedScalar | None = Field(default=None)
    provenance: ObservationProvenance | None = Field(default=None)
    unknown_reason: UnknownReason | None = Field(default=None)
    unknown_detail: ObservationText | None = Field(default=None)

    @model_validator(mode="after")
    def _status_matches_content(self) -> Self:
        observed = (self.statement, self.value, self.provenance)
        unknown = (self.unknown_reason, self.unknown_detail)
        if self.status is ObservationStatus.OBSERVED:
            if any(item is None for item in observed):
                raise ValueError(
                    "an OBSERVED dimension requires a statement, a typed value and provenance"
                )
            if any(item is not None for item in unknown):
                raise ValueError("an OBSERVED dimension must not name an unknown reason")
            return self
        if any(item is None for item in unknown):
            raise ValueError(
                "an UNKNOWN dimension must name one of ABSENT, LATE, AMBIGUOUS or DISPUTED "
                "and say why"
            )
        if any(item is not None for item in observed):
            raise ValueError(
                "an UNKNOWN dimension carries no value, no statement and no provenance: "
                "an unknown is the recorded fact, not a placeholder for one"
            )
        return self


class ReconciliationRecord(StrictModel):
    """One revision of one reconciliation. Observation, never a decision."""

    contract_version: str = Field(min_length=5, max_length=20)
    reconciliation_id: Identifier
    revision: int = Field(ge=1, le=MAX_RECONCILIATION_REVISION)
    revision_kind: ReconciliationRevisionKind
    revision_reason: ReasonText | None = Field(default=None)
    supersedes_revision_seal: Seal | None = Field(default=None)
    binding: ReconciliationBinding
    preimage: DecisionPreimageReference
    reconciled_at: Timestamp
    reconciled_by: Identifier
    sensitivity: SensitivityClassification
    authorization_state: AuthorizationState
    authorization_reference: Identifier | None = Field(default=None)
    execution_state: ExecutionState
    executed_direction_id: Identifier | None = Field(default=None)
    observations: tuple[DimensionObservation, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def _reconciliation_is_admissible_and_coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_RECONCILIATION_VERSIONS,
            "reconciliation record",
        )
        if self.sensitivity is not SensitivityClassification.NON_SENSITIVE:
            raise ValueError("a reconciliation requires an explicit NON_SENSITIVE classification")
        self._check_revision_shape()
        self._check_authorization_shape()
        self._check_execution_and_observations()
        return self

    def _check_revision_shape(self) -> None:
        initial = self.revision == 1
        if initial != (self.revision_kind is ReconciliationRevisionKind.INITIAL):
            raise ValueError(
                "revision 1 is the INITIAL reconciliation and every later revision is a "
                "CORRECTION or a DISAGREEMENT"
            )
        if initial:
            if self.supersedes_revision_seal is not None:
                raise ValueError("an initial revision supersedes nothing and must not name a seal")
            if self.revision_reason is not None:
                raise ValueError("an initial revision corrects nothing and states no reason")
            return
        if self.supersedes_revision_seal is None:
            raise ValueError("a revision above 1 must name the seal of the revision it supersedes")
        if self.revision_reason is None:
            raise ValueError(
                "a correction or a disagreement must say why it was appended: "
                "an unexplained overwrite is what this journal exists to prevent"
            )

    def _check_authorization_shape(self) -> None:
        authorized = self.authorization_state is AuthorizationState.AUTHORIZED_ELSEWHERE
        if authorized and self.authorization_reference is None:
            raise ValueError(
                "AUTHORIZED_ELSEWHERE must name the external authority it defers to; "
                "this package issues no authorisation of its own"
            )
        if not authorized and self.authorization_reference is not None:
            raise ValueError("only AUTHORIZED_ELSEWHERE names an external authority reference")

    def _check_execution_and_observations(self) -> None:
        executed = self.execution_state is ExecutionState.EXECUTED
        if executed:
            if self.executed_direction_id is None:
                raise ValueError("EXECUTED must name the one direction that was executed")
            if tuple(item.dimension for item in self.observations) != DIMENSION_ORDER:
                raise ValueError(
                    "an executed reconciliation carries every one of the five dimensions "
                    "exactly once, in canonical order"
                )
        else:
            if self.executed_direction_id is not None:
                raise ValueError(
                    "only EXECUTED names a direction: naming one here would assert an "
                    "execution the state denies"
                )
            if self.observations:
                raise ValueError(
                    "there is no observation for an unexecuted candidate; a counterfactual "
                    "outcome is not recordable here"
                )
        for observation in self.observations:
            provenance = observation.provenance
            if provenance is not None and provenance.observed_at > self.reconciled_at:
                # Lexicographic ordering is chronological for the one accepted
                # timestamp encoding, so no parsing and no timezone guessing.
                raise ValueError(
                    f"{observation.dimension.value} was observed after this reconciliation "
                    "was written; an observation cannot postdate the record that reports it"
                )

    def record_seal(self) -> str:
        """Reproducible seal over the whole reconciliation, in its own domain."""
        return seal(RECONCILIATION_SEAL_DOMAIN, self.canonical_payload())


def build_reconciliation_payload(
    *,
    reconciliation_id: str,
    revision: int,
    revision_kind: ReconciliationRevisionKind,
    binding: ReconciliationBinding,
    preimage: DecisionPreimageReference,
    reconciled_at: str,
    reconciled_by: str,
    authorization_state: AuthorizationState,
    execution_state: ExecutionState,
    observations: tuple[DimensionObservation, ...] = (),
    executed_direction_id: str | None = None,
    authorization_reference: str | None = None,
    revision_reason: str | None = None,
    supersedes_revision_seal: str | None = None,
) -> dict[str, object]:
    """Assemble a raw reconciliation payload for the admission path.

    Returns plain JSON-shaped data on purpose: the only supported way to obtain a
    :class:`ReconciliationRecord` is to put a payload through
    :func:`~latent_compass.decision_reconciliation.admission.admit_reconciliation`,
    so no construction path can skip admission.
    """
    return {
        "contract_version": RECONCILIATION_CONTRACT_VERSION,
        "reconciliation_id": reconciliation_id,
        "revision": revision,
        "revision_kind": revision_kind.value,
        "revision_reason": revision_reason,
        "supersedes_revision_seal": supersedes_revision_seal,
        "binding": binding.canonical_payload(),
        "preimage": preimage.canonical_payload(),
        "reconciled_at": reconciled_at,
        "reconciled_by": reconciled_by,
        "sensitivity": SensitivityClassification.NON_SENSITIVE.value,
        "authorization_state": authorization_state.value,
        "authorization_reference": authorization_reference,
        "execution_state": execution_state.value,
        "executed_direction_id": executed_direction_id,
        "observations": [observation.canonical_payload() for observation in observations],
    }

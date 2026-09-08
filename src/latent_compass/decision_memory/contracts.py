"""HOK-243 — the pre-action strategic decision memory contracts.

A record here is **memory, never mandate**. It says "this strategic decision was
taken, by this named authority, over this judgeable candidate set, and it must be
reviewed by this date". It cannot say which candidate to take, whether the
decision worked, or that anything may now be executed.

What a native record must carry
-------------------------------
* a stable decision identity and a monotonic revision number;
* the seal of the exact revision it supersedes, or nothing at all for the first;
* the store binding it belongs to — host, agent family, store and epoch;
* capture time, a named decision authority, a review deadline and an expiry;
* an explicit ``STRATEGIC_HIGH_IMPACT`` opt-in;
* an explicit ``NON_SENSITIVE`` classification;
* a complete HOK-234 judgeable projection over 2..256 canonical candidates, each
  carrying exactly the five evidence dimensions.

What it structurally cannot carry
---------------------------------
No selected route, outcome, label, score, verdict, holdout metadata or execution
authorization. ``extra="forbid"`` makes each of those a loud refusal rather than
an ignored key, and :mod:`latent_compass.decision_memory.admission` refuses the
names again on the raw payload, before validation and before any durable write.

The authority a record names is an **accountability label**, not a grant. Naming
a decision authority confers no capability on anyone, least of all on this
package.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    DECISION_MEMORY_CONTRACT_VERSION,
    DECISION_TRANSFER_CONTRACT_VERSION,
    SUPPORTED_DECISION_MEMORY_VERSIONS,
    SUPPORTED_DECISION_TRANSFER_VERSIONS,
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import AgentFamily
from latent_compass.errors import TransferRefused
from latent_compass.governance import DeletionMode
from latent_compass.pairwise_capture import JudgeableDecisionProjection

__all__ = [
    "MAX_REVISION",
    "ActiveDecision",
    "DecisionBinding",
    "DecisionImpactClass",
    "DecisionOriginKind",
    "DecisionTombstone",
    "DecisionTransferEnvelope",
    "EventKind",
    "ReasonText",
    "RevocationEvent",
    "SensitivityClassification",
    "StrategicDecisionRecord",
    "build_record_payload",
    "build_transfer_envelope",
    "transfer_envelope_seal",
]

RECORD_SEAL_DOMAIN: Final = "decision-memory.record.v1"
REVOCATION_SEAL_DOMAIN: Final = "decision-memory.revocation.v1"
TOMBSTONE_SEAL_DOMAIN: Final = "decision-memory.tombstone.v1"
TRANSFER_SEAL_DOMAIN: Final = "decision-memory.transfer.v1"

#: A decision that has been revised four thousand times is a process failure,
#: not a decision. The bound is here so an unbounded revision counter cannot be
#: used to grow a store without limit.
MAX_REVISION: Final = 4096


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


ReasonText = Annotated[
    str,
    Field(min_length=1, max_length=500),
    AfterValidator(_no_control_characters),
]


class DecisionImpactClass(StrEnum):
    """Why this decision is worth remembering. Only one class is admissible.

    ``ROUTINE`` exists so that "not strategic" is something a caller can *say*
    and this store can *refuse*, rather than something expressed by omission.
    """

    STRATEGIC_HIGH_IMPACT = "STRATEGIC_HIGH_IMPACT"
    ROUTINE = "ROUTINE"


class SensitivityClassification(StrEnum):
    """The caller's explicit statement about the content being admitted.

    ``UNKNOWN`` is a first-class value and is refused. An unclassified payload
    must fail loudly; it must never be read as "presumably fine".
    """

    NON_SENSITIVE = "NON_SENSITIVE"
    UNKNOWN = "UNKNOWN"
    HOLDOUT = "HOLDOUT"


class DecisionOriginKind(StrEnum):
    """Whether a stored decision was written here or imported from elsewhere."""

    NATIVE = "NATIVE"
    FOREIGN_READ_ONLY = "FOREIGN_READ_ONLY"


class EventKind(StrEnum):
    """The closed set of durable events a decision memory chain can hold."""

    RECORD = "RECORD"
    REVOCATION = "REVOCATION"
    TOMBSTONE = "TOMBSTONE"


class DecisionBinding(StrictModel):
    """The store identity a record belongs to. Immutable once written."""

    host_id: Identifier
    agent_family: AgentFamily
    store_id: Identifier
    epoch: Identifier


class StrategicDecisionRecord(StrictModel):
    """One revision of one opt-in strategic decision. Memory, not mandate."""

    contract_version: str = Field(min_length=5, max_length=20)
    decision_id: Identifier
    revision: int = Field(ge=1, le=MAX_REVISION)
    supersedes_revision_seal: Seal | None = Field(default=None)
    binding: DecisionBinding
    captured_at: Timestamp
    decision_authority: Identifier
    review_due_at: Timestamp
    expires_at: Timestamp
    impact_class: DecisionImpactClass
    sensitivity: SensitivityClassification
    projection: JudgeableDecisionProjection

    @model_validator(mode="after")
    def _record_is_admissible_and_coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_DECISION_MEMORY_VERSIONS,
            "strategic decision record",
        )
        if self.impact_class is not DecisionImpactClass.STRATEGIC_HIGH_IMPACT:
            raise ValueError(
                "decision memory is opt-in: only STRATEGIC_HIGH_IMPACT records are admissible"
            )
        if self.sensitivity is not SensitivityClassification.NON_SENSITIVE:
            raise ValueError("decision memory requires an explicit NON_SENSITIVE classification")
        if self.revision == 1:
            if self.supersedes_revision_seal is not None:
                raise ValueError("an initial revision supersedes nothing and must not name a seal")
        elif self.supersedes_revision_seal is None:
            raise ValueError("a revision above 1 must name the seal of the revision it supersedes")
        # Lexicographic ordering is chronological for the one accepted timestamp
        # encoding, so no parsing — and no timezone guessing — happens here.
        if self.review_due_at <= self.captured_at:
            raise ValueError("the review deadline must fall after capture time")
        if self.expires_at < self.review_due_at:
            raise ValueError("the retention deadline must not fall before the review deadline")
        if self.projection.decision_point_id != self.decision_id:
            raise ValueError(
                "the embedded projection must describe this decision: "
                f"{self.projection.decision_point_id!r} != {self.decision_id!r}"
            )
        return self

    def record_seal(self) -> str:
        """Reproducible seal over the whole record, in its own domain."""
        return seal(RECORD_SEAL_DOMAIN, self.canonical_payload())


class RevocationEvent(StrictModel):
    """The durable statement that a decision no longer holds.

    Revocation adds; it never removes. The revised history stays readable and
    replayable, and the decision simply stops appearing in the active view.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    decision_id: Identifier
    revoked_revision: int = Field(ge=1, le=MAX_REVISION)
    revoked_revision_seal: Seal
    revoked_by: Identifier
    reason: ReasonText
    revoked_at: Timestamp

    @model_validator(mode="after")
    def _revocation_version(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_DECISION_MEMORY_VERSIONS, "decision revocation"
        )
        return self

    def event_seal(self) -> str:
        return seal(REVOCATION_SEAL_DOMAIN, self.canonical_payload())


class DecisionTombstone(StrictModel):
    """The durable record that one revision's payload was deliberately removed.

    The row, its position, its content seal and the chain link over it all
    survive, so the remaining history is not silently rewritten. What is lost is
    the ability to read that revision back.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    decision_id: Identifier
    revision: int = Field(ge=1, le=MAX_REVISION)
    redacted_record_seal: Seal
    reason: ReasonText
    created_at: Timestamp
    mode: DeletionMode = Field(default=DeletionMode.TOMBSTONE)

    @model_validator(mode="after")
    def _tombstone_version_and_mode(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_DECISION_MEMORY_VERSIONS, "decision tombstone"
        )
        if self.mode is not DeletionMode.TOMBSTONE:
            raise ValueError("a decision tombstone record must declare TOMBSTONE mode")
        return self

    def event_seal(self) -> str:
        return seal(TOMBSTONE_SEAL_DOMAIN, self.canonical_payload())


def transfer_envelope_seal(body: dict[str, object]) -> str:
    """Seal the transfer envelope body — every field except the seal itself."""
    return seal(TRANSFER_SEAL_DOMAIN, body)


class DecisionTransferEnvelope(StrictModel):
    """An explicit, versioned, one-record transfer between two bound stores.

    Codex and Claude stores are physically separate and are never synchronised.
    The only way content crosses is this envelope, produced deliberately for one
    named destination and refused everywhere else.

    **What the seal proves.** That these bytes are internally consistent with the
    source record seal and source root seal they name, on a host running this
    version. It does **not** prove who issued the envelope, that the issuer held
    any authority, or when the envelope was made: the timestamp inside is
    asserted by the producer, not witnessed. Establishing authenticity or
    chronology needs a co-signature or an external witness this package does not
    have, and that limit is intrinsic to the threat model.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    source_binding: DecisionBinding
    source_root_seal: Seal
    source_record_seal: Seal
    destination_binding: DecisionBinding
    exported_at: Timestamp
    exported_by: Identifier
    record: StrategicDecisionRecord
    transfer_seal: Seal

    def sealed_body(self) -> dict[str, object]:
        """The canonical payload minus the seal, which is what the seal covers."""
        body = self.canonical_payload()
        body.pop("transfer_seal", None)
        return body

    @model_validator(mode="after")
    def _envelope_is_bound_and_sealed(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_DECISION_TRANSFER_VERSIONS,
            "decision transfer envelope",
        )
        if self.record.binding != self.source_binding:
            raise ValueError("the carried record does not belong to the declared source binding")
        if self.record.record_seal() != self.source_record_seal:
            raise ValueError("source_record_seal does not reproduce from the carried record")
        if self.destination_binding == self.source_binding:
            raise ValueError(
                "a transfer must cross a store boundary; "
                "source and destination bindings are identical"
            )
        if transfer_envelope_seal(self.sealed_body()) != self.transfer_seal:
            raise ValueError("transfer_seal does not reproduce from the envelope body")
        return self


class ActiveDecision(StrictModel):
    """The head revision of one decision, as of an explicit instant.

    ``record`` is ``None`` exactly when the head revision has been tombstoned:
    the decision is still part of the active state, and the entry says plainly
    that its payload was removed rather than pretending it never existed. The
    deadlines go with the payload — reporting deadlines for a revision whose
    content no longer exists would be inventing them.
    """

    decision_id: Identifier
    revision: int = Field(ge=1, le=MAX_REVISION)
    origin_kind: DecisionOriginKind
    record_seal: Seal
    redacted: bool
    captured_at: Timestamp | None = Field(default=None)
    review_due_at: Timestamp | None = Field(default=None)
    expires_at: Timestamp | None = Field(default=None)
    review_overdue: bool | None = Field(default=None)
    record: StrategicDecisionRecord | None = Field(default=None)

    @model_validator(mode="after")
    def _redaction_and_content_agree(self) -> Self:
        carried = (self.record, self.captured_at, self.review_due_at, self.expires_at)
        if self.redacted:
            if any(value is not None for value in carried) or self.review_overdue is not None:
                raise ValueError("a redacted head carries no record and no deadlines")
        elif any(value is None for value in carried) or self.review_overdue is None:
            raise ValueError("an unredacted head must carry its record and every deadline")
        return self


def build_record_payload(
    *,
    decision_id: str,
    revision: int,
    binding: DecisionBinding,
    captured_at: str,
    decision_authority: str,
    review_due_at: str,
    expires_at: str,
    projection: JudgeableDecisionProjection,
    supersedes_revision_seal: str | None = None,
) -> dict[str, object]:
    """Assemble a raw record payload for the admission path.

    Returns plain JSON-shaped data on purpose: the only supported way to obtain
    a :class:`StrategicDecisionRecord` is to put a payload through
    :func:`~latent_compass.decision_memory.admission.admit_strategic_decision`,
    so no construction path can skip admission.
    """
    return {
        "contract_version": DECISION_MEMORY_CONTRACT_VERSION,
        "decision_id": decision_id,
        "revision": revision,
        "supersedes_revision_seal": supersedes_revision_seal,
        "binding": binding.canonical_payload(),
        "captured_at": captured_at,
        "decision_authority": decision_authority,
        "review_due_at": review_due_at,
        "expires_at": expires_at,
        "impact_class": DecisionImpactClass.STRATEGIC_HIGH_IMPACT.value,
        "sensitivity": SensitivityClassification.NON_SENSITIVE.value,
        "projection": projection.canonical_payload(),
    }


def build_transfer_envelope(
    *,
    record: StrategicDecisionRecord,
    source_root_seal: str,
    destination_binding: DecisionBinding,
    exported_at: str,
    exported_by: str,
) -> DecisionTransferEnvelope:
    """Seal one record for one named destination, and nothing else."""
    body: dict[str, object] = {
        "contract_version": DECISION_TRANSFER_CONTRACT_VERSION,
        "source_binding": record.binding.canonical_payload(),
        "source_root_seal": source_root_seal,
        "source_record_seal": record.record_seal(),
        "destination_binding": destination_binding.canonical_payload(),
        "exported_at": exported_at,
        "exported_by": exported_by,
        "record": record.canonical_payload(),
    }
    return validate_contract(
        DecisionTransferEnvelope,
        {**body, "transfer_seal": transfer_envelope_seal(body)},
        error=TransferRefused,
        context="decision transfer envelope",
    )

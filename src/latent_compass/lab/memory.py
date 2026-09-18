"""HOK-798 — the justification memory, per ADR 0011.

Separate from the finite decision model and from every legacy
``latent_compass`` store: this memory records *why* a claim might be
applicable, not what to decide. It is append-only, replayed fresh on every
query, and bound to one host/family/source scope — the same binding shape as
:class:`~latent_compass.lab.state.LabBinding`, reused here under the name
``MemoryBinding``.

A **fact** is a root premise: provenance, scope, an assertion time and an
optional expiry, never derived from anything. A **claim** is derived: its
``supports`` are a disjunction of conjunctions — at least one conjunction must
have every named premise (a fact id or another claim id) *grounded* for the
claim itself to be grounded. Grounding is computed as a least fixed point
starting from eligible facts: a claim that only ever cites itself, directly or
through a cycle with no independent root, is never added, no matter how many
times the fixpoint iterates. Revocation, expiry and a superseding support
revision are all durable appends; nothing here is edited in place, and
applicability is recomputed from the full history on every call — never
cached, never trusted from a prior read.

Applicability is not a truth verdict. It states that a claim's supports
resolve to grounded premises under this history as of this instant; it says
nothing about whether the claim is actually true.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AfterValidator, Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.lab.contracts import (
    MAX_MEMORY_CLAIMS,
    MAX_MEMORY_EVENTS,
    MAX_MEMORY_FACTS,
    MAX_PREMISES_PER_CONJUNCTION,
    MAX_SUPPORTS_PER_CLAIM,
    SUPPORTED_LAB_MEMORY_VERSIONS,
)
from latent_compass.lab.errors import LabMemoryViolationError
from latent_compass.lab.state import LabBinding

__all__ = [
    "MEMORY_EVENT_SEAL_DOMAIN",
    "ClaimAssertion",
    "ClaimRevocation",
    "FactAssertion",
    "FactRevocation",
    "MemoryApplicability",
    "MemoryBinding",
    "MemoryEvent",
    "MemoryEventKind",
    "SupportConjunction",
    "SupportRevision",
    "compute_applicability",
    "load_memory_event",
]

MEMORY_EVENT_SEAL_DOMAIN: Final = "lab.memory.event.v1"

#: Same shape as the diagnosis episode binding, reused: both bind one
#: host/family/source scope, and ADR 0011 draws no distinction between them.
MemoryBinding = LabBinding


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


ReasonText = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_no_control_characters)
]


class MemoryEventKind(StrEnum):
    """The closed set of durable events a justification memory can hold."""

    FACT = "FACT"
    FACT_REVOCATION = "FACT_REVOCATION"
    CLAIM = "CLAIM"
    CLAIM_REVOCATION = "CLAIM_REVOCATION"
    SUPPORT_REVISION = "SUPPORT_REVISION"


class FactAssertion(StrictModel):
    """One root premise. Never derived; grounded by definition when eligible."""

    contract_version: str = Field(min_length=5, max_length=20)
    fact_id: Identifier
    statement: ReasonText
    provenance: Identifier
    scope: Identifier
    asserted_at: Timestamp
    expires_at: Timestamp | None = Field(default=None)

    @model_validator(mode="after")
    def _fact_version_and_ordering(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory fact")
        if self.expires_at is not None and self.expires_at <= self.asserted_at:
            raise ValueError("a fact's expiry must fall strictly after its assertion")
        return self


class FactRevocation(StrictModel):
    """The durable statement that a fact no longer holds. Adds; never removes."""

    contract_version: str = Field(min_length=5, max_length=20)
    fact_id: Identifier
    reason: ReasonText
    revoked_at: Timestamp

    @model_validator(mode="after")
    def _revocation_version(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory fact revocation"
        )
        return self


class SupportConjunction(StrictModel):
    """One AND-clause: every named premise id must be grounded for this to hold."""

    premise_ids: tuple[Identifier, ...] = Field(
        min_length=1, max_length=MAX_PREMISES_PER_CONJUNCTION
    )

    @model_validator(mode="after")
    def _premises_are_unique(self) -> Self:
        if len(set(self.premise_ids)) != len(self.premise_ids):
            raise ValueError("a support conjunction names the same premise more than once")
        return self


class ClaimAssertion(StrictModel):
    """One derived claim. Grounded applicability is computed, never asserted."""

    contract_version: str = Field(min_length=5, max_length=20)
    claim_id: Identifier
    statement: ReasonText
    scope: Identifier
    asserted_at: Timestamp
    expires_at: Timestamp | None = Field(default=None)
    supports: tuple[SupportConjunction, ...] = Field(
        min_length=1, max_length=MAX_SUPPORTS_PER_CLAIM
    )

    @model_validator(mode="after")
    def _claim_version_and_ordering(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory claim")
        if self.expires_at is not None and self.expires_at <= self.asserted_at:
            raise ValueError("a claim's expiry must fall strictly after its assertion")
        return self


class ClaimRevocation(StrictModel):
    """The durable statement that a claim no longer holds. Adds; never removes."""

    contract_version: str = Field(min_length=5, max_length=20)
    claim_id: Identifier
    reason: ReasonText
    revoked_at: Timestamp

    @model_validator(mode="after")
    def _revocation_version(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory claim revocation"
        )
        return self


class SupportRevision(StrictModel):
    """Replaces a claim's supports. Names the exact event it extends; never edits in place."""

    contract_version: str = Field(min_length=5, max_length=20)
    claim_id: Identifier
    supersedes_seal: Seal
    supports: tuple[SupportConjunction, ...] = Field(
        min_length=1, max_length=MAX_SUPPORTS_PER_CLAIM
    )
    reason: ReasonText
    revised_at: Timestamp

    @model_validator(mode="after")
    def _revision_version(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory support revision"
        )
        return self


class MemoryEvent(StrictModel):
    """One envelope in one append-only justification memory. Carries exactly one payload."""

    contract_version: str = Field(min_length=5, max_length=20)
    binding: MemoryBinding
    sequence: int = Field(ge=0)
    recorded_at: Timestamp
    kind: MemoryEventKind
    fact: FactAssertion | None = Field(default=None)
    fact_revocation: FactRevocation | None = Field(default=None)
    claim: ClaimAssertion | None = Field(default=None)
    claim_revocation: ClaimRevocation | None = Field(default=None)
    support_revision: SupportRevision | None = Field(default=None)

    @model_validator(mode="after")
    def _event_carries_exactly_its_declared_kind(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory event")
        by_kind: dict[MemoryEventKind, object | None] = {
            MemoryEventKind.FACT: self.fact,
            MemoryEventKind.FACT_REVOCATION: self.fact_revocation,
            MemoryEventKind.CLAIM: self.claim,
            MemoryEventKind.CLAIM_REVOCATION: self.claim_revocation,
            MemoryEventKind.SUPPORT_REVISION: self.support_revision,
        }
        carried = [kind for kind, payload in by_kind.items() if payload is not None]
        if carried != [self.kind]:
            raise ValueError(
                f"a {self.kind.value} event must carry exactly its own payload and no other"
            )
        return self

    def event_seal(self) -> str:
        """Reproducible seal over the whole event, in its own domain."""
        return seal(MEMORY_EVENT_SEAL_DOMAIN, self.canonical_payload())


def load_memory_event(payload: object) -> MemoryEvent:
    """Validate a raw JSON-shaped payload as a :class:`MemoryEvent`."""
    if not isinstance(payload, dict):
        raise LabMemoryViolationError(
            "memory event must be a JSON object", detail={"received_type": type(payload).__name__}
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabMemoryViolationError(
            "memory event must declare contract_version", detail={"reason": "absent"}
        )
    check_contract_version(version, SUPPORTED_LAB_MEMORY_VERSIONS, "memory event")
    return validate_contract(
        MemoryEvent, payload, error=LabMemoryViolationError, context="memory event"
    )


#: Same canonical encoding ``Timestamp`` enforces on every other timestamp
#: field. ``as_of`` is a bare ``str`` parameter, not a ``Timestamp``-typed
#: pydantic field, so nothing validates it on the way in unless this module
#: does: without this, a caller could pass a different-offset or otherwise
#: non-canonical instant, and every comparison below would become a
#: lexicographic string comparison against genuinely canonical stored
#: timestamps rather than a comparison of actual instants.
_INSTANT_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parse_instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or _INSTANT_PATTERN.fullmatch(value) is None:
        raise LabMemoryViolationError(
            "timestamp must be exactly YYYY-MM-DDTHH:MM:SSZ, the same canonical encoding as "
            "every other timestamp in this history",
            detail={"field": field, "received": value},
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise LabMemoryViolationError(
            "timestamp is not a real UTC instant", detail={"field": field, "received": value}
        ) from exc


@dataclass(frozen=True)
class MemoryApplicability:
    """The result of replaying one justification memory's history to one instant.

    ``inactive_claim_ids`` are claims that are not revoked and not expired but
    are not grounded — an unsupported cycle lands here, distinct from a claim
    that was actively revoked or has expired.
    """

    active_fact_ids: frozenset[str]
    active_claim_ids: frozenset[str]
    revoked_fact_ids: frozenset[str]
    revoked_claim_ids: frozenset[str]
    inactive_claim_ids: frozenset[str]


def compute_applicability(events: Sequence[MemoryEvent], *, as_of: str) -> MemoryApplicability:
    """Replay ``events`` and compute grounded applicability as of ``as_of``.

    A pure function of the full history: nothing is cached between calls, and
    nothing here mutates ``events``. Every event is independently revalidated
    from its own canonical payload first (closing a caller-mutated nested
    field or a ``model_construct``-style bypass, neither of which a frozen
    pydantic model prevents on its own). The full history is then replayed
    and validated in order — sequencing, binding, a duplicate *or* cross-kind
    fact/claim id (facts and claims share one premise namespace: a claim
    citing its own id as a "fact" must never ground itself through an
    unrelated fact that happens to reuse that id), an unknown premise, and the
    support-revision seal chain are all checked over *every* event regardless
    of ``as_of`` — before the as-of view is derived from it.

    ``as_of`` must be the same canonical ``YYYY-MM-DDTHH:MM:SSZ`` encoding
    every other timestamp in this package uses; every comparison below is
    between real UTC instants, never a lexicographic string comparison. An
    event only contributes to the as-of view when it was itself both
    *recorded* (``recorded_at``) and *effective* (its own assertion,
    revocation or revision timestamp) at or before ``as_of``: a fact asserted
    on the 20th cannot be active on the 18th, and a revocation recorded or
    effective on the 20th cannot retroactively erase what was true on the
    18th — the same rule applies to a claim's supports as of a support
    revision. Recorded times must be non-decreasing in append order; a
    history whose declared chronology contradicts its own append order is
    refused rather than silently normalised.
    """
    as_of_instant = _parse_instant(as_of, field="as_of")
    if not events:
        return MemoryApplicability(frozenset(), frozenset(), frozenset(), frozenset(), frozenset())
    if len(events) > MAX_MEMORY_EVENTS:
        raise LabMemoryViolationError(
            "memory event history exceeds the declared bound",
            detail={"count": len(events), "max_memory_events": MAX_MEMORY_EVENTS},
        )
    events = tuple(load_memory_event(event.canonical_payload()) for event in events)

    binding = events[0].binding
    facts: dict[str, FactAssertion] = {}
    claim_meta: dict[str, ClaimAssertion] = {}
    support_history: list[tuple[str, tuple[SupportConjunction, ...]]] = []
    current_supports_seal: dict[str, str] = {}

    facts_as_of: dict[str, FactAssertion] = {}
    fact_revoked_as_of: set[str] = set()
    claim_meta_as_of: dict[str, ClaimAssertion] = {}
    claim_supports_as_of: dict[str, tuple[SupportConjunction, ...]] = {}
    claim_revoked_as_of: set[str] = set()

    previous_recorded_at: datetime | None = None
    for index, event in enumerate(events):
        if event.binding != binding:
            raise LabMemoryViolationError("memory event history's binding drifted mid-history")
        if event.sequence != index:
            raise LabMemoryViolationError(
                "memory event history is not sequential",
                detail={"expected_sequence": index, "found": event.sequence},
            )

        recorded_at = _parse_instant(event.recorded_at, field="recorded_at")
        if previous_recorded_at is not None and recorded_at < previous_recorded_at:
            raise LabMemoryViolationError(
                "memory event history's recorded_at is not chronologically consistent with its "
                "append order",
                detail={"sequence": event.sequence, "recorded_at": event.recorded_at},
            )
        previous_recorded_at = recorded_at
        qualifies_recorded = recorded_at <= as_of_instant

        if event.kind is MemoryEventKind.FACT:
            fact = event.fact
            assert fact is not None  # guaranteed by _event_carries_exactly_its_declared_kind
            if fact.fact_id in facts:
                raise LabMemoryViolationError(
                    "duplicate fact id in memory history", detail={"fact_id": fact.fact_id}
                )
            if fact.fact_id in claim_meta:
                raise LabMemoryViolationError(
                    "fact id collides with an existing claim id in the shared premise namespace",
                    detail={"id": fact.fact_id},
                )
            if len(facts) >= MAX_MEMORY_FACTS:
                raise LabMemoryViolationError("memory fact count exceeds the declared bound")
            facts[fact.fact_id] = fact
            asserted_at = _parse_instant(fact.asserted_at, field="asserted_at")
            if qualifies_recorded and asserted_at <= as_of_instant:
                facts_as_of[fact.fact_id] = fact
        elif event.kind is MemoryEventKind.FACT_REVOCATION:
            fact_revocation = event.fact_revocation
            assert fact_revocation is not None
            if fact_revocation.fact_id not in facts:
                raise LabMemoryViolationError(
                    "revocation names an unknown fact",
                    detail={"fact_id": fact_revocation.fact_id},
                )
            revoked_at = _parse_instant(fact_revocation.revoked_at, field="revoked_at")
            if qualifies_recorded and revoked_at <= as_of_instant:
                fact_revoked_as_of.add(fact_revocation.fact_id)
        elif event.kind is MemoryEventKind.CLAIM:
            claim = event.claim
            assert claim is not None
            if claim.claim_id in claim_meta:
                raise LabMemoryViolationError(
                    "duplicate claim id in memory history", detail={"claim_id": claim.claim_id}
                )
            if claim.claim_id in facts:
                raise LabMemoryViolationError(
                    "claim id collides with an existing fact id in the shared premise namespace",
                    detail={"id": claim.claim_id},
                )
            if len(claim_meta) >= MAX_MEMORY_CLAIMS:
                raise LabMemoryViolationError("memory claim count exceeds the declared bound")
            claim_meta[claim.claim_id] = claim
            support_history.append((claim.claim_id, claim.supports))
            current_supports_seal[claim.claim_id] = event.event_seal()
            asserted_at = _parse_instant(claim.asserted_at, field="asserted_at")
            if qualifies_recorded and asserted_at <= as_of_instant:
                claim_meta_as_of[claim.claim_id] = claim
                claim_supports_as_of[claim.claim_id] = claim.supports
        elif event.kind is MemoryEventKind.CLAIM_REVOCATION:
            claim_revocation = event.claim_revocation
            assert claim_revocation is not None
            if claim_revocation.claim_id not in claim_meta:
                raise LabMemoryViolationError(
                    "revocation names an unknown claim",
                    detail={"claim_id": claim_revocation.claim_id},
                )
            revoked_at = _parse_instant(claim_revocation.revoked_at, field="revoked_at")
            if qualifies_recorded and revoked_at <= as_of_instant:
                claim_revoked_as_of.add(claim_revocation.claim_id)
        else:
            support_revision = event.support_revision
            assert support_revision is not None
            if support_revision.claim_id not in claim_meta:
                raise LabMemoryViolationError(
                    "support revision names an unknown claim",
                    detail={"claim_id": support_revision.claim_id},
                )
            current_seal = current_supports_seal.get(support_revision.claim_id)
            if current_seal != support_revision.supersedes_seal:
                raise LabMemoryViolationError(
                    "support revision does not extend the exact current supports of its claim",
                    detail={"claim_id": support_revision.claim_id},
                )
            support_history.append((support_revision.claim_id, support_revision.supports))
            current_supports_seal[support_revision.claim_id] = event.event_seal()
            revised_at = _parse_instant(support_revision.revised_at, field="revised_at")
            if (
                qualifies_recorded
                and revised_at <= as_of_instant
                and support_revision.claim_id in claim_meta_as_of
            ):
                claim_supports_as_of[support_revision.claim_id] = support_revision.supports

    known_ids = frozenset(facts) | frozenset(claim_meta)
    for claim_id, supports in support_history:
        for conjunction in supports:
            unknown = [premise for premise in conjunction.premise_ids if premise not in known_ids]
            if unknown:
                raise LabMemoryViolationError(
                    "claim support names unknown premise id(s)",
                    detail={"claim_id": claim_id, "unknown": unknown},
                )

    fact_revoked_as_of &= frozenset(facts_as_of)
    claim_revoked_as_of &= frozenset(claim_meta_as_of)

    eligible_facts = frozenset(
        fact_id
        for fact_id, fact in facts_as_of.items()
        if fact_id not in fact_revoked_as_of
        and (
            fact.expires_at is None
            or _parse_instant(fact.expires_at, field="expires_at") > as_of_instant
        )
    )
    eligible_claims = frozenset(
        claim_id
        for claim_id, claim in claim_meta_as_of.items()
        if claim_id not in claim_revoked_as_of
        and (
            claim.expires_at is None
            or _parse_instant(claim.expires_at, field="expires_at") > as_of_instant
        )
    )

    grounded: set[str] = set(eligible_facts)
    changed = True
    while changed:
        changed = False
        for claim_id in eligible_claims:
            if claim_id in grounded:
                continue
            for conjunction in claim_supports_as_of.get(claim_id, ()):
                if all(premise in grounded for premise in conjunction.premise_ids):
                    grounded.add(claim_id)
                    changed = True
                    break

    active_claim_ids = frozenset(grounded) & eligible_claims
    return MemoryApplicability(
        active_fact_ids=eligible_facts,
        active_claim_ids=active_claim_ids,
        revoked_fact_ids=frozenset(fact_revoked_as_of),
        revoked_claim_ids=frozenset(claim_revoked_as_of),
        inactive_claim_ids=eligible_claims - active_claim_ids,
    )

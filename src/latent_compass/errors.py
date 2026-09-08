"""Typed failure vocabulary for latent-compass.

Every failure carries a stable machine-readable ``code``. The CLI maps error
classes onto distinct exit codes so a caller can distinguish *refused* from
*corrupt* from *broken*, without parsing prose.

The hierarchy encodes the fail-closed posture: anything ambiguous, unknown or
unauthorised raises rather than degrading into a permissive default.
"""

from __future__ import annotations

__all__ = [
    "AuthorityRefusal",
    "BenchmarkViolation",
    "BudgetExceeded",
    "ContractViolation",
    "DecisionMemoryViolation",
    "DecisionRevisionConflict",
    "DuplicateEpisode",
    "EpisodeValidationError",
    "EpochClosed",
    "IntegrityError",
    "LatentCompassError",
    "LedgerError",
    "PairwiseCaptureViolation",
    "PreimageMismatch",
    "ProspectiveCollectionViolation",
    "ProtocolViolation",
    "ProvenanceMismatch",
    "ReconciliationRevisionConflict",
    "ReconciliationViolation",
    "SensitiveContentRefused",
    "StoreAlreadyExists",
    "StoreNotFound",
    "TransferRefused",
    "UnsupportedContractVersion",
]


class LatentCompassError(Exception):
    """Root of every error raised by this package."""

    code = "latent_compass_error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ContractViolation(LatentCompassError):
    """Input did not satisfy a versioned contract. Always fail-closed."""

    code = "contract_violation"


class EpisodeValidationError(ContractViolation):
    """An episode payload is not a valid episode under its declared version."""

    code = "episode_validation_error"


class UnsupportedContractVersion(ContractViolation):
    """A contract version this build does not implement.

    Raised for both future and retired versions. There is deliberately no
    implicit migration path: an unknown version is never guessed at.
    """

    code = "unsupported_contract_version"


class ProvenanceMismatch(ContractViolation):
    """Episode provenance does not match the store it was presented to."""

    code = "provenance_mismatch"


class ProtocolViolation(ContractViolation):
    """A pre-registered evaluation protocol rule was broken."""

    code = "protocol_violation"


class BenchmarkViolation(ContractViolation):
    """An offline benchmark rule was broken.

    Distinct from :class:`ProtocolViolation` so a caller can tell "the
    pre-registration was violated" from "the benchmark harness was violated".
    The benchmark consumes a pre-registration; it is not one.
    """

    code = "benchmark_violation"


class PairwiseCaptureViolation(ContractViolation):
    """A pre-action judgeable projection is incomplete or contaminated."""

    code = "pairwise_capture_violation"


class DecisionMemoryViolation(ContractViolation):
    """A HOK-243 pre-action strategic decision memory rule was broken.

    Distinct from :class:`PairwiseCaptureViolation` so a caller can tell "the
    embedded judgeable projection is malformed" from "the memory record around
    it is inadmissible". The memory embeds a projection; it is not one.
    """

    code = "decision_memory_violation"


class SensitiveContentRefused(DecisionMemoryViolation):
    """Admission refused a payload before any durable write.

    Raised for an absent or non-``NON_SENSITIVE`` classification, a holdout
    marker, a forbidden field name, oversized text, or material matching a
    known credential shape. The refusal is deliberately coarse: this package
    does not claim to detect every secret, only to refuse the ones it names.
    """

    code = "sensitive_content_refused"


class DecisionRevisionConflict(DecisionMemoryViolation):
    """A revision did not extend the exact current head of its decision.

    Raised for a stale revision, a fork off an earlier revision, a replayed
    initial revision, and a revision of a revoked or foreign decision. The
    store is unchanged: no count, no generation and no root seal moves.
    """

    code = "decision_revision_conflict"


class TransferRefused(DecisionMemoryViolation):
    """An explicit cross-family transfer envelope was refused.

    Raised for a destination that is not this store, a seal that does not
    reproduce, a replayed envelope, and a decision identity that already exists
    natively. Import fails closed in every one of those cases.
    """

    code = "transfer_refused"


class ReconciliationViolation(ContractViolation):
    """A HOK-244 post-action reconciliation rule was broken.

    Deliberately *not* a :class:`DecisionMemoryViolation` subclass. A
    reconciliation references a decision record by seal and rewrites none of it;
    the two contracts fail on their own axes so a caller can tell "the
    pre-action memory refused" from "the post-action journal refused".
    """

    code = "reconciliation_violation"


class ProspectiveCollectionViolation(ContractViolation):
    """A HOK-252 preregistration, enrollment, or closure rule was broken."""

    code = "prospective_collection_violation"


class ReconciliationRevisionConflict(ReconciliationViolation):
    """A reconciliation revision did not extend the exact current head.

    Raised for a stale revision, a fork off an earlier revision, a replayed
    initial revision, a revision that re-points at a different preimage, and a
    second reconciliation identity over an already reconciled preimage. The
    journal is unchanged: no count, no generation and no root seal moves.
    """

    code = "reconciliation_revision_conflict"


class PreimageMismatch(ReconciliationViolation):
    """A reconciliation does not bind the pre-action record it was checked against.

    Raised when the binding, decision id, pre-action revision, record seal or
    projection seal differs from the supplied
    :class:`~latent_compass.decision_memory.StrategicDecisionRecord`, and when an
    executed direction is not a candidate of that record's projection.
    """

    code = "preimage_mismatch"


class BudgetExceeded(BenchmarkViolation):
    """A baseline asked for more work than the common grant allows.

    Raised on the *first* operation past the cap, not counted up and reported
    afterwards: a baseline that has already inspected a forbidden candidate has
    already had the unfair look, whatever the harness does with the tally next.
    """

    code = "budget_exceeded"


class AuthorityRefusal(LatentCompassError):
    """An actor requested an authority it does not hold, or an illegal move.

    This is the load-bearing refusal: latent-compass raises it against itself.
    """

    code = "authority_refusal"


class LedgerError(LatentCompassError):
    """Base class for ledger-level failures."""

    code = "ledger_error"


class StoreNotFound(LedgerError):
    code = "store_not_found"


class StoreAlreadyExists(LedgerError):
    code = "store_already_exists"


class DuplicateEpisode(LedgerError):
    """An episode id already present in the store was appended again."""

    code = "duplicate_episode"


class EpochClosed(LedgerError):
    """The store's epoch was abandoned; no further appends are accepted."""

    code = "epoch_closed"


class IntegrityError(LedgerError):
    """The stored chain does not reproduce. Distinct from a refused write."""

    code = "integrity_error"

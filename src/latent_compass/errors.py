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
    "ContractViolation",
    "DuplicateEpisode",
    "EpisodeValidationError",
    "EpochClosed",
    "IntegrityError",
    "LatentCompassError",
    "LedgerError",
    "ProtocolViolation",
    "ProvenanceMismatch",
    "StoreAlreadyExists",
    "StoreNotFound",
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

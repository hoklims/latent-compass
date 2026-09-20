"""Typed failure vocabulary for ``latent_compass.lab``.

Every lab refusal subclasses :class:`~latent_compass.errors.ContractViolation`
so it can flow through the shared :func:`~latent_compass.contracts.validate_contract`
helper, and so a caller catching the legacy root still catches a lab refusal.
Each carries its own ``lab_``-prefixed ``code`` so a lab refusal is never
mistaken for a legacy one. Every class name ends in ``Error`` (ruff N818);
the stable machine-readable ``code`` a caller actually matches on is
unaffected by this naming.
"""

from __future__ import annotations

from latent_compass.errors import ContractViolation

__all__ = [
    "LabBindingMismatchError",
    "LabContractViolationError",
    "LabCrossModelStateError",
    "LabError",
    "LabImpossibleObservationError",
    "LabMemoryViolationError",
    "LabRepeatedProbeError",
    "LabReplayViolationError",
    "LabSourceScopeMismatchError",
    "LabUnknownReferenceError",
]


class LabError(ContractViolation):
    """Root of every ``latent_compass.lab`` refusal."""

    code = "lab_error"


class LabContractViolationError(LabError):
    """A lab payload did not satisfy its declared ``1.0.0`` experimental contract."""

    code = "lab_contract_violation"


class LabUnknownReferenceError(LabContractViolationError):
    """A payload names a world, probe, decision or outcome the model does not declare."""

    code = "lab_unknown_reference"


class LabCrossModelStateError(LabContractViolationError):
    """A state, plan or observation was presented against the wrong sealed model."""

    code = "lab_cross_model_state"


class LabBindingMismatchError(LabContractViolationError):
    """A state's declared binding did not match the binding a caller expects.

    Raised at the consumer boundary of :func:`~latent_compass.lab.planner.propose`
    and :func:`~latent_compass.lab.state.apply_observation`: an internally
    self-consistent, correctly replayed state can still claim a host id or
    agent family that is not the one actually calling. A digest or seal proves
    integrity of what was recorded, never which caller recorded it.
    """

    code = "lab_binding_mismatch"


class LabSourceScopeMismatchError(LabContractViolationError):
    """An observation named a source-scope digest that drifted from the episode's."""

    code = "lab_source_scope_mismatch"


class LabRepeatedProbeError(LabContractViolationError):
    """A probe already acquired in this episode was presented for acquisition again."""

    code = "lab_repeated_probe"


class LabImpossibleObservationError(LabContractViolationError):
    """An observation is inconsistent with every world remaining in the posterior.

    Refused outside the model: a zero-mass posterior is never converted into one
    of the model's terminal decisions, and the prior state is left unchanged.
    """

    code = "lab_impossible_observation"


class LabReplayViolationError(LabContractViolationError):
    """A state or memory revision history does not deterministically replay.

    Raised for a broken seal chain, a non-sequential revision, a binding or
    model drift mid-history, an omitted or truncated history, or a posterior
    that does not reproduce from its recorded observation. A tampered or
    incomplete history is refused, never repaired and never trusted cheaply.
    """

    code = "lab_replay_violation"


class LabMemoryViolationError(LabContractViolationError):
    """A justification memory event or revision chain is inadmissible."""

    code = "lab_memory_violation"

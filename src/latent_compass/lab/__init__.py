"""``latent_compass.lab`` — HOK-798 isolated experimental active-diagnosis lab.

See ``docs/adr/0011-experimental-active-diagnosis.md`` for the boundary this
package is not allowed to cross, and ``docs/active-diagnosis.md`` for the
usage guide.

Everything here is experimental, carries its own ``1.0.0`` contract axes
separate from the rest of ``latent_compass``, and has **no execution
authority**. A ``PlanReport`` is a recommendation computed from a caller
-supplied finite model; it is not a decision, an authorization, or an
empirical claim about the real world. Nothing in this package launches a
model, tool, agent, shell command, network request, or host mutation, and it
is deliberately not wired into the legacy ``latent-compass`` CLI.
"""

from __future__ import annotations

from latent_compass.lab.contracts import (
    LAB_CONSTRAINED_PLAN_CONTRACT_VERSION,
    LAB_CONTRACT_VERSION,
    LAB_MEMORY_CONTRACT_VERSION,
    LAB_NON_AUTHORITY_NOTICE,
    lab_limits,
)
from latent_compass.lab.errors import (
    LabBindingMismatchError,
    LabContractViolationError,
    LabCrossModelStateError,
    LabError,
    LabImpossibleObservationError,
    LabMemoryViolationError,
    LabRepeatedProbeError,
    LabReplayViolationError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.memory import (
    ClaimAssertion,
    ClaimRevocation,
    FactAssertion,
    FactRevocation,
    MemoryApplicability,
    MemoryBinding,
    MemoryEvent,
    MemoryEventKind,
    SupportConjunction,
    SupportRevision,
    compute_applicability,
)
from latent_compass.lab.model import (
    Decision,
    DiagnosisModel,
    EvidenceRequirement,
    Probe,
    WorldPrior,
    load_model,
)
from latent_compass.lab.planner import (
    ConstrainedPlanReport,
    PlanReport,
    load_constrained_plan_report,
    load_plan_report,
    propose,
    propose_excluding,
)
from latent_compass.lab.state import (
    DiagnosisStateRevision,
    LabBinding,
    ObservationEvent,
    apply_observation,
    initial_state,
    load_state,
    verify_state_history,
)

__all__ = [
    "LAB_CONSTRAINED_PLAN_CONTRACT_VERSION",
    "LAB_CONTRACT_VERSION",
    "LAB_MEMORY_CONTRACT_VERSION",
    "LAB_NON_AUTHORITY_NOTICE",
    "ClaimAssertion",
    "ClaimRevocation",
    "ConstrainedPlanReport",
    "Decision",
    "DiagnosisModel",
    "DiagnosisStateRevision",
    "EvidenceRequirement",
    "FactAssertion",
    "FactRevocation",
    "LabBinding",
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
    "MemoryApplicability",
    "MemoryBinding",
    "MemoryEvent",
    "MemoryEventKind",
    "ObservationEvent",
    "PlanReport",
    "Probe",
    "SupportConjunction",
    "SupportRevision",
    "WorldPrior",
    "apply_observation",
    "compute_applicability",
    "initial_state",
    "lab_limits",
    "load_constrained_plan_report",
    "load_model",
    "load_plan_report",
    "load_state",
    "propose",
    "propose_excluding",
    "verify_state_history",
]

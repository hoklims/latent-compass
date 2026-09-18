"""Contract versions and bounds shared by every ``latent_compass.lab`` module.

Two independent version axes, per ADR 0011: the finite-world diagnosis core
(model, state, plan report) and the justification memory. Neither shares a
version, a seal domain, or a field with any legacy ``latent_compass`` contract.

Every bound below exists so a caller-supplied model or search request has an
explicit, checkable ceiling rather than an implicit one discovered by a hang
or an out-of-memory crash. Bounds are deliberately conservative: this is a
local, single-operator research tool, not a service with a capacity plan.
"""

from __future__ import annotations

from typing import Final

#: The finite-world diagnosis core: worlds, probes, decisions, state, plan report.
LAB_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_LAB_VERSIONS: Final = frozenset({LAB_CONTRACT_VERSION})

#: The separate justification memory: facts, claims, supports, revisions.
LAB_MEMORY_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_LAB_MEMORY_VERSIONS: Final = frozenset({LAB_MEMORY_CONTRACT_VERSION})

#: Carried on every lab output document. Not a licence disclaimer — a repeated,
#: machine-readable statement that nothing here may be read as authorization.
LAB_NON_AUTHORITY_NOTICE: Final = (
    "latent_compass.lab is experimental, has no execution authority, and makes "
    "no empirical claim. A plan or applicability report is a recommendation "
    "computed from a caller-supplied finite model, never a decision."
)

# --- Model bounds ------------------------------------------------------------

MAX_WORLDS: Final = 64
MAX_DECISIONS: Final = 32
MAX_PROBES: Final = 32
MAX_OUTCOMES_PER_PROBE: Final = 16
MAX_REQUIRED_EVIDENCE_PER_DECISION: Final = 8
MAX_PRIOR_WEIGHT: Final = 1_000_000_000
MAX_LOSS: Final = 1_000_000_000
MAX_PROBE_COST: Final = 1_000_000_000

# --- Planning bounds -----------------------------------------------------

MAX_BUDGET: Final = 1_000_000_000
MAX_HORIZON: Final = 6
MAX_EXPANSIONS: Final = 200_000

# --- State bounds ----------------------------------------------------------

MAX_STATE_REVISION: Final = MAX_PROBES
MAX_CLI_INPUT_BYTES: Final = 8 * 1024 * 1024

# --- Memory bounds -----------------------------------------------------------

MAX_MEMORY_FACTS: Final = 4096
MAX_MEMORY_CLAIMS: Final = 4096
MAX_MEMORY_EVENTS: Final = 65_536
MAX_PREMISES_PER_CONJUNCTION: Final = 16
MAX_SUPPORTS_PER_CLAIM: Final = 16


def lab_limits() -> dict[str, object]:
    """Machine-readable statement of every bound this package enforces.

    Exposed as ``python -m latent_compass.lab limits`` so a caller can size a
    model or a search request before submitting it, and so a refusal on an
    oversized input can cite the exact ceiling it crossed.
    """
    return {
        "lab_contract_version": LAB_CONTRACT_VERSION,
        "lab_memory_contract_version": LAB_MEMORY_CONTRACT_VERSION,
        "model": {
            "max_worlds": MAX_WORLDS,
            "max_decisions": MAX_DECISIONS,
            "max_probes": MAX_PROBES,
            "max_outcomes_per_probe": MAX_OUTCOMES_PER_PROBE,
            "max_required_evidence_per_decision": MAX_REQUIRED_EVIDENCE_PER_DECISION,
            "max_prior_weight": MAX_PRIOR_WEIGHT,
            "max_loss": MAX_LOSS,
            "max_probe_cost": MAX_PROBE_COST,
        },
        "planning": {
            "max_budget": MAX_BUDGET,
            "max_horizon": MAX_HORIZON,
            "max_expansions": MAX_EXPANSIONS,
        },
        "state": {
            "max_state_revision": MAX_STATE_REVISION,
        },
        "cli": {"max_input_bytes": MAX_CLI_INPUT_BYTES},
        "memory": {
            "max_facts": MAX_MEMORY_FACTS,
            "max_claims": MAX_MEMORY_CLAIMS,
            "max_events": MAX_MEMORY_EVENTS,
            "max_premises_per_conjunction": MAX_PREMISES_PER_CONJUNCTION,
            "max_supports_per_claim": MAX_SUPPORTS_PER_CLAIM,
        },
        "non_authority_notice": LAB_NON_AUTHORITY_NOTICE,
    }

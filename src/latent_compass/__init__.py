"""latent-compass — a proof-governed latent direction arbiter for coding agents.

The operational surface **validates and records**. It takes advisories supplied
by a caller, refuses anything ambiguous or unversioned, and appends what survives
to a local, append-only, host-bound ledger. Separately, HOK-188 runs four closed
policies offline over recorded synthetic cases and computes diagnostic
confidences; the scoped summaries never reach an agent and are rejected when
passed directly as lifecycle evidence.

It is advisory by construction: it holds no execution, mutation, promotion or
final-decision authority, and the deterministic judge of truth, invariants,
impact, gates and proof stays outside this package entirely.

Start at :mod:`latent_compass.authority` — the boundary is the product.
"""

from __future__ import annotations

from latent_compass.authority import (
    RefusalReason,
    TransitionAuthorization,
    authority_boundary_seal,
    authority_boundary_snapshot,
    authorize_transition,
)
from latent_compass.benchmark import (
    BaselineId,
    BenchmarkReport,
    BenchmarkSpec,
    CorpusManifest,
    run_benchmark,
    verify_report,
)
from latent_compass.contracts import (
    AUTHORITY_CONTRACT_VERSION,
    BENCHMARK_CONTRACT_VERSION,
    EPISODE_CONTRACT_VERSION,
    JUDGEABLE_PROJECTION_CONTRACT_VERSION,
    LEDGER_FORMAT_VERSION,
    PROTOCOL_CONTRACT_VERSION,
)
from latent_compass.episode import AgentFamily, Episode, load_episode
from latent_compass.errors import (
    AuthorityRefusal,
    ContractViolation,
    IntegrityError,
    LatentCompassError,
    LedgerError,
    PairwiseCaptureViolation,
    ProtocolViolation,
    UnsupportedContractVersion,
)
from latent_compass.governance import DeletionMode, Tombstone
from latent_compass.ledger import LedgerStore
from latent_compass.pairwise_capture import (
    JudgeableDecisionProjection,
    PairwiseJudgeInput,
    load_judgeable_projection,
    load_pairwise_judge_input,
    verify_pairwise_judge_input,
)
from latent_compass.protocol import (
    HoldoutLedger,
    Preregistration,
    Verdict,
    evaluate,
    load_measurement_set,
    load_preregistration,
    recompute_verdict_seal,
)
from latent_compass.vocabulary import (
    Actor,
    Advisory,
    AdvisoryKind,
    Capability,
    ContinueKill,
    LifecycleState,
    may_issue_direction,
)

__version__ = "0.1.0"

__all__ = [
    "AUTHORITY_CONTRACT_VERSION",
    "BENCHMARK_CONTRACT_VERSION",
    "EPISODE_CONTRACT_VERSION",
    "JUDGEABLE_PROJECTION_CONTRACT_VERSION",
    "LEDGER_FORMAT_VERSION",
    "PROTOCOL_CONTRACT_VERSION",
    "Actor",
    "Advisory",
    "AdvisoryKind",
    "AgentFamily",
    "AuthorityRefusal",
    "BaselineId",
    "BenchmarkReport",
    "BenchmarkSpec",
    "Capability",
    "ContinueKill",
    "ContractViolation",
    "CorpusManifest",
    "DeletionMode",
    "Episode",
    "HoldoutLedger",
    "IntegrityError",
    "JudgeableDecisionProjection",
    "LatentCompassError",
    "LedgerError",
    "LedgerStore",
    "LifecycleState",
    "PairwiseCaptureViolation",
    "PairwiseJudgeInput",
    "Preregistration",
    "ProtocolViolation",
    "RefusalReason",
    "Tombstone",
    "TransitionAuthorization",
    "UnsupportedContractVersion",
    "Verdict",
    "__version__",
    "authority_boundary_seal",
    "authority_boundary_snapshot",
    "authorize_transition",
    "evaluate",
    "load_episode",
    "load_judgeable_projection",
    "load_measurement_set",
    "load_pairwise_judge_input",
    "load_preregistration",
    "may_issue_direction",
    "recompute_verdict_seal",
    "run_benchmark",
    "verify_pairwise_judge_input",
    "verify_report",
]

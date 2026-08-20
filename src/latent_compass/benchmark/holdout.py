"""Pre-HOK-190 prerequisite — a sealed checkpoint before final holdout use.

The plan freezes which already-registered baseline may enter a future final
holdout comparison. Creating it replays the complete HOK-188 validation report,
but never resolves or reads the holdout split. It is neither a holdout result
nor authority evidence, and it carries no execution or ledger capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from latent_compass.benchmark.baselines import BaselineId
from latent_compass.benchmark.corpus import CorpusManifest
from latent_compass.benchmark.report import UNSEALED, BenchmarkReport
from latent_compass.benchmark.runner import verify_report
from latent_compass.benchmark.spec import BenchmarkSpec
from latent_compass.canonical import seal
from latent_compass.contracts import (
    BENCHMARK_CONTRACT_VERSION,
    SUPPORTED_BENCHMARK_VERSIONS,
    Identifier,
    Seal,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import Preregistration
from latent_compass.vocabulary import ContinueKill

__all__ = [
    "HOLDOUT_PLAN_SCOPE",
    "HoldoutPlan",
    "create_holdout_plan",
    "load_holdout_plan",
    "recompute_holdout_plan_seal",
]

HOLDOUT_PLAN_SCOPE: Final = "FINAL_HOLDOUT_PLAN_ONLY"
HOLDOUT_PLAN_SEAL_DOMAIN: Final = "benchmark.holdout-plan"


class HoldoutPlan(StrictModel):
    """The immutable identity of one explicitly selected ``CONTINUE`` baseline."""

    contract_version: str = Field(min_length=5, max_length=20)
    scope: Literal["FINAL_HOLDOUT_PLAN_ONLY"] = HOLDOUT_PLAN_SCOPE
    benchmark_id: Identifier
    spec_seal: Seal
    validation_report_seal: Seal
    protocol_id: Identifier
    protocol_seal: Seal
    epoch: Identifier
    corpus_id: Identifier
    corpus_version: Identifier
    validation_corpus_seal: Seal
    holdout_corpus_seal: Seal
    baseline_registry_seal: Seal
    selected_baseline_id: BaselineId
    selected_algorithm_version: Identifier
    selection_mode: Literal["EXTERNAL_EXPLICIT"] = "EXTERNAL_EXPLICIT"
    holdout_executed: Literal[False] = False
    plan_seal: Seal

    def model_post_init(self, __context: object) -> None:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")

    def sealed_body(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload.pop("plan_seal", None)
        return payload


def recompute_holdout_plan_seal(plan: HoldoutPlan) -> str:
    """Recompute the consistency seal over the complete pre-holdout checkpoint."""
    return seal(HOLDOUT_PLAN_SEAL_DOMAIN, plan.sealed_body())


def _seal_holdout_plan(plan: HoldoutPlan) -> HoldoutPlan:
    return load_holdout_plan(
        plan.model_copy(update={"plan_seal": recompute_holdout_plan_seal(plan)}).canonical_payload()
    )


def load_holdout_plan(payload: object) -> HoldoutPlan:
    """Validate an untrusted phase-1 plan payload without granting it authority."""
    if not isinstance(payload, dict):
        raise BenchmarkViolation(
            "holdout plan payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise BenchmarkViolation(
            "holdout plan declares no contract_version",
            detail={"contract": "benchmark", "reason": "absent"},
        )
    plan = validate_contract(HoldoutPlan, payload, error=BenchmarkViolation, context="holdout plan")
    recomputed = recompute_holdout_plan_seal(plan)
    if plan.plan_seal != recomputed:
        raise BenchmarkViolation(
            "the holdout plan seal does not reproduce from the plan's contents",
            detail={"carried": plan.plan_seal, "recomputed": recomputed},
        )
    return plan


def create_holdout_plan(
    report: BenchmarkReport,
    *,
    spec: BenchmarkSpec,
    protocol: Preregistration,
    manifest: CorpusManifest,
    corpus_dir: Path,
    selected_baseline_id: BaselineId,
) -> HoldoutPlan:
    """Replay validation and freeze one explicit ``CONTINUE`` baseline.

    ``corpus_dir`` is deliberately passed only to :func:`verify_report`, whose
    HOK-188 contract resolves the VALIDATION manifest entry exclusively.
    """
    verify_report(
        report,
        spec=spec,
        protocol=protocol,
        manifest=manifest,
        corpus_dir=corpus_dir,
    )

    selected = next(
        (item for item in report.baselines if item.baseline_id is selected_baseline_id), None
    )
    if selected is None:  # pragma: no cover - BenchmarkReport validates the closed registry
        raise BenchmarkViolation(
            "the selected baseline is absent from the validation report",
            detail={"selected_baseline_id": selected_baseline_id.value},
        )
    if selected.verdict.decision is not ContinueKill.CONTINUE:
        raise BenchmarkViolation(
            "only a baseline with a validation CONTINUE verdict may enter the final holdout plan",
            detail={
                "selected_baseline_id": selected_baseline_id.value,
                "validation_decision": selected.verdict.decision.value,
            },
        )

    unsealed = HoldoutPlan(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        benchmark_id=report.benchmark_id,
        spec_seal=report.spec_seal,
        validation_report_seal=report.report_seal,
        protocol_id=report.protocol_id,
        protocol_seal=report.protocol_seal,
        epoch=report.epoch,
        corpus_id=report.corpus_id,
        corpus_version=report.corpus_version,
        validation_corpus_seal=report.validation_corpus_seal,
        holdout_corpus_seal=report.holdout_corpus_seal,
        baseline_registry_seal=report.baseline_registry_seal,
        selected_baseline_id=selected.baseline_id,
        selected_algorithm_version=selected.algorithm_version,
        plan_seal=UNSEALED,
    )
    return _seal_holdout_plan(unsealed)

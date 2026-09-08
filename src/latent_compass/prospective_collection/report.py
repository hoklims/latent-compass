"""Deterministic all-case manifest and narrow HOK-252 collection diagnostic."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import Seal, StrictModel
from latent_compass.decision_reconciliation import ObservationStatus
from latent_compass.pairwise_capture import DIMENSION_ORDER
from latent_compass.prospective_collection.contracts import CaseState, ProspectiveCollectionPlan
from latent_compass.prospective_collection.store import CollectionCase, CollectionStatus

__all__ = [
    "CollectionDiagnosticReport",
    "CollectionManifest",
    "CollectionManifestCase",
    "SelectionImbalanceDiagnostic",
    "StratumInclusion",
    "build_collection_manifest",
    "build_collection_report",
]

Rate = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class CollectionManifestCase(StrictModel):
    case_id: str
    state: CaseState
    stratum: str
    decision_id: str
    decision_revision: int = Field(ge=1)
    preimage_seal: Seal
    reconciliation_record_seal: Seal | None
    eligible_for_corpus: bool
    terminal_reason: str | None
    exclusion_reason: str | None


class CollectionManifest(StrictModel):
    plan_id: str
    plan_seal: Seal
    collection_state: str
    journal_root_seal: Seal
    enrolled_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    cases: tuple[CollectionManifestCase, ...]
    eligible_corpus_seal: Seal
    manifest_seal: Seal

    @model_validator(mode="after")
    def _seals_reproduce(self) -> CollectionManifest:
        eligible = [item.canonical_payload() for item in self.cases if item.eligible_for_corpus]
        if seal("prospective-collection.eligible-corpus.v1", eligible) != self.eligible_corpus_seal:
            raise ValueError("eligible corpus seal does not reproduce from the eligible subset")
        body = self.canonical_payload()
        body.pop("manifest_seal")
        if seal("prospective-collection.manifest.v1", body) != self.manifest_seal:
            raise ValueError("manifest seal does not reproduce from the all-case manifest")
        return self


class StratumInclusion(StrictModel):
    stratum: str
    enrolled_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    inclusion_rate: Rate


class SelectionImbalanceDiagnostic(StrictModel):
    label: str
    maximum_stratum_gap: Rate


class CollectionDiagnosticReport(StrictModel):
    plan_id: str
    plan_seal: Seal
    manifest_seal: Seal
    eligibility_rate: Rate
    abstention_or_nonexecution_rate: Rate
    cancellation_rate: Rate
    lost_to_followup_rate: Rate
    missingness: dict[str, dict[str, int]]
    inclusion_by_stratum: tuple[StratumInclusion, ...]
    selection_imbalance_diagnostic: SelectionImbalanceDiagnostic
    report_seal: Seal

    @model_validator(mode="after")
    def _seal_reproduces(self) -> CollectionDiagnosticReport:
        body = self.canonical_payload()
        body.pop("report_seal")
        if seal("prospective-collection.report.v1", body) != self.report_seal:
            raise ValueError("report seal does not reproduce from the diagnostic")
        return self


def build_collection_manifest(
    plan: ProspectiveCollectionPlan,
    status: CollectionStatus,
    cases: tuple[CollectionCase, ...],
) -> CollectionManifest:
    entries = tuple(
        CollectionManifestCase(
            case_id=case.case_id,
            state=case.state,
            stratum=case.stratum,
            decision_id=case.decision_id,
            decision_revision=case.decision_revision,
            preimage_seal=case.preimage_seal,
            reconciliation_record_seal=case.reconciliation_record_seal,
            eligible_for_corpus=case.state is CaseState.RECONCILED,
            terminal_reason=case.terminal_reason,
            exclusion_reason=(
                None
                if case.state is CaseState.RECONCILED
                else case.terminal_reason or "NONTERMINAL_AT_MANIFEST"
            ),
        )
        for case in cases
    )
    eligible_body = [entry.canonical_payload() for entry in entries if entry.eligible_for_corpus]
    eligible_seal = seal("prospective-collection.eligible-corpus.v1", eligible_body)
    body: dict[str, object] = {
        "plan_id": plan.plan_id,
        "plan_seal": plan.plan_seal(),
        "collection_state": status.state.value,
        "journal_root_seal": status.root_seal,
        "enrolled_count": len(entries),
        "eligible_count": len(eligible_body),
        "cases": [entry.canonical_payload() for entry in entries],
        "eligible_corpus_seal": eligible_seal,
    }
    return CollectionManifest(
        plan_id=plan.plan_id,
        plan_seal=plan.plan_seal(),
        collection_state=status.state.value,
        journal_root_seal=status.root_seal,
        enrolled_count=len(entries),
        eligible_count=len(eligible_body),
        cases=entries,
        eligible_corpus_seal=eligible_seal,
        manifest_seal=seal("prospective-collection.manifest.v1", body),
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def build_collection_report(
    plan: ProspectiveCollectionPlan,
    manifest: CollectionManifest,
    cases: tuple[CollectionCase, ...],
) -> CollectionDiagnosticReport:
    denominator = len(cases)
    missingness = {
        dimension.value: dict.fromkeys(("ABSENT", "LATE", "AMBIGUOUS", "DISPUTED"), 0)
        for dimension in DIMENSION_ORDER
    }
    for case in cases:
        if case.reconciliation is None:
            continue
        observations = case.reconciliation.get("observations", [])
        if not isinstance(observations, list):
            continue
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            if observation.get("status") == ObservationStatus.UNKNOWN.value:
                dimension = observation.get("dimension")
                reason = observation.get("unknown_reason")
                if isinstance(dimension, str) and isinstance(reason, str):
                    missingness[dimension][reason] += 1
    strata: list[StratumInclusion] = []
    for stratum in plan.declared_strata:
        members = [case for case in cases if case.stratum == stratum]
        eligible = sum(case.state is CaseState.RECONCILED for case in members)
        strata.append(
            StratumInclusion(
                stratum=stratum,
                enrolled_count=len(members),
                eligible_count=eligible,
                inclusion_rate=_rate(eligible, len(members)),
            )
        )
    rates = [item.inclusion_rate for item in strata]
    imbalance = SelectionImbalanceDiagnostic(
        label="selection_imbalance_diagnostic",
        maximum_stratum_gap=max(rates) - min(rates) if rates else 0.0,
    )
    body: dict[str, object] = {
        "plan_id": plan.plan_id,
        "plan_seal": plan.plan_seal(),
        "manifest_seal": manifest.manifest_seal,
        "eligibility_rate": _rate(manifest.eligible_count, denominator),
        "abstention_or_nonexecution_rate": _rate(
            sum(case.state is CaseState.ABSTAINED for case in cases), denominator
        ),
        "cancellation_rate": _rate(
            sum(case.state is CaseState.CANCELLED for case in cases), denominator
        ),
        "lost_to_followup_rate": _rate(
            sum(case.state is CaseState.LOST_TO_FOLLOWUP for case in cases), denominator
        ),
        "missingness": missingness,
        "inclusion_by_stratum": [item.canonical_payload() for item in strata],
        "selection_imbalance_diagnostic": imbalance.canonical_payload(),
    }
    return CollectionDiagnosticReport(
        plan_id=plan.plan_id,
        plan_seal=plan.plan_seal(),
        manifest_seal=manifest.manifest_seal,
        eligibility_rate=_rate(manifest.eligible_count, denominator),
        abstention_or_nonexecution_rate=_rate(
            sum(case.state is CaseState.ABSTAINED for case in cases), denominator
        ),
        cancellation_rate=_rate(
            sum(case.state is CaseState.CANCELLED for case in cases), denominator
        ),
        lost_to_followup_rate=_rate(
            sum(case.state is CaseState.LOST_TO_FOLLOWUP for case in cases), denominator
        ),
        missingness=missingness,
        inclusion_by_stratum=tuple(strata),
        selection_imbalance_diagnostic=imbalance,
        report_seal=seal("prospective-collection.report.v1", body),
    )

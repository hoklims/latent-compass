"""HOK-805/806 — a read-only retirement dry-run, never an activation.

:func:`build_retirement_dry_run` maps a declared inventory of providers and
assets onto exactly three possible actions — ``KEEP``, ``DISABLE_LATER`` and
``RETAIN_FOR_ROLLBACK`` — and lists, for every asset, the gate that remains
missing. It performs no action: there is no ``apply`` and no ``delete``
function anywhere in this module, by design, and the result it returns can
never itself become one, however complete its declared evidence references.

Authored data, required evidence, source or symbolic tooling, the vault, and
any asset scoped as professional or unscoped are protected outright. An asset
with unknown consumers or an unknown classification is also kept: ambiguity
defaults to protection, never to eligibility.

An evidence reference is a claimed digest, nothing more. This module never
opens, fetches or verifies what it points at, so a populated reference changes
what is *listed as missing*, never what is *proven*. Independent comparison,
a live pilot, external verification and the owner's own irreversible-deletion
authority are conditions this module can never satisfy on an asset's behalf,
and every dry-run report names them as still outstanding.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import ContractViolation

__all__ = [
    "LAB_MIGRATION_CONTRACT_VERSION",
    "SUPPORTED_LAB_MIGRATION_VERSIONS",
    "AssetClassification",
    "AssetConsumer",
    "AssetDisposition",
    "EvidenceReference",
    "EvidenceReferenceKind",
    "InventoryAsset",
    "LabMigrationViolation",
    "MissingGate",
    "ProtectionReason",
    "RetirementAction",
    "RetirementDryRunReport",
    "ScopeKind",
    "admit_inventory_asset",
    "build_retirement_dry_run",
]

LAB_MIGRATION_CONTRACT_VERSION: Final = "1.0.0"
SUPPORTED_LAB_MIGRATION_VERSIONS: Final = frozenset({LAB_MIGRATION_CONTRACT_VERSION})

INVENTORY_SEAL_DOMAIN: Final = "lab.migration.inventory.v1"
REPORT_SEAL_DOMAIN: Final = "lab.migration.report.v1"


class LabMigrationViolation(ContractViolation):
    """A HOK-805/806 retirement inventory or dry-run rule was broken."""

    code = "lab_migration_violation"


class AssetClassification(StrEnum):
    CANDIDATE_INDEX = "CANDIDATE_INDEX"
    REQUIRED_EVIDENCE = "REQUIRED_EVIDENCE"
    SOURCE_OR_SYMBOLIC_TOOL = "SOURCE_OR_SYMBOLIC_TOOL"
    AUTHORED_DATA = "AUTHORED_DATA"
    VAULT = "VAULT"
    UNKNOWN = "UNKNOWN"


#: Every classification a retirement candidate is not. Only ``CANDIDATE_INDEX``
#: is ever eligible for anything other than ``KEEP``.
PROTECTED_CLASSIFICATIONS: Final = frozenset(
    {
        AssetClassification.REQUIRED_EVIDENCE,
        AssetClassification.SOURCE_OR_SYMBOLIC_TOOL,
        AssetClassification.AUTHORED_DATA,
        AssetClassification.VAULT,
        AssetClassification.UNKNOWN,
    }
)


class ScopeKind(StrEnum):
    PERSONAL_LAB = "PERSONAL_LAB"
    PROFESSIONAL = "PROFESSIONAL"
    UNSCOPED = "UNSCOPED"


class EvidenceReferenceKind(StrEnum):
    BACKUP = "BACKUP"
    RESTORE = "RESTORE"
    STABILITY = "STABILITY"


class RetirementAction(StrEnum):
    """The only three outcomes this module can ever propose."""

    KEEP = "KEEP"
    DISABLE_LATER = "DISABLE_LATER"
    RETAIN_FOR_ROLLBACK = "RETAIN_FOR_ROLLBACK"


class ProtectionReason(StrEnum):
    CLASSIFICATION_PROTECTED = "CLASSIFICATION_PROTECTED"
    SCOPE_PROFESSIONAL_OR_UNSCOPED = "SCOPE_PROFESSIONAL_OR_UNSCOPED"
    CONSUMERS_UNKNOWN = "CONSUMERS_UNKNOWN"


class MissingGate(StrEnum):
    BACKUP_EVIDENCE_ABSENT = "BACKUP_EVIDENCE_ABSENT"
    RESTORE_EVIDENCE_ABSENT = "RESTORE_EVIDENCE_ABSENT"
    STABILITY_EVIDENCE_ABSENT = "STABILITY_EVIDENCE_ABSENT"
    INDEPENDENT_COMPARISON_ABSENT = "INDEPENDENT_COMPARISON_ABSENT"
    LIVE_PILOT_ABSENT = "LIVE_PILOT_ABSENT"
    EXTERNAL_VERIFICATION_ABSENT = "EXTERNAL_VERIFICATION_ABSENT"
    OWNER_DELETION_AUTHORITY_ABSENT = "OWNER_DELETION_AUTHORITY_ABSENT"


#: Gates this module can never itself close, named on every non-protected
#: disposition regardless of how complete its evidence references are.
_ALWAYS_OUTSTANDING_GATES: Final = (
    MissingGate.INDEPENDENT_COMPARISON_ABSENT,
    MissingGate.LIVE_PILOT_ABSENT,
    MissingGate.EXTERNAL_VERIFICATION_ABSENT,
    MissingGate.OWNER_DELETION_AUTHORITY_ABSENT,
)


class EvidenceReference(StrictModel):
    """A claimed digest, not a verified fact. This module never dereferences it."""

    kind: EvidenceReferenceKind
    digest: Seal
    referenced_at: Timestamp


class AssetConsumer(StrictModel):
    consumer_id: Identifier


class InventoryAsset(StrictModel):
    """One declared provider or asset, as an unauthenticated input."""

    contract_version: str
    asset_id: Identifier
    provider_id: Identifier
    role: Identifier
    scope: Identifier
    scope_kind: ScopeKind
    host_id: Identifier
    configuration_digest: Seal
    consumers: tuple[AssetConsumer, ...] = Field(default=(), max_length=256)
    classification: AssetClassification
    evidence_references: tuple[EvidenceReference, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _coherent_asset(self) -> Self:
        check_contract_version(
            self.contract_version, SUPPORTED_LAB_MIGRATION_VERSIONS, "lab inventory asset"
        )
        consumer_ids = [item.consumer_id for item in self.consumers]
        if len(set(consumer_ids)) != len(consumer_ids):
            raise ValueError("a consumer id must be listed at most once")
        return self

    def has_reference(self, kind: EvidenceReferenceKind) -> bool:
        return any(item.kind is kind for item in self.evidence_references)


class AssetDisposition(StrictModel):
    """The one dry-run outcome for one asset. Never a grant, never applied."""

    asset_id: Identifier
    classification: AssetClassification
    action: RetirementAction
    protected: bool
    protection_reasons: tuple[ProtectionReason, ...] = Field(default=(), max_length=8)
    missing_gates: tuple[MissingGate, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _coherent_disposition(self) -> Self:
        if self.protected:
            if self.action is not RetirementAction.KEEP:
                raise ValueError("a protected asset's action must be KEEP")
            if not self.protection_reasons:
                raise ValueError("a protected asset must name at least one protection reason")
        elif self.protection_reasons:
            raise ValueError("an unprotected asset names no protection reason")
        if self.action is RetirementAction.KEEP and not self.protected and self.missing_gates:
            raise ValueError("an unprotected KEEP action names no missing gate to report")
        return self


class RetirementDryRunReport(StrictModel):
    """The always-non-authoritative result of one retirement dry-run."""

    contract_version: str
    inventory_digest: Seal
    generated_at: Timestamp
    dispositions: tuple[AssetDisposition, ...]
    dry_run: Literal[True] = True
    requires_external_verification: Literal[True] = True
    requires_external_authorization: Literal[True] = True
    activation_token: Literal[False] = False
    report_seal: Seal

    @model_validator(mode="after")
    def _coherent_report(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_LAB_MIGRATION_VERSIONS,
            "lab retirement dry-run report",
        )
        asset_ids = [item.asset_id for item in self.dispositions]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("a disposition must name each asset id at most once")
        body = self.canonical_payload()
        body.pop("report_seal")
        if seal(REPORT_SEAL_DOMAIN, body) != self.report_seal:
            raise ValueError("report seal does not reproduce from its own body")
        return self


def admit_inventory_asset(payload: object) -> InventoryAsset:
    return validate_contract(
        InventoryAsset, payload, error=LabMigrationViolation, context="lab inventory asset"
    )


def _revalidated_asset(asset: InventoryAsset) -> InventoryAsset:
    """Force ``asset`` back through full contract validation.

    Guards :func:`build_retirement_dry_run`, this module's public boundary,
    against a ``model_construct``-built or mutated asset that never ran its
    own ``model_validator``.
    """
    try:
        return InventoryAsset.model_validate(asset)
    except ValidationError as exc:
        raise LabMigrationViolation(
            "lab inventory asset failed strict contract validation",
            detail={
                "violations": [
                    {
                        "location": ".".join(str(part) for part in item["loc"]),
                        "type": item["type"],
                        "message": item["msg"],
                    }
                    for item in exc.errors(include_url=False)
                ],
            },
        ) from exc


def _disposition(asset: InventoryAsset) -> AssetDisposition:
    protection_reasons: list[ProtectionReason] = []
    if asset.classification in PROTECTED_CLASSIFICATIONS:
        protection_reasons.append(ProtectionReason.CLASSIFICATION_PROTECTED)
    if asset.scope_kind in (ScopeKind.PROFESSIONAL, ScopeKind.UNSCOPED):
        protection_reasons.append(ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED)
    if not asset.consumers:
        protection_reasons.append(ProtectionReason.CONSUMERS_UNKNOWN)

    if protection_reasons:
        return AssetDisposition(
            asset_id=asset.asset_id,
            classification=asset.classification,
            action=RetirementAction.KEEP,
            protected=True,
            protection_reasons=tuple(protection_reasons),
            missing_gates=(),
        )

    missing_gates: list[MissingGate] = list(_ALWAYS_OUTSTANDING_GATES)
    has_backup = asset.has_reference(EvidenceReferenceKind.BACKUP)
    has_restore = asset.has_reference(EvidenceReferenceKind.RESTORE)
    has_stability = asset.has_reference(EvidenceReferenceKind.STABILITY)
    if not has_backup:
        missing_gates.append(MissingGate.BACKUP_EVIDENCE_ABSENT)
    if not has_restore:
        missing_gates.append(MissingGate.RESTORE_EVIDENCE_ABSENT)
    if not has_stability:
        missing_gates.append(MissingGate.STABILITY_EVIDENCE_ABSENT)

    action = (
        RetirementAction.RETAIN_FOR_ROLLBACK
        if has_backup and has_restore and has_stability
        else RetirementAction.DISABLE_LATER
    )
    return AssetDisposition(
        asset_id=asset.asset_id,
        classification=asset.classification,
        action=action,
        protected=False,
        protection_reasons=(),
        missing_gates=tuple(missing_gates),
    )


def build_retirement_dry_run(
    assets: tuple[InventoryAsset, ...], *, generated_at: str
) -> RetirementDryRunReport:
    """Build the one always-dry-run disposition list over a declared inventory.

    Refuses a duplicate ``asset_id``. Never opens, fetches or verifies an
    evidence reference; a populated reference only removes the corresponding
    ``*_EVIDENCE_ABSENT`` gate from a non-protected asset's report, and the
    four gates this module can never itself close stay listed regardless.

    Every asset is revalidated on entry, guarding this public boundary against
    a ``model_construct``-built or mutated forgery. ``inventory_digest`` hashes
    each asset's *complete* canonical payload — scope, classification,
    consumers and evidence references included, not just its identity and
    configuration — so swapping any one of them, including a single evidence
    reference, changes both the inventory digest and the report seal even when
    the resulting disposition is unchanged.
    """
    assets = tuple(_revalidated_asset(asset) for asset in assets)
    asset_ids = [asset.asset_id for asset in assets]
    if len(set(asset_ids)) != len(asset_ids):
        raise LabMigrationViolation(
            "duplicate asset id in inventory", detail={"asset_ids": asset_ids}
        )

    dispositions = tuple(_disposition(asset) for asset in assets)
    inventory_digest = seal(
        INVENTORY_SEAL_DOMAIN,
        [asset.canonical_payload() for asset in sorted(assets, key=lambda item: item.asset_id)],
    )
    body: dict[str, object] = {
        "contract_version": LAB_MIGRATION_CONTRACT_VERSION,
        "inventory_digest": inventory_digest,
        "generated_at": generated_at,
        "dispositions": [item.canonical_payload() for item in dispositions],
        "dry_run": True,
        "requires_external_verification": True,
        "requires_external_authorization": True,
        "activation_token": False,
    }
    return RetirementDryRunReport(
        contract_version=LAB_MIGRATION_CONTRACT_VERSION,
        inventory_digest=inventory_digest,
        generated_at=generated_at,
        dispositions=dispositions,
        report_seal=seal(REPORT_SEAL_DOMAIN, body),
    )

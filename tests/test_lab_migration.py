"""HOK-805/806 — the retirement dry-run never authorizes, applies or deletes."""

from __future__ import annotations

import pytest

from latent_compass.errors import LatentCompassError
from latent_compass.lab.migration import (
    AssetClassification,
    AssetConsumer,
    InventoryAsset,
    LabMigrationViolation,
    MissingGate,
    ProtectionReason,
    RetirementAction,
    ScopeKind,
    admit_inventory_asset,
    build_retirement_dry_run,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REFERENCED_AT = "2026-09-01T00:00:00Z"
GENERATED_AT = "2026-09-10T00:00:00Z"


def asset_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "asset_id": "asset-one",
        "provider_id": "provider-one",
        "role": "candidate-index",
        "scope": "lab-scope",
        "scope_kind": "PERSONAL_LAB",
        "host_id": "lab-host",
        "configuration_digest": DIGEST_A,
        "consumers": [{"consumer_id": "consumer-one"}],
        "classification": "CANDIDATE_INDEX",
        "evidence_references": [],
    }
    payload.update(overrides)
    return payload


def admitted_asset(**overrides: object) -> InventoryAsset:
    return admit_inventory_asset(asset_payload(**overrides))


def evidence(kind: str, digest: str = DIGEST_A) -> dict[str, object]:
    return {"kind": kind, "digest": digest, "referenced_at": REFERENCED_AT}


@pytest.mark.parametrize(
    "classification", ["SOURCE_OR_SYMBOLIC_TOOL", "AUTHORED_DATA", "VAULT", "UNKNOWN"]
)
def test_a_protected_classification_asset_stays_protected_and_kept(classification: str) -> None:
    asset = admitted_asset(classification=classification)
    report = build_retirement_dry_run((asset,), generated_at=GENERATED_AT)
    disposition = report.dispositions[0]
    assert disposition.protected is True
    assert disposition.action is RetirementAction.KEEP
    assert ProtectionReason.CLASSIFICATION_PROTECTED in disposition.protection_reasons
    assert disposition.missing_gates == ()


def test_a_professional_scope_asset_stays_protected_regardless_of_classification() -> None:
    asset = admitted_asset(scope_kind="PROFESSIONAL")
    report = build_retirement_dry_run((asset,), generated_at=GENERATED_AT)
    disposition = report.dispositions[0]
    assert disposition.protected is True
    assert ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED in disposition.protection_reasons


def test_an_unknown_consumers_asset_stays_protected() -> None:
    asset = admitted_asset(consumers=[])
    report = build_retirement_dry_run((asset,), generated_at=GENERATED_AT)
    disposition = report.dispositions[0]
    assert disposition.protected is True
    assert ProtectionReason.CONSUMERS_UNKNOWN in disposition.protection_reasons


def test_an_eligible_code_index_asset_with_full_evidence_is_still_dry_run() -> None:
    """Both halves: a fully-evidenced eligible asset is retained for rollback,
    never applied — proving fabricated complete evidence never authorizes."""
    asset = admitted_asset(
        evidence_references=[evidence("BACKUP"), evidence("RESTORE"), evidence("STABILITY")]
    )
    report = build_retirement_dry_run((asset,), generated_at=GENERATED_AT)
    disposition = report.dispositions[0]
    assert disposition.protected is False
    assert disposition.action is RetirementAction.RETAIN_FOR_ROLLBACK
    assert MissingGate.BACKUP_EVIDENCE_ABSENT not in disposition.missing_gates
    assert MissingGate.RESTORE_EVIDENCE_ABSENT not in disposition.missing_gates
    assert MissingGate.STABILITY_EVIDENCE_ABSENT not in disposition.missing_gates
    # The four gates this module can never itself close stay listed regardless.
    assert MissingGate.INDEPENDENT_COMPARISON_ABSENT in disposition.missing_gates
    assert MissingGate.LIVE_PILOT_ABSENT in disposition.missing_gates
    assert MissingGate.EXTERNAL_VERIFICATION_ABSENT in disposition.missing_gates
    assert MissingGate.OWNER_DELETION_AUTHORITY_ABSENT in disposition.missing_gates
    assert report.dry_run is True
    assert report.requires_external_verification is True
    assert report.requires_external_authorization is True
    assert report.activation_token is False


def test_an_eligible_asset_missing_evidence_is_disabled_later_not_retained() -> None:
    asset = admitted_asset()
    report = build_retirement_dry_run((asset,), generated_at=GENERATED_AT)
    disposition = report.dispositions[0]
    assert disposition.protected is False
    assert disposition.action is RetirementAction.DISABLE_LATER
    assert MissingGate.BACKUP_EVIDENCE_ABSENT in disposition.missing_gates
    assert MissingGate.RESTORE_EVIDENCE_ABSENT in disposition.missing_gates
    assert MissingGate.STABILITY_EVIDENCE_ABSENT in disposition.missing_gates


def test_an_injected_approval_field_is_refused() -> None:
    with pytest.raises(LatentCompassError):
        admit_inventory_asset(asset_payload(approved=True))


def test_swapping_a_backup_evidence_digest_changes_inventory_and_report_seals() -> None:
    asset_a = admitted_asset(
        evidence_references=[
            evidence("BACKUP", DIGEST_A),
            evidence("RESTORE"),
            evidence("STABILITY"),
        ]
    )
    asset_b = admitted_asset(
        evidence_references=[
            evidence("BACKUP", DIGEST_B),
            evidence("RESTORE"),
            evidence("STABILITY"),
        ]
    )
    report_a = build_retirement_dry_run((asset_a,), generated_at=GENERATED_AT)
    report_b = build_retirement_dry_run((asset_b,), generated_at=GENERATED_AT)
    # The disposition itself is unchanged...
    assert report_a.dispositions[0].action == report_b.dispositions[0].action
    # ...but the provenance the seals bind to is not.
    assert report_a.inventory_digest != report_b.inventory_digest
    assert report_a.report_seal != report_b.report_seal


def test_build_retirement_dry_run_revalidates_a_model_construct_forged_asset() -> None:
    forged = InventoryAsset.model_construct(
        contract_version="1.0.0",
        asset_id="asset-one",
        provider_id="provider-one",
        role="candidate-index",
        scope="lab-scope",
        scope_kind=ScopeKind.PERSONAL_LAB,
        host_id="lab-host",
        configuration_digest=DIGEST_A,
        consumers=(
            AssetConsumer(consumer_id="consumer-one"),
            AssetConsumer(consumer_id="consumer-one"),  # duplicate, bypasses admission
        ),
        classification=AssetClassification.CANDIDATE_INDEX,
        evidence_references=(),
    )
    with pytest.raises(LabMigrationViolation):
        build_retirement_dry_run((forged,), generated_at=GENERATED_AT)


def test_a_duplicate_asset_id_is_refused() -> None:
    asset = admitted_asset()
    with pytest.raises(LabMigrationViolation):
        build_retirement_dry_run((asset, asset), generated_at=GENERATED_AT)

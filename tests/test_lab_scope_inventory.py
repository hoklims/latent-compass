"""HOK-799 — the committed scope inventory is bound to the dry-run contract.

The inventory under ``evidence/hok799-scope-inventory`` is a read-only
observation of one personal workstation. These tests prove that it still
admits through the ``1.0.0`` migration contract, that the committed dry-run
reproduces from it seal for seal, that only the named pilot perimeter is ever
eligible for anything other than ``KEEP``, and that no private host detail
travels with it. They prove nothing about whether any index is useful.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from latent_compass.contracts import validate_contract
from latent_compass.lab.migration import (
    AssetClassification,
    InventoryAsset,
    LabMigrationViolation,
    MissingGate,
    ProtectionReason,
    RetirementAction,
    RetirementDryRunReport,
    ScopeKind,
    admit_inventory_asset,
    build_retirement_dry_run,
)

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "evidence" / "hok799-scope-inventory"
SCOPE_DOCUMENT = REPO / "docs" / "active-diagnosis-scope.md"

PILOT_SCOPE = "pilot-latent-compass"
PILOT_ASSETS = {
    "graphify-code-graph.pilot-worktrees",
    "graphify-worktree-cache.pilot-worktrees",
}
ALL_GATES = set(MissingGate)


def raw_inventory() -> list[dict[str, object]]:
    loaded = json.loads((EVIDENCE / "inventory.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded


def inventory() -> tuple[InventoryAsset, ...]:
    return tuple(admit_inventory_asset(item) for item in raw_inventory())


def committed_report() -> RetirementDryRunReport:
    payload = json.loads((EVIDENCE / "dry-run-report.json").read_text(encoding="utf-8"))
    # Strict JSON validation also recomputes the report seal from its own body.
    return validate_contract(
        RetirementDryRunReport,
        payload,
        error=LabMigrationViolation,
        context="committed retirement dry-run report",
    )


def test_the_inventory_is_not_vacuous_and_every_asset_admits() -> None:
    assets = inventory()
    assert len(assets) == 25
    assert len({asset.asset_id for asset in assets}) == 25
    # One operator-chosen label, never a hostname, account or address.
    assert {asset.host_id for asset in assets} == {"personal-workstation"}


def test_the_committed_dry_run_reproduces_from_the_committed_inventory() -> None:
    committed = committed_report()
    rebuilt = build_retirement_dry_run(inventory(), generated_at=committed.generated_at)

    assert rebuilt.canonical_payload() == committed.canonical_payload()
    assert rebuilt.report_seal == committed.report_seal
    assert rebuilt.inventory_digest == committed.inventory_digest


def test_only_the_named_pilot_perimeter_is_ever_eligible_for_more_than_keep() -> None:
    dispositions = {item.asset_id: item for item in committed_report().dispositions}
    not_kept = {
        asset_id
        for asset_id, item in dispositions.items()
        if item.action is not RetirementAction.KEEP
    }

    assert not_kept == PILOT_ASSETS
    for asset_id in PILOT_ASSETS:
        item = dispositions[asset_id]
        # No evidence exists yet, so nothing is retained for rollback and
        # every gate, closable or not, is still listed as missing.
        assert item.action is RetirementAction.DISABLE_LATER
        assert not item.protected
        assert set(item.missing_gates) == ALL_GATES
        assert len(item.missing_gates) == len(ALL_GATES)


def test_the_personal_lab_scope_kind_names_the_pilot_perimeter_and_nothing_else() -> None:
    assets = inventory()
    lab_assets = {asset.asset_id for asset in assets if asset.scope_kind is ScopeKind.PERSONAL_LAB}
    pilot_scoped = {asset.asset_id for asset in assets if asset.scope == PILOT_SCOPE}

    assert lab_assets == PILOT_ASSETS
    assert pilot_scoped == PILOT_ASSETS


def test_every_asset_outside_the_pilot_perimeter_is_protected_for_a_stated_reason() -> None:
    assets = {asset.asset_id: asset for asset in inventory()}
    dispositions = {item.asset_id: item for item in committed_report().dispositions}
    assert set(assets) == set(dispositions)

    outside = set(assets) - PILOT_ASSETS
    assert len(outside) == 23
    for asset_id in outside:
        item = dispositions[asset_id]
        assert item.action is RetirementAction.KEEP, asset_id
        assert item.protected, asset_id
        assert ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED in item.protection_reasons
        assert not item.missing_gates, asset_id


def test_professional_vault_and_ambiguous_assets_are_each_present_and_kept() -> None:
    assets = inventory()
    dispositions = {item.asset_id: item for item in committed_report().dispositions}

    professional = [asset for asset in assets if asset.scope_kind is ScopeKind.PROFESSIONAL]
    vault = [asset for asset in assets if asset.classification is AssetClassification.VAULT]
    ambiguous = [asset for asset in assets if asset.classification is AssetClassification.UNKNOWN]
    unknown_consumers = [asset for asset in assets if not asset.consumers]
    assert len(professional) == 2
    assert len(vault) == 2
    assert len(ambiguous) == 3
    assert len(unknown_consumers) == 2

    for asset in (*vault, *ambiguous):
        reasons = dispositions[asset.asset_id].protection_reasons
        assert ProtectionReason.CLASSIFICATION_PROTECTED in reasons, asset.asset_id
    for asset in unknown_consumers:
        reasons = dispositions[asset.asset_id].protection_reasons
        assert ProtectionReason.CONSUMERS_UNKNOWN in reasons, asset.asset_id


@pytest.mark.parametrize("asset_id", ["professional.worktree-artefacts", "vault-graphify.graph"])
def test_the_scope_field_is_what_protects_an_excluded_asset(asset_id: str) -> None:
    """Negative witness: relabel one excluded asset into the pilot scope kind.

    The committed professional asset is kept; the same payload declared as
    ``PERSONAL_LAB`` with a candidate classification is not. The protection is
    therefore carried by what the inventory declares, which is exactly why
    the two tests above pin those declarations to exact sets.
    """
    payload = next(item for item in raw_inventory() if item["asset_id"] == asset_id)
    committed = build_retirement_dry_run(
        (admit_inventory_asset(payload),), generated_at="2026-09-19T00:00:00Z"
    )
    assert committed.dispositions[0].action is RetirementAction.KEEP

    relabelled = dict(payload, scope_kind="PERSONAL_LAB", classification="CANDIDATE_INDEX")
    forged = build_retirement_dry_run(
        (admit_inventory_asset(relabelled),), generated_at="2026-09-19T00:00:00Z"
    )
    assert forged.dispositions[0].action is RetirementAction.DISABLE_LATER


def test_no_private_host_detail_travels_with_the_committed_evidence() -> None:
    # Assembled at runtime so this module does not contain what it forbids.
    needles = (
        ":" + "\\",
        ":" + "/",
        "\\" + "users",
        "/" + "users/",
        "app" + "data",
        "@",
    )
    scanned = 0
    for path in sorted(EVIDENCE.glob("*.json")):
        text = path.read_text(encoding="utf-8").lower()
        scanned += 1
        for needle in needles:
            assert needle not in text, f"{path.name} leaks {needle!r}"
    assert scanned == 2


def test_the_scope_document_names_every_inventoried_asset() -> None:
    text = SCOPE_DOCUMENT.read_text(encoding="utf-8")
    missing = [asset.asset_id for asset in inventory() if f"`{asset.asset_id}`" not in text]
    assert not missing, f"assets absent from the scope document: {missing}"
    committed = committed_report()
    assert committed.inventory_digest in text
    assert committed.report_seal in text

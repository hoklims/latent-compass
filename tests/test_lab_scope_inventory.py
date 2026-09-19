"""HOK-799 — the committed scope inventory is bound to the dry-run contract.

Three revisions are committed. ``evidence/hok799-scope-inventory`` is the first
read and stays exactly as committed, defects included: it declared three
never-assessed assets as index candidates, and its report carries a placeholder
instant later than its own commit. ``evidence/hok799-scope-inventory-v1.1.0``
corrects both. ``evidence/hok799-scope-inventory-v1.2.0`` carries the owner's
U1 decision: it appends the index artefacts of a second personal repository,
under an alias, and changes nothing else. It is the revision every other test
here reads.

These tests prove that each revision still reproduces seal for seal and is never
edited in place, that only the two named pilot perimeters are ever eligible for
anything other than ``KEEP``, which assets hold by one lock and which by two,
and that the vocabulary travelling with the evidence is exactly the declared
one. They prove nothing about whether any index is useful.
"""

from __future__ import annotations

import hashlib
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
ORIGINAL = "hok799-scope-inventory"
CORRECTED = "hok799-scope-inventory-v1.1.0"
CURRENT = "hok799-scope-inventory-v1.2.0"
SCENARIOS = REPO / "examples" / "lab-scenarios"
SCOPE_DOCUMENT = REPO / "docs" / "active-diagnosis-scope.md"
ADR = REPO / "docs" / "adr" / "0011-experimental-active-diagnosis.md"
#: Every section of the ADR, pinned. An amendment is appended and pinned here in
#: the same change; an edit to any existing section, the decision included, is red.
ADR_SECTIONS = {
    "decision": "sha256:a4aabe9d8928f4b577b3fb4d251aa506a52bae5f22c6800e811b2718b07478c9",
    "Amendment 2026-09-19 — operational scope (HOK-799)": (
        "sha256:488e0521ee88283a2bf36cdef804b5cb39bd4b60d9f8024bdcb991bfe0f8368e"
    ),
    "Amendment 2026-09-19 — unobtainable probes (HOK-802)": (
        "sha256:6694cbff7ed758a35a2664e3a545b3bd527f874c3a52ed0ddf606ee72724fbf3"
    ),
    "Amendment 2026-09-19 — a second pilot repository (HOK-799, U1)": (
        "sha256:ba30f07452dcd307d4ceb84e2dcd45aa0e9c9c984623d472d215406c0bc3724b"
    ),
}

#: (generated_at, inventory_digest, report_seal) of every committed revision.
PINNED = {
    ORIGINAL: (
        "2026-09-19T00:00:00Z",
        "sha256:e139cfb224264d341c6206a21d9b444d804aeb17deb955e3acb97adf575a0fea",
        "sha256:853172528179fafafba2616a7ca128c79d0405c43c7e8fb443ff8b71211f3249",
    ),
    CORRECTED: (
        "2026-09-18T23:47:51Z",
        "sha256:b7d494805b10ad1b2f08151de84bdd8ecd36371bb06bb95f2f16982cb6e38887",
        "sha256:3e2e819e274144f1bf736f71a86cc35e946c412e9f59792c37517e95fb7fcb31",
    ),
    CURRENT: (
        "2026-09-19T12:57:30Z",
        "sha256:8b0468fc231cd5f6720a2a688c4e08b0cf3a9ee0325144d27096a24640d17a67",
        "sha256:df4c53db79580da7f186e0fc9ef4aeaa9d9efc7d0f9dd70145b6ad586f9121a9",
    ),
}

PILOT_SCOPE = "pilot-latent-compass"
PILOT_ASSETS = {
    "graphify-code-graph.pilot-worktrees",
    "graphify-worktree-cache.pilot-worktrees",
}
#: The owner's U1 decision of 2026-09-19. The repository travels under this alias; the
#: alias-to-path mapping is operator-held.
SECOND_PILOT_SCOPE = "pilot-second-repository"
#: Its index/usage couples: what the decision made eligible for more than ``KEEP``.
SECOND_PILOT_CANDIDATES = {
    "ccc-semantic-index.pilot-second-repository",
    "graphify-code-graph.pilot-second-repository",
    "graphify-worktree-cache.pilot-second-repository",
}
#: In that perimeter too, and never candidates: their classification is their one lock.
SECOND_PILOT_KEPT = {
    "semctx-semantic-layer.pilot-second-repository",
    "serena-symbolic-cache.pilot-second-repository",
}
EVERY_PILOT_ASSET = PILOT_ASSETS | SECOND_PILOT_CANDIDATES | SECOND_PILOT_KEPT
#: Recorded but never assessed, so never declared to be index candidates.
NOT_ASSESSED = {
    "personal-unassessed.worktree-artefacts",
    "professional.code-index-servers",
    "professional.worktree-artefacts",
}
#: Identified workstation-wide index machinery: its scope is its only lock.
SINGLY_LOCKED = {
    "ccc-semantic-index.workstation",
    "claude.hook.index-mark-dirty",
    "claude.hook.index-refresh",
    "claude.hook.index-routing",
    "claude.store.host-receipts",
    "codex.hook.index-routing",
    "codex.store.host-receipts",
    "shared.store.control-plane",
    "shared.task.index-control-sweep",
    "shared.worker.reconcile",
}
DECLARED_SCOPES = {
    "personal-unassessed",
    "personal-vault",
    "pilot-latent-compass",
    "professional-excluded",
    "workstation-shared",
}
DECLARED_PROVIDERS = {
    "cocoindex-code",
    "graphify",
    "index-control-plane",
    "mixed-index-providers",
    "native-lsp",
    "semctx",
    "serena",
    "vault-semantic",
}
DECLARED_CONSUMERS = {
    "claude.agent-sessions",
    "claude.agent.explorer",
    "claude.agent.skeptic",
    "claude.hook.index-mark-dirty",
    "claude.hook.index-refresh",
    "claude.hook.index-routing",
    "claude.hook.vault-semantic-refresh",
    "claude.mcp.code-intelligence",
    "claude.mcp.vault-graphify",
    "claude.mcp.vault-semantic",
    "claude.plugin.semctx",
    "claude.skill.ccc",
    "claude.skill.graphify",
    "claude.skill.vault-search-cascade",
    "codex.agent-sessions",
    "codex.hook.harness-adapter",
    "codex.hook.index-routing",
    "codex.mcp.code-intelligence",
    "codex.mcp.graphify",
    "codex.plugin.semctx-control",
    "professional.agent-sessions",
    "shared.skill.index-control-plane",
    "shared.store.control-plane",
    "shared.task.index-control-sweep",
    "shared.worker.reconcile",
}
ALL_GATES = set(MissingGate)
INSTANT = "2026-09-18T23:47:51Z"


def raw_inventory(directory: str = CURRENT) -> list[dict[str, object]]:
    path = REPO / "evidence" / directory / "inventory.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded


def inventory(directory: str = CURRENT) -> tuple[InventoryAsset, ...]:
    return tuple(admit_inventory_asset(item) for item in raw_inventory(directory))


def committed_report(directory: str = CURRENT) -> RetirementDryRunReport:
    path = REPO / "evidence" / directory / "dry-run-report.json"
    # Strict JSON validation also recomputes the report seal from its own body.
    return validate_contract(
        RetirementDryRunReport,
        json.loads(path.read_text(encoding="utf-8")),
        error=LabMigrationViolation,
        context="committed retirement dry-run report",
    )


def action_of(payload: dict[str, object]) -> RetirementAction:
    report = build_retirement_dry_run((admit_inventory_asset(payload),), generated_at=INSTANT)
    return report.dispositions[0].action


def test_the_inventory_is_not_vacuous_and_every_asset_admits() -> None:
    assets = inventory()
    assert len(assets) == 30
    assert len({asset.asset_id for asset in assets}) == 30
    # One operator-chosen label, never a hostname, account or address.
    assert {asset.host_id for asset in assets} == {"personal-workstation"}


@pytest.mark.parametrize("directory", sorted(PINNED))
def test_a_committed_revision_reproduces_and_is_never_edited_in_place(directory: str) -> None:
    committed = committed_report(directory)
    rebuilt = build_retirement_dry_run(inventory(directory), generated_at=committed.generated_at)

    assert rebuilt.canonical_payload() == committed.canonical_payload()
    # A correction is a new revision beside the old one; these three values are
    # what an in-place edit of either file would have to change.
    assert (
        committed.generated_at,
        committed.inventory_digest,
        committed.report_seal,
    ) == PINNED[directory]


def test_the_revision_changes_three_unassessed_classifications_and_nothing_else() -> None:
    original, corrected = raw_inventory(ORIGINAL), raw_inventory(CORRECTED)
    assert [item["asset_id"] for item in original] == [item["asset_id"] for item in corrected]

    differences = {
        (before["asset_id"], key): (before[key], after[key])
        for before, after in zip(original, corrected, strict=True)
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    assert differences == {
        (asset_id, "classification"): ("CANDIDATE_INDEX", "UNKNOWN") for asset_id in NOT_ASSESSED
    }


def test_the_second_pilot_revision_appends_five_assets_and_changes_nothing_else() -> None:
    corrected, current = raw_inventory(CORRECTED), raw_inventory(CURRENT)
    assert len(corrected) == 25

    # The earlier read is carried over asset for asset, in the same order.
    assert current[: len(corrected)] == corrected
    appended = current[len(corrected) :]
    assert {item["asset_id"] for item in appended} == SECOND_PILOT_CANDIDATES | SECOND_PILOT_KEPT
    assert len(appended) == 5
    # The decision is about one repository: every appended asset is scoped to it and to it
    # alone, and nothing that was read before moved into it.
    assert {(item["scope"], item["scope_kind"]) for item in appended} == {
        (SECOND_PILOT_SCOPE, "PERSONAL_LAB")
    }
    assert not [item["asset_id"] for item in corrected if item["scope"] == SECOND_PILOT_SCOPE]
    assert {item["classification"] for item in appended} == {
        "CANDIDATE_INDEX",
        "REQUIRED_EVIDENCE",
        "SOURCE_OR_SYMBOLIC_TOOL",
    }


def test_only_the_named_pilot_perimeters_are_ever_eligible_for_more_than_keep() -> None:
    dispositions = {item.asset_id: item for item in committed_report().dispositions}
    not_kept = {
        asset_id
        for asset_id, item in dispositions.items()
        if item.action is not RetirementAction.KEEP
    }

    assert not_kept == PILOT_ASSETS | SECOND_PILOT_CANDIDATES
    # Before the owner's decision the first pilot stood alone.
    assert {
        item.asset_id
        for item in committed_report(CORRECTED).dispositions
        if item.action is not RetirementAction.KEEP
    } == PILOT_ASSETS
    for asset_id in PILOT_ASSETS | SECOND_PILOT_CANDIDATES:
        item = dispositions[asset_id]
        # No evidence exists yet, so nothing is retained for rollback and
        # every gate, closable or not, is still listed as missing.
        assert item.action is RetirementAction.DISABLE_LATER
        assert not item.protected
        assert set(item.missing_gates) == ALL_GATES
        assert len(item.missing_gates) == len(ALL_GATES)


def test_the_personal_lab_scope_kind_names_the_two_pilot_perimeters_and_nothing_else() -> None:
    assets = inventory()
    lab_assets = {asset.asset_id for asset in assets if asset.scope_kind is ScopeKind.PERSONAL_LAB}
    by_scope = {
        scope: {asset.asset_id for asset in assets if asset.scope == scope}
        for scope in (PILOT_SCOPE, SECOND_PILOT_SCOPE)
    }

    assert lab_assets == EVERY_PILOT_ASSET
    assert by_scope == {
        PILOT_SCOPE: PILOT_ASSETS,
        SECOND_PILOT_SCOPE: SECOND_PILOT_CANDIDATES | SECOND_PILOT_KEPT,
    }


def test_inside_the_second_pilot_a_kept_asset_holds_by_its_classification_alone() -> None:
    dispositions = {item.asset_id: item for item in committed_report().dispositions}
    raw = {str(item["asset_id"]): item for item in raw_inventory()}

    for asset_id in SECOND_PILOT_KEPT:
        item = dispositions[asset_id]
        assert item.action is RetirementAction.KEEP, asset_id
        # One lock, and the record says which: joining a pilot removes the scope lock.
        assert item.protection_reasons == (ProtectionReason.CLASSIFICATION_PROTECTED,), asset_id
        assert not item.missing_gates, asset_id
        # Negative witness, one variable: that classification is what holds it.
        forged = dict(raw[asset_id], classification="CANDIDATE_INDEX")
        assert action_of(forged) is RetirementAction.DISABLE_LATER, asset_id
    for asset_id in SECOND_PILOT_CANDIDATES:
        # The other half: what the owner's decision moved is the scope, and only the scope.
        assert action_of(raw[asset_id]) is RetirementAction.DISABLE_LATER, asset_id
        assert action_of(dict(raw[asset_id], scope_kind="UNSCOPED")) is RetirementAction.KEEP


def test_every_asset_outside_the_pilot_perimeters_is_protected_for_a_stated_reason() -> None:
    assets = {asset.asset_id: asset for asset in inventory()}
    dispositions = {item.asset_id: item for item in committed_report().dispositions}
    assert set(assets) == set(dispositions)

    outside = set(assets) - EVERY_PILOT_ASSET
    assert len(outside) == 23
    for asset_id in outside:
        item = dispositions[asset_id]
        assert item.action is RetirementAction.KEEP, asset_id
        assert item.protected, asset_id
        assert ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED in item.protection_reasons
        assert not item.missing_gates, asset_id


def test_professional_vault_and_unassessed_assets_are_each_kept_by_two_locks() -> None:
    assets = inventory()
    dispositions = {item.asset_id: item for item in committed_report().dispositions}

    professional = {a.asset_id for a in assets if a.scope_kind is ScopeKind.PROFESSIONAL}
    vault = {a.asset_id for a in assets if a.classification is AssetClassification.VAULT}
    ambiguous = {a.asset_id for a in assets if a.classification is AssetClassification.UNKNOWN}
    unknown_consumers = {a.asset_id for a in assets if not a.consumers}
    assert (len(professional), len(vault), len(ambiguous), len(unknown_consumers)) == (2, 2, 6, 2)
    assert professional < NOT_ASSESSED <= ambiguous

    for asset_id in professional | vault | ambiguous:
        reasons = set(dispositions[asset_id].protection_reasons)
        assert {
            ProtectionReason.CLASSIFICATION_PROTECTED,
            ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED,
        } <= reasons, asset_id
    for asset_id in unknown_consumers:
        assert ProtectionReason.CONSUMERS_UNKNOWN in dispositions[asset_id].protection_reasons


def test_the_assets_held_by_their_scope_alone_are_exactly_the_identified_index_machinery() -> None:
    assets = {asset.asset_id: asset for asset in inventory()}
    singly_locked = {
        item.asset_id
        for item in committed_report().dispositions
        if item.protection_reasons == (ProtectionReason.SCOPE_PROFESSIONAL_OR_UNSCOPED,)
    }

    assert singly_locked == SINGLY_LOCKED
    for asset_id in singly_locked:
        assert assets[asset_id].scope_kind is ScopeKind.UNSCOPED, asset_id
        assert assets[asset_id].classification is AssetClassification.CANDIDATE_INDEX, asset_id


@pytest.mark.parametrize(
    ("asset_id", "after_rescoping"),
    [
        # Two locks: the classification still protects once the scope is forged.
        ("professional.code-index-servers", RetirementAction.KEEP),
        ("professional.worktree-artefacts", RetirementAction.KEEP),
        ("personal-unassessed.worktree-artefacts", RetirementAction.KEEP),
        ("vault-graphify.graph", RetirementAction.KEEP),
        # One lock: an identified index that only its declared scope protects.
        ("ccc-semantic-index.workstation", RetirementAction.DISABLE_LATER),
        ("shared.store.control-plane", RetirementAction.DISABLE_LATER),
    ],
)
def test_forging_the_scope_alone_moves_only_an_asset_held_by_one_lock(
    asset_id: str, after_rescoping: RetirementAction
) -> None:
    """Negative witness, one variable at a time: only ``scope_kind`` changes."""
    payload = next(item for item in raw_inventory() if item["asset_id"] == asset_id)
    assert action_of(payload) is RetirementAction.KEEP
    assert action_of(dict(payload, scope_kind="PERSONAL_LAB")) is after_rescoping


@pytest.mark.parametrize(
    "asset_id",
    ["professional.worktree-artefacts", "vault-graphify.graph", "codex.hook.harness-adapter"],
)
def test_forging_the_classification_alone_never_moves_an_excluded_asset(asset_id: str) -> None:
    """The other variable alone: the declared scope still protects."""
    payload = next(item for item in raw_inventory() if item["asset_id"] == asset_id)
    assert payload["classification"] != "CANDIDATE_INDEX"
    assert action_of(dict(payload, classification="CANDIDATE_INDEX")) is RetirementAction.KEEP


def test_the_vocabulary_travelling_with_the_evidence_is_exactly_the_declared_one() -> None:
    # Every text field is an identifier, which cannot hold a path separator: a
    # needle scan is inert here. What an identifier can hold is a hostname, an
    # account, an employer or a private repository name, so each vocabulary is
    # pinned to an exact set that a newcomer has to be added to by hand.
    for directory in PINNED:
        assets = inventory(directory)
        assert assets, directory
        # The second pilot's alias is the one newcomer, and only from the revision that
        # carries the owner's decision. It names no repository.
        newcomers = {SECOND_PILOT_SCOPE} if directory == CURRENT else set()
        assert {asset.scope for asset in assets} == DECLARED_SCOPES | newcomers, directory
        assert {asset.provider_id for asset in assets} == DECLARED_PROVIDERS
        assert {
            consumer.consumer_id for asset in assets for consumer in asset.consumers
        } == DECLARED_CONSUMERS


def test_no_private_host_detail_travels_with_the_free_text_fixtures() -> None:
    # The scenario fixtures carry free text, where these needles can really fire.
    # Assembled at runtime so this module does not contain what it forbids.
    needles = (
        ":" + "\\",
        "\\" + "users",
        "/" + "users/",
        "/" + "home/",
        "app" + "data",
        "@",
    )
    paths = sorted(SCENARIOS.glob("*.json")) + sorted(REPO.glob("evidence/hok799-*/*.json"))
    assert len(paths) == 11
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            assert needle not in text, f"{path.name} leaks {needle!r}"


def test_the_adr_is_append_only_and_every_section_is_pinned() -> None:
    text = ADR.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    decision, *amendments = text.split("\n## Amendment ")
    sections = {"decision": decision}
    for body in amendments:
        sections["Amendment " + body.split("\n", 1)[0]] = "\n## Amendment " + body

    digests = {
        name: "sha256:" + hashlib.sha256(section.encode("utf-8")).hexdigest()
        for name, section in sections.items()
    }
    assert digests == ADR_SECTIONS
    # Each amendment names the evidence it rests on; the revision carrying the owner's U1
    # decision is named by the amendment that records it, and by no earlier one.
    for directory in (ORIGINAL, CORRECTED):
        assert f"evidence/{directory}/" in amendments[0]
    assert f"evidence/{CURRENT}/" in amendments[-1]
    assert f"evidence/{CURRENT}/" not in "".join(amendments[:-1])
    assert f"`{SECOND_PILOT_SCOPE}`" in amendments[-1]


def test_the_scope_document_names_every_asset_and_every_revision() -> None:
    text = SCOPE_DOCUMENT.read_text(encoding="utf-8")
    assets = inventory()
    assert assets
    missing = [asset.asset_id for asset in assets if f"`{asset.asset_id}`" not in text]
    assert not missing, f"assets absent from the scope document: {missing}"
    for directory, (generated_at, inventory_digest, report_seal) in PINNED.items():
        assert f"evidence/{directory}/" in text
        assert generated_at in text
        assert inventory_digest in text
        assert report_seal in text

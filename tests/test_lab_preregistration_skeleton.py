"""HOK-804 — the preregistration skeleton stays complete, and records only what was decided.

``docs/active-diagnosis-preregistration.md`` lists what the repository owner has to decide
before the first trial. These tests check that it names every field the sealed protocol
holds, every arm, every verdict and every candidate couple of the committed inventory, so
that a contract or inventory change cannot leave it silently incomplete; and that every
decision is either ``OPEN`` or recorded exactly as the owner took it, with its date. They
prove nothing about the experiment, which has not run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from latent_compass.lab.evaluation import Arm, LabEvaluationProtocol

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "docs" / "active-diagnosis-preregistration.md"
CURRENT = REPO / "evidence" / "hok799-scope-inventory-v1.2.0"
CURRENT_REPORT = CURRENT / "dry-run-report.json"
CURRENT_INVENTORY = CURRENT / "inventory.json"
VERDICTS = ("RETIRABLE_EN_PILOTE", "CONSERVER", "PREUVE_INSUFFISANTE")

# The one decision that is not a table row.
STANDALONE = "One protocol, or several"
# Every decision the repository owner has taken, exactly as the document records it. A new
# decision is a new entry here in the same diff: the author cannot fill a row silently, and
# a recorded decision cannot be reworded silently.
RECORDED = {
    STANDALONE: (
        "**grouped first, then per couple only where the grouped result shows a regression**"
        " — 2026-09-19, repository owner, on the author's proposal"
    ),
}
# A record holds the value, its date and who took it. Only the repository owner decides.
RECORD = re.compile(
    r"\*\*[^*|]+\*\* — \d{4}-\d{2}-\d{2}, repository owner(, on the author's proposal)?"
)


def skeleton() -> str:
    return SKELETON.read_text(encoding="utf-8")


def candidate_couples() -> list[str]:
    report = json.loads(CURRENT_REPORT.read_text(encoding="utf-8"))
    return [item["asset_id"] for item in report["dispositions"] if item["action"] != "KEEP"]


def decisions() -> dict[str, str]:
    """Every decision of the document, by name, with what its "Decided" cell holds."""
    text = skeleton()
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("|")
    ]
    # Every decision table ends with a "Decided" column.
    assert len([row for row in rows if row[-1] == "Decided"]) == 2
    table = [
        row for row in rows if len(row) == 4 and row[-1] != "Decided" and set(row[-1]) != {"-"}
    ]
    found = {row[0]: row[-1] for row in table}
    assert len(found) == len(table) == 28
    paragraphs = [" ".join(block.split()) for block in text.split("\n\n")]
    standalone = [block for block in paragraphs if block.startswith("Decided: ")]
    assert len(standalone) == 1
    found[STANDALONE] = standalone[0].removeprefix("Decided: ").removesuffix(".")
    assert len(found) == 29
    return found


def test_the_skeleton_names_every_field_of_the_sealed_protocol() -> None:
    text = skeleton()
    fields = list(LabEvaluationProtocol.model_fields)
    assert len(fields) == 17
    missing = [name for name in fields if f"`{name}`" not in text]
    assert not missing, f"protocol fields absent from the skeleton: {missing}"


def test_the_skeleton_names_every_arm_verdict_and_candidate_couple() -> None:
    text = skeleton()
    candidates = candidate_couples()
    assert len(candidates) == 5
    expected = [arm.value for arm in Arm] + list(VERDICTS) + candidates
    missing = [name for name in expected if f"`{name}`" not in text]
    assert not missing, f"absent from the skeleton: {missing}"


def test_every_decision_is_open_or_recorded_exactly_as_the_owner_took_it() -> None:
    found = decisions()
    recorded = {name: value for name, value in found.items() if value != "`OPEN`"}
    assert recorded == RECORDED
    for name, value in RECORDED.items():
        assert RECORD.fullmatch(value), f"{name}: a record holds a value, a date and the owner"


def test_the_status_line_counts_the_recorded_decisions() -> None:
    found = decisions()
    recorded = [name for name, value in found.items() if value != "`OPEN`"]
    flat = " ".join(skeleton().split())
    status = f"- Status: **skeleton — {len(recorded)} of {len(found)} decisions recorded, "
    assert status + "nothing is frozen, nothing was measured**" in flat


def test_the_cache_couples_declare_no_agent_facing_consumer() -> None:
    """Section 4 says a cache-only protocol would measure rebuild cost, not quality."""
    candidates = set(candidate_couples())
    caches = {
        asset["asset_id"]: [consumer["consumer_id"] for consumer in asset["consumers"]]
        for asset in json.loads(CURRENT_INVENTORY.read_text(encoding="utf-8"))
        if asset["asset_id"] in candidates and asset["role"] == "worktree-local-cache"
    }
    assert len(caches) == 2
    assert all(consumers == ["shared.worker.reconcile"] for consumers in caches.values())
    flat = " ".join(skeleton().split())
    assert "Two of the five couples are worktree-local caches." in flat
    assert "their only declared consumer is `shared.worker.reconcile`" in flat

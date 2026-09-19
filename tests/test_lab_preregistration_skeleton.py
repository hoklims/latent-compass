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
# How a record of the 2026-09-19 sitting ends: every one was taken on the author's proposal.
PROPOSED_2026_09_19 = " — 2026-09-19, repository owner, on the author's proposal"
# Every decision the repository owner has taken, exactly as the document records it. A new
# decision is a new entry here in the same diff: the author cannot fill a row silently, and
# a recorded decision cannot be reworded silently.
RECORDED = {
    name: f"**{value}**{PROPOSED_2026_09_19}"
    for name, value in {
        STANDALONE: (
            "grouped first, then per couple only where the grouped result shows a regression"
        ),
        "`task_ids`": (
            "real past changes of the two pilot repositories, drawn by a rule written before "
            "the freeze and chosen by hand by nobody; each runs from the state before the change"
        ),
        "`origin`": (
            "REAL_DECLARED: real agent sessions on isolated copies, never on a live checkout"
        ),
        "Snapshots — index state": (
            "indexes rebuilt on the state of the isolated copy before its trials — an index "
            "built later would hold the answer — so the existing stack runs at its best; "
            "nothing is said about day-to-day use of older indexes"
        ),
        "Strata": (
            "the six strata HOK-804 names; a task's stratum is fixed with the task list, and a "
            "stratum with no real example has no trial, which the report says"
        ),
        "Exclusions": (
            "no task or trial is excluded after the freeze; a failed task stays in the denominator"
        ),
        "What `SUCCESS` means": (
            "a mechanical check where one exists; otherwise an agent judge blind to the arm "
            "and of another family than the runner; the owner audits one trial in five, drawn "
            "before unblinding"
        ),
        "Cost accounting — index build and maintenance": (
            "reported beside the trials, never amortised into them"
        ),
        "Power, or a bounded descriptive study": (
            "a bounded descriptive study first: a small series fixed in advance, which may "
            "support CONSERVER or PREUVE_INSUFFISANTE and never RETIRABLE_EN_PILOTE; its "
            "variance estimate sizes a powered protocol, registered separately"
        ),
        "Stopping and reruns": (
            "the pair count is fixed at the freeze; a harness failure before the agent's first "
            "action is rerun once, any later failure is MISSING"
        ),
        "Memory": (
            "justification memory off in the controller arm; its effect is not measured here "
            "and stays with HOK-246"
        ),
        "Agent family": (
            "one agent family for the bounded series: Codex; nothing is said about the other "
            "family, which a powered protocol has to cover before any retirement"
        ),
        "Reuse of the HOK-253/HOK-254 collection": (
            "no reuse: a separate isolated experiment, nothing collected earlier enters it"
        ),
        "Chronology anchor": (
            "a reviewed commit of the sealed protocols pushed before any trial, and the CI run "
            "on that commit"
        ),
        "Publication": (
            "results by stratum, with uncertainties and the cases that regress, are published "
            "whatever the outcome"
        ),
        "Rehearsal outside the protocol": (
            "six real sessions of at most fifteen minutes on a throwaway copy, outside the "
            "protocol and excluded from the population; only duration and cost are recorded, "
            "never an outcome; paid through a dedicated key under a 25 USD hard limit"
        ),
    }.items()
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
    assert len(found) == len(table) == 33
    paragraphs = [" ".join(block.split()) for block in text.split("\n\n")]
    standalone = [block for block in paragraphs if block.startswith("Decided: ")]
    assert len(standalone) == 1
    found[STANDALONE] = standalone[0].removeprefix("Decided: ").removesuffix(".")
    assert len(found) == 34
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

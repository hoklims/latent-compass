"""HOK-804 — the preregistration skeleton stays complete, and stays a skeleton.

``docs/active-diagnosis-preregistration.md`` lists what the repository owner has to decide
before the first trial. These tests check that it names every field the sealed protocol
holds, every arm, every verdict and every candidate couple of the committed inventory, so
that a contract or inventory change cannot leave it silently incomplete; and that, for as
long as it calls itself a skeleton, no decision row holds a value. They prove nothing about
the experiment, which has not run.
"""

from __future__ import annotations

import json
from pathlib import Path

from latent_compass.lab.evaluation import Arm, LabEvaluationProtocol

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "docs" / "active-diagnosis-preregistration.md"
CURRENT_REPORT = REPO / "evidence" / "hok799-scope-inventory-v1.2.0" / "dry-run-report.json"
VERDICTS = ("RETIRABLE_EN_PILOTE", "CONSERVER", "PREUVE_INSUFFISANTE")


def skeleton() -> str:
    return SKELETON.read_text(encoding="utf-8")


def test_the_skeleton_names_every_field_of_the_sealed_protocol() -> None:
    text = skeleton()
    fields = list(LabEvaluationProtocol.model_fields)
    assert len(fields) == 17
    missing = [name for name in fields if f"`{name}`" not in text]
    assert not missing, f"protocol fields absent from the skeleton: {missing}"


def test_the_skeleton_names_every_arm_verdict_and_candidate_couple() -> None:
    text = skeleton()
    report = json.loads(CURRENT_REPORT.read_text(encoding="utf-8"))
    candidates = [item["asset_id"] for item in report["dispositions"] if item["action"] != "KEEP"]
    assert len(candidates) == 5
    expected = [arm.value for arm in Arm] + list(VERDICTS) + candidates
    missing = [name for name in expected if f"`{name}`" not in text]
    assert not missing, f"absent from the skeleton: {missing}"


def test_a_document_that_calls_itself_a_skeleton_decides_nothing() -> None:
    text = skeleton()
    assert "- Status: **skeleton" in text
    # Every decision table ends with a "Decided" column. The author fills none of them.
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("|")
    ]
    headers = [row for row in rows if row[-1] == "Decided"]
    assert len(headers) == 2
    decisions = [
        row for row in rows if len(row) == 4 and row[-1] != "Decided" and set(row[-1]) != {"-"}
    ]
    assert len(decisions) == 27
    decided = [row[0] for row in decisions if row[-1] != "`OPEN`"]
    assert not decided, f"the skeleton decides: {decided}"
    # The one decision that is not a table row.
    assert text.count("Decided: `OPEN`.") == 1

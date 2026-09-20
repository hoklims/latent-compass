from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from runpy import run_path
from typing import Any, cast

import pytest

_TOOL = run_path(str(Path(__file__).resolve().parents[1] / "tools" / "independent_audit.py"))
AuditError = cast(type[ValueError], _TOOL["AuditError"])
gate = cast(Callable[[dict[str, Any], dict[str, Any]], dict[str, object]], _TOOL["gate"])


def _epoch() -> dict[str, object]:
    return {
        "schema": "hoklims/latent-compass:independent-audit/2",
        "epoch_digest": "sha256:epoch",
        "policy_digest": "sha256:policy",
        "head_sha": "a" * 40,
    }


def _receipt() -> dict[str, object]:
    return {
        **_epoch(),
        "independence": {
            "not_candidate_author": True,
            "read_only_candidate": True,
            "fresh_session": True,
            "distinct_harness": True,
            "distinct_account": True,
            "distinct_environment": True,
            "distinct_evidence_store": True,
            "first_pass_before_author_narrative": True,
        },
        "claims": [
            {
                "claim": "the gate detects a representative defect",
                "invocation_paths": ["pull_request", "main"],
                "witness": {
                    "mutation": "replace expected decision",
                    "command": "pytest -q",
                    "red_exit": 1,
                    "green_exit": 0,
                    "red_output_digest": "sha256:red",
                    "green_output_digest": "sha256:green",
                },
            }
        ],
        "unresolved_blockers": [],
        "verdict": "PROOF_ADEQUATE",
    }


def test_gate_allows_an_adequate_receipt() -> None:
    assert gate(_epoch(), _receipt())["decision"] == "ALLOW"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("head_sha",), "b" * 40),
        (("verdict",), "PROOF_WEAK"),
        (("unresolved_blockers",), ["still blocked"]),
        (("independence", "first_pass_before_author_narrative"), False),
        (("claims", 0, "witness", "red_exit"), 0),
        (("claims", 0, "witness", "green_exit"), 1),
    ],
)
def test_gate_fails_closed(path: tuple[object, ...], value: object) -> None:
    receipt = deepcopy(_receipt())
    target: object = receipt
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(AuditError):
        gate(_epoch(), receipt)

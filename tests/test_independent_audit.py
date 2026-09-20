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
epoch_digest = cast(Callable[[dict[str, Any]], str], _TOOL["_epoch_digest"])


def _epoch() -> dict[str, object]:
    epoch: dict[str, object] = {
        "schema": "hoklims/latent-compass:independent-audit/2",
        "repository": "hoklims/latent-compass",
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
        "head_tree": "c" * 40,
        "policy_digest": "sha256:" + "d" * 64,
        "files": [{"path": "src/example.py", "kind": "file", "digest": "sha256:" + "e" * 64}],
    }
    epoch["epoch_digest"] = epoch_digest(epoch)
    return epoch


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
                    "red_output_digest": "sha256:" + "1" * 64,
                    "green_output_digest": "sha256:" + "2" * 64,
                },
            }
        ],
        "unresolved_blockers": [],
        "verdict": "PROOF_ADEQUATE",
    }


def test_gate_allows_an_adequate_receipt() -> None:
    assert gate(_epoch(), _receipt())["decision"] == "ALLOW"


def test_gate_refuses_a_fabricated_or_tampered_epoch() -> None:
    epoch = _epoch()
    epoch["head_sha"] = "f" * 40
    receipt = _receipt()
    receipt["head_sha"] = epoch["head_sha"]
    with pytest.raises(AuditError, match="epoch_digest does not match"):
        gate(epoch, receipt)


def test_gate_refuses_clone_specific_repository_identity() -> None:
    epoch = _epoch()
    epoch["repository"] = "git@github.com:hoklims/latent-compass.git"
    epoch["epoch_digest"] = epoch_digest(epoch)
    receipt = _receipt()
    receipt["epoch_digest"] = epoch["epoch_digest"]
    with pytest.raises(AuditError, match="repository must be"):
        gate(epoch, receipt)


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

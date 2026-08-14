"""Shared fixtures.

Every builder returns plain JSON-shaped data, so a test can corrupt exactly one
field and prove that the corruption — and nothing else — is what refuses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.ledger import LedgerStore
from latent_compass.protocol import (
    HoldoutLedger,
    MeasurementSet,
    Preregistration,
    Verdict,
    evaluate,
    load_measurement_set,
    load_preregistration,
    recompute_verdict_seal,
)

HOST_ID = "host-alpha"
STORE_ID = "store-alpha"
EPOCH = "LC-HOK181-E1-7d1fc727"
FAMILY = "claude"

FIXED_APPEND_TIME = "2026-08-14T12:00:00Z"


@pytest.fixture
def fixed_clock() -> Callable[[], str]:
    """A clock that never moves, so seals depend only on content."""
    return lambda: FIXED_APPEND_TIME


def episode_payload(
    episode_id: str = "ep-00000001",
    *,
    host_id: str = HOST_ID,
    store_id: str = STORE_ID,
    epoch: str = EPOCH,
    agent_family: str = FAMILY,
    **overrides: Any,
) -> dict[str, Any]:
    """A complete, valid episode. Overrides replace top-level keys outright."""
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "episode_id": episode_id,
        "provenance": {
            "host_id": host_id,
            "agent_family": agent_family,
            "store_id": store_id,
            "epoch": epoch,
            "recorded_at": "2026-08-14T10:00:00Z",
            "tool_version": "0.1.0",
        },
        "state": {
            "levels": [
                {"depth": 0, "name": "L6-strategy", "summary_digest": "digest-l6"},
                {"depth": 1, "name": "L5-product-intent", "summary_digest": "digest-l5"},
                {"depth": 2, "name": "L4-invariants", "summary_digest": "digest-l4"},
            ],
            "branch_point": {"node_id": "node-l4-a", "depth": 2, "parent_node_id": "node-l5-a"},
        },
        "candidates": [
            {"direction_id": "dir-alpha", "propensity": 0.6, "prior_uncertainty": 0.2},
            {"direction_id": "dir-beta", "propensity": 0.4, "prior_uncertainty": 0.3},
        ],
        "decision": {
            "advisory": {
                "contract_version": "1.0.0",
                "kind": "DIRECTION",
                "direction_id": "dir-alpha",
                "confidence": 0.7,
                "uncertainty": 0.2,
                "rationale": "higher observed reversibility on the sibling branch",
                "issued_by": "latent_compass",
            },
            "selected_direction_id": "dir-alpha",
        },
        "external_verdict": {
            "judge": "external-judge",
            "verdict": "PASS",
            "observed_at": "2026-08-14T10:05:00Z",
            "evidence_digest": "evidence-digest-1",
        },
        "outcome": {
            "observability": "FULL",
            "observed": True,
            "observed_at": "2026-08-14T11:00:00Z",
            "success": True,
            "violations": 0,
        },
        "economics": {
            "cost": 1.5,
            "information_gain": 0.25,
            "reversibility": 0.9,
            "uncertainty": 0.2,
        },
        "redactions": [],
    }
    payload.update(overrides)
    return payload


def protocol_payload(**overrides: Any) -> dict[str, Any]:
    """A complete, valid pre-registration fixing all eight metric families."""
    payload: dict[str, Any] = {
        "contract_version": "1.0.0",
        "protocol_id": "lc-shadow-p1",
        "revision": 1,
        "epoch": EPOCH,
        "registered_at": "2026-08-14T09:00:00Z",
        "tasks": ["branch-direction-ranking", "abstention-calibration"],
        "splits": [
            {
                "split": "TRAIN",
                "corpus_seal": "sha256:train-corpus",
                "item_count": 400,
                "disjoint_from": ["VALIDATION", "HOLDOUT"],
            },
            {
                "split": "VALIDATION",
                "corpus_seal": "sha256:validation-corpus",
                "item_count": 100,
                "disjoint_from": ["TRAIN", "HOLDOUT"],
            },
            {
                "split": "HOLDOUT",
                "corpus_seal": "sha256:holdout-corpus",
                "item_count": 100,
                "disjoint_from": ["TRAIN", "VALIDATION"],
            },
        ],
        "baselines": [
            {
                "name": "always-abstain",
                "kind": "TRIVIAL",
                "description": "abstain on every branch point",
            },
            {
                "name": "propensity-argmax",
                "kind": "STRONG",
                "description": "pick the highest-propensity candidate",
            },
        ],
        "seeds": [11, 23, 37],
        "metrics": [
            {
                "name": "success-rate",
                "family": "SUCCESS",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.6,
                "required": True,
            },
            {
                "name": "violation-rate",
                "family": "VIOLATION",
                "direction": "LOWER_IS_BETTER",
                "threshold": 0.05,
                "required": True,
            },
            {
                "name": "cost-per-episode",
                "family": "COST",
                "direction": "LOWER_IS_BETTER",
                "threshold": 2.0,
                "required": True,
            },
            {
                "name": "information-gain",
                "family": "INFORMATION",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.1,
                "required": True,
            },
            {
                "name": "reversibility",
                "family": "REVERSIBILITY",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.8,
                "required": True,
            },
            {
                "name": "calibration-error",
                "family": "CALIBRATION",
                "direction": "LOWER_IS_BETTER",
                "threshold": 0.1,
                "required": True,
            },
            {
                "name": "p99-regret",
                "family": "TAIL",
                "direction": "LOWER_IS_BETTER",
                "threshold": 3.0,
                "required": True,
            },
            {
                "name": "drift-divergence",
                "family": "DRIFT",
                "direction": "LOWER_IS_BETTER",
                "threshold": 0.2,
                "required": False,
            },
        ],
        "sensitivity_analyses": [
            "re-score with each seed dropped in turn",
            "re-score with the abstention threshold at 0.30 and 0.40",
        ],
        "failure_cases": [
            "candidate set of size one, where propensity carries no information",
            "outcome observability NONE for more than half the holdout",
        ],
    }
    payload.update(overrides)
    return payload


#: The corpus seal each split declares in :func:`protocol_payload`.
CORPUS_SEALS: dict[str, str] = {
    "TRAIN": "sha256:train-corpus",
    "VALIDATION": "sha256:validation-corpus",
    "HOLDOUT": "sha256:holdout-corpus",
}


def measurement_payload(
    protocol_seal: str,
    *,
    split: str = "VALIDATION",
    purpose: str = "EXPLORATION",
    epoch: str = EPOCH,
    values: dict[str, float] | None = None,
    seeds: tuple[int, ...] = (11, 23, 37),
    corpus_seal: str | None = None,
) -> dict[str, Any]:
    """Measurements for every required metric, on every pre-registered seed."""
    defaults = {
        "success-rate": 0.72,
        "violation-rate": 0.02,
        "cost-per-episode": 1.4,
        "information-gain": 0.31,
        "reversibility": 0.91,
        "calibration-error": 0.04,
        "p99-regret": 2.1,
    }
    if values is not None:
        defaults.update(values)
    return {
        "contract_version": "1.0.0",
        "protocol_seal": protocol_seal,
        "corpus_seal": corpus_seal if corpus_seal is not None else CORPUS_SEALS[split],
        "epoch": epoch,
        "purpose": purpose,
        "split": split,
        "measurements": [
            {"metric": metric, "split": split, "seed": seed, "value": value}
            for metric, value in sorted(defaults.items())
            for seed in seeds
        ],
    }


@pytest.fixture
def protocol() -> Preregistration:
    """A complete, valid pre-registration."""
    return load_preregistration(protocol_payload())


def build_measurements(payload: dict[str, Any]) -> MeasurementSet:
    return load_measurement_set(payload)


@pytest.fixture
def validation_measurements(protocol: Preregistration) -> MeasurementSet:
    """The raw validation evidence used by the validation verdict fixture."""
    return build_measurements(measurement_payload(protocol.protocol_seal()))


@pytest.fixture
def validation_verdict(
    protocol: Preregistration, validation_measurements: MeasurementSet
) -> Verdict:
    """A real, self-sealing CONTINUE verdict on the validation split."""
    return evaluate(protocol, validation_measurements)


@pytest.fixture
def holdout_measurements(protocol: Preregistration) -> MeasurementSet:
    """The raw final-holdout measurements from which the fixture verdict is derived."""
    return build_measurements(
        measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT")
    )


@pytest.fixture
def holdout_ledger(tmp_path: Path) -> HoldoutLedger:
    """The durable usage ledger carrying the fixture verdict's consumption receipt."""
    return HoldoutLedger(tmp_path / "holdout-fixture.json")


@pytest.fixture
def failing_holdout_measurements(protocol: Preregistration) -> MeasurementSet:
    """Raw holdout evidence whose required success metric fails."""
    return build_measurements(
        measurement_payload(
            protocol.protocol_seal(),
            split="HOLDOUT",
            purpose="FINAL_VERDICT",
            values={"success-rate": 0.41},
        )
    )


@pytest.fixture
def failing_holdout_verdict(
    protocol: Preregistration, failing_holdout_measurements: MeasurementSet
) -> Verdict:
    """A reproducible KILL verdict, scored without claiming a new holdout spend."""
    from latent_compass.protocol import score_measurements

    return score_measurements(protocol, failing_holdout_measurements)


@pytest.fixture
def holdout_verdict(
    protocol: Preregistration,
    holdout_measurements: MeasurementSet,
    holdout_ledger: HoldoutLedger,
) -> Verdict:
    """A real, self-sealing final CONTINUE verdict on the holdout."""
    return evaluate(
        protocol,
        holdout_measurements,
        holdout_ledger=holdout_ledger,
        consumed_at="2026-08-14T13:00:00Z",
    )


def reseal(verdict: Verdict, **changes: Any) -> Verdict:
    """Edit a verdict and re-seal it honestly, as a legitimate producer would."""
    edited = verdict.model_copy(update=changes)
    return edited.model_copy(update={"verdict_seal": recompute_verdict_seal(edited)})


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "ledger-root"


@pytest.fixture
def store(store_root: Path, fixed_clock: Callable[[], str]) -> Iterator[LedgerStore]:
    """An open store bound to the fixture host, family and epoch."""
    opened = LedgerStore.create(
        store_root,
        store_id=STORE_ID,
        host_id=HOST_ID,
        agent_family=AgentFamily.CLAUDE,
        epoch=EPOCH,
        clock=fixed_clock,
    )
    try:
        yield opened
    finally:
        opened.close()


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def snapshot_tree(root: Path) -> dict[str, str]:
    """Content digest of every file under ``root``, keyed by relative path."""
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

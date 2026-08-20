"""Deterministic generator for the committed synthetic benchmark corpus.

The corpus under ``corpus/synthetic-v1/`` is committed data, and committed data
rots. This module is its provenance: running it reproduces every byte of every
file, and ``test_synthetic_corpus.py`` asserts exactly that. A corpus edited by
hand stops reproducing, which is the point.

What the corpus is built to exercise
------------------------------------
It is small enough to read and shaped to make the harness's refusals reachable:

* every corpus-derived eligibility state occurs — eligible, unobservable,
  censored past its cutoff and observed-but-incomplete;
* a stricter support floor makes ``case-val-09`` refuse the whole run rather
  than changing one baseline's analysis population;
* the four baselines disagree on most cases, so a report in which two of them
  agreed everywhere would be visibly wrong;
* three distribution strata — one nominal and two shift groups — each keep
  enough eligible cases for ``DRIFT`` to be measured for every baseline;
* propensities are dyadic rationals, so they sum to exactly ``1.0`` in binary
  floating point rather than within a tolerance.

It is **not** built to make any baseline look good, and it is far too small and
too artificial to support a claim that one is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[1]
CORPUS_DIR: Final = REPO / "corpus" / "synthetic-v1"

CORPUS_ID: Final = "lc-synthetic-bench"
CORPUS_VERSION: Final = "v1.0.0"
BENCHMARK_ID: Final = "lc-hok188-b1"
PROTOCOL_ID: Final = "lc-hok188-p1"
EPOCH: Final = "LC-HOK188-E1"
SEEDS: Final = (11, 23, 37)

HOST_ID: Final = "synthetic-host"
STORE_ID: Final = "synthetic-store"
RECORDED_AT: Final = "2026-06-01T09:00:00Z"
CUTOFF: Final = "2026-07-01T00:00:00Z"
OBSERVED_AT: Final = "2026-06-02T09:00:00Z"
LATE_OBSERVED_AT: Final = "2026-07-15T09:00:00Z"
HORIZON_SECONDS: Final = 2_592_000

NOMINAL: Final = "nominal"
LONG_HORIZON: Final = "shift.long-horizon"
HIGH_UNCERTAINTY: Final = "shift.high-uncertainty"

MINIMUM_PROPENSITY: Final = 0.015625
TAIL_QUANTILE: Final = 0.9
BENCHMARK_CONTRACT_VERSION: Final = "1.1.0"

#: ``(case number, group, ambiguous, candidates, logged direction, outcome kind,
#: success, violations, cost, information gain, reversibility)``.
#:
#: ``candidates`` is ``(direction suffix, propensity, prior uncertainty)``.
#: Outcome kinds: ``full`` observed in time, ``none`` never observable,
#: ``late`` observed after the cutoff, ``partial`` observed without a violation
#: count.
VALIDATION_TABLE: Final[tuple[tuple[Any, ...], ...]] = (
    (
        1,
        NOMINAL,
        False,
        (("a", 0.5, 0.30), ("b", 0.25, 0.10), ("c", 0.25, 0.40)),
        "b",
        "full",
        True,
        0,
        1.0,
        0.30,
        0.90,
    ),
    (
        2,
        NOMINAL,
        False,
        (("a", 0.25, 0.20), ("b", 0.5, 0.35), ("c", 0.25, 0.15)),
        "a",
        "full",
        False,
        1,
        2.5,
        0.10,
        0.40,
    ),
    (
        3,
        NOMINAL,
        True,
        (("a", 0.375, 0.25), ("b", 0.25, 0.05), ("c", 0.25, 0.30), ("d", 0.125, 0.45)),
        "b",
        "full",
        True,
        0,
        0.8,
        0.45,
        0.95,
    ),
    (
        4,
        NOMINAL,
        False,
        (("a", 0.25, 0.40), ("b", 0.25, 0.20), ("c", 0.5, 0.10)),
        "c",
        "full",
        True,
        0,
        1.2,
        0.25,
        0.85,
    ),
    (
        5,
        NOMINAL,
        True,
        (("a", 0.5, 0.15), ("b", 0.5, 0.15)),
        "a",
        "full",
        False,
        2,
        3.0,
        0.05,
        0.20,
    ),
    (
        6,
        NOMINAL,
        False,
        (("a", 0.5, 0.20), ("b", 0.5, 0.30)),
        "a",
        "none",
        None,
        None,
        1.1,
        0.20,
        0.60,
    ),
    (
        7,
        NOMINAL,
        False,
        (("a", 0.25, 0.25), ("b", 0.5, 0.20), ("c", 0.25, 0.35)),
        "b",
        "late",
        True,
        0,
        1.4,
        0.15,
        0.70,
    ),
    (
        8,
        NOMINAL,
        False,
        (("a", 0.375, 0.30), ("b", 0.375, 0.20), ("c", 0.25, 0.45)),
        "b",
        "partial",
        True,
        None,
        1.3,
        0.18,
        0.65,
    ),
    (
        9,
        LONG_HORIZON,
        False,
        (("a", 0.015625, 0.30), ("b", 0.484375, 0.10), ("c", 0.25, 0.35), ("d", 0.25, 0.20)),
        "b",
        "full",
        True,
        0,
        1.6,
        0.20,
        0.70,
    ),
    (
        10,
        LONG_HORIZON,
        False,
        (("a", 0.25, 0.35), ("b", 0.375, 0.25), ("c", 0.375, 0.05)),
        "c",
        "full",
        True,
        0,
        1.9,
        0.22,
        0.55,
    ),
    (
        11,
        LONG_HORIZON,
        True,
        (("a", 0.5, 0.20), ("b", 0.25, 0.30), ("c", 0.25, 0.10)),
        "a",
        "full",
        False,
        1,
        2.8,
        0.08,
        0.35,
    ),
    (
        12,
        LONG_HORIZON,
        False,
        (("a", 0.25, 0.15), ("b", 0.5, 0.20), ("c", 0.25, 0.25)),
        "b",
        "full",
        True,
        0,
        2.1,
        0.28,
        0.60,
    ),
    (
        13,
        HIGH_UNCERTAINTY,
        True,
        (("a", 0.25, 0.55), ("b", 0.5, 0.60), ("c", 0.25, 0.50)),
        "b",
        "full",
        False,
        1,
        3.4,
        0.12,
        0.30,
    ),
    (
        14,
        HIGH_UNCERTAINTY,
        False,
        (("a", 0.375, 0.45), ("b", 0.375, 0.70), ("c", 0.25, 0.65)),
        "a",
        "full",
        True,
        0,
        2.2,
        0.33,
        0.50,
    ),
    (
        15,
        HIGH_UNCERTAINTY,
        False,
        (("a", 0.5, 0.80), ("b", 0.25, 0.55), ("c", 0.25, 0.75)),
        "a",
        "full",
        False,
        3,
        4.0,
        0.04,
        0.15,
    ),
    (
        16,
        HIGH_UNCERTAINTY,
        True,
        (("a", 0.25, 0.60), ("b", 0.25, 0.45), ("c", 0.5, 0.50)),
        "c",
        "full",
        True,
        0,
        2.6,
        0.26,
        0.45,
    ),
)

TRAIN_TABLE: Final[tuple[tuple[Any, ...], ...]] = (
    (
        1,
        NOMINAL,
        False,
        (("a", 0.5, 0.20), ("b", 0.5, 0.30)),
        "a",
        "full",
        True,
        0,
        1.0,
        0.20,
        0.80,
    ),
    (
        2,
        NOMINAL,
        True,
        (("a", 0.25, 0.35), ("b", 0.75, 0.15)),
        "b",
        "full",
        True,
        0,
        1.5,
        0.25,
        0.75,
    ),
    (
        3,
        NOMINAL,
        False,
        (("a", 0.375, 0.10), ("b", 0.375, 0.40), ("c", 0.25, 0.30)),
        "a",
        "full",
        False,
        1,
        2.0,
        0.10,
        0.50,
    ),
    (
        4,
        LONG_HORIZON,
        False,
        (("a", 0.5, 0.25), ("b", 0.25, 0.20), ("c", 0.25, 0.45)),
        "a",
        "full",
        True,
        0,
        1.8,
        0.30,
        0.65,
    ),
    (
        5,
        LONG_HORIZON,
        True,
        (("a", 0.25, 0.50), ("b", 0.5, 0.35), ("c", 0.25, 0.15)),
        "b",
        "full",
        False,
        2,
        2.9,
        0.06,
        0.25,
    ),
    (
        6,
        HIGH_UNCERTAINTY,
        False,
        (("a", 0.25, 0.65), ("b", 0.5, 0.55), ("c", 0.25, 0.70)),
        "b",
        "full",
        True,
        0,
        2.4,
        0.19,
        0.40,
    ),
)

HOLDOUT_TABLE: Final[tuple[tuple[Any, ...], ...]] = (
    (
        1,
        NOMINAL,
        False,
        (("a", 0.5, 0.15), ("b", 0.5, 0.25)),
        "b",
        "full",
        True,
        0,
        1.1,
        0.21,
        0.88,
    ),
    (
        2,
        NOMINAL,
        True,
        (("a", 0.25, 0.30), ("b", 0.5, 0.10), ("c", 0.25, 0.40)),
        "b",
        "full",
        True,
        0,
        1.3,
        0.27,
        0.82,
    ),
    (
        3,
        NOMINAL,
        False,
        (("a", 0.375, 0.20), ("b", 0.25, 0.35), ("c", 0.375, 0.05)),
        "c",
        "full",
        False,
        1,
        2.3,
        0.09,
        0.45,
    ),
    (
        4,
        NOMINAL,
        False,
        (("a", 0.5, 0.40), ("b", 0.25, 0.20), ("c", 0.25, 0.30)),
        "a",
        "full",
        True,
        0,
        1.7,
        0.24,
        0.72,
    ),
    (
        5,
        LONG_HORIZON,
        True,
        (("a", 0.25, 0.25), ("b", 0.5, 0.15), ("c", 0.25, 0.50)),
        "b",
        "full",
        True,
        0,
        2.0,
        0.16,
        0.58,
    ),
    (
        6,
        LONG_HORIZON,
        False,
        (("a", 0.375, 0.45), ("b", 0.375, 0.30), ("c", 0.25, 0.20)),
        "c",
        "full",
        False,
        2,
        3.1,
        0.07,
        0.28,
    ),
    (
        7,
        HIGH_UNCERTAINTY,
        False,
        (("a", 0.5, 0.75), ("b", 0.25, 0.60), ("c", 0.25, 0.55)),
        "a",
        "full",
        False,
        1,
        3.6,
        0.11,
        0.22,
    ),
    (
        8,
        HIGH_UNCERTAINTY,
        True,
        (("a", 0.25, 0.50), ("b", 0.5, 0.65), ("c", 0.25, 0.70)),
        "b",
        "full",
        True,
        0,
        2.7,
        0.29,
        0.48,
    ),
)


def _outcome(kind: str, success: bool | None, violations: int | None) -> dict[str, Any] | None:
    """Build the deferred outcome for one of the four observation kinds."""
    if kind == "none":
        return {"observability": "NONE", "observed": False}
    if kind == "late":
        return {
            "observability": "FULL",
            "observed": True,
            "observed_at": LATE_OBSERVED_AT,
            "success": success,
            "violations": violations,
        }
    if kind == "partial":
        return {
            "observability": "PARTIAL",
            "observed": True,
            "observed_at": OBSERVED_AT,
            "success": success,
        }
    return {
        "observability": "FULL",
        "observed": True,
        "observed_at": OBSERVED_AT,
        "success": success,
        "violations": violations,
    }


def _case(split: str, prefix: str, row: tuple[Any, ...]) -> dict[str, Any]:
    number, group, ambiguous, candidates, logged, kind, success, violations, cost, gain, rev = row
    logged_direction = f"dir-{logged}"
    logged_uncertainty = next(
        uncertainty for suffix, _, uncertainty in candidates if suffix == logged
    )
    # The advisory's abstention threshold is a property of the advisory
    # contract, not of the corpus. A logged direction whose prior uncertainty
    # sits above it is still a real logged direction, so the recorded advisory
    # carries the highest uncertainty a DIRECTION may carry rather than being
    # rewritten into an abstention the agent did not take.
    advisory_uncertainty = min(logged_uncertainty, 0.35)
    return {
        "contract_version": BENCHMARK_CONTRACT_VERSION,
        "case_id": f"case-{prefix}-{number:02d}",
        "split": split,
        "distribution_kind": "NOMINAL" if group == NOMINAL else "SHIFT",
        "distribution_group": group,
        "ambiguous": ambiguous,
        "observation_cutoff": CUTOFF,
        "observation_horizon_seconds": HORIZON_SECONDS,
        "episode": {
            "schema_version": "1.0.0",
            "episode_id": f"ep-{prefix}-{number:02d}",
            "provenance": {
                "host_id": HOST_ID,
                "agent_family": "other",
                "store_id": STORE_ID,
                "epoch": EPOCH,
                "recorded_at": RECORDED_AT,
                "tool_version": "0.1.0",
            },
            "state": {
                "levels": [
                    {
                        "depth": 0,
                        "name": "L4-invariants",
                        "summary_digest": f"digest-{prefix}-{number:02d}-l4",
                    },
                    {
                        "depth": 1,
                        "name": "L2-components",
                        "summary_digest": f"digest-{prefix}-{number:02d}-l2",
                    },
                ],
                "branch_point": {
                    "node_id": f"node-{prefix}-{number:02d}",
                    "depth": 1,
                    "parent_node_id": None,
                },
            },
            "candidates": [
                {
                    "direction_id": f"dir-{suffix}",
                    "propensity": propensity,
                    "prior_uncertainty": uncertainty,
                }
                for suffix, propensity, uncertainty in candidates
            ],
            "decision": {
                "advisory": {
                    "contract_version": "1.0.0",
                    "kind": "DIRECTION",
                    "direction_id": logged_direction,
                    "confidence": round(1.0 - advisory_uncertainty, 4),
                    "uncertainty": advisory_uncertainty,
                    "rationale": "logged by the synthetic behaviour policy",
                    "issued_by": "latent_compass",
                },
                "selected_direction_id": logged_direction,
            },
            "external_verdict": None,
            "outcome": _outcome(kind, success, violations),
            "economics": {
                "cost": cost,
                "information_gain": gain,
                "reversibility": rev,
                "uncertainty": logged_uncertainty,
            },
            "redactions": [
                {
                    "path": "external_verdict",
                    "reason": "no deterministic judge in the synthetic corpus",
                }
            ],
        },
    }


def split_payload(split: str, prefix: str, table: tuple[tuple[Any, ...], ...]) -> dict[str, Any]:
    """The complete on-disk payload for one split."""
    return {
        "contract_version": BENCHMARK_CONTRACT_VERSION,
        "split": split,
        "cases": [_case(split, prefix, row) for row in table],
    }


SPLIT_FILES: Final[dict[str, tuple[str, str, tuple[tuple[Any, ...], ...]]]] = {
    "TRAIN": ("train.json", "trn", TRAIN_TABLE),
    "VALIDATION": ("validation.json", "val", VALIDATION_TABLE),
    "HOLDOUT": ("holdout.json", "hld", HOLDOUT_TABLE),
}


def dumps(payload: object) -> str:
    """The one serialisation used for every committed file."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def protocol_payload(seals: dict[str, str], counts: dict[str, int]) -> dict[str, Any]:
    """The pre-registration that governs the synthetic benchmark.

    Thresholds are pre-registered values chosen before any of these baselines
    was run against this corpus. They are not measured optima, and a baseline
    failing one of them is not evidence that the baseline is bad.
    """
    return {
        "contract_version": "1.0.0",
        "protocol_id": PROTOCOL_ID,
        "revision": 1,
        "epoch": EPOCH,
        "registered_at": "2026-06-01T08:00:00Z",
        "tasks": ["offline-baseline-benchmark"],
        "splits": [
            {
                "split": split,
                "corpus_seal": seals[split],
                "item_count": counts[split],
                "disjoint_from": sorted({"TRAIN", "VALIDATION", "HOLDOUT"} - {split}),
            }
            for split in ("HOLDOUT", "TRAIN", "VALIDATION")
        ],
        "baselines": [
            {
                "name": "fixed-canonical",
                "kind": "TRIVIAL",
                "description": "select the first direction_id in canonical order",
            },
            {
                "name": "least-uncertainty",
                "kind": "STRONG",
                "description": "select the smallest prior_uncertainty, canonical tie-break",
            },
            {
                "name": "logged-propensity-arbiter",
                "kind": "STRONG",
                "description": "maximise logged propensity, then minimise uncertainty",
            },
            {
                "name": "seeded-uniform",
                "kind": "TRIVIAL",
                "description": "uniform pick derived from the spec seal, corpus, case and seed",
            },
        ],
        "seeds": list(SEEDS),
        "metrics": [
            {
                "name": "ips-success",
                "family": "SUCCESS",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.5,
                "required": True,
            },
            {
                "name": "ips-violations",
                "family": "VIOLATION",
                "direction": "LOWER_IS_BETTER",
                "threshold": 0.5,
                "required": True,
            },
            {
                "name": "ips-cost",
                "family": "COST",
                "direction": "LOWER_IS_BETTER",
                "threshold": 3.0,
                "required": True,
            },
            {
                "name": "ips-information-gain",
                "family": "INFORMATION",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.1,
                "required": True,
            },
            {
                "name": "ips-reversibility",
                "family": "REVERSIBILITY",
                "direction": "HIGHER_IS_BETTER",
                "threshold": 0.4,
                "required": True,
            },
            {
                "name": "ips-brier",
                "family": "CALIBRATION",
                "direction": "LOWER_IS_BETTER",
                "threshold": 1.0,
                "required": True,
            },
            {
                "name": "tail-weighted-cost-quantile",
                "family": "TAIL",
                "direction": "LOWER_IS_BETTER",
                "threshold": 12.0,
                "required": True,
            },
            {
                "name": "drift-max-success-gap",
                "family": "DRIFT",
                "direction": "LOWER_IS_BETTER",
                "threshold": 1.0,
                "required": False,
            },
        ],
        "sensitivity_analyses": [
            "report SNIPS beside IPS for every observed channel",
            "report support, weight quantiles and effective sample size",
        ],
        "failure_cases": [
            "a near-deterministic logging policy leaves most baselines with no non-zero weight",
            "a distribution stratum with no eligible case makes DRIFT unmeasurable",
            "outcomes observed after the cutoff censor a case rather than counting as failures",
        ],
    }


def spec_payload(protocol_seal: str, seals: dict[str, str]) -> dict[str, Any]:
    """The benchmark plan bound to that pre-registration and that corpus."""
    return {
        "contract_version": BENCHMARK_CONTRACT_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_seal": protocol_seal,
        "epoch": EPOCH,
        "corpus_id": CORPUS_ID,
        "corpus_version": CORPUS_VERSION,
        "executed_split": "VALIDATION",
        "train_corpus_seal": seals["TRAIN"],
        "validation_corpus_seal": seals["VALIDATION"],
        "holdout_corpus_seal": seals["HOLDOUT"],
        "seeds": list(SEEDS),
        "baselines": [
            {"baseline_id": "fixed-canonical", "algorithm_version": "1.0.0", "kind": "TRIVIAL"},
            {"baseline_id": "least-uncertainty", "algorithm_version": "1.0.0", "kind": "STRONG"},
            {
                "baseline_id": "logged-propensity-arbiter",
                "algorithm_version": "1.0.0",
                "kind": "STRONG",
            },
            {"baseline_id": "seeded-uniform", "algorithm_version": "1.0.0", "kind": "TRIVIAL"},
        ],
        "budget": {
            "max_cases": 64,
            "max_candidate_inspections_per_case": 4,
            "max_random_draws_per_case": 1,
        },
        "minimum_propensity": MINIMUM_PROPENSITY,
        "tail_quantile": TAIL_QUANTILE,
    }


def generate(target: Path) -> dict[str, str]:
    """Produce every committed corpus file's text, keyed by file name.

    Returns the texts rather than writing them, so a test can compare against
    what is on disk without touching the repository.
    """
    from latent_compass.benchmark.corpus import (
        CorpusProvenance,
        build_manifest,
        load_case_file,
        split_corpus_seal,
    )
    from latent_compass.protocol import Split, load_preregistration

    texts: dict[str, str] = {}
    seals: dict[str, str] = {}
    counts: dict[str, int] = {}
    for split, (name, prefix, table) in sorted(SPLIT_FILES.items()):
        payload = split_payload(split, prefix, table)
        texts[name] = dumps(payload)
        case_file = load_case_file(payload)
        seals[split] = split_corpus_seal(case_file.cases)
        counts[split] = len(case_file.cases)

    manifest = build_manifest(
        target,
        corpus_id=CORPUS_ID,
        corpus_version=CORPUS_VERSION,
        provenance=CorpusProvenance(
            origin="generated by tests/corpus_generator.py in this repository",
            licence="Apache-2.0, same as the repository",
            synthetic=True,
            description=(
                "A hand-specified synthetic corpus. It demonstrates that the HOK-188 "
                "pipeline runs, reproduces and refuses correctly. It contains no real "
                "agent behaviour and supports no claim about the value of any baseline."
            ),
        ),
        relative_paths={split: SPLIT_FILES[split.value][0] for split in Split},
    )
    texts["manifest.json"] = dumps(manifest.canonical_payload())

    protocol = load_preregistration(protocol_payload(seals, counts))
    texts["protocol.json"] = dumps(protocol.canonical_payload())
    texts["spec.json"] = dumps(spec_payload(protocol.protocol_seal(), seals))
    return texts


def write(target: Path = CORPUS_DIR) -> None:
    """Write the corpus to ``target``, creating it if needed."""
    target.mkdir(parents=True, exist_ok=True)
    # The manifest is derived from the split files on disk, so the splits are
    # written first and the derived documents second.
    for split, (name, prefix, table) in sorted(SPLIT_FILES.items()):
        (target / name).write_text(dumps(split_payload(split, prefix, table)), encoding="utf-8")
    for name, text in generate(target).items():
        (target / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - developer entry point
    write()
    print(f"wrote the synthetic corpus to {CORPUS_DIR}")

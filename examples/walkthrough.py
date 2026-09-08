"""Run the complete synthetic capture-to-audit path through public interfaces."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import latent_compass
from latent_compass.decision_memory import build_record_payload

SYNTHETIC_NOTICE = (
    "SYNTHETIC DEMONSTRATION ONLY: these records are not empirical evidence, "
    "training data, or an activation decision."
)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        json.dump(payload, destination, indent=2, sort_keys=True)
        destination.write("\n")


def _observations() -> list[dict[str, object]]:
    values: dict[str, dict[str, object]] = {
        "SUCCESS": {"kind": "BOOLEAN", "boolean_value": True},
        "VIOLATION": {"kind": "INTEGER", "integer_value": 0},
        "INFORMATION": {"kind": "FLOAT", "float_value": 0.25},
        "REVERSIBILITY": {"kind": "STRING", "string_value": "synthetic-revert"},
    }
    observations: list[dict[str, object]] = []
    for index, dimension in enumerate(
        ("SUCCESS", "VIOLATION", "COST", "INFORMATION", "REVERSIBILITY"), start=1
    ):
        if dimension == "COST":
            observations.append(
                {
                    "dimension": dimension,
                    "status": "UNKNOWN",
                    "statement": None,
                    "value": None,
                    "provenance": None,
                    "unknown_reason": "LATE",
                    "unknown_detail": "The synthetic cost source was deliberately unavailable.",
                }
            )
            continue
        observations.append(
            {
                "dimension": dimension,
                "status": "OBSERVED",
                "statement": f"Synthetic post-action observation for {dimension.lower()}.",
                "value": values[dimension],
                "provenance": {
                    "source_id": f"synthetic-source-{dimension.lower()}",
                    "source_digest": f"sha256:{index}{'0' * 63}",
                    "observed_at": "2026-08-20T09:00:00Z",
                    "producer": "synthetic-observer",
                    "confidence": 0.9,
                },
                "unknown_reason": None,
                "unknown_detail": None,
            }
        )
    return observations


def _reconciliation_payload(
    decision: latent_compass.StrategicDecisionRecord,
) -> dict[str, object]:
    return {
        "contract_version": "1.0.0",
        "reconciliation_id": "synthetic-reconciliation-0001",
        "revision": 1,
        "revision_kind": "INITIAL",
        "revision_reason": None,
        "supersedes_revision_seal": None,
        "binding": decision.binding.canonical_payload(),
        "preimage": {
            "decision_binding": decision.binding.canonical_payload(),
            "decision_id": decision.decision_id,
            "decision_revision": decision.revision,
            "record_seal": decision.record_seal(),
            "projection_seal": decision.projection.projection_seal(),
        },
        "reconciled_at": "2026-08-20T10:00:00Z",
        "reconciled_by": "synthetic-observer",
        "sensitivity": "NON_SENSITIVE",
        "authorization_state": "AUTHORIZED_ELSEWHERE",
        "authorization_reference": "synthetic-external-authority",
        "execution_state": "EXECUTED",
        "executed_direction_id": "direction-alpha",
        "observations": _observations(),
    }


def _prospective_plan(
    decision: latent_compass.StrategicDecisionRecord,
) -> latent_compass.ProspectiveCollectionPlan:
    power = latent_compass.plan_exact_one_sided_binomial(
        p0=0.01,
        p1=0.99,
        alpha=0.05,
        target_power=0.8,
        max_enrollments=1,
        clustering_inflation=1.0,
    )
    return latent_compass.admit_prospective_plan(
        {
            "contract_version": "1.0.0",
            "plan_id": "synthetic-prospective-plan-0001",
            "source_binding": {
                "decision": decision.binding.canonical_payload(),
                "reconciliation": decision.binding.canonical_payload(),
            },
            "population": "One explicitly synthetic demonstration decision.",
            "declared_strata": ["synthetic-demonstration"],
            "hard_exclusions": [item.value for item in latent_compass.HardExclusion],
            "plan_created_at": "2026-08-15T08:00:00Z",
            "collection_not_before": "2026-08-16T08:00:00Z",
            "minimum_calendar_end": "2026-08-21T08:00:00Z",
            "hard_calendar_end": "2026-08-31T08:00:00Z",
            "max_enrollments": 1,
            "outcome_dependent_interim_looks": 0,
            "independence_policy": {"decision_producer_identities": ["synthetic-operator"]},
            "power": power.canonical_payload(),
            "stop_priority": [item.value for item in latent_compass.StopCondition],
        }
    )


def run(root: Path, *, examples_dir: Path | None = None) -> dict[str, Any]:
    """Create a new synthetic store tree and return its diagnostic summary."""
    if root.exists():
        raise FileExistsError(f"refusing to reuse existing walkthrough root: {root}")
    root.mkdir(parents=True)
    inputs = examples_dir or Path(__file__).resolve().parent

    episode = latent_compass.load_episode(_read_json(inputs / "synthetic-episode.json"))
    projection = latent_compass.load_judgeable_projection(
        _read_json(inputs / "synthetic-projection.json")
    )
    pair = projection.derive_pair("direction-alpha", "direction-beta")
    _write_json(root / "capture" / "synthetic-pair.json", pair.canonical_payload())

    binding = latent_compass.DecisionBinding(
        host_id=episode.provenance.host_id,
        agent_family=episode.provenance.agent_family,
        store_id=episode.provenance.store_id,
        epoch=episode.provenance.epoch,
    )
    decision_payload = build_record_payload(
        decision_id=projection.decision_point_id,
        revision=1,
        binding=binding,
        captured_at=projection.captured_at,
        decision_authority="synthetic-operator",
        review_due_at="2026-11-16T17:00:00Z",
        expires_at="2027-08-16T17:00:00Z",
        projection=projection,
    )
    decision = latent_compass.admit_strategic_decision(decision_payload)

    with latent_compass.DecisionMemoryStore.create(
        root / "decision-memory",
        store_id=binding.store_id,
        host_id=binding.host_id,
        agent_family=binding.agent_family,
        epoch=binding.epoch,
        clock=lambda: "2026-08-16T17:00:00Z",
    ) as memory:
        memory.append_decision(decision_payload, clock=lambda: "2026-08-16T17:00:00Z")
        memory_integrity = memory.verify()

    reconciliation_payload = _reconciliation_payload(decision)
    reconciliation = latent_compass.admit_reconciliation(reconciliation_payload)
    with latent_compass.ReconciliationJournal.create(
        root / "reconciliation",
        store_id=binding.store_id,
        host_id=binding.host_id,
        agent_family=binding.agent_family,
        epoch=binding.epoch,
        clock=lambda: "2026-08-15T08:00:00Z",
    ) as source_journal:
        source_journal.append_reconciliation(
            reconciliation_payload,
            decision_record=decision,
            clock=lambda: "2026-08-20T10:00:00Z",
        )
        reconciliation_integrity = source_journal.verify()
        replay = latent_compass.replay_reconciliation(reconciliation, decision)

        plan = _prospective_plan(decision)
        with latent_compass.ProspectiveCollectionJournal.create(
            root / "prospective",
            plan=plan,
            clock=lambda: "2026-08-15T08:00:00Z",
        ) as collection:
            collection.start(clock=lambda: "2026-08-16T08:00:00Z")
            case = collection.enroll(
                decision,
                stratum="synthetic-demonstration",
                clock=lambda: "2026-08-16T18:00:00Z",
            )
            collection.reconcile(
                case.case_id,
                decision_record=decision,
                source_journal=source_journal,
                reconciliation_id=reconciliation.reconciliation_id,
                clock=lambda: "2026-08-20T10:00:00Z",
            )
            collection.close_collection(clock=lambda: "2026-08-21T08:00:00Z")
            collection_integrity = collection.verify()
            manifest = collection.manifest()
            diagnostic = collection.report()

    summary: dict[str, Any] = {
        "notice": SYNTHETIC_NOTICE,
        "root": str(root.resolve()),
        "episode_id": episode.episode_id,
        "projection_seal": projection.projection_seal(),
        "pair_input_seal": pair.input_seal(),
        "decision_record_seal": decision.record_seal(),
        "reconciliation_record_seal": reconciliation.record_seal(),
        "replay_seal": replay.replay_seal,
        "memory_integrity": memory_integrity.ok,
        "reconciliation_integrity": reconciliation_integrity.ok,
        "collection_integrity": collection_integrity.ok,
        "collection_state": manifest.collection_state,
        "eligible_count": manifest.eligible_count,
        "missingness": diagnostic.missingness,
        "diagnostic_report_seal": diagnostic.report_seal,
        "empirical_claim": False,
    }
    _write_json(root / "synthetic-walkthrough-summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="new output root; omit to use a temporary directory",
    )
    args = parser.parse_args(argv)
    try:
        if args.root is not None:
            summary = run(args.root)
        else:
            with tempfile.TemporaryDirectory(prefix="latent-compass-synthetic-") as temporary:
                summary = run(Path(temporary) / "walkthrough")
    except FileExistsError as exc:
        parser.error(str(exc))
    print(SYNTHETIC_NOTICE)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

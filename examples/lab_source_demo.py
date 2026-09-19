"""HOK-798 — offline demo: core, source observation, lab routing and shared budgets.

SYNTHETIC DEMONSTRATION ONLY. Every file this script reads is written by the
script itself into a fresh temporary directory; no real repository is ever
read, and no host, tool, model or process is ever started or dispatched.
Every routing decision this script accepts is non-authoritative advice per
ADR 0011 and :data:`latent_compass.lab.LAB_NON_AUTHORITY_NOTICE` — never an
authorization, and the CODEX/CLAUDE family case below is a declared lab
identity, not a live-host parity claim. Costs are settled conservatively at
each reservation's own ceiling: this script never fabricates a measured
dollar or token cost.

This is still a lab (see ``docs/adr/0011-experimental-active-diagnosis.md``):
no operational host adapter, no process execution, no index refresh, no
deletion. It threads the finite-model core
(:mod:`latent_compass.lab.planner`), the bounded literal-source bridge
(:mod:`latent_compass.lab.source_session`) and lab routing/budgets
(``latent_compass.lab.routing``) through one small, bounded loop.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from latent_compass.canonical import seal
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation
from latent_compass.lab import LAB_NON_AUTHORITY_NOTICE
from latent_compass.lab.model import load_model
from latent_compass.lab.observations import HostBinding, capture_source_snapshot
from latent_compass.lab.planner import propose
from latent_compass.lab.routing import (
    LAB_ROUTING_CONTRACT_VERSION,
    BudgetPoolState,
    BudgetReservation,
    BudgetSettlement,
    CapabilityKind,
    CapabilityObservation,
    HostCapabilitySnapshot,
    RouteRequest,
    RouteVerdict,
    SettlementOutcome,
    admit_host_capability_snapshot,
    admit_route_request,
    evaluate_route,
    reserve_budget,
    settle_budget,
)
from latent_compass.lab.source_session import (
    derive_lab_binding,
    load_source_probe_catalog,
    observe_probe_from_source,
)
from latent_compass.lab.state import DiagnosisStateRevision, initial_state

SYNTHETIC_NOTICE = (
    "SYNTHETIC DEMONSTRATION ONLY: every file read here is written by this script "
    "into a temporary directory; no real repository is read and no advice below is "
    "authoritative or a claim about a real host."
)

NOW = "2026-09-18T00:00:00Z"
HOST_ID = "demo-host"
ROOT_ID = "demo-source-root"
SCOPE = "lab-source-demo"
DEMO_MODEL_PATH = Path(__file__).resolve().parent / "lab-model.json"


def _catalog_payload() -> dict[str, Any]:
    """The complementary-probe model's own probes, mapped to two synthetic files."""
    return {
        "contract_version": "1.0.0",
        "probes": [
            {
                "probe_id": "check-a",
                "relative_paths": ["signal-a.txt"],
                "query": "HIGH-A",
                "present_outcome_id": "a-hi",
                "absent_outcome_id": "a-lo",
            },
            {
                "probe_id": "check-b",
                "relative_paths": ["signal-b.txt"],
                "query": "HIGH-B",
                "present_outcome_id": "b-hi",
                "absent_outcome_id": "b-lo",
            },
        ],
    }


def _write_synthetic_source(root: Path) -> None:
    (root / "signal-a.txt").write_text("synthetic marker\nHIGH-A\n", encoding="utf-8")
    (root / "signal-b.txt").write_text("synthetic marker\nHIGH-B\n", encoding="utf-8")


def _route_request(
    *,
    model_digest: str,
    state_digest: str,
    source_digest: str,
    candidate_capability_id: str,
    cost_ceiling: int,
    remaining_budget: int,
) -> RouteRequest:
    request_digest = seal(
        "lab.source-session.demo.request.v1",
        {
            "model_digest": model_digest,
            "state_digest": state_digest,
            "candidate_capability_id": candidate_capability_id,
        },
    )
    return admit_route_request(
        {
            "contract_version": LAB_ROUTING_CONTRACT_VERSION,
            "request_digest": request_digest,
            "model_digest": model_digest,
            "state_digest": state_digest,
            "source_digest": source_digest,
            "host_id": HOST_ID,
            "agent_family": AgentFamily.CLAUDE.value,
            "scope": SCOPE,
            "candidate_capability_id": candidate_capability_id,
            "candidate_kind": CapabilityKind.OBSERVATION.value,
            "requested_at": NOW,
            "expiry": NOW,
            "cost_ceiling": cost_ceiling,
            "remaining_budget": remaining_budget,
            "advisor_present": True,
            "kill_switch_engaged": False,
            "review_required": False,
            "review_satisfied": False,
            "semctx_proof_required": False,
            "semctx_proof_satisfied": False,
            "explicit_missing_authority": False,
            "explicit_missing_precondition": False,
            "fallback_observation_id": None,
        }
    )


def _host_capability_snapshot(
    *, source_digest: str, candidate_capability_id: str
) -> HostCapabilitySnapshot:
    return admit_host_capability_snapshot(
        {
            "contract_version": LAB_ROUTING_CONTRACT_VERSION,
            "host_id": HOST_ID,
            "agent_family": AgentFamily.CLAUDE.value,
            "scope": SCOPE,
            "current_source_digest": source_digest,
            "snapshot_taken_at": NOW,
            "observed_capabilities": [
                CapabilityObservation(
                    capability_id=candidate_capability_id,
                    kind=CapabilityKind.OBSERVATION,
                    observed_at=NOW,
                    expires_at=NOW,
                ).canonical_payload()
            ],
        }
    )


def run(root: Path) -> dict[str, Any]:
    """Propose, route, observe and settle a bounded number of probes; return a trace."""
    if root.exists():
        raise FileExistsError(f"refusing to reuse existing demo root: {root}")
    root.mkdir(parents=True)
    _write_synthetic_source(root)

    model = load_model(json.loads(DEMO_MODEL_PATH.read_text(encoding="utf-8")))
    snapshot = capture_source_snapshot(
        root,
        [Path("signal-a.txt"), Path("signal-b.txt")],
        host=HostBinding(host_id=HOST_ID, agent_family=AgentFamily.CLAUDE),
        root_id=ROOT_ID,
        captured_at=NOW,
        max_bytes_per_file=4096,
    )
    catalog = load_source_probe_catalog(_catalog_payload())
    binding = derive_lab_binding(model, snapshot, catalog)
    state = initial_state(model, state_id="lab-source-demo-episode-1", binding=binding)
    history: list[DiagnosisStateRevision] = [state]
    pool = BudgetPoolState(total_budget=len(model.probes), reservations=(), charges=())

    steps: list[dict[str, Any]] = []
    for _ in range(len(model.probes)):
        report = propose(
            model,
            state,
            budget=pool.available(),
            horizon=2,
            expected_binding=binding,
            history=None if state.revision == 0 else history,
        )
        recommended_probe_id = report.recommended_probe_id
        if report.recommended_action == "STOP" or recommended_probe_id is None:
            steps.append({"proposal": "STOP", "plan_value": report.plan_value})
            break

        probe_id = recommended_probe_id
        probe = model.probe_by_id(probe_id)
        assert probe is not None  # named by propose() from this same model

        request = _route_request(
            model_digest=model.model_seal(),
            state_digest=state.state_seal(),
            source_digest=binding.source_scope_digest,
            candidate_capability_id=probe_id,
            cost_ceiling=probe.cost,
            remaining_budget=pool.available(),
        )
        capabilities = _host_capability_snapshot(
            source_digest=binding.source_scope_digest, candidate_capability_id=probe_id
        )
        decision = evaluate_route(request, capabilities, now=NOW)
        step: dict[str, Any] = {
            "proposal": "PROBE",
            "probe_id": probe_id,
            "plan_value": report.plan_value,
            "routing_verdict": decision.verdict.value,
            "decision_seal": decision.decision_seal,
        }
        if decision.verdict is not RouteVerdict.ADVICE:
            step["abstained_reason"] = (
                decision.abstain_reason.value
                if decision.abstain_reason is not None
                else decision.escalate_reason.value
                if decision.escalate_reason is not None
                else None
            )
            steps.append(step)
            break

        reservation = BudgetReservation(
            reservation_id=f"reservation-{probe_id}", requested_maximum=probe.cost, reserved_at=NOW
        )
        pool = reserve_budget(pool, reservation)
        try:
            outcome = observe_probe_from_source(
                model,
                state,
                root=root,
                snapshot=snapshot,
                catalog=catalog,
                probe_id=probe_id,
                observation_id=f"obs-{probe_id}",
                observed_at=NOW,
                expected_host_id=HOST_ID,
                expected_agent_family=AgentFamily.CLAUDE,
                expected_root_id=ROOT_ID,
                history=None if state.revision == 0 else history,
            )
        except ContractViolation as exc:
            # Unknown actual cost on a refused reservation is charged at its own
            # ceiling, never refunded: settlement is conservative, not optimistic.
            pool = settle_budget(
                pool,
                BudgetSettlement(
                    reservation_id=reservation.reservation_id,
                    outcome=SettlementOutcome.FAILED,
                    actual_cost=None,
                    settled_at=NOW,
                ),
            )
            step["refused"] = exc.code
            steps.append(step)
            break

        pool = settle_budget(
            pool,
            BudgetSettlement(
                reservation_id=reservation.reservation_id,
                outcome=SettlementOutcome.SUCCEEDED,
                actual_cost=None,
                settled_at=NOW,
            ),
        )
        state = outcome.state
        history.append(state)
        step["outcome_id"] = outcome.outcome_id
        step["state_seal"] = state.state_seal()
        steps.append(step)

    summary: dict[str, Any] = {
        "notice": SYNTHETIC_NOTICE,
        "non_authority_notice": LAB_NON_AUTHORITY_NOTICE,
        "root": str(root.resolve()),
        "model_seal": model.model_seal(),
        "initial_binding_source_scope_digest": binding.source_scope_digest,
        "final_state_seal": state.state_seal(),
        "final_state_revision": state.revision,
        "remaining_budget": pool.available(),
        "steps": steps,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="new output root; omit to use a temporary directory"
    )
    args = parser.parse_args(argv)
    try:
        if args.root is not None:
            summary = run(args.root)
        else:
            with tempfile.TemporaryDirectory(prefix="latent-compass-lab-source-demo-") as temp:
                summary = run(Path(temp) / "lab-source-demo")
    except FileExistsError as exc:
        parser.error(str(exc))
    print(SYNTHETIC_NOTICE)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

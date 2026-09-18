# Lab modules operator guide (HOK-803/804/805/806)

This guide covers three pure Python modules inside `latent_compass.lab`:
`routing`, `evaluation` and `migration`. Their APIs have no host adapter or side effect.
Every function here is descriptive or advisory: it validates a declared input
and returns a sealed judgement over it. None of the three modules observes a
host, dispatches a model, runs a trial, or deletes, disables or moves
anything. Their reports carry non-authoritative literals
(`lab_only`, `experimental`, `dry_run`, `requires_external_authorization`, …)
so a caller cannot mistake a report for a permission.

## 1. Routing advice (`latent_compass.lab.routing`)

`evaluate_route` is a closed function of a `RouteRequest`, a
`HostCapabilitySnapshot` and a caller-supplied `now`: same three inputs, same
`RouteDecision`, every time. It returns exactly one of `ADVICE`, `ABSTAIN` or
`ESCALATE` — never an instruction to dispatch anything.

```python
from latent_compass.lab.routing import (
    admit_host_capability_snapshot,
    admit_route_request,
    evaluate_route,
)

request = admit_route_request(
    {
        "contract_version": "1.0.0",
        "request_digest": "sha256:" + "a" * 64,
        "model_digest": "sha256:" + "a" * 64,
        "state_digest": "sha256:" + "a" * 64,
        "source_digest": "sha256:" + "a" * 64,
        "host_id": "lab-host-alpha",
        "agent_family": "claude",
        "scope": "lab-scope-alpha",
        "candidate_capability_id": "capability-one",
        "candidate_kind": "TOOL",
        "requested_at": "2026-09-10T00:00:00Z",
        "expiry": "2026-09-20T00:00:00Z",
        "cost_ceiling": 5,
        "remaining_budget": 10,
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
snapshot = admit_host_capability_snapshot(
    {
        "contract_version": "1.0.0",
        "host_id": "lab-host-alpha",
        "agent_family": "claude",
        "scope": "lab-scope-alpha",
        "current_source_digest": "sha256:" + "a" * 64,
        "snapshot_taken_at": "2026-09-01T00:00:00Z",
        "observed_capabilities": [
            {
                "capability_id": "capability-one",
                "kind": "TOOL",
                "observed_at": "2026-09-01T00:00:00Z",
                "expires_at": "2026-09-30T00:00:00Z",
            }
        ],
    }
)
decision = evaluate_route(request, snapshot, now="2026-09-15T00:00:00Z")
decision.verdict  # RouteVerdict.ADVICE
```

A required review or Semctx proof check is read **before** the budget: a
`cost_ceiling` of zero never substitutes for either. A host capability
snapshot dated after `now` is refused (`ABSTAIN` / `CAPABILITY_UNCERTAIN`) —
a snapshot cannot claim to describe a host state that had not happened yet.

Every `RouteDecision` recomputes `decision_seal` from its own complete body,
which itself binds `request_seal` and `snapshot_seal` — domain-separated
seals over the *complete* canonical `RouteRequest` and `HostCapabilitySnapshot`
payloads — plus the plain `host_id` / `agent_family` / `scope` context. A
decision minted for one host, state or model cannot be replayed, by
constructing a new envelope around it, as if it described another: any
mutation to those fields makes `decision_seal` fail to reproduce. These are
integrity digests an external consumer matches against its own expected
context; they are never a signature, and this module treats no caller-supplied
approval boolean or digest as its own authorization. `evaluate_route`,
`reserve_budget` and `settle_budget` each revalidate every model instance
handed to them, so a `model_construct`-built or attribute-mutated forgery is
refused exactly as a freshly admitted payload with the same defect would be.

### Budget pools

A `BudgetPoolState` is immutable: `reserve_budget` and `settle_budget` each
return a *new* state. `total_budget` is the one ceiling a pool ever enforces;
`available()` is `total_budget` minus every outstanding reservation's maximum
minus every settled charge, and it is left **negative** on a genuine overrun
rather than clamped to zero — an overrun is a value a caller reads, never a
state this module hides. Once `available()` is negative, no new reservation
of any size, including zero, is accepted.

```python
from latent_compass.lab.routing import (
    BudgetPoolState,
    BudgetReservation,
    BudgetSettlement,
    reserve_budget,
    settle_budget,
)

pool = BudgetPoolState(total_budget=100)
pool = reserve_budget(
    pool,
    BudgetReservation(
        reservation_id="r-1", requested_maximum=40, reserved_at="2026-09-10T00:00:00Z"
    ),
)
pool = settle_budget(
    pool,
    BudgetSettlement(
        reservation_id="r-1",
        outcome="SUCCEEDED",
        actual_cost=None,
        settled_at="2026-09-10T00:00:05Z",
    ),
)
pool.available()  # 60 — an unknown actual_cost charges the reservation's own
# ceiling in full (CostBasis.CONSERVATIVE_MAXIMUM), never
# released back into the pool as if it were free.
```

## 2. Paired evaluation reporting (`latent_compass.lab.evaluation`)

`build_evaluation_report` turns a frozen `LabEvaluationProtocol` and a closed
set of `LabTrial` rows into a `LabEvaluationReport`. Every count in the report
is exact and descriptive: there is no p-value, no `approved` flag and no
`causal_gain` anywhere in this module. `status` is `DESCRIPTIVE_ONLY` when at
least one trial produced a non-`MISSING` outcome, and
`INSUFFICIENT_EVIDENCE` when the entire closed set was `MISSING`. Neither
value, nor anything else this module returns, is a causal, powered or
calibration claim — the real HOK-246/HOK-247 experiment this module could one
day report on requires independent evidence this module does not and cannot
supply.

A protocol's `pair_task_bindings` fixes exactly one task id per declared pair
id **before** any trial is admitted, so a trial cannot pair a baseline task
with an unrelated comparison task under the same pair id — every trial's
`task_id` must match the task its `pair_id` was bound to.

```python
from latent_compass.lab.evaluation import (
    admit_lab_evaluation_protocol,
    admit_lab_trial,
    build_evaluation_report,
)

protocol = admit_lab_evaluation_protocol(
    {
        "contract_version": "1.0.0",
        "protocol_id": "protocol-one",
        "candidate_identity": "candidate-one",
        "config_identity": "config-one",
        "source_identity": "source-one",
        "expected_arms": ["EXISTING_STACK", "SOURCE_ONLY", "CONTROLLER_PLUS_SOURCE"],
        "task_ids": ["task-1"],
        "pair_ids": ["pair-1"],
        "pair_task_bindings": [{"pair_id": "pair-1", "task_id": "task-1"}],
        "min_cost": 0,
        "max_cost": 100,
        "min_latency_ms": 0,
        "max_latency_ms": 1000,
        "non_inferiority_margin": 0.05,
        "cost_target": 20,
        "origin": "SYNTHETIC",
        "frozen_at": "2026-09-01T00:00:00Z",
    }
)
```

Every declared `(pair_id, arm)` slot must be submitted, even when nothing ran
for it — as an explicit `MISSING` trial, never dropped — so `per_arm` counts
stay exact denominators. A `PairedDelta.pairs_compared` counts **only** the
pairs where both the baseline and the comparison arm produced a non-`MISSING`
outcome: a missing baseline paired with an observed comparison success is
never counted as a gain, nor the reverse as a loss.
`PairedDelta.pairs_incomplete` names every pair excluded this way, so the
exclusion is visible in the report rather than silently folded into the
delta.

## 3. Retirement dry-run (`latent_compass.lab.migration`)

`build_retirement_dry_run` maps a declared inventory of `InventoryAsset`
records onto exactly three actions — `KEEP`, `DISABLE_LATER` and
`RETAIN_FOR_ROLLBACK` — and lists, per asset, the gate that remains missing.
There is no `apply` and no `delete` function in this module, and the report
it returns can never itself become one: `dry_run`, `requires_external_
verification`, `requires_external_authorization` and `activation_token` are
fixed literals on every report, regardless of how complete the declared
evidence is.

```python
from latent_compass.lab.migration import admit_inventory_asset, build_retirement_dry_run

asset = admit_inventory_asset(
    {
        "contract_version": "1.0.0",
        "asset_id": "asset-one",
        "provider_id": "provider-one",
        "role": "candidate-index",
        "scope": "lab-scope",
        "scope_kind": "PERSONAL_LAB",
        "host_id": "lab-host",
        "configuration_digest": "sha256:" + "a" * 64,
        "consumers": [{"consumer_id": "consumer-one"}],
        "classification": "CANDIDATE_INDEX",
        "evidence_references": [],
    }
)
report = build_retirement_dry_run((asset,), generated_at="2026-09-10T00:00:00Z")
report.dispositions[0].action  # RetirementAction.DISABLE_LATER — no evidence yet
```

Required evidence, authored data, source or symbolic tooling, the vault, and
any asset scoped as professional or unscoped are protected outright and
always `KEEP`. An asset with unknown consumers or an unknown classification is
also kept — ambiguity defaults to protection, never to eligibility. An
`EvidenceReference` is a claimed digest, nothing more: this module never
opens, fetches or verifies what it points at, so a populated reference only
removes the corresponding `*_EVIDENCE_ABSENT` gate from a report — never a
fact this module has itself confirmed. Four gates this module can **never**
itself close stay listed on every non-protected disposition regardless of how
complete the declared evidence is:
`INDEPENDENT_COMPARISON_ABSENT`, `LIVE_PILOT_ABSENT`,
`EXTERNAL_VERIFICATION_ABSENT` and `OWNER_DELETION_AUTHORITY_ABSENT` — an
independent comparison, a live pilot, external verification and the owner's
own irreversible-deletion authority are conditions no evidence reference, however
complete, can satisfy on an asset's behalf.

`inventory_digest` hashes each asset's *complete* canonical payload — scope,
classification, consumers and every evidence reference, not just its identity
and configuration — so swapping a single evidence digest changes both the
inventory digest and the report seal even when the resulting disposition does
not change.

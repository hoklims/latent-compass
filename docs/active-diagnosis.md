# `latent_compass.lab` — an isolated, experimental active-diagnosis lab

Status: **experimental**, gated for local implementation only. See
[ADR 0011](adr/0011-experimental-active-diagnosis.md) for the design record
and the boundary this package does not cross.

`latent_compass.lab` is a separate, additive package with its own `1.0.0`
experimental contract axes. It shares no version, seal domain or store with
`latent_compass`'s legacy authority, episode, decision-memory, reconciliation,
benchmark or prospective-collection contracts, and it is **not** wired into
the `latent-compass` command line. Nothing here executes, promotes, dispatches
or reaches any external system: a `PlanReport` is a recommendation computed
from a caller-supplied finite model, never a decision, and the plan report
carries a `non_authority_notice` saying so.

## What it does

You supply:

1. A finite **decision model** — a closed set of possible worlds with integer
   prior weights, terminal decisions with complete integer loss tables, and
   probes with an integer cost and a total, deterministic outcome table over
   worlds.
2. A **diagnosis state** — the model's prior, or the exact posterior after
   some sequence of observed probe outcomes.

The lab computes, exactly (using `fractions.Fraction`, never floating point):

- `R(E)`, the minimum expected loss among **admissible** decisions — a
  decision whose `required_evidence` has actually been observed, never merely
  a low expected loss;
- `V(E, budget, horizon)`, a bounded Bellman search over whether stopping now
  or buying one more affordable, not-yet-acquired probe first has the lower
  total expected loss.

Applying an observation exactly filters the posterior: because a probe's
outcome is a deterministic function of the true world, there is no likelihood
to approximate, only worlds to keep or drop.

## Boundary reminders

- **Core performs no source reading.** The core never opens the file or system an observation
  is about. `source_scope_digest` is an opaque digest you supply; binding the
  whole diagnosis episode to one digest, and revalidating it against real
  content, is adapter responsibility. The optional [source-session bridge](source-session.md)
  does this for explicitly listed local files and literal probes. Operational
  host integration remains deferred.
- **No probability calibration.** Every prior weight, cost and loss is your
  assumption, not evidence this package collected or checked.
- **No forced decision.** Every model must declare an unconditionally
  admissible abstain decision, so the planner always has a legal stopping
  point that does not depend on evidence that might never arrive.
- **Bounded, and says so.** `horizon`, `budget` and `max_expansions` are
  explicit ceilings (`python -m latent_compass.lab limits` prints all of
  them). A search that hits the expansion ceiling sets `search_exhausted` and
  `exact_for_declared_bounds: false`; even a search that completes is exact
  only *relative to the supplied finite model, budget and horizon*, never a
  claim about the real world.

## CLI walkthrough: complementary probes

`examples/lab-model.json` encodes a small world where two probes are
individually uninformative but jointly decisive — the classic case a
one-step-lookahead planner gets wrong. Two independent bits `a, b ∈ {lo, hi}`
give four equally likely worlds. `decide-equal` costs `10` when `a != b` and
`0` when `a == b`; `decide-different` is the mirror image; `decide-abstain`
costs a flat `3`. Each probe (`check-a`, `check-b`, cost `1` each) reveals
only one bit — alone, either leaves the posterior over `a == b` exactly at
50/50, so `R(E)` after either alone is unchanged from `R(∅)`.

```bash
python -m latent_compass.lab validate-model --model examples/lab-model.json

python -m latent_compass.lab init-state \
  --model examples/lab-model.json \
  --state-id demo-episode-1 \
  --host-id demo-host \
  --agent-family claude \
  --source-scope-digest sha256:00000000000000000000000000000000000000000000000000000000000000aa \
  > /tmp/state-0.json

# One-step lookahead: stopping (abstain, value 3) beats buying either probe
# alone (value 1 + 3 = 4), so horizon=1 recommends STOP.
python -m latent_compass.lab propose \
  --model examples/lab-model.json --state /tmp/state-0.json --budget 2 --horizon 1 \
  --host-id demo-host --agent-family claude \
  --source-scope-digest sha256:00000000000000000000000000000000000000000000000000000000000000aa

# Two-step lookahead finds the complementary pair: buy check-a (cost 1), then
# check-b (cost 1), landing on a fully identified world with loss 0 either
# way — total value 2, strictly better than stopping at 3. horizon=2
# recommends PROBE check-a.
python -m latent_compass.lab propose \
  --model examples/lab-model.json --state /tmp/state-0.json --budget 2 --horizon 2 \
  --host-id demo-host --agent-family claude \
  --source-scope-digest sha256:00000000000000000000000000000000000000000000000000000000000000aa
```

Continue the episode by applying the recommended probe's real outcome, then
propose again from the resulting state:

```bash
python -m latent_compass.lab apply-observation \
  --model examples/lab-model.json --state /tmp/state-0.json \
  --observation-id obs-1 --probe-id check-a --outcome-id a-lo \
  --observed-at 2026-09-18T00:00:00Z \
  --host-id demo-host --agent-family claude \
  --source-scope-digest sha256:00000000000000000000000000000000000000000000000000000000000000aa \
  > /tmp/state-1.json
```

An observation inconsistent with every remaining world (for example, applying
`check-a` a second time, or an outcome the model does not declare) is
refused with a typed error. The CLI emits JSON and does not persist states;
check the exit status before saving a returned document. Shell redirection
has its own file-writing behavior.

## Mandatory evidence: loss is not the only gate

`examples/lab-mandatory-evidence.json` has `decide-release` with a loss of
`0` on **every** world — but its `required_evidence` names
`(probe-approval, outcome-pass)`. If the actually-observed outcome is
`outcome-fail`, `decide-release` stays inadmissible regardless of its zero
loss: `R(E)` is computed only over decisions whose required evidence pairs
are a subset of what was actually observed, and a `PlanReport`'s
`decisions[].expected_loss` still reports the real number — `0` — right next
to `admissible: false`, so the refusal is legible rather than silent.

## Justification memory

Separate from the model/state pair, `latent_compass.lab.memory` records *why*
a claim might apply — never what to decide. A `FactAssertion` is a root
premise (provenance, scope, assertion time, optional expiry). A
`ClaimAssertion`'s `supports` is a disjunction of conjunctions over fact or
claim ids; `compute_applicability` replays the full event history and
computes grounded applicability as a least fixed point starting from eligible
facts. A claim that only cites itself, or a cycle with no independent root, is
never grounded — no matter how many times the fixpoint iterates — until a
`SupportRevision` adds an independent conjunction, and removing that revision
makes it inactive again. `FactRevocation` and `ClaimRevocation` are durable
appends, never in-place edits, and every dependent claim's applicability is
recomputed from the full history on every call: nothing is cached, and
nothing is trusted from a prior read. Applicability is not a truth verdict.

## Python API

```python
from latent_compass.lab import apply_observation, initial_state, load_model, propose
from latent_compass.lab.state import LabBinding
from latent_compass.episode import AgentFamily
import json
from pathlib import Path

model = load_model(json.loads(Path("examples/lab-model.json").read_text()))
binding = LabBinding(
    host_id="demo-host",
    agent_family=AgentFamily.CLAUDE,
    source_scope_digest="sha256:" + "0" * 62 + "aa",
)
state = initial_state(model, state_id="demo-episode-1", binding=binding)
report = propose(model, state, expected_binding=binding, budget=2, horizon=2)
assert report.recommended_action == "PROBE"
```

See `tests/test_lab_planner.py` for the independent, hand-verified exact
values this example produces at `horizon=1` versus `horizon=2`.


## Required replay context

Public `propose` and `apply_observation` calls require `expected_binding` from
the consumer. Revision zero is recomputed. A nonzero state also requires
`history=[initial_state, ..., current_state]`; the CLI accepts that JSON array
through `--history` and requires explicit host/family/source flags. Replay
rejects changed model, episode identity, binding, posterior or evidence, broken
seal chains, reused observation IDs and backwards observation times. Each
probe is observed at most once per episode. Public inputs are revalidated,
including nested data changed after construction.

Replay establishes consistency with caller-declared observations, not their
authenticity. Source adapters must establish content identity and start a new
episode after drift; a digest is not proof of source truth or verified Git HEAD.

Memory as-of queries use events both recorded and effective by that instant,
with expiry exclusive at the expiry time. Future assertions and revocations do
not change earlier views. Fact and claim IDs share one collision-free namespace.
Superseded support references are still validated as part of full history.
Timestamps use the existing canonical UTC format `YYYY-MM-DDTHH:MM:SSZ`.

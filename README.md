# Latent Compass

> A flight recorder and offline proving ground for strategic decisions made by
> coding agents.

[English](README.md) · [Français](README.fr.md)

Coding agents make consequential choices all day: which hypothesis to test,
which file to change, which failure to pursue, when to stop, and when to ask for
help. Most systems preserve the final patch. They do not preserve the decision
that produced it.

That creates a dangerous blind spot. A successful task does not prove that the
chosen strategy was sound. A failed task rarely tells us whether another
available direction would have worked. If an agent later “learns” from those
outcomes without strict evidence and authority boundaries, it can end up
grading its own work, rewriting its own history, and promoting its own policy.

Latent Compass was created to explore a harder question:

**Can an agent learn from strategic decisions without gaining the authority to
declare those decisions correct?**

The project starts with evidence, not intelligence. It captures decision
episodes, validates their contracts, records them in a tamper-evident local
ledger, and evaluates fixed policies offline. A deterministic external judge
remains authoritative. A human remains the only actor allowed to promote.

Latent Compass **validates and records**. It does not steer the agent.

## The problem it addresses

Imagine an agent facing three plausible directions:

1. patch the visible symptom;
2. inspect the contract that produced it;
3. stop and request a missing product decision.

The agent chooses one. Hours later, the task is either green or broken. What is
usually missing?

- the alternatives that were available before the choice;
- the evidence attached to each alternative;
- the logging propensity of the selected direction;
- the cost, violations, information gain, and reversibility observed later;
- a trustworthy record of what was known before the outcome;
- a separate authority capable of saying “continue” or “kill.”

Without those pieces, “learning from experience” is mostly storytelling after
the fact. Latent Compass exists to make that story falsifiable.

## Why it was built this way

The first design question was not “Which model should we train?” It was “What
must a learning loop never be allowed to do?”

That led to five non-negotiable boundaries:

1. **Capture before outcome.** Candidate evidence belongs to the moment before
   a direction is selected, not to a retrospective explanation.
2. **Refuse ambiguity.** Missing versions, malformed probabilities, invented
   fields, and unsupported evidence fail closed.
3. **Separate observation from authority.** The component that records a
   verdict cannot grant itself permission to act on that verdict.
4. **Keep holdout evidence scarce.** Validation can be replayed; claim-bearing
   holdout evidence cannot be spent repeatedly until it says what we want.
5. **Make “not enough evidence” a valid result.** Abstention and
   `KILL_DISCOVERY` are safer than manufacturing a winner.

The name reflects that role: a compass can describe direction without taking
the helm.

## How it works

```text
Agent reaches a decision point
            │
            ▼
Latent Compass validates the episode and candidate evidence
            │
            ▼
Host-bound ledger records an immutable, replayable observation
            │
            ▼
Offline benchmark compares fixed policies on sealed data
            │
            ▼
External judge evaluates evidence ─── Human retains promotion authority
```

The authority boundary is deliberate. Latent Compass never calls the external
judge, never writes back to it, and never turns a benchmark result into an
authorization.

## What exists today

### Demonstrated — implemented and covered by repository tests

- **Strict, versioned episode contracts.** Unknown fields, type coercions,
  non-finite values, non-canonical timestamps, invalid propensity distributions,
  and overstated observability are refused.
- **A fail-closed authority boundary.** Latent Compass emits no operational
  advisory. Even a locally consistent positive result is refused as
  `untrusted_evidence` when no external trust root is configured.
- **A host-bound append-only ledger.** It supports atomic append, replay,
  structural redaction, integrity verification, and durable anchors.
- **A reproducible offline benchmark.** Four pre-registered baseline policies
  run under the same budget on sealed validation data. Verification re-executes
  them instead of trusting a supplied checksum.
- **Holdout discipline.** The validation runner and pre-holdout planner do not
  open the holdout split. Consumption is atomic and keyed to the holdout corpus.
- **Judgeable pre-action capture.** Candidate-specific evidence can be captured
  as a separate immutable sidecar without leaking the selected action or later
  outcome.
- **Confinement.** Durable writes stay beneath an explicit root, refuse silent
  overwrite, and do not follow replacement links.

### Experimental — real, but not yet evidence of value

- The eight-family metric taxonomy and its thresholds.
- IPS with a support floor, SNIPS diagnostics, and the weighted empirical tail
  cost quantile introduced in benchmark contract 1.1.0.
- The synthetic corpus. It proves that the pipeline executes and refuses bad
  inputs; it is not evidence that any policy is good.
- Tombstone redaction and the local durable anchor.
- An externally evidenced keyless labeler. Its private workflow is bound by
  GitHub OIDC and Sigstore, but its conservative rubric produced abstention, not
  useful directional supervision.

### Projected — not implemented and not claimed

- A trained pairwise ranker or contextual bandit. That was HOK-182's intended
  lane; the issue was canceled before training because its data gate did not pass.
- Calibration for a live coding agent.
- Canary evaluation or integration with Semctx.
- Automatic promotion or execution authority.
- Any demonstrated improvement in agent outcomes. **No such claim is made.**

The first research epoch ended in `KILL_DISCOVERY`. That does not mean a ranker
failed: no ranker was trained. It means the project did not have enough eligible
directional supervision, calibrated outcomes, or claim-bearing holdout evidence
to authorize the next step. The refusal is part of the result.

## A concrete example

Suppose an agent is debugging an authorization failure. Before it acts, a
producer records three candidates:

```text
A — patch the failing condition locally
B — inspect the authority contract and its evidence provenance
C — stop because the required product authority is missing
```

Each candidate carries bounded pre-action evidence for success, violations,
cost, information, and reversibility. Latent Compass validates and seals that
projection. Later, an outcome may be attached to the episode.

What does Latent Compass do with it?

- It preserves what was known before the choice.
- It makes silent mutation detectable.
- It allows offline policies to be replayed against the recorded case.
- It exposes uncertainty and lack of support.

What does it not do?

- It does not claim which candidate was truly best.
- It does not convert an observed outcome into causal proof.
- It does not let a model promote itself because it scored well.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hoklims/latent-compass
cd latent-compass
uv sync --all-groups
```

Validate the complete repository gate:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q && uv build
```

Each step is independently runnable. `pytest` proves the behavioral contracts;
Ruff and mypy do not.

## Record and replay an episode

```bash
uv run latent-compass init --root ./store --store-id store-alpha \
    --host-id host-alpha --agent-family claude --epoch LC-2026-E1
uv run latent-compass validate --episode episode.json
uv run latent-compass append   --root ./store --episode episode.json
uv run latent-compass verify   --root ./store
uv run latent-compass replay   --root ./store
uv run latent-compass export   --root ./store --out ./store/snapshot.json
```

Capture a judgeable pre-action projection:

```bash
uv run latent-compass pairwise capture --projection ./projection.json \
    --root ./run --out ./run/captures/decision-0001.json
```

`--out` must remain inside `--root`. Omit it to write only to stdout. Exit codes
are part of the contract: `0` success, `2` usage, `3` refusal, `4` integrity
failure, and `5` store or filesystem failure.

## Run the offline benchmark

The committed corpus is synthetic demonstration data. The validation runner
uses it as a measuring instrument; the policies never reach a live agent.

```bash
uv run latent-compass benchmark manifest verify \
    --corpus-dir ./corpus/synthetic-v1 --manifest ./corpus/synthetic-v1/manifest.json
uv run latent-compass benchmark run \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1 \
    --root ./run --out ./run/report.json
uv run latent-compass benchmark verify --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1
uv run latent-compass benchmark holdout plan --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1 \
    --baseline least-uncertainty --root ./run --out ./run/holdout-plan.json
```

The benchmark runner, report verifier, and phase-one planner never read the
holdout split. Manifest authoring and verification do read all three split files
to derive and check their seals.

## Architecture

```text
vocabulary.py           actors, capabilities, advisories, lifecycle states
authority.py            refusals, transitions, reproduced evidence
episode.py              versioned decision episode contract
pairwise_capture.py     bounded pre-action projections and blinded pair inputs
governance.py           retention, minimization, redaction, deletion semantics
protocol.py             pre-registration, holdout discipline, continue/kill
ledger.py               append-only store, chain, anchor, replay, export
benchmark/              sealed offline baseline benchmark
cli.py                  the only entry point: read, validate, record, refuse
canonical.py            canonical serialization and domain-separated seals
contracts.py            versions, strict primitives, validation
errors.py               typed refusal vocabulary
```

Dependencies flow one way:

```text
errors → canonical → contracts → vocabulary → {episode, protocol}
       → authority → governance → ledger → cli
                   → benchmark → cli
```

`benchmark` imports neither `authority` nor `ledger`. Neither imports the
benchmark. A benchmark report is evidence, never authorization.

## What the evidence proves — and what it cannot

Latent Compass distinguishes consistency, integrity, authenticity, and
authority because they are different claims.

- A valid contract proves that an input has the expected shape.
- A recomputed seal proves that content matches a known preimage.
- A hash chain and anchor detect tampering within their threat model.
- A Sigstore bundle identifies a workflow and immutable input.
- None of those facts proves that an outcome is true, that evidence predates a
  decision, or that an actor is authorized to promote.

An administrator who can rewrite both a ledger and its local anchor can forge a
history that verifies. Detecting that attack requires an external anchor this
package does not have. The limitation is documented and tested.

## Documentation map

| Document | Purpose |
| --- | --- |
| [Authority boundary](docs/authority-boundary.md) | actors, capabilities, lifecycle, provenance, threat model |
| [Episode contract](docs/episode-contract.md) | the decision episode schema |
| [Evaluation protocol](docs/evaluation-protocol.md) | pre-registration and holdout discipline |
| [Benchmark protocol](docs/benchmark-protocol.md) | baselines, estimators, metrics, and limits |
| [Pairwise supervision](docs/pairwise-supervision.md) | labels, outcomes, calibration, and the data gate |
| [Judgeable projection](docs/judgeable-projection.md) | pre-action sidecars and blinded pair derivation |
| [Independent labeler](docs/independent-labeler.md) | external workflow identity, proof, rotation, and limits |
| [Corpus provenance](corpus/synthetic-v1/PROVENANCE.md) | how the synthetic corpus was produced and what it cannot show |
| [Ledger](docs/ledger.md) | chain, anchor, replay, redaction, and honest limits |
| [Architecture decisions](docs/adr/) | why the project chose its current boundaries |
| [Governance](GOVERNANCE.md) | data and project governance |
| [Contributing](CONTRIBUTING.md) | contribution rules and the required gate |
| [Security](SECURITY.md) | vulnerability reporting |

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

There are no build, coverage, or quality badges here. No public reproducible run
has earned them yet.

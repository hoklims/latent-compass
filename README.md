# latent-compass

Shadow-mode contracts and an append-only episode ledger for coding agents.

Latent Compass **validates and records**. It takes an advisory supplied by a
caller, refuses anything ambiguous, unversioned or non-canonical, and appends
what survives to a local, host-bound, append-only ledger. It holds no execution,
mutation, promotion or final-decision authority over anything it observes. A
deterministic external judge remains the authority on truth, invariants, impact,
gates and proof; Latent Compass records that judge's verdicts as observations
and never calls, appeals to, or writes back to it.

**Latent Compass emits no operational advisory.** It does not rank, vectorise or
calibrate an advisory for an agent to act on, and it holds no authority to act
on one. That is HOK-182 and does not exist yet. What exists here is the
boundary, the contracts, the governance and the ledger those things would have
to live inside.

Since HOK-188 it does run four pre-registered baseline policies — **offline,
over a sealed corpus, as a measuring instrument**. Those baselines pick a
direction for a *recorded* case and never reach a live agent: they are pure
functions inside a benchmark, and no result they produce is an advisory or an
authorisation. See [docs/benchmark-protocol.md](docs/benchmark-protocol.md).

## Status: what is demonstrated, what is experimental, what is projected

The distinction is load-bearing. Do not read a projected item as an implemented
one.

### Demonstrated — implemented here, covered by tests in this repository

- **An authority boundary that cannot be talked past.** Latent Compass is
  refused unconditionally, before any table is read — widening the capability
  grant does not help, and a test proves that by widening it. Reaching
  `PROMOTED` requires the `promote` capability, which only a human operator
  holds. In the built-in fail-closed path, the judge can enter `SHADOW` and no
  actor can advance farther until a trusted external attestation verifier exists.
- **Evidence provenance fail-closed.** Local validation and rescoring can prove
  consistency, not origin. `OFFLINE_VERIFIED`, `CANARY_ELIGIBLE` and `PROMOTED`
  are therefore refused as `untrusted_evidence` before evidence or ledger access
  whenever no external trust root is configured.
- **Versioned contracts that fail closed.** Every contract version is declared,
  never defaulted — at the top level and nested. Unknown fields, type coercions,
  non-finite numbers, non-canonical timestamps, propensity distributions that do
  not sum to one, and outcomes claiming more than their stated observability are
  all refused rather than repaired.
- **Structural redaction.** A declared redaction must correspond to a real
  absence, in one of the schema's optional fields. A marker over a present value
  is refused.
- **A pre-registered evaluation protocol whose holdout is spent once.**
  Consumption is keyed on the holdout *corpus* seal, so revising the protocol
  does not re-arm it. Check-and-consume is atomic across processes: concurrent
  spends of one corpus yield exactly one consumption, and concurrent spends of
  different corpora never lose an entry. Both are proved with real processes.
  Each receipt binds the protocol, epoch, raw-measurement seal and resulting
  verdict, so the authority boundary can reproduce rather than trust a decision.
- **A ledger that detects what a chain alone cannot.** Atomic appends; genesis
  recomputed from the binding, so relabelling the host breaks the chain; a
  durable anchor, so deleting the last rows is detected; a redaction refused on
  a corrupt store; no export seal at all from a store that does not verify.
- **An offline benchmark whose report is reproducible and re-executable.** Four
  pre-registered baselines run over a sealed corpus under one common budget, and
  produce an eight-family vector per seed with IPS as the primary estimate and
  SNIPS, support rate and effective sample size beside it. Split seals and
  cross-split disjointness are **recomputed from the real cases**, not believed.
  Two runs produce the same seal; verification re-executes the baselines rather
  than checking a supplied checksum; a report missing, repeating or renaming one
  grid coordinate does not validate, so it cannot be sealed. The runner never
  opens the holdout file, and a test spies on every file-open route to prove it.
- **A sealed pre-holdout selection checkpoint.** A pre-HOK-190 prerequisite replays the
  validation report, requires an explicitly named `CONTINUE` baseline, and
  freezes every relevant seal and version in a deterministic `HoldoutPlan`.
  It has no holdout input, ledger, execution capability or authority meaning.
- **A judgeable pre-action sidecar contract.** A separate immutable projection
  describes every candidate through bounded typed context and explicit evidence
  for five comparison dimensions, then derives canonical blinded pair inputs.
  It leaves `Episode 1.0.0` and baseline public views unchanged, performs no
  corpus or holdout I/O and creates no label, chronology or independence claim.
  A confined `pairwise capture` command publishes canonical sidecars atomically
  without overwriting an existing capture.
- **Confinement.** Every durable write resolves canonically and must land
  strictly inside a root named on the command line. Traversals, siblings,
  external absolute paths and silent overwrites are refused, and a refused
  command writes nothing. The package opens no socket and spawns no process.
- **Typed failure.** SQLite, UTF-8, JSON and filesystem failures become typed
  errors with documented exit codes. No failure reaches the user as a traceback.

### Experimental — implemented, but the design may change before it is relied on

- The **tombstone** redaction model. It preserves the row, the chain and the
  root seal while dropping the payload, so a redaction is visible rather than
  silent. It is a *logical* removal: it does not guarantee the bytes have left
  the device. Whether it satisfies a given erasure obligation is a legal
  question this repository does not answer.
- The **eight-family metric taxonomy** and the abstention threshold of `0.35`.
  Both are pre-registered values, not measured optima.
- The **median-over-seeds** aggregation rule. Deterministic and robust, but not
  compared against alternatives on real data.
- The **durable anchor**. It raises the cost of forging a ledger; it lives in
  the database it anchors, so it does not change the conclusion below.

- The **synthetic benchmark corpus** and every number taken on it. 16 validation
  cases with hand-written propensities and stipulated outcomes. It demonstrates
  that the pipeline runs, reproduces and refuses; it separates nothing. See
  [corpus/synthetic-v1/PROVENANCE.md](corpus/synthetic-v1/PROVENANCE.md).
- The **IPS-with-a-support-floor** estimator. Unbiased under overlap, but with no
  confidence interval, no doubly robust variant and no variance estimate. A
  target action below the floor refuses the run so baselines never use different
  analysis populations.
- The **pre-HOK-190 holdout plan**. It freezes an explicit selection after
  validation replay; it does not rank baselines, execute or consume the holdout,
  compare final results, provide an attestation, or complete HOK-190.
- The **externally evidenced keyless labeler**. A private repository runs a
  pinned GitHub-hosted workflow and signs canonical labels through GitHub OIDC
  and Sigstore. This repository records the accepted workflow identity and
  verification receipt, but cannot reproduce the private source locally. Its
  rubric produced only honest abstention, not useful HOK-190 supervision.

### Projected — not implemented, not started, no evidence

- **Any operational emission at all**: ranking, vectorisation, confidence
  estimation, calibration for a live agent, a contextual bandit, training.
  HOK-182. The HOK-188 baselines are offline benchmark policies over recorded
  cases; none of them reaches an agent, and none of them is learned.
- Any semantic preference-producing rubric or useful ranker supervision. The
  provisioned conservative labeler deliberately abstains on non-identical
  evidence.
- Canary evaluation and promotion in practice.
- The ranker training, calibration, then validation-to-holdout comparison required by HOK-190.
  The holdout is sealed and declared; neither the HOK-188 runner nor the
  phase-1 planner opens it. The committed synthetic holdout is authoring data,
  not an unseen claim-bearing holdout.
- Any claim that Latent Compass improves an agent's outcomes. **No such claim is
  made, and no measurement in this repository would support one.** The
  evaluation protocol exists so that a future claim could be falsified; it has
  been run only against the small synthetic corpus committed here, which is a
  demonstration of the pipeline and evidence of nothing else.

There are no build, coverage or quality badges in this README, because none has
been earned by a public, reproducible run.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
```

To install the built artifact into a clean environment instead:

```bash
uv build && uv pip install dist/latent_compass-0.1.0-py3-none-any.whl
```

## Validate

The full gate, in the order the project runs it:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q && uv build
```

Each step is independently runnable. `pytest` alone proves the contracts;
`ruff` and `mypy` prove nothing about behaviour.

## Use

```bash
uv run latent-compass init --root ./store --store-id store-alpha \
    --host-id host-alpha --agent-family claude --epoch LC-2026-E1
uv run latent-compass validate --episode episode.json
uv run latent-compass append   --root ./store --episode episode.json
uv run latent-compass verify   --root ./store
uv run latent-compass replay   --root ./store
uv run latent-compass export   --root ./store --out ./store/snapshot.json
uv run latent-compass pairwise capture --projection ./projection.json \
    --root ./run --out ./run/captures/decision-0001.json
```

`--out` must be lexically inside `--root`; omit it to print to stdout, which
writes nothing. Publication traverses every directory from a held volume/root
handle without following links. On Windows this surface accepts local drive
paths only: UNC/device namespaces, ADS, ambiguous trailing dots/spaces, control
characters and reserved DOS names are refused. On POSIX, held descriptors refer
to filesystem objects; an external rename can therefore make the returned
lexical path stale, but a replacement symlink is never followed. Exit codes are
part of the contract: `0` success, `2` usage, `3` refused (contract violation,
authority refusal, duplicate, closed epoch), `4` integrity failure, `5` store
or filesystem error.

Ask the boundary itself what it permits:

```bash
uv run latent-compass authority boundary
uv run latent-compass authority transition --from-state DEFINE --to-state SHADOW \
    --actor latent_compass --protocol protocol.json   # refused, exit 3
```

The current CLI deliberately refuses a promotion even with internally valid
artefacts, because no external attestation verifier is configured:

```bash
uv run latent-compass protocol verdict --protocol protocol.json \
    --measurements holdout.json --root ./run --holdout-ledger ./run/usage.json \
    --out ./run/verdict.json
uv run latent-compass authority transition --from-state CANARY_ELIGIBLE \
    --to-state PROMOTED --actor human_operator \
    --protocol protocol.json --measurements holdout.json \
    --verdict ./run/verdict.json --holdout-ledger ./run/usage.json --human-ack
# refused: untrusted_evidence (exit 3)
```

Run the offline benchmark on the demonstration corpus, then have it re-executed:

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

There is no `--holdout-ledger` option on the benchmark surface. The benchmark
runner, report verifier and phase-1 planner never read the holdout split; the
separate manifest authoring and verification commands necessarily read all
three files to derive and check their seals.

## Architecture

```
vocabulary.py           actors, capabilities, advisories, lifecycle states
authority.py   HOK-184  the boundary: refusals, transitions, reproduced evidence
episode.py     HOK-185  the versioned episode contract
pairwise_capture.py     bounded pre-action projections and canonical pair inputs
governance.py  HOK-185  retention, minimisation, redaction, deletion semantics
protocol.py    HOK-186  pre-registration, holdout discipline, continue/kill
ledger.py      HOK-187  the local append-only store, chain, anchor, replay, export
benchmark/     HOK-188  the offline baseline benchmark
  corpus.py               cases, the public projection, manifests, split seals
  spec.py                 the sealed plan, bound to a pre-registration
  budget.py               the one grant every baseline receives
  baselines.py            the closed registry of four pure baselines
  ope.py                  eligibility, importance weights, IPS/SNIPS, support
  metrics.py              the eight families, per seed
  report.py               the sealed report contract and its grid invariant
  runner.py               run and verify; VALIDATION only
cli.py                  the only entry point; read, validate, capture, record, refuse
canonical.py            canonical serialisation and domain-separated seals
contracts.py            contract versions, field primitives, strict validation
errors.py               the typed refusal vocabulary
```

Dependencies flow one way:

```
errors → canonical → contracts → vocabulary → {episode, protocol}
       → authority → governance → ledger → cli
                   → benchmark → cli
```

`benchmark` imports neither `authority` nor `ledger`, and neither imports it. A
benchmark report is not an authorisation and the boundary must never accept one
as evidence; a test asserts the import direction in both directions.

`vocabulary` exists so that `authority` can depend on `protocol` — which it must,
in order to recompute a verdict's seal — without a cycle. See
[ADR 0002](docs/adr/0002-verified-evidence-and-durable-anchors.md).

### The authority boundary in one paragraph

`authorize_transition` refuses Latent Compass **before it reads any table**. The
security property is not "the capability check happens first" — ordering is a
property of one implementation, not an invariant. The property is that the
refusal is unconditional and table-independent: widening the grant, by any
means, does not make Latent Compass able to authorise anything. Beyond that,
every transition past shadow entry is refused until an external trust root can
attest the origin of the otherwise reproducible artefacts.

Full statement: [docs/authority-boundary.md](docs/authority-boundary.md).

### What the ledger does not prove

A hash chain plus a durable anchor detects tampering by anyone who cannot
rewrite **both**. It establishes **no authenticity**. An administrator with
write access can recompute the chain and the anchor together from a forged
history and produce a store that verifies perfectly — and a test in this
repository demonstrates exactly that. Detecting a wholesale forgery needs an
external anchor this package does not have. See [docs/ledger.md](docs/ledger.md).

## Documentation

| Document | Subject |
| --- | --- |
| [docs/authority-boundary.md](docs/authority-boundary.md) | actors, capabilities, lifecycle, evidence provenance, threat model |
| [docs/episode-contract.md](docs/episode-contract.md) | the episode schema, field by field |
| [docs/evaluation-protocol.md](docs/evaluation-protocol.md) | pre-registration and holdout discipline |
| [docs/benchmark-protocol.md](docs/benchmark-protocol.md) | the offline baseline benchmark, its estimator and its limits |
| [docs/pairwise-supervision.md](docs/pairwise-supervision.md) | independent pairwise labels, observed-outcome calibration and the data gate |
| [docs/judgeable-projection.md](docs/judgeable-projection.md) | the HOK-234 pre-action sidecar, pair derivation and proof limits |
| [docs/independent-labeler.md](docs/independent-labeler.md) | the HOK-235 private keyless labeler, trust policy, proof run and rotation |
| [docs/pairwise-corpus-readiness.md](docs/pairwise-corpus-readiness.md) | HOK-225 refusal on the current opaque candidate contract and its exit criteria |
| [corpus/synthetic-v1/PROVENANCE.md](corpus/synthetic-v1/PROVENANCE.md) | where the demonstration corpus came from, and what it cannot show |
| [docs/ledger.md](docs/ledger.md) | store format, chain, anchor, atomicity, honest limits |
| [docs/adr/0001-minimal-durable-stack.md](docs/adr/0001-minimal-durable-stack.md) | why this stack, and what was refused |
| [docs/adr/0002-verified-evidence-and-durable-anchors.md](docs/adr/0002-verified-evidence-and-durable-anchors.md) | the corrective tranche, and what it changed |
| [docs/adr/0003-evidence-provenance-fails-closed.md](docs/adr/0003-evidence-provenance-fails-closed.md) | why positive authority transitions require an external trust root |
| [docs/adr/0004-independent-pairwise-supervision.md](docs/adr/0004-independent-pairwise-supervision.md) | why judge preferences and observed outcomes remain separate |
| [docs/adr/0005-judgeable-pre-action-sidecar.md](docs/adr/0005-judgeable-pre-action-sidecar.md) | why judgeable capture is a separate compatible sidecar |
| [docs/adr/0006-keyless-independent-pairwise-labeler.md](docs/adr/0006-keyless-independent-pairwise-labeler.md) | why the external labeler uses GitHub OIDC, Sigstore and an exact workflow SHA |
| [docs/adr/0007-correct-off-policy-tail-and-labeler-trust.md](docs/adr/0007-correct-off-policy-tail-and-labeler-trust.md) | why `TAIL` preserves cost units and labeler evidence matches the accepted workflow SHA |
| [docs/licenses/dependency-audit.md](docs/licenses/dependency-audit.md) | audited licence inventory |
| [GOVERNANCE.md](GOVERNANCE.md) | data provenance, retention, deletion, project governance |
| [SECURITY.md](SECURITY.md) | vulnerability reporting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to contribute |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | expected conduct |

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

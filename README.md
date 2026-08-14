# latent-compass

Shadow-mode contracts and an append-only episode ledger for coding agents.

Latent Compass **validates and records**. It takes an advisory supplied by a
caller, refuses anything ambiguous, unversioned or non-canonical, and appends
what survives to a local, host-bound, append-only ledger. It holds no execution,
mutation, promotion or final-decision authority over anything it observes. A
deterministic external judge remains the authority on truth, invariants, impact,
gates and proof; Latent Compass records that judge's verdicts as observations
and never calls, appeals to, or writes back to it.

**This foundation does not compute anything about a decision.** It does not
emit, rank, vectorise or calibrate advisories. That is HOK-182 and does not
exist yet. What exists here is the boundary, the contracts, the governance and
the ledger those things would have to live inside.

## Status: what is demonstrated, what is experimental, what is projected

The distinction is load-bearing. Do not read a projected item as an implemented
one.

### Demonstrated — implemented here, covered by tests in this repository

- **An authority boundary that cannot be talked past.** Latent Compass is
  refused unconditionally, before any table is read — widening the capability
  grant does not help, and a test proves that by widening it. Reaching
  `PROMOTED` requires the `promote` capability, which only a human operator
  holds, so the external judge can advance a candidate and never promote one.
- **Authorisation on verified evidence.** A positive authorisation beyond entry
  into shadow requires a real pre-registration and a real verdict, whose seals
  are recomputed from their own contents. An invented seal, or a verdict edited
  from `KILL` to `CONTINUE` after sealing, is refused. There is no parameter
  through which a caller can assert a conclusion.
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

### Projected — not implemented, not started, no evidence

- **Any emission at all**: ranking, vectorisation, confidence estimation,
  calibration, a policy, a contextual bandit, training. HOK-182.
- Any adapter to a deterministic judge.
- Canary evaluation and promotion in practice.
- Any claim that Latent Compass improves an agent's outcomes. **No such claim is
  made, and no measurement in this repository would support one.** The
  evaluation protocol exists so that a future claim could be falsified; it has
  not been run against a real corpus.

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
```

`--out` must resolve strictly inside `--root`; omit it to print to stdout, which
writes nothing. Exit codes are part of the contract: `0` success, `2` usage, `3`
refused (contract violation, authority refusal, duplicate, closed epoch), `4`
integrity failure, `5` store or filesystem error.

Ask the boundary itself what it permits:

```bash
uv run latent-compass authority boundary
uv run latent-compass authority transition --from-state DEFINE --to-state SHADOW \
    --actor latent_compass --protocol protocol.json   # refused, exit 3
```

Authorising a promotion requires the artefacts, not assertions about them:

```bash
uv run latent-compass protocol verdict --protocol protocol.json \
    --measurements holdout.json --root ./run --holdout-ledger ./run/usage.json \
    --out ./run/verdict.json
uv run latent-compass authority transition --from-state CANARY_ELIGIBLE \
    --to-state PROMOTED --actor human_operator \
    --protocol protocol.json --measurements holdout.json \
    --verdict ./run/verdict.json --holdout-ledger ./run/usage.json --human-ack
```

## Architecture

```
vocabulary.py           actors, capabilities, advisories, lifecycle states
authority.py   HOK-184  the boundary: refusals, transitions, verified evidence
episode.py     HOK-185  the versioned episode contract
governance.py  HOK-185  retention, minimisation, redaction, deletion semantics
protocol.py    HOK-186  pre-registration, holdout discipline, continue/kill
ledger.py      HOK-187  the local append-only store, chain, anchor, replay, export
cli.py                  the only entry point; read, validate, record, refuse
canonical.py            canonical serialisation and domain-separated seals
contracts.py            contract versions, field primitives, strict validation
errors.py               the typed refusal vocabulary
```

Dependencies flow one way:

```
errors → canonical → contracts → vocabulary → {episode, protocol}
       → authority → governance → ledger → cli
```

`vocabulary` exists so that `authority` can depend on `protocol` — which it must,
in order to recompute a verdict's seal — without a cycle. See
[ADR 0002](docs/adr/0002-verified-evidence-and-durable-anchors.md).

### The authority boundary in one paragraph

`authorize_transition` refuses Latent Compass **before it reads any table**. The
security property is not "the capability check happens first" — ordering is a
property of one implementation, not an invariant. The property is that the
refusal is unconditional and table-independent: widening the grant, by any
means, does not make Latent Compass able to authorise anything. Beyond that,
every positive authorisation past shadow entry is backed by artefacts whose
seals are recomputed here.

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
| [docs/authority-boundary.md](docs/authority-boundary.md) | actors, capabilities, lifecycle, verified evidence, threat model |
| [docs/episode-contract.md](docs/episode-contract.md) | the episode schema, field by field |
| [docs/evaluation-protocol.md](docs/evaluation-protocol.md) | pre-registration and holdout discipline |
| [docs/ledger.md](docs/ledger.md) | store format, chain, anchor, atomicity, honest limits |
| [docs/adr/0001-minimal-durable-stack.md](docs/adr/0001-minimal-durable-stack.md) | why this stack, and what was refused |
| [docs/adr/0002-verified-evidence-and-durable-anchors.md](docs/adr/0002-verified-evidence-and-durable-anchors.md) | the corrective tranche, and what it changed |
| [docs/licenses/dependency-audit.md](docs/licenses/dependency-audit.md) | audited licence inventory |
| [GOVERNANCE.md](GOVERNANCE.md) | data provenance, retention, deletion, project governance |
| [SECURITY.md](SECURITY.md) | vulnerability reporting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to contribute |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | expected conduct |

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

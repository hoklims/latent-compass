# Offline baseline benchmark (HOK-188)

Contract version `1.0.0`. Implementation: `src/latent_compass/benchmark/`.

Four pre-registered baselines are run over the `VALIDATION` split of a sealed
corpus, under one common deterministic budget, evaluated against logged bandit
feedback, and written into a versioned, reproducible, independently verifiable
vector report.

The protocol seal proves integrity of the declared protocol, and the spec seal
proves integrity of the benchmark plan. A seal **does not prove when the plan was
registered**. Claim-bearing use therefore requires an external durable anchor
established before data access (for example a reviewed VCS commit or a trusted
transparency log). This repository's unclaimed synthetic demonstration provides
no chronology proof and makes no performance claim.

**What this does not do.** It trains nothing, fits nothing, tunes nothing and
adapts nothing. It emits no operational advisory and cannot influence any agent:
the baselines are pure functions over a public projection of a recorded case,
and the benchmark is a measuring instrument pointed at a fixed corpus. Latent
Compass still holds no execution, mutation, promotion or final-decision
authority over anything it observes.

**What it does not show.** The corpus in this repository is small and synthetic.
Running the pipeline on it demonstrates that the pipeline runs, reproduces and
refuses correctly. It supports **no** claim that any baseline is good, that one
beats another, or that Latent Compass improves an agent's outcomes. See
[`corpus/synthetic-v1/PROVENANCE.md`](../corpus/synthetic-v1/PROVENANCE.md).

## The corpus contract

A benchmark case wraps one episode and adds only what the bench needs.

| Field | Meaning |
| --- | --- |
| `case_id` | stable identity, unique within a split and across all three |
| `split` | which split the case belongs to; must match the file it is in |
| `distribution_kind` | `NOMINAL` or `SHIFT` |
| `distribution_group` | `nominal` for the nominal population; any other name is a shift group |
| `ambiguous` | whether the recorded decision was an ambiguous one |
| `observation_cutoff` | the instant this case's outcome was frozen |
| `observation_horizon_seconds` | how long the recorder waited |
| `episode` | the unmodified HOK-185 episode |

A case whose episode records no logged direction is **refused at load**:
off-policy evaluation is undefined without a logged action drawn from a known
distribution. Outcome *maturity* is a different matter — it is classified and
reported, never used to make a case disappear.

### What a baseline may see

`PublicCaseView` carries exactly three fields — `case_id`, `state`,
`candidates` — and each candidate carries only `direction_id`, `propensity` and
`prior_uncertainty`. There is no historically selected direction, no outcome,
no economics, no external verdict, and no scorer label, because the projection
does not contain them. It is a type boundary, not a convention: the view is
built field by field from three permitted sources, so a future episode field is
absent here until someone adds it deliberately.

Candidate propensities *are* public. They describe the logging policy, not its
realised draw, and the fourth baseline is defined in terms of them.

### Splits, files and seals

The three splits live in **physically distinct files**. A single file holding
several splits would make "the holdout was never opened" unprovable.

A split's `corpus_seal` is computed from the canonical payloads of the cases
actually present, in canonical case order. Consequences:

- permuting the file leaves the seal, and the whole report, unchanged;
- editing any byte of any case moves the seal, and the run refuses;
- a manifest that merely *declares* a seal proves nothing — `manifest verify`
  recomputes all three from the real files and refuses on any mismatch.

Case identity **and** episode identity must be disjoint across the three
splits, and disjointness is *checked* here rather than declared. This closes,
for the benchmark corpus, the gap `docs/evaluation-protocol.md` names as an
honest limit of the protocol layer: the protocol still cannot verify a seal
supplied to it by an operator; the benchmark computes its own.

The manifest also refuses a validation split with no ambiguous case, no shift
group, or no nominal population — a `DRIFT` number measured against an empty
comparison would validate, seal, and mean nothing.

## The four baselines

A **closed** registry of four pure functions. No plugin hook, no callback
parameter, no dynamic import, no weights file, no fit step, no network.

| Identity | Kind | Rule |
| --- | --- | --- |
| `fixed-canonical` | `TRIVIAL` | the first `direction_id` in canonical order |
| `least-uncertainty` | `STRONG` | smallest `prior_uncertainty`; canonical tie-break |
| `seeded-uniform` | `TRIVIAL` | uniform pick derived from `(spec_seal, corpus_seal, case_id, seed)` |
| `logged-propensity-arbiter` | `STRONG` | highest logged propensity, then lowest uncertainty, then canonical |

`seeded-uniform` touches no global random state. Its choice for a case depends
on that case alone, so it is unchanged by the order in which cases run, by how
many ran first, or by whether another baseline ran at all.

Algorithm versions are pinned next to the code they describe, and their declared
identity, kind and version are sealed into the registry and checked before any
case is read. The seal does not hash Python function bodies: changing an
implementation therefore requires a reviewed version bump, enforced by the
discriminating regression tests. Report verification re-executes the installed
implementation and detects any observable divergence from the supplied report.

Each trial produces a direction, a finite confidence in `[0, 1]`, a budget
receipt, the baseline identity and version, and a `binding_seal` over
`(spec_seal, corpus_seal, baseline, version, case, seed, selection)`. A receipt
lifted from another run, corpus, baseline or seed does not reproduce that seal.

## The common budget

One `BudgetGrant`, sealed into the spec, is handed to all four baselines:

| Cap | Meaning |
| --- | --- |
| `max_cases` | corpus size ceiling; checked before any baseline runs |
| `max_candidate_inspections_per_case` | candidate looks per trial |
| `max_random_draws_per_case` | pseudo-random draws per trial |

Fairness is not a property of the runner's discipline; it is a property of
there being one grant. Consumption may differ — `seeded-uniform` spends a draw
and the others do not — but caps are enforced on the **first** operation past
the line, not tallied afterwards. A baseline that has already inspected a
forbidden candidate has already had the unfair look.

Nothing consults wall-clock time. A wall-clock budget would make "the report
differs because the host was slower" indistinguishable from "the report differs
because the result changed".

## Off-policy evaluation

The corpus records what a logging policy did and what happened next. It does
not record what would have happened had a baseline chosen differently.

For a deterministic target policy `pi` and a logged action `a` drawn with
probability `mu(a|x)`:

```
w_i   = pi(a_i|x_i) / mu(a_i|x_i)        # 1/mu when the target agrees, else 0
IPS   = mean_i( w_i * y_i )              # primary
SNIPS = sum_i(w_i * y_i) / sum_i(w_i)    # sensitivity diagnostic only
```

`IPS` is the **primary** value and the one that reaches the measurement set.
`SNIPS` has lower variance but is biased in finite samples, so it stays a
diagnostic. When `sum(w) == 0`, `SNIPS` is *undefined* and is reported as
undefined rather than as zero.

Doubly robust estimation would need an outcome model. There is none here, and
inventing one would make the benchmark depend on a fitted artefact this tranche
deliberately does not have.

### One fixed analysis population

Every case reaches exactly one state, and `SupportDiagnostics` refuses to
validate unless the states account for the whole corpus:

| State | Meaning |
| --- | --- |
| `ELIGIBLE` | observed by the cutoff, complete, target action supported |
| `UNOBSERVABLE` | observability `NONE` — it could never have been seen |
| `CENSORED_IMMATURE` | observable, but not observed by this case's cutoff |
| `INCOMPLETE_OUTCOME` | observed, but missing a field the estimator needs |
| `OUT_OF_SUPPORT` | `mu(target action)` below the pre-registered floor |

An incomplete outcome is never defaulted to zero. A censored one is never
counted as a failure. Those outcome states depend only on the logged corpus, so
every baseline uses the same analysis population. If any target action falls
below the common support floor, the whole run refuses: it is not removed from
one baseline's denominator while remaining in another's.

Each baseline and seed also reports eligible-case count, support rate,
non-zero-weight count, weight sum, maximum and `p50`/`p90`/`p99` quantiles, and
effective sample size `(sum w)^2 / sum(w^2)`.

### The support caveat this benchmark cannot escape

IPS is only meaningful where the logging policy could have produced the target
action. A near-deterministic logging policy gives most of its mass to one
direction, so a baseline that reproduces that direction inherits a large
effective sample while every other baseline collapses towards zero non-zero
weights. That is a property of the **data**, not evidence about the baselines.
`effective_sample_size` is reported precisely so a reader can see it, and it is
why no comparison in this package is stated as a ranking.

## The eight metric families

Six are IPS values over an observed channel; two are shaped differently and are
pre-registered as such.

| Family | Metric name | Direction | Definition |
| --- | --- | --- | --- |
| `SUCCESS` | `ips-success` | higher | IPS over observed success |
| `VIOLATION` | `ips-violations` | lower | IPS over observed violation counts |
| `COST` | `ips-cost` | lower | IPS over observed cost |
| `INFORMATION` | `ips-information-gain` | higher | IPS over observed information gain |
| `REVERSIBILITY` | `ips-reversibility` | higher | IPS over observed reversibility |
| `CALIBRATION` | `ips-brier` | lower | IPS over `(confidence - success)^2` |
| `TAIL` | `tail-weighted-cost-quantile` | lower | nearest-rank quantile of `w_i * cost_i` at the spec's `tail_quantile` |
| `DRIFT` | `drift-max-success-gap` | lower | largest `abs` gap between the nominal success estimate and any one shift group's |

Quantiles are **nearest-rank**, never interpolating: interpolation invents a
value no case produced, and every number in the report has to be traceable to a
case. `DRIFT` refuses rather than reports when a stratum has no eligible case.

There is no global mean and no composite score. Eight numbers stay eight
numbers; a scalar champion would hide the trade-off the vector exists to expose.

### Per seed first, aggregated afterwards

Nothing in the benchmark aggregates across seeds. The raw per-seed vector
reaches the report, and HOK-181's median-over-seeds rule is applied afterwards
to a benchmark-scoped diagnostic measurement set. Collapsing seeds earlier
would put an aggregation rule in two places.

The spec **binds**; it does not restate. Thresholds, families and directions
live in the pre-registration and nowhere else. The spec is refused unless the
pre-registration fixes exactly these eight names, with these families and these
directions, and declares exactly the four baseline names with matching kinds.

## The report

The report is the whole run, not a summary of it: every raw receipt, every
support diagnostic, every per-seed vector, and benchmark-scoped diagnostic
measurement and verdict summaries.

**Determinism is structural.** Nothing time-varying, host-varying or
path-varying enters the sealed body. No wall clock, no corpus directory, no
temporary path, no iteration order — cases, seeds and baselines are all carried
in canonical order. Two runs of the same spec against the same corpus produce
the same canonical payload and the same `report_seal`, on any host.

**The grid is validated, not assumed.** A report is refused unless every
baseline carries exactly the declared case set crossed with the declared seed
set: no missing coordinate, no extra one, no duplicate. A run that quietly
skipped an unfavourable seed does not validate, so it can never be sealed.

**A refused run seals nothing.** The report is constructed once, at the end,
from values that all already exist, so there is no partial artefact to be
mistaken for a completed one.

### Verification is re-execution

`benchmark verify` recomputes the carried seal **and** re-executes the four
baselines against the real corpus, then compares the entire canonical payload.
Checking a supplied checksum alone is not a proof: a forged report sealed
consistently passes that check and fails this one.

### A benchmark report is not an authorisation

`authority.py` does not import this package, and this package does not import
`authority.py` or `ledger.py`. The summaries carry the strict scope
`BENCHMARK_DIAGNOSTIC_ONLY`, which makes them structurally incompatible with
HOK-181 `MeasurementSet` and `Verdict` evidence. A hostile regression passes
them directly to `authorize_transition` and proves the authority boundary
refuses them.

This marker is **not** a provenance attestation: an untrusted caller can remove
it and reconstruct structurally valid HOK-181 evidence from the raw values. The
authority contract therefore refuses every evidence-bearing positive
transition without a trusted external attestation. No verifier or trust root is
implemented yet, so benchmark results cannot prove that `OFFLINE_VERIFIED` is
ready and the built-in path stays fail-closed.

## The command line

```bash
# derive a manifest from the real split files
uv run latent-compass benchmark manifest build \
    --root ./out --corpus-dir ./corpus/synthetic-v1 \
    --corpus-id lc-synthetic-bench --corpus-version v1.0.0 \
    --train train.json --validation validation.json --holdout holdout.json \
    --origin "generated by tests/corpus_generator.py" --licence Apache-2.0 \
    --synthetic --description "pipeline demonstration corpus" \
    --out ./out/manifest.json

# recompute every seal from the real files
uv run latent-compass benchmark manifest verify \
    --corpus-dir ./corpus/synthetic-v1 --manifest ./corpus/synthetic-v1/manifest.json

# validate and seal the plan
uv run latent-compass benchmark spec validate \
    --spec ./corpus/synthetic-v1/spec.json \
    --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json

# run the four baselines on VALIDATION
uv run latent-compass benchmark run \
    --spec ./corpus/synthetic-v1/spec.json \
    --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json \
    --corpus-dir ./corpus/synthetic-v1 \
    --root ./run --out ./run/report.json

# re-execute and compare the whole report
uv run latent-compass benchmark verify --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json \
    --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json \
    --corpus-dir ./corpus/synthetic-v1

# pre-HOK-190 prerequisite: explicitly freeze a validation CONTINUE baseline
uv run latent-compass benchmark holdout plan --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json \
    --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json \
    --corpus-dir ./corpus/synthetic-v1 \
    --baseline least-uncertainty \
    --root ./run --out ./run/holdout-plan.json
```

`--root` is required whenever a command writes. `--out` must be lexically inside
it; the shared handle-relative writer refuses traversal, siblings, external
paths, links/reparse points and ambiguous Windows names. An existing file is
never silently overwritten, and a refused command writes nothing.

**There is no `--holdout-ledger` option anywhere on this surface.** The pre-HOK-190
phase-1 command has no holdout-data argument and cannot consume one.

## The holdout is never opened by the runner

The runner resolves and reads exactly one path: the `VALIDATION` entry of the
manifest. The holdout's seal reaches the report from the manifest, established
by the separate corpus-authoring path. Three guards stand in front of a holdout
read, and the first two fire before a filesystem path has been constructed:

1. `BenchmarkSpec` refuses any `executed_split` but `VALIDATION` during model
   validation;
2. `run_benchmark` re-checks the split before resolving any path;
3. only the `VALIDATION` manifest entry is ever resolved.

A test spies on `Path.read_text`, `Path.open` and the builtin `open` for the
whole run and asserts the holdout file's resolved path never appears.

## Pre-HOK-190 prerequisite: the sealed pre-holdout plan

`benchmark holdout plan` creates an immutable `HoldoutPlan` before any final
holdout execution. It first calls the same replay verifier described above, so
a report that was edited and then honestly re-sealed is still refused. The
caller must name one baseline explicitly; the command performs no inferred
ranking, and the named baseline must carry a validation `CONTINUE` verdict.

The plan binds the benchmark spec and validation report seals, protocol and
epoch, validation and holdout corpus seals, baseline registry, and the selected
baseline identity and algorithm version. Its `scope` is
`FINAL_HOLDOUT_PLAN_ONLY`, its `selection_mode` is `EXTERNAL_EXPLICIT`, and
`holdout_executed` is permanently `false`. The plan seal proves consistency of
that document, not chronology, provenance, attestation, performance or
authority.

The phase-1 implementation imports neither authority nor the holdout ledger. It
passes its corpus directory only to validation replay, which resolves the
`VALIDATION` manifest entry exclusively. A regression test makes any attempt to
open the resolved `holdout.json` fail immediately.

This prerequisite does **not** start or complete HOK-190's functional result.
There is no pairwise ranker, training, calibration, ablation, one-shot final
holdout executor, holdout measurement/report contract, comparison result or
claim-bearing external attestation. The committed synthetic holdout also remains
authoring data rather than an unseen claim-bearing holdout.

## Known limits

- **The committed corpus carries no claim.** 16 validation cases, hand-written
  propensities, stipulated outcomes. No interval computed on it would separate
  two baselines.
- **The single OPE assumption is unverified outside this corpus.** IPS is
  unbiased when the logged propensities are the true behaviour probabilities.
  Here they are true by construction because they were authored. On real data
  they would be an assumption, and nothing in this package can check it.
- **No doubly robust estimate**, no confidence interval and no significance
  test. A variance estimate under weights this heavy-tailed is its own piece of
  work, and reporting one without it would be worse than reporting none.
- **`TAIL` and `DRIFT` are declared definitions, not measured optima**, as are
  every threshold in a protocol and the `minimum_propensity` floor. Their seals
  prove integrity, not registration chronology.
- **The support floor is a blunt instrument.** A target action below it refuses
  the run rather than changing one baseline's denominator. This preserves a
  common estimand at the cost of making sparse logged corpora unusable.
- **Baseline comparison is not ranking.** Four verdicts on one small synthetic
  corpus, with effective sample sizes in single digits, order nothing.

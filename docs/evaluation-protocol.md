# Pre-registered evaluation protocol (HOK-186)

Contract version `1.0.0`. Implementation: `src/latent_compass/protocol.py`.

Everything that could otherwise be chosen *after* seeing a result is fixed
before any result exists, and then sealed.

**This module does not train, optimise, tune or compare models, and makes no
claim about intelligence.** It scores measurements handed to it, against
thresholds fixed before those measurements existed. The protocol exists so that
a future claim could be falsified. It has not been run against a real corpus.

## What a pre-registration must fix

| Section | Requirement |
| --- | --- |
| `tasks` | at least one named task |
| `splits` | exactly one `TRAIN`, one `VALIDATION`, one `HOLDOUT`; each sealed; each declaring disjointness from both others; no shared corpus seal |
| `baselines` | at least one `TRIVIAL` and one `STRONG` |
| `seeds` | at least two, unique, ascending |
| `metrics` | at least one per family: `SUCCESS`, `VIOLATION`, `COST`, `INFORMATION`, `REVERSIBILITY`, `CALIBRATION`, `TAIL`, `DRIFT`; each with a direction, a threshold, and whether it is required |
| `sensitivity_analyses` | at least one |
| `failure_cases` | at least one |
| `epoch`, `revision`, `registered_at` | identity of this pre-registration |
| `contract_version` | **declared, never defaulted**; an absent version is refused |

A protocol leaving any metric family unfixed is refused. This is the rule that
prevents "we will decide how to measure calibration once we see the numbers".

## Sealing and revision

`protocol_seal()` is a domain-separated digest over the whole pre-registration.
Any edit — a threshold, a metric, a corpus seal, a seed — changes it.

`revise()` demands a strictly increasing `revision` **and** a different
`epoch`. Reusing either is refused. A protocol edit that kept its epoch would
let post-hoc thresholds inherit the credibility of the pre-registration they
replace.

Measurements carry **both** the `protocol_seal` they were collected under and
the `corpus_seal` of the split they came from, and both are verified.

The protocol seal says which rules applied. Scoring measurements against a
protocol whose seal has moved is refused:

> `measurements were collected under a different protocol seal`
> — meaning the protocol changed after these measurements were taken.

The corpus seal says which data was measured. Without it, any corpus could be
scored under a protocol that pre-registered a different one:

> `measurements do not come from the pre-registered corpus for this split`

Together they are the whole answer to "the threshold was lowered after the
holdout was seen": the lowered protocol is a different protocol, the old data
cannot be scored against it, and — see below — the holdout corpus is already
spent.

## Holdout discipline

Three rules, all fail-closed:

1. **Purpose.** The holdout may only be scored with `purpose: FINAL_VERDICT`.
   `TRAINING`, `SELECTION` and `EXPLORATION` are refused outright.
2. **A final verdict is only meaningful on the holdout.** Requesting one on
   `TRAIN` or `VALIDATION` is refused.
3. **Once per corpus.** A final holdout verdict requires a durable
   `HoldoutLedger` path. A guard that forgets at process exit is not a guard, so
   an in-memory-only run is refused rather than silently permitted.

### The spent resource is the corpus, not the protocol

Consumption is keyed on the **holdout corpus seal**.

Keying on the protocol seal would have made the guard trivially defeatable:
revising a protocol produces a new protocol seal, so "revise, then re-measure"
would be an unlimited supply of final verdicts over one corpus. A revision that
reuses the same holdout corpus therefore cannot re-arm it. A revision that
introduces a genuinely new holdout corpus can spend that one, once.

### Check-and-consume is atomic across processes

Read, decide and write all happen while holding an exclusive lock built on
`O_CREAT | O_EXCL`, portable to Windows and POSIX with the standard library
alone. Two consequences, both proved with real operating-system processes:

* several processes racing on one corpus produce **exactly one** consumption;
* several processes spending **different** corpora all land — a read-modify
  -write outside a lock would silently drop all but the last.

A lock that cannot be acquired within the timeout refuses. Proceeding without it
would mean deciding "unused" from a read another process is in the middle of
invalidating.

The usage record is written atomically — temporary file in the same directory,
flushed, `fsync`ed, replaced — so an interrupted write never leaves a
half-written ledger that reads as "unused". An unreadable or malformed ledger
is refused rather than treated as unused.

The record is a typed receipt, not a free-form note. It binds the consumption
timestamp, protocol id and seal, epoch, verdict seal and a seal over the complete
raw `MeasurementSet`. A later authority check requires an exact match on those
fields; a receipt for another score or another measurement set proves nothing.

**A refused evaluation never spends the holdout.** Consumption is recorded only
once a verdict actually exists.

## Scoring

For **every metric present** — required or optional — the values for every
pre-registered seed must be present; a missing seed is refused, which is what
prevents seed cherry-picking. An optional metric scored on one of three seeds is
a cherry-picked metric, so it is refused rather than reported.
Values are aggregated as the **median** over seeds — deterministic, and robust
to a single outlying seed in either direction. A metric passes if the aggregate
meets its threshold in its declared direction.

Refused, rather than scored:

- a required metric with no measurements at all;
- any metric present that is missing a pre-registered seed;
- a duplicate metric-and-seed pair;
- a metric that was not pre-registered;
- a measurement whose split disagrees with the set's declared split.

Optional metrics may be **absent entirely**, and a failing optional metric does
not kill. What is refused is a *partial* one.

## Continue / kill

`KILL` if any required metric fails; `CONTINUE` otherwise. The verdict names
every failing metric, carries the protocol seal and epoch, and is itself sealed.
Its contract version is mandatory. Metric pass flags, the failing-metric list
and the decision are re-derived during validation, so contradictory verdict
objects are refused before their seal is considered.

Determinism is a property, not an aspiration: the same measurements produce a
byte-identical verdict, and the verdict does not depend on the order in which
measurements were supplied. Nothing consults wall-clock time, iteration order
or randomness.

## Example

```bash
uv run latent-compass protocol validate --file protocol.json
# -> {"protocol_seal": "sha256:...", "revision": 1, ...}

uv run latent-compass protocol verdict \
    --protocol protocol.json \
    --measurements validation.json
# -> {"verdict": {"decision": "CONTINUE", ...}}

uv run latent-compass protocol verdict \
    --protocol protocol.json \
    --measurements holdout.json \
    --root ./run \
    --holdout-ledger ./run/holdout-usage.json \
    --out ./run/verdict.json
# -> the one final verdict; a second run against this corpus is refused

uv run latent-compass authority transition \
    --from-state CANARY_ELIGIBLE --to-state PROMOTED \
    --actor human_operator --protocol protocol.json \
    --measurements holdout.json --verdict ./run/verdict.json \
    --holdout-ledger ./run/holdout-usage.json --human-ack
# -> raw measurements are re-scored and the matching usage receipt is required
```

`--root` is required whenever the command writes. Both `--holdout-ledger` and
`--out` must resolve strictly inside it; a traversal, a sibling or an external
absolute path is refused, and a refused command writes nothing.

## Known limits

- The eight-family taxonomy and the thresholds in any given protocol are
  pre-registered values, not measured optima.
- Median-over-seeds is deterministic and robust, but has not been compared
  against alternatives on real data.
- Corpus seals are supplied by the operator. This package does not compute them
  and cannot verify that a declared seal corresponds to the corpus actually
  used. Split disjointness is *declared*, not *verified* — nothing here reads
  the corpora. Binding measurements to a corpus seal makes a *mismatch*
  detectable; it does not make a *lie* detectable.
- The holdout guard is a local file. An operator who deletes or edits it can
  spend the corpus again. That is outside the threat model: this guards against
  a mistake and against a second run, not against the operator. Detecting it
  would need the same external anchor `docs/ledger.md` says the ledger lacks.

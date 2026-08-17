# Judgeable pre-action projection

HOK-234 adds a separate, versioned sidecar for capturing what an independent
pairwise labeler is allowed to inspect. It is a capture contract, not a label,
a ranking result or an authority artefact.

## Why it is separate

`Episode 1.0.0` intentionally exposes only candidate identity, logging
propensity and prior uncertainty. Those fields support reproducible offline
baselines but do not describe what each direction means. Widening that schema
would change persisted payloads and seals; widening `PublicCandidateView` would
also give every baseline new information.

`JudgeableDecisionProjection` is therefore an additive sidecar. Existing
episodes and benchmark projections remain byte- and schema-compatible. An old
episode without a sidecar captured before its decision remains `UNJUDGEABLE`.
No backfill may infer candidate meaning from the selected action or its outcome.

## Contract shape

The projection declares contract and canonicalization versions, a preallocated
decision-point identifier, capture time, producer identifier and a bounded
sanitized objective. Shared context is represented as canonically ordered facts
and constraints, each bound to a source digest.

Candidates are ordered by `direction_id`. Each candidate contains:

- a bounded sanitized semantic summary;
- a closed set of typed scalar parameters;
- applicable constraint identifiers;
- exactly one evidence entry for success, violation, cost, information and
  reversibility, in that order.

Each evidence dimension is either `PRESENT`, with one or more bounded facts, or
`NOT_JUDGEABLE`, with a reason and no facts. Missing information is never
silently treated as neutral evidence. Unknown fields, type coercion, non-finite
numbers, numeric magnitudes outside the inclusive range -1e12 to 1e12,
dangling references, duplicates and non-canonical order fail closed. A JSON
integer is not accepted as a float.

```python
from latent_compass import load_judgeable_projection, verify_pairwise_judge_input

projection = load_judgeable_projection(payload)
pair = projection.derive_pair("direction-alpha", "direction-beta")
projection_seal = projection.projection_seal()
pair_input_seal = pair.input_seal()
verified_pair = verify_pairwise_judge_input(pair.canonical_payload(), projection)
```

Pair derivation selects two known candidates and orders them canonically, so
requesting the same pair in reverse produces identical bytes and the same seal.
The pair binds the source projection seal and uses its own seal domain.

`load_pairwise_judge_input` validates only the standalone structure; it cannot
authenticate a caller-supplied source seal. Any provenance-sensitive consumer
must call `verify_pairwise_judge_input` with the source projection preimage. The
verifier checks both the projection seal and exact canonical re-derivation.

## Local durable capture

An upstream producer can publish a validated sidecar before it records any
selection or outcome:

```bash
uv run latent-compass pairwise capture \
    --projection ./projection.json \
    --root ./run \
    --out ./run/captures/decision-0001.json
```

The command reads one projection, validates the complete strict contract and
atomically publishes canonical JSON. The destination must be lexically inside
the named root and must not already exist; publication uses the shared
handle-relative, no-follow writer described in [ledger.md](ledger.md#write-confinement).
Identical projection inputs produce byte-identical files and the same projection
seal, independent of the destination path.

This surface has no episode, selection, outcome, corpus, split, holdout,
network or labeler input. It records what the producer supplies; it does not
prove that the producer invoked it before acting. That chronology claim still
requires an external anchor.

## Blindness and minimisation

The contract rejects logging propensity, prior preference, advisory or ranker
output, selected direction, outcome, external verdict, episode economics, split
name and holdout metadata. The implementation performs no corpus or holdout I/O.
It accepts no raw prompts, source files, credentials, arbitrary JSON values or
live attachment references.

A source digest identifies referenced evidence but does not reveal or validate
its content. Bounded text reduces exposure; validation cannot prove that a
producer sanitized it correctly.

## Proof boundary

Canonical payloads and domain-separated local seals prove deterministic replay.
Exact pair substitution is detected only when the pair is verified against its
projection preimage. These mechanisms do not prove when capture occurred, that
the producer is independent or that a fact is true. HOK-235 now provides an
externally witnessed identity and signing time for labels, but it cannot
retroactively prove that a source projection predates a real action. Real
capture chronology remains a separate evidence obligation.

This contract does not provision or call a labeler, create labels, train a
ranker, calibrate probabilities, consume holdout data or support any performance
or authority claim. The design decision and compatibility rationale are in
[ADR 0005](adr/0005-judgeable-pre-action-sidecar.md).

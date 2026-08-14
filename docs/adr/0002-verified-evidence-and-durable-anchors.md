# ADR 0002 — Verified evidence, durable anchors, and a corpus-keyed holdout

- **Status**: accepted
- **Date**: 2026-08-14
- **Supersedes**: nothing. Amends the contracts introduced in ADR 0001.
- **Scope**: HOK-184, HOK-185, HOK-186, HOK-187, HOK-189

## Context

The first foundation stated the right guarantees and enforced weaker ones. An
independent review found the gaps. Five were structural rather than local, and
fixing them changed the shape of the package, so they are recorded here.

1. **Authority rested on self-declaration.** `authorize_transition` accepted a
   `protocol_seal` *string* and the word `CONTINUE` from the caller. Anyone who
   could call it could assert the evidence that entitled them. The module also
   documented "the capability check happens first" as the security property,
   which is a property of one implementation, not an invariant.
2. **The enforcement tables were mutable.** `CAPABILITIES` was a plain `dict`.
   Widening the grant was one assignment away, and the refusal read that table.
3. **`external_judge` could promote**, because promotion required only
   `authorize_transition`, which it holds.
4. **A truncated ledger still verified.** Deleting the last rows leaves a
   shorter chain that reproduces perfectly.
5. **The holdout was keyed on the protocol seal.** Revising a protocol produced
   a new seal, so "revise, then re-measure" was an unlimited supply of final
   verdicts over one corpus.

## Decision

### A new bottom layer: `vocabulary`

Verifying real evidence means `authority` must depend on `protocol`. But
`protocol` needed `ContinueKill` and `episode` needed `Advisory`, both of which
lived in `authority` — giving `authority -> protocol -> episode -> authority`.

Actors, capabilities, advisories, lifecycle states and `ContinueKill` therefore
moved into `latent_compass.vocabulary`, below both. The field primitives
(`Identifier`, `Timestamp`, `UnitInterval`, …) moved into `contracts`, so
`protocol` no longer imports `episode` either. The graph is now::

    errors -> canonical -> contracts -> vocabulary -> {episode, protocol}
           -> authority -> governance -> ledger -> cli

`authority` keeps its name and its role: it is still the boundary, and still the
first module to read.

### Authority: unconditional refusal, real evidence, `promote` as a capability

- `authorize_transition` refuses `Actor.LATENT_COMPASS` **before reading any
  table**. The invariant is unconditionality, not ordering; the docstring and
  the docs now say so, and a test widens the private grant to full and asserts
  the refusal still holds.
- The tables are exposed as `MappingProxyType` views.
- Reaching `PROMOTED` additionally requires `Capability.PROMOTE`, which only
  `human_operator` holds. `external_judge` may advance a candidate and can
  never promote one.
- Evidence is now the artefacts themselves: a `Preregistration` and a `Verdict`.
  `authority` recomputes the verdict seal from the verdict's own contents, then
  matches it against the protocol's recomputed seal, epoch and pre-registered
  corpus. Promotion further requires a *final verdict on the holdout*. There is
  no parameter left through which a caller can assert a conclusion.

### Episodes: declared versions, one timestamp encoding, structural redaction

- `schema_version` and the nested advisory `contract_version` are required. No
  version is injected on the reader's behalf, at any depth.
- A sealed timestamp is exactly `YYYY-MM-DDTHH:MM:SSZ`. Fractional seconds and
  `+00:00` denote the same instants but different bytes, and would seal
  differently. They are refused rather than normalised: normalising would mean
  the bytes that were sealed are not the bytes that were supplied.
- An unobserved outcome may not carry a violation count. A violation count is an
  observation like any other.
- Redaction is **structural**. `REDACTABLE_PATHS` names the schema's optional
  fields, and a declared redaction is verified against a real absence, so a
  marker can never be decoration over present data.

### Protocol: the corpus is the spent resource

- `MeasurementSet` carries and declares its `corpus_seal`, checked against the
  split's pre-registered seal.
- `HoldoutLedger` is keyed on the **holdout corpus seal**, not the protocol
  seal. A revision that reuses the corpus cannot re-arm it; a genuinely new
  corpus can still be spent.
- Read, decide and write happen under a cross-process exclusive lock built on
  `O_CREAT | O_EXCL`, so concurrent spends of one corpus yield exactly one
  consumption and concurrent spends of different corpora never lose an entry.
- The durable receipt binds the protocol, epoch, raw-measurement seal and
  verdict seal. `authorize_transition` re-scores the raw `MeasurementSet` and
  requires that exact receipt for final-holdout transitions; a self-consistent
  hash over invented metrics is not evidence.
- Every metric *present* must carry every pre-registered seed, optional metrics
  included. A partial optional metric is a cherry-picked metric.

### Ledger: revalidate, anchor, and refuse to launder

- `append` revalidates the episode from its own canonical payload:
  `model_copy(update=…)` bypasses pydantic entirely, so an in-memory `Episode`
  is not proof of a valid episode.
- The clock's output is validated, and the receipt is constructed, **before**
  the commit. Nothing that can fail runs after a row is durable.
- The existing chain, anchor, binding, epoch status and tail are verified and
  re-read *inside* the append transaction, so a truncated tail cannot be
  accepted and then laundered into a new anchor.
- `genesis_seal` is recomputed from the binding rather than read back from
  storage, so relabelling `host_id` breaks the chain instead of renaming
  history. Each stored payload's provenance is re-checked against the binding.
- A durable **anchor** (expected count, expected tail, epoch status) is written
  in the same transaction as each append, because a chain alone cannot notice
  that its own suffix was removed.
- `verify`, `replay`, `tombstone` and `export` each verify and read or mutate one
  transactionally consistent state. `tombstone` and `export` refuse on a corrupt store: a
  redaction must never launder a corruption, and a corrupt store must produce no
  export seal at all. `export` reads inside one transaction, so a snapshot is
  always a single state.
- `create` claims the database file with `O_CREAT | O_EXCL`, so a losing
  concurrent creator never unlinks the winner's store.

### CLI: confinement and typed failure

Every durable write resolves canonically and must land strictly inside a root
named on the command line; traversals, siblings, external absolute paths and
silent overwrites are refused. New output files are published with an atomic
no-overwrite hard link. SQLite, UTF-8, JSON and
filesystem failures become typed errors with documented exit codes; no failure
reaches the user as a traceback.

### Public surface

The README, the docs, the package metadata and the module docstrings now
describe this foundation as what it is: a **validator and recorder of supplied
advisories**. Emission, vectorisation, ranking and calibration are projected
with HOK-182 and are not claimed.

The build backend is pinned exactly and mirrored into the dev group, so
`hatchling` and its closure appear in `uv.lock` and in the licence audit.

## Alternatives refused

- **Keeping a `TransitionEvidence` value object and validating its fields.** It
  would still be the caller's assertion, only better shaped. The packet is
  explicit: a function documented as authorisation must not merely check a
  string it was handed.
- **Signing the ledger.** It would defend against the administrator rewrite, but
  requires key custody this package has no story for. The limit stays
  documented and demonstrated instead of half-solved.
- **Normalising timestamps instead of refusing them.** Cheaper for callers, and
  it would silently break the correspondence between the supplied bytes and the
  sealed bytes.
- **A thread lock for the holdout.** Proves nothing about the cross-process case
  the packet demands.

## Consequences

**Good.** The authority boundary can no longer be talked past. A truncated,
relabelled or laundered ledger is detected. One holdout corpus yields one final
verdict, under concurrency. Every write is confined to a declared root.

**Costs, accepted.** `authorize_transition` now needs real artefacts, which is a
breaking API change and more work for callers — that is the point. Strict
timestamps will reject inputs most tools emit by default. The cross-process lock
adds a stale-lock failure mode, bounded by a timeout that refuses rather than
proceeds. The anchor is stored in the same database it anchors, so it raises the
cost of a forgery without changing the conclusion in `docs/ledger.md`.

**Revisit when.** An external anchor becomes available; or a store must be
shared across hosts; or HOK-182 introduces something that actually emits.

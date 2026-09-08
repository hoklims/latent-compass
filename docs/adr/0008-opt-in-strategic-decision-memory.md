# ADR 0008 — Strategic decision memory is a separate opt-in store, not an episode

- **Status**: accepted
- **Date**: 2026-08-17
- **Scope**: HOK-243
- **Decision owner**: project owner, approved in-session before implementation

## Context

`Episode 1.0.0` records a branching decision *after the fact*: it carries a
selected direction, an observed external verdict and a deferred outcome. Reusing
it as pre-action memory would mean either backfilling post-action fields with
invented values or making them optional, and either move changes the persisted
shape and the seals of every existing episode. The HOK-234 judgeable projection
is the right payload for pre-action candidate meaning, but it is a sidecar: it
has no identity over time, no revision history, no revocation and no store.

Codex and Claude also need durable strategic memory that never merges. A shared
store, or any implicit synchronisation, would make one agent family's record
appear as the other's local memory.

## Decision

Introduce a separate `DECISION_MEMORY` contract family and a separate SQLite
store, `decision-memory.sqlite3`, alongside — never inside — the episode ledger.

A native record is opt-in: it must declare `impact_class` `STRATEGIC_HIGH_IMPACT`
and `sensitivity` `NON_SENSITIVE`, both explicitly. `ROUTINE` and `UNKNOWN` are
first-class values so that "not strategic" and "unclassified" are things a caller
can *say* and this store can *refuse*, rather than states expressed by omission.
Each record binds a stable `decision_id`, a monotonic `revision`, the seal of the
exact revision it supersedes, its store binding, capture time, a named decision
authority, a review deadline, a retention deadline, and one complete HOK-234
projection over 2..256 canonical candidates each carrying exactly five evidence
dimensions.

The record carries no selected route, outcome, label, score, verdict, holdout
metadata, execution authorization or automatic route choice. `extra="forbid"`
makes each a validation failure, and admission refuses those field names again by
name, at any depth, on the raw payload before any transaction opens.

The store is an append-only event chain. A revision, a revocation and a
redaction are all appends. A revision must extend the exact current head by one
and name that head's record seal; a stale revision, a fork, a replayed initial
revision, a revision of a revoked decision and a revision of imported content are
all refused, and none of them moves record count, generation or root seal. Every
write accepts an optional `expected_generation` compare-and-set.

The store refuses a root containing a pre-existing symlink/reparse point. Its
SQLite path nevertheless requires a trusted local namespace for the store
lifetime: stdlib SQLite cannot open from the handle-relative confinement API,
so concurrent same-privilege namespace replacement is outside this store's
threat model and is stated in the security documentation.

Revocation excludes a decision from the active view and removes nothing.
Expiry excludes it as of an explicitly supplied instant. A tombstone drops one
revision's payload while preserving the row, its content seal and the chain link
over it, and appends its own durable event — so the remaining history is never
silently rewritten. Physical destruction of the store file remains an explicit
operator act performed with operator tools, exactly as in HOK-185.

Store binding is immutable and physically separate per agent family. The only
crossing is an explicit, versioned, sealed one-record transfer envelope bound to
the exact source record seal, the exact source root seal and one exact
destination binding. What arrives is `FOREIGN_READ_ONLY`: listed separately from
the native view, never revisable locally, never re-exported, and never
authority-bearing. Replay, destination mismatch, tamper and identity collision
all fail closed.

## Data minimisation and the truth boundary

Admission bounds nesting depth, canonical payload size and text length before
validation, and refuses a short, named list of credential shapes. That list is
published through `memory limits` and is explicitly **not** universal secret
detection: content outside the named shapes passes, and classifying it stays the
caller's obligation.

Local seals prove canonical consistency only. They do not prove issuer
authenticity and they do not prove chronology. An administrator able to rewrite
both the chain and the durable anchor can produce a store that verifies
perfectly, and a transfer envelope's `exported_at` is asserted by its producer
rather than witnessed. Closing that gap needs a co-signature or an external
witness this package does not have.

Naming a decision authority is an accountability label. It grants no capability
to anyone, and reading a record grants none either.

## Compatibility

- `Episode 1.0.0`, the advisory contract and every episode seal are unchanged.
- The HOK-234 projection and pair contracts are unchanged and are embedded, not
  modified; existing projection seals stay byte-compatible.
- The HOK-188 benchmark, the HOK-181 protocol and the authority boundary are
  untouched. Decision memory grants no lifecycle authority and is absent from the
  sealed authority boundary on purpose.
- The decision memory carries its own contract, transfer and store-format
  versions, and fails closed on an unknown or future version at any depth.

## Non-goals

This decision does not select a route, rank candidates, score anything, call a
labeler or a model, read holdout data, touch the continual harness, add a
dependency, or make any performance or authority claim.

## Acceptance

The tranche is acceptable when targeted tests prove: deterministic domain-separated
sealing; exact-head revision linking with stale and forked revisions refused;
compare-and-set on generation; interrupted-append rollback; tamper, truncation and
host-relabelling detection; cross-binding refusal; secret, holdout and unclassified
refusal with the store unmoved; expiry, revocation and tombstone semantics; and
transfer destination, source, tamper and replay refusal — with Codex and Claude
stores shown to be byte-separate and semantically parallel.

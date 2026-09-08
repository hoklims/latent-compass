# ADR 0009 — Post-action reconciliation is a separate journal, not an outcome field

- **Status**: accepted
- **Date**: 2026-08-17
- **Scope**: HOK-244
- **Decision owner**: project owner, approved in-session before implementation

## Context

HOK-243 records strategic decisions *before* they are acted on, and records them
in a shape that structurally cannot carry an outcome: `chosen_route`, `outcome`,
`verdict`, `score` and the rest are refused by name, at any depth, before any
durable write. That refusal is the contract's load-bearing property, and every
`StrategicDecisionRecord` seal in existence depends on the shape it produces.

Something must nevertheless record what actually happened, or the pre-action
evidence is unfalsifiable in practice: a decision whose consequences are never
written down cannot be examined later, and the five HOK-234 evidence dimensions
never get to be compared against anything.

Three obvious routes were refused.

*Add outcome fields to `StrategicDecisionRecord`.* This changes the persisted
shape, breaks every existing record seal, and destroys the exact property that
makes the pre-action record trustworthy — that it was written before the answer
was known and demonstrably contains none of it.

*Add a post-action event kind to the HOK-243 chain.* No seal moves, but the two
lifetimes fuse. A decision store could then no longer be verified, retained or
destroyed without dragging observations with it, and an observation arriving
years later would extend the chain of a decision that was closed.

*Reuse `Episode 1.0.0`, which already has an `outcome` block.* An episode records
one branching step with a selected direction and an external verdict. It has no
decision identity over time, no revision history, no unknown vocabulary, and its
`outcome.observed` boolean cannot distinguish "no observation exists" from "one
is late" from "observers disagree". Collapsing those into `false` is the exact
dishonesty this tranche exists to prevent.

## Decision

Introduce a separate `RECONCILIATION` contract family and a separate SQLite
journal, `reconciliation-journal.sqlite3`, alongside — never inside — both the
episode ledger and the decision memory. Seal domains, format version and contract
versions are all its own, so a decision record and a reconciliation cannot
collide or be relabelled into one another.

A reconciliation names the exact pre-action revision it is about: the
`DecisionBinding`, the `decision_id`, the pre-action `revision`, that revision's
`record_seal`, and the embedded projection's `projection_seal`. That reference is
the whole coupling. Nothing is written back, no decision gains a field, and the
HOK-243 event-kind set stays `RECORD`, `REVOCATION`, `TOMBSTONE`.

Verification against a supplied `StrategicDecisionRecord` is **mandatory**. All
five preimage fields must match it and any executed direction must be one of its
candidates. The CLI requires the record before opening the journal, and the API
requires it as a keyword-only argument, so no unverified preimage can be durable.

### Authorization, execution and observation are three separate things

`AuthorizationState` records what an authority *outside this package* is asserted
to have done. Its most permissive member is named `AUTHORIZED_ELSEWHERE`
precisely so it cannot be misread as a grant issued here, and it must name the
external authority it defers to. `ExecutionState` is independent: `EXECUTED`
alongside `NOT_AUTHORIZED` is a legal, recordable combination, because that
pairing is exactly the fact an audit needs and a schema that made it
inexpressible would be concealing it.

Observations exist only for the one candidate the record says was executed. When
nothing was executed, the observation tuple is *empty*. There is no field in which
an outcome for an unexecuted candidate could be written, so a counterfactual is
unrepresentable rather than merely discouraged.

### Five dimensions, and unknowns that stay unknown

Exactly the five HOK-234 dimensions — `SUCCESS`, `VIOLATION`, `COST`,
`INFORMATION`, `REVERSIBILITY` — appear once each, in canonical order. Reusing
that enum rather than declaring a parallel one is deliberate: the pre-action and
post-action sides must be the *same* five dimensions or the comparison is between
two different vocabularies.

Each dimension is `OBSERVED` or `UNKNOWN`. `OBSERVED` requires a typed value, a
sanitized statement, and complete provenance: source id, source digest,
observation instant, producer and a stated confidence. `UNKNOWN` requires one of
`ABSENT`, `LATE`, `AMBIGUOUS`, `DISPUTED` and a reason, and carries no value, no
statement and no provenance at all. An unknown is the recorded fact, never a
placeholder to be filled in later by inference.

`confidence` is the producer's confidence in that one observation. It is not a
calibrated probability, not a quality score and never a comparison between
candidates — there is only ever one candidate to speak about.

### Append-only, one narrative per preimage

A correction and a disagreement are both appends. A revision must extend the
exact current head by one, name that head's content seal, state its kind
(`CORRECTION` or `DISAGREEMENT`) and say why. It may never re-point at a different
preimage.

One exact pre-action revision is reconciled by exactly one reconciliation identity. A
second identity over the same domain-separated seal of the full preimage reference is refused, so a
later observer cannot open a parallel narrative with no defined head; disagreeing
means appending, visibly and in order. `verify` reports a forked preimage
introduced below the API as `PREIMAGE_FORKED`.

### One deliberate divergence from HOK-243

`abandon_epoch` refuses on a journal that fails verification, where the HOK-243
equivalent does not. The reason is specific rather than stylistic: abandoning
re-writes the durable anchor from the current entry count and tail, so on a
truncated journal it would replace the anchor that proves entries are missing with
one that agrees with what remains. An operator who needs to stop writing to a
corrupt journal already has that, because every append refuses too.

### Replay compares; it does not conclude

Replay is pure, total and deterministic. Given the reconciliation and the exact
record it names, it lays the recorded pre-action evidence facts for the executed
candidate beside the observed value — or the named unknown — dimension by
dimension, and stops. The only summary it emits is a `ComparisonLinkage` saying
which of the two sides exist; `BOTH_PRESENT` means both sides have content, not
that they agree. There is no scalar score, no aggregate, no ranking, no agreement
verdict, no causal claim, no authorization, no promotion, no routing hint and
nothing learned. Whether an observation vindicates a decision is a judgment for a
reader holding authority this package does not have and does not model.

## Data minimisation and the truth boundary

Admission bounds nesting depth, canonical size and text length before validation,
and refuses a forbidden field-name set one step further out than HOK-243's: the
vocabulary that would turn an observation into a judgment — score, rank, reward,
preference, winner, verdict, promotion, causal effect, counterfactual — plus the
holdout and credential vocabularies. `authorization_state` and `execution_state`
are admissible because they are the contract; bare `authorization`, `execution`,
`argv` and `command` stay refused so no free-form blob rides along beside them.

The credential screen is HOK-243's published, named, non-universal screen,
imported rather than re-implemented so the two families cannot drift. It is not
universal secret detection: content outside the named shapes passes, and
classifying it stays the caller's obligation.

Local seals prove canonical consistency only. They do not prove issuer
authenticity and they do not prove chronology. For this contract there is a
further and larger limit: `observed_at`, `source_digest`, `producer` and
`confidence` are all asserted by the producer and witnessed by nothing here. The
journal proves that these observations were recorded and have not been altered
since. **It proves nothing about whether they are true.**

## Compatibility

- `Episode 1.0.0`, the advisory contract and every episode seal are unchanged.
- `StrategicDecisionRecord`, the HOK-243 transfer envelope, the store format and
  every decision seal are unchanged; the event-kind set is unchanged. A test
  asserts the record's exact field set and the exact event-kind set.
- The HOK-234 projection and pair contracts are unchanged and are *read*, never
  modified; existing projection seals stay byte-compatible.
- One additive helper, `screen_located_text`, was exposed on the HOK-243
  admission module so this family screens against the one published shape list
  rather than a second copy. `screen_persisted_text` delegates to it and behaves
  identically.
- The HOK-188 benchmark, the HOK-181 protocol and the authority boundary are
  untouched. Reconciliation grants no lifecycle authority and is absent from the
  sealed authority boundary on purpose; a test asserts that absence.
- The journal carries its own contract, replay and store-format versions, and
  fails closed on an unknown or future version at any depth.

## Non-goals

This decision does not score an outcome, rank candidates, compare a decision
against its alternatives, estimate a causal effect, produce a training label,
authorise or promote anything, route anything, call a labeler or a model, read
holdout data, touch the continual harness, add a dependency, or make any
performance or authority claim. It also implements no redaction: the journal
carries only bounded sanitized observations, and a correction appends. Physical
destruction of the journal file remains an explicit operator act with operator
tools, as in HOK-185 and HOK-243.

## Acceptance

The tranche is acceptable when targeted tests prove: deterministic
domain-separated sealing and deterministic replay; exact-head revision linking
with stale, forked and re-pointed revisions refused; one identity per preimage;
compare-and-set on generation; interrupted-append rollback; tamper detection on
every semantic column, on suffix truncation and on binding and anchor
relabelling; cross-binding refusal; every explicit unknown reason admissible and
never acquiring a value on any path; observed provenance and confidence required
in full; no observation for an unexecuted candidate; wrong preimage id, revision,
seal, projection seal and binding all refused; an executed direction outside the
preimage refused; credential refusal on every durable metadata surface with the
journal unmoved; deep nesting typed through both the API and the CLI; symlinked
root and symlinked database file refused; and no authority, routing, scoring or
promotion surface anywhere in the package or the parser — with the HOK-243
record shape, event-kind set and store bytes shown to be unchanged throughout.

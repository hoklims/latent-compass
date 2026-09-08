# Post-action decision reconciliation (HOK-244)

An append-only, host-and-family-bound journal of what was observed after a
decision was acted on. It **records observations**. It does not judge the
decision, score it, rank its alternatives, explain it causally, authorise
anything or learn from anything.

It lives *beside* the strategic decision memory, never inside it. A pre-action
record is named by seal and left byte-identical; no decision gains an outcome
field, and the HOK-243 event chain gains no event kind.

## Lifecycle

```bash
latent-compass reconcile init --root ./journal --store-id store-alpha \
  --host-id host-alpha --agent-family claude --epoch LC-E1

# --decision-record is mandatory: no unverified preimage is durable
latent-compass reconcile append --root ./journal \
  --reconciliation reconciliation.json --decision-record decision.json

latent-compass reconcile revise --root ./journal \
  --reconciliation correction.json --decision-record decision.json \
  --expected-generation 1

latent-compass reconcile status --root ./journal
latent-compass reconcile verify --root ./journal
latent-compass reconcile list   --root ./journal
latent-compass reconcile show   --root ./journal \
  --reconciliation-id reconciliation-0001

latent-compass reconcile replay --root ./journal \
  --reconciliation-id reconciliation-0001 --decision-record decision.json

latent-compass reconcile abandon-epoch --root ./journal --reason "collected"
latent-compass reconcile limits
```

Exit codes are the package-wide contract: `0` success, `2` usage, `3` refused,
`4` integrity failure, `5` store or filesystem error.

There is deliberately no `select`, `decide`, `authorize`, `promote` or `learn`
subcommand, and no flag that would express one. Every verb above is an evidence
verb.

## What a reconciliation carries

The exact preimage: the decision's `DecisionBinding`, its `decision_id`, the
pre-action `revision`, that revision's `record_seal`, and the embedded
projection's `projection_seal`. Naming the record seal alone would let a
reconciliation float between stores; naming the projection seal too is what makes
"the executed direction was one of *these* candidates" a checkable claim.

Then a stable `reconciliation_id`, a monotonic `revision`, the seal of the exact
revision it supersedes, the journal binding, `reconciled_at`, a named
`reconciled_by`, an explicit `NON_SENSITIVE` classification, an explicit
authorization state, an explicit execution state, and — only when something was
executed — exactly the five observation dimensions.

Naming a reconciling party is an accountability label, not a grant.

Append, revision and replay compare the declared chronology against that exact
preimage: neither `reconciled_at` nor an observation's `observed_at` may precede
the decision's `captured_at`. An observation cannot postdate `reconciled_at`.
Equality is allowed at the timestamp resolution; these comparisons establish
internal consistency, not externally witnessed time.

## Three separations the contract holds

**Authorization is not execution.** `AuthorizationState` records what an authority
*outside this package* is asserted to have done. Its most permissive value is
`AUTHORIZED_ELSEWHERE`, named that way so it can never be read as a grant issued
here, and it must name the external authority it defers to. `EXECUTED` alongside
`NOT_AUTHORIZED` is a legal, recordable combination — that pairing is exactly what
an audit needs to see, and a schema that made it inexpressible would be hiding it.

**Execution is not observation.** Observations exist only for the one candidate
the record says was executed. With `NOT_EXECUTED` or `EXECUTION_UNKNOWN` the
observation tuple is empty and no direction is named. There is no field in which
an outcome for a road not taken could be written, so **the counterfactual is
unrepresentable, not merely discouraged**.

**Observed is not unknown.** A value exists only when a dimension is `OBSERVED`.

## The five dimensions

`SUCCESS`, `VIOLATION`, `COST`, `INFORMATION`, `REVERSIBILITY` — the same five as
the HOK-234 pre-action projection, once each, in canonical order. They are the
same enum, not a parallel one: a comparison between two different vocabularies
would not be a comparison.

| Status | Requires | Forbids |
| --- | --- | --- |
| `OBSERVED` | a typed value, a sanitized statement, and provenance carrying `source_id`, `source_digest`, `observed_at`, `producer` and `confidence` | any unknown reason |
| `UNKNOWN` | one of `ABSENT`, `LATE`, `AMBIGUOUS`, `DISPUTED`, and a reason | any value, statement or provenance |

`ABSENT` means no observation exists. `LATE` means one is expected and has not
arrived. `AMBIGUOUS` means material exists and does not determine a value.
`DISPUTED` means observers disagree. **None of the four may ever acquire a value**:
each is the recorded fact itself, not a placeholder for one. An observation may
not be dated after the reconciliation that reports it.

`confidence` is the producer's confidence in that one observation. It is not a
calibrated probability, not a quality score, and never a comparison between
candidates — there is only ever one candidate here to speak about.

## Exact preimage verification is mandatory

`--decision-record` is required for every append and revision. All five
preimage fields must match the record exactly and any executed direction must be
one of its candidates; a divergence is refused as `preimage_mismatch`. A missing
record is a usage refusal before the journal opens. The decision record is only
*read* and every durable receipt therefore reports `preimage_verified: true`.

## Corrections, disagreements and one narrative per preimage

A correction and a disagreement are both appends. A revision must extend the exact
current head by one, name that head's content seal, declare its kind
(`CORRECTION` or `DISAGREEMENT`) and say why — an unexplained overwrite is what
this journal exists to prevent. A revision may never re-point at a different
preimage.

One exact pre-action revision is reconciled by exactly one reconciliation identity. A
second identity over the same domain-separated seal of the full preimage reference is refused: a
later observer disagrees by appending to the existing reconciliation, visibly and
in order, rather than opening a parallel narrative with no defined head.

A refusal never moves entry count, generation or root seal.
`--expected-generation` turns any write into a compare-and-set.

`abandon-epoch` refuses on a journal that fails verification, unlike its decision
memory counterpart. That difference is deliberate: abandoning re-writes the
durable anchor from the current count and tail, so on a truncated journal it would
replace the anchor that proves entries are missing with one that agrees with what
is left. An operator who needs to stop writing to a corrupt journal already has
that — every append refuses too.

There is no redaction verb. The journal carries only bounded, sanitized
observations, and a correction appends. Physical destruction of the journal file
is an explicit operator act with operator tools, as elsewhere in this repository.

## Replay compares; it does not conclude

`reconcile replay` is pure and deterministic — it reads no clock, opens no
network and consults nothing but the journal and the record you supply. For the
executed candidate it emits five comparisons, in canonical order, each carrying
the pre-action evidence facts **verbatim** beside the observed value or the named
unknown, plus one `ComparisonLinkage`:

| Linkage | Meaning |
| --- | --- |
| `BOTH_PRESENT` | pre-action evidence was judgeable *and* the dimension was observed |
| `PRE_ACTION_ONLY` | evidence existed; no observation was obtained |
| `OBSERVATION_ONLY` | the dimension was not judgeable in advance; it was observed |
| `NEITHER` | neither side has content |

That label is **structural**. `BOTH_PRESENT` says both sides have content; it does
not say they agree, and nothing here computes whether they do.

Replay produces no scalar score, no aggregate, no ranking, no agreement or
disagreement verdict, no causal claim, no authorization, no promotion, no routing
hint and nothing learned. Replaying an unexecuted reconciliation produces **no
comparisons at all**. Whether an observation vindicates the decision is a judgment
for a reader with authority this package does not have and does not model.

Same inputs, same bytes, same `replay_seal`.

## Two families, two journals, no crossing

A Codex journal and a Claude journal are different files with different genesis
seals, and nothing synchronises them. An append must match the host, agent family,
store id and epoch the journal was bound to, or it is refused as a provenance
mismatch. Unlike the decision memory, the journal has **no transfer envelope at
all**: there is no crossing to make, so none is offered.

## Admission, and what it does not claim

Before any transaction opens, admission refuses: a payload with no reproducible
canonical encoding; nesting or canonical size beyond the published bounds; any
forbidden field name at any depth; anything but an explicit `NON_SENSITIVE`
classification; oversized text; and text matching one of the named credential
shapes. `reconcile limits` prints the whole list, together with four explicit
`false` values: `produces_scalar_score`, `produces_ranking`,
`produces_causal_claim`, `grants_authority`.

The forbidden set sits one step further out than the decision memory's. HOK-243
refuses the whole post-action vocabulary because a pre-action record must not
carry an outcome. This journal exists to carry outcomes, so it refuses instead the
vocabulary that would turn an observation into a judgment: `score`, `rank`,
`reward`, `preference`, `winner`, `verdict`, `promote`, `causal_effect`,
`counterfactual` and their neighbours. `authorization_state` and
`execution_state` are admissible because they are the contract; bare
`authorization`, `execution`, `argv` and `command` stay refused so no free-form
blob can ride along beside them.

The credential screen is the decision memory's published screen, imported rather
than re-implemented so the two families cannot drift. It is **not** universal
secret detection and it is not a sanitiser. A secret that looks like ordinary
prose passes. Classifying content remains the caller's obligation.

The SQLite root must be a trusted local directory. Creation and opening refuse
pre-existing symlinks and reparse points, but Python's SQLite API accepts a path,
not the already-confined handles used by file-publication commands. It therefore
does not defend against a same-privilege actor concurrently renaming the journal
namespace while it is open.

## What the seals do not prove

The event chain and the durable anchor detect tampering by anyone who cannot
rewrite both. They establish no **authenticity** and no chronology: an
administrator with write access can recompute the chain and the anchor together
from a forged history and produce a journal that verifies perfectly.

For this contract there is a further and larger limit. An observation's
`observed_at`, its `source_digest`, its `producer` and its `confidence` are all
asserted by whoever wrote them and witnessed by nothing here. **The journal proves
that these observations were recorded and have not been altered since. It proves
nothing about whether they are true.** Closing that gap needs an external witness
or a co-signature this package does not have, and the limit is intrinsic to the
threat model rather than an implementation gap.

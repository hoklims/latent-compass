# Strategic decision memory (HOK-243)

An opt-in, append-only, host-and-family-bound record of strategic decisions.
It **remembers** decisions. It does not take them, rank them, execute them, or
acquire any authority by holding them.

## Lifecycle

```bash
latent-compass memory init --root ./memory --store-id store-alpha \
  --host-id host-alpha --agent-family claude --epoch LC-E1

latent-compass memory append --root ./memory --record decision.json
latent-compass memory revise --root ./memory --record revision.json \
  --expected-generation 1

latent-compass memory status --root ./memory
latent-compass memory verify --root ./memory
latent-compass memory list  --root ./memory --as-of 2026-09-01T00:00:00Z
latent-compass memory show  --root ./memory --decision-id decision-0001

latent-compass memory revoke    --root ./memory --decision-id decision-0001 \
  --reason "superseded" --revoked-by operator-alpha
latent-compass memory tombstone --root ./memory --decision-id decision-0001 \
  --revision 1 --reason "subject erasure request"

latent-compass memory export-transfer --root ./memory --decision-id decision-0001 \
  --to-host-id host-alpha --to-agent-family codex --to-store-id store-codex \
  --to-epoch LC-E1 --exported-by operator-alpha --out ./memory/transfer.json
latent-compass memory import-transfer --root ./codex-memory --envelope ./transfer.json

latent-compass memory limits
```

Exit codes are the package-wide contract: `0` success, `2` usage, `3` refused,
`4` integrity failure, `5` store or filesystem error.

## What a record carries, and what it cannot

Carried: a stable `decision_id`, a monotonic `revision`, the seal of the exact
revision it supersedes, the store binding, `captured_at`, a named
`decision_authority`, `review_due_at`, `expires_at`, an explicit
`STRATEGIC_HIGH_IMPACT` opt-in, an explicit `NON_SENSITIVE` classification, and
one complete HOK-234 judgeable projection over 2..256 canonical candidates each
carrying exactly the five evidence dimensions.

Not carried, and structurally impossible to add: a selected route, an outcome, a
label, a score, a verdict, holdout metadata, an execution authorization, or an
automatic route choice. Unknown fields are refused, and admission refuses those
names again on the raw payload before any durable write.

Naming a decision authority is an accountability label, not a grant.

## Revisions, revocation, expiry, redaction

A revision must extend the exact current head by one and name that head's record
seal. A stale revision, a fork, a replayed initial revision, a revision of a
revoked decision and a revision of imported content are all refused — and a
refusal never moves record count, generation or root seal. `--expected-generation`
turns any write into a compare-and-set.

Revocation is a durable appended event: it excludes the decision from the active
view and removes nothing. Expiry excludes as of the instant you supply, and
`--as-of` must be exactly `YYYY-MM-DDTHH:MM:SSZ`. A tombstone drops one
revision's payload while preserving the row, its content seal and the chain link
over it, and appends its own event, so the remaining history is never silently
rewritten. A tombstoned head stays in the active view and reports
`"redacted": true` with a null record rather than disappearing.

Physical destruction of the store file is an explicit operator act with operator
tools. It is not implemented here, by design.

The SQLite root must be a trusted local directory. Creation and opening refuse
pre-existing symlinks/reparse points, but the Python SQLite API accepts a path,
not the already-confined handles used by file-publication commands. It therefore
does not defend against a same-privilege actor concurrently renaming the store
namespace while it is open.

## Two families, two stores, one explicit crossing

A Codex store and a Claude store are different files with different genesis
seals. Nothing synchronises them. A native append must match the host, agent
family, store id and epoch the store was bound to, or it is refused as a
provenance mismatch.

The only crossing is a sealed transfer envelope bound to the exact source record
seal, the exact source root seal, and one exact destination binding. What arrives
is `FOREIGN_READ_ONLY`: listed separately from the native view (`--origin
FOREIGN_READ_ONLY`), never revisable locally, never re-exported, never
authority-bearing. A replayed envelope, a re-sealed envelope over an already
imported record, a mismatched destination, a tampered field and a decision
identity that already exists natively are all refused, and the store does not
move.

Keep the original transfer envelope if later audits need its source root,
exporter or export time. The destination stores the imported record and a chained
commitment to the envelope seal; it does not retain the complete envelope.
`memory verify` checks that durable commitment and record, while reproducing the
complete transfer seal later requires the retained envelope file.

## Epoch abandonment

Abandoning an epoch closes a healthy memory to every later write while preserving
its history. The operation first verifies the complete event chain and durable
anchor inside the same write transaction. A truncated, altered or divergently
anchored memory is refused without changing its bytes, counters, root or open
status: closing must never replace the evidence of corruption with a fresh anchor.

## Admission, and what it does not claim

Before any transaction opens, admission refuses: a payload outside the exact JSON
data model (objects, arrays and JSON scalars only); a payload with no reproducible
canonical encoding; nesting or canonical size beyond the published bounds; any
forbidden field name at any depth; anything but an explicit `NON_SENSITIVE`
classification, so `UNKNOWN` and `HOLDOUT` both fail; anything but the
`STRATEGIC_HIGH_IMPACT` opt-in; oversized text; and text matching one of the
named credential shapes. `memory limits` prints the whole list.

This is **not** universal secret detection and it is not a sanitiser. A secret
that looks like ordinary prose passes. Classifying content remains the caller's
obligation; this package refuses the shapes it names and claims nothing about the
rest.

## What the seals do not prove

The event chain and the durable anchor detect tampering by anyone who cannot
rewrite both. They establish no **authenticity** and no chronology: an
administrator with write access can recompute the chain and the anchor together
from a forged history and produce a store that verifies perfectly. A transfer
envelope's seal proves that the envelope is internally consistent with the record
and root seal it names — never who issued it, and never when. The `exported_at`
instant is asserted by its producer, not witnessed. Closing that gap requires a
co-signature or an external witness this package does not have, and the limit is
intrinsic to the threat model rather than an implementation gap.

# Episode contract (HOK-185)

Contract version `1.0.0`. Implementation: `src/latent_compass/episode.py`.

An episode is one observed branching decision by a coding agent, recorded after
the fact. It is **evidence, never instruction**. No field can express a command,
a target to mutate, or a callback.

## Shape

```jsonc
{
  "schema_version": "1.0.0",
  "episode_id": "ep-00000001",

  "provenance": {
    "host_id": "host-alpha",          // operator-chosen label, not a hostname
    "agent_family": "claude",         // claude | codex | other
    "store_id": "store-alpha",
    "epoch": "LC-2026-E1",
    "recorded_at": "2026-08-14T10:00:00Z",
    "tool_version": "0.1.0"
  },

  "state": {                          // the hierarchical state, coarse to fine
    "levels": [
      {"depth": 0, "name": "L6-strategy",       "summary_digest": "..."},
      {"depth": 1, "name": "L5-product-intent", "summary_digest": "..."},
      {"depth": 2, "name": "L4-invariants",     "summary_digest": "..."}
    ],
    "branch_point": {"node_id": "node-l4-a", "depth": 2, "parent_node_id": "node-l5-a"}
  },

  "candidates": [                     // propensities must sum to 1.0
    {"direction_id": "dir-alpha", "propensity": 0.6, "prior_uncertainty": 0.2},
    {"direction_id": "dir-beta",  "propensity": 0.4, "prior_uncertainty": 0.3}
  ],

  "decision": {                       // a direction, or an explicit non-action
    "advisory": {
      "contract_version": "1.0.0",
      "kind": "DIRECTION",            // DIRECTION | ABSTAIN | FALLBACK | ESCALATE
      "direction_id": "dir-alpha",
      "confidence": 0.7,
      "uncertainty": 0.2,
      "rationale": "...",
      "issued_by": "latent_compass"
    },
    "selected_direction_id": "dir-alpha"
  },

  "external_verdict": {               // observed only; never called, never written back
    "judge": "external-judge",
    "verdict": "PASS",                // PASS | BLOCK | PARTIAL | UNKNOWN
    "observed_at": "2026-08-14T10:05:00Z",
    "evidence_digest": "..."
  },

  "outcome": {                        // deferred; may be null
    "observability": "FULL",          // FULL | PARTIAL | NONE
    "observed": true,
    "observed_at": "2026-08-14T11:00:00Z",
    "success": true,
    "violations": 0
  },

  "economics": {
    "cost": 1.5,
    "information_gain": 0.25,
    "reversibility": 0.9,             // [0, 1]
    "uncertainty": 0.2                // [0, 1]
  },

  "redactions": [                     // only optional paths, and only real absences
    {"path": "external_verdict", "reason": "operator request"}
  ]
}
```

## Invariants

| Invariant | Refusal |
| --- | --- |
| `schema_version` is **declared**, never defaulted | `episode_validation_error`, reason `absent` |
| The nested advisory declares `contract_version` too | `episode_validation_error` |
| Every declared version is implemented by this build, at any depth | `unsupported_contract_version`, reason `future`, `unknown` or `malformed` |
| No unknown field, at any depth | `episode_validation_error` |
| No type coercion (`"0.7"` for a float, `1` for a bool, `"3"` for an int) | `episode_validation_error` |
| Every number finite | `contract_violation` |
| Level depths strictly increasing; branch point on a declared depth | `episode_validation_error` |
| Branch point is not its own parent | `episode_validation_error` |
| Candidate ids unique; propensities in `(0, 1]` summing to `1.0 ± 1e-9` | `episode_validation_error` |
| Selected direction is among the candidates | `episode_validation_error` |
| A non-`DIRECTION` decision names no direction | `episode_validation_error` |
| An unobserved outcome carries no result — including no violation count | `episode_validation_error` |
| A redaction names a redactable path **and** that path really is empty | `episode_validation_error` |
| Redaction paths unique | `episode_validation_error` |

Nothing is repaired by inference. A malformed episode is refused, not fixed.

### One deliberate accepted case

Validation runs in pydantic's **JSON strict mode**. JSON has a single number
type, so an integral literal such as `2` is accepted where a float is required;
`2` and `2.0` are the same JSON literal, and refusing it would refuse
well-formed JSON rather than catch a coercion. String-to-number, number-to-bool
and string-to-int remain refused. A test asserts both halves so the limit
cannot drift silently.

## Timestamps

Exactly one encoding: `YYYY-MM-DDTHH:MM:SSZ`. Nothing else is accepted, and
nothing is normalised.

`2026-08-14T10:00:00.000Z` and `2026-08-14T10:00:00+00:00` denote the same
instant as `2026-08-14T10:00:00Z`, but they are different bytes and would seal
differently. Normalising them would mean the bytes that were sealed are not the
bytes that were supplied, so they are refused instead. The value is also checked
to be a real instant: `2026-02-30T10:00:00Z` and `2026-08-14T25:00:00Z` match
the shape and are refused.

## Provenance, epoch and host separation

A store is bound at creation to one `host_id`, one `agent_family`, one
`store_id` and one `epoch`. Every append checks all four. A mismatch on any one
is refused with the offending field named, no row is written, and the root seal
is unchanged.

Codex stores and Claude stores are therefore separate both physically
(different files) and logically (an episode from one is inadmissible to the
other). There is no import path, no merge, and no implicit migration.

## Seals

The content seal is a domain-separated SHA-256 over the canonical encoding:
sorted keys, no insignificant whitespace, UTF-8, no non-finite floats, string
mapping keys only. Two logically identical episodes seal identically regardless
of key order; any content difference changes the seal.

## Retention and minimisation

- Record identifiers and digests, never raw source, prompts or file contents.
- `host_id` is an operator-chosen label, not a hostname, IP or account.
- A field withheld at recording time is declared as a `RedactionMark`, so an
  absence is distinguishable from a value that never existed.
- No **structured** field exists for a credential, token or key, and unknown
  fields are refused, so one cannot be added. This is a schema guarantee, not a
  content guarantee. The free-text fields — a rationale, a redaction reason, a
  tombstone reason, a metric or task name — accept whatever the caller writes.
  Sanitising those is the caller's obligation; this package does not scan them
  and does not claim they are clean.
- A store is local and operator-owned. Nothing is transmitted.

## Redaction is structural

A `RedactionMark` may only name a path in `REDACTABLE_PATHS` — the schema's
genuinely optional fields:

```
external_verdict
outcome
state.branch_point.parent_node_id
```

and the value at that path must actually be absent. Declaring
`external_verdict` redacted while the verdict is still present is refused. A
redaction marker can therefore never be decoration over data that is still
there, and a reader can rely on a marker meaning what it says.

Anything outside that set — `economics.cost`, `episode_id`, a state level's
digest — is refused as a redaction path, because those fields cannot be absent
and a "redaction" of them would be a lie by construction.

## Deletion

Append-only and erasure pull in opposite directions. Three mechanisms exist,
they are not interchangeable, and none rewrites history silently.

| Mechanism | Payload | Row | Root seal | Replayable | Visible in `verify` | Implemented |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| `TOMBSTONE` | removed | kept | **unchanged** | no | yes | yes |
| `EPOCH_ABANDONMENT` | kept | kept | unchanged | yes | yes | yes |
| `STORE_DESTRUCTION` | removed | removed | gone | no | n/a | **no** |

**Tombstone** drops the payload and keeps the row, its content seal and its
chain link. It is refused outright on a store that does not verify, so it can
never be used to make an existing corruption disappear. The removal is
**logical**: the payload leaves the contract and every read path, but SQLite may
retain the bytes in freelist pages and the filesystem may retain them after
that. Physical erasure is an operator act with operator tools and is not
demonstrated here. The root seal is unchanged, `verify` reports the row as redacted
and names the tombstone that explains it, and `replay` lists it under
`redacted_episode_ids` rather than skipping it. What is lost is the ability to
replay that episode's content. This is the mechanism for a subject-level
erasure request against a store that must stay auditable.

**Epoch abandonment** closes the epoch: no further appends, everything already
recorded preserved and still verifiable. This is the mechanism for "this
collection run is void". It bounds **acquisition**, not storage duration, and it
deletes nothing — how long an abandoned store is kept is an operator policy this
package neither sets nor enforces.

**Store destruction** — deleting the store file — is the only mechanism that
actually erases content, and it is deliberately not implemented. Destroying
data is an operator act performed with operator tools, not a side effect of a
library call. The gap is a decision, not an oversight.

Run `latent-compass governance` to print these semantics as machine-readable
data.

## Export

`latent-compass export` produces a sealed, self-describing snapshot: the store
binding, every row with its seals, every tombstone, the root seal, and an
export seal over all of it. Identical logical content yields an identical
export seal. An exported file leaves this package's custody and inherits the
operator's retention policy.

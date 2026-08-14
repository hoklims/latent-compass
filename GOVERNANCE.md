# Governance

Two things share this file: how the **data** is governed, and how the
**project** is governed. The first is the one with obligations attached.

---

# Part 1 — Data governance

Detailed contract: `docs/episode-contract.md`. Machine-readable deletion
semantics: `latent-compass governance`.

## Provenance

Every episode carries provenance, and an episode without it is refused:

| Field | Meaning |
| --- | --- |
| `host_id` | an **operator-chosen label**, not a hostname, IP or account |
| `agent_family` | `claude`, `codex` or `other` |
| `store_id` | which store the episode belongs to |
| `epoch` | which collection run it belongs to |
| `recorded_at` | ISO-8601 UTC, trailing `Z` |
| `tool_version` | the version that recorded it |

A store is bound to all four identity fields at creation. Every append checks
all four; a mismatch on any one is refused with the offending field named, no
row is written, and the root seal is unchanged.

**Codex and Claude stores never mix**, physically or logically. There is no
import path, no merge, and no implicit migration.

## Minimisation

Minimisation is a recording-time obligation, not a cleanup step.

- Record identifiers and digests, never raw source, prompts or file contents.
- `host_id` is a label chosen by the operator. Do not put a real hostname,
  IP address, username or account identifier in it.
- No **structured** field exists for a credential, token or key, and unknown
  fields are refused, so one cannot be added. This is a schema guarantee, not a
  content guarantee: the free-text fields — a rationale, a redaction reason, a
  tombstone reason, a metric or task name — accept whatever the caller writes.
  Sanitising them is the caller's obligation. This package does not scan them
  and does not claim they are clean.
- A field withheld at recording time is declared as a `RedactionMark` over one
  of the schema's optional fields, and the declaration is **verified against a
  real absence**. A marker over a value that is still present is refused, so a
  redaction can never be decoration.

## Retention

- A store is local, single-host and operator-owned. **Nothing is transmitted.**
  The package opens no socket.
- Abandoning an epoch bounds **acquisition**: no further episode is accepted. It
  does **not** bound storage duration, and it deletes nothing. How long an
  abandoned store is kept is an operator policy this package neither sets nor
  enforces.
- An exported snapshot leaves this package's custody and inherits the
  operator's retention policy.

## Deletion, and the tension it resolves

Append-only and erasure pull in opposite directions. **No deletion mechanism
here rewrites history silently.**

| Mechanism | Payload | Row | Root seal | Replayable | Visible in `verify` | Implemented |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| `TOMBSTONE` | removed *logically* | kept | **unchanged** | no | yes | yes |
| `EPOCH_ABANDONMENT` | kept | kept | unchanged | yes | yes | yes |
| `STORE_DESTRUCTION` | removed | removed | gone | no | n/a | **no** |

**Tombstone** is the subject-level erasure mechanism for a store that must stay
auditable: the payload goes, the row and its chain link stay, the root seal is
unchanged, and `verify` reports the row as redacted with the tombstone that
explains it. It is refused on a store that does not verify. The removal is
**logical**: the payload leaves the contract and every read path, but SQLite may
retain the bytes in freelist pages and the filesystem or device may retain them
after that. Physical erasure is an operator act with operator tools, and is not
demonstrated here.

**Epoch abandonment** voids a collection run while preserving everything
recorded.

**Store destruction** is the only mechanism that actually erases content, and
it is deliberately **not implemented**. Deleting data is an operator act
performed with operator tools, not a side effect of a library call. The gap is
a decision, not an oversight.

## What this project does not claim

- It is not a compliance product. Whether a tombstone satisfies a given legal
  erasure obligation is a question for your counsel, not for this file.
- It does not verify that a declared corpus seal matches the corpus used, and
  it never reads a corpus.
- It cannot defend a ledger against an administrator who rewrites it wholesale.
  See `docs/ledger.md` and `SECURITY.md`.

---

# Part 2 — Project governance

## Current state, stated plainly

Small project, early stage, maintained by the repository owner. There is no
foundation, no steering committee, no elected body, and pretending otherwise
would be governance theatre. This section describes what actually happens.

## Decisions

- **Ordinary changes** — bug fixes, tests, documentation: a maintainer reviews
  and merges.
- **Contract changes** — anything touching the authority boundary, the episode
  contract, the evaluation protocol or the ledger format: requires an ADR in
  `docs/adr/` recording context, decision, alternatives refused and
  consequences. A contract change is a version change, never an edit in place.
- **New dependencies**: require a demonstrated need recorded in an ADR and a
  verified licence compatibility. See `docs/licenses/dependency-audit.md`.

## Amending the authority boundary

The boundary in `src/latent_compass/authority.py` is the product. Widening it
is the change most likely to be regretted, so it costs the most:

1. An ADR stating what new capability is granted, to whom, and what it enables.
2. The specific threat that granting it re-opens, named.
3. A test proving the new boundary still refuses everything it claims to.
4. A contract-version bump.

**`latent_compass` must never hold `authorize_transition`, `execute`,
`promote` or `mutate_external_judge`.** A change granting any of these is not
an amendment to this project; it is a different project.

## Releases

Semantic versioning. Contract versions are independent of the package version
and are listed in `src/latent_compass/contracts.py`. An unsupported contract
version is refused, never migrated implicitly.

## Licence

Apache-2.0. Contributions are accepted under the same licence — see
`CONTRIBUTING.md`.

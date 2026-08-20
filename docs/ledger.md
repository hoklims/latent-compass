# Shadow-only ledger (HOK-187)

Ledger format `1.0.0`. Implementation: `src/latent_compass/ledger.py`.

A store is a **single SQLite file** inside a root directory named on the command
line. It is bound at creation to one host, one agent family, one store id and
one epoch, and that binding never changes.

```
<root>/ledger.sqlite3
```

## Schema

```sql
CREATE TABLE store_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL) STRICT;

CREATE TABLE episodes (
    seq             INTEGER PRIMARY KEY NOT NULL,
    episode_id      TEXT NOT NULL UNIQUE,
    content_seal    TEXT NOT NULL,
    prev_chain_seal TEXT NOT NULL,
    chain_seal      TEXT NOT NULL UNIQUE,
    payload         TEXT,             -- NULL once redacted
    redacted        INTEGER NOT NULL DEFAULT 0,
    appended_at     TEXT NOT NULL
) STRICT;

CREATE TABLE tombstones (
    episode_id TEXT PRIMARY KEY NOT NULL REFERENCES episodes(episode_id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    mode       TEXT NOT NULL
) STRICT;
```

`STRICT` tables reject values of the wrong storage class, so a text column
cannot silently hold an integer.

## Append-only at the contract level

There is no update and no delete in the write path. The only mutation offered is
`tombstone`, which drops a payload while preserving the row, its content seal,
its chain link and the root seal — and which **refuses outright on a store that
does not verify**, so a redaction can never be used to make an existing
corruption disappear. See `docs/episode-contract.md`.

## Integrity chain

```
chain_seal(0) = seal(ledger_format_version, store_id, host_id, agent_family, epoch)
chain_seal(n) = seal(seq, episode_id, content_seal, chain_seal(n-1), appended_at)
root_seal     = chain_seal(last)   # or chain_seal(0) for an empty store
```

`chain_seal(0)` is **recomputed from the binding at verification time**, never
read back from storage. Relabelling `host_id` in `store_meta` therefore breaks
the chain instead of quietly renaming the store's history. Every stored payload's
provenance is also re-checked against the binding, so a store cannot be
re-attributed to another host or family after the fact.

Every seal is a domain-separated SHA-256 over a canonical encoding, so a content
seal and a chain link can never collide even on identical bytes.

## The durable anchor

A chain alone cannot notice that its own **suffix** was removed: delete the last
rows and the shorter chain still reproduces perfectly.

An anchor — the expected episode count, the expected tail seal and the epoch
status — is therefore written into `store_meta` inside the **same transaction**
as each append, and checked by `verify`. Removing rows leaves the anchor
describing a store that no longer exists. Removing the anchor is itself a
finding.

## What `verify` reports

Every defect found, by kind, never collapsed into a single boolean:

| Kind | Raised when |
| --- | --- |
| `SEQUENCE_INVALID` | sequence numbers are not contiguous from 1 |
| `CHAIN_BROKEN` | a recorded predecessor seal, or a stored chain seal, does not reproduce |
| `PAYLOAD_ALTERED` | a payload does not reproduce its recorded content seal |
| `PAYLOAD_UNPARSEABLE` | a payload is not decodable JSON |
| `PAYLOAD_MISSING` | a payload is absent on a row not marked redacted |
| `PAYLOAD_PRESENT_ON_REDACTED` | a redacted row still carries a payload |
| `REDACTED_WITHOUT_TOMBSTONE` | a payload was removed with nothing explaining it |
| `ORPHAN_TOMBSTONE` | a tombstone refers to an episode not in the ledger |
| `PROVENANCE_MISMATCH` | a stored episode no longer matches the store binding |
| `ANCHOR_MISMATCH` | the anchor does not describe the ledger it anchors |
| `ANCHOR_MISSING` | the anchor is absent, so a truncated suffix would be invisible |

Why the chain is layered: altering a payload breaks its content seal; altering
the payload *and* the content seal breaks that row's chain seal; altering all
three breaks the *next* row's link; deleting a row breaks the sequence and the
chain; deleting the *last* row breaks the anchor; relabelling the binding breaks
genesis and every link after it.

`replay` refuses to run on a store that does not verify, and returns a
deterministic `replay_digest` alongside the reproduced root seal.

## What this does not prove

**A hash chain plus an anchor detects tampering by anyone who cannot rewrite
both. It establishes no authenticity.**

An administrator with write access to the store can recompute the chain *and*
the anchor from a forged history and produce a store that verifies perfectly.
The anchor raises the cost; it does not change the conclusion, because it lives
in the same database it anchors. The test suite demonstrates the underlying
point: two stores with the same binding and different histories both verify, and
nothing inside either can distinguish the genuine one from the forgery.

Detecting a wholesale forgery requires an external anchor this package does not
have — a co-signed digest, a remote witness, or genuinely append-only storage.
That is intrinsic to the threat model, not an implementation gap. Anyone relying
on this ledger as evidence against a privileged adversary needs to add that
anchor themselves.

Likewise, anything running in the same process with the same privileges can
bypass every check here.

## Atomicity

Every append runs inside a single `BEGIN IMMEDIATE` transaction, with
`synchronous = FULL` and a rollback journal. Inside that transaction the append
**re-reads** the binding, the epoch status and the tail, so a concurrent
`abandon_epoch` cannot be overtaken by a check made before it.

Everything that can fail happens **before** the commit:

* the episode is revalidated from its own canonical payload —
  `model_copy(update=…)` bypasses pydantic entirely, so an in-memory `Episode`
  is not proof of a valid episode;
* the clock's output is validated against the canonical timestamp contract — an
  injected clock is untrusted input like any other;
* the existing binding, chain and durable anchor are verified inside the write
  transaction, before a new link is derived;
* the new chain link and the whole receipt are constructed and validated.

There is therefore no failure mode that leaves a durable row behind a raised
exception. An interruption before commit leaves no trace: no row, no burned
sequence number, an unchanged root seal, and a store that still verifies. The
test that proves this installs a SQLite trigger which fires only after the new
row is visible inside the transaction and before the anchor update completes.
The trigger aborts the statement; the row and anchor both roll back.

Duplicate episode ids are caught by a `UNIQUE` constraint, not only by a
pre-check, so a race cannot slip one through.

`create` claims the database file with `O_CREAT | O_EXCL`. That is the mutual
exclusion: a losing concurrent creator never obtains the file, so it can never
unlink the winner's store on its way out.

## Bounded reads

`list` refuses an unbounded page: `limit` must be between 1 and 1000, `offset`
must not be negative. Serving an unbounded read on request is how an inspection
tool becomes an extraction tool.

## Export

`export` **verifies first, and refuses on a corrupt store** — a store that does
not verify produces no export seal at all. The verification and the read run
inside a single `BEGIN IMMEDIATE` transaction, so a concurrent append cannot
land between "this store verifies" and "these are its rows": a snapshot is
always one state. Exactly the document that was sealed is returned.

Identical logical content yields an identical export seal — proved across two
independently built stores, not merely across two calls on one store. Export
adds no wall-clock field, so nothing drifts between two exports of the same
content.

## Write confinement

`export --out` is planned lexically and must land strictly inside `--root`. The
writer opens only the local volume root on Windows, or `/` on POSIX, then
traverses or creates every root and target component relative to held directory
handles without following symlinks or reparse points. Windows UNC/device paths,
ADS, trailing dots/spaces, control characters and reserved DOS names are
refused. An existing file is never silently overwritten and stdout writes
nothing.

Publication uses a flushed temporary file and an exclusive hard link in the
already opened target directory. On Windows the temporary handle starts with
`FILE_DELETE_ON_CLOSE`, so collision and cleanup failures cannot retain its
payload; closing the handle removes only the temporary link after successful
publication. POSIX flushes the file and parent directory around `linkat` and
unlink. POSIX directory descriptors denote objects rather than immutable path
names: if another actor renames an opened ancestor, publication remains attached
to that held object and the returned lexical path can be stale. It never follows
the replacement path or symlink.

## Typed failure

A file that is not a SQLite database, a truncated database, a non-UTF-8 or
non-standard JSON input, a missing file and an unwritable destination all become
typed errors carrying a stable `error` code, mapped to the documented exit
codes. No failure reaches the user as a traceback.

## Determinism

The clock is injectable, and validated. Two stores built from the same episodes
with the same timestamps produce the same root seal, the same replay digest and
the same export seal. Change one field of one episode and all three differ.

# Source observations (HOK-800)

A bounded, read-only answer to one narrow question: *what bytes actually sit
at this relative path, beneath this explicit trusted root, right now?* It
never scans a root, never starts a provider, never touches the network, and
never persists raw source.

## Example

```python
from pathlib import Path

from latent_compass.episode import AgentFamily
from latent_compass.lab.observations import (
    HostBinding,
    capture_source_snapshot,
    observe_literal_matches,
    observe_source_file,
    revalidate_source_snapshot,
)

host = HostBinding(host_id="host-alpha", agent_family=AgentFamily.CLAUDE)
root = Path("/repo")

observation = observe_source_file(
    root,
    Path("src/module.py"),
    host=host,
    root_id="root-alpha",
    observed_at="2026-09-18T00:00:00Z",
    max_bytes=1_000_000,
    declared_git_head="6ec94e86f5b6c429fd264708f28d5105b19fbaf",
)

report = observe_literal_matches(
    root,
    [Path("src/module.py")],
    "TODO",
    host=host,
    root_id="root-alpha",
    observed_at="2026-09-18T00:00:00Z",
    max_bytes_per_file=1_000_000,
    max_matches_per_file=100,
)

snapshot = capture_source_snapshot(
    root,
    [Path("src/module.py")],
    host=host,
    root_id="root-alpha",
    captured_at="2026-09-18T00:00:00Z",
    max_bytes_per_file=1_000_000,
)
revalidation = revalidate_source_snapshot(
    root, snapshot, host=host, root_id="root-alpha", revalidated_at="2026-09-18T01:00:00Z"
)
```

## What every call proves, and what it does not

- **Explicit manifest only.** Every function reads exactly the files the
  caller names. A snapshot's or match report's absence of a file is a claim
  about the files actually read, never a claim about the rest of the root.
  There is no implicit whole-root scan and no live index.
- **UTF-8 text versus binary.** Bytes that decode as UTF-8 report a line
  count; everything else is reported as `BINARY_OR_INVALID_UTF8` with no line
  count and, for `observe_literal_matches`, no attempted line search.
- **Digests are computed from the same bytes read.** `byte_digest` and
  `size_bytes` on every result — including a binary
  `LiteralMatchFileResult` — are computed from the exact bytes just read, so a
  sealed report changes whenever those bytes change, whether or not any match
  location does. They are directly comparable, field for field, to a
  `SourceFileIdentity` in a `SourceSnapshot` manifest for the same relative
  path.
- **`declared_git_head` is asserted, not verified.** This module opens no
  subprocess and reads no `.git` state; a caller-supplied commit identity is
  recorded verbatim and carries no proof of a verified Git `HEAD`.
- **No raw source is persisted.** These functions return in-memory contract
  objects; nothing here writes source bytes to disk.
- **A read cannot establish truth about a host.** Observing a file proves
  what one read returned at one instant; it authorises nothing and grants no
  capability to act on what it read.
- **No implicit global index or live host change.** Nothing here scans,
  watches, mutates, or ranks anything on the host; every function is a single
  bounded read (or bounded set of reads) of the files the caller named.

## The confined reader's residual limits

`read_confined_file` binds each read to the opened handle's own size and
modification time, checked immediately before and after the read, and
refuses a file that ends before its declared length or that is observably
grown, shrunk or modified in place during the read. This is a single-read
guarantee, not a snapshot guarantee:

- It cannot detect a path-level replacement that leaves the already-open
  handle's underlying file untouched — ordinary rename/unlink semantics on
  both POSIX and Windows mean a handle stays bound to the file it opened, not
  to the name that used to point at it.
- It does not make several separate reads atomic with each other. A
  `SourceSnapshot` captured over several files, or a
  `SourceSnapshotRevalidation` rereading them later, is a sequence of
  independent single-file guarantees, not one atomic multi-file snapshot.

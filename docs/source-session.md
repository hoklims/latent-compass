# `latent_compass.lab.source_session` — the source-to-model bridge

Status: **experimental**, part of the `latent_compass.lab` isolated lab. See
[ADR 0011](adr/0011-experimental-active-diagnosis.md) and
[`docs/active-diagnosis.md`](active-diagnosis.md) for the core this bridges to.

This module is the one seam between three independently sealed HOK-798-family
contracts: the finite decision model/state core, the bounded read-only source
observer (`latent_compass.lab.observations`), and lab routing/budgets
(`latent_compass.lab.routing`). It performs no probe selection, no routing
decision and no budget accounting — see `examples/lab_source_demo.py` for how
a caller composes all four. It is still a lab: no operational host adapter, no
process execution, no index refresh, no deletion.

## What it adds

1. **`SourceProbeCatalog`** — a versioned, session-scoped map from each of the
   model's probe ids to a bounded relative-path list, one literal query and
   two distinct outcome ids (present/absent) the model's own probe declares.
   The query text is configuration this module treats as opaque: it is never
   echoed back in any report or error.
2. **`derive_lab_binding(model, snapshot, catalog)`** — seals one session's
   `LabBinding` from the model's *full* `model_seal()`, the snapshot's *full*
   `manifest_seal()`, its `root_id` and the *complete* catalog's own seal.
   Changing the catalog or a query — even against unchanged source bytes —
   never silently reuses a prior episode's binding. Refuses first when the
   catalog does not exactly cover the model's probes, names an outcome the
   model's probe does not declare, or names a path outside the snapshot.
3. **`observe_probe_from_source(model, prior, ...)`** — turns one caller-
   selected probe's bounded literal read into a core observation. Before
   touching the filesystem it revalidates every argument, confirms `prior`
   replays and its binding matches this session's derived one. It then
   revalidates the whole snapshot, performs only the selected probe's bounded
   read, compares every result's same-read byte digest and size against the
   captured manifest, and revalidates the snapshot again. Any drift, missing
   file, binary or undecodable content, or digest/size mismatch refuses with
   `prior` left completely unchanged — a binary file is **never** mapped to
   the absent outcome. Any match across the probe's declared UTF-8 files maps
   to the present outcome; zero matches across all of them maps to absent.
   Only then does it call `latent_compass.lab.state.apply_observation`.

Every refusal from `derive_lab_binding` and `observe_probe_from_source` (other
than a handful reused from `latent_compass.lab.errors` for cases that are
exactly the same failure as the core's own) raises
`LabSourceSessionViolationError`, with `detail["reason"]` naming the exact case —
see the module docstring for the closed list of reasons.

## What it returns

`SourceProbeObservationResult` (a plain frozen dataclass, not a wire
contract): the updated `DiagnosisStateRevision`, the outcome id applied, the
`LiteralMatchReport` (ids, digests, sizes, line numbers — never raw file
content), and both the pre- and post-read `SourceSnapshotRevalidation`
reports. Nothing here ever carries the catalog's query text or a file's raw
bytes.

## What it does not do

- No probe selection, no routing advice, no budget reservation or
  settlement — that composition lives in the caller
  (`examples/lab_source_demo.py`), using `latent_compass.lab.routing`.
- No atomic multi-file snapshot and no authenticated observation claim: three
  separate bounded reads (revalidate, literal match, revalidate) can still
  race a concurrent external write; this module only refuses when that race
  produces a byte-identity mismatch it can actually observe.
- Caller-declared host id, agent family, root id and Git HEAD remain
  declarations this module checks for internal consistency, never
  independently authenticated facts.

## Minimal usage

```python
from pathlib import Path

from latent_compass.episode import AgentFamily
from latent_compass.lab.model import load_model
from latent_compass.lab.observations import HostBinding, capture_source_snapshot
from latent_compass.lab.source_session import (
    derive_lab_binding,
    load_source_probe_catalog,
    observe_probe_from_source,
)
from latent_compass.lab.state import initial_state

model = load_model(...)  # a finite decision model whose probes this catalog covers
snapshot = capture_source_snapshot(
    root,
    [Path("signal-a.txt")],
    host=HostBinding(host_id="demo-host", agent_family=AgentFamily.CLAUDE),
    root_id="demo-source-root",
    captured_at="2026-09-18T00:00:00Z",
    max_bytes_per_file=4096,
)
catalog = load_source_probe_catalog({...})
binding = derive_lab_binding(model, snapshot, catalog)
state = initial_state(model, state_id="episode-1", binding=binding)

result = observe_probe_from_source(
    model,
    state,
    root=root,
    snapshot=snapshot,
    catalog=catalog,
    probe_id="check-a",
    observation_id="obs-1",
    observed_at="2026-09-18T00:00:00Z",
    expected_host_id="demo-host",
    expected_agent_family=AgentFamily.CLAUDE,
    expected_root_id="demo-source-root",
)
```

A nonzero-revision `state` requires `history=[state_0, ..., state]`, exactly
as `apply_observation` and `propose` already require in the core module — see
`docs/active-diagnosis.md#required-replay-context`.

See `tests/test_lab_source_session.py` for the full adversarial coverage
(drift before and after a read, a same-read digest mismatch isolated from
that drift check, binary refusal, host/root scope mismatch, catalog coverage
and outcome checks, a tampered-state replay refusal, and a legitimate
two-probe continuation) and `examples/lab_source_demo.py` for the complete,
offline, synthetic walkthrough with routing and shared budgets.

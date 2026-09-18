# Host observations and the host session (HOK-800, HOK-801/802)

Status: **experimental**, part of the isolated `latent_compass.lab`. See
[ADR 0011](adr/0011-experimental-active-diagnosis.md) for the boundary and
[`docs/active-diagnosis-scope.md`](active-diagnosis-scope.md) for the perimeter.

ADR 0011 forbids the lab from launching a process, so it cannot run Git, a
language server or a test. Those tools belong to the **authorised host
executor**. Two modules make what the host returns usable without trusting it
more than it deserves:

- `latent_compass.lab.host_observations` — one envelope for every observation
  kind, and the verification the lab can do for itself;
- `latent_compass.lab.host_session` — the bridge from a verified observation to
  one outcome of the finite model, or to `UNKNOWN`.

`tests/test_lab_boundary.py` parses every lab module and refuses any import or
call that would launch a process or reach the network.

## One envelope, five kinds

| Kind | Who runs it | Result |
| --- | --- | --- |
| `FILE_READ` | the lab's confined reader, or the host when it interprets what it read | the cited lines; optionally a flagged `interpreted_outcome_id` |
| `LITERAL_SEARCH` | the lab's confined reader, or the host | line locations |
| `GIT_DIFF` | the host | working-tree paths changed against a declared base, with post-image digests |
| `SYMBOL_NAVIGATION` | the host | line locations for one declared relation |
| `TARGETED_CHECK` | the host | a declared verdict, exit code and log digest |

Every `HostObservation` states, in the same fields whatever its kind: the
question (as a digest, never as text), the declared coverage, the evidence, the
**tool identity and version**, the providers that contributed, duration,
observed cost, resource accounting (child processes, files read, repeated
reads, internal index, cache) and its limits. `None` means *not observed* —
never zero.

### Status is not success or failure

`OBSERVED` and `EMPTY` are conclusive over the declared coverage. `TRUNCATED`
proves what it lists and nothing about what it omitted. `TIMEOUT`,
`TOOL_ABSENT`, `UNSUPPORTED_LANGUAGE` and `FAILED` prove nothing, carry no
result, and are still recorded so their cost is accounted for. An `ERRORED`
check is an infrastructure or collection error, never a detection.

### Limits travel on the sealed record

A record that omits a mandatory limit code is refused, so a consumer cannot
strip the caveat without breaking the seal.

| Limit | Mandatory when |
| --- | --- |
| `COVERAGE_IS_DECLARED_PATHS_ONLY` | always |
| `EMPTY_IS_NOT_ABSENCE` | the status is `EMPTY` |
| `OUTPUT_TRUNCATED` | the status is `TRUNCATED` |
| `GIT_IDENTITY_IS_DECLARED` | the kind is `GIT_DIFF` |
| `DIRECT_RELATIONS_ONLY` | a symbol relation other than `DEFINITION` — indirect dependencies are not covered |
| `TOOL_INTERNAL_INDEX_USED` | the tool used an internal index |
| `GENERATED_CODE_IN_EVIDENCE` | any cited location is flagged generated |
| `CHECK_VERDICT_IS_HOST_DECLARED` | the kind is `TARGETED_CHECK` |
| `OUTCOME_IS_HOST_INTERPRETED` | a file read carries an interpreted outcome |

## What the lab verifies for itself

`verify_host_observation` re-reads, through the confined reader, every file a
conclusive observation covers, and refuses when:

- a covered path is outside the episode's snapshot, or no longer matches it —
  an external mutation or a branch change invalidates the observation **even
  when the declared Git `HEAD` is identical**, because a dirty working tree is
  compared by content;
- the tool saw different bytes than the snapshot binds;
- a cited line does not exist, or the caller's anchor does not occur in it;
- a Git post-image is not the content on disk;
- an `EMPTY` literal search is contradicted by the covered source — the one
  emptiness the lab can check, since it holds both the bytes and the query;
- a **retired provider** contributed: there is no hidden fallback;
- the record's mode is not the session's, or the scope or chronology is wrong.

`SOURCE_ONLY` is the witness mode: it admits no symbol navigation and requires
`internal_index_used` to be `false`, not unknown. `SOURCE_AND_SYMBOLIC` admits
all five kinds and counts the language server's internal index.

A non-conclusive observation passes the scope and policy checks and **reads
nothing**.

## From an observation to the model

A `HostProbeCatalog` declares, for every probe of the model, its kind, exact
coverage, question, expected tool identity and how a result maps onto the
probe's own outcomes. A model probe no source can answer — an authored
requirement, an owner's ruling — is listed in `external_probe_ids`, so its
omission is a statement rather than an accident.

`derive_host_lab_binding` seals the model, the snapshot manifest, its root, the
whole catalog and the session policy. **A tool upgrade is therefore a new
episode**, as is a changed question, a widened coverage or a relaxed policy.

`apply_host_observation` returns exactly one of three things:

| Result | When | State |
| --- | --- | --- |
| an outcome | the verified result maps onto exactly one declared outcome | advances by one revision |
| `UNKNOWN` | non-conclusive status, errored check, or a truncated result that could hide a differently-mapped path | **untouched**, and the probe is not spent |
| a refusal | the observation is not the catalog's, verification refuses, or the result maps onto several outcomes | untouched, typed error |

A result in paths that answer differently is a world the model does not
declare. It is refused, never resolved by picking one.

## Non-claims

The lab cannot authenticate the host. A tool identity, a Git identity, a check
verdict, a duration, a cost and the providers used are declarations; the
limit codes say so on the record. Nothing here shows that the source-only or
the symbolic stack is fast enough, complete enough or cheaper than an index on
a real repository — that is HOK-804's measurement, not a property of this
contract.

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

`tests/test_lab_boundary.py` parses every lab module **and every first-party
module the lab can import**, and refuses any import or call that would launch a
process or reach the network. One exception is named there, and only there: the
confined reader loads a system library through `ctypes` to open files without
following reparse points.

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
reads, internal index, cache) and its limits. The three counts are mandatory: a
host that cannot count cannot produce an admissible record. For duration, cost
and the two flags, `None` means *not observed* — never zero or false.

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
- the host declares a Git `HEAD` other than the snapshot's — both are
  declarations, and one that names a change is still believed;
- the record's mode is not the session's, or the scope or chronology is wrong.

`SOURCE_ONLY` is the witness mode: it admits no symbol navigation and requires
`internal_index_used` to be `false`, not unknown. `SOURCE_AND_SYMBOLIC` admits
all five kinds and counts the language server's internal index: a symbolic
observation that leaves `internal_index_used` unknown is refused.
`worktree_dirty` is recorded for the reader and never consulted; the content
digests decide.

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

## The isolated bench (HOK-803)

`examples/lab_host_bench.py` runs the whole loop — advice, routing, host
decision, execution, observation — in a throwaway Git repository, with real
tools rather than mocks: `git diff` on a dirty working tree, a symbol lookup —
the in-process Python `ast` parser, or a real language server — and a check
process whose exit code is the verdict.

```bash
uv run python examples/lab_host_bench.py --family claude
uv run python examples/lab_host_bench.py --symbol-tool pyright-langserver
```

The executor lives in `examples/` on purpose: `tests/test_lab_boundary.py`
refuses any process launch inside `latent_compass.lab`. Each executed step
keeps five separate records — `advice`, `routing`, `host_decision`,
`execution`, `result` — and the bench's router is the only party that accepts,
ignores or escalates advice. Tool identities are observed (`git --version`, the
running interpreter, the version the installed language-server package
declares), never hard-coded.

**A real language server.** With `--symbol-tool pyright-langserver` the symbol
question is put to a `pyright-langserver` found on the host's `PATH`, over the
Language Server Protocol: it is started for the one question, handed the very
bytes the evidence digests (`textDocument/didOpen`, not a path to re-read),
asked for the file's symbols, told to shut down, and killed if it has not left
by the deadline. It is no dependency of this package and nothing installs it.
The record says what such a tool is: `internal_index_used` is `true` with the
`TOOL_INTERNAL_INDEX_USED` limit — a language server keeps a model of the
workspace in memory, and the executor cannot see which requests consult it, so
it declares the index used rather than claim it was not — and `cache_used` is
`null`, because that was not observed. The lab then re-reads the cited line
itself, as it does for any host observation. On the bench sources the parser
and the server cite the same definitions, and swapping one for the other
changes who answered, what it cost and what it kept in memory — not what the
loop concludes.

Those paths run only where the server is installed; elsewhere their tests are
skipped with that reason. Two paths run on every host: a host without the
server meets `TOOL_ABSENT` and plans around the probe, and a real binary that
is no language server — `git`, launched in its place — is a `FAILED`
observation, never an outcome.

`tests/test_lab_host_bench.py` drives it through the paths that do not end in
an outcome:

| Situation | What the loop does |
| --- | --- |
| binary missing, or tool declared absent | `TOOL_ABSENT`; state untouched |
| process overruns its timeout | interrupted; `TIMEOUT`; state untouched |
| language the symbol tool cannot parse | `UNSUPPORTED_LANGUAGE`, never guessed |
| advisor absent, kill switch, capability withheld | `ABSTAIN`; nothing executes |
| advice expired, computed on another source, or for another scope | `ABSTAIN`; nothing executes |
| review, authority or Semctx proof missing | `ESCALATE`; nothing executes |
| host ignores the advice | nothing executes; the native decision stands |
| recommended probe cannot be obtained | not retried, no other provider stands in; the host declares it unobtainable and the `1.1.0` plan does without it |
| provider retired by the session policy | the session is refused before its first step |
| observation made for the other agent family | refused, `scope_mismatch` |

Every attempt, a failed one included, is charged at its reserved ceiling: the
bench measures durations and invents no token or money cost.

**Delegation.** `run_delegation` (`--delegate`) hands the same diagnosis to
child episodes that reserve against **one** pool. A delegation records its
scope, what the pool still held when it was made, whether it was cancelled, and
its result; a child's probe reservations are nested under it, so the one ledger
says who spent what.

| Situation | What the loop does |
| --- | --- |
| the pool cannot pay for a second child | that child buys nothing and decides on its prior |
| a child plans on a stale remainder | advised and accepted, then refused by the pool; nothing runs, nothing is retried |
| a delegation is cancelled before it starts | no checkout is built; the budget stays for its sibling |
| two children are given the same checkout | refused before anything is built |

The children are bench episodes run one after another. Nothing here is a live
sub-agent, and nothing is claimed about concurrent writers to a pool.

**Planning around an unobtainable probe.** The `1.0.0` `propose()` has no way
to be told that a probe cannot be obtained: a failed attempt leaves the state
untouched, so it names the same probe again. A host that speaks only `1.0.0`
can then do no better than stop — and on the bench model that stop is not free:
with 4 points left, the check probe is still worth 7/2 against 15/4 for
stopping. The report's other values cannot simply be reused either:
`diff-pricing`'s 3/1 assumes the unobtainable probe comes next.

The bench therefore plans with `1.0.0` until a probe proves unobtainable, then
with the `1.1.0` `propose_excluding`, which is told so (owner decision of
2026-09-19, see the ADR amendment and `docs/active-diagnosis.md`). With the
symbol tool absent it runs the check instead — a different question put to its
own tool, never the symbol question handed to another provider — and its final
decision rests on an observation rather than on the prior. With the check tool
absent as well, the one probe left is not worth its cost and the advisor says
stop, at 15/4. That a probe is unobtainable stays the host's declaration; here
it means "attempted once, no outcome, and this host does not retry".

A `CLAUDE` or `CODEX` family on the bench is a declared lab identity. Running
the same loop under both shows that two episodes never mix — **not** that two
live hosts behave alike. Wiring the loop into real Codex and Claude Code
sessions remains open.

## The rehearsal launcher (HOK-804)

`examples/lab_rehearsal_launcher.py` is the one script here that contacts a
real agent. The repository owner authorised a rehearsal **outside the
protocol** (`docs/active-diagnosis-preregistration.md`, "Rehearsal outside the
protocol"): six real `codex exec` sessions of at most fifteen minutes on a
throwaway copy, to size the cost and latency ranges, the spend ceiling and the
pair count, which cannot be chosen blind. It is not a trial runner and emits no
`LabTrial`.

```text
python examples/lab_rehearsal_launcher.py --key-file <file holding the dedicated key> \
    --source <repository to copy> --ref <commit> \
    --tasks examples/lab-rehearsal-tasks.json --out <directory outside this repository>
```

- **It keeps durations and token usage, never an outcome and never text.** The
  agent's events are read for their `type` and their `usage` and dropped. A
  session record holds a harness status (completed, failed turn, timed out,
  failed to start) and a diagnostic **label** from a fixed vocabulary: whatever
  the CLI wrote on its error stream is classified and dropped, because that
  stream can carry the agent's last message. A rehearsal that kept any of it
  would be a look at results before the freeze. A harness status is still a
  fact about a task, which is why rehearsal tasks belong to no population. It
  counts tokens and fabricates no money cost.
- **Its ceilings are constants, not defaults.** Six sessions **per dedicated
  key file**: the ledger lies beside the key, so another output directory does
  not give the allowance back, and a lock beside the key refuses a second run
  at the same time. A session is counted once its copy is ready and before its
  process starts: a crash still counts, a copy that could not be made does not.
  Fifteen minutes each: on overrun the process tree is killed and the pipes get
  a bounded time to drain — an orphan that keeps them open is abandoned, not
  awaited — and an interruption of the launcher kills the session before it
  propagates. Flags lower both ceilings and never raise them. A mistyped commit
  or a refused key is refused before it can cost a session.
- **The ledger is not a spend limit.** It guards against mistakes, not against
  its owner, who can delete it. The money bound is the hard limit set on the
  key's project at the provider — outside this script, and not instantaneous.
- **No key file, nothing starts.** A key file holding more than one token is
  refused. The key travels on standard input to `codex login --with-api-key`,
  into a `CODEX_HOME` created beside the key file for the run and deleted after
  it; it is never an argument, an environment value, a log line or a report
  field. That home's configuration pins the CLI's credential store to a file
  inside it, and the run stops before any session unless the login really left
  its credentials there: a CLI that put the key where the launcher cannot
  delete it — an OS keyring — is refused, with the instruction to revoke the
  key. The pin is the setting the CLI documents; it was not exercised against
  the installed CLI, which is why the check after the login exists.
  Whether that deletion succeeded is printed on every path, a run that
  dies included, written in the report, and is the command line's exit code
  (`3`). The CLI that receives the key is resolved from an absolute path or an
  absolute `PATH` entry, never from the current directory. The launcher never
  reads, copies or writes the owner's own agent home or login: a token
  refreshed in a copy could sign the owner out of the live session.
- **The agent inherits almost nothing — and that is not a jail.** An
  allow-listed environment: every home and temporary directory and Git's global
  configuration inside the isolated home, Git's system configuration and
  credential prompts off, a `PATH` rebuilt from the few tools it needs; its own
  copy of one commit, extracted from Git objects; the source repository only
  read. The launcher does **not** control, and does not claim to: what the
  agent can read on the machine under the CLI's `workspace-write` sandbox, what
  the CLI writes outside `CODEX_HOME`, and the network. Do not run it under a
  harness that prints local variables in tracebacks: the key is one of them.

An independent security review of the first version of this launcher found
five serious defects, all on error paths (a killed session awaited without a
bound, an interruption that left the session running, a removal reported
without looking, the CLI resolved from the current directory, an allowance
tied to the output directory). This version is the corrected one. The
corrections are covered by tests; they have **not** been reviewed again.

`examples/lab-rehearsal-tasks.json` holds six throwaway tasks on this
repository. They were written by the author, belong to no population and are
excluded from any protocol. The tests drive the launcher with a stub standing
in for the agent CLI — a real process, which can overrun, leave a child or an
orphan behind, fail and write on its error stream: no real session runs in the
test suite or in CI, and **the rehearsal itself has not run**.

## Non-claims

The lab cannot authenticate the host. A tool identity, a Git identity, a check
verdict, a duration, a cost and the providers used are declarations; the
limit codes say so on the record. Nothing here shows that the source-only or
the symbolic stack is fast enough, complete enough or cheaper than an index on
a real repository — that is HOK-804's measurement, not a property of this
contract.

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

## Local harness surface

The package exposes the composition points a host harness needs through
`python -m latent_compass.lab`. They are JSON-file-in, JSON-out commands and
perform no network request, model call, tool execution or host mutation:

```text
python -m latent_compass.lab init-host-state \
  --model model.json --snapshot snapshot.json --catalog catalog.json \
  --policy policy.json --state-id episode-one

python -m latent_compass.lab apply-host-observation \
  --root <observed checkout> --model model.json --snapshot snapshot.json \
  --catalog catalog.json --policy policy.json --state state.json \
  --observation observation.json --expected-host-id <declared host label> \
  --expected-agent-family codex --expected-root-id <declared root label> \
  --verified-at 2026-09-20T00:00:00Z

python -m latent_compass.lab route-advice \
  --request route-request.json --capabilities capabilities.json \
  --now 2026-09-20T00:00:00Z
```

`init-host-state` derives the source-scope digest from the sealed model,
snapshot, catalog, tool identities and policy; a harness does not invent that
digest. `apply-host-observation` emits the updated state, the mapped outcome or
typed `UNKNOWN`, and the lab's verification record. `route-advice` emits the
existing sealed `ADVICE`, `ABSTAIN` or `ESCALATE` document. The harness remains
the router and executor, and may ignore the advice.

This surface is suitable for shadow integration into ordinary local harness
runs. Shadow integration does not create an extra model call and does not turn
normal work into an HOK-804 trial. It establishes contract compatibility only;
it does not establish comparative quality, cost, latency or retirement
readiness.

### Passive Codex and Claude hook adapter

`examples/latent_compass_shadow_hook.py` is the installed host-side wrapper. It
owns the bounded local Git inspection, then passes a sanitised envelope to
`python -m latent_compass.shadow_harness`, which writes the local journal. The
package remains process-free. On an asynchronous `PreToolUse` event the pair:

- accepts only an explicitly allow-listed repository root;
- seals session and turn identifiers instead of storing them;
- records the host, project alias, model label, permission mode, tool name and
  a bounded Git source declaration;
- evaluates the candidate tool against the capabilities declared for that
  host; and
- writes the sealed `ADVICE` or `ABSTAIN` record to the store belonging to that
  host.

It deliberately ignores prompts, transcripts, tool arguments and tool results.
It writes nothing to stdout, is fail-open, runs asynchronously and never gives
its decision back to the model, route or permission system. Codex and Claude
use physically separate configurations and stores.

The reversible installer preflights every selected host before it changes any
file, then creates timestamped backups for existing files:

```text
latent-compass host install --host codex \
  --project-root <allowed-repository-root> \
  --project-alias <stable-project-alias> --dry-run --json

latent-compass host install --host codex \
  --project-root <allowed-repository-root> \
  --project-alias <stable-project-alias> --json

latent-compass host status --host codex \
  --project-root <allowed-repository-root> --json

latent-compass host remove --host codex \
  --project-alias <stable-project-alias> --dry-run --json

latent-compass host recover --dry-run --json
latent-compass host recover --json
```

The installed tool environment's interpreter runs a packaged wrapper resource
that the installer materializes in the selected host store, so `uv tool install
.` provides a persistent runtime without depending on the source checkout. The legacy `python -m
latent_compass.shadow_install` entry point and its explicit `--runtime-python`
and `--hook-script` options remain available for controlled deployments.
Removing one project keeps every other project registration and leaves the
hooks active. Removing the final registration removes only Latent Compass hook
groups and the host registration file; observation journals remain available.
Before its first mutation, install or remove writes a minimal pending journal
containing paths and before/after digests, but no copied hook contents. An
interrupted transaction makes later install/remove previews return
`recovery_required`. `host recover --dry-run --json` reports the exact repair;
apply performs only those actions. If current bytes match neither recorded
state, recovery returns `pending_transaction_conflict`, preserves the file and
its backup, and requires operator inspection. Run the original setup again
after recovery succeeds.

The installer's portable filesystem boundary rejects paths that are already
redirected through symbolic links or Windows reparse points, and it refuses a
parent substitution encountered while opening a path. It does not claim to
contain a local peer that can rename an already-open profile directory during
the operation: POSIX directory descriptors continue to address a directory
after it has been moved. Run host installation and recovery only while other
processes with permission to rename the selected profile directories are
quiescent. File creation remains exclusive; updates are revalidated immediately
before atomic replacement, with backups and the pending journal retained for
recovery when completion is uncertain.

Codex binds trust to the current hook definition. A newly installed or changed
hook therefore remains skipped until an operator reviews it through Codex's
hook-management interface. Direct smoke execution can verify the adapter, but
does not replace that trust decision. Claude loads the added asynchronous hook
on its next session. Removing the entries stops collection; the runtime and
host-local journals are retained for inspection or explicit later deletion.

### Local status and summary

After installation, inspect one project without reading any recorded content:

```text
latent-compass-status --project-root <repository>
latent-compass-status --project-root <repository> --json
```

The command parses only sealed privacy-minimised record files and renders the
validated fields required for counts and timestamps. Unknown fields, invalid
seals, oversized files and malformed timestamps/verdicts are counted as invalid
without being displayed. It never launches Git, a model, a network request or
an operational recommendation. Its
states are `NOT_CONFIGURED`, `HOST_CONFIGURATION_INVALID`,
`SHADOW_CONFIGURATION_INVALID`, `DISABLED`, `PROJECT_NOT_REGISTERED`,
`HOOKS_MISSING`, `RUNTIME_MISSING`, `NO_OBSERVATIONS`, `OBSERVING` and
`DEGRADED`. A truncated scan is explicitly `DEGRADED`; its counts are partial.
Trust is reported
as `UNKNOWN` because filesystem inspection cannot replace the host's own trust
review.

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
real agent. It is retained as a tested historical artefact, but **must not be
run under the current project decision**. On 2026-09-20 the repository owner
superseded the earlier rehearsal authorisation: neither a rehearsal nor a trial
may create an additional model or API call. Cost and latency may be observed
only from ordinary work already being performed through a harness, in shadow
mode, or remain unknown. The launcher is not a trial runner and emits no
`LabTrial`; the command and safeguards below document the dormant artefact,
not an authorised next action.

```text
python examples/lab_rehearsal_launcher.py --key-file <file holding the dedicated key> \
    --source <repository to copy> --ref <commit> --cli-version <the CLI version expected> \
    --tasks examples/lab-rehearsal-tasks.json --out <directory outside this repository>
```

- **It keeps durations and token usage, never an outcome and never text.** The
  agent's events are read for their `type` and their `usage` and dropped. A
  session record holds a harness status (completed, failed turn, timed out,
  failed to start) and a diagnostic **label** from a fixed vocabulary: whatever
  the CLI wrote on its error stream is classified and dropped, because that
  stream can carry the agent's last message. The one string of the CLI's that
  is kept is its version, and only when it has the shape of a version — a key
  cannot pass for one. A task id is a plain name, and a model name cannot be a
  key: both are written in the report. A rehearsal that kept more would be a
  look at results before the freeze. A harness status is still a fact about a
  task, which is why rehearsal tasks belong to no population. It counts tokens
  and fabricates no money cost; the report's ranges cover completed sessions
  only, beside a count per status, because a session that failed or timed out
  says nothing about what a session costs.
- **Its ceilings are constants, not defaults.** Six sessions **per directory
  holding the key file**: the ledger lies beside the key under a fixed name, so
  neither another output directory nor a copy of the key file beside it gives
  the allowance back, and a lock beside the key — it names the process that
  took it — refuses a second run at the same time. A session is counted once
  its copy is ready and before its process starts: a crash still counts, a copy
  that could not be made does not. A run stops at the first session that fails
  — failed to start, failed turn, error exit: a CLI that rejects a flag or the
  key would otherwise burn the allowance in seconds. A run that stopped early
  says so in the command line's exit code (`4`). Flags lower both ceilings and
  never raise them. A mistyped commit, a refused key, a task file that cannot
  be read, a prompt of more than 4096 bytes, an argument that a batch shim of
  the CLI would hand back to the shell as a command, a Git that is itself a
  batch shim, or an output directory whose records cannot be read is refused
  before it can cost a session.
- **Fifteen minutes per session, while the launcher is alive** — plus at most
  some forty seconds to kill it and drain its pipes. The write of the prompt
  itself has no timeout on Windows; what stands in for one is the bound on its
  size — on the author's host a pipe that is never read took 4096 bytes and
  blocked at 4097, and the system promises no size: a test writes a prompt of
  the largest size allowed to a process that never reads. On overrun the
  process tree is killed. A kill that does not take is printed with the process id,
  and so is the proof that it missed something: pipes still held once the tree
  is dead. What holds them is not awaited and not killed — it is recorded, and
  the run stops there: no session is started while something of the last one
  may still be alive. An interruption of the launcher — Ctrl+C anywhere,
  Ctrl+Break on Windows; on POSIX a termination signal, a quit (`SIGQUIT`), or
  the terminal closing (`SIGHUP`) — kills the session before it propagates. A
  launcher killed outright — its process terminated,
  which on Windows is what any termination request from outside amounts to, or
  the power cut — kills nothing: the session it started runs to its own end.
  The durable fix on Windows, a job object that dies with the launcher, is not
  built.
- **The ledger is not a spend limit.** It guards against mistakes — not
  against its owner, who can delete it, and not against the agent the launcher
  starts, which on a platform without a sandbox can write the ledger like
  anything else. The money bound is the hard limit set on the key's project at
  the provider — outside this script, and not instantaneous.
- **No key file, nothing starts.** A file that does not hold exactly one API
  key — several tokens, too long, not the provider's prefix — is refused: a
  wrong file is not sent to the CLI. The CLI that receives the key is resolved
  from an absolute path or an absolute `PATH` entry, never from the current
  directory; it has to state a version before the key is handed to it, and
  `--cli-version` pins that version. The key travels on standard input to
  `codex login --with-api-key`, into a `CODEX_HOME` created beside the key file
  for the run and deleted after it; it is never an argument, an environment
  value, a log line or a report field. That home's configuration pins the CLI's
  credential store to a file inside it, and the run stops before any session
  unless the login really left its credentials there: a CLI that put the key
  where the launcher cannot delete it — an OS keyring — is refused, with the
  instruction to revoke the key. The pin is the setting the CLI documents; it
  was not exercised against the installed CLI, which is why the check after the
  login exists. **The agent can read the key it runs under** — the CLI keeps it
  in the home the agent is given: that is why the key is dedicated and capped,
  and why it is to be **revoked when the rehearsal is over**.
- **Whether the key copy is gone is said, as far as the launcher still runs.**
  It is printed on every path on which the launcher still runs — an
  interruption included, and a second one while the copy is being deleted,
  which is held back until the deletion is over; it is written in the report,
  and is the command line's exit code (`3`), when the run reaches its end. A
  run that died without unwinding leaves that home, and the key in it, behind:
  the next run looks for it first, under the lock, deletes it, says so, and
  refuses once. If the home cannot be deleted, do not read what else it holds:
  the CLI is asked not to persist session files (`--ephemeral`), and that is a
  request, not something the launcher checks.
- **The launcher deletes only real directories it made, and checks that the
  home is one before it writes in it.** A link or a junction — under the name
  of a home, of a session copy, or anywhere inside one — is never followed and
  never removed: what lies behind it is not the launcher's, the owner's own
  agent home least of all. It is named, and the run refuses or reports a
  failure. These are checks, not locks: a name swapped for a link between a
  check and the write or the removal it guards is not caught, and whatever
  does that is already writing beside the key file. The launcher refuses to
  start on a Python older than 3.12: what tells a junction from a directory
  does not exist before.
- **The agent inherits almost nothing — and that is not a jail.** An
  allow-listed environment: every home and temporary directory and Git's global
  configuration inside the isolated home, Git's system configuration and
  credential prompts off, a `PATH` rebuilt from the few tools it needs; its own
  copy of one commit, extracted from Git objects; the source repository only
  read. The launcher never reads, copies or writes the owner's own agent home
  or login — **the agent it starts is another matter**. Every session is
  started with `--sandbox workspace-write` and `--ephemeral`: nothing in the
  launcher takes them away, and a test holds both. `--codex-command` is the
  operator's own: what is put there comes before `exec`, and whether a
  configuration override placed there could outweigh the explicit flag was
  not verified on the installed CLI. The CLI's `workspace-write`
  policy lets the agent **read the whole file system** by design. What stops
  it from **writing** outside its copy is the platform's
  sandbox: documented for Linux and macOS. On the author's host it was
  exercised before any session, under WSL2 and with CLI `0.116.0`, through the
  CLI's own `sandbox linux --full-auto` subcommand: a write and a deletion
  outside the working directory — in the Linux home and on a mounted Windows
  drive — were refused, each beside the same attempt without the sandbox,
  which succeeded; a read outside was allowed, as designed, and the network
  was off for the sandboxed command. That exercises the mechanism on one host,
  not a real `exec` session, which needs the key. **On native Windows the CLI applies
  no sandbox unless its configuration asks for one, and the launcher does not
  ask**: the author could not exercise the Windows sandbox, and it is not
  something to switch on blind on the owner's machine. On Windows the agent can
  read and write whatever the account running the launcher can — the owner's
  own agent home included, and the launcher's ledger and lock — and two of the
  six tasks ask it to edit files and run commands. Nor does the launcher
  control what the CLI writes outside `CODEX_HOME` — on Windows a redirected
  home does not move what a program asks the system for — or the network. Do
  not run it under a harness that prints local variables in tracebacks: the key
  is one of them until the login is done, and Python does not erase it from
  memory.

Four independent security reviews preceded any real run, each by a fresh
reviewer, and each returned "fix before the first real run". The first found
five serious defects, all on error paths (a killed session awaited without a
bound, an interruption that left the session running, a removal reported
without looking, the CLI resolved from the current directory, an allowance
tied to the output directory). The second found two: a run that dies without
unwinding left the key on disk and no later run noticed; and the text
disclaimed what the agent can read, never what it can write. The third found
two more, **one of them introduced by the correction of the second**: the
sweep of a dead run's key copy would delete *through* a junction planted under
that name — the owner's own login, on the platform where the agent can write
anywhere — and the branch that proves a kill missed something said nothing.
The fourth found no serious defect and four lesser ones it still wanted closed
first: on POSIX a closed terminal or a quit ended the launcher without
unwinding, against what this page claimed, and left a paid session with
nothing to bound it; a removal could report as removed a name that a link had
taken meanwhile; the link policy held for what the launcher deletes and not
for what it writes; and no test held the sandbox flag a session is started
with. Each review also found lesser defects.

The fourth reviewer then re-read the corrections of its own findings and
returned "safe to run once the key exists", under conditions: the CLI version
pinned, a single session first and its report read before any other, an output
directory outside the repository, the provider's hard limit checked to be
active, the key revoked at the end — and an exit code of `4`, not `0`, to be
expected from a first contact that stops at its first session. It also found
three minor defects **that those corrections had introduced**: a refusal that
was not this run's could be printed in its name, on a path production does not
reach; the guard on a batch shim of Git missed the one argument the launcher
builds itself; and an interrupted sweep was followed by a sentence claiming a
killed session. They are closed here, with three claims of this page that
nothing held. Every round of corrections so far has introduced or left a
defect, and this one has not been re-read at the commit that made it: what was
re-read afterwards is recorded in `HANDOFF.md`, not here. **What an agent can
do on native Windows is not a defect a commit can close: it is a condition of
the first real run, for the owner to decide.**

`examples/lab-rehearsal-tasks.json` holds six throwaway tasks on this
repository. They were written by the author, belong to no population and are
excluded from any protocol. The tests drive the launcher with a stub standing
in for the agent CLI — a real process, which can overrun, leave a child or an
orphan behind, fail, write on its error stream, lie about its version and keep
the key elsewhere; on Windows one test runs it behind a batch shim, the shape
the real CLI has on the author's host, and another plants a junction under the
name of a home. No real session runs in the test suite or in CI, no test
delivers a real termination signal, and **the rehearsal itself has not run**.

## Non-claims

The lab cannot authenticate the host. A tool identity, a Git identity, a check
verdict, a duration, a cost and the providers used are declarations; the
limit codes say so on the record. Nothing here shows that the source-only or
the symbolic stack is fast enough, complete enough or cheaper than an index on
a real repository — that is HOK-804's measurement, not a property of this
contract.

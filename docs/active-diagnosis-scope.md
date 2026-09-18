# Active-diagnosis operational scope (HOK-799)

Status: **experimental framing**. A read-only inventory, a contract mapping and
six acceptance scenarios. Nothing here activates, disables, deletes, trains or
measures anything; see [ADR 0011](adr/0011-experimental-active-diagnosis.md)
for the boundary and [`docs/active-diagnosis.md`](active-diagnosis.md) for the
core this scope applies to.

This document fixes three things the later tranches must not invent: **which
decisions the controller helps with, which index/usage couples may ever be
considered for retirement, and which constraints stay mandatory.**

## 1. Perimeter

| Element | Fixed value | Not included |
| --- | --- | --- |
| Host | One personal workstation, recorded under the operator-chosen label `personal-workstation` (a label, never a hostname, account or address — see `GOVERNANCE.md`) | Any other machine |
| Agent families | Claude Code and Codex, each with its own host receipt store; one shared control-plane store | Any merged or cross-family store |
| Pilot repository | This repository, `latent-compass`, and its Git worktrees — the only scope declared `PERSONAL_LAB` | Every other personal repository: **unassessed**, excluded from any verdict |
| Professional environment | Excluded **as a class** and never named here | No professional asset is ever eligible |
| Obsidian vault | Excluded: authored memory and its two indexes are protected | No vault artefact is a candidate |

The observation was taken on 2026-09-19 with control-plane controller `2.5.0`,
Git `2.50.1` and uv `0.12.5`. It is a dated read, not a standing fact.

## 2. Inventory

The machine-readable inventory is
[`evidence/hok799-scope-inventory/inventory.json`](../evidence/hok799-scope-inventory/inventory.json):
25 assets in the `latent_compass.lab.migration` `1.0.0` contract. Its sealed
dry-run is `dry-run-report.json` beside it.

```text
inventory_digest  sha256:e139cfb224264d341c6206a21d9b444d804aeb17deb955e3acb97adf575a0fea
report_seal       sha256:853172528179fafafba2616a7ca128c79d0405c43c7e8fb443ff8b71211f3249
```

Each asset falls in exactly one of three classes. **Candidate** means
`DISABLE_LATER` with every gate still missing — never a permission.

### Candidates — the pilot perimeter only

| Asset | What it is |
| --- | --- |
| `graphify-code-graph.pilot-worktrees` | Derived structural graph artefacts kept for the pilot worktrees in the shared store (eight entries, about 109 MiB) |
| `graphify-worktree-cache.pilot-worktrees` | The git-ignored `graphify-out/` cache directory inside each pilot worktree |

For the pilot worktree the control plane reports Graphify configured and
**CCC and Semctx not configured**. The Graphify code graph is therefore the
only index the pilot can produce evidence about.

### Kept — shared wiring, symbolic tools and required evidence

These serve every enrolled worktree on the workstation, personal and
professional alike, so none can be retired from inside the pilot perimeter.

| Asset | What it is |
| --- | --- |
| `claude.hook.index-refresh` | Session-start and stop hook that signals and launches the reconcile worker |
| `claude.hook.index-mark-dirty` | Post-edit hook that invalidates the Claude receipt |
| `claude.hook.index-routing` | Per-prompt routing hook for Claude Code (compiled cache reader) |
| `codex.hook.index-routing` | The same routing hook, registered for Codex |
| `codex.hook.harness-adapter` | Multi-purpose Codex adapter; its index-related part is not isolated (ambiguous, kept) |
| `shared.worker.reconcile` | Controller and detached reconcile worker |
| `shared.task.index-control-sweep` | Scheduled out-of-session sweep of enrolled repositories |
| `shared.task.control-plane-autocommit` | Scheduled task whose purpose and consumers were not examined (ambiguous, kept) |
| `shared.store.control-plane` | Shared artefact store, about 9 GiB, **mixed scope** |
| `claude.store.host-receipts` | Claude route caches and receipts |
| `codex.store.host-receipts` | Codex route caches and receipts |
| `claude.mcp.code-intelligence` | The single code-intelligence gateway exposed to Claude Code |
| `codex.mcp.code-intelligence` | The same gateway exposed to Codex |
| `serena-symbolic-server.workstation` | Symbolic edit server (one resident process observed) |
| `native-lsp.claude-plugin` | Language-server bridge; its internal index is **kept and counted** |
| `semctx-semantic-layer.workstation` | Authored semantic layer whose gates stay mandatory (required evidence) |
| `claude.hook.unregistered-routing-advisor` | Hook script present on disk but registered nowhere observed (ambiguous, kept) |

### Out of scope — never eligible

| Asset | Why |
| --- | --- |
| `ccc-semantic-index.workstation` | Not provisioned for the pilot repository; present only in unassessed or excluded scopes |
| `personal-unassessed.worktree-artefacts` | Artefacts of personal repositories no pilot names |
| `professional.code-index-servers` | Professional scope |
| `professional.worktree-artefacts` | Professional scope |
| `vault-graphify.graph` | Vault |
| `vault-semantic.dense-index` | Vault |

### What the inventory establishes, and what it does not

- **The shared store has no personal/professional separation.** Its admission
  gate denies the vault, agent memory and session folders, temporary and
  dependency directories; no rule separates personal from professional roots.
  Any retirement must therefore be decided per *(provider, worktree)* couple
  and never for the store as a whole.
- **CCC cannot be judged on the pilot.** The admission policy provisions CCC
  only from 150 code files in at least two languages; this repository tracks
  about 90, all Python, and carries no CCC settings. A CCC verdict needs a
  second named pilot repository — an owner decision (U1).
- **Hidden indexes are declared, not denied.** Language servers keep their own
  index, and the lab's justification memory is itself a specialised lookup
  structure. Both count as residual cost; neither supports a “zero index”
  claim.
- **Configured is not used.** Every boolean above says an asset exists. None
  attests freshness, consumption frequency, usefulness or safe removal.
- Configuration digests bind the bytes of one named configuration source per
  asset at observation time. The label-to-path mapping is operator-held,
  because this repository refuses private host paths. A digest is a drift
  anchor, not proof of what the file means.

## 3. The decision contract, term by term

| HOK-799 term | Where it lives in the `1.0.0` lab contract |
| --- | --- |
| Problem to solve | One `DiagnosisModel` (`model_id`) and its set of terminal `decisions` |
| Competing hypotheses, including “none of the above” | `worlds`. A residual world is declared explicitly when the operator cannot enumerate; an observation no declared world explains is refused **outside** the model and never renormalised |
| Possible observations | `probes` with total outcome tables, answered by an adapter of a declared capability |
| Final actions | `decisions`, one of which is the mandatory unconditional abstention |
| Stop conditions | `STOP` when no affordable probe beats `R(E)`; budget, horizon and the controller's own `max_expansions`; a truncated search is labelled inexact |
| Budget | `budget` in policy units; shared pools in `latent_compass.lab.routing` |
| Authority | None. Every report carries the non-authority notice; the host keeps router and executor |

Applicability is bounded by the episode's `LabBinding`: host, agent family and
a source-scope digest sealing the model, the snapshot manifest, its root
identity and the full probe catalog. Dirty and untracked content is covered
exactly when the file is in the manifest, by the digest of the bytes read;
anything outside the manifest is outside the claim. A declared Git `HEAD` is a
declaration. The `1.0.0` core binding does not bind tool versions by itself;
a [host session](host-observations.md) does, by sealing each probe's expected
tool identity and version into the episode, so a tool upgrade is a new episode
(U6).

## 4. Loss and budget policy

One unit everywhere: the **cost-point**. Losses and probe costs are declared in
it and are directly comparable; there is no hidden conversion constant.

| Concern | How it is expressed |
| --- | --- |
| Correctness | The loss of a terminal decision in a world where it is wrong |
| Critical omission | A larger loss cell on the world carrying the omission — never an average |
| Total cost | Probe `cost`: tokens, tool time and delegated work, converted by the scenario's own declared table |
| Delay | Part of probe cost. There is no separate discounting, and that is a stated limit |
| Safety and authority | `required_evidence` on the decision — **admissibility**, never a loss term, so no cost saving can buy it |

Every number is an operator hypothesis. When no defensible number exists the
answer is not a guess: the quantity is declared `UNKNOWN` and the controller
abstains or the host escalates. Exactness is only ever relative to the
supplied finite model, budget and horizon.

## 5. Known, assumed and unknown

| Kind | Content |
| --- | --- |
| Known (observed, dated) | The inventory above; provider configuration for the pilot worktree; hook and gateway registration on both hosts |
| Model hypotheses | Every world, weight, loss and cost in any model, including the scenario fixtures |
| Intervals | None established |
| Unknown | The list in section 8, each with an owner |

The first prototype uses an explicit revisable model. It needs no ranker and
no training corpus, and the cancelled ranker's holdout is not reused.

## 6. Roles

| Role | Holder | Boundary |
| --- | --- | --- |
| Advisor | `latent_compass.lab` planner and routing advice | Proposes; holds no execution, promotion or judge-mutation capability |
| Router | The host's already-authorised router | Accepts or ignores the advice |
| Executor | The host | Runs tools under its existing permissions |
| Evaluator | An independent party under HOK-804 | The lab only tabulates what it is given |

Historical memory and reconciliation contracts and seals are unchanged; lab
state lives in its own surface, so a routine probe never becomes a
`STRATEGIC_HIGH_IMPACT` record.

## 7. Acceptance scenarios

Fixtures live in [`examples/lab-scenarios/`](../examples/lab-scenarios/) and
are checked against hand-derived exact values by
`tests/test_lab_scope_scenarios.py`. Their numbers are illustrative.

| Kind | Behaviour the contract must show | What it cannot show |
| --- | --- | --- |
| `KNOWN_IDENTIFIER` | An exact search is proposed before any edit; an empty result ends in abstention | That exact search finds real identifiers fast enough |
| `CONCEPT_WITHOUT_SHARED_TERMS` | A literal search on the request's own words is never proposed; a structural walk is, when affordable | Whether structure reaches the concept in real code — the use a semantic index serves today |
| `INDIRECT_DEPENDENCY` | An empty direct-reference result leaves the indirect world alive; the discriminating probe comes first | That a registry key is searchable in practice |
| `REQUIREMENT_ABSENT_FROM_CODE` | The change stays inadmissible until the authored requirement is observed, even when cheaper in expectation | Who answers, and how fast |
| `CONTRADICTION` | The contradicting observation is refused outside the model; the prior state is untouched | Which of the two observations was wrong |
| `BUDGET_EXHAUSTED` | Stops on the best admissible decision, invents no probe value, labels a truncated search inexact | Whether the declared budget was the right one |

The lab itself only ever runs a literal search. File interpretation, Git diff,
symbol navigation and targeted checks are run by the host executor and admitted
through [`docs/host-observations.md`](host-observations.md); an authored
requirement is external to source by nature. The fixture manifest records that
split, and its test binds it to the real observation kinds.

Criteria and owners: the repository owner freezes the HOK-804 protocol
(population, budgets, primary metric, non-inferiority margin, useful cost
improvement) **before** the first measurement and judges nothing themselves;
an independent evaluator judges outcomes. The repository owner alone grants the
HOK-805 pilot authorisation and, separately, any irreversible deletion under
HOK-806.

## 8. Unknowns and their owners

| Id | Unknown | Owner — resolved in |
| --- | --- | --- |
| U1 | Whether a second personal repository, with CCC provisioned, joins the pilot. Without it CCC receives no verdict | Repository owner — HOK-804 protocol freeze |
| U2 | How often either host actually consumes the pilot's Graphify graph | HOK-804 baseline measurement |
| U3 | Owners and command lines of the resident index processes (observed by image name only) | Operator — HOK-806 dry-run |
| U4 | Purpose and consumers of the autocommit task and of the unregistered advisor script | Operator — HOK-806 dry-run |
| U5 | Which part of the Codex harness adapter is index-related | HOK-803 bench |
| U6 | Tool identity (Git, language servers, test runners) bound to each observation — **closed in the contract** by the host session; the identity stays a host declaration | HOK-803 bench, on observed tool versions |
| U7 | Cost of language-server indexes and caches | HOK-804 cost accounting |
| U8 | Every numeric budget, margin and threshold of the real experiment | Repository owner — HOK-804 preregistration |
| U9 | Whether the pilot repository must be enrolled in Semctx before HOK-805 | Repository owner — HOK-805 entry conditions |

## 9. Non-claims

No index has been retired, disabled or shown to be unnecessary. No provider
was started, refreshed or reconfigured to take this inventory. A dry-run
disposition is not an authorisation, and a green scenario test is evidence
about the decision contract, not about any repository.

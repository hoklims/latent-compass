# Active-diagnosis operational scope (HOK-799)

- Status: **experimental framing**
- Revision: 3, 2026-09-19 (UTC) — records the owner's decision U1: a second
  personal repository joins the pilot. Revision 2 (2026-09-18) corrected
  revision 1 after an independent read-only review, written the same day

A read-only inventory, a contract mapping and
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
| Pilot repositories | This repository, `latent-compass`, and its Git worktrees; and, by the owner's decision of 2026-09-19 (U1), one second personal repository, named to the operator and recorded here under the alias `pilot-second-repository`. These are the only two scopes declared `PERSONAL_LAB` | Every other personal repository: **unassessed**, excluded from any verdict |
| Professional environment | Excluded **as a class** and never named here | No professional asset is ever eligible |
| Obsidian vault | Excluded: authored memory and its two indexes are protected | No vault artefact is a candidate |

The observation was read on 2026-09-18, ending at 22:38 UTC (2026-09-19 local
time), with control-plane controller `2.5.0`, Git `2.50.1` and uv `0.12.5`. It
is a dated read, not a standing fact; every size below is as of that read.
The second pilot repository was read separately, on 2026-09-19 at 12:56 UTC,
the same way; its sizes are as of that second read.

Every size, file count, language count and age in this document is an
**operator declaration**, and so is every statement about how a read was made
— that it started, refreshed or reconfigured nothing, here and where the ADR
or `evidence/README.md` repeat it. The committed inventory carries none of
them — the `1.0.0` asset contract has no size, count or age field — and no
test or artefact in this repository supports them. The reads they come from
are operator-held, because they name repositories and host paths this
repository refuses. They explain a decision; they prove nothing, and nobody
outside the workstation can check them.

## 2. Inventory

The machine-readable inventory is 30 assets in the
`latent_compass.lab.migration` `1.0.0` contract, committed in three revisions.
Each directory holds `inventory.json` and its sealed `dry-run-report.json`.

| Revision | Directory | Report `generated_at` |
| --- | --- | --- |
| 1.0.0 — superseded, kept exactly as committed | `evidence/hok799-scope-inventory/` | `2026-09-19T00:00:00Z` |
| 1.1.0 — superseded, kept exactly as committed | `evidence/hok799-scope-inventory-v1.1.0/` | `2026-09-18T23:47:51Z` |
| 1.2.0 — **current** | `evidence/hok799-scope-inventory-v1.2.0/` | `2026-09-19T12:57:30Z` |

```text
1.0.0  inventory_digest  sha256:e139cfb224264d341c6206a21d9b444d804aeb17deb955e3acb97adf575a0fea
1.0.0  report_seal       sha256:853172528179fafafba2616a7ca128c79d0405c43c7e8fb443ff8b71211f3249
1.1.0  inventory_digest  sha256:b7d494805b10ad1b2f08151de84bdd8ecd36371bb06bb95f2f16982cb6e38887
1.1.0  report_seal       sha256:3e2e819e274144f1bf736f71a86cc35e946c412e9f59792c37517e95fb7fcb31
1.2.0  inventory_digest  sha256:8b0468fc231cd5f6720a2a688c4e08b0cf3a9ee0325144d27096a24640d17a67
1.2.0  report_seal       sha256:df4c53db79580da7f186e0fc9ef4aeaa9d9efc7d0f9dd70145b6ad586f9121a9
```

Revision 1.1.0 is the same read: the same 25 assets and the same configuration
digests. It corrects two defects an independent review found in 1.0.0. Three
assets that were recorded but never assessed — the two professional ones and
`personal-unassessed.worktree-artefacts` — were declared `CANDIDATE_INDEX`, the
only class that can ever leave `KEEP`; they are now `UNKNOWN`. And 1.0.0's
`generated_at` is a placeholder later than the commit that carries it, where
1.1.0 states the instant its report was really built. Corrections append: 1.0.0
is never edited, and a test pins every revision's seals.

Revision 1.2.0 carries the owner's decision U1. It is revision 1.1.0 asset for
asset, in the same order, plus five assets: the index artefacts found in the
second pilot repository. Nothing that was read before changed class or scope,
and a test says so. By the operator's account, the second read started,
refreshed and reconfigured nothing, and left that repository's working tree as
it found it.

Each asset falls in exactly one of three classes. **Candidate** means
`DISABLE_LATER` with every gate still missing — never a permission. The
contract's third action, `RETAIN_FOR_ROLLBACK`, appears nowhere in this
inventory: it needs backup, restore and stability references, and those three
gates close **by declaration** — a digest the operator supplies, never one the
lab dereferences.

### Candidates — the first pilot perimeter

| Asset | What it is |
| --- | --- |
| `graphify-code-graph.pilot-worktrees` | Derived structural graph artefacts kept for the pilot worktrees in the shared store (eight entries, about 109 MiB at the read) |
| `graphify-worktree-cache.pilot-worktrees` | The git-ignored `graphify-out/` cache directory inside each pilot worktree |

For the pilot worktree the control plane reports Graphify configured and
**CCC and Semctx not configured**. The Graphify code graph is therefore the
only index the first pilot can produce evidence about.

### Candidates — the second pilot repository (owner decision U1)

| Asset | What it is |
| --- | --- |
| `ccc-semantic-index.pilot-second-repository` | The CCC semantic index kept inside that checkout, with its own settings file (about 461 MiB at the read) |
| `graphify-code-graph.pilot-second-repository` | Derived structural graph artefacts kept for that repository in the shared store (one entry, about 52 MiB at the read) |
| `graphify-worktree-cache.pilot-second-repository` | The git-ignored `graphify-out/` cache directory inside that checkout (under 1 MiB) |

By the operator's read, that repository tracked 1,671 files, 939 of them code
in five languages, on a single checkout: above the 150-file, two-language
admission threshold the first pilot stays under. The newest file date among its
index artefacts, and among both hosts' route caches for it, was about 23 days
before the read. A file date is not a freshness verdict and measures no use
(U2).

### Kept inside the second pilot — by classification alone

| Asset | What it is |
| --- | --- |
| `semctx-semantic-layer.pilot-second-repository` | That repository's authored semantic layer (about 125 MiB at the read): required evidence, whose gates stay mandatory |
| `serena-symbolic-cache.pilot-second-repository` | That repository's symbolic-tool cache and project settings (about 69 MiB at the read) |

### Kept — shared wiring, symbolic tools and required evidence

These serve every enrolled worktree on the workstation, personal and
professional alike, so none can be retired from inside a pilot perimeter.

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
| `shared.store.control-plane` | Shared artefact store, about 9 GiB at the read, **mixed scope** |
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
| `ccc-semantic-index.workstation` | Recorded under the `personal-unassessed` scope, outside both pilots. The second pilot's own CCC index is a separate asset, recorded under that pilot's scope |
| `personal-unassessed.worktree-artefacts` | Artefacts of personal repositories no pilot names; recorded, never assessed |
| `professional.code-index-servers` | Professional scope; recorded as a class, never assessed |
| `professional.worktree-artefacts` | Professional scope; recorded as a class, never assessed |
| `vault-graphify.graph` | Vault |
| `vault-semantic.dense-index` | Vault |

### What the inventory establishes, and what it does not

- **Excluded assets hold by two locks, index machinery by one.** Professional,
  vault, unassessed and ambiguous assets are protected by their classification
  *and* by their scope. The ten identified workstation-wide index assets — hooks,
  worker, sweep, stores and CCC — are `CANDIDATE_INDEX` and hold by their
  `UNSCOPED` scope alone. Re-scoping one of them into a pilot would turn a
  pinned exact-set test red and yield `DISABLE_LATER` with all seven gates
  missing, never a permission. The owner's U1 decision did **not** do that: it
  added the second repository's own assets and left the workstation-wide
  machinery where it was.
- **Inside a pilot perimeter a kept asset holds by one lock.** Joining a pilot
  removes the scope lock. The second pilot's Semctx layer and symbolic-tool
  cache are kept by their classification alone, and a test shows that this
  classification is the only thing holding them.
- **The shared store has no personal/professional separation.** Its admission
  gate denies the vault, agent memory and session folders, temporary and
  dependency directories; no rule separates personal from professional roots.
  Any retirement must therefore be decided per *(provider, worktree)* couple
  and never for the store as a whole.
- **CCC has one subject: the second pilot.** The committed inventory holds a
  CCC asset, with a settings digest, for the second pilot and none for this
  repository. The operator's reads say why — the admission policy provisions
  CCC only from 150 code files in at least two languages, and this repository
  tracked about 90, all Python — but that reason is declared, not shown here.
  A CCC verdict needed a second named pilot repository — the owner decision
  U1, taken on 2026-09-19. One repository is one subject: whatever HOK-804
  finds there says nothing about any other repository.
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
| Authority | None. Each lab output says so in its own form, and the forms differ: `PlanReport.non_authority_notice`, `RouteDecision.experimental`, the four literal booleans of `RetirementDryRunReport`, and `empirical_claim` / `causal_claim` on `LabEvaluationReport`. A missing notice therefore never means authority; the host keeps router and executor |

Applicability is bounded by the episode's `LabBinding`: host, agent family and
a source-scope digest sealing the model, the snapshot manifest, its root
identity and the full probe catalog. Dirty and untracked content is covered
exactly when the file is in the manifest, by the digest of the bytes read;
anything outside the manifest is outside the claim. A declared Git `HEAD`,
`root_id`, `host_id` and `agent_family` are all declarations: the lab checks
that what the caller declares matches what the snapshot declares, never an
authenticated fact. The `1.0.0` core binding does not bind tool versions by itself;
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
| U1 | Whether a second personal repository, with CCC provisioned, joins the pilot — **closed on 2026-09-19 by the repository owner: yes**, one repository, recorded under the alias `pilot-second-repository` (inventory revision 1.2.0) | Repository owner — decided before the HOK-804 protocol freeze |
| U2 | How often either host actually consumes either pilot's indexes. At the second read the second pilot's artefacts and route caches were about 23 days old; that dates them and measures nothing | HOK-804 baseline measurement |
| U3 | Owners and command lines of the resident index processes (observed by image name only) | Operator — HOK-806 dry-run |
| U4 | Purpose and consumers of the autocommit task and of the unregistered advisor script | Operator — HOK-806 dry-run |
| U5 | Which part of the Codex harness adapter is index-related | HOK-803 bench |
| U6 | Tool identity (Git, language servers, test runners) bound to each observation — **closed in the contract** by the host session; the identity stays a host declaration | HOK-803 bench, on observed tool versions |
| U7 | Cost of language-server indexes and caches | HOK-804 cost accounting |
| U8 | Every numeric budget, margin and threshold of the real experiment | Repository owner — HOK-804 preregistration |
| U9 | Whether the first pilot repository must be enrolled in Semctx before HOK-805; the inventory records a Semctx layer for the second pilot and none for the first | Repository owner — HOK-805 entry conditions |

## 9. Non-claims

No index has been retired, disabled or shown to be unnecessary. No provider
was started, refreshed or reconfigured to take this inventory. A dry-run
disposition is not an authorisation, and a green scenario test is evidence
about the decision contract, not about any repository.

# Active-diagnosis preregistration skeleton (HOK-804)

- Status: **skeleton — 16 of 34 decisions recorded, nothing is frozen, nothing
  was measured**
- Written: 2026-09-19 (UTC), after the owner's decision U1

This document lists what the repository owner has to decide before the first
HOK-804 trial (unknown U8 of
[`docs/active-diagnosis-scope.md`](active-diagnosis-scope.md)), and where each
decision would be frozen. Every cell marked `OPEN` is the owner's. The author
chose none: an option listed here is not a recommendation, and its order means
nothing. A decision is recorded by replacing `OPEN` with the value, its date
and who took it. Where the owner took an option the author had proposed, the
record says so.

The decisions dated 2026-09-19 were taken in one sitting: each on a proposal of
the author, put in plain language with one stated consequence, and answered by
the owner in a word. That is a weaker form of ownership than reading this
document row by row. Nothing is frozen: the owner can reopen any of them before
the freeze, and a changed decision is a new record with its own date.

It grants no authority to run anything. HOK-804 is a laboratory experiment; it
is neither a canary nor an activation, and a favourable result would unlock
neither (see [ADR 0011](adr/0011-experimental-active-diagnosis.md)).

## 1. What already exists, and what it cannot hold

`latent_compass.lab.evaluation`, contract `1.0.0`, already freezes a paired
design and seals it (`docs/lab-operations.md`, section 2). A
`LabEvaluationProtocol` holds exactly these fields: `contract_version`,
`protocol_id`, `candidate_identity`, `config_identity`, `source_identity`,
`expected_arms`, `task_ids`, `pair_ids`, `pair_task_bindings`, `min_cost`,
`max_cost`, `min_latency_ms`, `max_latency_ms`, `non_inferiority_margin`,
`cost_target`, `origin` and `frozen_at`.

Its report is exact counting: per-arm tallies, paired deltas against the
existing stack, one p95 convention. It runs no statistical test and returns no
verdict; `empirical_claim` and `causal_claim` are fixed to `false`. The margin
and the cost target are carried through and **never applied**.

Six properties of that contract shape the decisions below. They are facts
about the code, not choices:

1. The three arms are fixed, in canonical order: `EXISTING_STACK`,
   `SOURCE_ONLY`, `CONTROLLER_PLUS_SOURCE`. There is no fourth arm.
2. A closed trial set holds **exactly one trial per (pair, arm)**. A repetition
   cannot be a second trial of the same pair: it has to be its own pair.
3. A trial carries one integer `total_cost` and one `latency_ms`. A breakdown
   of cost lives outside the trial, or nowhere.
4. A protocol names one candidate, one configuration and one source. One
   protocol is one subject under one configuration.
5. Both paired deltas are computed against `EXISTING_STACK`. The report holds
   no paired delta between the two index-free arms: the controller's own
   effect is not in it.
6. A seal proves integrity, not chronology: it does not show that the protocol
   existed before the first trial.

## 2. Decisions the `1.0.0` protocol can hold

| Field | What has to be decided | Options and what each costs | Decided |
| --- | --- | --- | --- |
| `candidate_identity` | The exact candidate under test | A commit of this branch; a later commit is a different candidate. The report refuses trials that mix `candidate_generation` values; it does not check that a trial's generation is the protocol's candidate, so that binding is the operator's to keep | `OPEN` |
| `config_identity` | The one configuration of the index-free arms | See section 4: all candidate couples removed at once, or one couple at a time, each its own protocol | `OPEN` |
| `source_identity` | The snapshot the tasks run on | See section 3, "Snapshots". A protocol names one source, while tasks drawn from history each start from their own state: the workable reading is a sealed manifest of task to start commit beside the protocol, whose digest is the source identity | `OPEN` |
| `task_ids` | The task population | Tasks written for the experiment: controllable, and exposed to the author's blind spots. Tasks drawn from real past work: representative, and their expected outcome may be contestable. HOK-804 requires real tasks on isolated snapshots. The drawing rule itself — how many per stratum, and the prompt template of each stratum — is still to be written and approved before the freeze | **real past changes of the two pilot repositories, drawn by a rule written before the freeze and chosen by hand by nobody; each runs from the state before the change** — 2026-09-19, repository owner, on the author's proposal |
| `pair_ids`, `pair_task_bindings` | How many pairs, and whether a task repeats | One pair per task: cheapest, and run-to-run variance stays invisible. Several pairs per task: variance becomes visible, cost multiplies by the repetition count and by three arms | `OPEN` |
| `min_cost`, `max_cost` | The admissible cost range, and its unit | The contract names no unit; the cost-point of the scope document is the one used everywhere else. A trial outside the range is refused, not clipped. Too tight loses real trials; too loose is no bound | `OPEN` |
| `min_latency_ms`, `max_latency_ms` | The admissible latency range | Same trade-off. A timeout policy has to agree with `max_latency_ms` | `OPEN` |
| `non_inferiority_margin` | How much worse an index-free arm may be and still count as not inferior | A unit-interval number on a scale the contract does not name: the decision rule of section 3 has to say what it is a fraction of. Wider is easier to meet and protects less | `OPEN` |
| `cost_target` | The cost improvement that would make a retirement worth it | A non-negative integer in the unit chosen above. Below it, "not inferior" is not a reason to retire anything | `OPEN` |
| `origin` | `SYNTHETIC` or `REAL_DECLARED` | Real trials are `REAL_DECLARED`: declared real by the host, not verified by the lab | **REAL_DECLARED: real agent sessions on isolated copies, never on a live checkout** — 2026-09-19, repository owner, on the author's proposal |
| `frozen_at` | The instant of the freeze | Stated by the operator. See "Chronology anchor" below | `OPEN` |

This repository is public and one pilot repository is private: task ids are
opaque, and no task text, path or name of a private repository enters this
repository. That is a constraint of the repository, not a decision.

## 3. Decisions the `1.0.0` protocol cannot hold

HOK-804 requires each of these to be fixed before the first decision
measurement. The contract has no field for them. Where they are frozen is
itself a decision, and a row of the table below.

| Decision | Why it cannot be skipped | Options and what each costs | Decided |
| --- | --- | --- | --- |
| Snapshots — index state | A result binds a version and a state. By the operator's read, the newest file date among the second pilot's index artefacts was about 23 days earlier — a declared date, not a usage measure | Indexes as found: measures the stack as it really is, stale or not. Indexes rebuilt on the snapshot: measures the stack at its best, and the rebuild has a cost. With tasks run from the state before a past change, an index as found was built on a later state and holds the answer: the `task_ids` decision closes that option | **indexes rebuilt on the state of the isolated copy before its trials — an index built later would hold the answer — so the existing stack runs at its best; nothing is said about day-to-day use of older indexes** — 2026-09-19, repository owner, on the author's proposal |
| Snapshots — caches per arm | HOK-804 asks for the initial state and for cold or warm caches to be documented | Warm or cold caches, stated per arm before trials | `OPEN` |
| Strata | HOK-804 names six: known identifier, conceptual need, architecture or impact, business constraint absent from the code, large repository, branch change | Which stratum each task belongs to, fixed before trials. A stratum with no task has no result, and saying so is a result | **the six strata HOK-804 names; a task's stratum is fixed with the task list, and a stratum with no real example has no trial, which the report says** — 2026-09-19, repository owner, on the author's proposal |
| Exclusions | An exclusion chosen after results is a favourable stop in disguise | Rules for excluding a task or a trial, written before any trial. Failed tasks stay in the denominator | **no task or trial is excluded after the freeze; a failed task stays in the denominator** — 2026-09-19, repository owner, on the author's proposal |
| What `SUCCESS` means | The report counts successes; the contract does not define one | Judged by whom, on which artefacts, blind to the arm or not. An agent judge is cheap and shares the models' blind spots. The owner judging is authoritative and slow. Two judges give an agreement rate and double the cost. Both agent families co-wrote the candidate, so no agent judge is independent by its family: blinding, the mechanical check first and the owner's sample are the guards | **a mechanical check where one exists; otherwise an agent judge blind to the arm and of another family than the runner; the owner audits one trial in five, drawn before unblinding** — 2026-09-19, repository owner, on the author's proposal |
| What a critical omission is | `critical_violation` is one boolean per trial | The list of omissions that count, fixed before trials. They are an admissibility condition, never traded against cost | `OPEN` |
| Cost accounting — conversion table | One integer per trial has to stand for models and sub-agents, tokens, reads, specialised memory, RAM, disk | The conversion table into cost-points. Language-server indexes are declared and counted (unknown U7) | `OPEN` |
| Cost accounting — index build and maintenance | Rebuilding the indexes on each copy has a cost, and so does keeping them | Amortised into each `EXISTING_STACK` trial: needs a usage rate nobody has (U2), and inflates the existing stack's per-trial cost. Reported beside the trials: trials compare like with like, and the upkeep stays visible for the final decision | **reported beside the trials, never amortised into them** — 2026-09-19, repository owner, on the author's proposal |
| Decision rule | The verdicts are `RETIRABLE_EN_PILOTE`, `CONSERVER` and `PREUVE_INSUFFISANTE`; the report returns none of them | How per-arm tallies, paired deltas, critical violations, the margin and the cost target map to one verdict, written before trials. What `pairs_incomplete` does to a verdict. A green test, a zero usage rate or a better mean time is not a reason to retire | `OPEN` |
| Power, or a bounded descriptive study | HOK-804 asks for one or the other, explicitly | A powered design needs a variance estimate nobody has yet, and many pairs. A bounded descriptive study is affordable and can only support `CONSERVER` or `PREUVE_INSUFFISANTE` with confidence; what it may support beyond that has to be said now. Taken with its consequence stated: no retirement can follow from the first experiment alone | **a bounded descriptive study first: a small series fixed in advance, which may support CONSERVER or PREUVE_INSUFFISANTE and never RETIRABLE_EN_PILOTE; its variance estimate sizes a powered protocol, registered separately** — 2026-09-19, repository owner, on the author's proposal |
| Stopping and reruns | No favourable stop chosen after results | The pair count is fixed up front. An infrastructure failure is `MISSING`, or is rerun under a rule written before trials — never decided case by case | **the pair count is fixed at the freeze; a harness failure before the agent's first action is rerun once, any later failure is MISSING** — 2026-09-19, repository owner, on the author's proposal |
| Second-stage rule | The owner's two-stage decision (section 4) opens a second stage after the first is seen; a rule written then would be a stop chosen after results | What counts as a regression for a pilot: the decision rule above, or a separate threshold. Which per-couple protocols then run: one per couple; or one per agent-facing index, a cache following its graph (section 4 says why a cache-only protocol would measure rebuild cost, not quality) | `OPEN` |
| Memory | HOK-804 asks to separate the effect of the controller, of the memory and of the index removal; three arms separate two of them | Justification memory off in `CONTROLLER_PLUS_SOURCE`: the controller's effect is isolated, the memory's is not measured. On: they are confounded. Two protocols: both are measured, at twice the cost. HOK-246 remains the longitudinal memory experiment | **justification memory off in the controller arm; its effect is not measured here and stays with HOK-246** — 2026-09-19, repository owner, on the author's proposal |
| Which plan the controller arm uses | The candidate produces two plan documents | The free `1.0.0` plan, or the constrained `1.1.0` plan and the condition under which the host declares a probe unobtainable. A constrained plan is exact for the restricted problem only; a false declaration is a failure mode to count, not to exclude afterwards | `OPEN` |
| Agent family | Both hosts consume the candidate indexes, so a retirement needs both families to do without them | One family: cheaper, and says nothing about the other. Both: the design doubles | **one agent family for the bounded series: Codex; nothing is said about the other family, which a powered protocol has to cover before any retirement** — 2026-09-19, repository owner, on the author's proposal |
| Isolation of real sessions | Tasks run on isolated snapshots and never modify an active working environment; the Codex and Claude stores stay separate | How each arm's configuration is derived without touching the active one, and where the session stores live. An owner decision shared with HOK-803 | `OPEN` |
| Spend ceiling | Three arms times pairs times repetitions of real agent sessions | A ceiling fixed before the first trial. Reaching it is a stop rule like any other, written now | `OPEN` |
| Reuse of the HOK-253/HOK-254 collection | HOK-804 allows it only with verified epoch identity, population and absence of leakage | Reuse: no new collection, and the verification is its own work. A separate isolated experiment: clean by construction, and everything has to be collected. No holdout of the cancelled programme is reused | **no reuse: a separate isolated experiment, nothing collected earlier enters it** — 2026-09-19, repository owner, on the author's proposal |
| Chronology anchor | A seal does not prove when the protocol was registered | A reviewed commit of the sealed protocol pushed before any trial; a CI run on that commit; an external transparency log. The anchor has to exist before data access, or the preregistration claims nothing | **a reviewed commit of the sealed protocols pushed before any trial, and the CI run on that commit** — 2026-09-19, repository owner, on the author's proposal |
| Publication | Results by stratum, with uncertainties and the cases that regress | What is published when the result is unfavourable or insufficient — which is a normal outcome of this experiment | **results by stratum, with uncertainties and the cases that regress, are published whatever the outcome** — 2026-09-19, repository owner, on the author's proposal |
| Rehearsal outside the protocol | Cost and latency ranges, the ceiling and the pair count cannot be chosen blind, and a rehearsal that recorded outcomes would be a look at results before the freeze | None: the ranges are guessed. A rehearsal on throwaway tasks excluded from the population, recording duration and cost only | **six real sessions of at most fifteen minutes on a throwaway copy, outside the protocol and excluded from the population; only duration and cost are recorded, never an outcome; paid through a dedicated key under a 25 USD hard limit** — 2026-09-19, repository owner, on the author's proposal |
| Where the decisions without a field are frozen | The contract has no field for the rows of this table | In this document, pinned by digest beside the sealed protocols: cheap, and only as strong as the pin. In a new contract version beside `1.0.0`: enforced by code, and a code change with its own review | `OPEN` |

## 4. One protocol, or several

Two facts meet here. A protocol is one subject under one configuration
(section 1, property 4). And the verdict HOK-804 asks for is **per index/usage
couple**, of which the committed inventory holds five candidates:

| Pilot | Candidate couple |
| --- | --- |
| First | `graphify-code-graph.pilot-worktrees` |
| First | `graphify-worktree-cache.pilot-worktrees` |
| Second | `ccc-semantic-index.pilot-second-repository` |
| Second | `graphify-code-graph.pilot-second-repository` |
| Second | `graphify-worktree-cache.pilot-second-repository` |

One repository is one subject: a result on one pilot says nothing about the
other, so the two pilots cannot share a protocol. Within a pilot:

| Option | What it gives | What it costs |
| --- | --- | --- |
| One protocol per pilot, every candidate couple removed at once | The fewest trials, on the configuration a joint retirement would produce | One grouped result per pilot. A regression cannot be attributed to a couple. Without one, a per-couple verdict holds for that configuration only — every candidate couple of the pilot removed together — which is the binding HOK-804 asks of a verdict |
| One protocol per couple | A regression that can be attributed | Up to five protocols, each with three arms over the whole task population: a trial is sealed to one protocol, so the existing-stack arm runs again for each. Two indexes that cover for each other would each look removable alone, so per-couple results do not add up to a joint retirement |
| Grouped first, then per couple only where the grouped result is not clearly unfavourable | Fewer trials when the answer is no | A two-stage design: the second stage is decided after seeing the first, so its rule has to be written before the first stage runs. A clearly unfavourable grouped result stops a pilot, couples that were removable alone included |
| Grouped first, then per couple only where the grouped result shows a regression | Fewer trials when the answer is yes, and attribution where it is missing | The same two-stage constraint. A favourable result rests on the grouped protocol alone |

What the index-free arms may not remove is already fixed: the second pilot's
Semctx layer and symbolic-tool cache are kept by their classification, and no
asset outside the two pilot perimeters is eligible for anything but `KEEP`.

Two of the five couples are worktree-local caches. In the committed inventory
their only declared consumer is `shared.worker.reconcile` — a declaration, not
observed use (U2). If it holds, removing a cache alone changes no input of an
agent-facing consumer: such a protocol would measure rebuild cost, which
belongs to "Cost accounting" in section 3, not quality.

Decided: **grouped first, then per couple only where the grouped result shows
a regression** — 2026-09-19, repository owner, on the author's proposal.

The decision leaves its second stage unwritten. What counts as a regression,
and which per-couple protocols then run, are the "Second-stage rule" row of
section 3, still `OPEN`.

## 5. Freeze procedure

This is the order of operations once every row above is decided. It is
mechanical and holds no parameter.

1. Replace every `OPEN` in this document with its value, date and decider.
2. Write each protocol as JSON, admit it with `admit_lab_evaluation_protocol`,
   and record its `protocol_seal`.
3. Commit the protocols, their seals and this document's digest together, and
   establish the chronology anchor chosen in section 3.
4. Only then run the first trial. Every declared `(pair, arm)` slot is
   submitted, a slot where nothing ran as an explicit `MISSING`.
5. A change after the anchor is a new protocol under a new `protocol_id`,
   beside the old one. A frozen protocol is never edited.

## 6. Non-claims

Nothing here was measured, and no number in this document is a result, a
default or a suggestion. The skeleton fixes no population, budget, metric,
margin or threshold. It does not show that the experiment is affordable, that
its design is powered, or that any index can be retired. Until its rows are
decided and anchored, HOK-804 stays unstarted.

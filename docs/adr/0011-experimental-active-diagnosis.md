# ADR 0011 — An isolated laboratory for active diagnosis

- Status: accepted for local implementation; activation remains gated
- Date: 2026-09-18
- Scope: HOK-798, HOK-799 through HOK-806
- Authority: owner requested implementation; Codex owns this bounded design
- Starting source: `6ec94e86f5b6c429fd264708f28d5105b19fbafa`

## Context and objective

The current package records supplied decisions and observations. Its fixed
offline baselines do not decide which observation would improve a decision.
The requested outcome is a governed information-acquisition loop, eventually
allowing named global retrieval indexes to be retired when evidence permits.

The first implementation must be useful without training a ranker or silently
changing the historical authority, episode, memory, reconciliation, benchmark,
or prospective-collection contracts. Synthetic correctness is not deployment
readiness or evidence that indexes can be removed.

## Decision record before implementation

Decision ID: LC-ACTIVE-20260918-01. Authority: Codex within the owner's local
implementation request. Candidates assessed before authoring:

| Candidate | Success evidence | Constraint exposure | Cost | Information | Reversibility |
| --- | --- | --- | --- | --- | --- |
| Finite explicit model in an isolated lab | Small cases have an independent exhaustive oracle | Model assumptions must stay visible | Bounded search; actual runtime unknown | Tests sequential observation value | Additive package, no active host changes |
| Learned ranker in a new epoch | No trained model currently exists | Old failed epoch cannot supply valid supervision | Collection/training cost unknown | Tests learned ranking, not necessarily observation choice | Training can stop, but needs separate corpus custody |

Governance result: RECORD. This records a choice context, not a claim that one
candidate is empirically superior. Codex selects the finite explicit model for
the first local implementation because its correctness is directly falsifiable
without training. The learner route remains outside this tranche.

## Boundary and contract

Add `latent_compass.lab`, with its own explicit `1.0.0` experimental contracts
and `python -m latent_compass.lab` entry point. Keep the legacy CLI and every
legacy contract/version/seal unchanged. All lab outputs say that they are
experimental, have no execution authority, and make no empirical claim.

The lab can observe local files, update its own isolated diagnostic state and
propose an observation. It never launches a model, tool, agent, shell command,
network request, provider refresh, host reconfiguration, promotion or deletion.
The external authorized host remains the router and executor. Source contents
are untrusted data, never policy. Existing authority capabilities are unchanged.

## Finite decision model

An operator supplies a finite set of possible worlds, positive integer prior
weights, terminal decisions with complete nonnegative integer loss tables, and
probes with nonnegative integer acquisition costs and a total outcome table
over worlds. World tables represent joint possibilities, so correlated and
complementary probes require no independence assumption. General continuous
worlds, learned probabilities and automatic semantic hypothesis generation are
outside this version.

Use exact rational arithmetic for posterior weights and expected loss. All
costs and losses have one declared policy unit; time/money conversion belongs
to that explicit policy, not a hidden constant. Every model and state is sealed
with its own domain. Unsupported versions, unknown fields, duplicate IDs,
missing table cells, invalid references and impossible observations fail closed.

R(E) is the minimum expected terminal loss among admissible decisions. A bounded
Bellman search compares stopping with each remaining affordable probe:

`V(E, b, h) = min(R(E), min_o(cost(o) + sum_y P(y|E,o) V(E+y,b-cost(o),h-1)))`.

Previously acquired probes cannot be acquired twice. Input limits, horizon,
expanded-state budget and deterministic tie-breaking bound the calculation.
Search exhaustion must be explicit, never labelled optimal. Exhaustive
optimality is only relative to this supplied finite model, budget and horizon.
Include a required abstention alternative. Terminal decisions can require
specific observation/outcome pairs, not merely the fact a probe ran; missing or
failed mandatory evidence cannot be bought away by a low expected loss. The
result is a lab recommendation, not authorization or independently signed proof.
The entire observation episode binds one immutable source-scope digest. Source
drift requires a new episode, even when every individual observation has a
well-formed digest. Unknown outcomes and a zero-mass posterior fail outside the
model; they must not be converted into one of its terminal decisions.

## Observation and justification boundary

Source observations identify a confined root and relative path, exact content
digest, bounded location/coverage and truncation. They are revalidated before
reuse. A digest identifies bytes, not semantic truth; missing source is not
evidence of absence. Never follow links/reparse points outside the root or read
unbounded files. Read-only APIs must not start index providers or subprocesses.
Caller-supplied HEAD is declared provenance, not independently verified Git
identity; external experiment receipts must bind actual Git state separately.

Keep diagnostic justification history separate from strategic decision stores.
Bind it to one host/family and source scope. Support alternatives are disjunctions
of conjunctions. Compute grounded applicability from explicit valid roots;
cycles cannot justify themselves. Revocation, expiry, source drift and contrary
observations cause a new revision and dependent applicability recomputation.
Alternative supports can keep a conclusion applicable. Applicability is not a
truth verdict. No silent rewrite, cross-family merge or proof by absent record.

## Lab routing, evaluation and retirement preparation

Lab routing checks declared capabilities, budget and scope, then returns an
advice/refusal envelope. It performs no dispatch. A model/provider absent or an
incompatible/stale observation produces UNKNOWN/ABSTAIN or ESCALATE as appropriate.
The host's own authority and independently observed capabilities remain required.

Evaluation tooling records paired outcomes, costs and missingness against a
sealed protocol. It must not fabricate real trials or reuse the old holdout.
Source-only, source-plus-controller and existing-index conditions isolate effects;
failures remain in denominators. Inference without adequate design/precision
remains descriptive and cannot produce replacement readiness.

Migration tooling produces a read-only inventory/dry-run and explicit unmet
conditions. It never changes hooks, services, providers, data or stores. A
declaration such as `approved: true` is not evidence of an approval. No lab
artifact can grant a live transition. HOK-247, HOK-737 and HOK-740 retain their
own gates; real pilot and retirement require separately verified evidence.

## Initial scope and inventory evidence

The first development/pilot source is the isolated personal Latent Compass
worktree. Python 3.13 and frozen uv dependencies are available. The existing
index-control-plane `doctor` reports controller 2.5.0, Graphify configured,
CCC not configured and Semctx not configured for this worktree. These booleans
do not attest freshness, consumption, processes, usefulness or safe removal.
No provider was started or retired by this check. Global/historical consumers,
other repositories and professional environments are unassessed and excluded
from any removal claim. The inventory interface must expose these unknowns.

## Verification and non-claims

Use independent enumeration for finite decisions, an XOR complementarity case,
mandatory-evidence cases, impossible outcomes, resource exhaustion, stale source,
path escape, cycles/alternative supports, tampered history, cross-host refusal,
and advisor refusal without external mutation. Preserve the entire existing
test suite and required format/lint/type/build gates.

Implementation and local proof may finish before HOK-804's real comparison,
HOK-805's live pilot and HOK-806's removal. Those outcomes remain open until their
observations exist. Do not transform the absence of empirical evidence into a
software gate success or an unearned Done status.

## Amendment 2026-09-19 — operational scope (HOK-799)

The decision above is unchanged, and a test pins its text. The scope it left
open is now fixed in
[`docs/active-diagnosis-scope.md`](../active-diagnosis-scope.md): one personal
workstation, this repository and its worktrees as the only `PERSONAL_LAB`
perimeter, and the professional environment, the vault and every other
personal repository excluded. Where the decision spoke of one isolated
worktree, the perimeter is named here as the repository's own worktrees,
plural; no other repository joins it. The read-only inventory it rests on is
sealed under `evidence/hok799-scope-inventory/`, superseded without being
edited by the corrected `evidence/hok799-scope-inventory-v1.1.0/`, and its
unknowns each carry an owner.

Two consequences bind the later tranches. The pilot can produce evidence about
the Graphify code graph only, because CCC and Semctx are not configured for
it; a CCC verdict needs a second named pilot repository. And the `1.0.0` lab
binding does not bind tool versions, so every HOK-800 adapter must record the
identity of the tool that produced its observation.

## Amendment 2026-09-19 — unobtainable probes (HOK-802)

The decision above is unchanged, and a test pins its text. Its recursion ranges
over every probe not yet acquired and affordable, and the `1.0.0` contract gives
a host no way to say that one of them cannot be obtained. The HOK-803 bench
showed the cost: a failed attempt leaves the state untouched, the same probe is
named again, and the loop can only stop — on the bench model at 15/4 while
another probe was still worth 7/2.

On 2026-09-19 the repository owner decided to add that input as a new contract
version beside `1.0.0`, never in place of it. `propose_excluding` solves the
same exact recursion with the inner minimum restricted, at every node, to
probes outside a host-declared set. Its result is a separate `1.1.0` document,
the constrained plan report, which names that set. Models, states and the
unrestricted plan report stay `1.0.0`, their output is unchanged byte for byte,
and neither loader accepts the other's version.

Two limits bind it. That a probe is unobtainable is the host's declaration: the
lab checks that the id names a probe of the model, not that the tool is absent,
and the record says so. And the restricted plan is exact for the restricted
problem only; it may be worse than the unrestricted one when the declaration
was wrong. The lab still launches nothing and gains no authority.

## Amendment 2026-09-19 — a second pilot repository (HOK-799, U1)

The decision above is unchanged, and a test pins its text. The scope amendment
said that no other repository joins the pilot, and that a CCC verdict needs a
second named pilot repository — an owner decision, recorded as unknown U1.

On 2026-09-19 the repository owner took it: one second personal repository,
with CCC provisioned, joins the pilot. It is named to the operator and travels
here under the alias `pilot-second-repository`; the alias-to-path mapping is
operator-held, like every other private detail. Its index artefacts were read
the same day, without starting, refreshing or reconfiguring anything, and are
sealed as five more assets in `evidence/hok799-scope-inventory-v1.2.0/`, beside
the two earlier revisions, which are unchanged.

What the decision moves, and what it does not. Three index/usage couples of
that repository — its CCC index and its two Graphify artefacts — become
candidates: `DISABLE_LATER` with every gate missing, never a permission. Its
Semctx layer and its symbolic-tool cache are in the perimeter too and stay
kept, by their classification alone: joining a pilot removes the scope lock.
Every other personal repository stays unassessed, the professional environment
and the vault stay excluded, and the workstation-wide CCC machinery stays
outside any pilot. Nothing was activated, disabled, deleted or measured.

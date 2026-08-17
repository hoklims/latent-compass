# HOK-225 pairwise corpus readiness

- **Assessment date**: 2026-08-16
- **Decision**: `REFUSED`
- **Scope**: current TRAIN and VALIDATION contracts, before holdout

## Result

The current corpus cannot produce an honest pairwise supervision bundle. No
labels are generated, no ranker work is unlocked and no holdout input is needed
for this refusal.

HOK-234 now defines and validates a separate judgeable pre-action sidecar. That
removes the schema-design blocker for future captures, but it does not alter
this assessment: the current synthetic episodes have no such sidecars captured
before their decisions. They remain `UNJUDGEABLE`; the contract cannot create
historical evidence or make a backfill honest.

This is a successful fail-closed result for HOK-225: its stop condition requires
`REFUSED` when judge independence, holdout isolation, coverage or data
sufficiency cannot be demonstrated.

## Evidence available to a judge

The current `Candidate` contract exposes exactly three fields:

| Field | Meaning | Why it cannot establish quality |
| --- | --- | --- |
| `direction_id` | opaque candidate identity | lexical order is a tie-break, not a preference |
| `propensity` | probability under the historical logging policy | using it would reproduce selection behaviour, not judge quality |
| `prior_uncertainty` | uncertainty recorded before selection | uncertainty alone does not determine success, violations, cost, information or reversibility |

The hierarchical state contains names, node identifiers and summary digests.
The digests preserve identity but reveal no semantic context to an independent
judge. Cost, information gain, reversibility and uncertainty are recorded once
for the episode, after one branch was selected; they are not candidate-specific
pre-action descriptions.

The advisory rationale, selected direction, observed outcome, external verdict
and episode economics are forbidden judge inputs. Reusing them would leak the
historical decision or attach the selected branch's evidence to alternatives
that were never executed.

## Hostile alternatives considered

| Proposed shortcut | Decision | Reason |
| --- | --- | --- |
| Prefer the canonical `direction_id` | refused | deterministic but semantically arbitrary |
| Prefer the highest logging propensity | refused | copies the logging policy and its selection bias |
| Prefer the lowest prior uncertainty | refused | a baseline heuristic, not a vector judgment |
| Copy the selected action's outcome to every pair | refused | invents counterfactual outcomes |
| Use episode-level economics for both candidates | refused | erases candidate-specific differences |
| Put a rule-based judge in this repository | synthetic-only | replayable, but authored by the same control plane and not independent claim-bearing supervision |

## Independence boundary

For claim-bearing HOK-190 supervision, `pairwise_labeler` must be a distinct
non-authority actor. It must not reuse `ExternalVerdict`, because that actor
participates in the authority boundary. The labeler runs outside the ranker
implementation and receives only a sealed allowed pre-action projection.

The repository may export that projection and import an immutable label bundle.
It must not call the labeler. The label bundle must bind the producer, judge
specification, canonical input, rubric and canonicalization versions. An
external trust and chronology anchor is required for claims of producer
independence or registration time; local digests prove consistency only.

A same-repository rule may later be introduced as a clearly named synthetic
oracle for pipeline tests. Such an oracle cannot satisfy this gate or unblock
HOK-190.

## Remaining prerequisites

HOK-235 now provisions a named, keyless external labeler and demonstrates one
signed, Rekor-witnessed label bundle under an exact workflow identity and SHA.
That closes the labeler-provisioning prerequisite, not the supervision-data
gate. Two prerequisites still prevent HOK-190:

1. **Real judgeable pre-action capture.** The HOK-234 contract exists, but a
   producer still needs to capture its immutable candidate descriptions,
   parameters, applicable constraints and candidate-specific evidence before a
   real decision. No such capture exists in the current corpus.
2. **Preference-producing semantic rubric and sufficient labels.** The frozen
   external rubric v1 emits only honest `TIE` or `ABSTAIN` results. It refuses to
   turn unstructured semantic differences into invented left/right winners, so
   it cannot yet supply ranker supervision. A future rubric version must be
   independently reviewed and then satisfy the pre-registered power, coverage,
   graph, abstention and support gates.

Only after both are present may a new pre-registered supervision plan schedule
pairs and perform those gates. Sample-size analysis is not reached today: real
pre-action semantic supervision is still absent.

## Claim boundary

`REFUSED` means the current data cannot support pairwise label collection. It
does not mean a ranker failed, because no ranker was trained. It does not assess
the final holdout, demonstrate model value, or grant any authority.

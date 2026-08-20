# Pairwise supervision contract

This document defines the evidence required before HOK-190 may train a pairwise
ranker. It is a data and evaluation contract, not a ranker implementation and
not an authorization boundary.

## Three objects that must remain separate

| Object | Meaning | Permitted claim |
| --- | --- | --- |
| `J(e, a, b)` | an independent judge's preference between candidates `a` and `b` in episode `e` | agreement with a sealed rubric |
| `s(e, a, b)` | a ranker's uncalibrated preference score learned from `J` | discrimination on explicit judge labels |
| `Y(e, a, b)` | a separately defined observed outcome | calibration or outcome effect, subject to its experimental assumptions |

A judge preference is not an observed outcome. A model that predicts `J`
accurately is not therefore calibrated for `Y`. Calibration must compare
predictions with events that actually materialize, using a proper scoring rule
such as Brier score or log loss and an uncertainty interval.

## `PAIRWISE_SUPERVISION_CONTRACT_V1`

### Unit and pair

A supervision record contains exactly two distinct candidates from one episode
at one pre-action decision point. Candidate order is canonical and recorded;
swapping the display order cannot change the semantic result. The independent
unit for splitting and uncertainty is the episode, never an individual pair
derived from that episode.

The result is:

```text
relation = LEFT_WINS | RIGHT_WINS | TIE | ABSTAIN
dimensions = {
  success, violation, cost, information, reversibility
}
```

Each dimension carries the same four-way relation. The aggregate relation is
computed by the sealed rubric; it is not an opaque reward written by the data
producer. `ABSTAIN` means the allowed evidence is insufficient. It must not be
silently converted to a tie or a loss.

### Independent deterministic judge

`JudgeSpec` is frozen before label generation and contains:

- judge identifier and version;
- canonical input schema and the exact allowed pre-action projection;
- comparison dimensions, aggregation, tie and abstention rules;
- a deterministic schedule of episode/candidate pairs and seed, if a seed is
  needed;
- a protocol digest and canonicalization version.

The judge may see only the sealed pre-action context and the two candidate
descriptions. It must not receive the historical selected action, ranker score
or prediction, logging propensity, prior preference, observed outcome, external
verdict, split name, or any holdout content or derivative.

The same `JudgeSpec` and canonical input must yield byte-identical canonical
output. Every record binds the episode and candidate identifiers, input digest,
judge identifier/version, specification digest, dimension results and aggregate
result. These local digests detect inconsistent replay; they do not attest who
ran the judge or when.

### Split discipline

- `TRAIN_J` trains the ranker from explicit `J` labels.
- `VALIDATION_J` selects and evaluates preference models without refitting on
  its labels.
- `CALIB_Y` may fit a calibration map only after the ranker is frozen.
- `AUDIT_Y` measures calibration and reliability without further fitting.
- All sets are disjoint by episode identifier.
- Holdout inputs, labels, outcomes, seals derived from content, scores and model
  choices are inaccessible to this loop. Holdout remains a one-shot final gate.

Observed historical outcomes for only the selected action do not create a
counterfactual `Y(e, a, b)`. A downstream outcome claim requires paired or
randomized execution, or an explicitly observational estimand with recorded
propensities, common-support diagnostics and declared weighting. Existing OPE
metrics can support that observational claim; they do not calibrate the judge.

## `DATA_SUFFICIENCY_GATE_V1`

There is no universal sufficient number of pairs. Before any label is read, a
sealed supervision plan must declare:

1. the estimand, endpoint, comparator and episode-level independent unit;
2. the minimum effect worth acting on, significance level, target power,
   multiplicity handling and expected event rate;
3. allocation, repeated-pair dependency and clustering assumptions;
4. required coverage by candidate, decision class and declared stratum;
5. maximum acceptable abstention and tie rates, plus the adjudication rule;
6. a reproducible power calculation or simulation that yields the required
   number of independent episodes;
7. the comparison-graph connectivity and identifiability check;
8. for propensity weighting, positivity/support limits, weight concentration
   diagnostics and episode-level effective sample size;
9. the episode-level uncertainty method for discrimination and calibration.

The gate is `PASS` only when the plan predates labels and every declared
requirement is met. Effective sample size is a diagnostic, not a substitute for
power, coverage or graph identifiability. For an unpenalized Bradley–Terry fit,
the directed win graph must satisfy the declared strong-connectivity condition;
otherwise finite estimation is refused.

Any absent declaration, retrospective threshold, missing stratum, unsupported
comparison, insufficient independent episode count, excessive abstention, or
failed graph/support check yields `REFUSED`. No model comparison begins.

## Accepted and refused records

Accepted example:

- one TRAIN episode supplies two actual candidates from the same pre-action
  state;
- their order follows the canonical rule;
- the judge receives only the allowed projection;
- the vector decision and aggregate relation reproduce under the sealed judge
  version;
- the record belongs to a plan frozen before labels were generated.

Refused examples:

- either candidate or any input comes from holdout;
- candidates come from different episodes or decision times;
- the judge sees the historical selection, an outcome, ranker output, external
  verdict or split name;
- the producer supplies only a scalar reward or invents the unobserved
  candidate's outcome;
- judge identity, version, input digest or specification digest is absent;
- pairs, strata or thresholds are changed after labels are observed;
- replay changes canonical output for an identical sealed input.

## Status of the current corpus

The synthetic corpus contains zero eligible explicit pairwise judgments, zero
independent judge specifications and zero counterfactual pair outcomes. The
data gate therefore returns **`DISCOVERY_REQUIRED`** before sample size is even
considered. The corpus may continue to demonstrate the HOK-188 pipeline, but it
cannot train, calibrate or validate a HOK-190 pairwise ranker.

## Claim boundary

This contract creates no authority, attestation, live safety, calibration or
performance claim. A passing pairwise data gate would permit ranker work to
start; it would not prove the resulting ranker is useful. The final holdout and
the external evidence provenance boundary remain unchanged.

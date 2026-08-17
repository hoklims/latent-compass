# ADR 0004 — Independent pairwise supervision before holdout

- **Status**: accepted
- **Date**: 2026-08-16
- **Scope**: HOK-224 and the data prerequisite for HOK-190
- **Decision owner**: project owner, recorded in HOK-224

## Context

HOK-190 needs a training signal before it can implement or compare a pairwise
ranker. The current synthetic corpus records the action historically selected
for each episode and, sometimes, its stipulated outcome. It records no explicit
comparison between two candidates, no counterfactual outcome for the candidate
that was not selected, and no independent judge. Turning those fields into
pairwise labels would invent evidence.

A judge preference and an observed outcome are different objects. Agreement
with a judge can establish reproduction of that judge's rubric. It cannot, by
itself, establish an improved outcome or a calibrated probability of one.

## Decision

Pairwise training labels will be produced before holdout by an independent,
deterministic judge governed by the contract in
[`docs/pairwise-supervision.md`](../pairwise-supervision.md).

The judge compares exactly two candidates from the same pre-action episode. Its
sealed specification fixes the allowed input projection, comparison dimensions,
tie and abstention rules, pair schedule and canonical ordering. The judge cannot
see a ranker score, the historical selection, an observed outcome, an external
verdict, a split name, or holdout material. Replaying the same sealed input with
the same judge specification must produce the same canonical bytes.

The resulting preference label `J` is training supervision only. A ranker score
trained on `J` remains an uncalibrated preference score. Calibration against an
outcome `Y` is a separate, later claim: the frozen ranker is calibrated on
observed outcomes in a disjoint calibration set and audited without refitting on
another disjoint set. If the data are observational rather than randomized or
paired, propensity, support and weighting assumptions must be declared and
diagnosed. Holdout remains closed throughout training, calibration and model
selection.

Data sufficiency has no universal row-count threshold. Before labels are
generated, a sealed supervision plan must declare the estimand, independent
unit, minimum operationally useful effect, error rate, power, multiplicity,
allocation and dependency assumptions, strata, coverage requirements and a
reproducible power or simulation method. Its realized data must also satisfy
the comparison-graph and support diagnostics in the contract. Missing plans,
post-hoc thresholds, uncovered strata, an unidentified comparison graph or
insufficient independent episodes fail closed.

## Current verdict

**`DISCOVERY_REQUIRED`.** The current corpus has zero eligible explicit
pairwise judgments and no independent judge. Its six TRAIN episodes and sixteen
VALIDATION episodes cannot satisfy a gate that has not yet been pre-registered,
and their single-action outcomes do not supply counterfactual pair outcomes.
No ranker implementation, calibration result, holdout access or value claim is
authorized by this ADR.

## Consequences

- The next tranche must freeze a judge specification and supervision plan,
  then collect a pre-holdout pairwise corpus.
- Ranker implementation remains blocked until the data gate passes.
- Judge agreement, preference discrimination and observed-outcome calibration
  remain three separately named claims.
- Local digests can prove internal consistency, not provenance, independence or
  registration time. Any stronger claim needs an external trust or chronology
  anchor, as described by ADR 0003.
- Choosing a statistical or machine-learning dependency remains a separate ADR
  and licence review; this decision adds none.

## References

- Bradley and Terry, *Rank Analysis of Incomplete Block Designs* (1952),
  <https://doi.org/10.1093/biomet/39.3-4.324>
- Ford, *Solution of a Ranking Problem from Binary Comparisons* (1957),
  <https://doi.org/10.1080/00029890.1957.11989117>
- Gneiting and Raftery, *Strictly Proper Scoring Rules, Prediction, and
  Estimation* (2007), <https://doi.org/10.1198/016214506000001437>
- Guo et al., *On Calibration of Modern Neural Networks* (2017),
  <https://proceedings.mlr.press/v70/guo17a.html>


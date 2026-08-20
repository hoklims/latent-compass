# ADR 0007 — Preserve cost units and align labeler trust evidence

## Context

The benchmark 1.0.0 `TAIL` metric took a nearest-rank quantile of
`importance_weight * cost`. That changes the outcome unit and is not an
off-policy quantile estimator. Separately, the terminal discovery evidence
linked workflow SHA `75af31a8aee941469fe088891cb090747ec0ce89` while the public
labeler trust policy still named the earlier accepted SHA.

Both defects can survive the previous green suite: generic quantile tests do not
prove where importance weights enter the distribution, and prose-presence tests
do not reconcile a trust policy with a claim-bearing evidence record.

## Decision

Benchmark contract 1.1.0 defines `TAIL` as a self-normalized
importance-weighted empirical quantile. Positive importance weights contribute
probability mass to observed costs; they never scale the costs. The returned
value is always one observed cost and therefore preserves its unit.

The labeler trust policy is rotated to the workflow SHA already bound by the
terminal evidence, after independently verifying the downloaded Sigstore bundle
and its certificate extensions. A regression test requires the accepted SHA in
the public trust document to equal the evidence record's workflow SHA.

The README classifies the private labeler as externally evidenced rather than as
an implementation reproducible inside this repository.

## Alternatives refused

- Keep `quantile(weight * cost)` and rename it: refused because the value already
  feeds a decision-bearing threshold and has no cost-quantile interpretation.
- Interpolate between costs: refused because the report requires every value to
  remain traceable to an observed case.
- Rewrite the terminal decision to the earlier workflow SHA: refused because it
  would falsify the immutable run identity recorded by GitHub and Sigstore.
- Treat a successful signature as proof of useful supervision: refused; the
  verified bundle remains an `ABSTAIN`.

## Consequences

- Benchmark 1.0.0 payloads are no longer accepted by this build; corpus, spec,
  report and holdout-plan artifacts are regenerated at 1.1.0.
- Uniformly scaling importance weights cannot change `TAIL`.
- The labeler source remains private and its signed output remains
  non-authoritative.

# ADR 0005 — Judgeable pre-action data lives in a separate sidecar

- **Status**: accepted
- **Date**: 2026-08-16
- **Scope**: HOK-234
- **Decision owner**: project owner, approved in-session after the HOK-234 design gate

## Context

Episode contract `1.0.0` deliberately records candidates as an identifier,
logging propensity and prior uncertainty. That is enough for HOK-188 baselines
but not enough for an independent labeler to compare candidate meaning. Adding
semantic fields to that version would change its persisted shape and seals.
Adding them to the baseline public view would also expand what every existing
baseline can inspect.

The HOK-225 assessment therefore refused label generation. HOK-234 must create
judgeable pre-action evidence without backfilling old episodes, importing
post-action facts or weakening the existing benchmark boundary.

## Decision

Introduce a separate, self-contained and immutable
`JUDGEABLE_DECISION_PROJECTION_V1` sidecar. Keep `Episode 1.0.0` and
`PublicCandidateView` unchanged. Historical episodes remain `UNJUDGEABLE` unless
a real sidecar was captured before their decision; no migration may invent one.

The sidecar binds:

- its contract and canonicalization versions;
- a decision-point identity allocated before action;
- capture time and producer identity;
- one bounded sanitized objective summary;
- typed, bounded context facts and constraints;
- all candidates in canonical `direction_id` order.

Each candidate binds its identifier, a bounded sanitized semantic summary,
typed parameters, applicable constraint identifiers and one explicit evidence
entry for each comparison dimension: success, violation, cost, information and
reversibility. A dimension is either supported by bounded factual statements
with source digests, or explicitly `NOT_JUDGEABLE` with a reason. Missing a
dimension is invalid; absence is never silently converted to neutral evidence.

A claim-bearing pair input is derived from one sidecar, never trusted as a
standalone authored object. It contains
the shared context and exactly two candidates ordered canonically, and binds the
source projection seal. Swapping requested candidates therefore produces the
same canonical pair. Projection and pair use separate seal domains. Standalone
loading validates structure only; provenance-sensitive use requires the source
projection preimage and exact canonical re-derivation.

The contract and pair input exclude logging propensity, prior preference or
score, advisory/ranker output, selected direction, outcome, external verdict,
episode economics, split name and all holdout metadata. The implementation has
no corpus or holdout file input.

## Data minimisation and truth boundary

Free text is limited to bounded sanitized summaries and factual statements;
raw prompts, source files, credentials and unrestricted attachments are not
accepted by this contract. Parameters use a closed scalar representation rather
than arbitrary JSON. Identifiers and source digests are references, not source
content.

Structural validation and local seals prove canonical consistency only. They do
not prove that text was sanitized, that capture happened before action, that a
producer is independent or that a source statement is true. Those claims need
the external labeler and chronology boundary tracked by HOK-235.

## Compatibility

- Episode and advisory contract versions do not change.
- The HOK-188 public case and candidate projections do not change.
- Existing corpus payloads, manifests, baseline outputs and episode seals remain
  byte-compatible.
- The sidecar has its own version authority and fails closed on unknown fields,
  coercion, non-finite numbers, missing dimensions, dangling constraint
  references, scalar magnitudes outside the inclusive range -1e12 to 1e12,
  duplicate identifiers, non-canonical candidate order and
  post-action field injection.

## Non-goals

This decision does not collect real labels, call or provision a labeler, import
signed bundles, train a ranker, calibrate a probability, read holdout data or
make a performance or authority claim.

## Acceptance

The tranche is acceptable when contract tests prove deterministic sealing,
canonical pair derivation, explicit not-judgeable dimensions, strict scalar and
size bounds, v1 episode compatibility, unchanged baseline projection and hostile
rejection of every forbidden post-action field.

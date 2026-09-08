# ADR 0010 — Prospective evidence is preregistered before collection

- **Status**: accepted
- **Date**: 2026-08-17
- **Scope**: HOK-252
- **Decision owner**: project owner

## Context

HOK-243 preserves what was known before a strategic decision. HOK-244 records
what was observed afterward. Joining records that already exist would be a
retrospective sample: outcome availability could silently decide which cases
enter the denominator. That cannot establish an honest measurement corpus.

## Decision

Add a fourth, physically separate journal for prospective shadow collection.
Before collection starts, an operator supplies every power input, the exact
HOK-243 and HOK-244 source binding, the calendar window, population, strata,
hard exclusions, independence identities and deterministic stop priority. The
sealed plan admits no existing or backfilled cases. `POWER_NOT_ESTABLISHED`
remains explicit until caller inputs produce a sufficient exact-binomial plan;
that draft cannot be admitted, sealed or started.

Collection is append-only. Every qualifying decision stays in the denominator.
Cancellation, abstention and loss to follow-up are terminal exclusions, not
deletions. A reconciliation is eligible only when it is the current verified
tail of the bound HOK-244 journal, matches the exact HOK-243 preimage and passes
the declared syntactic producer-independence check.

There are no outcome-dependent interim looks. Sufficient closure requires the
preregistered enrollment target, the minimum calendar end and terminal state
for every enrolled case. Otherwise the journal closes insufficient only at the
hard calendar end. Integrity or security failure is the only abort reason.

The operator CLI lives under `latent-compass shadow`. Its exact command set is
`power`, `validate-plan`, `init`, `start`, `enroll`, `reconcile`, `terminal`,
`status`, `close`, `abort`, `verify`, `manifest`, `report` and `limits`. It
admits JSON through the same core contracts and confines new manifest/report
files beneath the named journal root without overwrite. `abort` maps only the
closed choices `integrity-failure` and `security-failure` to the corresponding
core terminal reasons. It adds no operational control surface.

## Evidence and limits

This infrastructure can be described as **OFFLINE_VERIFIED**: tests establish
the local state machine, exact planner, refusal behavior and deterministic
artifacts. It has not completed a real elapsed collection. That remains the
external blocker before any empirical corpus claim.

Journal and artifact seals prove local consistency only. They do not prove
producer identity, observation truth, chronology outside the recorded contract,
causal effect or benefit to an agent. The collection never reads evaluation
holdout data and creates no routing, authority or training surface.

The decision source binding is declarative equality, not a decision-store
membership proof. Enrollment accepts the supplied sealed record without opening
that source store, and its capture time is declared rather than witnessed.
Prospective empirical acceptance therefore also requires operator evidence of
native capture and real chronology outside this package.

## Consequences

- Prospective evidence cannot be manufactured from the existing journals.
- Missingness and selection imbalance remain visible in the all-case manifest.
- An insufficient close is an honest terminal result, not a failed command.
- Operators must wait for the real calendar and collect new qualifying cases.

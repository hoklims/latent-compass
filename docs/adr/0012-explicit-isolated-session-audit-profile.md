# ADR 0012 - Explicit audit profile for an isolated session on one account

- **Status**: accepted design; implementation admission requires external N-1 review
- **Date**: 2026-10-06
- **Scope**: public independent-audit protocol, schema 4
- **Supersedes**: the assumption that every admitted review must use a distinct account

## Context

Schema 3 required eight true independence declarations, including a distinct
account. A fresh read-only reviewer on the operator's current account cannot
honestly declare account separation. Green source tests or a fresh model session
do not satisfy that declaration. The owner explicitly selected an isolated
same-account review instead of obtaining another account.

The change must state which guarantee is being chosen. It must not manufacture
cross-account independence, silently choose a weaker profile from receipt
contents, or replace behavioral proof with reviewer agreement.

## Decision

Schema 4 has two externally selected profiles. `separate-account` remains the
default and retains all eight true declarations. `isolated-session` requires
an explicit operator choice, a matching receipt profile, and a false
`distinct_account` declaration. It makes no claim of a separate account, host,
harness or evidence store.

The isolated reviewer must be a non-author, use a fresh non-forked session with
an observed read-only sandbox, read source before the author's narrative, and
then review the final candidate and raw evidence. Active persistent memory or
write tools refuse. Unavailable attestations remain null and appear as named
limits; they are never converted to false. Admission always reports
`PROOF_ADEQUATE_WITH_LIMITS` and `SAME_ACCOUNT_ISOLATED_REVIEW` for this profile.

Both profiles retain identical Git-object and policy bindings, complete epoch
inventory, claim and invocation requirements, mutation-byte validation, actual
nonzero red detection, pristine zero green replay, and blocker/verdict checks.
Old schema 3 epochs and receipts remain historical; no conversion or relabeling
can admit them under schema 4.

This changed public gate is a subject of review. Its own result cannot admit
its changed mechanism. Admission requires a fresh independent aggregate audit,
sensitive negative witnesses and an unchanged trusted external N-1 evaluator.

## Consequences

One account can operate a bounded source review without claiming stronger
organizational independence. The operator remains responsible for the supplied
reviewer metadata: schema validation is not authentication of an account or a
runtime. Unknown attestations and the same-account limit remain visible.

This profile grants no hook execution, pilot activation, index removal,
publication permission, empirical value or correctness outside the stated
checks. Those operations retain their own authority and evidence requirements.

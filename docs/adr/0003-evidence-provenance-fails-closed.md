# ADR 0003 — Evidence provenance fails closed

- **Status**: accepted
- **Date**: 2026-08-16
- **Amends**: ADR 0002
- **Scope**: authority boundary and HOK-188

## Context

ADR 0002 made evidence reproducible: the authority reloads contracts, verifies
their seals and scores raw measurements again. That proves internal consistency,
not origin. HOK-188 exposed the distinction because its diagnostic wrappers can
be removed and their raw values reconstructed as ordinary protocol evidence.
Every remaining seal is then caller-computable.

## Decision

`OFFLINE_VERIFIED`, `CANARY_ELIGIBLE` and `PROMOTED` require a trusted external
attestation. The current package has no external trust root and therefore
refuses those transitions as `untrusted_evidence` before inspecting evidence or
touching a holdout ledger. `DEFINE -> SHADOW` and every transition to `REJECTED`
remain available.

Because this changes the public authority semantics and boundary snapshot, the
authority contract advances to `2.0.0`. Advisory payloads at `1.0.0` remain in
the explicitly supported read set; there is no implicit payload migration.

No marker, subclass, local registry, hash domain or verifier supplied per call
is treated as provenance. A future positive path must bind at least the protocol,
measurement, verdict and corpus seals, epoch, split, purpose, issuer and allowed
target, and must verify against a trust root configured outside the payload.

## Consequences

The historical function and CLI are deliberately fail-closed for evidence-based
advancement. This breaks earlier positive examples, but closes the benchmark
laundering path without claiming cryptographic guarantees the repository does
not implement. Restoring a positive path requires a separately reviewed
composition-root verifier and external asymmetric attestation.

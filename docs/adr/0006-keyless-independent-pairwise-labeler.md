# ADR 0006 — Keyless independent pairwise labeler

- **Status**: accepted
- **Date**: 2026-08-17
- **Scope**: HOK-235 and the external prerequisite for HOK-190
- **Decision owner**: project owner, delegated to the Codex project lead on 2026-08-17

## Context

HOK-234 provides a bounded pre-action projection and canonical two-candidate
judge input. Those local seals make replay deterministic, but they do not prove
who produced a label or when. A label, verifier, key or trust policy supplied by
the same Python caller would be caller-controlled provenance and would not
satisfy the boundary fixed by ADR 0003.

The current candidate evidence is intentionally semantic and sanitized. It does
not contain a general machine-comparable score for success, violation, cost,
information or reversibility. A labeler must therefore abstain rather than
invent a preference from lexical order, fact count, logging propensity or
another convenient proxy.

## Decision

The claim-bearing pairwise labeler is the GitHub Actions workflow at
`hoklims/latent-compass-labeler/.github/workflows/label.yml` on `main`, in a
dedicated private repository. It runs only on GitHub-hosted runners and has no
authority capability in Latent Compass.

Each canonical label document is signed keylessly with Sigstore. Fulcio binds
the ephemeral signing key to the exact GitHub Actions OIDC workflow identity;
the Sigstore bundle carries the certificate, signature, signing-time evidence
and Rekor transparency-log inclusion material. No long-lived private signing
key exists in either repository.

Verification expectations are configured out of band from the label and from
the request being verified. They pin at least:

- OIDC issuer `https://token.actions.githubusercontent.com`;
- certificate identity for the exact repository, workflow path and `main` ref;
- GitHub workflow repository, ref, trigger and accepted commit SHA;
- judge, rubric, canonicalization and label-contract versions;
- allowed purpose and evidence class.

The initial rubric is deliberately conservative. For a dimension, it emits
`TIE` only when both canonical dimension-evidence payloads are identical. Every
other case emits `ABSTAIN`; it never emits a left or right winner. The aggregate
is `TIE` only when all five dimensions tie, otherwise `ABSTAIN`. This proves the
external signing and import boundary without manufacturing supervision from an
under-specified semantic comparison. A future preference-producing rubric is a
new version and authority decision, not an edit to this rubric.

The private repository is an operational boundary, not a claim that the project
owner is independent from themself. The supported claim is narrower: the
ranker process, benchmark payload and ordinary import caller cannot mint a
label accepted under the pinned workflow identity and external Sigstore roots.

## Signed-object boundary

The signed object is the canonical label document. The Sigstore verification
bundle is a sidecar and is not included recursively in the signed document.
The label binds the source projection and pair input seals, decision and
candidate identifiers, judge specification, rubric, canonicalization, purpose,
invocation and all dimension plus aggregate relations.

Structural loading and semantic binding inside this package do not establish
provenance. Claim-bearing use additionally requires successful Sigstore
verification against the fixed composition-root policy. Supplying a different
policy in the same call cannot widen trust.

## Refused alternatives

- **Long-lived repository key** — refused because rotation, custody and secret
  exfiltration would become project obligations without improving identity.
- **Signer or verifier inside this repository** — refused because the same
  control plane could manufacture the evidence and its acceptance path.
- **Callback, public key or trust policy supplied per request** — refused as a
  caller-replaceable root of trust.
- **Unsigned local registry or digest domain** — refused because local hashes
  prove consistency, not origin or chronology.
- **Human labels without an externally witnessed signature** — refused for
  claim-bearing use because identity and registration time would remain
  unverified.
- **Lexical, propensity, uncertainty or fact-count preference** — refused as
  deterministic but semantically unsupported supervision.
- **Self-hosted runner** — refused for the first version because it adds a
  persistent execution environment and secret boundary that are unnecessary
  for a keyless workflow.

## Consequences

- HOK-235 can be closed only after the private workflow is published, its exact
  revision is pinned, a real Sigstore/Rekor bundle is produced and hostile
  identity or payload substitutions are refused.
- The initial conservative rubric may legitimately produce only abstentions.
  That provisions trustworthy collection but does not satisfy the HOK-190 data
  sufficiency gate.
- HOK-190 remains blocked until real pre-action captures exist and a separately
  approved rubric produces sufficient, covered and independently signed
  preferences.
- Positive authority transitions remain fail-closed. Pairwise training labels
  do not constitute an authority attestation, observed outcome or promotion
  grant.
- The repository remains network- and subprocess-free; exporting inputs and
  invoking the external workflow are operator actions outside this package.

## Operational proof

Provisioning completed on 2026-08-17. The private labeler repository published
accepted source SHA `fd4e9c50947403a638817404fc6c596add1b0cf3`. GitHub Actions
run `32004975121` produced and keylessly signed a canonical abstaining label for
immutable request `303c875b65aa61176910ac1bd1d31bb72c23ebfd`.

Independent offline verification accepted the Sigstore bundle under the exact
workflow identity and issuer. Certificate inspection confirmed the repository,
`main` ref, `workflow_dispatch` trigger, GitHub-hosted runner and accepted
workflow SHA. The pair input seal reproduced across the source projection,
request and signed label. See
[`docs/independent-labeler.md`](../independent-labeler.md) for the proof manifest,
rotation procedure and remaining claim boundary.

GitHub branch protection is unavailable for a private repository on the current
account plan. Trust therefore pins the workflow SHA carried by the certificate;
the mutable branch name alone is never sufficient.

## External basis

- Sigstore keyless signing and Rekor witnessing:
  <https://docs.sigstore.dev/cosign/signing/overview/>
- Sigstore bundle verification:
  <https://docs.sigstore.dev/quickstart/quickstart-cosign/>
- GitHub Actions OIDC identity values:
  <https://docs.sigstore.dev/quickstart/verification-cheat-sheet/>
- GitHub guidance to pin Actions dependencies to full commit SHAs:
  <https://docs.github.com/en/actions/reference/security/secure-use>
- SLSA verification against preconfigured signer and builder expectations:
  <https://slsa.dev/spec/v1.1/verifying-artifacts>

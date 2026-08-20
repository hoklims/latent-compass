# Independent pairwise labeler (HOK-235)

## Operational result

The project now has a distinct private labeler repository:
<https://github.com/hoklims/latent-compass-labeler>.

Its claim-bearing principal is the GitHub-hosted workflow:

```text
issuer: https://token.actions.githubusercontent.com
identity: https://github.com/hoklims/latent-compass-labeler/.github/workflows/label.yml@refs/heads/main
accepted workflow SHA: 75af31a8aee941469fe088891cb090747ec0ce89
trigger: workflow_dispatch
repository: hoklims/latent-compass-labeler
ref: refs/heads/main
```

The accepted SHA is part of the trust policy, not caller input. A later commit,
even on the same branch and workflow path, is untrusted until an explicit
review, proof run and rotation updates this document and the external verifier.

The repository is private because sanitized decision summaries and labels are
not intended for public distribution. GitHub branch protection is unavailable
for a private repository on the current account plan. The design therefore does
not treat `main` as immutable: verification pins the certificate's workflow SHA
extension. This limitation is visible rather than replaced by a false protected-
branch claim.

## Flow

1. Latent Compass verifies a canonical pair against its captured projection
   preimage.
2. An operator records that sanitized `pair.json` in an immutable request
   commit in the private labeler repository.
3. The trusted workflow checks out its own accepted revision, installs only the
   locked dependencies, validates the request commit SHA and reads only the
   request's `pair.json`.
4. The frozen rubric produces canonical `labels.json`.
5. GitHub OIDC and Sigstore sign that exact file keylessly. Fulcio binds the
   ephemeral key to the workflow identity and Rekor witnesses the signing event.
6. The workflow publishes `labels.json` beside the non-circular
   `labels.json.sigstore.json` verification bundle.
7. A consumer verifies the artifact against the out-of-band issuer, identity,
   repository, ref, trigger and workflow SHA expectations before loading its
   label contract.

The main package does not invoke this workflow, open a socket, spawn Sigstore or
accept a caller-supplied verifier. Export, dispatch, download and cryptographic
verification remain composition-root/operator actions.

## Frozen rubric v1

Rubric `hok-235.conservative-evidence-identity@1.0.0` never creates a directional
winner:

- identical canonical dimension evidence yields `TIE`;
- unavailable or different evidence yields `ABSTAIN`;
- the aggregate ties only when all five dimensions tie, otherwise it abstains.

This conservative behavior is intentional. HOK-234's sanitized prose and fact
digests are judgeable to an external semantic actor, but they do not encode a
general machine-comparable utility. Lexical order, fact count, parameters,
logging propensity and uncertainty are not substituted for judgment.

The labeler is therefore operational and externally identifiable, while the
current rubric remains insufficient to train HOK-190. A semantic rubric is a
new reviewed version with its own data, licence, privacy and determinism
decision.

## First accepted proof

- Labeler source SHA: `fd4e9c50947403a638817404fc6c596add1b0cf3`
- Immutable request SHA: `303c875b65aa61176910ac1bd1d31bb72c23ebfd`
- GitHub Actions run: <https://github.com/hoklims/latent-compass-labeler/actions/runs/32004975121>
- Durable private release: <https://github.com/hoklims/latent-compass-labeler/releases/tag/labeler-v1.0.0>
  contains the exact label and Sigstore bundle after the Actions artifact expires.
- Run result: success on 2026-08-17; all production, signing, self-verification
  and publication steps passed.
- Source projection seal:
  `sha256:3fb96357b8440ce0457531adc533e8c911acab285a3e51dde0e8da70271918f6`
- Pair input seal, reproduced independently by Latent Compass, the request and
  the signed label:
  `sha256:81c60381cfb41d7fabea7e961c657b7a7f781b3794dd4c3acc803b134a5f4d45`
- Label bundle seal:
  `sha256:de03f5797bd09421c064e11d9deed3fa380143d745fddcd912a7f773b73422c2`
- Label result: `ABSTAIN`, invocation `bootstrap-final-20260817`.
- Downloaded label SHA-256:
  `4b75419cc3c782da06b11dd49f4ccc22eceb08ce95f6910e4edf13cc63ab26b0`.
- Downloaded Sigstore bundle SHA-256:
  `1d41a158d5ddf58c0ca36552ae64f4341120b6dd3b49a22c2b1373a39463d902`.

Independent offline Sigstore verification accepted the exact label, certificate
identity and issuer. Certificate inspection additionally confirmed:

```text
GitHub workflow SHA: fd4e9c50947403a638817404fc6c596add1b0cf3
workflow trigger: workflow_dispatch
workflow repository: hoklims/latent-compass-labeler
workflow ref: refs/heads/main
runner environment: github-hosted
repository visibility: private
```

## Accepted rotation proof

The trust policy was rotated from `fd4e9c50947403a638817404fc6c596add1b0cf3`
to `75af31a8aee941469fe088891cb090747ec0ce89` after the version-boundary fix was
reviewed and run successfully. GitHub Actions run
<https://github.com/hoklims/latent-compass-labeler/actions/runs/32008390667>
produced the HOK-190 abstention bundle used by the terminal discovery record.

Independent offline Sigstore verification of the downloaded artifact accepted
the canonical label against the pinned workflow identity and OIDC issuer. The
certificate extensions bind workflow SHA `75af31a8aee941469fe088891cb090747ec0ce89`,
trigger `workflow_dispatch`, repository `hoklims/latent-compass-labeler`, ref
`refs/heads/main`, a GitHub-hosted runner and run `32008390667` attempt 1. The
downloaded SHA-256 values are:

- label: `8137e43e77f939c9ccf05991302a355ecb6e18a2c32a03fb9079efbb590e819c`;
- Sigstore bundle: `e40cef892b71e9bab4144390b6224807caa525a12bdeebdd343dbcb84b009fbf`.

This rotation proves artifact identity and integrity only. It does not turn an
`ABSTAIN` into directional supervision or make the project owner independent
from themself.

## Rotation and revocation

Any workflow, rubric, dependency-lock or policy change creates a new candidate
SHA. The old accepted SHA remains the only trusted signer until all of these
steps complete:

1. review the exact new diff and action pins;
2. run locked tests, lint, formatting, typing and build;
3. produce a new signed fixture from an immutable request;
4. verify its Sigstore bundle independently and inspect the certificate claims;
5. update the accepted SHA and proof record in a reviewed contract change;
6. only then configure consumers to accept the new SHA.

Revocation removes an accepted SHA from the out-of-band verifier policy. It
does not delete Rekor history. A label signed by an unlisted SHA fails closed,
even when its repository, workflow path and branch still match.

## Claim boundary

The proof establishes that the accepted GitHub-hosted workflow signed the exact
label at a Rekor-witnessed time. It does not establish that the project owner is
independent from themself, that supplied facts are true, that capture preceded a
real action, that the rubric is useful, that sufficient labels exist, that a
ranker improves outcomes, or that any authority transition is allowed.

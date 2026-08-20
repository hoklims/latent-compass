# Reproducible evidence

This directory contains non-authoritative evidence produced while advancing the
offline evaluation gates. It deliberately excludes holdout content and cannot
authorize a lifecycle transition.

## HOK-188 validation replay

`hok188-run/report.json` is the canonical report produced by the four frozen
baselines on `VALIDATION`. `hok188-run/holdout-plan.json` binds the selected
baseline and validation report while recording that no holdout was executed.

Reproduce the report and verification with the commands in
[`docs/benchmark-protocol.md`](../docs/benchmark-protocol.md#the-command-line).

## HOK-234 real pre-action capture

`pre-action-inputs/hok-190-first-directional-label-source.json` records three
candidate strategies before the project selected its first source of
directional labels. The confined capture command published the canonical form
to `captures/hok-190-first-directional-label-source-v1.json` with projection
seal:

```text
sha256:216b14adf3aafa758a7b4459306469ccefdfce7a43fc196f7f50242540033b74
```

The Linear comment created before the selection provides the external
chronology anchor. The later decision selected a deterministic rule labeler
that must abstain whenever structured evidence is insufficient.

These local files prove deterministic replay and byte stability. They do not
prove that candidate facts are true, that a label is correct, or that an
arbitrator is better than a baseline.

## Terminal discovery decision

`decisions/latent-compass-kill-discovery-v1.json` records the terminal
`KILL_DISCOVERY` decision for the current research epoch. It preserves the
distinction between a failed data-readiness gate and a model performance
failure: no ranker was trained, so the decision makes no performance claim.
Its domain-separated seal is:

```text
sha256:a85103f85a3e8659cb893693df2084630ca949bac0a66cc30227240572b152fe
```

The corresponding HOK-194 Linear comment is the dated human attestation. A
future restart requires a new project and epoch, prospective supervision and
outcome budgets, and a newly provisioned claim-bearing holdout.

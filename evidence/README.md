# Reproducible evidence

This directory contains non-authoritative evidence produced while advancing the
offline evaluation gates. It deliberately excludes holdout content and cannot
authorize a lifecycle transition.

## HOK-188 validation replay

`hok188-run/report.json` and `hok188-run/holdout-plan.json` preserve the exact
benchmark 1.0.0 evidence available to the terminal decision on 2026-08-17. They
remain immutable historical evidence, including the subsequently identified
`TAIL` defect.

`hok188-run-v1.1.0/report.json` is the corrected replay produced by the same four
frozen baselines on `VALIDATION`. Its companion holdout plan binds the selected
baseline and corrected validation report while recording that no holdout was
executed. Benchmark 1.1.0 preserves cost units by using importance weights as
empirical probability mass rather than multiplying them into observed costs.

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
sha256:f36ad614b89c43fa2eab733fb9a995ca50de933d92929cb00a8a4e29392a8cb4
```

The corresponding HOK-194 Linear comment is the dated human attestation. A
future restart requires a new project and epoch, prospective supervision and
outcome budgets, and a newly provisioned claim-bearing holdout.

## Dated reconciliation

`decisions/latent-compass-kill-discovery-v1-reconciliation-2026-08-20.json`
preserves the original decision and its attestation, then binds the historical
benchmark 1.0.0 report to the corrected 1.1.0 replay. It explicitly states that
the later evidence did not exist on 2026-08-17 and requires its own Linear
attestation. Its domain-separated seal is:

```text
sha256:da17854eee708e6ec0e6c647475c222db6829e7a076d3a0219ace8c92c75c51e
```

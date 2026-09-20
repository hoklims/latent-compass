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

## HOK-799 scope inventory

`hok799-scope-inventory/inventory.json` is a read-only observation, read on
2026-09-18 and ending at 22:38 UTC, of the index providers, hooks, gateways, stores, scheduled tasks and
consumers on one personal workstation, as 25 assets in the
`latent_compass.lab.migration` `1.0.0` contract. No provider was started,
refreshed or reconfigured to take it. `dry-run-report.json` is the sealed
dry-run built from it:

```text
inventory_digest  sha256:e139cfb224264d341c6206a21d9b444d804aeb17deb955e3acb97adf575a0fea
report_seal       sha256:853172528179fafafba2616a7ca128c79d0405c43c7e8fb443ff8b71211f3249
```

Reproduce it with:

```python
import json
from pathlib import Path

from latent_compass.lab.migration import admit_inventory_asset, build_retirement_dry_run

base = Path("evidence/hok799-scope-inventory")
assets = tuple(
    admit_inventory_asset(item)
    for item in json.loads((base / "inventory.json").read_text(encoding="utf-8"))
)
print(build_retirement_dry_run(assets, generated_at="2026-09-19T00:00:00Z").report_seal)
```

That first revision stays exactly as committed, including two defects found
by an independent review: three never-assessed assets are declared
`CANDIDATE_INDEX`, and its `generated_at` is a placeholder later than the
commit that carries it. `hok799-scope-inventory-v1.1.0/` is the corrected
revision of the same read — same 25 assets, same configuration digests, those
three assets now `UNKNOWN`, and the real instant its report was built:

```text
inventory_digest  sha256:b7d494805b10ad1b2f08151de84bdd8ecd36371bb06bb95f2f16982cb6e38887
report_seal       sha256:3e2e819e274144f1bf736f71a86cc35e946c412e9f59792c37517e95fb7fcb31
```

It reproduces the same way, from its own directory and with
`generated_at="2026-09-18T23:47:51Z"`.

`hok799-scope-inventory-v1.2.0/` carries an owner decision, not a correction:
on 2026-09-19 a second personal repository, with CCC provisioned, joined the
pilot. It is revision 1.1.0 asset for asset, in the same order, plus the five
index artefacts found in that repository, read the same day at 12:56 UTC — by
the operator's account, without starting, refreshing or reconfiguring anything.
The repository travels under the alias `pilot-second-repository`:

```text
inventory_digest  sha256:8b0468fc231cd5f6720a2a688c4e08b0cf3a9ee0325144d27096a24640d17a67
report_seal       sha256:df4c53db79580da7f186e0fc9ef4aeaa9d9efc7d0f9dd70145b6ad586f9121a9
```

It reproduces the same way, with `generated_at="2026-09-19T12:57:30Z"`. Three of
the five new assets leave `KEEP` — for `DISABLE_LATER`, with all seven gates
missing; the two earlier revisions stay exactly as committed.

Each `configuration_digest` is the SHA-256 of one named configuration source
as read at observation time. The label-to-path mapping is operator-held, and so
is the second pilot's alias-to-path mapping: this repository refuses private
host paths and private repository names, and the professional environment is
recorded as an excluded class, never by name. The files prove deterministic
replay of a declared inventory. They do not prove that the inventory is
complete, that an index is unused, or that anything may be removed. Scope and
unknowns: [`docs/active-diagnosis-scope.md`](../docs/active-diagnosis-scope.md).

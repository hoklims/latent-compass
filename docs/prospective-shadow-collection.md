# Prospective shadow collection operator guide

The `shadow` namespace turns a sealed measurement design into an append-only
local collection. It does not choose work, change routing, grant authority or
feed a model. Current infrastructure status is **OFFLINE_VERIFIED**. A real
elapsed collection is still required before the resulting corpus says anything
about practice.

## 1. Compute power before writing a plan

There are no defaults. Supply every input explicitly:

```bash
uv run latent-compass shadow power \
  --p0 0.50 --p1 0.70 --alpha 0.05 --target-power 0.70 \
  --max-enrollments 33 --clustering-inflation 1.10
```

Copy the returned `power` object into the plan. If the result is
`POWER_NOT_ESTABLISHED`, change the caller-owned assumptions or enrollment
ceiling and recompute. A draft carrying that status cannot pass `validate-plan`,
be sealed by `init` or start collection.

Start from the deliberately non-admissible draft
[`examples/prospective-plan.template.json`](examples/prospective-plan.template.json).
Declare one exact decision/reconciliation binding, new calendar dates, every
pre-action stratum and the decision producer identities that the observer must
not reuse.

```bash
uv run latent-compass shadow validate-plan --plan plan.json
uv run latent-compass shadow init --root ./shadow-run --plan plan.json
uv run latent-compass shadow start --root ./shadow-run
```

`init` refuses an existing destination. `start` refuses an unpowered plan, an
early instant and an instant after the hard end.

## 2. Enroll new decisions only

The population is prospective: declared capture times before collection starts
are refused.
Pass the exact JSON record accepted by HOK-243, not an identifier or a projection
reconstructed later.

Enrollment validates this supplied record and its declared source binding. It
does not open the decision store, prove that the record was appended there, or
witness when capture happened. Preserve the native source record and operational
evidence of its chronology; a detached, self-sealed record alone cannot establish
that an empirical case was genuinely collected prospectively.

```bash
uv run latent-compass shadow enroll --root ./shadow-run \
  --decision-record ./decision.json --stratum maintenance
```

Wrong bindings, undeclared strata, pre-window decisions, duplicates, holdout
classification and enrollment above the sealed maximum refuse without moving
the collection generation.

## 3. Terminalize every enrolled case

For an observed execution, point at the durable HOK-244 journal and exact
HOK-243 record. Only the current verified HOK-244 tail can be linked.

```bash
uv run latent-compass shadow reconcile --root ./shadow-run \
  --case-id case-id-from-enroll --decision-record ./decision.json \
  --source-root ./reconciliation-journal --reconciliation-id reconciliation-0001
```

The observer and every observation producer must differ syntactically from the
decision producer identities sealed in the plan. This is an honesty check on
declared strings, not identity authentication.

When execution evidence will not arrive, retain the case in the denominator:

```bash
uv run latent-compass shadow terminal --root ./shadow-run \
  --case-id case-id-from-enroll --state cancelled --reason "cancelled externally"
uv run latent-compass shadow terminal --root ./shadow-run \
  --case-id another-case --state lost-to-followup --reason "observer unavailable"
```

## 4. Verify, close and publish

```bash
uv run latent-compass shadow status --root ./shadow-run
uv run latent-compass shadow verify --root ./shadow-run
uv run latent-compass shadow close --root ./shadow-run
uv run latent-compass shadow manifest --root ./shadow-run \
  --out ./shadow-run/artifacts/manifest.json
uv run latent-compass shadow report --root ./shadow-run \
  --out ./shadow-run/artifacts/report.json
uv run latent-compass shadow limits
```

An integrity or security failure uses the separate terminal abort path:

```bash
uv run latent-compass shadow abort --root ./shadow-run \
  --reason integrity-failure --expected-generation 17
```

The only accepted reasons are `integrity-failure` and `security-failure`.
`ABORTED` is terminal: repeat abort, start, enrollment, reconciliation,
terminalization and close writes refuse, and neither manifest nor report can be
published.

Manifest and report refuse while collecting, after abort or when verification
is not green. Output paths must be strictly beneath the named root and existing
files are never overwritten. The manifest includes every enrolled case; the
eligible-corpus seal covers only structurally eligible reconciliations. The
report is deliberately narrow: rates, missingness, inclusion by declared
stratum and `selection_imbalance_diagnostic` only.

## Claim boundary

Local seals prove local consistency only. They do not authenticate producers,
establish observation truth, prove a causal effect, establish better routing or
justify training. The package does not read or enumerate evaluation holdout
content. Even a sufficient close proves only that the preregistered collection
conditions were met; interpretation remains outside this package.

# Public independent-audit protocol

This repository owns a public, deterministic audit protocol. It replaces the
unobtainable private schema and gate referenced by the original wording of
issue #5.

The auditor creates an epoch from public Git objects:

```bash
python tools/independent_audit.py epoch \
  --repository . --base <base-sha> --head <candidate-sha> --output epoch.json
```

The epoch uses the stable logical repository identity `hoklims/latent-compass`,
independent of whether the clone remote uses HTTPS or SSH. The gate recomputes
the canonical epoch digest and validates its Git identities and complete changed
file inventory before considering the receipt.

The receipt uses schema `hoklims/latent-compass:independent-audit/2` and copies
the epoch's `epoch_digest`, `policy_digest`, and `head_sha`. It records all eight
independence booleans enforced by the gate, a non-empty `claims` array, an empty
`unresolved_blockers` array, and verdict `PROOF_ADEQUATE`.

Each claim names the claim and every invocation path. Its `witness` contains a
specific mutation, the executed command, a non-zero integer `red_exit`, a zero
integer `green_exit`, and SHA-256 digests of both outputs. The auditor keeps the
raw outputs and mutation source in their own evidence store or audit PR.

Run the fail-closed gate with:

```bash
python tools/independent_audit.py gate --epoch epoch.json --receipt receipt.json
```

Only exit code zero with `"decision": "ALLOW"` is an adequate receipt. A
missing field, stale epoch, failed independence condition, absent red/green
witness, unresolved blocker, or weaker verdict exits non-zero with `BLOCK`.

This protocol establishes that the named public candidate survived the stated
independent adversarial checks. It does not establish comparative product
quality, pilot readiness, authority to execute hooks, or permission to remove
an index.

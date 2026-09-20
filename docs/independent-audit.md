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
independent of whether the clone remote uses HTTPS or SSH. At gate time, the
tool resolves the named commits from the selected repository, reads the policy
files from the candidate Git object, reconstructs the tree and complete changed
file inventory, then requires byte-for-byte equality with the submitted epoch.
Working-tree edits therefore cannot change an epoch for an unchanged commit.

The receipt uses schema `hoklims/latent-compass:independent-audit/3` and copies
the epoch's `epoch_digest`, `policy_digest`, and `head_sha`. It records all eight
independence booleans enforced by the gate, a non-empty `claims` array, an empty
`unresolved_blockers` array, and verdict `PROOF_ADEQUATE`.

Each claim names the claim and every invocation path. Its `witness` contains a
specific mutation, one or more precise repository-relative pytest node IDs, the
node ID expected to fail, and the exact UTF-8 before/after bytes and digests for
each canonical mutation target. The gate creates two independent detached
disposable worktrees at the candidate commit. In the first it checks every
`before` value against that Git object, applies the mutation, and requires both
a non-zero exit and the named failure in pytest output. In the pristine second
worktree it requires the same tests to pass. Exit codes and output digests are
produced by the gate itself, not accepted from the receipt. Every claim must
cover the exact invocation paths `local`, `pull_request`, and `main`.

Run the fail-closed gate with:

```bash
python tools/independent_audit.py gate \
  --repository . --epoch epoch.json --receipt receipt.json
```

Only exit code zero with `"decision": "ALLOW"` is an adequate receipt. A
missing field, stale epoch, failed independence condition, absent red/green
witness, unresolved blocker, or weaker verdict exits non-zero with `BLOCK`.

This protocol establishes that the named public candidate survived the stated
independent adversarial checks. It does not establish comparative product
quality, pilot readiness, authority to execute hooks, or permission to remove
an index.

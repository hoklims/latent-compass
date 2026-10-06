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

Epochs and receipts use schema `hoklims/latent-compass:independent-audit/4`.
The receipt copies the epoch's `epoch_digest`, `policy_digest`, and `head_sha`.
It records `audit_profile`, `independence`, `isolation`, a non-empty `claims`
array, an empty `unresolved_blockers` array, and verdict `PROOF_ADEQUATE`.
Legacy v3 artifacts are rejected; do not relabel an old epoch or receipt as v4.
Create a new Git-bound epoch and perform a new audit.

## Explicit audit profiles

The operator selects the profile through `--audit-profile`; its default is
`separate-account`. The receipt must name exactly the same profile. Receipt
contents never select or relax the gate's profile.

`separate-account` retains the eight required true booleans:
`not_candidate_author`, `read_only_candidate`, `fresh_session`,
`distinct_harness`, `distinct_account`, `distinct_environment`,
`distinct_evidence_store`, and `first_pass_before_author_narrative`.
Its `isolation` field must be `null`.

`isolated-session` is an operator-chosen adaptation for a fresh independent
review on the same account. It requires exactly this receipt profile section:

```json
{
  "audit_profile": "isolated-session",
  "independence": {
    "not_candidate_author": true,
    "read_only_candidate": true,
    "fresh_session": true,
    "first_pass_before_author_narrative": true,
    "distinct_account": false
  },
  "isolation": {
    "session_id": "observed-fresh-session-id",
    "forked": false,
    "sandbox_mode": "read-only",
    "persistent_memory": false,
    "write_tools_enabled": false
  }
}
```

The session identifier must be non-empty, the session must not be forked, and
the observed sandbox must be read-only. `persistent_memory` and
`write_tools_enabled` may be `null` only when their attestations are unavailable;
report that uncertainty rather than inventing a false attestation. `true`,
missing or malformed values are refused. This profile does not claim a distinct
account, host/environment, harness or evidence store. Additional independence
or isolation fields are rejected.

The auditor first reads the source without the author's narrative, then reviews
the final raw evidence and candidate bindings before issuing the receipt. A
fresh session on the same account is not author-independent if it authored the
candidate or did not perform that first pass.

Each claim names the claim and every invocation path. Its `witness` contains a
specific mutation, one or more precise repository-relative pytest node IDs, the
node ID expected to fail, and the exact UTF-8 before/after bytes and digests for
each canonical mutation target. The gate creates two independent detached
disposable worktrees at the candidate commit. In the first it checks every
`before` value against that Git object, applies the mutation, and requires both
pytest exit 1 and a trusted execution report showing the named node actually
failed during its `call` phase. Collection, usage, internal, setup and teardown
errors, signals, timeouts, failures outside declared selectors, skipped and
xfailed executions cannot count as detection. In the pristine second worktree it requires exit 0
and all selected nodes to have been collected, executed and passed. Qualified
class and parameter selectors are resolved against exact collected node IDs.
The expected failure identifies a leaf function/method or its parameter group,
never an arbitrary class prefix. Other declared selected tests may also fail
during `call`; the expected leaf must be among those actual failures.
Before any child runs, each selector's file must be a canonical tracked regular
Git blob in the candidate. Traversal, aliased or missing paths and symlink nodes
are rejected, and the expected failure must belong to the selected nodes.
Exit codes and output digests are
produced by the gate itself, not accepted from the receipt. Every claim must
cover the exact invocation paths `local`, `pull_request`, and `main`.

The pytest reporter belongs to the policy digest. Before each child process the
evaluator copies it from its canonical policy directory into a temporary
directory outside the mutated worktree. Each fresh report has a per-run nonce;
missing, malformed or stale reports refuse admission.
The child imports installed pytest and canonical instrumentation before adding
the candidate's import paths, so candidate module names cannot replace either
evaluator during bootstrap. Reported execution proves the test outcome and
phase, not universal semantic relevance of every possible
test-body failure. Independent source and material-claim review remain required.

Run the fail-closed gate with:

```bash
python tools/independent_audit.py gate \
  --repository . --epoch epoch.json --receipt receipt.json
```

To use the same-account adaptation explicitly:

```bash
python tools/independent_audit.py gate \
  --repository . --epoch epoch.json --receipt receipt.json \
  --audit-profile isolated-session
```

Only exit code zero with `"decision": "ALLOW"` is an adequate receipt. A
missing field, stale epoch, failed independence condition, absent red/green
witness, unresolved blocker, or weaker verdict exits non-zero with `BLOCK`.
An isolated-session allowance reports verdict `PROOF_ADEQUATE_WITH_LIMITS` and
always includes `SAME_ACCOUNT_ISOLATED_REVIEW`. Unknown attestations additionally
produce `AUDITOR_ENVIRONMENT_UNATTESTED:persistent_memory` and/or
`AUDITOR_ENVIRONMENT_UNATTESTED:write_tools_enabled`. The strict profile reports
`PROOF_ADEQUATE` with an empty limits list. All Git-object bindings, claim and
invocation requirements, mutation-byte checks, actual red rejection, pristine
green replay and blocker/verdict requirements are identical across profiles.

## Changes to this proof mechanism

The candidate gate cannot approve its own modification. Admission of a change
to this protocol, its gate or tests requires the immutable external N-1
evaluator; in the current operator workflow this is the installed global proof
policy, kept unchanged during candidate authoring and review. Exercise this
public gate for behavioral evidence, but do not treat its result as authority
to activate its own changed policy. A fresh independent source-first audit and
final evidence review remain required before external admission.

This protocol establishes that the named public candidate survived the stated
independent adversarial checks. It does not establish comparative product
quality, pilot readiness, authority to execute hooks, or permission to remove
an index.

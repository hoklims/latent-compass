# Independent audit round 3 — `hoklims/latent-compass`

**Gate decision: `{"decision": "BLOCK", "error": "distinct_harness must be true"}`, exit code `1`.**
**Verdict: `PROOF_WEAK`.** Five unresolved blockers; three protocol claims verified with real red/green witnesses.

All local filesystem paths in this report and in every deliverable are replaced by
`<SCRATCH>`, `<TMP>` or `<HOME>`. No secrets, tokens, prompts, tool arguments or
third-party personal data appear anywhere in the published set.

---

## 1. Epoch

Created by the candidate's own tool from public Git objects, in a clone detached
at the head SHA:

```console
$ python tools/independent_audit.py epoch --repository . \
    --base f652690f29420b5d4ae4f37b22caa5e98df102a2 \
    --head 45078dcc10d228326f410df76f7af15ff2133e0b \
    --output epoch.json
exit=0
```

| field | value |
| --- | --- |
| `schema` | `hoklims/latent-compass:independent-audit/2` |
| `repository` | `hoklims/latent-compass` |
| `base_sha` | `f652690f29420b5d4ae4f37b22caa5e98df102a2` |
| `head_sha` | `45078dcc10d228326f410df76f7af15ff2133e0b` |
| `head_tree` | `497312722bbc3a5602a4e95a56b7240f6a2ba61a` |
| `policy_digest` | `sha256:22575e6533a7ea2b4d9b2b5c92ee58489b6f8145c8dc387d04763af100a35061` |
| `epoch_digest` | `sha256:8d53dfe9e297ea4b57bc5dd528050d21d8b1f86490a996936d5dd1d86605994e` |
| inventory | 13 files, all `kind: "file"` |

`45078dcc` is the merge of PR #13 into `main`; `f652690f` is its merge base with
that history, so `base...head` is the complete candidate.

**Epoch reproducibility (clean tree): confirmed.** Regenerating from a second,
independently-fetched clone at a different filesystem path produced a
byte-identical `epoch.json`
(`sha256:85d55f57b96169a1bf4b571fd047ca7931319ed3a9fd2565ee0a5714ae6a316c`).
The round-2 finding R2 — `repository` derived from `remote.origin.url`, so HTTPS
and SSH clones disagreed — is genuinely fixed by the hardcoded `REPOSITORY`
constant. **Epoch reproducibility with any uncommitted edit: broken.** See B4.

---

## 2. Identity and independence

I am a fresh agent session. My entire input was a handoff carrying the
repository name, the two SHAs, the protocol path and the working rules — no
prior audit artifact, no author narrative.

The GitHub account available in this environment is **`Laegel`**. Measured
against the candidate repository:

```console
$ gh api repos/hoklims/latent-compass --jq '.permissions'
{"admin":false,"maintain":false,"push":false,"triage":false,"pull":true}
$ gh api repos/hoklims/latent-compass/contributors --jq '.[].login'
hoklims
$ gh api repos/hoklims/latent-compass/collaborators/Laegel
HTTP/2.0 403 Forbidden
```

`Laegel` is **not** in the contributor list (the only contributor is `hoklims`)
and holds **pull-only** permissions. Nothing was pushed to
`hoklims/latent-compass`.

### The independence booleans, answered truthfully

| boolean | value | why |
| --- | :-: | --- |
| `not_candidate_author` | `true` | The candidate's commits are authored by the repository owner. This session wrote none of them. |
| `read_only_candidate` | `true` | Every mutation was applied to a disposable clone, reverted, asserted byte-identical, and the clone finished with an empty `git status --porcelain` at the head SHA. |
| `fresh_session` | `true` | Entire input was the handoff. |
| `distinct_harness` | **`false`** | Earlier rounds of this repository were audited from the same agent harness on the same machine. Distinct from the *author's* harness, not from the *previous auditors'*. The protocol does not define the referent; I answer against the stricter reading. |
| `distinct_account` | **`false`** | `Laegel` is the only account available here and is also the account that published the round-1 and round-2 audit artifacts. Distinct from the author's account `hoklims`. |
| `distinct_environment` | **`false`** | Same Linux host as the previous rounds. A fresh clone, a fresh `UV_CACHE_DIR` and a fresh `UV_PYTHON_INSTALL_DIR` were used inside a new scratch directory, but the machine is not separate. |
| `distinct_evidence_store` | `true` | A new output directory for this round; no prior round's store was opened or reused. It lives on the same filesystem as those stores. |
| `first_pass_before_author_narrative` | `true` | See below. |

**This is the honest failure that blocks first.** Three of the eight booleans do
not hold. I record them as `false` rather than asserting them away. A genuinely
distinct account, machine and harness is the remedy, and it is outside what this
session can arrange.

### First-pass ordering — met

`FIRST-PASS.md` was written from only: the aggregate `base...head` diff with the
`HANDOFF.md` and `README*.md` hunks deliberately excluded,
`docs/independent-audit.md`, `tools/independent_audit.py`,
`tools/verify_sdist.py`, `tests/test_independent_audit.py`, both workflow files,
`pyproject.toml`, and the repository's own test suite run at head.

`HANDOFF.md`, the READMEs' proof sections, PR #11, PR #13 and issue #12 were
read **only after** `FIRST-PASS.md` existed on disk. No accidental early read
occurred; the only unavoidable exposure was the presence of `HANDOFF.md`,
`README.md` and `README.fr.md` in `git diff --stat`, whose contents and diffs I
did not open. Round 2's R3 is therefore cleared for round 3.

---

## 3. What reproduces

All commands run in a clone detached at `45078dcc`, with the locked environment
(`uv sync --locked --all-groups`, exit 0) and Python 3.13.14.

| command | exit | observed |
| --- | :-: | --- |
| `uv run --frozen ruff format --check .` | 0 | `154 files already formatted` |
| `uv run --frozen ruff check .` | 0 | `All checks passed!` |
| `uv run --frozen mypy` | 0 | `Success: no issues found in 113 source files` |
| `uv run --frozen pytest -o addopts='' -q` | 0 | `1412 passed, 33 skipped in 102.50s` |
| `uv build --no-sources` | 0 | sdist + wheel |
| `python tools/verify_sdist.py dist/latent_compass-*.tar.gz` | 0 | `1412 passed, 33 skipped in 97.69s` |
| `python tools/independent_audit.py epoch …` | 0 | epoch above |
| `git status --porcelain` after all mutations | 0 | empty |

`pyright` is not installed here, so 33 tests skip; on CI's Ubuntu leg the
`npm install --global pyright@1.1.414` step is what removes most of those skips.
I did not install pyright and therefore did not exercise those paths.

---

## 4. Claims matrix

Three claims survive adversarial testing with genuine red/green pairs. Digests
are SHA-256 of the full untrimmed combined stdout+stderr.

| # | Claim | Mutation | Red | Green |
| :-: | --- | --- | :-: | :-: |
| C1 | the gate rejects an epoch whose `epoch_digest` no longer matches its own contents | set `epoch.head_sha` to `ffff…` without recomputing `epoch_digest` | `1` — `{"decision":"BLOCK","error":"epoch_digest does not match the canonical epoch contents"}` — `sha256:6674aac8…` | `0` — `"decision": "ALLOW"` — `sha256:92ba7191…` |
| C2 | `tests/test_independent_audit.py` is load-bearing | delete the `unresolved_blockers` check from `gate()` | `1` — `1 failed, 8 passed` — `sha256:69e3b5d0…` | `0` — `9 passed` — `sha256:d69b6f69…` |
| C3 | the sdist really is self-testing | remove `"/tools"` from `[tool.hatch.build.targets.sdist].include` | `2` — `ERROR collecting tests/test_independent_audit.py`, `1 error in 1.75s` — `sha256:2fec4dd4…` | `0` — `1412 passed, 33 skipped` — `sha256:5d5e4afd…` |

C1 is the round-2 R1 remediation, and it is real — just much narrower than the
documentation claims (B1, B2). C3 shows the new `/tools` sdist include and the
new "Test extracted source distribution" step are coupled and both necessary:
without the include, the shipped suite cannot even collect.

The corpus line-ending work (`.gitattributes` `* text=auto eol=lf`, and
`newline="\n"` in `tests/corpus_generator.py::write`) is **not witnessable on
Linux**: `write_text`'s default `newline=None` already emits `\n` on POSIX, so
the mutation is a no-op here. The test it protects,
`tests/test_synthetic_corpus.py::test_the_committed_corpus_regenerates_byte_for_byte`,
is only discriminating on the `windows-latest` matrix leg. I record this as
plausible and untested rather than verified.

---

## 5. Findings

### B1 (blocking) — the gate's `ALLOW` is still not bound to the candidate

`gate(epoch, receipt)` takes two JSON objects and never touches Git, the
filesystem, or the candidate. Its checks on the epoch are: 40-hex shape for the
three SHAs, `sha256:` shape for the digests, well-formedness and sortedness of
the inventory, and `epoch_digest == _epoch_digest(epoch)` — self-consistency of
the object with itself. Nothing ties any of it to `hoklims/latent-compass`.

I built an epoch whose `base_sha`/`head_sha`/`head_tree` are `000…1`, `000…2`,
`000…3` (no such objects exist in any repository), whose `policy_digest` is
`sha256:000…0`, and whose inventory is one invented path — then computed its
`epoch_digest` with the tool's own `_epoch_digest`, and paired it with a receipt
whose only claim reads `"no audit work was performed at all"`:

```console
$ python tools/independent_audit.py gate --epoch w1-forged-epoch.json --receipt w1-forged-receipt.json
{
  "schema": "hoklims/latent-compass:independent-audit/2",
  "decision": "ALLOW",
  "epoch_digest": "sha256:29e627bafc27dd48b4e7d56aca356c1a06c2edbb69cbc04b181e8f7effffcfb9",
  "head_sha": "0000000000000000000000000000000000000002"
}
exit=0
```

Round 2's R1 asked for two things: recompute `epoch_digest` inside `gate`, **and**
re-derive the epoch from the repository. Commit `170a751` delivered the first.
The second is what makes an `ALLOW` mean anything, and it is absent. The
practical effect of the round-2 fix is that a forger must now run one function
instead of typing a literal — `_epoch_digest` is a module-level function in the
same file.

**Bounded fix.** Give `gate` a `--repository` argument, call
`create_epoch(repository, epoch["base_sha"], epoch["head_sha"])` and require the
result to equal the supplied epoch field-for-field; fail closed when the
repository is absent rather than falling back to the JSON-only path. One
function call plus one fail-closed test, in the same file as C1's check.

### B2 (blocking) — `docs/independent-audit.md` describes checks that do not exist

The protocol document states:

> The gate recomputes the canonical epoch digest and validates its Git
> identities and complete changed file inventory before considering the receipt.

Only the first clause is true. "Validates its Git identities" is a 40-hex regex.
"Complete changed file inventory" is not checked at all — the gate has no way to
know what the complete inventory is. Witness: I removed the
`tools/independent_audit.py` entry — the gate's own source — from the real
epoch's inventory, recomputed `epoch_digest`, and gated it:

```console
$ python tools/independent_audit.py gate --epoch w3-truncated-epoch.json --receipt w3-truncated-receipt.json
"decision": "ALLOW"
exit=0
```

An epoch can therefore hide the very change under audit and still be allowed.
PR #13's description carries the same overstatement, plus "rejects placeholder
witness digests" — the implementation rejects only strings failing
`sha256:[0-9a-f]{64}`; `"sha256:" + "a"*64` is accepted, as every forged receipt
in `witnesses/inputs/` demonstrates.

**Bounded fix.** Either implement B1's re-derivation, which makes the sentence
true, or rewrite the sentence to say what the gate does: it checks the epoch is
internally self-consistent and well-formed; it does not re-derive it from the
repository, so an epoch must be independently regenerated and compared by hand.

### B3 (blocking) — JSON booleans pass the exit-code checks

```python
if not isinstance(witness.get("red_exit"), int) or witness["red_exit"] == 0:
    raise AuditError(f"claim {index} has no observed red result")
if witness.get("green_exit") != 0:
    raise AuditError(f"claim {index} has no observed green result")
```

`bool` subclasses `int` in Python, `True != 0`, and `False == 0`. A receipt
carrying `"red_exit": true, "green_exit": false` — which records no exit codes at
all — is accepted against the **real** epoch for this candidate:

```console
$ python tools/independent_audit.py gate --epoch epoch.json --receipt w2-boolean-receipt.json
"decision": "ALLOW"
exit=0
```

The documentation asks for "a non-zero integer `red_exit`, a zero integer
`green_exit`". The implementation does not enforce that.

**Bounded fix.** `type(value) is int` for both fields (or
`isinstance(v, int) and not isinstance(v, bool)`), and require `green_exit` to be
an `int` as well as `== 0`. Add the two cases to the existing
`test_gate_fails_closed` parametrisation, where `red_exit: 0` and `green_exit: 1`
already live.

### B4 (blocking) — the epoch is not a pure function of `(base, head)`

`create_epoch` derives `files[]` digests from `git show <head_sha>:<path>`, but
`_policy_digest` reads the three `POLICY_FILES` from the **working tree**
(`path.read_bytes()`). Same arguments, one uncommitted edit to
`docs/independent-audit.md`:

| | clean tree | dirty tree |
| --- | --- | --- |
| `head_sha` / `head_tree` | identical | identical |
| `policy_digest` | `sha256:22575e65…` | `sha256:9931a657…` |
| `epoch_digest` | `sha256:8d53dfe9…` | `sha256:1d3aae28…` |

The dirty epoch is additionally self-contradictory: its `files[]` entry for
`docs/independent-audit.md` still carries the **committed** blob digest
(`sha256:2d3f09e6…`) while `policy_digest` covers the **edited** bytes. Nothing
detects this, because the gate never recomputes either value.

Two honest auditors at the same head can therefore produce different canonical
epoch digests, and neither the gate nor a reader can tell which is canonical.
Since regenerating the epoch is the *only* external check standing in for B1,
B1 and B4 compound — exactly the way R1 and R2 compounded in round 2.

**Bounded fix.** Read the three policy files through
`_git(root, "show", f"{head_sha}:{relative}", binary=True)`, like every other
inventory entry. Four lines in `_policy_digest`, plus a test that a dirty tree
does not move `epoch_digest`.

### B5 (blocking for the *enforcement* claim) — nothing invokes the gate

```console
$ grep -rn independent_audit .github
exit=1   (no output)
```

Neither `verify.yml` nor `release.yml` references `tools/independent_audit.py`.
No CI status, no branch protection and no release step depends on a receipt
existing. `docs/independent-audit.md` does not claim CI enforcement, so this is
not a documentation lie — but it is the whole difference between a proof
mechanism and a receipt formatter. Combined with B1, the current state is: a
document that nothing runs, which if run would accept a fabrication.

**Bounded fix.** Either add a `Proof` job that runs
`python tools/independent_audit.py gate --epoch … --receipt …` against a
committed receipt, or state plainly in the protocol document that the gate is
operated out of band by a human and that its result is not a repository status
check.

### Non-blocking observations

| # | Observation |
| :-: | --- |
| N1 | `verify.yml` hardcodes `dist/latent_compass-0.1.0.tar.gz` and `…-0.1.0-py3-none-any.whl` while `release.yml` globs. A version bump silently breaks the Verify job on both matrix legs. |
| N2 | `release.yml` is triggered directly by a tag push. It runs the extracted-sdist suite but not `ruff`/`mypy`, and does not require a successful `Verify` run for the same commit. Nothing prevents tagging a commit that never passed Verify. |
| N3 | `release.yml` has still never run — no tag has been pushed since it landed — so its attestation and publication steps are verified by reading, not observation. (This repeats round 2's N3; I confirm it is unchanged.) |
| N4 | The epoch path check rejects a leading `/` or `../` but not an embedded `a/../../b`. Harmless today because the gate never touches the filesystem; it becomes load-bearing the moment B1's fix makes the gate read files. |
| N5 | `npm install --global pyright@1.1.414` is a floating network dependency in CI, pinned by version but not by integrity hash, and installed globally rather than under a project prefix. |
| N6 | The protocol has no representation for an honest negative receipt. The only receipt shape the schema permits is an approving one; a blocking outcome exists only as "the gate exited 1". This audit's receipt is structurally invalid *because* it reports the truth. Consider allowing `verdict` to range over the weaker values with `unresolved_blockers` non-empty, so that a blocking receipt is a first-class, gate-validated artifact rather than a rejected one. |
| N7 | The gate does not reject unknown top-level keys in either document. My receipt carries `auditor`, `independence_notes` and `unresolved_blockers_detail` and the gate does not mind. Convenient here, but it means a receipt's shape is not closed. |

### Comparison with the author's own narrative (read after `FIRST-PASS.md`)

The narrative is honest about status and does not overclaim the outcome.
`HANDOFF.md` says this proof-mechanism change must itself receive a second
independent audit before any `ALLOW`; both READMEs state the status remains
`PROOF_WEAK/BLOCK`. Nothing in my first pass contradicts that.

What the narrative does **not** disclose, and `docs/independent-audit.md`
actively contradicts, is B1/B2: the round-2 finding R1 was reported as addressed
("recomputes and verifies the canonical epoch digest inside the gate",
"validates … complete changed file inventory"), and only its first half was
implemented. B3, B4 and B5 are new and appear in none of the prior rounds'
published findings. Round 2's R2 (clone-transport-dependent identity), N1
(`tools/` outside mypy), N2 (deleted/empty digest collision) and N4 (dead
`newline` argument) are all genuinely fixed; I verified R2 and N1 directly.

---

## 6. Gate invocation

Canonical run, on the deliverables in this directory:

```console
$ python tools/independent_audit.py gate --epoch epoch.json --receipt receipt.json
{"decision": "BLOCK", "error": "distinct_harness must be true"}
exit=1
full_output_sha256=sha256:eb7ab08df1f83f3b9e4c804e5dbe85c1200923deceff301b446f7eb75ecfbcf5
```

The gate short-circuits on the first failure, so the reported reason is the
independence boolean. Three reasons block **independently**; waiving each in turn
on a copy of the receipt reaches the next:

| receipt | exit | output |
| --- | :-: | --- |
| `receipt.json` (as submitted) | `1` | `{"decision": "BLOCK", "error": "distinct_harness must be true"}` |
| independence waived to all-`true` | `1` | `{"decision": "BLOCK", "error": "unresolved_blockers must be an empty array"}` |
| independence waived **and** blockers emptied | `1` | `{"decision": "BLOCK", "error": "verdict must be PROOF_ADEQUATE"}` |

Those two waived copies are counterfactual probes kept in `witnesses/inputs/`;
the submitted receipt is `receipt.json` and it is the truthful one.

---

## 7. Read-only discipline

Every mutation was applied to a disposable clone of the public repository,
reverted, and asserted byte-identical to its saved original. The driver refuses
to emit `claims.json` unless `git status --porcelain` is empty afterwards:

```console
$ git status --porcelain
exit=0
output=''
```

Files mutated and restored: `tools/independent_audit.py` (C2),
`pyproject.toml` (C3), `docs/independent-audit.md` (B4). Build output landed in
the gitignored `dist/` and was removed. Nothing was pushed to
`hoklims/latent-compass`; the account used holds pull-only permissions.

---

## 8. What this audit does not establish

- **Nothing about product quality.** No comparative evaluation, no
  non-inferiority, no cost or latency measurement.
- **Nothing about pilot readiness**, hook-execution authority, or permission to
  remove an index. Those are explicitly out of scope for the protocol.
- **Nothing observed about the release workflow.** No tag was pushed, so
  provenance attestation, artifact retention and publication are verified by
  reading `release.yml` only (N3).
- **Nothing about the Windows matrix leg.** All runs here are Linux. The
  line-ending work is precisely the part that only discriminates on Windows, and
  I could not exercise it.
- **Nothing about the pyright-dependent tests.** 33 tests skip in this
  environment; I did not install the language server.
- **Nothing about the historical `v0.1.0` artifacts.** Out of scope for this
  epoch, and the READMEs already characterise them as historical.
- **No claim of clean separation from prior audits.** The machine and the GitHub
  account are shared with the sessions that audited earlier candidates of this
  repository. I did not read their work before forming my first pass, but that
  is a procedural assertion about this session, not an environmental guarantee.
  Three independence booleans are `false` for this reason and it is the first
  thing the gate blocks on.
- **Coverage is bounded to the diff.** I audited `base...head` and the gate
  mechanism adversarially. I did not re-audit the 1400-test body of the project
  beyond running it.

---

## 9. Deliverables

| file | what it is |
| --- | --- |
| `FIRST-PASS.md` | findings formed before any author narrative was read (`sha256:3ea0ef9f…`) |
| `epoch.json` | produced by the candidate's own `tools/independent_audit.py epoch` (`sha256:85d55f57…`) |
| `receipt.json` | protocol receipt, `PROOF_WEAK`, 5 blockers, 3 claims (`sha256:5c2f3772…`) |
| `witnesses/claims.json` | machine-readable claims and blockers (`sha256:22c5a963…`) |
| `witnesses/*.txt` | trimmed outputs, each carrying its command, exit code and full-output SHA-256 |
| `witnesses/inputs/*.json` | the forged and counterfactual epochs/receipts the witnesses consumed |
| `witnesses.py` | the driver that produced every witness, carrying the exact mutations and commands |
| `REPORT.md` | this report |

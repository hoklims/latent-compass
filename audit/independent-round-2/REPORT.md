# Independent audit, round 2 — `d5ae2362ff326f2dbece7eacf889f4fd7c8d8dd1`

Answers issue [#5](https://github.com/hoklims/latent-compass/issues/5) under the
public protocol `hoklims/latent-compass:independent-audit/2`. Round 1 is
[PR #6](https://github.com/hoklims/latent-compass/pull/6) and is not rewritten here.

**Public gate result: exit `1`, `{"decision": "BLOCK", "error": "first_pass_before_author_narrative must be true"}`.**
**Verdict: `PROOF_WEAK`.**

```bash
python tools/independent_audit.py gate --epoch epoch.json --receipt receipt.json
```

The gate blocks for three independent reasons, each reproducible from the files
in this directory: the auditor's first-pass constraint is not met (R3), the
receipt carries unresolved blockers (R1, R2), and the verdict is weaker than
`PROOF_ADEQUATE`. Removing each reason in turn still blocks on the next —
`witnesses/gate.txt` records the first; the other two are reproduced in §6.

**The round-1 remediations are real and I verified all of them.** The blockers
below are in the new proof mechanism itself, not in the fixes.

## 1. Epoch

Created from public Git objects with the repository's own tool:

```bash
python tools/independent_audit.py epoch --repository . \
  --base f652690f29420b5d4ae4f37b22caa5e98df102a2 \
  --head d5ae2362ff326f2dbece7eacf889f4fd7c8d8dd1 --output epoch.json
```

| field | value |
| --- | --- |
| `base_sha` | `f652690f29420b5d4ae4f37b22caa5e98df102a2` (round-1 candidate) |
| `head_sha` | `d5ae2362ff326f2dbece7eacf889f4fd7c8d8dd1` (merge of PR #11) |
| `head_tree` | `d3af235b8adc8e660a0bd26f8682e439202c0cd7` |
| `policy_digest` | `sha256:9c9e5531cb2366e4d44a8ed4529eae99a2ff967af76edb7bb3540cd1d3a372c5` |
| `epoch_digest` | `sha256:9f4a5daebba2b97119676620c63f8d70ba9ed36eac714bf34aa2257f369938bb` |
| files | 13 |

`main` was at `d5ae2362…` throughout this pass — **no divergence to report**. The
merge's parents are the round-1 candidate `f652690…` and the CI-reviewed head
`24911ba2…`, so the epoch's diff is exactly the aggregate diff of PR #11:
13 files, +434 / −4.

## 2. Auditor and independence

Same auditor as round 1: GitHub account `Laegel`, absent from the commit history
and from `GET /repos/hoklims/latent-compass/contributors`, with
`{admin: false, maintain: false, push: false, triage: false, pull: true}` on this
repository — a push returns `403`. Read-only is enforced by GitHub, not by
promise. Distinct harness, account, environment and evidence store.

One constraint is **not** met, and the receipt says so rather than asserting it
away: `first_pass_before_author_narrative` is `false`. The author's two comments
on PR #6 were relayed to this session as the task assignment. They enumerate the
round-2 fixes and conclude that they work, and were read before the round-2 first
pass was formed. Every finding in §4 and §5 was reached from the candidate's
source and its public CI, and §5 contradicts rather than follows that narrative —
but the ordering criterion is about ordering, and it was not met.

This is finding **R3**. It is an auditor-side defect, not a candidate defect, and
it is the reason the gate blocks first.

## 3. What reproduces

Independent Linux run at `d5ae2362…`, clean tree, cold cache:

| Command | rc | Output |
| --- | :-: | --- |
| `uv run --frozen ruff format --check .` | 0 | `154 files already formatted` |
| `uv run --frozen ruff check .` | 0 | `All checks passed!` |
| `uv run --frozen mypy` | 0 | `Success: no issues found in 110 source files` |
| `uv run --frozen pytest -o addopts='' -q` | 0 | `1410 passed, 33 skipped` |
| `uv run --frozen python tools/verify_sdist.py dist/latent_compass-0.1.0.tar.gz` | 0 | `1410 passed, 33 skipped` |
| `pytest tests/test_lab_host_bench.py` **with `pyright@1.1.414` installed** | 0 | `42 passed` (0 skipped) |

CI run [35522059653](https://github.com/hoklims/latent-compass/actions/runs/35522059653)
at `24911ba2…` concludes `success` on both legs, with `Test extracted source
distribution` succeeding on each: ubuntu `1417 passed, 26 skipped`, windows
`1440 passed, 3 skipped`.

## 4. Round-1 findings: all remediations verified

| # | Round-1 finding | Status | How I checked it |
| :-: | --- | --- | --- |
| F1 | published sdist failed six of its own tests | **fixed** | `tools/verify_sdist.py` on a freshly built sdist exits 0 with `1410 passed`. The six `test_the_committed_corpus_regenerates_byte_for_byte` failures are gone. Witness W3 shows the check is load-bearing. |
| F2 | artifacts not reproducible, not built by CI | **fixed for future tags, honestly documented for `v0.1.0`** | `.github/workflows/release.yml` builds on `ubuntu-latest` from the tag, runs the sdist test, attests with `actions/attest-build-provenance@v3.2.0`, then uploads. `README.md` states plainly that the `v0.1.0` assets were built from a Windows working tree and are not claimed as reproducible or attested. See N3 below. |
| F3 | pyright real-server paths skipped on both CI legs | **fixed** | `verify.yml` installs `pyright@1.1.414`. CI skips drop 33→26 (ubuntu) and 10→3 (windows): exactly seven tests now execute on each leg. Locally, with pyright installed, `test_lab_host_bench.py` goes from 7 skipped to `42 passed`. Witness W4 shows those paths carry live oracles. |
| F4 | `HANDOFF.md`'s `1 433/3` reproduced nowhere | **fixed** | The figure is retracted in place as non-reproducible, and the real CI numbers (`1403/33` ubuntu, `1426/10` windows) are recorded. Both match what I measured independently in round 1. |
| F5 | `v0.1.0` is an unsigned lightweight tag | **explicitly historical** | Unchanged and documented as such, as issue #5 asks. |
| F6 | CI never exercised a built distribution | **fixed** | `verify.yml` and `release.yml` both run `tools/verify_sdist.py`; both legs of run 35522059653 show the step succeeding. |

`.gitattributes` (`* text=auto eol=lf`, CRLF for `*.bat`/`*.cmd`) and the
`newline="\n"` writes in `tests/corpus_generator.py` are the mechanism behind F1.

## 5. Blocking findings in the new proof mechanism

### R1 — the gate accepts a wholly fabricated epoch and answers `ALLOW`

`gate()` never recomputes `epoch_digest` from the epoch's own contents, never
re-derives the epoch from the repository, and never compares `head_sha` to
anything real. It checks only that the receipt *repeats* the three values the
epoch file happens to contain.

`forged-epoch-demo/` contains an epoch naming commit `ffff…ffff`, with
`"epoch_digest": "sha256:not-a-real-epoch-digest"`, a policy digest matching no
policy, an empty `files` array, and a repository URL pointing nowhere; plus a
receipt echoing those three values, with witness digests that are the literal
strings `"x"` and `"y"`. Result:

```console
$ python tools/independent_audit.py gate \
    --epoch forged-epoch-demo/forged-epoch.json \
    --receipt forged-epoch-demo/forged-receipt.json
{
  "schema": "hoklims/latent-compass:independent-audit/2",
  "decision": "ALLOW",
  "epoch_digest": "sha256:not-a-real-epoch-digest",
  "head_sha": "ffffffffffffffffffffffffffffffffffffffff"
}
$ echo $?
0
```

Nothing ties `ALLOW` to the candidate, to the policy, or to any executed
evidence. The shipped test suite encodes this as intended behaviour:
`tests/test_independent_audit.py::_epoch` returns `{"epoch_digest":
"sha256:epoch", "head_sha": "aaaa…"}` and `test_gate_allows_an_adequate_receipt`
asserts `ALLOW` on it.

**Bounded fix.** In `gate()`, recompute
`_digest(_canonical({k: v for k, v in epoch.items() if k != "epoch_digest"}))`
and compare it to `epoch["epoch_digest"]`; add a fail-closed test that a mutated
epoch field is rejected. Optionally re-derive the epoch from the repository at
gate time and compare `head_sha` and `policy_digest`.

### R2 — `epoch_digest` is not reproducible between honest auditors

`create_epoch` puts `git config --get remote.origin.url` into the digested
material. The same base and head in two clones of the same repository give:

| clone's `origin` | `policy_digest` | `epoch_digest` |
| --- | --- | --- |
| `https://github.com/hoklims/latent-compass.git` | `sha256:9c9e5531…` | `sha256:9f4a5dae…` |
| `git@github.com:hoklims/latent-compass.git` | `sha256:9c9e5531…` | `sha256:9eca0174…` |

Regenerating the epoch and comparing digests is the *only* way a third party can
detect an R1 forgery. R2 makes that comparison unreliable, so the two findings
compound: the epoch binding is neither self-verified nor externally checkable.

**Bounded fix.** Drop `repository` from the digested material, or normalise it to
a canonical form before digesting.

### R3 — the auditor's first-pass ordering (see §2)

**Bounded fix.** Assign round 3 to a fresh session whose task text carries the
candidate SHA, the base SHA and the protocol path, and no account of what was
fixed or why.

## 6. Claims matrix — red/green witnesses

Each witness mutates one thing in a disposable checkout, runs the named command,
is reverted, and the command is run again. Machine-readable in
`witnesses/claims.json` (with SHA-256 digests of both outputs, as the protocol
requires); trimmed outputs in `witnesses/`; driver in `witnesses.py`.

| # | Claim | Command | Red (rc 1) | Green (rc 0) |
| :-: | --- | --- | --- | --- |
| W1 | the public gate refuses a receipt carrying unresolved blockers | `pytest tests/test_independent_audit.py` | 1 failed, 6 passed | 7 passed |
| W2 | the public gate refuses any verdict weaker than `PROOF_ADEQUATE` | `pytest tests/test_independent_audit.py` | 1 failed, 6 passed | 7 passed |
| W3 | the CI sdist step catches a reintroduced line-ending regression (F1/F6) | `uv build && tools/verify_sdist.py` | 6 failed, 1411 passed, 26 skipped | 1417 passed, 26 skipped |
| W4 | the real language-server paths re-enabled by round 2 carry live oracles (F3) | `pytest tests/test_lab_host_bench.py` | 1 failed, 41 passed | 42 passed |

W3's red is precisely the round-1 F1 failure set, now caught by the step that
did not exist before. W4 re-reads the file from disk instead of using the bytes
handed to the server, and `test_the_language_server_answers_about_the_bytes_it_is_handed_not_the_file_on_disk`
catches it — so the seven newly-executed tests assert behaviour, not merely that
a server starts.

The gate itself blocking on each of its three conditions is reproduced directly:

```console
receipt.json                                   BLOCK  first_pass_before_author_narrative must be true
  + first_pass set true                        BLOCK  unresolved_blockers must be an empty array
  + unresolved_blockers emptied                BLOCK  verdict must be PROOF_ADEQUATE
```

## 7. Non-blocking findings

**N1 — the gate has no type coverage.** `pyproject.toml` sets
`files = ["src", "tests", "examples"]`, so `tools/` is outside mypy. Ruff lints it
(`ruff check .` walks the tree) but the code that issues `ALLOW` is the only
executable surface in the repository with no type gate. Adding `"tools"` to that
list is a one-word change.

**N2 — a deleted path and an empty file are indistinguishable in the epoch.**
`create_epoch` records `_digest(b"")` for a path that `git show` cannot resolve,
which is the same digest a genuinely empty file gets. Recording an explicit
`"deleted": true` would remove the collision.

**N3 — `release.yml` has never run.** No tag has been pushed since it landed
(`gh run list --workflow release.yml` is empty), so F2's fix is verified by
reading, not by observation. The first `v*` tag push is what will actually
demonstrate it.

**N4 — half of the corpus line-ending fix is dead code.** In
`tests/corpus_generator.py::write`, the first loop writes the three split files
with `newline="\n"`, then the second loop rewrites **all six** files, splits
included, from `generate(target)`. Only the second `write_text` is load-bearing;
mutating the first alone changes no byte on disk. Harmless, but the redundancy
hides which call the guarantee rests on.

## 8. Next bounded step

R1 and R2 are one small commit in `tools/independent_audit.py` plus two
fail-closed tests. R3 is a handoff detail, not a code change. With those three
closed, nothing I found in this pass stands between the candidate and an
`ALLOW` — the remediation work itself is sound and independently verified.

## 9. What this does not establish

Out of scope per issue #5: comparative quality, empirical non-inferiority, pilot
readiness, hook execution authority, index removal.

No secrets, prompts, tool arguments or private local paths are published.
Witness outputs are reduced to test identifiers and summary lines; the SHA-256
digests in `receipt.json` are over the full untrimmed outputs.

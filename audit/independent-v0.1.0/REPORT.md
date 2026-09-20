# Independent audit of v0.1.0 — `f652690f29420b5d4ae4f37b22caa5e98df102a2`

Answers issue [#5](https://github.com/hoklims/latent-compass/issues/5).

**Decision: `BLOCK`. Verdict: `PROOF_WEAK`.** The candidate keeps the status it
was released with. Issue #5 explicitly admits a blocking result as a valid one;
this is that result, with the actionable findings and the next bounded fix.

The deterministic gate was **not run**, because no deterministic gate, no
schema-v3 specification and no active proof policy is reachable from outside the
author's private tooling. Acceptance criteria 6 and 7 are therefore unsatisfiable
by any independent auditor as issue #5 is currently written. That is finding B1,
and it is the first thing to fix.

Machine-readable form of everything below: [`receipt.json`](receipt.json).
Re-run it yourself: [`reproduce.sh`](reproduce.sh).

## 1. Auditor, independence and read-only constraints

The auditor is an agent session (Claude Opus 5) started from the text of issue #5
alone, in a harness instance distinct from the one that produced the candidate,
with no memory of the authoring work, driving the GitHub account `Laegel`.

All 48 commits in the candidate's history carry a single author identity, and
`GET /repos/hoklims/latent-compass/contributors` lists exactly one contributor,
`hoklims`. The auditor account appears in neither.

| Constraint (issue #5) | Held | Evidence / defect |
| --- | :-: | --- |
| Did not contribute to the candidate | yes | Not in the commit history, not in the contributor list. |
| Does not modify the candidate | yes | Enforced by GitHub, not by policy: the auditor's permissions on the repository are `{admin: false, maintain: false, push: false, triage: false, pull: true}`, and a push to it returns `403 Permission to hoklims/latent-compass.git denied to Laegel`. |
| Fresh, unforked session without author memory | yes | Started from the issue text. |
| Read-only on the frozen candidate | yes | Candidate obtained by a fresh `git clone` of the public repository, detached at the frozen SHA. Mutants were applied only to that ephemeral working tree, reverted by `git checkout --`, byte-equality re-asserted, and `git status --porcelain` is empty at `f652690`. |
| Distinct harness | yes | Different harness instance from the authoring sessions. |
| Distinct account | yes | `Laegel`, distinct from `hoklims`, with read-only access as above. |
| Distinct environment | yes | The auditor's own Linux workstation, with a fresh project venv and the `uv` cache and Python install directory redirected to a scratch directory. |
| Distinct evidence storage | yes | Evidence is produced and retained under the auditor's own account and reaches the candidate repository only as a pull request the author may accept or refuse. |
| **First pass before the author's narrative** | **partial** | During the initial search for the gate vocabulary, a `grep` returned three lines of `HANDOFF.md` (lines 38, 40 and 42 — the author's narrative and prior verdicts) before the first pass was complete. Disclosed rather than concealed. |
| Signals candidate/policy/environment divergence invalidating the epoch | yes | §2, §6 and §7. |

Eight of the nine constraints hold and are externally checkable. The ninth,
first-pass ordering, is partially violated and is recorded as finding B3.

## 2. Epoch

Fresh epoch, targeting exactly:

- repository `hoklims/latent-compass`, tag `v0.1.0`
- commit `f652690f29420b5d4ae4f37b22caa5e98df102a2`, tree `f913208ad377cea5652a95a0603665b5c7a90804`
- active proof policy: **`UNRESOLVED` / `MISSING_INPUT`**

The policy digest required by criterion 6 could not be computed, because the
policy is not published. The epoch is therefore incomplete by construction.

## 3. First-pass inputs

| # | Input | Status |
| :-: | --- | --- |
| 1 | Aggregate candidate diff | available — 187 files, +80 949 / −2 against the initial commit, 48 commits |
| 2 | Acceptance contract and release invariants | available — `docs/authority-boundary.md` (contract `2.0.0`), `README.md`, release notes |
| 3 | **Proof-surface classifier output** | **`MISSING_INPUT`** — the classifier belongs to the unreachable active policy |
| 4 | Raw commands, outputs, artifact identities | available — reproduced below and in `witnesses/` |
| 5 | Representative red/green witnesses | none shipped with the candidate; the auditor produced six (§5) |

## 4. What reproduces, exactly

Independent Linux run at the frozen SHA, cold cache, fresh venv:

| Command | rc | Output |
| --- | :-: | --- |
| `uv sync --locked --all-groups` | 0 | 19 packages |
| `uv run --frozen ruff format --check .` | 0 | `149 files already formatted` |
| `uv run --frozen ruff check .` | 0 | `All checks passed!` |
| `uv run --frozen mypy` | 0 | `Success: no issues found in 109 source files` |
| `uv run --frozen pytest -o addopts='' -q` | 0 | `1403 passed, 33 skipped` |

CI run [35512148649](https://github.com/hoklims/latent-compass/actions/runs/35512148649)
is bound to the exact head SHA and both legs conclude `success`: ubuntu-latest
`1403 passed, 33 skipped`, windows-latest `1426 passed, 10 skipped`. The
independent Linux run matches the ubuntu leg exactly.

The auditor ran `uv` 0.11.26; CI pins 0.12.5. The lock still resolved under
`--locked`, so the divergence did not change the closure, but it is recorded.

## 5. Claims matrix — high-impact invariants, with negative witnesses

Each mutant breaks one invariant of `docs/authority-boundary.md` in the working
tree, runs the full suite, is reverted, and the suite is re-run. Red first, then
green. Exact return codes and failing test names in [`witnesses/`](witnesses/); the
driver that produced them is [`mutants.py`](mutants.py), shipped with the same
mutation strings that were executed.

| # | Claim | Application point | Oracle | Red (rc 1) | Green (rc 0) |
| :-: | --- | --- | --- | --- | --- |
| M1 | `authorize_transition` refuses `LATENT_COMPASS` unconditionally and table-independently | `authority.py::authorize_transition` | `test_authority.py`, `test_cli.py` | 10 failed, 1393 passed | 1403 passed |
| M2 | The enforcement tables are read-only mappings | `authority.py::ALLOWED_TRANSITIONS` | `test_authority.py` | 2 failed, 1401 passed | 1403 passed |
| M3 | Evidence-bearing advancement is refused without trusted external attestation | `authority.py::authorize_transition` | `test_authority.py` | 15 failed, 1388 passed | 1403 passed |
| M4 | `plan_confined_target` refuses targets outside the named root | `confined_io.py::plan_confined_target` | `test_cli.py`, `test_lab_observations.py` | 8 failed, 1395 passed | 1403 passed |
| M5 | The episode ledger refuses a duplicate `episode_id` | `ledger.py`, `episodes` schema | `test_ledger.py` | 14 failed, 1389 passed | 1403 passed |
| M6 | No module under `src/` imports `subprocess` | `canonical.py` | `test_lab_boundary.py` | 2 failed, 1401 passed | 1403 passed |

All six invariants carry real oracles. The refusal in M1 fails a test that first
widens the private capability grant and asserts the widening took effect — so the
property proved is the table-independent one the contract claims, not merely
"the capability check runs first". Provenance in M3 fails closed as documented.
33 skips in every green run, all environmental; none is a silenced failure.

**This is the candidate's strongest result, and it is genuine.** The findings
below are about what surrounds the code, not about these invariants.

## 6. Blocking findings

**B1 — The gate, the schema and the policy are unreachable, so criteria 6 and 7
cannot be met by anyone independent.** `schema-v3`, `PROOF_ADEQUATE` as a receipt
form, the proof-surface classifier and the deterministic gate exist only in the
author's out-of-repository tooling (`HANDOFF.md` records the reports as living in
`proof-<sha>/` folders outside the repository, and the policy classifier as out of
repository). Nothing public defines the receipt schema or the policy digest.
Issue #5 as written can only be closed by the author's own private gate, which is
exactly the dependency the issue exists to remove.

**B2 — First-pass ordering was partially violated** (§1): three lines of the
author's narrative and prior verdicts were read during reconnaissance, before the
first pass closed. The findings in §5 and §7 were all reached from the candidate
and its public artifacts, not from that text, but the ordering criterion is not
met as written.

B1 alone is determinative: with no obtainable gate, no independent audit of this
candidate can produce `PROOF_ADEQUATE/ALLOW`, however independent the auditor.

## 7. Material findings in the candidate

**F1 — The published sdist fails six of its own tests.** Running the shipped
suite from `latent_compass-0.1.0.tar.gz` exits `rc=1`:

```
6 failed, 1397 passed, 33 skipped
FAILED tests/test_synthetic_corpus.py::test_the_committed_corpus_regenerates_byte_for_byte[train.json]
      ... [validation.json] [holdout.json] [manifest.json] [protocol.json] [spec.json]
AssertionError: spec.json no longer reproduces from tests/corpus_generator.py
assert b'{\n  "basel...1e70bd9"\n}\n' == b'{\r\n  "bas...bd9"\r\n}\r\n'
```

`pyproject.toml` ships the corpus in the sdist for precisely this test. The
shipped corpus has CRLF line endings; the generator writes LF. A distribution
rebuilt from the commit on Linux passes the same six tests (`12 passed`), so the
defect is in the published artifact, not in the test.

**F2 — The release artifacts are not reproducible from the commit and were not
produced by the green CI.** `uv build --no-sources` at `f652690` on Linux is
byte-deterministic (two runs, identical digests — hatchling pins zip timestamps):

| | wheel sha256 | sdist sha256 |
| --- | --- | --- |
| rebuilt from the commit | `3cb70cce…f783` | `d8ac92dd…c189` |
| published on the release | `db01bac6…5617` | `5e976a8b…26db` |

The published payloads are identical to the commit's tree *after CRLF→LF
normalisation* — 57/57 wheel payload files and 185/186 sdist files are
CRLF-converted, `py.typed` included. That is a Windows working tree, not the CI
recipe: `verify.yml` builds distributions but never uploads them, so what the
public downloads was built on an operator workstation and is bound to the commit
by nothing verifiable. No foreign code is present — the content matches — but the
release notes' "source exacte : `f652690…`" and "artefacts GitHub vérifiés"
overstate what a third party can check. The published SHA-256 values are correct
and match the downloads; they attest upload integrity, not build provenance.

**F3 — The real language-server paths have zero executed coverage anywhere.**
Seven tests in `tests/test_lab_host_bench.py` skip with *"no pyright-langserver on
this host: the real-server paths cannot run here"*. `verify.yml` never installs
`pyright-langserver`, and it is not in `uv.lock`, so those seven skip on both CI
legs (windows: 10 skipped = 7 + 3). A green CI asserts nothing about them.

**F4 — The author's recorded local figures do not reproduce.** `HANDOFF.md:38`
reports "1 433 tests réussis et 3 cas de plateforme ignorés". Neither CI leg nor
the independent run produces that split: ubuntu and this audit both give
`1403 passed, 33 skipped`, windows gives `1426 passed, 10 skipped`. The totals
agree (1436); the pass/skip split does not. A verification figure that matches no
reproducible run should not stand in the handoff.

**F5 — The release anchor is unsigned and movable.** `v0.1.0` is a lightweight
tag (a `commit` object, not an annotated tag), so it carries no tagger, no
signature and can be moved by force-push. 43 of 48 commits are unsigned; the
remaining 5 are GitHub merge commits whose signature could not be verified
locally. Every claim in this audit is pinned to the SHA, not to the tag, for that
reason.

**F6 — CI never exercises a built distribution.** The wheel step runs
`latent-compass shadow --help` outside the checkout and nothing more. Running the
suite from the built sdist would have caught F1 before the release.

## 8. Next bounded fix

One change closes F1, F2 and F6 together:

1. Add a `.gitattributes` with `* text=auto eol=lf` (and set `core.autocrlf=input`
   on the release workstation) so no build can produce CRLF payloads.
2. Add a release job to `verify.yml` that builds on `ubuntu-latest` from the tag,
   runs `pytest` **from the extracted sdist**, and uploads those exact files as
   the release assets.
3. Attach build provenance (`actions/attest-build-provenance`) so the assets are
   verifiably bound to the run and the commit.

Then F5 becomes addressable by re-cutting `v0.1.0` as an annotated, signed tag.

F3 and F4 are separate and smaller: either install `pyright-langserver` in CI so
the seven tests run, or state in the release notes that those paths are untested;
and correct or remove the `1433/3` figure in `HANDOFF.md`.

None of this touches the six verified invariants in §5.

## 9. What this audit does not establish

Out of scope per issue #5 and untouched here: comparative quality or empirical
non-inferiority, pilot readiness, index removal, hook execution authority, Codex
trust via `/hooks`.

No prompts, tool arguments, private results, local paths, tokens or secrets are
published. Witness logs are reduced to test identifiers and summary lines.

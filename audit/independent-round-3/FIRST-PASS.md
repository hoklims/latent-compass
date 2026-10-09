# First-pass findings — `hoklims/latent-compass` `f652690...` → `45078dc...`

Written **before** reading `HANDOFF.md`, any `audit/` directory, any pull-request
or issue text, or the proof-related sections of either README. Inputs used for
this pass, and nothing else:

- `git diff f652690...45078dc` restricted to `.gitattributes`, `.github/`,
  `pyproject.toml`, `tests/`, `tools/`, `docs/` (the `HANDOFF.md` and
  `README*.md` hunks were deliberately excluded from the diff I read)
- `docs/independent-audit.md`
- `tools/independent_audit.py`, `tools/verify_sdist.py`
- `tests/test_independent_audit.py`
- `.github/workflows/verify.yml`, `.github/workflows/release.yml`
- the repository's own test suite, run at head

## Disclosure

I have not read any of the excluded material at the time of writing. I did
observe two facts about excluded files that are unavoidable from the allowed
inputs and that I did not act on: `HANDOFF.md`, `README.md` and `README.fr.md`
appear in `git diff --stat` and therefore in the epoch's file inventory. I read
neither their contents nor their diffs.

## Shape of the change

13 files, +512/−4. Substantively:

1. A new public audit protocol: `docs/independent-audit.md`,
   `tools/independent_audit.py` (epoch generator + gate), and
   `tests/test_independent_audit.py` (9 tests over the gate).
2. `tools/` packaged: `tools/__init__.py`, `/tools` added to the sdist include
   list, `tools` added to mypy's `files`.
3. `tools/verify_sdist.py` plus a new CI/release step that extracts the built
   sdist and runs its shipped test suite against its shipped source.
4. A new `release.yml` (tag-triggered, SHA-pinned actions, build-provenance
   attestation over `dist/*`, upload to a GitHub release).
5. `npm install --global pyright@1.1.414` added to `verify.yml` so the
   host-bench tests exercise a real language server.
6. Line-ending hygiene: `.gitattributes` (`* text=auto eol=lf`) and
   `newline="\n"` in `tests/corpus_generator.py::write`.

## Baseline

At head, in a clean clone with the locked environment:

- `uv sync --locked --all-groups` → exit 0
- `uv run --frozen ruff format --check .` → exit 0 (`154 files already formatted`)
- `uv run --frozen ruff check .` → exit 0
- `uv run --frozen mypy` → exit 0 (`113 source files`)
- `uv run --frozen pytest -o addopts='' -q` → exit 0, `1412 passed, 33 skipped in 102.50s`

## Findings

### B1 (blocking) — the gate's `ALLOW` is not bound to any real repository

`gate(epoch, receipt)` in `tools/independent_audit.py` takes two JSON objects
and never touches Git, the filesystem, or the candidate. Its "verification" of
the epoch is:

- `base_sha` / `head_sha` / `head_tree` match `[0-9a-f]{40}` — a *shape* check,
  not an existence check;
- `policy_digest` matches `sha256:[0-9a-f]{64}` — again shape only;
- `files` is a non-empty list of well-shaped, uniquely-and-sortedly-pathed
  entries;
- `epoch_digest == _epoch_digest(epoch)` — self-consistency of the object with
  itself.

Nothing ties any of it to `hoklims/latent-compass`. I constructed an epoch whose
`base_sha`/`head_sha`/`head_tree` are `000…1`, `000…2`, `000…3` (no such objects
exist in any repository), whose `policy_digest` is `sha256:000…0`, and whose
inventory is a single invented path, paired with a receipt whose only claim is
literally `"no audit work was performed at all"`:

```
$ python tools/independent_audit.py gate --epoch w1-forged-epoch.json --receipt w1-forged-receipt.json
{ "schema": "...", "decision": "ALLOW", "epoch_digest": "sha256:29e627ba…", "head_sha": "0000000000000000000000000000000000000002" }
exit=0
```

An `ALLOW` therefore certifies only that a JSON document was well-formed. It is
not evidence about the candidate.

### B2 (blocking) — the documentation describes checks the gate does not perform

`docs/independent-audit.md` states:

> The gate recomputes the canonical epoch digest and validates its Git
> identities and complete changed file inventory before considering the receipt.

Only the first clause is true. "Validates its Git identities" is a 40-hex regex.
"Complete changed file inventory" is not checked at all. Witness: I removed the
`tools/independent_audit.py` entry — the gate's own source file — from the
inventory of the real epoch, recomputed `epoch_digest`, and gated it:

```
$ python tools/independent_audit.py gate --epoch w3-truncated-epoch.json --receipt w3-truncated-receipt.json
"decision": "ALLOW"
exit=0
```

So an epoch can hide the very change under audit and still be allowed.

### B3 (blocking) — JSON booleans pass the exit-code checks

```python
if not isinstance(witness.get("red_exit"), int) or witness["red_exit"] == 0:
    raise AuditError(...)
if witness.get("green_exit") != 0:
    raise AuditError(...)
```

`bool` is a subclass of `int` in Python, `True != 0`, and `False == 0`. A
receipt carrying `"red_exit": true, "green_exit": false` — which records no exit
codes at all — is accepted. Confirmed against the *real* epoch for this
candidate: exit 0, `"decision": "ALLOW"`. The documentation asks for "a non-zero
integer `red_exit`, a zero integer `green_exit`"; the implementation does not
enforce that.

Bounded fix: `type(x) is int` (or `isinstance(x, int) and not isinstance(x, bool)`)
for both fields, and require `green_exit` to be an `int` too.

### B4 (blocking) — the epoch is not a pure function of `(base, head)`

`create_epoch` derives `files[]` digests from `git show <head_sha>:<path>`, but
`_policy_digest` reads the three `POLICY_FILES` from the **working tree**
(`path.read_bytes()`). With an uncommitted edit to `docs/independent-audit.md`
and otherwise identical arguments:

| | clean tree | dirty tree |
|---|---|---|
| `head_sha` / `head_tree` | identical | identical |
| `policy_digest` | `sha256:22575e65…` | `sha256:9931a657…` |
| `epoch_digest` | `sha256:8d53dfe9…` | `sha256:1d3aae28…` |

The dirty epoch is additionally self-contradictory: its `files[]` entry for
`docs/independent-audit.md` still carries the *committed* blob digest
(`sha256:2d3f09e6…`) while `policy_digest` covers the *edited* bytes. The gate
cannot detect this, because it never recomputes either value.

Two auditors at the same head can therefore produce different canonical epoch
digests, and neither the gate nor a reader can tell which is canonical.

Bounded fix: read the policy files with `git show <head_sha>:<path>` like every
other inventory entry, and have `gate` accept a `--repository` so it can
recompute `create_epoch(...)` and compare it to the supplied epoch.

### B5 (blocking for the *enforcement* claim) — nothing invokes the gate

```
$ grep -rn independent_audit .github
exit=1   (no output)
```

Neither `verify.yml` nor `release.yml` references `tools/independent_audit.py`.
The gate is an out-of-band command a human must choose to run; no CI status, no
branch protection, and no release step depends on a receipt existing. The
protocol document does not claim CI enforcement, so this is a gap between "a
gate exists" and "a gate constrains anything", not a documentation lie — but it
is the difference between a proof mechanism and a formatter.

### What does hold

Three claims survive adversarial testing with real red/green pairs:

1. **The gate rejects an epoch whose `epoch_digest` no longer matches its
   contents.** Tamper `head_sha` without recomputing → exit 1,
   `{"decision":"BLOCK","error":"epoch_digest does not match the canonical epoch contents"}`;
   canonical epoch → exit 0. This is genuine, just much narrower than advertised.
2. **`tests/test_independent_audit.py` is load-bearing.** Deleting the
   `unresolved_blockers` check from `gate` → `1 failed, 8 passed`, exit 1;
   reverted → exit 0.
3. **The sdist really is self-testing.** Removing `"/tools"` from
   `[tool.hatch.build.targets.sdist].include` → `uv build` succeeds but
   `tools/verify_sdist.py` exits 2 with a collection error on
   `tests/test_independent_audit.py`; reverted → `1412 passed, 33 skipped`,
   exit 0. The new `/tools` include and the new sdist step are coupled and both
   necessary.

**Epoch reproducibility (clean tree) is good.** Generating the epoch from a
second, independently-fetched clone at a different filesystem path produced a
byte-identical `epoch.json`. `repository` is a hardcoded logical constant, so
HTTPS vs SSH remotes do not perturb it, as documented.

### Non-blocking observations

- `verify.yml` hardcodes `dist/latent_compass-0.1.0.tar.gz` and
  `dist/latent_compass-0.1.0-py3-none-any.whl` while `release.yml` globs. A
  version bump silently breaks the Verify job.
- `release.yml` is triggered directly by a tag push and runs the extracted-sdist
  suite but not `ruff`/`mypy`, and does not depend on a successful `Verify` run
  for the same commit. Nothing prevents tagging a commit that never passed
  Verify.
- The epoch path check rejects `/…` and `../…` prefixes but not embedded
  `a/../../b`. Irrelevant today because the gate never touches the filesystem;
  it becomes relevant the moment B1/B2's fix makes the gate read files.
- `npm install --global pyright@1.1.414` is a floating network dependency in CI;
  pinned by version, not by integrity hash.
- The line-ending work (`.gitattributes`, `newline="\n"` in
  `tests/corpus_generator.py`) is **not witnessable on Linux**: `write_text`'s
  default `newline=None` already emits `\n` on POSIX, so the mutation is a no-op
  here. `tests/test_synthetic_corpus.py::test_the_committed_corpus_regenerates_byte_for_byte`
  is the test it protects, on the `windows-latest` matrix leg only. I record
  this as plausible and untested rather than verified.
- The protocol has no representation for an honest negative receipt: the only
  receipt shape the schema permits is an approving one, and a blocking outcome
  exists only as "the gate exited 1". An auditor reporting blockers cannot
  produce a *valid* receipt stating so — which is what this audit is about to do.

## First-pass verdict

`PROOF_INADEQUATE`. The mechanism that issues the verdict does not verify the
thing it is verdicting on. B1–B4 are defects in the gate itself; B2/B4 are also
mismatches between `docs/independent-audit.md` and the implementation. The
surrounding release and sdist work is real and independently witnessed.

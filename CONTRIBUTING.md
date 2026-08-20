# Contributing

Thanks for looking. This project is small and its value is that its guarantees
are checkable, so contributions are judged mostly on whether they keep them
checkable.

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hoklims/latent-compass
cd latent-compass
uv sync --all-groups
```

## The gate

Run all of it before opening a pull request:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q && uv build
```

A green gate is necessary and not sufficient. `ruff` and `mypy` prove nothing
about behaviour; only the tests do.

## How to write a test here

The house style is a **hostile** test: one that would pass against a plausibly
wrong implementation is not worth adding.

- **Prove both halves.** When asserting that an actor is refused something,
  also assert that a capable actor is granted the same thing. Otherwise an
  implementation that refuses everything passes.
- **Never let a scan pass vacuously.** A "the source contains no X" test must
  first assert that it actually found the source files.
- **Prove the trap is live.** When a test monkeypatches something to forbid it,
  call it directly first and assert it raises.
- **Prove atomicity at the boundary, not by call count.** The interrupted-append
  test observes the row *inside* the transaction before interrupting, so an
  implementation that validated and returned early fails rather than passes.
- **Prove concurrency with processes.** The holdout tests launch real operating
  -system processes. A thread proves nothing about the cross-process guarantee,
  and a mock proves nothing at all.
- **Prove absence, not annotation.** A redaction test asserts the value is gone,
  never that a marker is present.
- **Assert exact sets, not subsets,** when the point is exhaustiveness — a
  capability grant, a command list, a set of integrity findings.
- **Assert what did not change.** After a refusal: unchanged root seal,
  unchanged count, still-verifying store.

Name tests as sentences: `test_a_duplicate_episode_is_refused_and_changes_nothing`.

## Changes that cost more

- **Anything touching the authority boundary, the episode contract, the
  evaluation protocol or the ledger format** needs an ADR in `docs/adr/` and a
  contract-version bump. See `GOVERNANCE.md` and
  [ADR 0002](docs/adr/0002-verified-evidence-and-durable-anchors.md) for what a
  corrective tranche looks like.
- **Anything that would let an authorisation rest on a caller's assertion.** The
  evidence artefacts are verified here, by recomputing their seals. A parameter
  that accepts a conclusion instead of the artefact behind it will be refused.
- **A new dependency** needs a demonstrated need recorded in an ADR and a
  verified licence compatible with Apache-2.0. See
  `docs/licenses/dependency-audit.md`. The default answer is no; the standard
  library is usually enough here.
- **Weakening a test, a contract or a boundary to get a green gate** will be
  refused. If a guarantee is wrong, change the guarantee explicitly, with an
  ADR — do not soften the check that enforces it.

## Documentation

If behaviour changes, the document describing it changes in the same pull
request. `README.md` sorts everything into demonstrated, experimental or
projected — put your change in the right one, and do not promote something to
"demonstrated" without a test in this repository.

Do not add badges. None has been earned by a public reproducible run.

Do not describe this foundation as emitting, ranking, vectorising or calibrating
anything. It validates and records advisories supplied to it. Emission is
HOK-182 and does not exist yet; the README, the docs and the package metadata
all say so, and they must keep saying so until it does.

## Reporting bugs

Include the version or commit, the exact command, the full JSON error document
including its `error` code, and the smallest reproduction you have. Never
include credentials, personal data or a real ledger store — a synthetic
reproduction is always sufficient.

For anything security-relevant, follow `SECURITY.md` instead of opening a
public issue.

## Licence and conduct

Contributions are accepted under Apache-2.0 (see `LICENSE`). By opening a pull
request you agree your contribution is licensed that way. Participation is
governed by `CODE_OF_CONDUCT.md`.

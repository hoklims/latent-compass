# ADR 0001 — The minimal durable stack

- **Status**: accepted
- **Date**: 2026-08-14
- **Scope**: HOK-181 foundation (HOK-184, HOK-185, HOK-186, HOK-189, HOK-187)

## Context

The foundation must be functional, tested, documented and installable in a
clean environment. It must also stay legible to a reviewer who has never seen
the repository, because the product *is* the boundary: if the contracts cannot
be read and checked, they are not contracts.

Every dependency is a claim that the problem is harder than the standard
library. Each one added here would have to be audited for licence, carried into
the wheel, and understood by anyone verifying the authority boundary.

## Decision

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.13 | `StrEnum`, modern typing, `datetime.UTC`; pinned `>=3.13,<3.14` so the runtime is a checked fact |
| Packaging | `pyproject.toml` + `hatchling` | PEP 517/621, builds both wheel and sdist, no plugin needed for a `src/` layout, MIT-licensed |
| Environment | `uv` with `uv.lock` | reproducible resolution; PEP 735 dependency groups keep dev tools out of the wheel |
| Contracts | `pydantic>=2,<3` | the one runtime dependency; strict validation, unknown-field rejection and structured errors are exactly the fail-closed posture, and hand-rolling them would be more code and less trustworthy |
| Storage | `sqlite3` (standard library) | single-file store, real transactions, `STRICT` tables, `UNIQUE` constraints; no server, no driver |
| CLI | `argparse` (standard library) | subcommands, `choices` validation and generated help are enough; a CLI framework would be a dependency for ergonomics |
| Serialisation, paths, time, seals | `json`, `pathlib`, `datetime`, `hashlib` | canonical JSON and SHA-256 need nothing more |
| Dev tooling | `pytest`, `ruff`, `mypy --strict` | test, lint/format, typecheck; none ships in the wheel |

The runtime dependency closure is `pydantic`, `pydantic-core`,
`annotated-types`, `typing-extensions`, `typing-inspection` — all permissive
(MIT, PSF-2.0) and compatible with Apache-2.0 redistribution. See
`docs/licenses/dependency-audit.md`.

## Explicitly refused

Refused for this slice, and the refusal is the decision:

- **NumPy, pandas, scikit-learn, notebooks** — nothing here computes statistics
  beyond a median over at most 64 seeds. `statistics.median` is sufficient and
  exact. These belong to a later slice that actually models something.
- **Typer, Click, Rich** — ergonomics, not capability.
- **An ORM** — the schema is three tables. An ORM would obscure the very
  `BEGIN IMMEDIATE` boundary the atomicity proof depends on.
- **A migration framework** — there is one ledger format. Unknown formats are
  refused rather than migrated, by design: implicit migration is how history
  gets silently rewritten.
- **A multi-backend storage abstraction** — a store is local, single-host and
  operator-owned. An abstraction over a second backend that does not exist
  would be speculative structure.
- **An event bus or plugin system** — extension points are also injection
  points, and this package's whole value is that its outputs cannot become
  actions.
- **Any HTTP client, socket use or subprocess** — the package must reach
  nothing. A test asserts the source tree imports none of them, and another
  runs the full CLI with those primitives trapped.

## Consequences

**Good.** The wheel carries one dependency chain, entirely permissive. The
storage and transaction boundary is visible in plain SQL. The whole package is
readable in one sitting, which is what makes the authority boundary auditable
rather than merely asserted. Nothing requires a network at runtime.

**Costs, accepted.** Strict pydantic validation is stricter than most users
expect — `"0.7"` is refused where `0.7` is required — and that is documented
rather than softened. `argparse` output is plainer than a framework's. SQLite
gives single-host durability only, which matches the scope and would not
survive a move to a shared store. `statistics.median` will not carry a real
statistical slice; that slice will need this ADR revisited.

**Revisit when.** A slice needs distributions, calibration curves or confidence
intervals; or a store must be shared across hosts; or a second ledger format
becomes necessary. Adding a dependency requires a demonstrated need recorded
here and a verified licence compatibility.

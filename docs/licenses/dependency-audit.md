# Dependency and licence audit

- **Date**: 2026-08-14
- **Project licence**: Apache-2.0
- **Lock file**: `uv.lock`, 20 packages including `latent-compass` itself
- **Build backend**: `hatchling==1.32.0`, pinned exactly and mirrored into the
  dev group so its closure is locked and audited here

## Reproduce this audit

```bash
uv sync --all-groups
uv run python - <<'PY'
import importlib.metadata as md
import pathlib, tomllib
lock = tomllib.loads(pathlib.Path("uv.lock").read_text(encoding="utf-8"))
for package in sorted(lock["package"], key=lambda item: item["name"]):
    meta = md.metadata(package["name"])
    expression = meta.get("License-Expression") or meta.get("License") or ""
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License")]
    print(f"{package['name']:20} {package['version']:10} {expression or classifiers}")
PY
```

## Runtime closure — shipped in the wheel

These are the only packages a user installs alongside `latent-compass`.

| Package | Version | Licence | Compatible with Apache-2.0 redistribution |
| --- | --- | --- | :-: |
| `pydantic` | 2.13.4 | MIT | yes |
| `pydantic-core` | 2.46.4 | MIT | yes |
| `annotated-types` | 0.8.0 | MIT | yes |
| `typing-extensions` | 4.16.0 | PSF-2.0 | yes |
| `typing-inspection` | 0.4.4 | MIT | yes |

MIT and PSF-2.0 are permissive: both allow redistribution under Apache-2.0
provided their notices are retained, which `NOTICE` does. **No copyleft
obligation reaches the distributed artifact.**

Apache-2.0 was chosen and no incompatibility was found during this audit, so
the preference stands unchanged.

## Build and development closure — never shipped

Present only in the `dev` dependency group and in `[build-system].requires`.
None is declared in `[project.dependencies]`, none is installed by
`pip install latent-compass`, and none appears in the wheel's or the sdist's
install requirements.

The build backend is **pinned exactly** (`hatchling==1.32.0`) and mirrored into
the dev group. A range in `[build-system]` would mean the audited build and the
built build could differ; pinning and mirroring is what makes the row below a
statement about the backend that actually runs.

| Package | Version | Licence | Note |
| --- | --- | --- | --- |
| `pytest` | 8.4.2 | MIT | |
| `ruff` | 0.16.3 | MIT | |
| `mypy` | 1.20.2 | MIT | |
| `mypy-extensions` | 1.1.0 | MIT | via mypy |
| `hatchling` | 1.32.0 | MIT | the build backend |
| `tomlkit` | 0.15.1 | MIT | via hatchling |
| `trove-classifiers` | 2026.6.1.19 | Apache-2.0 | via hatchling |
| `pathspec` | 1.1.1 | **MPL-2.0** | via mypy **and** hatchling — see below |
| `librt` | 0.15.0 | MIT | via mypy |
| `pluggy` | 1.6.0 | MIT | via pytest and hatchling |
| `iniconfig` | 2.3.0 | MIT | via pytest |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | via pytest and hatchling |
| `pygments` | 2.20.0 | BSD-2-Clause | via pytest |
| `colorama` | 0.4.6 | BSD-3-Clause | via pytest, Windows only |

### The one non-permissive licence, stated plainly

`pathspec` is **MPL-2.0**, a weak file-level copyleft. It reaches this project
as a transitive dependency of `mypy` (a development tool) and of `hatchling`
(the build backend).

- It is not a runtime dependency and is not redistributed by this project.
- MPL-2.0 obligations attach to distribution of MPL-covered files. This project
  distributes none: the wheel contains only `latent_compass`, `LICENSE` and
  `NOTICE`, and the sdist contains only this project's own sources.
- A build backend runs at build time and is not part of the artifact it builds.
- No `pathspec` code is copied, modified or vendored here.

**Conclusion: no MPL obligation flows to the Apache-2.0 artifact.** It is
recorded here rather than omitted, because "all dependencies are permissive"
would have been a slightly false statement, and a licence audit that rounds
inconvenient facts away is not an audit.

## Scope and limits of this audit

**Checked.** Every package in `uv.lock`, by installed distribution metadata, on
2026-08-14. Direct and transitive alike. Runtime and development closures
separated.

**Not checked.** Provenance or supply-chain integrity of the published
artifacts. Patent grants beyond what each licence states. Any transitive
dependency introduced by a future version bump — this audit is a snapshot, and
the lock file is what makes it reproducible.

**Partly checked.** The build backend closure is now locked and audited above.
`uv build` still uses build isolation by default and resolves the pinned
`hatchling==1.32.0` fresh; `uv build --no-build-isolation` uses the locked
environment. The version is identical either way, which is what the exact pin
buys; the *resolution path* differs, and that is stated rather than glossed.

**No vulnerability scan was run.** This is a licence audit, not a security
audit. See `SECURITY.md`.

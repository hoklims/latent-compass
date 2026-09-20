# Changelog

All notable changes to Latent Compass are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses semantic versioning for published releases. A release entry
describes shipped behavior; experimental results and proof status remain in the
README and evidence documents rather than being promoted by this history.

## [Unreleased]

No unreleased user-facing change is currently recorded.

## [0.2.0] - 2026-09-20

### Added

- Installed `latent-compass-status` command with human-readable output and a
  deterministic `--json` form.
- Per-host visibility for exact hooks, runtime and wrapper availability,
  project registration, valid event/session counts, latest observation and
  `ADVICE`, `ABSTAIN` and `ESCALATE` totals.
- Explicit reporting that hook trust is unknown until reviewed by the host and
  that Latent Compass has no execution authority, recorded no content and did
  not influence host routing.

### Changed

- CI and tag-release workflows now build, test, attest and publish the exact
  `0.2.0` sdist and wheel, including an installed-wheel smoke of the new status
  command.
- English, French and host-observation documentation now explain how to see
  passive usage without interpreting an observation as product efficacy.

### Security

- Status aggregation accepts only the exact privacy-minimised record shape with
  a valid record seal, matching host/project identity, semantic UTC timestamp,
  bounded verdict vocabulary and explicit no-content/no-influence flags.
- Hook detection requires the exact asynchronous host invocation and existing
  runtime/wrapper; marker substrings or inert commands do not count.
- Filesystem discovery and individual record reads are bounded. Invalid,
  oversized, mismatched or truncated stores become `DEGRADED` without exposing
  their contents.

### Proof status

- This adoption release does not change the formal project verdict, which
  remains `PROOF_WEAK/BLOCK`. It grants no routing, pilot, promotion or
  index-removal authority.

## [0.1.0] - 2026-09-20

### Added

- Initial public alpha with strict contracts, append-only stores, offline
  benchmark and experimental active-diagnosis laboratory.
- Passive Codex and Claude host hooks with separate local stores and no host
  influence.

### Known limitations

- The original release assets were built from a Windows working tree rather
  than uploaded by CI. Their published SHA-256 values establish download
  integrity, not reproducible build provenance.
- The formal verdict remained `PROOF_WEAK/BLOCK`.

[Unreleased]: https://github.com/hoklims/latent-compass/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hoklims/latent-compass/releases/tag/v0.2.0
[0.1.0]: https://github.com/hoklims/latent-compass/releases/tag/v0.1.0

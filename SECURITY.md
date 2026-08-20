# Security policy

## Reporting a vulnerability

Report privately, not in a public issue.

1. Preferred: open a **private vulnerability report** via GitHub Security
   Advisories on <https://github.com/hoklims/latent-compass> ("Security" →
   "Report a vulnerability"). This keeps the report private until a fix exists.
2. If that is unavailable to you, open a public issue containing **only** the
   words "security report, requesting a private channel" and no technical
   detail, and wait to be contacted.

Please include: affected version or commit, what an attacker gains, the
smallest reproduction you have, and your disclosure timeline if you have one.

**Do not** include credentials, personal data, or the contents of a real ledger
store in a report. A synthetic reproduction is always sufficient here; if you
believe it is not, say so and wait for a private channel.

### What to expect

This is a small project with no paid on-call rotation. Acknowledgement is
best-effort within 7 days. There is no guaranteed fix window, and there is no
bug bounty. If a report goes unanswered for 90 days, public disclosure is
reasonable and will not be treated as hostile.

## What is in scope

- Bypassing the authority boundary: obtaining a `TransitionAuthorization` as
  `latent_compass`, for a transition outside `ALLOWED_TRANSITIONS`, without the
  `promote` capability where it is required, or on evidence whose seal does not
  reproduce from its own contents.
- Getting an episode accepted that should be refused: unknown or future
  contract version, unknown field, non-finite number, cross-host provenance,
  closed epoch, duplicate.
- Corrupting a ledger without `verify` reporting it, **within the threat model
  below**.
- Leaving a partially accepted episode after an interrupted append.
- Spending a holdout **corpus** more than once — including by revising the
  protocol, and including under concurrency — or scoring measurements against a
  protocol seal or corpus seal they were not collected under.
- Any write outside the root named on the command line, including via a
  traversal, a symlink or a silent overwrite.
- Corrupting a ledger in a way `verify` does not report: an altered payload, a
  relabelled binding, a deleted suffix, or a redaction or export that succeeds
  on a corrupt store.
- Any network access or process spawn from the package.
- Any failure reaching the user as a traceback rather than a typed JSON error.

## What is out of scope

These are documented properties, not vulnerabilities.

- **A privileged administrator rewriting the whole ledger.** A hash chain plus
  a durable anchor detects tampering by anyone who cannot rewrite both; it does
  not establish authenticity. The anchor lives in the database it anchors, so it
  raises the cost without changing the conclusion. Two stores with the same
  binding and different histories both verify. Defending against this needs an
  external anchor — co-signed digests, a remote witness, or genuinely
  append-only storage — which this package does not have. See `docs/ledger.md`.
- **An operator deleting or editing the holdout usage record.** The same limit:
  it is a local file, and this guards against a mistake and a second run, not
  against the operator.
- **Secrets written into a free-text field.** No structured field exists for a
  credential and unknown fields are refused, but a rationale, a redaction reason
  or a tombstone reason accepts whatever the caller writes. Sanitising those is
  the caller's obligation; this package does not scan them.
- **Code running in the same process with the same privileges.** It can bypass
  every check here.
- **An operator deleting the store file.** That is the documented
  `STORE_DESTRUCTION` deletion mode.
- **Denial of service by supplying an enormous input file.** Bound your inputs.
- **Corpus seals not matching the corpus.** They are operator-supplied; this
  package never reads a corpus and cannot verify them. Split disjointness is
  declared, not verified.
- Vulnerabilities in Python, SQLite or a dependency. Report those upstream; if
  one materially affects this project, an issue here is welcome.

## Security-relevant design

- No network: the package imports no socket, HTTP or subprocess machinery, and
  a test runs the full CLI with those primitives trapped.
- No execution: nothing in this package runs, applies or schedules anything it
  recommends. There is no CLI verb that acts on the observed system.
- Fail-closed: ambiguity, unknown versions and unknown fields refuse rather
  than degrade. A refused operation writes nothing.
- Minimisation: episodes carry identifiers and digests, never source, prompts
  or file contents. No *structured* field exists for a credential, and unknown
  fields are refused, so one cannot be added — see the free-text caveat above
  and `GOVERNANCE.md`.
- Confinement: every durable write resolves canonically and must land strictly
  inside a root named on the command line.
- Evidence provenance: local seals and rescoring can prove consistency rather
  than origin. Evidence-based lifecycle transitions therefore refuse before
  evidence or ledger access without external attestation.

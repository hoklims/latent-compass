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
- Getting a strategic decision accepted without the explicit
  `STRATEGIC_HIGH_IMPACT` and `NON_SENSITIVE` opt-ins; moving the decision
  store after a refused or interrupted append; accepting a stale/forked
  revision; or letting native and imported identities shadow one another.
- Importing a decision transfer after tampering, replay, source-record reuse or
  destination-binding mismatch, or treating `FOREIGN_READ_ONLY` content as
  native, revisable, re-exportable or authority-bearing.
- Getting a reconciliation to alter, re-seal or extend the pre-action decision
  record it names; getting an observation accepted for a candidate the record
  does not say was executed; getting an `ABSENT`, `LATE`, `AMBIGUOUS` or
  `DISPUTED` dimension to carry a value; getting an `OBSERVED` dimension accepted
  without its full provenance; moving the journal after a refused or interrupted
  append; accepting a stale, forked or preimage-re-pointed revision; or opening a
  second reconciliation identity over one pre-action revision.
- Starting a prospective collection without established power; enrolling a
  backfilled, duplicate, wrong-binding or undeclared-stratum case; losing a case
  from the denominator; linking anything except the verified current HOK-244
  tail; accepting a dependent producer; closing outside the sealed calendar and
  denominator rules; or publishing from a collecting, aborted or corrupt journal.
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
- **Secrets that do not match a named detector.** Episode free text is not
  scanned. Strategic decision admission additionally refuses a published,
  bounded list of credential shapes before opening a transaction, but it is not
  universal secret detection or a sanitiser. Ordinary prose can still contain a
  secret. Classification and sanitisation remain the caller's obligation; run
  `latent-compass memory limits` for the exact claimed coverage.
- **An observation that is simply false.** The reconciliation journal proves that
  an observation was recorded and has not been altered since. Its `observed_at`,
  `source_digest`, `producer` and `confidence` are asserted by whoever wrote them
  and witnessed by nothing here, so a truthful-looking wrong observation is a
  data-quality problem, not a vulnerability. Distinguishing the two needs an
  external witness this package does not have.
- **A false producer identity or chronology assertion.** Prospective collection
  compares declared producer strings and canonical timestamps. Its local seals
  prove local consistency only; they do not authenticate a person, process or
  clock, and they do not turn observations into causal evidence.
- **Code running in the same process with the same privileges.** It can bypass
  every check here.
- **Concurrent adversarial renaming of a SQLite store namespace.** Store open
  and creation refuse pre-existing symlinks/reparse points, but Python's SQLite
  API reopens a pathname and cannot consume the repository's confined file
  handle. Keep SQLite roots in an operator-controlled local directory. The
  handle-relative publication commands have the stronger race-safe guarantee.
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
- Strategic memory minimisation: only explicit high-impact, non-sensitive
  pre-action projections are admitted. Selected routes, outcomes, labels,
  scores, verdicts, holdout metadata and execution authority are structurally
  refused. Named credential detectors reduce accidental capture but make no
  completeness claim.
- Memory authenticity limit: the decision event chain, durable anchor and
  transfer seals prove local consistency, not issuer identity or chronology. An
  administrator who can rewrite the database and anchor can forge a verifying
  history; closing that gap requires an external witness or co-signature.
- Reconciliation minimisation and separation: the post-action journal is a third
  physically separate store that references decisions by seal and writes nothing
  back. Scores, rankings, rewards, preferences, verdicts, promotions, causal
  effects and counterfactuals are structurally refused, observations exist only
  for the executed candidate, and unknowns are recorded as named unknowns rather
  than defaulted values. Its seals carry the memory authenticity limit above and
  one more: they attest that an observation was recorded, never that it is true.
- Prospective minimisation: the sealed plan permits zero outcome-dependent
  interim looks, all enrolled cases remain in the denominator, and terminal
  publication exposes only an all-case manifest and narrow diagnostic rates.
  The surface has no option for operational authority, routing or model training.
- Confinement: file-publication commands are handle-relative and race-safe.
  SQLite stores refuse existing symlink/reparse roots and rely on the documented
  trusted local-namespace boundary above.
- Evidence provenance: local seals and rescoring can prove consistency rather
  than origin. Evidence-based lifecycle transitions therefore refuse before
  evidence or ledger access without external attestation.

# ADR 0013: Literal wrapper references and durable created-file ownership

Status: accepted design; delivery remains subject to independent admission.

## Problem

A foreign hook could invoke a managed wrapper immediately before a shell
separator. Whitespace-only tokenization retained the separator on the pathname
and incorrectly concluded that no reference existed. Separately, recovery after
process interruption used a confirmation flag plus content digest as ownership
evidence. A peer could replace a confirmed-created file with equal bytes before
recovery observation, and the recovery lease would adopt the peer's identity.

## Decision

Reference detection is bounded lexical analysis of literal arguments. It
recognizes the selected command separators `;`, `&&`, `||`, `|` and `&` through
quote-aware tokenization. Quoted punctuation and comments retain their literal
meaning. Known PowerShell literal invocation syntax remains supported. Unquoted
expansions/globs, grouping, redirection, malformed or ambiguous syntax and
exhausted character/token budgets produce `UNKNOWN` through the existing refusal
path. An unquoted hash inside a word is unresolved, not treated as a comment;
quoted hash paths remain literal and comments at token boundaries are ignored.
After quotes, comments and separators are classified, the unquoted alphabet is
Unicode letters/digits and `._-/:\\=` only. Other characters are unresolved.
Double quotes refuse dynamic `$`, backtick, `%`, `!` and `^`, including after a
backslash; single-quoted contents remain literal. Environment assignments as
executables are unresolved. This grammar covers POSIX literal invocation and
the recognized PowerShell literal-call form, not all Windows command languages.
Known Python launchers (including versioned names and `.exe`) accept only the
separate flags `-u`, `-I`, `-B`, `-E`, `-s`, `-S`, `-P`, `-O`, `-OO`, `-b`,
`-bb`, `-q`, an optional `--`, and an absolute script followed by literal
arguments. Every other pre-script mode or flag, including attached/clustered
inline code, is `UNKNOWN`. `env` accepts only `env EXEC...` without options or
assignments and classifies the nested executable. Known shells, cwd-changing
commands and sourcing/evaluation are `UNKNOWN`, apart from the supported
PowerShell literal call. Explicit relative executable/path references and
relative Python scripts lack an attested host cwd and are `UNKNOWN`; bare
executable names are not resolved against the installer's cwd. Unquoted tilde
is `UNKNOWN`; quoted tilde remains literal.
Known Node, Bun, Deno, Ruby, Perl, PHP and Lua launchers accept only a direct
absolute script without pre-script options; other launch modes are unresolved.
A lexical absence result concerns literal references only, never arbitrary
behavior inside a foreign program. This is not
a general shell or foreign-program interpreter.

New pending transaction journals use schema 2. Each entry includes
`publication_identity`; it starts as null. Confirmation of a created file
persists its fingerprint and `publication_state: confirmed` together in one
confined journal replacement. The caller must pass the original creating lease
after publication. The fingerprint is read from that lease's held descriptor or
handle; recovery never derives ownership by reopening the pathname after
creation.

The fingerprint domain is `latent-compass-created-file/1`. It contains the
native backend/token and native change evidence: POSIX device/inode plus
`st_ctime_ns`, or Windows volume/file-ID128 plus creation/change timestamps from
`FileBasicInfo`. The schema validates the platform, field set and integer/hex
types. Recovery compares the persisted fingerprint with its observed lease
before changing any recovery target or revoking creation authority, then checks
again before mutation. Live lease identity/content checks remain in force.

Confirmed legacy creations without a fingerprint are `UNKNOWN` and are
preserved with an actionable diagnostic. There is no implicit conversion from
a content digest to ownership. Legacy schema-1 update/delete records retain
their existing backup/content safeguards. An interruption before the atomic
identity/confirmation update leaves the create unconfirmed and preserves it.

## Guarantee and limits

This restores the distinction between matching content and locally observed
created-file ownership. A same-byte replacement before recovery observation,
or a reused native identifier accompanied by different change evidence, does
not authorize deletion. Unavailable, malformed or foreign-platform identity
evidence refuses automatic recovery.

The journal is a trusted local coordination record, not a signature or
authentication system. Native identifiers and change/creation timestamps are
filesystem-dependent comparison evidence. They are not universally unique
generation numbers; protection against privileged timestamp restoration,
deliberate journal forgery or indistinguishable identifier/stamp collisions is
outside this guarantee. The record does not attest clock accuracy or filesystem
timestamp resolution. Missing, invalid or incompatible native fields refuse
recovery; matching valid fields are comparison evidence under the stated local
filesystem and trusted-metadata conditions, not proof of universal uniqueness.
Coarse timestamps do not remove the indistinguishable-collision limitation.
Metadata changes can conservatively block recovery and require operator
inspection.

Tests cover original-creator recovery, same-byte peer replacement before
observation, legacy unknown ownership, unavailable evidence, malformed fields,
a modeled native-ID collision with different stamps, and interruption before
confirmation persistence. Windows tests observe native Windows handles; the
same POSIX implementation and tests require their native CI execution before a
POSIX delivery claim. The changed mechanism cannot admit itself: external
immutable N-1 review and admission remain required.

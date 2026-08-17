# Authority boundary (HOK-184)

Contract version `2.0.0`; advisory payloads at `1.0.0` remain readable. The authoritative statement is the code in
`src/latent_compass/authority.py` and `src/latent_compass/vocabulary.py`; this
document explains it. Where the two disagree, the code is what runs, and the
disagreement is a bug in this file.

Run `latent-compass authority boundary` to print the live boundary.

## Actors

| Actor | Role |
| --- | --- |
| `latent_compass` | This system. Validates and records supplied advisories. Never authorises. |
| `external_judge` | The deterministic authority on truth, invariants, impact, gates and proof. Observed from outside; never called from here. |
| `human_operator` | A person. The only actor holding `promote`. |
| `observed_agent` | The coding agent whose episodes are recorded. Unaffected by this system. |

## Capabilities

| Actor | observe | advise | record | authorize_transition | execute | promote | mutate_external_judge |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `latent_compass` | ✓ | ✓ | ✓ | — | — | — | — |
| `external_judge` | ✓ | — | — | ✓ | — | — | ✓ |
| `human_operator` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `observed_agent` | — | — | — | — | ✓ | — | — |

Four capabilities are permanently withheld from `latent_compass`:
`authorize_transition`, `execute`, `promote`, `mutate_external_judge`.

The grant, the transition table and the evidence requirements are exposed as
**read-only mappings**. A consumer cannot widen the boundary by assignment.

## The unconditional refusal

`authorize_transition` refuses `latent_compass` **before it reads any table**.

The security property is *not* "the capability check happens first". Ordering is
a property of one implementation; reorder the function and the property is gone
while the code still looks correct. The invariant is that the refusal is
**unconditional and table-independent**.

A test widens the private capability table to grant `latent_compass` every
capability, asserts the widening took effect, and asserts the refusal still
holds with reason `self_authorisation`. An implementation whose refusal was
merely "this actor lacks the capability" authorises in that test and fails it.

## Promotion is a separate capability

Reaching `PROMOTED` requires `Capability.PROMOTE` **in addition to**
`authorize_transition`. Only `human_operator` holds it.

`external_judge` holds `authorize_transition`, so it can enter `SHADOW`. The
built-in path cannot advance any actor farther without a trusted external
attestation. Even after such a verifier exists, the judge can never promote:
that still requires `promote`.

## Inputs and outputs

**Inputs.** Episodes (`docs/episode-contract.md`), pre-registered protocols and
sealed verdicts (`docs/evaluation-protocol.md`), and externally observed
verdicts recorded inside episodes.

**Outputs.** Four advisory kinds, and only four:

| Output | Meaning |
| --- | --- |
| `DIRECTION` | A ranked opinion **supplied by a caller**. Requires another actor to do anything. |
| `ABSTAIN` | No opinion is warranted. |
| `FALLBACK` | Defer to the pre-existing default behaviour. |
| `ESCALATE` | Hand the decision to a human operator. |

None is an instruction. An `Advisory` has no field capable of naming a target to
mutate, a command to run, or an authority to assume, and unknown fields are
rejected rather than ignored — so a crafted `{"action": "promote"}` is a loud
validation failure.

This foundation does not *produce* advisories. It validates and records the ones
it is given. Emission, ranking and calibration are HOK-182.

## Uncertainty resolves away from action

A `DIRECTION` may not be issued when uncertainty exceeds
`ABSTENTION_UNCERTAINTY_THRESHOLD` (`0.35`). The model refuses construction.

`may_issue_direction` refuses non-numbers, `NaN`, infinities and values outside
`[0, 1]` with a typed error instead of answering. A bare comparison returns
`False` for `NaN` — an answer it has no basis for — and `True` for `-5.0`, which
is wrong.

## Lifecycle

```
DEFINE ──▶ SHADOW ──▶ OFFLINE_VERIFIED ──▶ CANARY_ELIGIBLE ──▶ PROMOTED
   │          │              │                     │              │
   └──────────┴──────────────┴─────────────────────┴──────────────┴──▶ REJECTED
```

`REJECTED` is terminal. A rejected candidate is redefined as a new candidate
rather than resurrected, so the record of the rejection survives. Every other
state can reach `REJECTED` with **no evidence at all**: refusing is never gated.
`PROMOTED → REJECTED` is the rollback edge.

### Evidence per target state

| Target | Protocol | Raw measurements + verdict | Holdout receipt | Human ack | External attestation | Extra capability |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| `SHADOW` | ✓ | — | — | — | — | — |
| `OFFLINE_VERIFIED` | ✓ | ✓ | — | — | ✓ | — |
| `CANARY_ELIGIBLE` | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `PROMOTED` | ✓ | ✓ | ✓ | ✓ | ✓ | `promote` |
| `REJECTED` | — | — | — | — | — | — |

### Provenance fails closed before evidence access

This call is deliberately refused:

```python
authorize_transition(
    from_state=LifecycleState.CANARY_ELIGIBLE,
    to_state=LifecycleState.PROMOTED,
    actor=Actor.HUMAN_OPERATOR,
    protocol=preregistration,
    measurements=measurements,
    verdict=verdict,
    holdout_ledger=usage,
    human_acknowledged=True,
)
```

After actor capabilities and the transition edge are checked,
`OFFLINE_VERIFIED`, `CANARY_ELIGIBLE` and `PROMOTED` refuse immediately as
`untrusted_evidence`. The refusal is literal and table-independent: mutating the
private evidence-requirement table cannot disable it. No protocol,
measurement, verdict or holdout ledger is inspected, and a refused call creates
no ledger lock.

Hashes and rescoring cannot establish where measurements came from. A future
positive path must be rooted outside the request payload, bind every evidence
seal and keep the verifier fixed at the composition root rather than accepting
one per call. The existing structural and semantic verification code remains
useful for supplied evidence on ungated rejection paths and for that future
attested composition root; it is not presented as provenance.

### Named refusals

These shortcuts are refused for **every** actor, including a human operator:

`DEFINE → PROMOTED`, `DEFINE → CANARY_ELIGIBLE`, `SHADOW → PROMOTED`,
`SHADOW → CANARY_ELIGIBLE`, `OFFLINE_VERIFIED → PROMOTED`,
`REJECTED → SHADOW`, `REJECTED → PROMOTED`.

Every refusal carries a machine-readable reason: `self_authorisation`,
`actor_lacks_capability`, `illegal_transition`, `terminal_state`,
`missing_evidence`, `evidence_forged`, `evidence_mismatch`, `evidence_rejects`,
`untrusted_evidence`.

## Continue / kill and rollback

**Continue** requires every required metric of the pre-registered protocol to
meet its threshold on the evaluated split, aggregated as a median over all
pre-registered seeds. **Kill** is any other outcome: a failing required metric, a
missing seed, an unregistered metric, a seal mismatch, an epoch mismatch, a
corpus mismatch, or a spent holdout. Silence is never a pass.

**Rollback** is `PROMOTED → REJECTED`, available to any actor holding
`authorize_transition`, requiring no evidence. Rolling back must never be harder
than rolling forward.

## Anti-goals

1. Latent Compass never executes, schedules or applies a recommendation.
2. Latent Compass never calls, mutates or adjudicates on behalf of the external judge.
3. Latent Compass never promotes a candidate, and never authorises its own promotion.
4. Latent Compass never resolves an unknown by inference; unknowns stay unknown.
5. Latent Compass never emits, ranks or calibrates an advisory in this foundation; it validates and records advisories supplied to it.
6. Latent Compass never claims an intelligence gain.

## Threat model

| Threat | Defence | Status |
| --- | --- | --- |
| Episode carries an action or authority field | Unknown fields rejected, not ignored | Defended, tested |
| Caller asks Latent Compass to authorise a legal move | Refused unconditionally, before any table is read | Defended, tested |
| Capability table widened by a consumer | Public tables are read-only views; and the refusal does not consult them | Defended, tested |
| Caller invents self-consistent metrics and the word `CONTINUE` | Rescoring checks consistency; absence of external attestation then refuses the transition | Defended fail-closed, tested |
| Benchmark marker removed and values reconstructed as protocol evidence | Evidence-bearing transitions require external attestation unavailable to the report | Defended fail-closed, tested |
| Sealed verdict edited from `KILL` to `CONTINUE` | Recomputed seal no longer matches the carried one | Defended, tested |
| External judge asks for a promotion | Promotion demands `promote`, which only a human holds | Defended, tested |
| A validation `CONTINUE` used to promote | Promotion demands a final verdict on the holdout | Defended, tested |
| A holdout verdict copied without its spend record | Matching typed consumption receipt required | Defended, tested |
| Chained uncertainty or a non-finite number as a confident direction | Finiteness, range and abstention checks | Defended, tested |
| Threshold edited after seeing the holdout | New version, epoch and seal forced; the holdout **corpus** stays spent | Defended, tested |
| Cross-host episode entering the wrong store | Store bound at creation; provenance checked per append and re-checked at verify | Defended, tested |
| Interrupted write leaving a partial episode | Single `BEGIN IMMEDIATE`; nothing that can fail runs after the commit | Defended, tested |
| Ledger suffix silently deleted | Durable anchor written in the append transaction | Defended, tested |
| **Administrator rewrites the whole ledger, chain and anchor together** | **None.** A hash chain and an anchor establish internal consistency, never **authenticity**; detecting a wholesale forgery needs an external anchor this package does not have. | **Not defended.** See `docs/ledger.md`. |
| Code running in the same process with the same privileges | None. It can bypass every check here. | Not defended. |

## Scenarios

**Positive — a direction is recorded and goes nowhere.** An agent branches; a
caller supplies a `DIRECTION` advisory at uncertainty `0.2`; the episode is
validated and appended; the agent's behaviour is unchanged, because nothing
consumes the advisory. Recording is the whole effect.

**Positive — uncertainty resolves to escalation.** Uncertainty is `0.8`; a
`DIRECTION` cannot be constructed; an `ESCALATE` advisory is recorded with no
`direction_id`. A human decides.

**Fail-closed — a promotion with internally valid evidence.** A protocol is
pre-registered and sealed. Holdout measurements reproduce a sealed `CONTINUE`
final verdict and have a matching consumption receipt. A human presents all of
it and acknowledges. The transition is still refused as `untrusted_evidence`:
none of those local hashes proves measurement origin.

**Hostile — self-promotion.** A caller requests `CANARY_ELIGIBLE → PROMOTED` as
`latent_compass` with complete, genuine evidence. Refused with
`self_authorisation`, before any table is read.

**Hostile — the judge promotes.** Same request as `external_judge`. Refused with
`actor_lacks_capability`, naming `promote`.

**Hostile — laundered verdict.** A `KILL` verdict is edited to `CONTINUE` in
memory. The seal it still carries no longer matches its contents. Refused as
`evidence_forged`.

**Hostile — post-hoc threshold.** Holdout measurements fail; the operator lowers
the threshold. The edit forces a new revision, epoch and seal; the old
measurements no longer match. Re-measuring the same holdout corpus under the new
protocol is refused, because the corpus was already spent. The original `KILL`
stands in the record.

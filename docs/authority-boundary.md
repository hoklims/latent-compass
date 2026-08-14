# Authority boundary (HOK-184)

Contract version `1.0.0`. The authoritative statement is the code in
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

`external_judge` holds `authorize_transition`, so it can advance a candidate
through the lifecycle. It can never promote one — the refusal names
`required_capability: promote`.

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

| Target | Protocol | Raw measurements + verdict | Holdout receipt | Human ack | Extra capability |
| --- | :-: | :-: | :-: | :-: | :-: |
| `SHADOW` | ✓ | — | — | — | — |
| `OFFLINE_VERIFIED` | ✓ | ✓ | — | — | — |
| `CANARY_ELIGIBLE` | ✓ | ✓ | ✓ | ✓ | — |
| `PROMOTED` | ✓ | ✓ | ✓ | ✓ | `promote` |
| `REJECTED` | — | — | — | — | — |

### Evidence is verified, not trusted

`authorize_transition` takes the **artefacts**, not claims about them:

```python
authorize_transition(
    from_state=LifecycleState.CANARY_ELIGIBLE,
    to_state=LifecycleState.PROMOTED,
    actor=Actor.HUMAN_OPERATOR,
    protocol=preregistration,  # a real Preregistration
    measurements=measurements,  # the complete raw MeasurementSet
    verdict=verdict,  # a real, sealed Verdict
    holdout_ledger=usage,  # the matching durable consumption receipt
    human_acknowledged=True,
)
```

and then, in order:

1. **recomputes the verdict's seal** from the verdict's own contents. A verdict
   edited after sealing — `KILL` rewritten to `CONTINUE` — no longer reproduces
   its seal. Refused as `evidence_forged`.
2. **recomputes the protocol's seal** and matches the verdict against it, plus
   the epoch and the split's pre-registered corpus seal. A verdict that is
   internally consistent but belongs to a different protocol, epoch or corpus is
   refused as `evidence_mismatch`.
3. **re-scores the raw measurements** under the validated protocol and compares
   the complete expected verdict with the supplied one. A caller can recompute
   a hash over invented metrics, but cannot make them reproduce from different
   raw evidence.
4. requires `decision == CONTINUE`; a `KILL` admits no target but `REJECTED`.
5. for `CANARY_ELIGIBLE` and `PROMOTED`, requires the verdict to be a
   `FINAL_VERDICT` on the `HOLDOUT` split. A genuine `CONTINUE` on validation is
   enough for `OFFLINE_VERIFIED` and not enough for promotion.
6. requires the durable holdout receipt to bind this protocol, epoch,
   measurement-set seal and verdict seal exactly.

There is no parameter left through which a caller can assert a conclusion.

### Named refusals

These shortcuts are refused for **every** actor, including a human operator:

`DEFINE → PROMOTED`, `DEFINE → CANARY_ELIGIBLE`, `SHADOW → PROMOTED`,
`SHADOW → CANARY_ELIGIBLE`, `OFFLINE_VERIFIED → PROMOTED`,
`REJECTED → SHADOW`, `REJECTED → PROMOTED`.

Every refusal carries a machine-readable reason: `self_authorisation`,
`actor_lacks_capability`, `illegal_transition`, `terminal_state`,
`missing_evidence`, `evidence_forged`, `evidence_mismatch`, `evidence_rejects`.

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
| Caller invents self-consistent metrics and the word `CONTINUE` | Raw measurements are re-scored; the complete expected verdict must match | Defended, tested |
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

**Positive — a promotion with real evidence.** A protocol is pre-registered and
sealed. Holdout measurements are scored once, producing a sealed `CONTINUE`
final verdict. A human operator presents both, acknowledges, and
`CANARY_ELIGIBLE → PROMOTED` is authorised. The authorisation records the
protocol seal, the verdict seal and the corpus seal.

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

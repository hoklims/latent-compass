"""HOK-244 — durable, append-only, tamper-evident post-action decision reconciliation.

This package records what was observed after a decision. It does not judge the
decision, score it, rank its alternatives, explain it causally, authorise anything
or learn from anything.

What it is
----------
A separate, host-and-family-bound journal, adjacent to the HOK-243 decision
memory. Each entry names one exact pre-action revision — its ``DecisionBinding``,
``decision_id``, revision, ``record_seal`` and ``projection_seal`` — states
explicitly whether an external authority authorised the action and whether one
candidate was executed, and, when one was, carries exactly five observation
dimensions: ``SUCCESS``, ``VIOLATION``, ``COST``, ``INFORMATION``,
``REVERSIBILITY``. Each dimension is either ``OBSERVED`` — with a source id, a
source digest, an observation instant, a producer and a stated confidence — or
``UNKNOWN`` with one of four named reasons: ``ABSENT``, ``LATE``, ``AMBIGUOUS``,
``DISPUTED``. An unknown never acquires a value.

What it is not
--------------
It is **not** an extension of ``StrategicDecisionRecord``: no outcome field is
added to a decision, no event kind is added to the HOK-243 chain, and no
pre-action seal moves. It computes no scalar score, no ranking, no agreement
verdict and no causal claim. It selects nothing, promotes nothing, routes nothing
and trains nothing. Reading a reconciliation grants no capability to anyone.

No counterfactuals
------------------
Observations exist only for the one candidate the record says was executed. When
nothing was executed, the observation tuple is empty — there is no field in which
an outcome for an unexecuted candidate could be written, so the counterfactual is
unrepresentable rather than merely discouraged.

Replay
------
:func:`~latent_compass.decision_reconciliation.replay.replay_reconciliation` is
pure and deterministic: given the reconciliation and the exact decision record it
names, it lays the recorded pre-action evidence facts for the executed candidate
beside the observed value — or the named unknown — dimension by dimension, and
stops there. It reads no clock, opens no file, and reaches no conclusion.

Store separation
----------------
The journal is its own SQLite file with its own format version and its own seal
domains, bound at creation to one host, one agent family, one store id and one
epoch. A Codex journal and a Claude journal are different files with different
genesis seals, and nothing synchronises them. One pre-action revision is
reconciled by exactly one reconciliation identity; a correction or a disagreement
appends a revision naming the exact head seal it extends.

Honest limits
-------------
Local seals prove *consistency*: that these bytes reproduce the chain, anchor and
record seals they claim. They prove neither issuer authenticity nor chronology,
and for this contract specifically they prove nothing about the *truth* of an
observation. ``observed_at``, ``source_digest``, ``producer`` and ``confidence``
are all asserted by the producer and witnessed by nothing here. Anyone able to
rewrite both the chain and the anchor can produce a journal that verifies
perfectly. Admission refuses a short, named list of credential shapes — the same
list HOK-243 publishes — and claims nothing about content outside it.
"""

from __future__ import annotations

from latent_compass.decision_reconciliation.admission import (
    FORBIDDEN_FIELD_NAMES,
    admit_reconciliation,
    reconciliation_limits,
)
from latent_compass.decision_reconciliation.contracts import (
    AuthorizationState,
    DecisionPreimageReference,
    DimensionObservation,
    ExecutionState,
    ObservationProvenance,
    ObservationStatus,
    ReconciliationBinding,
    ReconciliationRecord,
    ReconciliationRevisionKind,
    UnknownReason,
    build_reconciliation_payload,
)
from latent_compass.decision_reconciliation.replay import (
    ComparisonLinkage,
    DimensionComparison,
    ReconciliationReplay,
    replay_reconciliation,
    require_preimage_match,
)
from latent_compass.decision_reconciliation.store import (
    RECONCILIATION_DATABASE_FILENAME,
    ReconciliationAppendReceipt,
    ReconciliationHead,
    ReconciliationIntegrityFinding,
    ReconciliationIntegrityKind,
    ReconciliationIntegrityReport,
    ReconciliationJournal,
    ReconciliationJournalBinding,
    ReconciliationJournalStatus,
)

__all__ = [
    "FORBIDDEN_FIELD_NAMES",
    "RECONCILIATION_DATABASE_FILENAME",
    "AuthorizationState",
    "ComparisonLinkage",
    "DecisionPreimageReference",
    "DimensionComparison",
    "DimensionObservation",
    "ExecutionState",
    "ObservationProvenance",
    "ObservationStatus",
    "ReconciliationAppendReceipt",
    "ReconciliationBinding",
    "ReconciliationHead",
    "ReconciliationIntegrityFinding",
    "ReconciliationIntegrityKind",
    "ReconciliationIntegrityReport",
    "ReconciliationJournal",
    "ReconciliationJournalBinding",
    "ReconciliationJournalStatus",
    "ReconciliationRecord",
    "ReconciliationReplay",
    "ReconciliationRevisionKind",
    "UnknownReason",
    "admit_reconciliation",
    "build_reconciliation_payload",
    "reconciliation_limits",
    "replay_reconciliation",
    "require_preimage_match",
]

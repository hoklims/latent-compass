"""HOK-243 — durable, revocable, tamper-evident pre-action strategic decision memory.

This package remembers strategic decisions. It does not take them, rank them,
execute them, or acquire any authority by holding them.

What it is
----------
An opt-in, append-only, host-and-family-bound record of decisions a named
authority declared ``STRATEGIC_HIGH_IMPACT``, each carrying a complete HOK-234
judgeable projection over its candidate set, a review deadline and a retention
deadline. Revisions extend the exact prior revision and never replace history.
Revocation and redaction are themselves durable appended events.

What it is not
--------------
It selects no route and stores none. It carries no outcome, label, score,
verdict, holdout metadata or execution authorization — those field names are
refused by name, at any depth, before any durable write. It calls nothing, opens
no socket, spawns no process, and does not touch the continual harness. Reading
a record grants no capability to anyone.

Store separation
----------------
A Codex store and a Claude store are different files with different genesis
seals, and nothing synchronises them. The only crossing is an explicit, sealed,
one-record transfer envelope addressed to one named destination; what arrives is
``FOREIGN_READ_ONLY`` — readable reference material, never native, never
revisable locally, never authority-bearing.

Honest limits
-------------
Local seals prove *consistency*: that these bytes reproduce the chain, anchor and
record seals they claim. They prove neither issuer authenticity nor chronology.
Anyone able to rewrite both the chain and the anchor can produce a store that
verifies perfectly, and an envelope's ``exported_at`` is asserted by its producer
rather than witnessed. Admission refuses a short, named list of credential shapes
and claims nothing about content outside that list.
"""

from __future__ import annotations

from latent_compass.decision_memory.admission import (
    FORBIDDEN_FIELD_NAMES,
    admission_limits,
    admit_strategic_decision,
    admit_transfer_envelope,
    screen_persisted_text,
)
from latent_compass.decision_memory.contracts import (
    ActiveDecision,
    DecisionBinding,
    DecisionImpactClass,
    DecisionOriginKind,
    DecisionTombstone,
    DecisionTransferEnvelope,
    EventKind,
    RevocationEvent,
    SensitivityClassification,
    StrategicDecisionRecord,
    build_record_payload,
    build_transfer_envelope,
)
from latent_compass.decision_memory.store import (
    DECISION_DATABASE_FILENAME,
    DecisionAppendReceipt,
    DecisionIntegrityFinding,
    DecisionIntegrityKind,
    DecisionIntegrityReport,
    DecisionMemoryStore,
    DecisionStoreBinding,
    DecisionStoreStatus,
    TransferReceipt,
)

__all__ = [
    "DECISION_DATABASE_FILENAME",
    "FORBIDDEN_FIELD_NAMES",
    "ActiveDecision",
    "DecisionAppendReceipt",
    "DecisionBinding",
    "DecisionImpactClass",
    "DecisionIntegrityFinding",
    "DecisionIntegrityKind",
    "DecisionIntegrityReport",
    "DecisionMemoryStore",
    "DecisionOriginKind",
    "DecisionStoreBinding",
    "DecisionStoreStatus",
    "DecisionTombstone",
    "DecisionTransferEnvelope",
    "EventKind",
    "RevocationEvent",
    "SensitivityClassification",
    "StrategicDecisionRecord",
    "TransferReceipt",
    "admission_limits",
    "admit_strategic_decision",
    "admit_transfer_envelope",
    "build_record_payload",
    "build_transfer_envelope",
    "screen_persisted_text",
]

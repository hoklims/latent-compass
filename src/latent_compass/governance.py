"""HOK-185 — data governance: retention, minimisation, redaction, deletion.

Append-only and "delete my data" pull in opposite directions. This module
resolves that tension explicitly instead of letting one quietly win.

The rule is: **no deletion ever rewrites history silently.** Three mechanisms
exist, they are not interchangeable, and each is honest about what it does not
achieve.

:attr:`DeletionMode.TOMBSTONE`
    The episode payload is dropped; the row, its position, its content seal and
    the chain link over it all survive. Integrity still verifies, the root seal
    is unchanged, and the redaction is enumerable — ``verify`` reports the row
    as redacted and names the tombstone that explains it. What is lost is the
    ability to replay that episode's content, and :func:`replay` says so rather
    than skipping the row. This is the mechanism for a subject-level erasure
    request against a store that must stay auditable.

:attr:`DeletionMode.EPOCH_ABANDONMENT`
    The store's epoch is closed. No further appends are accepted; everything
    already recorded is preserved and remains verifiable. This is the mechanism
    for "this collection run is void", where the data must survive but must
    stop being extended or treated as current.

:attr:`DeletionMode.STORE_DESTRUCTION`
    Deleting the store file. This is the only mechanism that actually erases
    content, and it is deliberately *not* implemented here: destroying data is
    an operator act performed with operator tools, not a side effect of a
    library call. Documented so the gap is a decision rather than an oversight.

Minimisation is a recording-time obligation, not a cleanup step: a field that
should not be stored must be absent at append, and its absence declared as a
:class:`~latent_compass.episode.RedactionMark` so a reader can tell "removed on
purpose" from "never existed".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field, model_validator

from latent_compass.contracts import Identifier, StrictModel, Timestamp

__all__ = [
    "MINIMISATION_RULES",
    "RETENTION_RULES",
    "DeletionMode",
    "Tombstone",
    "deletion_semantics",
]


class DeletionMode(StrEnum):
    """How data may leave a store. See the module docstring for semantics."""

    TOMBSTONE = "TOMBSTONE"
    EPOCH_ABANDONMENT = "EPOCH_ABANDONMENT"
    STORE_DESTRUCTION = "STORE_DESTRUCTION"


class Tombstone(StrictModel):
    """The durable record that an episode payload was deliberately removed."""

    episode_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    created_at: Timestamp
    mode: DeletionMode = Field(default=DeletionMode.TOMBSTONE)

    @model_validator(mode="after")
    def _mode_is_a_tombstone(self) -> Tombstone:
        if self.mode is not DeletionMode.TOMBSTONE:
            raise ValueError("a tombstone record must declare TOMBSTONE mode")
        return self


MINIMISATION_RULES: Final[tuple[str, ...]] = (
    "Record identifiers and digests, never raw source, prompts or file contents.",
    "Host identity is an operator-chosen label, not a hostname, IP or account.",
    "A field withheld at recording time is declared as a RedactionMark over one "
    "of the schema's optional fields, and the declaration is verified against a "
    "real absence, so a marker can never be decoration over present data.",
    "No structured field exists for a credential, token or key, and unknown "
    "fields are refused, so one cannot be added. This is a schema guarantee, "
    "not a content guarantee: the free-text fields (a rationale, a redaction "
    "reason, a tombstone reason, a metric or task name) accept whatever the "
    "caller writes. Sanitising those is the caller's obligation; this package "
    "does not scan them and does not claim they are clean.",
)

RETENTION_RULES: Final[tuple[str, ...]] = (
    "A store is local, single-host and operator-owned; nothing is transmitted.",
    "Abandoning an epoch bounds *acquisition*: no further episode is accepted. "
    "It does not bound storage duration, and it deletes nothing. How long an "
    "abandoned store is kept is an operator policy this package neither sets "
    "nor enforces.",
    "Erasure of one subject's episode is a tombstone; erasure of everything is "
    "destruction of the store file by the operator.",
    "A tombstone removes the payload from the logical contract and from every "
    "read path. It does not guarantee the bytes have left the physical device: "
    "SQLite may retain them in freelist pages, and a filesystem or SSD may "
    "retain them after that. Physical erasure is an operator act with operator "
    "tools, and is not demonstrated here.",
    "Export produces a sealed snapshot; an exported file is outside this "
    "package's custody and inherits the operator's retention policy.",
)


def deletion_semantics() -> dict[str, dict[str, object]]:
    """Machine-readable statement of what each deletion mode does and does not do."""
    return {
        DeletionMode.TOMBSTONE.value: {
            "removes_payload": True,
            "preserves_row": True,
            "preserves_root_seal": True,
            "replayable_afterwards": False,
            "visible_in_verify": True,
            "implemented": True,
            "physical_erasure_guaranteed": False,
            "note": "logical removal only; refused outright on a store that does not verify",
        },
        DeletionMode.EPOCH_ABANDONMENT.value: {
            "removes_payload": False,
            "preserves_row": True,
            "preserves_root_seal": True,
            "replayable_afterwards": True,
            "visible_in_verify": True,
            "implemented": True,
        },
        DeletionMode.STORE_DESTRUCTION.value: {
            "removes_payload": True,
            "preserves_row": False,
            "preserves_root_seal": False,
            "replayable_afterwards": False,
            "visible_in_verify": False,
            "implemented": False,
            "note": "an operator act performed with operator tools, by design",
        },
    }

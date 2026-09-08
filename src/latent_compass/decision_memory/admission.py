"""HOK-243 — admission: everything that must refuse *before* a durable write.

Admission is a pure function of the payload. It opens no file, holds no
transaction and mutates nothing, so a refusal here provably leaves the store's
record count, generation and root seal untouched.

What admission refuses
----------------------
* a payload that is not a JSON object, or that declares no contract version;
* a payload that does not encode canonically — non-finite numbers, non-string
  mapping keys, values that have no reproducible encoding;
* a nesting depth or a canonical size beyond the declared bounds;
* any field name from the forbidden set, at any depth — the route, outcome,
  label, score, verdict, holdout and authorization vocabulary, and the
  credential vocabulary;
* anything but an explicit ``NON_SENSITIVE`` classification: ``UNKNOWN`` and
  ``HOLDOUT`` are named values and both are refused;
* anything but an explicit ``STRATEGIC_HIGH_IMPACT`` opt-in;
* text longer than the admission bound, before the schema's own bounds apply;
* text matching one of the credential shapes named below.

What admission does **not** claim
---------------------------------
This is not universal secret detection, and it is not a sanitiser. It matches a
short, named list of well-known credential shapes and a labelled-assignment
form. A secret that looks like ordinary prose passes, and a caller who admits
one has admitted it. Classifying content is the caller's obligation; this
package refuses the shapes it names and says nothing about the rest.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from latent_compass.canonical import canonical_bytes
from latent_compass.contracts import (
    SUPPORTED_DECISION_MEMORY_VERSIONS,
    SUPPORTED_DECISION_TRANSFER_VERSIONS,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.contracts import (
    DecisionImpactClass,
    DecisionTransferEnvelope,
    SensitivityClassification,
    StrategicDecisionRecord,
)
from latent_compass.errors import (
    DecisionMemoryViolation,
    SensitiveContentRefused,
    TransferRefused,
)

__all__ = [
    "FORBIDDEN_FIELD_NAMES",
    "MAX_ADMITTED_TEXT_LENGTH",
    "MAX_CANONICAL_BYTES",
    "MAX_NESTING_DEPTH",
    "admission_limits",
    "admit_strategic_decision",
    "admit_transfer_envelope",
    "screen_located_text",
    "screen_persisted_text",
]

#: Bounds applied before validation, so an oversized payload is refused by name
#: rather than as a pile of per-field length errors.
MAX_ADMITTED_TEXT_LENGTH: Final = 2000
MAX_CANONICAL_BYTES: Final = 262_144
MAX_NESTING_DEPTH: Final = 32

#: Field names refused at any depth, lowercased.
#:
#: The closed schema already forbids unknown fields, so this set is not what
#: keeps them out — it is what makes the refusal *legible*: a payload carrying
#: ``verdict`` is told it carried a forbidden field, not that some nested model
#: rejected an extra key. Two vocabularies are covered: the post-action and
#: authorization one this contract must never express, and the credential one it
#: must never carry.
FORBIDDEN_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        # post-action, selection and scoring
        "chosen_route",
        "deferred_outcome",
        "external_verdict",
        "label",
        "labels",
        "outcome",
        "rank",
        "ranking",
        "route",
        "score",
        "scores",
        "selected_candidate",
        "selected_direction_id",
        "selected_route",
        "verdict",
        # evaluation and holdout metadata
        "holdout",
        "holdout_corpus_seal",
        "holdout_ledger",
        "split",
        # execution authority
        "argv",
        "authorisation",
        "authorization",
        "authorized",
        "command",
        "execute",
        "execution",
        # credential vocabulary
        "access_token",
        "api_key",
        "apikey",
        "authorization_header",
        "bearer_token",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secrets",
        "token",
    }
)

#: Named credential shapes. Each entry is (name, pattern); the name is what the
#: refusal reports, so an operator learns which shape fired without the matched
#: text ever being echoed back.
_CREDENTIAL_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("authorization_header", re.compile(r"(?i)\bauthorization\s*:\s*\S+")),
    ("bearer_credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_credential", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("generic_api_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_credential", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("json_web_token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    (
        "labelled_credential_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|passphrase|token|credential)s?\b"
            r"\s*[:=]\s*\S{8,}"
        ),
    ),
)


def admission_limits() -> dict[str, object]:
    """Machine-readable statement of what admission bounds and what it refuses."""
    return {
        "max_admitted_text_length": MAX_ADMITTED_TEXT_LENGTH,
        "max_canonical_bytes": MAX_CANONICAL_BYTES,
        "max_nesting_depth": MAX_NESTING_DEPTH,
        "forbidden_field_names": sorted(FORBIDDEN_FIELD_NAMES),
        "credential_shapes": [name for name, _ in _CREDENTIAL_SHAPES],
        "required_sensitivity": SensitivityClassification.NON_SENSITIVE.value,
        "required_impact_class": DecisionImpactClass.STRATEGIC_HIGH_IMPACT.value,
        "universal_secret_detection": False,
        "note": (
            "admission refuses the shapes it names and claims nothing about any "
            "other content; classification remains the caller's obligation"
        ),
    }


def _collect(
    value: object,
    *,
    path: str,
    depth: int,
    field_names: list[tuple[str, str]],
    texts: list[tuple[str, str]],
) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise DecisionMemoryViolation(
            "payload nesting exceeds the admitted depth",
            detail={"path": path, "max_nesting_depth": MAX_NESTING_DEPTH},
        )
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise DecisionMemoryViolation(
                    "payload contains a non-string field name",
                    detail={"path": path, "received_type": type(key).__name__},
                )
            field_names.append((f"{path}.{key}", key))
            _collect(
                item, path=f"{path}.{key}", depth=depth + 1, field_names=field_names, texts=texts
            )
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _collect(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                field_names=field_names,
                texts=texts,
            )
        return
    if isinstance(value, str):
        texts.append((path, value))
        return
    if value is None or type(value) in (bool, int, float):
        return
    raise DecisionMemoryViolation(
        "payload contains a value outside the JSON data model",
        detail={"path": path, "received_type": type(value).__name__},
    )


def _refuse_forbidden_field_names(field_names: list[tuple[str, str]]) -> None:
    offending = sorted({path for path, key in field_names if key.lower() in FORBIDDEN_FIELD_NAMES})
    if offending:
        raise SensitiveContentRefused(
            "payload declares a field name this contract must never carry",
            detail={"paths": offending[:16], "count": len(offending)},
        )


def _refuse_oversized_text(texts: list[tuple[str, str]]) -> None:
    for path, text in texts:
        if len(text) > MAX_ADMITTED_TEXT_LENGTH:
            raise SensitiveContentRefused(
                "payload carries text beyond the admitted length",
                detail={
                    "path": path,
                    "length": len(text),
                    "max_admitted_text_length": MAX_ADMITTED_TEXT_LENGTH,
                },
            )


def _refuse_credential_shapes(texts: list[tuple[str, str]]) -> None:
    """Refuse the named shapes. The matched text is never echoed back."""
    for path, text in texts:
        for name, pattern in _CREDENTIAL_SHAPES:
            if pattern.search(text):
                raise SensitiveContentRefused(
                    "payload carries text matching a known credential shape",
                    detail={"path": path, "shape": name},
                )


def screen_located_text(texts: Sequence[tuple[str, str]]) -> None:
    """Apply the named, non-echoing text screen to already-located strings.

    Same bounds and same shape list as :func:`screen_persisted_text`; this form
    takes ``(path, text)`` pairs so a caller that walked a payload can report
    *where* the refusal fired. Exposed so the HOK-244 reconciliation journal
    screens against this one named list rather than keeping a second copy of it
    that could drift.
    """
    located = list(texts)
    _refuse_oversized_text(located)
    _refuse_credential_shapes(located)


def screen_persisted_text(**values: str) -> None:
    """Apply the named, non-echoing text screen to durable operator metadata."""
    screen_located_text([(f"$.{name}", value) for name, value in values.items()])


def _refuse_unclassified(payload: dict[str, object]) -> None:
    """Require both explicit opt-ins, from the raw payload, by name."""
    sensitivity = payload.get("sensitivity")
    if sensitivity != SensitivityClassification.NON_SENSITIVE.value:
        raise SensitiveContentRefused(
            "decision memory requires an explicit NON_SENSITIVE classification",
            detail={
                "declared": sensitivity if isinstance(sensitivity, str) else None,
                "required": SensitivityClassification.NON_SENSITIVE.value,
                "reason": "absent" if sensitivity is None else "refused",
            },
        )
    impact = payload.get("impact_class")
    if impact != DecisionImpactClass.STRATEGIC_HIGH_IMPACT.value:
        raise SensitiveContentRefused(
            "decision memory is opt-in and only admits STRATEGIC_HIGH_IMPACT records",
            detail={
                "declared": impact if isinstance(impact, str) else None,
                "required": DecisionImpactClass.STRATEGIC_HIGH_IMPACT.value,
                "reason": "absent" if impact is None else "refused",
            },
        )


def _screen(payload: object, *, context: str) -> dict[str, object]:
    """Structure, canonical encodability, bounds and content screening."""
    if not isinstance(payload, dict):
        raise DecisionMemoryViolation(
            f"{context} must be a JSON object",
            detail={"context": context, "received_type": type(payload).__name__},
        )
    field_names: list[tuple[str, str]] = []
    texts: list[tuple[str, str]] = []
    _collect(payload, path="$", depth=0, field_names=field_names, texts=texts)
    try:
        encoded = canonical_bytes(payload)
    except (RecursionError, TypeError, ValueError) as exc:
        raise DecisionMemoryViolation(
            f"{context} has no reproducible canonical encoding",
            detail={"context": context, "cause": str(exc)},
        ) from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise SensitiveContentRefused(
            f"{context} exceeds the admitted canonical size",
            detail={
                "context": context,
                "canonical_bytes": len(encoded),
                "max_canonical_bytes": MAX_CANONICAL_BYTES,
            },
        )
    _refuse_forbidden_field_names(field_names)
    _refuse_oversized_text(texts)
    _refuse_credential_shapes(texts)
    return payload


def _declared_version(payload: dict[str, object], *, context: str) -> str:
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise DecisionMemoryViolation(
            f"{context} must declare contract_version",
            detail={"context": context, "reason": "absent"},
        )
    return version


def admit_strategic_decision(payload: object) -> StrategicDecisionRecord:
    """Screen and validate one native record. The only way to obtain one.

    Every refusal raised here happens before any transaction is opened, so a
    refused payload cannot move a store's record count, generation or root seal.
    """
    screened = _screen(payload, context="strategic decision record")
    check_contract_version(
        _declared_version(screened, context="strategic decision record"),
        SUPPORTED_DECISION_MEMORY_VERSIONS,
        "strategic decision record",
    )
    _refuse_unclassified(screened)
    return validate_contract(
        StrategicDecisionRecord,
        screened,
        error=DecisionMemoryViolation,
        context="strategic decision record",
    )


def admit_transfer_envelope(payload: object) -> DecisionTransferEnvelope:
    """Screen and validate one transfer envelope, including the record it carries.

    The carried record is screened by the same rules as a native one: an import
    is not a way around admission.
    """
    screened = _screen(payload, context="decision transfer envelope")
    check_contract_version(
        _declared_version(screened, context="decision transfer envelope"),
        SUPPORTED_DECISION_TRANSFER_VERSIONS,
        "decision transfer envelope",
    )
    carried = screened.get("record")
    if not isinstance(carried, dict):
        raise TransferRefused(
            "transfer envelope must carry one strategic decision record",
            detail={"reason": "absent" if carried is None else "malformed"},
        )
    _refuse_unclassified(carried)
    return validate_contract(
        DecisionTransferEnvelope,
        screened,
        error=TransferRefused,
        context="decision transfer envelope",
    )

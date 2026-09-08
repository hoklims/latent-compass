"""HOK-244 — admission: everything that must refuse *before* a durable write.

Admission is a pure function of the payload. It opens no file, holds no
transaction and mutates nothing, so a refusal here provably leaves the journal's
entry count, generation and root seal untouched.

What admission refuses
----------------------
* a payload that is not a JSON object, or that declares no contract version;
* a payload that does not encode canonically — non-finite numbers, non-string
  mapping keys, values that have no reproducible encoding;
* a nesting depth or a canonical size beyond the declared bounds;
* any field name from the forbidden set, at any depth — the selection, scoring,
  ranking, learning, promotion, causal-claim, holdout and credential
  vocabularies;
* anything but an explicit ``NON_SENSITIVE`` classification: ``UNKNOWN`` and
  ``HOLDOUT`` are named values and both are refused;
* text longer than the admission bound, before the schema's own bounds apply;
* text matching one of the credential shapes named by HOK-243.

Why the forbidden set differs from HOK-243's
--------------------------------------------
The two sets are deliberately *not* the same. HOK-243 refuses the whole
post-action vocabulary because a pre-action record must not carry an outcome.
HOK-244 exists to carry outcomes, so it refuses one step further out: the
vocabulary that would turn an observation into a *judgment* — a score, a rank, a
reward, a preference, a winner, a verdict, a promotion, a causal effect, a
counterfactual. ``execution_state`` and ``authorization_state`` are the point of
this contract and are therefore admissible field names, while bare ``execution``,
``authorization`` and ``argv`` stay refused so no free-form execution blob or
authority grant can ride along beside them.

What admission does **not** claim
---------------------------------
The credential screen is the one named, non-universal screen HOK-243 publishes —
imported, not re-implemented, so the two families cannot drift. It matches a
short, named list of well-known shapes and a labelled-assignment form. A secret
that looks like ordinary prose passes, and a caller who admits one has admitted
it. Classifying content is the caller's obligation.
"""

from __future__ import annotations

from typing import Final

from latent_compass.canonical import canonical_bytes
from latent_compass.contracts import (
    SUPPORTED_RECONCILIATION_VERSIONS,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.admission import (
    MAX_ADMITTED_TEXT_LENGTH,
    MAX_CANONICAL_BYTES,
    MAX_NESTING_DEPTH,
    screen_located_text,
)
from latent_compass.decision_memory.contracts import SensitivityClassification
from latent_compass.decision_reconciliation.contracts import (
    AuthorizationState,
    ExecutionState,
    ObservationStatus,
    ReconciliationRecord,
    UnknownReason,
)
from latent_compass.errors import ReconciliationViolation, SensitiveContentRefused
from latent_compass.pairwise_capture import DIMENSION_ORDER

__all__ = [
    "FORBIDDEN_FIELD_NAMES",
    "admit_reconciliation",
    "reconciliation_limits",
]

#: Field names refused at any depth, lowercased.
#:
#: The closed schema already forbids unknown fields, so this set is not what
#: keeps them out — it is what makes the refusal *legible*: a payload carrying
#: ``score`` is told it carried a forbidden field, not that some nested model
#: rejected an extra key.
FORBIDDEN_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        # route selection — a reconciliation observes; it never chooses
        "choice",
        "chosen",
        "chosen_route",
        "decide",
        "decision",
        "route",
        "routes",
        "selected_candidate",
        "selected_direction_id",
        "selected_route",
        # scoring, ranking and learning
        "advantage",
        "gradient",
        "label",
        "labels",
        "learning_rate",
        "learned_policy",
        "policy_update",
        "preference",
        "rank",
        "ranked",
        "ranking",
        "reward",
        "rewards",
        "score",
        "scores",
        "training_label",
        "weight",
        "weights",
        "winner",
        # verdicts, free-form outcomes and causal claims
        "attribution",
        "causal_effect",
        "counterfactual",
        "counterfactual_outcome",
        "deferred_outcome",
        "external_verdict",
        "outcome",
        "treatment_effect",
        "uplift",
        "verdict",
        # promotion and execution authority: the *states* are named
        # authorization_state / execution_state, and nothing wider is admissible
        "argv",
        "authorisation",
        "authorization",
        "authorised",
        "authorized",
        "capability",
        "command",
        "execute",
        "execution",
        "grant",
        "promote",
        "promotion",
        # evaluation and holdout metadata
        "holdout",
        "holdout_corpus_seal",
        "holdout_ledger",
        "split",
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


def reconciliation_limits() -> dict[str, object]:
    """Machine-readable statement of what admission bounds, refuses and disclaims."""
    return {
        "max_admitted_text_length": MAX_ADMITTED_TEXT_LENGTH,
        "max_canonical_bytes": MAX_CANONICAL_BYTES,
        "max_nesting_depth": MAX_NESTING_DEPTH,
        "forbidden_field_names": sorted(FORBIDDEN_FIELD_NAMES),
        "credential_screen": "latent_compass.decision_memory.admission.screen_located_text",
        "required_sensitivity": SensitivityClassification.NON_SENSITIVE.value,
        "observation_dimensions": [dimension.value for dimension in DIMENSION_ORDER],
        "observation_statuses": [status.value for status in ObservationStatus],
        "unknown_reasons": [reason.value for reason in UnknownReason],
        "authorization_states": [state.value for state in AuthorizationState],
        "execution_states": [state.value for state in ExecutionState],
        "universal_secret_detection": False,
        "produces_scalar_score": False,
        "produces_ranking": False,
        "produces_causal_claim": False,
        "grants_authority": False,
        "note": (
            "the journal records observations about one executed candidate and the "
            "explicit unknowns beside them; it computes no score, no ranking and no "
            "causal claim, it authorises nothing, and it rewrites no pre-action record"
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
        raise ReconciliationViolation(
            "payload nesting exceeds the admitted depth",
            detail={"path": path, "max_nesting_depth": MAX_NESTING_DEPTH},
        )
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReconciliationViolation(
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
    raise ReconciliationViolation(
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


def _refuse_unclassified(payload: dict[str, object]) -> None:
    """Require the explicit classification, from the raw payload, by name."""
    sensitivity = payload.get("sensitivity")
    if sensitivity != SensitivityClassification.NON_SENSITIVE.value:
        raise SensitiveContentRefused(
            "a reconciliation requires an explicit NON_SENSITIVE classification",
            detail={
                "declared": sensitivity if isinstance(sensitivity, str) else None,
                "required": SensitivityClassification.NON_SENSITIVE.value,
                "reason": "absent" if sensitivity is None else "refused",
            },
        )


def _screen(payload: object, *, context: str) -> dict[str, object]:
    """Structure, canonical encodability, bounds and content screening."""
    if not isinstance(payload, dict):
        raise ReconciliationViolation(
            f"{context} must be a JSON object",
            detail={"context": context, "received_type": type(payload).__name__},
        )
    field_names: list[tuple[str, str]] = []
    texts: list[tuple[str, str]] = []
    _collect(payload, path="$", depth=0, field_names=field_names, texts=texts)
    try:
        encoded = canonical_bytes(payload)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ReconciliationViolation(
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
    screen_located_text(texts)
    return payload


def _declared_version(payload: dict[str, object], *, context: str) -> str:
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise ReconciliationViolation(
            f"{context} must declare contract_version",
            detail={"context": context, "reason": "absent"},
        )
    return version


def admit_reconciliation(payload: object) -> ReconciliationRecord:
    """Screen and validate one reconciliation. The only way to obtain one.

    Every refusal raised here happens before any transaction is opened, so a
    refused payload cannot move a journal's entry count, generation or root seal.
    """
    screened = _screen(payload, context="reconciliation record")
    check_contract_version(
        _declared_version(screened, context="reconciliation record"),
        SUPPORTED_RECONCILIATION_VERSIONS,
        "reconciliation record",
    )
    _refuse_unclassified(screened)
    return validate_contract(
        ReconciliationRecord,
        screened,
        error=ReconciliationViolation,
        context="reconciliation record",
    )

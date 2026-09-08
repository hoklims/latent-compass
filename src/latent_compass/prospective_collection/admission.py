"""Strict JSON-only, bounded admission for HOK-252 plans."""

from __future__ import annotations

from typing import Final

from latent_compass.canonical import canonical_bytes
from latent_compass.contracts import (
    SUPPORTED_PROSPECTIVE_COLLECTION_VERSIONS,
    check_contract_version,
    validate_contract,
)
from latent_compass.decision_memory.admission import (
    MAX_ADMITTED_TEXT_LENGTH,
    MAX_CANONICAL_BYTES,
    MAX_NESTING_DEPTH,
    screen_located_text,
)
from latent_compass.errors import ProspectiveCollectionViolation
from latent_compass.prospective_collection.contracts import ProspectiveCollectionPlan

__all__ = ["admit_prospective_plan", "prospective_collection_limits"]

FORBIDDEN_FIELD_NAMES: Final = frozenset(
    {
        "action",
        "argv",
        "authority",
        "bandit",
        "canary",
        "causal_effect",
        "command",
        "counterfactual",
        "credential",
        "credentials",
        "holdout",
        "holdout_corpus_seal",
        "label",
        "labels",
        "password",
        "policy_update",
        "private_key",
        "promote",
        "promotion",
        "rank",
        "ranking",
        "reward",
        "route",
        "routing",
        "score",
        "secret",
        "token",
        "training_label",
        "treatment_effect",
        "weight",
        "winner",
    }
)


def _collect(
    value: object,
    *,
    path: str,
    depth: int,
    names: list[tuple[str, str]],
    texts: list[tuple[str, str]],
) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise ProspectiveCollectionViolation(
            "payload nesting exceeds the admitted depth",
            detail={"path": path, "max_nesting_depth": MAX_NESTING_DEPTH},
        )
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProspectiveCollectionViolation(
                    "payload contains a non-string field name", detail={"path": path}
                )
            names.append((f"{path}.{key}", key))
            _collect(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                names=names,
                texts=texts,
            )
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _collect(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                names=names,
                texts=texts,
            )
        return
    if type(value) is str:
        texts.append((path, value))
        return
    if value is None or type(value) in (bool, int, float):
        return
    raise ProspectiveCollectionViolation(
        "payload contains a value outside the JSON data model",
        detail={"path": path, "received_type": type(value).__name__},
    )


def admit_prospective_plan(payload: object) -> ProspectiveCollectionPlan:
    if type(payload) is not dict:
        raise ProspectiveCollectionViolation("prospective plan must be a JSON object")
    names: list[tuple[str, str]] = []
    texts: list[tuple[str, str]] = []
    _collect(payload, path="$", depth=0, names=names, texts=texts)
    forbidden = sorted(path for path, name in names if name.lower() in FORBIDDEN_FIELD_NAMES)
    if forbidden:
        raise ProspectiveCollectionViolation(
            "prospective plan carries a forbidden field",
            detail={"paths": forbidden[:16], "count": len(forbidden)},
        )
    screen_located_text(texts)
    try:
        encoded = canonical_bytes(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProspectiveCollectionViolation(
            "prospective plan has no reproducible canonical encoding",
            detail={"cause": str(exc)},
        ) from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise ProspectiveCollectionViolation(
            "prospective plan exceeds the admitted canonical size",
            detail={"canonical_bytes": len(encoded), "max_canonical_bytes": MAX_CANONICAL_BYTES},
        )
    version = payload.get("contract_version")
    if type(version) is not str:
        raise ProspectiveCollectionViolation("prospective plan must declare contract_version")
    check_contract_version(
        version, SUPPORTED_PROSPECTIVE_COLLECTION_VERSIONS, "prospective collection plan"
    )
    return validate_contract(
        ProspectiveCollectionPlan,
        payload,
        error=ProspectiveCollectionViolation,
        context="prospective collection plan",
    )


def prospective_collection_limits() -> dict[str, object]:
    return {
        "max_admitted_text_length": MAX_ADMITTED_TEXT_LENGTH,
        "max_canonical_bytes": MAX_CANONICAL_BYTES,
        "max_nesting_depth": MAX_NESTING_DEPTH,
        "forbidden_field_names": sorted(FORBIDDEN_FIELD_NAMES),
        "reads_holdout": False,
        "grants_authority": False,
        "influences_routing": False,
        "produces_causal_claim": False,
    }

"""HOK-234 — hostile proofs for the judgeable pre-action sidecar."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from conftest import episode_payload
from latent_compass.benchmark.corpus import PublicCandidateView
from latent_compass.contracts import JUDGEABLE_PROJECTION_CONTRACT_VERSION
from latent_compass.episode import Candidate, load_episode
from latent_compass.errors import (
    ContractViolation,
    PairwiseCaptureViolation,
    UnsupportedContractVersion,
)
from latent_compass.pairwise_capture import (
    CANONICALIZATION_VERSION,
    EvidenceAvailability,
    JudgeableDecisionProjection,
    JudgmentDimension,
    ScalarKind,
    load_judgeable_projection,
    load_pairwise_judge_input,
    verify_pairwise_judge_input,
)


def _source_digest(seed: str) -> str:
    return "sha256:" + seed * 64


def _evidence(prefix: str) -> list[dict[str, Any]]:
    return [
        {
            "dimension": dimension.value,
            "availability": EvidenceAvailability.PRESENT.value,
            "facts": [
                {
                    "fact_id": f"{prefix}-{index:02d}",
                    "statement": f"Sanitized pre-action fact for {dimension.value.lower()}",
                    "source_digest": _source_digest(str(index + 1)),
                }
            ],
            "reason": None,
        }
        for index, dimension in enumerate(JudgmentDimension)
    ]


def projection_payload() -> dict[str, Any]:
    return {
        "contract_version": JUDGEABLE_PROJECTION_CONTRACT_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "decision_point_id": "decision-0001",
        "captured_at": "2026-08-16T17:00:00Z",
        "producer_id": "capture-agent-v1",
        "objective_summary": "Choose the safest reversible implementation direction.",
        "context_facts": [
            {
                "fact_id": "context-01",
                "statement": "The public contract must remain backward compatible.",
                "source_digest": _source_digest("a"),
            }
        ],
        "constraints": [
            {
                "constraint_id": "constraint-01",
                "kind": "INVARIANT",
                "statement": "The selected direction must not mutate authority.",
                "source_digest": _source_digest("b"),
            }
        ],
        "candidates": [
            {
                "direction_id": "direction-alpha",
                "sanitized_summary": "Add a separate immutable pre-action contract.",
                "parameters": [
                    {
                        "name": "max-items",
                        "value": {"kind": ScalarKind.INTEGER.value, "integer_value": 32},
                    }
                ],
                "applicable_constraint_ids": ["constraint-01"],
                "evidence": _evidence("alpha"),
            },
            {
                "direction_id": "direction-beta",
                "sanitized_summary": "Extend a post-action record with optional fields.",
                "parameters": [
                    {
                        "name": "enabled",
                        "value": {"kind": ScalarKind.BOOLEAN.value, "boolean_value": False},
                    }
                ],
                "applicable_constraint_ids": ["constraint-01"],
                "evidence": _evidence("beta"),
            },
        ],
    }


def test_projection_loads_and_seals_deterministically() -> None:
    first = load_judgeable_projection(projection_payload())
    second = load_judgeable_projection(deepcopy(projection_payload()))

    assert first == second
    assert first.projection_seal() == second.projection_seal()


def test_pair_derivation_is_canonical_under_reversed_requests() -> None:
    projection = load_judgeable_projection(projection_payload())

    forward = projection.derive_pair("direction-alpha", "direction-beta")
    reverse = projection.derive_pair("direction-beta", "direction-alpha")

    assert forward.canonical_payload() == reverse.canonical_payload()
    assert forward.input_seal() == reverse.input_seal()
    assert [item.direction_id for item in forward.candidates] == [
        "direction-alpha",
        "direction-beta",
    ]
    assert forward.input_seal() != projection.projection_seal()


def test_derived_pair_reloads_without_post_action_context() -> None:
    projection = load_judgeable_projection(projection_payload())
    pair = projection.derive_pair("direction-alpha", "direction-beta")

    reloaded = load_pairwise_judge_input(pair.canonical_payload())

    assert reloaded == pair
    forbidden = {
        "captured_at",
        "producer_id",
        "propensity",
        "prior_uncertainty",
        "selected_direction_id",
        "outcome",
        "external_verdict",
        "economics",
        "split",
        "holdout",
    }
    assert forbidden.isdisjoint(reloaded.canonical_payload())


@pytest.mark.parametrize(
    "field",
    [
        "selected_direction_id",
        "advisory",
        "ranker_score",
        "outcome",
        "external_verdict",
        "economics",
        "split",
        "holdout_metadata",
    ],
)
def test_post_action_or_holdout_fields_are_refused_at_the_projection_boundary(field: str) -> None:
    payload = projection_payload()
    payload[field] = "forbidden"

    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


@pytest.mark.parametrize("field", ["propensity", "prior_uncertainty", "selected", "outcome"])
def test_forbidden_fields_are_refused_inside_a_candidate(field: str) -> None:
    payload = projection_payload()
    payload["candidates"][0][field] = 0.5

    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


def test_every_dimension_is_required_once_and_in_canonical_order() -> None:
    missing = projection_payload()
    missing["candidates"][0]["evidence"].pop()
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(missing)

    duplicate = projection_payload()
    duplicate["candidates"][0]["evidence"][-1] = deepcopy(duplicate["candidates"][0]["evidence"][0])
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(duplicate)

    reordered = projection_payload()
    reordered["candidates"][0]["evidence"].reverse()
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(reordered)


def test_not_judgeable_is_explicit_and_cannot_carry_facts() -> None:
    payload = projection_payload()
    evidence = payload["candidates"][0]["evidence"][0]
    evidence.update(
        availability=EvidenceAvailability.NOT_JUDGEABLE.value,
        facts=[],
        reason="No pre-action evidence is available for this dimension.",
    )
    projection = load_judgeable_projection(payload)
    assert projection.candidates[0].evidence[0].availability is EvidenceAvailability.NOT_JUDGEABLE

    payload["candidates"][0]["evidence"][0]["facts"] = _evidence("hostile")[0]["facts"]
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


def test_present_evidence_cannot_pose_as_empty_or_reasoned() -> None:
    empty = projection_payload()
    empty["candidates"][0]["evidence"][0]["facts"] = []
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(empty)

    reasoned = projection_payload()
    reasoned["candidates"][0]["evidence"][0]["reason"] = "hedged"
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(reasoned)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
    ],
)
def test_parameter_scalars_refuse_non_finite_values(value: float) -> None:
    payload = projection_payload()
    scalar = payload["candidates"][0]["parameters"][0]["value"]
    scalar.clear()
    scalar.update(kind=ScalarKind.FLOAT.value, float_value=value)

    with pytest.raises(ContractViolation):
        load_judgeable_projection(payload)


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        (ScalarKind.INTEGER.value, "integer_value", "32"),
        (ScalarKind.BOOLEAN.value, "boolean_value", 1),
    ],
)
def test_parameter_scalars_refuse_coercion(kind: str, field: str, value: object) -> None:
    payload = projection_payload()
    scalar = payload["candidates"][0]["parameters"][0]["value"]
    scalar.clear()
    scalar.update(kind=kind)
    scalar[field] = value

    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        (ScalarKind.INTEGER.value, "integer_value", 1_000_000_000_001),
        (ScalarKind.INTEGER.value, "integer_value", -1_000_000_000_001),
        (ScalarKind.FLOAT.value, "float_value", 1_000_000_000_001.0),
        (ScalarKind.FLOAT.value, "float_value", -1_000_000_000_001.0),
    ],
)
def test_parameter_scalars_refuse_values_outside_declared_bounds(
    kind: str, field: str, value: object
) -> None:
    payload = projection_payload()
    scalar = payload["candidates"][0]["parameters"][0]["value"]
    scalar.clear()
    scalar.update(kind=kind)
    scalar[field] = value

    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        (ScalarKind.INTEGER.value, "integer_value", 1_000_000_000_000),
        (ScalarKind.INTEGER.value, "integer_value", -1_000_000_000_000),
        (ScalarKind.FLOAT.value, "float_value", 1_000_000_000_000.0),
        (ScalarKind.FLOAT.value, "float_value", -1_000_000_000_000.0),
    ],
)
def test_parameter_scalar_bounds_are_inclusive(kind: str, field: str, value: object) -> None:
    payload = projection_payload()
    scalar = payload["candidates"][0]["parameters"][0]["value"]
    scalar.clear()
    scalar.update(kind=kind)
    scalar[field] = value

    loaded = load_judgeable_projection(payload)
    assert loaded.candidates[0].parameters[0].value.kind.value == kind


def test_float_kind_refuses_a_json_integer() -> None:
    payload = projection_payload()
    scalar = payload["candidates"][0]["parameters"][0]["value"]
    scalar.clear()
    scalar.update(kind=ScalarKind.FLOAT.value, float_value=32)

    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


def test_pair_loader_refuses_noncanonical_or_dangling_context() -> None:
    pair = load_judgeable_projection(projection_payload()).derive_pair(
        "direction-alpha", "direction-beta"
    )

    noncanonical = cast(dict[str, Any], pair.canonical_payload())
    noncanonical["context_facts"] = [
        {**noncanonical["context_facts"][0], "fact_id": "context-02"},
        noncanonical["context_facts"][0],
    ]
    with pytest.raises(PairwiseCaptureViolation):
        load_pairwise_judge_input(noncanonical)

    dangling = cast(dict[str, Any], pair.canonical_payload())
    dangling["candidates"][0]["applicable_constraint_ids"] = ["constraint-missing"]
    with pytest.raises(PairwiseCaptureViolation):
        load_pairwise_judge_input(dangling)


def test_pair_verification_requires_the_projection_preimage() -> None:
    projection = load_judgeable_projection(projection_payload())
    pair = projection.derive_pair("direction-alpha", "direction-beta")

    assert verify_pairwise_judge_input(pair.canonical_payload(), projection) == pair

    forged_seal = cast(dict[str, Any], pair.canonical_payload())
    forged_seal["source_projection_seal"] = "sha256:" + "0" * 64
    structurally_loaded = load_pairwise_judge_input(forged_seal)
    assert structurally_loaded.source_projection_seal == "sha256:" + "0" * 64
    with pytest.raises(PairwiseCaptureViolation):
        verify_pairwise_judge_input(forged_seal, projection)

    substituted = cast(dict[str, Any], pair.canonical_payload())
    substituted["candidates"][0]["sanitized_summary"] = "Substituted content."
    with pytest.raises(PairwiseCaptureViolation):
        verify_pairwise_judge_input(substituted, projection)


def test_scalar_kind_must_match_the_single_value_field() -> None:
    payload = projection_payload()
    payload["candidates"][0]["parameters"][0]["value"]["string_value"] = "also-present"
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(payload)


def test_text_is_bounded_and_control_characters_are_refused() -> None:
    oversized = projection_payload()
    oversized["objective_summary"] = "x" * 1001
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(oversized)

    controlled = projection_payload()
    controlled["candidates"][0]["sanitized_summary"] = "secret\nsecond line"
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(controlled)


def test_dangling_constraints_and_noncanonical_collections_are_refused() -> None:
    dangling = projection_payload()
    dangling["candidates"][0]["applicable_constraint_ids"] = ["constraint-missing"]
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(dangling)

    reordered = projection_payload()
    reordered["candidates"].reverse()
    with pytest.raises(PairwiseCaptureViolation):
        load_judgeable_projection(reordered)


def test_pair_requires_two_known_distinct_candidates() -> None:
    projection = load_judgeable_projection(projection_payload())
    with pytest.raises(PairwiseCaptureViolation):
        projection.derive_pair("direction-alpha", "direction-alpha")
    with pytest.raises(PairwiseCaptureViolation):
        projection.derive_pair("direction-alpha", "direction-missing")


def test_unknown_projection_version_fails_closed() -> None:
    payload = projection_payload()
    payload["contract_version"] = "2.0.0"
    with pytest.raises(UnsupportedContractVersion):
        load_judgeable_projection(payload)


def test_episode_v1_and_baseline_candidate_contracts_remain_exact() -> None:
    before = episode_payload()
    episode = load_episode(deepcopy(before))

    assert episode.canonical_payload() == before
    assert set(Candidate.model_fields) == {"direction_id", "propensity", "prior_uncertainty"}
    assert set(PublicCandidateView.model_fields) == {
        "direction_id",
        "propensity",
        "prior_uncertainty",
    }
    assert "judgeable_projection" not in JudgeableDecisionProjection.model_fields

"""HOK-185 — proofs that the episode contract refuses rather than repairs."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import episode_payload
from latent_compass.canonical import canonical_bytes, seal
from latent_compass.episode import Episode, load_episode
from latent_compass.errors import (
    ContractViolation,
    EpisodeValidationError,
    UnsupportedContractVersion,
)


def test_the_reference_episode_is_valid() -> None:
    episode = load_episode(episode_payload())
    assert episode.episode_id == "ep-00000001"
    assert episode.content_seal().startswith("sha256:")


def test_a_missing_provenance_refuses_the_episode() -> None:
    payload = episode_payload()
    del payload["provenance"]
    with pytest.raises(EpisodeValidationError) as refusal:
        load_episode(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert any(item["location"] == "provenance" for item in detail["violations"])


@pytest.mark.parametrize("field", ["host_id", "agent_family", "store_id", "epoch", "recorded_at"])
def test_every_provenance_field_is_mandatory(field: str) -> None:
    payload = episode_payload()
    del payload["provenance"][field]
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


@pytest.mark.parametrize(
    ("version", "reason"),
    [("2.0.0", "future"), ("1.1.0", "future"), ("0.9.0", "unknown")],
)
def test_an_unsupported_version_is_refused_with_its_reason(version: str, reason: str) -> None:
    with pytest.raises(UnsupportedContractVersion) as refusal:
        load_episode(episode_payload(schema_version=version))
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == reason
    assert detail["version"] == version


@pytest.mark.parametrize("version", ["1.0", "one.0.0", "1.0.0.0", "-1.0.0", ""])
def test_a_malformed_version_is_refused_not_guessed(version: str) -> None:
    with pytest.raises(UnsupportedContractVersion) as refusal:
        load_episode(episode_payload(schema_version=version))
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert detail["reason"] == "malformed"


def test_an_injected_top_level_action_field_is_refused() -> None:
    payload = episode_payload()
    payload["action"] = {"execute": "promote", "target": "main"}
    with pytest.raises(EpisodeValidationError) as refusal:
        load_episode(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert any(item["location"] == "action" for item in detail["violations"])


def test_an_injected_nested_authority_field_is_refused() -> None:
    payload = episode_payload()
    payload["decision"]["advisory"]["authority"] = "promote"
    with pytest.raises(EpisodeValidationError) as refusal:
        load_episode(payload)
    detail = refusal.value.detail
    assert isinstance(detail, dict)
    assert any(item["location"] == "decision.advisory.authority" for item in detail["violations"])


@pytest.mark.parametrize("smuggled", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_cannot_enter_the_contract(smuggled: float) -> None:
    payload = episode_payload()
    payload["economics"]["cost"] = smuggled
    with pytest.raises(ContractViolation):
        load_episode(payload)


def test_a_non_finite_number_parsed_from_json_text_is_also_refused() -> None:
    """``json.loads`` accepts the ``NaN`` literal, so the contract must not."""
    import json

    text = json.dumps(episode_payload()).replace('"cost": 1.5', '"cost": NaN')
    decoded = json.loads(text)
    assert decoded["economics"]["cost"] != decoded["economics"]["cost"]  # it really is NaN
    with pytest.raises(ContractViolation):
        load_episode(decoded)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("economics", "cost"), "2.0"),
        (("economics", "reversibility"), "0.9"),
        (("outcome", "observed"), 1),
        (("outcome", "violations"), "0"),
        (("decision", "advisory", "confidence"), "0.7"),
    ],
)
def test_type_coercions_are_refused(path: tuple[str, ...], value: Any) -> None:
    payload = episode_payload()
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_an_integral_json_number_is_accepted_for_a_float_field() -> None:
    """A measured, deliberate limit of JSON strict mode, asserted so it cannot drift.

    JSON has a single number type, so ``2`` and ``2.0`` are the same literal.
    Refusing ``2`` here would refuse well-formed JSON rather than catch a
    coercion. String-to-number, number-to-bool and string-to-int all remain
    refused, as the surrounding test proves.
    """
    payload = episode_payload()
    payload["economics"]["cost"] = 2
    assert load_episode(payload).economics.cost == 2.0


def test_propensities_must_form_a_distribution() -> None:
    payload = episode_payload()
    payload["candidates"][1]["propensity"] = 0.3
    with pytest.raises(EpisodeValidationError, match="strict contract validation"):
        load_episode(payload)


def test_a_dropped_candidate_cannot_hide_behind_the_tolerance() -> None:
    payload = episode_payload()
    payload["candidates"] = [payload["candidates"][0]]
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_duplicate_candidate_directions_are_refused() -> None:
    payload = episode_payload()
    payload["candidates"][1]["direction_id"] = payload["candidates"][0]["direction_id"]
    payload["candidates"][0]["propensity"] = 0.5
    payload["candidates"][1]["propensity"] = 0.5
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_a_selected_direction_must_be_among_the_candidates() -> None:
    payload = episode_payload()
    payload["decision"]["advisory"]["direction_id"] = "dir-not-offered"
    payload["decision"]["selected_direction_id"] = "dir-not-offered"
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_a_zero_propensity_candidate_is_refused() -> None:
    """Propensity zero means "could not be selected", which is not a candidate."""
    payload = episode_payload()
    payload["candidates"].append(
        {"direction_id": "dir-gamma", "propensity": 0.0, "prior_uncertainty": 0.1}
    )
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


@pytest.mark.parametrize(
    "timestamp",
    ["2026-08-14T10:00:00+00:00", "2026-08-14T10:00:00", "2026-08-14T12:00:00+02:00", "not-a-time"],
)
def test_only_one_timestamp_encoding_is_accepted(timestamp: str) -> None:
    payload = episode_payload()
    payload["provenance"]["recorded_at"] = timestamp
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_state_levels_must_be_a_strictly_increasing_ladder() -> None:
    payload = episode_payload()
    payload["state"]["levels"][1]["depth"] = 0
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_the_branch_point_must_sit_on_a_declared_level() -> None:
    payload = episode_payload()
    payload["state"]["branch_point"]["depth"] = 9
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_a_branch_point_cannot_be_its_own_parent() -> None:
    payload = episode_payload()
    payload["state"]["branch_point"]["parent_node_id"] = payload["state"]["branch_point"]["node_id"]
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "observability": "NONE",
            "observed": True,
            "observed_at": "2026-08-14T11:00:00Z",
            "success": True,
            "violations": 0,
        },
        {"observability": "NONE", "observed": False, "success": True},
        {"observability": "FULL", "observed": True, "success": True},
        {"observability": "FULL", "observed": False, "success": True},
        {"observability": "PARTIAL", "observed": True, "observed_at": "2026-08-14T11:00:00Z"},
    ],
)
def test_an_outcome_may_not_claim_more_than_it_observed(outcome: dict[str, Any]) -> None:
    with pytest.raises(EpisodeValidationError):
        load_episode(episode_payload(outcome=outcome))


def test_partial_observability_is_representable() -> None:
    episode = load_episode(
        episode_payload(
            outcome={
                "observability": "PARTIAL",
                "observed": True,
                "observed_at": "2026-08-14T11:00:00Z",
                "success": False,
                "violations": 2,
            }
        )
    )
    assert episode.outcome is not None
    assert episode.outcome.violations == 2


def test_an_absent_outcome_is_valid_because_outcomes_are_deferred() -> None:
    payload = episode_payload()
    payload["outcome"] = None
    assert load_episode(payload).outcome is None


def test_an_external_verdict_is_observed_and_cannot_be_appealed() -> None:
    payload = episode_payload()
    payload["external_verdict"]["appeal"] = "please re-run"
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_abstention_is_representable_without_a_direction() -> None:
    payload = episode_payload()
    payload["decision"] = {
        "advisory": {
            "contract_version": "1.0.0",
            "kind": "ABSTAIN",
            "direction_id": None,
            "confidence": 0.0,
            "uncertainty": 0.8,
            "rationale": "no candidate clears the abstention threshold",
            "issued_by": "latent_compass",
        },
        "selected_direction_id": None,
    }
    episode = load_episode(payload)
    assert episode.decision.selected_direction_id is None


def test_a_payload_that_is_not_an_object_is_refused() -> None:
    non_objects: tuple[object, ...] = ([], "episode", 42, None)
    for payload in non_objects:
        with pytest.raises(EpisodeValidationError, match="must be a JSON object"):
            load_episode(payload)


def test_the_content_seal_ignores_key_order_but_not_content() -> None:
    payload = episode_payload()
    reordered = dict(reversed(list(payload.items())))
    assert load_episode(payload).content_seal() == load_episode(reordered).content_seal()

    changed = episode_payload()
    changed["economics"]["cost"] = 1.6
    assert load_episode(changed).content_seal() != load_episode(payload).content_seal()


def test_the_content_seal_is_domain_separated() -> None:
    payload = load_episode(episode_payload()).canonical_payload()
    assert seal("episode.content", payload) != seal("ledger.chain", payload)


def test_a_validated_episode_is_immutable() -> None:
    episode = load_episode(episode_payload())
    with pytest.raises(ValueError, match="frozen"):
        episode.episode_id = "ep-00000002"


def test_canonical_encoding_is_stable_across_equal_payloads() -> None:
    first = load_episode(episode_payload()).canonical_payload()
    second = load_episode(episode_payload()).canonical_payload()
    assert canonical_bytes(first) == canonical_bytes(second)


def test_redactions_declare_absence_rather_than_hiding_it() -> None:
    payload = episode_payload(
        redactions=[{"path": "external_verdict", "reason": "operator request"}]
    )
    payload["external_verdict"] = None
    episode = load_episode(payload)
    assert episode.redactions[0].path == "external_verdict"
    assert episode.external_verdict is None


def test_duplicate_redaction_paths_are_refused() -> None:
    payload = episode_payload(
        redactions=[
            {"path": "outcome", "reason": "first"},
            {"path": "outcome", "reason": "second"},
        ]
    )
    payload["outcome"] = None
    with pytest.raises(EpisodeValidationError):
        load_episode(payload)


def test_the_episode_model_forbids_construction_bypassing_validation() -> None:
    with pytest.raises(Exception, match=r"validation error|Extra inputs"):
        Episode.model_validate({"episode_id": "ep-00000001"})

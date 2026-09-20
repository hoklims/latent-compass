"""Hostile and functional coverage for latent_compass.lab.model."""

from __future__ import annotations

from typing import Any

import pytest

from latent_compass.errors import UnsupportedContractVersion
from latent_compass.lab.errors import LabContractViolationError
from latent_compass.lab.model import DiagnosisModel, load_model


def two_world_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "1.0.0",
        "model_id": "test-model",
        "worlds": [
            {"id": "world-a", "weight": 1},
            {"id": "world-b", "weight": 1},
        ],
        "probes": [
            {
                "id": "probe-x",
                "cost": 1,
                "outcome_space": ["outcome-lo", "outcome-hi"],
                "outcomes": {"world-a": "outcome-lo", "world-b": "outcome-hi"},
            }
        ],
        "decisions": [
            {
                "id": "decide-a",
                "losses": {"world-a": 0, "world-b": 5},
                "required_evidence": [],
            },
            {
                "id": "decide-abstain",
                "losses": {"world-a": 2, "world-b": 2},
                "required_evidence": [],
            },
        ],
        "abstain_decision_id": "decide-abstain",
    }
    payload.update(overrides)
    return payload


def test_a_complete_model_validates_and_produces_a_stable_seal() -> None:
    model = load_model(two_world_payload())
    assert isinstance(model, DiagnosisModel)
    assert model.model_seal() == load_model(two_world_payload()).model_seal()


def test_an_incoherent_edit_changes_the_seal() -> None:
    baseline = load_model(two_world_payload()).model_seal()
    edited = load_model(two_world_payload(model_id="test-model-2")).model_seal()
    assert baseline != edited


def test_a_future_contract_version_is_refused() -> None:
    with pytest.raises(UnsupportedContractVersion):
        load_model(two_world_payload(contract_version="99.0.0"))


def test_an_unknown_contract_version_is_refused() -> None:
    with pytest.raises(UnsupportedContractVersion):
        load_model(two_world_payload(contract_version="0.0.1"))


def test_a_duplicate_world_id_is_refused() -> None:
    payload = two_world_payload()
    payload["worlds"] = [{"id": "world-a", "weight": 1}, {"id": "world-a", "weight": 2}]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_duplicate_probe_id_is_refused() -> None:
    payload = two_world_payload()
    payload["probes"] = [payload["probes"][0], payload["probes"][0]]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_duplicate_decision_id_is_refused() -> None:
    payload = two_world_payload()
    payload["decisions"] = [payload["decisions"][0], payload["decisions"][0]]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_probe_outcome_table_missing_a_world_is_refused() -> None:
    payload = two_world_payload()
    payload["probes"][0]["outcomes"] = {"world-a": "outcome-lo"}
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_probe_outcome_table_naming_an_extra_world_is_refused() -> None:
    payload = two_world_payload()
    payload["probes"][0]["outcomes"] = {
        "world-a": "outcome-lo",
        "world-b": "outcome-hi",
        "world-c": "outcome-hi",
    }
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_probe_outcome_undeclared_by_its_own_outcome_space_is_refused() -> None:
    payload = two_world_payload()
    payload["probes"][0]["outcomes"]["world-b"] = "outcome-undeclared"
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_decision_loss_table_incomplete_over_worlds_is_refused() -> None:
    payload = two_world_payload()
    payload["decisions"][0]["losses"] = {"world-a": 0}
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_decision_requiring_an_unknown_probe_is_refused() -> None:
    payload = two_world_payload()
    payload["decisions"][0]["required_evidence"] = [
        {"probe_id": "probe-nonexistent", "outcome_id": "outcome-lo"}
    ]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_decision_requiring_an_outcome_undeclared_by_its_probe_is_refused() -> None:
    payload = two_world_payload()
    payload["decisions"][0]["required_evidence"] = [
        {"probe_id": "probe-x", "outcome_id": "outcome-undeclared"}
    ]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_an_unknown_abstain_decision_id_is_refused() -> None:
    payload = two_world_payload(abstain_decision_id="decide-nonexistent")
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_an_abstain_decision_with_required_evidence_is_refused() -> None:
    payload = two_world_payload()
    payload["decisions"][1]["required_evidence"] = [
        {"probe_id": "probe-x", "outcome_id": "outcome-lo"}
    ]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_a_non_admissible_abstain_free_model_still_validates() -> None:
    """Proves the other half: a model with a *conforming* abstain decision is granted."""
    model = load_model(two_world_payload())
    abstain = model.decision_by_id("decide-abstain")
    assert abstain is not None
    assert abstain.required_evidence == ()


def test_a_model_declaring_no_probes_still_validates() -> None:
    payload = two_world_payload()
    payload["probes"] = []
    payload["decisions"][0]["required_evidence"] = []
    model = load_model(payload)
    assert model.probes == ()


def test_a_payload_that_is_not_a_json_object_is_refused() -> None:
    with pytest.raises(LabContractViolationError):
        load_model([1, 2, 3])


def test_a_payload_without_a_declared_version_is_refused() -> None:
    payload = two_world_payload()
    del payload["contract_version"]
    with pytest.raises(LabContractViolationError):
        load_model(payload)


def test_world_count_beyond_the_declared_bound_is_refused() -> None:
    from latent_compass.lab.contracts import MAX_WORLDS

    payload = two_world_payload()
    payload["worlds"] = [
        {"id": f"world-{index:03d}", "weight": 1} for index in range(MAX_WORLDS + 1)
    ]
    payload["probes"] = []
    payload["decisions"][0]["losses"] = {world["id"]: 0 for world in payload["worlds"]}
    payload["decisions"][1]["losses"] = {world["id"]: 1 for world in payload["worlds"]}
    with pytest.raises(LabContractViolationError):
        load_model(payload)

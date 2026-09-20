"""Hostile and functional coverage for latent_compass.lab.state."""

from __future__ import annotations

from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.errors import (
    LabBindingMismatchError,
    LabContractViolationError,
    LabCrossModelStateError,
    LabImpossibleObservationError,
    LabRepeatedProbeError,
    LabReplayViolationError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.state import (
    LabBinding,
    apply_observation,
    initial_state,
    verify_state_history,
)

SOURCE_SCOPE = "sha256:" + "1" * 64
OTHER_SOURCE_SCOPE = "sha256:" + "2" * 64


def two_world_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "1.0.0",
        "model_id": "state-test-model",
        "worlds": [
            {"id": "world-a", "weight": 1},
            {"id": "world-b", "weight": 3},
        ],
        "probes": [
            {
                "id": "probe-x",
                "cost": 1,
                "outcome_space": ["outcome-lo", "outcome-hi"],
                "outcomes": {"world-a": "outcome-lo", "world-b": "outcome-hi"},
            },
            {
                "id": "probe-y",
                "cost": 1,
                "outcome_space": ["outcome-lo", "outcome-hi"],
                "outcomes": {"world-a": "outcome-lo", "world-b": "outcome-lo"},
            },
        ],
        "decisions": [
            {"id": "decide-a", "losses": {"world-a": 0, "world-b": 5}, "required_evidence": []},
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


def model() -> DiagnosisModel:
    return load_model(two_world_payload())


def binding() -> LabBinding:
    return LabBinding(
        host_id="host-alpha", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )


def test_history_rejects_an_episode_id_change() -> None:
    m = model()
    zero = initial_state(m, state_id="episode", binding=binding())
    one = apply_observation(
        m,
        zero,
        observation_id="obs",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    forged = one.model_copy(update={"state_id": "different-episode"})
    with pytest.raises(LabReplayViolationError):
        verify_state_history(m, binding(), [zero, forged])


def test_an_observation_id_cannot_be_reused_for_another_probe() -> None:
    m = model()
    zero = initial_state(m, state_id="episode", binding=binding())
    one = apply_observation(
        m,
        zero,
        observation_id="obs",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            one,
            observation_id="obs",
            probe_id="probe-y",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T01:00:00Z",
            expected_binding=binding(),
            history=[zero, one],
        )


def test_observation_time_cannot_move_backwards() -> None:
    m = model()
    zero = initial_state(m, state_id="episode", binding=binding())
    one = apply_observation(
        m,
        zero,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            one,
            observation_id="obs-2",
            probe_id="probe-y",
            outcome_id="outcome-lo",
            observed_at="2026-09-17T23:59:59Z",
            expected_binding=binding(),
            history=[zero, one],
        )


def test_revision_zero_carries_the_models_declared_prior_unfiltered() -> None:
    state = initial_state(model(), state_id="episode-1", binding=binding())
    assert state.revision == 0
    assert state.posterior_weights == {"world-a": 1, "world-b": 3}
    assert state.prior_state_seal is None
    assert state.applied_observation is None


def test_applying_a_consistent_observation_filters_the_posterior_exactly() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    assert state1.revision == 1
    assert state1.posterior_weights == {"world-a": 1}
    assert state1.prior_state_seal == state0.state_seal()
    assert state1.acquired_probe_ids() == frozenset({"probe-x"})


def test_an_impossible_observation_is_refused_and_the_prior_is_reported_unchanged() -> None:
    # "outcome-never" is a declared outcome no world's table ever produces, so
    # filtering the posterior by it empties the posterior outright.
    m2 = load_model(
        two_world_payload(
            probes=[
                {
                    "id": "probe-z",
                    "cost": 1,
                    "outcome_space": ["outcome-a", "outcome-never"],
                    "outcomes": {"world-a": "outcome-a", "world-b": "outcome-a"},
                }
            ],
            decisions=[
                {"id": "decide-a", "losses": {"world-a": 0, "world-b": 5}, "required_evidence": []},
                {
                    "id": "decide-abstain",
                    "losses": {"world-a": 2, "world-b": 2},
                    "required_evidence": [],
                },
            ],
        )
    )
    state0 = initial_state(m2, state_id="episode-1", binding=binding())
    with pytest.raises(LabImpossibleObservationError):
        apply_observation(
            m2,
            state0,
            observation_id="obs-1",
            probe_id="probe-z",
            outcome_id="outcome-never",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=binding(),
        )
    # The prior is provably untouched: it still reproduces the same seal.
    rebuilt = initial_state(m2, state_id="episode-1", binding=binding())
    assert state0.state_seal() == rebuilt.state_seal()


def test_an_unknown_probe_is_refused() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    with pytest.raises(LabUnknownReferenceError):
        apply_observation(
            m,
            state0,
            observation_id="obs-1",
            probe_id="probe-nonexistent",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=binding(),
        )


def test_an_unknown_outcome_is_refused() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    with pytest.raises(LabUnknownReferenceError):
        apply_observation(
            m,
            state0,
            observation_id="obs-1",
            probe_id="probe-x",
            outcome_id="outcome-undeclared",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=binding(),
        )


def test_a_repeated_probe_is_refused() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    with pytest.raises(LabRepeatedProbeError):
        apply_observation(
            m,
            state1,
            observation_id="obs-2",
            probe_id="probe-x",
            outcome_id="outcome-hi",
            observed_at="2026-09-18T00:01:00Z",
            expected_binding=binding(),
            history=[state0, state1],
        )


def test_a_different_probe_after_the_first_is_granted() -> None:
    """Proves the other half of the repeated-probe refusal: a new probe is fine."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    state2 = apply_observation(
        m,
        state1,
        observation_id="obs-2",
        probe_id="probe-y",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:01:00Z",
        expected_binding=binding(),
        history=[state0, state1],
    )
    assert state2.acquired_probe_ids() == frozenset({"probe-x", "probe-y"})


def test_a_source_scope_drift_is_refused() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    drifted = LabBinding(
        host_id="host-alpha",
        agent_family=AgentFamily.CLAUDE,
        source_scope_digest=OTHER_SOURCE_SCOPE,
    )
    with pytest.raises(LabSourceScopeMismatchError):
        apply_observation(
            m,
            state0,
            observation_id="obs-1",
            probe_id="probe-x",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=drifted,
        )


def test_a_host_or_family_mismatch_is_refused() -> None:
    """The consumer boundary checks host/family independently of source scope."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    wrong_host = LabBinding(
        host_id="host-beta", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )
    with pytest.raises(LabBindingMismatchError):
        apply_observation(
            m,
            state0,
            observation_id="obs-1",
            probe_id="probe-x",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=wrong_host,
        )


def test_a_cross_model_state_is_refused() -> None:
    m = model()
    other_model = load_model(two_world_payload(model_id="a-different-model"))
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    with pytest.raises(LabCrossModelStateError):
        apply_observation(
            other_model,
            state0,
            observation_id="obs-1",
            probe_id="probe-x",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=binding(),
        )


def test_a_two_step_history_replays_cleanly() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    verify_state_history(m, binding(), [state0, state1])


def test_a_tampered_posterior_fails_replay() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    tampered = state1.model_copy(update={"posterior_weights": {"world-a": 1, "world-b": 3}})
    with pytest.raises(LabReplayViolationError):
        verify_state_history(m, binding(), [state0, tampered])


def test_a_tampered_prior_seal_fails_replay() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    tampered = state1.model_copy(update={"prior_state_seal": "sha256:" + "0" * 64})
    with pytest.raises(LabReplayViolationError):
        verify_state_history(m, binding(), [state0, tampered])


def test_a_non_sequential_history_fails_replay() -> None:
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    bumped = state1.model_copy(update={"revision": 2})
    with pytest.raises(LabReplayViolationError):
        verify_state_history(m, binding(), [state0, bumped])


def test_an_empty_history_fails_replay() -> None:
    with pytest.raises(LabReplayViolationError):
        verify_state_history(model(), binding(), [])


def test_a_state_above_revision_zero_without_history_is_refused() -> None:
    """The security defect this test closes: a caller-declared posterior above
    revision 0 must never be trusted on model-seal match alone."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            state1,
            observation_id="obs-2",
            probe_id="probe-y",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:01:00Z",
            expected_binding=binding(),
        )


def test_a_forged_posterior_at_revision_one_without_replay_is_refused() -> None:
    """A tampered ``posterior_weights`` that still matches the model seal must
    still be refused: matching the seal is not evidence of a real replay."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    forged = state1.model_copy(update={"posterior_weights": {"world-a": 1, "world-b": 3}})
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            forged,
            observation_id="obs-2",
            probe_id="probe-y",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:01:00Z",
            expected_binding=binding(),
            history=[state0, forged],
        )


def test_a_truncated_history_is_refused() -> None:
    """``history`` must end at the presented ``prior``, not merely be non-empty."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    state1 = apply_observation(
        m,
        state0,
        observation_id="obs-1",
        probe_id="probe-x",
        outcome_id="outcome-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=binding(),
    )
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            state1,
            observation_id="obs-2",
            probe_id="probe-y",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:01:00Z",
            expected_binding=binding(),
            history=[state0],
        )


def test_a_tampered_revision_zero_state_is_refused() -> None:
    """Revision 0's cheap recompute path still closes a directly forged prior."""
    m = model()
    state0 = initial_state(m, state_id="episode-1", binding=binding())
    tampered = state0.model_copy(update={"posterior_weights": {"world-a": 1, "world-b": 1}})
    with pytest.raises(LabReplayViolationError):
        apply_observation(
            m,
            tampered,
            observation_id="obs-1",
            probe_id="probe-x",
            outcome_id="outcome-lo",
            observed_at="2026-09-18T00:00:00Z",
            expected_binding=binding(),
        )


def test_a_mutated_nested_loss_is_refused_at_the_next_public_boundary() -> None:
    """``frozen=True`` blocks reassigning a field, never mutating the dict it
    points at. A caller-mutated ``Decision.losses`` value out of bounds must
    still be refused the next time it crosses a public entry point."""
    m = model()
    m.decisions[0].losses["world-a"] = -100
    with pytest.raises(LabContractViolationError):
        initial_state(m, state_id="episode-1", binding=binding())

"""Hostile and independent-oracle coverage for latent_compass.lab.planner.

The complementary-probes example is hand-verified by exhaustive enumeration in
this file's module docstring-adjacent comments; the assertions below check the
planner's output against those independently computed exact values, not
against whatever the planner itself would compute for a different input.

Hand-verified values for ``examples/lab-model.json`` (four equally-weighted
worlds, ``decide-abstain`` loss 3 flat, ``decide-equal``/``decide-different``
loss 0 on a match and 10 on a mismatch, two cost-1 probes each revealing one
independent bit):

* ``R(∅) = 3`` (abstain dominates both informed guesses at 5 each).
* A single probe alone leaves the equal/different posterior unchanged (each
  outcome is 50/50 over the two worlds sharing it), so buying either probe
  alone costs 1 for zero information: ``V(∅, budget=2, horizon=1) = 3``
  (STOP).
* Buying both probes pins the exact world, driving expected loss to 0:
  ``V(∅, budget=2, horizon=2) = 1 + 1 = 2`` (PROBE ``check-a``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.contracts import MAX_BUDGET, MAX_HORIZON
from latent_compass.lab.errors import LabContractViolationError, LabCrossModelStateError
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.planner import propose
from latent_compass.lab.state import LabBinding, apply_observation, initial_state

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

SOURCE_SCOPE = "sha256:" + "3" * 64


def _binding() -> LabBinding:
    return LabBinding(
        host_id="host-alpha", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )


def complementary_model() -> DiagnosisModel:
    return load_model(json.loads((EXAMPLES / "lab-model.json").read_text(encoding="utf-8")))


def mandatory_evidence_model() -> DiagnosisModel:
    payload = json.loads((EXAMPLES / "lab-mandatory-evidence.json").read_text(encoding="utf-8"))
    return load_model(payload)


def test_root_stopping_value_is_abstain_at_three() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=2, horizon=1)
    assert report.stopping_decision_id == "decide-abstain"
    assert report.stopping_value == "3/1"


def test_one_step_lookahead_recommends_stopping() -> None:
    """A one-step-lookahead planner is fooled: neither probe alone helps."""
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=2, horizon=1)
    assert report.recommended_action == "STOP"
    assert report.plan_value == "3/1"
    values = {probe.probe_id: probe.value for probe in report.probes}
    assert values == {"check-a": "4/1", "check-b": "4/1"}


def test_two_step_lookahead_finds_the_complementary_pair() -> None:
    """The acceptance case: two-step planning beats the one-step-greedy stop."""
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=2, horizon=2)
    assert report.recommended_action == "PROBE"
    assert report.recommended_probe_id == "check-a"
    assert report.plan_value == "2/1"


def test_two_step_plan_value_is_strictly_lower_than_one_step_stop() -> None:
    from fractions import Fraction

    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    two_step = propose(model, state, expected_binding=_binding(), budget=2, horizon=2)
    one_step = propose(model, state, expected_binding=_binding(), budget=2, horizon=1)

    def as_fraction(text: str) -> Fraction:
        numerator, denominator = text.split("/")
        return Fraction(int(numerator), int(denominator))

    assert as_fraction(two_step.plan_value) < as_fraction(one_step.plan_value)


def test_tie_breaking_is_deterministic_by_lexicographically_smallest_id() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=2, horizon=2)
    # check-a and check-b both value at exactly 2/1; the deterministic
    # tie-break must always pick "check-a", never depend on model.probes order.
    assert report.recommended_probe_id == "check-a"


def test_an_already_acquired_probe_is_never_recommended_again() -> None:
    model = complementary_model()
    state0 = initial_state(model, state_id="ep-1", binding=_binding())
    state1 = apply_observation(
        model,
        state0,
        observation_id="obs-1",
        probe_id="check-a",
        outcome_id="a-lo",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=_binding(),
    )
    report = propose(
        model, state1, expected_binding=_binding(), history=[state0, state1], budget=1, horizon=1
    )
    assert report.recommended_action == "PROBE"
    assert report.recommended_probe_id == "check-b"
    assert report.plan_value == "1/1"
    probe_values = {probe.probe_id: (probe.affordable, probe.value) for probe in report.probes}
    assert probe_values["check-a"] == (False, None)
    assert probe_values["check-b"] == (True, "1/1")


def test_an_unaffordable_probe_is_never_recommended() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=0, horizon=1)
    assert report.recommended_action == "STOP"
    for probe in report.probes:
        assert probe.affordable is False
        assert probe.value is None


def test_horizon_zero_reports_no_probe_values_even_if_affordable() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    report = propose(model, state, expected_binding=_binding(), budget=2, horizon=0)
    assert report.recommended_action == "STOP"
    for probe in report.probes:
        assert probe.affordable is True
        assert probe.value is None


def test_an_expansion_ceiling_truncates_deterministically_and_says_so() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    truncated = propose(
        model, state, expected_binding=_binding(), budget=2, horizon=2, max_expansions=1
    )
    assert truncated.search_exhausted is True
    assert truncated.exact_for_declared_bounds is False
    assert truncated.expansions_used == 1
    # Never claims the true optimum once truncated.
    assert truncated.plan_value == "3/1"

    full = propose(model, state, expected_binding=_binding(), budget=2, horizon=2)
    assert full.search_exhausted is False
    assert full.exact_for_declared_bounds is True
    assert full.plan_value == "2/1"


def test_mandatory_evidence_gates_a_decision_even_at_zero_loss() -> None:
    model = mandatory_evidence_model()
    state0 = initial_state(model, state_id="ep-1", binding=_binding())
    state1 = apply_observation(
        model,
        state0,
        observation_id="obs-1",
        probe_id="probe-approval",
        outcome_id="outcome-fail",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=_binding(),
    )
    report = propose(
        model, state1, expected_binding=_binding(), history=[state0, state1], budget=0, horizon=0
    )
    release = next(item for item in report.decisions if item.decision_id == "decide-release")
    assert release.expected_loss == "0/1"
    assert release.admissible is False
    assert report.stopping_decision_id == "decide-hold"


def test_mandatory_evidence_admits_the_decision_once_the_exact_outcome_is_seen() -> None:
    """Proves the other half: the same decision is admissible once PASS is observed."""
    model = mandatory_evidence_model()
    state0 = initial_state(model, state_id="ep-1", binding=_binding())
    state1 = apply_observation(
        model,
        state0,
        observation_id="obs-1",
        probe_id="probe-approval",
        outcome_id="outcome-pass",
        observed_at="2026-09-18T00:00:00Z",
        expected_binding=_binding(),
    )
    report = propose(
        model, state1, expected_binding=_binding(), history=[state0, state1], budget=0, horizon=0
    )
    release = next(item for item in report.decisions if item.decision_id == "decide-release")
    assert release.admissible is True
    assert report.stopping_decision_id == "decide-release"
    assert report.stopping_value == "0/1"


def test_a_cross_model_plan_request_is_refused() -> None:
    model = complementary_model()
    other = mandatory_evidence_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    with pytest.raises(LabCrossModelStateError):
        propose(other, state, expected_binding=_binding(), budget=1, horizon=1)


def test_budget_beyond_the_declared_bound_is_refused() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    with pytest.raises(LabContractViolationError):
        propose(model, state, expected_binding=_binding(), budget=MAX_BUDGET + 1, horizon=1)


def test_horizon_beyond_the_declared_bound_is_refused() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    with pytest.raises(LabContractViolationError):
        propose(model, state, expected_binding=_binding(), budget=1, horizon=MAX_HORIZON + 1)


def test_a_negative_budget_is_refused() -> None:
    model = complementary_model()
    state = initial_state(model, state_id="ep-1", binding=_binding())
    with pytest.raises(LabContractViolationError):
        propose(model, state, expected_binding=_binding(), budget=-1, horizon=1)

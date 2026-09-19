"""HOK-799 — the six acceptance scenarios, checked against hand-derived exact values.

Each expectation below is an exact value that can be recomputed by hand from
the scenario model's own weights, losses and costs (see
``docs/active-diagnosis-scope.md``). The scenario numbers are illustrative model
hypotheses; these tests prove the decision contract can express each
scenario, not that any observation is useful in a real repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.errors import LabImpossibleObservationError
from latent_compass.lab.host_observations import ObservationKind
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.planner import PlanReport, propose
from latent_compass.lab.state import (
    DiagnosisStateRevision,
    LabBinding,
    apply_observation,
    initial_state,
)

SCENARIOS = Path(__file__).resolve().parent.parent / "examples" / "lab-scenarios"
OBSERVED_AT = "2026-09-19T00:00:00Z"
BINDING = LabBinding(
    host_id="scenario-host",
    agent_family=AgentFamily.CLAUDE,
    source_scope_digest="sha256:" + "5" * 64,
)

REQUIRED_KINDS = {
    "KNOWN_IDENTIFIER",
    "CONCEPT_WITHOUT_SHARED_TERMS",
    "INDIRECT_DEPENDENCY",
    "REQUIREMENT_ABSENT_FROM_CODE",
    "CONTRADICTION",
    "BUDGET_EXHAUSTED",
}
#: A requirement written nowhere in source has no observation kind, by nature.
EXTERNAL_CAPABILITY = "AUTHORED_REQUIREMENT"
CAPABILITIES = {kind.value for kind in ObservationKind} | {EXTERNAL_CAPABILITY}
#: Who runs the observation: the lab's confined reader, the host executor, or nobody in source.
EXECUTION = {
    "LAB_EXECUTED": {ObservationKind.LITERAL_SEARCH.value},
    "HOST_EXECUTED": {
        ObservationKind.FILE_READ.value,
        ObservationKind.GIT_DIFF.value,
        ObservationKind.SYMBOL_NAVIGATION.value,
        ObservationKind.TARGETED_CHECK.value,
    },
    "EXTERNAL_TO_SOURCE": {EXTERNAL_CAPABILITY},
}


def manifest() -> dict[str, object]:
    loaded = json.loads((SCENARIOS / "scenarios.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def scenario_model(name: str) -> DiagnosisModel:
    return load_model(json.loads((SCENARIOS / name).read_text(encoding="utf-8")))


def start(model: DiagnosisModel) -> DiagnosisStateRevision:
    return initial_state(model, state_id="scenario-episode", binding=BINDING)


def plan(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    *,
    budget: int,
    horizon: int,
    history: list[DiagnosisStateRevision] | None = None,
    max_expansions: int | None = None,
) -> PlanReport:
    if max_expansions is None:
        return propose(
            model, state, budget=budget, horizon=horizon, expected_binding=BINDING, history=history
        )
    return propose(
        model,
        state,
        budget=budget,
        horizon=horizon,
        expected_binding=BINDING,
        history=history,
        max_expansions=max_expansions,
    )


def observe(
    model: DiagnosisModel,
    history: list[DiagnosisStateRevision],
    probe_id: str,
    outcome_id: str,
) -> DiagnosisStateRevision:
    prior = history[-1]
    return apply_observation(
        model,
        prior,
        observation_id=f"observation-{prior.revision + 1}",
        probe_id=probe_id,
        outcome_id=outcome_id,
        observed_at=OBSERVED_AT,
        expected_binding=BINDING,
        history=history if prior.revision > 0 else None,
    )


def probe_values(report: PlanReport) -> dict[str, tuple[bool, str | None]]:
    return {item.probe_id: (item.affordable, item.value) for item in report.probes}


def test_the_manifest_covers_exactly_the_six_required_scenario_kinds() -> None:
    document = manifest()
    # The manifest's own honesty fields: deleting one must not go unnoticed.
    assert document["manifest_version"] == "1.0.0"
    assert document["policy_unit"] == "cost-point"
    assert isinstance(document["non_claim"], str)
    assert "illustrative model hypothesis" in document["non_claim"]
    assert "None is a measurement" in document["non_claim"]

    scenarios = document["scenarios"]
    assert isinstance(scenarios, list)
    kinds = [item["kind"] for item in scenarios]
    assert len(kinds) == len(set(kinds)), "a scenario kind is declared twice"
    assert set(kinds) == REQUIRED_KINDS
    for item in scenarios:
        assert (SCENARIOS / item["model"]).is_file(), item["model"]
        assert item["question"].strip(), item["kind"]
        assert item["expected_behaviour"].strip(), item["kind"]


def test_every_model_probe_is_mapped_to_one_declared_capability_and_nothing_else_is() -> None:
    document = manifest()
    capabilities = document["probe_capabilities"]
    scenarios = document["scenarios"]
    assert isinstance(capabilities, dict)
    assert isinstance(scenarios, list)
    assert set(capabilities) == {item["model"] for item in scenarios}
    for model_name, mapping in capabilities.items():
        model = scenario_model(model_name)
        assert set(mapping) == {probe.id for probe in model.probes}, model_name
        for probe_id, entry in mapping.items():
            assert entry["capability"] in CAPABILITIES, probe_id
            assert entry["adapter"] in EXECUTION, probe_id
            # The lab itself only ever reads; everything else is the host's to run.
            assert entry["capability"] in EXECUTION[entry["adapter"]], probe_id


def test_a_known_identifier_is_searched_exactly_before_any_edit_is_chosen() -> None:
    model = scenario_model("known-identifier.json")
    report = plan(model, start(model), budget=3, horizon=2)

    assert (report.stopping_decision_id, report.stopping_value) == ("abstain", "4/1")
    assert report.recommended_action == "PROBE"
    assert report.recommended_probe_id == "literal-search-identifier"
    assert report.plan_value == "2/1"
    assert probe_values(report) == {
        "literal-search-identifier": (True, "2/1"),
        "symbol-definition-lookup": (True, "3/1"),
    }


def test_an_empty_exact_search_keeps_the_residual_world_and_ends_in_abstention() -> None:
    model = scenario_model("known-identifier.json")
    first = start(model)
    second = observe(model, [first], "literal-search-identifier", "no-hit")

    assert second.posterior_weights == {"none-of-the-above": 1}
    report = plan(model, second, budget=2, horizon=2, history=[first, second])
    assert report.recommended_action == "STOP"
    assert (report.stopping_decision_id, report.plan_value) == ("abstain", "4/1")
    assert probe_values(report)["symbol-definition-lookup"] == (True, "6/1")


def test_a_literal_search_with_no_shared_vocabulary_is_never_proposed() -> None:
    model = scenario_model("concept-without-shared-terms.json")

    afforded = plan(model, start(model), budget=3, horizon=2)
    assert (afforded.stopping_decision_id, afforded.stopping_value) == ("abstain", "5/1")
    assert afforded.recommended_action == "PROBE"
    assert afforded.recommended_probe_id == "symbol-walk-from-entrypoint"
    assert afforded.plan_value == "14/3"
    assert probe_values(afforded)["literal-search-user-terms"] == (True, "6/1")

    # The cheap probe stays affordable and stays worthless: the controller
    # abstains rather than buying an observation that cannot change its choice.
    constrained = plan(model, start(model), budget=2, horizon=2)
    assert constrained.recommended_action == "STOP"
    assert (constrained.stopping_decision_id, constrained.plan_value) == ("abstain", "5/1")
    assert probe_values(constrained) == {
        "literal-search-user-terms": (True, "6/1"),
        "symbol-walk-from-entrypoint": (False, None),
    }


@pytest.mark.parametrize(("horizon", "reference_value"), [(1, "8/1"), (2, "23/4")])
def test_the_probe_that_discriminates_the_critical_omission_is_proposed_first(
    horizon: int, reference_value: str
) -> None:
    model = scenario_model("indirect-dependency.json")
    report = plan(model, start(model), budget=3, horizon=horizon)

    assert (report.stopping_decision_id, report.stopping_value) == ("change-with-adapter", "6/1")
    assert report.recommended_action == "PROBE"
    assert report.recommended_probe_id == "literal-search-registry-key"
    assert report.plan_value == "9/2"
    assert probe_values(report) == {
        "find-direct-references": (True, reference_value),
        "literal-search-registry-key": (True, "9/2"),
    }


def test_an_empty_direct_reference_result_does_not_prove_the_dependency_absent() -> None:
    model = scenario_model("indirect-dependency.json")
    first = start(model)
    second = observe(model, [first], "find-direct-references", "no-reference")

    assert second.posterior_weights == {"no-dependent": 2, "indirect-dependent-via-registry": 1}
    report = plan(model, second, budget=1, horizon=1, history=[first, second])
    assert report.recommended_action == "PROBE"
    assert report.recommended_probe_id == "literal-search-registry-key"
    assert (report.stopping_decision_id, report.stopping_value) == ("change-with-adapter", "6/1")
    assert report.plan_value == "3/1"


def test_a_low_expected_loss_never_buys_a_change_whose_requirement_is_unobserved() -> None:
    model = scenario_model("requirement-absent-from-code.json")

    unaffordable = plan(model, start(model), budget=1, horizon=1)
    valuations = {item.decision_id: item for item in unaffordable.decisions}
    # Cheaper in expectation than abstaining, and still illegal.
    assert (valuations["apply-change"].admissible, valuations["apply-change"].expected_loss) == (
        False,
        "1/1",
    )
    assert unaffordable.recommended_action == "STOP"
    assert (unaffordable.stopping_decision_id, unaffordable.plan_value) == (
        "abstain-and-request-ruling",
        "3/1",
    )
    assert probe_values(unaffordable) == {
        "authored-requirement-lookup": (False, None),
        "read-implementation": (True, "4/1"),
    }

    afforded = plan(model, start(model), budget=2, horizon=1)
    assert afforded.recommended_action == "PROBE"
    assert afforded.recommended_probe_id == "authored-requirement-lookup"
    assert afforded.plan_value == "23/10"


@pytest.mark.parametrize(
    ("outcome_id", "decision_id", "value", "admissible"),
    [
        ("rule-allows", "apply-change", "0/1", True),
        ("rule-forbids", "abstain-and-request-ruling", "3/1", False),
    ],
)
def test_only_the_observed_authored_requirement_makes_the_change_admissible(
    outcome_id: str, decision_id: str, value: str, admissible: bool
) -> None:
    model = scenario_model("requirement-absent-from-code.json")
    first = start(model)
    second = observe(model, [first], "authored-requirement-lookup", outcome_id)

    report = plan(model, second, budget=0, horizon=0, history=[first, second])
    assert report.recommended_action == "STOP"
    assert (report.stopping_decision_id, report.plan_value) == (decision_id, value)
    valuations = {item.decision_id: item.admissible for item in report.decisions}
    assert valuations["apply-change"] is admissible


def test_a_contradiction_is_refused_outside_the_model_and_changes_nothing() -> None:
    model = scenario_model("known-identifier.json")
    first = start(model)
    second = observe(model, [first], "literal-search-identifier", "hit-module-a")
    assert second.posterior_weights == {"defined-in-module-a": 2}
    before = second.canonical_payload()

    # Live trap: the consistent outcome is accepted, so the refusal below is
    # about the contradiction and not about the probe or the history.
    consistent = observe(model, [first, second], "symbol-definition-lookup", "defined-in-a")
    assert consistent.posterior_weights == {"defined-in-module-a": 2}

    with pytest.raises(LabImpossibleObservationError) as refusal:
        observe(model, [first, second], "symbol-definition-lookup", "defined-in-b")
    assert refusal.value.code == "lab_impossible_observation"
    assert second.canonical_payload() == before
    # The untouched prior state still plans: nothing was renormalised.
    report = plan(model, second, budget=0, horizon=0, history=[first, second])
    assert (report.stopping_decision_id, report.plan_value) == ("edit-module-a", "0/1")


def test_an_exhausted_observation_budget_stops_without_inventing_probe_values() -> None:
    model = scenario_model("indirect-dependency.json")
    report = plan(model, start(model), budget=0, horizon=2)

    assert report.recommended_action == "STOP"
    assert (report.stopping_decision_id, report.plan_value) == ("change-with-adapter", "6/1")
    assert probe_values(report) == {
        "find-direct-references": (False, None),
        "literal-search-registry-key": (False, None),
    }
    assert (report.search_exhausted, report.exact_for_declared_bounds) == (False, True)


def test_an_exhausted_search_budget_is_labelled_inexact_never_optimal() -> None:
    model = scenario_model("indirect-dependency.json")
    complete = plan(model, start(model), budget=3, horizon=2)
    truncated = plan(model, start(model), budget=3, horizon=2, max_expansions=1)

    assert (complete.search_exhausted, complete.exact_for_declared_bounds) == (False, True)
    assert (truncated.search_exhausted, truncated.exact_for_declared_bounds) == (True, False)
    # One expansion sees only one step ahead: the reference probe loses the
    # second-step value the complete search finds for it.
    assert probe_values(complete)["find-direct-references"] == (True, "23/4")
    assert probe_values(truncated)["find-direct-references"] == (True, "8/1")
    assert truncated.recommended_probe_id == "literal-search-registry-key"

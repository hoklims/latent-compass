"""Lab contract ``1.1.0`` — planning around probes the host declared unobtainable.

Two halves throughout: what the restricted plan must do, and that the ``1.0.0``
plan beside it did not move. The exact values are confronted with an
independent tree enumeration in ``test_lab_enumeration.py``; this module pins
the contract itself — the versions, the refusals that come before any search,
what the record says, and the two documents never being taken for one another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation, UnsupportedContractVersion
from latent_compass.lab import planner
from latent_compass.lab.errors import LabContractViolationError, LabUnknownReferenceError
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.planner import (
    ConstrainedPlanReport,
    PlanReport,
    load_constrained_plan_report,
    load_plan_report,
    propose,
    propose_excluding,
)
from latent_compass.lab.state import LabBinding, apply_observation, initial_state

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE_SCOPE = "sha256:" + "4" * 64

#: What a ``1.0.0`` plan report is made of. Growing this set is a contract change.
PLAN_REPORT_1_0_0_KEYS = {
    "contract_version",
    "model_seal",
    "state_seal",
    "budget",
    "horizon",
    "max_expansions",
    "expansions_used",
    "search_exhausted",
    "exact_for_declared_bounds",
    "stopping_decision_id",
    "stopping_value",
    "recommended_action",
    "recommended_probe_id",
    "plan_value",
    "decisions",
    "probes",
    "non_authority_notice",
}
EXCLUSION_RECORD_KEYS = {"unobtainable_probe_ids", "exclusion_basis"}


def _binding() -> LabBinding:
    return LabBinding(
        host_id="host-alpha", agent_family=AgentFamily.CLAUDE, source_scope_digest=SOURCE_SCOPE
    )


def _model(filename: str) -> DiagnosisModel:
    return load_model(json.loads((EXAMPLES / filename).read_text(encoding="utf-8")))


def _constrained(model: DiagnosisModel, excluded: Any, **options: Any) -> ConstrainedPlanReport:
    state = initial_state(model, state_id="ep-1", binding=_binding())
    return propose_excluding(
        model,
        state,
        unobtainable_probe_ids=excluded,
        expected_binding=_binding(),
        **{"budget": 2, "horizon": 2, **options},
    )


# --- the 1.0.0 document did not move ------------------------------------------------------------


def test_the_unrestricted_report_keeps_its_exact_1_0_0_shape() -> None:
    model = _model("lab-model.json")
    state = initial_state(model, state_id="ep-1", binding=_binding())
    payload = propose(
        model, state, expected_binding=_binding(), budget=2, horizon=2
    ).canonical_payload()

    assert set(payload) == PLAN_REPORT_1_0_0_KEYS
    assert payload["contract_version"] == "1.0.0"
    assert (payload["recommended_probe_id"], payload["plan_value"]) == ("check-a", "2/1")


@pytest.mark.parametrize("filename", ["lab-model.json", "lab-mandatory-evidence.json"])
@pytest.mark.parametrize(("budget", "horizon"), [(0, 0), (2, 1), (2, 2), (3, 2)])
def test_no_exclusion_plans_exactly_as_propose_and_still_says_1_1_0(
    filename: str, budget: int, horizon: int
) -> None:
    model = _model(filename)
    state = initial_state(model, state_id="ep-1", binding=_binding())
    plain = propose(
        model, state, expected_binding=_binding(), budget=budget, horizon=horizon
    ).canonical_payload()
    constrained = _constrained(model, (), budget=budget, horizon=horizon).canonical_payload()

    assert set(constrained) == PLAN_REPORT_1_0_0_KEYS | EXCLUSION_RECORD_KEYS
    assert (constrained["contract_version"], plain["contract_version"]) == ("1.1.0", "1.0.0")
    assert (constrained["unobtainable_probe_ids"], constrained["exclusion_basis"]) == (
        [],
        "HOST_DECLARED",
    )
    shared = PLAN_REPORT_1_0_0_KEYS - {"contract_version"}
    assert {key: constrained[key] for key in shared} == {key: plain[key] for key in shared}


# --- what the restricted plan does --------------------------------------------------------------


def test_an_unobtainable_probe_is_excluded_below_the_root_too() -> None:
    model = _model("lab-model.json")
    # The other half first: unrestricted, the two complementary probes are worth buying.
    free = _constrained(model, ())
    assert (free.recommended_action, free.recommended_probe_id, free.plan_value) == (
        "PROBE",
        "check-a",
        "2/1",
    )

    # Without check-a, check-b alone separates nothing worth its cost. A restriction applied
    # at the first node only would still find "check-b, then check-a" and price it at 2.
    report = _constrained(model, ["check-a"])
    assert (report.recommended_action, report.recommended_probe_id) == ("STOP", None)
    assert (report.plan_value, report.stopping_value) == ("3/1", "3/1")
    assert report.exact_for_declared_bounds


def test_the_record_names_what_it_was_told_and_values_nothing_it_excluded() -> None:
    report = _constrained(_model("lab-model.json"), {"check-b", "check-a"}, budget=2, horizon=1)
    assert report.unobtainable_probe_ids == ("check-a", "check-b")
    assert report.exclusion_basis == "HOST_DECLARED"
    # Unobtainable is not unaffordable: the two are separate statements on the record.
    assert {(probe.probe_id, probe.affordable, probe.value) for probe in report.probes} == {
        ("check-a", True, None),
        ("check-b", True, None),
    }

    # The other half: a probe left obtainable is still valued.
    partial = _constrained(_model("lab-model.json"), ["check-a"], budget=2, horizon=1)
    assert {probe.probe_id: probe.value for probe in partial.probes} == {
        "check-a": None,
        "check-b": "4/1",
    }


def test_mandatory_evidence_stays_out_of_reach_when_its_only_probe_is_unobtainable() -> None:
    model = _model("lab-mandatory-evidence.json")
    free = _constrained(model, ())
    assert (free.recommended_probe_id, free.plan_value) == ("probe-approval", "1/2")

    report = _constrained(model, ["probe-approval"])
    assert (report.recommended_action, report.plan_value) == ("STOP", "1/1")
    release = next(item for item in report.decisions if item.decision_id == "decide-release")
    # Its expected loss is still the lowest on the table, and it is still not admissible.
    assert release.admissible is False
    assert report.stopping_decision_id != "decide-release"


def test_an_already_acquired_probe_may_be_named_and_changes_nothing() -> None:
    model = _model("lab-model.json")
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
    options: dict[str, Any] = {
        "expected_binding": _binding(),
        "history": [state0, state1],
        "budget": 1,
        "horizon": 1,
    }
    named = propose_excluding(model, state1, unobtainable_probe_ids=["check-a"], **options)
    unnamed = propose_excluding(model, state1, unobtainable_probe_ids=[], **options)

    assert named.recommended_probe_id == unnamed.recommended_probe_id == "check-b"
    assert named.plan_value == unnamed.plan_value == "1/1"
    assert (named.unobtainable_probe_ids, unnamed.unobtainable_probe_ids) == (("check-a",), ())


# --- refused before any search ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("excluded", "error", "message"),
    [
        (["check-z"], LabUnknownReferenceError, "names no probe of this model"),
        ("check-a", LabContractViolationError, "must be a collection of probe ids"),
        (7, LabContractViolationError, "must be a collection of probe ids"),
        (["check-a", "check-a"], LabContractViolationError, "named at most once"),
        (["check-a", 3], LabContractViolationError, "must be a string"),
    ],
)
def test_a_malformed_exclusion_is_refused_before_the_search_starts(
    excluded: Any, error: type[Exception], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_search(*args: object, **kwargs: object) -> Any:
        pytest.fail("a malformed exclusion reached the search")

    monkeypatch.setattr(planner, "_search", unexpected_search)
    with pytest.raises(error, match=message):
        _constrained(_model("lab-model.json"), excluded)


def test_an_unknown_exclusion_is_reported_by_name_not_ignored() -> None:
    # The record would refuse an id it does not list anyway, but later and without naming it.
    # Catch the whole family, then assert the stable code a caller matches on.
    with pytest.raises(LabContractViolationError) as refusal:
        _constrained(_model("lab-model.json"), ["check-a", "check-z", "check-y"])
    assert refusal.value.code == "lab_unknown_reference"
    assert refusal.value.detail == {"unknown_probe_ids": ["check-y", "check-z"]}


# --- two documents, never taken for one another -------------------------------------------------


def test_each_loader_accepts_only_its_own_version() -> None:
    model = _model("lab-model.json")
    state = initial_state(model, state_id="ep-1", binding=_binding())
    plain = propose(
        model, state, expected_binding=_binding(), budget=2, horizon=2
    ).canonical_payload()
    constrained = _constrained(model, ["check-a"]).canonical_payload()

    assert load_plan_report(plain).contract_version == "1.0.0"
    assert load_constrained_plan_report(constrained).contract_version == "1.1.0"

    # Strict models would refuse the other document's fields anyway. The refusal asserted
    # here is the earlier one, on the version: it must not depend on the shapes differing.
    with pytest.raises(ContractViolation) as newer:
        load_plan_report(constrained)
    assert isinstance(newer.value, UnsupportedContractVersion)
    assert isinstance(newer.value.detail, dict)
    assert (newer.value.detail["reason"], newer.value.detail["supported"]) == ("future", ["1.0.0"])

    with pytest.raises(ContractViolation) as older:
        load_constrained_plan_report(plain)
    assert isinstance(older.value, UnsupportedContractVersion)
    assert isinstance(older.value.detail, dict)
    assert (older.value.detail["reason"], older.value.detail["supported"]) == ("unknown", ["1.1.0"])


def test_the_two_reports_share_every_field_but_the_exclusion_record() -> None:
    plain, constrained = set(PlanReport.model_fields), set(ConstrainedPlanReport.model_fields)
    assert plain == PLAN_REPORT_1_0_0_KEYS
    assert constrained - plain == EXCLUSION_RECORD_KEYS
    assert plain - constrained == set()
    # Siblings on purpose: a consumer typed for the unrestricted report cannot be handed this one.
    assert not issubclass(ConstrainedPlanReport, PlanReport)


def _forge(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**payload, **changes}


def test_a_forged_constrained_report_is_refused() -> None:
    honest = _constrained(_model("lab-model.json"), ["check-a"], budget=2, horizon=1)
    payload = honest.canonical_payload()
    assert load_constrained_plan_report(payload) == honest, "the honest report must load"

    listed = payload["probes"]
    assert isinstance(listed, list)
    valued = [
        {**probe, "value": "1/1"} if probe["probe_id"] == "check-a" else probe for probe in listed
    ]
    forgeries = {
        "it recommends what it excluded": _forge(
            payload, recommended_action="PROBE", recommended_probe_id="check-a"
        ),
        "it values what it excluded": _forge(payload, probes=valued),
        "it excludes a probe it does not list": _forge(
            payload, unobtainable_probe_ids=["check-a", "check-q"]
        ),
        "its exclusions are unsorted": _forge(
            payload, unobtainable_probe_ids=["check-b", "check-a"]
        ),
        "its exclusions repeat": _forge(payload, unobtainable_probe_ids=["check-a", "check-a"]),
        "it claims another basis": _forge(payload, exclusion_basis="LAB_VERIFIED"),
        "it drops its exclusion record": {
            key: value for key, value in payload.items() if key not in EXCLUSION_RECORD_KEYS
        },
    }
    for name, forged in forgeries.items():
        with pytest.raises(LabContractViolationError):
            load_constrained_plan_report(forged)
        assert forged != payload, name

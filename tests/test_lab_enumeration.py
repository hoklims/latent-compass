"""Enumerate complete tiny policies, then price each world independently.

This oracle does not call the planner's Bellman recurrence or memoization.
It is intentionally restricted to two probes so complete tree enumeration fits.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.planner import propose, propose_excluding
from latent_compass.lab.state import LabBinding, initial_state

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Tree:
    decision: str | None = None
    probe: str | None = None
    branches: dict[str, Tree] = field(default_factory=dict)


def trees(
    model: DiagnosisModel,
    budget: int,
    depth: int,
    history: tuple[tuple[str, str], ...] = (),
    excluded: frozenset[str] = frozenset(),
) -> list[Tree]:
    """Every complete policy. An ``excluded`` probe is never placed anywhere in a tree."""
    probes = {p.id: p for p in model.probes}
    known = dict(history)
    worlds = [w.id for w in model.worlds if all(probes[p].outcomes[w.id] == y for p, y in history)]
    choices = [
        Tree(decision=d.id)
        for d in model.decisions
        if all(known.get(r.probe_id) == r.outcome_id for r in d.required_evidence)
    ]
    if depth == 0:
        return choices
    for probe in model.probes:
        if probe.id in known or probe.id in excluded or probe.cost > budget:
            continue
        labels = sorted({probe.outcomes[w] for w in worlds})
        continuations = [
            trees(model, budget - probe.cost, depth - 1, (*history, (probe.id, label)), excluded)
            for label in labels
        ]
        choices.extend(
            Tree(probe=probe.id, branches=dict(zip(labels, branches, strict=True)))
            for branches in product(*continuations)
        )
    return choices


def price(tree: Tree, world: str, model: DiagnosisModel) -> int:
    if tree.decision is not None:
        decision = next(d for d in model.decisions if d.id == tree.decision)
        return decision.losses[world]
    probe = next(p for p in model.probes if p.id == tree.probe)
    return probe.cost + price(tree.branches[probe.outcomes[world]], world, model)


@pytest.mark.parametrize("filename", ["lab-model.json", "lab-mandatory-evidence.json"])
@pytest.mark.parametrize(("budget", "depth"), [(2, 2), (1, 2), (2, 1)])
@pytest.mark.parametrize("first_weight", [1, 3])
def test_planner_matches_complete_tree_enumeration(
    filename: str,
    budget: int,
    depth: int,
    first_weight: int,
) -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["worlds"][0]["weight"] = first_weight
    model = load_model(payload)
    policies = trees(model, budget, depth)
    assert len(policies) >= 3
    weight = sum(w.weight for w in model.worlds)
    optimum = min(
        sum(Fraction(price(t, w.id, model) * w.weight, weight) for w in model.worlds)
        for t in policies
    )
    binding = LabBinding(
        host_id="oracle", agent_family=AgentFamily.CODEX, source_scope_digest="sha256:" + "0" * 64
    )
    state = initial_state(model, state_id="oracle", binding=binding)
    report = propose(model, state, expected_binding=binding, budget=budget, horizon=depth)
    assert report.exact_for_declared_bounds
    assert Fraction(report.plan_value) == optimum


def _model_payload(name: str) -> dict[str, Any]:
    if name != "host-bench":
        loaded = json.loads((REPO / "examples" / name).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded
    # The bench's three-probe model lives in its example module, not in a JSON file.
    spec = importlib.util.spec_from_file_location(
        "latent_compass_lab_host_bench_model", REPO / "examples" / "lab_host_bench.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module.MODEL_PAYLOAD
    assert isinstance(payload, dict)
    return payload


#: (model, budget, depth, unobtainable probes, optimum, recommended probe). The optimum is a
#: hand-checkable literal; the enumeration has to reach it without the planner's help.
CONSTRAINED_CASES = [
    ("lab-model.json", 2, 2, (), "2/1", "check-a"),
    # "check-b, then check-a" is worth 2: only a restriction that holds below the root says 3.
    ("lab-model.json", 2, 2, ("check-a",), "3/1", None),
    # The mirror case. A search that ignored an exclusion altogether never gets this far: it
    # values the excluded probe at the root, and the record refuses that. Only what happens
    # below the root is invisible to the record — which is what this enumeration is for.
    ("lab-model.json", 2, 2, ("check-b",), "3/1", None),
    ("lab-model.json", 2, 2, ("check-a", "check-b"), "3/1", None),
    ("lab-mandatory-evidence.json", 2, 2, (), "1/2", "probe-approval"),
    ("lab-mandatory-evidence.json", 2, 2, ("probe-approval",), "1/1", None),
    ("host-bench", 4, 2, (), "5/2", "define-legacy-consumer"),
    # The numbers docs/host-observations.md quotes: around the symbol probe, the check is worth it.
    ("host-bench", 4, 2, ("define-legacy-consumer",), "7/2", "run-invoice-check"),
    ("host-bench", 4, 2, ("define-legacy-consumer", "run-invoice-check"), "15/4", None),
    # Excluding a probe the plan never wanted changes nothing.
    ("host-bench", 6, 3, ("diff-pricing",), "5/2", "define-legacy-consumer"),
    ("host-bench", 6, 3, ("define-legacy-consumer",), "7/2", "run-invoice-check"),
]


@pytest.mark.parametrize(
    ("name", "budget", "depth", "unobtainable", "expected", "recommended"), CONSTRAINED_CASES
)
def test_constrained_planner_matches_enumeration_without_the_unobtainable_probes(
    name: str,
    budget: int,
    depth: int,
    unobtainable: tuple[str, ...],
    expected: str,
    recommended: str | None,
) -> None:
    model = load_model(_model_payload(name))
    policies = trees(model, budget, depth, excluded=frozenset(unobtainable))
    # Never vacuous: abstaining is unconditionally admissible, so stopping on it is always a
    # policy — even when every probe is excluded and every other decision lacks its evidence.
    assert Tree(decision=model.abstain_decision_id) in policies
    assert not any(_uses(tree, set(unobtainable)) for tree in policies)
    weight = sum(w.weight for w in model.worlds)
    optimum = min(
        sum(Fraction(price(t, w.id, model) * w.weight, weight) for w in model.worlds)
        for t in policies
    )
    assert optimum == Fraction(expected)

    binding = LabBinding(
        host_id="oracle", agent_family=AgentFamily.CODEX, source_scope_digest="sha256:" + "0" * 64
    )
    state = initial_state(model, state_id="oracle", binding=binding)
    report = propose_excluding(
        model,
        state,
        unobtainable_probe_ids=unobtainable,
        expected_binding=binding,
        budget=budget,
        horizon=depth,
    )
    assert report.exact_for_declared_bounds
    assert Fraction(report.plan_value) == optimum
    assert report.recommended_probe_id == recommended
    assert report.unobtainable_probe_ids == tuple(sorted(unobtainable))


def _uses(tree: Tree, probe_ids: set[str]) -> bool:
    if tree.probe is None:
        return False
    return tree.probe in probe_ids or any(
        _uses(branch, probe_ids) for branch in tree.branches.values()
    )


def test_the_enumeration_really_loses_trees_when_a_probe_is_excluded() -> None:
    """The trap is live: the oracle's own exclusion rule removes policies, it is not a no-op."""
    model = load_model(_model_payload("lab-model.json"))
    free = trees(model, 2, 2)
    without_a = trees(model, 2, 2, excluded=frozenset({"check-a"}))
    assert any(_uses(tree, {"check-a"}) for tree in free)
    assert 0 < len(without_a) < len(free)

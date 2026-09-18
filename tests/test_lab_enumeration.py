"""Enumerate complete tiny policies, then price each world independently.

This oracle does not call the planner's Bellman recurrence or memoization.
It is intentionally restricted to two probes so complete tree enumeration fits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from pathlib import Path

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.planner import propose
from latent_compass.lab.state import LabBinding, initial_state


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
) -> list[Tree]:
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
        if probe.id in known or probe.cost > budget:
            continue
        labels = sorted({probe.outcomes[w] for w in worlds})
        continuations = [
            trees(model, budget - probe.cost, depth - 1, (*history, (probe.id, label)))
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

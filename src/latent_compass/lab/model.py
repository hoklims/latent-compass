"""HOK-798 — the finite decision model, per ADR 0011.

An operator supplies a finite set of possible worlds with positive integer
prior weights, terminal decisions with complete nonnegative integer loss
tables, and probes with a nonnegative integer cost and a total outcome table
over worlds. World tables represent joint possibilities: a probe's outcome is
a deterministic function of the true world, so correlated and complementary
probes need no independence assumption and no separate likelihood model.

A terminal decision may require specific ``(probe_id, outcome_id)`` evidence
pairs before it is admissible at all — not merely the fact that some probe
ran. Missing or wrong-outcome mandatory evidence cannot be bought away by a
low expected loss; :mod:`latent_compass.lab.planner` enforces this at every
node of the search, never only at the root.

Every model declares exactly one designated abstention decision, required to
carry no mandatory evidence, so a caller-supplied model can never make every
terminal choice conditional on evidence that might not arrive: the planner
always has a legal, unconditionally admissible stopping point.
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    Identifier,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.lab.contracts import (
    MAX_DECISIONS,
    MAX_LOSS,
    MAX_OUTCOMES_PER_PROBE,
    MAX_PRIOR_WEIGHT,
    MAX_PROBE_COST,
    MAX_PROBES,
    MAX_REQUIRED_EVIDENCE_PER_DECISION,
    MAX_WORLDS,
    SUPPORTED_LAB_VERSIONS,
)
from latent_compass.lab.errors import LabContractViolationError

__all__ = [
    "MODEL_SEAL_DOMAIN",
    "Decision",
    "DiagnosisModel",
    "EvidenceRequirement",
    "Probe",
    "WorldPrior",
    "load_model",
]

MODEL_SEAL_DOMAIN: Final = "lab.model.v1"


class WorldPrior(StrictModel):
    """One possible world and its positive integer prior weight."""

    id: Identifier
    weight: int = Field(ge=1, le=MAX_PRIOR_WEIGHT)


class Probe(StrictModel):
    """An acquirable observation with a total, deterministic outcome table.

    ``outcome_space`` is the probe's own closed set of possible outcomes.
    ``outcomes`` must name exactly one outcome, drawn from that set, for every
    world in the model — a *complete* table, so ``P(outcome | world)`` is
    always defined and always degenerate (0 or 1). This is what lets the
    planner compute an exact posterior by filtering rather than approximating
    a likelihood.
    """

    id: Identifier
    cost: int = Field(ge=0, le=MAX_PROBE_COST)
    outcome_space: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_OUTCOMES_PER_PROBE)
    outcomes: dict[Identifier, Identifier]

    @model_validator(mode="after")
    def _outcome_space_is_closed_and_declared(self) -> Self:
        if len(set(self.outcome_space)) != len(self.outcome_space):
            raise ValueError(f"probe {self.id!r} declares a duplicate outcome id")
        space = set(self.outcome_space)
        undeclared = sorted({outcome for outcome in self.outcomes.values() if outcome not in space})
        if undeclared:
            raise ValueError(
                f"probe {self.id!r} names outcome(s) {undeclared!r} outside its own outcome_space"
            )
        return self


class EvidenceRequirement(StrictModel):
    """One ``(probe_id, outcome_id)`` pair. Used both as a decision's mandatory
    evidence and as a diagnosis state's record of what was actually observed.
    """

    probe_id: Identifier
    outcome_id: Identifier


class Decision(StrictModel):
    """One terminal choice: a complete loss table plus optional mandatory evidence.

    ``required_evidence`` is a conjunction: every named pair must have been
    observed — probe acquired *and* that exact outcome seen — before this
    decision is admissible, independently of how low its expected loss is.
    """

    id: Identifier
    losses: dict[Identifier, int]
    required_evidence: tuple[EvidenceRequirement, ...] = Field(
        default=(), max_length=MAX_REQUIRED_EVIDENCE_PER_DECISION
    )

    @model_validator(mode="after")
    def _losses_are_bounded(self) -> Self:
        for world_id, loss in self.losses.items():
            if not (0 <= loss <= MAX_LOSS):
                raise ValueError(
                    f"decision {self.id!r} loss for world {world_id!r} is out of bounds: {loss}"
                )
        probe_ids = [requirement.probe_id for requirement in self.required_evidence]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError(f"decision {self.id!r} names the same required probe more than once")
        return self


class DiagnosisModel(StrictModel):
    """A closed, sealed, finite decision model. See module docstring."""

    contract_version: str = Field(min_length=5, max_length=20)
    model_id: Identifier
    worlds: tuple[WorldPrior, ...] = Field(min_length=1, max_length=MAX_WORLDS)
    probes: tuple[Probe, ...] = Field(max_length=MAX_PROBES, default=())
    decisions: tuple[Decision, ...] = Field(min_length=1, max_length=MAX_DECISIONS)
    abstain_decision_id: Identifier

    @model_validator(mode="after")
    def _model_is_closed_and_coherent(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_VERSIONS, "diagnosis model")

        world_ids = [world.id for world in self.worlds]
        if len(set(world_ids)) != len(world_ids):
            raise ValueError("diagnosis model declares a duplicate world id")
        world_id_set = frozenset(world_ids)

        probe_ids = [probe.id for probe in self.probes]
        if len(set(probe_ids)) != len(probe_ids):
            raise ValueError("diagnosis model declares a duplicate probe id")
        probe_by_id = {probe.id: probe for probe in self.probes}

        for probe in self.probes:
            covered = frozenset(probe.outcomes.keys())
            if covered != world_id_set:
                missing = sorted(world_id_set - covered)
                extra = sorted(covered - world_id_set)
                raise ValueError(
                    f"probe {probe.id!r} outcome table does not cover exactly the model's "
                    f"worlds: missing={missing!r} extra={extra!r}"
                )

        decision_ids = [decision.id for decision in self.decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("diagnosis model declares a duplicate decision id")

        for decision in self.decisions:
            covered = frozenset(decision.losses.keys())
            if covered != world_id_set:
                missing = sorted(world_id_set - covered)
                extra = sorted(covered - world_id_set)
                raise ValueError(
                    f"decision {decision.id!r} loss table does not cover exactly the model's "
                    f"worlds: missing={missing!r} extra={extra!r}"
                )
            for requirement in decision.required_evidence:
                required_probe = probe_by_id.get(requirement.probe_id)
                if required_probe is None:
                    raise ValueError(
                        f"decision {decision.id!r} requires unknown probe {requirement.probe_id!r}"
                    )
                if requirement.outcome_id not in required_probe.outcome_space:
                    raise ValueError(
                        f"decision {decision.id!r} requires outcome {requirement.outcome_id!r} "
                        f"undeclared by probe {required_probe.id!r}"
                    )

        if self.abstain_decision_id not in frozenset(decision_ids):
            raise ValueError(
                f"abstain_decision_id {self.abstain_decision_id!r} names no declared decision"
            )
        abstain = next(
            decision for decision in self.decisions if decision.id == self.abstain_decision_id
        )
        if abstain.required_evidence:
            raise ValueError(
                "the abstain decision must be unconditionally admissible and carry no "
                "required_evidence"
            )
        return self

    def probe_by_id(self, probe_id: str) -> Probe | None:
        for probe in self.probes:
            if probe.id == probe_id:
                return probe
        return None

    def decision_by_id(self, decision_id: str) -> Decision | None:
        for decision in self.decisions:
            if decision.id == decision_id:
                return decision
        return None

    def model_seal(self) -> str:
        """Reproducible seal over the whole model, in its own domain."""
        return seal(MODEL_SEAL_DOMAIN, self.canonical_payload())


def load_model(payload: object) -> DiagnosisModel:
    """Validate a raw JSON-shaped payload as a :class:`DiagnosisModel`.

    The only supported way to obtain a model: construction always passes
    through strict contract validation.
    """
    if not isinstance(payload, dict):
        raise LabContractViolationError(
            "diagnosis model must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabContractViolationError(
            "diagnosis model must declare contract_version", detail={"reason": "absent"}
        )
    check_contract_version(version, SUPPORTED_LAB_VERSIONS, "diagnosis model")
    return validate_contract(
        DiagnosisModel, payload, error=LabContractViolationError, context="diagnosis model"
    )

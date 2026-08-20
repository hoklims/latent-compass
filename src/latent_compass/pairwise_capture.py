"""HOK-234 — strict pre-action inputs for an independent pairwise labeler.

This module models capture, never judgment. It performs no file I/O, knows
nothing about corpus splits, and cannot observe a selected action or outcome.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Self, cast

from pydantic import AfterValidator, Field, field_validator, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS,
    Identifier,
    Seal,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import PairwiseCaptureViolation

__all__ = [
    "CANONICALIZATION_VERSION",
    "CandidateParameter",
    "ConstraintKind",
    "ContextConstraint",
    "DimensionEvidence",
    "EvidenceAvailability",
    "JudgeableCandidate",
    "JudgeableDecisionProjection",
    "JudgmentDimension",
    "PairwiseJudgeInput",
    "ScalarKind",
    "SemanticFact",
    "TypedScalar",
    "load_judgeable_projection",
    "load_pairwise_judge_input",
    "verify_pairwise_judge_input",
]

CANONICALIZATION_VERSION: Final = "1.0.0"
PROJECTION_SEAL_DOMAIN: Final = "pairwise.projection.v1"
PAIR_INPUT_SEAL_DOMAIN: Final = "pairwise.judge-input.v1"
INTEGER_ABS_MAX: Final = 1_000_000_000_000
FLOAT_ABS_MAX: Final = 1_000_000_000_000.0


def _no_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("text must not contain control characters")
    return value


ShortText = Annotated[
    str,
    Field(min_length=1, max_length=200),
    AfterValidator(_no_control_characters),
]
SummaryText = Annotated[
    str,
    Field(min_length=1, max_length=1000),
    AfterValidator(_no_control_characters),
]


class ScalarKind(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"


class JudgmentDimension(StrEnum):
    SUCCESS = "SUCCESS"
    VIOLATION = "VIOLATION"
    COST = "COST"
    INFORMATION = "INFORMATION"
    REVERSIBILITY = "REVERSIBILITY"


DIMENSION_ORDER: Final = tuple(JudgmentDimension)


class EvidenceAvailability(StrEnum):
    PRESENT = "PRESENT"
    NOT_JUDGEABLE = "NOT_JUDGEABLE"


class ConstraintKind(StrEnum):
    INVARIANT = "INVARIANT"
    LIMIT = "LIMIT"
    OBJECTIVE = "OBJECTIVE"


class TypedScalar(StrictModel):
    """One closed scalar value; arbitrary JSON is intentionally impossible."""

    kind: ScalarKind
    string_value: ShortText | None = None
    integer_value: Annotated[int, Field(ge=-INTEGER_ABS_MAX, le=INTEGER_ABS_MAX)] | None = None
    float_value: (
        Annotated[
            float,
            Field(ge=-FLOAT_ABS_MAX, le=FLOAT_ABS_MAX, allow_inf_nan=False),
        ]
        | None
    ) = None
    boolean_value: bool | None = None

    @field_validator("string_value", mode="before")
    @classmethod
    def _string_type_is_exact(cls, value: object) -> object:
        if value is not None and type(value) is not str:
            raise ValueError("string_value must be a JSON string")
        return value

    @field_validator("integer_value", mode="before")
    @classmethod
    def _integer_type_is_exact(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("integer_value must be a JSON integer")
        return value

    @field_validator("float_value", mode="before")
    @classmethod
    def _float_type_is_exact(cls, value: object) -> object:
        if value is not None and type(value) is not float:
            raise ValueError("float_value must be a JSON float")
        return value

    @field_validator("boolean_value", mode="before")
    @classmethod
    def _boolean_type_is_exact(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("boolean_value must be a JSON boolean")
        return value

    @model_validator(mode="after")
    def _one_value_matching_kind(self) -> Self:
        values = {
            ScalarKind.STRING: self.string_value,
            ScalarKind.INTEGER: self.integer_value,
            ScalarKind.FLOAT: self.float_value,
            ScalarKind.BOOLEAN: self.boolean_value,
        }
        present = [kind for kind, value in values.items() if value is not None]
        if present != [self.kind]:
            raise ValueError("exactly the value field named by kind must be present")
        return self


class CandidateParameter(StrictModel):
    name: Identifier
    value: TypedScalar


class SemanticFact(StrictModel):
    fact_id: Identifier
    statement: SummaryText
    source_digest: Seal


class ContextConstraint(StrictModel):
    constraint_id: Identifier
    kind: ConstraintKind
    statement: SummaryText
    source_digest: Seal


class DimensionEvidence(StrictModel):
    dimension: JudgmentDimension
    availability: EvidenceAvailability
    facts: tuple[SemanticFact, ...] = Field(default=(), max_length=32)
    reason: SummaryText | None = None

    @model_validator(mode="after")
    def _availability_matches_content(self) -> Self:
        fact_ids = [fact.fact_id for fact in self.facts]
        if fact_ids != sorted(fact_ids) or len(set(fact_ids)) != len(fact_ids):
            raise ValueError("evidence facts must have unique fact_ids in canonical order")
        if self.availability is EvidenceAvailability.PRESENT:
            if not self.facts or self.reason is not None:
                raise ValueError("PRESENT evidence requires facts and forbids a reason")
        elif self.facts or self.reason is None:
            raise ValueError("NOT_JUDGEABLE evidence requires a reason and forbids facts")
        return self


class JudgeableCandidate(StrictModel):
    direction_id: Identifier
    sanitized_summary: SummaryText
    parameters: tuple[CandidateParameter, ...] = Field(default=(), max_length=64)
    applicable_constraint_ids: tuple[Identifier, ...] = Field(default=(), max_length=64)
    evidence: tuple[DimensionEvidence, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _candidate_is_canonical_and_complete(self) -> Self:
        parameter_names = [parameter.name for parameter in self.parameters]
        if parameter_names != sorted(parameter_names) or len(set(parameter_names)) != len(
            parameter_names
        ):
            raise ValueError("parameters must have unique names in canonical order")
        if list(self.applicable_constraint_ids) != sorted(self.applicable_constraint_ids) or len(
            set(self.applicable_constraint_ids)
        ) != len(self.applicable_constraint_ids):
            raise ValueError("constraint ids must be unique and canonically ordered")
        if tuple(item.dimension for item in self.evidence) != DIMENSION_ORDER:
            raise ValueError("evidence must contain every dimension once in canonical order")
        return self


class PairwiseJudgeInput(StrictModel):
    contract_version: str
    canonicalization_version: str
    source_projection_seal: Seal
    decision_point_id: Identifier
    objective_summary: SummaryText
    context_facts: tuple[SemanticFact, ...] = Field(default=(), max_length=128)
    constraints: tuple[ContextConstraint, ...] = Field(default=(), max_length=128)
    candidates: tuple[JudgeableCandidate, JudgeableCandidate]

    @model_validator(mode="after")
    def _pair_contract_and_order(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS,
            "pairwise judge input",
        )
        if self.canonicalization_version != CANONICALIZATION_VERSION:
            raise ValueError("unsupported canonicalization version")
        direction_ids = [candidate.direction_id for candidate in self.candidates]
        if direction_ids != sorted(direction_ids) or len(set(direction_ids)) != 2:
            raise ValueError("pair candidates must be distinct and canonically ordered")

        fact_ids = [fact.fact_id for fact in self.context_facts]
        if fact_ids != sorted(fact_ids) or len(set(fact_ids)) != len(fact_ids):
            raise ValueError("context facts must have unique fact_ids in canonical order")
        constraint_ids = [constraint.constraint_id for constraint in self.constraints]
        if constraint_ids != sorted(constraint_ids) or len(set(constraint_ids)) != len(
            constraint_ids
        ):
            raise ValueError("constraints must have unique ids in canonical order")
        known_constraints = set(constraint_ids)
        for candidate in self.candidates:
            unknown = set(candidate.applicable_constraint_ids) - known_constraints
            if unknown:
                raise ValueError(
                    f"candidate {candidate.direction_id!r} references unknown constraints: "
                    f"{sorted(unknown)}"
                )
        return self

    def input_seal(self) -> str:
        return seal(PAIR_INPUT_SEAL_DOMAIN, self.canonical_payload())


class JudgeableDecisionProjection(StrictModel):
    contract_version: str
    canonicalization_version: str
    decision_point_id: Identifier
    captured_at: Timestamp
    producer_id: Identifier
    objective_summary: SummaryText
    context_facts: tuple[SemanticFact, ...] = Field(default=(), max_length=128)
    constraints: tuple[ContextConstraint, ...] = Field(default=(), max_length=128)
    candidates: tuple[JudgeableCandidate, ...] = Field(min_length=2, max_length=256)

    @model_validator(mode="after")
    def _projection_is_closed_and_canonical(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS,
            "judgeable projection",
        )
        if self.canonicalization_version != CANONICALIZATION_VERSION:
            raise ValueError("unsupported canonicalization version")

        fact_ids = [fact.fact_id for fact in self.context_facts]
        if fact_ids != sorted(fact_ids) or len(set(fact_ids)) != len(fact_ids):
            raise ValueError("context facts must have unique fact_ids in canonical order")
        constraint_ids = [constraint.constraint_id for constraint in self.constraints]
        if constraint_ids != sorted(constraint_ids) or len(set(constraint_ids)) != len(
            constraint_ids
        ):
            raise ValueError("constraints must have unique ids in canonical order")

        direction_ids = [candidate.direction_id for candidate in self.candidates]
        if direction_ids != sorted(direction_ids) or len(set(direction_ids)) != len(direction_ids):
            raise ValueError("candidates must have unique direction_ids in canonical order")
        known_constraints = set(constraint_ids)
        for candidate in self.candidates:
            unknown = set(candidate.applicable_constraint_ids) - known_constraints
            if unknown:
                raise ValueError(
                    f"candidate {candidate.direction_id!r} references unknown constraints: "
                    f"{sorted(unknown)}"
                )
        return self

    def projection_seal(self) -> str:
        return seal(PROJECTION_SEAL_DOMAIN, self.canonical_payload())

    def derive_pair(self, first_direction_id: str, second_direction_id: str) -> PairwiseJudgeInput:
        if first_direction_id == second_direction_id:
            raise PairwiseCaptureViolation("a pair requires two distinct candidates")
        by_id = {candidate.direction_id: candidate for candidate in self.candidates}
        requested = {first_direction_id, second_direction_id}
        missing = requested - set(by_id)
        if missing:
            raise PairwiseCaptureViolation(
                "pair references an unknown candidate",
                detail={"missing_direction_ids": sorted(missing)},
            )
        ordered = cast(
            tuple[JudgeableCandidate, JudgeableCandidate],
            tuple(by_id[direction_id] for direction_id in sorted(requested)),
        )
        return PairwiseJudgeInput(
            contract_version=self.contract_version,
            canonicalization_version=self.canonicalization_version,
            source_projection_seal=self.projection_seal(),
            decision_point_id=self.decision_point_id,
            objective_summary=self.objective_summary,
            context_facts=self.context_facts,
            constraints=self.constraints,
            candidates=ordered,
        )


def _declared_version(payload: object, *, context: str) -> str:
    if not isinstance(payload, dict):
        raise PairwiseCaptureViolation(f"{context} must be a JSON object")
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise PairwiseCaptureViolation(
            f"{context} must declare contract_version",
            detail={"context": context, "reason": "absent"},
        )
    return version


def load_judgeable_projection(payload: object) -> JudgeableDecisionProjection:
    declared = _declared_version(payload, context="judgeable projection")
    check_contract_version(
        declared,
        SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS,
        "judgeable projection",
    )
    return validate_contract(
        JudgeableDecisionProjection,
        payload,
        error=PairwiseCaptureViolation,
        context="judgeable projection",
    )


def load_pairwise_judge_input(payload: object) -> PairwiseJudgeInput:
    """Validate pair structure only; this does not authenticate its source seal."""

    declared = _declared_version(payload, context="pairwise judge input")
    check_contract_version(
        declared,
        SUPPORTED_JUDGEABLE_PROJECTION_VERSIONS,
        "pairwise judge input",
    )
    return validate_contract(
        PairwiseJudgeInput,
        payload,
        error=PairwiseCaptureViolation,
        context="pairwise judge input",
    )


def verify_pairwise_judge_input(
    payload: object,
    source_projection: JudgeableDecisionProjection,
) -> PairwiseJudgeInput:
    """Require a pair to be exactly derivable from the supplied projection preimage."""

    pair = load_pairwise_judge_input(payload)
    expected_projection_seal = source_projection.projection_seal()
    if pair.source_projection_seal != expected_projection_seal:
        raise PairwiseCaptureViolation(
            "pair source projection seal does not match the supplied projection",
            detail={
                "expected": expected_projection_seal,
                "actual": pair.source_projection_seal,
            },
        )
    expected = source_projection.derive_pair(
        pair.candidates[0].direction_id,
        pair.candidates[1].direction_id,
    )
    if pair.canonical_payload() != expected.canonical_payload():
        raise PairwiseCaptureViolation(
            "pair is not the canonical derivation of the supplied projection"
        )
    return pair

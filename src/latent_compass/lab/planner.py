"""HOK-798 — bounded, exact value-of-observation planning, per ADR 0011.

Implements the Bellman recursion from the ADR exactly:

    V(E, b, h) = min(R(E), min_o(cost(o) + sum_y P(y|E,o) V(E+y, b-cost(o), h-1)))

where ``R(E)`` is the minimum expected terminal loss among *admissible*
decisions — a decision whose ``required_evidence`` is a subset of what has
actually been observed on this path, never merely a low expected loss — and
the inner minimum ranges over probes not yet acquired and affordable under
the remaining budget. All arithmetic is exact (:class:`fractions.Fraction`):
a posterior probability ``P(y|E,o)`` is always a ratio of the integer prior
weights that remain, so there is no floating-point rounding anywhere in the
search.

The search is bounded by an explicit, caller-visible ``max_expansions``
ceiling on top of the declared ``horizon``. Once the ceiling is reached, every
further node is scored as its stopping value only. This is an upper bound on
the minimum remaining loss, so the estimated benefit of probing is conservative,
not an exact optimum. The report's ``search_exhausted`` flag is set. ``exact_for_declared_bounds``
is true only when the ceiling was never reached; even then it is exact only
*relative to the supplied finite model, budget and horizon*, never a claim
about the real world.

:func:`propose_excluding` solves the same recursion with the inner minimum
restricted to probes outside a host-declared set ``X`` of unobtainable ones,
at **every** node of the search and not only the first:

    V_X(E, b, h) = min(R(E), min_{o not in X}(cost(o) + sum_y P(y|E,o) V_X(E+y, b-cost(o), h-1)))

Its result is a separate document, a ``1.1.0`` :class:`ConstrainedPlanReport`
that names ``X``. :func:`propose` and the ``1.0.0`` :class:`PlanReport` are
unchanged, and neither loader accepts the other's version: a value planned
around ``X`` is never read as an unrestricted one. That a probe is unobtainable
is the host's declaration. The lab does not verify it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from fractions import Fraction
from typing import Final, Literal, Self

from pydantic import Field, model_validator

from latent_compass.contracts import (
    Identifier,
    Seal,
    StrictModel,
    check_contract_version,
    validate_contract,
)
from latent_compass.lab.contracts import (
    LAB_CONSTRAINED_PLAN_CONTRACT_VERSION,
    LAB_CONTRACT_VERSION,
    LAB_NON_AUTHORITY_NOTICE,
    MAX_BUDGET,
    MAX_EXPANSIONS,
    MAX_HORIZON,
    MAX_PROBES,
    SUPPORTED_LAB_CONSTRAINED_PLAN_VERSIONS,
    SUPPORTED_LAB_VERSIONS,
)
from latent_compass.lab.errors import (
    LabBindingMismatchError,
    LabContractViolationError,
    LabCrossModelStateError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.model import Decision, DiagnosisModel, load_model
from latent_compass.lab.state import DiagnosisStateRevision, LabBinding, _trust_state, load_state

__all__ = [
    "ConstrainedPlanReport",
    "DecisionValuation",
    "PlanReport",
    "ProbeValuation",
    "load_constrained_plan_report",
    "load_plan_report",
    "propose",
    "propose_excluding",
]

_MAX_FRACTION_DIGITS: Final = 40


def _fraction_text(value: Fraction) -> str:
    """Render an exact rational as ``"numerator/denominator"``, lowest terms.

    A plain string, not a float: the whole point of exact arithmetic is that
    nothing downstream is tempted to compare it with ``==`` against a
    binary-floating-point literal.
    """
    if (
        len(str(abs(value.numerator))) > _MAX_FRACTION_DIGITS
        or len(str(value.denominator)) > _MAX_FRACTION_DIGITS
    ):
        raise LabContractViolationError(  # pragma: no cover - guarded by model/budget bounds
            "an exact expected-value fraction grew beyond the bounds this build renders",
            detail={"numerator_digits": len(str(value.numerator))},
        )
    return f"{value.numerator}/{value.denominator}"


class DecisionValuation(StrictModel):
    """One terminal decision's exact expected loss under the current posterior.

    ``expected_loss`` is always the real expected loss under the posterior,
    computed whether or not the decision is admissible — so a caller can see,
    for example, that a decision's expected loss is exactly zero while it
    remains illegal for want of its required evidence.
    """

    decision_id: Identifier
    admissible: bool
    expected_loss: str


class ProbeValuation(StrictModel):
    """One probe's exact one-branch-deep continuation value, if it was explored.

    ``value`` is ``None`` when the probe was not affordable, was already
    acquired, or the search horizon was already exhausted at this node — never
    a fabricated number standing in for "not computed".
    """

    probe_id: Identifier
    cost: int
    affordable: bool
    value: str | None


class PlanReport(StrictModel):
    """The result of one bounded planning call. A recommendation, not a decision."""

    contract_version: str = Field(min_length=5, max_length=20)
    model_seal: Seal
    state_seal: Seal
    budget: int = Field(ge=0, le=MAX_BUDGET)
    horizon: int = Field(ge=0, le=MAX_HORIZON)
    max_expansions: int = Field(ge=1, le=MAX_EXPANSIONS)
    expansions_used: int = Field(ge=0)
    search_exhausted: bool
    exact_for_declared_bounds: bool
    stopping_decision_id: Identifier
    stopping_value: str
    recommended_action: Literal["STOP", "PROBE"]
    recommended_probe_id: Identifier | None = Field(default=None)
    plan_value: str
    decisions: tuple[DecisionValuation, ...]
    probes: tuple[ProbeValuation, ...]
    non_authority_notice: str = Field(default=LAB_NON_AUTHORITY_NOTICE)

    @model_validator(mode="after")
    def _report_is_coherent(self) -> Self:
        check_contract_version(self.contract_version, SUPPORTED_LAB_VERSIONS, "plan report")
        if self.exact_for_declared_bounds and self.search_exhausted:
            raise ValueError("a search cannot be both exhausted and exact for its declared bounds")
        if self.recommended_action == "PROBE" and self.recommended_probe_id is None:
            raise ValueError("a PROBE recommendation must name the recommended probe")
        if self.recommended_action == "STOP" and self.recommended_probe_id is not None:
            raise ValueError("a STOP recommendation must not name a probe")
        return self


def load_plan_report(payload: object) -> PlanReport:
    """Validate a raw JSON-shaped payload as a :class:`PlanReport`."""
    if not isinstance(payload, dict):
        raise LabContractViolationError(
            "plan report must be a JSON object", detail={"received_type": type(payload).__name__}
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabContractViolationError(
            "plan report must declare contract_version", detail={"reason": "absent"}
        )
    check_contract_version(version, SUPPORTED_LAB_VERSIONS, "plan report")
    return validate_contract(
        PlanReport, payload, error=LabContractViolationError, context="plan report"
    )


class ConstrainedPlanReport(StrictModel):
    """A plan computed around probes the host declared unobtainable. Lab contract ``1.1.0``.

    A sibling of :class:`PlanReport`, deliberately not a subclass: every value
    here is conditional on ``unobtainable_probe_ids``, so a consumer typed for
    the unrestricted report must not accept this one by accident. The shared
    fields mean what they mean there. A probe's ``value`` is also ``None`` when
    the probe is unobtainable; ``affordable`` still only compares cost and
    budget. ``exclusion_basis`` says on the record that the lab took the host's
    word: excluding a probe that could in fact be obtained yields a plan that
    is exact for the restricted problem and may be worse than the unrestricted
    one.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    model_seal: Seal
    state_seal: Seal
    budget: int = Field(ge=0, le=MAX_BUDGET)
    horizon: int = Field(ge=0, le=MAX_HORIZON)
    max_expansions: int = Field(ge=1, le=MAX_EXPANSIONS)
    expansions_used: int = Field(ge=0)
    search_exhausted: bool
    exact_for_declared_bounds: bool
    stopping_decision_id: Identifier
    stopping_value: str
    recommended_action: Literal["STOP", "PROBE"]
    recommended_probe_id: Identifier | None = Field(default=None)
    plan_value: str
    decisions: tuple[DecisionValuation, ...]
    probes: tuple[ProbeValuation, ...]
    unobtainable_probe_ids: tuple[Identifier, ...] = Field(max_length=MAX_PROBES)
    exclusion_basis: Literal["HOST_DECLARED"]
    non_authority_notice: str = Field(default=LAB_NON_AUTHORITY_NOTICE)

    @model_validator(mode="after")
    def _report_is_coherent(self) -> Self:
        check_contract_version(
            self.contract_version,
            SUPPORTED_LAB_CONSTRAINED_PLAN_VERSIONS,
            "constrained plan report",
        )
        if self.exact_for_declared_bounds and self.search_exhausted:
            raise ValueError("a search cannot be both exhausted and exact for its declared bounds")
        if self.recommended_action == "PROBE" and self.recommended_probe_id is None:
            raise ValueError("a PROBE recommendation must name the recommended probe")
        if self.recommended_action == "STOP" and self.recommended_probe_id is not None:
            raise ValueError("a STOP recommendation must not name a probe")
        if list(self.unobtainable_probe_ids) != sorted(set(self.unobtainable_probe_ids)):
            raise ValueError("unobtainable probe ids must be sorted and named at most once")
        unobtainable = set(self.unobtainable_probe_ids)
        if self.recommended_probe_id in unobtainable:
            raise ValueError("a plan cannot recommend a probe it was told is unobtainable")
        listed = {probe.probe_id for probe in self.probes}
        if not unobtainable <= listed:
            raise ValueError("an unobtainable probe must be one of the probes this report lists")
        if any(probe.value is not None for probe in self.probes if probe.probe_id in unobtainable):
            raise ValueError("an unobtainable probe carries no continuation value")
        return self


def load_constrained_plan_report(payload: object) -> ConstrainedPlanReport:
    """Validate a raw JSON-shaped payload as a :class:`ConstrainedPlanReport`."""
    if not isinstance(payload, dict):
        raise LabContractViolationError(
            "constrained plan report must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    version = payload.get("contract_version")
    if not isinstance(version, str):
        raise LabContractViolationError(
            "constrained plan report must declare contract_version", detail={"reason": "absent"}
        )
    check_contract_version(
        version, SUPPORTED_LAB_CONSTRAINED_PLAN_VERSIONS, "constrained plan report"
    )
    return validate_contract(
        ConstrainedPlanReport,
        payload,
        error=LabContractViolationError,
        context="constrained plan report",
    )


def _admissible_decisions(
    model: DiagnosisModel, satisfied: frozenset[tuple[str, str]]
) -> list[Decision]:
    admissible = []
    for decision in model.decisions:
        required = {(req.probe_id, req.outcome_id) for req in decision.required_evidence}
        if required <= satisfied:
            admissible.append(decision)
    return admissible


def _expected_loss(posterior: dict[str, int], decision: Decision) -> Fraction:
    total = sum(posterior.values())
    numerator = sum(weight * decision.losses[world_id] for world_id, weight in posterior.items())
    return Fraction(numerator, total)


def _stopping(
    model: DiagnosisModel, posterior: dict[str, int], satisfied: frozenset[tuple[str, str]]
) -> tuple[str, Fraction]:
    """``R(E)``: the minimum expected loss among admissible decisions.

    The model guarantees a designated, unconditionally admissible abstain
    decision, so the admissible set is always non-empty. Ties break on the
    lexicographically smallest decision id, deterministically — never on
    dict/set iteration order.
    """
    admissible = sorted(_admissible_decisions(model, satisfied), key=lambda decision: decision.id)
    best_id = admissible[0].id
    best_value = _expected_loss(posterior, admissible[0])
    for decision in admissible[1:]:
        value = _expected_loss(posterior, decision)
        if value < best_value:
            best_id, best_value = decision.id, value
    return best_id, best_value


def _search(
    model: DiagnosisModel,
    posterior: dict[str, int],
    satisfied: frozenset[tuple[str, str]],
    *,
    budget: int,
    horizon: int,
    memo: dict[tuple[object, ...], tuple[Fraction, str | None]],
    counter: list[int],
    max_expansions: int,
    exhausted: list[bool],
    record: dict[str, Fraction] | None = None,
    excluded: frozenset[str] = frozenset(),
) -> tuple[Fraction, str | None]:
    key = (tuple(sorted(posterior.items())), satisfied, budget, horizon)
    if record is None:
        cached = memo.get(key)
        if cached is not None:
            return cached

    _, stop_value = _stopping(model, posterior, satisfied)
    if horizon == 0 or exhausted[0]:
        memo[key] = (stop_value, None)
        return stop_value, None
    if counter[0] >= max_expansions:
        exhausted[0] = True
        memo[key] = (stop_value, None)
        return stop_value, None
    counter[0] += 1

    acquired = {probe_id for probe_id, _ in satisfied}
    candidates = [
        probe
        for probe in model.probes
        if probe.id not in acquired and probe.id not in excluded and probe.cost <= budget
    ]
    best_value, best_probe = stop_value, None
    total = sum(posterior.values())
    for probe in sorted(candidates, key=lambda item: item.id):
        partitions: dict[str, dict[str, int]] = {}
        for world_id, weight in posterior.items():
            outcome = probe.outcomes[world_id]
            partitions.setdefault(outcome, {})[world_id] = weight
        continuation = Fraction(0)
        for outcome, sub_posterior in partitions.items():
            probability = Fraction(sum(sub_posterior.values()), total)
            sub_value, _ = _search(
                model,
                sub_posterior,
                satisfied | {(probe.id, outcome)},
                budget=budget - probe.cost,
                horizon=horizon - 1,
                memo=memo,
                counter=counter,
                max_expansions=max_expansions,
                exhausted=exhausted,
                excluded=excluded,
            )
            continuation += probability * sub_value
        candidate_value = Fraction(probe.cost) + continuation
        if record is not None:
            record[probe.id] = candidate_value
        if candidate_value < best_value:
            best_value, best_probe = candidate_value, probe.id

    memo[key] = (best_value, best_probe)
    return best_value, best_probe


def _require_exact_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    """Refuse anything that is not an exact, in-bounds Python ``int``.

    ``bool`` is an ``int`` subclass and a plain range comparison would accept
    it silently as ``0``/``1``; a ``float`` — including ``inf`` and ``nan`` —
    would also satisfy ``minimum <= value <= maximum`` for ordinary values,
    contaminating budget/horizon bookkeeping that the rest of this module
    keeps exact on purpose. Checked before the bounded search starts, not
    only at the final report's JSON-mode validation, so a costly search never
    runs on a value that was never a declared size to begin with.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabContractViolationError(
            f"{name} must be an exact integer, not {type(value).__name__}",
            detail={"name": name, "received_type": type(value).__name__},
        )
    if not (minimum <= value <= maximum):
        raise LabContractViolationError(
            f"{name} is outside this build's declared bounds",
            detail={"name": name, "value": value, "minimum": minimum, "maximum": maximum},
        )
    return value


def _require_known_probes(model: DiagnosisModel, value: object) -> frozenset[str]:
    """Refuse anything that is not a collection of distinct probe ids of ``model``.

    A bare string is refused rather than iterated character by character, and an
    id the model does not declare is refused rather than ignored: a mistyped
    exclusion would otherwise exclude nothing while the host believes its plan
    avoids the probe.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise LabContractViolationError(
            "unobtainable_probe_ids must be a collection of probe ids",
            detail={"received_type": type(value).__name__},
        )
    items = list(value)
    if not all(isinstance(item, str) for item in items):
        raise LabContractViolationError(
            "every unobtainable probe id must be a string",
            detail={"received_types": sorted({type(item).__name__ for item in items})},
        )
    if len(set(items)) != len(items):
        raise LabContractViolationError(
            "an unobtainable probe id must be named at most once",
            detail={"unobtainable_probe_ids": sorted(items)},
        )
    unknown = sorted(set(items) - {probe.id for probe in model.probes})
    if unknown:
        raise LabUnknownReferenceError(
            "an unobtainable probe id names no probe of this model",
            detail={"unknown_probe_ids": unknown},
        )
    return frozenset(items)


def _plan(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    *,
    budget: int,
    horizon: int,
    expected_binding: LabBinding,
    max_expansions: int,
    history: Sequence[DiagnosisStateRevision] | None,
    unobtainable_probe_ids: object | None,
) -> dict[str, object]:
    """The shared body of :func:`propose` and :func:`propose_excluding`.

    ``unobtainable_probe_ids`` is ``None`` for the unrestricted ``1.0.0`` plan,
    whose payload this returns exactly as before. Anything else, the empty
    collection included, yields the ``1.1.0`` payload that names the exclusions.
    """
    model = load_model(model.canonical_payload())
    state = load_state(state.canonical_payload())
    expected_binding = validate_contract(
        LabBinding,
        expected_binding.canonical_payload(),
        error=LabContractViolationError,
        context="expected diagnosis binding",
    )

    if state.model_seal != model.model_seal():
        raise LabCrossModelStateError(
            "diagnosis state was computed against a different sealed model",
            detail={
                "state_model_seal": state.model_seal,
                "presented_model_seal": model.model_seal(),
            },
        )

    _trust_state(model, state, history)

    if (
        state.binding.host_id != expected_binding.host_id
        or state.binding.agent_family != expected_binding.agent_family
    ):
        raise LabBindingMismatchError(
            "diagnosis state's binding does not match the host/agent-family a caller expects "
            "at this consumer boundary",
            detail={
                "state_host_id": state.binding.host_id,
                "expected_host_id": expected_binding.host_id,
                "state_agent_family": state.binding.agent_family.value,
                "expected_agent_family": expected_binding.agent_family.value,
            },
        )
    if state.binding.source_scope_digest != expected_binding.source_scope_digest:
        raise LabSourceScopeMismatchError(
            "source scope has drifted since this episode began; a new episode is required",
            detail={
                "episode_source_scope_digest": state.binding.source_scope_digest,
                "presented_source_scope_digest": expected_binding.source_scope_digest,
            },
        )

    budget = _require_exact_int(budget, name="budget", minimum=0, maximum=MAX_BUDGET)
    horizon = _require_exact_int(horizon, name="horizon", minimum=0, maximum=MAX_HORIZON)
    max_expansions = _require_exact_int(
        max_expansions, name="max_expansions", minimum=1, maximum=MAX_EXPANSIONS
    )
    excluded = (
        frozenset[str]()
        if unobtainable_probe_ids is None
        else _require_known_probes(model, unobtainable_probe_ids)
    )

    posterior = dict(state.posterior_weights)
    satisfied = state.satisfied_pairs()
    acquired = state.acquired_probe_ids()

    memo: dict[tuple[object, ...], tuple[Fraction, str | None]] = {}
    counter = [0]
    exhausted = [False]
    top_level_probe_values: dict[str, Fraction] = {}

    plan_value, recommended_probe = _search(
        model,
        posterior,
        satisfied,
        budget=budget,
        horizon=horizon,
        memo=memo,
        counter=counter,
        max_expansions=max_expansions,
        exhausted=exhausted,
        record=top_level_probe_values,
        excluded=excluded,
    )
    stopping_decision_id, stopping_value = _stopping(model, posterior, satisfied)

    decisions = tuple(
        DecisionValuation(
            decision_id=decision.id,
            admissible=decision in _admissible_decisions(model, satisfied),
            expected_loss=_fraction_text(_expected_loss(posterior, decision)),
        )
        for decision in sorted(model.decisions, key=lambda item: item.id)
    )
    probes = tuple(
        ProbeValuation(
            probe_id=probe.id,
            cost=probe.cost,
            affordable=probe.id not in acquired and probe.cost <= budget,
            value=(
                _fraction_text(top_level_probe_values[probe.id])
                if probe.id in top_level_probe_values
                else None
            ),
        )
        for probe in sorted(model.probes, key=lambda item: item.id)
    )

    payload: dict[str, object] = {
        "contract_version": LAB_CONTRACT_VERSION,
        "model_seal": model.model_seal(),
        "state_seal": state.state_seal(),
        "budget": budget,
        "horizon": horizon,
        "max_expansions": max_expansions,
        "expansions_used": counter[0],
        "search_exhausted": exhausted[0],
        "exact_for_declared_bounds": not exhausted[0],
        "stopping_decision_id": stopping_decision_id,
        "stopping_value": _fraction_text(stopping_value),
        "recommended_action": "STOP" if recommended_probe is None else "PROBE",
        "recommended_probe_id": recommended_probe,
        "plan_value": _fraction_text(plan_value),
        "decisions": [decision.canonical_payload() for decision in decisions],
        "probes": [probe.canonical_payload() for probe in probes],
        "non_authority_notice": LAB_NON_AUTHORITY_NOTICE,
    }
    if unobtainable_probe_ids is not None:
        payload["contract_version"] = LAB_CONSTRAINED_PLAN_CONTRACT_VERSION
        payload["unobtainable_probe_ids"] = sorted(excluded)
        payload["exclusion_basis"] = "HOST_DECLARED"
    return payload


def propose(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    *,
    budget: int,
    horizon: int,
    expected_binding: LabBinding,
    max_expansions: int = MAX_EXPANSIONS,
    history: Sequence[DiagnosisStateRevision] | None = None,
) -> PlanReport:
    """Compute ``V(E, budget, horizon)`` from ``state``'s current posterior.

    ``model`` and ``state`` are revalidated from their own canonical payloads
    first (see :mod:`latent_compass.lab.state`'s module docstring), then
    ``state`` is either cheaply recomputed (revision 0) or replayed in full
    from ``history`` (any higher revision) before its posterior is trusted at
    all — matching a presented model's seal is not evidence a posterior
    replays. Refuses before searching when: ``state`` was computed against a
    different sealed model; ``state`` does not replay; ``state``'s binding
    does not match ``expected_binding``'s host, agent family or source scope;
    or ``budget``/``horizon``/``max_expansions`` are not exact integers within
    this build's declared bounds.
    """
    payload = _plan(
        model,
        state,
        budget=budget,
        horizon=horizon,
        expected_binding=expected_binding,
        max_expansions=max_expansions,
        history=history,
        unobtainable_probe_ids=None,
    )
    return validate_contract(
        PlanReport, payload, error=LabContractViolationError, context="plan report"
    )


def propose_excluding(
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    *,
    unobtainable_probe_ids: Collection[str],
    budget: int,
    horizon: int,
    expected_binding: LabBinding,
    max_expansions: int = MAX_EXPANSIONS,
    history: Sequence[DiagnosisStateRevision] | None = None,
) -> ConstrainedPlanReport:
    """Compute ``V_X(E, budget, horizon)``: plan around probes that cannot be obtained.

    Everything :func:`propose` refuses is refused here, in the same order. Then
    ``unobtainable_probe_ids`` must be a collection of distinct probe ids of
    ``model`` — checked before the search starts. Those probes are removed from
    the candidates at every node, so a decision whose required evidence only
    they could supply stays inadmissible on every path. Naming a probe that was
    already acquired is allowed and changes nothing; an empty collection plans
    exactly as :func:`propose` does and still returns the ``1.1.0`` document.
    """
    payload = _plan(
        model,
        state,
        budget=budget,
        horizon=horizon,
        expected_binding=expected_binding,
        max_expansions=max_expansions,
        history=history,
        unobtainable_probe_ids=unobtainable_probe_ids,
    )
    return validate_contract(
        ConstrainedPlanReport,
        payload,
        error=LabContractViolationError,
        context="constrained plan report",
    )

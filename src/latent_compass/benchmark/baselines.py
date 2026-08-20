"""HOK-188 — the four pre-registered baselines.

A **closed** registry of four pure functions. There is no plugin hook, no
callback parameter, no dynamic import, no weights file, no fit step and no
network call. A baseline maps a :class:`~latent_compass.benchmark.corpus.
PublicCaseView`, a seed and a sealed context to one direction and one
confidence, and it can do nothing else, because nothing else is reachable from
its arguments.

The four
--------
``fixed-canonical`` (TRIVIAL)
    The first ``direction_id`` in canonical order. Ignores everything else.

``least-uncertainty`` (STRONG)
    The candidate with the smallest ``prior_uncertainty``; canonical tie-break.

``seeded-uniform`` (TRIVIAL)
    A uniform pick over the candidates, drawn from a seal over
    ``(spec_seal, corpus_seal, case_id, seed)``. No global random state is
    touched, so the choice for a case depends on that case alone and is
    unchanged by the order in which cases are executed, by how many other cases
    ran first, or by whether another baseline ran at all.

``logged-propensity-arbiter`` (STRONG)
    The candidate the logging policy was most likely to take; ties broken by
    smallest ``prior_uncertainty``, then canonically. Not a learned model: it
    reads the logged propensities carried by the case and nothing else.

Versions are pinned next to the code they describe. Their declared identity,
kind and version are sealed and checked before any case is read. The seal does
not hash Python function bodies: a code change requires a reviewed version bump,
backed by the discriminating regression cases below the registry boundary.
Report verification re-executes the installed implementation and detects an
observable mismatch with a supplied report.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import Field

from latent_compass.benchmark.budget import CaseBudgetMeter
from latent_compass.benchmark.corpus import PublicCandidateView, PublicCaseView
from latent_compass.canonical import canonical_bytes, seal
from latent_compass.contracts import Identifier, StrictModel, UnitInterval
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import BaselineKind

__all__ = [
    "BASELINES",
    "BaselineAlgorithm",
    "BaselineChoice",
    "BaselineId",
    "SelectionContext",
    "baseline_registry_seal",
    "select",
]

_DRAW_DOMAIN: Final = "benchmark.baseline.seeded-uniform"
_REGISTRY_SEAL_DOMAIN: Final = "benchmark.baselines"


class BaselineId(StrEnum):
    """The four identities. Closed: there is no fifth, and no alias."""

    FIXED_CANONICAL = "fixed-canonical"
    LEAST_UNCERTAINTY = "least-uncertainty"
    SEEDED_UNIFORM = "seeded-uniform"
    LOGGED_PROPENSITY_ARBITER = "logged-propensity-arbiter"


class SelectionContext(StrictModel):
    """The sealed identity of the run, as a baseline may see it.

    Carries seals, never data. A baseline can derive a reproducible draw from
    it and can learn nothing about any outcome from it.
    """

    spec_seal: str = Field(min_length=1, max_length=200)
    corpus_seal: str = Field(min_length=1, max_length=200)


class BaselineChoice(StrictModel):
    """What one baseline decided for one case and seed."""

    direction_id: Identifier
    confidence: UnitInterval


SelectFn = Callable[[PublicCaseView, int, SelectionContext, CaseBudgetMeter], BaselineChoice]


class BaselineAlgorithm(StrictModel):
    """One registered baseline's sealed identity.

    The implementation lives in a separate table keyed by the same identity, so
    the sealed record can never accidentally contain a function object — and so
    the seal is over what was *pre-registered*, not over a code address.
    """

    baseline_id: BaselineId
    algorithm_version: Identifier
    kind: BaselineKind
    description: str = Field(min_length=1, max_length=500)

    def identity(self) -> dict[str, object]:
        """The sealed part: identity and version, never the function object."""
        return {
            "baseline_id": self.baseline_id.value,
            "algorithm_version": self.algorithm_version,
            "kind": self.kind.value,
        }


def _inspect_all(view: PublicCaseView, meter: CaseBudgetMeter) -> tuple[PublicCandidateView, ...]:
    """Charge one inspection per candidate and return them in canonical order.

    Every baseline pays this, because every one of them has to look at the whole
    candidate set to answer at all. Charging it in one shared helper is what
    makes the inspection counts comparable rather than an artefact of how each
    function happens to be written.
    """
    ordered = tuple(sorted(view.candidates, key=lambda candidate: candidate.direction_id))
    for _ in ordered:
        meter.inspect_candidate()
    return ordered


def _fixed_canonical(
    view: PublicCaseView, _seed: int, _context: SelectionContext, meter: CaseBudgetMeter
) -> BaselineChoice:
    ordered = _inspect_all(view, meter)
    chosen = ordered[0]
    return BaselineChoice(direction_id=chosen.direction_id, confidence=1.0 / len(ordered))


def _least_uncertainty(
    view: PublicCaseView, _seed: int, _context: SelectionContext, meter: CaseBudgetMeter
) -> BaselineChoice:
    ordered = _inspect_all(view, meter)
    # `min` returns the earliest minimal element, and `ordered` is canonical, so
    # the canonical tie-break falls out of the scan rather than being asserted.
    chosen = min(ordered, key=lambda candidate: candidate.prior_uncertainty)
    return BaselineChoice(
        direction_id=chosen.direction_id, confidence=1.0 - chosen.prior_uncertainty
    )


def _seeded_uniform(
    view: PublicCaseView, seed: int, context: SelectionContext, meter: CaseBudgetMeter
) -> BaselineChoice:
    ordered = _inspect_all(view, meter)
    meter.draw()
    digest = hashlib.sha256()
    digest.update(_DRAW_DOMAIN.encode("utf-8"))
    digest.update(
        canonical_bytes(
            {
                "spec_seal": context.spec_seal,
                "corpus_seal": context.corpus_seal,
                "case_id": view.case_id,
                "seed": seed,
            }
        )
    )
    index = int.from_bytes(digest.digest(), "big") % len(ordered)
    chosen = ordered[index]
    return BaselineChoice(direction_id=chosen.direction_id, confidence=1.0 / len(ordered))


def _logged_propensity_arbiter(
    view: PublicCaseView, _seed: int, _context: SelectionContext, meter: CaseBudgetMeter
) -> BaselineChoice:
    ordered = _inspect_all(view, meter)
    # `max` returns the earliest maximal element and `ordered` is canonical, so
    # the three-level rule — highest propensity, then lowest uncertainty, then
    # canonical — is exactly this key scanned in this order.
    chosen = max(
        ordered, key=lambda candidate: (candidate.propensity, -candidate.prior_uncertainty)
    )
    return BaselineChoice(direction_id=chosen.direction_id, confidence=chosen.propensity)


_ALGORITHMS: Final[dict[BaselineId, BaselineAlgorithm]] = {
    BaselineId.FIXED_CANONICAL: BaselineAlgorithm(
        baseline_id=BaselineId.FIXED_CANONICAL,
        algorithm_version="1.0.0",
        kind=BaselineKind.TRIVIAL,
        description="select the first direction_id in canonical order",
    ),
    BaselineId.LEAST_UNCERTAINTY: BaselineAlgorithm(
        baseline_id=BaselineId.LEAST_UNCERTAINTY,
        algorithm_version="1.0.0",
        kind=BaselineKind.STRONG,
        description="select the smallest prior_uncertainty, canonical tie-break",
    ),
    BaselineId.SEEDED_UNIFORM: BaselineAlgorithm(
        baseline_id=BaselineId.SEEDED_UNIFORM,
        algorithm_version="1.0.0",
        kind=BaselineKind.TRIVIAL,
        description="uniform pick derived from the spec seal, corpus seal, case and seed",
    ),
    BaselineId.LOGGED_PROPENSITY_ARBITER: BaselineAlgorithm(
        baseline_id=BaselineId.LOGGED_PROPENSITY_ARBITER,
        algorithm_version="1.0.0",
        kind=BaselineKind.STRONG,
        description="maximise the logged propensity, then minimise uncertainty, then canonical",
    ),
}

_SELECTORS: Final[dict[BaselineId, SelectFn]] = {
    BaselineId.FIXED_CANONICAL: _fixed_canonical,
    BaselineId.LEAST_UNCERTAINTY: _least_uncertainty,
    BaselineId.SEEDED_UNIFORM: _seeded_uniform,
    BaselineId.LOGGED_PROPENSITY_ARBITER: _logged_propensity_arbiter,
}

if set(_ALGORITHMS) != set(BaselineId) or set(_SELECTORS) != set(BaselineId):  # pragma: no cover
    raise RuntimeError("the baseline registry does not cover exactly the declared identities")

#: The registry, as an immutable view. A caller cannot add a fifth baseline
#: through it, and :func:`select` refuses an unregistered identity anyway.
BASELINES: Final[MappingProxyType[BaselineId, BaselineAlgorithm]] = MappingProxyType(_ALGORITHMS)


def baseline_registry_seal() -> str:
    """Seal over the four identities and their pinned versions.

    Sealed into the report, so a report produced by a build whose baselines were
    re-versioned cannot be confused with one produced by this build.
    """
    return seal(
        _REGISTRY_SEAL_DOMAIN,
        [BASELINES[identity].identity() for identity in sorted(BASELINES)],
    )


def select(
    baseline_id: BaselineId,
    view: PublicCaseView,
    *,
    seed: int,
    context: SelectionContext,
    meter: CaseBudgetMeter,
) -> BaselineChoice:
    """Run one registered baseline, refusing anything not in the registry.

    The chosen direction is checked back against the candidate set: a baseline
    that answered with a direction the case never offered is a broken baseline,
    not a result.
    """
    algorithm = _SELECTORS.get(baseline_id)
    if algorithm is None:  # pragma: no cover - unreachable while BaselineId is closed
        raise BenchmarkViolation("unregistered baseline", detail={"baseline_id": str(baseline_id)})
    choice = algorithm(view, seed, context, meter)
    if choice.direction_id not in view.canonical_direction_ids():
        raise BenchmarkViolation(
            "baseline selected a direction that is not a candidate of the case",
            detail={
                "baseline_id": baseline_id.value,
                "case_id": view.case_id,
                "selected": choice.direction_id,
            },
        )
    return choice

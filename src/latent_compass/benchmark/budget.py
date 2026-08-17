"""HOK-188 — the common compute budget.

Every baseline receives the **same** grant: the identical :class:`BudgetGrant`
object, sealed into the spec, is handed to all four. Fairness is therefore not a
property of the runner's discipline; it is a property of there being one grant.

Consumption may differ — that is the interesting signal — but the caps are
enforced on the *first* operation past the line, not tallied afterwards. A
baseline that has already inspected a forbidden candidate has already had the
unfair look, whatever the harness does with the count next.

Nothing here consults wall-clock time. A wall-clock budget would make the
benchmark irreproducible on a different machine, and "the report differs because
the host was slower" is indistinguishable from "the report differs because the
result changed".
"""

from __future__ import annotations

from pydantic import Field

from latent_compass.contracts import Identifier, StrictModel
from latent_compass.errors import BudgetExceeded

__all__ = [
    "BudgetGrant",
    "BudgetLedgerEntry",
    "BudgetReceipt",
    "CaseBudgetMeter",
    "require_case_count_within_budget",
]


class BudgetGrant(StrictModel):
    """The deterministic allowance, identical for every baseline."""

    max_cases: int = Field(ge=1, le=4096)
    max_candidate_inspections_per_case: int = Field(ge=1, le=4096)
    max_random_draws_per_case: int = Field(ge=0, le=4096)


class BudgetReceipt(StrictModel):
    """What one baseline actually spent on one case, for one seed."""

    candidate_inspections: int = Field(ge=0)
    random_draws: int = Field(ge=0)


class CaseBudgetMeter:
    """Meters a single baseline invocation on a single case and seed.

    Deliberately not a contract model: it is mutable working state, and the
    durable artefact is the :class:`BudgetReceipt` it closes into.
    """

    def __init__(self, grant: BudgetGrant, *, baseline_id: str, case_id: str, seed: int) -> None:
        self._grant = grant
        self._baseline_id = baseline_id
        self._case_id = case_id
        self._seed = seed
        self._inspections = 0
        self._draws = 0

    def _where(self) -> dict[str, object]:
        return {"baseline_id": self._baseline_id, "case_id": self._case_id, "seed": self._seed}

    def inspect_candidate(self) -> None:
        """Charge one candidate inspection, refusing the one past the cap."""
        if self._inspections >= self._grant.max_candidate_inspections_per_case:
            raise BudgetExceeded(
                "baseline exceeded the common candidate-inspection budget",
                detail={
                    **self._where(),
                    "cap": self._grant.max_candidate_inspections_per_case,
                    "requested": self._inspections + 1,
                },
            )
        self._inspections += 1

    def draw(self) -> None:
        """Charge one pseudo-random draw, refusing the one past the cap."""
        if self._draws >= self._grant.max_random_draws_per_case:
            raise BudgetExceeded(
                "baseline exceeded the common random-draw budget",
                detail={
                    **self._where(),
                    "cap": self._grant.max_random_draws_per_case,
                    "requested": self._draws + 1,
                },
            )
        self._draws += 1

    def receipt(self) -> BudgetReceipt:
        """Close the meter into a durable receipt."""
        return BudgetReceipt(candidate_inspections=self._inspections, random_draws=self._draws)


def require_case_count_within_budget(grant: BudgetGrant, case_count: int, *, split: str) -> None:
    """Refuse a corpus larger than the pre-registered case cap.

    Checked before any baseline runs, so an over-large corpus produces no
    partial receipts to be mistaken for a completed run.
    """
    if case_count > grant.max_cases:
        raise BudgetExceeded(
            "the corpus holds more cases than the common budget allows",
            detail={"split": split, "cap": grant.max_cases, "case_count": case_count},
        )


class BudgetLedgerEntry(StrictModel):
    """One baseline's measured consumption, aggregated over the whole run."""

    baseline_id: Identifier
    total_candidate_inspections: int = Field(ge=0)
    total_random_draws: int = Field(ge=0)
    max_candidate_inspections_on_a_case: int = Field(ge=0)
    max_random_draws_on_a_case: int = Field(ge=0)

"""HOK-186 — proofs that pre-registration cannot be edited after the fact."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from conftest import EPOCH, measurement_payload, protocol_payload
from latent_compass.authority import ContinueKill
from latent_compass.contracts import validate_contract
from latent_compass.errors import ContractViolation, ProtocolViolation
from latent_compass.protocol import (
    HoldoutLedger,
    MeasurementSet,
    Preregistration,
    evaluate,
    load_preregistration,
)

NEXT_EPOCH = "LC-HOK181-E2-0000beef"
CONSUMED_AT = "2026-08-14T13:00:00Z"


def build_measurements(payload: dict[str, Any]) -> MeasurementSet:
    return validate_contract(
        MeasurementSet, payload, error=ContractViolation, context="measurement set"
    )


@pytest.fixture
def protocol() -> Preregistration:
    return load_preregistration(protocol_payload())


# -- the pre-registration itself -------------------------------------------


def test_a_complete_protocol_validates_and_seals_stably(protocol: Preregistration) -> None:
    assert protocol.protocol_seal() == protocol.protocol_seal()
    assert protocol.protocol_seal() == load_preregistration(protocol_payload()).protocol_seal()


def test_every_metric_family_must_be_fixed_in_advance() -> None:
    payload = protocol_payload()
    payload["metrics"] = [
        metric for metric in payload["metrics"] if metric["family"] != "CALIBRATION"
    ]
    with pytest.raises(ProtocolViolation):
        load_preregistration(payload)


def test_both_a_trivial_and_a_strong_baseline_are_required() -> None:
    payload = protocol_payload()
    payload["baselines"] = [payload["baselines"][0], dict(payload["baselines"][0], name="other")]
    with pytest.raises(ProtocolViolation):
        load_preregistration(payload)


@pytest.mark.parametrize("seeds", [[37, 11, 23], [11, 11, 23], [11]])
def test_seeds_must_be_unique_ordered_and_repeated(seeds: list[int]) -> None:
    with pytest.raises(ProtocolViolation):
        load_preregistration(protocol_payload(seeds=seeds))


def test_splits_must_be_exactly_train_validation_holdout() -> None:
    payload = protocol_payload()
    payload["splits"] = payload["splits"][:2]
    with pytest.raises(ProtocolViolation):
        load_preregistration(payload)


def test_each_split_must_declare_disjointness_from_both_others() -> None:
    payload = protocol_payload()
    payload["splits"][2]["disjoint_from"] = ["TRAIN"]
    with pytest.raises(ProtocolViolation):
        load_preregistration(payload)


def test_splits_may_not_share_a_corpus_seal() -> None:
    payload = protocol_payload()
    payload["splits"][2]["corpus_seal"] = payload["splits"][0]["corpus_seal"]
    with pytest.raises(ProtocolViolation):
        load_preregistration(payload)


def test_a_protocol_must_declare_sensitivity_analyses_and_failure_cases() -> None:
    for field in ("sensitivity_analyses", "failure_cases"):
        with pytest.raises(ProtocolViolation):
            load_preregistration(protocol_payload(**{field: []}))


# -- revision --------------------------------------------------------------


def test_a_revision_must_advance_both_revision_and_epoch(protocol: Preregistration) -> None:
    with pytest.raises(ProtocolViolation, match="both a new revision number and a new epoch"):
        protocol.revise(revision=2)
    with pytest.raises(ProtocolViolation, match="must open a new epoch"):
        protocol.revise(revision=2, epoch=EPOCH)
    with pytest.raises(ProtocolViolation, match="must strictly increase"):
        protocol.revise(revision=1, epoch=NEXT_EPOCH)


def test_a_valid_revision_changes_the_seal(protocol: Preregistration) -> None:
    revised = protocol.revise(revision=2, epoch=NEXT_EPOCH)
    assert revised.revision == 2
    assert revised.epoch == NEXT_EPOCH
    assert revised.protocol_seal() != protocol.protocol_seal()


@pytest.mark.parametrize(
    "mutation",
    [
        {"tasks": ["only-one-task"]},
        {"seeds": [11, 23, 37, 41]},
    ],
)
def test_any_substantive_edit_changes_the_seal(
    protocol: Preregistration, mutation: dict[str, Any]
) -> None:
    revised = protocol.revise(revision=2, epoch=NEXT_EPOCH, **mutation)
    assert revised.protocol_seal() != protocol.protocol_seal()


# -- the load-bearing scenario ---------------------------------------------


def test_a_threshold_moved_after_the_fact_cannot_score_the_old_measurements(
    protocol: Preregistration, tmp_path: Path
) -> None:
    """The hostile scenario, end to end.

    Measurements are collected under protocol 1 and fail it. The operator then
    lowers the threshold to make them pass. The lowered protocol is a different
    seal, so the old measurements cannot be scored against it, and the holdout
    of the new protocol has not been collected.
    """
    ledger = HoldoutLedger(tmp_path / "holdout.json")
    measurements = build_measurements(
        measurement_payload(
            protocol.protocol_seal(),
            split="HOLDOUT",
            purpose="FINAL_VERDICT",
            values={"success-rate": 0.41},
        )
    )
    verdict = evaluate(protocol, measurements, holdout_ledger=ledger, consumed_at=CONSUMED_AT)
    assert verdict.decision is ContinueKill.KILL
    assert verdict.failing_metrics == ("success-rate",)

    lowered = [metric.canonical_payload() for metric in protocol.metrics]
    for metric in lowered:
        if metric["name"] == "success-rate":
            metric["threshold"] = 0.4
    rescued = protocol.revise(revision=2, epoch=NEXT_EPOCH, metrics=lowered)

    with pytest.raises(ProtocolViolation, match="different protocol seal"):
        evaluate(rescued, measurements, holdout_ledger=ledger, consumed_at=CONSUMED_AT)


def test_measurements_from_another_epoch_are_refused(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal(), epoch=NEXT_EPOCH)
    with pytest.raises(ProtocolViolation, match="different epoch"):
        evaluate(protocol, build_measurements(payload))


# -- holdout discipline ----------------------------------------------------


@pytest.mark.parametrize("purpose", ["TRAINING", "SELECTION", "EXPLORATION"])
def test_the_holdout_may_not_be_used_for_anything_but_a_final_verdict(
    protocol: Preregistration, tmp_path: Path, purpose: str
) -> None:
    measurements = build_measurements(
        measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose=purpose)
    )
    with pytest.raises(ProtocolViolation, match="only be used for a final verdict"):
        evaluate(
            protocol,
            measurements,
            holdout_ledger=HoldoutLedger(tmp_path / "holdout.json"),
            consumed_at=CONSUMED_AT,
        )


def test_a_final_verdict_is_only_meaningful_on_the_holdout(protocol: Preregistration) -> None:
    measurements = build_measurements(
        measurement_payload(protocol.protocol_seal(), split="VALIDATION", purpose="FINAL_VERDICT")
    )
    with pytest.raises(ProtocolViolation, match="only meaningful on the holdout"):
        evaluate(protocol, measurements)


def test_a_final_holdout_verdict_requires_a_durable_ledger(protocol: Preregistration) -> None:
    measurements = build_measurements(
        measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT")
    )
    with pytest.raises(ProtocolViolation, match="durable holdout ledger"):
        evaluate(protocol, measurements)


def test_the_holdout_is_spent_once_and_the_record_outlives_the_process(
    protocol: Preregistration, tmp_path: Path
) -> None:
    path = tmp_path / "holdout.json"
    measurements = build_measurements(
        measurement_payload(protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT")
    )
    first = evaluate(
        protocol, measurements, holdout_ledger=HoldoutLedger(path), consumed_at=CONSUMED_AT
    )
    assert first.decision is ContinueKill.CONTINUE

    reopened = HoldoutLedger(path)
    recorded = reopened.consumption(protocol.holdout_corpus_seal())
    assert recorded is not None
    assert recorded["consumed_at"] == CONSUMED_AT
    assert recorded["protocol_seal"] == protocol.protocol_seal()
    with pytest.raises(ProtocolViolation, match="already been consumed"):
        evaluate(
            protocol, measurements, holdout_ledger=reopened, consumed_at="2026-08-15T09:00:00Z"
        )


def test_a_refused_evaluation_does_not_spend_the_holdout(
    protocol: Preregistration, tmp_path: Path
) -> None:
    path = tmp_path / "holdout.json"
    incomplete = measurement_payload(
        protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT", seeds=(11, 23)
    )
    with pytest.raises(ProtocolViolation, match="missing measurements"):
        evaluate(
            protocol,
            build_measurements(incomplete),
            holdout_ledger=HoldoutLedger(path),
            consumed_at=CONSUMED_AT,
        )
    assert HoldoutLedger(path).consumption(protocol.holdout_corpus_seal()) is None

    complete = measurement_payload(
        protocol.protocol_seal(), split="HOLDOUT", purpose="FINAL_VERDICT"
    )
    evaluate(
        protocol,
        build_measurements(complete),
        holdout_ledger=HoldoutLedger(path),
        consumed_at=CONSUMED_AT,
    )


def test_an_unreadable_holdout_ledger_is_not_treated_as_unused(
    protocol: Preregistration, tmp_path: Path
) -> None:
    path = tmp_path / "holdout.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProtocolViolation, match="unreadable"):
        HoldoutLedger(path).consumption(protocol.holdout_corpus_seal())

    path.write_text('["a list, not a mapping"]', encoding="utf-8")
    with pytest.raises(ProtocolViolation, match="malformed"):
        HoldoutLedger(path).consumption(protocol.holdout_corpus_seal())


# -- scoring ---------------------------------------------------------------


def test_a_required_metric_missing_a_seed_is_refused(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal(), seeds=(11, 23))
    with pytest.raises(ProtocolViolation, match="missing measurements for pre-registered seeds"):
        evaluate(protocol, build_measurements(payload))


def test_a_required_metric_with_no_measurements_is_refused(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"] = [
        item for item in payload["measurements"] if item["metric"] != "p99-regret"
    ]
    with pytest.raises(ProtocolViolation, match="required metric has no measurements"):
        evaluate(protocol, build_measurements(payload))


def test_a_metric_that_was_not_pre_registered_is_refused(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"].append(
        {"metric": "invented-after-the-fact", "split": "VALIDATION", "seed": 11, "value": 1.0}
    )
    with pytest.raises(ProtocolViolation, match="were not pre-registered"):
        evaluate(protocol, build_measurements(payload))


def test_a_duplicate_metric_and_seed_is_refused(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"].append(dict(payload["measurements"][0]))
    with pytest.raises(ProtocolViolation, match="duplicate measurement"):
        evaluate(protocol, build_measurements(payload))


def test_measurements_must_agree_with_their_declared_split(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"][0]["split"] = "TRAIN"
    with pytest.raises(ContractViolation):
        build_measurements(payload)


def test_an_optional_metric_may_be_absent(protocol: Preregistration) -> None:
    verdict = evaluate(protocol, build_measurements(measurement_payload(protocol.protocol_seal())))
    assert "drift-divergence" not in {item.metric for item in verdict.metrics}
    assert verdict.decision is ContinueKill.CONTINUE


def test_aggregation_is_the_median_not_the_mean(protocol: Preregistration) -> None:
    """Values chosen so median and mean fall on opposite sides of the threshold."""
    payload = measurement_payload(protocol.protocol_seal())
    payload["measurements"] = [
        item for item in payload["measurements"] if item["metric"] != "success-rate"
    ]
    for seed, value in zip((11, 23, 37), (0.10, 0.70, 0.90), strict=True):
        payload["measurements"].append(
            {"metric": "success-rate", "split": "VALIDATION", "seed": seed, "value": value}
        )
    verdict = evaluate(protocol, build_measurements(payload))
    scored = next(item for item in verdict.metrics if item.metric == "success-rate")
    assert scored.aggregate == pytest.approx(0.70)
    assert sum((0.10, 0.70, 0.90)) / 3 < scored.threshold <= scored.aggregate
    assert scored.passed


def test_a_failing_required_metric_kills(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal(), values={"violation-rate": 0.5})
    verdict = evaluate(protocol, build_measurements(payload))
    assert verdict.decision is ContinueKill.KILL
    assert verdict.failing_metrics == ("violation-rate",)


def test_a_failing_optional_metric_does_not_kill(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    for seed in (11, 23, 37):
        payload["measurements"].append(
            {"metric": "drift-divergence", "split": "VALIDATION", "seed": seed, "value": 9.0}
        )
    verdict = evaluate(protocol, build_measurements(payload))
    assert verdict.decision is ContinueKill.CONTINUE
    drift = next(item for item in verdict.metrics if item.metric == "drift-divergence")
    assert drift.passed is False


def test_identical_data_produces_an_identical_verdict(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    first = evaluate(protocol, build_measurements(payload))
    second = evaluate(protocol, build_measurements(payload))
    assert first.verdict_seal == second.verdict_seal
    assert first.canonical_payload() == second.canonical_payload()


def test_the_verdict_does_not_depend_on_measurement_order(protocol: Preregistration) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    shuffled = dict(payload)
    items = list(payload["measurements"])
    random.Random(1234).shuffle(items)  # noqa: S311 - shuffling a fixture, not keying anything
    shuffled["measurements"] = items
    assert items != payload["measurements"]
    assert (
        evaluate(protocol, build_measurements(shuffled)).verdict_seal
        == evaluate(protocol, build_measurements(payload)).verdict_seal
    )


def test_a_different_threshold_produces_a_different_verdict_seal(
    protocol: Preregistration,
) -> None:
    payload = measurement_payload(protocol.protocol_seal())
    baseline = evaluate(protocol, build_measurements(payload))

    lowered = [metric.canonical_payload() for metric in protocol.metrics]
    for metric in lowered:
        if metric["name"] == "success-rate":
            metric["threshold"] = 0.1
    revised = protocol.revise(revision=2, epoch=NEXT_EPOCH, metrics=lowered)
    rescored = evaluate(
        protocol=revised,
        measurements=build_measurements(
            measurement_payload(revised.protocol_seal(), epoch=NEXT_EPOCH)
        ),
    )
    assert rescored.verdict_seal != baseline.verdict_seal

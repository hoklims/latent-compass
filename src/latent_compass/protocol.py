"""HOK-186 — the pre-registered evaluation protocol.

Everything that could otherwise be chosen *after* seeing a result is fixed
before any result exists: tasks, splits, corpus seals, baselines, seeds,
metrics, thresholds and the continue/kill rule. The protocol is then sealed.

Four properties carry the weight:

**A change is never silent.** Any edit — a threshold, a metric, a corpus seal —
changes :meth:`Preregistration.protocol_seal`. Measurements carry the seal they
were collected under *and* the corpus seal of the split they came from, and
both are verified.

**The spent resource is the corpus, not the protocol.** A holdout consumption
is keyed on the holdout ``corpus_seal``. Revising a protocol produces a new
protocol seal, but if the revision reuses the same holdout corpus it cannot
re-arm it. Otherwise "revise, then re-measure" would be an unlimited supply of
final verdicts over one corpus.

**Check-and-consume is atomic across processes.** :class:`HoldoutLedger` reads,
decides and writes while holding an exclusive lock, so two processes racing on
one corpus produce exactly one consumption, and two processes spending
*different* corpora both land.

**Identical data yields an identical verdict.** Aggregation is a median over
the declared seeds; the verdict is sealed; nothing consults wall-clock time,
iteration order or randomness.

This module does not train, optimise, tune or compare models, and makes no
claim about intelligence. It scores measurements handed to it, against
thresholds fixed before the measurements existed.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Final, cast

from pydantic import Field, model_validator

from latent_compass.canonical import canonical_bytes, seal
from latent_compass.contracts import (
    PROTOCOL_CONTRACT_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    FiniteFloat,
    Identifier,
    StrictModel,
    Timestamp,
    check_contract_version,
    validate_contract,
)
from latent_compass.errors import ProtocolViolation
from latent_compass.vocabulary import ContinueKill

__all__ = [
    "HOLDOUT_LOCK_TIMEOUT_SECONDS",
    "REQUIRED_METRIC_FAMILIES",
    "BaselineKind",
    "BaselineSpec",
    "HoldoutConsumption",
    "HoldoutLedger",
    "HoldoutPurpose",
    "Measurement",
    "MeasurementSet",
    "MetricDirection",
    "MetricFamily",
    "MetricSpec",
    "MetricVerdict",
    "Preregistration",
    "Split",
    "SplitSpec",
    "Verdict",
    "evaluate",
    "load_measurement_set",
    "load_preregistration",
    "load_verdict",
    "recompute_verdict_seal",
    "score_measurements",
]

PROTOCOL_SEAL_DOMAIN: Final = "protocol.preregistration"
MEASUREMENT_SEAL_DOMAIN: Final = "protocol.measurements"
VERDICT_SEAL_DOMAIN: Final = "protocol.verdict"

#: How long a process waits for the holdout lock before refusing. Refusing is
#: the only safe outcome: proceeding without the lock would mean deciding
#: "unused" from a read that another process is in the middle of invalidating.
HOLDOUT_LOCK_TIMEOUT_SECONDS: Final = 20.0


class Split(StrEnum):
    """The three disjoint corpora. All three must be declared."""

    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


class HoldoutPurpose(StrEnum):
    """Why a measurement set is being scored.

    Only :attr:`FINAL_VERDICT` may touch the holdout, and only once per corpus.
    """

    FINAL_VERDICT = "FINAL_VERDICT"
    TRAINING = "TRAINING"
    SELECTION = "SELECTION"
    EXPLORATION = "EXPLORATION"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"


class MetricFamily(StrEnum):
    """The dimensions a protocol must fix before it may run."""

    SUCCESS = "SUCCESS"
    VIOLATION = "VIOLATION"
    COST = "COST"
    INFORMATION = "INFORMATION"
    REVERSIBILITY = "REVERSIBILITY"
    CALIBRATION = "CALIBRATION"
    TAIL = "TAIL"
    DRIFT = "DRIFT"


#: A protocol that leaves any of these unfixed is not pre-registered.
REQUIRED_METRIC_FAMILIES: Final = frozenset(MetricFamily)


class BaselineKind(StrEnum):
    TRIVIAL = "TRIVIAL"
    STRONG = "STRONG"


class MetricSpec(StrictModel):
    """One metric, its direction, and the threshold fixed in advance."""

    name: Identifier
    family: MetricFamily
    direction: MetricDirection
    threshold: FiniteFloat
    required: bool = Field(default=True)

    def passes(self, value: float) -> bool:
        """Whether ``value`` satisfies the pre-registered threshold."""
        if self.direction is MetricDirection.HIGHER_IS_BETTER:
            return value >= self.threshold
        return value <= self.threshold


class SplitSpec(StrictModel):
    """One corpus split, sealed, with its declared disjointness."""

    split: Split
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    item_count: int = Field(ge=1)
    disjoint_from: tuple[Split, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _disjointness_is_coherent(self) -> SplitSpec:
        if self.split in self.disjoint_from:
            raise ValueError("a split cannot be declared disjoint from itself")
        if len(set(self.disjoint_from)) != len(self.disjoint_from):
            raise ValueError("disjoint_from must not repeat a split")
        return self


class BaselineSpec(StrictModel):
    """A comparison point fixed before any result exists."""

    name: Identifier
    kind: BaselineKind
    description: Annotated[str, Field(min_length=1, max_length=500)]


class Preregistration(StrictModel):
    """The full pre-registered protocol. Sealed; revised only explicitly."""

    contract_version: str = Field(min_length=5, max_length=20)
    protocol_id: Identifier
    revision: int = Field(ge=1)
    epoch: Identifier
    registered_at: Timestamp
    tasks: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = Field(
        min_length=1, max_length=64
    )
    splits: tuple[SplitSpec, ...] = Field(min_length=3, max_length=3)
    baselines: tuple[BaselineSpec, ...] = Field(min_length=2, max_length=32)
    seeds: tuple[int, ...] = Field(min_length=2, max_length=64)
    metrics: tuple[MetricSpec, ...] = Field(min_length=8, max_length=64)
    sensitivity_analyses: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        min_length=1, max_length=32
    )
    failure_cases: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        min_length=1, max_length=32
    )

    @model_validator(mode="after")
    def _preregistration_invariants(self) -> Preregistration:
        check_contract_version(self.contract_version, SUPPORTED_PROTOCOL_VERSIONS, "protocol")

        declared = [spec.split for spec in self.splits]
        if sorted(declared) != sorted(Split):
            raise ValueError("exactly one TRAIN, one VALIDATION and one HOLDOUT split are required")
        for spec in self.splits:
            expected = {other for other in Split if other is not spec.split}
            if set(spec.disjoint_from) != expected:
                raise ValueError(f"{spec.split} must declare disjointness from {sorted(expected)}")
        corpus_seals = [spec.corpus_seal for spec in self.splits]
        if len(set(corpus_seals)) != len(corpus_seals):
            raise ValueError("each split must have a distinct corpus seal")

        if list(self.seeds) != sorted(self.seeds):
            raise ValueError("seeds must be listed in ascending order")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique")

        names = [metric.name for metric in self.metrics]
        if len(set(names)) != len(names):
            raise ValueError("metric names must be unique")
        families = {metric.family for metric in self.metrics}
        missing = REQUIRED_METRIC_FAMILIES - families
        if missing:
            raise ValueError(
                "protocol leaves metric families unfixed: "
                + ", ".join(sorted(family.value for family in missing))
            )
        if not any(metric.required for metric in self.metrics):
            raise ValueError("at least one metric must be required for a continue/kill rule")

        kinds = {baseline.kind for baseline in self.baselines}
        if kinds != {BaselineKind.TRIVIAL, BaselineKind.STRONG}:
            raise ValueError("both a trivial and a strong baseline must be pre-registered")
        baseline_names = [baseline.name for baseline in self.baselines]
        if len(set(baseline_names)) != len(baseline_names):
            raise ValueError("baseline names must be unique")
        return self

    def protocol_seal(self) -> str:
        """Seal over the entire pre-registration."""
        return seal(PROTOCOL_SEAL_DOMAIN, self.canonical_payload())

    def split_spec(self, split: Split) -> SplitSpec:
        for spec in self.splits:
            if spec.split is split:
                return spec
        raise ProtocolViolation(  # pragma: no cover - forbidden by the validator
            f"split {split} is not declared", detail={"split": split.value}
        )

    def holdout_corpus_seal(self) -> str:
        """The identity of the spendable resource."""
        return self.split_spec(Split.HOLDOUT).corpus_seal

    def required_metrics(self) -> tuple[MetricSpec, ...]:
        return tuple(metric for metric in self.metrics if metric.required)

    def revise(self, **changes: object) -> Preregistration:
        """Produce a successor protocol.

        A revision must advance both ``revision`` and ``epoch``. Reusing either
        is refused: a protocol edit that kept its epoch would let post-hoc
        thresholds inherit the credibility of the pre-registration they replace.

        A revision does **not** replenish the holdout. If it reuses the same
        holdout corpus, that corpus stays spent.
        """
        if "revision" not in changes or "epoch" not in changes:
            raise ProtocolViolation(
                "a revision must supply both a new revision number and a new epoch",
                detail={"supplied": sorted(changes)},
            )
        new_revision = changes["revision"]
        if not isinstance(new_revision, int) or new_revision <= self.revision:
            raise ProtocolViolation(
                "revision number must strictly increase",
                detail={"current": self.revision, "supplied": new_revision},
            )
        if changes["epoch"] == self.epoch:
            raise ProtocolViolation(
                "a revision must open a new epoch", detail={"epoch": self.epoch}
            )
        payload = self.canonical_payload()
        payload.update(changes)
        revised = validate_contract(
            Preregistration, payload, error=ProtocolViolation, context="protocol revision"
        )
        if revised.protocol_seal() == self.protocol_seal():
            raise ProtocolViolation(  # pragma: no cover - unreachable while epoch is sealed
                "a revision must change the protocol seal",
                detail={"protocol_seal": self.protocol_seal()},
            )
        return revised


def load_preregistration(payload: object) -> Preregistration:
    """Validate an untrusted payload into a :class:`Preregistration`."""
    if not isinstance(payload, dict):
        raise ProtocolViolation(
            "protocol payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise ProtocolViolation(
            "protocol declares no contract_version",
            detail={"contract": "protocol", "reason": "absent"},
        )
    declared = payload["contract_version"]
    if not isinstance(declared, str):
        raise ProtocolViolation(
            "contract_version must be a string",
            detail={"received_type": type(declared).__name__},
        )
    check_contract_version(declared, SUPPORTED_PROTOCOL_VERSIONS, "protocol")
    return validate_contract(Preregistration, payload, error=ProtocolViolation, context="protocol")


class Measurement(StrictModel):
    """One metric value, for one seed, on one split."""

    metric: Identifier
    split: Split
    seed: int
    value: FiniteFloat


class MeasurementSet(StrictModel):
    """Measurements, bound to the protocol *and* the corpus they came from.

    ``protocol_seal`` alone says which rules applied. ``corpus_seal`` says which
    data was measured. Without the second, any corpus could be scored under a
    protocol that pre-registered a different one.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    purpose: HoldoutPurpose
    split: Split
    measurements: tuple[Measurement, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _measurements_match_declared_split(self) -> MeasurementSet:
        check_contract_version(self.contract_version, SUPPORTED_PROTOCOL_VERSIONS, "protocol")
        for measurement in self.measurements:
            if measurement.split is not self.split:
                raise ValueError(
                    f"measurement on {measurement.split} in a {self.split} measurement set"
                )
        return self

    def measurement_seal(self) -> str:
        """Seal the complete raw evidence from which a verdict is derived."""
        return seal(MEASUREMENT_SEAL_DOMAIN, self.canonical_payload())


def load_measurement_set(payload: object) -> MeasurementSet:
    """Validate an untrusted payload into a :class:`MeasurementSet`."""
    if not isinstance(payload, dict):
        raise ProtocolViolation(
            "measurement payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise ProtocolViolation(
            "measurement set declares no contract_version",
            detail={"contract": "protocol", "reason": "absent"},
        )
    return validate_contract(
        MeasurementSet, payload, error=ProtocolViolation, context="measurement set"
    )


class MetricVerdict(StrictModel):
    """The scored outcome for one metric."""

    metric: Identifier
    family: MetricFamily
    direction: MetricDirection
    threshold: FiniteFloat
    aggregate: FiniteFloat
    seed_count: int = Field(ge=1)
    required: bool
    passed: bool


class Verdict(StrictModel):
    """A deterministic continue/kill decision over sealed measurements."""

    contract_version: str = Field(min_length=5, max_length=20)
    decision: ContinueKill
    protocol_id: Identifier
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    split: Split
    purpose: HoldoutPurpose
    metrics: tuple[MetricVerdict, ...] = Field(min_length=1)
    failing_metrics: tuple[Identifier, ...] = Field(default=())
    verdict_seal: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _decision_reproduces_from_metrics(self) -> Verdict:
        check_contract_version(self.contract_version, SUPPORTED_PROTOCOL_VERSIONS, "verdict")
        names = [metric.metric for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("verdict metric names must be unique")
        for metric in self.metrics:
            expected = (
                metric.aggregate >= metric.threshold
                if metric.direction is MetricDirection.HIGHER_IS_BETTER
                else metric.aggregate <= metric.threshold
            )
            if metric.passed is not expected:
                raise ValueError(f"metric {metric.metric!r} carries an inconsistent passed flag")
        failing = tuple(
            sorted(
                metric.metric for metric in self.metrics if metric.required and not metric.passed
            )
        )
        if self.failing_metrics != failing:
            raise ValueError("failing_metrics does not reproduce from the required metric results")
        expected_decision = ContinueKill.KILL if failing else ContinueKill.CONTINUE
        if self.decision is not expected_decision:
            raise ValueError("decision does not reproduce from failing_metrics")
        return self


def load_verdict(payload: object) -> Verdict:
    """Validate an untrusted verdict, including its declared contract version."""
    if not isinstance(payload, dict):
        raise ProtocolViolation(
            "verdict payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise ProtocolViolation(
            "verdict declares no contract_version",
            detail={"contract": "verdict", "reason": "absent"},
        )
    declared = payload["contract_version"]
    if not isinstance(declared, str):
        raise ProtocolViolation(
            "verdict contract_version must be a string",
            detail={"received_type": type(declared).__name__},
        )
    check_contract_version(declared, SUPPORTED_PROTOCOL_VERSIONS, "verdict")
    return validate_contract(Verdict, payload, error=ProtocolViolation, context="verdict")


def _verdict_body(verdict: Verdict) -> dict[str, object]:
    """The sealed content of a verdict: everything except the seal itself."""
    payload = verdict.canonical_payload()
    payload.pop("verdict_seal", None)
    return payload


def recompute_verdict_seal(verdict: Verdict) -> str:
    """Recompute a verdict's seal from its own contents.

    This detects an edit after sealing. It does not prove that the carried
    metrics came from real measurements; the authority boundary establishes
    that separately by re-scoring a sealed :class:`MeasurementSet`.
    """
    return seal(VERDICT_SEAL_DOMAIN, _verdict_body(verdict))


class _ExclusiveLock:
    """A cross-process advisory lock built on ``O_CREAT | O_EXCL``.

    Portable to Windows and POSIX with the standard library alone. A timeout
    refuses rather than proceeding: deciding "unused" from a read another
    process is concurrently invalidating is exactly the lost update this guards.
    """

    def __init__(self, path: Path, timeout: float = HOLDOUT_LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self._held = False

    def __enter__(self) -> _ExclusiveLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError) as exc:
                # PermissionError is the Windows form of contention: while the
                # holder is unlinking the lock, the file is delete-pending and
                # an exclusive create fails with ACCESS_DENIED rather than
                # EEXIST. Treating that as a hard failure made this lock flaky
                # under real concurrency; both mean "someone else has it".
                if time.monotonic() >= deadline:
                    raise ProtocolViolation(
                        "could not acquire the holdout ledger lock",
                        detail={
                            "lock": str(self.path),
                            "timeout_seconds": self.timeout,
                            "last_cause": type(exc).__name__,
                        },
                    ) from exc
                time.sleep(0.005)
                continue
            except OSError as exc:
                raise ProtocolViolation(
                    "could not create the holdout ledger lock",
                    detail={"lock": str(self.path), "cause": exc.strerror},
                ) from exc
            os.close(descriptor)
            self._held = True
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


class HoldoutConsumption(StrictModel):
    """One durable receipt binding a holdout spend to all scored evidence."""

    consumed_at: Timestamp
    protocol_id: Identifier
    protocol_seal: Annotated[str, Field(min_length=1, max_length=200)]
    epoch: Identifier
    verdict_seal: Annotated[str, Field(min_length=1, max_length=200)]
    measurement_set_seal: Annotated[str, Field(min_length=1, max_length=200)]


class HoldoutLedger:
    """Durable, cross-process record of which holdout corpora have been spent.

    Backed by a file because an in-memory guard forgets at process exit, and a
    guard that forgets is not a guard. Entries are keyed by **corpus seal**, so
    a protocol revision that reuses the same holdout cannot re-arm it.

    Read, decide and write all happen while holding an exclusive lock, so:

    * two processes racing on the same corpus produce exactly one consumption;
    * two processes spending different corpora both land — neither read-modify
      -write clobbers the other.

    Writes are atomic: a temporary file in the same directory, flushed,
    ``fsync``ed and replaced, so an interrupted write never leaves a
    half-written ledger that reads as "unused".
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolation(
                "holdout ledger is unreadable; refusing to treat the holdout as unused",
                detail={"path": str(self.path), "cause": type(exc).__name__},
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ProtocolViolation(
                "holdout ledger is malformed; refusing to treat the holdout as unused",
                detail={"path": str(self.path)},
            )
        validated: dict[str, dict[str, str]] = {}
        for corpus_seal, value in raw.items():
            receipt = validate_contract(
                HoldoutConsumption,
                value,
                error=ProtocolViolation,
                context=f"holdout consumption for {corpus_seal}",
            )
            validated[corpus_seal] = cast(dict[str, str], receipt.canonical_payload())
        return validated

    def _write_unlocked(self, entries: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".holdout-", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_bytes(entries).decode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def consumption(self, corpus_seal: str) -> dict[str, str] | None:
        """The recorded consumption of ``corpus_seal``, if any."""
        with _ExclusiveLock(self.lock_path):
            return self._read_unlocked().get(corpus_seal)

    def consume(self, corpus_seal: str, *, consumed_at: str, **provenance: str) -> None:
        """Spend a holdout corpus, refusing a second spend of the same corpus.

        The whole read-decide-write runs under the lock, which is what makes the
        check meaningful under concurrency: without it, two processes both read
        "unused", both decide to proceed, and the second write silently
        overwrites the first.
        """
        with _ExclusiveLock(self.lock_path):
            entries = self._read_unlocked()
            previous = entries.get(corpus_seal)
            if previous is not None:
                raise ProtocolViolation(
                    "this holdout corpus has already been consumed",
                    detail={
                        "corpus_seal": corpus_seal,
                        "previous": previous,
                        "remedy": "a new final verdict requires a new holdout corpus",
                    },
                )
            receipt = validate_contract(
                HoldoutConsumption,
                {"consumed_at": consumed_at, **provenance},
                error=ProtocolViolation,
                context="holdout consumption",
            )
            entries[corpus_seal] = cast(dict[str, str], receipt.canonical_payload())
            self._write_unlocked(entries)

    def require_consumption(
        self,
        corpus_seal: str,
        *,
        protocol_seal: str,
        verdict_seal: str,
        measurement_set_seal: str,
        epoch: str,
    ) -> dict[str, str]:
        """Require a receipt that exactly binds this final-holdout evidence."""
        with _ExclusiveLock(self.lock_path):
            receipt = self._read_unlocked().get(corpus_seal)
        if receipt is None:
            raise ProtocolViolation(
                "no durable consumption receipt exists for this holdout corpus",
                detail={"corpus_seal": corpus_seal},
            )
        expected = {
            "protocol_seal": protocol_seal,
            "verdict_seal": verdict_seal,
            "measurement_set_seal": measurement_set_seal,
            "epoch": epoch,
        }
        mismatches = {
            field: {"expected": value, "received": receipt.get(field)}
            for field, value in expected.items()
            if receipt.get(field) != value
        }
        if mismatches:
            raise ProtocolViolation(
                "the holdout consumption receipt does not match the supplied evidence",
                detail={"corpus_seal": corpus_seal, "mismatches": mismatches},
            )
        return receipt


def score_measurements(protocol: Preregistration, measurements: MeasurementSet) -> Verdict:
    """Purely reproduce the verdict implied by a protocol and raw measurements.

    This function does not spend a holdout. It exists so the authority boundary
    can independently reproduce evidence that was already evaluated and spent.
    New final-holdout evaluations must use :func:`evaluate`.

    Refuses, rather than degrading, when:

    * the measurement seal does not match the protocol's current seal — this is
      what stops a threshold edited after seeing the holdout from being scored
      against data collected under the older protocol;
    * the measurement corpus seal does not match the pre-registered corpus for
      that split;
    * the epoch does not match;
    * the holdout is touched for anything but a final verdict;
    * any metric present is missing a value for any declared seed.
    """
    protocol = load_preregistration(protocol.canonical_payload())
    measurements = load_measurement_set(measurements.canonical_payload())
    current_seal = protocol.protocol_seal()
    if measurements.protocol_seal != current_seal:
        raise ProtocolViolation(
            "measurements were collected under a different protocol seal",
            detail={
                "expected": current_seal,
                "received": measurements.protocol_seal,
                "meaning": "the protocol changed after these measurements were taken",
            },
        )
    if measurements.epoch != protocol.epoch:
        raise ProtocolViolation(
            "measurements belong to a different epoch",
            detail={"expected": protocol.epoch, "received": measurements.epoch},
        )
    expected_corpus = protocol.split_spec(measurements.split).corpus_seal
    if measurements.corpus_seal != expected_corpus:
        raise ProtocolViolation(
            "measurements do not come from the pre-registered corpus for this split",
            detail={
                "split": measurements.split.value,
                "expected": expected_corpus,
                "received": measurements.corpus_seal,
            },
        )

    if measurements.split is Split.HOLDOUT:
        if measurements.purpose is not HoldoutPurpose.FINAL_VERDICT:
            raise ProtocolViolation(
                "the holdout may only be used for a final verdict",
                detail={
                    "purpose": measurements.purpose.value,
                    "allowed": HoldoutPurpose.FINAL_VERDICT.value,
                },
            )
    elif measurements.purpose is HoldoutPurpose.FINAL_VERDICT:
        raise ProtocolViolation(
            "a final verdict is only meaningful on the holdout",
            detail={"split": measurements.split.value},
        )

    by_metric: dict[str, dict[int, float]] = {}
    for measurement in measurements.measurements:
        seeds = by_metric.setdefault(measurement.metric, {})
        if measurement.seed in seeds:
            raise ProtocolViolation(
                "duplicate measurement for a metric and seed",
                detail={"metric": measurement.metric, "seed": measurement.seed},
            )
        seeds[measurement.seed] = measurement.value

    unknown = sorted(set(by_metric) - {metric.name for metric in protocol.metrics})
    if unknown:
        raise ProtocolViolation(
            "measurements name metrics that were not pre-registered",
            detail={"unknown_metrics": unknown},
        )

    declared_seeds = set(protocol.seeds)
    scored: list[MetricVerdict] = []
    for metric in sorted(protocol.metrics, key=lambda spec: spec.name):
        values = by_metric.get(metric.name)
        if values is None:
            if metric.required:
                raise ProtocolViolation(
                    "required metric has no measurements",
                    detail={"metric": metric.name, "split": measurements.split.value},
                )
            continue
        # Every metric that is present must be complete, required or not. A
        # partial optional metric is a metric scored on a chosen subset of
        # seeds, which is the cherry-picking this protocol exists to prevent.
        if set(values) != declared_seeds:
            raise ProtocolViolation(
                "metric is missing measurements for pre-registered seeds",
                detail={
                    "metric": metric.name,
                    "required": metric.required,
                    "expected_seeds": sorted(declared_seeds),
                    "received_seeds": sorted(values),
                },
            )
        ordered = [values[seed] for seed in sorted(values)]
        aggregate = statistics.median(ordered)
        scored.append(
            MetricVerdict(
                metric=metric.name,
                family=metric.family,
                direction=metric.direction,
                threshold=metric.threshold,
                aggregate=aggregate,
                seed_count=len(ordered),
                required=metric.required,
                passed=metric.passes(aggregate),
            )
        )

    failing = tuple(item.metric for item in scored if item.required and not item.passed)
    decision = ContinueKill.KILL if failing else ContinueKill.CONTINUE

    verdict = Verdict(
        contract_version=PROTOCOL_CONTRACT_VERSION,
        decision=decision,
        protocol_id=protocol.protocol_id,
        protocol_seal=current_seal,
        corpus_seal=measurements.corpus_seal,
        epoch=protocol.epoch,
        split=measurements.split,
        purpose=measurements.purpose,
        metrics=tuple(scored),
        failing_metrics=failing,
        verdict_seal="sha256:" + "0" * 64,
    )
    verdict = verdict.model_copy(update={"verdict_seal": recompute_verdict_seal(verdict)})
    return load_verdict(verdict.canonical_payload())


def evaluate(
    protocol: Preregistration,
    measurements: MeasurementSet,
    *,
    holdout_ledger: HoldoutLedger | None = None,
    consumed_at: str | None = None,
) -> Verdict:
    """Score measurements and atomically record any final-holdout consumption."""
    verdict = score_measurements(protocol, measurements)

    if verdict.split is Split.HOLDOUT and (holdout_ledger is None or consumed_at is None):
        raise ProtocolViolation(
            "a final holdout verdict requires a durable holdout ledger",
            detail={"corpus_seal": verdict.corpus_seal},
        )

    if holdout_ledger is not None and consumed_at is not None and verdict.split is Split.HOLDOUT:
        # Recorded only once a verdict actually exists, so a refused evaluation
        # never spends the holdout.
        holdout_ledger.consume(
            verdict.corpus_seal,
            consumed_at=consumed_at,
            protocol_id=verdict.protocol_id,
            protocol_seal=verdict.protocol_seal,
            epoch=verdict.epoch,
            verdict_seal=verdict.verdict_seal,
            measurement_set_seal=measurements.measurement_seal(),
        )
    return verdict

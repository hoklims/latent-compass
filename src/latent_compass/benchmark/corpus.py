"""HOK-188 — the benchmark corpus contract.

A benchmark case wraps one HOK-185 episode and adds exactly the metadata the
bench needs: a stable case identity, the split it belongs to, its distribution
group, whether the decision was ambiguous, and the cutoff at which its outcome
was frozen. Nothing else. The episode contract is not widened.

Two projections, and the gap between them is the point
------------------------------------------------------
:class:`PublicCaseView` is what a baseline sees: the state it was in and the
candidates it could pick from. It carries **no** historically selected
direction, **no** outcome, **no** economics, **no** external verdict and **no**
scorer label. A baseline physically cannot read them, because the projection
does not contain them — a type boundary, not a convention. The private scorer
joins predictions to logged feedback afterwards.

Candidate propensities *are* public. They describe the logging policy, not its
realised draw, and the fourth pre-registered baseline is defined in terms of
them. See :mod:`latent_compass.benchmark.ope` for the support caveat that comes
with a near-deterministic logging policy.

Seals are recomputed, never believed
------------------------------------
A split's ``corpus_seal`` is computed from the canonical payloads of the cases
actually present in the file, in canonical case order. A manifest that merely
*declares* a seal proves nothing: :func:`verify_manifest` recomputes every seal
from the real files and refuses on any mismatch. Case identity and episode
identity must both be disjoint across the three splits, and disjointness is
*checked* here rather than declared — the gap ``docs/evaluation-protocol.md``
names as an honest limit of the protocol layer.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Final

from pydantic import Field, model_validator

from latent_compass.canonical import seal
from latent_compass.contracts import (
    BENCHMARK_CONTRACT_VERSION,
    SUPPORTED_BENCHMARK_VERSIONS,
    Identifier,
    StrictModel,
    Timestamp,
    UnitInterval,
    check_contract_version,
    validate_contract,
)
from latent_compass.episode import Episode, HierarchicalState
from latent_compass.errors import BenchmarkViolation
from latent_compass.protocol import Preregistration, Split

__all__ = [
    "CORPUS_SEAL_DOMAIN",
    "NOMINAL_GROUP",
    "BenchmarkCase",
    "CaseFile",
    "CorpusManifest",
    "CorpusProvenance",
    "DistributionKind",
    "PublicCandidateView",
    "PublicCaseView",
    "SplitManifestEntry",
    "build_manifest",
    "load_case_file",
    "load_corpus_manifest",
    "read_split_file",
    "require_manifest_matches_protocol",
    "split_corpus_seal",
    "verify_manifest",
]

CORPUS_SEAL_DOMAIN: Final = "benchmark.corpus"

#: The one distribution group name reserved for the nominal population. Every
#: other group is a shift group, which is what makes the ``DRIFT`` family
#: computable without a second declaration that could disagree with this one.
NOMINAL_GROUP: Final = "nominal"

Propensity = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]


class DistributionKind(StrEnum):
    """Whether a case belongs to the nominal population or to a shift group."""

    NOMINAL = "NOMINAL"
    SHIFT = "SHIFT"


class PublicCandidateView(StrictModel):
    """One candidate as a baseline sees it."""

    direction_id: Identifier
    propensity: Propensity
    prior_uncertainty: UnitInterval


class PublicCaseView(StrictModel):
    """Everything a baseline is allowed to see, and nothing else.

    The field set is deliberately closed and small. ``extra="forbid"`` plus
    ``frozen=True`` mean a harness bug cannot smuggle a label in at runtime, and
    :meth:`BenchmarkCase.public_view` is the only constructor the runner uses.
    """

    case_id: Identifier
    state: HierarchicalState
    candidates: tuple[PublicCandidateView, ...] = Field(min_length=1, max_length=256)

    def canonical_direction_ids(self) -> tuple[str, ...]:
        """Candidate ids in canonical (sorted) order — the universal tie-break."""
        return tuple(sorted(candidate.direction_id for candidate in self.candidates))


class BenchmarkCase(StrictModel):
    """One episode, promoted to a benchmark case."""

    contract_version: str = Field(min_length=5, max_length=20)
    case_id: Identifier
    split: Split
    distribution_kind: DistributionKind
    distribution_group: Identifier
    ambiguous: bool
    observation_cutoff: Timestamp
    observation_horizon_seconds: int = Field(ge=0, le=31_536_000)
    episode: Episode

    @model_validator(mode="after")
    def _case_invariants(self) -> BenchmarkCase:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")

        if self.distribution_kind is DistributionKind.NOMINAL:
            if self.distribution_group != NOMINAL_GROUP:
                raise ValueError(f"a NOMINAL case must sit in the {NOMINAL_GROUP!r} group")
        elif self.distribution_group == NOMINAL_GROUP:
            raise ValueError(f"a SHIFT case must not sit in the {NOMINAL_GROUP!r} group")

        # Off-policy evaluation is only defined against a logged action drawn
        # from a known distribution. A case with no logged direction carries no
        # feedback to reweight, so it is refused at load rather than silently
        # contributing a zero. Outcome *maturity* is a different matter: it is
        # classified and reported, never used to drop a case (see ope.py).
        if self.episode.decision.selected_direction_id is None:
            raise ValueError(
                "a benchmark case must carry a logged direction; "
                f"episode {self.episode.episode_id!r} records "
                f"{self.episode.decision.advisory.kind.value} instead"
            )

        # The cutoff is the instant this case's outcome was frozen. A cutoff
        # before the decision was recorded would make maturity unanswerable
        # rather than merely false.
        if self.observation_cutoff < self.episode.provenance.recorded_at:
            raise ValueError("observation cutoff precedes the instant the episode was recorded")
        return self

    def public_view(self) -> PublicCaseView:
        """Project the case down to what a baseline may see.

        Built field by field from the three permitted sources. It is not a
        filtered copy of the episode: a future episode field is absent here
        until someone adds it deliberately, rather than leaking by default.
        """
        return PublicCaseView(
            case_id=self.case_id,
            state=self.episode.state,
            candidates=tuple(
                PublicCandidateView(
                    direction_id=candidate.direction_id,
                    propensity=candidate.propensity,
                    prior_uncertainty=candidate.prior_uncertainty,
                )
                for candidate in self.episode.candidates
            ),
        )

    def logged_direction_id(self) -> str:
        """The direction the logging policy actually took."""
        selected = self.episode.decision.selected_direction_id
        if selected is None:  # pragma: no cover - forbidden by the validator
            raise BenchmarkViolation(
                "benchmark case carries no logged direction", detail={"case_id": self.case_id}
            )
        return selected

    def logged_propensity(self, direction_id: str) -> float:
        """The logging policy's probability of ``direction_id`` for this case."""
        for candidate in self.episode.candidates:
            if candidate.direction_id == direction_id:
                return candidate.propensity
        raise BenchmarkViolation(
            "direction is not a candidate of this case",
            detail={"case_id": self.case_id, "direction_id": direction_id},
        )

    def outcome_is_mature(self) -> bool:
        """Whether the outcome was observed, and observed by the cutoff.

        Timestamps are fixed-width UTC with a trailing ``Z``, so lexicographic
        order *is* chronological order and no parsing is needed to compare them.
        """
        outcome = self.episode.outcome
        if outcome is None or not outcome.observed or outcome.observed_at is None:
            return False
        return outcome.observed_at <= self.observation_cutoff


class CaseFile(StrictModel):
    """The on-disk shape of exactly one split.

    One split per file, physically. A single file holding several splits would
    make "the holdout was never opened" unprovable, because opening the
    validation data would have opened the holdout data too.
    """

    contract_version: str = Field(min_length=5, max_length=20)
    split: Split
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _cases_belong_to_the_declared_split(self) -> CaseFile:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case ids must be unique within a split")
        episode_ids = [case.episode.episode_id for case in self.cases]
        if len(set(episode_ids)) != len(episode_ids):
            raise ValueError("episode ids must be unique within a split")
        for case in self.cases:
            if case.split is not self.split:
                raise ValueError(
                    f"a {case.split.value} case appears in the {self.split.value} file"
                )
        return self

    def ordered_cases(self) -> tuple[BenchmarkCase, ...]:
        """Cases in canonical order, so file order never reaches a result."""
        return tuple(sorted(self.cases, key=lambda case: case.case_id))


def load_case_file(payload: object) -> CaseFile:
    """Validate an untrusted payload into a :class:`CaseFile`."""
    if not isinstance(payload, dict):
        raise BenchmarkViolation(
            "corpus split payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise BenchmarkViolation(
            "corpus split declares no contract_version",
            detail={"contract": "benchmark", "reason": "absent"},
        )
    return validate_contract(
        CaseFile, payload, error=BenchmarkViolation, context="benchmark corpus split"
    )


def split_corpus_seal(cases: tuple[BenchmarkCase, ...]) -> str:
    """Seal the cases of one split, from the cases themselves.

    Canonical case order is used, so permuting the file leaves the seal
    unchanged while editing any byte of any case moves it.
    """
    ordered = sorted(cases, key=lambda case: case.case_id)
    return seal(CORPUS_SEAL_DOMAIN, [case.canonical_payload() for case in ordered])


class CorpusProvenance(StrictModel):
    """Where the corpus came from, and what it may be used to claim."""

    origin: Annotated[str, Field(min_length=1, max_length=300)]
    licence: Annotated[str, Field(min_length=1, max_length=120)]
    synthetic: bool
    description: Annotated[str, Field(min_length=1, max_length=1000)]


def _resolve_under(corpus_dir: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` and refuse it unless it lands under the corpus."""
    root = corpus_dir.resolve()
    target = (root / relative_path).resolve()
    if target == root or root not in target.parents:
        raise BenchmarkViolation(
            "a corpus split file must resolve strictly inside the corpus directory",
            detail={"corpus_dir": str(root), "requested": str(target)},
        )
    return target


class SplitManifestEntry(StrictModel):
    """One split: where it lives, how big it is, and what it seals to."""

    split: Split
    path: Annotated[str, Field(min_length=1, max_length=200)]
    case_count: int = Field(ge=1)
    corpus_seal: Annotated[str, Field(min_length=1, max_length=200)]
    ambiguous_case_count: int = Field(ge=0)
    distribution_groups: tuple[Identifier, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _path_is_a_confined_relative_name(self) -> SplitManifestEntry:
        pure = PurePosixPath(self.path)
        if pure.is_absolute() or Path(self.path).is_absolute() or ".." in pure.parts:
            raise ValueError("a split path must be relative and free of traversal")
        if "\\" in self.path or ":" in self.path:
            raise ValueError("a split path must be a plain POSIX relative path")
        if self.ambiguous_case_count > self.case_count:
            raise ValueError("more ambiguous cases than cases")
        groups = list(self.distribution_groups)
        if groups != sorted(groups) or len(set(groups)) != len(groups):
            raise ValueError("distribution groups must be unique and sorted")
        return self

    def resolve(self, corpus_dir: Path) -> Path:
        """Resolve this split's file strictly under ``corpus_dir``."""
        return _resolve_under(corpus_dir, self.path)


class CorpusManifest(StrictModel):
    """The sealed identity of a three-split benchmark corpus."""

    contract_version: str = Field(min_length=5, max_length=20)
    corpus_id: Identifier
    corpus_version: Identifier
    provenance: CorpusProvenance
    splits: tuple[SplitManifestEntry, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def _manifest_invariants(self) -> CorpusManifest:
        check_contract_version(self.contract_version, SUPPORTED_BENCHMARK_VERSIONS, "benchmark")
        declared = [entry.split for entry in self.splits]
        if sorted(declared) != sorted(Split):
            raise ValueError("exactly one TRAIN, one VALIDATION and one HOLDOUT split are required")
        paths = [entry.path for entry in self.splits]
        if len(set(paths)) != len(paths):
            raise ValueError("each split must live in its own file")
        seals = [entry.corpus_seal for entry in self.splits]
        if len(set(seals)) != len(seals):
            raise ValueError("each split must have a distinct corpus seal")
        return self

    def entry(self, split: Split) -> SplitManifestEntry:
        for candidate in self.splits:
            if candidate.split is split:
                return candidate
        raise BenchmarkViolation(  # pragma: no cover - forbidden by the validator
            "split is not declared in the manifest", detail={"split": split.value}
        )


def load_corpus_manifest(payload: object) -> CorpusManifest:
    """Validate an untrusted payload into a :class:`CorpusManifest`."""
    if not isinstance(payload, dict):
        raise BenchmarkViolation(
            "corpus manifest payload must be a JSON object",
            detail={"received_type": type(payload).__name__},
        )
    if "contract_version" not in payload:
        raise BenchmarkViolation(
            "corpus manifest declares no contract_version",
            detail={"contract": "benchmark", "reason": "absent"},
        )
    return validate_contract(
        CorpusManifest, payload, error=BenchmarkViolation, context="benchmark corpus manifest"
    )


def read_split_file(path: Path, *, expected_split: Split) -> CaseFile:
    """Read and validate one split file, refusing a split it does not hold."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkViolation(
            f"corpus split file {path} is not valid UTF-8",
            detail={"path": str(path), "reason": exc.reason},
        ) from exc
    except OSError as exc:
        raise BenchmarkViolation(
            f"cannot read corpus split file {path}",
            detail={"path": str(path), "cause": exc.strerror},
        ) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkViolation(
            f"corpus split file {path} is not valid JSON",
            detail={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc

    case_file = load_case_file(payload)
    if case_file.split is not expected_split:
        raise BenchmarkViolation(
            "corpus split file declares a different split",
            detail={
                "path": str(path),
                "expected": expected_split.value,
                "declared": case_file.split.value,
            },
        )
    return case_file


def _entry_from_cases(
    split: Split, relative_path: str, cases: tuple[BenchmarkCase, ...]
) -> SplitManifestEntry:
    return SplitManifestEntry(
        split=split,
        path=relative_path,
        case_count=len(cases),
        corpus_seal=split_corpus_seal(cases),
        ambiguous_case_count=sum(1 for case in cases if case.ambiguous),
        distribution_groups=tuple(sorted({case.distribution_group for case in cases})),
    )


def _check_cross_split_disjointness(loaded: dict[Split, CaseFile]) -> None:
    """Refuse any case or episode identity shared between two splits.

    Declared disjointness is worth nothing here: the whole reason a holdout is
    spendable is that it is genuinely unseen, and one shared case makes that
    false while every declaration still reads as true.
    """
    fields: tuple[tuple[str, str], ...] = (("case_id", "case"), ("episode_id", "episode"))
    for field, kind in fields:
        seen: dict[str, Split] = {}
        collisions: list[dict[str, str]] = []
        for split in sorted(loaded):
            for case in loaded[split].ordered_cases():
                identity = case.case_id if kind == "case" else case.episode.episode_id
                previous = seen.get(identity)
                if previous is None:
                    seen[identity] = split
                else:
                    collisions.append(
                        {field: identity, "first": previous.value, "second": split.value}
                    )
        if collisions:
            raise BenchmarkViolation(
                f"splits share a {field}; they are not disjoint",
                detail={"field": field, "collisions": collisions},
            )


def _require_benchmarkable_validation_split(cases: tuple[BenchmarkCase, ...]) -> None:
    """The validation split must exercise what the benchmark claims to measure.

    A corpus with no ambiguous decision and no shift group would still run, and
    would still produce an eight-family vector — with ``DRIFT`` measured over an
    empty comparison. That is a number with no content, so it is refused here
    rather than reported.
    """
    if not any(case.ambiguous for case in cases):
        raise BenchmarkViolation(
            "the validation split contains no ambiguous case",
            detail={"split": Split.VALIDATION.value, "required_ambiguous_cases": 1},
        )
    shift = {
        case.distribution_group
        for case in cases
        if case.distribution_kind is DistributionKind.SHIFT
    }
    if not shift:
        raise BenchmarkViolation(
            "the validation split contains no shift group",
            detail={"split": Split.VALIDATION.value, "required_shift_groups": 1},
        )
    if not any(case.distribution_kind is DistributionKind.NOMINAL for case in cases):
        raise BenchmarkViolation(
            "the validation split contains no nominal case to compare shifts against",
            detail={"split": Split.VALIDATION.value},
        )


def build_manifest(
    corpus_dir: Path,
    *,
    corpus_id: str,
    corpus_version: str,
    provenance: CorpusProvenance,
    relative_paths: dict[Split, str],
) -> CorpusManifest:
    """Build a manifest by reading the three real split files.

    Every seal, count and group list is derived from the cases on disk. Nothing
    a caller asserts about the corpus enters the manifest.

    This is the corpus-*authoring* path, and it does read the holdout file — the
    holdout has to be sealed by someone. The HOK-188 runner is a different entry
    point and never calls this.
    """
    if set(relative_paths) != set(Split):
        raise BenchmarkViolation(
            "a manifest must name a file for each of the three splits",
            detail={"supplied": sorted(split.value for split in relative_paths)},
        )

    loaded = {
        split: read_split_file(
            _resolve_under(corpus_dir, relative_paths[split]), expected_split=split
        )
        for split in sorted(Split)
    }
    _check_cross_split_disjointness(loaded)
    _require_benchmarkable_validation_split(loaded[Split.VALIDATION].ordered_cases())

    return CorpusManifest(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        corpus_id=corpus_id,
        corpus_version=corpus_version,
        provenance=provenance,
        splits=tuple(
            _entry_from_cases(split, relative_paths[split], loaded[split].ordered_cases())
            for split in sorted(Split)
        ),
    )


def verify_manifest(corpus_dir: Path, manifest: CorpusManifest) -> dict[str, object]:
    """Recompute every seal from the real files and refuse any mismatch.

    A declared seal is never accepted. This reads all three splits, including
    the holdout, because verifying a corpus is precisely the act of looking at
    it. The runner never calls this.
    """
    loaded = {
        split: read_split_file(manifest.entry(split).resolve(corpus_dir), expected_split=split)
        for split in sorted(Split)
    }
    _check_cross_split_disjointness(loaded)

    mismatches: list[dict[str, object]] = []
    for split in sorted(Split):
        declared = manifest.entry(split)
        recomputed = _entry_from_cases(split, declared.path, loaded[split].ordered_cases())
        differences: dict[str, object] = {
            field: {"declared": getattr(declared, field), "recomputed": getattr(recomputed, field)}
            for field in ("case_count", "corpus_seal", "ambiguous_case_count")
            if getattr(declared, field) != getattr(recomputed, field)
        }
        if tuple(declared.distribution_groups) != tuple(recomputed.distribution_groups):
            differences["distribution_groups"] = {
                "declared": list(declared.distribution_groups),
                "recomputed": list(recomputed.distribution_groups),
            }
        if differences:
            mismatches.append({"split": split.value, "differences": differences})

    if mismatches:
        raise BenchmarkViolation(
            "the manifest does not reproduce from the corpus files",
            detail={"mismatches": mismatches},
        )

    _require_benchmarkable_validation_split(loaded[Split.VALIDATION].ordered_cases())
    return {
        "verified": True,
        "corpus_id": manifest.corpus_id,
        "corpus_version": manifest.corpus_version,
        "splits": [
            {
                "split": split.value,
                "case_count": manifest.entry(split).case_count,
                "corpus_seal": manifest.entry(split).corpus_seal,
            }
            for split in sorted(Split)
        ],
    }


def require_manifest_matches_protocol(manifest: CorpusManifest, protocol: Preregistration) -> None:
    """Bind the corpus to the pre-registration that fixed it.

    A corpus whose seals do not match the ``SplitSpec`` seals is a different
    corpus from the one that was pre-registered, whatever its manifest says.
    """
    mismatches: list[dict[str, object]] = []
    for split in sorted(Split):
        entry = manifest.entry(split)
        spec = protocol.split_spec(split)
        if entry.corpus_seal != spec.corpus_seal:
            mismatches.append(
                {
                    "split": split.value,
                    "protocol_corpus_seal": spec.corpus_seal,
                    "manifest_corpus_seal": entry.corpus_seal,
                }
            )
        elif entry.case_count != spec.item_count:
            mismatches.append(
                {
                    "split": split.value,
                    "protocol_item_count": spec.item_count,
                    "manifest_case_count": entry.case_count,
                }
            )
    if mismatches:
        raise BenchmarkViolation(
            "the corpus manifest does not match the pre-registered splits",
            detail={"mismatches": mismatches},
        )

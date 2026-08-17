"""The committed synthetic corpus is generated, licensed and honestly described.

Committed data rots. These assertions keep ``corpus/synthetic-v1/`` tied to the
generator that produced it, and keep its documentation from quietly growing a
claim the data cannot carry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import corpus_generator
from conftest import BENCHMARK_CORPUS
from latent_compass.benchmark import DistributionKind, load_case_file, load_corpus_manifest
from latent_compass.protocol import Split

COMMITTED_FILES = (
    "train.json",
    "validation.json",
    "holdout.json",
    "manifest.json",
    "protocol.json",
    "spec.json",
    "PROVENANCE.md",
)


def test_every_committed_corpus_file_is_present() -> None:
    for name in COMMITTED_FILES:
        assert (BENCHMARK_CORPUS / name).is_file(), f"{name} is missing from the corpus"


@pytest.mark.parametrize(
    "name",
    [
        "train.json",
        "validation.json",
        "holdout.json",
        "manifest.json",
        "protocol.json",
        "spec.json",
    ],
)
def test_the_committed_corpus_regenerates_byte_for_byte(name: str, tmp_path: Path) -> None:
    """Editing a corpus file by hand breaks this, which is the intent."""
    corpus_generator.write(tmp_path / "regenerated")
    regenerated = (tmp_path / "regenerated" / name).read_bytes()
    committed = (BENCHMARK_CORPUS / name).read_bytes()
    assert regenerated == committed, f"{name} no longer reproduces from tests/corpus_generator.py"


def test_the_three_splits_live_in_three_physically_distinct_files() -> None:
    manifest = load_corpus_manifest(
        json.loads((BENCHMARK_CORPUS / "manifest.json").read_text(encoding="utf-8"))
    )
    paths = {manifest.entry(split).path for split in Split}
    assert len(paths) == 3
    resolved = {manifest.entry(split).resolve(BENCHMARK_CORPUS) for split in Split}
    assert len(resolved) == 3
    for path in resolved:
        assert path.is_file()


def test_the_validation_split_covers_ambiguity_and_distribution_shift() -> None:
    cases = load_case_file(
        json.loads((BENCHMARK_CORPUS / "validation.json").read_text(encoding="utf-8"))
    ).ordered_cases()

    assert sum(1 for case in cases if case.ambiguous) >= 1
    shift_groups = {
        case.distribution_group
        for case in cases
        if case.distribution_kind is DistributionKind.SHIFT
    }
    assert len(shift_groups) >= 1
    assert any(case.distribution_kind is DistributionKind.NOMINAL for case in cases)


def test_the_provenance_declares_the_licence_and_refuses_to_carry_a_claim() -> None:
    text = " ".join(
        (BENCHMARK_CORPUS / "PROVENANCE.md").read_text(encoding="utf-8").split()
    ).lower()
    assert "apache-2.0" in text
    assert "synthetic" in text
    assert "tests/corpus_generator.py" in text
    for admission in (
        "is not evidence about anything",
        "no real agent behaviour",
        "the declared thresholds",
        "known limits",
    ):
        assert admission in text, f"the provenance omits {admission!r}"


def test_the_manifest_provenance_states_the_corpus_is_synthetic() -> None:
    manifest = load_corpus_manifest(
        json.loads((BENCHMARK_CORPUS / "manifest.json").read_text(encoding="utf-8"))
    )
    assert manifest.provenance.synthetic is True
    assert "Apache-2.0" in manifest.provenance.licence
    assert "no claim" in manifest.provenance.description.lower()


def test_the_corpus_names_no_real_host_or_repository() -> None:
    """The demo data must not look like it came from somewhere."""
    for name in ("train.json", "validation.json", "holdout.json"):
        text = (BENCHMARK_CORPUS / name).read_text(encoding="utf-8").lower()
        assert "synthetic-host" in text
        for needle in ("github.com", "http://", "https://", "@"):
            assert needle not in text, f"{name} contains {needle!r}"

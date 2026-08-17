"""HOK-189 — proofs that the repository is legally and operationally open source.

These assertions are about repository state, so each one first proves it is
looking at a real file. A check that silently examines nothing always passes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "LICENSE",
    "NOTICE",
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "GOVERNANCE.md",
    "docs/authority-boundary.md",
    "docs/episode-contract.md",
    "docs/evaluation-protocol.md",
    "docs/benchmark-protocol.md",
    "docs/pairwise-supervision.md",
    "docs/judgeable-projection.md",
    "docs/independent-labeler.md",
    "docs/pairwise-corpus-readiness.md",
    "docs/ledger.md",
    "docs/adr/0001-minimal-durable-stack.md",
    "docs/adr/0002-verified-evidence-and-durable-anchors.md",
    "docs/adr/0003-evidence-provenance-fails-closed.md",
    "docs/adr/0004-independent-pairwise-supervision.md",
    "docs/adr/0005-judgeable-pre-action-sidecar.md",
    "docs/adr/0006-keyless-independent-pairwise-labeler.md",
    "docs/licenses/dependency-audit.md",
    "corpus/synthetic-v1/PROVENANCE.md",
)


def prose(relative: str) -> str:
    """Document text with runs of whitespace collapsed, lowercased.

    Phrases in these documents wrap across lines, so a naive substring search
    reports a missing sentence that is plainly there.
    """
    return " ".join((REPO / relative).read_text(encoding="utf-8").split()).lower()


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_every_required_document_exists_and_is_substantive(relative: str) -> None:
    path = REPO / relative
    assert path.is_file(), f"{relative} is missing"
    assert len(path.read_text(encoding="utf-8").strip()) > 500, f"{relative} is a stub"


def test_the_licence_is_apache_2_0_and_declared_consistently() -> None:
    licence = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence
    assert "Version 2.0, January 2004" in licence

    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["license"] == "Apache-2.0"
    assert set(metadata["project"]["license-files"]) == {"LICENSE", "NOTICE"}


def test_the_notice_names_the_runtime_dependency_and_its_licence() -> None:
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    assert "pydantic" in notice
    assert "MIT" in notice
    assert "Apache License, Version 2.0" in notice


def test_the_declared_runtime_dependency_set_is_exactly_pydantic() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["dependencies"] == ["pydantic>=2,<3"]
    assert set(metadata["dependency-groups"]) == {"dev"}


def test_the_audit_records_the_one_non_permissive_development_licence() -> None:
    """The audit must not round an inconvenient fact away."""
    audit = prose("docs/licenses/dependency-audit.md")
    assert "mpl-2.0" in audit
    assert "pathspec" in audit
    assert "no mpl obligation flows" in audit
    assert "not checked" in audit, "an audit must state its own limits"


def test_the_readme_separates_demonstrated_experimental_and_projected() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for heading in ("### Demonstrated", "### Experimental", "### Projected"):
        assert heading in readme
    assert "no such claim is made" in prose("README.md")


def test_benchmark_docs_do_not_claim_a_seal_proves_registration_time() -> None:
    protocol = prose("docs/benchmark-protocol.md")
    assert "does not prove when the plan was registered" in protocol
    assert "external durable anchor" in protocol


def test_the_readme_documents_install_and_validation() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for command in (
        "uv sync --all-groups",
        "uv run pytest",
        "uv run mypy",
        "uv run ruff check",
        "uv build",
    ):
        assert command in readme


def test_the_readme_carries_no_unearned_badge() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    badges = re.findall(r"!\[[^\]]*\]\([^)]*(?:shields\.io|badge|travis|circleci)[^)]*\)", readme)
    assert badges == [], f"unearned badges present: {badges}"


def test_the_honest_limit_appears_in_every_document_that_relies_on_it() -> None:
    """The hash-chain limit must not be stated once and quietly dropped elsewhere."""
    for relative in ("README.md", "docs/ledger.md", "SECURITY.md", "docs/authority-boundary.md"):
        text = (REPO / relative).read_text(encoding="utf-8").lower()
        assert "authenticity" in text, f"{relative} omits the authenticity limit"


def test_the_security_policy_states_a_reporting_route_and_a_scope() -> None:
    policy = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "github.com/hoklims/latent-compass" in policy
    assert "## What is in scope" in policy
    assert "## What is out of scope" in policy
    assert "no bug bounty" in prose("SECURITY.md")


def test_governance_covers_data_provenance_and_project_decisions() -> None:
    governance = (REPO / "GOVERNANCE.md").read_text(encoding="utf-8")
    for topic in (
        "Provenance",
        "Minimisation",
        "Retention",
        "Deletion",
        "Amending the authority boundary",
    ):
        assert topic in governance


@pytest.mark.parametrize(
    "pattern",
    [
        r"AKIA[0-9A-Z]{16}",  # AWS access key id
        r"gh[pousr]_[A-Za-z0-9]{36,}",  # GitHub token
        r"sk-[A-Za-z0-9]{32,}",  # generic API secret key
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",  # private key block
        r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
    ],
)
def test_no_secret_material_is_committed(pattern: str) -> None:
    scanned = 0
    compiled = re.compile(pattern)
    for path in sorted(REPO.rglob("*")):
        parts = set(path.parts)
        if not path.is_file() or {".git", ".venv", ".omx", "dist", "graphify-out"} & parts:
            continue
        if path.suffix not in {
            ".py",
            ".md",
            ".toml",
            ".json",
            ".lock",
            ".cfg",
            ".yaml",
            ".yml",
            "",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        assert not compiled.search(text), f"{path.name} matches {pattern}"
    assert scanned > 20, f"the secret scan only examined {scanned} files"


def test_no_private_host_path_leaks_into_the_repository() -> None:
    # Needles are assembled at runtime so that this module does not contain the
    # literals it forbids and therefore match itself.
    needles = ("c:" + "\\users\\", "c:" + "/users/", "mainten" + "ence", "appdata" + "\\local")
    scanned = 0
    for path in sorted(REPO.rglob("*.py")) + sorted(REPO.rglob("*.md")):
        if {".git", ".venv", ".omx", "dist"} & set(path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        scanned += 1
        for needle in needles:
            assert needle not in text, f"{path.name} leaks {needle!r}"
    assert scanned > 15, f"the path scan only examined {scanned} files"


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "docs/authority-boundary.md",
        "GOVERNANCE.md",
        "SECURITY.md",
        "docs/benchmark-protocol.md",
    ],
)
def test_no_document_claims_an_emission_this_foundation_does_not_perform(
    relative: str,
) -> None:
    """HOK-182 does not exist; nothing may describe this as if it did.

    HOK-188 runs baseline policies *offline*, over recorded cases. That is not
    an emission, and no document may blur the two.
    """
    text = prose(relative)
    for claim in (
        "probabilistic, vectorised",
        "probabilistic, vectorized",
        "emits probabilistic",
        "ranks candidates",
        "calibrated opinions",
    ):
        assert claim not in text, f"{relative} claims {claim!r}"


def test_the_readme_describes_this_foundation_as_a_validator_and_recorder() -> None:
    readme = prose("README.md")
    assert "validates and records" in readme
    assert "hok-182" in readme
    assert "emits no operational advisory" in readme


def test_the_benchmark_documents_separate_a_working_pipeline_from_a_result() -> None:
    """HOK-188 runs. Nothing it produced on synthetic data is evidence of value."""
    benchmark = prose("docs/benchmark-protocol.md")
    assert "small and synthetic" in benchmark
    assert "trains nothing" in benchmark
    assert "emits no operational advisory" in benchmark
    assert "known limits" in benchmark

    provenance = prose("corpus/synthetic-v1/PROVENANCE.md")
    assert "is not evidence about anything" in provenance
    assert "apache-2.0" in provenance

    readme = prose("README.md")
    assert "offline" in readme
    assert "never reach a live agent" in readme


def test_no_document_presents_the_benchmark_as_an_authorisation() -> None:
    """A SELECTION verdict on VALIDATION is not a final verdict."""
    benchmark = prose("docs/benchmark-protocol.md")
    assert "is not an authorisation" in benchmark
    assert "hok-190" in benchmark


def test_pairwise_supervision_separates_labels_calibration_and_authority() -> None:
    contract = prose("docs/pairwise-supervision.md")
    adr = prose("docs/adr/0004-independent-pairwise-supervision.md")

    for claim in (
        "a judge preference is not an observed outcome",
        "uncalibrated preference score",
        "calibration must compare predictions with events",
        "holdout remains a one-shot final gate",
        "creates no authority",
    ):
        assert claim in contract

    assert "zero eligible explicit pairwise judgments" in contract
    assert "discovery_required" in contract
    assert "no universal row-count threshold" in adr
    assert "no ranker implementation" in adr


def test_pairwise_corpus_readiness_refuses_invented_supervision() -> None:
    readiness = prose("docs/pairwise-corpus-readiness.md")

    for claim in (
        "decision**: `refused`",
        "lexical order is a tie-break, not a preference",
        "copies the logging policy and its selection bias",
        "invents counterfactual outcomes",
        "cannot satisfy this gate or unblock hok-190",
        "sample-size analysis is not reached today",
    ):
        assert claim in readiness


def test_judgeable_projection_docs_preserve_the_capture_and_proof_boundaries() -> None:
    contract = prose("docs/judgeable-projection.md")
    adr = prose("docs/adr/0005-judgeable-pre-action-sidecar.md")

    for claim in (
        "capture contract, not a label",
        "remains `unjudgeable`",
        "performs no corpus or holdout i/o",
        "do not prove when capture occurred",
        "does not provision or call a labeler",
        "pairwise capture",
        "does not prove that the producer invoked it before acting",
    ):
        assert claim in contract

    assert "historical episodes remain `unjudgeable`" in adr
    assert "no migration may invent one" in adr


def test_independent_labeler_docs_pin_proof_without_claiming_useful_supervision() -> None:
    contract = prose("docs/independent-labeler.md")
    adr = prose("docs/adr/0006-keyless-independent-pairwise-labeler.md")

    for claim in (
        "accepted workflow sha: fd4e9c50947403a638817404fc6c596add1b0cf3",
        "verification pins the certificate's workflow sha extension",
        "label result: `abstain`",
        "cannot retroactively prove that a source projection predates a real action",
        "does not establish that the project owner is independent from themself",
    ):
        assert claim in contract or claim in prose("docs/judgeable-projection.md")

    assert "never emits a left or right winner" in adr
    assert "pairwise training labels do not constitute an authority attestation" in adr


def test_the_package_metadata_makes_no_emission_claim() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    description = metadata["project"]["description"].lower()
    assert "validates and records" in description
    for claim in ("arbiter", "calibrated", "vectorised", "ranking"):
        assert claim not in description, f"the package description claims {claim!r}"


def test_the_build_backend_is_pinned_and_audited() -> None:
    """A range would mean the audited build and the built build could differ."""
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    requires = metadata["build-system"]["requires"]
    assert requires == ["hatchling==1.32.0"]
    assert "hatchling==1.32.0" in metadata["dependency-groups"]["dev"]

    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert locked.get("hatchling") == "1.32.0"
    for backend_dependency in ("tomlkit", "trove-classifiers", "pathspec", "packaging"):
        assert backend_dependency in locked, backend_dependency

    audit = prose("docs/licenses/dependency-audit.md")
    assert "hatchling" in audit
    assert "trove-classifiers" in audit
    assert "tomlkit" in audit


def test_the_authority_boundary_document_states_the_unconditional_invariant() -> None:
    """The doc must not re-assert check order as the security property."""
    text = prose("docs/authority-boundary.md")
    assert "unconditional and table-independent" in text
    assert 'the security property is *not* "the capability check happens first"' in text


def test_the_package_declares_the_python_it_was_built_for() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["requires-python"] == ">=3.13,<3.14"
    assert metadata["project"]["scripts"] == {"latent-compass": "latent_compass.cli:main"}

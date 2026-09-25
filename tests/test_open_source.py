"""HOK-189 — proofs that the repository is legally and operationally open source.

These assertions are about repository state, so each one first proves it is
looking at a real file. A check that silently examines nothing always passes.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from latent_compass.canonical import seal
from workflow_assertions import assert_required, assert_run, job, needs, step, workflow

REPO = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.fr.md",
    "CHANGELOG.md",
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
    "docs/adr/0007-correct-off-policy-tail-and-labeler-trust.md",
    "docs/adr/0008-opt-in-strategic-decision-memory.md",
    "docs/adr/0009-post-action-reconciliation-journal.md",
    "docs/adr/0010-prospective-shadow-collection.md",
    "docs/decision-memory.md",
    "docs/decision-reconciliation.md",
    "docs/prospective-shadow-collection.md",
    "docs/licenses/dependency-audit.md",
    "examples/synthetic-episode.json",
    "examples/synthetic-projection.json",
    "examples/walkthrough.py",
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


def test_the_readmes_share_bilingual_authority_invariants() -> None:
    english = prose("README.md")
    french = prose("README.fr.md")
    assert "[français](readme.fr.md)" in english
    assert "[english](readme.md)" in french
    for claim in ("validates and records", "emits no operational advisory", "kill_discovery"):
        assert claim in english
    for claim in ("valide et enregistre", "aucun avis opérationnel", "kill_discovery"):
        assert claim in french
    assert "automatically chooses the best strategy" not in english
    assert "choisit automatiquement la meilleure stratégie" not in french
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "/README.fr.md" in metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


def test_readme_relative_links_resolve() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for readme in ("README.md", "README.fr.md"):
        text = (REPO / readme).read_text(encoding="utf-8")
        targets = link_pattern.findall(text)
        assert targets, f"{readme} contains no Markdown links"
        for target in targets:
            path = target.split("#", 1)[0]
            if not path or re.match(r"^[a-z][a-z0-9+.-]*://", path, re.IGNORECASE):
                continue
            assert (REPO / path).exists(), f"{readme} links to missing path: {path}"


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


def test_the_readme_carries_only_workflow_backed_badges() -> None:
    expected = [
        "[![Verify](https://github.com/hoklims/latent-compass/actions/workflows/verify.yml/badge.svg)](https://github.com/hoklims/latent-compass/actions/workflows/verify.yml)",
        "[![Build attested release](https://github.com/hoklims/latent-compass/actions/workflows/release.yml/badge.svg)](https://github.com/hoklims/latent-compass/actions/workflows/release.yml)",
    ]
    for relative in ("README.md", "README.fr.md"):
        lines = (REPO / relative).read_text(encoding="utf-8").splitlines()
        assert [line for line in lines if "![" in line] == expected
    assert (REPO / ".github" / "workflows" / "verify.yml").is_file()
    assert (REPO / ".github" / "workflows" / "release.yml").is_file()


def test_release_workflow_keeps_publish_permissions_in_separate_jobs() -> None:
    payload = workflow(REPO / ".github" / "workflows" / "release.yml")
    verify = job(payload, "verify")
    build = job(payload, "build")
    verify_artifacts = job(payload, "verify-artifacts")
    attest_job = job(payload, "attest")
    github_release = job(payload, "github-release")
    pypi = job(payload, "pypi-publish")

    assert verify["uses"] == "./.github/workflows/verify.yml"
    assert verify["permissions"] == {"contents": "read"}
    assert build["permissions"] == {
        "contents": "read",
    }
    assert verify_artifacts["permissions"] == {"contents": "read"}
    assert attest_job["permissions"] == {"id-token": "write", "attestations": "write"}
    assert github_release["permissions"] == {"contents": "write"}
    assert pypi["permissions"] == {"id-token": "write"}
    attest = step(attest_job, "Attest exact verified artifacts")
    publish_pypi = step(pypi, "Publish distributions to PyPI with trusted publishing")
    assert attest["uses"].startswith("actions/attest-build-provenance@")
    assert publish_pypi["uses"] == (
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    )
    assert pypi["environment"]["name"] == "pypi"
    for required in (
        verify,
        build,
        verify_artifacts,
        attest_job,
        github_release,
        pypi,
        attest,
        publish_pypi,
    ):
        assert_required(required)
    assert needs(build) == {"verify"}
    assert needs(verify_artifacts) == {"build"}
    assert needs(attest_job) == {"build", "verify-artifacts"}
    assert needs(github_release) == {"attest"}
    assert needs(pypi) == {"attest"}
    verify_workflow = workflow(REPO / ".github" / "workflows" / "verify.yml")
    assert "workflow_call" in verify_workflow["on"]
    assert job(verify_workflow, "verify")["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]


def test_github_release_uses_an_explicit_repository_without_checkout() -> None:
    payload = workflow(REPO / ".github" / "workflows" / "release.yml")
    github_release = job(payload, "github-release")
    publish = step(github_release, "Publish exact attested artifacts to GitHub")

    assert all(
        not str(candidate.get("uses", "")).startswith("actions/checkout@")
        for candidate in github_release["steps"]
    )
    assert publish["env"]["GH_REPO"] == "${{ github.repository }}"
    assert_required(publish)


def test_verify_workflow_exercises_the_installed_host_cli_on_all_supported_os() -> None:
    verify = job(workflow(REPO / ".github" / "workflows" / "verify.yml"), "verify")
    assert_run(
        verify,
        "Verify installed wheel outside checkout",
        'uv run --no-project --isolated --python 3.13 --with "${{ github.workspace }}/dist/'
        'latent_compass-0.3.0-py3-none-any.whl" python '
        '"${{ github.workspace }}/tools/verify_installed_wheel.py"',
    )

    assert verify["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    assert_required(verify)
    assert_run(
        verify,
        "Test complete source tree",
        "uv run --frozen pytest -o addopts='' -q",
    )
    assert_run(
        verify,
        "Test extracted source distribution",
        "uv run --frozen python tools/verify_sdist.py dist/latent_compass-0.3.0.tar.gz",
    )
    names = [candidate["name"] for candidate in verify["steps"]]
    assert names.index("Build distributions") < names.index(
        "Verify installed wheel outside checkout"
    )


def test_release_workflow_verifies_the_exact_published_artifacts_on_three_os() -> None:
    payload = workflow(REPO / ".github" / "workflows" / "release.yml")
    build = job(payload, "build")
    verify_artifacts = job(payload, "verify-artifacts")
    attest_job = job(payload, "attest")
    github_release = job(payload, "github-release")
    pypi = job(payload, "pypi-publish")
    artifact_name = step(build, "Retain exact release artifacts")["with"]["name"]

    assert build["outputs"]["version"] == "${{ steps.package.outputs.version }}"
    assert verify_artifacts["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    for candidate_job, download_step in (
        (verify_artifacts, "Retrieve exact release artifacts"),
        (attest_job, "Retrieve verified release artifacts"),
        (github_release, "Retrieve exact release artifacts"),
        (pypi, "Retrieve exact release artifacts"),
    ):
        assert step(candidate_job, download_step)["with"]["name"] == artifact_name
    assert_run(
        verify_artifacts,
        "Test exact installed wheel lifecycle",
        'uv run --no-project --isolated --python 3.13 --with "${{ runner.temp }}/dist/'
        'latent_compass-${{ needs.build.outputs.version }}-py3-none-any.whl" python '
        '"${{ github.workspace }}/tools/verify_installed_wheel.py"',
    )
    assert_run(
        verify_artifacts,
        "Test exact source distribution",
        'uv run --frozen python tools/verify_sdist.py "${{ runner.temp }}/dist/'
        'latent_compass-${{ needs.build.outputs.version }}.tar.gz"',
    )
    build_commands = [
        candidate.get("run", "")
        for candidate_job in payload["jobs"].values()
        if isinstance(candidate_job, dict)
        for candidate in candidate_job.get("steps", [])
        if isinstance(candidate, dict) and "uv build" in str(candidate.get("run", ""))
    ]
    assert build_commands == ["uv build --no-sources"]


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
        ignored = {
            ".git",
            ".mypy_cache",
            ".omx",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "dist",
            "graphify-out",
        }
        if not path.is_file() or ignored & parts:
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
        if {
            ".git",
            ".mypy_cache",
            ".omx",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "dist",
        } & set(path.parts):
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
        "accepted workflow sha: 75af31a8aee941469fe088891cb090747ec0ce89",
        "verification pins the certificate's workflow sha extension",
        "label result: `abstain`",
        "cannot retroactively prove that a source projection predates a real action",
        "does not establish that the project owner is independent from themself",
    ):
        assert claim in contract or claim in prose("docs/judgeable-projection.md")

    assert "never emits a left or right winner" in adr
    assert "pairwise training labels do not constitute an authority attestation" in adr

    raw_contract = (REPO / "docs/independent-labeler.md").read_text(encoding="utf-8")
    accepted = re.search(r"accepted workflow SHA: ([0-9a-f]{40})", raw_contract)
    assert accepted is not None
    decision = json.loads(
        (REPO / "evidence/decisions/latent-compass-kill-discovery-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert decision["evidence_links"]["labeler_workflow_sha"] == accepted.group(1)


def test_terminal_decision_is_immutable_and_the_later_replay_is_reconciled() -> None:
    decision = json.loads(
        (REPO / "evidence/decisions/latent-compass-kill-discovery-v1.json").read_text(
            encoding="utf-8"
        )
    )
    reconciliation = json.loads(
        (
            REPO
            / "evidence/decisions/latent-compass-kill-discovery-v1-reconciliation-2026-08-20.json"
        ).read_text(encoding="utf-8")
    )
    historical_report = json.loads(
        (REPO / "evidence/hok188-run/report.json").read_text(encoding="utf-8")
    )
    corrected_report = json.loads(
        (REPO / "evidence/hok188-run-v1.1.0/report.json").read_text(encoding="utf-8")
    )

    decision_seal = decision.pop("decision_seal")
    reconciliation_seal = reconciliation.pop("reconciliation_seal")
    assert (
        decision_seal == "sha256:f36ad614b89c43fa2eab733fb9a995ca50de933d92929cb00a8a4e29392a8cb4"
    )
    assert decision_seal == seal("project.discovery-decision.v1", decision)
    assert reconciliation["reconciled_at"] > decision["decided_at"]
    assert reconciliation["historical_decision_seal"] == decision_seal
    assert (
        reconciliation["historical_evidence"]["report_seal"] == (historical_report["report_seal"])
    )
    assert reconciliation["corrected_evidence"]["report_seal"] == corrected_report["report_seal"]
    assert reconciliation_seal == seal(
        "project.discovery-decision-reconciliation.v1", reconciliation
    )


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
    assert metadata["project"]["scripts"] == {
        "latent-compass": "latent_compass.cli:main",
        "latent-compass-status": "latent_compass.shadow_status:main",
    }


def test_typed_metadata_and_source_distribution_inputs_exist() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    included = set(metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert (REPO / "src" / "latent_compass" / "py.typed").is_file()
    assert {"/examples", "/evidence", "/CHANGELOG.md"} <= included

"""HOK-801/802 — hostile coverage for the observation-to-model bridge.

Every test drives real ``tmp_path`` reads through the full bridge: scenario
model, snapshot capture, catalog, policy and core state. The host executor is
simulated by hand-built records; the lab-executed literal search is real.
"""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from latent_compass import __version__
from latent_compass.episode import AgentFamily
from latent_compass.lab.cli import EXIT_OK
from latent_compass.lab.cli import main as lab_cli_main
from latent_compass.lab.errors import (
    LabCrossModelStateError,
    LabRepeatedProbeError,
    LabReplayViolationError,
    LabSourceScopeMismatchError,
    LabUnknownReferenceError,
)
from latent_compass.lab.host_observations import (
    Conclusiveness,
    HostObservation,
    HostObservationViolation,
    ObservationPolicy,
    admit_host_observation,
    admit_observation_policy,
    question_digest,
)
from latent_compass.lab.host_session import (
    HostProbeCatalog,
    HostProbeResult,
    LabHostSessionViolationError,
    UnknownReason,
    apply_host_observation,
    derive_host_lab_binding,
    load_host_probe_catalog,
    observe_literal_probe,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.observations import HostBinding, SourceSnapshot, capture_source_snapshot
from latent_compass.lab.planner import propose
from latent_compass.lab.state import DiagnosisStateRevision, initial_state

SCENARIOS = Path(__file__).resolve().parent.parent / "examples" / "lab-scenarios"
CAPTURED_AT = "2026-09-19T00:00:00Z"
OBSERVED_AT = "2026-09-19T00:00:05Z"
LATER_AT = "2026-09-19T00:00:07Z"
VERIFIED_AT = "2026-09-19T00:00:09Z"
HOST_ID = "host-alpha"
ROOT_ID = "root-alpha"
HOST = HostBinding(host_id=HOST_ID, agent_family=AgentFamily.CLAUDE)

CORE = "def compute():\n    return 1\n"
CALLER_DIRECT = "from core import compute\n\nVALUE = compute()\n"
CALLER_SILENT = "VALUE = 2\n"
REGISTRY_WIRED = 'HANDLERS = {"compute-key": "core.compute"}\n'
REGISTRY_BARE = "HANDLERS = {}\n"

PYRIGHT = {"tool_id": "pyright", "version": "1.1.400", "provider_id": "language-server"}
LAB_READER = {"tool_id": "lab-confined-reader", "version": __version__, "provider_id": "source"}
PYTEST = {"tool_id": "pytest", "version": "8.4.1", "provider_id": "test-runner"}
HOST_READER = {"tool_id": "host-file-reader", "version": "1.0.0", "provider_id": "source"}
COVERAGE = "COVERAGE_IS_DECLARED_PATHS_ONLY"


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(root: Path, name: str, text: str) -> None:
    (root / name).write_bytes(text.encode("utf-8"))


def scenario(name: str) -> DiagnosisModel:
    return load_model(json.loads((SCENARIOS / name).read_text(encoding="utf-8")))


def capture(root: Path, names: list[str]) -> SourceSnapshot:
    return capture_source_snapshot(
        root,
        [Path(name) for name in names],
        host=HOST,
        root_id=ROOT_ID,
        captured_at=CAPTURED_AT,
        max_bytes_per_file=4096,
    )


def session_policy(mode: str = "SOURCE_AND_SYMBOLIC", retired: list[str] | None = None) -> Any:
    return admit_observation_policy(
        {"contract_version": "1.0.0", "mode": mode, "retired_providers": retired or []}
    )


def dependency_catalog(**reference_overrides: object) -> HostProbeCatalog:
    references: dict[str, object] = {
        "probe_id": "find-direct-references",
        "kind": "SYMBOL_NAVIGATION",
        "relative_paths": ["caller.py", "registry.py"],
        "question": "compute",
        "anchor": "compute",
        "tool": PYRIGHT,
        "symbol_relation": "REFERENCES",
        "outcome_by_path": {"caller.py": "reference-found", "registry.py": "reference-found"},
        "empty_outcome_id": "no-reference",
    }
    references.update(reference_overrides)
    return load_host_probe_catalog(
        {
            "contract_version": "1.0.0",
            "probes": [
                references,
                {
                    "probe_id": "literal-search-registry-key",
                    "kind": "LITERAL_SEARCH",
                    "relative_paths": ["registry.py"],
                    "question": "compute-key",
                    "anchor": "compute-key",
                    "tool": LAB_READER,
                    "outcome_by_path": {"registry.py": "key-present"},
                    "empty_outcome_id": "key-absent",
                },
            ],
            "external_probe_ids": [],
        }
    )


def resources(index: bool | None, processes: int = 1) -> dict[str, object]:
    return {
        "child_processes": processes,
        "files_read": 2,
        "repeated_reads": 0,
        "internal_index_used": index,
        "cache_used": None,
    }


def references_observation(**overrides: object) -> HostObservation:
    """The host's language server found no direct reference to ``compute``."""
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "observation_id": "observation-references",
        "kind": "SYMBOL_NAVIGATION",
        "probe_id": "find-direct-references",
        "question_digest": question_digest("compute"),
        "host": {"host_id": HOST_ID, "agent_family": "claude"},
        "root_id": ROOT_ID,
        "mode": "SOURCE_AND_SYMBOLIC",
        "executed_by": "HOST_EXECUTOR",
        "tool": PYRIGHT,
        "providers_used": ["language-server"],
        "declared_git_head": None,
        "worktree_dirty": None,
        "observed_at": OBSERVED_AT,
        "status": "EMPTY",
        "covered_paths": ["caller.py", "registry.py"],
        "evidence": [],
        "git_diff": None,
        "symbol": {"relation": "REFERENCES", "language": "python"},
        "check": None,
        "interpreted_outcome_id": None,
        "duration_ms": 80,
        "observed_cost": 2,
        "resources": resources(index=True),
        "limits": [
            COVERAGE,
            "DIRECT_RELATIONS_ONLY",
            "TOOL_INTERNAL_INDEX_USED",
            "EMPTY_IS_NOT_ABSENCE",
        ],
    }
    payload.update(overrides)
    return admit_host_observation(payload)


def found_in_caller(**overrides: object) -> HostObservation:
    found: dict[str, object] = {
        "status": "OBSERVED",
        "evidence": [
            {
                "relative_path": "caller.py",
                "byte_digest": digest(CALLER_DIRECT),
                "size_bytes": len(CALLER_DIRECT.encode("utf-8")),
                "line_start": 3,
                "line_end": 3,
                "generated": False,
            }
        ],
        "limits": [COVERAGE, "DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED"],
    }
    found.update(overrides)
    return references_observation(**found)


class Session:
    """One model, one captured source tree, one catalog and one policy."""

    def __init__(
        self,
        root: Path,
        model: DiagnosisModel,
        catalog: HostProbeCatalog,
        names: list[str],
        policy: ObservationPolicy | None = None,
    ) -> None:
        self.root = root
        self.model = model
        self.catalog = catalog
        self.policy = policy if policy is not None else session_policy()
        self.snapshot = capture(root, names)
        self.binding = derive_host_lab_binding(model, self.snapshot, catalog, self.policy)
        self.history = [initial_state(model, state_id="episode-one", binding=self.binding)]

    @property
    def state(self) -> DiagnosisStateRevision:
        return self.history[-1]

    def apply(self, observation: HostObservation, **overrides: Any) -> HostProbeResult:
        arguments: dict[str, Any] = {
            "root": self.root,
            "snapshot": self.snapshot,
            "catalog": self.catalog,
            "policy": self.policy,
            "observation": observation,
            "expected_host_id": HOST_ID,
            "expected_agent_family": AgentFamily.CLAUDE,
            "expected_root_id": ROOT_ID,
            "verified_at": VERIFIED_AT,
            "history": self.history if self.state.revision > 0 else None,
        }
        arguments.update(overrides)
        result = apply_host_observation(self.model, self.state, **arguments)
        if result.outcome_id is not None:
            self.history.append(result.state)
        return result

    def reason(self, observation: HostObservation, **overrides: Any) -> object:
        with pytest.raises((LabHostSessionViolationError, HostObservationViolation)) as refusal:
            self.apply(observation, **overrides)
        assert isinstance(refusal.value.detail, dict)
        return refusal.value.detail["reason"]


def dependency_session(
    tmp_path: Path, *, caller: str = CALLER_SILENT, registry: str = REGISTRY_WIRED, **options: Any
) -> Session:
    write(tmp_path, "core.py", CORE)
    write(tmp_path, "caller.py", caller)
    write(tmp_path, "registry.py", registry)
    return Session(
        tmp_path,
        scenario("indirect-dependency.json"),
        options.pop("catalog", dependency_catalog()),
        ["caller.py", "core.py", "registry.py"],
        **options,
    )


def run_lab_cli(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = lab_cli_main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue() or "null"), json.loads(err.getvalue() or "null")


def write_payload(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_harness_cli_initializes_and_advances_a_bound_host_session(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    session = dependency_session(root, caller=CALLER_DIRECT)
    model_path = write_payload(tmp_path / "model.json", session.model.canonical_payload())
    snapshot_path = write_payload(tmp_path / "snapshot.json", session.snapshot.canonical_payload())
    catalog_path = write_payload(tmp_path / "catalog.json", session.catalog.canonical_payload())
    policy_path = write_payload(tmp_path / "policy.json", session.policy.canonical_payload())

    code, state, error = run_lab_cli(
        "init-host-state",
        "--model",
        str(model_path),
        "--snapshot",
        str(snapshot_path),
        "--catalog",
        str(catalog_path),
        "--policy",
        str(policy_path),
        "--state-id",
        "episode-one",
    )
    assert code == EXIT_OK, error
    assert error is None
    assert state == session.state.canonical_payload()

    state_path = write_payload(tmp_path / "state.json", state)
    observation_path = write_payload(
        tmp_path / "observation.json", found_in_caller().canonical_payload()
    )
    code, result, error = run_lab_cli(
        "apply-host-observation",
        "--root",
        str(root),
        "--model",
        str(model_path),
        "--snapshot",
        str(snapshot_path),
        "--catalog",
        str(catalog_path),
        "--policy",
        str(policy_path),
        "--state",
        str(state_path),
        "--observation",
        str(observation_path),
        "--expected-host-id",
        HOST_ID,
        "--expected-agent-family",
        "claude",
        "--expected-root-id",
        ROOT_ID,
        "--verified-at",
        VERIFIED_AT,
    )
    assert code == EXIT_OK
    assert error is None
    assert result["outcome_id"] == "reference-found"
    assert result["unknown_reason"] is None
    assert result["state"]["revision"] == 1
    assert result["verification"]["conclusiveness"] == "FULL"


# --- the whole loop -----------------------------------------------------------------


def test_an_episode_runs_from_plan_to_stop_through_lab_and_host_observations(
    tmp_path: Path,
) -> None:
    session = dependency_session(tmp_path)
    first_plan = propose(
        session.model, session.state, budget=3, horizon=2, expected_binding=session.binding
    )
    assert first_plan.recommended_probe_id == "literal-search-registry-key"

    literal = observe_literal_probe(
        root=session.root,
        snapshot=session.snapshot,
        catalog=session.catalog,
        policy=session.policy,
        probe_id="literal-search-registry-key",
        observation_id="observation-literal",
        observed_at=OBSERVED_AT,
    )
    first = session.apply(literal)
    assert (first.outcome_id, first.unknown_reason) == ("key-present", None)
    assert first.state.posterior_weights == {"indirect-dependent-via-registry": 1}
    assert first.verification.conclusiveness is Conclusiveness.FULL

    # The language server finds nothing. That is consistent with the registry
    # world and removes nothing from it: empty is not absence.
    second = session.apply(references_observation(observed_at=LATER_AT))
    assert second.outcome_id == "no-reference"
    assert second.state.revision == 2
    assert second.state.posterior_weights == {"indirect-dependent-via-registry": 1}

    final_plan = propose(
        session.model,
        session.state,
        budget=0,
        horizon=0,
        expected_binding=session.binding,
        history=session.history,
    )
    assert (final_plan.recommended_action, final_plan.stopping_decision_id) == (
        "STOP",
        "change-with-adapter",
    )


def test_a_located_result_maps_to_the_outcome_of_the_path_it_was_found_in(tmp_path: Path) -> None:
    session = dependency_session(tmp_path, caller=CALLER_DIRECT, registry=REGISTRY_BARE)
    result = session.apply(found_in_caller())

    assert result.outcome_id == "reference-found"
    assert result.state.posterior_weights == {"direct-dependent-only": 1}
    assert result.verification.verified_location_count == 1


# --- tool identity and binding ---------------------------------------------------------


def test_a_tool_upgrade_after_the_episode_started_requires_a_new_episode(tmp_path: Path) -> None:
    session = dependency_session(tmp_path)
    upgraded_tool = dict(PYRIGHT, version="1.1.401")

    reason = session.reason(references_observation(tool=upgraded_tool))
    assert reason == "tool_identity_mismatch"
    assert session.state.revision == 0

    # The other half: a session whose catalog expects the new version is a
    # different episode, and admits exactly that observation.
    upgraded = Session(
        tmp_path,
        session.model,
        dependency_catalog(tool=upgraded_tool),
        ["caller.py", "core.py", "registry.py"],
    )
    assert upgraded.binding.source_scope_digest != session.binding.source_scope_digest
    assert upgraded.apply(references_observation(tool=upgraded_tool)).outcome_id == "no-reference"
    with pytest.raises(LabSourceScopeMismatchError):
        upgraded.apply(references_observation(tool=upgraded_tool), catalog=session.catalog)


@pytest.mark.parametrize(
    "variant",
    [
        {"catalog": {"question": "compute_all", "anchor": "compute_all"}},
        {"catalog": {"relative_paths": ["caller.py"], "outcome_by_path": {"caller.py": "x"}}},
        {"policy": ["graphify"]},
    ],
)
def test_the_binding_seals_every_question_coverage_and_policy_change(
    tmp_path: Path, variant: dict[str, Any]
) -> None:
    session = dependency_session(tmp_path)
    catalog_overrides = dict(variant.get("catalog", {}))
    if "outcome_by_path" in catalog_overrides:
        catalog_overrides["outcome_by_path"] = {"caller.py": "reference-found"}
    changed = derive_host_lab_binding(
        session.model,
        session.snapshot,
        dependency_catalog(**catalog_overrides),
        session_policy(retired=variant.get("policy")),
    )
    assert changed.source_scope_digest != session.binding.source_scope_digest
    unchanged = derive_host_lab_binding(
        session.model, session.snapshot, dependency_catalog(), session_policy()
    )
    assert unchanged.source_scope_digest == session.binding.source_scope_digest


# --- UNKNOWN is not an outcome and not a refusal -------------------------------------------


@pytest.mark.parametrize("status", ["TIMEOUT", "TOOL_ABSENT", "UNSUPPORTED_LANGUAGE", "FAILED"])
def test_a_failed_acquisition_leaves_the_state_untouched_and_the_probe_unspent(
    tmp_path: Path, status: str
) -> None:
    session = dependency_session(tmp_path)
    before = session.state.state_seal()
    limits = [COVERAGE, "DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED"]

    unknown = session.apply(references_observation(status=status, limits=limits))
    assert (unknown.outcome_id, unknown.unknown_reason) == (
        None,
        UnknownReason.NON_CONCLUSIVE_STATUS,
    )
    assert unknown.state.state_seal() == before
    assert unknown.verification.conclusiveness is Conclusiveness.NONE
    assert "find-direct-references" not in session.state.acquired_probe_ids()

    # The probe was not consumed: a later conclusive observation is accepted.
    retried = session.apply(
        references_observation(observation_id="observation-retry", observed_at=LATER_AT)
    )
    assert retried.outcome_id == "no-reference"


def check_session(tmp_path: Path) -> Session:
    write(tmp_path, "core.py", CORE)
    write(tmp_path, "test_core.py", "from core import compute\n\nassert compute() == 1\n")
    model = load_model(
        {
            "contract_version": "1.0.0",
            "model_id": "targeted-check-model",
            "worlds": [{"id": "bug-present", "weight": 1}, {"id": "bug-absent", "weight": 1}],
            "probes": [
                {
                    "id": "run-focused-test",
                    "cost": 2,
                    "outcome_space": ["test-fails", "test-passes"],
                    "outcomes": {"bug-present": "test-fails", "bug-absent": "test-passes"},
                }
            ],
            "decisions": [
                {"id": "fix", "losses": {"bug-present": 0, "bug-absent": 5}},
                {"id": "abstain", "losses": {"bug-present": 3, "bug-absent": 3}},
            ],
            "abstain_decision_id": "abstain",
        }
    )
    catalog = load_host_probe_catalog(
        {
            "contract_version": "1.0.0",
            "probes": [
                {
                    "probe_id": "run-focused-test",
                    "kind": "TARGETED_CHECK",
                    "relative_paths": ["core.py", "test_core.py"],
                    "question": "does the focused test pass?",
                    "tool": PYTEST,
                    "passed_outcome_id": "test-passes",
                    "failed_outcome_id": "test-fails",
                }
            ],
        }
    )
    return Session(
        tmp_path, model, catalog, ["core.py", "test_core.py"], session_policy(mode="SOURCE_ONLY")
    )


def check_observation(verdict: str | None, exit_code: int | None, status: str) -> HostObservation:
    return admit_host_observation(
        {
            "contract_version": "1.0.0",
            "observation_id": f"observation-check-{status.lower()}-{verdict or 'none'}".lower(),
            "kind": "TARGETED_CHECK",
            "probe_id": "run-focused-test",
            "question_digest": question_digest("does the focused test pass?"),
            "host": {"host_id": HOST_ID, "agent_family": "claude"},
            "root_id": ROOT_ID,
            "mode": "SOURCE_ONLY",
            "executed_by": "HOST_EXECUTOR",
            "tool": PYTEST,
            "providers_used": ["test-runner"],
            "observed_at": OBSERVED_AT,
            "status": status,
            "covered_paths": ["core.py", "test_core.py"],
            "check": {
                "command_digest": "sha256:" + "c" * 64,
                "verdict": verdict,
                "exit_code": exit_code,
                "log_digest": None,
                "log_bytes": None,
            },
            "resources": resources(index=False),
            "limits": [COVERAGE, "CHECK_VERDICT_IS_HOST_DECLARED"],
        }
    )


@pytest.mark.parametrize(
    ("verdict", "exit_code", "outcome_id", "posterior"),
    [
        ("PASSED", 0, "test-passes", {"bug-absent": 1}),
        ("FAILED", 1, "test-fails", {"bug-present": 1}),
    ],
)
def test_a_check_verdict_maps_onto_the_probes_declared_outcomes(
    tmp_path: Path, verdict: str, exit_code: int, outcome_id: str, posterior: dict[str, int]
) -> None:
    session = check_session(tmp_path)
    result = session.apply(check_observation(verdict, exit_code, "OBSERVED"))
    assert result.outcome_id == outcome_id
    assert result.state.posterior_weights == posterior


def test_an_errored_or_unfinished_check_is_never_counted_as_a_detection(tmp_path: Path) -> None:
    session = check_session(tmp_path)
    before = session.state.state_seal()

    errored = session.apply(check_observation("ERRORED", 4, "OBSERVED"))
    assert (errored.outcome_id, errored.unknown_reason) == (None, UnknownReason.CHECK_ERRORED)
    timed_out = session.apply(check_observation(None, None, "TIMEOUT"))
    assert timed_out.unknown_reason is UnknownReason.NON_CONCLUSIVE_STATUS
    assert session.state.state_seal() == before
    assert session.state.posterior_weights == {"bug-present": 1, "bug-absent": 1}


def identifier_session(tmp_path: Path, module_a: str, module_b: str) -> Session:
    write(tmp_path, "module_a.py", module_a)
    write(tmp_path, "module_b.py", module_b)
    catalog = load_host_probe_catalog(
        {
            "contract_version": "1.0.0",
            "probes": [
                {
                    "probe_id": "literal-search-identifier",
                    "kind": "LITERAL_SEARCH",
                    "relative_paths": ["module_a.py", "module_b.py"],
                    "question": "parse_order",
                    "anchor": "parse_order",
                    "tool": LAB_READER,
                    "outcome_by_path": {
                        "module_a.py": "hit-module-a",
                        "module_b.py": "hit-module-b",
                    },
                    "empty_outcome_id": "no-hit",
                },
                {
                    "probe_id": "symbol-definition-lookup",
                    "kind": "SYMBOL_NAVIGATION",
                    "relative_paths": ["module_a.py", "module_b.py"],
                    "question": "parse_order",
                    "anchor": "parse_order",
                    "tool": PYRIGHT,
                    "symbol_relation": "DEFINITION",
                    "outcome_by_path": {
                        "module_a.py": "defined-in-a",
                        "module_b.py": "defined-in-b",
                    },
                    "empty_outcome_id": "undefined",
                },
            ],
        }
    )
    return Session(
        tmp_path, scenario("known-identifier.json"), catalog, ["module_a.py", "module_b.py"]
    )


def literal(session: Session) -> HostObservation:
    return observe_literal_probe(
        root=session.root,
        snapshot=session.snapshot,
        catalog=session.catalog,
        policy=session.policy,
        probe_id="literal-search-identifier",
        observation_id="observation-literal",
        observed_at=OBSERVED_AT,
    )


def test_a_result_no_declared_world_explains_is_refused_not_resolved(tmp_path: Path) -> None:
    both = "def parse_order():\n    return None\n"
    session = identifier_session(tmp_path, both, both)
    before = session.state.state_seal()

    assert session.reason(literal(session)) == "result_maps_to_several_outcomes"
    assert session.state.state_seal() == before

    # Live trap: the same bridge accepts a result one declared world explains.
    single = identifier_session(tmp_path, both, "VALUE = 1\n")
    assert single.apply(literal(single)).outcome_id == "hit-module-a"


def test_an_empty_exact_search_maps_to_the_residual_world_never_to_a_guess(tmp_path: Path) -> None:
    session = identifier_session(tmp_path, "VALUE = 1\n", "VALUE = 2\n")
    result = session.apply(literal(session))
    assert result.outcome_id == "no-hit"
    assert result.state.posterior_weights == {"none-of-the-above": 1}


def test_a_truncated_result_concludes_only_when_no_other_outcome_could_hide(
    tmp_path: Path,
) -> None:
    definition = "def parse_order():\n    return None\n"
    session = identifier_session(tmp_path, definition, "VALUE = 1\n")
    truncated = admit_host_observation(
        {
            "contract_version": "1.0.0",
            "observation_id": "observation-definition",
            "kind": "SYMBOL_NAVIGATION",
            "probe_id": "symbol-definition-lookup",
            "question_digest": question_digest("parse_order"),
            "host": {"host_id": HOST_ID, "agent_family": "claude"},
            "root_id": ROOT_ID,
            "mode": "SOURCE_AND_SYMBOLIC",
            "executed_by": "HOST_EXECUTOR",
            "tool": PYRIGHT,
            "providers_used": ["language-server"],
            "observed_at": OBSERVED_AT,
            "status": "TRUNCATED",
            "covered_paths": ["module_a.py", "module_b.py"],
            "evidence": [
                {
                    "relative_path": "module_a.py",
                    "byte_digest": digest(definition),
                    "size_bytes": len(definition.encode("utf-8")),
                    "line_start": 1,
                    "line_end": 1,
                    "generated": False,
                }
            ],
            "symbol": {"relation": "DEFINITION", "language": "python"},
            "resources": resources(index=True),
            "limits": [COVERAGE, "OUTPUT_TRUNCATED", "TOOL_INTERNAL_INDEX_USED"],
        }
    )
    hidden = session.apply(truncated)
    assert hidden.unknown_reason is UnknownReason.TRUNCATION_MAY_HIDE_ANOTHER_OUTCOME
    assert hidden.state.revision == 0

    # Every path of the dependency probe answers alike, so presence concludes.
    dependency = dependency_session(tmp_path, caller=CALLER_DIRECT, registry=REGISTRY_BARE)
    presence = found_in_caller(
        status="TRUNCATED",
        limits=[COVERAGE, "DIRECT_RELATIONS_ONLY", "TOOL_INTERNAL_INDEX_USED", "OUTPUT_TRUNCATED"],
    )
    assert dependency.apply(presence).outcome_id == "reference-found"


# --- refusals ------------------------------------------------------------------------------


def test_a_mutation_after_the_observation_refuses_it_and_the_prior_stays_usable(
    tmp_path: Path,
) -> None:
    session = dependency_session(tmp_path, caller=CALLER_DIRECT, registry=REGISTRY_BARE)
    before = session.state.state_seal()
    observation = found_in_caller()

    write(tmp_path, "caller.py", CALLER_DIRECT + "# edited\n")
    assert session.reason(observation) == "drift_since_snapshot"
    assert session.state.state_seal() == before

    write(tmp_path, "caller.py", CALLER_DIRECT)
    assert session.apply(observation).outcome_id == "reference-found"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"question_digest": question_digest("something-else")}, "question_mismatch"),
        ({"covered_paths": ["caller.py"]}, "coverage_mismatch"),
        (
            {
                "symbol": {"relation": "CALLERS", "language": "python"},
            },
            "symbol_relation_mismatch",
        ),
        ({"providers_used": ["graphify", "language-server"]}, None),
    ],
)
def test_an_observation_that_is_not_the_catalogs_is_refused_for_its_own_reason(
    tmp_path: Path, overrides: dict[str, object], expected: str | None
) -> None:
    session = dependency_session(tmp_path)
    if expected is None:
        # Both halves of the provider rule on one record: admitted while the
        # provider is live, refused once the session retires it.
        assert session.apply(references_observation(**overrides)).outcome_id == "no-reference"
        retired = dependency_session(tmp_path, policy=session_policy(retired=["graphify"]))
        assert retired.reason(references_observation(**overrides)) == "retired_provider_used"
        return
    assert session.reason(references_observation(**overrides)) == expected
    assert session.state.revision == 0


def test_a_kind_the_catalog_does_not_declare_for_the_probe_is_refused(tmp_path: Path) -> None:
    session = dependency_session(tmp_path)
    as_literal = admit_host_observation(
        dict(
            references_observation().canonical_payload(),
            kind="LITERAL_SEARCH",
            symbol=None,
            limits=[COVERAGE, "TOOL_INTERNAL_INDEX_USED", "EMPTY_IS_NOT_ABSENCE"],
        )
    )
    assert session.reason(as_literal) == "kind_mismatch"


def test_a_catalog_that_does_not_fit_the_session_never_derives_a_binding(tmp_path: Path) -> None:
    session = dependency_session(tmp_path)
    model, snapshot = session.model, session.snapshot

    def reason(catalog: HostProbeCatalog, policy: ObservationPolicy) -> object:
        with pytest.raises(LabHostSessionViolationError) as refusal:
            derive_host_lab_binding(model, snapshot, catalog, policy)
        assert isinstance(refusal.value.detail, dict)
        return refusal.value.detail["reason"]

    source_only = session_policy(mode="SOURCE_ONLY")
    assert reason(dependency_catalog(), source_only) == "catalog_kind_outside_mode"
    no_language_server = session_policy(retired=["language-server"])
    assert reason(dependency_catalog(), no_language_server) == "catalog_tool_provider_retired"
    outside = dependency_catalog(
        relative_paths=["elsewhere.py"], outcome_by_path={"elsewhere.py": "reference-found"}
    )
    assert reason(outside, session_policy()) == "catalog_path_outside_snapshot"
    undeclared = dependency_catalog(empty_outcome_id="not-an-outcome")
    assert reason(undeclared, session_policy()) == "catalog_outcome_undeclared"
    partial = load_host_probe_catalog(
        {"contract_version": "1.0.0", "probes": [session.catalog.probes[0].canonical_payload()]}
    )
    assert reason(partial, session_policy()) == "catalog_probe_coverage_mismatch"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"empty_outcome_id": None}, "must name its empty outcome"),
        ({"empty_outcome_id": "reference-found"}, "must differ from every located outcome"),
        ({"outcome_by_path": {"caller.py": "reference-found"}}, "must map every declared path"),
        ({"symbol_relation": None}, "only a symbol navigation names a relation"),
        ({"passed_outcome_id": "reference-found"}, "only a targeted check maps verdicts"),
        ({"relative_paths": ["registry.py", "caller.py"]}, "must be sorted"),
    ],
)
def test_an_incoherent_probe_spec_is_refused_for_its_own_reason(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(LabHostSessionViolationError) as refusal:
        dependency_catalog(**overrides)
    assert expected in str(refusal.value.detail)


# --- external probes and interpreted reads ---------------------------------------------------


def requirement_session(tmp_path: Path) -> Session:
    write(tmp_path, "service.py", "def apply():\n    return True\n")
    catalog = load_host_probe_catalog(
        {
            "contract_version": "1.0.0",
            "probes": [
                {
                    "probe_id": "read-implementation",
                    "kind": "FILE_READ",
                    "relative_paths": ["service.py"],
                    "question": "is the implementation consistent with the change?",
                    "tool": HOST_READER,
                }
            ],
            "external_probe_ids": ["authored-requirement-lookup"],
        }
    )
    return Session(
        tmp_path,
        scenario("requirement-absent-from-code.json"),
        catalog,
        ["service.py"],
        session_policy(mode="SOURCE_ONLY"),
    )


def file_read(outcome_id: str | None, **overrides: object) -> HostObservation:
    text = "def apply():\n    return True\n"
    limits = [COVERAGE] + (["OUTCOME_IS_HOST_INTERPRETED"] if outcome_id else [])
    payload: dict[str, object] = {
        "contract_version": "1.0.0",
        "observation_id": "observation-read",
        "kind": "FILE_READ",
        "probe_id": "read-implementation",
        "question_digest": question_digest("is the implementation consistent with the change?"),
        "host": {"host_id": HOST_ID, "agent_family": "claude"},
        "root_id": ROOT_ID,
        "mode": "SOURCE_ONLY",
        "executed_by": "HOST_EXECUTOR",
        "tool": HOST_READER,
        "providers_used": ["source"],
        "observed_at": OBSERVED_AT,
        "status": "OBSERVED",
        "covered_paths": ["service.py"],
        "evidence": [
            {
                "relative_path": "service.py",
                "byte_digest": digest(text),
                "size_bytes": len(text.encode("utf-8")),
                "line_start": 1,
                "line_end": 2,
                "generated": False,
            }
        ],
        "interpreted_outcome_id": outcome_id,
        "resources": resources(index=False, processes=0),
        "limits": limits,
    }
    payload.update(overrides)
    return admit_host_observation(payload)


def test_a_source_read_cannot_stand_in_for_a_requirement_written_nowhere_in_the_code(
    tmp_path: Path,
) -> None:
    session = requirement_session(tmp_path)

    read = session.apply(file_read("code-consistent"))
    assert read.outcome_id == "code-consistent"
    # The read changed nothing the decision depends on: both worlds remain.
    assert read.state.posterior_weights == {"requirement-allows": 9, "requirement-forbids": 1}

    plan = propose(
        session.model,
        session.state,
        budget=0,
        horizon=0,
        expected_binding=session.binding,
        history=session.history,
    )
    assert plan.stopping_decision_id == "abstain-and-request-ruling"
    admissible = {item.decision_id: item.admissible for item in plan.decisions}
    assert admissible["apply-change"] is False

    forged = file_read("rule-allows", probe_id="authored-requirement-lookup")
    assert session.reason(forged) == "probe_is_external"


def test_a_file_read_needs_a_flagged_interpretation_the_probe_declares(tmp_path: Path) -> None:
    session = requirement_session(tmp_path)
    assert session.reason(file_read(None)) == "file_read_not_interpreted"
    with pytest.raises(LabUnknownReferenceError):
        session.apply(file_read("rule-allows"))
    assert session.state.revision == 0


# --- replay, scope and forgery -----------------------------------------------------------------


def test_the_bridge_refuses_a_repeated_probe_a_foreign_model_and_a_missing_history(
    tmp_path: Path,
) -> None:
    session = dependency_session(tmp_path)
    session.apply(references_observation())

    with pytest.raises(LabRepeatedProbeError):
        session.apply(references_observation(observation_id="observation-again"))
    with pytest.raises(LabReplayViolationError):
        session.apply(references_observation(observation_id="observation-again"), history=None)
    with pytest.raises(LabCrossModelStateError):
        apply_host_observation(
            scenario("known-identifier.json"),
            session.state,
            root=session.root,
            snapshot=session.snapshot,
            catalog=session.catalog,
            policy=session.policy,
            observation=references_observation(),
            expected_host_id=HOST_ID,
            expected_agent_family=AgentFamily.CLAUDE,
            expected_root_id=ROOT_ID,
            verified_at=VERIFIED_AT,
            history=session.history,
        )


def test_the_bridge_revalidates_a_forged_catalog_before_anything_else(tmp_path: Path) -> None:
    session = dependency_session(tmp_path)
    forged = session.catalog.model_copy(
        update={
            "probes": (
                session.catalog.probes[0].model_copy(update={"empty_outcome_id": None}),
                session.catalog.probes[1],
            )
        }
    )
    with pytest.raises(LabHostSessionViolationError):
        session.apply(references_observation(), catalog=forged)


def test_the_labs_reader_answers_literal_searches_and_nothing_else(tmp_path: Path) -> None:
    session = dependency_session(tmp_path)
    with pytest.raises(LabHostSessionViolationError) as refusal:
        observe_literal_probe(
            root=session.root,
            snapshot=session.snapshot,
            catalog=session.catalog,
            policy=session.policy,
            probe_id="find-direct-references",
            observation_id="observation-wrong-kind",
            observed_at=OBSERVED_AT,
        )
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "probe_is_not_a_literal_search"

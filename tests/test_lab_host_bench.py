"""HOK-803 — the host bench, driven with real tools rather than mocks.

Every test builds a throwaway Git repository under ``tmp_path`` and lets the
bench's reference executor really run ``git``, parse Python source and execute
a check script. The failures exercised here are real ones too: a binary that
does not exist, a process that overruns its timeout, a language the symbol
tool cannot parse. Nothing touches an agent session, a hook or a provider.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from latent_compass.episode import AgentFamily
from latent_compass.lab.host_observations import (
    HostObservationViolation,
    ObservationStatus,
    admit_observation_policy,
)
from latent_compass.lab.host_session import (
    HostProbeCatalog,
    LabHostSessionViolationError,
    UnknownReason,
    apply_host_observation,
    derive_host_lab_binding,
    load_host_probe_catalog,
)
from latent_compass.lab.model import load_model
from latent_compass.lab.observations import HostBinding, capture_source_snapshot
from latent_compass.lab.planner import propose
from latent_compass.lab.state import initial_state

BENCH_PATH = Path(__file__).resolve().parents[1] / "examples" / "lab_host_bench.py"


def _bench_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("latent_compass_lab_host_bench", BENCH_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _bench_module()

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the bench runs a real git")

EXPECTED_OUTCOMES = {
    "diff-pricing": "changed",
    "define-legacy-consumer": "defined-in-invoice",
    "run-invoice-check": "check-fails",
}


class Drill:
    """One fresh bench repository with a session at revision 0."""

    def __init__(
        self,
        root: Path,
        *,
        family: AgentFamily = AgentFamily.CLAUDE,
        catalog: HostProbeCatalog | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> None:
        root.mkdir(parents=True)
        self.root = root
        self.family = family
        setup = bench.ReferenceHostExecutor(root)
        git_tool = setup.git_identity()
        assert git_tool is not None
        bench.build_repository(root, setup)
        # Extra files land before the snapshot, so they may also replace a bench file.
        for name, text in (extra_files or {}).items():
            (root / name).write_bytes(text.encode("utf-8"))
        names = {*bench.SOURCE_FILES, *(extra_files or {})}
        self.model = load_model(bench.MODEL_PAYLOAD)
        self.catalog = catalog if catalog is not None else bench.build_catalog(git_tool)
        self.policy = admit_observation_policy(
            {"contract_version": "1.0.0", "mode": "SOURCE_AND_SYMBOLIC", "retired_providers": []}
        )
        self.snapshot = capture_source_snapshot(
            root,
            [Path(name) for name in sorted(names)],
            host=HostBinding(host_id=bench.HOST_ID, agent_family=family),
            root_id=bench.ROOT_ID,
            captured_at=bench.CAPTURED_AT,
            max_bytes_per_file=64 * 1024,
        )
        self.binding = derive_host_lab_binding(self.model, self.snapshot, self.catalog, self.policy)
        self.state = initial_state(self.model, state_id="drill-episode", binding=self.binding)

    def observe(self, probe_id: str, **executor_options: Any) -> Any:
        spec = self.catalog.spec_by_id(probe_id)
        assert spec is not None
        executor = bench.ReferenceHostExecutor(self.root, **executor_options)
        return executor.observe(
            spec,
            family=self.family,
            mode=self.policy.mode.value,
            observation_id=f"drill-{probe_id}",
        )

    def apply(self, observation: Any, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "root": self.root,
            "snapshot": self.snapshot,
            "catalog": self.catalog,
            "policy": self.policy,
            "observation": observation,
            "expected_host_id": bench.HOST_ID,
            "expected_agent_family": self.family,
            "expected_root_id": bench.ROOT_ID,
            "verified_at": bench.NOW,
        }
        arguments.update(overrides)
        return apply_host_observation(self.model, self.state, **arguments)


# --- the three real tool paths -------------------------------------------------------


@pytest.mark.parametrize("probe_id", sorted(EXPECTED_OUTCOMES))
def test_every_real_tool_path_is_admitted_verified_and_mapped(
    tmp_path: Path, probe_id: str
) -> None:
    drill = Drill(tmp_path / "repository")
    observation = drill.observe(probe_id)

    assert observation.status is ObservationStatus.OBSERVED
    result = drill.apply(observation)
    assert result.outcome_id == EXPECTED_OUTCOMES[probe_id]
    assert result.state.revision == 1
    assert result.verification.verified_paths == observation.covered_paths


def test_the_git_diff_is_a_real_run_bound_to_the_dirty_working_tree(tmp_path: Path) -> None:
    drill = Drill(tmp_path / "repository")
    observation = drill.observe("diff-pricing")

    assert observation.git_diff is not None
    assert [entry.relative_path for entry in observation.git_diff.entries] == ["pricing.py"]
    assert observation.worktree_dirty is True
    assert observation.resources.child_processes >= 2
    observed_git = bench.ReferenceHostExecutor(drill.root).git_identity()
    assert observed_git is not None
    assert observation.tool.version == observed_git["version"]
    # HEAD is identical before and after; only the content on disk decides.
    assert observation.declared_git_head == observation.git_diff.base_identity
    (drill.root / "pricing.py").write_bytes(bench.PRICING_BASE.encode("utf-8"))
    with pytest.raises(HostObservationViolation) as refusal:
        drill.apply(observation)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "drift_since_snapshot"


def test_the_check_verdict_is_the_real_exit_code_of_a_real_process(tmp_path: Path) -> None:
    drill = Drill(tmp_path / "repository")
    failing = drill.observe("run-invoice-check")
    assert failing.check is not None
    assert (failing.check.verdict, failing.check.exit_code) == ("FAILED", 1)
    assert failing.resources.child_processes == 1


# --- real failures, never converted into an outcome ---------------------------------------


def test_a_binary_that_does_not_exist_is_tool_absent_and_leaves_the_state_alone(
    tmp_path: Path,
) -> None:
    drill = Drill(tmp_path / "repository")
    observation = drill.observe("diff-pricing", git_binary="git-binary-that-does-not-exist")

    assert observation.status is ObservationStatus.TOOL_ABSENT
    result = drill.apply(observation)
    assert (result.outcome_id, result.unknown_reason) == (
        None,
        UnknownReason.NON_CONCLUSIVE_STATUS,
    )
    assert result.state.state_seal() == drill.state.state_seal()


def test_a_process_that_overruns_is_interrupted_and_reported_as_a_timeout(tmp_path: Path) -> None:
    slow = Drill(
        tmp_path / "repository",
        extra_files={"check_invoice.py": "import time\n\ntime.sleep(30)\n"},
    )

    observation = slow.observe("run-invoice-check", timeout_seconds=0.5)
    assert observation.status is ObservationStatus.TIMEOUT
    assert observation.check is not None
    assert (observation.check.verdict, observation.check.exit_code) == (None, None)
    assert observation.duration_ms is not None
    assert observation.duration_ms < 20_000, "the overrun was waited for, not interrupted"

    result = slow.apply(observation)
    assert (result.outcome_id, result.unknown_reason) == (
        None,
        UnknownReason.NON_CONCLUSIVE_STATUS,
    )
    assert result.state.state_seal() == slow.state.state_seal()


def test_a_language_the_symbol_tool_cannot_parse_is_reported_not_guessed(tmp_path: Path) -> None:
    git_tool = bench.ReferenceHostExecutor(tmp_path).git_identity()
    assert git_tool is not None
    payload = bench.build_catalog(git_tool).canonical_payload()
    assert isinstance(payload["probes"], list)
    symbol_spec = dict(payload["probes"][1])
    symbol_spec.update(
        relative_paths=["notes.txt"], outcome_by_path={"notes.txt": "defined-in-invoice"}
    )
    payload["probes"] = [payload["probes"][0], symbol_spec, payload["probes"][2]]
    drill = Drill(
        tmp_path / "repository",
        catalog=load_host_probe_catalog(payload),
        extra_files={"notes.txt": "legacy_total is described here\n"},
    )

    observation = drill.observe("define-legacy-consumer")
    assert observation.status is ObservationStatus.UNSUPPORTED_LANGUAGE
    assert drill.apply(observation).outcome_id is None


# --- the loop: who decides what -----------------------------------------------------------


def test_the_episode_stops_on_the_decision_the_real_observations_support(tmp_path: Path) -> None:
    trace = bench.run_episode(tmp_path / "repository")

    assert trace["final_decision_id"] == "hold-and-fix"
    executed = [step for step in trace["steps"] if "execution" in step]
    assert executed, "the episode executed nothing"
    for step in executed:
        # Five distinct records per executed step, none standing in for another.
        assert set(step) == {"advice", "routing", "host_decision", "execution", "result"}
        assert step["routing"]["verdict"] == "ADVICE"
        assert step["host_decision"] == "ACCEPTED"
        assert step["result"]["verification_seal"].startswith("sha256:")
        assert step["execution"]["observed_cost"] is None, "the bench invents no measured cost"
    spent = sum(step["execution"]["planned_cost"] for step in executed)
    assert trace["remaining_budget"] == trace["total_budget"] - spent
    assert trace["steps"][-1]["advice"]["recommended_action"] == "STOP"


def test_a_host_that_ignores_the_advisor_runs_nothing_and_still_decides(tmp_path: Path) -> None:
    ignored = bench.run_episode(
        tmp_path / "repository", router=bench.BenchHostRouter(accept_advice=False)
    )
    assert [step["host_decision"] for step in ignored["steps"]] == ["IGNORED"]
    assert all("execution" not in step for step in ignored["steps"])
    # Building the repository did run git; that is counted, and counted apart.
    assert ignored["setup_child_processes"] >= 4
    assert ignored["probe_child_processes"] == 0
    assert ignored["remaining_budget"] == ignored["total_budget"]
    assert ignored["final_state_revision"] == 0
    assert ignored["final_decision_id"] == "hold-and-fix"


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"advisor_present": False}, "ADVISOR_ABSENT"),
        ({"kill_switch_engaged": True}, "KILL_SWITCH_ENGAGED"),
        ({"withheld_capabilities": frozenset({"python-ast"})}, "CAPABILITY_MISSING"),
    ],
)
def test_without_advice_the_loop_takes_the_native_path_and_executes_nothing(
    tmp_path: Path, options: dict[str, Any], reason: str
) -> None:
    trace = bench.run_episode(tmp_path / "repository", **options)
    step = trace["steps"][0]

    assert (step["routing"]["verdict"], step["routing"]["reason"]) == ("ABSTAIN", reason)
    assert step["host_decision"] == "NOT_ADVISED"
    assert "execution" not in step
    assert trace["probe_child_processes"] == 0
    assert trace["final_state_revision"] == 0


@pytest.mark.parametrize(
    ("overrides", "verdict", "reason", "host_decision"),
    [
        (
            {"requested_at": "2026-09-19T00:00:05Z", "expiry": "2026-09-19T00:00:10Z"},
            "ABSTAIN",
            "TIMING_INCOHERENT",
            "NOT_ADVISED",
        ),
        ({"source_digest": "sha256:" + "0" * 64}, "ABSTAIN", "SOURCE_CONFLICT", "NOT_ADVISED"),
        ({"scope": "another-scope"}, "ABSTAIN", "CAPABILITY_INCOMPATIBLE", "NOT_ADVISED"),
        (
            {"semctx_proof_required": True},
            "ESCALATE",
            "SEMCTX_PROOF_REQUIRED_UNSATISFIED",
            "ESCALATED",
        ),
        ({"review_required": True}, "ESCALATE", "REVIEW_REQUIRED_UNSATISFIED", "ESCALATED"),
        ({"explicit_missing_authority": True}, "ESCALATE", "AUTHORITY_MISSING", "ESCALATED"),
    ],
)
def test_expired_conflicting_or_ungated_advice_executes_nothing(
    tmp_path: Path, overrides: dict[str, Any], verdict: str, reason: str, host_decision: str
) -> None:
    trace = bench.run_episode(tmp_path / "repository", route_request_overrides=overrides)
    assert len(trace["steps"]) == 1
    step = trace["steps"][0]

    assert (step["routing"]["verdict"], step["routing"]["reason"]) == (verdict, reason)
    assert step["host_decision"] == host_decision
    assert "execution" not in step
    assert trace["probe_child_processes"] == 0
    assert trace["remaining_budget"] == trace["total_budget"]
    assert trace["final_state_revision"] == 0
    # The native path still ends on the best admissible decision of the prior.
    assert trace["final_decision_id"] == "hold-and-fix"


def test_a_gate_whose_proof_is_present_lets_the_same_loop_proceed(tmp_path: Path) -> None:
    trace = bench.run_episode(
        tmp_path / "repository",
        route_request_overrides={"semctx_proof_required": True, "semctx_proof_satisfied": True},
    )
    first = trace["steps"][0]
    assert (first["routing"]["verdict"], first["host_decision"]) == ("ADVICE", "ACCEPTED")
    assert first["execution"]["status"] == "OBSERVED"
    assert trace["final_state_revision"] == 1


def test_an_exhausted_budget_buys_no_observation(tmp_path: Path) -> None:
    trace = bench.run_episode(tmp_path / "repository", total_budget=0)
    assert all("execution" not in step for step in trace["steps"])
    assert trace["steps"][0]["advice"]["recommended_action"] == "STOP"
    assert trace["remaining_budget"] == 0


def test_an_unobtainable_probe_is_not_retried_and_no_other_provider_stands_in(
    tmp_path: Path,
) -> None:
    trace = bench.run_episode(tmp_path / "repository", unavailable_tools=frozenset({"python-ast"}))
    assert len(trace["steps"]) == 2, "the unobtainable probe was attempted again"
    attempted, stopped = trace["steps"]

    # Hand-derived: probing the symbol first is worth 5/2 against 15/4 for stopping, and it
    # still is once its failed attempt has cost 2 of the 6 points. The advisor names it again.
    assert attempted["advice"]["recommended_probe_id"] == "define-legacy-consumer"
    assert attempted["advice"]["plan_value"] == "5/2"
    assert attempted["execution"]["status"] == "TOOL_ABSENT"
    assert attempted["execution"]["child_processes"] == 0
    assert attempted["result"]["outcome_id"] is None
    assert attempted["result"]["unknown_reason"] == "NON_CONCLUSIVE_STATUS"
    assert attempted["result"]["state_seal"] == attempted["result"]["prior_state_seal"]

    assert stopped["advice"]["recommended_probe_id"] == "define-legacy-consumer"
    assert stopped["stopped_because"] == "recommended_probe_unobtainable"
    assert "execution" not in stopped
    # The failed attempt is still paid for, at its reserved ceiling; nothing else ran.
    assert trace["remaining_budget"] == 4
    assert trace["probe_child_processes"] == 0
    assert trace["final_state_revision"] == 0
    assert trace["final_decision_id"] == "hold-and-fix"


def test_the_documented_planner_limit_is_stated_with_its_real_numbers(tmp_path: Path) -> None:
    # docs/host-observations.md quotes these values. With 4 points left the advisor still
    # names the unobtainable symbol probe; the check probe (7/2) would beat stopping (15/4),
    # and diff-pricing's 3/1 cannot be reused: it assumes the symbol probe comes next.
    drill = Drill(tmp_path / "repository")
    report = propose(drill.model, drill.state, budget=4, horizon=2, expected_binding=drill.binding)

    assert (report.recommended_probe_id, report.plan_value) == ("define-legacy-consumer", "5/2")
    assert report.stopping_value == "15/4"
    assert {probe.probe_id: probe.value for probe in report.probes} == {
        "define-legacy-consumer": "5/2",
        "diff-pricing": "3/1",
        "run-invoice-check": "7/2",
    }


def test_a_retired_provider_refuses_the_session_instead_of_falling_back(tmp_path: Path) -> None:
    retired = admit_observation_policy(
        {
            "contract_version": "1.0.0",
            "mode": "SOURCE_AND_SYMBOLIC",
            "retired_providers": ["python-ast"],
        }
    )
    with pytest.raises(LabHostSessionViolationError) as refusal:
        bench.run_episode(tmp_path / "repository", policy=retired)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "catalog_tool_provider_retired"


# --- two agent families, two separate episodes ------------------------------------------------


def test_both_families_run_the_same_loop_and_never_share_an_episode(tmp_path: Path) -> None:
    claude = bench.run_episode(tmp_path / "claude", family=AgentFamily.CLAUDE)
    codex = bench.run_episode(tmp_path / "codex", family=AgentFamily.CODEX)

    def shape(trace: dict[str, Any]) -> list[tuple[Any, ...]]:
        return [
            (
                step["advice"]["recommended_probe_id"],
                step["host_decision"],
                step.get("result", {}).get("outcome_id"),
            )
            for step in trace["steps"]
        ]

    assert shape(claude) == shape(codex)
    assert claude["final_decision_id"] == codex["final_decision_id"]

    # An observation made for one family is refused by the other's episode.
    claude_drill = Drill(tmp_path / "claude-drill", family=AgentFamily.CLAUDE)
    codex_drill = Drill(tmp_path / "codex-drill", family=AgentFamily.CODEX)
    foreign = codex_drill.observe("define-legacy-consumer")
    with pytest.raises(HostObservationViolation) as refusal:
        claude_drill.apply(foreign)
    assert isinstance(refusal.value.detail, dict)
    assert refusal.value.detail["reason"] == "scope_mismatch"
    assert claude_drill.apply(claude_drill.observe("define-legacy-consumer")).outcome_id == (
        "defined-in-invoice"
    )


# --- delegation: child agents share one global budget -----------------------------------------


def _executed_costs(episode: dict[str, Any]) -> list[int]:
    return [step["execution"]["planned_cost"] for step in episode["steps"] if "execution" in step]


def test_child_agents_draw_on_one_global_budget_not_on_a_reserve_each(tmp_path: Path) -> None:
    # Alone, 3 points buy the 2-point symbol probe. That is what each child would do with a
    # reserve of its own — and what only the first can do once the 3 points are shared.
    alone = bench.run_episode(tmp_path / "alone", total_budget=3)
    assert _executed_costs(alone) == [2]

    trace = bench.run_delegation(tmp_path / "shared", total_budget=3)
    first, second = trace["delegations"]
    assert [item["child"] for item in (first, second)] == ["child-a", "child-b"]
    assert [item["remaining_budget_at_delegation"] for item in (first, second)] == [3, 1]
    assert _executed_costs(first["episode"]) == [2]
    assert first["episode"]["final_state_revision"] == 1

    # The starved child buys nothing and still decides natively, on its prior alone.
    assert _executed_costs(second["episode"]) == []
    assert [step["advice"]["recommended_action"] for step in second["episode"]["steps"]] == ["STOP"]
    assert second["episode"]["final_state_revision"] == 0
    assert second["episode"]["final_decision_id"] == "hold-and-fix"

    # One ledger says who spent what: each probe reservation is nested under its delegation.
    ledger = trace["budget_ledger"]
    assert {
        (item["reservation_id"], item["parent_reservation_id"], item["requested_maximum"])
        for item in ledger["reservations"]
    } == {
        ("delegation-child-a", None, 0),
        ("child-a-reservation-0-define-legacy-consumer", "delegation-child-a", 2),
        ("delegation-child-b", None, 0),
    }
    assert ledger["total_budget"] == 3
    assert (trace["total_budget"], trace["spent"], trace["remaining_budget"]) == (3, 2, 1)


def test_a_pool_that_holds_enough_serves_every_child(tmp_path: Path) -> None:
    # The other half: a child is starved by the pool, not by being a second child.
    trace = bench.run_delegation(tmp_path / "shared", total_budget=4)
    assert [_executed_costs(item["episode"]) for item in trace["delegations"]] == [[2], [2]]
    assert [item["remaining_budget_at_delegation"] for item in trace["delegations"]] == [4, 2]
    assert (trace["spent"], trace["remaining_budget"]) == (4, 0)
    # Two children, two checkouts: neither episode ran in the other's repository.
    assert sorted(path.name for path in (tmp_path / "shared").iterdir()) == ["child-a", "child-b"]
    assert [item["scope"]["checkout"] for item in trace["delegations"]] == ["child-a", "child-b"]


def test_a_child_planning_on_a_stale_remainder_is_refused_by_the_pool_before_anything_runs(
    tmp_path: Path,
) -> None:
    # Both children are told "3 points remain", as two children briefed at the same instant
    # would be. The first spends 2. The second still plans, is advised and accepted — and the
    # pool, not its view of the pool, refuses the reservation.
    trace = bench.run_delegation(tmp_path / "shared", total_budget=3, stale_budget_view=3)
    first, second = trace["delegations"]
    assert _executed_costs(first["episode"]) == [2]

    assert len(second["episode"]["steps"]) == 1, "the refused child went on"
    (refused,) = second["episode"]["steps"]
    assert refused["advice"]["recommended_probe_id"] == "define-legacy-consumer"
    assert refused["routing"]["verdict"] == "ADVICE"
    assert refused["host_decision"] == "ACCEPTED"
    assert "execution" not in refused
    assert refused["budget_refusal"] == {
        "reservation_id": "child-b-reservation-0-define-legacy-consumer",
        "requested_maximum": 2,
        "available": 1,
    }
    assert second["episode"]["final_state_revision"] == 0
    assert (trace["spent"], trace["remaining_budget"]) == (2, 1)
    assert "child-b-reservation-0-define-legacy-consumer" not in {
        item["reservation_id"] for item in trace["budget_ledger"]["reservations"]
    }


def test_a_cancelled_delegation_builds_no_checkout_and_leaves_the_budget_to_its_sibling(
    tmp_path: Path,
) -> None:
    trace = bench.run_delegation(
        tmp_path / "shared", total_budget=3, cancelled=frozenset({"child-a"})
    )
    cancelled, sibling = trace["delegations"]
    assert (cancelled["outcome"], cancelled["episode"]) == ("CANCELLED", None)
    assert not (tmp_path / "shared" / "child-a").exists()
    assert sibling["outcome"] == "SUCCEEDED"
    assert sibling["remaining_budget_at_delegation"] == 3
    assert _executed_costs(sibling["episode"]) == [2]
    assert {
        item["reservation_id"]: (item["outcome"], item["charged_cost"])
        for item in trace["budget_ledger"]["charges"]
    } == {
        "delegation-child-a": ("CANCELLED", 0),
        "child-b-reservation-0-define-legacy-consumer": ("SUCCEEDED", 2),
        "delegation-child-b": ("SUCCEEDED", 0),
    }


def test_two_children_are_never_handed_the_same_checkout(tmp_path: Path) -> None:
    # A later lock would refuse too — a replayed reservation id — but only once the first
    # child had already built its checkout and spent. The refusal has to come first.
    with pytest.raises((ValueError, bench.LabRoutingViolation)) as refusal:
        bench.run_delegation(tmp_path / "shared", children=("child-a", "child-a"))
    assert not (tmp_path / "shared").exists(), "something was built before the refusal"
    assert isinstance(refusal.value, ValueError)
    assert "checkout of its own" in str(refusal.value)

    # And no episode, delegated or not, reuses a checkout that already exists.
    bench.run_delegation(tmp_path / "shared", children=("child-a",))
    with pytest.raises(FileExistsError, match="refusing to reuse"):
        bench.run_delegation(tmp_path / "shared", children=("child-a",))

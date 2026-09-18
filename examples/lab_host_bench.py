"""HOK-803 — an isolated bench of the host, advisor, router, executor, observation loop.

SYNTHETIC BENCH ONLY. It builds a throwaway Git repository in a fresh temporary
directory and never reads, configures or contacts a real agent session, hook,
MCP server or index provider. It exists to exercise, with **real tools rather
than mocks**, the one thing ``latent_compass.lab`` may not do itself: execute.

The roles stay separate, and the trace keeps them separate:

* **advisor** — ``latent_compass.lab``: proposes a probe, gives routing advice;
* **router** — :class:`BenchHostRouter`, standing in for the host's own
  already-authorised router: it alone accepts or ignores advice;
* **executor** — :class:`ReferenceHostExecutor`, the only code here that
  launches a process (``git``, a Python check) or parses source for symbols;
* **observation** — what the executor returns, admitted and re-checked by
  ``latent_compass.lab.host_session`` before the model may consume it.

The executor lives in ``examples/`` on purpose: ``tests/test_lab_boundary.py``
refuses any process launch inside the lab package. Tool identities are
observed (``git --version``, the running interpreter), never hard-coded. A
CODEX or CLAUDE family here is a declared lab identity, not evidence of parity
between two live hosts. Costs are settled at each reservation's own ceiling:
the bench measures durations and fabricates no token or money cost. Every
timestamp in a trace comes from a fixed bench clock, so that seals reproduce;
only ``duration_ms`` is wall-clock.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

from latent_compass.canonical import seal
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation
from latent_compass.lab import LAB_NON_AUTHORITY_NOTICE
from latent_compass.lab.host_observations import (
    HostObservation,
    ObservationPolicy,
    admit_host_observation,
    admit_observation_policy,
    question_digest,
)
from latent_compass.lab.host_session import (
    HostProbeCatalog,
    HostProbeSpec,
    apply_host_observation,
    derive_host_lab_binding,
    load_host_probe_catalog,
)
from latent_compass.lab.model import DiagnosisModel, load_model
from latent_compass.lab.observations import HostBinding, capture_source_snapshot
from latent_compass.lab.planner import propose
from latent_compass.lab.routing import (
    LAB_ROUTING_CONTRACT_VERSION,
    BudgetPoolState,
    BudgetReservation,
    BudgetSettlement,
    CapabilityKind,
    RouteVerdict,
    SettlementOutcome,
    admit_host_capability_snapshot,
    admit_route_request,
    evaluate_route,
    reserve_budget,
    settle_budget,
)
from latent_compass.lab.state import DiagnosisStateRevision, initial_state

BENCH_NOTICE: Final = (
    "SYNTHETIC BENCH ONLY: a throwaway Git repository, real local tools, no agent session, "
    "no hook, no MCP server and no index provider."
)
HOST_ID: Final = "lab-bench-host"
ROOT_ID: Final = "lab-bench-root"
SCOPE: Final = "lab-bench-scope"
CAPTURED_AT: Final = "2026-09-19T00:00:00Z"
NOW: Final = "2026-09-19T00:00:30Z"
EXPIRY: Final = "2026-09-19T01:00:00Z"
COVERAGE_LIMIT: Final = "COVERAGE_IS_DECLARED_PATHS_ONLY"
GIT_IDENTITY: Final = ["-c", "user.name=lab-bench", "-c", "user.email=lab-bench@example.invalid"]

PRICING_BASE: Final = "RATE = 10\n\n\ndef price(quantity):\n    return quantity * RATE\n"
PRICING_CHANGED: Final = "RATE = 12\n\n\ndef price(quantity):\n    return quantity * RATE\n"
INVOICE: Final = (
    "from pricing import price\n\n\ndef legacy_total(quantity):\n    return price(quantity) + 1\n"
)
CHECK: Final = (
    "import sys\n\nfrom invoice import legacy_total\n\n"
    "sys.exit(0 if legacy_total(2) == 21 else 1)\n"
)
SOURCE_FILES: Final = ("check_invoice.py", "invoice.py", "pricing.py")

MODEL_PAYLOAD: Final[dict[str, Any]] = {
    "contract_version": "1.0.0",
    "model_id": "lab-host-bench-pending-change",
    "worlds": [
        {"id": "change-is-isolated", "weight": 2},
        {"id": "change-breaks-consumer", "weight": 1},
        {"id": "no-pending-change", "weight": 1},
    ],
    "probes": [
        {
            "id": "diff-pricing",
            "cost": 1,
            "outcome_space": ["changed", "unchanged"],
            "outcomes": {
                "change-is-isolated": "changed",
                "change-breaks-consumer": "changed",
                "no-pending-change": "unchanged",
            },
        },
        {
            "id": "define-legacy-consumer",
            "cost": 2,
            "outcome_space": ["defined-in-invoice", "undefined"],
            "outcomes": {
                "change-is-isolated": "undefined",
                "change-breaks-consumer": "defined-in-invoice",
                "no-pending-change": "undefined",
            },
        },
        {
            "id": "run-invoice-check",
            "cost": 3,
            "outcome_space": ["check-passes", "check-fails"],
            "outcomes": {
                "change-is-isolated": "check-passes",
                "change-breaks-consumer": "check-fails",
                "no-pending-change": "check-passes",
            },
        },
    ],
    "decisions": [
        {
            "id": "ship",
            "losses": {
                "change-is-isolated": 0,
                "change-breaks-consumer": 20,
                "no-pending-change": 2,
            },
        },
        {
            "id": "hold-and-fix",
            "losses": {
                "change-is-isolated": 5,
                "change-breaks-consumer": 0,
                "no-pending-change": 5,
            },
        },
        {
            "id": "abstain",
            "losses": {
                "change-is-isolated": 4,
                "change-breaks-consumer": 4,
                "no-pending-change": 4,
            },
        },
    ],
    "abstain_decision_id": "abstain",
}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ReferenceHostExecutor:
    """The only process-launching code in this bench. Never part of the lab.

    Every method returns a record of what happened, including when nothing
    could be observed: a missing binary is ``TOOL_ABSENT``, an overrun is
    ``TIMEOUT``, an unparseable language is ``UNSUPPORTED_LANGUAGE``. None of
    them is retried, and none falls back to another provider.
    """

    def __init__(
        self,
        root: Path,
        *,
        git_binary: str = "git",
        timeout_seconds: float = 30.0,
        unavailable_tools: frozenset[str] = frozenset(),
    ):
        self.root = root
        self.git_binary = git_binary
        self.timeout_seconds = timeout_seconds
        #: Tool ids this host declares it does not have. A real missing binary is
        #: detected on its own; this lets a loop meet an absent in-process tool too.
        self.unavailable_tools = unavailable_tools
        self.child_processes = 0

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.child_processes += 1
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, bench-owned temp directory
            argv,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._run([self.git_binary, *GIT_IDENTITY, *arguments])

    def git_identity(self) -> dict[str, str] | None:
        if shutil.which(self.git_binary) is None:
            return None
        version = self.git("--version").stdout.strip().removeprefix("git version ").strip()
        return {"tool_id": "git", "version": version or "unknown", "provider_id": "git"}

    @staticmethod
    def symbol_identity() -> dict[str, str]:
        return {
            "tool_id": "python-ast",
            "version": platform.python_version(),
            "provider_id": "python-ast",
        }

    @staticmethod
    def check_identity() -> dict[str, str]:
        return {
            "tool_id": "python-check",
            "version": platform.python_version(),
            "provider_id": "test-runner",
        }

    def _envelope(
        self, spec: HostProbeSpec, *, family: AgentFamily, mode: str, observation_id: str
    ) -> dict[str, Any]:
        return {
            "contract_version": "1.0.0",
            "observation_id": observation_id,
            "kind": spec.kind.value,
            "probe_id": spec.probe_id,
            "question_digest": question_digest(spec.question),
            "host": {"host_id": HOST_ID, "agent_family": family.value},
            "root_id": ROOT_ID,
            "mode": mode,
            "executed_by": "HOST_EXECUTOR",
            "tool": spec.tool.canonical_payload(),
            "providers_used": [spec.tool.provider_id],
            "observed_at": NOW,
            "covered_paths": list(spec.relative_paths),
            "evidence": [],
            "resources": {
                "child_processes": 0,
                "files_read": len(spec.relative_paths),
                "repeated_reads": 0,
                "internal_index_used": False,
                "cache_used": False,
            },
            "limits": [COVERAGE_LIMIT],
        }

    def observe(
        self, spec: HostProbeSpec, *, family: AgentFamily, mode: str, observation_id: str
    ) -> HostObservation:
        """Run the one tool a probe names and return what it actually did."""
        record = self._envelope(spec, family=family, mode=mode, observation_id=observation_id)
        self._declare_detail(spec, record)
        processes_before = self.child_processes
        started = time.monotonic()
        try:
            if spec.tool.tool_id in self.unavailable_tools:
                raise FileNotFoundError(spec.tool.tool_id)
            if spec.kind.value == "GIT_DIFF":
                self._git_diff(spec, record)
            elif spec.kind.value == "SYMBOL_NAVIGATION":
                self._symbol_definition(spec, record)
            elif spec.kind.value == "TARGETED_CHECK":
                self._targeted_check(spec, record)
            else:
                raise ValueError(f"the bench executor does not run {spec.kind.value}")
        except subprocess.TimeoutExpired:
            record["status"] = "TIMEOUT"
            self._clear_result(record)
        except FileNotFoundError:
            record["status"] = "TOOL_ABSENT"
            self._clear_result(record)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        record["resources"]["child_processes"] = self.child_processes - processes_before
        return admit_host_observation(record)

    @staticmethod
    def _declare_detail(spec: HostProbeSpec, record: dict[str, Any]) -> None:
        """The kind's own detail, still empty: a run that never starts keeps this shape."""
        if spec.kind.value == "GIT_DIFF":
            record["limits"].append("GIT_IDENTITY_IS_DECLARED")
            record["git_diff"] = {"base_identity": "0" * 40, "entries": []}
        elif spec.kind.value == "SYMBOL_NAVIGATION":
            record["symbol"] = {"relation": "DEFINITION", "language": "python"}
        elif spec.kind.value == "TARGETED_CHECK":
            record["limits"].append("CHECK_VERDICT_IS_HOST_DECLARED")
            record["check"] = {
                "command_digest": _digest(json.dumps(["python", spec.relative_paths[0]]).encode()),
                "verdict": None,
                "exit_code": None,
                "log_digest": None,
                "log_bytes": None,
            }

    @staticmethod
    def _clear_result(record: dict[str, Any]) -> None:
        record["evidence"] = []
        if "git_diff" in record:
            record["git_diff"]["entries"] = []
        if "check" in record:
            record["check"].update(verdict=None, exit_code=None, log_digest=None, log_bytes=None)

    def _git_diff(self, spec: HostProbeSpec, record: dict[str, Any]) -> None:
        head = self.git("rev-parse", "HEAD")
        record["git_diff"]["base_identity"] = head.stdout.strip() or "0" * 40
        record["declared_git_head"] = record["git_diff"]["base_identity"]
        listing = self.git("diff", "--name-status", "HEAD", "--", *spec.relative_paths)
        untracked = self.git(
            "ls-files", "--others", "--exclude-standard", "--", *spec.relative_paths
        )
        if head.returncode != 0 or listing.returncode != 0 or untracked.returncode != 0:
            record["status"] = "FAILED"
            return
        changes = {
            line.split("\t", 1)[1]: ("ADDED" if line.startswith("A") else "MODIFIED")
            for line in listing.stdout.splitlines()
            if "\t" in line and line[0] in "AM"
        }
        changes.update({line: "ADDED" for line in untracked.stdout.splitlines() if line})
        record["worktree_dirty"] = bool(changes)
        for relative_path in sorted(changes):
            data = (self.root / relative_path).read_bytes()
            record["git_diff"]["entries"].append(
                {
                    "relative_path": relative_path,
                    "change": changes[relative_path],
                    "post_image_digest": _digest(data),
                    "post_image_size": len(data),
                }
            )
        if changes:
            record["status"] = "OBSERVED"
        else:
            record["status"] = "EMPTY"
            record["limits"].append("EMPTY_IS_NOT_ABSENCE")

    def _symbol_definition(self, spec: HostProbeSpec, record: dict[str, Any]) -> None:
        if any(not relative_path.endswith(".py") for relative_path in spec.relative_paths):
            record["status"] = "UNSUPPORTED_LANGUAGE"
            return
        for relative_path in spec.relative_paths:
            data = (self.root / relative_path).read_bytes()
            for node in ast.walk(ast.parse(data.decode("utf-8"))):
                if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name == spec.anchor:
                    record["evidence"].append(
                        {
                            "relative_path": relative_path,
                            "byte_digest": _digest(data),
                            "size_bytes": len(data),
                            "line_start": node.lineno,
                            "line_end": node.lineno,
                            "generated": False,
                        }
                    )
        if record["evidence"]:
            record["status"] = "OBSERVED"
        else:
            record["status"] = "EMPTY"
            record["limits"].append("EMPTY_IS_NOT_ABSENCE")

    def _targeted_check(self, spec: HostProbeSpec, record: dict[str, Any]) -> None:
        completed = self._run([sys.executable, spec.relative_paths[0]])
        log = (completed.stdout + completed.stderr).encode("utf-8")[: 64 * 1024]
        # Exit 0 passes and exit 1 fails; anything else is the check itself breaking.
        verdict = {0: "PASSED", 1: "FAILED"}.get(completed.returncode, "ERRORED")
        record["check"].update(
            verdict=verdict,
            exit_code=completed.returncode,
            log_digest=_digest(log),
            log_bytes=len(log),
        )
        record["status"] = "OBSERVED"


class BenchHostRouter:
    """Stands in for the host's own router: the only party that accepts advice.

    ``accept_advice=False`` is a host that ignores the advisor entirely; the
    loop must then take the documented native path — stop on the best
    admissible decision — without the advisor gaining any say. An ``ESCALATE``
    verdict belongs to whoever holds the missing authority or proof: the bench
    records it and executes nothing.
    """

    def __init__(self, *, accept_advice: bool = True):
        self.accept_advice = accept_advice

    def decide(self, verdict: RouteVerdict) -> str:
        if verdict is RouteVerdict.ESCALATE:
            return "ESCALATED"
        if verdict is not RouteVerdict.ADVICE:
            return "NOT_ADVISED"
        return "ACCEPTED" if self.accept_advice else "IGNORED"


def build_repository(root: Path, executor: ReferenceHostExecutor) -> None:
    """A committed base, then an uncommitted change that breaks a consumer."""
    (root / "pricing.py").write_bytes(PRICING_BASE.encode("utf-8"))
    (root / "invoice.py").write_bytes(INVOICE.encode("utf-8"))
    (root / "check_invoice.py").write_bytes(CHECK.encode("utf-8"))
    for arguments in (
        ("init", "--quiet"),
        ("config", "core.autocrlf", "false"),
        ("add", *SOURCE_FILES),
        ("commit", "--quiet", "--no-gpg-sign", "-m", "bench base"),
    ):
        completed = executor.git(*arguments)
        if completed.returncode != 0:
            raise RuntimeError(f"bench repository setup failed: git {arguments[0]}")
    (root / "pricing.py").write_bytes(PRICING_CHANGED.encode("utf-8"))


def build_catalog(git_tool: dict[str, str]) -> HostProbeCatalog:
    return load_host_probe_catalog(
        {
            "contract_version": "1.0.0",
            "probes": [
                {
                    "probe_id": "diff-pricing",
                    "kind": "GIT_DIFF",
                    "relative_paths": ["pricing.py"],
                    "question": "did the working tree change pricing.py against HEAD?",
                    "tool": git_tool,
                    "outcome_by_path": {"pricing.py": "changed"},
                    "empty_outcome_id": "unchanged",
                },
                {
                    "probe_id": "define-legacy-consumer",
                    "kind": "SYMBOL_NAVIGATION",
                    "relative_paths": ["invoice.py"],
                    "question": "legacy_total",
                    "anchor": "legacy_total",
                    "tool": ReferenceHostExecutor.symbol_identity(),
                    "symbol_relation": "DEFINITION",
                    "outcome_by_path": {"invoice.py": "defined-in-invoice"},
                    "empty_outcome_id": "undefined",
                },
                {
                    "probe_id": "run-invoice-check",
                    "kind": "TARGETED_CHECK",
                    "relative_paths": ["check_invoice.py", "invoice.py", "pricing.py"],
                    "question": "does the invoice consumer still hold after the change?",
                    "tool": ReferenceHostExecutor.check_identity(),
                    "passed_outcome_id": "check-passes",
                    "failed_outcome_id": "check-fails",
                },
            ],
        }
    )


def _route(
    *,
    family: AgentFamily,
    model: DiagnosisModel,
    state: DiagnosisStateRevision,
    source_digest: str,
    capability_id: str,
    cost_ceiling: int,
    remaining_budget: int,
    observed_capabilities: list[str],
    advisor_present: bool,
    kill_switch_engaged: bool,
    request_overrides: dict[str, Any],
) -> Any:
    request = admit_route_request(
        {
            "contract_version": LAB_ROUTING_CONTRACT_VERSION,
            "request_digest": seal(
                "lab.host-bench.request.v1",
                {"state": state.state_seal(), "capability": capability_id, "family": family.value},
            ),
            "model_digest": model.model_seal(),
            "state_digest": state.state_seal(),
            "source_digest": source_digest,
            "host_id": HOST_ID,
            "agent_family": family.value,
            "scope": SCOPE,
            "candidate_capability_id": capability_id,
            "candidate_kind": CapabilityKind.TOOL.value,
            "requested_at": NOW,
            "expiry": EXPIRY,
            "cost_ceiling": cost_ceiling,
            "remaining_budget": remaining_budget,
            "advisor_present": advisor_present,
            "kill_switch_engaged": kill_switch_engaged,
            "review_required": False,
            "review_satisfied": False,
            "semctx_proof_required": False,
            "semctx_proof_satisfied": False,
            "explicit_missing_authority": False,
            "explicit_missing_precondition": False,
            "fallback_observation_id": None,
            **request_overrides,
        }
    )
    capabilities = admit_host_capability_snapshot(
        {
            "contract_version": LAB_ROUTING_CONTRACT_VERSION,
            "host_id": HOST_ID,
            "agent_family": family.value,
            "scope": SCOPE,
            "current_source_digest": source_digest,
            "snapshot_taken_at": CAPTURED_AT,
            "observed_capabilities": [
                {
                    "capability_id": capability,
                    "kind": CapabilityKind.TOOL.value,
                    "observed_at": CAPTURED_AT,
                    "expires_at": EXPIRY,
                }
                for capability in observed_capabilities
            ],
        }
    )
    return evaluate_route(request, capabilities, now=NOW)


def run_episode(
    root: Path,
    *,
    family: AgentFamily = AgentFamily.CLAUDE,
    total_budget: int = 6,
    router: BenchHostRouter | None = None,
    advisor_present: bool = True,
    kill_switch_engaged: bool = False,
    git_binary: str = "git",
    timeout_seconds: float = 30.0,
    withheld_capabilities: frozenset[str] = frozenset(),
    unavailable_tools: frozenset[str] = frozenset(),
    policy: ObservationPolicy | None = None,
    route_request_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one bounded episode in a fresh ``root`` and return its linked trace.

    ``route_request_overrides`` replaces fields of every route request, so a
    drill can present advice that is expired, computed on another source,
    addressed to another scope, or missing a required review or Semctx proof.
    """
    if root.exists():
        raise FileExistsError(f"refusing to reuse existing bench root: {root}")
    root.mkdir(parents=True)
    setup = ReferenceHostExecutor(root)
    git_tool = setup.git_identity()
    if git_tool is None:
        raise RuntimeError("the bench needs a real git binary to build its repository")
    build_repository(root, setup)

    router = router if router is not None else BenchHostRouter()
    executor = ReferenceHostExecutor(
        root,
        git_binary=git_binary,
        timeout_seconds=timeout_seconds,
        unavailable_tools=unavailable_tools,
    )
    model = load_model(MODEL_PAYLOAD)
    catalog = build_catalog(git_tool)
    session_policy = policy or admit_observation_policy(
        {"contract_version": "1.0.0", "mode": "SOURCE_AND_SYMBOLIC", "retired_providers": []}
    )
    snapshot = capture_source_snapshot(
        root,
        [Path(name) for name in SOURCE_FILES],
        host=HostBinding(host_id=HOST_ID, agent_family=family),
        root_id=ROOT_ID,
        captured_at=CAPTURED_AT,
        max_bytes_per_file=64 * 1024,
    )
    binding = derive_host_lab_binding(model, snapshot, catalog, session_policy)
    state = initial_state(model, state_id=f"bench-episode-{family.value}", binding=binding)
    history: list[DiagnosisStateRevision] = [state]
    pool = BudgetPoolState(total_budget=total_budget, reservations=(), charges=())
    capabilities = sorted(
        {spec.tool.tool_id for spec in catalog.probes} - set(withheld_capabilities)
    )
    unknown_probe_ids: set[str] = set()
    steps: list[dict[str, Any]] = []

    for index in range(len(model.probes) + 1):
        report = propose(
            model,
            state,
            budget=max(pool.available(), 0),
            horizon=2,
            expected_binding=binding,
            history=None if state.revision == 0 else history,
        )
        advice = {
            "recommended_action": report.recommended_action,
            "recommended_probe_id": report.recommended_probe_id,
            "stopping_decision_id": report.stopping_decision_id,
            "plan_value": report.plan_value,
            "plan_seal": seal("lab.host-bench.plan.v1", report.canonical_payload()),
        }
        probe_id = report.recommended_probe_id
        if report.recommended_action == "STOP" or probe_id is None:
            steps.append({"advice": advice, "routing": None, "host_decision": "NOT_ADVISED"})
            break
        if probe_id in unknown_probe_ids:
            # The 1.0.0 planner cannot plan around a probe it cannot obtain, so the
            # native path is to stop on the best admissible decision. Nothing is
            # retried and no other provider stands in.
            steps.append(
                {
                    "advice": advice,
                    "routing": None,
                    "host_decision": "NOT_ADVISED",
                    "stopped_because": "recommended_probe_unobtainable",
                }
            )
            break
        probe = model.probe_by_id(probe_id)
        spec = catalog.spec_by_id(probe_id)
        assert probe is not None  # named by propose() from this same model
        assert spec is not None  # the catalog covers every model probe

        decision = _route(
            family=family,
            model=model,
            state=state,
            source_digest=binding.source_scope_digest,
            capability_id=spec.tool.tool_id,
            cost_ceiling=probe.cost,
            remaining_budget=max(pool.available(), 0),
            observed_capabilities=capabilities,
            advisor_present=advisor_present,
            kill_switch_engaged=kill_switch_engaged,
            request_overrides=route_request_overrides or {},
        )
        routing = {
            "verdict": decision.verdict.value,
            "reason": (decision.abstain_reason or decision.escalate_reason or None),
            "decision_seal": decision.decision_seal,
        }
        if routing["reason"] is not None:
            routing["reason"] = routing["reason"].value
        host_decision = router.decide(decision.verdict)
        step: dict[str, Any] = {
            "advice": advice,
            "routing": routing,
            "host_decision": host_decision,
        }
        if host_decision != "ACCEPTED":
            steps.append(step)
            break

        reservation = BudgetReservation(
            reservation_id=f"reservation-{index}-{probe_id}",
            requested_maximum=probe.cost,
            reserved_at=NOW,
        )
        pool = reserve_budget(pool, reservation)
        observation = executor.observe(
            spec,
            family=family,
            mode=session_policy.mode.value,
            observation_id=f"observation-{index}-{probe_id}",
        )
        step["execution"] = {
            "observation_seal": observation.observation_seal(),
            "status": observation.status.value,
            "tool": observation.tool.canonical_payload(),
            "duration_ms": observation.duration_ms,
            "child_processes": observation.resources.child_processes,
            "planned_cost": probe.cost,
            "observed_cost": observation.observed_cost,
        }
        try:
            result = apply_host_observation(
                model,
                state,
                root=root,
                snapshot=snapshot,
                catalog=catalog,
                policy=session_policy,
                observation=observation,
                expected_host_id=HOST_ID,
                expected_agent_family=family,
                expected_root_id=ROOT_ID,
                verified_at=NOW,
                history=None if state.revision == 0 else history,
            )
        except ContractViolation as exc:
            pool = _settle(pool, reservation, SettlementOutcome.FAILED)
            step["result"] = {"refused": exc.code, "state_seal": state.state_seal()}
            steps.append(step)
            break

        unknown = result.outcome_id is None
        pool = _settle(
            pool, reservation, SettlementOutcome.FAILED if unknown else SettlementOutcome.SUCCEEDED
        )
        step["result"] = {
            "outcome_id": result.outcome_id,
            "unknown_reason": result.unknown_reason.value if result.unknown_reason else None,
            "conclusiveness": result.verification.conclusiveness.value,
            "verification_seal": result.verification.verification_seal(),
            "prior_state_seal": state.state_seal(),
            "state_seal": result.state.state_seal(),
        }
        steps.append(step)
        if unknown:
            # No retry and no other provider. The advisor plans again on what is
            # left of the budget; if it names this probe again, the loop stops.
            unknown_probe_ids.add(probe_id)
            continue
        state = result.state
        history.append(state)

    final = propose(
        model,
        state,
        budget=0,
        horizon=0,
        expected_binding=binding,
        history=None if state.revision == 0 else history,
    )
    return {
        "notice": BENCH_NOTICE,
        "non_authority_notice": LAB_NON_AUTHORITY_NOTICE,
        "agent_family": family.value,
        "observed_tools": {
            "git": git_tool,
            "symbols": ReferenceHostExecutor.symbol_identity(),
            "check": ReferenceHostExecutor.check_identity(),
        },
        "binding_source_scope_digest": binding.source_scope_digest,
        "final_state_revision": state.revision,
        "final_decision_id": final.stopping_decision_id,
        "total_budget": total_budget,
        "remaining_budget": pool.available(),
        "executor_child_processes": executor.child_processes,
        "steps": steps,
    }


def _settle(
    pool: BudgetPoolState, reservation: BudgetReservation, outcome: SettlementOutcome
) -> BudgetPoolState:
    # The bench measures no real cost, so every attempt — successful, unknown or
    # refused — is charged at its own reserved ceiling, never refunded.
    return settle_budget(
        pool,
        BudgetSettlement(
            reservation_id=reservation.reservation_id,
            outcome=outcome,
            actual_cost=None,
            settled_at=NOW,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--family", choices=["claude", "codex"], default="claude")
    arguments = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="lab-host-bench-") as temporary:
        trace = run_episode(Path(temporary) / "repository", family=AgentFamily(arguments.family))
    json.dump(trace, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

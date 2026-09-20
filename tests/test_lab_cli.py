"""CLI behaviour and the exit-code contract for ``python -m latent_compass.lab``."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from latent_compass.errors import ContractViolation
from latent_compass.lab import cli
from latent_compass.lab.cli import EXIT_OK, EXIT_REFUSED, main

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

SOURCE_SCOPE = "sha256:" + "5" * 64


def run(*argv: str) -> tuple[int, Any, Any]:
    out, err = StringIO(), StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return (
        code,
        json.loads(out.getvalue() or "null"),
        json.loads(err.getvalue() or "null"),
    )


def test_limits_reports_every_declared_bound() -> None:
    code, out, _ = run("limits")
    assert code == EXIT_OK
    assert "non_authority_notice" in out
    # The published bounds of the 1.0.0 contracts, as literals: a bound that is
    # relaxed or tightened without a contract version change turns this red.
    assert {section: values for section, values in out.items() if isinstance(values, dict)} == {
        "model": {
            "max_worlds": 64,
            "max_decisions": 32,
            "max_probes": 32,
            "max_outcomes_per_probe": 16,
            "max_required_evidence_per_decision": 8,
            "max_prior_weight": 1_000_000_000,
            "max_loss": 1_000_000_000,
            "max_probe_cost": 1_000_000_000,
        },
        "planning": {"max_budget": 1_000_000_000, "max_horizon": 6, "max_expansions": 200_000},
        "state": {"max_state_revision": 32},
        "cli": {"max_input_bytes": 8 * 1024 * 1024},
        "memory": {
            "max_facts": 4096,
            "max_claims": 4096,
            "max_events": 65_536,
            "max_premises_per_conjunction": 16,
            "max_supports_per_claim": 16,
        },
    }
    assert (out["lab_contract_version"], out["lab_memory_contract_version"]) == ("1.0.0", "1.0.0")
    # The constrained plan report is a second document beside 1.0.0, never a bump of it.
    assert out["lab_constrained_plan_contract_version"] == "1.1.0"


def test_validate_model_reports_the_model_seal() -> None:
    code, out, _ = run("validate-model", "--model", str(EXAMPLES / "lab-model.json"))
    assert code == EXIT_OK
    assert out["model_id"] == "lab-complementary-probes-example"
    assert out["model_seal"].startswith("sha256:")


def test_validate_model_refuses_a_malformed_payload(tmp_path: Path) -> None:
    bad = tmp_path / "bad-model.json"
    bad.write_text(json.dumps({"contract_version": "1.0.0"}), encoding="utf-8")
    code, _, err = run("validate-model", "--model", str(bad))
    assert code == EXIT_REFUSED
    assert err["error"] == "lab_contract_violation"
    assert "violations" in err["detail"]


def test_validate_model_refuses_non_json_input(tmp_path: Path) -> None:
    bad = tmp_path / "not-json.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run("validate-model", "--model", str(bad))
    assert code == EXIT_REFUSED
    assert err["error"] == "contract_violation"


def test_init_state_produces_the_models_declared_prior(tmp_path: Path) -> None:
    code, out, _ = run(
        "init-state",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state-id",
        "cli-episode-1",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_OK
    assert out["revision"] == 0
    assert out["posterior_weights"] == {
        "world-lo-lo": 1,
        "world-lo-hi": 1,
        "world-hi-lo": 1,
        "world-hi-hi": 1,
    }


def test_the_full_loop_runs_end_to_end_through_the_cli(tmp_path: Path) -> None:
    code, state0, _ = run(
        "init-state",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state-id",
        "cli-episode-2",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_OK
    state0_path = tmp_path / "state-0.json"
    state0_path.write_text(json.dumps(state0), encoding="utf-8")

    code, one_step, _ = run(
        "propose",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state0_path),
        "--budget",
        "2",
        "--horizon",
        "1",
    )
    assert code == EXIT_OK
    assert one_step["recommended_action"] == "STOP"
    assert one_step["plan_value"] == "3/1"

    code, two_step, _ = run(
        "propose",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state0_path),
        "--budget",
        "2",
        "--horizon",
        "2",
    )
    assert code == EXIT_OK
    assert two_step["recommended_action"] == "PROBE"
    assert two_step["recommended_probe_id"] == "check-a"
    assert two_step["plan_value"] == "2/1"

    code, state1, _ = run(
        "apply-observation",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state0_path),
        "--observation-id",
        "cli-obs-1",
        "--probe-id",
        "check-a",
        "--outcome-id",
        "a-lo",
        "--observed-at",
        "2026-09-18T00:00:00Z",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_OK
    assert state1["revision"] == 1
    assert state1["posterior_weights"] == {"world-lo-lo": 1, "world-lo-hi": 1}

    state1_path = tmp_path / "state-1.json"
    history_path = tmp_path / "history.json"
    state1_path.write_text(json.dumps(state1), encoding="utf-8")
    history_path.write_text(json.dumps([state0, state1]), encoding="utf-8")
    code, after, _ = run(
        "propose",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state1_path),
        "--history",
        str(history_path),
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
        "--budget",
        "1",
        "--horizon",
        "1",
    )
    assert code == EXIT_OK
    assert after["plan_value"] == "1/1"
    state1["posterior_weights"] = {"world-lo-lo": 1}
    state1_path.write_text(json.dumps(state1), encoding="utf-8")
    code, out, error = run(
        "propose",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state1_path),
        "--history",
        str(history_path),
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
        "--budget",
        "1",
        "--horizon",
        "1",
    )
    assert code == EXIT_REFUSED
    assert out is None
    assert error["error"] == "lab_replay_violation"


def _propose_at_revision_zero(tmp_path: Path, *extra: str) -> tuple[int, Any, Any]:
    code, state0, _ = run(
        "init-state",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state-id",
        "cli-episode-x",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_OK
    state_path = tmp_path / "state-0.json"
    state_path.write_text(json.dumps(state0), encoding="utf-8")
    return run(
        "propose",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state",
        str(state_path),
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
        "--budget",
        "2",
        "--horizon",
        "2",
        *extra,
    )


def test_propose_plans_around_an_unobtainable_probe_only_when_told_to(tmp_path: Path) -> None:
    # Without the flag: the 1.0.0 document, with exactly the keys it always had.
    code, plain, _ = _propose_at_revision_zero(tmp_path)
    assert code == EXIT_OK
    assert plain["contract_version"] == "1.0.0"
    assert "unobtainable_probe_ids" not in plain
    assert "exclusion_basis" not in plain
    assert len(plain) == 17
    assert (plain["recommended_probe_id"], plain["plan_value"]) == ("check-a", "2/1")

    # With it: the 1.1.0 document, which names what it was told and plans around it.
    code, constrained, _ = _propose_at_revision_zero(tmp_path, "--unobtainable-probe", "check-a")
    assert code == EXIT_OK
    assert constrained["contract_version"] == "1.1.0"
    assert constrained["unobtainable_probe_ids"] == ["check-a"]
    assert constrained["exclusion_basis"] == "HOST_DECLARED"
    assert (constrained["recommended_action"], constrained["plan_value"]) == ("STOP", "3/1")
    assert set(constrained) - set(plain) == {"unobtainable_probe_ids", "exclusion_basis"}


@pytest.mark.parametrize(
    ("flags", "error"),
    [
        (("--unobtainable-probe", "check-z"), "lab_unknown_reference"),
        (
            ("--unobtainable-probe", "check-a", "--unobtainable-probe", "check-a"),
            "lab_contract_violation",
        ),
    ],
)
def test_propose_refuses_an_unknown_or_repeated_unobtainable_probe(
    tmp_path: Path, flags: tuple[str, ...], error: str
) -> None:
    code, out, err = _propose_at_revision_zero(tmp_path, *flags)
    assert code == EXIT_REFUSED
    assert out is None
    assert err["error"] == error


def test_apply_observation_refuses_an_impossible_outcome(tmp_path: Path) -> None:
    code, state0, _ = run(
        "init-state",
        "--model",
        str(EXAMPLES / "lab-mandatory-evidence.json"),
        "--state-id",
        "cli-episode-3",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_OK
    state0_path = tmp_path / "state-0.json"
    state0_path.write_text(json.dumps(state0), encoding="utf-8")

    code, _, err = run(
        "apply-observation",
        "--host-id",
        "cli-host",
        "--agent-family",
        "claude",
        "--model",
        str(EXAMPLES / "lab-mandatory-evidence.json"),
        "--state",
        str(state0_path),
        "--observation-id",
        "cli-obs-1",
        "--probe-id",
        "probe-approval",
        "--outcome-id",
        "outcome-nonexistent",
        "--observed-at",
        "2026-09-18T00:00:00Z",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_REFUSED
    assert err["error"] == "lab_unknown_reference"


def test_lab_is_not_wired_into_the_legacy_cli() -> None:
    import latent_compass.cli as legacy_cli

    source = Path(legacy_cli.__file__).read_text(encoding="utf-8")
    assert "latent_compass.lab" not in source
    assert "from latent_compass import lab" not in source


def test_oversized_valid_json_is_refused_before_model_validation(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (8 * 1024 * 1024) + (EXAMPLES / "lab-model.json").read_bytes())
    code, out, error = run("validate-model", "--model", str(oversized))
    assert code == EXIT_REFUSED
    assert out is None
    assert error["error"] == "contract_violation"


def test_history_count_is_bounded_before_loading_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = tmp_path / "history.json"
    history.write_text(json.dumps([{}] * 34), encoding="utf-8")

    def unexpected_load(payload: object) -> Any:
        pytest.fail("oversized history reached state loading")

    monkeypatch.setattr(cli, "load_state", unexpected_load)
    with pytest.raises(ContractViolation, match="history exceeds"):
        cli._read_history(history)  # noqa: SLF001 - assert pre-validation bound


def test_invalid_cli_binding_is_a_structured_refusal() -> None:
    code, out, error = run(
        "init-state",
        "--model",
        str(EXAMPLES / "lab-model.json"),
        "--state-id",
        "cli-state",
        "--host-id",
        "x",
        "--agent-family",
        "codex",
        "--source-scope-digest",
        SOURCE_SCOPE,
    )
    assert code == EXIT_REFUSED
    assert out is None
    assert error["error"] == "contract_violation"


def test_route_advice_is_a_local_json_in_json_out_harness_surface(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    request = tmp_path / "route-request.json"
    capabilities = tmp_path / "capabilities.json"
    request.write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "request_digest": digest,
                "model_digest": digest,
                "state_digest": digest,
                "source_digest": digest,
                "host_id": "harness-host",
                "agent_family": "codex",
                "scope": "local-worktree",
                "candidate_capability_id": "literal-search",
                "candidate_kind": "TOOL",
                "requested_at": "2026-09-20T00:00:00Z",
                "expiry": "2026-09-20T00:10:00Z",
                "cost_ceiling": 1,
                "remaining_budget": 1,
                "advisor_present": True,
                "kill_switch_engaged": False,
                "review_required": False,
                "review_satisfied": False,
                "semctx_proof_required": False,
                "semctx_proof_satisfied": False,
                "explicit_missing_authority": False,
                "explicit_missing_precondition": False,
                "fallback_observation_id": None,
            }
        ),
        encoding="utf-8",
    )
    capabilities.write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "host_id": "harness-host",
                "agent_family": "codex",
                "scope": "local-worktree",
                "current_source_digest": digest,
                "snapshot_taken_at": "2026-09-20T00:00:00Z",
                "observed_capabilities": [
                    {
                        "capability_id": "literal-search",
                        "kind": "TOOL",
                        "observed_at": "2026-09-20T00:00:00Z",
                        "expires_at": "2026-09-20T00:10:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code, out, error = run(
        "route-advice",
        "--request",
        str(request),
        "--capabilities",
        str(capabilities),
        "--now",
        "2026-09-20T00:05:00Z",
    )

    assert code == EXIT_OK
    assert error is None
    assert out["verdict"] == "ADVICE"
    assert out["matched_capability_id"] == "literal-search"
    assert out["execution_authority"] is False
    assert out["empirical_claim"] is False

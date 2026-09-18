"""``python -m latent_compass.lab`` — the isolated active-diagnosis lab CLI.

Five verbs, all read-validate-compute-emit: ``limits``, ``validate-model``,
``init-state``, ``propose`` and ``apply-observation``. There is no verb that
executes, promotes, dispatches or reaches any external system — every command
either validates a payload, computes a value from one, or refuses. This CLI is
deliberately **not** wired into ``latent-compass``'s own command line; nothing
here is reachable from the legacy entry point.

Success writes one JSON document to stdout. Refusal writes one JSON document
to stderr carrying a stable ``error`` code and exits ``3``. No failure reaches
the caller as a traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from pydantic import ValidationError

from latent_compass.confined_io import read_confined_file
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation, LatentCompassError
from latent_compass.lab.contracts import (
    MAX_CLI_INPUT_BYTES,
    MAX_EXPANSIONS,
    MAX_STATE_REVISION,
    lab_limits,
)
from latent_compass.lab.model import load_model
from latent_compass.lab.planner import propose
from latent_compass.lab.state import (
    DiagnosisStateRevision,
    LabBinding,
    apply_observation,
    initial_state,
    load_state,
)

__all__ = ["build_parser", "main"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_REFUSED: Final = 3


def _emit(stream: TextIO, document: object) -> None:
    json.dump(document, stream, indent=2, sort_keys=True, ensure_ascii=False)
    stream.write("\n")


def _read_json(path: Path) -> object:
    try:
        absolute = path.absolute()
        text = read_confined_file(
            absolute.parent, absolute, max_bytes=MAX_CLI_INPUT_BYTES, what="lab JSON input"
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation(
            f"{path} is not valid UTF-8",
            detail={"path": str(path), "position": exc.start, "reason": exc.reason},
        ) from exc
    except OSError as exc:
        raise ContractViolation(
            f"cannot read {path}", detail={"path": str(path), "cause": exc.strerror}
        ) from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        detail: dict[str, object] = {"path": str(path), "reason": type(exc).__name__}
        if isinstance(exc, json.JSONDecodeError):
            detail.update({"line": exc.lineno, "column": exc.colno})
        raise ContractViolation(f"{path} is not valid JSON", detail=detail) from exc


def _read_history(path: Path | None) -> list[DiagnosisStateRevision] | None:
    if path is None:
        return None
    raw = _read_json(path)
    if not isinstance(raw, list):
        raise ContractViolation(
            "--history must be a JSON array of diagnosis state revisions",
            detail={"path": str(path), "received_type": type(raw).__name__},
        )
    if len(raw) > MAX_STATE_REVISION + 1:
        raise ContractViolation(
            "--history exceeds the maximum episode length",
            detail={"max_revisions": MAX_STATE_REVISION + 1},
        )
    return [load_state(entry) for entry in raw]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m latent_compass.lab",
        description=(
            "Isolated experimental active-diagnosis lab (ADR 0011). Validates and "
            "computes; never executes, promotes or reaches any external system."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("limits", help="print every bound this build enforces")

    validate = sub.add_parser("validate-model", help="validate one finite decision model")
    validate.add_argument("--model", required=True, type=Path)

    init = sub.add_parser(
        "init-state", help="revision 0: the model's declared prior, unfiltered by any observation"
    )
    init.add_argument("--model", required=True, type=Path)
    init.add_argument("--state-id", required=True)
    init.add_argument("--host-id", required=True)
    init.add_argument("--agent-family", required=True, choices=["codex", "claude", "other"])
    init.add_argument("--source-scope-digest", required=True)

    proposal = sub.add_parser(
        "propose", help="bounded exact value-of-observation planning from one state"
    )
    proposal.add_argument("--model", required=True, type=Path)
    proposal.add_argument("--state", required=True, type=Path)
    proposal.add_argument(
        "--history",
        type=Path,
        default=None,
        help="JSON array of the full replay history; required above revision 0",
    )
    proposal.add_argument("--host-id", required=True)
    proposal.add_argument("--agent-family", required=True, choices=["codex", "claude", "other"])
    proposal.add_argument("--source-scope-digest", required=True)
    proposal.add_argument("--budget", required=True, type=int)
    proposal.add_argument("--horizon", required=True, type=int)
    proposal.add_argument("--max-expansions", type=int, default=None)

    apply_command = sub.add_parser(
        "apply-observation", help="exactly filter one state's posterior by one observed outcome"
    )
    apply_command.add_argument("--model", required=True, type=Path)
    apply_command.add_argument("--state", required=True, type=Path)
    apply_command.add_argument(
        "--history",
        type=Path,
        default=None,
        help="JSON array of the full replay history; required above revision 0",
    )
    apply_command.add_argument("--observation-id", required=True)
    apply_command.add_argument("--probe-id", required=True)
    apply_command.add_argument("--outcome-id", required=True)
    apply_command.add_argument("--observed-at", required=True)
    apply_command.add_argument("--host-id", required=True)
    apply_command.add_argument(
        "--agent-family", required=True, choices=["codex", "claude", "other"]
    )
    apply_command.add_argument("--source-scope-digest", required=True)

    return parser


def _dispatch(args: argparse.Namespace, out: TextIO) -> int:
    if args.command == "limits":
        _emit(out, lab_limits())
        return EXIT_OK

    if args.command == "validate-model":
        model = load_model(_read_json(args.model))
        _emit(out, {"model_id": model.model_id, "model_seal": model.model_seal()})
        return EXIT_OK

    if args.command == "init-state":
        model = load_model(_read_json(args.model))
        binding = LabBinding(
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            source_scope_digest=args.source_scope_digest,
        )
        state = initial_state(model, state_id=args.state_id, binding=binding)
        _emit(out, state.canonical_payload())
        return EXIT_OK

    if args.command == "propose":
        model = load_model(_read_json(args.model))
        state = load_state(_read_json(args.state))
        history = _read_history(args.history)
        expected_binding = LabBinding(
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            source_scope_digest=args.source_scope_digest,
        )
        report = propose(
            model,
            state,
            budget=args.budget,
            horizon=args.horizon,
            expected_binding=expected_binding,
            history=history,
            max_expansions=MAX_EXPANSIONS if args.max_expansions is None else args.max_expansions,
        )
        _emit(out, report.canonical_payload())
        return EXIT_OK

    if args.command == "apply-observation":
        model = load_model(_read_json(args.model))
        state = load_state(_read_json(args.state))
        history = _read_history(args.history)
        expected_binding = LabBinding(
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            source_scope_digest=args.source_scope_digest,
        )
        updated = apply_observation(
            model,
            state,
            observation_id=args.observation_id,
            probe_id=args.probe_id,
            outcome_id=args.outcome_id,
            observed_at=args.observed_at,
            expected_binding=expected_binding,
            history=history,
        )
        _emit(out, updated.canonical_payload())
        return EXIT_OK

    raise AssertionError(f"unreachable command: {args.command!r}")  # pragma: no cover


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI and return its exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args, out)
    except (ContractViolation, LatentCompassError) as exc:
        _emit(err, exc.as_dict())
        return EXIT_REFUSED
    except ValidationError as exc:
        _emit(
            err,
            ContractViolation(
                "invalid lab command input",
                detail={
                    "violations": [
                        {"location": list(item["loc"]), "type": item["type"]}
                        for item in exc.errors(include_url=False, include_input=False)
                    ]
                },
            ).as_dict(),
        )
        return EXIT_REFUSED
    except OSError as exc:
        _emit(
            err,
            {"error": "os_error", "message": str(exc), "detail": {"errno": exc.errno}},
        )
        return EXIT_REFUSED

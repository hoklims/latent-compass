"""The ``latent-compass`` command line.

Everything the CLI can do is read, validate, record locally, or refuse. There
is no command that executes a recommendation, promotes a candidate, or reaches
any external system. The package imports no networking and no subprocess
machinery.

Write confinement
-----------------
Every durable write is confined to a root named on the command line, after
canonical resolution of both the root and the target. A traversal (``..``), a
sibling of the root, an absolute path elsewhere and a symlinked escape are all
refused, and an existing file is never silently overwritten. Emitting to stdout
writes nothing and needs no root.

Exit codes are part of the contract, so a caller can branch without parsing
prose:

===== ===========================================================
Code  Meaning
===== ===========================================================
``0`` the operation succeeded
``2`` usage error (argparse)
``3`` refused: contract violation, authority refusal, duplicate,
      closed epoch — the fail-closed family
``4`` integrity failure: the store does not reproduce
``5`` store or filesystem error
===== ===========================================================

Success writes one JSON document to stdout. Refusal writes one JSON document to
stderr carrying a stable ``error`` code. No failure reaches the user as a
traceback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from latent_compass import __version__
from latent_compass.authority import (
    Actor,
    LifecycleState,
    authority_boundary_snapshot,
    authorize_transition,
)
from latent_compass.episode import AgentFamily, load_episode
from latent_compass.errors import (
    AuthorityRefusal,
    ContractViolation,
    DuplicateEpisode,
    EpochClosed,
    IntegrityError,
    LatentCompassError,
    LedgerError,
)
from latent_compass.governance import deletion_semantics
from latent_compass.ledger import LedgerStore, utc_now
from latent_compass.protocol import (
    HoldoutLedger,
    evaluate,
    load_measurement_set,
    load_preregistration,
    load_verdict,
)

__all__ = ["build_parser", "main"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_REFUSED: Final = 3
EXIT_INTEGRITY: Final = 4
EXIT_STORE: Final = 5


def _emit(stream: TextIO, document: object) -> None:
    json.dump(document, stream, indent=2, sort_keys=True, ensure_ascii=False)
    stream.write("\n")


def _resolve_under(root: Path, target: Path, *, what: str, may_exist: bool = False) -> Path:
    """Resolve ``target`` and refuse it unless it lands strictly under ``root``.

    Both sides are resolved first, so ``..`` segments, a symlinked directory and
    an absolute path elsewhere are all reduced to the same question: is the
    resolved target inside the resolved root? A path equal to the root, or a
    mere string-prefix sibling such as ``<root>-other``, is refused too.

    ``may_exist`` is for durable *state* that a later run is meant to read back
    — the holdout usage record. Output artefacts keep the default and are never
    silently overwritten.
    """
    # A relative path resolves against the working directory, as every other CLI
    # does. Resolving it against the root instead would silently place the file
    # somewhere the caller did not name — confined, but not where they asked.
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise ContractViolation(
            f"{what} must be written inside the root named on the command line",
            detail={
                "what": what,
                "root": str(resolved_root),
                "requested": str(resolved_target),
            },
        )
    if resolved_target.exists() and not may_exist:
        raise ContractViolation(
            f"{what} already exists; refusing to overwrite it",
            detail={"what": what, "path": str(resolved_target)},
        )
    return resolved_target


def _write_atomically(path: Path, text: str) -> None:
    """Publish a durable new file atomically, never replacing a concurrent winner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".lc-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation(
            f"{path} is not valid UTF-8",
            detail={"path": str(path), "position": exc.start, "reason": exc.reason},
        ) from exc
    except OSError as exc:
        raise LedgerError(
            f"cannot read {path}", detail={"path": str(path), "cause": exc.strerror}
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractViolation(
            f"{path} is not valid JSON",
            detail={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latent-compass",
        description=(
            "Shadow-only episode ledger and governance contracts. "
            "Validates and records; never executes, promotes or mutates anything."
        ),
    )
    parser.add_argument("--version", action="version", version=f"latent-compass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a store bound to one host, family and epoch")
    init.add_argument("--root", required=True, type=Path)
    init.add_argument("--store-id", required=True)
    init.add_argument("--host-id", required=True)
    init.add_argument(
        "--agent-family", required=True, choices=[family.value for family in AgentFamily]
    )
    init.add_argument("--epoch", required=True)

    validate = sub.add_parser("validate", help="validate an episode file without recording it")
    validate.add_argument("--episode", required=True, type=Path)

    append = sub.add_parser("append", help="validate and atomically append an episode")
    append.add_argument("--root", required=True, type=Path)
    append.add_argument("--episode", required=True, type=Path)

    show = sub.add_parser("show", help="show one recorded episode")
    show.add_argument("--root", required=True, type=Path)
    show.add_argument("--episode-id", required=True)

    listing = sub.add_parser("list", help="bounded listing of recorded episodes")
    listing.add_argument("--root", required=True, type=Path)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)

    verify = sub.add_parser("verify", help="walk the integrity chain")
    verify.add_argument("--root", required=True, type=Path)

    replay = sub.add_parser("replay", help="deterministic replay from genesis")
    replay.add_argument("--root", required=True, type=Path)

    export = sub.add_parser("export", help="verified, sealed snapshot of the store")
    export.add_argument("--root", required=True, type=Path)
    export.add_argument(
        "--out", type=Path, help="destination, resolved strictly inside --root; stdout if omitted"
    )

    tombstone = sub.add_parser("tombstone", help="redact one payload, preserving the chain")
    tombstone.add_argument("--root", required=True, type=Path)
    tombstone.add_argument("--episode-id", required=True)
    tombstone.add_argument("--reason", required=True)

    abandon = sub.add_parser("abandon-epoch", help="close the epoch; preserve everything recorded")
    abandon.add_argument("--root", required=True, type=Path)
    abandon.add_argument("--reason", required=True)

    sub.add_parser("governance", help="print retention and deletion semantics")

    protocol = sub.add_parser("protocol", help="pre-registered evaluation protocol")
    protocol_sub = protocol.add_subparsers(dest="protocol_command", required=True)
    protocol_validate = protocol_sub.add_parser("validate", help="validate and seal a protocol")
    protocol_validate.add_argument("--file", required=True, type=Path)
    protocol_verdict = protocol_sub.add_parser(
        "verdict", help="score measurements deterministically"
    )
    protocol_verdict.add_argument("--protocol", required=True, type=Path)
    protocol_verdict.add_argument("--measurements", required=True, type=Path)
    protocol_verdict.add_argument(
        "--root", type=Path, help="writable root; required with --holdout-ledger"
    )
    protocol_verdict.add_argument(
        "--holdout-ledger", type=Path, help="usage record, resolved strictly inside --root"
    )
    protocol_verdict.add_argument(
        "--out", type=Path, help="destination for the verdict, inside --root; stdout if omitted"
    )

    authority = sub.add_parser("authority", help="inspect and exercise the authority boundary")
    authority_sub = authority.add_subparsers(dest="authority_command", required=True)
    authority_sub.add_parser("boundary", help="print the sealed authority boundary")
    transition = authority_sub.add_parser("transition", help="ask whether a move is authorised")
    transition.add_argument(
        "--from-state", required=True, choices=[state.value for state in LifecycleState]
    )
    transition.add_argument(
        "--to-state", required=True, choices=[state.value for state in LifecycleState]
    )
    transition.add_argument("--actor", required=True, choices=[actor.value for actor in Actor])
    transition.add_argument(
        "--protocol", type=Path, help="the pre-registered protocol file, as evidence"
    )
    transition.add_argument(
        "--verdict", type=Path, help="a sealed verdict file; its seal is recomputed here"
    )
    transition.add_argument(
        "--measurements", type=Path, help="the raw measurements from which the verdict was scored"
    )
    transition.add_argument(
        "--holdout-ledger",
        type=Path,
        help="durable holdout usage ledger carrying the matching consumption receipt",
    )
    transition.add_argument("--human-ack", action="store_true")

    return parser


def _dispatch(args: argparse.Namespace, stdout: TextIO) -> int:
    command: str = args.command

    if command == "init":
        store = LedgerStore.create(
            args.root,
            store_id=args.store_id,
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            epoch=args.epoch,
        )
        with store:
            _emit(stdout, {"created": store.binding().canonical_payload()})
        return EXIT_OK

    if command == "validate":
        episode = load_episode(_read_json(args.episode))
        _emit(
            stdout,
            {
                "valid": True,
                "episode_id": episode.episode_id,
                "schema_version": episode.schema_version,
                "content_seal": episode.content_seal(),
            },
        )
        return EXIT_OK

    if command == "append":
        episode = load_episode(_read_json(args.episode))
        with LedgerStore.open(args.root) as store:
            receipt = store.append(episode)
            _emit(stdout, {"appended": receipt.canonical_payload()})
        return EXIT_OK

    if command == "show":
        with LedgerStore.open(args.root) as store:
            _emit(stdout, {"record": store.get(args.episode_id).canonical_payload()})
        return EXIT_OK

    if command == "list":
        with LedgerStore.open(args.root) as store:
            records = store.list_records(limit=args.limit, offset=args.offset)
            _emit(
                stdout,
                {
                    "total": store.count(),
                    "limit": args.limit,
                    "offset": args.offset,
                    "records": [record.canonical_payload() for record in records],
                },
            )
        return EXIT_OK

    if command == "verify":
        with LedgerStore.open(args.root) as store:
            report = store.verify()
            _emit(stdout, {"integrity": report.canonical_payload()})
        return EXIT_OK if report.ok else EXIT_INTEGRITY

    if command == "replay":
        with LedgerStore.open(args.root) as store:
            _emit(stdout, {"replay": store.replay().canonical_payload()})
        return EXIT_OK

    if command == "export":
        destination = (
            _resolve_under(args.root, args.out, what="export destination")
            if args.out is not None
            else None
        )
        with LedgerStore.open(args.root) as store:
            document = store.export().canonical_payload()
        if destination is None:
            _emit(stdout, {"export": document})
            return EXIT_OK
        _write_atomically(
            destination, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        _emit(
            stdout,
            {
                "exported_to": str(destination),
                "export_seal": document["export_seal"],
                "episode_count": document["episode_count"],
            },
        )
        return EXIT_OK

    if command == "tombstone":
        with LedgerStore.open(args.root) as store:
            before = store.root_seal()
            stone = store.tombstone(args.episode_id, reason=args.reason)
            _emit(
                stdout,
                {
                    "tombstone": stone.canonical_payload(),
                    "root_seal_before": before,
                    "root_seal_after": store.root_seal(),
                },
            )
        return EXIT_OK

    if command == "abandon-epoch":
        with LedgerStore.open(args.root) as store:
            _emit(stdout, {"binding": store.abandon_epoch(reason=args.reason).canonical_payload()})
        return EXIT_OK

    if command == "governance":
        _emit(stdout, {"deletion_semantics": deletion_semantics()})
        return EXIT_OK

    if command == "protocol":
        return _dispatch_protocol(args, stdout)

    if command == "authority":
        return _dispatch_authority(args, stdout)

    raise AssertionError(f"unhandled command {command!r}")  # pragma: no cover


def _dispatch_protocol(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.protocol_command == "validate":
        protocol = load_preregistration(_read_json(args.file))
        _emit(
            stdout,
            {
                "valid": True,
                "protocol_id": protocol.protocol_id,
                "revision": protocol.revision,
                "epoch": protocol.epoch,
                "protocol_seal": protocol.protocol_seal(),
                "holdout_corpus_seal": protocol.holdout_corpus_seal(),
            },
        )
        return EXIT_OK

    if (args.holdout_ledger is not None or args.out is not None) and args.root is None:
        raise ContractViolation(
            "--root is required whenever this command writes a file",
            detail={"requires": "--root", "for": "--holdout-ledger and --out"},
        )
    ledger_path = (
        _resolve_under(args.root, args.holdout_ledger, what="holdout ledger", may_exist=True)
        if args.holdout_ledger is not None
        else None
    )
    destination = (
        _resolve_under(args.root, args.out, what="verdict destination")
        if args.out is not None
        else None
    )

    protocol = load_preregistration(_read_json(args.protocol))
    measurements = load_measurement_set(_read_json(args.measurements))
    ledger = HoldoutLedger(ledger_path) if ledger_path is not None else None
    verdict = evaluate(
        protocol,
        measurements,
        holdout_ledger=ledger,
        consumed_at=utc_now() if ledger is not None else None,
    )
    document = verdict.canonical_payload()
    if destination is not None:
        _write_atomically(
            destination, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
    _emit(stdout, {"verdict": document})
    return EXIT_OK


def _dispatch_authority(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.authority_command == "boundary":
        _emit(stdout, {"boundary": authority_boundary_snapshot()})
        return EXIT_OK

    protocol = load_preregistration(_read_json(args.protocol)) if args.protocol else None
    measurements = (
        load_measurement_set(_read_json(args.measurements)) if args.measurements else None
    )
    verdict = load_verdict(_read_json(args.verdict)) if args.verdict else None
    holdout_ledger = HoldoutLedger(args.holdout_ledger) if args.holdout_ledger else None
    authorization = authorize_transition(
        from_state=LifecycleState(args.from_state),
        to_state=LifecycleState(args.to_state),
        actor=Actor(args.actor),
        protocol=protocol,
        measurements=measurements,
        verdict=verdict,
        holdout_ledger=holdout_ledger,
        human_acknowledged=args.human_ack,
    )
    _emit(stdout, {"authorized": authorization.canonical_payload()})
    return EXIT_OK


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
    except (ContractViolation, AuthorityRefusal, DuplicateEpisode, EpochClosed) as exc:
        _emit(err, exc.as_dict())
        return EXIT_REFUSED
    except IntegrityError as exc:
        _emit(err, exc.as_dict())
        return EXIT_INTEGRITY
    except LedgerError as exc:
        _emit(err, exc.as_dict())
        return EXIT_STORE
    except LatentCompassError as exc:  # pragma: no cover - defensive catch-all
        _emit(err, exc.as_dict())
        return EXIT_REFUSED
    except OSError as exc:
        # A filesystem failure is a store error, never a traceback.
        _emit(
            err,
            {
                "error": "filesystem_error",
                "message": "a filesystem operation failed",
                "detail": {"cause": exc.strerror, "path": getattr(exc, "filename", None)},
            },
        )
        return EXIT_STORE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

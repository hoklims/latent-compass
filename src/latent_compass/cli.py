"""The ``latent-compass`` command line.

Everything the CLI can do is read, validate, record locally, run a confined
offline benchmark, or refuse. There is no command that executes an operational
recommendation, promotes a candidate, or reaches any external system. The
package imports no networking and no subprocess machinery.

Write confinement
-----------------
Every durable write is lexically planned beneath a root named on the command
line, then executed by traversing filesystem handles from the local volume root
on Windows or ``/`` on POSIX. A traversal (``..``), sibling, external absolute
path, symlink/reparse escape, ambiguous Windows name and existing destination
are refused. Windows UNC and device namespaces are outside this local surface.
Emitting to stdout writes nothing and needs no root.

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
import sys
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
from latent_compass.benchmark import (
    BaselineId,
    CorpusProvenance,
    build_manifest,
    create_holdout_plan,
    load_benchmark_report,
    load_benchmark_spec,
    load_corpus_manifest,
    run_benchmark,
    verify_manifest,
    verify_report,
)
from latent_compass.benchmark.spec import (
    require_spec_matches_manifest,
    require_spec_matches_protocol,
)
from latent_compass.confined_io import plan_confined_target, write_new_file
from latent_compass.decision_memory import (
    DecisionBinding,
    DecisionMemoryStore,
    DecisionOriginKind,
    StrategicDecisionRecord,
    admission_limits,
    admit_strategic_decision,
)
from latent_compass.decision_reconciliation import (
    ReconciliationJournal,
    reconciliation_limits,
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
from latent_compass.pairwise_capture import load_judgeable_projection
from latent_compass.prospective_collection import (
    AbortReason,
    ProspectiveCollectionJournal,
    admit_prospective_plan,
    plan_exact_one_sided_binomial,
    prospective_collection_limits,
)
from latent_compass.protocol import (
    HoldoutLedger,
    Split,
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
    """Plan ``target`` and refuse it unless it is lexically below ``root``.

    Normalisation removes ``..`` and rejects an absolute path elsewhere, a path
    equal to the root, and string-prefix siblings such as ``<root>-other``.
    Links are intentionally not resolved here: the handle-relative writer
    rejects symlinks/reparse points without a check/use gap.

    ``may_exist`` is for durable *state* that a later run is meant to read back
    — the holdout usage record. Output artefacts keep the default and are never
    silently overwritten.
    """
    # A relative path resolves against the working directory, as every other CLI
    # does. Resolving it against the root instead would silently place the file
    # somewhere the caller did not name — confined, but not where they asked.
    planned_target = plan_confined_target(root, target, what=what)
    if planned_target.exists() and not may_exist:
        raise ContractViolation(
            f"{what} already exists; refusing to overwrite it",
            detail={"what": what, "path": str(planned_target)},
        )
    return planned_target


def _write_atomically(
    root: Path,
    path: Path | str,
    text: str | None = None,
    *,
    what: str = "destination",
) -> None:
    """Publish a durable new file atomically, never replacing a concurrent winner."""
    if text is None:
        # Compatibility for the historical private helper used by regression
        # tests. CLI call sites always pass the explicit authority root.
        destination = root
        payload = str(path)
        authority_root = destination.parent
        compatibility_call = True
    else:
        destination = Path(path)
        payload = text
        authority_root = root
        compatibility_call = False
    try:
        write_new_file(authority_root, destination, payload.encode("utf-8"), what=what)
    except ContractViolation as exc:
        if compatibility_call and destination.exists():
            raise FileExistsError(str(destination)) from exc
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
    except (json.JSONDecodeError, RecursionError) as exc:
        detail: dict[str, object] = {"path": str(path), "reason": type(exc).__name__}
        if isinstance(exc, json.JSONDecodeError):
            detail.update({"line": exc.lineno, "column": exc.colno})
        raise ContractViolation(
            f"{path} is not valid JSON",
            detail=detail,
        ) from exc


def _add_memory_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The HOK-243 pre-action strategic decision memory surface.

    There is deliberately no command that selects a route, records an outcome,
    scores a candidate or authorises anything. Every subcommand either creates a
    store, appends a durable event, reads state back, or refuses.
    """
    memory = sub.add_parser(
        "memory",
        help="opt-in strategic decision memory: remembers decisions, never takes them",
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)

    initialise = memory_sub.add_parser(
        "init", help="create a decision memory bound to one host, family and epoch"
    )
    initialise.add_argument("--root", required=True, type=Path)
    initialise.add_argument("--store-id", required=True)
    initialise.add_argument("--host-id", required=True)
    initialise.add_argument(
        "--agent-family", required=True, choices=[family.value for family in AgentFamily]
    )
    initialise.add_argument("--epoch", required=True)

    for name, help_text in (
        ("append", "admit and append the initial revision of one strategic decision"),
        ("revise", "admit and append a revision that extends the exact current head"),
    ):
        command = memory_sub.add_parser(name, help=help_text)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--record", required=True, type=Path)
        command.add_argument(
            "--expected-generation",
            type=int,
            help="refuse unless the store is at exactly this generation",
        )

    status = memory_sub.add_parser("status", help="binding, generation, record count, root seal")
    status.add_argument("--root", required=True, type=Path)

    verify = memory_sub.add_parser("verify", help="walk the whole event chain")
    verify.add_argument("--root", required=True, type=Path)

    listing = memory_sub.add_parser("list", help="bounded active state as of an explicit instant")
    listing.add_argument("--root", required=True, type=Path)
    listing.add_argument("--as-of", help="UTC instant, YYYY-MM-DDTHH:MM:SSZ; now if omitted")
    listing.add_argument(
        "--origin",
        default=DecisionOriginKind.NATIVE.value,
        choices=[kind.value for kind in DecisionOriginKind],
        help="native and imported decisions are listed separately and never merged",
    )
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)

    show = memory_sub.add_parser("show", help="the active head revision of one decision")
    show.add_argument("--root", required=True, type=Path)
    show.add_argument("--decision-id", required=True)
    show.add_argument("--as-of", help="UTC instant, YYYY-MM-DDTHH:MM:SSZ; now if omitted")

    revoke = memory_sub.add_parser(
        "revoke", help="append the durable statement that a decision no longer holds"
    )
    revoke.add_argument("--root", required=True, type=Path)
    revoke.add_argument("--decision-id", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--revoked-by", required=True)
    revoke.add_argument("--expected-generation", type=int)

    tombstone = memory_sub.add_parser(
        "tombstone", help="drop one revision's payload, preserving the row and the chain"
    )
    tombstone.add_argument("--root", required=True, type=Path)
    tombstone.add_argument("--decision-id", required=True)
    tombstone.add_argument("--revision", required=True, type=int)
    tombstone.add_argument("--reason", required=True)
    tombstone.add_argument("--expected-generation", type=int)

    export = memory_sub.add_parser(
        "export-transfer", help="seal one decision for one explicitly named destination"
    )
    export.add_argument("--root", required=True, type=Path)
    export.add_argument("--decision-id", required=True)
    export.add_argument("--to-host-id", required=True)
    export.add_argument(
        "--to-agent-family", required=True, choices=[family.value for family in AgentFamily]
    )
    export.add_argument("--to-store-id", required=True)
    export.add_argument("--to-epoch", required=True)
    export.add_argument("--exported-by", required=True)
    export.add_argument(
        "--out", type=Path, help="destination, resolved strictly inside --root; stdout if omitted"
    )

    import_transfer = memory_sub.add_parser(
        "import-transfer", help="admit one sealed envelope as FOREIGN_READ_ONLY content"
    )
    import_transfer.add_argument("--root", required=True, type=Path)
    import_transfer.add_argument("--envelope", required=True, type=Path)
    import_transfer.add_argument("--expected-generation", type=int)

    memory_sub.add_parser("limits", help="print what admission bounds and what it refuses")

    abandon = memory_sub.add_parser(
        "abandon-epoch", help="close the epoch; preserve everything recorded"
    )
    abandon.add_argument("--root", required=True, type=Path)
    abandon.add_argument("--reason", required=True)


def _add_reconcile_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The HOK-244 post-action reconciliation surface.

    Every verb is an *evidence* verb: create a journal, append an observation,
    read state back, replay a comparison, close an epoch, or refuse. There is
    deliberately no ``select``, ``decide``, ``authorize``, ``promote`` or ``learn``
    subcommand, and no flag that would express one. ``revise`` appends a
    correction or a disagreement; it never edits what is already recorded, and it
    never touches the decision memory.
    """
    reconcile = sub.add_parser(
        "reconcile",
        help="post-action evidence journal: records observations, never judges or authorises",
    )
    reconcile_sub = reconcile.add_subparsers(dest="reconcile_command", required=True)

    initialise = reconcile_sub.add_parser(
        "init", help="create a reconciliation journal bound to one host, family and epoch"
    )
    initialise.add_argument("--root", required=True, type=Path)
    initialise.add_argument("--store-id", required=True)
    initialise.add_argument("--host-id", required=True)
    initialise.add_argument(
        "--agent-family", required=True, choices=[family.value for family in AgentFamily]
    )
    initialise.add_argument("--epoch", required=True)

    for name, help_text in (
        ("append", "admit and append the initial reconciliation of one pre-action revision"),
        ("revise", "append a correction or a disagreement extending the exact current head"),
    ):
        command = reconcile_sub.add_parser(name, help=help_text)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--reconciliation", required=True, type=Path)
        command.add_argument(
            "--decision-record",
            required=True,
            type=Path,
            help=("exact pre-action record required to verify the declared preimage before append"),
        )
        command.add_argument(
            "--expected-generation",
            type=int,
            help="refuse unless the journal is at exactly this generation",
        )

    status = reconcile_sub.add_parser(
        "status", help="binding, generation, entry count, reconciliation count, root seal"
    )
    status.add_argument("--root", required=True, type=Path)

    verify = reconcile_sub.add_parser("verify", help="walk the whole entry chain")
    verify.add_argument("--root", required=True, type=Path)

    listing = reconcile_sub.add_parser("list", help="bounded listing of head revisions")
    listing.add_argument("--root", required=True, type=Path)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)

    show = reconcile_sub.add_parser(
        "show", help="one reconciliation revision, with its whole revision history"
    )
    show.add_argument("--root", required=True, type=Path)
    show.add_argument("--reconciliation-id", required=True)
    show.add_argument("--revision", type=int, help="the head revision if omitted")

    replay = reconcile_sub.add_parser(
        "replay",
        help="pre-action evidence beside the observation, per dimension; no score, no verdict",
    )
    replay.add_argument("--root", required=True, type=Path)
    replay.add_argument("--reconciliation-id", required=True)
    replay.add_argument(
        "--decision-record",
        required=True,
        type=Path,
        help="the exact pre-action record this reconciliation names",
    )
    replay.add_argument("--revision", type=int, help="the head revision if omitted")

    reconcile_sub.add_parser("limits", help="print what admission bounds, refuses and disclaims")

    abandon = reconcile_sub.add_parser(
        "abandon-epoch", help="close the epoch; preserve everything recorded"
    )
    abandon.add_argument("--root", required=True, type=Path)
    abandon.add_argument("--reason", required=True)


def _add_shadow_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The preregistered prospective collection operator surface."""
    shadow = sub.add_parser(
        "shadow",
        help="preregister and collect diagnostic cases without operational influence",
    )
    commands = shadow.add_subparsers(dest="shadow_command", required=True)

    power = commands.add_parser("power", help="compute the exact preregistered sample target")
    power.add_argument("--p0", required=True, type=float)
    power.add_argument("--p1", required=True, type=float)
    power.add_argument("--alpha", required=True, type=float)
    power.add_argument("--target-power", required=True, type=float)
    power.add_argument("--max-enrollments", required=True, type=int)
    power.add_argument("--clustering-inflation", required=True, type=float)

    validate = commands.add_parser("validate-plan", help="admit and seal a plan without writing")
    validate.add_argument("--plan", required=True, type=Path)

    initialise = commands.add_parser("init", help="create a journal from one admitted sealed plan")
    initialise.add_argument("--root", required=True, type=Path)
    initialise.add_argument("--plan", required=True, type=Path)

    start = commands.add_parser("start", help="move a sealed plan into collection")
    start.add_argument("--root", required=True, type=Path)
    start.add_argument("--expected-generation", type=int)

    enroll = commands.add_parser("enroll", help="append one exact qualifying HOK-243 decision")
    enroll.add_argument("--root", required=True, type=Path)
    enroll.add_argument("--decision-record", required=True, type=Path)
    enroll.add_argument("--stratum", required=True)
    enroll.add_argument("--expected-generation", type=int)

    reconcile = commands.add_parser(
        "reconcile", help="terminalize from the current verified HOK-244 journal tail"
    )
    reconcile.add_argument("--root", required=True, type=Path)
    reconcile.add_argument("--case-id", required=True)
    reconcile.add_argument("--decision-record", required=True, type=Path)
    reconcile.add_argument("--source-root", required=True, type=Path)
    reconcile.add_argument("--reconciliation-id", required=True)
    reconcile.add_argument("--revision", type=int)
    reconcile.add_argument("--expected-generation", type=int)

    terminal = commands.add_parser(
        "terminal", help="record cancellation or loss to follow-up in the denominator"
    )
    terminal.add_argument("--root", required=True, type=Path)
    terminal.add_argument("--case-id", required=True)
    terminal.add_argument("--state", required=True, choices=("cancelled", "lost-to-followup"))
    terminal.add_argument("--reason", required=True)
    terminal.add_argument("--expected-generation", type=int)

    for name, help_text in (
        ("status", "show lifecycle state and denominator counts"),
        ("close", "apply only the preregistered calendar and denominator stop rules"),
        ("verify", "replay the complete journal and semantic state machine"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--root", required=True, type=Path)
        if name == "close":
            command.add_argument("--expected-generation", type=int)

    abort = commands.add_parser(
        "abort", help="terminalize only for an integrity or security failure"
    )
    abort.add_argument("--root", required=True, type=Path)
    abort.add_argument("--reason", required=True, choices=("integrity-failure", "security-failure"))
    abort.add_argument("--expected-generation", type=int)

    for name in ("manifest", "report"):
        command = commands.add_parser(name, help=f"publish the terminal diagnostic {name}")
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--out", type=Path)

    commands.add_parser("limits", help="print admission bounds and explicit non-capabilities")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latent-compass",
        description=(
            "Shadow-only episode ledger, governance contracts and offline benchmark. "
            "Never executes an operational recommendation, promotes or mutates externally."
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

    pairwise = sub.add_parser(
        "pairwise", help="capture judgeable pre-action projections without selecting"
    )
    pairwise_sub = pairwise.add_subparsers(dest="pairwise_command", required=True)
    pairwise_capture = pairwise_sub.add_parser(
        "capture", help="validate and atomically publish one pre-action sidecar"
    )
    pairwise_capture.add_argument("--projection", required=True, type=Path)
    pairwise_capture.add_argument("--root", required=True, type=Path)
    pairwise_capture.add_argument("--out", required=True, type=Path)

    _add_memory_parser(sub)
    _add_reconcile_parser(sub)
    _add_shadow_parser(sub)

    benchmark = sub.add_parser("benchmark", help="the HOK-188 offline baseline benchmark")
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)

    manifest = benchmark_sub.add_parser("manifest", help="build or verify a corpus manifest")
    manifest_sub = manifest.add_subparsers(dest="manifest_command", required=True)

    manifest_build = manifest_sub.add_parser(
        "build", help="derive a manifest from the real split files"
    )
    manifest_build.add_argument("--root", required=True, type=Path)
    manifest_build.add_argument("--corpus-dir", required=True, type=Path)
    manifest_build.add_argument("--corpus-id", required=True)
    manifest_build.add_argument("--corpus-version", required=True)
    manifest_build.add_argument(
        "--train", required=True, help="TRAIN file, relative to --corpus-dir"
    )
    manifest_build.add_argument(
        "--validation", required=True, help="VALIDATION file, relative to --corpus-dir"
    )
    manifest_build.add_argument(
        "--holdout", required=True, help="HOLDOUT file, relative to --corpus-dir"
    )
    manifest_build.add_argument("--origin", required=True)
    manifest_build.add_argument("--licence", required=True)
    manifest_build.add_argument("--description", required=True)
    manifest_build.add_argument(
        "--synthetic",
        action="store_true",
        help="declare the corpus synthetic; omitting it declares it is not",
    )
    manifest_build.add_argument("--out", required=True, type=Path)

    manifest_verify = manifest_sub.add_parser(
        "verify", help="recompute every seal from the real split files"
    )
    manifest_verify.add_argument("--corpus-dir", required=True, type=Path)
    manifest_verify.add_argument("--manifest", required=True, type=Path)

    spec = benchmark_sub.add_parser("spec", help="the benchmark specification")
    spec_sub = spec.add_subparsers(dest="spec_command", required=True)
    spec_validate = spec_sub.add_parser("validate", help="validate and seal a benchmark spec")
    spec_validate.add_argument("--spec", required=True, type=Path)
    spec_validate.add_argument("--protocol", required=True, type=Path)
    spec_validate.add_argument("--manifest", required=True, type=Path)

    run = benchmark_sub.add_parser("run", help="run the four baselines on VALIDATION")
    run.add_argument("--spec", required=True, type=Path)
    run.add_argument("--protocol", required=True, type=Path)
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--corpus-dir", required=True, type=Path)
    run.add_argument("--root", type=Path, help="writable root; required with --out")
    run.add_argument(
        "--out", type=Path, help="destination for the report, inside --root; stdout if omitted"
    )

    benchmark_verify = benchmark_sub.add_parser(
        "verify", help="re-execute the baselines and compare the whole report"
    )
    benchmark_verify.add_argument("--report", required=True, type=Path)
    benchmark_verify.add_argument("--spec", required=True, type=Path)
    benchmark_verify.add_argument("--protocol", required=True, type=Path)
    benchmark_verify.add_argument("--manifest", required=True, type=Path)
    benchmark_verify.add_argument("--corpus-dir", required=True, type=Path)

    holdout = benchmark_sub.add_parser(
        "holdout", help="pre-HOK-190 holdout workflow (planning only)"
    )
    holdout_sub = holdout.add_subparsers(dest="holdout_command", required=True)
    holdout_plan = holdout_sub.add_parser(
        "plan", help="freeze one validation CONTINUE baseline before holdout access"
    )
    holdout_plan.add_argument("--report", required=True, type=Path)
    holdout_plan.add_argument("--spec", required=True, type=Path)
    holdout_plan.add_argument("--protocol", required=True, type=Path)
    holdout_plan.add_argument("--manifest", required=True, type=Path)
    holdout_plan.add_argument("--corpus-dir", required=True, type=Path)
    holdout_plan.add_argument(
        "--baseline", required=True, choices=[identity.value for identity in BaselineId]
    )
    holdout_plan.add_argument("--root", required=True, type=Path)
    holdout_plan.add_argument("--out", required=True, type=Path)

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
            args.root,
            destination,
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            what="export destination",
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

    if command == "pairwise":
        return _dispatch_pairwise(args, stdout)

    if command == "memory":
        return _dispatch_memory(args, stdout)

    if command == "reconcile":
        return _dispatch_reconcile(args, stdout)

    if command == "shadow":
        return _dispatch_shadow(args, stdout)

    if command == "benchmark":
        return _dispatch_benchmark(args, stdout)

    if command == "authority":
        return _dispatch_authority(args, stdout)

    raise AssertionError(f"unhandled command {command!r}")  # pragma: no cover


def _publish_shadow_document(
    args: argparse.Namespace,
    stdout: TextIO,
    *,
    name: str,
    document: dict[str, object],
) -> int:
    if args.out is None:
        _emit(stdout, {name: document})
        return EXIT_OK
    destination = _resolve_under(args.root, args.out, what=f"shadow {name} destination")
    encoded = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_atomically(
        args.root,
        destination,
        encoded,
        what=f"shadow {name} destination",
    )
    _emit(stdout, {"published_to": str(destination), f"{name}_seal": document[f"{name}_seal"]})
    return EXIT_OK


def _dispatch_shadow(args: argparse.Namespace, stdout: TextIO) -> int:
    command: str = args.shadow_command
    if command == "power":
        result = plan_exact_one_sided_binomial(
            p0=args.p0,
            p1=args.p1,
            alpha=args.alpha,
            target_power=args.target_power,
            max_enrollments=args.max_enrollments,
            clustering_inflation=args.clustering_inflation,
        )
        _emit(stdout, {"power": result.canonical_payload()})
        return EXIT_OK

    if command == "validate-plan":
        plan = admit_prospective_plan(_read_json(args.plan))
        _emit(
            stdout,
            {
                "valid": True,
                "plan_id": plan.plan_id,
                "plan_seal": plan.plan_seal(),
                "power_status": plan.power.status.value,
            },
        )
        return EXIT_OK

    if command == "init":
        plan = admit_prospective_plan(_read_json(args.plan))
        with ProspectiveCollectionJournal.create(args.root, plan=plan, clock=utc_now) as journal:
            _emit(stdout, {"created": journal.status().canonical_payload()})
        return EXIT_OK

    if command == "limits":
        _emit(stdout, {"limits": prospective_collection_limits()})
        return EXIT_OK

    if command == "start":
        with ProspectiveCollectionJournal.open(args.root) as journal:
            status = journal.start(
                clock=utc_now,
                expected_generation=args.expected_generation,
            )
            _emit(stdout, {"status": status.canonical_payload()})
        return EXIT_OK

    if command == "enroll":
        decision = admit_strategic_decision(_read_json(args.decision_record))
        with ProspectiveCollectionJournal.open(args.root) as journal:
            receipt = journal.enroll(
                decision,
                stratum=args.stratum,
                expected_generation=args.expected_generation,
                clock=utc_now,
            )
            _emit(stdout, {"enrolled": receipt.canonical_payload()})
        return EXIT_OK

    if command == "reconcile":
        decision = admit_strategic_decision(_read_json(args.decision_record))
        with (
            ReconciliationJournal.open(args.source_root) as source,
            ProspectiveCollectionJournal.open(args.root) as journal,
        ):
            receipt = journal.reconcile(
                args.case_id,
                decision_record=decision,
                source_journal=source,
                reconciliation_id=args.reconciliation_id,
                reconciliation_revision=args.revision,
                expected_generation=args.expected_generation,
                clock=utc_now,
            )
            _emit(stdout, {"terminal": receipt.canonical_payload()})
        return EXIT_OK

    if command == "terminal":
        with ProspectiveCollectionJournal.open(args.root) as journal:
            if args.state == "cancelled":
                receipt = journal.mark_cancelled(
                    args.case_id,
                    reason=args.reason,
                    expected_generation=args.expected_generation,
                    clock=utc_now,
                )
            else:
                receipt = journal.mark_lost_to_followup(
                    args.case_id,
                    reason=args.reason,
                    expected_generation=args.expected_generation,
                    clock=utc_now,
                )
            _emit(stdout, {"terminal": receipt.canonical_payload()})
        return EXIT_OK

    if command == "status":
        with ProspectiveCollectionJournal.open(args.root) as journal:
            _emit(stdout, {"status": journal.status().canonical_payload()})
        return EXIT_OK

    if command == "close":
        with ProspectiveCollectionJournal.open(args.root) as journal:
            status = journal.close_collection(
                expected_generation=args.expected_generation,
                clock=utc_now,
            )
            _emit(stdout, {"status": status.canonical_payload()})
        return EXIT_OK

    if command == "abort":
        reason = (
            AbortReason.INTEGRITY_FAILURE
            if args.reason == "integrity-failure"
            else AbortReason.SECURITY_FAILURE
        )
        with ProspectiveCollectionJournal.open(args.root) as journal:
            status = journal.abort(
                reason=reason,
                expected_generation=args.expected_generation,
                clock=utc_now,
            )
            _emit(stdout, {"status": status.canonical_payload()})
        return EXIT_OK

    if command == "verify":
        with ProspectiveCollectionJournal.open(args.root) as journal:
            report = journal.verify()
            _emit(stdout, {"integrity": report.canonical_payload()})
        return EXIT_OK if report.ok else EXIT_INTEGRITY

    if command in {"manifest", "report"}:
        with ProspectiveCollectionJournal.open(args.root) as journal:
            artifact = journal.manifest() if command == "manifest" else journal.report()
            return _publish_shadow_document(
                args,
                stdout,
                name=command,
                document=artifact.canonical_payload(),
            )

    raise AssertionError(f"unhandled shadow command {command!r}")  # pragma: no cover


def _dispatch_pairwise(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.pairwise_command != "capture":  # pragma: no cover - argparse closes the set
        raise AssertionError(f"unhandled pairwise command {args.pairwise_command!r}")

    destination = _resolve_under(
        args.root,
        args.out,
        what="judgeable pre-action projection destination",
    )
    projection = load_judgeable_projection(_read_json(args.projection))
    document = projection.canonical_payload()
    _write_atomically(
        args.root,
        destination,
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        what="judgeable pre-action projection destination",
    )
    _emit(
        stdout,
        {
            "captured_to": str(destination),
            "decision_point_id": projection.decision_point_id,
            "candidate_count": len(projection.candidates),
            "projection_seal": projection.projection_seal(),
        },
    )
    return EXIT_OK


def _require_declared_revision(payload: object, *, initial: bool) -> object:
    """Keep ``append`` and ``revise`` meaning exactly what their names say.

    The store would accept either payload through the same path. Splitting the
    command means an operator who meant "start a decision" cannot silently
    extend one, and an operator who meant "revise" cannot silently start one.
    """
    revision = payload.get("revision") if isinstance(payload, dict) else None
    if initial and revision != 1:
        raise ContractViolation(
            "append records the initial revision; use revise to extend an existing decision",
            detail={"expected_revision": 1, "declared": revision},
        )
    if not initial and revision == 1:
        raise ContractViolation(
            "revise extends an existing decision; use append for the initial revision",
            detail={"forbidden_revision": 1, "declared": revision},
        )
    return payload


def _dispatch_memory(args: argparse.Namespace, stdout: TextIO) -> int:
    """The HOK-243 surface. Nothing here selects, executes or authorises."""
    command: str = args.memory_command

    if command == "limits":
        _emit(stdout, {"admission": admission_limits()})
        return EXIT_OK

    if command == "init":
        store = DecisionMemoryStore.create(
            args.root,
            store_id=args.store_id,
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            epoch=args.epoch,
        )
        with store:
            _emit(stdout, {"created": store.binding().canonical_payload()})
        return EXIT_OK

    if command in {"append", "revise"}:
        payload = _require_declared_revision(_read_json(args.record), initial=command == "append")
        with DecisionMemoryStore.open(args.root) as store:
            receipt = store.append_decision(payload, expected_generation=args.expected_generation)
            _emit(stdout, {"appended": receipt.canonical_payload()})
        return EXIT_OK

    if command == "status":
        with DecisionMemoryStore.open(args.root) as store:
            _emit(stdout, {"status": store.status().canonical_payload()})
        return EXIT_OK

    if command == "verify":
        with DecisionMemoryStore.open(args.root) as store:
            report = store.verify()
            _emit(stdout, {"integrity": report.canonical_payload()})
        return EXIT_OK if report.ok else EXIT_INTEGRITY

    if command == "list":
        with DecisionMemoryStore.open(args.root) as store:
            active = store.list_active(
                as_of=args.as_of,
                origin_kind=DecisionOriginKind(args.origin),
                limit=args.limit,
                offset=args.offset,
            )
            _emit(
                stdout,
                {
                    "origin_kind": args.origin,
                    "limit": args.limit,
                    "offset": args.offset,
                    "active": [entry.canonical_payload() for entry in active],
                },
            )
        return EXIT_OK

    if command == "show":
        with DecisionMemoryStore.open(args.root) as store:
            _emit(
                stdout,
                {
                    "active": store.get_active(
                        args.decision_id, as_of=args.as_of
                    ).canonical_payload()
                },
            )
        return EXIT_OK

    if command == "revoke":
        with DecisionMemoryStore.open(args.root) as store:
            receipt = store.revoke(
                args.decision_id,
                reason=args.reason,
                revoked_by=args.revoked_by,
                expected_generation=args.expected_generation,
            )
            _emit(stdout, {"revoked": receipt.canonical_payload()})
        return EXIT_OK

    if command == "tombstone":
        with DecisionMemoryStore.open(args.root) as store:
            receipt = store.tombstone(
                args.decision_id,
                args.revision,
                reason=args.reason,
                expected_generation=args.expected_generation,
            )
            _emit(stdout, {"tombstoned": receipt.canonical_payload()})
        return EXIT_OK

    if command == "abandon-epoch":
        with DecisionMemoryStore.open(args.root) as store:
            _emit(stdout, {"binding": store.abandon_epoch(reason=args.reason).canonical_payload()})
        return EXIT_OK

    if command == "export-transfer":
        return _dispatch_memory_export(args, stdout)

    if command == "import-transfer":
        with DecisionMemoryStore.open(args.root) as store:
            imported = store.import_transfer(
                _read_json(args.envelope), expected_generation=args.expected_generation
            )
            _emit(stdout, {"imported": imported.canonical_payload()})
        return EXIT_OK

    raise AssertionError(f"unhandled memory command {command!r}")  # pragma: no cover


def _dispatch_memory_export(args: argparse.Namespace, stdout: TextIO) -> int:
    destination = (
        _resolve_under(args.root, args.out, what="decision transfer destination")
        if args.out is not None
        else None
    )
    with DecisionMemoryStore.open(args.root) as store:
        envelope = store.export_transfer(
            args.decision_id,
            destination=DecisionBinding(
                host_id=args.to_host_id,
                agent_family=AgentFamily(args.to_agent_family),
                store_id=args.to_store_id,
                epoch=args.to_epoch,
            ),
            exported_by=args.exported_by,
        )
    document = envelope.canonical_payload()
    if destination is None:
        _emit(stdout, {"transfer": document})
        return EXIT_OK
    _write_atomically(
        args.root,
        destination,
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        what="decision transfer destination",
    )
    _emit(
        stdout,
        {
            "transfer_written_to": str(destination),
            "transfer_seal": envelope.transfer_seal,
            "source_record_seal": envelope.source_record_seal,
            "destination_binding": envelope.destination_binding.canonical_payload(),
        },
    )
    return EXIT_OK


def _require_declared_reconciliation_revision(payload: object, *, initial: bool) -> object:
    """Keep ``append`` and ``revise`` meaning exactly what their names say.

    The journal would accept either payload through the same path. Splitting the
    command means an operator who meant "record the first observation" cannot
    silently append a correction, and an operator who meant "correct" cannot
    silently start a new reconciliation.
    """
    revision = payload.get("revision") if isinstance(payload, dict) else None
    if initial and revision != 1:
        raise ContractViolation(
            "append records the initial reconciliation; use revise to correct or disagree",
            detail={"expected_revision": 1, "declared": revision},
        )
    if not initial and revision == 1:
        raise ContractViolation(
            "revise appends a correction or a disagreement; use append for the initial "
            "reconciliation",
            detail={"forbidden_revision": 1, "declared": revision},
        )
    return payload


def _supplied_decision_record(path: Path) -> StrategicDecisionRecord:
    """Load the required pre-action record through HOK-243's own admission.

    Reading it here never writes to the decision memory and never revises it. A
    record that would not be admissible as a decision is not usable as a preimage
    either, so it goes through the same door.
    """
    return admit_strategic_decision(_read_json(path))


def _dispatch_reconcile(args: argparse.Namespace, stdout: TextIO) -> int:
    """The HOK-244 surface. Nothing here selects, scores, authorises or promotes."""
    command: str = args.reconcile_command

    if command == "limits":
        _emit(stdout, {"admission": reconciliation_limits()})
        return EXIT_OK

    if command == "init":
        journal = ReconciliationJournal.create(
            args.root,
            store_id=args.store_id,
            host_id=args.host_id,
            agent_family=AgentFamily(args.agent_family),
            epoch=args.epoch,
        )
        with journal:
            _emit(stdout, {"created": journal.binding().canonical_payload()})
        return EXIT_OK

    if command in {"append", "revise"}:
        payload = _require_declared_reconciliation_revision(
            _read_json(args.reconciliation), initial=command == "append"
        )
        decision_record = _supplied_decision_record(args.decision_record)
        with ReconciliationJournal.open(args.root) as journal:
            receipt = journal.append_reconciliation(
                payload,
                decision_record=decision_record,
                expected_generation=args.expected_generation,
            )
            _emit(stdout, {"appended": receipt.canonical_payload()})
        return EXIT_OK

    if command == "status":
        with ReconciliationJournal.open(args.root) as journal:
            _emit(stdout, {"status": journal.status().canonical_payload()})
        return EXIT_OK

    if command == "verify":
        with ReconciliationJournal.open(args.root) as journal:
            report = journal.verify()
            _emit(stdout, {"integrity": report.canonical_payload()})
        return EXIT_OK if report.ok else EXIT_INTEGRITY

    if command == "list":
        with ReconciliationJournal.open(args.root) as journal:
            heads = journal.list_heads(limit=args.limit, offset=args.offset)
            _emit(
                stdout,
                {
                    "limit": args.limit,
                    "offset": args.offset,
                    "heads": [head.canonical_payload() for head in heads],
                },
            )
        return EXIT_OK

    if command == "show":
        with ReconciliationJournal.open(args.root) as journal:
            entry = journal.get(args.reconciliation_id, revision=args.revision)
            history = journal.revisions(args.reconciliation_id)
            _emit(
                stdout,
                {
                    "reconciliation": entry.canonical_payload(),
                    "revision_seals": [
                        {"revision": item.revision, "content_seal": item.content_seal}
                        for item in history
                    ],
                },
            )
        return EXIT_OK

    if command == "replay":
        # ``--decision-record`` is required for replay: a comparison against a
        # preimage nobody supplied would have nothing to put on the pre-action side.
        preimage = admit_strategic_decision(_read_json(args.decision_record))
        with ReconciliationJournal.open(args.root) as journal:
            comparison = journal.replay(args.reconciliation_id, preimage, revision=args.revision)
            _emit(stdout, {"replay": comparison.canonical_payload()})
        return EXIT_OK

    if command == "abandon-epoch":
        with ReconciliationJournal.open(args.root) as journal:
            _emit(
                stdout, {"binding": journal.abandon_epoch(reason=args.reason).canonical_payload()}
            )
        return EXIT_OK

    raise AssertionError(f"unhandled reconcile command {command!r}")  # pragma: no cover


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
            args.root,
            destination,
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            what="verdict destination",
        )
    _emit(stdout, {"verdict": document})
    return EXIT_OK


def _dispatch_benchmark(args: argparse.Namespace, stdout: TextIO) -> int:
    """The HOK-188 surface.

    There is deliberately no ``--holdout-ledger`` option anywhere below. The
    pre-HOK-190 route can freeze an explicitly selected validation
    ``CONTINUE`` baseline, but it has no holdout input and no execution
    capability. HOK-188 itself still refuses any executed split but
    ``VALIDATION`` during validation.
    """
    if args.benchmark_command == "manifest":
        return _dispatch_benchmark_manifest(args, stdout)

    if args.benchmark_command == "spec":
        manifest = load_corpus_manifest(_read_json(args.manifest))
        protocol = load_preregistration(_read_json(args.protocol))
        specification = load_benchmark_spec(_read_json(args.spec))
        require_spec_matches_protocol(specification, protocol)
        require_spec_matches_manifest(specification, manifest)
        _emit(
            stdout,
            {
                "valid": True,
                "benchmark_id": specification.benchmark_id,
                "executed_split": specification.executed_split.value,
                "spec_seal": specification.spec_seal(),
                "protocol_seal": specification.protocol_seal,
                "validation_corpus_seal": specification.validation_corpus_seal,
                "holdout_corpus_seal": specification.holdout_corpus_seal,
                "holdout_executed": False,
            },
        )
        return EXIT_OK

    if args.benchmark_command == "run":
        if args.out is not None and args.root is None:
            raise ContractViolation(
                "--root is required whenever this command writes a file",
                detail={"requires": "--root", "for": "--out"},
            )
        destination = (
            _resolve_under(args.root, args.out, what="benchmark report destination")
            if args.out is not None
            else None
        )
        report = run_benchmark(
            spec=load_benchmark_spec(_read_json(args.spec)),
            protocol=load_preregistration(_read_json(args.protocol)),
            manifest=load_corpus_manifest(_read_json(args.manifest)),
            corpus_dir=args.corpus_dir,
        )
        document = report.canonical_payload()
        if destination is not None:
            _write_atomically(
                args.root,
                destination,
                json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                what="benchmark report destination",
            )
            _emit(
                stdout,
                {
                    "report_written_to": str(destination),
                    "report_seal": report.report_seal,
                    "spec_seal": report.spec_seal,
                    "baselines": [item.baseline_id.value for item in report.baselines],
                },
            )
            return EXIT_OK
        _emit(stdout, {"report": document})
        return EXIT_OK

    if args.benchmark_command == "verify":
        _emit(
            stdout,
            {
                "verification": verify_report(
                    load_benchmark_report(_read_json(args.report)),
                    spec=load_benchmark_spec(_read_json(args.spec)),
                    protocol=load_preregistration(_read_json(args.protocol)),
                    manifest=load_corpus_manifest(_read_json(args.manifest)),
                    corpus_dir=args.corpus_dir,
                )
            },
        )
        return EXIT_OK

    if args.benchmark_command == "holdout":
        if args.holdout_command != "plan":  # pragma: no cover - argparse closes the set
            raise AssertionError(f"unhandled holdout command {args.holdout_command!r}")
        destination = _resolve_under(args.root, args.out, what="holdout plan destination")
        plan = create_holdout_plan(
            load_benchmark_report(_read_json(args.report)),
            spec=load_benchmark_spec(_read_json(args.spec)),
            protocol=load_preregistration(_read_json(args.protocol)),
            manifest=load_corpus_manifest(_read_json(args.manifest)),
            corpus_dir=args.corpus_dir,
            selected_baseline_id=BaselineId(args.baseline),
        )
        _write_atomically(
            args.root,
            destination,
            json.dumps(plan.canonical_payload(), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            what="holdout plan destination",
        )
        _emit(
            stdout,
            {
                "plan_written_to": str(destination),
                "plan_seal": plan.plan_seal,
                "validation_report_seal": plan.validation_report_seal,
                "selected_baseline_id": plan.selected_baseline_id.value,
                "holdout_executed": False,
            },
        )
        return EXIT_OK

    raise AssertionError(
        f"unhandled benchmark command {args.benchmark_command!r}"
    )  # pragma: no cover


def _dispatch_benchmark_manifest(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.manifest_command == "verify":
        manifest = load_corpus_manifest(_read_json(args.manifest))
        _emit(stdout, {"manifest": verify_manifest(args.corpus_dir, manifest)})
        return EXIT_OK

    destination = _resolve_under(args.root, args.out, what="manifest destination")
    manifest = build_manifest(
        args.corpus_dir,
        corpus_id=args.corpus_id,
        corpus_version=args.corpus_version,
        provenance=CorpusProvenance(
            origin=args.origin,
            licence=args.licence,
            synthetic=args.synthetic,
            description=args.description,
        ),
        relative_paths={
            Split.TRAIN: args.train,
            Split.VALIDATION: args.validation,
            Split.HOLDOUT: args.holdout,
        },
    )
    document = manifest.canonical_payload()
    _write_atomically(
        args.root,
        destination,
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        what="manifest destination",
    )
    _emit(
        stdout,
        {
            "manifest_written_to": str(destination),
            "corpus_id": manifest.corpus_id,
            "corpus_version": manifest.corpus_version,
            "splits": [
                {"split": entry.split.value, "corpus_seal": entry.corpus_seal}
                for entry in sorted(manifest.splits, key=lambda entry: entry.split.value)
            ],
        },
    )
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

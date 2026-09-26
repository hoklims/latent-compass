"""Passive host-hook adapter for Latent Compass routing advice.

The adapter is deliberately outside :mod:`latent_compass.lab`: it validates a
sanitised lifecycle envelope and writes a host-local journal, while the lab
remains pure. The separately installed host wrapper owns local Git inspection.
Neither layer returns advice to the host, changes routing or stores prompts,
tool arguments, tool results or transcript paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, TextIO

from pydantic import Field, model_validator

from latent_compass.canonical import canonical_text, seal
from latent_compass.confined_io import (
    lease_confined_file,
    read_confined_file,
    validate_portable_component,
    write_new_file,
)
from latent_compass.contracts import Identifier, StrictModel, validate_contract
from latent_compass.episode import AgentFamily
from latent_compass.errors import ContractViolation
from latent_compass.lab.routing import (
    LAB_ROUTING_CONTRACT_VERSION,
    CapabilityKind,
    CapabilityObservation,
    HostCapabilitySnapshot,
    RouteRequest,
    evaluate_route,
)

__all__ = [
    "DEFAULT_CONFIG_NAME",
    "SHADOW_HARNESS_CONTRACT_VERSION",
    "ShadowHarnessConfig",
    "default_store_root",
    "load_shadow_config",
    "main",
    "process_hook_event",
]

SHADOW_HARNESS_CONTRACT_VERSION: Final = "1.0.0"
DEFAULT_CONFIG_NAME: Final = "config.json"
MAX_HOOK_BYTES: Final = 1_048_576
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SEAL = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORM: Final = os.name


def _path_identity(path: Path, *, platform: str = _PLATFORM) -> str:
    resolved = str(path.resolve(strict=False))
    return resolved.casefold() if platform == "nt" else resolved


class ShadowHarnessViolation(ContractViolation):
    """A local shadow-hook configuration or envelope was refused."""

    code = "shadow_harness_violation"


class ShadowCapability(StrictModel):
    capability_id: Identifier
    kind: CapabilityKind
    cost_ceiling: int = Field(default=0, ge=0)


class ShadowProject(StrictModel):
    root: str = Field(min_length=3, max_length=1024)
    alias: Identifier
    capabilities: tuple[ShadowCapability, ...] = Field(min_length=1, max_length=256)
    remaining_budget: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> ShadowProject:
        root = Path(self.root)
        if not root.is_absolute():
            raise ValueError("project root must be absolute")
        try:
            validate_portable_component(str(self.alias), what="project alias")
        except ContractViolation as exc:
            raise ValueError(str(exc)) from exc
        keys = [(item.capability_id, item.kind) for item in self.capabilities]
        if len(keys) != len(set(keys)):
            raise ValueError("project capabilities must be unique by id and kind")
        return self


class ShadowHarnessConfig(StrictModel):
    contract_version: Literal["1.0.0"]
    enabled: bool
    host_id: Identifier
    agent_family: AgentFamily
    projects: tuple[ShadowProject, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _coherent(self) -> ShadowHarnessConfig:
        aliases = [str(project.alias).casefold() for project in self.projects]
        roots = [
            _path_identity(Path(project.root), platform=_PLATFORM) for project in self.projects
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("project aliases must be unique")
        if len(roots) != len(set(roots)):
            raise ValueError("project roots must be unique")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def load_shadow_config(payload: object) -> ShadowHarnessConfig:
    """Validate one host-local shadow configuration."""
    return validate_contract(
        ShadowHarnessConfig,
        payload,
        error=ShadowHarnessViolation,
        context="shadow harness config",
    )


def default_store_root(host: Literal["codex", "claude"], home: Path | None = None) -> Path:
    """Return the separate host-local store for ``host``."""
    base = home if home is not None else Path.home()
    return base / f".{host}" / "latent-compass-shadow"


def _utc_now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_label(value: object, *, fallback_domain: str) -> str:
    text = value if isinstance(value, str) else ""
    if _SAFE_LABEL.fullmatch(text):
        return text
    return "value-" + seal(fallback_domain, {"value": text}).removeprefix("sha256:")[:16]


def _resolved(path: str) -> Path:
    return Path(path).resolve(strict=False)


def _project_for_cwd(config: ShadowHarnessConfig, cwd: object) -> ShadowProject | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    candidate = _resolved(cwd)
    matches: list[tuple[Path, ShadowProject]] = []
    for project in config.projects:
        root = _resolved(project.root)
        if candidate == root or candidate.is_relative_to(root):
            matches.append((root, project))
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0].parts))[1]


def _source_cache_path(store_root: Path, project: ShadowProject) -> Path:
    return store_root / "source" / f"{project.alias}.json"


def _refresh_source_cache(
    store_root: Path,
    project: ShadowProject,
    *,
    source_digest: object,
    observed_at: object,
) -> dict[str, str]:
    if not isinstance(source_digest, str) or _SEAL.fullmatch(source_digest) is None:
        raise ShadowHarnessViolation(
            "source declaration digest is absent or malformed",
            detail={"reason": "source_declaration_invalid"},
        )
    if not isinstance(observed_at, str):
        raise ShadowHarnessViolation(
            "source observation instant is absent",
            detail={"reason": "source_observed_at_absent"},
        )
    cache = {
        "owner": "latent-compass-shadow",
        "source_declaration_digest": source_digest,
        "source_observed_at": observed_at,
    }
    destination = _source_cache_path(store_root, project)
    content = (canonical_text(cache) + "\n").encode("utf-8")
    if not os.path.lexists(destination.parent):
        try:
            write_new_file(store_root, destination, content, what="shadow source cache")
        except ContractViolation as exc:
            raise ShadowHarnessViolation(
                "source cache changed during refresh",
                detail={"reason": "source_cache_changed", "detail": str(exc)},
            ) from exc
        return {key: value for key, value in cache.items() if key != "owner"}
    lease = lease_confined_file(
        store_root,
        destination,
        max_bytes=MAX_HOOK_BYTES,
        what="shadow source cache",
        allow_absent=True,
    )
    try:
        if lease.content is not None:
            previous = json.loads(lease.content.decode("utf-8"))
            if not isinstance(previous, dict) or previous.get("owner") != "latent-compass-shadow":
                raise ShadowHarnessViolation(
                    "source cache ownership is not established",
                    detail={"reason": "source_cache_unowned"},
                )
        lease.replace(content)
    except ContractViolation as exc:
        raise ShadowHarnessViolation(
            "source cache changed during refresh",
            detail={"reason": "source_cache_changed", "detail": str(exc)},
        ) from exc
    finally:
        lease.close()
    return {key: value for key, value in cache.items() if key != "owner"}


def _read_source_cache(store_root: Path, project: ShadowProject) -> dict[str, str] | None:
    path = _source_cache_path(store_root, project)
    try:
        raw = read_confined_file(
            store_root,
            path,
            max_bytes=MAX_HOOK_BYTES,
            what="shadow source cache",
        )
    except FileNotFoundError:
        return None
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("owner") != "latent-compass-shadow":
        return None
    digest = payload.get("source_declaration_digest")
    observed_at = payload.get("source_observed_at")
    if not isinstance(digest, str) or not isinstance(observed_at, str):
        return None
    return {"source_declaration_digest": digest, "source_observed_at": observed_at}


def _route_decision(
    *,
    payload: dict[str, object],
    config: ShadowHarnessConfig,
    project: ShadowProject,
    source_digest: str,
    now: str,
) -> dict[str, object]:
    tool_name = _safe_label(payload.get("tool_name"), fallback_domain="shadow.harness.tool.v1")
    capability = next(
        (
            item
            for item in project.capabilities
            if item.capability_id == tool_name and item.kind is CapabilityKind.TOOL
        ),
        None,
    )
    cost_ceiling = capability.cost_ceiling if capability is not None else 0
    session_seal = seal(
        "shadow.harness.session.v1", {"host": config.host_id, "id": payload.get("session_id")}
    )
    turn_seal = seal(
        "shadow.harness.turn.v1",
        {"session_seal": session_seal, "id": payload.get("turn_id")},
    )
    model_digest = seal(
        "shadow.harness.model.v1",
        {
            "host_id": config.host_id,
            "agent_family": config.agent_family.value,
            "capabilities": [item.canonical_payload() for item in project.capabilities],
        },
    )
    state_digest = seal(
        "shadow.harness.state.v1",
        {
            "session_seal": session_seal,
            "turn_seal": turn_seal,
            "source_digest": source_digest,
        },
    )
    request_digest = seal(
        "shadow.harness.request.v1",
        {
            "state_digest": state_digest,
            "tool_name": tool_name,
            "observed_at": now,
        },
    )
    request = RouteRequest(
        contract_version=LAB_ROUTING_CONTRACT_VERSION,
        request_digest=request_digest,
        model_digest=model_digest,
        state_digest=state_digest,
        source_digest=source_digest,
        host_id=config.host_id,
        agent_family=config.agent_family,
        scope=project.alias,
        candidate_capability_id=tool_name,
        candidate_kind=CapabilityKind.TOOL,
        requested_at=now,
        expiry=now,
        cost_ceiling=cost_ceiling,
        remaining_budget=project.remaining_budget,
        advisor_present=True,
        kill_switch_engaged=False,
        review_required=False,
        review_satisfied=False,
        semctx_proof_required=False,
        semctx_proof_satisfied=False,
        explicit_missing_authority=False,
        explicit_missing_precondition=False,
        fallback_observation_id=None,
    )
    snapshot = HostCapabilitySnapshot(
        contract_version=LAB_ROUTING_CONTRACT_VERSION,
        host_id=config.host_id,
        agent_family=config.agent_family,
        scope=project.alias,
        current_source_digest=source_digest,
        snapshot_taken_at=now,
        observed_capabilities=tuple(
            CapabilityObservation(
                capability_id=item.capability_id,
                kind=item.kind,
                observed_at=now,
                expires_at=now,
            )
            for item in project.capabilities
        ),
    )
    return evaluate_route(request, snapshot, now=now).canonical_payload()


def _persist(store_root: Path, project: ShadowProject, record: dict[str, object]) -> None:
    session_seal = str(record["session_seal"]).removeprefix("sha256:")
    record_seal = str(record["record_seal"]).removeprefix("sha256:")
    directory = store_root / "events" / project.alias / session_seal[:32]
    destination = directory / f"{record_seal}.json"
    write_new_file(
        store_root,
        destination,
        (canonical_text(record) + "\n").encode("utf-8"),
        what="shadow event record",
    )


def process_hook_event(
    payload: dict[str, object],
    *,
    host: Literal["codex", "claude"],
    config: ShadowHarnessConfig,
    store_root: Path,
    now: str | None = None,
) -> dict[str, object] | None:
    """Process one hook envelope and persist a privacy-minimised shadow record."""
    if not config.enabled or config.agent_family.value != host:
        return None
    project = _project_for_cwd(config, payload.get("cwd"))
    if project is None:
        return None
    observed_at = now or _utc_now()
    event_name = payload.get("hook_event_name")
    if event_name in {"SessionStart", "PostToolUse"}:
        refreshed: dict[str, object] = dict(
            _refresh_source_cache(
                store_root,
                project,
                source_digest=payload.get("source_declaration_digest"),
                observed_at=payload.get("source_observed_at"),
            )
        )
        return refreshed
    if event_name != "PreToolUse":
        return None
    source_cache = _read_source_cache(store_root, project)
    if source_cache is None:
        return None
    source_digest = source_cache["source_declaration_digest"]
    session_seal = seal(
        "shadow.harness.session.v1", {"host": config.host_id, "id": payload.get("session_id")}
    )
    turn_seal = seal(
        "shadow.harness.turn.v1",
        {"session_seal": session_seal, "id": payload.get("turn_id")},
    )
    record: dict[str, object] = {
        "contract_version": SHADOW_HARNESS_CONTRACT_VERSION,
        "host": host,
        "host_id": config.host_id,
        "project_alias": project.alias,
        "hook_event_name": "PreToolUse",
        "observed_at": observed_at,
        "session_seal": session_seal,
        "turn_seal": turn_seal,
        "model": _safe_label(payload.get("model"), fallback_domain="shadow.harness.model-label.v1"),
        "permission_mode": _safe_label(
            payload.get("permission_mode"), fallback_domain="shadow.harness.permission.v1"
        ),
        "tool_name": _safe_label(
            payload.get("tool_name"), fallback_domain="shadow.harness.tool.v1"
        ),
        "source_declaration_digest": source_digest,
        "source_observed_at": source_cache["source_observed_at"],
        "route_decision": _route_decision(
            payload=payload,
            config=config,
            project=project,
            source_digest=source_digest,
            now=observed_at,
        ),
        "shadow_only": True,
        "host_influenced": False,
        "content_recorded": False,
    }
    record["record_seal"] = seal("shadow.harness.record.v1", record)
    _persist(store_root, project, record)
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passive Latent Compass host-hook adapter")
    parser.add_argument("--host", choices=("codex", "claude"), required=True)
    parser.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run fail-open and silent so shadow telemetry can never block its host."""
    del stdout
    args = _parser().parse_args(argv)
    store_root = default_store_root(args.host, args.home)
    try:
        raw = stdin.read(MAX_HOOK_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_HOOK_BYTES:
            raise ShadowHarnessViolation(
                "hook payload exceeded the shadow bound",
                detail={"reason": "hook_payload_too_large", "max_bytes": MAX_HOOK_BYTES},
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ShadowHarnessViolation(
                "hook payload must be a JSON object", detail={"reason": "not_an_object"}
            )
        config_path = store_root / DEFAULT_CONFIG_NAME
        config = load_shadow_config(
            json.loads(
                read_confined_file(
                    store_root,
                    config_path,
                    max_bytes=MAX_HOOK_BYTES,
                    what="shadow host configuration",
                ).decode("utf-8")
            )
        )
        process_hook_event(
            payload,
            host=args.host,
            config=config,
            store_root=store_root,
            now=args.now,
        )
    except Exception:
        stderr.write("[latent-compass-shadow] fail-open: event not recorded\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

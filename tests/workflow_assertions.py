from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml


def workflow(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{path.name} must contain a YAML mapping")
    return cast(dict[str, Any], payload)


def job(payload: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get(name), dict):
        raise AssertionError(f"workflow job {name!r} is missing")
    return cast(dict[str, Any], jobs[name])


def step(job_payload: dict[str, Any], name: str) -> dict[str, Any]:
    steps = job_payload.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("workflow job steps are missing")
    matches = [item for item in steps if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"workflow step {name!r} must occur exactly once")
    return cast(dict[str, Any], matches[0])


def assert_required(mapping: dict[str, Any]) -> None:
    """Assert a job or step cannot be disabled or allowed to fail."""
    assert "if" not in mapping
    assert mapping.get("continue-on-error", False) is False


def needs(job_payload: dict[str, Any]) -> set[str]:
    raw = job_payload.get("needs", [])
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return set(raw)
    raise AssertionError("workflow job needs must be a string or string array")

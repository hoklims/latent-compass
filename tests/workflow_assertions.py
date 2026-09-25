from __future__ import annotations

import re
from pathlib import Path


def active_lines(path: Path) -> list[str]:
    """Return active YAML lines with trailing comments removed."""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        lines.append(raw.split(" #", 1)[0].rstrip())
    return lines


def job_lines(path: Path, job: str) -> list[str]:
    lines = active_lines(path)
    start = lines.index(f"  {job}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]):
            end = index
            break
    return lines[start:end]


def step_values(job: list[str], key: str) -> dict[str, str]:
    """Map each active step name to one scalar key from the same step."""
    values: dict[str, str] = {}
    current_name: str | None = None
    for line in job:
        if line.startswith("      - name: "):
            current_name = line.removeprefix("      - name: ")
            continue
        if current_name is not None and line.startswith(f"        {key}: "):
            values[current_name] = line.removeprefix(f"        {key}: ")
    return values


def permissions(job: list[str]) -> dict[str, str]:
    try:
        start = job.index("    permissions:")
    except ValueError:
        return {}
    result: dict[str, str] = {}
    for line in job[start + 1 :]:
        match = re.fullmatch(r"      ([A-Za-z0-9_-]+): (.+)", line)
        if match is None:
            break
        result[match.group(1)] = match.group(2)
    return result

"""Integration proof for the shipped synthetic walkthrough."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


def _walkthrough_module() -> ModuleType:
    path = EXAMPLES / "walkthrough.py"
    spec = importlib.util.spec_from_file_location("latent_compass_walkthrough", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shipped_synthetic_inputs_validate_through_public_interfaces() -> None:
    import latent_compass

    episode = latent_compass.load_episode(
        json.loads((EXAMPLES / "synthetic-episode.json").read_text(encoding="utf-8"))
    )
    projection = latent_compass.load_judgeable_projection(
        json.loads((EXAMPLES / "synthetic-projection.json").read_text(encoding="utf-8"))
    )
    assert episode.episode_id == "synthetic-episode-0001"
    assert projection.decision_point_id == "synthetic-decision-0001"


def test_walkthrough_runs_end_to_end_and_refuses_root_reuse(tmp_path: Path) -> None:
    module = _walkthrough_module()
    root = tmp_path / "walkthrough"
    summary = module.run(root, examples_dir=EXAMPLES)

    assert summary["notice"].startswith("SYNTHETIC DEMONSTRATION ONLY")
    assert summary["empirical_claim"] is False
    assert summary["memory_integrity"] is True
    assert summary["reconciliation_integrity"] is True
    assert summary["collection_integrity"] is True
    assert summary["collection_state"] == "CLOSED_SUFFICIENT"
    assert summary["eligible_count"] == 1
    assert summary["missingness"]["COST"]["LATE"] == 1
    assert (root / "capture" / "synthetic-pair.json").is_file()

    with pytest.raises(FileExistsError, match="refusing to reuse"):
        module.run(root, examples_dir=EXAMPLES)


def test_walkthrough_is_executable_as_a_standalone_script(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(EXAMPLES / "walkthrough.py"), "--root", str(root)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "SYNTHETIC DEMONSTRATION ONLY" in completed.stdout
    assert (
        json.loads((root / "synthetic-walkthrough-summary.json").read_text(encoding="utf-8"))[
            "empirical_claim"
        ]
        is False
    )

    refused = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(EXAMPLES / "walkthrough.py"), "--root", str(root)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 2
    assert "refusing to reuse existing walkthrough root" in refused.stderr

"""Record exact pytest collection and execution phases for the audit evaluator."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_state: dict[str, Any] = {}


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--latent-audit-report", action="store")
    parser.addoption("--latent-audit-nonce", action="store")


def pytest_sessionstart(session: pytest.Session) -> None:
    _state.update(
        {
            "schema": 1,
            "nonce": session.config.getoption("--latent-audit-nonce"),
            "collected": [],
            "reports": [],
            "collection_errors": [],
            "internal_error": False,
            "exit_status": None,
        }
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    _state["collected"] = [item.nodeid for item in session.items]


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _state["collection_errors"].append(report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _state["reports"].append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "xfail": hasattr(report, "wasxfail")
            or (report.failed and str(report.longrepr).startswith("[XPASS(strict)]")),
        }
    )


def pytest_internalerror(excrepr: Any, excinfo: Any) -> None:  # noqa: ARG001
    _state["internal_error"] = True


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _state["exit_status"] = int(exitstatus)
    path = Path(session.config.getoption("--latent-audit-report"))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(_state), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    # Import the installed pytest and canonical reporter before exposing candidate modules.
    root = Path(sys.argv[1])
    sys.path[:0] = [str(root), str(root / "src")]
    raise SystemExit(pytest.main(["-q", *sys.argv[2:]], plugins=[sys.modules[__name__]]))

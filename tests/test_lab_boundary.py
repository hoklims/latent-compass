"""ADR 0011 — the lab package launches nothing and reaches nothing.

The lab admits what an authorised host executor observed; it never runs a
process, opens a socket or loads native code itself. This is a structural
check over the parsed imports and calls of every module in the package — not
a search for a word, which a docstring would satisfy or defeat.
"""

from __future__ import annotations

import ast
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent / "src" / "latent_compass" / "lab"

FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "ctypes",
        "ftplib",
        "http",
        "multiprocessing",
        "pty",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
        "webbrowser",
        "xmlrpc",
    }
)
FORBIDDEN_OS_CALLS = frozenset(
    {"system", "popen", "startfile", "fork", "forkpty", "kill", "posix_spawn", "posix_spawnp"}
)
FORBIDDEN_OS_PREFIXES = ("exec", "spawn")
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})


def violations(source: str) -> list[str]:
    """Every forbidden import or call in one module's parsed source."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(
                f"import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] in FORBIDDEN_MODULES
            )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_MODULES:
                found.append(f"from {node.module} import ...")
            if root == "os":
                found.extend(
                    f"from os import {alias.name}"
                    for alias in node.names
                    if alias.name in FORBIDDEN_OS_CALLS
                    or alias.name.startswith(FORBIDDEN_OS_PREFIXES)
                )
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in FORBIDDEN_BUILTINS:
                found.append(f"{function.id}(...)")
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
                and (
                    function.attr in FORBIDDEN_OS_CALLS
                    or function.attr.startswith(FORBIDDEN_OS_PREFIXES)
                )
            ):
                found.append(f"os.{function.attr}(...)")
    return found


def test_the_scanner_itself_catches_every_family_it_forbids() -> None:
    """The trap is live: each forbidden form is detected before the scan is trusted."""
    # Every sample below is source text handed to ``ast.parse``. None is ever executed.
    assert violations("import subprocess") == ["import subprocess"]
    assert violations("import urllib.request") == ["import urllib.request"]
    assert violations("from socket import create_connection") == ["from socket import ..."]
    assert violations("from os import execv") == ["from os import execv"]
    assert violations("import os\nos.system('git status')") == ["os.system(...)"]
    assert violations("import os\nos.spawnl(0, 'git')") == ["os.spawnl(...)"]
    assert violations("eval('1')") == ["eval(...)"]
    # A word in a docstring or a comment is not an import.
    assert violations('"""This module opens no subprocess."""\n# import socket\n') == []
    assert violations("import os\nos.path.abspath('.')") == []


def test_no_lab_module_launches_a_process_or_reaches_the_network() -> None:
    modules = sorted(LAB.glob("*.py"))
    names = {module.name for module in modules}
    assert {"host_observations.py", "host_session.py", "observations.py", "routing.py"} <= names
    assert len(modules) >= 15, f"the boundary scan only examined {len(modules)} modules"

    offences = {
        module.name: found
        for module in modules
        if (found := violations(module.read_text(encoding="utf-8")))
    }
    assert offences == {}

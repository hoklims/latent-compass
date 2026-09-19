"""ADR 0011 — the lab package launches nothing and reaches nothing.

The lab admits what an authorised host executor observed; it never runs a
process, opens a socket or loads native code itself. This is a structural
check over the parsed imports and calls of every lab module **and of every
first-party module the lab can import** — not a search for a word, which a
docstring would satisfy or defeat. A helper placed next to the lab and imported
by it is inside the boundary, so it is inside the scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
PACKAGE = SRC / "latent_compass"
LAB = PACKAGE / "lab"

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

#: Every offence tolerated inside the boundary, by module and by exact form. A
#: second exception is a visible diff here; one that stops being needed is red.
NAMED_EXCEPTIONS = {
    # The confined reader opens files through the operating system's own API so
    # that it never follows a link or a reparse point. It loads a system
    # library; it launches nothing and reaches nothing.
    "latent_compass/confined_io.py": ["from ctypes import ...", "import ctypes"],
}


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


def first_party_imports(
    module: Path, *, src: Path = SRC, package: str = "latent_compass"
) -> set[Path]:
    """Every file of ``package`` that importing ``module`` would execute.

    An imported name resolves to a module file or to a package's ``__init__``,
    and importing ``a.b.c`` runs ``a`` and ``a.b`` first, so every ancestor is
    included. Relative imports resolve against the importing file's package.
    """
    found: set[Path] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = module.parent
                for _ in range(node.level - 1):
                    anchor = anchor.parent
                prefix = ".".join(anchor.relative_to(src).parts)
                base = f"{prefix}.{base}" if base else prefix
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        for name in names:
            parts = name.split(".")
            if parts[0] != package:
                continue
            for depth in range(1, len(parts) + 1):
                stem = src.joinpath(*parts[:depth])
                for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                    if candidate.is_file():
                        found.add(candidate)
    return found


def reachable_from(
    modules: list[Path], *, src: Path = SRC, package: str = "latent_compass"
) -> set[Path]:
    seen: set[Path] = set(modules)
    queue = list(modules)
    while queue:
        for imported in first_party_imports(queue.pop(), src=src, package=package) - seen:
            seen.add(imported)
            queue.append(imported)
    return seen


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


def test_the_reachability_walk_follows_a_helper_placed_outside_the_lab(tmp_path: Path) -> None:
    """The trap is live: a launcher hidden behind a first-party import is reached."""
    package = tmp_path / "pkg"
    (package / "lab" / "hosts").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lab" / "__init__.py").write_text("", encoding="utf-8")
    (package / "lab" / "hosts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "lab" / "session.py").write_text("from pkg.helpers import run\n", encoding="utf-8")
    (package / "lab" / "hosts" / "local.py").write_text(
        "from .. import session\n", encoding="utf-8"
    )
    (package / "helpers.py").write_text("from pkg import launcher\n", encoding="utf-8")
    (package / "launcher.py").write_text("import subprocess\n", encoding="utf-8")
    (package / "unrelated.py").write_text("import socket\n", encoding="utf-8")

    lab_modules = sorted((package / "lab").rglob("*.py"))
    assert package / "lab" / "hosts" / "local.py" in lab_modules, "a sub-package escaped the scan"
    reached = reachable_from(lab_modules, src=tmp_path, package="pkg")

    assert package / "launcher.py" in reached, "two first-party hops hid a process launch"
    assert package / "unrelated.py" not in reached
    offences = {
        module.relative_to(tmp_path).as_posix(): found
        for module in reached
        if (found := violations(module.read_text(encoding="utf-8")))
    }
    assert offences == {"pkg/launcher.py": ["import subprocess"]}


def test_nothing_the_lab_can_import_launches_a_process_or_reaches_the_network() -> None:
    lab_modules = sorted(LAB.rglob("*.py"))
    names = {module.name for module in lab_modules}
    assert {"host_observations.py", "host_session.py", "observations.py", "routing.py"} <= names
    assert len(lab_modules) >= 15, f"the boundary scan only examined {len(lab_modules)} modules"

    reached = reachable_from(lab_modules)
    # The walk really leaves the lab: the confined reader is a first-party import.
    assert PACKAGE / "confined_io.py" in reached
    assert len(reached) > len(lab_modules)

    offences = {
        module.relative_to(SRC).as_posix(): sorted(found)
        for module in reached
        if (found := violations(module.read_text(encoding="utf-8")))
    }
    assert offences == NAMED_EXCEPTIONS
    assert not any(name.startswith("latent_compass/lab/") for name in offences)

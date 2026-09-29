"""Red/green witnesses for the six high-impact invariants audited in REPORT.md §5.

Applies one invariant-breaking mutation at a time to a *disposable* checkout of
the frozen candidate, runs the whole suite (expect red), reverts, runs it again
(expect green). Never point this at a tree you care about.

Usage: python3 mutants.py <path-to-disposable-checkout>
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "candidate")
LOGS = ROOT.parent / "audit-logs"
PYTEST = ["uv", "run", "--frozen", "pytest", "-o", "addopts=", "-q"]

AUTHORITY = "src/latent_compass/authority.py"

# Verbatim candidate source, kept exactly as executed for the recorded witnesses.
_UNTRUSTED = '            "caller-supplied evidence has no trusted external attestation",'
_TABLE = (
    "ALLOWED_TRANSITIONS: Final[MappingProxyType[LifecycleState, frozenset[LifecycleState]]] = (\n"
)

MUTANTS = [
    (
        "M1-self-authorisation",
        AUTHORITY,
        "    if actor is Actor.LATENT_COMPASS:\n        # Unconditional",
        "    if False and actor is Actor.LATENT_COMPASS:\n        # Unconditional",
        "authorize_transition refuses Actor.LATENT_COMPASS unconditionally",
    ),
    (
        "M2-mutable-transition-table",
        AUTHORITY,
        _TABLE + "    MappingProxyType(_ALLOWED_TRANSITIONS)",
        _TABLE + "    dict(_ALLOWED_TRANSITIONS)  # type: ignore[assignment]",
        "enforcement tables are read-only mappings",
    ),
    (
        "M3-provenance-fails-open",
        AUTHORITY,
        "        raise _refuse(\n" + _UNTRUSTED,
        "        pass  # noqa\n    if False:\n        raise _refuse(\n" + _UNTRUSTED,
        "evidence-bearing advancement refused without external attestation",
    ),
    (
        "M4-confinement-escape",
        "src/latent_compass/confined_io.py",
        "    if os.path.normcase(common) != os.path.normcase(absolute_root) or os.path.normcase(\n"
        "        absolute_target\n    ) == os.path.normcase(absolute_root):",
        "    if False:",
        "plan_confined_target refuses targets outside the named root",
    ),
    (
        "M5-ledger-duplicate-allowed",
        "src/latent_compass/ledger.py",
        "    episode_id      TEXT NOT NULL UNIQUE,",
        "    episode_id      TEXT NOT NULL,",
        "the episode ledger refuses a duplicate episode_id",
    ),
    (
        "M6-subprocess-in-src",
        "src/latent_compass/canonical.py",
        "from __future__ import annotations",
        "from __future__ import annotations\n\nimport subprocess  # noqa",
        "no src/ module imports subprocess",
    ),
]


def run(log: pathlib.Path) -> tuple[int, str, float]:
    """Run the suite, tee to ``log``, return (returncode, summary line, seconds)."""
    started = time.time()
    with log.open("w") as handle:
        rc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no caller input
            PYTEST, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, check=False
        ).returncode
    lines = [line for line in log.read_text().splitlines() if "passed" in line or "failed" in line]
    return rc, (lines[-1] if lines else ""), round(time.time() - started, 1)


def main() -> None:
    LOGS.mkdir(exist_ok=True)
    results = []
    for name, rel, old, new, claim in MUTANTS:
        path = ROOT / rel
        pristine = path.read_text()
        if pristine.count(old) != 1:
            raise SystemExit(f"{name}: anchor matched {pristine.count(old)} times, expected 1")
        path.write_text(pristine.replace(old, new))
        red_rc, red, secs = run(LOGS / f"{name}.red.log")
        subprocess.run(  # noqa: S603 - fixed argv, no shell, no caller input
            ["git", "checkout", "--", rel],  # noqa: S607 - the checkout's own VCS, from PATH
            cwd=ROOT,
            check=True,
        )
        if path.read_text() != pristine:
            raise SystemExit(f"{name}: revert did not restore the file")
        green_rc, green, _ = run(LOGS / f"{name}.green.log")
        results.append(
            {
                "mutant": name,
                "file": rel,
                "claim": claim,
                "red_rc": red_rc,
                "red": red,
                "green_rc": green_rc,
                "green": green,
                "secs": secs,
            }
        )
        print(json.dumps(results[-1]), flush=True)

    (LOGS / "mutants.json").write_text(json.dumps(results, indent=2))
    assert all(r["red_rc"] != 0 and r["green_rc"] == 0 for r in results), (
        "a witness did not go red then green"
    )
    print("all witnesses red then green")


if __name__ == "__main__":
    main()

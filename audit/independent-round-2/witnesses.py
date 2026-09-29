"""Red/green witnesses for the round-2 candidate (d5ae236)."""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(sys.argv[1]).resolve()
LOGS = ROOT.parent / "r2-logs"
LOGS.mkdir(exist_ok=True)

PYTEST_AUDIT = "uv run --frozen pytest tests/test_independent_audit.py -o addopts='' -q"
PYTEST_BENCH = "uv run --frozen pytest tests/test_lab_host_bench.py -o addopts='' -q"
SDIST = (
    "uv build --no-sources && "
    "uv run --frozen python tools/verify_sdist.py dist/latent_compass-0.1.0.tar.gz"
)

WITNESSES = [
    (
        "W1-gate-accepts-unresolved-blockers",
        "tools/independent_audit.py",
        '    if receipt.get("unresolved_blockers") != []:\n'
        '        raise AuditError("unresolved_blockers must be an empty array")\n',
        "",
        PYTEST_AUDIT,
        "the public gate refuses a receipt carrying unresolved blockers",
        ["tools/independent_audit.py::gate", "tests/test_independent_audit.py"],
    ),
    (
        "W2-gate-accepts-weak-verdict",
        "tools/independent_audit.py",
        '    if receipt.get("verdict") != "PROOF_ADEQUATE":\n'
        '        raise AuditError("verdict must be PROOF_ADEQUATE")\n',
        "",
        PYTEST_AUDIT,
        "the public gate refuses any verdict weaker than PROOF_ADEQUATE",
        ["tools/independent_audit.py::gate", "tests/test_independent_audit.py"],
    ),
    (
        "W3-corpus-crlf-regression",
        "tests/corpus_generator.py",
        '(target / name).write_text(text, encoding="utf-8", newline="\\n")',
        '(target / name).write_text(text, encoding="utf-8", newline="\\r\\n")',
        SDIST,
        "the CI sdist step catches a reintroduced line-ending regression (round-1 F1/F6)",
        [
            "tools/verify_sdist.py",
            ".github/workflows/verify.yml::Test extracted source distribution",
            ".github/workflows/release.yml::Test extracted source distribution",
        ],
    ),
    (
        "W4-language-server-rereads-disk",
        "examples/lab_host_bench.py",
        '                            "text": data.decode("utf-8"),',
        '                            "text": (self.root / relative_path).read_text("utf-8"),',
        PYTEST_BENCH,
        "the real language-server paths re-enabled by round-2 carry live oracles (round-1 F3)",
        ["examples/lab_host_bench.py", "tests/test_lab_host_bench.py"],
    ),
]


def run(command: str, log: pathlib.Path) -> tuple[int, str, str]:
    started = time.time()
    completed = subprocess.run(  # noqa: S602 - fixed command strings defined above
        command, cwd=ROOT, shell=True, capture_output=True, text=True, check=False
    )
    output = completed.stdout + completed.stderr
    log.write_text(output)
    digest = "sha256:" + hashlib.sha256(output.encode()).hexdigest()
    summary = next(
        (
            ln
            for ln in reversed(output.splitlines())
            if "passed" in ln or "failed" in ln or "error" in ln
        ),
        "",
    )
    print(
        f"    {log.name}: rc={completed.returncode} {summary} ({time.time() - started:.0f}s)",
        flush=True,
    )
    return completed.returncode, digest, summary


def main() -> None:
    only = set(sys.argv[2:])
    results = []
    for name, rel, old, new, command, claim, paths in WITNESSES:
        if only and name not in only:
            continue
        path = ROOT / rel
        mutation = f"{rel}: {old.strip()[:90]!r} -> {new.strip()[:90] or '<deleted>'!r}"
        pristine = path.read_text()
        if pristine.count(old) != 1:
            raise SystemExit(f"{name}: anchor matched {pristine.count(old)} times")
        print(name, flush=True)
        path.write_text(pristine.replace(old, new))
        red_rc, red_digest, red_summary = run(command, LOGS / f"{name}.red.log")
        subprocess.run(  # noqa: S603 - fixed argv, no shell, no caller input
            ["git", "checkout", "--", rel],  # noqa: S607 - the checkout's own VCS, from PATH
            cwd=ROOT,
            check=True,
        )
        if path.read_text() != pristine:
            raise SystemExit(f"{name}: revert failed")
        green_rc, green_digest, green_summary = run(command, LOGS / f"{name}.green.log")
        results.append(
            {
                "claim": claim,
                "invocation_paths": paths,
                "witness": {
                    "mutation": mutation,
                    "command": command,
                    "red_exit": red_rc,
                    "green_exit": green_rc,
                    "red_output_digest": red_digest,
                    "green_output_digest": green_digest,
                    "red_summary": red_summary,
                    "green_summary": green_summary,
                },
            }
        )
    (LOGS / ("claims.json" if not only else "claims-partial.json")).write_text(
        json.dumps(results, indent=2)
    )
    ok = all(r["witness"]["red_exit"] != 0 and r["witness"]["green_exit"] == 0 for r in results)
    print("all witnesses red then green" if ok else "A WITNESS DID NOT GO RED THEN GREEN")


if __name__ == "__main__":
    main()

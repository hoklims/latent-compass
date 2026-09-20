# ruff: noqa: E501, S603, S108, RUF005, RET504
# Audit evidence: this is the driver exactly as executed, kept byte-for-byte apart
# from this header so the shipped mutations match the recorded witnesses. The repo's
# lint rules are waived here rather than reformatting evidence.
# fmt: off
#!/usr/bin/env python3
"""Independent-audit witness driver for hoklims/latent-compass.

Run from a read-only clone detached at the candidate head:

    python witnesses.py --repository <clone> --out <output dir>

Every witness either (a) demonstrates a protocol claim holding, with a real
red/green pair, or (b) demonstrates the gate accepting something it must
refuse.  Mutations are applied to the clone, then reverted and asserted
byte-identical; the driver refuses to finish if `git status --porcelain` is
non-empty afterwards.

Outputs are trimmed to identifiers and summary lines and scrubbed of local
absolute paths.  Digests recorded in claims.json are SHA-256 of the FULL
untrimmed combined stdout+stderr of each command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from runpy import run_path

BASE = "f652690f29420b5d4ae4f37b22caa5e98df102a2"
HEAD = "45078dcc10d228326f410df76f7af15ff2133e0b"
INDEPENDENCE = (
    "not_candidate_author",
    "read_only_candidate",
    "fresh_session",
    "distinct_harness",
    "distinct_account",
    "distinct_environment",
    "distinct_evidence_store",
    "first_pass_before_author_narrative",
)


def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def scrub(text: str, roots: list[Path]) -> str:
    for root in roots:
        text = text.replace(str(root), "<SCRATCH>")
    text = re.sub(r"/tmp/[A-Za-z0-9_./-]*", "<TMP>", text)
    text = re.sub(r"/home/[A-Za-z0-9_./-]*", "<HOME>", text)
    return text


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def trim(text: str, keep: int = 40) -> str:
    """Keep test identifiers, summary lines, decisions and errors."""
    interesting = re.compile(
        r"(passed|failed|error|Error|ERROR|FAILED|PASSED|decision|BLOCK|ALLOW|"
        r"::|collected|warnings summary|assert|Traceback|exit)",
    )
    lines = [line.rstrip() for line in text.splitlines() if interesting.search(line)]
    if len(lines) > keep:
        lines = lines[: keep // 2] + ["... (trimmed) ..."] + lines[-keep // 2 :]
    return "\n".join(lines) + "\n"


class Mutation:
    """Apply a textual mutation to a tracked file and guarantee exact revert."""

    def __init__(self, path: Path, old: str, new: str) -> None:
        self.path = path
        self.old = old
        self.new = new

    def __enter__(self) -> Mutation:
        self.original = self.path.read_bytes()
        text = self.original.decode("utf-8")
        if self.old not in text:
            raise SystemExit(f"mutation anchor absent in {self.path}")
        self.path.write_bytes(text.replace(self.old, self.new, 1).encode("utf-8"))
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.write_bytes(self.original)
        assert self.path.read_bytes() == self.original, f"revert failed for {self.path}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--epoch", type=Path, required=True)
    args = parser.parse_args()
    repo: Path = args.repository.resolve()
    out: Path = args.out.resolve()
    (out / "witnesses").mkdir(parents=True, exist_ok=True)
    wdir = out / "witnesses"
    roots = [repo, out, out.parent]
    env = os.environ.copy()

    tool = repo / "tools" / "independent_audit.py"
    loaded = run_path(str(tool))
    epoch_digest_of = loaded["_epoch_digest"]
    canonical_epoch = json.loads(args.epoch.read_text(encoding="utf-8"))

    py = [sys.executable]
    gate_cmd = [*py, "tools/independent_audit.py", "gate"]
    scratch = wdir / "inputs"
    scratch.mkdir(exist_ok=True)

    def receipt_for(epoch: dict[str, object], claim: str, **witness: object) -> dict[str, object]:
        base_witness: dict[str, object] = {
            "mutation": "none",
            "command": "none",
            "red_exit": 1,
            "green_exit": 0,
            "red_output_digest": "sha256:" + "a" * 64,
            "green_output_digest": "sha256:" + "b" * 64,
        }
        base_witness.update(witness)
        return {
            **epoch,
            "independence": dict.fromkeys(INDEPENDENCE, True),
            "claims": [
                {"claim": claim, "invocation_paths": ["manual"], "witness": base_witness}
            ],
            "unresolved_blockers": [],
            "verdict": "PROOF_ADEQUATE",
        }

    def write_json(name: str, value: object) -> Path:
        path = scratch / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def record(name: str, cmd: list[str], code: int, output: str) -> str:
        digest = sha256(output)
        (wdir / f"{name}.txt").write_text(
            f"$ {' '.join(scrub(c, roots) for c in cmd)}\nexit={code}\nfull_output_sha256={digest}\n"
            f"--- trimmed ---\n{trim(scrub(output, roots))}",
            encoding="utf-8",
        )
        return digest

    claims: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []

    # ---------------------------------------------------------------- W1
    # A wholly fabricated epoch: no Git object named here exists anywhere.
    forged = {
        "schema": "hoklims/latent-compass:independent-audit/2",
        "repository": "hoklims/latent-compass",
        "base_sha": "0" * 39 + "1",
        "head_sha": "0" * 39 + "2",
        "head_tree": "0" * 39 + "3",
        "policy_digest": "sha256:" + "0" * 64,
        "files": [
            {"path": "totally/invented.py", "kind": "file", "digest": "sha256:" + "f" * 64}
        ],
    }
    forged["epoch_digest"] = epoch_digest_of(forged)
    fe = write_json("w1-forged-epoch.json", forged)
    fr = write_json(
        "w1-forged-receipt.json", receipt_for(forged, "no audit work was performed at all")
    )
    cmd = [*gate_cmd, "--epoch", str(fe), "--receipt", str(fr)]
    code, output = run(cmd, repo, env)
    d = record("w1-forged-epoch-gate", cmd, code, output)
    blockers.append(
        {
            "id": "B1",
            "blocker": "gate ALLOWs an epoch that names no real Git object; ALLOW is not bound to the candidate",
            "witness": {
                "command": "python tools/independent_audit.py gate --epoch w1-forged-epoch.json --receipt w1-forged-receipt.json",
                "expected_exit": "non-zero (BLOCK)",
                "observed_exit": code,
                "output_digest": d,
            },
        }
    )

    # ---------------------------------------------------------------- W2
    bool_receipt = receipt_for(
        canonical_epoch,
        "JSON booleans smuggled into the exit-code fields",
        red_exit=True,
        green_exit=False,
    )
    br = write_json("w2-boolean-receipt.json", bool_receipt)
    cmd = [*gate_cmd, "--epoch", str(args.epoch), "--receipt", str(br)]
    code, output = run(cmd, repo, env)
    d = record("w2-boolean-exit-codes", cmd, code, output)
    blockers.append(
        {
            "id": "B2",
            "blocker": 'gate accepts `"red_exit": true` / `"green_exit": false`; bool is a subclass of int in Python, so a receipt with no numeric exit codes passes',
            "witness": {
                "command": "python tools/independent_audit.py gate --epoch epoch.json --receipt w2-boolean-receipt.json",
                "expected_exit": "non-zero (BLOCK)",
                "observed_exit": code,
                "output_digest": d,
            },
        }
    )

    # ---------------------------------------------------------------- W3
    hidden = json.loads(json.dumps(canonical_epoch))
    hidden["files"] = [
        f for f in hidden["files"] if f["path"] != "tools/independent_audit.py"
    ]
    hidden["epoch_digest"] = epoch_digest_of(hidden)
    he = write_json("w3-truncated-epoch.json", hidden)
    hr = write_json(
        "w3-truncated-receipt.json",
        receipt_for(hidden, "the inventory hides the gate's own source change"),
    )
    cmd = [*gate_cmd, "--epoch", str(he), "--receipt", str(hr)]
    code, output = run(cmd, repo, env)
    d = record("w3-truncated-inventory", cmd, code, output)
    blockers.append(
        {
            "id": "B3",
            "blocker": "gate ALLOWs an epoch whose changed-file inventory silently omits tools/independent_audit.py; the documented 'complete changed file inventory' check does not exist",
            "witness": {
                "command": "python tools/independent_audit.py gate --epoch w3-truncated-epoch.json --receipt w3-truncated-receipt.json",
                "expected_exit": "non-zero (BLOCK)",
                "observed_exit": code,
                "output_digest": d,
            },
        }
    )

    # ---------------------------------------------------------------- W4
    # policy_digest is read from the WORKING TREE, not from head_sha.
    policy = repo / "docs" / "independent-audit.md"
    epoch_cmd = [
        *py,
        "tools/independent_audit.py",
        "epoch",
        "--repository",
        ".",
        "--base",
        BASE,
        "--head",
        HEAD,
    ]
    with Mutation(policy, "# Public independent-audit protocol", "# Public independent-audit protocol\n\n<!-- auditor scratch -->"):
        code, output = run(epoch_cmd, repo, env)
        dirty = json.loads(output)
    d = record("w4-dirty-tree-epoch", epoch_cmd, code, output)
    code_clean, output_clean = run(epoch_cmd, repo, env)
    clean = json.loads(output_clean)
    record("w4-clean-tree-epoch", epoch_cmd, code_clean, output_clean)
    same_identity = dirty["head_sha"] == clean["head_sha"] and dirty["head_tree"] == clean["head_tree"]
    differs = dirty["epoch_digest"] != clean["epoch_digest"]
    (wdir / "w4-epoch-purity.txt").write_text(
        json.dumps(
            {
                "same_base_and_head": same_identity,
                "epoch_digest_differs": differs,
                "clean_policy_digest": clean["policy_digest"],
                "dirty_policy_digest": dirty["policy_digest"],
                "inventory_digest_for_that_same_file": [
                    f["digest"] for f in dirty["files"] if f["path"] == "docs/independent-audit.md"
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    blockers.append(
        {
            "id": "B4",
            "blocker": "epoch is not a pure function of (base, head): _policy_digest reads the three policy files from the working tree while files[] digests come from head_sha, so an uncommitted edit yields a different epoch_digest and an internally inconsistent epoch",
            "witness": {
                "command": "python tools/independent_audit.py epoch --repository . --base <base> --head <head>, run with and without an uncommitted edit to docs/independent-audit.md",
                "expected": "identical epoch_digest for identical (base, head)",
                "observed": {
                    "epoch_digest_differs": differs,
                    "same_base_and_head": same_identity,
                },
                "output_digest": d,
            },
        }
    )

    # ---------------------------------------------------------------- W5 (holds)
    tampered = json.loads(json.dumps(canonical_epoch))
    tampered["head_sha"] = "f" * 40
    te = write_json("w5-tampered-epoch.json", tampered)
    tr = write_json("w5-tampered-receipt.json", receipt_for(tampered, "tampered head"))
    red_cmd = [*gate_cmd, "--epoch", str(te), "--receipt", str(tr)]
    red_code, red_out = run(red_cmd, repo, env)
    red_d = record("w5-red-tampered-epoch", red_cmd, red_code, red_out)
    gr = write_json("w5-good-receipt.json", receipt_for(canonical_epoch, "canonical epoch"))
    green_cmd = [*gate_cmd, "--epoch", str(args.epoch), "--receipt", str(gr)]
    green_code, green_out = run(green_cmd, repo, env)
    green_d = record("w5-green-canonical-epoch", green_cmd, green_code, green_out)
    claims.append(
        {
            "claim": "the gate rejects an epoch whose epoch_digest no longer matches its own contents",
            "invocation_paths": ["python tools/independent_audit.py gate"],
            "witness": {
                "mutation": "set epoch.head_sha to ffff... without recomputing epoch_digest",
                "command": "python tools/independent_audit.py gate --epoch <epoch> --receipt <receipt>",
                "red_exit": red_code,
                "green_exit": green_code,
                "red_output_digest": red_d,
                "green_output_digest": green_d,
            },
        }
    )

    # ---------------------------------------------------------------- W6 (holds)
    pytest_cmd = [*py, "-m", "pytest", "-o", "addopts=", "-q", "tests/test_independent_audit.py"]
    with Mutation(
        tool,
        '    if receipt.get("unresolved_blockers") != []:\n        raise AuditError("unresolved_blockers must be an empty array")\n',
        "",
    ):
        red_code, red_out = run(pytest_cmd, repo, env)
    red_d = record("w6-red-gate-unit-tests", pytest_cmd, red_code, red_out)
    green_code, green_out = run(pytest_cmd, repo, env)
    green_d = record("w6-green-gate-unit-tests", pytest_cmd, green_code, green_out)
    claims.append(
        {
            "claim": "tests/test_independent_audit.py is load-bearing: removing the unresolved_blockers check from the gate fails the suite",
            "invocation_paths": ["uv run --frozen pytest (Verify workflow, both matrix OSes)"],
            "witness": {
                "mutation": "delete the unresolved_blockers check from tools/independent_audit.py::gate",
                "command": "python -m pytest -o addopts= -q tests/test_independent_audit.py",
                "red_exit": red_code,
                "green_exit": green_code,
                "red_output_digest": red_d,
                "green_output_digest": green_d,
            },
        }
    )

    # ---------------------------------------------------------------- W7 (holds)
    def build_and_verify() -> tuple[int, str]:
        dist = repo / "dist"
        for stale in dist.glob("*") if dist.exists() else []:
            stale.unlink()
        code_b, out_b = run(["uv", "build", "--no-sources"], repo, env)
        if code_b != 0:
            return code_b, out_b
        sdist = next(iter(sorted(dist.glob("latent_compass-*.tar.gz"))))
        code_v, out_v = run(
            [*py, "tools/verify_sdist.py", str(sdist)], repo, env
        )
        return code_v, out_b + out_v

    with Mutation(repo / "pyproject.toml", '  "/tools",\n', ""):
        red_code, red_out = build_and_verify()
    red_d = record("w7-red-sdist-without-tools", ["uv build --no-sources", "python tools/verify_sdist.py dist/latent_compass-*.tar.gz"], red_code, red_out)
    green_code, green_out = build_and_verify()
    green_d = record("w7-green-sdist", ["uv build --no-sources", "python tools/verify_sdist.py dist/latent_compass-*.tar.gz"], green_code, green_out)
    claims.append(
        {
            "claim": "the source distribution really is self-testing: dropping /tools from the sdist include list makes the extracted-sdist test suite fail",
            "invocation_paths": [
                "Verify workflow step 'Test extracted source distribution'",
                "Build attested release workflow step 'Test extracted source distribution'",
            ],
            "witness": {
                "mutation": 'remove "/tools" from [tool.hatch.build.targets.sdist].include',
                "command": "uv build --no-sources && python tools/verify_sdist.py dist/latent_compass-*.tar.gz",
                "red_exit": red_code,
                "green_exit": green_code,
                "red_output_digest": red_d,
                "green_output_digest": green_d,
            },
        }
    )

    # ---------------------------------------------------------------- W8
    grep_cmd = ["grep", "-rn", "independent_audit", ".github"]
    code, output = run(grep_cmd, repo, env)
    d = record("w8-no-ci-invocation", grep_cmd, code, output)
    blockers.append(
        {
            "id": "B5",
            "blocker": "no workflow invokes tools/independent_audit.py; neither Verify nor the release workflow requires an audit receipt, so the gate constrains nothing automatically",
            "witness": {
                "command": "grep -rn independent_audit .github",
                "expected_exit": "0 if any workflow referenced the gate",
                "observed_exit": code,
                "output_digest": d,
            },
        }
    )

    # ---------------------------------------------------------------- cleanliness
    for stale in (repo / "dist").glob("*"):
        stale.unlink()
    status_code, status = run(["git", "status", "--porcelain"], repo, env)
    (wdir / "clone-clean.txt").write_text(
        f"$ git status --porcelain\nexit={status_code}\noutput={status!r}\n", encoding="utf-8"
    )
    if status.strip():
        raise SystemExit(f"clone is dirty after witnesses: {status!r}")

    (wdir / "claims.json").write_text(
        json.dumps(
            {
                "base_sha": BASE,
                "head_sha": HEAD,
                "epoch_digest": canonical_epoch["epoch_digest"],
                "claims": claims,
                "unresolved_blockers": blockers,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"claims": len(claims), "blockers": len(blockers)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

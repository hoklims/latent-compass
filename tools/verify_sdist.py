#!/usr/bin/env python3
"""Extract an sdist and run its shipped tests against its shipped source."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="latent-compass-sdist-") as temporary:
        root = Path(temporary)
        with tarfile.open(args.sdist.resolve(), "r:gz") as archive:
            archive.extractall(root, filter="data")
        candidates = [path for path in root.iterdir() if path.is_dir()]
        if len(candidates) != 1:
            raise RuntimeError("sdist must contain exactly one top-level directory")
        extracted = candidates[0]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join((str(extracted), str(extracted / "src")))
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "-q"],
            cwd=extracted,
            env=environment,
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

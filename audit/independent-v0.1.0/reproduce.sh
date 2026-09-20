#!/usr/bin/env bash
# Re-runs every check behind REPORT.md, from public inputs only.
# Requires: git, uv, python3, and `gh` for the release-asset checks.
set -euo pipefail

SHA=f652690f29420b5d4ae4f37b22caa5e98df102a2
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export UV_CACHE_DIR="$WORK/uvcache" UV_PYTHON_INSTALL_DIR="$WORK/pythons"

git clone --quiet https://github.com/hoklims/latent-compass.git "$WORK/candidate"
git -C "$WORK/candidate" checkout --quiet "$SHA"
cd "$WORK/candidate"

echo "== §4 repo gate =="
uv sync --locked --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest -o addopts='' -q

echo "== §7 F5 tag and signatures =="
git cat-file -t v0.1.0                       # 'commit' => lightweight, unsigned
git log --format='%G?' | sort | uniq -c      # 43 N (unsigned), 5 E (unverifiable)

echo "== §7 F3 skipped real-server paths =="
uv run --frozen pytest -o addopts='' -q -rs 2>&1 | grep pyright || true

echo "== §7 F2 deterministic rebuild =="
uv build --no-sources >/dev/null
sha256sum dist/*                             # 3cb70cce... / d8ac92dd...
gh release download v0.1.0 --repo hoklims/latent-compass --dir "$WORK/released" --clobber
sha256sum "$WORK"/released/*                 # db01bac6... / 5e976a8b...

echo "== §7 F1 published sdist fails its own tests =="
mkdir -p "$WORK/sdist" && tar xzf "$WORK"/released/latent_compass-0.1.0.tar.gz -C "$WORK/sdist"
cd "$WORK/sdist/latent_compass-0.1.0"
PYTHONPATH="$PWD/src" uv run --no-project --isolated --python 3.13 \
  --with pytest==8.4.2 --with "pydantic>=2,<3" \
  python -m pytest tests -o addopts='' -q && echo "UNEXPECTED: published sdist suite passed" || \
  echo "expected: published sdist exits non-zero (6 failed)"

echo "== §5 red/green witnesses =="
cd "$WORK/candidate"
python3 "$(dirname "$(realpath "$0")")/mutants.py" "$WORK/candidate"

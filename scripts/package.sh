#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv export --frozen --no-dev --no-emit-project -o build-requirements.txt > /dev/null
trap 'rm -f build-requirements.txt' EXIT
uv run python -c 'import shutil; shutil.rmtree("build/package", ignore_errors=True)'
mkdir -p build/package
uv pip install --python-version 3.12 --python-platform aarch64-manylinux2014 --only-binary :all: --target build/package -r build-requirements.txt
uv run python scripts/package.py

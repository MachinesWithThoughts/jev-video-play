#!/usr/bin/env bash
#
# Prepare the Python app: resolve its dependencies into uv's cache and check
# that the sources parse. There is no compile step — app.py declares its
# dependencies inline (PEP 723) and uv fetches them — so "build" here means
# doing that work now, instead of on the first run.
#
#   ./build-python.sh
#
# Run it after changing the dependency block in python/app.py, or on a fresh
# machine so the first ./run-python.sh starts instantly.

set -eo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

APP="python/app.py"

if ! command -v uv >/dev/null 2>&1; then
  echo "build-python.sh: uv is not installed, and $APP runs through it." >&2
  echo "        See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [[ ! -f $APP ]]; then
  echo "build-python.sh: $APP is missing." >&2
  exit 1
fi

echo "==> Resolving dependencies"
uv sync --script "$APP"

echo "==> Checking Python syntax"
uv run --script "$APP" --version

echo "==> Checking browser sources"
if command -v node >/dev/null 2>&1; then
  for file in python/static/*.js; do
    node --check "$file"
    echo "    ok  $file"
  done
else
  echo "    skipped (node not installed)"
fi

for file in python/static/index.html python/static/styles.css; do
  if [[ ! -s $file ]]; then
    echo "build-python.sh: $file is missing or empty." >&2
    exit 1
  fi
  echo "    ok  $file"
done

echo
echo "Build complete. Start it with ./run-python.sh"

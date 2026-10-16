#!/usr/bin/env bash
#
# Launch the Jev video player and open it in your browser.
#
#   ./run-python.sh                        play the bundled sample clip
#   ./run-python.sh ~/clips/harbour.mp4    play your own file
#   ./run-python.sh --port 9000            any app.py flag still works
#   ./run-python.sh --help                 full list of flags
#
# Run python/app.py directly if you would rather open the page yourself.

set -eo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

APP="python/app.py"
SAMPLE="samples/harbour-helsinki.mp4"

if ! command -v uv >/dev/null 2>&1; then
  echo "run-python.sh: uv is not installed, and $APP runs through it." >&2
  echo "        See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [[ ! -f .env && -z "${JEV_API_KEY:-}" && -z "${TYPESAFE_API_KEY:-}" ]]; then
  cat >&2 <<'MSG'
run-python.sh: no API key found.

Put one in .env at the project root:

  JEV_API_KEY=apikey_...

The page will still load without it, but every detection call will fail.
MSG
  exit 1
fi

# Nothing to play was named (no arguments, or flags only): fall back to the
# bundled sample so the app has something to open with.
if [[ $# -eq 0 || $1 == -* ]] && [[ -f $SAMPLE ]]; then
  set -- "$SAMPLE" "$@"
fi

exec "./$APP" "$@" --open

#!/bin/sh
# mc launcher: runs the mcctl package inside this folder's private Python environment.
DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "mc: setting up its Python environment (one time)..." >&2
  "$DIR/install.sh" >&2 || exit 1
fi
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$DIR/.venv/bin/python" -m mcctl "$@"

#!/bin/sh
# (Re)creates the private Python environment for mc and links `mc` onto PATH (macOS or Linux).
# MC_BIN_DIR=<folder> chooses where the `mc` link goes (default: Homebrew's bin on a Mac, otherwise ~/.local/bin).
set -e
cd "$(dirname "$0")"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "Installing uv..." >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
# Use a Python from this machine only if it is 3.12 or newer. An older one (macOS ships 3.9) would make `uv sync` fail,
# so with none that new, uv fetches one itself.
PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
  found="$(command -v "$candidate" 2>/dev/null || true)"
  if [ -n "$found" ] && "$found" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    PY="$found"
    break
  fi
done
"$UV" sync ${PY:+--python "$PY"}
BIN="${MC_BIN_DIR:-}"
if [ -z "$BIN" ]; then
  if [ -d /opt/homebrew/bin ]; then BIN=/opt/homebrew/bin; else BIN="$HOME/.local/bin"; fi
fi
mkdir -p "$BIN"
ln -sf "$PWD/mc" "$BIN/mc"
command -v tmux >/dev/null 2>&1 || echo "WARNING: tmux is required to run servers (apt install tmux / brew install tmux)." >&2
echo "mc installed in $BIN. Run: mc"

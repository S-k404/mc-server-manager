#!/bin/sh
# (Re)creates the private Python environment for mc and links `mc` onto PATH (macOS or Linux).
set -e
cd "$(dirname "$0")"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "Installing uv..." >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
PY="$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3 || true)"
[ -x /opt/homebrew/bin/python3 ] && PY=/opt/homebrew/bin/python3
"$UV" sync ${PY:+--python "$PY"}
if [ -d /opt/homebrew/bin ]; then BIN=/opt/homebrew/bin; else BIN="$HOME/.local/bin"; mkdir -p "$BIN"; fi
ln -sf "$PWD/mc" "$BIN/mc"
command -v tmux >/dev/null 2>&1 || echo "WARNING: tmux is required to run servers (apt install tmux / brew install tmux)." >&2
echo "mc installed in $BIN. Run: mc"

#!/usr/bin/env bash
# System Clearer — launcher
set -euo pipefail
cd "$(dirname "$0")"

PORT="${SC_PORT:-9761}"
HOST="${SC_HOST:-127.0.0.1}"
PY="./.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "Python venv not found. Creating it…"
  # Use a Python whose pyexpat/ssl work (pyenv 3.10.14 on this Mac); fall back to python3.
  if [ -x "$HOME/.pyenv/versions/3.10.14/bin/python3" ]; then
    "$HOME/.pyenv/versions/3.10.14/bin/python3" -m venv .venv
  else
    python3 -m venv .venv
  fi
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

echo "▶ System Clearer running at http://$HOST:$PORT"
echo "  (first launch: you'll be asked to create an access PIN)"
exec "$PY" -m uvicorn backend.main:app --host "$HOST" --port "$PORT"

#!/usr/bin/env bash
# Start the mymockllm service.
# This script can be invoked from anywhere; it will always run from its own directory.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "[mymockllm] Working dir: $SCRIPT_DIR"
echo "[mymockllm] Starting on ${HOST}:${PORT} ..."

# Source code lives in app/. Watching only that directory means agent shadow
# worktrees, .histories dumps, .venv, etc. cannot trigger spurious reloads.
exec uv run uvicorn app.main:app \
  --host "$HOST" --port "$PORT" \
  --reload \
  --reload-dir app

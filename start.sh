#!/usr/bin/env bash
# Start the mymockllm service.
# This script can be invoked from anywhere; it will always run from its own directory.

set -e

# Resolve the absolute directory this script lives in (works even via symlink).
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "[mymockllm] Working dir: $SCRIPT_DIR"
echo "[mymockllm] Starting on ${HOST}:${PORT} ..."

exec uv run uvicorn main:app \
  --host "$HOST" --port "$PORT" \
  --reload \
  --reload-dir "$SCRIPT_DIR" \
  --reload-include "*.py" \
  --reload-include "*.html" \
  --reload-exclude ".*" \
  --reload-exclude ".*/**" \
  --reload-exclude ".histories/*" \
  --reload-exclude ".claude/*" \
  --reload-exclude ".venv/*" \
  --reload-exclude "__pycache__/*"

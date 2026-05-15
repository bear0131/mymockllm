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

# watchfiles honors this env var: colon-separated absolute paths to ignore.
# This is the only reliable way to keep uvicorn --reload from tripping on
# IDE/agent shadow worktrees (e.g. .claude/worktrees/...) and history dumps.
export WATCHFILES_IGNORE_PATHS="${SCRIPT_DIR}/.claude:${SCRIPT_DIR}/.histories:${SCRIPT_DIR}/.venv:${SCRIPT_DIR}/.git:${SCRIPT_DIR}/.codebuddy:${SCRIPT_DIR}/__pycache__"

exec uv run uvicorn main:app \
  --host "$HOST" --port "$PORT" \
  --reload \
  --reload-dir "$SCRIPT_DIR" \
  --reload-include "*.py" \
  --reload-include "*.html"

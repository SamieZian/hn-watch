#!/usr/bin/env bash
# HN Watch launcher: bootstraps a venv on first run, then starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.12}"

if ! command -v "$PYTHON" >/dev/null; then
  echo "error: $PYTHON not found (install with: brew install python@3.12)" >&2
  exit 1
fi
if ! command -v claude >/dev/null; then
  echo "error: claude CLI not found. install Claude Code and run 'claude' once to authenticate" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "creating venv..."
  "$PYTHON" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec .venv/bin/python -m app.main

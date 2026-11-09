#!/bin/bash
# Start Career Agent and open it in your browser.
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing uv (Python package manager)…"; brew install uv
  else
    echo "Please install uv first: https://docs.astral.sh/uv/ (or Homebrew, then: brew install uv)"; exit 1
  fi
fi
exec uv run python -m career_agent "$@"

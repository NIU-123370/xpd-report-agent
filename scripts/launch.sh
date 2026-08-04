#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT_CLI="${PROJECT_CLI:-$ROOT/.venv/bin/xpd-report-agent}"
if [ ! -x "$PROJECT_CLI" ]; then
  echo "Project CLI was not found: $PROJECT_CLI" >&2
  echo "Create the project virtual environment before managing services." >&2
  exit 1
fi

exec "$PROJECT_CLI" "$@"

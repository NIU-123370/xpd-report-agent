#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ "${LAUNCH_MANAGED:-false}" != "true" ] && [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

if [ "${LAUNCH_MANAGED:-false}" != "true" ] && [ -f "$ROOT/configs/local.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/configs/local.env"
  set +a
fi

export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-127.0.0.1}"
export HERMES_GATEWAY_PORT="${HERMES_GATEWAY_PORT:-8642}"
export HERMES_GATEWAY_API_KEY="${HERMES_GATEWAY_API_KEY:-dev-secret}"
export HERMES_GATEWAY_MODEL="${HERMES_GATEWAY_MODEL:-hermes-agent}"

export FASTAPI_HOST="${FASTAPI_HOST:-127.0.0.1}"
export FASTAPI_PORT="${FASTAPI_PORT:-8000}"
export FASTAPI_RELOAD="${FASTAPI_RELOAD:-false}"

if [ "$FASTAPI_RELOAD" = "true" ]; then
  exec uv run uvicorn xpd_report_agent.api.main:app \
    --reload \
    --host "$FASTAPI_HOST" \
    --port "$FASTAPI_PORT"
fi

exec uv run uvicorn xpd_report_agent.api.main:app \
  --host "$FASTAPI_HOST" \
  --port "$FASTAPI_PORT"

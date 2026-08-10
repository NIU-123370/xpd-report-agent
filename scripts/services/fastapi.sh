#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ "${LAUNCH_MANAGED:-false}" != "true" ]; then
  xpd_inherited_exports="$(export -p)"
  if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
  fi
  if [ -f "$ROOT/configs/local.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/configs/local.env"
    set +a
  fi
  # `export -p` produces shell-escaped declarations. Replaying the inherited
  # environment keeps process variables above both local configuration files.
  eval "$xpd_inherited_exports"
  unset xpd_inherited_exports
fi

export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-127.0.0.1}"
export HERMES_GATEWAY_PORT="${HERMES_GATEWAY_PORT:-8642}"
export HERMES_GATEWAY_API_KEY="${HERMES_GATEWAY_API_KEY:-dev-secret}"
export HERMES_GATEWAY_MODEL="${HERMES_GATEWAY_MODEL:-hermes-agent}"
export HERMES_TIMEZONE="${HERMES_TIMEZONE:-Asia/Shanghai}"

export FASTAPI_HOST="${FASTAPI_HOST:-127.0.0.1}"
export FASTAPI_PORT="${FASTAPI_PORT:-8000}"
export FASTAPI_RELOAD="${FASTAPI_RELOAD:-false}"
export XPD_SERVICE_AUTH_ENABLED="${XPD_SERVICE_AUTH_ENABLED:-false}"
# Leave XPD_AGENT_MAX_CONCURRENCY unset unless the operator explicitly
# configured it. Python derives a topology-aware default from the Hermes pool.
export XPD_AGENT_RUN_MAX_ATTEMPTS="${XPD_AGENT_RUN_MAX_ATTEMPTS:-2}"
export XPD_AGENT_CHAT_TIMEOUT_SECONDS="${XPD_AGENT_CHAT_TIMEOUT_SECONDS:-600}"
export XPD_AGENT_RECONCILE_SECONDS="${XPD_AGENT_RECONCILE_SECONDS:-10}"
export XPD_AGENT_OUTCOME_RECONCILE_SECONDS="${XPD_AGENT_OUTCOME_RECONCILE_SECONDS:-600}"
export XPD_AGENT_RUN_SHUTDOWN_GRACE_SECONDS="${XPD_AGENT_RUN_SHUTDOWN_GRACE_SECONDS:-30}"
export XPD_FINAL_REFLECTION_TIMEOUT_SECONDS="${XPD_FINAL_REFLECTION_TIMEOUT_SECONDS:-180}"
export XPD_MEMORY_CONSOLIDATION_RATIO="${XPD_MEMORY_CONSOLIDATION_RATIO:-0.8}"
export XPD_MEMORY_CRITICAL_RATIO="${XPD_MEMORY_CRITICAL_RATIO:-0.95}"
export XPD_MEMORY_CONSOLIDATION_TARGET_RATIO="${XPD_MEMORY_CONSOLIDATION_TARGET_RATIO:-0.6}"
export XPD_MEMORY_AUTO_CONSOLIDATION_ENABLED="${XPD_MEMORY_AUTO_CONSOLIDATION_ENABLED:-true}"
export XPD_MEMORY_CONSOLIDATION_SCAN_SECONDS="${XPD_MEMORY_CONSOLIDATION_SCAN_SECONDS:-30}"
export XPD_MEMORY_CONSOLIDATION_MAX_ATTEMPTS="${XPD_MEMORY_CONSOLIDATION_MAX_ATTEMPTS:-3}"
export XPD_MEMORY_CONSOLIDATION_RETRY_COOLDOWN_SECONDS="${XPD_MEMORY_CONSOLIDATION_RETRY_COOLDOWN_SECONDS:-600}"
export XPD_HERMES_CONNECT_MAX_ATTEMPTS="${XPD_HERMES_CONNECT_MAX_ATTEMPTS:-3}"
export XPD_HERMES_RETRY_BASE_SECONDS="${XPD_HERMES_RETRY_BASE_SECONDS:-0.2}"

# These sitecustomize patches belong to the Hermes interpreter. The managed
# launcher passes one shared environment to both services, so explicitly keep
# the project FastAPI interpreter from importing Hermes-only gateway modules.
export XPD_HERMES_REASONING_STREAM_PATCH=false
export XPD_HERMES_CLARIFY_PATCH=false
export XPD_HERMES_REPORT_FILE_PATCH=false
export XPD_HERMES_CRON_PATCH=false
export XPD_HERMES_USER_MEMORY_PATCH=false

export XPD_FILE_STORAGE_PATH="${XPD_FILE_STORAGE_PATH:-$ROOT/data/report-files}"

PROJECT_PYTHON="${PROJECT_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PROJECT_PYTHON" ]; then
  echo "Project Python was not found: $PROJECT_PYTHON" >&2
  exit 1
fi

if [ "$FASTAPI_RELOAD" = "true" ]; then
  exec "$PROJECT_PYTHON" -m uvicorn xpd_report_agent.api.main:app \
    --reload \
    --host "$FASTAPI_HOST" \
    --port "$FASTAPI_PORT"
fi

exec "$PROJECT_PYTHON" -m uvicorn xpd_report_agent.api.main:app \
  --host "$FASTAPI_HOST" \
  --port "$FASTAPI_PORT"

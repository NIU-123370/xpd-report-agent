#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

HERMES_RUNTIME_LOCK="$ROOT/configs/hermes-runtime.lock"
if [ ! -r "$HERMES_RUNTIME_LOCK" ]; then
  echo "Hermes runtime lock was not found: $HERMES_RUNTIME_LOCK" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$HERMES_RUNTIME_LOCK"

if [ -z "${HERMES_AGENT_VERSION:-}" ] || [ -z "${HERMES_AGENT_COMMIT:-}" ]; then
  echo "Hermes runtime lock is incomplete: $HERMES_RUNTIME_LOCK" >&2
  exit 1
fi

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
export HERMES_GATEWAY_ALLOW_ALL_USERS="${HERMES_GATEWAY_ALLOW_ALL_USERS:-true}"
export HERMES_TIMEZONE="${HERMES_TIMEZONE:-Asia/Shanghai}"
if [ -n "${HERMES_LLM_API_KEY:-}" ]; then
  export ALIBABA_CODING_PLAN_API_KEY="${ALIBABA_CODING_PLAN_API_KEY:-$HERMES_LLM_API_KEY}"
  export DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-$HERMES_LLM_API_KEY}"
fi
export MYSQL_HOST="${MYSQL_HOST:-${XPD_DB_HOST:-127.0.0.1}}"
export MYSQL_PORT="${MYSQL_PORT:-${XPD_DB_PORT:-3306}}"
export MYSQL_USER="${MYSQL_USER:-${XPD_DB_USERNAME:-root}}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-${XPD_DB_PASSWORD:-}}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-${XPD_DB_NAME:-taobao_reports_test}}"
export XPD_MYSQL_READ_MAX_ATTEMPTS="${XPD_MYSQL_READ_MAX_ATTEMPTS:-2}"
export XPD_MYSQL_READ_RETRY_BACKOFF_MS="${XPD_MYSQL_READ_RETRY_BACKOFF_MS:-100}"
export HERMES_BOOTSTRAP_ON_START="${HERMES_BOOTSTRAP_ON_START:-false}"
export HERMES_REQUIRE_LLM_API_KEY="${HERMES_REQUIRE_LLM_API_KEY:-true}"
export XPD_MEMORY_ENABLED="${XPD_MEMORY_ENABLED:-true}"
export XPD_PERIODIC_REFLECTION_ENABLED="${XPD_PERIODIC_REFLECTION_ENABLED:-true}"
export XPD_REFLECTION_INTERVAL="${XPD_REFLECTION_INTERVAL:-3}"
export XPD_MEMORY_CHAR_LIMIT="${XPD_MEMORY_CHAR_LIMIT:-2200}"
export XPD_USER_CHAR_LIMIT="${XPD_USER_CHAR_LIMIT:-1375}"
export XPD_MEMORY_CONSOLIDATION_RATIO="${XPD_MEMORY_CONSOLIDATION_RATIO:-0.8}"
export XPD_MEMORY_CRITICAL_RATIO="${XPD_MEMORY_CRITICAL_RATIO:-0.95}"
export XPD_MEMORY_CONSOLIDATION_TARGET_RATIO="${XPD_MEMORY_CONSOLIDATION_TARGET_RATIO:-0.6}"
export XPD_IDENTITY_MODE="${XPD_IDENTITY_MODE:-session_key}"
export XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED="${XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED:-false}"
export XPD_MERCHANT_MEMORY_ENABLED="${XPD_MERCHANT_MEMORY_ENABLED:-true}"
export XPD_MERCHANT_MEMORY_CHAR_LIMIT="${XPD_MERCHANT_MEMORY_CHAR_LIMIT:-2200}"
export XPD_HERMES_REASONING_STREAM_PATCH="${XPD_HERMES_REASONING_STREAM_PATCH:-true}"
export XPD_HERMES_CLARIFY_PATCH="${XPD_HERMES_CLARIFY_PATCH:-true}"
export XPD_CLARIFY_TIMEOUT_SECONDS="${XPD_CLARIFY_TIMEOUT_SECONDS:-300}"
export XPD_HERMES_REPORT_FILE_PATCH="${XPD_HERMES_REPORT_FILE_PATCH:-true}"
export XPD_HERMES_CRON_PATCH="${XPD_HERMES_CRON_PATCH:-false}"
export XPD_HERMES_USER_MEMORY_PATCH="${XPD_HERMES_USER_MEMORY_PATCH:-true}"
export XPD_CRON_MAX_PARALLEL_JOBS="${XPD_CRON_MAX_PARALLEL_JOBS:-1}"
export XPD_SCHEDULES_ENABLED="${XPD_SCHEDULES_ENABLED:-false}"
export XPD_FILE_STORAGE_PATH="${XPD_FILE_STORAGE_PATH:-$ROOT/data/report-files}"
export XPD_FILE_MAX_ARTIFACTS_PER_SESSION="${XPD_FILE_MAX_ARTIFACTS_PER_SESSION:-50}"
export XPD_FILE_MAX_BYTES_PER_ARTIFACT="${XPD_FILE_MAX_BYTES_PER_ARTIFACT:-10485760}"
export XPD_FILE_MAX_TOTAL_BYTES_PER_SESSION="${XPD_FILE_MAX_TOTAL_BYTES_PER_SESSION:-104857600}"
export XPD_FILE_MAX_TOTAL_BYTES_PER_OWNER="${XPD_FILE_MAX_TOTAL_BYTES_PER_OWNER:-524288000}"
export XPD_FILE_MAX_TOTAL_BYTES="${XPD_FILE_MAX_TOTAL_BYTES:-5368709120}"
export XPD_FILE_MIN_FREE_BYTES="${XPD_FILE_MIN_FREE_BYTES:-268435456}"
export XPD_FILE_RETENTION_DAYS="${XPD_FILE_RETENTION_DAYS:-30}"
export XPD_QUERY_RESULT_TTL_SECONDS="${XPD_QUERY_RESULT_TTL_SECONDS:-3600}"
export XPD_QUERY_RESULT_MAX_ENTRIES="${XPD_QUERY_RESULT_MAX_ENTRIES:-500}"
export XPD_QUERY_RESULT_MAX_PER_SESSION="${XPD_QUERY_RESULT_MAX_PER_SESSION:-20}"
export XPD_QUERY_RESULT_MAX_BYTES_PER_RESULT="${XPD_QUERY_RESULT_MAX_BYTES_PER_RESULT:-5242880}"
export XPD_QUERY_RESULT_MAX_TOTAL_BYTES="${XPD_QUERY_RESULT_MAX_TOTAL_BYTES:-52428800}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

export API_SERVER_ENABLED=true
export API_SERVER_HOST="$HERMES_GATEWAY_HOST"
export API_SERVER_PORT="$HERMES_GATEWAY_PORT"
export API_SERVER_KEY="$HERMES_GATEWAY_API_KEY"
export GATEWAY_ALLOW_ALL_USERS="$HERMES_GATEWAY_ALLOW_ALL_USERS"

verify_hermes_runtime() {
  HERMES_BIN="${HERMES_BIN:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"
  if [ ! -x "$HERMES_BIN" ]; then
    echo "Hermes executable was not found: $HERMES_BIN" >&2
    exit 1
  fi

  hermes_version_output="$("$HERMES_BIN" --version 2>&1)" || {
    echo "Unable to read the installed Hermes version." >&2
    exit 1
  }
  hermes_commit_short="${HERMES_AGENT_COMMIT%${HERMES_AGENT_COMMIT#????????}}"

  case "$hermes_version_output" in
    *"Hermes Agent v${HERMES_AGENT_VERSION}"*) ;;
    *)
      echo "Hermes version mismatch. Required v${HERMES_AGENT_VERSION}." >&2
      echo "Installed runtime: $hermes_version_output" >&2
      exit 1
      ;;
  esac
  case "$hermes_version_output" in
    *"upstream ${hermes_commit_short}"*|*"local ${hermes_commit_short}"*) ;;
    *)
      hermes_repo="$(cd "$(dirname "$HERMES_BIN")/../.." && pwd)"
      hermes_git_commit="$(git -C "$hermes_repo" rev-parse HEAD 2>/dev/null || true)"
      if [ "$hermes_git_commit" != "$HERMES_AGENT_COMMIT" ]; then
        echo "Hermes revision mismatch. Required ${HERMES_AGENT_COMMIT}." >&2
        echo "Installed runtime: $hermes_version_output" >&2
        exit 1
      fi
      ;;
  esac
}

sync_project_assets() {
  mkdir -p "$HOME/.hermes/plugins/db-query" "$HOME/.hermes/skills/db-multitable-query"
  cp -R src/xpd_report_agent/hermes_plugin/db_query/. "$HOME/.hermes/plugins/db-query/"
  cp skills/db-multitable-query/SKILL.md "$HOME/.hermes/skills/db-multitable-query/SKILL.md"
}

configure_hermes_runtime() {
  PROJECT_PYTHON="${PROJECT_PYTHON:-$ROOT/.venv/bin/python}"
  if [ ! -x "$PROJECT_PYTHON" ]; then
    echo "Project Python was not found: $PROJECT_PYTHON" >&2
    exit 1
  fi
  if [ "$HERMES_REQUIRE_LLM_API_KEY" = "true" ]; then
    XPD_HERMES_REASONING_STREAM_PATCH=false \
      XPD_HERMES_CLARIFY_PATCH=false \
      XPD_HERMES_REPORT_FILE_PATCH=false \
      XPD_HERMES_CRON_PATCH=false \
      XPD_HERMES_USER_MEMORY_PATCH=false \
      "$PROJECT_PYTHON" scripts/configure_hermes.py --require-model-key
  else
    XPD_HERMES_REASONING_STREAM_PATCH=false \
      XPD_HERMES_CLARIFY_PATCH=false \
      XPD_HERMES_REPORT_FILE_PATCH=false \
      XPD_HERMES_CRON_PATCH=false \
      XPD_HERMES_USER_MEMORY_PATCH=false \
      "$PROJECT_PYTHON" scripts/configure_hermes.py
  fi
}

prepare_hermes() {
  HERMES_PY="${HERMES_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
  HERMES_BIN="${HERMES_BIN:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"
  if [ ! -x "$HERMES_PY" ]; then
    echo "Hermes Python was not found: $HERMES_PY" >&2
    echo "Install Hermes or set HERMES_PY to its Python interpreter." >&2
    exit 1
  fi
  if [ ! -x "$HERMES_BIN" ]; then
    echo "Hermes executable was not found: $HERMES_BIN" >&2
    exit 1
  fi
  verify_hermes_runtime

  if command -v uv >/dev/null 2>&1; then
    plugin_requirements="$(mktemp -t xpd-report-agent-hermes.XXXXXX)"
    trap 'rm -f "$plugin_requirements"' EXIT
    uv export \
      --only-group hermes-plugin \
      --no-hashes \
      --no-header \
      --output-file "$plugin_requirements"
    uv pip install --python "$HERMES_PY" -r "$plugin_requirements"
    rm -f "$plugin_requirements"
    trap - EXIT
  else
    "$HERMES_PY" -c 'import alibabacloud_oss_v2, openpyxl, pymysql, pypdf, reportlab, sqlglot, yaml' || {
      echo "uv is unavailable and Hermes plugin dependencies are missing." >&2
      echo "Install uv, then run this prepare command again." >&2
      exit 1
    }
  fi

  sync_project_assets
  configure_hermes_runtime
  "$HERMES_BIN" plugins enable db-query
}

run_hermes() {
  if [ "$HERMES_BOOTSTRAP_ON_START" = "true" ]; then
    prepare_hermes
  else
    verify_hermes_runtime
    # Code and Skill updates are local assets and must not require a dependency
    # bootstrap or network access before every restart.
    sync_project_assets
    configure_hermes_runtime
  fi
  HERMES_BIN="${HERMES_BIN:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"
  if [ ! -x "$HERMES_BIN" ]; then
    echo "Hermes executable was not found: $HERMES_BIN" >&2
    exit 1
  fi
  exec "$HERMES_BIN" gateway run --external-supervisor
}

case "${1:-run}" in
  verify)
    verify_hermes_runtime
    ;;
  prepare)
    prepare_hermes
    ;;
  run)
    run_hermes
    ;;
  *)
    echo "Usage: $0 [verify|prepare|run]" >&2
    exit 2
    ;;
esac

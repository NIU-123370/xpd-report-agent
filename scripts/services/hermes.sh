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
export HERMES_GATEWAY_ALLOW_ALL_USERS="${HERMES_GATEWAY_ALLOW_ALL_USERS:-true}"
export MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
export MYSQL_PORT="${MYSQL_PORT:-3306}"
export MYSQL_USER="${MYSQL_USER:-root}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-taobao_reports_test}"
export HERMES_BOOTSTRAP_ON_START="${HERMES_BOOTSTRAP_ON_START:-true}"
export HERMES_REQUIRE_LLM_API_KEY="${HERMES_REQUIRE_LLM_API_KEY:-true}"
export XPD_MEMORY_ENABLED="${XPD_MEMORY_ENABLED:-true}"
export XPD_PERIODIC_REFLECTION_ENABLED="${XPD_PERIODIC_REFLECTION_ENABLED:-true}"
export XPD_REFLECTION_INTERVAL="${XPD_REFLECTION_INTERVAL:-3}"
export XPD_MEMORY_CHAR_LIMIT="${XPD_MEMORY_CHAR_LIMIT:-2200}"
export XPD_USER_CHAR_LIMIT="${XPD_USER_CHAR_LIMIT:-1375}"
export XPD_HERMES_REASONING_STREAM_PATCH="${XPD_HERMES_REASONING_STREAM_PATCH:-true}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

export API_SERVER_ENABLED=true
export API_SERVER_HOST="$HERMES_GATEWAY_HOST"
export API_SERVER_PORT="$HERMES_GATEWAY_PORT"
export API_SERVER_KEY="$HERMES_GATEWAY_API_KEY"
export GATEWAY_ALLOW_ALL_USERS="$HERMES_GATEWAY_ALLOW_ALL_USERS"

prepare_hermes() {
  HERMES_PY="${HERMES_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
  if [ ! -x "$HERMES_PY" ]; then
    echo "Hermes Python was not found: $HERMES_PY" >&2
    echo "Install Hermes or set HERMES_PY to its Python interpreter." >&2
    exit 1
  fi

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

  mkdir -p "$HOME/.hermes/plugins/db-query" "$HOME/.hermes/skills/db-multitable-query"
  cp -R src/xpd_report_agent/hermes_plugin/db_query/. "$HOME/.hermes/plugins/db-query/"
  cp skills/db-multitable-query/SKILL.md "$HOME/.hermes/skills/db-multitable-query/SKILL.md"

  if [ "$HERMES_REQUIRE_LLM_API_KEY" = "true" ]; then
    uv run python scripts/configure_hermes.py --require-model-key
  else
    uv run python scripts/configure_hermes.py
  fi
  hermes plugins enable db-query
}

run_hermes() {
  if [ "$HERMES_BOOTSTRAP_ON_START" = "true" ]; then
    prepare_hermes
  fi
  HERMES_BIN="${HERMES_BIN:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"
  if [ ! -x "$HERMES_BIN" ]; then
    echo "Hermes executable was not found: $HERMES_BIN" >&2
    exit 1
  fi
  exec "$HERMES_BIN" gateway run --external-supervisor
}

case "${1:-run}" in
  prepare)
    prepare_hermes
    ;;
  run)
    run_hermes
    ;;
  *)
    echo "Usage: $0 [prepare|run]" >&2
    exit 2
    ;;
esac

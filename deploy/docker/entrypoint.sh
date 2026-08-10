#!/usr/bin/env bash
set -euo pipefail

export LAUNCH_MANAGED=true

/app/.venv/bin/python -m xpd_report_agent.runtime.deployment_preflight --quiet

case "${XPD_CONTAINER_ROLE:-}" in
  hermes)
    export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-0.0.0.0}"
    exec /app/scripts/services/hermes.sh run
    ;;
  fastapi)
    export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-hermes}"
    export FASTAPI_HOST="${FASTAPI_HOST:-0.0.0.0}"
    exec /app/scripts/services/fastapi.sh run
    ;;
  *)
    echo "XPD_CONTAINER_ROLE must be set to 'hermes' or 'fastapi'." >&2
    exit 2
    ;;
esac

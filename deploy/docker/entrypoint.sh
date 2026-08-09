#!/usr/bin/env bash
set -euo pipefail

export LAUNCH_MANAGED=true
export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-127.0.0.1}"
export FASTAPI_HOST="${FASTAPI_HOST:-0.0.0.0}"

/app/.venv/bin/python -m xpd_report_agent.runtime.deployment_preflight --quiet

hermes_pid=""
fastapi_pid=""

shutdown() {
  if [ -n "$fastapi_pid" ]; then kill -TERM "$fastapi_pid" 2>/dev/null || true; fi
  if [ -n "$hermes_pid" ]; then kill -TERM "$hermes_pid" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}
trap shutdown TERM INT EXIT

/app/scripts/services/hermes.sh run &
hermes_pid=$!
export XPD_HERMES_STARTUP_PID="$hermes_pid"

/app/.venv/bin/python - <<'PY'
import os
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

configured_host = os.environ["HERMES_GATEWAY_HOST"].strip()
probe_host = "127.0.0.1" if configured_host in {"", "0.0.0.0"} else configured_host
if probe_host in {"::", "[::]"}:
    probe_host = "[::1]"
elif ":" in probe_host and not probe_host.startswith("["):
    probe_host = f"[{probe_host}]"
url = f"http://{probe_host}:{os.getenv('HERMES_GATEWAY_PORT', '8642')}/v1/health"
request = Request(url, headers={"Authorization": f"Bearer {os.environ['HERMES_GATEWAY_API_KEY']}"})
opener = build_opener(ProxyHandler({}))
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        os.kill(int(os.environ["XPD_HERMES_STARTUP_PID"]), 0)
    except (OSError, ValueError):
        raise SystemExit("Hermes Gateway exited before becoming healthy")
    try:
        with opener.open(request, timeout=2) as response:
            if 200 <= response.status < 300:
                raise SystemExit(0)
    except (OSError, URLError):
        time.sleep(1)
raise SystemExit("Hermes Gateway did not become healthy within 120 seconds")
PY

/app/scripts/services/fastapi.sh run &
fastapi_pid=$!

wait -n "$hermes_pid" "$fastapi_pid"
exit_code=$?
exit "$exit_code"

#!/usr/bin/env bash
set -euo pipefail

export LAUNCH_MANAGED=true
export HERMES_GATEWAY_HOST="${HERMES_GATEWAY_HOST:-127.0.0.1}"
export FASTAPI_HOST="${FASTAPI_HOST:-0.0.0.0}"

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

python - <<'PY'
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

url = f"http://{os.environ['HERMES_GATEWAY_HOST']}:{os.getenv('HERMES_GATEWAY_PORT', '8642')}/v1/health"
request = Request(url, headers={"Authorization": f"Bearer {os.environ['HERMES_GATEWAY_API_KEY']}"})
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        with urlopen(request, timeout=2) as response:
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

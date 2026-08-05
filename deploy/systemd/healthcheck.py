#!/usr/bin/env python3
"""Wait for a managed XPD service to accept authenticated HTTP requests."""

from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def _probe_host(configured_host: str) -> str:
    host = configured_host.strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _probe_config(service: str) -> tuple[str, dict[str, str]]:
    if service == "hermes":
        host = _probe_host(os.getenv("HERMES_GATEWAY_HOST", "127.0.0.1"))
        port = os.getenv("HERMES_GATEWAY_PORT", "8642")
        api_key = os.getenv("HERMES_GATEWAY_API_KEY", "").strip()
        if not api_key:
            raise ValueError("HERMES_GATEWAY_API_KEY is required")
        return (
            f"http://{host}:{port}/v1/health",
            {"Authorization": f"Bearer {api_key}"},
        )

    host = _probe_host(os.getenv("FASTAPI_HOST", "127.0.0.1"))
    port = os.getenv("FASTAPI_PORT", "8000")
    return f"http://{host}:{port}/health", {}


def wait_until_ready(service: str, timeout_seconds: float) -> int:
    try:
        url, headers = _probe_config(service)
    except ValueError as exc:
        print(f"{service} startup probe configuration error: {exc}", file=sys.stderr)
        return 2

    deadline = time.monotonic() + timeout_seconds
    last_error = "service did not answer"
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers=headers)
            with opener.open(request, timeout=2.0) as response:
                if 200 <= response.status < 300:
                    print(f"{service} startup probe passed")
                    return 0
                last_error = f"HTTP {response.status}"
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (OSError, TimeoutError, URLError) as exc:
            last_error = str(exc)
        time.sleep(1.0)

    print(
        f"{service} startup probe failed after {timeout_seconds:g}s: {last_error}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("hermes", "fastapi"))
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return wait_until_ready(args.service, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

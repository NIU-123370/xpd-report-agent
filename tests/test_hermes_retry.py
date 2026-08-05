from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from xpd_report_agent.api import sessions as sessions_api


class SequenceClient:
    outcomes = []
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, headers=None, json=None):
        type(self).calls += 1
        outcome = type(self).outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(
            outcome,
            json={"ok": outcome < 400},
            request=httpx.Request(method, url),
        )


def configure(monkeypatch, outcomes):
    SequenceClient.outcomes = list(outcomes)
    SequenceClient.calls = 0
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-test-key")
    monkeypatch.setenv("XPD_HERMES_CONNECT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("XPD_HERMES_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr(sessions_api.httpx, "AsyncClient", SequenceClient)


def connect_error():
    return httpx.ConnectError(
        "connection refused",
        request=httpx.Request("POST", "http://127.0.0.1:8642/api/test"),
    )


def test_post_retries_only_failures_that_never_connected(monkeypatch):
    configure(monkeypatch, [connect_error(), connect_error(), 200])

    result = asyncio.run(
        sessions_api._hermes_json(
            "POST",
            "/api/test",
            payload={"value": 1},
            action="submit test",
        )
    )

    assert result == {"ok": True}
    assert SequenceClient.calls == 3


def test_safe_get_retries_transient_upstream_status(monkeypatch):
    configure(monkeypatch, [503, 503, 200])

    result = asyncio.run(
        sessions_api._hermes_json("GET", "/api/test", action="read test")
    )

    assert result == {"ok": True}
    assert SequenceClient.calls == 3


def test_post_is_not_blindly_replayed_after_upstream_accepted_it(monkeypatch):
    configure(monkeypatch, [503, 200])

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            sessions_api._hermes_json(
                "POST",
                "/api/test",
                payload={"value": 1},
                action="submit test",
            )
        )

    assert SequenceClient.calls == 1
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "HERMES_UNAVAILABLE"
    assert caught.value.detail["retryable"] is True
    assert caught.value.detail["outcome_unknown"] is True

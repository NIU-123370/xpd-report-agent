from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from xpd_report_agent.runtime import hermes_clarify


class FakeWeb:
    @staticmethod
    def json_response(payload, status=200):
        return SimpleNamespace(payload=payload, status=status)


class FakeRequest:
    def __init__(self, *, session_id="xpd_owner_session", clarification_id="clarify_1"):
        self.match_info = {
            "session_id": session_id,
            "clarification_id": clarification_id,
        }
        self.scope = "owner-scope"
        self.body = {"answer": "按支付金额"}
        self.auth_error = None


class FakeAdapter:
    def _create_agent(
        self,
        ephemeral_system_prompt=None,
        session_id=None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key=None,
        route=None,
    ):
        return SimpleNamespace(
            session_id=session_id,
            clarify_callback=None,
            tools=[
                {"type": "function", "function": {"name": "clarify"}},
                {"type": "function", "function": {"name": "db_get_schema_ddl"}},
            ],
            valid_tool_names={"clarify", "db_get_schema_ddl"},
        )

    def _http_route_table(self):
        return [("GET", "/health", lambda request: None)]

    async def _handle_session_chat_stream(self, request):
        return SimpleNamespace(status=200)

    def _check_auth(self, request):
        return request.auth_error

    def _parse_session_key_header(self, request):
        return request.scope, None

    async def _read_json_body(self, request):
        return request.body, None


def install_fake_gateway(monkeypatch):
    api_server = ModuleType("gateway.platforms.api_server")
    api_server.APIServerAdapter = FakeAdapter
    api_server.web = FakeWeb
    platforms = ModuleType("gateway.platforms")
    platforms.api_server = api_server
    gateway = ModuleType("gateway")
    gateway.platforms = platforms
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.api_server", api_server)
    hermes_clarify.install_patch()


def test_registry_requires_matching_session_and_owner_scope():
    registry = hermes_clarify.ClarificationRegistry()
    item = registry.create(
        session_id="session-1",
        owner_scope="owner-1",
        question="按哪个指标排序？",
        choices=["支付金额", "订单数"],
        timeout_seconds=30,
    )

    assert registry.answer(
        clarification_id=item.clarification_id,
        session_id="session-2",
        owner_scope="owner-1",
        answer="支付金额",
    ) == "not_found"
    assert registry.answer(
        clarification_id=item.clarification_id,
        session_id="session-1",
        owner_scope="owner-2",
        answer="支付金额",
    ) == "not_found"
    assert registry.answer(
        clarification_id=item.clarification_id,
        session_id="session-1",
        owner_scope="owner-1",
        answer="支付金额",
    ) == "answered"
    assert registry.wait(item) == ("answered", "支付金额")


def test_registry_expires_only_matching_owned_session():
    registry = hermes_clarify.ClarificationRegistry()
    owned = registry.create(
        session_id="session-1",
        owner_scope="owner-1",
        question="按哪个指标排序？",
        choices=None,
        timeout_seconds=30,
    )
    other = registry.create(
        session_id="session-2",
        owner_scope="owner-1",
        question="按哪个指标排序？",
        choices=None,
        timeout_seconds=30,
    )

    assert registry.expire_session(session_id="session-1", owner_scope="owner-1") == 1
    assert registry.wait(owned) == ("expired", None)
    assert other.ready.is_set() is False


def test_patch_emits_contract_events_and_registers_routes(monkeypatch):
    registry = hermes_clarify.ClarificationRegistry()
    monkeypatch.setattr(hermes_clarify, "clarification_registry", registry)
    monkeypatch.setenv("XPD_CLARIFY_TIMEOUT_SECONDS", "300")
    install_fake_gateway(monkeypatch)
    events = []

    def progress(event_type, tool_name, preview, args):
        events.append((event_type, tool_name, preview, args))
        if event_type == "tool.started" and tool_name == "_xpd_clarify":
            assert registry.answer(
                clarification_id=args["clarification_id"],
                session_id="xpd_owner_session",
                owner_scope="owner-scope",
                answer="按支付金额",
            ) == "answered"

    progress.__qualname__ = (
        "APIServerAdapter._handle_session_chat_stream.<locals>._tool_progress"
    )
    adapter = FakeAdapter()
    agent = adapter._create_agent(
        session_id="xpd_owner_session",
        gateway_session_key="owner-scope",
        tool_progress_callback=progress,
    )

    assert agent.clarify_callback("按哪个指标排序？", ["支付金额", "订单数"]) == "按支付金额"
    assert events[0][0:2] == ("tool.started", "_xpd_clarify")
    assert set(events[0][3]) == {
        "clarification_id",
        "question",
        "choices",
        "timeout_seconds",
        "expires_at",
    }
    assert events[1][0:2] == ("tool.completed", "_xpd_clarify")
    assert events[1][3]["status"] == "answered"
    routes = {(method, path) for method, path, _ in adapter._http_route_table()}
    assert ("GET", "/api/clarifications/health") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/clarifications/{clarification_id}/answer",
    ) in routes


def test_patch_does_not_block_unsupported_progress_transport(monkeypatch):
    monkeypatch.setattr(
        hermes_clarify,
        "clarification_registry",
        hermes_clarify.ClarificationRegistry(),
    )
    install_fake_gateway(monkeypatch)

    def ignored_progress(event_type, tool_name, preview, args):
        return None

    ignored_progress.__qualname__ = (
        "APIServerAdapter._handle_responses.<locals>._on_tool_progress"
    )
    adapter = FakeAdapter()
    agent = adapter._create_agent(
        session_id="xpd_owner_session",
        gateway_session_key="owner-scope",
        tool_progress_callback=ignored_progress,
    )

    assert agent.clarify_callback is None
    assert "clarify" not in agent.valid_tool_names
    assert [tool["function"]["name"] for tool in agent.tools] == [
        "db_get_schema_ddl"
    ]


def test_timeout_interrupts_turn_before_dependent_tools(monkeypatch):
    registry = hermes_clarify.ClarificationRegistry()
    monkeypatch.setattr(hermes_clarify, "clarification_registry", registry)
    monkeypatch.setattr(registry, "wait", lambda item: ("expired", None))
    install_fake_gateway(monkeypatch)

    def progress(event_type, tool_name, preview, args):
        return None

    progress.__qualname__ = (
        "APIServerAdapter._handle_session_chat_stream.<locals>._tool_progress"
    )
    adapter = FakeAdapter()
    agent = adapter._create_agent(
        session_id="xpd_owner_session",
        gateway_session_key="owner-scope",
        tool_progress_callback=progress,
    )
    interruptions = []
    agent.interrupt = interruptions.append

    with pytest.raises(TimeoutError):
        agent.clarify_callback("按哪个指标排序？", ["支付金额", "订单数"])

    assert interruptions == [
        "澄清请求已超时，本轮分析已停止，未执行依赖该答案的数据库查询。"
    ]


def test_gateway_answer_handler_rejects_wrong_session_scope(monkeypatch):
    registry = hermes_clarify.ClarificationRegistry()
    monkeypatch.setattr(hermes_clarify, "clarification_registry", registry)
    install_fake_gateway(monkeypatch)
    item = registry.create(
        session_id="xpd_owner_session",
        owner_scope="owner-scope",
        question="按哪个指标？",
        choices=None,
        timeout_seconds=30,
    )
    request = FakeRequest(
        session_id="xpd_other_session",
        clarification_id=item.clarification_id,
    )
    adapter = FakeAdapter()

    response = asyncio.run(adapter._xpd_handle_clarification_answer(request))

    assert response.status == 404
    assert item.ready.is_set() is False

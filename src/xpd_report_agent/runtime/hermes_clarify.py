from __future__ import annotations

import hmac
import inspect
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal

CLARIFY_TOOL_NAME = "_xpd_clarify"
MAX_ANSWER_LENGTH = 2000
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600


def clarify_timeout_seconds() -> int:
    """Return the bounded time an API request may wait for clarification."""
    try:
        configured = int(os.getenv("XPD_CLARIFY_TIMEOUT_SECONDS", "300"))
    except ValueError:
        configured = DEFAULT_TIMEOUT_SECONDS
    return min(MAX_TIMEOUT_SECONDS, max(1, configured))


def _secure_equal(left: str, right: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII request input."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass
class PendingClarification:
    clarification_id: str
    session_id: str
    owner_scope: str
    question: str
    choices: list[str]
    timeout_seconds: int
    expires_at: float
    status: Literal["pending", "answered", "expired"] = "pending"
    answer: str | None = None
    ready: threading.Event = field(default_factory=threading.Event, repr=False)


class ClarificationRegistry:
    """Thread-safe rendezvous between Hermes worker threads and HTTP answers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, PendingClarification] = {}

    def create(
        self,
        *,
        session_id: str,
        owner_scope: str,
        question: str,
        choices: list[str] | None,
        timeout_seconds: int,
    ) -> PendingClarification:
        now = time.time()
        item = PendingClarification(
            clarification_id=f"clarify_{uuid.uuid4().hex}",
            session_id=session_id,
            owner_scope=owner_scope,
            question=question.strip(),
            choices=[str(choice).strip() for choice in (choices or []) if str(choice).strip()][
                :4
            ],
            timeout_seconds=timeout_seconds,
            expires_at=now + timeout_seconds,
        )
        with self._lock:
            self._pending[item.clarification_id] = item
        return item

    def discard(self, clarification_id: str) -> None:
        with self._lock:
            item = self._pending.pop(clarification_id, None)
            if item is not None:
                item.status = "expired"
                item.ready.set()

    def expire_session(self, *, session_id: str, owner_scope: str) -> int:
        """Wake every pending question for one owned session as expired."""
        expired = 0
        with self._lock:
            for item in self._pending.values():
                if item.status != "pending":
                    continue
                if not _secure_equal(item.session_id, session_id):
                    continue
                if not _secure_equal(item.owner_scope, owner_scope):
                    continue
                item.status = "expired"
                item.answer = None
                item.ready.set()
                expired += 1
        return expired

    def wait(self, item: PendingClarification) -> tuple[str, str | None]:
        remaining = max(0.0, item.expires_at - time.time())
        item.ready.wait(remaining)
        with self._lock:
            current = self._pending.get(item.clarification_id)
            if current is None:
                return "expired", None
            # An answer that won the lock at the timeout boundary remains valid.
            if current.status != "answered":
                current.status = "expired"
                current.answer = None
            self._pending.pop(item.clarification_id, None)
            return current.status, current.answer

    def answer(
        self,
        *,
        clarification_id: str,
        session_id: str,
        owner_scope: str,
        answer: str,
    ) -> Literal["answered", "expired", "not_found", "conflict"]:
        with self._lock:
            item = self._pending.get(clarification_id)
            if item is None:
                return "not_found"
            if not _secure_equal(item.session_id, session_id) or not _secure_equal(
                item.owner_scope, owner_scope
            ):
                # Do not reveal whether another owner has a pending question.
                return "not_found"
            if item.status != "pending":
                return "conflict"
            if time.time() >= item.expires_at:
                item.status = "expired"
                item.ready.set()
                return "expired"
            item.answer = answer
            item.status = "answered"
            item.ready.set()
            return "answered"

    def pending_count(self) -> int:
        with self._lock:
            now = time.time()
            return sum(
                item.status == "pending" and item.expires_at > now
                for item in self._pending.values()
            )


clarification_registry = ClarificationRegistry()


def _disable_clarify_tool(agent: Any) -> None:
    """Hide clarify from transports that cannot deliver and resume a prompt."""
    tools = getattr(agent, "tools", None)
    if isinstance(tools, list):
        agent.tools = [
            tool
            for tool in tools
            if not (
                isinstance(tool, dict)
                and isinstance(tool.get("function"), dict)
                and tool["function"].get("name") == "clarify"
            )
        ]
    valid_tool_names = getattr(agent, "valid_tool_names", None)
    if hasattr(valid_tool_names, "discard"):
        valid_tool_names.discard("clarify")


def _interrupt_after_failed_clarification(agent: Any, message: str) -> None:
    """Hard-stop the turn so a failed clarification cannot reach DB tools."""
    interrupt = getattr(agent, "interrupt", None)
    if callable(interrupt):
        interrupt(message)
        return
    # Test doubles and older Hermes builds may not expose interrupt(). The
    # conversation loop checks this flag before starting the next iteration.
    setattr(agent, "_interrupt_requested", True)


def _callback_arguments(
    original_create_agent: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        bound = inspect.signature(original_create_agent).bind_partial(self, *args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _supports_session_clarification(progress_callback: Any) -> bool:
    """Only the persisted Session SSE transport preserves our event args.

    Other Hermes API surfaces also pass a progress callback, but currently
    either discard progress entirely (/v1/responses) or omit ``args``
    (/v1/runs). Installing a blocking clarify callback there would leave the
    caller without the clarification id needed to answer it.
    """
    qualname = str(getattr(progress_callback, "__qualname__", ""))
    return "_handle_session_chat_stream.<locals>._tool_progress" in qualname


def _event_args(item: PendingClarification, *, status: str | None = None) -> dict[str, Any]:
    if status is not None:
        return {"clarification_id": item.clarification_id, "status": status}
    return {
        "clarification_id": item.clarification_id,
        "question": item.question,
        "choices": item.choices,
        "timeout_seconds": item.timeout_seconds,
        "expires_at": item.expires_at,
    }


def install_patch() -> None:
    """Add interactive clarification support to Hermes' API Server adapter."""
    from gateway.platforms import api_server as api_server_module

    APIServerAdapter = api_server_module.APIServerAdapter
    web = api_server_module.web

    if getattr(APIServerAdapter, "_xpd_clarify_patch", False):
        return

    original_create_agent = APIServerAdapter._create_agent
    original_route_table = APIServerAdapter._http_route_table
    original_session_chat_stream = APIServerAdapter._handle_session_chat_stream

    @wraps(original_create_agent)
    def create_agent_with_clarify(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original_create_agent(self, *args, **kwargs)
        arguments = _callback_arguments(original_create_agent, self, args, kwargs)
        progress_callback = arguments.get("tool_progress_callback")
        session_id = arguments.get("session_id") or getattr(agent, "session_id", None)
        owner_scope = arguments.get("gateway_session_key")
        if not (
            callable(progress_callback)
            and _supports_session_clarification(progress_callback)
            and isinstance(session_id, str)
            and session_id
            and isinstance(owner_scope, str)
            and owner_scope
        ):
            _disable_clarify_tool(agent)
            return agent

        timeout_seconds = clarify_timeout_seconds()
        # A refreshed/retried stream for the same owned session supersedes a
        # stale question from the disconnected request. Its waiting agent is
        # interrupted by the expired result below before the new run proceeds.
        clarification_registry.expire_session(
            session_id=session_id,
            owner_scope=owner_scope,
        )

        def clarify_callback(question: str, choices: list[str] | None = None) -> str:
            item = clarification_registry.create(
                session_id=session_id,
                owner_scope=owner_scope,
                question=str(question),
                choices=choices,
                timeout_seconds=timeout_seconds,
            )
            try:
                progress_callback(
                    "tool.started",
                    CLARIFY_TOOL_NAME,
                    item.question,
                    _event_args(item),
                )
            except Exception:
                clarification_registry.discard(item.clarification_id)
                _interrupt_after_failed_clarification(
                    agent,
                    "澄清问题未能送达，本轮分析已停止，未执行依赖该答案的数据库查询。",
                )
                raise

            status, answer = clarification_registry.wait(item)
            try:
                progress_callback(
                    "tool.completed",
                    CLARIFY_TOOL_NAME,
                    "",
                    _event_args(item, status=status),
                )
            except Exception:
                # The waiting request is already resolved. A closed SSE transport
                # must not turn a valid user answer into a failed tool call.
                pass
            if status == "answered" and answer is not None:
                return answer
            _interrupt_after_failed_clarification(
                agent,
                "澄清请求已超时，本轮分析已停止，未执行依赖该答案的数据库查询。",
            )
            raise TimeoutError("澄清请求已超时，未收到用户回答。")

        agent.clarify_callback = clarify_callback
        return agent

    @wraps(original_session_chat_stream)
    async def session_chat_stream_with_cleanup(
        self: Any, request: Any, *args: Any, **kwargs: Any
    ) -> Any:
        limiter = getattr(self, "_concurrency_limited_response", None)
        if callable(limiter):
            limited = limiter()
            if limited is not None:
                return limited
        try:
            return await original_session_chat_stream(self, request, *args, **kwargs)
        finally:
            session_id = request.match_info.get("session_id")
            owner_scope, key_err = self._parse_session_key_header(request)
            if (
                key_err is None
                and isinstance(session_id, str)
                and session_id
                and isinstance(owner_scope, str)
                and owner_scope
            ):
                clarification_registry.expire_session(
                    session_id=session_id,
                    owner_scope=owner_scope,
                )

    async def handle_clarification_health(self: Any, request: Any) -> Any:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        return web.json_response(
            {
                "ok": True,
                "enabled": True,
                "pending_count": clarification_registry.pending_count(),
                "timeout_seconds": clarify_timeout_seconds(),
                "max_answer_length": MAX_ANSWER_LENGTH,
            }
        )

    async def handle_clarification_answer(self: Any, request: Any) -> Any:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        owner_scope, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        if not owner_scope:
            return web.json_response(
                {"error": {"message": "X-Hermes-Session-Key is required."}},
                status=400,
            )

        body, body_err = await self._read_json_body(request)
        if body_err is not None:
            return body_err
        answer = body.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return web.json_response(
                {"error": {"message": "answer must be a non-empty string."}},
                status=400,
            )
        answer = answer.strip()
        if len(answer) > MAX_ANSWER_LENGTH:
            return web.json_response(
                {
                    "error": {
                        "message": f"answer must not exceed {MAX_ANSWER_LENGTH} characters."
                    }
                },
                status=400,
            )

        clarification_id = request.match_info["clarification_id"]
        session_id = request.match_info["session_id"]
        result = clarification_registry.answer(
            clarification_id=clarification_id,
            session_id=session_id,
            owner_scope=owner_scope,
            answer=answer,
        )
        if result == "not_found":
            return web.json_response(
                {"error": {"message": "Clarification not found."}}, status=404
            )
        if result in {"expired", "conflict"}:
            message = (
                "Clarification has expired."
                if result == "expired"
                else "Clarification has already been answered."
            )
            return web.json_response({"error": {"message": message}}, status=409)
        return web.json_response(
            {
                "ok": True,
                "session_id": session_id,
                "clarification_id": clarification_id,
                "status": "answered",
            }
        )

    @wraps(original_route_table)
    def route_table_with_clarify(self: Any) -> list[tuple[Any, ...]]:
        routes = list(original_route_table(self))
        additions = (
            (
                "GET",
                "/api/clarifications/health",
                self._xpd_handle_clarification_health,
            ),
            (
                "POST",
                "/api/sessions/{session_id}/clarifications/{clarification_id}/answer",
                self._xpd_handle_clarification_answer,
            ),
        )
        existing = {(method, path) for method, path, *_ in routes}
        routes.extend(route for route in additions if route[:2] not in existing)
        return routes

    APIServerAdapter._xpd_handle_clarification_health = handle_clarification_health
    APIServerAdapter._xpd_handle_clarification_answer = handle_clarification_answer
    APIServerAdapter._create_agent = create_agent_with_clarify
    APIServerAdapter._handle_session_chat_stream = session_chat_stream_with_cleanup
    APIServerAdapter._http_route_table = route_table_with_clarify
    APIServerAdapter._xpd_clarify_patch = True

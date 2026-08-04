from __future__ import annotations

from functools import wraps
from typing import Any


def install_patch() -> None:
    """Expose provider reasoning deltas on Hermes Session SSE streams."""
    from gateway.platforms.api_server import APIServerAdapter

    if getattr(APIServerAdapter, "_xpd_reasoning_stream_patch", False):
        return

    original_create_agent = APIServerAdapter._create_agent

    @wraps(original_create_agent)
    def create_agent_with_reasoning(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original_create_agent(self, *args, **kwargs)
        progress_callback = kwargs.get("tool_progress_callback")
        if progress_callback is None:
            return agent

        existing_callback = getattr(agent, "reasoning_callback", None)

        def reasoning_callback(text: str) -> None:
            if existing_callback is not None:
                existing_callback(text)
            if isinstance(text, str) and text:
                progress_callback(
                    "reasoning.available",
                    "_thinking",
                    text,
                    None,
                )

        agent.reasoning_callback = reasoning_callback
        return agent

    APIServerAdapter._create_agent = create_agent_with_reasoning
    APIServerAdapter._xpd_reasoning_stream_patch = True

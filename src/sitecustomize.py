from __future__ import annotations

import os


if os.getenv("XPD_HERMES_REASONING_STREAM_PATCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from xpd_report_agent.runtime.hermes_reasoning_stream import install_patch

    install_patch()

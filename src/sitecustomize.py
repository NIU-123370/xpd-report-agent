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


if os.getenv("XPD_HERMES_CLARIFY_PATCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from xpd_report_agent.runtime.hermes_clarify import install_patch

    install_patch()


if os.getenv("XPD_HERMES_REPORT_FILE_PATCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from xpd_report_agent.runtime.hermes_report_files import install_patch

    install_patch()


if os.getenv("XPD_HERMES_CRON_PATCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from xpd_report_agent.runtime.hermes_cron import install_patch

    install_patch()


if os.getenv("XPD_HERMES_USER_MEMORY_PATCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from xpd_report_agent.runtime.hermes_user_memory import install_patch

    install_patch()

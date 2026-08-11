from __future__ import annotations

import os

# This must run before the API Server patches below import Hermes gateway
# modules, because gateway.run imports the Cron resolver by value.
from xpd_report_agent.runtime.hermes_cron_scheduler import (
    install_patch as install_cron_scheduler_patch,
)

install_cron_scheduler_patch()

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

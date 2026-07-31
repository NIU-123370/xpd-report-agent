from __future__ import annotations

from datetime import date

import pytest

from xpd_report_agent.demo.create_database import create_database
from xpd_report_agent.hermes_plugin.db_query.db import clear_schema_cache


@pytest.fixture()
def demo_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "demo_ecommerce.sqlite"
    create_database(db_path, base_date=date(2026, 6, 14))
    monkeypatch.setenv("HERMES_DEMO_SQLITE_PATH", str(db_path))
    clear_schema_cache()
    yield db_path
    clear_schema_cache()

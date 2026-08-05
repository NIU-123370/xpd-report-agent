from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stable_test_identity_mode(monkeypatch):
    # Tests opt into user_id explicitly. Never let a developer's ignored
    # configs/local.env change the baseline assumptions of unrelated tests.
    monkeypatch.setenv("XPD_IDENTITY_MODE", "session_key")
    monkeypatch.setenv("XPD_REPORT_OSS_ENABLED", "false")


def _columns(*names: str, primary_key: tuple[str, ...] = ()) -> list[dict]:
    return [
        {
            "cid": index,
            "name": name,
            "type": "varchar(32)" if name.endswith("_id") else "decimal(20,2)",
            "notnull": name in primary_key,
            "default": None,
            "pk": name in primary_key,
        }
        for index, name in enumerate(names)
    ]


@pytest.fixture()
def report_schema(monkeypatch):
    relationship = {
        "from_table": "tb_live_goods_session_stats",
        "from_column": "live_session_id",
        "to_table": "tb_session_endtime_stats",
        "to_column": "live_session_id",
        "logical": True,
    }
    schema = {
        "tables": {
            "tb_live_goods_daily_stats": {
                "name": "tb_live_goods_daily_stats",
                "columns": _columns(
                    "item_id",
                    "stat_date",
                    "item_title",
                    "pay_amt",
                    "refund_amt",
                    primary_key=("item_id", "stat_date"),
                ),
                "primary_key": ["item_id", "stat_date"],
                "foreign_keys": [],
                "indexes": [],
                "row_count": 200,
            },
            "tb_live_goods_session_stats": {
                "name": "tb_live_goods_session_stats",
                "columns": _columns(
                    "item_id",
                    "live_session_id",
                    "live_start_time",
                    "pay_amt",
                    primary_key=("item_id", "live_session_id"),
                ),
                "primary_key": ["item_id", "live_session_id"],
                "foreign_keys": [relationship],
                "indexes": [],
                "row_count": 200,
            },
            "tb_session_endtime_stats": {
                "name": "tb_session_endtime_stats",
                "columns": _columns(
                    "live_session_id",
                    "live_start_time",
                    "pay_amt",
                    "refund_amt",
                    primary_key=("live_session_id",),
                ),
                "primary_key": ["live_session_id"],
                "foreign_keys": [],
                "indexes": [],
                "row_count": 1,
            },
        },
        "foreign_keys": [relationship],
    }

    from xpd_report_agent.hermes_plugin.db_query import (
        db,
        join_graph,
        schema_index,
        sql_guard,
        tools,
    )

    monkeypatch.setattr(db, "load_schema", lambda: schema)
    monkeypatch.setattr(join_graph, "load_schema", lambda: schema)
    monkeypatch.setattr(schema_index, "load_schema", lambda: schema)
    monkeypatch.setattr(sql_guard, "load_schema", lambda: schema)
    monkeypatch.setattr(tools, "load_schema", lambda: schema)
    monkeypatch.setattr(
        sql_guard,
        "get_mysql_config",
        lambda: {"database": "taobao_reports_test"},
    )
    monkeypatch.setattr(
        sql_guard,
        "explain_query",
        lambda sql: {"ok": True, "plan": [{"table": "report"}]},
    )
    return schema

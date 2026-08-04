from __future__ import annotations

import json

from xpd_report_agent.hermes_plugin.db_query import tools


def decode(payload: str) -> dict:
    return json.loads(payload)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        self.closed = True


def test_db_get_schema_ddl_returns_mysql_tables_relationships_and_metrics(
    report_schema, monkeypatch
):
    monkeypatch.setattr(
        tools,
        "get_schema_ddl",
        lambda: (
            "CREATE TABLE `tb_live_goods_daily_stats` (...);\n"
            "CREATE TABLE `tb_session_endtime_stats` (...);"
        ),
    )

    result = decode(tools.db_get_schema_ddl({}))

    assert result["ok"] is True
    assert "tb_live_goods_daily_stats" in result["ddl"]
    assert "tb_session_endtime_stats" in result["ddl"]
    assert (
        "tb_live_goods_session_stats.live_session_id = "
        "tb_session_endtime_stats.live_session_id"
    ) in result["relationships"]
    assert result["metrics"]["成交金额"] == "SUM(pay_amt)"


def test_db_schema_search_returns_taobao_report_tables(report_schema):
    result = decode(tools.db_schema_search({"question": "按天查看商品销售额趋势"}))

    assert result["ok"] is True
    assert any(
        item["table"] == "tb_live_goods_daily_stats" for item in result["tables"]
    )


def test_db_get_table_profile_includes_samples(report_schema, monkeypatch):
    monkeypatch.setattr(
        tools,
        "get_sample_rows",
        lambda table, limit=3: [{"live_session_id": "session-1", "pay_amt": "100.00"}],
    )

    result = decode(
        tools.db_get_table_profile(
            {"tables": ["tb_session_endtime_stats"], "include_samples": True}
        )
    )

    assert result["ok"] is True
    profile = result["tables"]["tb_session_endtime_stats"]
    assert profile["ok"] is True
    assert profile["sample_rows"]


def test_db_execute_sql_revalidates_and_truncates(report_schema, monkeypatch):
    connection = FakeConnection(
        [
            {"item_id": "1", "pay_amt": "100.00"},
            {"item_id": "2", "pay_amt": "80.00"},
            {"item_id": "3", "pay_amt": "60.00"},
        ]
    )
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)

    result = decode(
        tools.db_execute_sql(
            {
                "sql": (
                    "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
                    "ORDER BY pay_amt DESC"
                ),
                "max_rows": 2,
            }
        )
    )

    assert result["ok"] is True
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert result["columns"] == ["item_id", "pay_amt"]
    assert connection.closed is True


def test_db_execute_sql_rejects_invalid_sql(report_schema):
    result = decode(
        tools.db_execute_sql({"sql": "SELECT * FROM tb_live_goods_daily_stats"})
    )

    assert result["ok"] is False
    assert "validation" in result


def test_sql_tools_report_missing_sqlglot(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_load_sql_guard",
        lambda: (None, tools.SQLGLOT_MISSING_ERROR),
    )

    sql = "SELECT item_id FROM tb_live_goods_daily_stats"
    validate_result = decode(tools.db_validate_sql({"sql": sql}))
    execute_result = decode(tools.db_execute_sql({"sql": sql}))

    assert validate_result["ok"] is False
    assert "sqlglot" in validate_result["error"]
    assert execute_result["ok"] is False
    assert "sqlglot" in execute_result["error"]

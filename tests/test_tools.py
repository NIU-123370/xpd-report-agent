from __future__ import annotations

import json

from xpd_report_agent.hermes_plugin.db_query import schemas, tools


def decode(payload: str) -> dict:
    return json.loads(payload)


class FakeCursor:
    def __init__(self, rows, columns=None, executed=None):
        self.rows = rows
        self.sql = None
        self.executed = executed if executed is not None else []
        resolved_columns = columns or (list(rows[0]) if rows else [])
        self.description = [(column,) for column in resolved_columns]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows, columns=None):
        self.rows = rows
        self.columns = columns
        self.closed = False
        self.executed = []

    def cursor(self):
        return FakeCursor(self.rows, self.columns, self.executed)

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


def test_schema_tool_contract_starts_with_search_and_keeps_ddl_as_fallback():
    assert "first database discovery tool" in schemas.DB_SCHEMA_SEARCH["description"]
    assert "db_get_schema_ddl" not in schemas.DB_SCHEMA_SEARCH["description"]
    assert "diagnostics" in schemas.DB_GET_SCHEMA_DDL["description"]
    assert (
        schemas.DB_GET_TABLE_PROFILE["parameters"]["properties"][
            "include_samples"
        ]["default"]
        is False
    )
    quality_context = schemas.DB_EXECUTE_SQL["parameters"]["properties"][
        "quality_context"
    ]
    assert quality_context["additionalProperties"] is False
    assert "required_dimensions" in quality_context["properties"]
    assert "denominator_columns" in quality_context["properties"]


def test_schema_tools_dynamically_support_additional_database_tables(report_schema):
    report_schema["tables"]["customer_order_fact"] = {
        "name": "customer_order_fact",
        "columns": [
            {"name": "customer_id"},
            {"name": "order_amount"},
        ],
        "primary_key": [],
        "foreign_keys": [],
        "indexes": [],
        "row_count": None,
    }

    result = decode(
        tools.db_schema_search({"question": "查询 customer_order_fact 订单金额"})
    )

    assert result["ok"] is True
    assert result["table_count"] == 4
    assert "customer_order_fact" in result["available_tables"]
    dynamic = next(
        item for item in result["tables"] if item["table"] == "customer_order_fact"
    )
    assert "动态发现" in dynamic["description"]


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


def test_db_get_table_profile_skips_samples_by_default(report_schema, monkeypatch):
    def unexpected_sample_query(table, limit=3):
        raise AssertionError(f"unexpected sample query for {table} with limit={limit}")

    monkeypatch.setattr(tools, "get_sample_rows", unexpected_sample_query)

    result = decode(
        tools.db_get_table_profile({"tables": ["tb_session_endtime_stats"]})
    )

    assert result["ok"] is True
    profile = result["tables"]["tb_session_endtime_stats"]
    assert profile["ok"] is True
    assert "sample_rows" not in profile


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
    quality = result["data_quality"]
    assert quality["returned_row_count"] == 2
    assert quality["truncated"] is True
    assert quality["freshness"] is None
    assert quality["period_coverage"]["complete"] is None
    assert "结果超过 2 行，仅返回前 2 行，不能视为完整明细。" in quality[
        "warnings"
    ]
    assert "result_id" not in result
    assert connection.closed is True


def test_db_execute_sql_sets_bounded_server_side_timeout(
    report_schema, monkeypatch
):
    connection = FakeConnection([{"item_id": "1", "pay_amt": "100.00"}])
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)
    monkeypatch.setenv("XPD_MYSQL_QUERY_TIMEOUT_MS", "1234")

    result = decode(
        tools.db_execute_sql(
            {
                "sql": (
                    "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
                    "ORDER BY pay_amt DESC"
                )
            }
        )
    )

    assert result["ok"] is True
    assert connection.executed[0] == (
        "SET SESSION MAX_EXECUTION_TIME = %s",
        (1234,),
    )
    assert connection.executed[1][0].startswith("SELECT * FROM (")


def test_db_execute_sql_rejects_duplicate_cursor_columns_before_capture(
    report_schema, monkeypatch
):
    connection = FakeConnection(
        [{"item_id": "left", "right.item_id": "right"}],
        columns=["item_id", "item_id"],
    )
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)

    result = decode(
        tools.db_execute_sql(
            {
                "sql": (
                    "SELECT item_id AS left_item_id, pay_amt AS right_item_id "
                    "FROM tb_live_goods_daily_stats"
                )
            },
            session_id="xpd_0123456789abcdefabcd_duplicate",
        )
    )

    assert result["ok"] is False
    assert "Duplicate output column names" in result["error"]
    assert "unique AS alias" in result["error"]
    assert connection.closed is True


def test_db_execute_sql_export_hint_raises_default_capture_limit(report_schema, monkeypatch):
    connection = FakeConnection([], columns=["item_id", "pay_amt"])
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)
    sql = (
        "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
        "ORDER BY pay_amt DESC"
    )

    result = decode(
        tools.db_execute_sql(
            {"sql": sql, "capture_for_export": True},
            session_id="xpd_0123456789abcdefabcd_export",
        )
    )

    assert result["ok"] is True
    assert result["columns"] == ["item_id", "pay_amt"]
    assert result["rows"] == []
    assert result["data_quality"]["empty_result"] is True
    assert "查询结果为空，请检查统计周期和筛选条件。" in result[
        "data_quality"
    ]["warnings"]
    assert result["result_id"].startswith("result_")


def test_db_execute_sql_keeps_short_lived_snapshot_for_followup_export(
    report_schema, monkeypatch
):
    connection = FakeConnection(
        [{"item_id": "1", "pay_amt": "100.00"}],
        columns=["item_id", "pay_amt"],
    )
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)

    result = decode(
        tools.db_execute_sql(
            {
                "sql": (
                    "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
                    "ORDER BY pay_amt DESC"
                )
            },
            session_id="xpd_0123456789abcdefabcd_followup",
        )
    )

    assert result["ok"] is True
    assert result["result_id"].startswith("result_")
    stored = tools.query_result_registry.get(
        result_id=result["result_id"],
        session_id="xpd_0123456789abcdefabcd_followup",
    )
    assert stored is not None
    assert stored["rows"] == [{"item_id": "1", "pay_amt": "100.00"}]


def test_db_execute_sql_export_capture_defaults_to_1000_rows(report_schema, monkeypatch):
    rows = [{"item_id": str(index), "pay_amt": "1.00"} for index in range(150)]
    connection = FakeConnection(rows)
    monkeypatch.setattr(tools, "connect_readonly", lambda: connection)

    result = decode(
        tools.db_execute_sql(
            {
                "sql": (
                    "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
                    "ORDER BY item_id"
                ),
                "capture_for_export": True,
            },
            session_id="xpd_0123456789abcdefabcd_export",
        )
    )

    assert result["ok"] is True
    assert result["row_count"] == 150
    assert result["truncated"] is False
    assert result["result_id"].startswith("result_")


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

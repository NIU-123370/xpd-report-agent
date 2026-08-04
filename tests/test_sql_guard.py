from __future__ import annotations

from xpd_report_agent.hermes_plugin.db_query.sql_guard import (
    validate_sql,
    wrap_with_limit,
)

SESSION_PRODUCT_SQL = """
WITH product_sales AS (
    SELECT
        live_session_id,
        item_id,
        SUM(pay_amt) AS pay_amt
    FROM tb_live_goods_session_stats
    WHERE live_start_time >= CURRENT_DATE - INTERVAL 30 DAY
    GROUP BY live_session_id, item_id
)
SELECT
    p.live_session_id,
    p.item_id,
    p.pay_amt,
    s.live_start_time
FROM product_sales AS p
JOIN tb_session_endtime_stats AS s
    ON p.live_session_id = s.live_session_id
ORDER BY p.pay_amt DESC
"""


def test_validate_sql_allows_mysql_cte_and_tracks_real_tables(report_schema):
    result = validate_sql(SESSION_PRODUCT_SQL)

    assert result["ok"] is True
    assert set(result["used_tables"]) == {
        "tb_live_goods_session_stats",
        "tb_session_endtime_stats",
    }
    assert "INTERVAL '30' DAY" in result["normalized_sql"]
    assert result["explain"]


def test_validate_sql_allows_count_star(report_schema):
    result = validate_sql(
        "SELECT COUNT(*) AS session_count FROM tb_session_endtime_stats"
    )

    assert result["ok"] is True
    assert result["used_tables"] == ["tb_session_endtime_stats"]


def test_validate_sql_rejects_select_wildcard(report_schema):
    result = validate_sql("SELECT * FROM tb_live_goods_daily_stats")

    assert result["ok"] is False
    assert "SELECT *" in result["error"]


def test_validate_sql_rejects_dml_and_commands(report_schema):
    delete_result = validate_sql(
        "DELETE FROM tb_live_goods_daily_stats WHERE item_id = '1'"
    )
    show_result = validate_sql("SHOW TABLES")

    assert delete_result["ok"] is False
    assert "Forbidden SQL operation" in delete_result["error"]
    assert show_result["ok"] is False


def test_validate_sql_rejects_unknown_cross_database_and_multiple_statements(report_schema):
    unknown_result = validate_sql("SELECT item_id FROM missing_reports")
    cross_db_result = validate_sql(
        "SELECT item_id FROM other_database.tb_live_goods_daily_stats"
    )
    multi_result = validate_sql(
        "SELECT item_id FROM tb_live_goods_daily_stats; "
        "SELECT live_session_id FROM tb_session_endtime_stats"
    )

    assert unknown_result["ok"] is False
    assert "Unknown or disallowed tables" in unknown_result["error"]
    assert cross_db_result["ok"] is False
    assert "Cross-database" in cross_db_result["error"]
    assert multi_result["ok"] is False
    assert "Only one SQL statement" in multi_result["error"]


def test_wrap_with_limit_clamps_max_rows():
    limited = wrap_with_limit(
        "SELECT item_id FROM tb_live_goods_daily_stats",
        max_rows=50000,
    )

    assert "_hermes_mysql_query" in limited
    assert limited.endswith("LIMIT 1001")

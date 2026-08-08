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


def test_validate_sql_rejects_dangerous_mysql_functions(report_schema):
    queries = [
        "SELECT SLEEP(10)",
        "SELECT BENCHMARK(1000000, MD5('x'))",
        "SELECT GET_LOCK('agent-lock', 10)",
        "SELECT RELEASE_LOCK('agent-lock')",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT MASTER_POS_WAIT('mysql-bin.000001', 1)",
    ]

    for sql in queries:
        result = validate_sql(sql)
        assert result["ok"] is False, sql
        assert "Forbidden SQL function" in result["error"]


def test_validate_sql_rejects_user_variables_and_locking_selects(report_schema):
    assignment = validate_sql(
        "SELECT @running_total := pay_amt "
        "FROM tb_live_goods_daily_stats"
    )
    select_into = validate_sql(
        "SELECT pay_amt INTO @last_amount FROM tb_live_goods_daily_stats"
    )
    for_update = validate_sql(
        "SELECT item_id FROM tb_live_goods_daily_stats FOR UPDATE"
    )
    for_share = validate_sql(
        "SELECT item_id FROM tb_live_goods_daily_stats LOCK IN SHARE MODE"
    )

    assert assignment["ok"] is False
    assert "User-variable assignment" in assignment["error"]
    assert select_into["ok"] is False
    assert "SELECT INTO" in select_into["error"]
    assert for_update["ok"] is False
    assert "Locking SELECT" in for_update["error"]
    assert for_share["ok"] is False
    assert "Locking SELECT" in for_share["error"]


def test_validate_sql_rejects_hints_that_can_bypass_the_timeout(report_schema):
    max_execution_hint = validate_sql(
        "SELECT /*+ MAX_EXECUTION_TIME(999999) */ item_id "
        "FROM tb_live_goods_daily_stats"
    )
    executable_comment = validate_sql(
        "SELECT /*!50000 SLEEP(10), */ item_id "
        "FROM tb_live_goods_daily_stats"
    )
    ordinary_comment = validate_sql(
        "SELECT /* monthly trend */ item_id FROM tb_live_goods_daily_stats"
    )
    ordinary_bang_comment = validate_sql(
        "SELECT /* ! business note */ item_id FROM tb_live_goods_daily_stats"
    )

    assert max_execution_hint["ok"] is False
    assert "optimizer hints" in max_execution_hint["error"]
    assert executable_comment["ok"] is False
    assert "executable comments" in executable_comment["error"]
    assert ordinary_comment["ok"] is True
    assert ordinary_bang_comment["ok"] is True


def test_validate_sql_rejects_duplicate_output_names_and_allows_unique_aliases(
    report_schema,
):
    duplicate = validate_sql(
        "SELECT g.live_session_id, s.live_session_id "
        "FROM tb_live_goods_session_stats AS g "
        "JOIN tb_session_endtime_stats AS s "
        "ON g.live_session_id = s.live_session_id"
    )
    unique = validate_sql(
        "SELECT g.live_session_id AS goods_session_id, "
        "s.live_session_id AS summary_session_id "
        "FROM tb_live_goods_session_stats AS g "
        "JOIN tb_session_endtime_stats AS s "
        "ON g.live_session_id = s.live_session_id"
    )

    assert duplicate["ok"] is False
    assert "Duplicate output column names" in duplicate["error"]
    assert "unique AS alias" in duplicate["error"]
    assert unique["ok"] is True


def test_validate_sql_rejects_duplicate_unaliased_expressions(report_schema):
    duplicate_aggregate = validate_sql(
        "SELECT SUM(pay_amt), SUM(pay_amt) FROM tb_live_goods_daily_stats"
    )
    duplicate_arithmetic = validate_sql(
        "SELECT pay_amt + 1, pay_amt + 1 FROM tb_live_goods_daily_stats"
    )
    unique = validate_sql(
        "SELECT SUM(pay_amt) AS total_amount, "
        "SUM(pay_amt) / 2 AS half_amount FROM tb_live_goods_daily_stats"
    )

    assert duplicate_aggregate["ok"] is False
    assert "Duplicate output column names" in duplicate_aggregate["error"]
    assert duplicate_arithmetic["ok"] is False
    assert "Duplicate output column names" in duplicate_arithmetic["error"]
    assert unique["ok"] is True


def test_validate_sql_keeps_normal_analysis_functions_available(report_schema):
    result = validate_sql(
        "SELECT DATE_FORMAT(stat_date, '%Y-%m') AS stat_month, "
        "COALESCE(SUM(pay_amt), 0) / NULLIF(COUNT(item_id), 0) AS average_amount "
        "FROM tb_live_goods_daily_stats GROUP BY stat_month"
    )

    assert result["ok"] is True


def test_wrap_with_limit_clamps_max_rows():
    limited = wrap_with_limit(
        "SELECT item_id FROM tb_live_goods_daily_stats",
        max_rows=50000,
    )

    assert "_hermes_mysql_query" in limited
    assert limited.endswith("LIMIT 1001")

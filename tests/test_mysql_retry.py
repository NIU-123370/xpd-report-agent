from __future__ import annotations

import json

import pymysql
import pytest

from xpd_report_agent.hermes_plugin.db_query import db, tools


class QueryCursor:
    def __init__(self, *, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.description = [("item_id",), ("pay_amt",)]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        if self.error is not None and not sql.startswith(
            "SET SESSION MAX_EXECUTION_TIME"
        ):
            raise self.error

    def fetchall(self):
        return self.rows


class QueryConnection:
    def __init__(self, *, rows=None, error: Exception | None = None):
        self.rows = rows
        self.error = error
        self.closed = False

    def cursor(self):
        return QueryCursor(rows=self.rows, error=self.error)

    def close(self):
        self.closed = True


def _query_payload() -> dict[str, object]:
    return {
        "sql": (
            "SELECT item_id, pay_amt FROM tb_live_goods_daily_stats "
            "ORDER BY pay_amt DESC"
        ),
        "max_rows": 10,
    }


def test_connect_readonly_retries_transient_connect_timeout(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "reports")
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "2")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    connection = object()
    outcomes = iter([TimeoutError("connect timed out"), connection])
    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)

    assert db.connect_readonly() is connection
    assert len(calls) == 2


def test_connect_readonly_default_is_two_total_attempts(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "reports")
    monkeypatch.delenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, raising=False)
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        raise pymysql.err.OperationalError(2003, "cannot connect")

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)

    with pytest.raises(pymysql.err.OperationalError):
        db.connect_readonly()

    assert len(calls) == 2


def test_connect_readonly_clamps_attempts_and_total_backoff(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "reports")
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "99")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "99999")
    calls = []
    sleeps = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        raise pymysql.err.OperationalError(1040, "too many connections")

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    with pytest.raises(pymysql.err.OperationalError):
        db.connect_readonly()

    assert len(calls) == db.MAX_MYSQL_READ_MAX_ATTEMPTS
    assert sleeps == [0.5, 0.5]
    assert sum(sleeps) <= db.MAX_MYSQL_READ_RETRY_TOTAL_DELAY_MS / 1_000


def test_execute_sql_reopens_connection_after_transient_loss(
    report_schema, monkeypatch
):
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "2")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    first = QueryConnection(
        error=pymysql.err.OperationalError(2013, "lost connection during query")
    )
    second = QueryConnection(rows=[{"item_id": "1", "pay_amt": "88.00"}])
    connections = iter([first, second])
    opened = []

    def open_connection():
        connection = next(connections)
        opened.append(connection)
        return connection

    monkeypatch.setattr(tools, "connect_readonly", open_connection)

    result = json.loads(tools.db_execute_sql(_query_payload()))

    assert result["ok"] is True
    assert result["rows"] == [{"item_id": "1", "pay_amt": "88.00"}]
    assert result["row_count"] == 1
    assert len(opened) == 2
    assert first.closed is True
    assert second.closed is True


def test_sample_query_reopens_connection_after_transient_loss(monkeypatch):
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "2")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    monkeypatch.setattr(
        db,
        "load_schema",
        lambda: {"tables": {"tb_live_goods_daily_stats": {}}},
    )
    first = QueryConnection(
        error=pymysql.err.OperationalError(2006, "server has gone away")
    )
    second = QueryConnection(rows=[{"item_id": "1", "pay_amt": "88.00"}])
    connections = iter([first, second])
    opened = []

    def open_connection():
        connection = next(connections)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db, "connect_readonly", open_connection)

    rows = db.get_sample_rows("tb_live_goods_daily_stats", limit=3)

    assert rows == [{"item_id": "1", "pay_amt": "88.00"}]
    assert len(opened) == 2
    assert first.closed is True
    assert second.closed is True


@pytest.mark.parametrize(
    "error",
    [
        pymysql.err.ProgrammingError(1064, "SQL syntax error"),
        pymysql.err.OperationalError(1054, "unknown column"),
        pymysql.err.OperationalError(1142, "SELECT denied"),
        pymysql.err.OperationalError(3024, "maximum statement execution time exceeded"),
        pymysql.err.OperationalError(
            2013,
            "Lost connection to MySQL server during query (timed out)",
        ),
    ],
    ids=[
        "syntax",
        "unknown-column",
        "permission",
        "server-query-timeout",
        "pymysql-read-timeout",
    ],
)
def test_execute_sql_does_not_retry_non_connection_errors(
    report_schema, monkeypatch, error
):
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "3")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    connection = QueryConnection(error=error)
    opened = []

    def open_connection():
        opened.append(connection)
        return connection

    monkeypatch.setattr(tools, "connect_readonly", open_connection)

    result = json.loads(tools.db_execute_sql(_query_payload()))

    assert result == {"ok": False, "error": str(error)}
    assert len(opened) == 1
    assert connection.closed is True


def test_execute_timeout_error_after_connect_is_not_retried(
    report_schema, monkeypatch
):
    monkeypatch.setenv(db.MYSQL_READ_MAX_ATTEMPTS_ENV, "3")
    monkeypatch.setenv(db.MYSQL_READ_RETRY_BACKOFF_MS_ENV, "0")
    connection = QueryConnection(error=TimeoutError("query read timed out"))
    opened = []

    def open_connection():
        opened.append(connection)
        return connection

    monkeypatch.setattr(tools, "connect_readonly", open_connection)

    result = json.loads(tools.db_execute_sql(_query_payload()))

    assert result == {"ok": False, "error": "query read timed out"}
    assert len(opened) == 1

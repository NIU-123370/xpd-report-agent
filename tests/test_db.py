from __future__ import annotations

import pytest

from xpd_report_agent.hermes_plugin.db_query import db


def test_get_mysql_config_reads_connection_environment(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.2")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    monkeypatch.setenv("MYSQL_USER", "report_reader")
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("MYSQL_DATABASE", "reports")

    assert db.get_mysql_config() == {
        "host": "127.0.0.2",
        "port": 3307,
        "user": "report_reader",
        "password": "secret",
        "database": "reports",
    }


def test_get_mysql_config_accepts_xpd_dms_aliases(monkeypatch):
    for name in db.MYSQL_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XPD_DB_HOST", "rds.internal")
    monkeypatch.setenv("XPD_DB_PORT", "3307")
    monkeypatch.setenv("XPD_DB_USERNAME", "main_biz_dev")
    monkeypatch.setenv("XPD_DB_PASSWORD", "secret")
    monkeypatch.setenv("XPD_DB_NAME", "main_biz_dev")

    assert db.get_mysql_config() == {
        "host": "rds.internal",
        "port": 3307,
        "user": "main_biz_dev",
        "password": "secret",
        "database": "main_biz_dev",
    }


def test_get_mysql_config_requires_database(monkeypatch):
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)

    with pytest.raises(RuntimeError, match="MYSQL_DATABASE"):
        db.get_mysql_config()


def test_connect_readonly_uses_dict_cursor_and_read_only_session(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "reports")
    captured = {}
    connection = object()

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)

    assert db.connect_readonly() is connection
    assert captured["database"] == "reports"
    assert captured["autocommit"] is True
    assert captured["cursorclass"] is db.DictCursor
    assert captured["init_command"] == "SET SESSION TRANSACTION READ ONLY"


def test_quote_ident_uses_mysql_backticks_and_rejects_unsafe_names():
    assert db.quote_ident("tb_live_goods_daily_stats") == "`tb_live_goods_daily_stats`"

    with pytest.raises(ValueError, match="Unsafe identifier"):
        db.quote_ident("reports; drop table reports")

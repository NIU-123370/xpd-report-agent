from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

MYSQL_ENV_KEYS = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
)

LOGICAL_FOREIGN_KEYS = (
    {
        "from_table": "tb_live_goods_session_stats",
        "from_column": "live_session_id",
        "to_table": "tb_session_endtime_stats",
        "to_column": "live_session_id",
        "logical": True,
    },
)


def get_mysql_config() -> dict[str, Any]:
    database = os.environ.get("MYSQL_DATABASE")
    if not database:
        raise RuntimeError("MYSQL_DATABASE is not set")

    try:
        port = int(os.environ.get("MYSQL_PORT", "3306"))
    except ValueError as exc:
        raise RuntimeError("MYSQL_PORT must be an integer") from exc

    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": port,
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": database,
    }


def connect_readonly() -> pymysql.Connection:
    config = get_mysql_config()
    return pymysql.connect(
        **config,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=10,
        init_command="SET SESSION TRANSACTION READ ONLY",
    )


def quote_ident(name: str) -> str:
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def _fetch_all(conn: pymysql.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def _fetch_one(conn: pymysql.Connection, sql: str, params: tuple = ()) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("MySQL query returned no rows")
    return row


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    database = get_mysql_config()["database"]
    conn = connect_readonly()
    try:
        table_rows = _fetch_all(
            conn,
            """
            SELECT TABLE_NAME AS name
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        tables = [row["name"] for row in table_rows]
        result: dict[str, Any] = {"tables": {}, "foreign_keys": []}

        for table in tables:
            column_rows = _fetch_all(
                conn,
                """
                SELECT
                    ORDINAL_POSITION - 1 AS cid,
                    COLUMN_NAME AS name,
                    COLUMN_TYPE AS type,
                    IS_NULLABLE AS is_nullable,
                    COLUMN_DEFAULT AS default_value,
                    COLUMN_KEY AS column_key
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (database, table),
            )
            columns = [
                {
                    "cid": row["cid"],
                    "name": row["name"],
                    "type": row["type"],
                    "notnull": row["is_nullable"] == "NO",
                    "default": row["default_value"],
                    "pk": row["column_key"] == "PRI",
                }
                for row in column_rows
            ]
            pk_columns = [column["name"] for column in columns if column["pk"]]

            foreign_keys = [
                {
                    "from_table": table,
                    "from_column": row["from_column"],
                    "to_table": row["to_table"],
                    "to_column": row["to_column"],
                    "logical": False,
                }
                for row in _fetch_all(
                    conn,
                    """
                    SELECT
                        COLUMN_NAME AS from_column,
                        REFERENCED_TABLE_NAME AS to_table,
                        REFERENCED_COLUMN_NAME AS to_column
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                    ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
                    """,
                    (database, table),
                )
            ]

            index_rows = _fetch_all(
                conn,
                """
                SELECT
                    INDEX_NAME AS name,
                    NON_UNIQUE AS non_unique,
                    COLUMN_NAME AS column_name
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """,
                (database, table),
            )
            indexes_by_name: dict[str, dict[str, Any]] = {}
            for row in index_rows:
                index = indexes_by_name.setdefault(
                    row["name"],
                    {
                        "name": row["name"],
                        "unique": not bool(row["non_unique"]),
                        "columns": [],
                    },
                )
                index["columns"].append(row["column_name"])

            row_count = _fetch_one(
                conn,
                f"SELECT COUNT(*) AS c FROM {quote_ident(table)}",
            )["c"]

            result["tables"][table] = {
                "name": table,
                "columns": columns,
                "primary_key": pk_columns,
                "foreign_keys": foreign_keys,
                "indexes": list(indexes_by_name.values()),
                "row_count": row_count,
            }
            result["foreign_keys"].extend(foreign_keys)

        existing_tables = set(tables)
        for relationship in LOGICAL_FOREIGN_KEYS:
            if {
                relationship["from_table"],
                relationship["to_table"],
            } <= existing_tables:
                item = dict(relationship)
                result["foreign_keys"].append(item)
                result["tables"][item["from_table"]]["foreign_keys"].append(item)

        return result
    finally:
        conn.close()


def clear_schema_cache() -> None:
    load_schema.cache_clear()


def get_sample_rows(table: str, limit: int = 3) -> list[dict[str, Any]]:
    schema = load_schema()
    if table not in schema["tables"]:
        raise ValueError(f"Unknown table: {table}")

    safe_limit = max(0, min(int(limit), 20))
    conn = connect_readonly()
    try:
        return _fetch_all(
            conn,
            f"SELECT * FROM {quote_ident(table)} LIMIT %s",
            (safe_limit,),
        )
    finally:
        conn.close()


def get_schema_ddl() -> str:
    schema = load_schema()
    conn = connect_readonly()
    try:
        statements = []
        for table in schema["tables"]:
            row = _fetch_one(conn, f"SHOW CREATE TABLE {quote_ident(table)}")
            ddl = row.get("Create Table")
            if ddl:
                statements.append(ddl.strip().rstrip(";") + ";")
        return "\n\n".join(statements)
    finally:
        conn.close()

from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from math import isfinite
from typing import Any, Callable, TypeVar

import pymysql
from pymysql.cursors import DictCursor

MYSQL_ENV_KEYS = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
)

XPD_DB_ALIASES = {
    "MYSQL_HOST": "XPD_DB_HOST",
    "MYSQL_PORT": "XPD_DB_PORT",
    "MYSQL_USER": "XPD_DB_USERNAME",
    "MYSQL_PASSWORD": "XPD_DB_PASSWORD",
    "MYSQL_DATABASE": "XPD_DB_NAME",
}

MYSQL_READ_MAX_ATTEMPTS_ENV = "XPD_MYSQL_READ_MAX_ATTEMPTS"
MYSQL_READ_RETRY_BACKOFF_MS_ENV = "XPD_MYSQL_READ_RETRY_BACKOFF_MS"

DEFAULT_MYSQL_READ_MAX_ATTEMPTS = 2
MAX_MYSQL_READ_MAX_ATTEMPTS = 3
DEFAULT_MYSQL_READ_RETRY_BACKOFF_MS = 100.0
MAX_MYSQL_READ_RETRY_BACKOFF_MS = 500.0
MAX_MYSQL_READ_RETRY_TOTAL_DELAY_MS = 1_000.0

# These codes represent an unavailable/lost connection, not a problem with the
# submitted SQL. In particular, query timeouts (for example 3024), syntax
# errors, missing columns, and permission errors are intentionally absent.
TRANSIENT_MYSQL_CONNECTION_ERROR_CODES = frozenset(
    {
        1040,  # Too many connections.
        2002,  # Cannot connect through the local socket.
        2003,  # Cannot connect to the MySQL server.
        2006,  # MySQL server has gone away.
        2013,  # Lost connection during query.
        2055,  # Lost connection at handshake/reading initial communication.
    }
)

MYSQL_QUERY_TIMEOUT_MARKERS = (
    "timed out",
    "read timeout",
    "query timeout",
)

_ResultT = TypeVar("_ResultT")

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
    def db_env(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name) or os.environ.get(XPD_DB_ALIASES[name]) or default

    database = db_env("MYSQL_DATABASE")
    if not database:
        raise RuntimeError("MYSQL_DATABASE/XPD_DB_NAME is not set")

    try:
        port = int(db_env("MYSQL_PORT", "3306") or "3306")
    except ValueError as exc:
        raise RuntimeError("MYSQL_PORT/XPD_DB_PORT must be an integer") from exc

    return {
        "host": db_env("MYSQL_HOST", "127.0.0.1"),
        "port": port,
        "user": db_env("MYSQL_USER", "root"),
        "password": db_env("MYSQL_PASSWORD", ""),
        "database": database,
    }


def _connect_readonly_once() -> pymysql.Connection:
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


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def mysql_read_retry_config() -> tuple[int, float]:
    """Return bounded total attempts and base backoff in seconds."""

    attempts = _bounded_int_env(
        MYSQL_READ_MAX_ATTEMPTS_ENV,
        DEFAULT_MYSQL_READ_MAX_ATTEMPTS,
        minimum=1,
        maximum=MAX_MYSQL_READ_MAX_ATTEMPTS,
    )
    backoff_ms = _bounded_float_env(
        MYSQL_READ_RETRY_BACKOFF_MS_ENV,
        DEFAULT_MYSQL_READ_RETRY_BACKOFF_MS,
        minimum=0.0,
        maximum=MAX_MYSQL_READ_RETRY_BACKOFF_MS,
    )
    return attempts, backoff_ms / 1_000.0


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_mysql_connection_error(
    exc: BaseException,
    *,
    during_connect: bool,
) -> bool:
    """Classify only explicit connection failures as safe read retries."""

    for current in _exception_chain(exc):
        if isinstance(current, pymysql.MySQLError):
            code = current.args[0] if current.args else None
            # An explicit MySQL code is authoritative. Do not let an incidental
            # chained transport exception turn a syntax/permission/timeout
            # error into a retryable one.
            if isinstance(code, int):
                # PyMySQL wraps socket read timeouts raised while waiting for a
                # query result as OperationalError(2013), the same code used
                # for a genuine connection loss. Retrying that slow query can
                # amplify database load, so use the message to keep query/read
                # timeouts non-retryable after the connection was established.
                message = " ".join(str(part) for part in current.args[1:]).lower()
                if (
                    not during_connect
                    and code == 2013
                    and any(marker in message for marker in MYSQL_QUERY_TIMEOUT_MARKERS)
                ):
                    return False
                return code in TRANSIENT_MYSQL_CONNECTION_ERROR_CODES

        # A transport can reset after execute has started. Retrying is safe
        # because this plugin accepts only validated, read-only SELECT queries.
        if isinstance(
            current,
            (ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
        ):
            return True

        # A raw timeout/refusal is retryable only while opening the connection.
        # During cursor execution it may instead be a query/read timeout, which
        # must not be retried mechanically.
        if during_connect and isinstance(
            current,
            (TimeoutError, ConnectionRefusedError),
        ):
            return True

    return False


def _retry_delay_seconds(
    failed_attempt: int,
    *,
    base_backoff_seconds: float,
    total_delay_seconds: float,
) -> float:
    requested = base_backoff_seconds * (2 ** max(0, failed_attempt - 1))
    remaining = max(
        0.0,
        (MAX_MYSQL_READ_RETRY_TOTAL_DELAY_MS / 1_000.0) - total_delay_seconds,
    )
    return min(requested, remaining)


def _sleep_before_retry(
    failed_attempt: int,
    *,
    base_backoff_seconds: float,
    total_delay_seconds: float,
) -> float:
    delay = _retry_delay_seconds(
        failed_attempt,
        base_backoff_seconds=base_backoff_seconds,
        total_delay_seconds=total_delay_seconds,
    )
    if delay > 0:
        time.sleep(delay)
    return total_delay_seconds + delay


def connect_readonly() -> pymysql.Connection:
    """Open a read-only connection, retrying only transient connect failures."""

    attempts, backoff_seconds = mysql_read_retry_config()
    total_delay_seconds = 0.0
    for attempt in range(1, attempts + 1):
        try:
            return _connect_readonly_once()
        except Exception as exc:
            if attempt >= attempts or not is_transient_mysql_connection_error(
                exc,
                during_connect=True,
            ):
                raise
            total_delay_seconds = _sleep_before_retry(
                attempt,
                base_backoff_seconds=backoff_seconds,
                total_delay_seconds=total_delay_seconds,
            )

    raise RuntimeError("MySQL connection retry loop exited unexpectedly")


_DEFAULT_RETRYING_CONNECTION_FACTORY = connect_readonly


def execute_readonly_with_retry(
    operation: Callable[[pymysql.Connection], _ResultT],
    *,
    connection_factory: Callable[[], pymysql.Connection] | None = None,
) -> _ResultT:
    """Run one read-only operation with a fresh connection per attempt."""

    attempts, backoff_seconds = mysql_read_retry_config()
    total_delay_seconds = 0.0

    # The public factory already retries connection establishment. Bypass that
    # inner loop here so the configured maximum remains a true total-attempt
    # cap. A custom factory is supported for tests and embedders.
    if (
        connection_factory is None
        or connection_factory is _DEFAULT_RETRYING_CONNECTION_FACTORY
    ):
        open_connection = _connect_readonly_once
    else:
        open_connection = connection_factory

    for attempt in range(1, attempts + 1):
        conn: pymysql.Connection | None = None
        during_connect = True
        retry = False
        try:
            conn = open_connection()
            during_connect = False
            return operation(conn)
        except Exception as exc:
            if attempt >= attempts or not is_transient_mysql_connection_error(
                exc,
                during_connect=during_connect,
            ):
                raise
            retry = True
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    # The connection is discarded either way. A close failure
                    # must not hide the original query error or turn a
                    # successful, read-only query into an unknown outcome.
                    pass

        if retry:
            total_delay_seconds = _sleep_before_retry(
                attempt,
                base_backoff_seconds=backoff_seconds,
                total_delay_seconds=total_delay_seconds,
            )

    raise RuntimeError("MySQL read retry loop exited unexpectedly")


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

    def load_from_connection(conn: pymysql.Connection) -> dict[str, Any]:
        table_rows = _fetch_all(
            conn,
            """
            SELECT TABLE_NAME AS name, TABLE_ROWS AS estimated_row_count
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        tables = [row["name"] for row in table_rows]
        estimated_rows = {
            str(row["name"]): row.get("estimated_row_count") for row in table_rows
        }
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

            result["tables"][table] = {
                "name": table,
                "columns": columns,
                "primary_key": pk_columns,
                "foreign_keys": foreign_keys,
                "indexes": list(indexes_by_name.values()),
                # INFORMATION_SCHEMA.TABLES avoids running COUNT(*) against
                # every table when a production database contains many tables.
                "row_count": estimated_rows.get(table),
                "row_count_is_estimate": True,
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

    return execute_readonly_with_retry(
        load_from_connection,
        connection_factory=connect_readonly,
    )


def clear_schema_cache() -> None:
    load_schema.cache_clear()


def get_sample_rows(table: str, limit: int = 3) -> list[dict[str, Any]]:
    schema = load_schema()
    if table not in schema["tables"]:
        raise ValueError(f"Unknown table: {table}")

    safe_limit = max(0, min(int(limit), 20))
    return execute_readonly_with_retry(
        lambda conn: _fetch_all(
            conn,
            f"SELECT * FROM {quote_ident(table)} LIMIT %s",
            (safe_limit,),
        ),
        connection_factory=connect_readonly,
    )


def get_schema_ddl() -> str:
    schema = load_schema()

    def load_ddl_from_connection(conn: pymysql.Connection) -> str:
        statements = []
        for table in schema["tables"]:
            row = _fetch_one(conn, f"SHOW CREATE TABLE {quote_ident(table)}")
            ddl = row.get("Create Table")
            if ddl:
                statements.append(ddl.strip().rstrip(";") + ";")
        return "\n\n".join(statements)

    return execute_readonly_with_retry(
        load_ddl_from_connection,
        connection_factory=connect_readonly,
    )

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any


def get_db_path() -> str:
    path = os.environ.get("HERMES_DEMO_SQLITE_PATH")
    if not path:
        raise RuntimeError("HERMES_DEMO_SQLITE_PATH is not set")

    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        raise RuntimeError(f"SQLite database not found: {db_path}")

    return str(db_path)


def connect_readonly() -> sqlite3.Connection:
    db_path = get_db_path()
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def quote_ident(name: str) -> str:
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    conn = connect_readonly()
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]

        result: dict[str, Any] = {
            "tables": {},
            "foreign_keys": [],
        }

        for table in tables:
            columns = []
            pk_columns = []

            for col in conn.execute(f"PRAGMA table_info({quote_ident(table)})"):
                item = {
                    "cid": col["cid"],
                    "name": col["name"],
                    "type": col["type"],
                    "notnull": bool(col["notnull"]),
                    "default": col["dflt_value"],
                    "pk": bool(col["pk"]),
                }
                columns.append(item)
                if col["pk"]:
                    pk_columns.append(col["name"])

            foreign_keys = []
            for fk in conn.execute(f"PRAGMA foreign_key_list({quote_ident(table)})"):
                item = {
                    "from_table": table,
                    "from_column": fk["from"],
                    "to_table": fk["table"],
                    "to_column": fk["to"],
                }
                foreign_keys.append(item)
                result["foreign_keys"].append(item)

            indexes = []
            for idx in conn.execute(f"PRAGMA index_list({quote_ident(table)})"):
                index_name = idx["name"]
                index_columns = [
                    x["name"]
                    for x in conn.execute(f"PRAGMA index_info({quote_ident(index_name)})")
                ]
                indexes.append(
                    {
                        "name": index_name,
                        "unique": bool(idx["unique"]),
                        "columns": index_columns,
                    }
                )

            row_count = conn.execute(
                f"SELECT COUNT(*) AS c FROM {quote_ident(table)}"
            ).fetchone()["c"]

            result["tables"][table] = {
                "name": table,
                "columns": columns,
                "primary_key": pk_columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
                "row_count": row_count,
            }

        return result
    finally:
        conn.close()


def clear_schema_cache() -> None:
    load_schema.cache_clear()


def get_sample_rows(table: str, limit: int = 3) -> list[dict[str, Any]]:
    schema = load_schema()
    if table not in schema["tables"]:
        raise ValueError(f"Unknown table: {table}")

    conn = connect_readonly()
    try:
        safe_limit = max(0, min(int(limit), 20))
        rows = conn.execute(
            f"SELECT * FROM {quote_ident(table)} LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_schema_ddl() -> str:
    conn = connect_readonly()
    try:
        rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        ddl_statements = [row["sql"].strip().rstrip(";") + ";" for row in rows if row["sql"]]
        return "\n\n".join(ddl_statements)
    finally:
        conn.close()

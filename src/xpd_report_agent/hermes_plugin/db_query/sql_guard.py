from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from .db import connect_readonly, load_schema

FORBIDDEN_NODE_TYPES = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Drop",
        "Create",
        "Alter",
        "Command",
        "Pragma",
        "Attach",
        "Detach",
        "Copy",
    )
    if hasattr(exp, name)
)

FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile"}
MAX_ROWS_CAP = 1000


def validate_sql(sql: str) -> dict[str, Any]:
    clean_sql = sql.strip()
    if not clean_sql:
        return {"ok": False, "error": "SQL is empty"}

    try:
        parsed = sqlglot.parse(clean_sql, read="sqlite")
    except Exception as exc:
        return {"ok": False, "error": f"SQL parse error: {exc}"}

    if len(parsed) != 1:
        return {"ok": False, "error": "Only one SQL statement is allowed"}

    tree = parsed[0]

    for forbidden_type in FORBIDDEN_NODE_TYPES:
        if tree.find(forbidden_type):
            return {
                "ok": False,
                "error": f"Forbidden SQL operation: {forbidden_type.__name__}",
            }

    if not tree.find(exp.Select):
        return {"ok": False, "error": "Only SELECT queries are allowed"}

    if _has_select_wildcard(tree):
        return {
            "ok": False,
            "error": "SELECT * is not allowed. Please select explicit columns.",
        }

    forbidden_functions = _find_forbidden_functions(tree)
    if forbidden_functions:
        return {
            "ok": False,
            "error": f"Forbidden SQL function: {forbidden_functions[0]}",
        }

    table_check = _check_tables(tree)
    if not table_check["ok"]:
        return table_check

    explain = explain_query(clean_sql)
    if not explain["ok"]:
        return explain

    return {
        "ok": True,
        "used_tables": sorted(table_check["used_tables"]),
        "normalized_sql": tree.sql(dialect="sqlite", pretty=True),
        "explain": explain.get("plan", []),
    }


def _has_select_wildcard(tree: exp.Expression) -> bool:
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Count):
                continue
            if isinstance(inner, exp.Star):
                return True
            if isinstance(inner, exp.Column) and inner.name == "*":
                return True
            if inner.find(exp.Star):
                return True
    return False


def _find_forbidden_functions(tree: exp.Expression) -> list[str]:
    names = []
    for node in tree.walk():
        expression = node[0] if isinstance(node, tuple) else node
        if isinstance(expression, exp.Anonymous):
            name = expression.name.lower()
            if name in FORBIDDEN_FUNCTIONS:
                names.append(name)
    return names


def _check_tables(tree: exp.Expression) -> dict[str, Any]:
    schema = load_schema()
    allowed_tables = set(schema["tables"].keys())
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE) if cte.alias_or_name}

    used_tables = set()
    invalid_qualified_tables = []
    for table in tree.find_all(exp.Table):
        table_name = table.name
        if not table_name:
            continue
        if table.db and table.db not in {"main"}:
            invalid_qualified_tables.append(table.sql(dialect="sqlite"))
        if table_name not in cte_names:
            used_tables.add(table_name)

    if invalid_qualified_tables:
        return {
            "ok": False,
            "error": f"Cross-database table references are not allowed: {invalid_qualified_tables}",
        }

    unknown_tables = sorted(used_tables - allowed_tables)
    if unknown_tables:
        return {
            "ok": False,
            "error": f"Unknown or disallowed tables: {unknown_tables}",
        }

    return {"ok": True, "used_tables": used_tables}


def explain_query(sql: str) -> dict[str, Any]:
    conn = None
    try:
        conn = connect_readonly()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql.strip().rstrip(';')}").fetchall()
        return {
            "ok": True,
            "plan": [dict(r) for r in rows],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"SQLite EXPLAIN failed: {exc}",
        }
    finally:
        if conn:
            conn.close()


def clamp_max_rows(max_rows: int) -> int:
    return max(1, min(int(max_rows), MAX_ROWS_CAP))


def wrap_with_limit(sql: str, max_rows: int) -> str:
    clean_sql = sql.strip().rstrip(";")
    safe_max_rows = clamp_max_rows(max_rows)
    return f"SELECT * FROM ({clean_sql}) AS _hermes_sqlite_query LIMIT {safe_max_rows + 1}"

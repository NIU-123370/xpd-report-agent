from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from .db import connect_readonly, get_mysql_config, load_schema

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

FORBIDDEN_FUNCTIONS = {
    # Resource-exhaustion and blocking primitives.
    "benchmark",
    "master_pos_wait",
    "sleep",
    "source_pos_wait",
    "wait_for_executed_gtid_set",
    "wait_until_sql_thread_after_gtids",
    # Advisory-lock functions mutate or retain server-side session state.
    "get_lock",
    "release_all_locks",
    "release_lock",
    # File/UDF primitives must never be reachable from generated SQL, even if
    # the database account is accidentally granted more privileges later.
    "load_extension",
    "load_file",
    "readfile",
    "sys_eval",
    "sys_exec",
    "writefile",
}
MAX_ROWS_CAP = 1000


def validate_sql(sql: str) -> dict[str, Any]:
    clean_sql = sql.strip()
    if not clean_sql:
        return {"ok": False, "error": "SQL is empty"}

    try:
        parsed = sqlglot.parse(clean_sql, read="mysql")
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

    if tree.find(exp.Lock):
        return {
            "ok": False,
            "error": "Locking SELECT queries are not allowed",
        }

    # Optimizer hints can override the session MAX_EXECUTION_TIME that protects
    # the database, so generated queries do not get to supply their own hints.
    if tree.find(exp.Hint):
        return {
            "ok": False,
            "error": "SQL optimizer hints are not allowed",
        }

    # MySQL executes the body of /*! ... */ version comments, while sqlglot
    # intentionally treats it as a comment. Reject it so validation and server
    # execution cannot see different statements.
    if _has_mysql_executable_comment(tree):
        return {
            "ok": False,
            "error": "MySQL executable comments are not allowed",
        }

    if tree.find(exp.Into):
        return {
            "ok": False,
            "error": "SELECT INTO is not allowed",
        }

    if _has_user_variable_assignment(tree):
        return {
            "ok": False,
            "error": "User-variable assignment is not allowed",
        }

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

    duplicate_columns = find_duplicate_column_names(_projected_column_names(tree))
    if duplicate_columns:
        return {
            "ok": False,
            "error": duplicate_column_error(duplicate_columns),
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
        "normalized_sql": tree.sql(dialect="mysql", pretty=True),
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
        elif isinstance(expression, exp.Func):
            name = expression.sql_name().lower()
        else:
            continue
        if name in FORBIDDEN_FUNCTIONS:
            names.append(name)
    return names


def _has_user_variable_assignment(tree: exp.Expression) -> bool:
    return any(
        isinstance(assignment.this, exp.Parameter)
        for assignment in tree.find_all(exp.PropertyEQ)
    )


def _has_mysql_executable_comment(tree: exp.Expression) -> bool:
    return any(
        comment.startswith("!")
        for expression in tree.walk()
        for comment in (expression.comments or ())
    )


def _projected_column_names(tree: exp.Expression) -> list[str]:
    """Derive MySQL result labels, including expressions without aliases."""

    labels: list[str] = []
    for projection in tree.selects:
        label = str(projection.alias_or_name or "")
        if not label:
            label = projection.sql(dialect="mysql", pretty=False)
        labels.append(label)
    return labels


def find_duplicate_column_names(columns: list[str]) -> list[str]:
    """Return duplicate result labels, comparing MySQL names case-insensitively."""

    first_spelling: dict[str, str] = {}
    duplicate_keys: set[str] = set()
    duplicates: list[str] = []
    for column in columns:
        key = column.casefold()
        if key in first_spelling:
            if key not in duplicate_keys:
                duplicate_keys.add(key)
                duplicates.append(first_spelling[key])
        else:
            first_spelling[key] = column
    return duplicates


def duplicate_column_error(duplicates: list[str]) -> str:
    labels = ", ".join(duplicates)
    return (
        f"Duplicate output column names are not allowed: {labels}. "
        "Use a unique AS alias for every selected column."
    )


def _check_tables(tree: exp.Expression) -> dict[str, Any]:
    schema = load_schema()
    allowed_tables = set(schema["tables"].keys())
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE) if cte.alias_or_name}

    used_tables = set()
    database = get_mysql_config()["database"]
    invalid_qualified_tables = []
    for table in tree.find_all(exp.Table):
        table_name = table.name
        if not table_name:
            continue
        if table.db and table.db != database:
            invalid_qualified_tables.append(table.sql(dialect="mysql"))
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
        with conn.cursor() as cursor:
            cursor.execute(f"EXPLAIN {sql.strip().rstrip(';')}")
            rows = cursor.fetchall()
        return {
            "ok": True,
            "plan": [dict(r) for r in rows],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"MySQL EXPLAIN failed: {exc}",
        }
    finally:
        if conn:
            conn.close()


def clamp_max_rows(max_rows: int) -> int:
    return max(1, min(int(max_rows), MAX_ROWS_CAP))


def wrap_with_limit(sql: str, max_rows: int) -> str:
    clean_sql = sql.strip().rstrip(";")
    safe_max_rows = clamp_max_rows(max_rows)
    return f"SELECT * FROM ({clean_sql}) AS _hermes_mysql_query LIMIT {safe_max_rows + 1}"

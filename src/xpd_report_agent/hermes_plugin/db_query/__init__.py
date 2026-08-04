from __future__ import annotations

from . import schemas, tools

MYSQL_REQUIRED_ENV = [
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
]


def register(ctx) -> None:
    ctx.register_tool(
        name="db_get_schema_ddl",
        toolset="db_query",
        schema=schemas.DB_GET_SCHEMA_DDL,
        handler=tools.db_get_schema_ddl,
        requires_env=MYSQL_REQUIRED_ENV,
    )
    ctx.register_tool(
        name="db_schema_search",
        toolset="db_query",
        schema=schemas.DB_SCHEMA_SEARCH,
        handler=tools.db_schema_search,
        requires_env=MYSQL_REQUIRED_ENV,
    )
    ctx.register_tool(
        name="db_get_table_profile",
        toolset="db_query",
        schema=schemas.DB_GET_TABLE_PROFILE,
        handler=tools.db_get_table_profile,
        requires_env=MYSQL_REQUIRED_ENV,
    )
    ctx.register_tool(
        name="db_get_join_paths",
        toolset="db_query",
        schema=schemas.DB_GET_JOIN_PATHS,
        handler=tools.db_get_join_paths,
        requires_env=MYSQL_REQUIRED_ENV,
    )
    ctx.register_tool(
        name="db_validate_sql",
        toolset="db_query",
        schema=schemas.DB_VALIDATE_SQL,
        handler=tools.db_validate_sql,
        requires_env=MYSQL_REQUIRED_ENV,
    )
    ctx.register_tool(
        name="db_execute_sql",
        toolset="db_query",
        schema=schemas.DB_EXECUTE_SQL,
        handler=tools.db_execute_sql,
        requires_env=MYSQL_REQUIRED_ENV,
    )

DB_GET_SCHEMA_DDL = {
    "name": "db_get_schema_ddl",
    "description": "Get the SQLite demo database DDL and table relationship summary. Call this first, before any other db-query tool, when answering SQLite database questions.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

DB_SCHEMA_SEARCH = {
    "name": "db_schema_search",
    "description": "Search relevant SQLite tables, columns, business meanings, and metrics for a natural language database question. Use only after db_get_schema_ddl has been called for the question.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The user's natural language data query.",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of matching tables to return.",
                "default": 8,
            },
        },
        "required": ["question"],
    },
}

DB_GET_TABLE_PROFILE = {
    "name": "db_get_table_profile",
    "description": "Get metadata for selected SQLite tables, including columns, primary keys, foreign keys, indexes, row counts, and sample rows.",
    "parameters": {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Table names.",
            },
            "include_samples": {
                "type": "boolean",
                "default": True,
            },
        },
        "required": ["tables"],
    },
}

DB_GET_JOIN_PATHS = {
    "name": "db_get_join_paths",
    "description": "Find join paths between SQLite tables using foreign key metadata.",
    "parameters": {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Table names that need to be joined.",
            },
        },
        "required": ["tables"],
    },
}

DB_VALIDATE_SQL = {
    "name": "db_validate_sql",
    "description": "Validate SQLite SQL before execution. Only safe read-only SELECT queries are allowed.",
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL query to validate.",
            },
        },
        "required": ["sql"],
    },
}

DB_EXECUTE_SQL = {
    "name": "db_execute_sql",
    "description": "Execute validated read-only SQLite SQL and return result rows.",
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "Validated SQL query.",
            },
            "max_rows": {
                "type": "integer",
                "default": 100,
            },
        },
        "required": ["sql"],
    },
}

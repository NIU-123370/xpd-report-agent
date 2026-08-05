DB_GET_SCHEMA_DDL = {
    "name": "db_get_schema_ddl",
    "description": (
        "Get the complete MySQL report database DDL and relationship summary for "
        "diagnostics. Use only when schema search has no usable result, schema metadata "
        "is inconsistent, or the user explicitly requests schema troubleshooting."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

DB_SCHEMA_SEARCH = {
    "name": "db_schema_search",
    "description": (
        "Search relevant MySQL tables, columns, business meanings, and metrics for a "
        "natural language database question. Use this as the first database discovery tool."
    ),
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
    "description": (
        "Get metadata for selected MySQL tables, including columns, primary keys, foreign "
        "keys, indexes, and row counts. Sample rows are optional and should be requested "
        "only when metadata is insufficient to resolve table semantics."
    ),
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
                "default": False,
            },
        },
        "required": ["tables"],
    },
}

DB_GET_JOIN_PATHS = {
    "name": "db_get_join_paths",
    "description": "Find join paths between MySQL tables using foreign key metadata.",
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
    "description": "Validate MySQL SQL before execution. Only safe read-only SELECT queries are allowed.",
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
    "description": "Execute validated read-only MySQL SQL and return result rows.",
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "Validated SQL query.",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Maximum rows to return. Defaults to 100 for ordinary analysis "
                    "and 1000 when capture_for_export=true."
                ),
            },
            "capture_for_export": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Compatibility hint: set true when the same request combines a new query "
                    "with file export, raising the default row cap from 100 to 1000. Every "
                    "successful session query already returns a short-lived result_id, so a "
                    "later pure export request must reuse that result_id instead of re-querying."
                ),
            },
            "quality_context": {
                "type": "object",
                "description": (
                    "Optional bounded context used by the server to calculate period "
                    "coverage, zero denominators, small samples, and missing dimensions."
                ),
                "properties": {
                    "period_start": {"type": "string", "format": "date"},
                    "period_end": {"type": "string", "format": "date"},
                    "time_column": {"type": "string"},
                    "required_dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                    },
                    "denominator_columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                    },
                    "sample_size_columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                    },
                    "small_sample_threshold": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                        "default": 30,
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["sql"],
    },
}

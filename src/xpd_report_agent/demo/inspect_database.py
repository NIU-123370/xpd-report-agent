from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from xpd_report_agent.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
DEFAULT_DB_PATH = ROOT / "data" / "demo_ecommerce.sqlite"


def resolve_db_path() -> Path:
    return Path(os.environ.get("HERMES_DEMO_SQLITE_PATH", DEFAULT_DB_PATH)).expanduser().resolve()


def quote_ident(name: str) -> str:
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'


def inspect_database(db_path: Path) -> None:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

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

    print(f"Database: {db_path}")
    print(f"Tables: {', '.join(tables)}")

    for table in tables:
        row_count = conn.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(table)}").fetchone()["c"]
        print(f"\n{table} ({row_count} rows)")
        for col in conn.execute(f"PRAGMA table_info({quote_ident(table)})"):
            pk = " PK" if col["pk"] else ""
            required = " NOT NULL" if col["notnull"] else ""
            print(f"  - {col['name']} {col['type']}{required}{pk}")
        for fk in conn.execute(f"PRAGMA foreign_key_list({quote_ident(table)})"):
            print(f"  FK {fk['from']} -> {fk['table']}.{fk['to']}")

    conn.close()


def main() -> None:
    inspect_database(resolve_db_path())


if __name__ == "__main__":
    main()

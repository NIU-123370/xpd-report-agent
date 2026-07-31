from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .db import load_schema


def build_graph() -> dict[str, list[dict[str, str]]]:
    schema = load_schema()
    graph: dict[str, list[dict[str, str]]] = defaultdict(list)

    for fk in schema["foreign_keys"]:
        src = fk["from_table"]
        dst = fk["to_table"]

        edge = {
            "from_table": src,
            "from_column": fk["from_column"],
            "to_table": dst,
            "to_column": fk["to_column"],
            "join_condition": f"{src}.{fk['from_column']} = {dst}.{fk['to_column']}",
        }
        reverse_edge = {
            "from_table": dst,
            "from_column": fk["to_column"],
            "to_table": src,
            "to_column": fk["from_column"],
            "join_condition": f"{dst}.{fk['to_column']} = {src}.{fk['from_column']}",
        }

        graph[src].append(edge)
        graph[dst].append(reverse_edge)

    return graph


def shortest_path(start: str, target: str) -> list[dict[str, str]] | None:
    graph = build_graph()
    queue: deque[tuple[str, list[dict[str, str]]]] = deque([(start, [])])
    visited = {start}

    while queue:
        current, path = queue.popleft()
        if current == target:
            return path

        for edge in graph[current]:
            next_table = edge["to_table"]
            if next_table not in visited:
                visited.add(next_table)
                queue.append((next_table, path + [edge]))

    return None


def find_join_paths(tables: list[str]) -> dict[str, Any]:
    schema = load_schema()
    existing = set(schema["tables"].keys())
    normalized_tables = list(dict.fromkeys(tables))

    unknown = [table for table in normalized_tables if table not in existing]
    if unknown:
        return {
            "ok": False,
            "error": f"Unknown tables: {unknown}",
            "join_paths": [],
        }

    paths = []
    for i in range(len(normalized_tables)):
        for j in range(i + 1, len(normalized_tables)):
            src = normalized_tables[i]
            dst = normalized_tables[j]
            path = shortest_path(src, dst)
            paths.append(
                {
                    "from": src,
                    "to": dst,
                    "path": path,
                    "reachable": path is not None,
                }
            )

    return {
        "ok": True,
        "join_paths": paths,
        "guidance": [
            "一对多 JOIN 后统计订单数时，应使用 COUNT(DISTINCT orders.order_id)。",
            "统计 GMV 时优先从 order_items 聚合。",
            "退款与订单明细直接 JOIN 可能导致重复，需要注意退款粒度。",
        ],
    }

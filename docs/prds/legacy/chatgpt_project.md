# 基于 Hermes Agent + SQLite 的多表查询验证系统技术开发文档

## 1. 项目目标

本项目用于快速验证：

1. 接入完整 Hermes Agent；
2. 使用本地 SQLite 数据库；
3. 内置 5～8 张表的示例数据库；
4. 通过自然语言提问，验证 Hermes 调用自定义数据库查询工具的效果；
5. 验证多表查询、JOIN 路径、SQL 生成、SQL 校验、SQL 执行、结果解释的完整链路。

本项目不是生产级 SaaS，不做多租户、不做复杂权限、不做用户独立数据库接入。

---

## 2. 验证边界

### 2.1 本次验证包含

* 完整 Hermes Agent API Server；
* 自定义 `db-query` Hermes Plugin；
* 自定义 `db-multitable-query` Skill；
* 本地 SQLite 示例库；
* 7 张电商业务表；
* SQLite schema 自动读取；
* 简单 schema 检索；
* JOIN 路径发现；
* SQL AST 安全校验；
* 只读 SQL 执行；
* Hermes API 调用测试；
* 自然语言多表查询测试集。

### 2.2 本次验证不包含

* 多用户账号系统；
* 多租户隔离；
* 每个用户独立数据库；
* 复杂权限；
* 数据脱敏；
* 报表订阅；
* 可视化图表；
* 生产级队列和监控；
* 企业私有网络接入。

---

## 3. 总体架构

```text
用户 / 开发者
   ↓
curl / Postman / Open WebUI / 简单 FastAPI Wrapper
   ↓
Hermes API Server
   ↓
完整 Hermes Agent Runtime
   ↓
db-multitable-query Skill
   ↓
db-query Plugin Tools
   ├── db_schema_search
   ├── db_get_table_profile
   ├── db_get_join_paths
   ├── db_validate_sql
   └── db_execute_sql
   ↓
本地 SQLite 示例数据库
   ↓
查询结果
```

本次验证可以先不做前端，直接用 Hermes API Server 测试。

---

## 4. 推荐目录结构

```text
hermes-sqlite-demo/
├── README.md
├── requirements.txt
├── data/
│   └── demo_ecommerce.sqlite
├── scripts/
│   ├── create_demo_db.py
│   └── inspect_db.py
├── hermes_plugins/
│   └── db_query/
│       ├── plugin.yaml
│       ├── __init__.py
│       ├── schemas.py
│       ├── tools.py
│       ├── db.py
│       ├── schema_index.py
│       ├── join_graph.py
│       └── sql_guard.py
└── skills/
    └── db-multitable-query/
        └── SKILL.md
```

部署到 Hermes 后，目录需要被复制或挂载为：

```text
~/.hermes/plugins/db-query/
~/.hermes/skills/db-multitable-query/
```

---

## 5. Python 依赖

`requirements.txt`

```txt
sqlglot>=25.0.0
pyyaml>=6.0.0
```

SQLite 使用 Python 标准库 `sqlite3`，不需要额外安装。

安装：

```bash
pip install -r requirements.txt
```

---

## 6. 示例数据库设计

本次使用 7 张表：

```text
customers      客户表
categories     商品类目表
products       商品表
orders         订单主表
order_items    订单明细表
payments       支付表
refunds        退款表
```

### 6.1 表关系

```text
customers 1 ─── N orders
orders    1 ─── N order_items
products  1 ─── N order_items
categories 1 ── N products
orders    1 ─── N payments
orders    1 ─── N refunds
order_items 1 ─ N refunds
```

### 6.2 业务口径

```text
GMV = SUM(order_items.quantity * order_items.unit_price)

订单数 = COUNT(DISTINCT orders.order_id)

支付金额 = SUM(payments.amount)，只统计 status = 'success'

退款金额 = SUM(refunds.refund_amount)，只统计 status = 'success'

退款率 = 退款金额 / GMV

有效订单 = orders.status IN ('paid', 'shipped', 'completed')

最近30天 = orders.order_date >= date('now', '-30 day')
```

---

## 7. 创建 SQLite 示例库

`scripts/create_demo_db.py`

```python
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import random


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "demo_ecommerce.sqlite"


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            city TEXT NOT NULL,
            signup_date TEXT NOT NULL
        );

        CREATE TABLE categories (
            category_id INTEGER PRIMARY KEY,
            category_name TEXT NOT NULL
        );

        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            brand_name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            list_price REAL NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(category_id)
        );

        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            status TEXT NOT NULL,
            channel TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );

        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );

        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );

        CREATE TABLE refunds (
            refund_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            order_item_id INTEGER,
            refund_date TEXT NOT NULL,
            refund_amount REAL NOT NULL,
            reason TEXT,
            status TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (order_item_id) REFERENCES order_items(order_item_id)
        );

        CREATE INDEX idx_orders_customer_id ON orders(customer_id);
        CREATE INDEX idx_orders_order_date ON orders(order_date);
        CREATE INDEX idx_order_items_order_id ON order_items(order_id);
        CREATE INDEX idx_order_items_product_id ON order_items(product_id);
        CREATE INDEX idx_products_category_id ON products(category_id);
        CREATE INDEX idx_payments_order_id ON payments(order_id);
        CREATE INDEX idx_refunds_order_id ON refunds(order_id);
        """
    )


def seed_data(conn: sqlite3.Connection):
    categories = [
        (1, "护肤"),
        (2, "彩妆"),
        (3, "香氛"),
        (4, "个护"),
    ]

    products = [
        (1, "水光保湿精华", "Aurelia", 1, 299.0),
        (2, "修护面霜", "Aurelia", 1, 399.0),
        (3, "清透粉底液", "Bella", 2, 259.0),
        (4, "丝绒口红", "Bella", 2, 169.0),
        (5, "木质淡香水", "Celinea", 3, 599.0),
        (6, "海盐洗发水", "DermaLab", 4, 129.0),
        (7, "身体乳", "DermaLab", 4, 99.0),
        (8, "抗老精华", "Aurelia", 1, 499.0),
        (9, "遮瑕膏", "Bella", 2, 149.0),
        (10, "玫瑰香水", "Celinea", 3, 699.0),
    ]

    customers = [
        (1, "张三", "上海", "2025-01-10"),
        (2, "李四", "北京", "2025-02-12"),
        (3, "王五", "广州", "2025-03-18"),
        (4, "赵六", "深圳", "2025-05-21"),
        (5, "钱七", "杭州", "2025-06-05"),
        (6, "孙八", "成都", "2025-07-09"),
        (7, "周九", "南京", "2025-08-14"),
        (8, "吴十", "武汉", "2025-09-20"),
    ]

    conn.executemany(
        "INSERT INTO categories(category_id, category_name) VALUES (?, ?)",
        categories,
    )
    conn.executemany(
        """
        INSERT INTO products(product_id, product_name, brand_name, category_id, list_price)
        VALUES (?, ?, ?, ?, ?)
        """,
        products,
    )
    conn.executemany(
        """
        INSERT INTO customers(customer_id, customer_name, city, signup_date)
        VALUES (?, ?, ?, ?)
        """,
        customers,
    )

    random.seed(7)
    today = datetime.now().date()

    order_id = 1
    order_item_id = 1
    payment_id = 1
    refund_id = 1

    channels = ["直播间", "商城", "小程序", "私域"]
    statuses = ["paid", "shipped", "completed", "cancelled"]
    payment_methods = ["alipay", "wechat", "card"]

    for day_offset in range(0, 90):
        order_date = today - timedelta(days=day_offset)

        for _ in range(random.randint(1, 4)):
            customer_id = random.randint(1, len(customers))
            status = random.choices(
                statuses,
                weights=[0.25, 0.25, 0.4, 0.1],
                k=1,
            )[0]
            channel = random.choice(channels)

            conn.execute(
                """
                INSERT INTO orders(order_id, customer_id, order_date, status, channel)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, customer_id, str(order_date), status, channel),
            )

            total_amount = 0.0
            item_count = random.randint(1, 3)
            current_order_item_ids = []

            for _ in range(item_count):
                product = random.choice(products)
                product_id = product[0]
                unit_price = product[4]
                quantity = random.randint(1, 3)
                amount = quantity * unit_price
                total_amount += amount

                conn.execute(
                    """
                    INSERT INTO order_items(order_item_id, order_id, product_id, quantity, unit_price)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_item_id, order_id, product_id, quantity, unit_price),
                )
                current_order_item_ids.append((order_item_id, amount))
                order_item_id += 1

            if status != "cancelled":
                conn.execute(
                    """
                    INSERT INTO payments(payment_id, order_id, payment_date, payment_method, amount, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payment_id,
                        order_id,
                        str(order_date),
                        random.choice(payment_methods),
                        round(total_amount, 2),
                        "success",
                    ),
                )
                payment_id += 1

                if random.random() < 0.18:
                    target_item_id, item_amount = random.choice(current_order_item_ids)
                    refund_amount = round(item_amount * random.choice([0.3, 0.5, 1.0]), 2)
                    refund_date = order_date + timedelta(days=random.randint(1, 10))

                    conn.execute(
                        """
                        INSERT INTO refunds(
                            refund_id, order_id, order_item_id, refund_date,
                            refund_amount, reason, status
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            refund_id,
                            order_id,
                            target_item_id,
                            str(refund_date),
                            refund_amount,
                            random.choice(["不喜欢", "尺码不合适", "破损", "七天无理由"]),
                            "success",
                        ),
                    )
                    refund_id += 1

            order_id += 1

    conn.commit()


def main():
    conn = connect()
    create_tables(conn)
    seed_data(conn)
    conn.close()
    print(f"Created demo database: {DB_PATH}")


if __name__ == "__main__":
    main()
```

执行：

```bash
python scripts/create_demo_db.py
```

---

## 8. Hermes Plugin 设计

### 8.1 插件能力

插件名称：

```text
db-query
```

工具列表：

```text
db_schema_search        根据自然语言问题检索相关表和字段
db_get_table_profile    获取表结构、字段、主键、外键、样例数据
db_get_join_paths       根据外键关系返回 JOIN 路径
db_validate_sql         校验 SQL 是否只读、安全、可执行
db_execute_sql          执行通过校验的 SQL
```

---

## 9. plugin.yaml

`hermes_plugins/db_query/plugin.yaml`

```yaml
name: db-query
version: 0.1.0
description: SQLite database query tools for Hermes Agent multi-table SQL verification.

provides_tools:
  - db_schema_search
  - db_get_table_profile
  - db_get_join_paths
  - db_validate_sql
  - db_execute_sql

requires_env:
  - HERMES_DEMO_SQLITE_PATH
```

---

## 10. 工具 Schema

`hermes_plugins/db_query/schemas.py`

```python
DB_SCHEMA_SEARCH = {
    "name": "db_schema_search",
    "description": "Search relevant SQLite tables, columns, business meanings, and metrics for a natural language database question.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The user's natural language data query."
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of matching tables to return.",
                "default": 8
            }
        },
        "required": ["question"]
    }
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
                "description": "Table names."
            },
            "include_samples": {
                "type": "boolean",
                "default": True
            }
        },
        "required": ["tables"]
    }
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
                "description": "Table names that need to be joined."
            }
        },
        "required": ["tables"]
    }
}

DB_VALIDATE_SQL = {
    "name": "db_validate_sql",
    "description": "Validate SQLite SQL before execution. Only safe read-only SELECT queries are allowed.",
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL query to validate."
            }
        },
        "required": ["sql"]
    }
}

DB_EXECUTE_SQL = {
    "name": "db_execute_sql",
    "description": "Execute validated read-only SQLite SQL and return result rows.",
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "Validated SQL query."
            },
            "max_rows": {
                "type": "integer",
                "default": 100
            }
        },
        "required": ["sql"]
    }
}
```

---

## 11. SQLite 连接与元数据读取

`hermes_plugins/db_query/db.py`

```python
import os
import sqlite3
from functools import lru_cache
from pathlib import Path


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


def connect_readwrite_for_schema_only() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@lru_cache(maxsize=1)
def load_schema() -> dict:
    conn = connect_readonly()

    tables = []
    for row in conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ):
        tables.append(row["name"])

    result = {
        "tables": {},
        "foreign_keys": []
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
            indexes.append({
                "name": index_name,
                "unique": bool(idx["unique"]),
                "columns": index_columns,
            })

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

    conn.close()
    return result


def get_sample_rows(table: str, limit: int = 3) -> list[dict]:
    conn = connect_readonly()
    rows = conn.execute(
        f"SELECT * FROM {quote_ident(table)} LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'
```

---

## 12. Schema 检索

`hermes_plugins/db_query/schema_index.py`

```python
from .db import load_schema


TABLE_DESCRIPTIONS = {
    "customers": "客户表，包含客户姓名、城市、注册日期。一行一个客户。",
    "categories": "商品类目表，包含护肤、彩妆、香氛、个护等类目。",
    "products": "商品表，包含商品名称、品牌、类目、标价。一行一个商品。",
    "orders": "订单主表，包含客户、下单日期、订单状态、销售渠道。一行一笔订单。",
    "order_items": "订单明细表，包含订单、商品、数量、成交单价。一行一个订单商品。",
    "payments": "支付表，包含订单支付方式、支付金额、支付状态。",
    "refunds": "退款表，包含订单退款金额、退款原因、退款状态。"
}


METRICS = {
    "gmv": {
        "description": "GMV，商品成交额，按订单明细 quantity * unit_price 计算。",
        "sql": "SUM(order_items.quantity * order_items.unit_price)",
        "tables": ["order_items", "orders"],
    },
    "order_count": {
        "description": "订单数，使用 COUNT(DISTINCT orders.order_id)。",
        "sql": "COUNT(DISTINCT orders.order_id)",
        "tables": ["orders"],
    },
    "refund_amount": {
        "description": "退款金额，只统计 refunds.status = 'success'。",
        "sql": "SUM(refunds.refund_amount)",
        "tables": ["refunds"],
    },
    "refund_rate": {
        "description": "退款率 = 退款金额 / GMV。",
        "tables": ["refunds", "order_items"],
    },
    "paid_amount": {
        "description": "支付金额，只统计 payments.status = 'success'。",
        "sql": "SUM(payments.amount)",
        "tables": ["payments"],
    }
}


SYNONYMS = {
    "销售额": ["gmv", "成交额", "金额", "销售"],
    "GMV": ["gmv", "销售额", "成交额"],
    "订单数": ["order_count", "订单", "下单"],
    "退款": ["refunds", "refund_amount", "refund_rate"],
    "退款率": ["refund_rate", "退款", "GMV"],
    "品牌": ["brand_name", "products"],
    "类目": ["category_name", "categories"],
    "渠道": ["channel", "orders"],
    "城市": ["city", "customers"],
    "支付": ["payments", "payment_method", "paid_amount"],
}


def search_schema(question: str, top_k: int = 8) -> dict:
    schema = load_schema()
    q = question.lower()

    expanded_terms = set(q.split())
    for key, values in SYNONYMS.items():
        if key.lower() in q:
            expanded_terms.update(x.lower() for x in values)

    table_hits = []

    for table_name, meta in schema["tables"].items():
        columns = [c["name"] for c in meta["columns"]]
        text = " ".join([
            table_name,
            TABLE_DESCRIPTIONS.get(table_name, ""),
            " ".join(columns),
        ]).lower()

        score = 0
        for term in expanded_terms:
            if term and term in text:
                score += 1

        if table_name in q:
            score += 5

        if score > 0:
            table_hits.append({
                "table": table_name,
                "score": score,
                "description": TABLE_DESCRIPTIONS.get(table_name, ""),
                "columns": columns,
            })

    table_hits.sort(key=lambda x: x["score"], reverse=True)

    metric_hits = []
    for metric_name, metric in METRICS.items():
        text = (metric_name + " " + metric["description"]).lower()
        score = 0
        for term in expanded_terms:
            if term and term in text:
                score += 1
        if metric_name.lower() in q:
            score += 5
        if score > 0:
            metric_hits.append({
                "metric": metric_name,
                **metric,
                "score": score,
            })

    metric_hits.sort(key=lambda x: x["score"], reverse=True)

    return {
        "tables": table_hits[:top_k],
        "metrics": metric_hits[:top_k],
        "notes": [
            "GMV 默认使用 order_items.quantity * order_items.unit_price。",
            "有效订单默认使用 orders.status IN ('paid', 'shipped', 'completed')。",
            "退款金额默认只统计 refunds.status = 'success'。",
            "支付金额默认只统计 payments.status = 'success'。",
        ]
    }
```

---

## 13. JOIN 路径发现

`hermes_plugins/db_query/join_graph.py`

```python
from collections import deque, defaultdict
from .db import load_schema


def build_graph():
    schema = load_schema()
    graph = defaultdict(list)

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


def shortest_path(start: str, target: str):
    graph = build_graph()
    queue = deque([(start, [])])
    visited = {start}

    while queue:
        current, path = queue.popleft()

        if current == target:
            return path

        for edge in graph[current]:
            nxt = edge["to_table"]
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [edge]))

    return None


def find_join_paths(tables: list[str]) -> dict:
    schema = load_schema()
    existing = set(schema["tables"].keys())

    unknown = [t for t in tables if t not in existing]
    if unknown:
        return {
            "ok": False,
            "error": f"Unknown tables: {unknown}",
            "join_paths": []
        }

    paths = []
    for i in range(len(tables)):
        for j in range(i + 1, len(tables)):
            src = tables[i]
            dst = tables[j]
            path = shortest_path(src, dst)
            paths.append({
                "from": src,
                "to": dst,
                "path": path,
                "reachable": path is not None,
            })

    return {
        "ok": True,
        "join_paths": paths,
        "guidance": [
            "一对多 JOIN 后统计订单数时，应使用 COUNT(DISTINCT orders.order_id)。",
            "统计 GMV 时优先从 order_items 聚合。",
            "退款与订单明细直接 JOIN 可能导致重复，需要注意退款粒度。",
        ]
    }
```

---

## 14. SQL 安全校验

`hermes_plugins/db_query/sql_guard.py`

```python
import re
import sqlglot
from sqlglot import exp

from .db import load_schema, connect_readonly


FORBIDDEN_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Truncate,
    exp.Command,
)


def validate_sql(sql: str) -> dict:
    sql = sql.strip()

    if not sql:
        return {"ok": False, "error": "SQL is empty"}

    if ";" in sql.rstrip(";"):
        return {"ok": False, "error": "Only one SQL statement is allowed"}

    try:
        parsed = sqlglot.parse(sql, read="sqlite")
    except Exception as exc:
        return {"ok": False, "error": f"SQL parse error: {exc}"}

    if len(parsed) != 1:
        return {"ok": False, "error": "Only one SQL statement is allowed"}

    tree = parsed[0]

    for forbidden_type in FORBIDDEN_NODE_TYPES:
        if tree.find(forbidden_type):
            return {
                "ok": False,
                "error": f"Forbidden SQL operation: {forbidden_type.__name__}"
            }

    if not tree.find(exp.Select):
        return {"ok": False, "error": "Only SELECT queries are allowed"}

    if re.search(r"select\s+\*", sql, flags=re.IGNORECASE):
        return {
            "ok": False,
            "error": "SELECT * is not allowed. Please select explicit columns."
        }

    schema = load_schema()
    allowed_tables = set(schema["tables"].keys())

    used_tables = set()
    for table in tree.find_all(exp.Table):
        table_name = table.name
        if table_name:
            used_tables.add(table_name)

    unknown_tables = sorted(used_tables - allowed_tables)
    if unknown_tables:
        return {
            "ok": False,
            "error": f"Unknown or disallowed tables: {unknown_tables}"
        }

    explain = explain_query(sql)
    if not explain["ok"]:
        return explain

    return {
        "ok": True,
        "used_tables": sorted(used_tables),
        "normalized_sql": tree.sql(dialect="sqlite", pretty=True),
        "explain": explain.get("plan", []),
    }


def explain_query(sql: str) -> dict:
    conn = None
    try:
        conn = connect_readonly()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
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


def wrap_with_limit(sql: str, max_rows: int) -> str:
    clean_sql = sql.strip().rstrip(";")
    return f"SELECT * FROM ({clean_sql}) AS _hermes_sqlite_query LIMIT {int(max_rows) + 1}"
```

---

## 15. Plugin Tools 实现

`hermes_plugins/db_query/tools.py`

```python
import json
import time

from .db import load_schema, get_sample_rows, connect_readonly
from .schema_index import search_schema, TABLE_DESCRIPTIONS
from .join_graph import find_join_paths
from .sql_guard import validate_sql, wrap_with_limit


def to_json(data):
    return json.dumps(data, ensure_ascii=False, default=str)


def error_json(message):
    return to_json({
        "ok": False,
        "error": str(message)
    })


def db_schema_search(args, **kwargs):
    try:
        question = args["question"]
        top_k = int(args.get("top_k", 8))
        result = search_schema(question, top_k=top_k)
        return to_json({
            "ok": True,
            "question": question,
            **result,
        })
    except Exception as exc:
        return error_json(exc)


def db_get_table_profile(args, **kwargs):
    try:
        tables = args["tables"]
        include_samples = bool(args.get("include_samples", True))
        schema = load_schema()

        result = {}
        for table in tables:
            if table not in schema["tables"]:
                result[table] = {"ok": False, "error": "table not found"}
                continue

            result[table] = {
                "ok": True,
                "description": TABLE_DESCRIPTIONS.get(table, ""),
                **schema["tables"][table],
            }

            if include_samples:
                result[table]["sample_rows"] = get_sample_rows(table, limit=3)

        return to_json({
            "ok": True,
            "tables": result,
        })
    except Exception as exc:
        return error_json(exc)


def db_get_join_paths(args, **kwargs):
    try:
        tables = args["tables"]
        result = find_join_paths(tables)
        return to_json(result)
    except Exception as exc:
        return error_json(exc)


def db_validate_sql(args, **kwargs):
    try:
        sql = args["sql"]
        result = validate_sql(sql)
        return to_json(result)
    except Exception as exc:
        return error_json(exc)


def db_execute_sql(args, **kwargs):
    try:
        sql = args["sql"]
        max_rows = int(args.get("max_rows", 100))

        validation = validate_sql(sql)
        if not validation.get("ok"):
            return to_json({
                "ok": False,
                "error": "SQL validation failed before execution",
                "validation": validation,
            })

        started = time.time()
        limited_sql = wrap_with_limit(sql, max_rows=max_rows)

        conn = connect_readonly()
        rows = conn.execute(limited_sql).fetchall()
        conn.close()

        data = [dict(r) for r in rows[:max_rows]]
        columns = list(data[0].keys()) if data else []

        elapsed_ms = int((time.time() - started) * 1000)

        return to_json({
            "ok": True,
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": len(rows) > max_rows,
            "elapsed_ms": elapsed_ms,
            "sql": sql,
        })
    except Exception as exc:
        return error_json(exc)
```

---

## 16. Plugin 注册入口

`hermes_plugins/db_query/__init__.py`

```python
from . import schemas
from . import tools


def register(ctx):
    ctx.register_tool(
        name="db_schema_search",
        toolset="db_query",
        schema=schemas.DB_SCHEMA_SEARCH,
        handler=tools.db_schema_search,
    )

    ctx.register_tool(
        name="db_get_table_profile",
        toolset="db_query",
        schema=schemas.DB_GET_TABLE_PROFILE,
        handler=tools.db_get_table_profile,
    )

    ctx.register_tool(
        name="db_get_join_paths",
        toolset="db_query",
        schema=schemas.DB_GET_JOIN_PATHS,
        handler=tools.db_get_join_paths,
    )

    ctx.register_tool(
        name="db_validate_sql",
        toolset="db_query",
        schema=schemas.DB_VALIDATE_SQL,
        handler=tools.db_validate_sql,
    )

    ctx.register_tool(
        name="db_execute_sql",
        toolset="db_query",
        schema=schemas.DB_EXECUTE_SQL,
        handler=tools.db_execute_sql,
    )
```

说明：

如果你安装的 Hermes 版本中 tool handler 的函数签名有所不同，优先以当前 Hermes 插件模板为准。上面的工具主体函数仍然可以复用，只需要适配 handler 入参即可。

---

## 17. Skill 设计

`skills/db-multitable-query/SKILL.md`

````md
---
name: db-multitable-query
description: Use SQLite database query tools to answer natural language multi-table reporting questions.
version: 0.1.0
metadata:
  tags:
    - database
    - sqlite
    - sql
    - report
    - analytics
---

# DB Multi-table Query Skill

## When to Use

Use this skill when the user asks questions about:
- sales
- GMV
- orders
- customers
- products
- categories
- brands
- payments
- refunds
- refund rate
- channel performance
- city performance
- business reports
- multi-table SQL queries

## Workflow

1. Understand the user's data question.
2. If the time range is missing, use a reasonable default:
   - For recent performance questions, default to recent 30 days.
   - State this assumption in the final answer.
3. Call `db_schema_search` with the user's question.
4. Select relevant tables and metrics.
5. Call `db_get_table_profile` for candidate tables.
6. If more than one table is needed, call `db_get_join_paths`.
7. Draft SQLite SELECT SQL.
8. Always use explicit columns. Never use `SELECT *`.
9. For multi-step metrics, use CTEs.
10. Validate SQL by calling `db_validate_sql`.
11. Only if validation passes, call `db_execute_sql`.
12. Return the final answer with:
    - direct conclusion
    - assumptions
    - result table summary
    - SQL used
    - caveats

## Business Definitions

GMV:
`SUM(order_items.quantity * order_items.unit_price)`

Order count:
`COUNT(DISTINCT orders.order_id)`

Effective orders:
`orders.status IN ('paid', 'shipped', 'completed')`

Refund amount:
`SUM(refunds.refund_amount)` with `refunds.status = 'success'`

Refund rate:
`refund_amount / gmv`

Paid amount:
`SUM(payments.amount)` with `payments.status = 'success'`

## SQL Rules

- SQLite dialect only.
- Read-only SELECT only.
- Use CTEs for complex queries.
- Never use `SELECT *`.
- Use `COUNT(DISTINCT orders.order_id)` after joining order_items.
- Avoid direct many-to-many joins without pre-aggregation.
- Add clear aliases for result columns.
- Use `date('now', '-30 day')` for recent 30 days.
- Use `NULLIF(gmv, 0)` when calculating ratios.

## Clarification Rules

Ask a short clarification question only when:
- the requested metric is ambiguous and cannot be safely assumed
- the ranking metric is missing
- the time range changes the meaning materially
- the user asks for a specific report that requires unknown business logic

Otherwise, make an assumption and continue.

## Final Answer Format

Use this format:

结论：
...

查询假设：
- ...

结果摘要：
...

SQL：
```sql
...
````

注意事项：

* ...

````

---

## 18. 安装到 Hermes

假设当前项目目录为：

```bash
cd hermes-sqlite-demo
````

### 18.1 生成示例数据库

```bash
python scripts/create_demo_db.py
```

### 18.2 复制插件

```bash
mkdir -p ~/.hermes/plugins/db-query
cp -r hermes_plugins/db_query/* ~/.hermes/plugins/db-query/
```

### 18.3 复制 Skill

```bash
mkdir -p ~/.hermes/skills/db-multitable-query
cp skills/db-multitable-query/SKILL.md ~/.hermes/skills/db-multitable-query/SKILL.md
```

### 18.4 设置环境变量

```bash
export HERMES_DEMO_SQLITE_PATH="$(pwd)/data/demo_ecommerce.sqlite"
```

### 18.5 启用插件

```bash
hermes plugins enable db-query
```

检查插件：

```bash
hermes plugins list
```

如果插件未出现，检查：

```text
~/.hermes/plugins/db-query/plugin.yaml
~/.hermes/plugins/db-query/__init__.py
```

---

## 19. 启动完整 Hermes API Server

本次验证使用完整 Hermes，不裁剪默认工具。

设置：

```bash
export API_SERVER_ENABLED=true
export API_SERVER_KEY=dev-secret
export API_SERVER_HOST=127.0.0.1
export API_SERVER_PORT=8642
```

启动：

```bash
hermes gateway
```

如果 Hermes 版本要求把 API Server 配置写入配置文件，则按当前版本配置方式设置等价参数即可。

---

## 20. 调用 Hermes API 测试

### 20.1 基础连通测试

```bash
curl http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [
      {
        "role": "user",
        "content": "你好，请说明你可以做什么"
      }
    ],
    "stream": false
  }'
```

### 20.2 数据库查询测试

```bash
curl http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [
      {
        "role": "system",
        "content": "你是数据查询验证助手。用户问数据库、报表、销售、订单、商品、退款、支付相关问题时，必须使用 db-query 工具，不能编造结果。"
      },
      {
        "role": "user",
        "content": "最近30天每个品牌的GMV、订单数、退款金额和退款率是多少？"
      }
    ],
    "stream": false
  }'
```

### 20.3 流式调用测试

```bash
curl -N http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [
      {
        "role": "system",
        "content": "你是数据查询验证助手。必须通过 db-query 工具查询 SQLite 数据库，不能编造结果。"
      },
      {
        "role": "user",
        "content": "按类目统计最近30天的销售额和订单数，按销售额倒序。"
      }
    ],
    "stream": true
  }'
```

---

## 21. 推荐测试问题

### 21.1 单表查询

```text
数据库里一共有多少个客户？
```

预期会访问：

```text
customers
```

---

### 21.2 两表 JOIN

```text
按城市统计最近30天的订单数和GMV。
```

预期会访问：

```text
customers
orders
order_items
```

---

### 21.3 品牌 GMV

```text
最近30天每个品牌的GMV是多少？按GMV从高到低排序。
```

预期会访问：

```text
orders
order_items
products
```

---

### 21.4 类目销售表现

```text
按商品类目统计最近60天的GMV、订单数和购买件数。
```

预期会访问：

```text
categories
products
order_items
orders
```

---

### 21.5 退款率

```text
最近30天每个品牌的GMV、退款金额和退款率是多少？
```

预期会访问：

```text
orders
order_items
products
refunds
```

---

### 21.6 支付方式分析

```text
最近30天不同支付方式的支付金额和订单数是多少？
```

预期会访问：

```text
payments
orders
```

---

### 21.7 渠道分析

```text
最近30天各销售渠道的GMV、订单数、客单价是多少？
```

预期会访问：

```text
orders
order_items
```

---

### 21.8 复购客户

```text
最近90天复购客户有多少？复购率是多少？
```

预期会访问：

```text
customers
orders
```

---

## 22. 期望 SQL 示例

用户问题：

```text
最近30天每个品牌的GMV、订单数、退款金额和退款率是多少？
```

理想 SQL：

```sql
WITH brand_gmv AS (
    SELECT
        p.brand_name AS brand_name,
        COUNT(DISTINCT o.order_id) AS order_count,
        SUM(oi.quantity * oi.unit_price) AS gmv
    FROM orders o
    JOIN order_items oi
        ON o.order_id = oi.order_id
    JOIN products p
        ON oi.product_id = p.product_id
    WHERE o.order_date >= date('now', '-30 day')
      AND o.status IN ('paid', 'shipped', 'completed')
    GROUP BY p.brand_name
),
brand_refund AS (
    SELECT
        p.brand_name AS brand_name,
        SUM(r.refund_amount) AS refund_amount
    FROM refunds r
    JOIN order_items oi
        ON r.order_item_id = oi.order_item_id
    JOIN products p
        ON oi.product_id = p.product_id
    JOIN orders o
        ON r.order_id = o.order_id
    WHERE o.order_date >= date('now', '-30 day')
      AND r.status = 'success'
    GROUP BY p.brand_name
)
SELECT
    g.brand_name,
    g.order_count,
    ROUND(g.gmv, 2) AS gmv,
    ROUND(COALESCE(r.refund_amount, 0), 2) AS refund_amount,
    ROUND(COALESCE(r.refund_amount, 0) / NULLIF(g.gmv, 0), 4) AS refund_rate
FROM brand_gmv g
LEFT JOIN brand_refund r
    ON g.brand_name = r.brand_name
ORDER BY g.gmv DESC;
```

---

## 23. 可选：增加 FastAPI Wrapper

如果你不想每次直接 curl Hermes，可以加一个简单 wrapper。

目录：

```text
app/
├── main.py
└── requirements.txt
```

`app/main.py`

```python
import os
import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


app = FastAPI()

HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "http://127.0.0.1:8642/v1")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "dev-secret")


class ChatRequest(BaseModel):
    message: str
    stream: bool = True


SYSTEM_PROMPT = """
你是数据查询验证助手。

当用户提出数据库、报表、销售、订单、商品、客户、支付、退款相关问题时：
1. 必须使用 db_schema_search 检索相关表；
2. 必须使用 db_get_table_profile 获取表结构；
3. 多表查询必须使用 db_get_join_paths；
4. 必须生成 SQLite SELECT SQL；
5. 必须调用 db_validate_sql；
6. 只有校验通过后才能调用 db_execute_sql；
7. 不能编造数据库结果。

最终回答包含：
- 结论
- 查询假设
- 结果摘要
- SQL
- 注意事项
"""


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    payload = {
        "model": "hermes-agent",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": req.message},
        ],
        "stream": req.stream,
    }

    if not req.stream:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                f"{HERMES_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def events():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{HERMES_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        yield line + "\n"

    return StreamingResponse(events(), media_type="text/event-stream")
```

运行：

```bash
pip install fastapi uvicorn httpx pydantic
export HERMES_BASE_URL="http://127.0.0.1:8642/v1"
export HERMES_API_KEY="dev-secret"
uvicorn app.main:app --reload --port 8000
```

测试：

```bash
curl -N http://127.0.0.1:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": "最近30天各品牌GMV和退款率是多少？",
    "stream": true
  }'
```

---

## 24. 验证指标

本项目验证是否成功，可以看以下指标。

### 24.1 工具调用正确性

成功标准：

```text
数据库相关问题时，Hermes 能调用 db_schema_search。
多表问题时，Hermes 能调用 db_get_table_profile 和 db_get_join_paths。
执行前能调用 db_validate_sql。
校验通过后才调用 db_execute_sql。
```

### 24.2 SQL 正确性

成功标准：

```text
SQL 可以执行。
JOIN 路径正确。
没有 SELECT *。
没有 DDL / DML。
复杂指标使用 CTE。
订单数使用 COUNT(DISTINCT orders.order_id)。
退款率不会因为 JOIN 放大而明显错误。
```

### 24.3 结果解释质量

成功标准：

```text
回答中包含业务结论。
说明默认时间范围。
说明 GMV、退款率等指标口径。
展示 SQL。
不编造未查询到的数据。
```

### 24.4 多表能力

至少验证：

```text
orders + order_items
orders + customers
orders + order_items + products
orders + order_items + products + categories
orders + order_items + products + refunds
orders + payments
```

---

## 25. 常见问题排查

### 25.1 Hermes 没有看到工具

检查：

```bash
hermes plugins list
```

检查目录：

```text
~/.hermes/plugins/db-query/plugin.yaml
~/.hermes/plugins/db-query/__init__.py
```

检查是否启用：

```bash
hermes plugins enable db-query
```

检查启动日志中是否有插件加载错误。

---

### 25.2 工具报找不到数据库

检查：

```bash
echo $HERMES_DEMO_SQLITE_PATH
ls -lh $HERMES_DEMO_SQLITE_PATH
```

如果使用 systemd、Docker 或后台进程，确认环境变量传给了 Hermes 进程。

---

### 25.3 SQL 执行失败

可能原因：

```text
字段名生成错误
JOIN 路径错误
SQLite 方言不兼容
使用了 PostgreSQL 函数
使用了 SELECT *
SQL 中包含多语句
```

建议让 Hermes 重新生成 SQLite 方言 SQL。

---

### 25.4 Hermes 没有主动调用 db-query 工具

可以在调用时增加 system prompt：

```text
用户问数据库、销售、订单、商品、支付、退款、报表相关问题时，必须调用 db-query 工具，不能凭空回答。
```

也可以显式告诉它：

```text
请使用 db_schema_search、db_get_table_profile、db_get_join_paths、db_validate_sql、db_execute_sql 完成查询。
```

---

### 25.5 完整 Hermes 使用了别的工具

本次验证要求接完整 Hermes，因此默认能力不会裁剪。若它误用了终端、文件或其他工具，说明 prompt 和 Skill 约束不足。

验证阶段处理方式：

```text
1. 在 system prompt 中强制数据库问题只能使用 db-query 工具；
2. 在 Skill 中强调数据库查询流程；
3. 测试问题聚焦报表查询；
4. 如果仍频繁误用其他工具，再考虑做受限 Hermes Runtime。
```

---

## 26. 推荐开发顺序

### 第 1 步：创建 SQLite 示例库

```bash
python scripts/create_demo_db.py
```

验收：

```bash
sqlite3 data/demo_ecommerce.sqlite ".tables"
```

应看到：

```text
categories  customers  order_items  orders  payments  products  refunds
```

---

### 第 2 步：本地测试数据库读取

写一个简单脚本或 Python REPL 测试：

```python
from hermes_plugins.db_query.db import load_schema
print(load_schema().keys())
```

---

### 第 3 步：安装 Plugin

```bash
mkdir -p ~/.hermes/plugins/db-query
cp -r hermes_plugins/db_query/* ~/.hermes/plugins/db-query/
hermes plugins enable db-query
```

---

### 第 4 步：安装 Skill

```bash
mkdir -p ~/.hermes/skills/db-multitable-query
cp skills/db-multitable-query/SKILL.md ~/.hermes/skills/db-multitable-query/SKILL.md
```

---

### 第 5 步：启动 Hermes API Server

```bash
export HERMES_DEMO_SQLITE_PATH="$(pwd)/data/demo_ecommerce.sqlite"
export API_SERVER_ENABLED=true
export API_SERVER_KEY=dev-secret
export API_SERVER_HOST=127.0.0.1
export API_SERVER_PORT=8642
hermes gateway
```

---

### 第 6 步：调用测试

```bash
curl http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [
      {
        "role": "system",
        "content": "你是数据查询验证助手。必须使用 db-query 工具查询 SQLite 数据库，不能编造结果。"
      },
      {
        "role": "user",
        "content": "最近30天每个品牌的GMV、订单数、退款金额和退款率是多少？"
      }
    ],
    "stream": false
  }'
```

---

## 27. 最小验收标准

完成后，至少应通过以下 5 个问题：

```text
1. 最近30天每个品牌的GMV是多少？
2. 最近30天各类目的GMV和订单数是多少？
3. 最近30天各渠道的GMV、订单数和客单价是多少？
4. 最近60天各品牌的退款金额和退款率是多少？
5. 最近90天复购客户数和复购率是多少？
```

每个问题的输出应包含：

```text
结论
查询假设
结果摘要
SQL
注意事项
```

并且日志中应能看到 Hermes 调用了 db-query 插件工具。

---

## 28. 后续优化方向

验证通过后，可以继续优化：

```text
1. 增加语义层 YAML 管理；
2. 增加更多业务指标；
3. 增加更强的 schema 检索；
4. 增加 SQL 自动修复；
5. 增加结果缓存；
6. 增加 Web UI；
7. 将 SQLite 替换为真实业务数据库；
8. 将完整 Hermes 改成受限 Hermes Runtime；
9. 最终替换为轻量 Query Agent Orchestrator。
```

---

## 29. 本验证项目的核心判断

本项目的目标不是构建生产系统，而是验证：

```text
Hermes Agent 是否能稳定识别数据库查询任务；
Hermes 是否能正确调用自定义 db-query plugin；
Hermes 生成的多表 SQL 是否可执行；
通过 Skill + Plugin 是否能提升多表查询准确率；
查询结果是否能被 Hermes 正确解释成报表答案。
```

如果这几个点验证通过，后续再考虑工程化、服务化和安全收敛。


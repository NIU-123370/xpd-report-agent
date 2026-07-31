from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from xpd_report_agent.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "demo_ecommerce.sqlite"


def resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    raw_path = path or os.environ.get("HERMES_DEMO_SQLITE_PATH") or DEFAULT_DB_PATH
    return Path(raw_path).expanduser().resolve()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
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
        CREATE INDEX idx_refunds_order_item_id ON refunds(order_item_id);
        """
    )


def seed_data(conn: sqlite3.Connection, base_date: date | None = None) -> None:
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
    today = base_date or datetime.now().date()

    order_id = 1
    order_item_id = 1
    payment_id = 1
    refund_id = 1

    channels = ["直播间", "商城", "小程序", "私域"]
    statuses = ["paid", "shipped", "completed", "cancelled"]
    payment_methods = ["alipay", "wechat", "card"]

    for day_offset in range(90):
        order_date = today - timedelta(days=day_offset)

        for _ in range(random.randint(1, 4)):
            customer_id = random.randint(1, len(customers))
            status = random.choices(statuses, weights=[0.25, 0.25, 0.4, 0.1], k=1)[0]
            channel = random.choice(channels)

            conn.execute(
                """
                INSERT INTO orders(order_id, customer_id, order_date, status, channel)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, customer_id, str(order_date), status, channel),
            )

            total_amount = 0.0
            current_order_item_ids: list[tuple[int, float]] = []
            for _ in range(random.randint(1, 3)):
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


def create_database(path: str | os.PathLike[str] | None = None, base_date: date | None = None) -> Path:
    db_path = resolve_db_path(path)
    conn = connect(db_path)
    try:
        create_tables(conn)
        seed_data(conn, base_date=base_date)
    finally:
        conn.close()
    return db_path


def main() -> None:
    db_path = create_database()
    print(f"Created demo database: {db_path}")


if __name__ == "__main__":
    main()

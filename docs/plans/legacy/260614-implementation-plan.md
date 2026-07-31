# Hermes SQLite Demo 实施计划

## 交付内容

- 建库和检查脚本：`scripts/create_demo_db.py`、`scripts/inspect_db.py`。
- Hermes 插件：`hermes_plugins/db_query/`。
- Hermes Skill：`skills/db-multitable-query/SKILL.md`。
- FastAPI Wrapper：`app/main.py`。
- 静态聊天页：`app/static/index.html`、`app/static/styles.css`、`app/static/app.js`。
- 测试：`tests/`。
- 依赖：`requirements.txt`。

## 实施步骤

1. 创建 SQLite 示例库脚本，生成 7 张电商表和 90 天随机订单数据。
2. 实现数据库访问层，支持 `HERMES_DEMO_SQLITE_PATH` 和只读连接。
3. 实现只读 schema DDL、schema 检索、表画像、JOIN 路径、SQL 安全校验和只读执行。
4. 注册 Hermes 插件工具，保持工具名与 PRD 一致。
5. 编写 Skill，强制数据库问题走工具链路。
6. 实现 FastAPI Wrapper 的非流式和流式代理接口。
7. 实现静态前端对话框，支持流式展示、错误展示和示例问题。
8. 补充 pytest，使用临时 SQLite 数据库验证核心能力。

## 验证命令

```bash
pip install -r requirements.txt
python scripts/create_demo_db.py
export HERMES_DEMO_SQLITE_PATH="$PWD/data/demo_ecommerce.sqlite"
pytest
```

Hermes 联调：

```bash
./launch/serv/hermes.sh prepare
./launch/launch.sh start hermes
```

Wrapper：

```bash
./launch/launch.sh start fastapi
```

统一启动/停止：

```bash
./launch/launch.sh start
./launch/launch.sh status
./launch/launch.sh stop
```

## 验收问题

- 最近30天每个品牌的GMV是多少？
- 最近30天各类目的GMV和订单数是多少？
- 最近30天各渠道的GMV、订单数和客单价是多少？
- 最近60天各品牌的退款金额和退款率是多少？
- 最近90天复购客户数和复购率是多少？

每个回答应包含结论、查询假设、结果摘要、SQL 和注意事项，并能在 Hermes 日志中看到 `db-query` 工具调用。

数据库问题的目标工具链路：

```text
db_get_schema_ddl
db_schema_search
db_get_table_profile
db_get_join_paths
db_validate_sql
db_execute_sql
```

Hermes 不应调用 `execute_code`、terminal、shell、file editing 或 browser 工具来理解或查询 SQLite 数据库。

启动前置检查：

- Hermes venv 可导入 `sqlglot`。
- `hermes plugins list --plain --no-bundled` 显示 `db-query` enabled。
- Wrapper `/health` 的 `db_query.ok` 为 `true`，且 `missing_tools` 为空。

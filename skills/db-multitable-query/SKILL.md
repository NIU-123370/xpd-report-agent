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

Use this skill when the user asks questions about sales, GMV, orders, customers, products, categories, brands, payments, refunds, refund rate, channel performance, city performance, business reports, or multi-table SQL queries.

## Workflow

1. For database, reporting, sales, order, product, refund, or payment questions, the first tool call must be `db_get_schema_ddl`.
2. Do not call any other tool before `db_get_schema_ddl` returns.
3. Do not use `execute_code`, terminal, shell, file editing, or browser tools to inspect or query the database.
4. Understand the user's data question.
5. If the time range is missing, use a reasonable default:
   - For recent performance questions, default to recent 30 days.
   - State this assumption in the final answer.
6. Call `db_schema_search` with the user's question.
7. Select relevant tables and metrics.
8. Call `db_get_table_profile` for candidate tables.
9. If more than one table is needed, call `db_get_join_paths`.
10. Draft SQLite SELECT SQL.
11. Always use explicit columns. Never use `SELECT *`.
12. For multi-step metrics, use CTEs.
13. Validate SQL by calling `db_validate_sql`.
14. Only if validation passes, call `db_execute_sql`.
15. Return the final answer with direct conclusion, assumptions, result table summary, SQL used, and caveats.

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
- Use `COUNT(DISTINCT orders.order_id)` after joining `order_items`.
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
```

注意事项：
- ...

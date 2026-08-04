---
name: db-multitable-query
description: 使用 MySQL 报表数据库工具回答淘宝直播电商数据问题。用户询问直播场次、商品、曝光、点击、加购、支付、转化、退款、粉丝、互动、打赏、趋势分析或多表 SQL 时使用。
metadata:
  tags:
    - database
    - mysql
    - sql
    - report
    - analytics
---

# 淘宝直播多表查询

## 语言要求

- 分析、模型思考过程（`reasoning` / `thinking`）、工具调用说明和最终回答全部使用简体中文。
- 即使工具返回英文，也继续使用中文分析，不要切换成英文思考。
- 工具名、表名、字段名、SQL 关键字和必要的技术标识保持原文，不要翻译或改名。
- 不要中英文重复输出同一段内容。

## 查询流程

1. 第一次工具调用必须是 `db_get_schema_ddl`。
2. `db_get_schema_ddl` 返回前，不要调用任何其他工具。
3. 不要使用 `execute_code`、`terminal`、`shell`、`file editing`、`browser` 或任意代码查询数据库。
4. 使用用户的原始问题调用 `db_schema_search`。
5. 对候选表调用 `db_get_table_profile`。
6. 需要多张表时调用 `db_get_join_paths`。
7. 使用明确列名编写只读 MySQL `SELECT` SQL。
8. 调用 `db_validate_sql` 校验 SQL。
9. 只有校验成功后才能调用 `db_execute_sql`。
10. 绝不编造数据库查询结果。

## 表粒度

- `tb_live_goods_daily_stats`：每个日期、每个商品一行（`item_id + stat_date`）。
- `tb_live_goods_session_stats`：每场直播、每个商品一行（`item_id + live_session_id`）。
- `tb_session_endtime_stats`：每场直播一行汇总（`live_session_id`）。

直播商品表通过 `live_session_id` 关联直播汇总表。将一行直播汇总数据关联到多行商品数据后，
直播层指标会被重复。因此，先聚合商品数据再关联，或确保每场直播的汇总指标只取一次。

## 业务口径

- 支付金额：`SUM(pay_amt)`。
- 支付订单数：`SUM(pay_ord_cnt)`。
- 退款金额：`SUM(refund_amt)`。
- 金额退款率：`SUM(refund_amt) / NULLIF(SUM(pay_amt), 0)`。
- 商品点击率：`SUM(item_click_uv) / NULLIF(SUM(item_exposure_uv), 0)`。
- 来源占比字段以小数存储，回答用户时格式化为百分比。

商品粒度的买家数在不同商品之间可能重复。计算整场直播的买家数和转化率时，优先使用
`tb_session_endtime_stats`，不要直接累加商品粒度的买家数。

## SQL 规则

- 只使用 MySQL 方言。
- 只允许只读 `SELECT`。
- 禁止使用 `SELECT *`。
- 多步骤指标使用 CTE。
- 默认最近 30 天范围使用 `CURRENT_DATE - INTERVAL 30 DAY`。
- 比率分母使用 `NULLIF(..., 0)`。
- 不要在缺少商品标识和明确日期规则时直接关联日粒度表与场次粒度表。

## 最终回答

使用中文直接给出结论、查询假设、精简结果摘要、实际执行的 SQL 和注意事项。

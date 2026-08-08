# xpd-report-agent 系统架构

## 组件关系

```text
中台后端（Bearer 服务凭证 + X-User-Id）/本地静态调试页
  -> FastAPI Wrapper（服务鉴权、owner 校验、全局 Agent 并发上限 3、幂等任务状态）
  -> Hermes Gateway（Session、memory、clarify、受限 file read；session_search 仅本地安全模式）
      -> db-query Plugin -> MySQL -> 短期查询结果注册表
                         -> report_file -> CSV / XLSX / Markdown / PDF / JSON -> OSS
      -> state.db        -> 会话历史
      -> memories/       -> MEMORY.md / USER.md
      -> report-files/   -> 按 session-id 隔离的导出文件
```

FastAPI 不直接生成或执行 SQL，也不建立第二套消息数据库。数据库理解、JOIN 路径发现、SQL
校验和执行由 Hermes Plugin 完成；原始会话消息以 Hermes `state.db` 为唯一事实源。

Session SSE、本地同步兼容接口和中台 Run 复用同一套 turn 路径、payload、系统提示词和提交前
上下文准备。中台 Run 额外用持久化状态机提供幂等、重试与
`pending/running/waiting_input/succeeded/failed`；旧的无 Session `/api/chat*` 仅保留为已弃用的
迁移入口。

## 预设经营分析

- `GET /api/analysis-presets` 只通过 `INFORMATION_SCHEMA.COLUMNS` 检查每种分析需要的字段，
  返回前端可用性、限制说明和允许的指标；不会为能力检查扫描整张业务表。
- `POST /api/sessions/{session_id}/analyses` 接受受限的 preset、周期、分析维度和 TopN 参数，
  在服务端生成固定中文任务说明，再复用普通 Session SSE、owner scope、SQL 验证和并发保护。
- 退款诊断只能输出金额退款率、订单退款率、退款贡献等异常线索。比率必须由分子分母加权重算，
  不允许对行级比率求和，也不允许在没有退款原因、物流、售后字段时声称找到根因。
- 商品排行使用 `item_id` 作为稳定实体、`item_title` 作为展示标签，不发明综合评分；无成本、
  毛利、库存、品牌或类目字段时，也不扩展成对应榜单。
- 复购必须能识别同一买家的多次有效订单。当前数据没有稳定 `buyer_id/customer_id` 和
  `order_id`，能力接口返回阻断状态；禁止使用聚合的 `pay_ord_cnt/pay_byr_cnt` 推算复购。

## Hermes 原生定时报告（默认关闭）

微服务默认设置 `XPD_SCHEDULES_ENABLED=false`、`XPD_HERMES_CRON_PATCH=false`，前端隐藏入口；
`/health` 将主动关闭视为健康。以下为需要时可重新开启的保留实现：

- 前端只调用 owner-scoped `/api/schedules`，不会接触 Hermes 全局 `/api/jobs` Bearer 或其他
  用户的任务。映射持久化为 `schedule_id -> native_job_id -> owner_scope/report_type`。
- Hermes 原生 Cron 是唯一计时器，使用 `jobs.json`、`executions.db`、文件锁和每 60 秒 ticker。
  前端输入被转换为 ISO 单次时间或 5 段 Cron，统一使用 `Asia/Shanghai`，精确到分钟。
- 原生 Cron 强制使用 `cron_*` Session，而查询结果与附件安全模型只接受 `xpd_<scope>_*`。
  因此原生任务使用受限 `no_agent` 回调：到点后调用 FastAPI 内部接口，由后端创建真正的
  `xpd_*_scheduled_*` Session，再走现有 Agent 查询与导出链路。
- 回调脚本位于 Hermes Home 的 `scripts/`，权限为 `0600`，携带每任务随机 capability token；
  原生任务自身不加载模型、memory、terminal 或 file。
- 运行使用“任务 + 自然日/自然周”幂等键；重复 ticker/补跑不会重复生成。成功必须检测到真实
  artifact，而不以模型文本作为成功依据。自动报告 Session 在 API 和前端均只读，不触发结束
  反思；删除计划保留已经生成的历史报告。
- 当前数据源没有明确品牌维度。后端通过 `INFORMATION_SCHEMA.COLUMNS` 做轻量能力检查；缺少
  同时包含 `item_id` 和品牌字段的表时，`weekly_brand` 返回
  `blocked_missing_brand_dimension`，不允许从 `item_title` 猜测。

## 报表文件导出

- 每次成功的 Session 查询都会把已执行 SQL、准确列、返回行和截断状态短期登记为 owner-scoped
  `result_id`，但普通分析不生成文件。同一请求同时要求查询和导出时可用
  `capture_for_export=true` 将默认结果上限从 100 提高到 1000；用户后续仅要求转换格式时直接复用
  最近的 `result_id`，不再重新检索 Schema、规划或执行 SQL。
- `export_report_file` 只接受当前 session 所有的未过期 `result_id`，不接受模型传入任意数据行。
  CSV 和 XLSX 保留查询明细；Markdown 和 PDF 增加经营结论、洞察、假设、SQL 和注意事项；
  JSON 保留结构化元数据与查询明细。生成器内部验证 UTF-8/JSON/ZIP/XML/PDF 结构，不把整份报表
  重新灌入模型上下文。
- 文件生成并通过格式校验后上传到 `starpartner-biz/public/dev/agent-report-files/`。
  对象按北京时间使用 `YYYYMMDD/uid-traceid-秒级Unix时间戳-artifact_id.扩展名`。本地只保存
  非敏感对象元数据，API 每次读取时重新签发短期下载 URL，不持久化长期有效的公开链接。
- Hermes 原生 `file` toolset 仅保留 `read_file`，且 session-id 内嵌 scope 必须与
  `X-Hermes-Session-Key` 一致；只可读取当前会话导出目录下符合系统命名的文件。
  `write_file`、`patch` 和 `search_files` 从 Agent 工具表移除，并在 handler 层再次阻断。
- FastAPI 在 SSE 结束后发送 `artifact.ready`，前端使用同一 `X-XPD-Session-Key` 鉴权下载。
  列表、下载和删除均先校验 session owner；同一 session 一次只允许一个分析请求，避免并发轮次
  相互认领附件。
- 导出目录权限为 `0700`、文件为 `0600`，并限制单文件、单 session、单 owner、全局容量和
  最低磁盘剩余空间。默认保留 30 天，删除 session 时立即删除对应附件。

## 会话所有权

一期无账号系统，浏览器生成高熵 `X-XPD-Session-Key`。FastAPI 使用服务端签名密钥计算不可逆
owner scope，并把它编码到服务端生成的 session-id 中。历史、消息和会话操作先校验 scope；
不匹配统一返回 404，避免通过 session-id 探测其他会话。

传给 Hermes 的 `X-Hermes-Session-Key` 是 owner scope，不是浏览器原始键。该方案用于一期
本地/单用户部署；接入账号系统后应以认证 user-id 替代本地键。

## 交互式澄清

- API Server 开放 Hermes 原生 `clarify` toolset。只有歧义会实质改变 SQL、指标口径、数据
  粒度或结论时才允许提问；低风险歧义使用合理默认值并在查询假设中声明。
- `hermes_clarify` 运行时补丁只为 Session SSE 且同时具备 session-id 和 owner scope 的请求设置
  `clarify_callback`；其他 API transport 会从该次 Agent 的可用工具中移除 `clarify`，避免暴露一个
  无法向调用方送达问题的工具。回调通过 Session SSE 发送 `_xpd_clarify` 的 `tool.started`，同步
  等待回答，随后发送状态为 `answered` 或 `expired` 的 `tool.completed`。
- 浏览器将回答提交给 FastAPI 的
  `POST /api/sessions/{session_id}/clarifications/{clarification_id}/answer`。FastAPI 先校验本地
  session-id owner scope，再携带不可逆的 `X-Hermes-Session-Key` 转发给 Hermes；Hermes 同时
  校验 Bearer Token、session-id 和 owner scope，任一所有权不匹配均返回 404。
- 待回答问题保存在 Hermes 进程内的线程安全注册表，默认超时 300 秒，答案最长 2000 字符。
  因此澄清期间不能重启 Hermes；一期只运行一个 Hermes 实例。多实例部署必须将流式请求与
  回答请求粘滞到同一实例，或把待回答状态迁移到带原子状态转换的共享存储。
- Session SSE 断开或同一归属的 session-id 发起新流式请求时，旧待回答问题会立即过期并中断
  原 Agent，避免刷新页面后遗留一个继续占用执行线程的孤儿澄清任务。
- 补丁同时让 Session SSE 复用 Hermes 的 `gateway.api_server.max_concurrent_runs` 限流（Hermes
  默认值为 10），限制等待澄清所占用的执行线程数量；达到上限时新请求返回 429。
- `db-multitable-query` Skill 只在实质性歧义会改变查询结论时使用 `clarify`；用户回答后从
  `db_schema_search` 恢复查询决策流程。超时会设置 Agent 中断状态并结束本轮，硬性阻止同一批次
  及后续迭代执行依赖该答案的数据库工具。
- 非流式持久化 Run 不安装进程内 `clarify` 回调。它在调用数据库工具前通过受控 envelope 将问题
  持久化为 `waiting_input`，立即释放 worker 和并发槽；中台调用
  `POST /api/v1/agent/runs/{run_id}/input` 后，同一 `run_id` 从已保存回答继续执行。回答接口按 owner
  隔离并要求独立 `Idempotency-Key`，服务重启不会自动重放等待中的任务。

## 反思与长期记忆

- `memory.nudge_interval=3` 复用 Hermes 原生后台 memory review。
- 会话关闭时 FastAPI 创建幂等最终反思任务，状态持久化到
  `~/.hermes/xpd-report-agent/reflections.json`，失败最多重试 3 次。
- 最终反思默认最多运行 180 秒，可通过 `XPD_FINAL_REFLECTION_TIMEOUT_SECONDS` 调整；超时会
  释放共享 Agent 槽位，不阻塞后续用户分析。
- 最终反思将脱敏后的会话交给临时 Hermes Session，系统提示明确限制其只形成结构化结论，
  并仅通过原生 `memory` 工具写入高置信长期记忆；临时 Session 完成后删除。
- 30 分钟无活动的会话由服务端扫描关闭并触发同一结束反思流程。
- 历史消息响应将 Hermes 的 `reasoning_content` 归一化为单一 `reasoning` 字段；前端与最终
  答案分区、默认折叠展示。流式响应先消费 `_thinking` 预览，再用 `run.completed.messages`
  中的完整思考替换。历史恢复时按用户轮次合并工具调用产生的多个 Assistant 思考片段，每轮
  只渲染一个折叠窗口。按当前产品要求，该字段原样返回，不做脱敏或内容过滤。
- FastAPI 的 `GET /api/memories` 只读返回当前身份作用域的记忆文件快照、容量和更新时间。
  本地模式读取根目录 `MEMORY.md`、`USER.md`；`user_id` 模式读取当前用户目录，并额外展示
  只读注入的 `merchant/MEMORY.md`。前端“Agent 记忆”页面只用于观察。
- FastAPI 系统提示词统一约束新生成的 reasoning、thinking、工具调用说明和最终回答使用简体中文；
  数据库技术标识保持原文。`db-multitable-query` Skill 只维护领域口径与查询决策流程。
- Hermes Session SSE 原生未连接 provider 的 `reasoning_callback`。启动脚本通过项目内兼容补丁
  将 `reasoning_content` 增量桥接为 `_thinking` 事件，使前端能在正式答案前实时展示思考；
  答案开始后的迟到 `_thinking` 副本会被忽略，`run.completed` 仍负责落定完整思考。

## 部署持久化

服务器或容器部署必须持久化完整 Hermes Home，至少包括：

- `state.db`：Session 和消息历史；
- `memories/MEMORY.md`、`memories/USER.md`：本地模式跨会话长期记忆；
- `memories/users/<owner_scope>/MEMORY.md`、`USER.md`：中台用户个人长期记忆；
- `memories/merchant/MEMORY.md`：中台商家公共经营规则，只读注入；
- `xpd-report-agent/reflections.json`：最终反思任务与审计状态；
- `xpd-report-agent/schedules.json`：定时配置、owner 映射和运行状态；
- `cron/jobs.json`、`cron/executions.db`：Hermes 原生任务与执行审计；
- `XPD_FILE_STORAGE_PATH`：CSV/XLSX/Markdown/PDF/JSON 导出文件（默认项目内 `data/report-files`）；
- Hermes 配置、插件和 Skill。

一期建议只运行一个 Hermes/FastAPI 实例。多实例部署前需要将 Session、反思队列和长期记忆
迁移到支持并发的一致共享存储，不能让多个实例各自维护不同的本地文件。

## 代码边界

- `src/xpd_report_agent/api/`：HTTP API、系统提示词和静态前端。
- `src/xpd_report_agent/hermes_plugin/db_query/`：独立 Hermes 插件源文件。
- `src/xpd_report_agent/runtime/`：配置归一化、Hermes 配置写入和进程生命周期。
- `scripts/`：面向开发者和运维的薄命令入口。
- `skills/`：安装给 Hermes 的数据库查询 Skill。

## 配置模型

配置优先级从高到低为：

1. 进程环境变量；
2. `configs/local.env`；
3. 兼容历史项目的根目录 `.env`；
4. 代码中的本地开发默认值。

`configs/local.env`、根目录 `.env`、日志和 PID 均不进入版本控制。

依赖安装只在显式执行 `prepare`，或显式将 `HERMES_BOOTSTRAP_ON_START` 设置为
`true` 使 `run` 在启动前执行完整 `prepare` 时发生。未启用 bootstrap 的 `run` 仍会
同步项目内的插件与 Skill，并将当前运行配置写入 Hermes。

## Hermes 插件安装

插件源文件保留 `plugin.yaml` 和相对导入结构。`prepare` 将其复制到
`~/.hermes/plugins/db-query/`，将 Skill 复制到 `~/.hermes/skills/`，并使用 `uv`
向 Hermes Python 环境安装锁定的插件依赖。

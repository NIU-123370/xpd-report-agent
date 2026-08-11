# xpd-report-agent 系统架构

## 组件关系

```text
中台后端（Bearer 服务凭证 + X-User-Id）/本地静态调试页
  -> FastAPI Wrapper（单副本；服务鉴权、owner 校验、幂等 Run、可配并发上限）
      -> 持久粘性路由：owner_scope -> Hermes node
      -> Hermes 实例池（容器/Pod 内网 8642）
          -> db-query Plugin -> MySQL/RDS -> 当前实例的短期查询结果注册表
                             -> report_file -> CSV / XLSX / Markdown / PDF / JSON -> OSS
          -> instances/<node-id>/state.db -> 该实例承载的会话历史

共享持久化根目录
  -> xpd-report-agent/hermes-routes.json -> owner/session 与实例映射
  -> memories/                          -> 商家只读记忆与用户长期记忆
  -> xpd-report-agent/                  -> Agent Run、反思与定时映射状态
  -> report-files/                      -> 按 session-id 隔离的导出文件
```

FastAPI 不直接生成或执行 SQL，也不建立第二套消息数据库。数据库理解、JOIN 路径发现、SQL
校验和执行由 Hermes Plugin 完成；原始会话消息以所属 Hermes 实例的 `state.db` 为
唯一事实源。

Session SSE、本地同步兼容接口和中台 Run 复用同一套 turn 路径、payload、系统提示词和提交前
上下文准备。中台 Run 额外用持久化状态机提供幂等、重试与
`pending/running/waiting_input/succeeded/failed`；旧的无 Session `/api/chat*` 仅保留为已弃用的
迁移入口。

## 部署拓扑与粘性路由

- ECS/Docker Compose 固定运行 `1 个 FastAPI + 3 个 Hermes`；ACK 固定运行
  1 个 FastAPI Pod，Hermes StatefulSet 从 2 个 Pod 按需扩容到最多 10 个。
  HPA 自动缩容已禁用。当前项目没有 drain、实例状态迁移和 owner 路由原子迁移
  控制器；在这套工具实现并通过验收前，禁止人工降低副本数或永久移除已承载用户的
  Hermes ordinal。同名 Pod 故障重建仍复用原实例目录，不属于路由迁移。
- 只有 FastAPI `8000` 对调用方开放。Hermes `8642` 仅存在于 Compose 或 Kubernetes
  内部网络；ACK NetworkPolicy 只允许 FastAPI Pod 访问。
- 生产 `user_id` 模式下，FastAPI 对 `X-User-Id` 做服务端签名，生成 20 位十六进制
  `owner_scope`。路由单位是 owner，不是独立 session：同一用户的所有 session 绑定
  到同一 Hermes，一个 Hermes 可同时承载多个用户。
- 新 owner 先在健康节点中选择“已绑定 owner 数量最少”的实例，数量相同时再用
  稳定哈希决定。这不是实时负载调度：已绑定用户不会因为其当前任务较重而迁移。
- 每个 Hermes 使用独立 `HERMES_HOME=instances/<node-id>`，不共享 Gateway 锁、PID、
  `state.db` 和本地任务状态。所有角色通过 `XPD_MEMORY_ROOT` 读写共享的用户长期记忆，
  报告文件也使用共享报告卷。
- `XPD_HERMES_ROUTE_REBINDING_ENABLED=false` 是当前安全默认。已绑定节点不健康或
  不再可发现时，该 owner 请求返回可重试的 `503 HERMES_POOL_UNAVAILABLE`，不会静默改绑
  到没有会话历史的其他实例。
- 多节点环境的 FastAPI Agent 并发上限默认为 20，单节点默认为 3，均可配置。
  该上限是 FastAPI 进程级任务槽，不等于实际可承载用户数；正式容量必须经压测确认。

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

- 多 Hermes 部署中仅允许一个 Cron leader 运行原生 ticker：Compose 固定为
  `hermes-1`，ACK 固定为 `hermes-0`。其他 worker 通过 no-op scheduler 禁止恢复、扫描和
  触发任务，避免多 Gateway 重复执行。
- leader 不可用时定时任务暂停，不自动切换到 worker。恢复或迁移 leader 状态前
  不得同时启动第二个 ticker。
- 前端只调用 owner-scoped `/api/schedules`，不会接触 Hermes 全局 `/api/jobs` Bearer 或其他
  用户的任务。映射持久化为 `schedule_id -> native_job_id -> owner_scope/report_type`。
- Hermes 原生 Cron 是唯一计时器，使用 `jobs.json`、`executions.db`、文件锁和每 60 秒 ticker。
  前端输入被转换为 ISO 单次时间或 5 段 Cron，统一使用 `Asia/Shanghai`，精确到分钟。
- 原生 Cron 强制使用 `cron_*` Session，而查询结果与附件安全模型只接受 `xpd_<scope>_*`。
  因此原生任务使用受限 `no_agent` 回调：到点后调用 FastAPI 内部接口，由后端创建真正的
  `xpd_*_scheduled_*` Session，再走现有 Agent 查询与导出链路。
- 回调脚本位于共享根目录 `<XPD_HERMES_SHARED_HOME>/scripts/`，权限为 `0600`，携带每任务随机 capability token；
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
- 商家版 XLSX 按已确认的分析类型构建：简单查询为“经营摘要、数据明细、
  口径与提示”；对比分析增加“趋势与对比”；诊断分析在此基础上再增加“异常与建议”。
  XLSX 不展示 SQL、数据表名、原始字段名或查询审计信息。
- 趋势图使用可解析日期的升序辅助区间，保证小日期在左、大日期在右；图表锚定在
  所有可见对比/趋势表格之后，不与数据区重叠。
- 文件生成并通过格式校验后上传到由 `XPD_REPORT_OSS_BUCKET` 和
  `XPD_REPORT_OSS_PREFIX` 配置的 OSS 位置。
  对象按北京时间使用 `YYYYMMDD/uid-traceid-秒级Unix时间戳.扩展名`。本地只保存
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

本地调试默认使用 `session_key` 模式：浏览器生成高熵 `X-XPD-Session-Key`。
生产中台使用 `user_id` 模式：已鉴权的中台后端传入稳定 `X-User-Id`。FastAPI
在两种模式下都使用服务端签名密钥计算不可逆 `owner_scope`，并把它编码到服务端
生成的 session-id 中。历史、消息、会话操作和文件先校验 scope；不匹配统一返回
404，避免通过 session-id 探测其他会话。传给 Hermes 的 `X-Hermes-Session-Key` 是
`owner_scope`，不是浏览器键或原始 user-id。

Hermes 原生 `session_search` 尚不具备 owner scope 过滤，因此只在本地 `session_key`
模式开启。生产 `user_id` 模式默认从工具面移除，会话列表由 FastAPI 按已鉴权
scope 查询当前粘性 Hermes；多用户部署不得开启不安全的兼容开关。

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
  当前多实例部署通过 owner 粘性路由保证 Session SSE 与回答请求进入同一实例。
  交互式澄清期间仍不能重启所属 Hermes；实例不可用时不会改绑到其他节点。
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
- 会话关闭时 FastAPI 创建幂等最终反思任务。单实例默认持久化到
  `<HERMES_HOME>/xpd-report-agent/reflections.json`；多实例持久化到
  `<XPD_HERMES_SHARED_HOME>/xpd-report-agent/reflections.json`，失败最多重试 3 次。
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

单实例 systemd/本地部署必须持久化完整 `HERMES_HOME` 和
`XPD_FILE_STORAGE_PATH`。多实例 Compose/ACK 部署必须区分三类状态：

1. 共享根目录 `XPD_HERMES_SHARED_HOME`：
   - `xpd-report-agent/hermes-routes.json`：持久的 owner/session 粘性路由；
   - `xpd-report-agent/agent-runs.json`、`reflections.json`、`schedules.json`：中台 Run、反思和定时映射状态；
   - `memories/users/<owner_scope>/MEMORY.md`、`USER.md`：中台用户长期记忆；
   - `memories/merchant/MEMORY.md`：所有用户可读、Agent 不可写的商家公共经营规则；
   - `memories/MEMORY.md`、`USER.md`：本地 `session_key` 模式记忆；
   - `scripts/`：需要时由 Cron leader 调用的内部回调脚本。
2. 实例目录 `instances/<node-id>/`：每个 Hermes 独立保存 `state.db`、Gateway 运行状态、
   配置、插件和 Skill。仅 Cron leader 的实例目录保存有效的 `cron/jobs.json` 与
   `cron/executions.db`。
3. 报告目录 `XPD_FILE_STORAGE_PATH`：保存 CSV/XLSX/Markdown/PDF/JSON 导出文件和
   OSS 对象元数据，供 FastAPI 和全部 Hermes 共享。

多个 Gateway 绝对不能使用同一个 `HERMES_HOME`，否则 PID、运行锁和 SQLite 会互相冲突。
当前 Compose 使用单机 Docker local volume；ACK 使用单工作节点的 RWO 云盘，不提供跨节点高可用。
不得为了跨节点共享 SQLite 而将状态目录改成 NFS/NAS/RWX 盘。备份和迁移必须同时覆盖
共享根目录、全部实例目录、报告目录与路由映射，并在所有写入进程停止时执行。

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

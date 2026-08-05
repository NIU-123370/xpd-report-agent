# xpd-report-agent

`xpd-report-agent` 验证 Hermes Agent 通过自定义 `db-query` 插件查询 MySQL
淘宝直播报表数据库的完整链路，并提供 FastAPI Wrapper 和静态聊天页面。

数据库问题遵循以下工具链路：

```text
[仅在实质性歧义时 clarify]
-> 用户回答后
db_schema_search
-> db_get_table_profile（默认 include_samples=false）
[仅在表语义仍不明确时：include_samples=true]
[仅在多表查询时：db_get_join_paths]
-> db_validate_sql
-> db_execute_sql
[同一请求还要求导出时：capture_for_export=true，提高结果行上限]
-> export_report_file（CSV / XLSX / Markdown / PDF / JSON）
[后续仅要求转换格式时：直接复用上一轮 result_id，不重新查询]
[仅在检索失败或 Schema 诊断时：db_get_schema_ddl]
```

## 开发环境

项目要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run pytest
uv run ruff check .
```

## 配置

```bash
cp configs/local.env.example configs/local.env
```

编辑 `configs/local.env`，设置 `HERMES_LLM_API_KEY` 和 `MYSQL_*` 连接参数。进程环境变量优先于配置
文件；根目录 `.env` 仅作为历史项目兼容入口。

`HERMES_GATEWAY_API_KEY` 是本地 Hermes Gateway 的 bearer token，
`HERMES_LLM_API_KEY` 是模型供应商密钥，两者用途不同。真实密钥不得提交。

会话与记忆默认开启：

- Hermes API Server 暴露 `db_query`、`report_file`、`file`、`session_search`、`memory`、
  `clarify`。
- 浏览器使用本地随机 session-key，FastAPI 只向 Hermes 转发不可逆的作用域标识。
- Hermes `state.db` 是会话消息事实源，前端请求不再重复发送完整历史。
- 每 3 轮由 Hermes 原生后台复盘；结束会话时由持久化任务补做最终复盘。
- `MEMORY.md` 和 `USER.md` 的默认上限分别为 2200 和 1375 字符。
- 只有歧义会改变 SQL 或业务口径时才弹出澄清卡片；回答经 session-id 与 owner scope 双重
  校验后唤醒原请求，默认等待 300 秒。
- 普通分析不生成文件，但会在受限内存中保留一小时的会话级查询快照引用。只有用户明确要求
  导出或下载 CSV、Excel/XLSX、Markdown、PDF 或 JSON 时才生成文件；后续纯导出请求直接复用
  最近的 `result_id`，不会重新执行 Schema 检索或 SQL。
- 数据库工具动态发现当前 MySQL 数据库中的全部基础表；三张直播报表表保留更完整的业务口径，
  其他表通过字段、索引、外键和样例数据确认语义后同样可以只读查询。

面向中台微服务的默认配置已关闭定时报告和 Hermes Cron，健康检查会把“主动关闭”视为正常，
不会阻止普通分析流量。历史实现仍保留，只有显式开启 `XPD_SCHEDULES_ENABLED` 和 Cron 补丁时
才会运行。

当前三张测试表没有 `brand/brand_name` 或可关联的商品品牌维表，因此经营日报可以启用，品牌表现
报告会在界面明确显示“缺少品牌维度”并阻止创建，避免从商品标题猜测品牌。补充带 `item_id` 和
明确品牌字段的维表后，该能力会自动变为可用。

侧边栏还提供三个固定口径的经营分析入口：

- “退款诊断”基于支付与退款聚合字段定位高退款金额、高退款率商品或场次以及异常线索；当前表
  没有退款原因、售后或物流字段，因此不会输出未经数据支持的根因。
- “商品排行”可按成交金额、订单数、件数、退款金额或加权退款率生成 TopN 商品榜单。
- “复购分析”需要稳定的买家标识和订单明细。当前三张表只有聚合买家数，缺少 `buyer_id` 和
  `order_id`，因此入口会展示但保持“数据未就绪”，禁止从聚合数字推算复购率。

预设分析仍复用同一条 Hermes Session 流式链路，完整执行 Schema 检查、SQL 校验和只读查询；
普通分析不会自动生成文件，用户明确要求导出时才走现有文件导出流程。

生产环境必须单独设置高熵 `XPD_SESSION_SIGNING_SECRET`，并将整个 Hermes Home
（默认 `~/.hermes`）挂载到持久卷；不要只持久化项目目录。

## 启动

先准备 Hermes 插件、Skill、依赖和配置：

```bash
scripts/services/hermes.sh prepare
```

统一管理 Hermes Gateway 和 FastAPI：

```bash
scripts/launch.sh start
scripts/launch.sh status
scripts/launch.sh restart
scripts/launch.sh stop
```

也可以只管理单个服务：

```bash
scripts/launch.sh start hermes
scripts/launch.sh start fastapi
```

启动完成后打开 `http://127.0.0.1:8000/`。

页面侧边栏提供历史记录，可新建、打开、重命名、结束和删除会话。已结束会话只读；刷新页面后
会恢复当前活跃会话及其 Hermes 持久化消息。“Agent 记忆”入口会原样展示当前
记忆文件内容、字符容量与最后更新时间，供测试观察，不提供网页编辑。本地模式显示根目录的
两个文件；`user_id` 模式显示商家共享只读记忆和当前用户的两份个人记忆。
明确要求导出后，Assistant 回答下方会流式出现文件卡片，可下载 CSV、真实 XLSX、Markdown、PDF 或 JSON
经营报告；历史会话也会恢复其已生成文件。本期不提供文件上传。
Assistant 消息中的“模型思考”在生成期间自动展开、连续流式更新并跟随内容滚动到底部，文本
填满当前行后自然换行；用户可随时收起。正式答案开始输出以及整轮结束后，思考窗口和完整内容
仍会保留。历史会话恢复时默认折叠，可点击展开。
思考内容原样展示，不做脱敏或摘要；同一轮中的多个片段会汇总进一个窗口，只展示该轮最后的
正式答案。

“定时报告”入口默认隐藏，相关 API 默认返回功能已关闭。

系统提示词统一要求新生成的模型思考、工具说明及最终回答使用简体中文；工具名、表名、字段名
和 SQL 关键字仍保持原文。`db-multitable-query` Skill 只维护领域口径与查询决策流程，避免与系统
提示词重复。已有历史思考不会被自动翻译。

主要 Session API：

```text
POST   /api/sessions
GET    /api/sessions
GET    /api/sessions/{session_id}/messages
GET    /api/sessions/{session_id}/artifacts
GET    /api/sessions/{session_id}/artifacts/{artifact_id}/download
POST   /api/sessions/{session_id}/chat/stream
POST   /api/sessions/{session_id}/clarifications/{clarification_id}/answer
POST   /api/sessions/{session_id}/runs
GET    /api/runs/{run_id}
POST   /api/runs/{run_id}/input
POST   /api/sessions/{session_id}/close
DELETE /api/sessions/{session_id}
GET    /api/memories
GET    /api/analysis-presets
POST   /api/sessions/{session_id}/analyses
GET    /api/schedules
POST   /api/schedules
PUT    /api/schedules/{schedule_id}
POST   /api/schedules/{schedule_id}/pause
POST   /api/schedules/{schedule_id}/resume
POST   /api/schedules/{schedule_id}/run
DELETE /api/schedules/{schedule_id}
```

身份模式由 `XPD_IDENTITY_MODE` 控制，默认是 `session_key`，因此当前本地客户端调用这些接口时
必须携带 `X-XPD-Session-Key`；现有前端会自动生成和保存该键，不需要改变本地测试方式。

接入直播中台时，将 `XPD_IDENTITY_MODE` 切换为 `user_id`，中台后端应在完成登录认证后，通过
`X-User-Id` 请求头传递稳定且唯一的用户标识。Agent 会对该标识执行服务端签名，再用于隔离会话、
个人记忆、文件和定时任务等 owner 范围资源；原始 `user_id` 不会写入 session-id、记忆目录或
传给 Hermes。`X-User-Id` 只能信任来自已鉴权的中台后端，部署时还应使用内网、API 网关或
服务间密钥阻止客户端直接伪造。

Hermes 原生 `session_search` 目前没有 owner scope 过滤，因此本地 `session_key` 模式保持开启，
`user_id` 模式默认不暴露该工具。`XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED=true` 仅作为
已知风险的单用户兼容开关，多用户中台部署不得开启。长期记忆 `memory` 仍按用户目录隔离。

生产中台调用需启用 `XPD_SERVICE_AUTH_ENABLED=true`，并使用独立
`XPD_SERVICE_API_KEY` 作为 Bearer 凭证。生产 systemd 单元会强制启用鉴权。
DMS/RDS 账号和权限由云端管理；应用只使用标准 MySQL 连接参数，不会自动创建、修改或强制检查云数据库账号。

```text
<HERMES_HOME>/memories/
├── MEMORY.md                         # 当前本地模式
├── USER.md                           # 当前本地模式
├── merchant/MEMORY.md                # 中台模式，商家公共经营规则，只读注入
└── users/<owner_scope>/
    ├── MEMORY.md                     # 当前用户的个人反思记忆
    └── USER.md                       # 当前用户的画像和偏好
```

个人反思只能修改个人目录，不能修改 `merchant/MEMORY.md`。切换身份模式不会自动复制或合并
根目录旧记忆，避免把本地测试画像泄露给中台员工；需要共享的稳定经营规则应人工筛选后写入商家
文件。生产环境必须长期固定 `XPD_SESSION_SIGNING_SECRET`，否则同一用户会映射到新的个人目录。

中台应使用 `/api/v1/agent/runs` 这组有身份作用域的持久化接口；其状态机为
`pending/running/waiting_input/succeeded/failed`，澄清回答提交到
`POST /api/v1/agent/runs/{run_id}/input`。本地网页继续使用 Session SSE 作为交互式 transport。
Session SSE、同步 Session 兼容入口和持久化 Run 共用同一套 turn payload、提示词组装与提交准备
内核；同步 Session chat 已标记为 deprecated。
`/api/chat` 与 `/api/chat/stream` 仅为迁移兼容保留，已标记为 deprecated；它们不管理
session，也不参与 `user_id` 身份隔离。切换到 `user_id` 模式后，
本地随机 session-key 创建的旧历史不会自动归到任何中台用户，这是预期的安全隔离行为。

定时配置和运行映射默认保存在 `~/.hermes/xpd-report-agent/schedules.json`，Hermes 原生任务保存在
`~/.hermes/cron/jobs.json`。生产环境应将它们与 `state.db` 和报表目录一起持久化；可通过
`XPD_SCHEDULE_STATE_PATH`、`XPD_CRON_SCRIPT_DIR` 配置路径。Hermes 与 FastAPI 分容器时，还需
把 `XPD_CRON_CALLBACK_ORIGIN` 设置为仅内部可达的 FastAPI 地址。

导出文件先安全生成在 `data/report-files/`，配置 OSS 凭据后会上传至
`oss://starpartner-biz/public/dev/agent-report-files/`，接口返回默认有效一小时的签名下载 URL。
对象按北京时间建立 `YYYYMMDD` 日目录，文件名为
`uid-traceid-秒级Unix时间戳.扩展名`；中台的 `X-User-Id` 和 `X-Request-Id` 分别作为
`uid` 和 `traceid`。本地网页没有真实 UID 时，使用稳定的用户范围标识。
服务器上仍应通过 `XPD_FILE_STORAGE_PATH` 指向临时或持久目录，并为 OSS 配置对象生命周期。
PDF 会嵌入中文字体；macOS 会自动寻找系统字体，Linux 生产环境应安装可嵌入的中文
TrueType 字体并配置 `XPD_PDF_FONT_PATH`。
默认限制为单文件 10 MiB、单会话 50 个/100 MiB、单 owner 500 MiB、全局 5 GiB，并保留至少
256 MiB 磁盘空间；超过 30 天的导出文件会在下次导出时清理。删除会话也会删除其文件。这些值
均可在 `configs/local.env` 中调整。

## 目录

- `src/xpd_report_agent/`：应用、MySQL-only Hermes 插件和运行时代码。
- `configs/`：可提交的配置模板；`local.env` 仅供本地使用。
- `scripts/`：数据库工具和服务管理入口。
- `skills/`：Hermes Skill。
- `tests/`：自动化测试。
- `docs/prds/`：产品需求文档。
- `docs/plans/`：设计和实施计划。
- `docs/archs/`：系统架构和代码说明。

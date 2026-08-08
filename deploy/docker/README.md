# Docker 部署指南（阿里云 ECS / Linux）

本文用于把 `xpd-report-agent` 部署为中台可调用的 AI 微服务。部署采用单容器模式：

```text
Docker 容器 xpd-report-agent
├── FastAPI            0.0.0.0:8000（中台调用）
├── Hermes Gateway     127.0.0.1:8642（仅容器内部）
├── db-query           RDS 只读查询
└── report-file        Excel/PDF 等报告并上传 OSS
```

数据库和 OSS 不部署在容器内，分别连接阿里云 RDS 和 OSS。

## 1. 部署前检查

- ECS 已安装 Docker Engine 和 Docker Compose。
- ECS 与 RDS 位于可互通的 VPC，使用 RDS 内网地址。
- RDS 白名单或安全组允许 ECS 私网地址访问 `3306`。
- RDS 使用 MySQL 5.7.8 或更高版本，推荐 MySQL 8.0。
- 数据库账号对目标库和业务表至少有 `SELECT` 权限，不要使用 root 账号。
- ECS 安全组只向中台来源开放 `8000`，不要将 `8642` 暴露到公网。

确认版本：

```bash
docker --version
docker compose version
```

## 2. 放置项目

推荐目录：

```text
/opt/xpd-report-agent/
```

进入部署目录：

```bash
cd /opt/xpd-report-agent/deploy/docker
```

应至少存在：

```text
Dockerfile
compose.yaml
entrypoint.sh
xpd-report-agent.env.example
```

## 3. 配置环境变量

创建只保存在服务器上的配置文件：

```bash
cp xpd-report-agent.env.example xpd-report-agent.env
chmod 600 xpd-report-agent.env
```

编辑 `xpd-report-agent.env`，填写以下配置：

- 模型：`HERMES_LLM_PROVIDER`、`HERMES_LLM_MODEL`、`HERMES_LLM_BASE_URL`、
  `HERMES_LLM_API_KEY`。
- RDS：`XPD_DB_HOST`、`XPD_DB_PORT`、`XPD_DB_NAME`、`XPD_DB_USERNAME`、
  `XPD_DB_PASSWORD`；`XPD_MYSQL_QUERY_TIMEOUT_MS` 可设置单条 Agent 查询的服务端执行上限。
- 中台鉴权：`XPD_SERVICE_API_KEY`。
- 会话签名：`XPD_SESSION_SIGNING_SECRET`，生产环境必须长期固定且与服务密钥不同。
- OSS：报告文件所需的 endpoint、bucket、prefix 和访问凭证。

真实 `xpd-report-agent.env` 已被 Git 和 Docker 构建上下文忽略，禁止提交到仓库、日志或接口文档。
如果密钥曾出现在聊天、工单或终端录屏中，应及时轮换。

## 4. 验证 RDS 内网

只测试网络端口，不打印账号和密码：

```bash
RDS_HOST="$(sed -n 's/^XPD_DB_HOST=//p' xpd-report-agent.env)"
RDS_PORT="$(sed -n 's/^XPD_DB_PORT=//p' xpd-report-agent.env)"
timeout 5 bash -c "</dev/tcp/$RDS_HOST/${RDS_PORT:-3306}" \
  && echo 'RDS 内网端口可达' \
  || echo 'RDS 内网端口不可达'
```

端口不通时依次检查 VPC、RDS 白名单/安全组、内网域名和端口。ECS 通常不需要开放入方向
`3306`，因为容器是主动连接 RDS。

## 5. 构建镜像

Dockerfile 已使用适合阿里云网络的公共 ECR Python 基础镜像、阿里云 Debian 镜像和 Python
包镜像。首次构建仍需下载固定版本的 Hermes Runtime 和依赖，时间明显长于后续构建。

前台构建：

```bash
docker compose build --progress=plain
```

远程终端可能断开时，使用后台构建：

```bash
nohup docker compose build --progress=plain \
  > /opt/xpd-report-agent-build.log 2>&1 &
tail -f /opt/xpd-report-agent-build.log
```

按 `Ctrl+C` 只会退出日志查看，不会终止后台构建。成功结尾应包含：

```text
Image xpd-report-agent:dev Built
```

验证镜像中的 Hermes：

```bash
docker run --rm --entrypoint sh xpd-report-agent:dev \
  -c '/var/lib/xpd-report-agent/.hermes/hermes-agent/venv/bin/hermes --version'
```

## 6. 启动服务

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100
```

启动初期显示 `health: starting` 属于正常现象。只有状态变为 `healthy`，才算部署完成：

```text
Up ... (healthy)
```

## 7. 就绪验收

```bash
curl -sS http://127.0.0.1:8000/health
echo
curl -sS http://127.0.0.1:8000/ready
echo
```

`/ready` 必须返回 HTTP `200`，并满足：

```json
{
  "ok": true,
  "status": "ready",
  "checks": {
    "runtime": true,
    "mysql": true
  }
}
```

`runtime=true` 表示 Agent 运行时可用，`mysql=true` 表示应用已实际连接 RDS，而不只是容器进程
存活。`/health` 是运维诊断接口，中台业务方只需要使用 `/ready`。

## 8. 真实业务接口测试

服务密钥从容器环境读取，不在终端输出：

```bash
XPD_TEST_KEY="$(docker exec xpd-report-agent printenv XPD_SERVICE_API_KEY)"
XPD_TEST_REQUEST_ID="$(cat /proc/sys/kernel/random/uuid)"
```

创建一个实际读取 RDS Schema 的任务：

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v1/agent/runs' \
  -H "Authorization: Bearer $XPD_TEST_KEY" \
  -H 'X-User-Id: deployment_test_user' \
  -H "Idempotency-Key: $XPD_TEST_REQUEST_ID" \
  -H 'Content-Type: application/json' \
  --data '{
    "message":"请通过数据库工具实际查询当前数据库，统计可访问的业务表数量，并列出前5个业务表名。不要根据记忆回答。",
    "session_id":null,
    "title":"RDS真实接口测试"
  }'
```

取得 `run_id` 后连接 SSE：

```bash
curl -N --max-time 300 \
  'http://127.0.0.1:8000/api/v1/agent/runs/<run_id>/stream' \
  -H "Authorization: Bearer $XPD_TEST_KEY" \
  -H 'X-User-Id: deployment_test_user' \
  -H 'Accept: text/event-stream'
```

正常会收到 `progress`、`answer.delta`，最后收到 `run.completed`。完整的 5 个中台接口、澄清续答
和文件下载流程见 [`../../docs/api/middle-platform-agent-api.md`](../../docs/api/middle-platform-agent-api.md)。

接口文档分工：

| 文档 | 使用对象 | 说明 |
|---|---|---|
| [`middle-platform-agent-api.md`](../../docs/api/middle-platform-agent-api.md) | 中台后端 | 正式稳定的 5 个业务接口及接入流程 |
| [`internal-test-api.md`](../../docs/api/internal-test-api.md) | Agent 开发、测试、运维 | 内部会话、网页调试和诊断接口，不提供给中台 |

部署完成后，交付给中台开发者的文档应为 `middle-platform-agent-api.md`，不要把内部测试文档
当作中台契约。

## 9. 中台调用地址

同 VPC 的中台服务使用：

```text
http://<ECS内网IP>:8000
```

中台后端必须携带 `Authorization: Bearer <service-key>` 和稳定的 `X-User-Id`。服务密钥只能保存
在中台后端，不得下发到浏览器。建议在安全组、内部负载均衡或 API 网关层进一步限制来源。

## 10. 日常运维

```bash
docker compose ps
docker compose logs -f --tail=200
docker compose restart
```

查看最近错误：

```bash
docker compose logs --since=10m \
  | grep -E 'ERROR|Traceback|AuthError|RuntimeError'
```

更新代码并重新生成容器前，先确认会话、记忆和文件的持久化策略。`docker compose restart` 会保留
当前容器层；`docker compose down` 会删除容器，不能把容器可写层当作长期数据存储。报告文件由
Compose 命名卷保存，生产环境还应定期验证 OSS 上传和下载。

## 11. 常见问题

### 构建停在下载依赖

使用 `--progress=plain` 查看具体包。不要反复同时启动多个构建进程，可用以下命令确认：

```bash
pgrep -af 'docker compose build'
tail -f /opt/xpd-report-agent-build.log
```

### `Hermes executable was not found`

当前 Dockerfile 会在构建阶段检查 `pyproject.toml` 和 `venv/bin/hermes`，缺失时构建必须失败。
不要把缺少 Hermes 的镜像当作成功镜像启动；查看完整构建日志中最早出现的 Git 或依赖错误。

### `API Server: aiohttp not installed`

正式依赖已包含 `aiohttp==3.14.1`。出现该错误通常说明服务器仍在使用旧镜像，请更新代码并重新
构建，不要长期依赖进入容器手工安装。

### `No usable credentials found for provider 'alibaba-coding-plan'`

确认 `HERMES_LLM_API_KEY` 已填写。启动脚本会把该通用变量映射为 Hermes 需要的
`ALIBABA_CODING_PLAN_API_KEY` 和 `DASHSCOPE_API_KEY`；旧镜像没有该兼容映射，需要重新构建。

### `/ready` 中 `mysql=false`

端口可达不等于账号可用。检查数据库名、账号密码、RDS 白名单及账号的 `SELECT` 权限，并查看：

```bash
docker compose logs --tail=200
```

### SQLite WAL 警告

Hermes 会在检测到受影响的 SQLite 版本时自动使用 `journal_mode=DELETE`。这不会阻止 RDS 查询或
中台接口启动，但应在后续基础镜像升级时同步升级 SQLite/Python Runtime。

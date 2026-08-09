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
  `HERMES_LLM_API_MODE`、`HERMES_LLM_API_KEY`。
- RDS：`XPD_DB_HOST`、`XPD_DB_PORT`、`XPD_DB_NAME`、`XPD_DB_USERNAME`、
  `XPD_DB_PASSWORD`；`XPD_MYSQL_QUERY_TIMEOUT_MS` 可设置单条 Agent 查询的服务端执行上限。
- 中台鉴权：`XPD_SERVICE_API_KEY`。
- 会话签名：`XPD_SESSION_SIGNING_SECRET`，生产环境必须长期固定且与服务密钥不同。
- OSS：报告文件所需的 endpoint、bucket、prefix 和访问凭证。

`HERMES_GATEWAY_API_KEY`、`XPD_SERVICE_API_KEY`、`XPD_SESSION_SIGNING_SECRET` 必须是
三个不同的高熵密钥，每个至少 32 个字符。可以分别执行三次下列命令生成：

```bash
openssl rand -hex 32
```

中台生产配置还必须保持：

```text
XPD_IDENTITY_MODE=user_id
XPD_SERVICE_AUTH_ENABLED=true
XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED=false
FASTAPI_RELOAD=false
```

真实 `xpd-report-agent.env` 已被 Git 和 Docker 构建上下文忽略，禁止提交到仓库、日志或接口文档。
如果密钥曾出现在聊天、工单或终端录屏中，应及时轮换。
容器启动前会执行失败关闭的配置预检；缺少必填项、使用 `REPLACE_WITH_*`
占位符、关闭鉴权或复用密钥时，服务不会带病启动。

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
  -c '/opt/hermes-agent/venv/bin/hermes --version && \
      uv pip check --python /opt/hermes-agent/venv/bin/python'
```

镜像内的 Hermes 固定运行时位于 `/opt/hermes-agent`，不再放进持久化的
`$HOME/.hermes`。这样重建镜像时不会被旧数据卷遮住新运行时。

构建完成后，可在启动前单独检查配置：

```bash
docker compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python xpd-report-agent \
  -m xpd_report_agent.runtime.deployment_preflight
```

### 首次从旧镜像升级

旧镜像把 Hermes 运行时和会话状态混在容器可写层的
`/var/lib/xpd-report-agent/.hermes` 中。首次升级到新状态卷前，如果要保留已有会话、
记忆和任务，必须在新镜像构建完成后、重建容器之前停服备份。不要在 Hermes
仍在写 SQLite/JSON 状态时直接打包。

```bash
umask 077
XPD_STATE_BACKUP_DIR="/opt/xpd-hermes-state-$(date +%Y%m%d%H%M%S)"
install -d -m 700 "$XPD_STATE_BACKUP_DIR"

# 即使 xpd-report-agent:dev 已被新构建覆盖，也从当前容器保留旧镜像。
docker image tag "$(docker inspect -f '{{.Image}}' xpd-report-agent)" \
  xpd-report-agent:rollback-before-state-volume
echo 'xpd-report-agent:rollback-before-state-volume' \
  > /opt/xpd-report-agent-last-rollback-image

curl -fsS http://127.0.0.1:8000/health | python3 -c '
import json, sys
active = json.load(sys.stdin)["agent_runs"]["active_tasks"]
print(f"active_tasks={active}")
raise SystemExit(0 if active == 0 else 1)
'
docker stop --time 60 xpd-report-agent
docker cp xpd-report-agent:/var/lib/xpd-report-agent/.hermes/. \
  "$XPD_STATE_BACKUP_DIR/"
test -n "$(find "$XPD_STATE_BACKUP_DIR" -mindepth 1 -print -quit)" \
  && echo "状态备份完成：$XPD_STATE_BACKUP_DIR" \
  || { echo '状态备份为空，已恢复旧服务，请停止发布' >&2; \
       docker start xpd-report-agent; false; }
```

只有命令输出 `active_tasks=0` 和“状态备份完成”时才能继续。任一检查失败都应立即停止
发布，不要在运行中任务尚未归零时强制备份。

将这份一致性备份恢复到固定名称的状态卷，然后直接启动新容器：

```bash
docker volume create xpd-report-agent-hermes-state
docker run --rm --user root --entrypoint sh \
  -v xpd-report-agent-hermes-state:/target \
  -v "$XPD_STATE_BACKUP_DIR:/source:ro" \
  xpd-report-agent:dev \
  -c 'cp -a /source/. /target/ && \
      chown -R xpd-agent:xpd-agent /target'
docker compose up -d --force-recreate
```

这份首次迁移备份也包含旧 Hermes 目录，主要用于首次发布回滚；新镜像始终使用
`/opt/hermes-agent`，不会执行卷中的旧运行时。备份目录中包含配置和密钥，必须保持
`0700` 权限。如果备份或恢复失败，先执行 `docker start xpd-report-agent` 恢复旧服务，不要继续
重建容器。纯新部署不需要执行本段。

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

`healthy` 是部署的必要条件，但不等于模型推理和 OSS 业务链路已经验收；还必须
完成第 8 节的真实 Agent 查询和文件下载测试。

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

再创建一个明确要求“查询数据并生成 Excel”的任务，验收 OSS 链路。该任务必须同时
满足：

- `run.status=succeeded`，且结果不是空字符串；
- SSE 出现 `artifact.ready`，`run.result.artifacts` 至少有一项；
- 使用第 5 个中台接口实际下载成功，返回的 Excel 可以打开且中文正常。

上述三项全部通过，才说明模型、RDS、报告生成、OSS 上传和下载都已实际工作。

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

## 10. 更新、回滚与日常运维

### 从 Codeup 更新

发布前先确认没有运行中的长任务，仓库没有未处理的服务器本地改动。真实
`xpd-report-agent.env` 是 Git 忽略文件，不会被拉取覆盖。

```bash
cd /opt/xpd-report-agent
git status --short
git fetch origin master
git merge --ff-only origin/master

cd deploy/docker
XPD_ROLLBACK_TAG="xpd-report-agent:rollback-$(date +%Y%m%d%H%M%S)"
docker image tag xpd-report-agent:dev "$XPD_ROLLBACK_TAG"
echo "$XPD_ROLLBACK_TAG" > /opt/xpd-report-agent-last-rollback-image

docker compose build --progress=plain
docker compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python xpd-report-agent \
  -m xpd_report_agent.runtime.deployment_preflight
docker compose up -d --force-recreate
docker compose ps
```

如果 `git status --short` 有输出，先确认改动来源，不要在服务器上盲目执行
`git reset --hard`。`docker compose restart` 只会重启当前容器，不会加载新代码或新镜像；
代码或镜像更新必须使用 `docker compose up -d --force-recreate`。

发布后依次执行第 7 节的就绪检查和第 8 节的真实业务验收。在它们全部通过前，
不要删除回滚镜像和状态备份。

### 回滚镜像

如果新版启动失败或真实业务验收不通过，使用发布前保留的镜像立即回滚：

```bash
cd /opt/xpd-report-agent/deploy/docker
XPD_ROLLBACK_TAG="$(cat /opt/xpd-report-agent-last-rollback-image)"
docker image inspect "$XPD_ROLLBACK_TAG" >/dev/null
docker image tag "$XPD_ROLLBACK_TAG" xpd-report-agent:dev
docker compose up -d --force-recreate --no-build
docker compose ps
curl -fsS http://127.0.0.1:8000/ready
```

首次引入 `hermes-state` 卷时，回滚依赖第 5 节保留的完整状态备份；不要跳过首次迁移。
镜像回滚只恢复运行服务，服务器 Git 工作树仍保留新版代码；确认原因后，通过新的
Codeup 修复提交再次发布，不在生产机上直接改代码。

### 日常命令

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

Hermes 会话、记忆和任务由 `xpd-report-agent-hermes-state` 命名卷保存，报告文件由
`report-files` 卷保存并上传 OSS。`docker compose down` 不会删除命名卷；不要在生产环境
执行 `docker compose down -v`。应定期备份状态卷，并实际验证 OSS 上传和下载。

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

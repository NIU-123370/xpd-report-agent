# systemd 部署

这组单元用于在 Linux 单机上分别监管 Hermes Gateway 和 FastAPI。两个进程异常退出后会由 systemd 自动拉起；主动 `stop` 时使用 `SIGTERM` 优雅退出，45 秒后才强制结束。FastAPI 会等 Hermes 的启动探针通过后再启动。

## 1. 渲染单元模板

`.service.in` 不能直接安装，先把下列占位符替换为服务器实际值：

| 占位符 | 含义 | 示例（仅示意） |
| --- | --- | --- |
| `@PROJECT_ROOT@` | 项目代码的绝对路径 | `/opt/xpd-report-agent` |
| `@SERVICE_USER@` | 专用的非 root 用户 | `xpd-agent` |
| `@SERVICE_GROUP@` | 服务用户组 | `xpd-agent` |
| `@SERVICE_HOME@` | 服务用户 Home，其中包含 `.hermes` | `/var/lib/xpd-report-agent` |
| `@ENV_FILE@` | systemd 环境文件的绝对路径 | `/etc/xpd-report-agent/xpd-report-agent.env` |

渲染后的文件名应分别为：

- `xpd-hermes.service`
- `xpd-fastapi.service`
- `xpd-hermes-prepare.service`

将它们和 `xpd-report-agent.target` 安装到 `/etc/systemd/system/`。安装前可以执行 `systemd-analyze verify <unit files...>` 检查单元语法。

## 2. 准备配置与持久化目录

### 安装锁定的 Hermes Runtime

项目把 Hermes 固定为 `configs/hermes-runtime.lock` 中记录的版本和 Git 提交。不要在服务器上
直接安装 `main` 分支或运行自动升级。以服务用户执行下面的首次安装：

```bash
sudo -u xpd-agent -H bash -lc '
  set -euo pipefail
  mkdir -p "$HOME/.hermes"
  HERMES_AGENT_DIR="$HOME/.hermes/hermes-agent"
  git init "$HERMES_AGENT_DIR"
  git -C "$HERMES_AGENT_DIR" remote add origin \
    https://github.com/NousResearch/hermes-agent.git
  git -C "$HERMES_AGENT_DIR" fetch --depth 1 origin \
    a61183b56fdb45b9d2a0f2f6b8482e665ccf702f
  git -C "$HERMES_AGENT_DIR" checkout --detach FETCH_HEAD
  UV_PROJECT_ENVIRONMENT="$HERMES_AGENT_DIR/venv" uv sync \
    --project "$HERMES_AGENT_DIR" --frozen --no-dev --python 3.11
  uv pip check --python "$HERMES_AGENT_DIR/venv/bin/python"
  "$HOME/.hermes/hermes-agent/venv/bin/hermes" --version
'
```

期望输出包含 `Hermes Agent v0.19.0` 和 `upstream a61183b5`。Hermes 的准备阶段和每次启动
都会核对这两个值；版本或提交不一致时服务会拒绝启动。升级 Hermes 时应先在开发环境验证，
然后只修改锁文件和本段安装提交，并重新执行完整测试。

安装后也可以单独执行只读校验：

```bash
sudo -u xpd-agent -H /opt/xpd-report-agent/scripts/services/hermes.sh verify
```

复制 `deploy/systemd/xpd-report-agent.env.example` 到上表的 `@ENV_FILE@`，填入真实的
模型、MySQL、OSS 和三个不同的高熵密钥。配置文件建议由 `root` 拥有，服务用户组
可读，权限设为 `0640`，且不要放入 Git。三个 systemd 单元都会在启动前执行
`deployment_preflight`，安全配置不完整时会直接失败，不会启动一半可用的服务。

确保以下位置持久化且对 `@SERVICE_USER@` 可写：

- `@SERVICE_HOME@/.hermes`
- `XPD_FILE_STORAGE_PATH`
- `XPD_SCHEDULE_STATE_PATH` 和 `XPD_REFLECTION_STATE_PATH` 所在目录
- `XPD_AGENT_RUN_STATE_PATH` 所在目录（中台请求幂等与断线恢复状态）

定时报告默认关闭，因此不需要准备 `XPD_CRON_SCRIPT_DIR`。若以后重新开启，
再为该目录和定时状态目录提供持久化写权限。

单元会设置 `LAUNCH_MANAGED=true`，因此生产运行时只读 `EnvironmentFile`，不会意外加载代码目录中的 `.env` 或 `configs/local.env`。
数据库账号与权限由 DMS/RDS 管理员配置，并通过标准 `MYSQL_HOST` / `MYSQL_PORT` /
`MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` 参数提供给应用。建议仅授予报表查询所需的
`SELECT` 权限；数据库版本要求 MySQL 5.7.8 或更高，推荐 MySQL 8.0。应用不负责创建、修改或
强制检查 DMS/RDS 账号。

PDF 导出会将中文字体子集嵌入文件，避免服务器和下载端出现乱码。Linux 服务器需安装
一个可嵌入的中文 TrueType 字体（例如 `fonts-wqy-zenhei`），并在
`XPD_PDF_FONT_PATH` 中填写字体文件的绝对路径。

报表上传位置由 `XPD_REPORT_OSS_BUCKET` 和 `XPD_REPORT_OSS_PREFIX` 决定。OSS 账号只需
目标前缀的上传和读取权限，并建议配置对象生命周期；接口返回有限时效的签名下载 URL。
对象按北京时间使用 `YYYYMMDD/uid-traceid-秒级Unix时间戳.扩展名`。

`user_id` 模式下默认不暴露 Hermes 原生 `session_search`，因为它尚未按 owner scope 过滤。
`XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED=true` 只是单用户兼容开关，不应在多用户中台部署中开启。

## 3. 首次准备与启动

依赖安装、Hermes 配置或项目 Skill/插件变更后，先停止对外服务，再单次执行
准备单元：

```bash
sudo systemctl daemon-reload
sudo systemctl stop xpd-report-agent.target
sudo systemctl start xpd-hermes-prepare.service
sudo systemctl enable --now xpd-report-agent.target
```

`xpd-hermes-prepare.service` 是一次性单元，不要 `enable`。它与运行中的 target、Hermes
和 FastAPI 互斥，避免在 Agent 正在执行任务时修改 Hermes venv。上线更新后必须再启动
target，进程日常重试不会反复安装依赖。准备单元最长允许运行 60 分钟，避免在
网络较慢时中途终止 frozen sync；应通过 `journalctl -u xpd-hermes-prepare.service -f`
观察实际进度。

常用运维命令：

```bash
systemctl status xpd-report-agent.target xpd-hermes.service xpd-fastapi.service
journalctl -u xpd-hermes.service -u xpd-fastapi.service -f
sudo systemctl restart xpd-report-agent.target
sudo systemctl stop xpd-report-agent.target
```

## 4. 健康检查与重试边界

启动探针只确认进程已经接受 HTTP 请求。它以 Python 的 `-S` 隔离模式运行，不加载 Agent 运行时补丁。Hermes 探针会使用 `EnvironmentFile` 中的 API Key，但不会把密钥放在命令行参数中。密钥错误或端口未就绪会使启动失败，systemd 按 `Restart=on-failure` 重试。

FastAPI `/health` 返回值还包含 Hermes、MySQL、记忆、报表 OSS 和定时功能等下游就绪状态，应交给监控系统告警。定时功能关闭时会显示 `enabled=false`，不会使健康检查失败。下游故障不应通过无限重启 FastAPI 来处理；systemd 的职责是在进程退出时拉起它。

健康检查通过只是必要条件。每次正式发布还应使用中台 Run 接口完成一次真实 RDS
查询，再生成并下载一个 Excel，确认模型、数据库、报告生成和 OSS 整条链路都可用。

每个单元在 5 分钟内最多启动 10 次，避免配置错误导致永久重启风暴。触发限制后，修复配置并执行
`systemctl reset-failed xpd-hermes xpd-fastapi` 再启动。

# Launch Design

## 目标

新增 `launch/` 作为本地 demo 的唯一启动入口，统一管理 Hermes gateway 与 FastAPI wrapper 的启动、停止、重启和状态检查。

## 目录与职责

- `launch/launch.sh`：命令行入口，转发到 Python 管理器。
- `launch/launch.py`：进程生命周期管理，负责 PID、日志、健康检查、失败回滚和 `.env` 归一化。
- `launch/serv/hermes.sh`：Hermes 单服务启动脚本，包含 prepare 与 run。
- `launch/serv/fastapi.sh`：FastAPI wrapper 单服务启动脚本。
- `launch/run/`：运行时 PID 文件目录。
- `logs/`：服务日志目录。

## 配置模型

用户侧只配置 `.env` 中的新命名：

- `HERMES_GATEWAY_*`：本地 Hermes gateway 配置。
- `HERMES_LLM_*`：Hermes 调用模型供应商的配置。
- `FASTAPI_*`：FastAPI wrapper 配置。

`launch/launch.py` 会派生 Hermes gateway 需要的内部变量：

- `API_SERVER_HOST <- HERMES_GATEWAY_HOST`
- `API_SERVER_PORT <- HERMES_GATEWAY_PORT`
- `API_SERVER_KEY <- HERMES_GATEWAY_API_KEY`
- `GATEWAY_ALLOW_ALL_USERS <- HERMES_GATEWAY_ALLOW_ALL_USERS`
旧变量 `API_SERVER_*`、`HERMES_API_KEY`、`HERMES_MODEL`、`HERMES_BASE_URL` 不再作为用户配置入口。

## Hermes Config 同步

`launch/serv/hermes.sh prepare` 会调用 `scripts/configure_hermes_demo.py`，将 `.env` 中的 `HERMES_LLM_*` 写入 `~/.hermes/config.yaml` 的 `model` 段，同时继续启用 `db-query` 并限制 API Server 工具面为 `db_query`。

`HERMES_LLM_API_KEY` 为空时不会覆盖已有 `model.api_key`。默认要求最终 config 中存在 `model.api_key`，可用 `HERMES_REQUIRE_LLM_API_KEY=false` 放宽。

## 生命周期语义

- `start`：按 Hermes、FastAPI 顺序启动；服务已由 launch 管理时不重复启动。
- `stop`：按 FastAPI、Hermes 顺序停止；只停止 launch 记录的 PID。
- `restart`：先 stop 后 start。
- `status`：展示 PID 存活状态和健康检查结果。

如果 FastAPI 启动失败，launch 会回滚本次启动的 Hermes，避免半启动状态。

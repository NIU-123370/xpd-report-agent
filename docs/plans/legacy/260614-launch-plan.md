# Launch Implementation Plan

## 实施项

1. 新增 `launch/launch.py` 与 `launch/launch.sh`，实现 `start|stop|restart|status [all|hermes|fastapi]`。
2. 将 `op/run_hermes.sh` 与 `op/bootstrap_hermes.sh` 合并迁移为 `launch/serv/hermes.sh`。
3. 将 `op/run_serv.sh` 迁移为 `launch/serv/fastapi.sh`，launch 模式默认关闭 reload。
4. 更新 `.env.example`，统一使用 `HERMES_GATEWAY_*`、`HERMES_LLM_*`、`FASTAPI_*`。
5. 扩展 `scripts/configure_hermes_demo.py`，同步 Hermes `model` 配置并支持模型 key 校验。
6. 更新 FastAPI wrapper，使其只读取新的 gateway 变量。
7. 更新 README，移除旧 `op/` 主入口说明。

## 验收命令

```bash
bash -n launch/launch.sh launch/serv/hermes.sh launch/serv/fastapi.sh
uv run pytest
./launch/launch.sh start
./launch/launch.sh status
curl http://127.0.0.1:8000/health
./launch/launch.sh restart
./launch/launch.sh stop
```

## 关键场景

- `.env` 只配置新变量时能正常启动。
- 旧变量不再被映射为新变量。
- Hermes 启动失败时 FastAPI 不启动。
- FastAPI 启动失败时回滚本次启动的 Hermes。
- stale PID 能被识别并清理。

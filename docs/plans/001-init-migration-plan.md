# xpd-report-agent 初始化迁移计划

## 迁移策略

采用一次性白名单迁移。历史目录只作为只读来源，不复制运行产物，也不修改历史项目。

## 文件映射

- `app/` -> `src/xpd_report_agent/api/`
- `hermes_plugins/db_query/` -> `src/xpd_report_agent/hermes_plugin/db_query/`
- `launch/launch.py` -> `src/xpd_report_agent/runtime/launcher.py`
- `launch/serv/` -> `scripts/services/`
- 历史 Python 脚本逻辑 -> `src/xpd_report_agent/demo/` 和 `runtime/`
- 命令包装 -> `scripts/`
- `.env.example` -> 清理后的 `configs/local.env.example`
- `requirements.txt` -> `pyproject.toml` 和 `uv.lock`

## 实施步骤

1. 建立 `src` 包结构、配置目录、脚本目录和 `uv` 元数据。
2. 迁移源码、静态资源、测试、Skill 和历史文档。
3. 调整导入路径、资源定位、配置优先级和启动命令。
4. 将 Hermes 准备流程切换到 `uv`，保留显式 `prepare` 能力。
5. 迁移原有测试并补充新目录、配置优先级和 CLI 测试。
6. 生成 `uv.lock`，执行单元测试、静态检查和脚本语法检查。
7. 在具备模型密钥的环境执行 Hermes 端到端冒烟测试。

## 回滚

历史项目保持只读且不被删除，因此迁移失败时可直接丢弃当前仓库中的迁移改动，不影响
历史运行环境。


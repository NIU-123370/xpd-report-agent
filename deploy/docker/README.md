# Docker 部署（Linux / ECS）

该部署以单容器运行 Hermes Gateway 和 FastAPI。仅 FastAPI 的 `8000` 端口对外开放；Hermes
Gateway 只监听容器内部的 `127.0.0.1:8642`。数据库配置兼容 DMS 使用的 `XPD_DB_*` 变量。

## 1. 准备配置

```bash
cd /opt/xpd-report-agent/deploy/docker
cp xpd-report-agent.env.example xpd-report-agent.env
chmod 600 xpd-report-agent.env
```

编辑 `xpd-report-agent.env`，填写模型密钥、数据库密码、中台服务密钥和长期固定的会话签名密钥。
真实 env 文件不会进入 Git，也不会被复制到 Docker 镜像。

## 2. RDS 网络前置条件

- ECS 与 RDS 位于同地域、同 VPC，能够解析并访问 RDS 内网地址。
- RDS 白名单允许 ECS 私网 IP（当前实例为 `10.241.221.126`）或其所属安全组。
- `main_biz_dev` 数据库账号至少拥有目标报表表的 `SELECT` 权限。
- ECS 安全组通常不需要开放入方向 `3306`；容器是主动连接 RDS 的 `3306`。

可在 ECS 上先验证网络（不会打印密码）：

```bash
timeout 5 bash -c '</dev/tcp/rm-2zei84d1ny5bi927t.mysql.rds.aliyuncs.com/3306' \
  && echo 'RDS 3306 可达' || echo 'RDS 3306 不可达'
```

## 3. 构建和启动

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=200
```

首次构建会下载 Python、Hermes Runtime 及依赖，因此耗时会明显长于后续构建。

## 4. 验证

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/ready
```

`/ready` 返回的 `mysql` 为 `true` 才说明容器已经实际连接到 RDS，而不只是服务进程启动成功。

常用运维命令：

```bash
docker compose restart
docker compose down
docker compose pull
```

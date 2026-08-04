# PR-Agent 离线部署迁移指南

本文档说明如何将已构建好的 PR-Agent 离线部署包迁移到另一台机器并部署运行。

## 一、需要拷贝的文件

假设源机器上已构建好离线包，位于 `deploy/` 目录。目标机器至少需要以下文件：

```text
deploy/
├── deploy.sh                 # 一键部署脚本
├── docker-compose.yml        # Docker Compose 编排文件
├── env.conf                  # 环境配置文件（用户按需修改）
├── test_env.conf             # 配置模板（可选，供参考）
├── offline_bundle.tar.gz     # 离线镜像包
└── README.md                 # 使用说明（可选）
```

**最小必需集合**：`deploy.sh`、`docker-compose.yml`、`env.conf`、`offline_bundle.tar.gz`。

> 注意：`offline_bundle.tar.gz` 体积较大（约 240MB+），传输时请确保完整，避免断点续传导致 tar 损坏。

## 二、目标机器环境要求

| 组件 | 版本/要求 |
|------|----------|
| 操作系统 | Linux x86_64（推荐 Debian 12+ / Ubuntu 22.04+） |
| Docker | 20.10+ |
| Docker Compose | v1 (`docker-compose`) 或 v2 (`docker compose`) |
| 网络 | 无需访问外网；需能访问你的 GitLab 实例和 AI API |

验证 Docker 环境：

```bash
docker info
docker compose version   # 或 docker-compose version
```

## 三、部署步骤

### 1. 上传文件

将整个 `deploy/` 目录上传到目标机器，例如：

```bash
scp -r deploy/ user@target-host:/path/to/
```

或者先打包再传输：

```bash
tar czf pr-agent-deploy.tar.gz deploy/
scp pr-agent-deploy.tar.gz user@target-host:/path/to/
```

### 2. 修改配置

```bash
cd deploy
cp test_env.conf env.conf   # 如果还没有 env.conf
vim env.conf
```

必填项：

| 配置项 | 说明 |
|--------|------|
| `OPENAI_API_KEY` | AI 服务 API Key |
| `OPENAI_API_BASE` | AI 服务地址，例如 `http://10.50.5.31:8317/v1` |
| `MODEL_NAME` | 模型名称，例如 `openai/deepseek-v4-flash` |
| `GITLAB_URL` | GitLab 实例地址 |
| `GITLAB_PAT` | GitLab Personal Access Token（需 `api` 和 `read_repository` 权限） |
| `GITLAB_SHARED_SECRET` | Webhook 密钥，设置一个随机字符串 |

可选但建议关注的配置：

| 配置项 | 说明 |
|--------|------|
| `SERVICE_PORT` | 服务端口，默认 `3000` |
| `PR_COMMANDS` | MR 创建/重新打开时自动执行的命令 |
| `PUSH_COMMANDS` | Push 提交时自动执行的命令（需 `HANDLE_PUSH_TRIGGER=true`） |
| `GUNICORN_TIMEOUT` | Worker 超时时间（秒），大 MR 建议 `1800` 或更大 |
| `AI_TIMEOUT` | AI 调用超时时间（秒），默认 `600` |
| `LOG_LEVEL` | 日志级别，`DEBUG`/`INFO`/`WARNING` |

### 3. 执行部署

```bash
cd deploy
bash deploy.sh
```

脚本会依次执行：

1. 自动解压 `offline_bundle.tar.gz`
2. 加载 Docker 镜像
3. 根据 `env.conf` 生成 `.secrets.toml`
4. 启动 `pr-agent-gitlab` 容器

看到以下输出表示部署成功：

```text
[INFO]  部署完成!
[INFO]  服务端口: 3000
[INFO]  GitLab Webhook URL: http://<服务器IP>:3000/webhook
```

### 4. 查看状态与日志

```bash
# 查看容器状态
docker ps -a --filter name=pr-agent-gitlab

# 查看实时日志
docker logs -f pr-agent-gitlab
```

## 四、配置 GitLab Webhook

### 4.1 开启 GitLab 本地 Webhook 请求（如需要）

如果 PR-Agent 和 GitLab 在同一台机器或内网，GitLab 默认可能禁止向本地地址发送 webhook。需要开启：

```bash
# 以管理员身份调用 GitLab API
curl -X PUT "http://<gitlab-url>/api/v4/application/settings" \
  -H "PRIVATE-TOKEN: <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"allow_local_requests_from_web_hooks_and_services": true}'
```

### 4.2 在项目上添加 Webhook

进入项目 **Settings → Webhooks → Add webhook**：

- **URL**：`http://<pr-agent-host-ip>:3000/webhook`
  - 如果 GitLab 运行在 Docker 中，而 PR-Agent 在宿主机上，可能需要使用 Docker 网关地址，例如 `http://172.19.0.1:3000/webhook`。
- **Secret Token**：填写 `env.conf` 中的 `GITLAB_SHARED_SECRET`
- **Trigger**：勾选 `Merge request events` 和 `Comments`
- 取消勾选 **SSL verification**（如果是 HTTP）

### 4.3 测试 Webhook

新建一个 MR，观察 PR-Agent 日志是否出现：

```text
Received a GitLab webhook
New merge request: ...
Performing command: /plus_review
```

## 五、常见问题

### 5.1 端口 3000 被占用

错误信息：

```text
[ERROR] 端口 3000 已被占用,请修改 env.conf 中的 SERVICE_PORT
```

解决：修改 `env.conf` 中的 `SERVICE_PORT`，例如改为 `3001`，同时更新 GitLab webhook URL。

### 5.2 Gunicorn worker 超时

错误信息：

```text
[CRITICAL] WORKER TIMEOUT (pid:xx)
```

解决：

1. 确保 `env.conf` 中有 `GUNICORN_TIMEOUT=1800`
2. 确保 `docker-compose.yml` 透传了该环境变量
3. 重新执行 `bash deploy.sh`

验证是否生效：

```bash
docker exec pr-agent-gitlab env | grep GUNICORN
# 应输出 GUNICORN_TIMEOUT=1800
```

### 5.3 GitLab webhook 返回 401/403

检查 `GITLAB_SHARED_SECRET` 是否与 webhook 中填写的 Secret Token 一致。

### 5.4 AI 调用超时

如果日志显示 AI 调用超时（不是 worker timeout），调大 `AI_TIMEOUT`：

```ini
AI_TIMEOUT=1800
```

### 5.5 离线包传输后损坏

如果 `deploy.sh` 解压或 `docker load` 时报错，重新传输 `offline_bundle.tar.gz`，并校验文件大小和 MD5：

```bash
md5sum offline_bundle.tar.gz
```

## 六、更新代码/配置

如果只是修改配置（如 `env.conf`、`docker-compose.yml`）：

```bash
cd deploy
docker stop pr-agent-gitlab
bash deploy.sh
```

如果 PR-Agent 代码有更新，需要重新构建离线包，参见 `prepare-offline.sh`。

## 七、目录说明

```text
deploy/
├── deploy.sh              # 一键部署脚本
├── docker-compose.yml     # Docker Compose 编排
├── env.conf               # 环境配置（按目标环境修改）
├── test_env.conf          # 配置模板
├── offline_bundle.tar.gz  # 离线镜像包
├── offline_bundle/        # 解压后的离线包（运行 deploy.sh 后生成，可删除）
├── .secrets.toml          # 由 deploy.sh 自动生成（不要手动编辑）
├── MIGRATION.md           # 本文件
└── README.md              # 快速开始
```

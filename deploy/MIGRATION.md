# PR-Agent 离线部署使用说明

## 一、需要复制的内容

把 `deploy/` 整个目录复制到目标电脑即可：

```text
deploy/
├── deploy.sh                 # 部署脚本
├── docker-compose.yml        # Docker 编排
├── env.conf                  # 配置文件
├── test_env.conf             # 配置模板
├── offline_bundle.tar.gz     # 离线镜像包
└── MIGRATION.md              # 本说明
```

最小只需要这 4 个文件：`deploy.sh`、`docker-compose.yml`、`env.conf`、`offline_bundle.tar.gz`。

## 二、环境要求

目标电脑需要：

- Linux x86_64
- Docker 20.10+
- Docker Compose v1 或 v2

检查命令：

```bash
docker info
docker compose version
```

## 三、修改配置

```bash
cd deploy
vim env.conf
```

必填项：

| 配置项 | 说明 |
|--------|------|
| `OPENAI_API_KEY` | AI 服务 API Key |
| `OPENAI_API_BASE` | AI 服务地址，例如 `http://10.50.5.31:8317/v1` |
| `MODEL_NAME` | 模型名称 |
| `GITLAB_URL` | GitLab 地址 |
| `GITLAB_PAT` | GitLab Personal Access Token |
| `GITLAB_SHARED_SECRET` | Webhook 密钥，随机字符串 |

大 MR 建议调大超时：

```ini
GUNICORN_TIMEOUT=1800
AI_TIMEOUT=1800
```

## 四、一键部署

```bash
cd deploy
bash deploy.sh
```

部署完成后会提示：

```text
服务端口: 3000
GitLab Webhook URL: http://<服务器IP>:3000/webhook
```

查看日志：

```bash
docker logs -f pr-agent-gitlab
```

## 五、配置 GitLab Webhook

### 5.1 允许 GitLab 访问本地地址

如果 PR-Agent 和 GitLab 在同一台机器，先开启本地 webhook：

```bash
curl -X PUT "http://<gitlab-url>/api/v4/application/settings" \
  -H "PRIVATE-TOKEN: <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"allow_local_requests_from_web_hooks_and_services": true}'
```

### 5.2 添加 Webhook

进入项目 **Settings → Webhooks → Add webhook**：

- **URL**：`http://<pr-agent-ip>:3000/webhook`
  - 如果 GitLab 也在 Docker 里，可能需要用 Docker 网关地址，例如 `http://172.19.0.1:3000/webhook`
- **Secret Token**：填 `env.conf` 里的 `GITLAB_SHARED_SECRET`
- **Trigger**：勾选 `Merge request events` 和 `Comments`
- 取消 `SSL verification`

### 5.3 验证

新建一个 MR，看日志里是否出现：

```text
Performing command: /plus_review
```

## 六、常见问题

### 端口 3000 被占用

改 `env.conf` 里的 `SERVICE_PORT`，同时改 webhook URL。

### Worker 超时

```text
[CRITICAL] WORKER TIMEOUT
```

确认 `env.conf` 有 `GUNICORN_TIMEOUT=1800`，然后重新部署。验证：

```bash
docker exec pr-agent-gitlab env | grep GUNICORN
```

### 改配置后重新部署

```bash
cd deploy
docker stop pr-agent-gitlab
bash deploy.sh
```

## 七、更新代码怎么办

如果 PR-Agent 代码有更新，需要重新构建离线包：

```bash
bash deploy/prepare-offline.sh
```

然后重新复制新的 `offline_bundle.tar.gz` 部署。

# PR-Agent GitLab Webhook 离线 Docker 部署

## 快速开始

```bash
# 1. 配置环境
cd deploy
cp test_env.conf env.conf
vim env.conf    # 填入你的 API key、GitLab 地址等

# 2. 一键部署
bash deploy.sh

# 3. 在 GitLab 上配置 Webhook
# URL:  http://<服务器IP>:3000/webhook
# Secret: env.conf 中 GITLAB_SHARED_SECRET 的值
# 触发事件: 勾选 "Merge request events" 和 "Comments"
```

## 目录说明

```
deploy/
├── env.conf                    # 环境配置（修改后生效，已 gitignore）
├── test_env.conf               # 配置模板（可复制为 env.conf）
├── deploy.sh                   # 一键部署脚本
├── uninstall.sh                # 一键卸载脚本
├── docker-compose.yml          # Docker Compose 编排
├── Dockerfile.offline          # 离线 Dockerfile
├── prepare-offline.sh          # 联网构建脚本（需联网）
├── offline_bundle.tar.gz       # 离线部署包（555MB）
└── README.md
```

## 配置说明

| 配置项 | 必填 | 说明 |
|--------|:--:|------|
| `OPENAI_API_KEY` | ✅ | API Key |
| `OPENAI_API_BASE` | ✅ | API 地址，如 `http://192.168.1.100/v1` |
| `MODEL_NAME` | ✅ | 模型名，LiteLLM 格式如 `openai/deepseek-v4-pro` |
| `GITLAB_URL` | ✅ | GitLab 地址 |
| `GITLAB_PAT` | ✅ | Personal Access Token |
| `GITLAB_SHARED_SECRET` | ✅ | Webhook 密钥 |
| `SERVICE_PORT` | | 服务端口，默认 3000 |
| `FALLBACK_MODELS` | | 备用模型列表 |
| `AI_TIMEOUT` | | 超时秒数，默认 600 |
| `MAX_MODEL_TOKENS` | | 模型最大 token |
| `RESPONSE_LANGUAGE` | | 回复语言，默认 zh-CN |
| `LOG_LEVEL` | | 日志级别 |
| `GITLAB_SSL_VERIFY` | | SSL 验证，自签名证书设 false |
| `HANDLE_PUSH_TRIGGER` | | 是否响应 push 事件 |

## 常用命令

```bash
# 部署
bash deploy.sh

# 查看日志
docker logs -f pr-agent-gitlab

# 查看状态
docker ps -a --filter name=pr-agent-gitlab

# 重启
docker restart pr-agent-gitlab

# 停止
docker stop pr-agent-gitlab

# 卸载（交互式）
bash uninstall.sh

# 强制卸载（代码更新时）
bash uninstall.sh --force
```

## 代码更新流程

```bash
# 1. 强制卸载旧版
bash uninstall.sh --force

# 2. 上传新的 offline_bundle.tar.gz 到 deploy/ 目录

# 3. 删除旧解压目录（如有）
rm -rf offline_bundle

# 4. 重新部署
bash deploy.sh
```

## 重新构建离线包

依赖更新、Python 版本变更时需要重建，在一台有网的 Linux x86_64 机器上：

```bash
# 确保已安装 Docker
bash deploy/prepare-offline.sh
# → 生成 deploy/offline_bundle.tar.gz
```

## 环境要求

| 组件 | 版本 |
|------|------|
| 操作系统 | Debian 12+ (x86_64) |
| Docker | 20.10+ |
| Docker Compose | v1 (docker-compose) 或 v2 (docker compose) |
| 网络 | 无需外网（离线部署） |

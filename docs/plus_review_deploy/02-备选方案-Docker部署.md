# 方案 B：Docker 部署 plus_review

适合：你希望部署更干净，不想在服务器直接装 Python 依赖。

## 你要先准备

```text
GITLAB_URL=你的 GitLab 地址，例如 https://gitlab.com
GITLAB_PAT=GitLab 机器人账号的 Personal Access Token，需要 api 权限
WEBHOOK_SECRET=一串随机字符串，后面 GitLab Webhook 里也填这个
OPENAI_KEY=你的模型 key
SERVER_DOMAIN=你的服务域名，例如 https://pr-agent.example.com
```

## 第 1 步：安装 Docker

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install -y git docker.io
sudo systemctl enable docker
sudo systemctl start docker
```

## 第 2 步：拉代码

```bash
mkdir -p ~/apps
cd ~/apps
git clone -b codex/customize-pr-agent https://github.com/xiaoxiao792/ai_pr.git PR-Agent
cd PR-Agent
```

## 第 3 步：改成自动跑 plus_review

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('pr_agent/settings/configuration.toml')
s = p.read_text(encoding='utf-8')
start = s.index('[gitlab]')
end = s.index('\n[gitea]', start)
new = '''[gitlab]
url = "https://gitlab.com"
expand_submodule_diffs = false
pr_commands = [
    "/plus_review",
]
handle_push_trigger = true
push_commands = [
    "/plus_review",
]
# ssl_verify = true
'''
p.write_text(s[:start] + new + s[end:], encoding='utf-8')
PY
```

如果你的 GitLab 不是 `https://gitlab.com`，编辑：

```bash
vim pr_agent/settings/configuration.toml
```

把 `[gitlab]` 里的 `url` 改成你的 GitLab 地址。

## 第 4 步：构建镜像

```bash
sudo docker build -f docker/Dockerfile --target gitlab_webhook -t ai-pr-agent:plus-review .
```

## 第 5 步：写环境变量文件

```bash
cat > .env.gitlab <<'EOF'
CONFIG__GIT_PROVIDER=gitlab
CONFIG__MODEL=gpt-4.1
CONFIG__RESPONSE_LANGUAGE=zh-CN
CONFIG__PUBLISH_OUTPUT=true
CONFIG__PUBLISH_OUTPUT_PROGRESS=false

GITLAB__URL=替换成你的 GitLab 地址
GITLAB__PERSONAL_ACCESS_TOKEN=替换成你的 GitLab PAT
GITLAB__SHARED_SECRET=替换成你的 Webhook Secret

OPENAI__KEY=替换成你的 OpenAI Key
PORT=3000
EOF
chmod 600 .env.gitlab
```

## 第 6 步：启动容器

```bash
sudo docker run -d \
  --name ai-pr-agent \
  --restart always \
  --env-file .env.gitlab \
  -p 3000:3000 \
  ai-pr-agent:plus-review
```

验证：

```bash
curl http://127.0.0.1:3000/
```

正常返回：

```json
{"status":"ok"}
```

看日志：

```bash
sudo docker logs -f ai-pr-agent
```

## 第 7 步：GitLab 配 Webhook

进入 GitLab 项目：

```text
Settings -> Webhooks
```

填写：

```text
URL: http://你的服务器IP:3000/webhook
Secret token: 和 GITLAB__SHARED_SECRET 一样
```

勾选：

```text
Merge request events
Comments events
```

保存后点 Test。

## 第 8 步：验证自动触发

新建一个 MR，或者往已有 MR push 一个新 commit。

成功结果：

```text
MR 评论区出现 ## Plus Review
```

## 常用命令

重启：

```bash
sudo docker restart ai-pr-agent
```

停止：

```bash
sudo docker stop ai-pr-agent
```

删除旧容器：

```bash
sudo docker rm ai-pr-agent
```

更新代码后重新构建：

```bash
cd ~/apps/PR-Agent
git pull
sudo docker build -f docker/Dockerfile --target gitlab_webhook -t ai-pr-agent:plus-review .
sudo docker rm -f ai-pr-agent
sudo docker run -d \
  --name ai-pr-agent \
  --restart always \
  --env-file .env.gitlab \
  -p 3000:3000 \
  ai-pr-agent:plus-review
```

## 常见问题

### 401 unauthorized

GitLab Webhook 里的 Secret token 和 `.env.gitlab` 里的 `GITLAB__SHARED_SECRET` 不一致。

### 没有评论

看日志：

```bash
sudo docker logs -f ai-pr-agent
```

确认：

```text
GitLab token 有 api 权限
机器人账号有项目权限
OPENAI__KEY 正确
CONFIG__PUBLISH_OUTPUT=true
```

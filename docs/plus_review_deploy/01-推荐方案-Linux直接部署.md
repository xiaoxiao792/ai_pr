# 方案 A：Linux 直接部署 plus_review

适合：你有一台 Linux 服务器，想长期运行 GitLab Webhook 服务。

## 你要先准备

把下面 5 个值准备好：

```text
GITLAB_URL=你的 GitLab 地址，例如 https://gitlab.com
GITLAB_PAT=GitLab 机器人账号的 Personal Access Token，需要 api 权限
WEBHOOK_SECRET=一串随机字符串，后面 GitLab Webhook 里也填这个
OPENAI_KEY=你的模型 key
SERVER_DOMAIN=你的服务域名，例如 https://pr-agent.example.com
```

如果没有域名，也可以先用服务器 IP，例如：

```text
http://你的服务器IP:3000
```

## 第 1 步：安装软件

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv python3-pip
```

## 第 2 步：拉代码

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone -b codex/customize-pr-agent https://github.com/xiaoxiao792/ai_pr.git PR-Agent
sudo chown -R $USER:$USER /opt/PR-Agent
cd /opt/PR-Agent
```

## 第 3 步：安装依赖

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 第 4 步：写配置

```bash
cat > /opt/PR-Agent/.env <<'EOF'
CONFIG__GIT_PROVIDER=gitlab
CONFIG__MODEL=gpt-4.1
CONFIG__RESPONSE_LANGUAGE=zh-CN
CONFIG__PUBLISH_OUTPUT=true
CONFIG__PUBLISH_OUTPUT_PROGRESS=false
GUNICORN_WORKERS=1

GITLAB__URL=替换成你的 GitLab 地址
GITLAB__PERSONAL_ACCESS_TOKEN=替换成你的 GitLab PAT
GITLAB__SHARED_SECRET=替换成你的 Webhook Secret

OPENAI__KEY=替换成你的 OpenAI Key
EOF
chmod 600 /opt/PR-Agent/.env
```

然后编辑自动触发命令：

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('/opt/PR-Agent/pr_agent/settings/configuration.toml')
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

如果你不是 `https://gitlab.com`，再改这里：

```bash
vim /opt/PR-Agent/pr_agent/settings/configuration.toml
```

把 `[gitlab]` 里的：

```toml
url = "https://gitlab.com"
```

改成你的 GitLab 地址。

## 第 5 步：先手动跑一次

把下面的 MR 地址换成真实地址：

```bash
cd /opt/PR-Agent
source .venv/bin/activate
set -a
source .env
set +a

PYTHONPATH=. python -m pr_agent.cli \
  --pr_url 'https://gitlab.com/你的组/你的仓库/-/merge_requests/1' \
  plus_review \
  --config.publish_output=true \
  --config.publish_output_progress=false
```

看到 GitLab MR 下出现 `## Plus Review` 评论，就说明配置能用。

## 第 6 步：启动 Webhook 服务

先前台启动测试：

```bash
cd /opt/PR-Agent
source .venv/bin/activate
set -a
source .env
set +a

PORT=3000 PYTHONPATH=. python -m pr_agent.servers.gitlab_webhook
```

另开一个窗口验证：

```bash
curl http://127.0.0.1:3000/
```

正常会返回：

```json
{"status":"ok"}
```

## 第 7 步：做成系统服务

```bash
sudo tee /etc/systemd/system/pr-agent-gitlab.service >/dev/null <<'EOF'
[Unit]
Description=PR-Agent GitLab Webhook
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/PR-Agent
EnvironmentFile=/opt/PR-Agent/.env
Environment=PYTHONPATH=/opt/PR-Agent
Environment=PORT=3000
Environment=GUNICORN_WORKERS=1
ExecStart=/opt/PR-Agent/.venv/bin/python -m pr_agent.servers.gitlab_webhook
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pr-agent-gitlab
sudo systemctl start pr-agent-gitlab
sudo systemctl status pr-agent-gitlab
```

看日志：

```bash
journalctl -u pr-agent-gitlab -f
```

## 并发规则

这个方案已经配置成一次只执行一个自动 `plus_review`。

多个 MR 同时到来时，后到的会等待前一个跑完。

不要把 `GUNICORN_WORKERS` 改大，否则多个进程会绕开单进程队列。

## 第 8 步：GitLab 配 Webhook

进入你的 GitLab 项目：

```text
Settings -> Webhooks
```

填写：

```text
URL: http://你的服务器IP:3000/webhook
Secret token: 和 GITLAB__SHARED_SECRET 一样
```

如果你有域名，就填：

```text
URL: https://你的域名/webhook
```

勾选：

```text
Merge request events
Comments events
```

保存后点 Test。

## 第 9 步：验证自动触发

新建一个 MR，或者往已有 MR push 一个新 commit。

成功结果：

```text
MR 评论区出现 ## Plus Review
```

## 常见问题

### 没有评论

按顺序查：

```bash
journalctl -u pr-agent-gitlab -f
```

确认：

```text
GitLab token 有 api 权限
机器人账号有项目权限
.env 里的 GITLAB__URL 正确
.env 里的 CONFIG__PUBLISH_OUTPUT=true
```

### 401 unauthorized

GitLab Webhook 里的 Secret token 和 `.env` 里的 `GITLAB__SHARED_SECRET` 不一致。

### 想手动触发

在 MR 评论里写：

```text
/plus_review
```

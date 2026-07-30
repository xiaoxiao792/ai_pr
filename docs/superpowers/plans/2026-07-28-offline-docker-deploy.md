# 离线 Docker 部署方案 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 PR-Agent 创建完整的离线 Docker 部署方案，包括配置宏文件、离线构建脚本、一键部署和一键卸载脚本。

**Architecture:** 所有文件位于 `deploy/` 目录下。`env.conf` 是唯一配置入口（用户编辑），`deploy.sh` 读取它生成 `.secrets.toml` 并通过 volume 挂载到容器中。`prepare-offline.sh` 在有网机器上拉取镜像和依赖打包成 `offline_bundle.tar.gz`。`Dockerfile.offline` 只保留 `gitlab_webhook` target 并支持离线 pip 安装。

**Tech Stack:** Bash 脚本, Docker, Docker Compose, Python 3.12, Dynaconf

## Global Constraints

- 目标环境: Debian 13.6 (x86_64), Docker 29.6.2, 无外网
- 构建环境: Debian 12 (x86_64), 需安装 Docker
- Python >= 3.12 (pyproject.toml 约束)
- 运行模式: GitLab Webhook (Dockerfile 中的 gitlab_webhook target)
- Dynaconf env_loader 使用 `__` 作为嵌套分隔符（e.g. `GITLAB__URL` → `gitlab.url`）
- 配置方式: `deploy.sh` 从 env.conf 生成 `.secrets.toml`，通过 Docker volume 挂载到 `/app/pr_agent/settings/.secrets.toml`

---

### Task 1: env.conf + .gitignore

**Files:**
- Create: `deploy/env.conf`
- Create: `deploy/.gitignore`

**Interfaces:**
- Produces: `env.conf` — `KEY=VALUE` 格式，被 `deploy.sh` 和 `prepare-offline.sh` 通过 `source` 加载
- 所有后续 Task 都依赖此文件作为配置入口

- [ ] **Step 1: 创建 deploy/.gitignore**

```bash
cat > deploy/.gitignore << 'GITIGNORE_EOF'
# 离线构建产物（体积大，不纳入版本管理）
offline_bundle/
offline_bundle.tar.gz

# 运行时生成的密钥文件（由 deploy.sh 从 env.conf 生成）
.secrets.toml
GITIGNORE_EOF
```

- [ ] **Step 2: 创建 deploy/env.conf**

```bash
cat > deploy/env.conf << 'ENVCONF_EOF'
# ============================================================
# PR-Agent GitLab Webhook 离线部署 — 环境配置文件
# ============================================================
# 用法: 修改此文件中的值,然后执行 ./deploy.sh
# 所有字段必须填写,脚本启动时会校验必填项
# ============================================================

# ---------- 服务配置 ----------
# 对外暴露的服务端口
SERVICE_PORT=3000
# Docker 容器名称
CONTAINER_NAME=pr-agent-gitlab

# ---------- AI 模型配置 (newapi / OpenAI 兼容 API) ----------
# API Key (必填)
OPENAI_API_KEY=sk-your-key-here
# API Base URL (必填), 例如 http://your-newapi-server/v1
OPENAI_API_BASE=http://localhost:8080/v1
# 模型名称 (必填)
MODEL_NAME=gpt-5.5

# ---------- GitLab 配置 ----------
# GitLab 实例 URL (必填), 例如 https://gitlab.your-company.com
GITLAB_URL=https://gitlab.example.com
# GitLab Personal Access Token (必填), 需要 api 和 read_repository 权限
GITLAB_PAT=glpat-your-personal-access-token
# Webhook Shared Secret (必填), 一个随机字符串用于验证 webhook 来源
GITLAB_SHARED_SECRET=change-me-to-a-random-string

# ---------- PR-Agent 自动触发命令 ----------
# MR 新建/reopen 时自动触发的命令列表 (JSON 数组格式)
PR_COMMANDS='["/describe", "/review"]'
# Push 新提交时自动触发的命令列表 (JSON 数组格式)
PUSH_COMMANDS='["/describe", "/review"]'
# 是否启用 push trigger (true/false)
HANDLE_PUSH_TRIGGER=false

# ---------- 运行时配置 ----------
# 日志级别: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=DEBUG
# 输出详细程度: 0=简洁, 1=详细, 2=调试
VERBOSITY_LEVEL=0
ENVCONF_EOF
```

- [ ] **Step 3: 验证文件存在**

```bash
ls -la deploy/env.conf deploy/.gitignore
```

- [ ] **Step 4: 提交**

```bash
git add deploy/env.conf deploy/.gitignore
git commit -m "feat: add deployment env config and gitignore"
```

---

### Task 2: Dockerfile.offline

**Files:**
- Create: `deploy/Dockerfile.offline`

**Interfaces:**
- Consumes: 项目根目录 `pyproject.toml`, `requirements.txt`, `pr_agent/` 目录
- Produces: Docker 镜像 `pr-agent-gitlab:offline`，暴露端口 3000
- build context: 项目根目录 (`docker build -f deploy/Dockerfile.offline -t pr-agent-gitlab:offline .`)

- [ ] **Step 1: 编写 Dockerfile.offline**

```bash
cat > deploy/Dockerfile.offline << 'DOCKERFILE_EOF'
# ============================================================
# PR-Agent GitLab Webhook 离线 Docker 镜像
# ============================================================
# 构建: docker build -f deploy/Dockerfile.offline -t pr-agent-gitlab:offline .
# 仅在 gitlab_webhook target; 离线安装 pip 依赖
# ============================================================

FROM python:3.12.13-slim AS base
RUN apt-get update && \
    apt-get install --no-install-recommends -y git curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

FROM base AS gitlab_webhook
WORKDIR /app

# 离线安装 pip 依赖 (wheel 文件由 prepare-offline.sh 下载到 deploy/offline_wheels/)
COPY deploy/offline_wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels/ /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels/

# 安装项目本身 (不含依赖,因为依赖已通过 wheel 安装)
COPY pyproject.toml requirements.txt ./
COPY pr_agent/ pr_agent/
RUN pip install --no-cache-dir --no-deps -e .

ENV PYTHONPATH=/app

# GitLab webhook 服务默认监听 3000 端口
EXPOSE 3000

CMD ["python", "-m", "gunicorn", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-c", "pr_agent/servers/gunicorn_config.py", \
     "--forwarded-allow-ips", "*", \
     "pr_agent.servers.gitlab_webhook:app"]
DOCKERFILE_EOF
```

- [ ] **Step 2: 验证 Dockerfile 语法（可选，需要 Docker）**

```bash
# 仅在有 Docker 时运行
docker build --check -f deploy/Dockerfile.offline . 2>&1 || true
```

- [ ] **Step 3: 提交**

```bash
git add deploy/Dockerfile.offline
git commit -m "feat: add offline Dockerfile for GitLab webhook"
```

---

### Task 3: docker-compose.yml

**Files:**
- Create: `deploy/docker-compose.yml`

**Interfaces:**
- Consumes: 镜像 `pr-agent-gitlab:offline`, 环境变量通过 deploy.sh 注入
- Produces: 运行中的 `pr-agent-gitlab` 容器，host 端口映射到容器 3000

- [ ] **Step 1: 编写 docker-compose.yml**

```bash
cat > deploy/docker-compose.yml << 'COMPOSE_EOF'
# ============================================================
# PR-Agent GitLab Webhook — Docker Compose 编排
# ============================================================
# 此文件中的 ${VAR} 占位符由 deploy.sh 通过 envsubst 注入
# 不要手动修改此文件,所有配置请在 env.conf 中修改
# ============================================================

version: "3.8"

services:
  pr-agent-gitlab:
    image: pr-agent-gitlab:offline
    container_name: ${CONTAINER_NAME:-pr-agent-gitlab}
    ports:
      - "${SERVICE_PORT:-3000}:3000"
    volumes:
      # 挂载 deploy.sh 生成的 .secrets.toml 到容器内
      - ./.secrets.toml:/app/pr_agent/settings/.secrets.toml:ro
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "5"
COMPOSE_EOF
```

- [ ] **Step 2: 提交**

```bash
git add deploy/docker-compose.yml
git commit -m "feat: add docker-compose for GitLab webhook service"
```

---

### Task 4: prepare-offline.sh

**Files:**
- Create: `deploy/prepare-offline.sh`

**Interfaces:**
- Consumes: `env.conf` (source), `requirements.txt`, 项目代码, Docker Hub
- Produces: `deploy/offline_bundle.tar.gz`（包含基础镜像 tar + 项目镜像 tar + pip wheel 包）

- [ ] **Step 1: 编写 prepare-offline.sh**

```bash
cat > deploy/prepare-offline.sh << 'PREPARE_EOF'
#!/bin/bash
# ============================================================
# PR-Agent 离线包构建脚本
# ============================================================
# 用途: 在有网络的 Linux x86_64 机器上执行,生成 offline_bundle.tar.gz
# 要求: Docker 已安装并运行, python3 已安装
# 输出: deploy/offline_bundle.tar.gz (传输到离线生产机使用)
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUNDLE_DIR="$SCRIPT_DIR/offline_bundle"
WHEELS_DIR="$SCRIPT_DIR/offline_wheels"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- 环境检查 ----------
check_prereqs() {
    log_info "检查构建环境..."

    if ! command -v docker &>/dev/null; then
        log_error "Docker 未安装,请先安装 Docker"
        exit 1
    fi

    if ! docker info &>/dev/null; then
        log_error "Docker daemon 未运行,请先启动 Docker"
        exit 1
    fi

    if ! command -v python3 &>/dev/null; then
        log_error "python3 未安装"
        exit 1
    fi

    # 检查是否为 x86_64
    local arch
    arch=$(uname -m)
    if [ "$arch" != "x86_64" ]; then
        log_warn "当前架构为 $arch,离线包将基于此架构构建。目标机器必须为相同架构。"
    fi

    log_info "环境检查通过"
}

# ---------- 清理旧产物 ----------
clean() {
    log_info "清理旧的构建产物..."
    rm -rf "$BUNDLE_DIR" "$WHEELS_DIR" "$SCRIPT_DIR/offline_bundle.tar.gz"
    mkdir -p "$BUNDLE_DIR" "$WHEELS_DIR"
}

# ---------- 下载并保存 Docker 基础镜像 ----------
pull_base_image() {
    local BASE_IMAGE="python:3.12.13-slim"
    local BASE_TAR="$BUNDLE_DIR/python_3.12.13_slim.tar"

    log_info "拉取基础镜像: $BASE_IMAGE"
    docker pull --platform linux/amd64 "$BASE_IMAGE"

    log_info "导出基础镜像: $BASE_TAR"
    docker save -o "$BASE_TAR" "$BASE_IMAGE"
    log_info "基础镜像导出完成 ($(du -h "$BASE_TAR" | cut -f1))"
}

# ---------- 下载 pip 依赖包 (linux x86_64) ----------
download_wheels() {
    log_info "下载 Python 依赖包 (linux_x86_64)..."

    cd "$PROJECT_DIR"

    # 下载所有依赖的 wheel 文件到 offline_wheels/
    pip3 download \
        --platform manylinux2014_x86_64 \
        --platform manylinux_2_17_x86_64 \
        --python-version 3.12 \
        --implementation cp \
        --abi cp312 \
        --only-binary=:all: \
        -r requirements.txt \
        -d "$WHEELS_DIR" \
        2>&1 || {
            log_warn "部分包没有预编译 wheel,尝试下载源码包..."
            pip3 download \
                --python-version 3.12 \
                --implementation cp \
                -r requirements.txt \
                -d "$WHEELS_DIR"
        }

    local count
    count=$(ls -1 "$WHEELS_DIR" | wc -l)
    log_info "已下载 $count 个包到 offline_wheels/ ($(du -sh "$WHEELS_DIR" | cut -f1))"
}

# ---------- 构建项目镜像 ----------
build_project_image() {
    local PROJECT_IMAGE="pr-agent-gitlab:offline"
    local PROJECT_TAR="$BUNDLE_DIR/pr_agent_gitlab_offline.tar"

    log_info "构建项目镜像: $PROJECT_IMAGE"
    cd "$PROJECT_DIR"
    docker build \
        --platform linux/amd64 \
        -f deploy/Dockerfile.offline \
        -t "$PROJECT_IMAGE" \
        .

    log_info "导出项目镜像: $PROJECT_TAR"
    docker save -o "$PROJECT_TAR" "$PROJECT_IMAGE"
    log_info "项目镜像导出完成 ($(du -h "$PROJECT_TAR" | cut -f1))"
}

# ---------- 打包 ----------
package() {
    log_info "打包离线部署文件..."

    # 复制部署脚本和配置到 bundle 目录
    cp "$SCRIPT_DIR/env.conf" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/deploy.sh" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/uninstall.sh" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/docker-compose.yml" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/Dockerfile.offline" "$BUNDLE_DIR/"
    cp -r "$WHEELS_DIR" "$BUNDLE_DIR/"

    # 打包
    cd "$SCRIPT_DIR"
    tar czf offline_bundle.tar.gz offline_bundle/
    log_info "离线包生成完成: $SCRIPT_DIR/offline_bundle.tar.gz ($(du -h offline_bundle.tar.gz | cut -f1))"
    log_info ""
    log_info "下一步: 将 offline_bundle.tar.gz 传输到离线生产服务器,然后执行:"
    log_info "  tar xzf offline_bundle.tar.gz -C /opt/pr-agent/"
    log_info "  cd /opt/pr-agent/offline_bundle && bash deploy.sh"
}

# ---------- 主流程 ----------
main() {
    echo ""
    log_info "============================================"
    log_info " PR-Agent 离线包构建工具"
    log_info "============================================"
    echo ""

    check_prereqs
    clean
    pull_base_image
    download_wheels
    build_project_image
    package
}

main "$@"
PREPARE_EOF

chmod +x deploy/prepare-offline.sh
```

- [ ] **Step 2: 提交**

```bash
git add deploy/prepare-offline.sh
git commit -m "feat: add offline bundle preparation script"
```

---

### Task 5: deploy.sh

**Files:**
- Create: `deploy/deploy.sh`

**Interfaces:**
- Consumes: `env.conf` (source), `offline_bundle/` 目录（含镜像 tar + wheel）
- Produces: 运行中的 Docker 容器, 生成的 `.secrets.toml`

- [ ] **Step 1: 编写 deploy.sh**

```bash
cat > deploy/deploy.sh << 'DEPLOY_EOF'
#!/bin/bash
# ============================================================
# PR-Agent 一键部署脚本
# ============================================================
# 用途: 在离线 Debian 生产机上执行,一键部署 PR-Agent GitLab Webhook
# 要求: Docker 已安装并运行,offline_bundle/ 目录与脚本在同一目录
# 用法: bash deploy.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/env.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- 加载配置 ----------
load_config() {
    if [ ! -f "$ENV_FILE" ]; then
        log_error "配置文件不存在: $ENV_FILE"
        exit 1
    fi
    source "$ENV_FILE"
    log_info "已加载配置: $ENV_FILE"
}

# ---------- 环境检查 ----------
check_prereqs() {
    log_info "检查 Docker 环境..."

    if ! command -v docker &>/dev/null; then
        log_error "Docker 未安装,请先安装 Docker"
        log_error "Debian 安装命令: curl -fsSL https://get.docker.com | bash"
        exit 1
    fi

    if ! docker info &>/dev/null; then
        log_error "Docker daemon 未运行,请先启动 Docker"
        exit 1
    fi

    if ! command -v docker &>/dev/null && ! docker compose version &>/dev/null; then
        log_warn "docker compose 插件未安装,Docker Compose v2 是推荐的"
    fi

    log_info "Docker 环境检查通过"
}

# ---------- 校验必填配置项 ----------
validate_config() {
    log_info "校验配置项..."
    local missing=()

    [ -z "${OPENAI_API_KEY:-}" ] && missing+=("OPENAI_API_KEY")
    [ -z "${OPENAI_API_BASE:-}" ] && missing+=("OPENAI_API_BASE")
    [ -z "${MODEL_NAME:-}" ] && missing+=("MODEL_NAME")
    [ -z "${GITLAB_URL:-}" ] && missing+=("GITLAB_URL")
    [ -z "${GITLAB_PAT:-}" ] && missing+=("GITLAB_PAT")
    [ -z "${GITLAB_SHARED_SECRET:-}" ] && missing+=("GITLAB_SHARED_SECRET")

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "以下必填配置项为空,请在 env.conf 中填写:"
        for m in "${missing[@]}"; do
            log_error "  - $m"
        done
        exit 1
    fi

    # 检查端口是否被占用
    local port="${SERVICE_PORT:-3000}"
    if ss -tlnp 2>/dev/null | grep -q ":$port " || netstat -tlnp 2>/dev/null | grep -q ":$port "; then
        log_error "端口 $port 已被占用,请修改 env.conf 中的 SERVICE_PORT"
        exit 1
    fi

    log_info "配置校验通过"
}

# ---------- 导入 Docker 镜像 ----------
load_images() {
    log_info "导入 Docker 镜像..."

    local BASE_TAR="$SCRIPT_DIR/offline_bundle/python_3.12.13_slim.tar"
    local PROJECT_TAR="$SCRIPT_DIR/offline_bundle/pr_agent_gitlab_offline.tar"

    if [ -f "$BASE_TAR" ]; then
        log_info "导入基础镜像..."
        docker load -i "$BASE_TAR"
    else
        log_warn "基础镜像 tar 不存在,跳过: $BASE_TAR"
    fi

    if [ -f "$PROJECT_TAR" ]; then
        log_info "导入项目镜像..."
        docker load -i "$PROJECT_TAR"
    else
        log_error "项目镜像 tar 不存在: $PROJECT_TAR"
        exit 1
    fi

    log_info "镜像导入完成"
}

# ---------- 生成 .secrets.toml ----------
generate_secrets() {
    log_info "生成 .secrets.toml..."

    cat > "$SCRIPT_DIR/.secrets.toml" << SECRETS_EOF
# 此文件由 deploy.sh 自动生成,来源于 env.conf
# 请勿手动编辑 — 修改 env.conf 后重新执行 deploy.sh

[openai]
key = "${OPENAI_API_KEY}"
api_base = "${OPENAI_API_BASE}"

[gitlab]
url = "${GITLAB_URL}"
personal_access_token = "${GITLAB_PAT}"
shared_secret = "${GITLAB_SHARED_SECRET}"
pr_commands = ${PR_COMMANDS:-'["/describe", "/review"]'}
push_commands = ${PUSH_COMMANDS:-'["/describe", "/review"]'}
handle_push_trigger = ${HANDLE_PUSH_TRIGGER:-false}
SECRETS_EOF

    log_info ".secrets.toml 已生成"
}

# ---------- 停止旧容器 ----------
stop_old() {
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME:-pr-agent-gitlab}$"; then
        log_info "停止旧容器: ${CONTAINER_NAME:-pr-agent-gitlab}"
        docker stop "${CONTAINER_NAME:-pr-agent-gitlab}" 2>/dev/null || true
        docker rm "${CONTAINER_NAME:-pr-agent-gitlab}" 2>/dev/null || true
    fi
}

# ---------- 启动服务 ----------
start_service() {
    log_info "启动 PR-Agent GitLab Webhook 服务..."

    cd "$SCRIPT_DIR"

    # 使用 --env-file 将 env.conf 中的变量注入 docker-compose
    docker compose --env-file "$ENV_FILE" up -d

    log_info ""
    log_info "============================================"
    log_info " 部署完成!"
    log_info "============================================"
    log_info ""
    log_info "服务端口: ${SERVICE_PORT:-3000}"
    log_info "GitLab Webhook URL: http://<服务器IP>:${SERVICE_PORT:-3000}/webhook"
    log_info ""
    log_info "查看日志: docker logs -f ${CONTAINER_NAME:-pr-agent-gitlab}"
    log_info "查看状态: docker ps -a --filter name=${CONTAINER_NAME:-pr-agent-gitlab}"
    log_info ""
}

# ---------- 主流程 ----------
main() {
    echo ""
    log_info "============================================"
    log_info " PR-Agent 一键部署工具"
    log_info "============================================"
    echo ""

    load_config
    check_prereqs
    validate_config
    load_images
    generate_secrets
    stop_old
    start_service
}

main "$@"
DEPLOY_EOF

chmod +x deploy/deploy.sh
```

- [ ] **Step 2: 提交**

```bash
git add deploy/deploy.sh
git commit -m "feat: add one-click deploy script"
```

---

### Task 6: uninstall.sh

**Files:**
- Create: `deploy/uninstall.sh`

**Interfaces:**
- Consumes: `env.conf` (source, 获取容器名)
- Produces: 停止的容器, 已删除的镜像（根据用户选择）

- [ ] **Step 1: 编写 uninstall.sh**

```bash
cat > deploy/uninstall.sh << 'UNINSTALL_EOF'
#!/bin/bash
# ============================================================
# PR-Agent 一键卸载脚本
# ============================================================
# 用途: 停止并清理 PR-Agent GitLab Webhook 容器和镜像
# 用法:
#   bash uninstall.sh            # 交互式卸载
#   bash uninstall.sh --force    # 强制卸载(删除所有,不询问)
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/env.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

FORCE_MODE=false
if [ "${1:-}" = "--force" ]; then
    FORCE_MODE=true
fi

# ---------- 加载配置 ----------
load_config() {
    if [ -f "$ENV_FILE" ]; then
        source "$ENV_FILE"
    fi
    CONTAINER_NAME="${CONTAINER_NAME:-pr-agent-gitlab}"
}

# ---------- 停止并删除容器 ----------
stop_container() {
    log_info "停止容器: $CONTAINER_NAME"

    if docker ps -q --filter "name=$CONTAINER_NAME" | grep -q .; then
        docker stop "$CONTAINER_NAME"
        log_info "容器已停止"
    else
        log_info "容器未在运行"
    fi

    if docker ps -a -q --filter "name=$CONTAINER_NAME" | grep -q .; then
        docker rm "$CONTAINER_NAME"
        log_info "容器已删除"
    else
        log_info "容器不存在"
    fi
}

# ---------- 删除镜像 ----------
remove_images() {
    local do_remove="$FORCE_MODE"

    if [ "$FORCE_MODE" = false ]; then
        echo ""
        read -r -p "是否删除 Docker 镜像? (y/N): " answer
        case "$answer" in
            [Yy]*) do_remove=true ;;
            *)     do_remove=false ;;
        esac
    fi

    if [ "$do_remove" = true ]; then
        log_info "删除项目镜像..."
        docker rmi pr-agent-gitlab:offline 2>/dev/null || log_warn "项目镜像不存在或已被删除"

        log_info "删除基础镜像..."
        docker rmi python:3.12.13-slim 2>/dev/null || log_warn "基础镜像不存在或已被删除"

        log_info "清理悬空镜像..."
        docker image prune -f 2>/dev/null || true
    else
        log_info "保留镜像"
    fi
}

# ---------- 删除配置和持久化文件 ----------
remove_config() {
    local do_remove="$FORCE_MODE"

    if [ "$FORCE_MODE" = false ]; then
        echo ""
        read -r -p "是否删除 .secrets.toml 配置文件? (y/N): " answer
        case "$answer" in
            [Yy]*) do_remove=true ;;
            *)     do_remove=false ;;
        esac
    fi

    if [ "$do_remove" = true ]; then
        log_info "删除 .secrets.toml..."
        rm -f "$SCRIPT_DIR/.secrets.toml"
    else
        log_info "保留配置文件"
    fi
}

# ---------- 主流程 ----------
main() {
    echo ""
    log_info "============================================"
    log_info " PR-Agent 卸载工具"
    if [ "$FORCE_MODE" = true ]; then
        log_info " 模式: --force (静默删除所有)"
    fi
    log_info "============================================"
    echo ""

    load_config
    stop_container
    remove_images
    remove_config

    echo ""
    log_info "============================================"
    log_info " 卸载完成!"
    log_info "============================================"

    # 显示残留文件提示
    if [ -f "$SCRIPT_DIR/.secrets.toml" ]; then
        log_warn "配置文件仍然存在: $SCRIPT_DIR/.secrets.toml"
    fi
    echo ""
}

main "$@"
UNINSTALL_EOF

chmod +x deploy/uninstall.sh
```

- [ ] **Step 2: 提交**

```bash
git add deploy/uninstall.sh
git commit -m "feat: add one-click uninstall script"
```

---

### Task 7: 最终验证与集成提交

这一步对整个 `deploy/` 目录做最终检查和提交。

- [ ] **Step 1: 检查所有文件可执行权限**

```bash
ls -la deploy/*.sh
# 确认 prepare-offline.sh, deploy.sh, uninstall.sh 均为 rwxr-xr-x
```

- [ ] **Step 2: 检查 .gitignore 生效**

```bash
git status deploy/
# 确认 offline_bundle/, offline_bundle.tar.gz, .secrets.toml 不会被追踪
```

- [ ] **Step 3: 最终提交**

```bash
git status
git add deploy/
git commit -m "feat: complete offline Docker deployment solution for GitLab webhook"
```

---

## 构建与部署流程图（参考）

```bash
# ========== 步骤 1: 在有网的 Linux 构建机上 ==========
cd pr-agent/
# 修改 deploy/env.conf 中的配置(可选,模型和GitLab地址可以在部署时再改)
bash deploy/prepare-offline.sh
# → 生成 deploy/offline_bundle.tar.gz

# ========== 步骤 2: 传输到离线生产机 ==========
scp deploy/offline_bundle.tar.gz user@production-server:/tmp/

# ========== 步骤 3: 在离线 Debian 生产机上部署 ==========
mkdir -p /opt/pr-agent
cd /opt/pr-agent
tar xzf /tmp/offline_bundle.tar.gz
cd offline_bundle
# 编辑 env.conf，填写真实的模型 key、GitLab URL 等
vim env.conf
# 一键部署
bash deploy.sh

# ========== 步骤 4: 代码更新时重新部署 ==========
bash uninstall.sh --force     # 一键卸载
rm -rf /opt/pr-agent/offline_bundle
# 上传新版本的 offline_bundle.tar.gz 到 /opt/pr-agent/
tar xzf offline_bundle.tar.gz
cd offline_bundle
bash deploy.sh                # 一键部署
```

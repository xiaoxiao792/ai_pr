#!/bin/bash
# ============================================================
# PR-Agent 一键部署脚本
# ============================================================
# 用途: 在离线 Debian 生产机上执行,一键部署 PR-Agent GitLab Webhook
# 要求: Docker 已安装并运行, offline_bundle/ 目录与脚本在同一目录
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

    # 检测 docker compose 命令 (v2 用 docker compose, v1 用 docker-compose)
    if docker compose version &>/dev/null 2>&1; then
        DOCKER_COMPOSE="docker compose"
    elif command -v docker-compose &>/dev/null; then
        DOCKER_COMPOSE="docker-compose"
    else
        log_error "docker compose 未安装,请先安装 docker-compose 或 docker compose 插件"
        exit 1
    fi
    log_info "Docker Compose 命令: $DOCKER_COMPOSE"

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

    local BASE_TAR="$SCRIPT_DIR/python_3.12.13_slim.tar"
    local PROJECT_TAR="$SCRIPT_DIR/pr_agent_gitlab_offline.tar"

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

[config]
git_provider = "${GIT_PROVIDER:-gitlab}"
model = "${MODEL_NAME}"
fallback_models = ${FALLBACK_MODELS:-'[]'}
ai_timeout = ${AI_TIMEOUT:-600}
max_model_tokens = ${MAX_MODEL_TOKENS:-32000}
custom_model_max_tokens = ${CUSTOM_MODEL_MAX_TOKENS:--1}
response_language = "${RESPONSE_LANGUAGE:-en-US}"
log_level = "${LOG_LEVEL:-DEBUG}"
verbosity_level = ${VERBOSITY_LEVEL:-0}

[openai]
key = "${OPENAI_API_KEY}"
api_base = "${OPENAI_API_BASE}"

[gitlab]
url = "${GITLAB_URL}"
personal_access_token = "${GITLAB_PAT}"
shared_secret = "${GITLAB_SHARED_SECRET}"
ssl_verify = ${GITLAB_SSL_VERIFY:-true}
auth_type = "${GITLAB_AUTH_TYPE:-private_token}"
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

    # 导出 env.conf 变量供 docker-compose 使用
    set -a
    source "$ENV_FILE"
    set +a
    $DOCKER_COMPOSE up -d

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

# ---------- 自动解压离线包 ----------
auto_extract() {
    local BUNDLE_TAR="$SCRIPT_DIR/offline_bundle.tar.gz"
    local BUNDLE_DIR="$SCRIPT_DIR/offline_bundle"
    local ORIG_DIR="$SCRIPT_DIR"

    if [ -f "$BUNDLE_TAR" ]; then
        log_info "检测到 offline_bundle.tar.gz,自动解压..."
        tar xzf "$BUNDLE_TAR" -C "$SCRIPT_DIR/"

        # 解压后切换到 bundle 目录继续执行
        if [ -d "$BUNDLE_DIR" ]; then
            log_info "解压完成,切换到 $BUNDLE_DIR"
            cd "$BUNDLE_DIR"
            SCRIPT_DIR="$BUNDLE_DIR"
            # 优先使用外层用户编辑过的 env.conf,否则用 bundle 内默认的
            if [ -f "$ORIG_DIR/env.conf" ]; then
                cp "$ORIG_DIR/env.conf" "$SCRIPT_DIR/env.conf"
                log_info "已从 $ORIG_DIR/env.conf 同步用户配置到 bundle"
            fi
            ENV_FILE="$SCRIPT_DIR/env.conf"
        fi
    elif [ -d "$BUNDLE_DIR" ]; then
        log_info "使用已解压的 offline_bundle/"
        # 同样: 优先同步用户编辑过的 env.conf
        if [ -f "$ORIG_DIR/env.conf" ]; then
            cp "$ORIG_DIR/env.conf" "$BUNDLE_DIR/env.conf"
            log_info "已从 $ORIG_DIR/env.conf 同步用户配置到 bundle"
        fi
    fi
}

# ---------- 主流程 ----------
main() {
    echo ""
    log_info "============================================"
    log_info " PR-Agent 一键部署工具"
    log_info "============================================"
    echo ""

    auto_extract
    load_config
    check_prereqs
    validate_config
    load_images
    generate_secrets
    stop_old
    start_service
}

main "$@"

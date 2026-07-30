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

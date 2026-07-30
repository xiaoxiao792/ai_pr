#!/bin/bash
# ============================================================
# PR-Agent 离线包构建脚本 (使用官方 Dockerfile)
# ============================================================
# 用途: 在有网络的 Linux x86_64 机器上执行,生成 offline_bundle.tar.gz
# 要求: Docker 已安装并运行
# 输出: deploy/offline_bundle.tar.gz (传输到离线生产机使用)
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUNDLE_DIR="$SCRIPT_DIR/offline_bundle"

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
    rm -rf "$BUNDLE_DIR" "$SCRIPT_DIR/offline_bundle.tar.gz"
    mkdir -p "$BUNDLE_DIR"
}

# ---------- 拉取并导出基础镜像 ----------
pull_base_image() {
    local BASE_IMAGE="python:3.12.13-slim"
    local BASE_TAR="$BUNDLE_DIR/python_3.12.13_slim.tar"

    log_info "拉取基础镜像: $BASE_IMAGE"
    docker pull --platform linux/amd64 "$BASE_IMAGE"

    log_info "导出基础镜像: $BASE_TAR"
    docker save -o "$BASE_TAR" "$BASE_IMAGE"
    log_info "基础镜像导出完成 ($(du -h "$BASE_TAR" | cut -f1))"
}

# ---------- 使用官方 Dockerfile 构建项目镜像 ----------
build_project_image() {
    local PROJECT_IMAGE="pr-agent-gitlab:offline"
    local PROJECT_TAR="$BUNDLE_DIR/pr_agent_gitlab_offline.tar"

    log_info "使用官方 docker/Dockerfile 构建项目镜像 (target: gitlab_webhook)..."
    cd "$PROJECT_DIR"
    docker build \
        --platform linux/amd64 \
        -f docker/Dockerfile \
        --target gitlab_webhook \
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
    cp "$SCRIPT_DIR/test_env.conf" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/deploy.sh" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/uninstall.sh" "$BUNDLE_DIR/"
    cp "$SCRIPT_DIR/docker-compose.yml" "$BUNDLE_DIR/"

    # 打包
    cd "$SCRIPT_DIR"
    tar czf offline_bundle.tar.gz offline_bundle/
    log_info "离线包生成完成: $SCRIPT_DIR/offline_bundle.tar.gz ($(du -h offline_bundle.tar.gz | cut -f1))"
    log_info ""
    log_info "下一步: 将 offline_bundle.tar.gz 传输到离线生产服务器,然后执行:"
    log_info "  tar xzf offline_bundle.tar.gz"
    log_info "  cd offline_bundle && bash deploy.sh"
}

# ---------- 主流程 ----------
main() {
    echo ""
    log_info "============================================"
    log_info " PR-Agent 离线包构建 (官方 Dockerfile)"
    log_info "============================================"
    echo ""

    check_prereqs
    clean
    pull_base_image
    build_project_image
    package
}

main "$@"

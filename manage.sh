#!/usr/bin/env bash
# ============================================================
# 漫创AI Web · 管理脚本（Ubuntu/Debian）
# ------------------------------------------------------------
# 菜单式管理：安装、配置域名、修改存储、查看状态、重启、卸载
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/JUUCHEN1/ainew/main/manage.sh | sudo bash
#   或：sudo bash manage.sh
# ============================================================
set -e

# ---- 配置 ----
INSTALL_DIR="/opt/libai-canvas-web"
STATE_FILE="$INSTALL_DIR/.manage_state"

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

banner() {
    clear
    echo -e "${CYAN}"
    cat <<'EOF'
╔═══════════════════════════════════════════════════════════╗
║         漫创AI Web · 管理脚本 (Ubuntu/Debian)            ║
╚═══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# ---- 检查权限 ----
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "请用 root 权限运行: sudo bash manage.sh"
    fi
}

# ---- 检查系统 ----
check_system() {
    if ! command -v apt-get &>/dev/null; then
        error "仅支持 Ubuntu/Debian (apt)。"
    fi
}

# ---- 智能检测 Nginx ----
detect_nginx() {
    NGINX_TYPE="none"
    NGINX_DOCKER=""

    # 检测 Docker 中的 Nginx
    if command -v docker &>/dev/null; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qi nginx; then
            NGINX_TYPE="docker"
            NGINX_DOCKER=$(docker ps --format '{{.Names}}' | grep -i nginx | head -1)
            info "检测到 Docker Nginx 容器: $NGINX_DOCKER"
            return 0
        fi
    fi

    # 检测宿主机 Nginx
    if command -v nginx &>/dev/null; then
        NGINX_TYPE="host"
        info "检测到宿主机 Nginx: $(nginx -v 2>&1)"
        return 0
    fi

    info "未检测到 Nginx，将使用临时静态服务器（仅 IP 访问）"
    return 1
}

# ---- 检测证书 ----
detect_cert() {
    [[ -z "$DOMAIN" ]] && return 1

    # 检查常见证书路径
    local cert_paths=(
        "/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
        "/etc/nginx/ssl/$DOMAIN/fullchain.pem"
        "$INSTALL_DIR/deploy/certs/fullchain.pem"
    )

    for path in "${cert_paths[@]}"; do
        if [[ -f "$path" ]]; then
            info "检测到已有证书: $path"
            EXISTING_CERT_PATH="$path"
            EXISTING_KEY_PATH="${path/fullchain.pem/privkey.pem}"
            return 0
        fi
    done

    return 1
}

# ---- 检测端口占用 ----
check_port() {
    local port=$1
    # 优先用 lsof，没有则用 ss/netstat 兜底
    if command -v lsof &>/dev/null; then
        lsof -i :$port -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
    elif command -v ss &>/dev/null; then
        ss -ltn 2>/dev/null | grep -q ":$port " && return 0 || return 1
    elif command -v netstat &>/dev/null; then
        netstat -ltn 2>/dev/null | grep -q ":$port " && return 0 || return 1
    fi
    return 1
}

# ---- 清理端口占用 ----
cleanup_port() {
    local port=$1
    info "检测到 $port 端口被占用，尝试自动清理..."

    # 查找占用进程
    local pids=$(lsof -ti :$port 2>/dev/null)
    if [[ -z "$pids" ]]; then
        return 0
    fi

    for pid in $pids; do
        local process=$(ps -p $pid -o comm= 2>/dev/null)
        warn "进程 $process (PID: $pid) 占用端口 $port"

        # 如果是我们自己的服务，停止它
        if [[ "$process" == "python"* ]] || [[ "$process" == "docker"* ]]; then
            info "停止进程 $pid..."
            kill $pid 2>/dev/null || true
            sleep 1
        fi
    done

    # 再次检查
    if check_port $port; then
        warn "端口 $port 仍被占用，可能需要手动处理"
        read -p "是否继续？(y/N): " confirm </dev/tty
        confirm=$(echo "$confirm" | tr -d '[:space:]')
        [[ "$confirm" != "y" && "$confirm" != "Y" ]] && return 1
    fi

    ok "端口 $port 已清理"
    return 0
}

# ---- 加载状态 ----
load_state() {
    if [[ -f "$STATE_FILE" ]]; then
        source "$STATE_FILE"
    else
        INSTALLED="false"
        DEPLOY_METHOD=""
        DOMAIN=""
        STORAGE_METHOD=""
    fi
}

# ---- 保存状态 ----
save_state() {
    mkdir -p "$INSTALL_DIR"
    cat > "$STATE_FILE" <<EOF
INSTALLED="true"
DEPLOY_METHOD="$DEPLOY_METHOD"
DOMAIN="$DOMAIN"
STORAGE_METHOD="$STORAGE_METHOD"
EOF
}

# ---- 主菜单 ----
show_menu() {
    banner
    load_state

    if [[ "$INSTALLED" == "true" ]]; then
        echo -e "${GREEN}✓ 已安装${NC} | 部署方式: ${CYAN}$DEPLOY_METHOD${NC} | 域名: ${CYAN}${DOMAIN:-未配置}${NC}"
    else
        echo -e "${YELLOW}未检测到安装${NC}"
    fi

    echo ""
    echo "═══════════════ 主菜单 ═══════════════"
    echo "  1) 全新安装（仅后端+前端，跳过域名和 Nginx）"
    echo "  2) 配置域名和 HTTPS（生成 Nginx 配置 + 证书）"
    echo "  3) 修改存储方式（本地自托管 / S3 / 图床）"
    echo "  4) 查看服务状态"
    echo "  5) 重启服务"
    echo "  6) 查看日志"
    echo "  7) 卸载"
    echo "  0) 退出"
    echo "══════════════════════════════════════"
    echo ""
    read -p "请选择 [0-7]: " choice </dev/tty

    case "$choice" in
        1) do_install ;;
        2) do_configure_domain ;;
        3) do_configure_storage ;;
        4) do_status ;;
        5) do_restart ;;
        6) do_logs ;;
        7) do_uninstall ;;
        0) info "退出"; exit 0 ;;
        *) warn "无效选择"; sleep 1; show_menu ;;
    esac
}

# ================ 1. 全新安装 ================
do_install() {
    banner
    info "全新安装 - 仅部署后端 + 前端，暴露端口，跳过域名配置"
    echo ""

    if [[ "$INSTALLED" == "true" ]]; then
        warn "检测到已安装，是否重新安装？(y/N)"
        read -p "> " confirm </dev/tty
        confirm=$(echo "$confirm" | tr -d '[:space:]')
        [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { show_menu; return; }
    fi

    # 选择部署方式
    echo ""
    info "请选择部署方式："
    echo "  1) Docker + Compose（推荐，环境隔离）"
    echo "  2) systemd（不装 Docker，资源最省）"
    read -p "请输入 1 或 2: " method </dev/tty
    case "$method" in
        1) DEPLOY_METHOD="docker" ;;
        2) DEPLOY_METHOD="systemd" ;;
        *) error "无效选择" ;;
    esac
    ok "部署方式: $DEPLOY_METHOD"

    # 选择存储
    echo ""
    info "参考图存储方式（图生视频需要公网可访问的图片 URL）："
    echo "  1) VPS 本机自托管（推荐，后续配置域名后自动生效）"
    echo "  2) S3 / 腾讯云 COS（需手动编辑配置文件）"
    echo "  3) 自定义图床（需手动编辑配置文件）"
    read -p "请输入 1/2/3: " storage </dev/tty
    case "$storage" in
        1) STORAGE_METHOD="local" ;;
        2) STORAGE_METHOD="s3" ;;
        3) STORAGE_METHOD="custom" ;;
        *) error "无效选择" ;;
    esac
    ok "存储方式: $STORAGE_METHOD"

    # 确认
    echo ""
    warn "即将开始安装，将执行："
    echo "  - 安装依赖（Docker 或 Python/ffmpeg）"
    echo "  - 拉取代码到 $INSTALL_DIR"
    echo "  - 配置环境变量"
    echo "  - 启动服务（后端 8765，前端 5180）"
    echo ""
    read -p "确认开始？(y/N): " confirm </dev/tty
    confirm=$(echo "$confirm" | tr -d '[:space:]')
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { info "已取消"; show_menu; return; }

    ok "开始安装..."

    # 执行安装
    clone_repo
    configure_env_basic
    configure_web_config_ip

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        install_docker
        start_docker_no_nginx
    else
        install_systemd_deps
        start_systemd_backend_only
    fi

    # 保存状态
    INSTALLED="true"
    DOMAIN=""
    save_state

    echo ""
    ok "========================================="
    ok "安装完成！"
    ok "========================================="
    echo ""
    get_server_ip
    echo -e "  后端地址: ${GREEN}http://$SERVER_IP:8765/health${NC}"
    echo -e "  前端地址: ${GREEN}http://$SERVER_IP:5180${NC}"
    echo ""
    warn "当前使用 IP + 端口访问，无 HTTPS。"
    info "准备好域名后，选择菜单【2】配置域名和 HTTPS。"
    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 2. 配置域名和 HTTPS ================
do_configure_domain() {
    banner
    info "配置域名和 HTTPS"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务（菜单选项 1）"; }

    # 检测 Nginx
    detect_nginx

    # 询问域名
    read -p "请输入你的域名（必须已解析到本机 IP）: " new_domain </dev/tty
    [[ -z "$new_domain" ]] && { warn "域名不能为空"; sleep 1; show_menu; return; }
    DOMAIN="$new_domain"
    ok "域名: $DOMAIN"

    # 检测已有证书
    CERT_METHOD=""
    if detect_cert; then
        info "检测到已有证书: $EXISTING_CERT_PATH"
        read -p "是否使用已有证书？(Y/n): " use_existing </dev/tty
        use_existing=$(echo "$use_existing" | tr -d '[:space:]')
        if [[ "$use_existing" != "n" && "$use_existing" != "N" ]]; then
            CERT_METHOD="existing"
            ok "将使用已有证书"
        fi
    fi

    # 如果没有已有证书或用户选择不用，询问证书方式
    if [[ -z "$CERT_METHOD" ]]; then
        echo ""
        info "HTTPS 证书获取方式："
        echo "  1) 自动申请 Let's Encrypt（推荐，域名必须已解析）"
        echo "  2) 手动放置证书文件"
        read -p "请输入 1 或 2: " cert_method </dev/tty
        case "$cert_method" in
            1) CERT_METHOD="auto" ;;
            2) CERT_METHOD="manual" ;;
            *) error "无效选择" ;;
        esac
        ok "证书方式: $CERT_METHOD"
    fi

    # 根据 Nginx 类型决定配置方式
    if [[ "$NGINX_TYPE" == "none" ]]; then
        warn "未检测到 Nginx，需要安装 Nginx 才能配置 HTTPS"
        read -p "是否安装 Nginx？(Y/n): " install_nginx </dev/tty
        install_nginx=$(echo "$install_nginx" | tr -d '[:space:]')
        if [[ "$install_nginx" == "n" || "$install_nginx" == "N" ]]; then
            warn "跳过域名配置"
            sleep 2
            show_menu
            return
        fi
        install_nginx_if_needed
        NGINX_TYPE="host"
    fi

    # 确认
    echo ""
    warn "即将执行："
    echo "  - 更新前端后端地址为 https://$DOMAIN"
    if [[ "$NGINX_TYPE" == "docker" ]]; then
        echo "  - 配置 Docker Nginx 容器"
    else
        echo "  - 配置宿主机 Nginx"
    fi
    if [[ "$CERT_METHOD" == "existing" ]]; then
        echo "  - 使用已有证书: $EXISTING_CERT_PATH"
    elif [[ "$CERT_METHOD" == "auto" ]]; then
        echo "  - 自动申请 Let's Encrypt 证书"
    fi
    echo "  - 重启服务"
    echo ""
    read -p "确认继续？(y/N): " confirm </dev/tty
    confirm=$(echo "$confirm" | tr -d '[:space:]')
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { info "已取消"; show_menu; return; }

    ok "开始配置..."

    # 更新配置
    cd "$INSTALL_DIR"
    configure_env_domain
    configure_web_config_domain

    # 根据 Nginx 类型配置
    if [[ "$NGINX_TYPE" == "docker" ]]; then
        warn "检测到 Docker Nginx，请手动配置反向代理到 http://宿主机IP:8765"
        warn "参考配置文件: $INSTALL_DIR/deploy/nginx/"
        read -p "配置完成后按回车继续..." </dev/tty
    else
        # 宿主机 Nginx
        configure_nginx_systemd

        if [[ "$CERT_METHOD" == "existing" ]]; then
            # 使用已有证书，更新 Nginx 配置
            sed -i "s|ssl_certificate .*|ssl_certificate $EXISTING_CERT_PATH;|" /etc/nginx/sites-available/libai.conf
            sed -i "s|ssl_certificate_key .*|ssl_certificate_key $EXISTING_KEY_PATH;|" /etc/nginx/sites-available/libai.conf
            ok "已配置使用现有证书"
        elif [[ "$CERT_METHOD" == "auto" ]]; then
            request_cert_systemd
        else
            warn "请手动配置证书路径到 /etc/nginx/sites-available/libai.conf"
            read -p "配置完成后按回车继续..." </dev/tty
        fi

        nginx -t && systemctl reload nginx || error "Nginx 配置错误"
    fi

    # 重启后端
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose restart backend
    else
        systemctl restart libai-backend
    fi

    # 保存状态
    save_state

    echo ""
    ok "========================================="
    ok "域名配置完成！"
    ok "========================================="
    echo ""
    echo -e "  访问地址: ${GREEN}https://$DOMAIN${NC}"
    echo ""
    warn "安全提醒: 建议在 Nginx 层加访问控制（IP 白名单 / basic auth）"
    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 3. 修改存储方式 ================
do_configure_storage() {
    banner
    info "修改存储方式"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务（菜单选项 1）"; }

    echo "当前存储方式: ${CYAN}${STORAGE_METHOD:-未设置}${NC}"
    echo ""
    echo "  1) VPS 本机自托管"
    echo "  2) S3 / 腾讯云 COS"
    echo "  3) 自定义图床"
    read -p "请选择新的存储方式 [1-3]: " storage </dev/tty
    case "$storage" in
        1) STORAGE_METHOD="local" ;;
        2) STORAGE_METHOD="s3" ;;
        3) STORAGE_METHOD="custom" ;;
        *) warn "无效选择"; sleep 1; show_menu; return ;;
    esac

    cd "$INSTALL_DIR"

    if [[ "$STORAGE_METHOD" == "local" ]]; then
        if [[ -n "$DOMAIN" ]]; then
            sed -i "s|^LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=https://$DOMAIN|" .env.deploy
            ok "本机自托管已配置，公网地址: https://$DOMAIN"
        else
            get_server_ip
            sed -i "s|^LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=http://$SERVER_IP:8765|" .env.deploy
            warn "当前未配置域名，使用 http://$SERVER_IP:8765（上游可能无法访问）"
            info "建议先配置域名（菜单选项 2）"
        fi
    else
        warn "请手动编辑 $INSTALL_DIR/.env.deploy 填写 S3/图床凭据"
        read -p "编辑完成后按回车继续..." </dev/tty
    fi

    # 重启服务
    info "重启服务以应用新配置..."
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose restart backend
    else
        systemctl restart libai-backend
    fi

    save_state
    ok "存储方式已更新并重启服务"
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 4. 查看服务状态 ================
do_status() {
    banner
    info "服务状态"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { warn "未检测到安装"; read -p "按回车返回..." </dev/tty; show_menu; return; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose ps
    else
        systemctl status libai-backend --no-pager -l
        echo ""
        systemctl status nginx --no-pager -l
    fi

    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 5. 重启服务 ================
do_restart() {
    banner
    info "重启服务"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务"; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose restart
        ok "Docker 服务已重启"
    else
        systemctl restart libai-backend
        systemctl reload nginx 2>/dev/null || true
        ok "systemd 服务已重启"
    fi

    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 6. 查看日志 ================
do_logs() {
    banner
    info "查看日志（Ctrl+C 退出）"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务"; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose logs -f --tail=100
    else
        journalctl -u libai-backend -f -n 100
    fi

    show_menu
}

# ================ 7. 卸载 ================
do_uninstall() {
    banner
    warn "卸载服务"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { warn "未检测到安装"; read -p "按回车返回..." </dev/tty; show_menu; return; }

    warn "将执行："
    echo "  - 停止并删除服务"
    echo "  - 删除 $INSTALL_DIR 目录（包括 data/，请提前备份）"
    echo "  - 删除 Nginx 配置"
    echo ""
    read -p "确认卸载？(yes/N): " confirm </dev/tty
    [[ "$confirm" != "yes" ]] && { info "已取消"; show_menu; return; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose down -v
    else
        systemctl stop libai-backend
        systemctl disable libai-backend
        rm -f /etc/systemd/system/libai-backend.service
        systemctl daemon-reload
        rm -f /etc/nginx/sites-enabled/libai.conf
        rm -f /etc/nginx/sites-available/libai.conf
        rm -f /etc/nginx/snippets/_libai_proxy.inc
        systemctl reload nginx 2>/dev/null || true
    fi

    rm -rf "$INSTALL_DIR"
    ok "卸载完成"
    exit 0
}

# ========== 工具函数 ==========

get_server_ip() {
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s icanhazip.com 2>/dev/null || echo "YOUR_VPS_IP")
}

clone_repo() {
    info "拉取代码到 $INSTALL_DIR..."
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        warn "目录已存在，拉取最新代码..."
        cd "$INSTALL_DIR" && git pull || error "git pull 失败"
    else
        rm -rf "$INSTALL_DIR"
        git clone https://github.com/JUUCHEN1/ainew.git "$INSTALL_DIR" || error "git clone 失败，请检查网络"
    fi
    cd "$INSTALL_DIR" || error "无法进入 $INSTALL_DIR"
    ok "代码准备完成"
}

configure_env_basic() {
    info "配置基础环境变量..."
    cd "$INSTALL_DIR"
    cp -n .env.deploy.example .env.deploy || true

    # 修复：systemd 模式下删除 Docker 专用的数据目录路径
    # run.py 会自动使用默认值 ./data（相对于 WorkingDirectory）
    if [[ "$DEPLOY_METHOD" == "systemd" ]]; then
        sed -i '/^LIBAI_APP_DATA_DIR=/d' .env.deploy
        sed -i '/^# LIBAI_APP_DATA_DIR=/d' .env.deploy
    fi

    # 基础配置
    sed -i "s|^LIBAI_CORS_ORIGINS=.*|LIBAI_CORS_ORIGINS=|" .env.deploy

    # 存储配置
    if [[ "$STORAGE_METHOD" == "local" ]]; then
        get_server_ip
        sed -i "s|^LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=http://$SERVER_IP:8765|" .env.deploy
    fi

    ok "环境变量配置完成"
}

configure_env_domain() {
    info "更新环境变量为域名模式..."
    cd "$INSTALL_DIR"
    sed -i "s|^LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=https://$DOMAIN|" .env.deploy
    sed -i "s|^LIBAI_CORS_ORIGINS=.*|LIBAI_CORS_ORIGINS=https://$DOMAIN|" .env.deploy
    ok "环境变量已更新"
}

configure_web_config_ip() {
    info "配置前端（IP 模式）..."
    get_server_ip
    sed -i "s|window.__LIBAI_BACKEND_BASE_URL__ = .*|window.__LIBAI_BACKEND_BASE_URL__ = \"http://$SERVER_IP:8765\";|" web/config.js
    ok "前端配置完成"
}

configure_web_config_domain() {
    info "配置前端（域名模式）..."
    sed -i 's|window.__LIBAI_BACKEND_BASE_URL__ = .*|window.__LIBAI_BACKEND_BASE_URL__ = window.location.origin;|' web/config.js
    ok "前端配置完成"
}

install_docker() {
    info "检查 Docker..."
    if command -v docker &>/dev/null && command -v docker compose &>/dev/null; then
        ok "Docker 已安装"
        return
    fi
    info "安装 Docker..."
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates gnupg lsb-release
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null || \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(lsb_release -is | tr '[:upper:]' '[:lower:]') $(lsb_release -cs) stable" \
        | tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    ok "Docker 安装完成"
}

install_systemd_deps() {
    info "安装 Python / ffmpeg..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git curl lsof
    ok "依赖安装完成"
}

install_nginx_if_needed() {
    if command -v nginx &>/dev/null; then
        ok "Nginx 已安装"
        return
    fi
    info "安装 Nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx certbot python3-certbot-nginx
    systemctl enable nginx
    ok "Nginx 安装完成"
}

start_docker_no_nginx() {
    info "启动 Docker 服务（仅后端，暴露 8765）..."
    cd "$INSTALL_DIR"

    # 检测端口冲突（修复 address already in use）
    if check_port 8765; then
        cleanup_port 8765 || warn "端口 8765 清理失败，启动可能失败"
    fi

    # 清理可能残留的旧容器和网络（修复 Docker 网络状态不一致）
    docker compose down 2>/dev/null || true
    docker ps -a --format '{{.Names}}' | grep -i libai | xargs -r docker rm -f 2>/dev/null || true
    docker network prune -f 2>/dev/null || true

    # 生成临时 compose（不含 nginx）
    cat > docker-compose.override.yml <<'EOF'
services:
  backend:
    ports:
      - "8765:8765"
EOF

    docker compose up -d --build backend
    ok "Docker 后端已启动"

    # 启动前端静态服务
    info "启动前端静态服务（5180）..."
    if check_port 5180; then
        cleanup_port 5180 || true
    fi
    cd web
    nohup python3 -m http.server 5180 > /tmp/libai-frontend.log 2>&1 &
    echo $! > /tmp/libai-frontend.pid
    ok "前端服务已启动"
}

restart_docker_with_nginx() {
    info "启动完整 Docker 服务（含 Nginx）..."
    cd "$INSTALL_DIR"

    # 删除 override，使用完整 compose
    rm -f docker-compose.override.yml

    # 停掉临时前端
    if [[ -f /tmp/libai-frontend.pid ]]; then
        kill $(cat /tmp/libai-frontend.pid) 2>/dev/null || true
        rm -f /tmp/libai-frontend.pid
    fi

    docker compose up -d --build
    ok "Docker 服务已启动（含 Nginx）"
}

start_systemd_backend_only() {
    info "安装 Python 依赖..."
    cd "$INSTALL_DIR"
    python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements.txt

    info "创建 libai 用户..."
    useradd -r -s /usr/sbin/nologin libai 2>/dev/null || true

    # 确保数据目录存在并赋权（修复 PermissionError）
    mkdir -p "$INSTALL_DIR/data"
    chown -R libai:libai "$INSTALL_DIR/data"

    info "安装 systemd 服务..."
    sed -i "s|/opt/libai-canvas-web|$INSTALL_DIR|g" deploy/systemd/libai-backend.service
    cp deploy/systemd/libai-backend.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now libai-backend

    # 等待并验证后端启动
    info "等待后端启动..."
    sleep 3
    if systemctl is-active --quiet libai-backend; then
        ok "后端服务已启动"
    else
        warn "后端启动异常，查看日志：journalctl -u libai-backend -n 30"
        journalctl -u libai-backend -n 15 --no-pager || true
    fi

    # 启动前端
    info "启动前端静态服务（5180）..."
    cd web
    nohup python3 -m http.server 5180 > /tmp/libai-frontend.log 2>&1 &
    echo $! > /tmp/libai-frontend.pid
    ok "前端服务已启动"
}

configure_nginx_docker() {
    info "配置 Nginx（Docker 版）..."
    cd "$INSTALL_DIR"
    sed -i "s|your-domain.com|$DOMAIN|g" deploy/nginx/libai.conf
    mkdir -p deploy/certs
    ok "Nginx 配置完成"
}

configure_nginx_systemd() {
    info "配置 Nginx（systemd 版）..."
    cd "$INSTALL_DIR"

    # 停掉临时前端
    if [[ -f /tmp/libai-frontend.pid ]]; then
        kill $(cat /tmp/libai-frontend.pid) 2>/dev/null || true
        rm -f /tmp/libai-frontend.pid
    fi

    sed -i "s|your-domain.com|$DOMAIN|g" deploy/nginx/libai.systemd.conf
    sed -i "s|/opt/libai-canvas-web|$INSTALL_DIR|g" deploy/nginx/libai.systemd.conf
    cp deploy/nginx/libai.systemd.conf /etc/nginx/sites-available/libai.conf
    cp deploy/nginx/_libai_proxy.host.inc /etc/nginx/snippets/_libai_proxy.inc
    ln -sf /etc/nginx/sites-available/libai.conf /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    ok "Nginx 配置完成"
}

request_cert_docker() {
    info "申请 Let's Encrypt 证书..."
    cd "$INSTALL_DIR"

    # 临时 80 端口 webroot
    docker run --rm -d --name certbot-temp -p 80:80 -v "$INSTALL_DIR/web:/usr/share/nginx/html:ro" nginx:alpine
    sleep 2

    docker run --rm \
        -v "$INSTALL_DIR/deploy/certs:/etc/letsencrypt" \
        -v "$INSTALL_DIR/web:/usr/share/nginx/html" \
        certbot/certbot certonly --webroot \
        -w /usr/share/nginx/html \
        -d "$DOMAIN" \
        --email "admin@$DOMAIN" \
        --agree-tos \
        --non-interactive || error "证书申请失败"

    docker stop certbot-temp 2>/dev/null || true

    cp "$INSTALL_DIR/deploy/certs/live/$DOMAIN/fullchain.pem" "$INSTALL_DIR/deploy/certs/"
    cp "$INSTALL_DIR/deploy/certs/live/$DOMAIN/privkey.pem" "$INSTALL_DIR/deploy/certs/"

    ok "证书申请完成"
}

request_cert_systemd() {
    info "申请 Let's Encrypt 证书..."
    certbot --nginx -d "$DOMAIN" --email "admin@$DOMAIN" --agree-tos --non-interactive --redirect || error "证书申请失败"
    ok "证书申请完成"
}

# ========== 入口 ==========
main() {
    check_root
    check_system
    load_state
    show_menu
}

main "$@"

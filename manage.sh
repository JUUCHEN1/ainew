#!/usr/bin/env bash
# ============================================================
# 漫创AI Web · 管理脚本（Ubuntu/Debian）
# ------------------------------------------------------------
# 统一 Nginx 入口架构：
#   - 本项目自带一个 Nginx 入口，监听自定义端口（默认 8080），
#     与你现有的其他 Nginx（如占用 80/443 的）互不干扰。
#   - 后端 uvicorn 只绑回环 127.0.0.1:8765，外网碰不到，安全。
#   - Nginx 同时托管前端静态 + 反代后端 API，前后端同源，
#     所以 web/config.js 永远用 window.location.origin，
#     IP 访问和以后加域名都自动正确，无需改配置。
#   - 以后加域名 HTTPS：只在这个 Nginx 上加 443 块，config.js 不动。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/JUUCHEN1/ainew/main/manage.sh | sudo bash
#   或：sudo bash manage.sh
#   安装后可直接输入：hb
# ============================================================

# ---- 配置 ----
INSTALL_DIR="/opt/libai-canvas-web"
STATE_FILE="$INSTALL_DIR/.manage_state"
REPO_URL="https://github.com/JUUCHEN1/ainew.git"
SCRIPT_URL="https://raw.githubusercontent.com/JUUCHEN1/ainew/main/manage.sh"
DEFAULT_PORT="8080"          # 本项目 Nginx 对外端口（可自定义）
BACKEND_PORT="8765"          # 后端回环端口（内部）

# ============================================================
# 管道模式自举（curl | bash）
# ------------------------------------------------------------
# 通过管道运行时，stdin 是脚本内容本身，交互式 read 无法从终端读取，
# 会出现“只显示标题就退出”的问题。这里把脚本下载成临时文件，再以
# 终端（/dev/tty）作为 stdin 重新执行，保证菜单可正常交互。
# ============================================================
if [ -z "${LIBAI_BOOTSTRAPPED:-}" ] && [ ! -t 0 ] && [ -e /dev/tty ]; then
    _self="$(mktemp /tmp/libai-manage.XXXXXX.sh 2>/dev/null || echo /tmp/libai-manage.sh)"
    if curl -fsSL "$SCRIPT_URL" -o "$_self" 2>/dev/null && [ -s "$_self" ]; then
        chmod +x "$_self" 2>/dev/null || true
        LIBAI_BOOTSTRAPPED=1 exec bash "$_self" </dev/tty
    fi
fi

# 注意：本脚本是交互式菜单，故意不使用 `set -e`。
# 很多命令（[[ ]] 测试、grep、read）正常返回非零，set -e 会导致脚本
# 莫名退出。关键步骤一律用显式的 `... || error "..."` 处理失败。

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

# ---- 获取公网 IP ----
get_server_ip() {
    SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null \
        || curl -s --max-time 5 icanhazip.com 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}' \
        || echo "YOUR_VPS_IP")
    [[ -z "$SERVER_IP" ]] && SERVER_IP="YOUR_VPS_IP"
}

# ---- 检测端口占用 ----
port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -ltn 2>/dev/null | grep -q ":$port " && return 0 || return 1
    elif command -v lsof &>/dev/null; then
        lsof -i :"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0 || return 1
    elif command -v netstat &>/dev/null; then
        netstat -ltn 2>/dev/null | grep -q ":$port " && return 0 || return 1
    fi
    return 1
}

# ---- 选一个可用端口（从建议值开始递增）----
pick_free_port() {
    local start=$1
    local p=$start
    while port_in_use "$p"; do
        p=$((p + 1))
        [[ $p -gt $((start + 50)) ]] && { echo "$start"; return; }
    done
    echo "$p"
}

# ---- 加载状态 ----
load_state() {
    if [[ -f "$STATE_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$STATE_FILE"
    else
        INSTALLED="false"
        DEPLOY_METHOD=""
        DOMAIN=""
        STORAGE_METHOD=""
        WEB_PORT="$DEFAULT_PORT"
    fi
    [[ -z "$WEB_PORT" ]] && WEB_PORT="$DEFAULT_PORT"
}

# ---- 保存状态 ----
save_state() {
    mkdir -p "$INSTALL_DIR"
    cat > "$STATE_FILE" <<EOF
INSTALLED="true"
DEPLOY_METHOD="$DEPLOY_METHOD"
DOMAIN="$DOMAIN"
STORAGE_METHOD="$STORAGE_METHOD"
WEB_PORT="$WEB_PORT"
EOF
}

# ---- 主菜单 ----
show_menu() {
    banner
    load_state

    if [[ "$INSTALLED" == "true" ]]; then
        get_server_ip
        local access="http://$SERVER_IP:$WEB_PORT"
        [[ -n "$DOMAIN" ]] && access="https://$DOMAIN"
        echo -e "${GREEN}✓ 已安装${NC} | 方式: ${CYAN}$DEPLOY_METHOD${NC} | 端口: ${CYAN}$WEB_PORT${NC} | 访问: ${CYAN}$access${NC}"
    else
        echo -e "${YELLOW}未检测到安装${NC}"
    fi

    echo ""
    echo "═══════════════ 主菜单 ═══════════════"
    echo "  1) 全新安装（统一入口，IP 即可访问）"
    echo "  2) 配置域名 + HTTPS（在已有入口上加证书）"
    echo "  3) 修改对外端口"
    echo "  4) 修改存储方式（本机自托管 / S3 / 图床）"
    echo "  5) 查看服务状态"
    echo "  6) 重启服务"
    echo "  7) 查看日志"
    echo "  8) 卸载"
    echo "  0) 退出"
    echo "══════════════════════════════════════"
    echo ""
    read -p "请选择 [0-8]: " choice </dev/tty

    case "$choice" in
        1) do_install ;;
        2) do_configure_domain ;;
        3) do_change_port ;;
        4) do_configure_storage ;;
        5) do_status ;;
        6) do_restart ;;
        7) do_logs ;;
        8) do_uninstall ;;
        0) info "退出"; exit 0 ;;
        *) warn "无效选择"; sleep 1; show_menu ;;
    esac
}

# ================ 1. 全新安装 ================
do_install() {
    banner
    info "全新安装 - 统一 Nginx 入口，后端绑回环，IP 直接访问"
    echo ""

    if [[ "$INSTALLED" == "true" ]]; then
        warn "检测到已安装，重新安装会覆盖配置（data/ 数据保留）"
        read -p "继续？(y/N): " confirm </dev/tty
        confirm=$(echo "$confirm" | tr -d '[:space:]')
        [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { show_menu; return; }
    fi

    # 部署方式
    echo ""
    info "请选择部署方式："
    echo "  1) Docker + Compose（推荐，环境隔离）"
    echo "  2) systemd（不装 Docker，资源最省）"
    read -p "请输入 1 或 2: " method </dev/tty
    method=$(echo "$method" | tr -d '[:space:]')
    case "$method" in
        1) DEPLOY_METHOD="docker" ;;
        2) DEPLOY_METHOD="systemd" ;;
        *) error "无效选择" ;;
    esac
    ok "部署方式: $DEPLOY_METHOD"

    # 对外端口
    echo ""
    local suggest
    suggest=$(pick_free_port "$DEFAULT_PORT")
    info "本项目 Nginx 对外端口（与你现有的 80/443 服务互不干扰）"
    read -p "请输入对外端口 [默认 $suggest]: " input_port </dev/tty
    input_port=$(echo "$input_port" | tr -d '[:space:]')
    WEB_PORT="${input_port:-$suggest}"
    if port_in_use "$WEB_PORT"; then
        warn "端口 $WEB_PORT 已被占用，自动改用 $(pick_free_port "$WEB_PORT")"
        WEB_PORT=$(pick_free_port "$WEB_PORT")
    fi
    ok "对外端口: $WEB_PORT"

    # 存储
    echo ""
    info "参考图存储方式（图生视频需要公网可访问的图片 URL）："
    echo "  1) VPS 本机自托管（推荐，经本项目 Nginx 对外）"
    echo "  2) S3 / 腾讯云 COS（需手动编辑配置文件）"
    echo "  3) 自定义图床（需手动编辑配置文件）"
    read -p "请输入 1/2/3: " storage </dev/tty
    storage=$(echo "$storage" | tr -d '[:space:]')
    case "$storage" in
        1) STORAGE_METHOD="local" ;;
        2) STORAGE_METHOD="s3" ;;
        3) STORAGE_METHOD="custom" ;;
        *) error "无效选择" ;;
    esac
    ok "存储方式: $STORAGE_METHOD"

    # 确认
    echo ""
    warn "即将执行："
    echo "  - 安装依赖（${DEPLOY_METHOD} 所需）"
    echo "  - 拉取代码到 $INSTALL_DIR"
    echo "  - 后端绑回环 127.0.0.1:$BACKEND_PORT"
    echo "  - 启动 Nginx 入口，监听 $WEB_PORT"
    echo ""
    read -p "确认开始？(y/N): " confirm </dev/tty
    confirm=$(echo "$confirm" | tr -d '[:space:]')
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { info "已取消"; show_menu; return; }

    ok "开始安装..."

    clone_repo
    configure_env_basic
    configure_web_config_origin

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        install_docker
        start_docker
    else
        install_systemd_deps
        install_nginx_pkg
        start_systemd
    fi

    INSTALLED="true"
    DOMAIN=""
    save_state
    install_hb_shortcut

    get_server_ip
    echo ""
    ok "========================================="
    ok "安装完成！"
    ok "========================================="
    echo ""
    echo -e "  访问地址: ${GREEN}http://$SERVER_IP:$WEB_PORT${NC}"
    echo ""
    info "后端绑在回环 127.0.0.1:$BACKEND_PORT，外网无法直连，安全。"
    info "准备好域名后，选菜单【2】加 HTTPS（config.js 无需改动）。"
    info "以后管理本服务，终端直接输入：${GREEN}hb${NC}"
    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 2. 配置域名 + HTTPS ================
do_configure_domain() {
    banner
    info "配置域名 + HTTPS（在本项目 Nginx 入口上加证书）"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务（菜单选项 1）"; }

    read -p "请输入你的域名（必须已解析到本机 IP）: " new_domain </dev/tty
    new_domain=$(echo "$new_domain" | tr -d '[:space:]')
    [[ -z "$new_domain" ]] && { warn "域名不能为空"; sleep 1; show_menu; return; }
    DOMAIN="$new_domain"
    ok "域名: $DOMAIN"

    # 证书：检测已有 / 申请 / 手动
    echo ""
    CERT_FULLCHAIN=""
    CERT_KEY=""
    if detect_existing_cert; then
        info "检测到已有证书: $CERT_FULLCHAIN"
        read -p "使用已有证书？(Y/n): " use_existing </dev/tty
        use_existing=$(echo "$use_existing" | tr -d '[:space:]')
        [[ "$use_existing" == "n" || "$use_existing" == "N" ]] && { CERT_FULLCHAIN=""; CERT_KEY=""; }
    fi

    local cert_mode="existing"
    if [[ -z "$CERT_FULLCHAIN" ]]; then
        echo ""
        info "HTTPS 证书获取方式："
        echo "  1) 自动申请 Let's Encrypt（域名必须已解析到本机）"
        echo "  2) 手动放置证书文件"
        read -p "请输入 1 或 2: " cm </dev/tty
        cm=$(echo "$cm" | tr -d '[:space:]')
        case "$cm" in
            1) cert_mode="auto" ;;
            2) cert_mode="manual" ;;
            *) error "无效选择" ;;
        esac
    fi

    # HTTPS 监听端口
    echo ""
    local https_suggest=443
    port_in_use 443 && https_suggest=8443
    info "HTTPS 监听端口（443 被占用可换其他，如 8443）"
    read -p "请输入 HTTPS 端口 [默认 $https_suggest]: " https_port </dev/tty
    https_port=$(echo "$https_port" | tr -d '[:space:]')
    HTTPS_PORT="${https_port:-$https_suggest}"
    ok "HTTPS 端口: $HTTPS_PORT"

    echo ""
    warn "即将执行："
    echo "  - 在 $DOMAIN 上启用 HTTPS（端口 $HTTPS_PORT）"
    [[ "$cert_mode" == "auto" ]] && echo "  - 自动申请 Let's Encrypt 证书（临时占用 80 端口验证）"
    [[ "$cert_mode" == "existing" ]] && echo "  - 使用已有证书 $CERT_FULLCHAIN"
    echo "  - 前端 config.js 无需改动（同源）"
    echo ""
    read -p "确认继续？(y/N): " confirm </dev/tty
    confirm=$(echo "$confirm" | tr -d '[:space:]')
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { info "已取消"; show_menu; return; }

    cd "$INSTALL_DIR"

    # 申请证书
    if [[ "$cert_mode" == "auto" ]]; then
        request_cert_letsencrypt || { warn "证书申请失败，返回菜单"; sleep 2; show_menu; return; }
        CERT_FULLCHAIN="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
        CERT_KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
    elif [[ "$cert_mode" == "manual" ]]; then
        mkdir -p "$INSTALL_DIR/deploy/certs"
        warn "请把证书放到："
        echo "    $INSTALL_DIR/deploy/certs/fullchain.pem"
        echo "    $INSTALL_DIR/deploy/certs/privkey.pem"
        read -p "放置完成后按回车继续..." </dev/tty
        CERT_FULLCHAIN="$INSTALL_DIR/deploy/certs/fullchain.pem"
        CERT_KEY="$INSTALL_DIR/deploy/certs/privkey.pem"
        [[ -f "$CERT_FULLCHAIN" && -f "$CERT_KEY" ]] || { warn "未找到证书文件"; sleep 2; show_menu; return; }
    fi

    # 更新存储公网地址为 https 域名
    configure_env_domain

    # 应用 HTTPS 配置
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        apply_https_docker
    else
        apply_https_systemd
    fi

    save_state

    echo ""
    ok "========================================="
    ok "HTTPS 配置完成！"
    ok "========================================="
    echo ""
    local url="https://$DOMAIN"
    [[ "$HTTPS_PORT" != "443" ]] && url="https://$DOMAIN:$HTTPS_PORT"
    echo -e "  访问地址: ${GREEN}$url${NC}"
    echo ""
    warn "安全提醒：建议在 Nginx 层加访问控制（IP 白名单 / basic auth）"
    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 3. 修改对外端口 ================
do_change_port() {
    banner
    info "修改对外端口"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务（菜单选项 1）"; }

    echo "当前端口: ${CYAN}$WEB_PORT${NC}"
    read -p "请输入新端口: " new_port </dev/tty
    new_port=$(echo "$new_port" | tr -d '[:space:]')
    [[ -z "$new_port" ]] && { warn "端口不能为空"; sleep 1; show_menu; return; }
    if port_in_use "$new_port"; then
        warn "端口 $new_port 已被占用"
        sleep 2; show_menu; return
    fi

    WEB_PORT="$new_port"
    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        regen_compose
        docker compose -f deploy/docker-compose.libai.yml up -d
    else
        regen_nginx_http_systemd
        nginx -t && systemctl reload nginx
    fi

    save_state
    get_server_ip
    ok "端口已改为 $WEB_PORT"
    echo -e "  访问地址: ${GREEN}http://$SERVER_IP:$WEB_PORT${NC}"
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 4. 修改存储方式 ================
do_configure_storage() {
    banner
    info "修改存储方式"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务（菜单选项 1）"; }

    echo -e "当前存储方式: ${CYAN}${STORAGE_METHOD:-未设置}${NC}"
    echo ""
    echo "  1) VPS 本机自托管"
    echo "  2) S3 / 腾讯云 COS"
    echo "  3) 自定义图床"
    read -p "请选择 [1-3]: " storage </dev/tty
    storage=$(echo "$storage" | tr -d '[:space:]')
    case "$storage" in
        1) STORAGE_METHOD="local" ;;
        2) STORAGE_METHOD="s3" ;;
        3) STORAGE_METHOD="custom" ;;
        *) warn "无效选择"; sleep 1; show_menu; return ;;
    esac

    cd "$INSTALL_DIR"

    if [[ "$STORAGE_METHOD" == "local" ]]; then
        set_public_base_url
        ok "本机自托管已配置，公网地址: $PUBLIC_BASE_URL"
    else
        warn "请手动编辑 $INSTALL_DIR/.env.deploy 填写 S3/图床凭据"
        read -p "编辑完成后按回车继续..." </dev/tty
    fi

    restart_backend
    save_state
    ok "存储方式已更新并重启后端"
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 5. 查看状态 ================
do_status() {
    banner
    info "服务状态"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { warn "未检测到安装"; read -p "按回车返回..." </dev/tty; show_menu; return; }

    cd "$INSTALL_DIR"
    get_server_ip

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose -f deploy/docker-compose.libai.yml ps
    else
        echo "— 后端 —"
        systemctl status libai-backend --no-pager -l | head -8 || true
        echo ""
        echo "— Nginx —"
        systemctl status nginx --no-pager -l | head -6 || true
    fi

    echo ""
    echo -e "  对外端口: ${CYAN}$WEB_PORT${NC}"
    echo -e "  访问地址: ${GREEN}http://$SERVER_IP:$WEB_PORT${NC}"
    [[ -n "$DOMAIN" ]] && echo -e "  域名访问: ${GREEN}https://$DOMAIN${NC}"
    echo ""
    info "后端健康检查："
    curl -s --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" && echo "" || warn "后端无响应"
    echo ""
    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 6. 重启 ================
do_restart() {
    banner
    info "重启服务"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务"; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose -f deploy/docker-compose.libai.yml restart
        ok "Docker 服务已重启"
    else
        systemctl restart libai-backend
        systemctl reload nginx 2>/dev/null || true
        ok "服务已重启"
    fi

    read -p "按回车返回主菜单..." </dev/tty
    show_menu
}

# ================ 7. 日志 ================
do_logs() {
    banner
    info "查看日志（Ctrl+C 退出）"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { error "请先安装服务"; }

    cd "$INSTALL_DIR"

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose -f deploy/docker-compose.libai.yml logs -f --tail=100
    else
        journalctl -u libai-backend -f -n 100
    fi

    show_menu
}

# ================ 8. 卸载 ================
do_uninstall() {
    banner
    warn "卸载服务"
    echo ""

    [[ "$INSTALLED" != "true" ]] && { warn "未检测到安装"; read -p "按回车返回..." </dev/tty; show_menu; return; }

    warn "将执行："
    echo "  - 停止并删除本项目服务（不影响你其他的 Nginx/容器）"
    echo "  - 删除 $INSTALL_DIR（含 data/，请提前备份）"
    echo ""
    read -p "确认卸载？(输入 yes): " confirm </dev/tty
    [[ "$confirm" != "yes" ]] && { info "已取消"; sleep 1; show_menu; return; }

    cd "$INSTALL_DIR" 2>/dev/null || true

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose -f deploy/docker-compose.libai.yml down -v 2>/dev/null || true
    else
        systemctl stop libai-backend 2>/dev/null || true
        systemctl disable libai-backend 2>/dev/null || true
        rm -f /etc/systemd/system/libai-backend.service
        systemctl daemon-reload
        rm -f /etc/nginx/sites-enabled/libai.conf /etc/nginx/sites-available/libai.conf
        rm -f /etc/nginx/snippets/_libai_proxy.inc
        systemctl reload nginx 2>/dev/null || true
    fi

    # 清理 hb 快捷命令
    rm -f /usr/local/bin/hb 2>/dev/null || true

    rm -rf "$INSTALL_DIR"
    ok "卸载完成"
    exit 0
}

# ============================================================
#                       工具函数
# ============================================================

clone_repo() {
    info "拉取代码到 $INSTALL_DIR..."
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        cd "$INSTALL_DIR" && git pull --rebase 2>/dev/null || warn "git pull 失败，使用现有代码"
    else
        rm -rf "$INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR" || error "git clone 失败，请检查网络"
    fi
    cd "$INSTALL_DIR" || error "无法进入 $INSTALL_DIR"
    ok "代码准备完成"
}

# 计算自托管公网 URL（供上游拉参考图）
set_public_base_url() {
    cd "$INSTALL_DIR"
    if [[ -n "$DOMAIN" ]]; then
        if [[ -n "$HTTPS_PORT" && "$HTTPS_PORT" != "443" ]]; then
            PUBLIC_BASE_URL="https://$DOMAIN:$HTTPS_PORT"
        else
            PUBLIC_BASE_URL="https://$DOMAIN"
        fi
    else
        get_server_ip
        PUBLIC_BASE_URL="http://$SERVER_IP:$WEB_PORT"
    fi
    if grep -q '^LIBAI_PUBLIC_BASE_URL=' .env.deploy; then
        sed -i "s|^LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=$PUBLIC_BASE_URL|" .env.deploy
    else
        echo "LIBAI_PUBLIC_BASE_URL=$PUBLIC_BASE_URL" >> .env.deploy
    fi
}

configure_env_basic() {
    info "配置环境变量..."
    cd "$INSTALL_DIR"
    [[ -f .env.deploy ]] || cp .env.deploy.example .env.deploy

    # 后端监听地址：
    #   - Docker：绑 0.0.0.0（容器网络隔离已保证外网碰不到，nginx 容器需经 backend:端口 访问）
    #   - systemd：绑 127.0.0.1（回环，外网无法直连，只能经 Nginx）
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        set_env_var LIBAI_BACKEND_HOST 0.0.0.0
    else
        set_env_var LIBAI_BACKEND_HOST 127.0.0.1
    fi
    set_env_var LIBAI_BACKEND_PORT "$BACKEND_PORT"

    # 同源反代，CORS 不需要
    set_env_var LIBAI_CORS_ORIGINS ""

    # systemd 模式：删除 Docker 容器内路径，用默认 ./data
    if [[ "$DEPLOY_METHOD" == "systemd" ]]; then
        sed -i '/^LIBAI_APP_DATA_DIR=/d' .env.deploy
        sed -i '/^# *LIBAI_APP_DATA_DIR=/d' .env.deploy
    fi

    # 存储公网地址
    if [[ "$STORAGE_METHOD" == "local" ]]; then
        set_public_base_url
    fi

    ok "环境变量配置完成"
}

configure_env_domain() {
    info "更新存储公网地址为域名..."
    cd "$INSTALL_DIR"
    [[ "$STORAGE_METHOD" == "local" ]] && set_public_base_url
    ok "已更新"
}

# 设置 .env.deploy 里的键值（存在则替换，否则追加）
set_env_var() {
    local key=$1 val=$2
    cd "$INSTALL_DIR"
    if grep -q "^${key}=" .env.deploy; then
        sed -i "s|^${key}=.*|${key}=${val}|" .env.deploy
    else
        echo "${key}=${val}" >> .env.deploy
    fi
}

# 前端永远用同源：window.location.origin
configure_web_config_origin() {
    info "配置前端为同源访问..."
    cd "$INSTALL_DIR"
    sed -i 's|window.__LIBAI_BACKEND_BASE_URL__ = .*|window.__LIBAI_BACKEND_BASE_URL__ = window.location.origin;|' web/config.js
    ok "前端配置完成（同源，IP/域名自动适配）"
}

# ---------- 依赖安装 ----------
install_docker() {
    info "检查 Docker..."
    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        ok "Docker 已就绪"
        return
    fi
    info "安装 Docker..."
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    local distro
    distro=$(lsb_release -is 2>/dev/null | tr '[:upper:]' '[:lower:]')
    [[ "$distro" != "ubuntu" && "$distro" != "debian" ]] && distro="debian"
    curl -fsSL "https://download.docker.com/linux/$distro/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$distro $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
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

install_nginx_pkg() {
    if command -v nginx &>/dev/null; then
        ok "Nginx 已安装"
        return
    fi
    info "安装 Nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx
    systemctl enable nginx
    ok "Nginx 安装完成"
}

# ---------- Docker 启动 ----------
regen_compose() {
    cd "$INSTALL_DIR"
    sed -e "s|__WEB_PORT__|$WEB_PORT|g" \
        -e "s|__BACKEND_PORT__|$BACKEND_PORT|g" \
        deploy/docker-compose.libai.yml.tpl > deploy/docker-compose.libai.yml
}

start_docker() {
    info "启动 Docker（后端回环 + Nginx 入口 $WEB_PORT）..."
    cd "$INSTALL_DIR"

    # 生成运行时 nginx 配置（HTTP，容器内监听 80）
    gen_nginx_http_docker

    # 生成 compose
    regen_compose

    # 清理可能的旧容器
    docker compose -f deploy/docker-compose.libai.yml down 2>/dev/null || true

    docker compose -f deploy/docker-compose.libai.yml up -d --build
    ok "Docker 服务已启动"
}

# 生成 Docker 版运行时 nginx 配置
gen_nginx_http_docker() {
    cd "$INSTALL_DIR"
    mkdir -p deploy/nginx/runtime
    sed -e "s|__LISTEN__|80|g" \
        -e "s|__SERVER_NAME__|_|g" \
        -e "s|__ROOT__|/usr/share/nginx/html|g" \
        -e "s|__BACKEND__|backend:$BACKEND_PORT|g" \
        -e "s|__INCLUDE__|/etc/nginx/conf.d/_libai_proxy.inc|g" \
        deploy/nginx/libai.http.conf > deploy/nginx/runtime/libai.conf
    cp deploy/nginx/_libai_proxy.inc deploy/nginx/runtime/_libai_proxy.inc
}

# ---------- systemd 启动 ----------
start_systemd() {
    info "安装 Python 依赖..."
    cd "$INSTALL_DIR"
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt

    info "创建 libai 用户与数据目录..."
    useradd -r -s /usr/sbin/nologin libai 2>/dev/null || true
    mkdir -p "$INSTALL_DIR/data"
    chown -R libai:libai "$INSTALL_DIR/data"

    info "安装后端 systemd 服务..."
    sed "s|/opt/libai-canvas-web|$INSTALL_DIR|g" deploy/systemd/libai-backend.service \
        > /etc/systemd/system/libai-backend.service
    systemctl daemon-reload
    systemctl enable --now libai-backend

    info "等待后端启动..."
    sleep 3
    if systemctl is-active --quiet libai-backend; then
        ok "后端已启动（回环 127.0.0.1:$BACKEND_PORT）"
    else
        warn "后端启动异常，最近日志："
        journalctl -u libai-backend -n 15 --no-pager || true
    fi

    info "配置 Nginx 入口（端口 $WEB_PORT）..."
    regen_nginx_http_systemd
    nginx -t && systemctl reload nginx
    ok "Nginx 入口已就绪"
}

# 生成/更新 systemd 版 HTTP nginx 配置
regen_nginx_http_systemd() {
    cd "$INSTALL_DIR"
    cp deploy/nginx/_libai_proxy.host.inc /etc/nginx/snippets/_libai_proxy.inc
    sed -e "s|__LISTEN__|$WEB_PORT|g" \
        -e "s|__SERVER_NAME__|_|g" \
        -e "s|__ROOT__|$INSTALL_DIR/web|g" \
        -e "s|__BACKEND__|127.0.0.1:$BACKEND_PORT|g" \
        -e "s|__INCLUDE__|/etc/nginx/snippets/_libai_proxy.inc|g" \
        deploy/nginx/libai.http.conf > /etc/nginx/sites-available/libai.conf
    ln -sf /etc/nginx/sites-available/libai.conf /etc/nginx/sites-enabled/libai.conf
}

# ---------- HTTPS ----------
detect_existing_cert() {
    local paths=(
        "/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
        "/etc/nginx/ssl/$DOMAIN/fullchain.pem"
        "/etc/ssl/$DOMAIN/fullchain.pem"
        "$INSTALL_DIR/deploy/certs/fullchain.pem"
    )
    for p in "${paths[@]}"; do
        if [[ -f "$p" ]]; then
            CERT_FULLCHAIN="$p"
            CERT_KEY="${p/fullchain.pem/privkey.pem}"
            [[ -f "$CERT_KEY" ]] && return 0
        fi
    done
    return 1
}

request_cert_letsencrypt() {
    info "申请 Let's Encrypt 证书（standalone，临时占用 80）..."
    command -v certbot &>/dev/null || apt-get install -y -qq certbot

    if port_in_use 80; then
        warn "80 端口被占用，无法用 standalone 模式申请证书"
        warn "请释放 80 端口后重试，或选手动放置证书"
        return 1
    fi

    certbot certonly --standalone \
        -d "$DOMAIN" \
        --email "admin@$DOMAIN" \
        --agree-tos --non-interactive || return 1
    ok "证书申请完成"
    return 0
}

apply_https_systemd() {
    info "应用 HTTPS 配置（systemd Nginx）..."
    cd "$INSTALL_DIR"
    cp deploy/nginx/_libai_proxy.host.inc /etc/nginx/snippets/_libai_proxy.inc
    gen_https_conf "$INSTALL_DIR/web" "127.0.0.1:$BACKEND_PORT" "/etc/nginx/snippets/_libai_proxy.inc" \
        > /etc/nginx/sites-available/libai.conf
    ln -sf /etc/nginx/sites-available/libai.conf /etc/nginx/sites-enabled/libai.conf
    nginx -t && systemctl reload nginx || error "Nginx 配置错误"
    ok "HTTPS 已启用"
}

apply_https_docker() {
    info "应用 HTTPS 配置（Docker Nginx）..."
    cd "$INSTALL_DIR"
    mkdir -p deploy/nginx/runtime deploy/certs

    # 证书拷进项目目录供容器挂载
    if [[ "$CERT_FULLCHAIN" != "$INSTALL_DIR/deploy/certs/fullchain.pem" ]]; then
        cp "$CERT_FULLCHAIN" deploy/certs/fullchain.pem
        cp "$CERT_KEY" deploy/certs/privkey.pem
    fi

    cp deploy/nginx/_libai_proxy.inc deploy/nginx/runtime/_libai_proxy.inc
    gen_https_conf "/usr/share/nginx/html" "backend:$BACKEND_PORT" "/etc/nginx/conf.d/_libai_proxy.inc" \
        > deploy/nginx/runtime/libai.conf

    regen_compose
    docker compose -f deploy/docker-compose.libai.yml up -d
    ok "HTTPS 已启用"
}

# 生成 HTTPS nginx 配置到 stdout
# $1=root  $2=backend  $3=include路径
gen_https_conf() {
    local root=$1 backend=$2 inc=$3
    local cert_path key_path
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        cert_path="/etc/nginx/certs/fullchain.pem"
        key_path="/etc/nginx/certs/privkey.pem"
    else
        cert_path="$CERT_FULLCHAIN"
        key_path="$CERT_KEY"
    fi

    cat <<EOF
# HTTP：跳转 HTTPS（同时保留 acme 校验目录）
server {
    listen ${WEB_PORT};
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root ${root}; }
    return 301 https://\$host:${HTTPS_PORT}\$request_uri;
}

server {
    listen ${HTTPS_PORT} ssl;
    http2 on;
    server_name ${DOMAIN};

    ssl_certificate     ${cert_path};
    ssl_certificate_key ${key_path};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    root ${root};
    index index.html;
    client_max_body_size 200m;

    location ~ ^/(health|jobs|newapi|providers|provider-models|projects|history|prompts|media|jianying|desktop-announcements|design-space)(/|\$) {
        proxy_pass http://${backend};
        include ${inc};
    }
    location = /assets        { proxy_pass http://${backend}; include ${inc}; }
    location = /assets/write  { proxy_pass http://${backend}; include ${inc}; }
    location = /assets/import { proxy_pass http://${backend}; include ${inc}; }
    location ~ ^/assets/[^/]+\$ {
        if (\$uri ~* \.(js|mjs|css|png|jpg|jpeg|gif|svg|webp|ico|woff2?|ttf|map)\$) { break; }
        proxy_pass http://${backend};
        include ${inc};
    }
    location ~ ^/assets/[^/]+/(thumb|delete|promote)\$ {
        proxy_pass http://${backend};
        include ${inc};
    }
    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF
}

restart_backend() {
    cd "$INSTALL_DIR"
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        docker compose -f deploy/docker-compose.libai.yml restart backend
    else
        systemctl restart libai-backend
    fi
}

# ---- 安装 hb 快捷命令 ----
install_hb_shortcut() {
    local target="/usr/local/bin/hb"
    cat > "$target" <<EOF
#!/usr/bin/env bash
# 漫创AI Web 管理脚本快捷入口（由 manage.sh 自动生成）
# 自动提权：非 root 时用 sudo 重新执行
if [ "\$(id -u)" -ne 0 ]; then
    exec sudo bash "$INSTALL_DIR/manage.sh" "\$@"
fi
exec bash "$INSTALL_DIR/manage.sh" "\$@"
EOF
    chmod +x "$target" 2>/dev/null || true
    ok "已创建快捷命令：终端输入 hb 即可打开本管理脚本"
}

# ========== 入口 ==========
main() {
    check_root
    check_system
    load_state
    show_menu
}

main "$@"

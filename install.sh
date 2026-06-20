#!/usr/bin/env bash
# ============================================================
# 漫创AI Web · 一键安装部署脚本（Ubuntu/Debian）
# ------------------------------------------------------------
# 交互式引导，小白友好：问答 → 自动装依赖 → 配置 → 启动
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/JUUCHEN1/ainew/main/install.sh | bash
#   或：wget -qO- https://raw.githubusercontent.com/JUUCHEN1/ainew/main/install.sh | bash
#   或：git clone <仓库> && cd libai-canvas-web && bash install.sh
# ============================================================
set -e

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

banner() {
    echo -e "${GREEN}"
    cat <<'EOF'
╔═══════════════════════════════════════════════════════════╗
║     漫创AI Web · 一键安装部署脚本 (Ubuntu/Debian)        ║
╚═══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# ---- 检查权限 ----
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "请用 root 权限运行: sudo bash install.sh"
    fi
}

# ---- 检查系统 ----
check_system() {
    if ! command -v apt-get &>/dev/null; then
        error "仅支持 Ubuntu/Debian (apt)。检测到不支持的系统。"
    fi
    info "检测到系统: $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d'"' -f2)"
}

# ---- 询问部署方式 ----
ask_deploy_method() {
    echo ""
    info "请选择部署方式："
    echo "  1) Docker + Compose（推荐小白，一条命令起，环境隔离）"
    echo "  2) systemd + Nginx（不装 Docker，资源最省）"
    read -p "请输入 1 或 2: " DEPLOY_METHOD </dev/tty
    case "$DEPLOY_METHOD" in
        1) DEPLOY_METHOD="docker" ;;
        2) DEPLOY_METHOD="systemd" ;;
        *) error "无效选择" ;;
    esac
    ok "部署方式: $DEPLOY_METHOD"
}

# ---- 询问域名 ----
ask_domain() {
    echo ""
    read -p "请输入你的域名（例 ai.example.com，必须已解析到本机 IP）: " DOMAIN </dev/tty
    [[ -z "$DOMAIN" ]] && error "域名不能为空"
    ok "域名: $DOMAIN"
}

# ---- 询问证书方式 ----
ask_cert_method() {
    echo ""
    info "HTTPS 证书获取方式："
    echo "  1) 自动申请 Let's Encrypt（推荐，域名必须已解析到本机 IP）"
    echo "  2) 手动放置（稍后把 fullchain.pem 和 privkey.pem 放到指定目录）"
    read -p "请输入 1 或 2: " CERT_METHOD </dev/tty
    case "$CERT_METHOD" in
        1) CERT_METHOD="auto" ;;
        2) CERT_METHOD="manual" ;;
        *) error "无效选择" ;;
    esac
    ok "证书方式: $CERT_METHOD"
}

# ---- 询问存储方式 ----
ask_storage() {
    echo ""
    info "参考图存储方式（图生视频等功能需要公网可访问的图片 URL）："
    echo "  1) VPS 本机自托管（推荐，不依赖外部图床）"
    echo "  2) S3 / 腾讯云 COS（需填写凭据）"
    echo "  3) 自定义图床（需填写上传接口 URL）"
    read -p "请输入 1/2/3: " STORAGE_METHOD </dev/tty
    case "$STORAGE_METHOD" in
        1) STORAGE_METHOD="local" ;;
        2) STORAGE_METHOD="s3" ;;
        3) STORAGE_METHOD="custom" ;;
        *) error "无效选择" ;;
    esac
    ok "存储方式: $STORAGE_METHOD"
}

# ---- 确认开始 ----
confirm() {
    echo ""
    warn "即将开始安装，将执行以下操作："
    echo "  - 安装系统依赖（Docker 或 Python/ffmpeg/nginx）"
    echo "  - 拉取代码到 /opt/libai-canvas-web"
    echo "  - 配置环境变量、Nginx、证书"
    echo "  - 启动服务"
    echo ""
    read -p "确认开始？(y/N): " CONFIRM </dev/tty
    # 去除前后空格
    CONFIRM=$(echo "$CONFIRM" | tr -d '[:space:]')
    if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
        info "已取消"
        exit 0
    fi
    ok "开始安装..."
}

# ---- 安装 Docker ----
install_docker() {
    info "安装 Docker..."
    if command -v docker &>/dev/null && command -v docker compose &>/dev/null; then
        ok "Docker 已安装"
        return
    fi
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

# ---- 安装 systemd 依赖 ----
install_systemd_deps() {
    info "安装 Python / ffmpeg / nginx / certbot..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip ffmpeg nginx certbot python3-certbot-nginx git curl
    ok "依赖安装完成"
}

# ---- 拉代码 ----
clone_repo() {
    info "拉取代码到 /opt/libai-canvas-web..."
    if [[ -d /opt/libai-canvas-web/.git ]]; then
        warn "目录已存在，拉取最新代码..."
        cd /opt/libai-canvas-web && git pull || error "git pull 失败"
    else
        rm -rf /opt/libai-canvas-web
        git clone https://github.com/JUUCHEN1/ainew.git /opt/libai-canvas-web || error "git clone 失败，请检查网络连接"
    fi
    cd /opt/libai-canvas-web || error "无法进入 /opt/libai-canvas-web 目录"
    ok "代码准备完成"
}

# ---- 配置环境变量 ----
configure_env() {
    info "配置环境变量..."
    cp -n .env.deploy.example .env.deploy || true

    # 填域名
    sed -i "s|LIBAI_PUBLIC_BASE_URL=.*|LIBAI_PUBLIC_BASE_URL=https://$DOMAIN|" .env.deploy
    sed -i "s|LIBAI_CORS_ORIGINS=.*|LIBAI_CORS_ORIGINS=https://$DOMAIN|" .env.deploy

    # 存储配置
    if [[ "$STORAGE_METHOD" == "s3" ]]; then
        warn "S3 配置需手动填写，请编辑 /opt/libai-canvas-web/.env.deploy"
    elif [[ "$STORAGE_METHOD" == "custom" ]]; then
        warn "自定义图床需手动填写，请编辑 /opt/libai-canvas-web/.env.deploy"
    fi

    ok "环境变量配置完成"
}

# ---- 配置前端后端地址 ----
configure_web_config() {
    info "配置前端后端地址为同源..."
    sed -i 's|window.__LIBAI_BACKEND_BASE_URL__ = .*|window.__LIBAI_BACKEND_BASE_URL__ = window.location.origin;|' web/config.js
    ok "前端配置完成"
}

# ---- 配置 Nginx (Docker) ----
configure_nginx_docker() {
    info "配置 Nginx（Docker 版）..."
    sed -i "s|your-domain.com|$DOMAIN|g" deploy/nginx/libai.conf
    mkdir -p deploy/certs
    ok "Nginx 配置完成"
}

# ---- 配置 Nginx (systemd) ----
configure_nginx_systemd() {
    info "配置 Nginx（systemd 版）..."
    sed -i "s|your-domain.com|$DOMAIN|g" deploy/nginx/libai.systemd.conf
    sed -i "s|/opt/libai-canvas-web/web|$(pwd)/web|g" deploy/nginx/libai.systemd.conf
    cp deploy/nginx/libai.systemd.conf /etc/nginx/sites-available/libai.conf
    cp deploy/nginx/_libai_proxy.host.inc /etc/nginx/snippets/_libai_proxy.inc
    ln -sf /etc/nginx/sites-available/libai.conf /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    ok "Nginx 配置完成"
}

# ---- 申请证书 (Docker) ----
request_cert_docker() {
    if [[ "$CERT_METHOD" == "manual" ]]; then
        warn "请手动放置证书到 /opt/libai-canvas-web/deploy/certs/fullchain.pem 和 privkey.pem"
        read -p "放置完成后按回车继续..." </dev/tty
        return
    fi

    info "申请 Let's Encrypt 证书（需要域名已解析到本机）..."
    # 先临时用 80 端口跑 webroot
    docker run --rm -p 80:80 -v /opt/libai-canvas-web/web:/usr/share/nginx/html:ro nginx:alpine &
    NGINX_PID=$!
    sleep 3

    docker run --rm \
        -v /opt/libai-canvas-web/deploy/certs:/etc/letsencrypt \
        -v /opt/libai-canvas-web/web:/usr/share/nginx/html \
        certbot/certbot certonly --webroot \
        -w /usr/share/nginx/html \
        -d "$DOMAIN" \
        --email "admin@$DOMAIN" \
        --agree-tos \
        --non-interactive || error "证书申请失败"

    kill $NGINX_PID 2>/dev/null || true

    # 复制证书到 deploy/certs
    cp /opt/libai-canvas-web/deploy/certs/live/$DOMAIN/fullchain.pem /opt/libai-canvas-web/deploy/certs/
    cp /opt/libai-canvas-web/deploy/certs/live/$DOMAIN/privkey.pem /opt/libai-canvas-web/deploy/certs/

    ok "证书申请完成"
}

# ---- 申请证书 (systemd) ----
request_cert_systemd() {
    if [[ "$CERT_METHOD" == "manual" ]]; then
        warn "请手动放置证书，然后在 /etc/nginx/sites-available/libai.conf 里配置路径"
        read -p "配置完成后按回车继续..." </dev/tty
        return
    fi

    info "申请 Let's Encrypt 证书..."
    certbot --nginx -d "$DOMAIN" --email "admin@$DOMAIN" --agree-tos --non-interactive --redirect || error "证书申请失败"
    ok "证书申请完成"
}

# ---- 启动 Docker ----
start_docker() {
    info "启动 Docker 服务..."
    cd /opt/libai-canvas-web
    docker compose up -d --build
    ok "Docker 服务启动完成"
}

# ---- 启动 systemd ----
start_systemd() {
    info "安装 Python 依赖..."
    cd /opt/libai-canvas-web
    python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements.txt

    info "创建 libai 用户..."
    useradd -r -s /usr/sbin/nologin libai 2>/dev/null || true
    chown -R libai:libai /opt/libai-canvas-web/data

    info "安装 systemd 服务..."
    sed -i "s|/opt/libai-canvas-web|$(pwd)|g" deploy/systemd/libai-backend.service
    cp deploy/systemd/libai-backend.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now libai-backend

    info "启动 Nginx..."
    nginx -t || error "Nginx 配置错误"
    systemctl enable --now nginx

    ok "服务启动完成"
}

# ---- 验证部署 ----
verify() {
    echo ""
    info "正在验证部署..."
    sleep 3

    if curl -fsSL -k "https://$DOMAIN/health" >/dev/null 2>&1; then
        ok "后端健康检查通过"
    else
        warn "后端健康检查失败，请查看日志"
    fi

    if curl -fsSL -k -I "https://$DOMAIN/" | grep -q "200\|301\|302"; then
        ok "前端访问正常"
    else
        warn "前端访问失败"
    fi

    echo ""
    ok "========================================="
    ok "部署完成！"
    ok "========================================="
    echo ""
    echo -e "  访问地址: ${GREEN}https://$DOMAIN${NC}"
    echo ""
    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        echo "  查看日志: docker compose logs -f"
        echo "  重启服务: docker compose restart"
        echo "  停止服务: docker compose down"
    else
        echo "  后端日志: journalctl -u libai-backend -f"
        echo "  重启后端: systemctl restart libai-backend"
        echo "  重启 Nginx: systemctl restart nginx"
    fi
    echo ""
    warn "安全提醒: 建议在 Nginx 层加访问控制（IP 白名单 / basic auth）"
    echo ""
}

# ---- 主流程 ----
main() {
    banner
    check_root
    check_system

    ask_deploy_method
    ask_domain
    ask_cert_method
    ask_storage

    confirm

    clone_repo
    configure_env
    configure_web_config

    if [[ "$DEPLOY_METHOD" == "docker" ]]; then
        install_docker
        configure_nginx_docker
        request_cert_docker
        start_docker
    else
        install_systemd_deps
        configure_nginx_systemd
        request_cert_systemd
        start_systemd
    fi

    verify
}

main "$@"

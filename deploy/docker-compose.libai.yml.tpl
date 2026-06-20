# ============================================================
# 漫创AI Web · Docker 统一入口编排模板（manage.sh 专用）
# ------------------------------------------------------------
# manage.sh 会把本模板渲染为 docker-compose.libai.yml 后启动：
#   sed 's|__WEB_PORT__|...|; s|__BACKEND_PORT__|...|' \
#       deploy/docker-compose.libai.yml.tpl > deploy/docker-compose.libai.yml
#   docker compose -f deploy/docker-compose.libai.yml up -d --build
#
# 与仓库根目录的 docker-compose.yml 区别：
#   - backend 只发布到回环 127.0.0.1:__BACKEND_PORT__，外网碰不到
#   - nginx 监听自定义端口 __WEB_PORT__，不占用 80/443，
#     与已有的其它 nginx / 反代互不干扰
#   - nginx 用运行时渲染好的 HTTP 配置（前端静态 + 后端 API 反代）
# ============================================================
services:
  backend:
    build: .
    image: libai-web-backend:latest
    container_name: libai-backend
    restart: unless-stopped
    env_file:
      - .env.deploy
    volumes:
      - ./data:/app/data
    expose:
      - "__BACKEND_PORT__"
    # 仅回环暴露，便于宿主机调试；对外一律走 nginx
    ports:
      - "127.0.0.1:__BACKEND_PORT__:__BACKEND_PORT__"

  nginx:
    image: nginx:1.27-alpine
    container_name: libai-nginx
    restart: unless-stopped
    depends_on:
      - backend
    ports:
      # 自定义对外端口 → 容器内 80
      - "__WEB_PORT__:80"
    volumes:
      # 运行时渲染好的 nginx 配置（前端静态 + 后端 API 反代）
      - ./deploy/nginx/runtime/libai.conf:/etc/nginx/conf.d/default.conf:ro
      - ./deploy/nginx/runtime/_libai_proxy.inc:/etc/nginx/conf.d/_libai_proxy.inc:ro
      # 前端静态文件
      - ./web:/usr/share/nginx/html:ro

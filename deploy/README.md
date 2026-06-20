# 漫创AI Web · 云 VPS 部署指南

这个项目部署到云 VPS 很轻量：纯静态前端 + FastAPI 后端 + SQLite 单文件，
**没有外部数据库、没有 Redis、前端无需构建**。本文给两种部署方式，任选其一。

---

## 0. 架构总览

```
              Internet (HTTPS)
                    │
              ┌─────▼─────┐
              │   Nginx   │  443 入口 / 证书 / 反向代理
              └─────┬─────┘
         ┌──────────┴───────────┐
   /  (前端静态 web/)      已知 API 前缀 → 后端 :8765
                                 │
                           data/ (SQLite + 参考图，持久化)
```

- 前端 `web/`：纯静态，Nginx 直接托管。
- 后端 `server/`：FastAPI，监听 8765，只在内网/回环暴露，对外一律走 Nginx。
- 数据 `data/`：SQLite、参考图自托管目录、项目数据，需持久化与备份。

---

## 1. 部署前必改的 3 处

### (1) 前端后端地址 `web/config.js`
同域反代部署时改为：
```js
window.__LIBAI_BACKEND_BASE_URL__ = window.location.origin;
```
（独立 API 子域时写 `"https://api.your-domain.com"`。）

### (2) CORS 白名单
通过环境变量 `LIBAI_CORS_ORIGINS` 设置（见 `.env.deploy`），填你的前端域名。
同域反代时浏览器请求同源，可不填；跨域/独立子域必须填。

### (3) 参考图存储（图生视频等功能必需）
上游模型需要一个**公网可访问的图片 URL**。三选一，在 `.env.deploy` 配置：

| 方案 | 配置项 | 说明 |
|------|--------|------|
| **A 本机自托管**（推荐） | `LIBAI_PUBLIC_BASE_URL=https://your-domain.com` | 图存 VPS，经 `/media/references/` 对外，不依赖外部图床 |
| **B S3 / 腾讯云 COS** | `LIBAI_REFERENCE_STORAGE_*` 一组 | 上传对象存储取公网 URL |
| **C 自定义图床** | `LIBAI_REFERENCE_IMAGE_UPLOAD_URL` | POST multipart(file)，返回含 url 的 JSON |

> 方案 A 的 `LIBAI_PUBLIC_BASE_URL` 必须是**上游能访问到的公网地址**，
> 且 Nginx 能把 `/media/references/` 反代到后端 8765（本文 Nginx 配置已包含）。

---

## 2. 域名与 HTTPS（两种方式通用）

1. 域名 A 记录指向 VPS 公网 IP。
2. 开放安全组/防火墙 80、443。
3. 用 Let's Encrypt 签证书（下面各方式给出对应做法）。

把所有配置文件里的 `your-domain.com` 全部替换成你的真实域名。

---

## 方式一：Docker + Compose（推荐）

需要：VPS 装好 `docker` 和 `docker compose`。

```bash
# 1. 拉代码到 VPS
git clone <你的仓库> libai-canvas-web && cd libai-canvas-web

# 2. 准备环境变量
cp .env.deploy.example .env.deploy
vim .env.deploy            # 填域名、CORS、存储方案

# 3. 改前端后端地址（同域）
#    web/config.js -> window.location.origin
vim web/config.js

# 4. 改 Nginx 配置里的域名
sed -i 's/your-domain.com/真实域名/g' deploy/nginx/libai.conf

# 5. 放证书（二选一）
#    a) 已有证书：拷到 deploy/certs/fullchain.pem 和 privkey.pem
#    b) 用 certbot 签发（先临时用 HTTP 跑起来再签，见下）
mkdir -p deploy/certs

# 6. 启动
docker compose up -d --build
docker compose logs -f          # 看日志
```

数据持久化：`docker-compose.yml` 已把 `./data` 挂进容器，SQLite 和参考图都在宿主机 `data/`，备份直接打包该目录即可。

证书签发（没有现成证书时）：先把 `libai.conf` 里 443 段和 80 段的 `return 301` 注释掉、用 80 端口跑通，再用 certbot：
```bash
docker run --rm -v $PWD/deploy/certs:/etc/letsencrypt \
  -v $PWD/web:/usr/share/nginx/html \
  certbot/certbot certonly --webroot -w /usr/share/nginx/html -d 真实域名
```
签好后把证书路径对到 `deploy/certs/`，恢复 443 配置，`docker compose restart nginx`。

更新版本：
```bash
git pull && docker compose up -d --build
```

---

## 方式二：systemd + Nginx（不装 Docker，资源最省）

需要：VPS 装 `python3`（3.10+）、`python3-venv`、`ffmpeg`、`nginx`、`certbot`。

```bash
# 1. 放到 /opt 并建虚拟环境
sudo git clone <你的仓库> /opt/libai-canvas-web
cd /opt/libai-canvas-web
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo apt install -y ffmpeg

# 2. 建运行用户 + 给 data 目录权限
sudo useradd -r -s /usr/sbin/nologin libai
sudo chown -R libai:libai /opt/libai-canvas-web/data

# 3. 环境变量
sudo cp .env.deploy.example .env.deploy
sudo vim .env.deploy            # 填域名、CORS、存储方案

# 4. 前端后端地址（同域）
sudo vim web/config.js          # -> window.location.origin

# 5. 装后端 systemd 服务
sudo cp deploy/systemd/libai-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now libai-backend
sudo systemctl status libai-backend
journalctl -u libai-backend -f   # 看日志

# 6. 配 Nginx
sudo sed -i 's/your-domain.com/真实域名/g' deploy/nginx/libai.systemd.conf
sudo cp deploy/nginx/libai.systemd.conf /etc/nginx/sites-available/libai.conf
sudo cp deploy/nginx/_libai_proxy.host.inc /etc/nginx/snippets/_libai_proxy.inc
sudo ln -sf /etc/nginx/sites-available/libai.conf /etc/nginx/sites-enabled/
#   先用 certbot 签证书
sudo certbot --nginx -d 真实域名
sudo nginx -t && sudo systemctl reload nginx
```

更新版本：
```bash
cd /opt/libai-canvas-web && sudo git pull
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart libai-backend
```

---

## 3. 验证清单

部署后逐项确认：

```bash
# 后端健康
curl -fsS https://真实域名/health

# 前端能打开
curl -I https://真实域名/

# 参考图自托管路由可达（方案 A）
curl -I https://真实域名/media/references/   # 404 正常（无具体文件），非 502 即反代通
```

浏览器打开站点，登录、跑一次图生视频，确认参考图能被上游拉取（看后端日志里参考图 URL 是你的域名而非 127.0.0.1）。

---

## 4. 安全与运维提示

- **后端不要直接对公网暴露 8765**：Docker 版已绑 `127.0.0.1`，systemd 版可在 `.env.deploy` 设 `LIBAI_BACKEND_HOST=127.0.0.1`（仅 Nginx 同机访问）。
- **认证**：后端通过 `/newapi/*` 走中转站账号体系，但服务本身没有额外网关鉴权。若 VPS 公开可访问，建议在 Nginx 层加 IP 白名单或 basic auth 限制管理操作，避免被陌生人调用消耗你的额度。
- **备份**：定期备份 `data/`（SQLite + 参考图）。本项目改 DB 前会自动留 `.bak` 文件。
- **密钥**：`.env.deploy`、`reference-storage.env`、`deploy/certs/` 都已在 `.gitignore` 中，切勿提交。
- **额度成本**：图生视频每次调用都消耗上游额度，部署后注意访问控制。

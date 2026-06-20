# 漫创AI · Web 版（画布功能 Web 化）

把原 Electron 桌面应用 `libai-canvas 0.20.10` 的**画布功能完整迁移到浏览器**运行的工程。
保留原后端（FastAPI）与原前端（React 构建产物）不变，去掉 Electron 外壳，
改成「**后端 HTTP 服务 + 前端静态站点**」的标准 Web 架构，可长期二次开发、更新和维护。

---

## 🚀 一键部署到云 VPS（小白友好）

**复制以下命令到你的 Ubuntu/Debian VPS，一路回答问题即可完成部署：**

```bash
curl -fsSL https://raw.githubusercontent.com/JUUCHEN1/ainew/main/install.sh | sudo bash
```

或先下载再运行：
```bash
wget https://raw.githubusercontent.com/JUUCHEN1/ainew/main/install.sh
sudo bash install.sh
```

脚本会交互式引导你选择：
- 部署方式（Docker 或 systemd）
- 域名（必须已解析到 VPS IP）
- HTTPS 证书（自动申请 Let's Encrypt 或手动放置）
- 参考图存储（本机自托管 / S3 / 图床）

全自动完成：装依赖、配置、证书、启动服务。部署完直接访问 `https://你的域名`。

> 高级用户或需要手动控制每一步，请看 [deploy/README.md](deploy/README.md)。

---

## 为什么能直接 Web 化

逆向原应用后确认：**前端构建产物本身就内置了浏览器运行分支**。

- 桌面模式下，前端通过 `window.libai.*`（Electron 预加载脚本）→ IPC → 本地 Python 后端。
- 当检测不到 `window.libai`（即浏览器环境）时，前端**自动回退**为直接用 `fetch`
  调用后端 HTTP 接口，基址取自 `window.__LIBAI_BACKEND_BASE_URL__`（默认 `http://127.0.0.1:8765`）。
- 自定义标题栏、本地文件选择器等 Electron 专属能力，在浏览器下会**自动隐藏/降级**，不影响画布主流程。
- 后端是纯 `FastAPI`（85 条路由），本来就是个 HTTP 服务，已配置 CORS。

所以迁移工作不是“重写”，而是：**用一个静态服务托管前端 + 独立跑后端 + 注入后端地址 + 打通 CORS**。

> 浏览器版与桌面版的差异：
> - ❌ 不能再用本机“选择文件/文件夹”对话框导入素材（浏览器安全沙箱限制）。
>   画布内的生成、编辑、保存、历史、剪映导出等仍走后端，正常工作。
> - ❌ 无自定义标题栏 / 自动更新（这是 Electron 特性，Web 不需要）。
> - ✅ 其余画布功能（节点图、生成任务、资产库、项目存取、WebSocket 任务进度）完整保留。

---

## 目录结构

```
libai-canvas-web/
├── 启动.bat                # Windows 双击一键启动（后端+前端+开浏览器）
├── 安装依赖.bat            # Windows 双击安装 Python 依赖
├── run_all.py             # 跨平台一键启动（后端+前端）
├── serve_web.py           # 仅启动前端静态服务（标准库，无依赖）
├── requirements.txt       # 后端 Python 依赖
├── data/                  # 运行时数据（SQLite、项目文件、缓存）—— 首次运行自动生成
├── server/
│   ├── run.py             # 后端启动脚本（设置环境变量并拉起 uvicorn）
│   ├── requirements.txt
│   └── backend/           # ★ 原 Python 后端源码（FastAPI），未改动
│       ├── app.py             主服务（路由、生成流程、模型同步）
│       ├── provider_adapters.py  各上游模型适配器
│       ├── new_api_client.py     NewAPI(中转站) 客户端
│       ├── jianying_export.py    剪映工程导出
│       ├── runtime_config.py     CORS / 运行时配置
│       └── ...
└── web/                   # ★ 前端（React 构建产物）
    ├── index.html         已注入 config.js 引用
    ├── config.js          ★ 运行时配置：后端地址写这里（改完刷新即可，无需重新打包）
    ├── assets/            主 JS/CSS bundle
    ├── style-library/     画风预览图
    ├── ui-assets/         登录页背景等
    └── _beautified-reference/   美化后的可读源码（仅供阅读/定位逻辑，不参与运行）
```

---

## 快速开始

### Windows（最简单）

1. 双击 **`安装依赖.bat`**（仅首次需要，会创建 `.venv` 并装好依赖）。
2. 双击 **`启动.bat`**，自动启动后端 + 前端，并打开浏览器到 `http://127.0.0.1:5180`。

### 任意平台（命令行）

```bash
# 1) 装依赖（建议用虚拟环境）
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 一键启动（后端 8765 + 前端 5180）
python run_all.py
# 浏览器打开 http://127.0.0.1:5180
```

### 分开启动（开发调试推荐）

```bash
# 终端 A：后端
python server/run.py            # http://127.0.0.1:8765

# 终端 B：前端静态服务
python serve_web.py             # http://127.0.0.1:5180
```

---

## 配置说明

### 后端地址（前端 → 后端）

编辑 `web/config.js`：

```js
window.__LIBAI_BACKEND_BASE_URL__ = "http://127.0.0.1:8765";   // 默认本机
// 部署到服务器时改成后端公网地址，例如：
// window.__LIBAI_BACKEND_BASE_URL__ = "https://api.your-domain.com";
```

改完**刷新浏览器即可**，不用重新构建前端。

### 端口 / 监听地址 / CORS（环境变量，均可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `LIBAI_BACKEND_PORT` | `8765` | 后端端口 |
| `LIBAI_BACKEND_HOST` | `0.0.0.0` | 后端监听地址（仅本机用可设 `127.0.0.1`） |
| `LIBAI_WEB_PORT` | `5180` | 前端静态服务端口（已在后端 CORS 白名单内） |
| `LIBAI_CORS_ORIGINS` | 见 `server/run.py` | 允许的前端来源，逗号分隔。**换前端端口/域名时必须同步这里** |
| `LIBAI_APP_DATA_DIR` | `./data` | 数据库与项目文件目录 |
| `LIBAI_FFMPEG_PATH` / `LIBAI_FFPROBE_PATH` | 空 | 视频相关功能需要 ffmpeg（见下） |

> ⚠️ **CORS 要点**：前端端口必须出现在后端 `LIBAI_CORS_ORIGINS` 里，否则浏览器会拦截请求。
> 默认前端端口 `5180` 已经在白名单内，开箱即用。换端口时记得改。

---

## 关于 ffmpeg（视频功能）

后端的视频分析、字幕去除、剪映导出等功能依赖 `ffmpeg` / `ffprobe`。
桌面版自带，这里没有打包。需要这些功能时自行安装并指向它：

```bash
# 安装 ffmpeg 后，设置环境变量再启动后端
set LIBAI_FFMPEG_PATH=C:\path\to\ffmpeg.exe        # Windows
set LIBAI_FFPROBE_PATH=C:\path\to\ffprobe.exe
```

不设置也能用画布、图像生成、项目管理等大部分功能。

---

## 部署到服务器（生产）

提供两套开箱即用的部署方案，完整步骤见 **[deploy/README.md](deploy/README.md)**：

- **Docker + Compose**（推荐）：`docker compose up -d --build` 一条命令起，
  自带后端镜像、Nginx 反代、`data/` 持久化。
- **systemd + Nginx**：不装 Docker，资源最省，宿主机直接跑。

两种方式都用 Nginx 同域反代（前端走 `/`，后端固定 API 前缀反代到 `:8765`，
已处理前端打包产物与后端 `/assets` 接口的路径冲突），并统一 HTTPS 入口。

部署前必改 3 处（详见 deploy/README）：

1. `web/config.js` 改为同源：
   ```js
   window.__LIBAI_BACKEND_BASE_URL__ = window.location.origin;
   ```
2. CORS：环境变量 `LIBAI_CORS_ORIGINS=你的域名`（跨域/独立子域必填）。
3. 参考图存储（图生视频等需要公网可访问的图片 URL），三选一：
   本机自托管 `LIBAI_PUBLIC_BASE_URL` / S3·腾讯云 COS / 自定义图床。
   配置项见 `.env.deploy.example`。

> WebSocket：任务进度走 `/jobs/events`（WS）。上述 Nginx 配置已开启长连接透传。

### 安全提醒
- 后端默认 `0.0.0.0` 监听，**公网暴露前**请放在反代之后并限制来源。
  Docker 方案已把后端绑定 `127.0.0.1`，仅 Nginx 可访问。
- 当前后端**没有内置鉴权**（原本依赖 Electron 本地环境），公网部署需自行在反代层
  加访问控制（IP 白名单 / basic auth），否则陌生人可调用接口消耗你的额度。
- 不要把 `data/` 目录暴露给静态服务。

---

## 二次开发指引

### 改后端（Python，源码完整可改）
直接编辑 `server/backend/*.py`，`python server/run.py` 重启即可。
- 上游接口/模型适配：`provider_adapters.py`、`new_api_client.py`
- 路由与业务流程：`app.py`
- 中转站地址等常量：`app.py` 顶部（`CURRENT_NEWAPI_HOST` / `DEFAULT_NEWAPI_BASE_URL`）

### 改前端（当前为构建产物）
`web/assets/*.js` 是 Vite 压缩产物。两种方式：
1. **小改**：参考 `web/_beautified-reference/` 里的美化版定位逻辑，直接改 `web/assets/` 内文件后刷新。
2. **正经二开**：原始 React/TSX 源码未随发行包提供，需要时按美化代码重建前端工程
   （成本较高，建议仅在需要大改 UI 时进行）。

后端是清晰的 REST + WebSocket 接口，**最推荐的二开路径是基于后端 API 重写/扩展前端**，
后端能力可长期复用。完整接口清单见 `server/backend/app.py` 中的 `@app.<method>(...)` 装饰器。

---

## 已验证

- 后端导入正常（85 条路由），`uvicorn` 启动成功，`/health` 返回 200。
- 前端静态服务正常托管，`index.html` / `config.js` / 主 bundle 均可访问（200）。
- CORS 预检通过（前端 `5180` 源被正确放行）。
- 端到端数据通路验证：通过 API 创建项目 → 写入 SQLite → 列表读回成功。
- `run_all.py` 一键启动：后端 + 前端同时拉起、自动开浏览器、Ctrl+C 同时停止。

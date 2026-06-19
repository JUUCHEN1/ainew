"""
漫创AI Web · 后端启动脚本 (FastAPI + uvicorn)
------------------------------------------------------------
独立于 Electron 运行原来的本地后端。负责：
  - 设置数据目录、CORS 白名单、ffmpeg 路径等环境变量
  - 用 uvicorn 拉起 backend.app:app

用法：
    python server/run.py
环境变量（均可选）：
    LIBAI_BACKEND_HOST        监听地址，默认 0.0.0.0（局域网可访问；只想本机用改 127.0.0.1）
    LIBAI_BACKEND_PORT        监听端口，默认 8765
    LIBAI_CORS_ORIGINS        允许的前端来源，逗号分隔。默认已含本地静态服务端口
    LIBAI_APP_DATA_DIR        数据/数据库目录，默认 ./data
    LIBAI_FFMPEG_PATH         ffmpeg 可执行文件路径（视频相关功能需要）
    LIBAI_FFPROBE_PATH        ffprobe 可执行文件路径
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

HERE = Path(__file__).resolve().parent          # .../server
ROOT = HERE.parent                              # 项目根


def _load_env_file(path: Path) -> None:
    """把 KEY=VALUE 形式的 .env 文件读进 os.environ（已存在的变量不覆盖）。
    文件不存在则静默跳过。仅用于用户自备的对象存储凭据。"""
    try:
        if not path.is_file():
            return
    except OSError:
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _default_cors() -> str:
    # 与前端静态服务默认端口保持一致；可用 LIBAI_CORS_ORIGINS 覆盖
    return ",".join([
        "http://127.0.0.1:5180", "http://localhost:5180",
        "http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:8080", "http://localhost:8080",
    ])


def main() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 数据目录默认放在项目内 ./data，便于备份与迁移
    os.environ.setdefault("LIBAI_APP_DATA_DIR", str(ROOT / "data"))
    Path(os.environ["LIBAI_APP_DATA_DIR"]).mkdir(parents=True, exist_ok=True)

    # 设计提示词模板目录（后端会读取）
    template_dir = HERE / "backend" / "design_prompt_templates"
    if template_dir.is_dir():
        os.environ.setdefault("LIBAI_DESIGN_PROMPT_TEMPLATE_DIR", str(template_dir))

    # 可选：加载对象存储凭据。仓库内只含 reference-storage.env.example 模板，
    # 用户复制为 reference-storage.env 并填入自己的密钥后才会被加载（不会进版本库）。
    _load_env_file(HERE / "backend" / "reference-storage.env")

    os.environ.setdefault("LIBAI_CORS_ORIGINS", _default_cors())

    host = os.environ.get("LIBAI_BACKEND_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int((os.environ.get("LIBAI_BACKEND_PORT") or "8765").strip() or "8765")
    log_level = (os.environ.get("LIBAI_BACKEND_LOG_LEVEL") or "info").strip() or "info"

    # 让 "backend.app" 可被导入：server/ 加入 sys.path
    sys.path.insert(0, str(HERE))

    print(f"[libai-web] backend  http://{host}:{port}  data={os.environ['LIBAI_APP_DATA_DIR']}")
    print(f"[libai-web] CORS     {os.environ['LIBAI_CORS_ORIGINS']}")

    uvicorn.run(
        "backend.app:app",
        app_dir=str(HERE),
        host=host,
        port=port,
        log_level=log_level,
        loop="asyncio",
        http="h11",
        ws="websockets",
    )


if __name__ == "__main__":
    main()

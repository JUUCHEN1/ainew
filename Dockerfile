# ============================================================
# 漫创AI Web · 后端镜像 (FastAPI + uvicorn)
# ------------------------------------------------------------
# 只打包后端。前端 web/ 是纯静态文件，交给 Nginx 直接托管，
# 不进镜像，改 config.js 后刷新即可生效，无需重建。
# ============================================================
FROM python:3.12-slim

# ffmpeg/ffprobe：视频相关功能需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LIBAI_BACKEND_HOST=0.0.0.0 \
    LIBAI_BACKEND_PORT=8765 \
    LIBAI_APP_DATA_DIR=/app/data \
    LIBAI_FFMPEG_PATH=/usr/bin/ffmpeg \
    LIBAI_FFPROBE_PATH=/usr/bin/ffprobe

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝后端代码
COPY server/ ./server/

# 数据目录（会被 compose 的卷覆盖挂载，保证持久化）
RUN mkdir -p /app/data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "server/run.py"]

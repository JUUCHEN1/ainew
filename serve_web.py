"""
漫创AI Web · 前端静态服务
------------------------------------------------------------
把 web/ 目录作为静态网站托管。纯标准库实现，无需额外依赖。
默认端口 5180（已在后端 CORS 白名单内，开箱即用）。

用法：
    python serve_web.py
环境变量：
    LIBAI_WEB_PORT   前端端口，默认 5180
    LIBAI_WEB_HOST   监听地址，默认 0.0.0.0
"""
from __future__ import annotations

import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"


class Handler(SimpleHTTPRequestHandler):
    """静态服务 + 正确的 MIME + 禁用缓存（方便二次开发热刷新）。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".wasm": "application/wasm",
    }

    def end_headers(self):
        # 开发期禁用缓存，避免改了文件浏览器还用旧的
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):  # 安静一点
        pass


def main() -> None:
    host = os.environ.get("LIBAI_WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int((os.environ.get("LIBAI_WEB_PORT") or "5180").strip() or "5180")
    handler = partial(Handler, directory=str(WEB_DIR))
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"[libai-web] frontend http://{host}:{port}  dir={WEB_DIR}")
    print(f"[libai-web] 打开浏览器访问  http://127.0.0.1:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()

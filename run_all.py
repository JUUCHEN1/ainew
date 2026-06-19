"""
漫创AI Web · 一键启动 (后端 + 前端静态服务)
------------------------------------------------------------
同时拉起：
  - 后端 FastAPI (默认 http://127.0.0.1:8765)
  - 前端静态服务 (默认 http://127.0.0.1:5180)

用法：
    python run_all.py
然后浏览器打开： http://127.0.0.1:5180

按 Ctrl+C 同时停止两个服务。

环境变量（可选）：
    LIBAI_BACKEND_PORT   后端端口，默认 8765
    LIBAI_WEB_PORT       前端端口，默认 5180
    LIBAI_BACKEND_HOST   后端监听地址，默认 0.0.0.0
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def main() -> None:
    web_port = (os.environ.get("LIBAI_WEB_PORT") or "5180").strip()

    backend = subprocess.Popen([PY, str(ROOT / "server" / "run.py")], cwd=str(ROOT))
    # 给后端一点启动时间，避免前端先打开时连接失败
    time.sleep(2.0)
    frontend = subprocess.Popen([PY, str(ROOT / "serve_web.py")], cwd=str(ROOT))

    url = f"http://127.0.0.1:{web_port}"
    print(f"\n[libai-web] 全部启动完成 -> 打开 {url}\n[libai-web] 按 Ctrl+C 停止\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    procs = [backend, frontend]
    try:
        while True:
            for p in procs:
                if p.poll() is not None:
                    # 某个进程退出了，连带停止另一个
                    raise KeyboardInterrupt
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[libai-web] 正在停止 ...")
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    if os.name == "nt":
                        p.terminate()
                    else:
                        p.send_signal(signal.SIGINT)
                except Exception:
                    pass
        for p in procs:
            try:
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    main()

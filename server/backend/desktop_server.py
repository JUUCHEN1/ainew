from __future__ import annotations

import os

import uvicorn


def main() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    port = int(str(os.environ.get("LIBAI_BACKEND_PORT") or "8765").strip() or "8765")
    log_level = str(os.environ.get("LIBAI_BACKEND_LOG_LEVEL") or "warning").strip() or "warning"
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        loop="asyncio",
        http="h11",
        ws="websockets",
    )


if __name__ == "__main__":
    main()

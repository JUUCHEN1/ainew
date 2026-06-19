from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://localhost:3001",
    "http://127.0.0.1:5177",
    "http://localhost:5177",
    "http://127.0.0.1:5180",
    "http://localhost:5180",
]


def _env_value(env: Optional[Mapping[str, str]], name: str, default: str = "") -> str:
    source = os.environ if env is None else env
    return str(source.get(name, default) or "").strip()


def cors_allowed_origins(env: Optional[Mapping[str, str]] = None) -> list[str]:
    raw = _env_value(env, "LIBAI_CORS_ORIGINS")
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def default_data_dir(env: Optional[Mapping[str, str]] = None, *, root: Optional[Path] = None) -> Path:
    configured = _env_value(env, "LIBAI_APP_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = root or Path(__file__).resolve().parents[1]
    return (base.parent / ".data").resolve()


def server_host(env: Optional[Mapping[str, str]] = None) -> str:
    return _env_value(env, "LIBAI_HOST", "127.0.0.1") or "127.0.0.1"


def server_port(env: Optional[Mapping[str, str]] = None) -> int:
    raw = _env_value(env, "LIBAI_PORT", "8787") or "8787"
    try:
        port = int(raw)
    except ValueError:
        return 8787
    return port if port > 0 else 8787

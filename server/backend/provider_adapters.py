import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

import httpx

from backend.network_security import (
    UnsafeRemoteUrlError,
    is_local_backend_asset_url,
    is_private_or_local_http_url,
    public_http_get,
    unsafe_remote_url_message,
)
from backend.image_security import safe_image_open


ProgressCallback = Callable[[int, Optional[str]], Awaitable[None]]
DEFAULT_NEWAPI_BASE_URL = "https://manchuang.xyz"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3000.0
DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS = DEFAULT_REQUEST_TIMEOUT_SECONDS
DEFAULT_NEWAPI_VIDEO_SUBMIT_TIMEOUT_SECONDS = 6000.0
DEFAULT_SEEDANCE_VIDEO_SUBMIT_TIMEOUT_SECONDS = 3000.0
DEFAULT_NEWAPI_VIDEO_SUBMIT_RECOVERY_SECONDS = 600.0
DEFAULT_NEWAPI_VIDEO_RESULT_TIMEOUT_SECONDS = 6000.0
DEFAULT_NEWAPI_VIDEO_STATUS_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_NEWAPI_VIDEO_SUBMIT_RECOVERY_LOOKUP_MISSES = 6
DEFAULT_NEWAPI_VIDEO_SUBMIT_RETRY_ATTEMPTS = 1
DEFAULT_NEWAPI_VIDEO_SUBMIT_RETRY_DELAY_SECONDS = 2.0
DEFAULT_SEEDANCE_VIDEO_SUBMIT_CONCURRENCY = 1
SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS = 15.4
MEDIA_PROBE_TIMEOUT_SECONDS = 10.0
NEWAPI_CLIENT_TASK_ID_HEADER = "X-LibAI-Task-Id"
DEFAULT_REFERENCE_IMAGE_HOSTING_UPLOAD_URL = "https://imageproxy.zhongzhuan.chat/api/upload"
DEFAULT_REFERENCE_IMAGE_HOSTING_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_REFERENCE_IMAGE_HOSTING_CONCURRENCY = 5
DEFAULT_REFERENCE_IMAGE_HOSTING_RETRY_ATTEMPTS = 3
DEFAULT_REFERENCE_IMAGE_HOSTING_RETRY_DELAY_SECONDS = 1.0
DEFAULT_REFERENCE_IMAGE_COMPRESS_THRESHOLD_BYTES = 2 * 1024 * 1024
DEFAULT_REFERENCE_IMAGE_COMPRESS_TARGET_BYTES = 2 * 1024 * 1024
DEFAULT_REFERENCE_IMAGE_COMPRESS_MIN_QUALITY = 55
DEFAULT_REFERENCE_MEDIA_HOSTING_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT = 10
AUTO_PROXY_PROBE_HOST = "manchuang.xyz"
AUTO_PROXY_PROBE_PORT = 443
PROXY_ENV_NAMES = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)
DEFAULT_AUTO_PROXY_PORTS = (24001, 7890, 7897, 10809, 10808)
AUTO_PROXY_CONNECT_TIMEOUT_SECONDS = 0.15
NO_PROXY_DEFAULTS = ("localhost", "127.0.0.1", "::1")
REFERENCE_IMAGE_HOSTING_CACHE_LIMIT = 512
SEEDANCE_VIDEO_SUBMIT_SEMAPHORES: Dict[str, asyncio.Semaphore] = {}
_REFERENCE_IMAGE_HOSTING_CACHE: Dict[str, str] = {}
_REFERENCE_IMAGE_HOSTING_INFLIGHT: Dict[str, asyncio.Task] = {}
_REFERENCE_IMAGE_HOSTING_LOCK: Optional[asyncio.Lock] = None
_REFERENCE_IMAGE_HOSTING_LOCK_LOOP_ID: Optional[int] = None
_REFERENCE_IMAGE_HOSTING_SEMAPHORE: Optional[asyncio.Semaphore] = None
_REFERENCE_IMAGE_HOSTING_SEMAPHORE_LOOP_ID: Optional[int] = None


class ProviderAdapterError(RuntimeError):
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _first_proxy_env_name() -> Optional[str]:
    first_name = None
    for name in PROXY_ENV_NAMES:
        raw_value = str(os.environ.get(name) or "").strip()
        if not raw_value:
            continue
        normalized = _normalize_proxy_url(raw_value)
        if normalized:
            local_port = _local_proxy_port_from_url(normalized)
            if local_port is not None and not _local_proxy_available(local_port):
                os.environ.pop(name, None)
                continue
            os.environ[name] = normalized
            if first_name is None:
                first_name = name
        else:
            os.environ.pop(name, None)
    return first_name


def _is_loopback_proxy_host(host: str) -> bool:
    return str(host or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _local_proxy_port_from_url(proxy_url: str) -> Optional[int]:
    try:
        parsed = urlparse(proxy_url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not _is_loopback_proxy_host(parsed.hostname or ""):
        return None
    try:
        return parsed.port
    except ValueError:
        return None


def _normalize_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        parsed = urlparse(text)
        if parsed.scheme.lower() == "https" and _is_loopback_proxy_host(parsed.hostname or ""):
            return parsed._replace(scheme="http").geturl()
        return text
    if re.match(r"^\[[^\]]+\]:\d+$", text) or re.match(r"^[^\s:]+:\d+$", text):
        return f"http://{text}"
    return ""


def _auto_proxy_enabled() -> bool:
    return _env_bool("LIBAI_AUTO_PROXY", True)


def _auto_proxy_ports() -> tuple[int, ...]:
    raw = str(os.environ.get("LIBAI_AUTO_PROXY_PORTS") or "").strip()
    if not raw:
        return DEFAULT_AUTO_PROXY_PORTS
    ports: List[int] = []
    for part in re.split(r"[,;\s]+", raw):
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            continue
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports) or DEFAULT_AUTO_PROXY_PORTS


def _merge_no_proxy(value: str) -> str:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    for item in NO_PROXY_DEFAULTS:
        if item not in items:
            items.append(item)
    return ",".join(items)


def _set_proxy_env(proxy_url: str) -> None:
    normalized = _normalize_proxy_url(proxy_url)
    if not normalized:
        return
    for name in PROXY_ENV_NAMES:
        os.environ[name] = normalized
    no_proxy = _merge_no_proxy(os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "")
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


def _local_proxy_available(port: int) -> bool:
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=AUTO_PROXY_CONNECT_TIMEOUT_SECONDS)
        sock.settimeout(AUTO_PROXY_CONNECT_TIMEOUT_SECONDS)
        probe = (
            f"CONNECT {AUTO_PROXY_PROBE_HOST}:{AUTO_PROXY_PROBE_PORT} HTTP/1.1\r\n"
            f"Host: {AUTO_PROXY_PROBE_HOST}:{AUTO_PROXY_PROBE_PORT}\r\n"
            "Proxy-Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(probe)
        response = sock.recv(128)
        first_line = response.splitlines()[0].decode("latin1", errors="ignore") if response else ""
        return bool(re.match(r"^HTTP/\d(?:\.\d)?\s+(?:200|407)\b", first_line))
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _ensure_auto_proxy_env() -> None:
    if _first_proxy_env_name() is not None or not _auto_proxy_enabled():
        return
    configured = _normalize_proxy_url(os.environ.get("LIBAI_PROXY_URL") or "")
    if configured:
        _set_proxy_env(configured)
        return
    for port in _auto_proxy_ports():
        if _local_proxy_available(port):
            _set_proxy_env(f"http://127.0.0.1:{port}")
            return


def _http_trust_env() -> bool:
    if os.environ.get("LIBAI_HTTP_TRUST_ENV") is not None:
        if not _env_bool("LIBAI_HTTP_TRUST_ENV", False):
            return False
        _ensure_auto_proxy_env()
        return True
    _ensure_auto_proxy_env()
    return _first_proxy_env_name() is not None


def _provider_connection_error_message(url: str, error: Exception) -> str:
    raw_message = str(error).strip() or type(error).__name__
    lower_message = raw_message.lower()
    trust_env = _http_trust_env()
    proxy_name = _first_proxy_env_name()

    if isinstance(error, httpx.TimeoutException):
        reason = "请求超时，请检查供应商服务或本地代理延迟"
    elif isinstance(error, httpx.ProxyError):
        reason = "代理不可用或代理连接失败，请检查本地代理端口"
    elif isinstance(error, httpx.RemoteProtocolError):
        reason = "连接被服务端或代理中断，请稍后重试或检查代理稳定性"
    elif isinstance(error, httpx.ConnectError):
        if any(marker in lower_message for marker in ("certificate", "ssl", "tls", "wrong version number")):
            reason = "TLS/证书握手失败，请检查代理 HTTPS 转发或证书配置"
        elif any(marker in lower_message for marker in ("getaddrinfo", "name resolution", "dns", "nodename nor servname")):
            reason = "DNS 解析失败，请检查网络 DNS 或代理 DNS 设置"
        elif trust_env and proxy_name:
            reason = f"连接失败；已启用代理环境 {proxy_name}，请确认本地代理可用"
        else:
            reason = "连接失败；未检测到代理环境，后端不会走本地代理"
    elif any(marker in lower_message for marker in ("connection reset", "connection aborted", "10053", "10054")):
        reason = "连接被重置，请检查代理稳定性或供应商链路"
    else:
        reason = "连接失败"

    return f"供应商接口连接失败：{url}（{reason}；原始错误：{raw_message}）"


def _clean_base_url(value: str, default: str = DEFAULT_NEWAPI_BASE_URL) -> str:
    base = (value or default).strip().rstrip("/")
    for suffix in (
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/images/compositions",
        "/v1/videos/generations",
        "/v1/videos",
        "/v1/video/create",
        "/v1/video/query",
        "/v1",
    ):
        while base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base or default


def _auth_headers(api_key: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers


def _merge_extra_headers(headers: Dict[str, str], extra_headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not extra_headers:
        return headers
    for key, value in extra_headers.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = str(value)
    return headers


def _json_response_payload(response: httpx.Response) -> Dict[str, Any]:
    text = response.text or ""
    if not response.is_success:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": text}
        raise ProviderAdapterError(_error_message(payload, f"HTTP {response.status_code}"))
    if not text.strip():
        return {}
    try:
        payload = response.json()
    except Exception:
        return {"raw": text}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ProviderAdapterError(_error_message(payload, "Provider request failed"))
    if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {"data": payload}


def _sync_json_request(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = _auth_headers(api_key)
    _merge_extra_headers(headers, extra_headers)
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
            response = client.request(method.upper(), url, json=body, params=params, headers=headers)
    except Exception as error:
        raise ProviderAdapterError(_provider_connection_error_message(url, error)) from error
    return _json_response_payload(response)


async def _json_request(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    extra_headers: Optional[Dict[str, str]] = None,
    force_sync: bool = False,
) -> Dict[str, Any]:
    if force_sync:
        return await asyncio.to_thread(
            _sync_json_request,
            method,
            url,
            api_key,
            body,
            params,
            timeout,
            extra_headers,
        )
    headers = _auth_headers(api_key)
    _merge_extra_headers(headers, extra_headers)
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
            response = await client.request(method.upper(), url, json=body, params=params, headers=headers)
    except Exception as error:
        raise ProviderAdapterError(_provider_connection_error_message(url, error)) from error
    return _json_response_payload(response)


def _is_transient_provider_connection_error(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = (
        "server disconnected",
        "without sending a response",
        "remoteprotocolerror",
        "readerror",
        "connection reset",
        "connection aborted",
        "connection closed",
        "broken pipe",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "http 521",
        "status_code=521",
        "status code: 521",
        "查询链路源站暂时不可达",
    )
    return any(marker in text for marker in markers)


def _is_task_lookup_miss(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = (
        "task_not_exist",
        "task not exist",
        "task does not exist",
        "not found",
        "404",
    )
    return any(marker in text for marker in markers)


def _is_retryable_newapi_video_submit_error(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if _is_transient_provider_connection_error(error) or _is_task_lookup_miss(error):
        return True
    markers = (
        "中转站未创建任务",
        "upstream request failed",
        "status_code=502",
        "status code: 502",
        "http 502",
        "bad gateway",
        "http 503",
        "service unavailable",
        "http 504",
        "gateway timeout",
    )
    return any(marker in text for marker in markers)


async def _json_get_with_transient_retries(
    url: str,
    api_key: str,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    attempts: int = 3,
    initial_delay: float = 1.0,
) -> Dict[str, Any]:
    last_error: Optional[ProviderAdapterError] = None
    for attempt in range(max(1, attempts)):
        try:
            return await _json_request("GET", url, api_key, timeout=timeout)
        except ProviderAdapterError as error:
            last_error = error
            if attempt >= attempts - 1 or not _is_transient_provider_connection_error(error):
                raise
            await asyncio.sleep(initial_delay * (attempt + 1))
    if last_error:
        raise last_error
    return {}


def _newapi_client_task_id(payload: Dict[str, Any]) -> Optional[str]:
    raw = str(payload.get("_jobId") or payload.get("jobId") or "").strip()
    if not raw:
        return None
    if raw.startswith("job_"):
        raw = raw[4:]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_-")
    if not safe:
        return None
    return f"task_libai_{safe}"[:120]


def _newapi_client_task_headers(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    task_id = _newapi_client_task_id(payload)
    if not task_id:
        return None
    return {NEWAPI_CLIENT_TASK_ID_HEADER: task_id}


def _newapi_video_submit_timeout() -> float:
    value = _env_float("LIBAI_NEWAPI_VIDEO_SUBMIT_TIMEOUT_SECONDS", DEFAULT_NEWAPI_VIDEO_SUBMIT_TIMEOUT_SECONDS)
    return max(DEFAULT_REQUEST_TIMEOUT_SECONDS, value)


def _seedance_video_submit_timeout() -> float:
    value = _env_float("LIBAI_SEEDANCE_VIDEO_SUBMIT_TIMEOUT_SECONDS", DEFAULT_SEEDANCE_VIDEO_SUBMIT_TIMEOUT_SECONDS)
    return max(10.0, value)


def _newapi_video_submit_recovery_seconds() -> float:
    value = _env_float("LIBAI_NEWAPI_VIDEO_SUBMIT_RECOVERY_SECONDS", DEFAULT_NEWAPI_VIDEO_SUBMIT_RECOVERY_SECONDS)
    return max(5.0, value)


def _newapi_video_result_timeout_seconds() -> int:
    value = _env_float("LIBAI_NEWAPI_VIDEO_RESULT_TIMEOUT_SECONDS", DEFAULT_NEWAPI_VIDEO_RESULT_TIMEOUT_SECONDS)
    return int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, value))


def _newapi_video_status_request_timeout_seconds() -> float:
    value = _env_float("LIBAI_NEWAPI_VIDEO_STATUS_REQUEST_TIMEOUT_SECONDS", DEFAULT_NEWAPI_VIDEO_STATUS_REQUEST_TIMEOUT_SECONDS)
    return max(5.0, value)


def _newapi_video_submit_recovery_lookup_misses() -> int:
    return max(1, _env_int(
        "LIBAI_NEWAPI_VIDEO_SUBMIT_RECOVERY_LOOKUP_MISSES",
        DEFAULT_NEWAPI_VIDEO_SUBMIT_RECOVERY_LOOKUP_MISSES,
    ))


def _newapi_video_submit_retry_attempts() -> int:
    return max(1, _env_int("LIBAI_NEWAPI_VIDEO_SUBMIT_RETRY_ATTEMPTS", DEFAULT_NEWAPI_VIDEO_SUBMIT_RETRY_ATTEMPTS))


def _newapi_video_submit_retry_delay_seconds() -> float:
    return max(0.1, _env_float(
        "LIBAI_NEWAPI_VIDEO_SUBMIT_RETRY_DELAY_SECONDS",
        DEFAULT_NEWAPI_VIDEO_SUBMIT_RETRY_DELAY_SECONDS,
    ))


def _seedance_video_submit_concurrency() -> int:
    return max(1, _env_int("LIBAI_SEEDANCE_VIDEO_SUBMIT_CONCURRENCY", DEFAULT_SEEDANCE_VIDEO_SUBMIT_CONCURRENCY))


def _seedance_video_submit_semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    limit = _seedance_video_submit_concurrency()
    key = f"{loop_id}:{limit}"
    semaphore = SEEDANCE_VIDEO_SUBMIT_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        SEEDANCE_VIDEO_SUBMIT_SEMAPHORES[key] = semaphore
    return semaphore


async def _recover_newapi_video_submission(
    *,
    base: str,
    api_key: str,
    client_task_id: str,
    progress: ProgressCallback,
    original_error: ProviderAdapterError,
    timeout_seconds: Optional[float] = None,
    status_path: Optional[str] = None,
) -> Dict[str, Any]:
    await progress(14, "recovering-submit")
    lookup_path = status_path or f"/v1/videos/{client_task_id}"
    status_url = f"{base}{lookup_path}"
    last_error: Exception = original_error
    deadline = time.monotonic() + (timeout_seconds if timeout_seconds is not None else _newapi_video_submit_recovery_seconds())
    attempt = 0
    lookup_misses = 0
    max_lookup_misses = _newapi_video_submit_recovery_lookup_misses()
    while True:
        if attempt > 0:
            await asyncio.sleep(min(5.0, 1.5 * attempt))
        if attempt > 0 and time.monotonic() >= deadline:
            break
        attempt += 1
        try:
            payload = await _json_get_with_transient_retries(
                status_url,
                api_key,
                timeout=_newapi_video_status_request_timeout_seconds(),
                attempts=3,
            )
        except ProviderAdapterError as error:
            last_error = error
            if _is_task_lookup_miss(error):
                lookup_misses += 1
                if lookup_misses >= max_lookup_misses:
                    raise ProviderAdapterError(
                        f"{original_error}；中转站未创建任务 {client_task_id}（task_not_exist）"
                    ) from error
                continue
            if _is_transient_provider_connection_error(error):
                continue
            raise
        if _extract_task_id(payload) or _extract_urls(payload):
            return payload
        last_error = ProviderAdapterError("任务恢复查询暂未返回 task_id")
    raise ProviderAdapterError(
        f"{original_error}；已用任务 ID {client_task_id} 尝试恢复，但中转站暂未找到该任务"
    ) from last_error


def _append_responses_stream_text(payload: Dict[str, Any], parts: List[str]) -> None:
    event_type = str(payload.get("type") or "")
    delta = payload.get("delta")
    if event_type in {"response.output_text.delta", "response.refusal.delta"} and isinstance(delta, str):
        parts.append(delta)
        return
    if event_type in {"response.output_text.done", "response.refusal.done"}:
        text = _normalize_chat_content(payload.get("text") or payload.get("refusal"))
        if text and not parts:
            parts.append(text)
        return
    if event_type == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and not parts:
            text = _extract_responses_text({"output": [item]})
            if text:
                parts.append(text)


def _append_chat_completion_stream_text(payload: Dict[str, Any], parts: List[str]) -> None:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            reasoning = _normalize_chat_content(delta.get("reasoning_content") or delta.get("reasoningContent"))
            content = _normalize_chat_content(delta.get("content"))
            if reasoning:
                parts.append(reasoning)
            if content:
                parts.append(content)
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            content = _normalize_chat_content(message.get("content"))
            if content:
                parts.append(content)


_IMAGE_STREAM_INDEX_KEY = "__stream_index"
_IMAGE_STREAM_PRIORITY_KEY = "__stream_priority"


def _is_preview_media_item(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("is_preview", "isPreview", "preview", "is_partial", "isPartial", "partial"):
        flag = value.get(key)
        if flag is True or flag == 1 or str(flag).lower() == "true":
            return True
    for key in ("type", "kind", "role", "status", "stage", "event"):
        text = str(value.get(key) or "").strip().lower()
        if any(marker in text for marker in ("preview", "partial", "thumbnail")):
            return True
    return False


def _image_stream_index(payload: Dict[str, Any], fallback_index: Optional[int] = None) -> Optional[int]:
    for key in ("partial_image_index", "index", "output_index"):
        if payload.get(key) is None:
            continue
        try:
            return int(payload.get(key))
        except (TypeError, ValueError):
            continue
    if fallback_index is not None:
        return fallback_index
    event_type = _as_text(payload.get("type")) or ""
    if event_type.startswith("image_generation."):
        return 0
    return None


def _image_stream_priority(payload: Dict[str, Any], inherited_priority: Optional[int] = None) -> int:
    event_type = (_as_text(payload.get("type")) or "").lower()
    if not event_type and inherited_priority is not None:
        return inherited_priority
    if _is_preview_media_item(payload):
        return 0
    if "completed" in event_type or "final" in event_type:
        return 2
    return 1


def _clean_image_stream_items(images: List[Dict[str, str]]) -> List[Dict[str, str]]:
    cleaned: List[Dict[str, str]] = []
    for item in images:
        priority = int(item.get(_IMAGE_STREAM_PRIORITY_KEY) or 1)
        if priority <= 0:
            continue
        image = {
            key: value
            for key, value in item.items()
            if key not in {_IMAGE_STREAM_INDEX_KEY, _IMAGE_STREAM_PRIORITY_KEY}
        }
        if image and image not in cleaned:
            cleaned.append(image)
    return cleaned


def _append_image_stream_image(
    payload: Any,
    images: List[Dict[str, str]],
    fallback_index: Optional[int] = None,
    inherited_priority: Optional[int] = None,
) -> None:
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _append_image_stream_image(item, images, index, inherited_priority)
        return
    if not isinstance(payload, dict):
        return

    stream_priority = _image_stream_priority(payload, inherited_priority)
    image: Dict[str, str] = {}
    b64_json = _as_text(payload.get("b64_json"))
    url = _as_text(payload.get("url"))
    revised_prompt = _as_text(payload.get("revised_prompt"))
    if b64_json:
        image["b64_json"] = b64_json
    if url:
        image["url"] = url
    if revised_prompt:
        image["revised_prompt"] = revised_prompt
    if image:
        stream_index = _image_stream_index(payload, fallback_index)
        if stream_index is not None:
            image[_IMAGE_STREAM_INDEX_KEY] = str(stream_index)
            image[_IMAGE_STREAM_PRIORITY_KEY] = str(stream_priority)
            for existing_index, existing in enumerate(images):
                if existing.get(_IMAGE_STREAM_INDEX_KEY) != str(stream_index):
                    continue
                existing_priority = int(existing.get(_IMAGE_STREAM_PRIORITY_KEY) or 0)
                if stream_priority >= existing_priority:
                    images[existing_index] = image
                break
            else:
                images.append(image)
        elif image not in images:
            images.append(image)

    for key in ("data", "image", "images", "output", "response", "result"):
        nested = payload.get(key)
        if nested is not None:
            _append_image_stream_image(nested, images, inherited_priority=stream_priority)


def _append_stream_payload(
    payload: Any,
    text_parts: List[str],
    image_items: List[Dict[str, str]],
    collect_images: bool = True,
) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    _append_responses_stream_text(payload, text_parts)
    _append_chat_completion_stream_text(payload, text_parts)
    if collect_images:
        _append_image_stream_image(payload, image_items)
    if payload.get("type") == "response.completed" and isinstance(payload.get("response"), dict):
        return payload["response"]
    return None


def _stream_response_collects_images(url: str) -> bool:
    try:
        path = urlparse(str(url)).path.lower()
    except Exception:
        path = str(url or "").lower()
    return "/images/" in path


def _stream_result(
    text_parts: List[str],
    image_items: List[Dict[str, str]],
    final_response: Dict[str, Any],
    event_count: int,
) -> Dict[str, Any]:
    text = "".join(text_parts).strip()
    if not text and final_response:
        text = _extract_responses_text(final_response)
    result: Dict[str, Any] = {
        "output_text": text,
        "content": text,
        "stream": True,
        "streamEventCount": event_count,
    }
    cleaned_image_items = _clean_image_stream_items(image_items)
    if cleaned_image_items:
        result["data"] = cleaned_image_items
    if final_response:
        for key in ("id", "object", "created_at", "status", "model", "usage"):
            if key in final_response:
                result[key] = final_response[key]
    return result


def _consume_sse_block(
    block: str,
    text_parts: List[str],
    image_items: List[Dict[str, str]],
    collect_images: bool = True,
) -> tuple[int, Optional[Dict[str, Any]]]:
    data_lines = [
        line[5:].lstrip()
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return 0, None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return 0, None
    try:
        payload = json.loads(data)
    except Exception:
        return 0, None
    final_response = _append_stream_payload(payload, text_parts, image_items, collect_images=collect_images)
    return 1 if isinstance(payload, dict) else 0, final_response


def _parse_sse_text(raw: str, collect_images: bool = True) -> Dict[str, Any]:
    text_parts: List[str] = []
    image_items: List[Dict[str, str]] = []
    final_response: Dict[str, Any] = {}
    event_count = 0
    pending = raw.replace("\r\n", "\n")
    while "\n\n" in pending:
        block, pending = pending.split("\n\n", 1)
        count, response = _consume_sse_block(block, text_parts, image_items, collect_images=collect_images)
        event_count += count
        if response:
            final_response = response
    if pending.strip():
        count, response = _consume_sse_block(pending, text_parts, image_items, collect_images=collect_images)
        event_count += count
        if response:
            final_response = response
    return _stream_result(text_parts, image_items, final_response, event_count)


def _normalize_json_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ProviderAdapterError(_error_message(payload, "Provider request failed"))
    if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {"data": payload}


def _parse_loose_stream_json(raw: str) -> Dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    candidates = [text]
    without_comments = "\n".join(
        line for line in text.replace("\r\n", "\n").splitlines()
        if not line.lstrip().startswith(":")
    ).strip()
    if without_comments and without_comments != text:
        candidates.append(without_comments)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for source in (candidate,):
            try:
                parsed = json.loads(source)
                return _normalize_json_payload(parsed)
            except ProviderAdapterError:
                raise
            except Exception:
                pass
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            try:
                parsed, _end = decoder.raw_decode(candidate[index:])
                return _normalize_json_payload(parsed)
            except ProviderAdapterError:
                raise
            except Exception:
                continue
    return {}


async def _consume_streaming_response(response: httpx.Response, url: str) -> Dict[str, Any]:
    collect_images = _stream_response_collects_images(url)
    if not response.is_success:
        raw = (await response.aread()).decode("utf-8", "replace")
        recovered = _parse_sse_text(raw, collect_images=collect_images)
        if recovered.get("data"):
            recovered["httpStatus"] = response.status_code
            recovered["recoveredFromErrorStream"] = True
            return recovered
        payload = _parse_loose_stream_json(raw) or {"raw": raw}
        raise ProviderAdapterError(_error_message(payload, f"HTTP {response.status_code}"))

    content_type = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" not in content_type:
        raw = (await response.aread()).decode("utf-8", "replace")
        return _parse_loose_stream_json(raw) or {"raw": raw}

    text_parts: List[str] = []
    image_items: List[Dict[str, str]] = []
    final_response: Dict[str, Any] = {}
    event_count = 0
    pending = ""
    raw_parts: List[str] = []

    async for chunk in response.aiter_text():
        raw_parts.append(chunk)
        pending = (pending + chunk).replace("\r\n", "\n")
        while "\n\n" in pending:
            block, pending = pending.split("\n\n", 1)
            count, response_payload = _consume_sse_block(block, text_parts, image_items, collect_images=collect_images)
            event_count += count
            if response_payload:
                final_response = response_payload
                return _stream_result(text_parts, image_items, final_response, event_count)

    if pending.strip():
        count, response_payload = _consume_sse_block(pending, text_parts, image_items, collect_images=collect_images)
        event_count += count
        if response_payload:
            final_response = response_payload

    result = _stream_result(text_parts, image_items, final_response, event_count)
    if not result.get("data") and not result.get("output_text") and not final_response:
        raw = "".join(raw_parts)
        parsed = _parse_loose_stream_json(raw)
        if parsed:
            return parsed
    return result


def _streaming_request_accept_header(body: Optional[Dict[str, Any]]) -> str:
    if isinstance(body, dict) and body.get("stream") is False:
        return "application/json"
    return "text/event-stream"


async def _streaming_json_request(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    headers = _auth_headers(api_key)
    headers["Accept"] = _streaming_request_accept_header(body)
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
            async with client.stream(method.upper(), url, json=body, params=params, headers=headers) as response:
                return await _consume_streaming_response(response, url)
    except ProviderAdapterError:
        raise
    except Exception as error:
        raise ProviderAdapterError(_provider_connection_error_message(url, error)) from error


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, str) and payload.strip():
        text = payload.strip()
        return _openai_error_message(payload) if _is_generic_openai_error(text) else text
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail", "fail_reason", "failReason", "reason", "raw"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                return _openai_error_message(payload) if _is_generic_openai_error(text) else text
            if isinstance(value, dict):
                nested = _error_message(value, "")
                if nested:
                    if _is_generic_openai_error(nested):
                        return _openai_error_message(payload)
                    if nested.startswith("上游模型通道返回 openai_error"):
                        return _openai_error_message(payload)
                    return nested
    return fallback


def _is_generic_openai_error(value: str) -> bool:
    return value.strip().lower() == "openai_error"


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip().startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collect_error_context(payload: Any) -> Dict[str, str]:
    context: Dict[str, str] = {}
    queue: List[Any] = [payload]
    seen = 0
    while queue and seen < 12:
        seen += 1
        current = queue.pop(0)
        if not isinstance(current, dict):
            continue
        for key in ("channel_name", "channelName", "request_path", "requestPath", "request_id", "requestId", "status_code", "statusCode", "error_code", "errorCode"):
            value = current.get(key)
            text = _as_text(value)
            if text and key not in context:
                context[key] = text
        for key in ("error", "detail", "data", "raw", "other"):
            value = current.get(key)
            if isinstance(value, dict):
                queue.append(value)
            else:
                parsed = _json_object(value)
                if parsed:
                    queue.append(parsed)
    return context


def _openai_error_message(payload: Any) -> str:
    context = _collect_error_context(payload)
    details: List[str] = []
    channel_name = context.get("channel_name") or context.get("channelName")
    request_path = context.get("request_path") or context.get("requestPath")
    status_code = context.get("status_code") or context.get("statusCode")
    request_id = context.get("request_id") or context.get("requestId")
    error_code = context.get("error_code") or context.get("errorCode")
    if channel_name:
        details.append(channel_name)
    if request_path:
        details.append(request_path)
    if status_code:
        details.append(f"HTTP {status_code}")
    if error_code:
        details.append(error_code)
    if request_id:
        details.append(f"request_id {request_id}")
    suffix = f"（{'，'.join(details)}）" if details else ""
    return (
        f"上游模型通道返回 openai_error{suffix}。"
        "这通常表示中转站渠道或上游模型没有返回兼容结果，不是画布参数解析错误；"
        "请切换同类模型，或在中转站后台检查该模型渠道。"
    )


def _as_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


RESULT_ORIGINAL_MEDIA_URL_KEYS = (
    "original_url",
    "originalUrl",
    "download_url",
    "downloadUrl",
    "result_url",
    "resultUrl",
    "output_url",
    "outputUrl",
    "file_url",
    "fileUrl",
    "media_url",
    "mediaUrl",
    "assetUrl",
    "asset_url",
    "image_url",
    "imageUrl",
    "video_url",
    "videoUrl",
    "audio_url",
    "audioUrl",
    "resource_url",
    "resourceUrl",
)

RESULT_FALLBACK_MEDIA_URL_KEYS = (
    "url",
    "src",
    "preview_url",
    "previewUrl",
    "poster_url",
    "posterUrl",
    "thumb_url",
    "thumbUrl",
    "thumbnail_url",
    "thumbnailUrl",
    "thumb",
    "thumbnail",
)

RESULT_MEDIA_URL_KEYS = RESULT_ORIGINAL_MEDIA_URL_KEYS + RESULT_FALLBACK_MEDIA_URL_KEYS


def _looks_like_media_url(value: Any) -> bool:
    text = _as_text(value) or ""
    return bool(re.match(r"^(https?://|data:|blob:|/assets/)", text, re.I))


def _media_kind_from_url(value: str) -> str:
    text = str(value or "").split("?", 1)[0].split("#", 1)[0].lower()
    if text.startswith("data:image/") or text.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
        return "image"
    if text.startswith("data:video/") or text.endswith((".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".m3u8")):
        return "video"
    if text.startswith("data:audio/") or text.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
        return "audio"
    return ""


def _collect_urls(value: Any, bucket: List[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_urls(item, bucket)
        return
    if isinstance(value, str):
        for match in re.findall(r"https?://[^\s<>()\"']+", value):
            text = match.rstrip(".,;，。)]}】")
            if text and text not in bucket:
                bucket.append(text)
        return
    if not isinstance(value, dict):
        return
    if _is_preview_media_item(value):
        return
    for key in RESULT_MEDIA_URL_KEYS:
        text = _as_text(value.get(key))
        if text and _looks_like_media_url(text) and text not in bucket:
            bucket.append(text)
    for key in ("urls", "images", "videos", "resource_list", "resourceList", "items", "data", "result", "task_result", "choices", "message", "content", "output_text", "outputText", "text", "metadata"):
        if key in value:
            _collect_urls(value.get(key), bucket)


def _extract_urls(value: Any) -> List[str]:
    urls: List[str] = []
    _collect_urls(value, urls)
    return urls


def _append_unique_url(bucket: List[str], value: Any, *, media_kind: Optional[str] = None) -> None:
    text = _as_text(value)
    if not text or not _looks_like_media_url(text):
        return
    if media_kind:
        inferred = _media_kind_from_url(text)
        if inferred and inferred != media_kind.lower():
            return
    if text not in bucket:
        bucket.append(text)


def _append_first_result_url_from_keys(
    bucket: List[str],
    value: Dict[str, Any],
    keys: tuple[str, ...],
    *,
    media_kind: Optional[str] = None,
) -> bool:
    for key in keys:
        before = len(bucket)
        _append_unique_url(bucket, value.get(key), media_kind=media_kind)
        if len(bucket) > before:
            return True
    return False


def _media_item_matches(item: Dict[str, Any], media_kind: Optional[str]) -> bool:
    if not media_kind:
        return True
    item_type = str(item.get("type") or item.get("kind") or item.get("assetKind") or "").strip().lower()
    if not item_type:
        return True
    media_kind = media_kind.lower()
    return item_type == media_kind or item_type.startswith(f"{media_kind}/") or media_kind in item_type


def _collect_result_media_urls(value: Any, bucket: List[str], *, media_kind: Optional[str], output_format: str) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_result_media_urls(item, bucket, media_kind=media_kind, output_format=output_format)
        return
    if isinstance(value, str):
        _append_unique_url(bucket, value, media_kind=media_kind)
        return
    if not isinstance(value, dict):
        return
    if _is_preview_media_item(value):
        return

    if _media_item_matches(value, media_kind):
        has_original_url = _append_first_result_url_from_keys(
            bucket,
            value,
            RESULT_ORIGINAL_MEDIA_URL_KEYS,
            media_kind=media_kind,
        )
        if not has_original_url:
            _append_first_result_url_from_keys(
                bucket,
                value,
                RESULT_FALLBACK_MEDIA_URL_KEYS,
                media_kind=media_kind,
            )
        if not has_original_url and media_kind in {None, "image"}:
            b64_json = _as_text(value.get("b64_json"))
            if b64_json:
                _append_unique_url(bucket, f"data:{_image_mime(output_format)};base64,{b64_json}", media_kind=media_kind)

    for key in ("items", "images", "videos", "audios", "files", "outputs", "resource_list", "resourceList"):
        if key in value:
            _collect_result_media_urls(value.get(key), bucket, media_kind=media_kind, output_format=output_format)


def _extract_result_media_urls(payload: Any, *, media_kind: Optional[str] = None, output_format: str = "png") -> List[str]:
    urls: List[str] = []
    if isinstance(payload, dict):
        _append_first_result_url_from_keys(
            urls,
            payload,
            RESULT_ORIGINAL_MEDIA_URL_KEYS,
            media_kind=media_kind,
        )
        if urls:
            return urls
        for key in ("result", "task_result", "taskResult", "output", "outputs", "data", "response", "items", "resource_list", "resourceList", "metadata"):
            if key in payload:
                _collect_result_media_urls(payload.get(key), urls, media_kind=media_kind, output_format=output_format)
        if not urls:
            _append_first_result_url_from_keys(
                urls,
                payload,
                RESULT_FALLBACK_MEDIA_URL_KEYS,
                media_kind=media_kind,
            )
    else:
        _collect_result_media_urls(payload, urls, media_kind=media_kind, output_format=output_format)
    return urls


def _extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested2 = nested.get("data") if isinstance(nested.get("data"), dict) else {}
    for source in (payload, nested, nested2):
        for key in ("requestId", "request_id", "taskId", "task_id", "id", "result"):
            text = _as_text(source.get(key))
            if text:
                return text
    data_text = _as_text(payload.get("data"))
    return data_text


def _normalize_status(value: Any) -> str:
    text = (_as_text(value) or "").lower()
    if text in {"completed", "complete", "succeeded", "succeed", "success", "done", "finished"}:
        return "completed"
    if text in {"failed", "fail", "failure", "error", "timeout", "timedout", "timed_out", "canceled", "cancelled"}:
        return "failed"
    if text in {"running", "processing", "generating", "submitted", "queued", "pending", "in_progress", "queueing", "submitting"}:
        return "running"
    return "running"


def _extract_status(payload: Dict[str, Any]) -> str:
    candidates = [payload]
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    if isinstance(payload.get("task"), dict):
        candidates.append(payload["task"])
    for source in candidates:
        for key in ("status", "task_status", "state"):
            if source.get(key) is not None:
                return _normalize_status(source.get(key))
    return "running"


def _remote_task_error_message(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return _error_message(payload, "Provider task lookup failed")
    success_codes = {"0", "200", "201", "202", "success", "succeeded", "ok"}
    for key in ("code", "status_code", "statusCode"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in success_codes:
            return _error_message(payload, f"Provider task lookup failed: {text}")
    error_value = payload.get("error")
    if isinstance(error_value, dict):
        message = _error_message(error_value, "")
        if message:
            return message
    elif isinstance(error_value, str) and error_value.strip():
        return error_value.strip()
    return None


GPT_IMAGE_MIN_PIXELS = 655_360
GPT_IMAGE_MAX_PIXELS = 8_294_400
GPT_IMAGE_MAX_EDGE = 3840
GPT_IMAGE_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$", re.IGNORECASE)
GPT_IMAGE_RATIO_SIZE_MAP: Dict[str, Dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024",
        "16:9": "1536x864",
        "9:16": "864x1536",
        "4:3": "1536x1152",
        "3:4": "1152x1536",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "2:1": "1536x768",
        "1:2": "768x1536",
    },
    "2K": {
        "1:1": "2048x2048",
        "16:9": "2048x1152",
        "9:16": "1152x2048",
        "4:3": "2048x1536",
        "3:4": "1536x2048",
        "2:1": "2048x1024",
        "1:2": "1024x2048",
    },
    "4K": {
        "1:1": "2880x2880",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "4:3": "3264x2448",
        "3:4": "2448x3264",
        "2:1": "3840x1920",
        "1:2": "1920x3840",
    },
}


def _gpt_image_resolution_label(value: Any) -> Optional[str]:
    text = _as_text(value)
    if not text:
        return None
    normalized = text.upper().replace(" ", "")
    if normalized in {"1K", "HD", "1024", "1024P"}:
        return "1K"
    if normalized in {"2K", "QHD", "1440P", "2048", "2048P"}:
        return "2K"
    if normalized in {"4K", "UHD", "2160P", "3840", "3840P"}:
        return "4K"
    return None


def _parse_aspect_ratio(value: Any) -> Optional[tuple[float, float]]:
    text = _as_text(value)
    if not text:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$", text.replace("：", ":"))
    if not match:
        return None
    width = float(match.group(1))
    height = float(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _is_valid_gpt_image_size(value: str) -> bool:
    match = GPT_IMAGE_SIZE_RE.match(value.strip())
    if not match:
        return False
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return False
    if max(width, height) > GPT_IMAGE_MAX_EDGE:
        return False
    if width % 16 != 0 or height % 16 != 0:
        return False
    if max(width, height) / min(width, height) > 3:
        return False
    pixels = width * height
    return GPT_IMAGE_MIN_PIXELS <= pixels <= GPT_IMAGE_MAX_PIXELS


def _round_to_image_multiple(value: float) -> int:
    return max(16, int(round(value / 16.0)) * 16)


def _floor_to_image_multiple(value: float) -> int:
    return max(16, int(math.floor(value / 16.0)) * 16)


def _fit_gpt_image_dimensions(width: int, height: int) -> tuple[int, int]:
    width = min(width, GPT_IMAGE_MAX_EDGE)
    height = min(height, GPT_IMAGE_MAX_EDGE)

    if max(width, height) / min(width, height) > 3:
        if width >= height:
            height = _round_to_image_multiple(width / 3)
        else:
            width = _round_to_image_multiple(height / 3)

    pixels = width * height
    if pixels > GPT_IMAGE_MAX_PIXELS:
        scale = math.sqrt(GPT_IMAGE_MAX_PIXELS / pixels)
        width = _floor_to_image_multiple(width * scale)
        height = _floor_to_image_multiple(height * scale)

    while width * height > GPT_IMAGE_MAX_PIXELS:
        if width >= height and width > 16:
            width -= 16
        elif height > 16:
            height -= 16
        else:
            break

    return width, height


def _gpt_image_size_from_resolution(resolution: Optional[str], ratio: str) -> Optional[str]:
    label = _gpt_image_resolution_label(resolution)
    if label is None:
        return None
    ratio_key = _normalized_ratio(ratio)
    mapped = GPT_IMAGE_RATIO_SIZE_MAP.get(label, {}).get(ratio_key)
    if mapped:
        return mapped

    ratio_dims = _parse_aspect_ratio(ratio) or (1.0, 1.0)
    ratio_width, ratio_height = ratio_dims
    target_long_edge = {"1K": 1536, "2K": 2048, "4K": 3840}.get(label, 1536)

    if ratio_width >= ratio_height:
        width = target_long_edge
        height = _round_to_image_multiple(target_long_edge * ratio_height / ratio_width)
    else:
        height = target_long_edge
        width = _round_to_image_multiple(target_long_edge * ratio_width / ratio_height)

    width, height = _fit_gpt_image_dimensions(width, height)
    return f"{width}x{height}" if _is_valid_gpt_image_size(f"{width}x{height}") else None


def _normalize_gpt_image_size(size: Optional[str], ratio: str, resolution: Optional[str] = None) -> str:
    normalized = (size or "").strip()
    if normalized.lower() == "auto":
        return "auto"
    if normalized and _is_valid_gpt_image_size(normalized):
        return normalized
    mapped = _gpt_image_size_from_resolution(resolution, ratio) or _gpt_image_size_from_resolution(normalized, ratio)
    if mapped:
        return mapped
    if ratio == "1:1":
        return "1024x1024"
    if ratio in {"9:16", "3:4", "2:3"}:
        return "1024x1536"
    return "1536x1024"


def _gpt_image_partial_images(payload: Dict[str, Any]) -> int:
    value = payload.get("partialImages")
    if value is None:
        value = payload.get("partial_images")
    if value is None:
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(3, parsed))


NANOBANANA2_IMAGE_SIZES: Dict[str, Dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024",
        "1:4": "512x2048",
        "1:8": "384x3072",
        "2:3": "848x1264",
        "3:2": "1264x848",
        "3:4": "896x1200",
        "4:1": "2048x512",
        "4:3": "1200x896",
        "4:5": "928x1152",
        "5:4": "1152x928",
        "8:1": "3072x384",
        "9:16": "768x1376",
        "16:9": "1376x768",
        "21:9": "1584x672",
    },
    "2K": {
        "1:1": "2048x2048",
        "1:4": "1024x4096",
        "1:8": "768x6144",
        "2:3": "1696x2528",
        "3:2": "2528x1696",
        "3:4": "1792x2400",
        "4:1": "4096x1024",
        "4:3": "2400x1792",
        "4:5": "1856x2304",
        "5:4": "2304x1856",
        "8:1": "6144x768",
        "9:16": "1536x2752",
        "16:9": "2752x1536",
        "21:9": "3168x1344",
    },
    "4K": {
        "1:1": "4096x4096",
        "1:4": "2048x8192",
        "1:8": "1536x12288",
        "2:3": "3392x5056",
        "3:2": "5056x3392",
        "3:4": "3584x4800",
        "4:1": "8192x2048",
        "4:3": "4800x3584",
        "4:5": "3712x4608",
        "5:4": "4608x3712",
        "8:1": "12288x1536",
        "9:16": "3072x5504",
        "16:9": "5504x3072",
        "21:9": "6336x2688",
    },
}
NANOBANANA_SIZE_MODEL_NAMES = {"nanobanana2", "nanobananapro"}


def _normalized_ratio(value: Any) -> str:
    return str(value or "").strip().replace("/", ":").replace("_", ":").replace(" ", "")


def _normalized_resolution(value: Any, default: str = "1K") -> str:
    text = str(value or "").strip().upper()
    return text if text in {"1K", "2K", "4K"} else default


def _nanobanana_size_table(model_name: str) -> Optional[Dict[str, Dict[str, str]]]:
    normalized = model_name.strip().lower()
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    normalized = normalized.replace("-", "").replace("_", "").replace(" ", "")
    if normalized in NANOBANANA_SIZE_MODEL_NAMES:
        return NANOBANANA2_IMAGE_SIZES
    return None


def _newapi_image_size_from_ratio_resolution(model_name: str, ratio: Any, resolution: Any) -> Optional[str]:
    table = _nanobanana_size_table(model_name)
    if not table:
        return None
    output_resolution = _normalized_resolution(resolution)
    aspect_ratio = _normalized_ratio(ratio) or "1:1"
    return table.get(output_resolution, {}).get(aspect_ratio)


def _newapi_image_quality_from_resolution(resolution: Any) -> str:
    return "hd" if _normalized_resolution(resolution) in {"2K", "4K"} else "standard"


def _xinghe_stable_image_resolution(model_name: str) -> str:
    normalized = model_name.strip().lower().replace("（", "(").replace("）", ")").replace(" ", "")
    if "gpt-image-2" not in normalized:
        return ""
    is_stable_alias = "稳定版" in normalized
    if "4k" in normalized:
        return "4K" if is_stable_alias or "gpt-image-2-4k" in normalized else ""
    if "2k" in normalized:
        return "2K" if is_stable_alias or "gpt-image-2-2k" in normalized else ""
    if "1k" in normalized or is_stable_alias:
        return "1K"
    return ""


def _is_legacy_gpt_image_model(model_name: str) -> bool:
    return model_name.strip().lower() == "gpt-image-2"


def _is_otu_gpt_image_video_model(model: Dict[str, Any]) -> bool:
    model_name = str(model.get("modelName") or model.get("model") or model.get("id") or "").strip()
    normalized = model_name.lower().replace("（", "(").replace("）", ")").replace(" ", "")
    if "gpt-image-2" not in normalized or "稳定版" not in normalized:
        return False
    return bool(re.fullmatch(r"gpt-image-2(?:-(?:1k|2k|4k))?\(.*稳定版(?:1k|2k|4k).*\)", normalized))


def _otu_gpt_image_video_aspect_ratio(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    requested = _normalized_ratio(_first_payload_value(payload, "aspectRatio", "aspect_ratio", "ratio"))
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    supported = [
        _normalized_ratio(item)
        for item in (params.get("ratios") if isinstance(params.get("ratios"), list) else [])
        if _normalized_ratio(item)
    ]
    default_ratio = _normalized_ratio(params.get("defaultRatio") or params.get("defaultAspectRatio")) or "1:1"
    if requested and (not supported or requested in supported):
        return requested
    if default_ratio and (not supported or default_ratio in supported):
        return default_ratio
    return supported[0] if supported else "1:1"


async def _run_otu_gpt_image_video_request(
    base: str,
    api_key: str,
    model: Dict[str, Any],
    payload: Dict[str, Any],
    progress: ProgressCallback,
    image_timeout: float,
) -> Dict[str, Any]:
    references = _image_reference_inputs(payload, _as_text(payload.get("_backendBaseUrl")))
    reference_limit = _model_reference_limit(model, "maxReferenceImages", DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT)
    if len(references) > reference_limit:
        model_name = str(model.get("modelName") or model.get("id") or "gpt-image-2").strip()
        raise ProviderAdapterError(f"{model_name} 最多支持 {reference_limit} 张参考图，当前提供了 {len(references)} 张")
    reference_urls = [
        await _normalize_json_image_reference(str(item).strip(), payload, index)
        for index, item in enumerate(references[:reference_limit])
        if str(item).strip()
    ]
    body = {
        "model": str(model.get("modelName") or payload.get("modelName") or payload.get("model") or model.get("id") or "").strip(),
        "prompt": _effective_prompt(payload),
        "metadata": {
            "aspect_ratio": _otu_gpt_image_video_aspect_ratio(model, payload),
            "urls": reference_urls,
        },
    }
    await progress(12, "submitting")
    response = await _json_request("POST", f"{base}/v1/videos", api_key, body=body, timeout=image_timeout)
    output_format = _api_image_format(payload.get("outputFormat") or payload.get("output_format") or payload.get("format") or "png")
    urls = _extract_result_media_urls(response, media_kind="image", output_format=output_format) or _extract_urls(response)
    status = _extract_status(response)
    task_id = _extract_task_id(response)
    if status == "completed" and urls:
        return {
            "provider": "",
            "providerModelId": model["id"],
            "status": "completed",
            "url": urls[0],
            "urls": urls,
            "assetKind": "image",
            "raw": response,
        }
    if not task_id:
        raise ProviderAdapterError(_error_message(response, "GPT Image 2 异步图片接口未返回任务 ID 或结果 URL"))
    status_result = await _poll_remote_task(
        status_url=f"{base}/v1/videos/{task_id}",
        api_key=api_key,
        progress=progress,
        timeout_seconds=int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, image_timeout)),
        request_timeout_seconds=image_timeout,
        media_kind="image",
        output_format=output_format,
    )
    return {
        **status_result,
        "provider": "",
        "providerModelId": model["id"],
        "remoteTaskId": task_id,
        "assetKind": "image",
    }


def _gpt_image_async_mode(model: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    value = _first_payload_value(payload, "asyncMode", "async_mode", "upstreamAsync", "upstream_async")
    if value is None:
        value = _first_model_param(model, "asyncMode", "async_mode", "upstreamAsync", "upstream_async")
    if value is not None:
        return _truthy_value(value)
    return _env_bool("LIBAI_GPT_IMAGE_2_ASYNC_MODE", False) or _env_bool("LIBAI_GPT_IMAGE2_ASYNC_MODE", False)


def _gpt_image_non_stream_body(body: Dict[str, Any]) -> Dict[str, Any]:
    request_body = dict(body)
    request_body.pop("stream", None)
    request_body.pop("partial_images", None)
    return request_body


def _gpt_image_response_has_result_or_task(payload: Dict[str, Any], output_format: str) -> bool:
    if _extract_task_id(payload):
        return True
    if _extract_result_media_urls(payload, media_kind="image", output_format=output_format):
        return True
    if _extract_openai_image_urls(payload, output_format):
        return True
    return bool(_extract_urls(payload))


def _provider_key(provider: Dict[str, Any]) -> str:
    key = str(provider.get("apiKey") or "").strip()
    if not key:
        raise ProviderAdapterError("供应商 API Key 未配置")
    return key


def _int_choice(value: Any, default: int, allowed: set[int]) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    return parsed if parsed in allowed else default


def _normalize_image_format(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = text.split(";", 1)[0]
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = text.lstrip(".")
    if text in {"jpeg", "jfif"}:
        text = "jpg"
    return text if text in {"png", "jpg", "webp"} else None


def _output_format(value: Any = None, input_path: Optional[Path] = None) -> str:
    explicit = _normalize_image_format(value)
    if explicit:
        return explicit
    if input_path:
        suffix_format = _normalize_image_format(input_path.suffix)
        if suffix_format:
            return suffix_format
        mime_format = _normalize_image_format(mimetypes.guess_type(str(input_path))[0])
        if mime_format:
            return mime_format
    return "png"


def _guess_suffix(mime: Optional[str], fallback: str = ".png") -> str:
    guessed = mimetypes.guess_extension(mime or "") or fallback
    return guessed if re.fullmatch(r"\.[a-z0-9]{1,10}", guessed) else fallback


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _backend_base_url() -> str:
    port = str(os.environ.get("LIBAI_BACKEND_PORT") or "8765").strip() or "8765"
    return f"http://127.0.0.1:{port}"


def _find_realesrgan_binary(payload: Dict[str, Any]) -> Optional[Path]:
    names = ["realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"]
    candidates: List[Path] = []
    explicit = _as_text(payload.get("enginePath") or payload.get("engine_path"))
    if explicit:
        candidates.append(Path(explicit))
    env_value = os.environ.get("LIBAI_REALESRGAN_PATH")
    if env_value:
        candidates.append(Path(env_value))
    root = _project_root()
    for base in (
        root / "tools" / "realesrgan-ncnn-vulkan",
        root / "tools",
        root / "bin" / "realesrgan-ncnn-vulkan",
        root / "bin",
        root / "backend" / "tools" / "realesrgan-ncnn-vulkan",
    ):
        for name in names:
            candidates.append(base / name)
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for item in candidates:
        try:
            if item.exists() and item.is_file():
                return item
        except OSError:
            continue
    return None


def _normalize_upscale_model(payload: Dict[str, Any]) -> str:
    raw = str(
        payload.get("upscaleModel")
        or payload.get("modelName")
        or payload.get("model")
        or payload.get("contentType")
        or payload.get("content_type")
        or ""
    ).strip()
    lowered = raw.lower()
    known = {
        "realesrgan-x4plus",
        "realesrgan-x4plus-anime",
        "realesrnet-x4plus",
        "realesr-animevideov3",
    }
    if lowered in known:
        return lowered
    if any(keyword in lowered for keyword in ("anime", "illustration", "comic")) or any(keyword in raw for keyword in ("动漫", "插画", "漫画", "二次元")):
        return "realesrgan-x4plus-anime"
    if any(keyword in lowered for keyword in ("portrait", "photo", "realistic", "person", "people")) or any(keyword in raw for keyword in ("真人", "照片", "人像", "写实")):
        return "realesrgan-x4plus"
    return "realesrgan-x4plus"


def _read_image_size(input_path: Path) -> Optional[tuple[int, int]]:
    try:
        with safe_image_open(input_path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None


def _auto_tile_size(input_path: Path, scale: int) -> int:
    size = _read_image_size(input_path)
    if not size:
        return 0
    width, height = size
    target_pixels = max(1, width) * max(1, height) * max(1, scale) * max(1, scale)
    target_side = max(width, height) * max(1, scale)
    if target_pixels >= 48_000_000 or target_side >= 8000:
        return 128
    if target_pixels >= 20_000_000 or target_side >= 5200:
        return 256
    return 0


def _tile_size(payload: Dict[str, Any], input_path: Path, scale: int) -> int:
    explicit = payload.get("tile") if payload.get("tile") is not None else payload.get("tileSize")
    explicit_text = str(explicit).strip().lower() if explicit is not None else ""
    if explicit_text and explicit_text not in {"0", "auto", "automatic", "none"}:
        return _int_choice(explicit, 0, {0, 128, 256, 512})
    return _auto_tile_size(input_path, scale)


def _output_dir(payload: Dict[str, Any]) -> Path:
    base = _as_text(payload.get("_projectCacheDir")) or tempfile.gettempdir()
    root = Path(base) / "upscale"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _decode_data_url(data_url: str, target_dir: Path) -> Path:
    if "," not in data_url:
        raise ProviderAdapterError("图片 data URL 无效")
    header, encoded = data_url.split(",", 1)
    mime = None
    if header.startswith("data:"):
        mime = header[5:].split(";", 1)[0] or None
    try:
        raw = base64.b64decode(encoded)
    except Exception as error:
        raise ProviderAdapterError("图片 data URL 解析失败") from error
    target = target_dir / f"input_{uuid.uuid4().hex[:10]}{_guess_suffix(mime)}"
    target.write_bytes(raw)
    return target


def _path_from_file_url(url: str) -> Optional[Path]:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path_text = url2pathname(unquote(parsed.path or ""))
    if re.match(r"^/[A-Za-z]:/", path_text):
        path_text = path_text[1:]
    candidate = Path(path_text)
    return candidate if candidate.exists() and candidate.is_file() else None


def _path_from_libai_asset_url(url: str) -> Optional[Path]:
    parsed = urlparse(url)
    if parsed.scheme != "libai-asset":
        return None
    raw_path = unquote(parsed.path or "")
    if parsed.netloc:
        if re.fullmatch(r"[A-Za-z]:", parsed.netloc):
            path_text = f"{parsed.netloc}{raw_path}"
        else:
            path_text = f"//{parsed.netloc}{raw_path}"
    else:
        path_text = raw_path
    path_text = url2pathname(path_text)
    if re.match(r"^/[A-Za-z]:[\\/]", path_text):
        path_text = path_text[1:]
    candidate = Path(path_text)
    return candidate if candidate.exists() and candidate.is_file() else None


async def _download_image(url: str, target_dir: Path, *, allow_local_asset: bool = False) -> Path:
    try:
        response = await public_http_get(
            url,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            trust_env=_http_trust_env(),
            allow_private=allow_local_asset and is_local_backend_asset_url(url),
        )
    except UnsafeRemoteUrlError as error:
        raise ProviderAdapterError(unsafe_remote_url_message("图片下载失败")) from error
    except Exception as error:
        message = str(error).strip() or type(error).__name__
        raise ProviderAdapterError(f"图片下载失败：{message}") from error
    if not response.is_success:
        raise ProviderAdapterError(f"图片下载失败：HTTP {response.status_code}")
    mime = response.headers.get("content-type", "").split(";", 1)[0] or None
    target = target_dir / f"input_{uuid.uuid4().hex[:10]}{_guess_suffix(mime)}"
    target.write_bytes(response.content)
    return target


async def _download_reference_file(
    url: str,
    target_dir: Path,
    kind: str = "file",
    *,
    allow_local_asset: bool = False,
) -> Path:
    try:
        response = await public_http_get(
            url,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            trust_env=_http_trust_env(),
            allow_private=allow_local_asset and is_local_backend_asset_url(url),
        )
    except UnsafeRemoteUrlError as error:
        raise ProviderAdapterError(unsafe_remote_url_message("素材下载失败")) from error
    except Exception as error:
        message = str(error).strip() or type(error).__name__
        raise ProviderAdapterError(f"素材下载失败：{message}") from error
    if not response.is_success:
        raise ProviderAdapterError(f"素材下载失败：HTTP {response.status_code}")
    mime = response.headers.get("content-type", "").split(";", 1)[0] or None
    target = target_dir / f"{kind}_{uuid.uuid4().hex[:10]}{_guess_suffix(mime, '.bin')}"
    target.write_bytes(response.content)
    return target


def _local_asset_url(source: str, backend_base_url: Optional[str]) -> str:
    base = (backend_base_url or _backend_base_url()).rstrip("/")
    return f"{base}{source}"


def _asset_original_url_from_thumb_url(source: str) -> str:
    text = _as_text(source)
    if not text:
        return ""
    match = re.match(r"^(?P<asset>https?://[^?#]+?/assets/[A-Za-z0-9_-]+)/thumb(?:[?#].*)?$", text, re.I)
    if match:
        return match.group("asset")
    match = re.match(r"^(?P<asset>/assets/[A-Za-z0-9_-]+)/thumb(?:[?#].*)?$", text)
    if match:
        return match.group("asset")
    return ""


async def _image_reference_to_path(value: str, target_dir: Path, index: int, backend_base_url: Optional[str] = None) -> Path:
    source = _as_text(value)
    if not source:
        raise ProviderAdapterError("参考图为空")
    if source.startswith("data:image/"):
        return _decode_data_url(source, target_dir)
    if source.startswith("libai-asset:"):
        asset_path = _path_from_libai_asset_url(source)
        if asset_path:
            return asset_path
    if source.startswith("file:"):
        file_path = _path_from_file_url(source)
        if file_path:
            return file_path
    allow_local_asset = False
    if source.startswith("/assets/"):
        source = _local_asset_url(source, backend_base_url)
        allow_local_asset = is_local_backend_asset_url(source)
    if re.match(r"^https?://", source, re.I):
        return await _download_image(source, target_dir, allow_local_asset=allow_local_asset or is_local_backend_asset_url(source))
    candidate = Path(source)
    if candidate.exists() and candidate.is_file():
        return candidate
    raise ProviderAdapterError(f"无法读取第 {index + 1} 张参考图")


def _parse_bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _reference_image_hosting_enabled(payload: Dict[str, Any]) -> bool:
    for key in (
        "referenceImageHostingEnabled",
        "reference_image_hosting_enabled",
        "imageReferenceHostingEnabled",
        "image_reference_hosting_enabled",
    ):
        if key in payload:
            return _parse_bool_value(payload.get(key), True)
    return _env_bool("LIBAI_REFERENCE_IMAGE_HOSTING_ENABLED", True)


def _reference_image_upload_url(payload: Dict[str, Any]) -> str:
    return _as_text(
        payload.get("referenceImageUploadUrl")
        or payload.get("reference_image_upload_url")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_UPLOAD_URL")
        or DEFAULT_REFERENCE_IMAGE_HOSTING_UPLOAD_URL
    )


def _has_explicit_reference_image_upload_url(payload: Dict[str, Any]) -> bool:
    return bool(_as_text(payload.get("referenceImageUploadUrl") or payload.get("reference_image_upload_url")))


def _reference_image_require_s3(payload: Dict[str, Any]) -> bool:
    for key in (
        "referenceImageRequireStorage",
        "reference_image_require_storage",
        "referenceImageRequireS3",
        "reference_image_require_s3",
        "referenceImageRequireRainyun",
        "reference_image_require_rainyun",
    ):
        if key in payload:
            return _parse_bool_value(payload.get(key), True)
    return _env_bool("LIBAI_REFERENCE_IMAGE_REQUIRE_S3", True)


def _reference_image_s3_config(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    provider = _as_text(
        payload.get("referenceStorageProvider")
        or payload.get("reference_storage_provider")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_PROVIDER")
        or "s3-compatible"
    ) or "s3-compatible"
    endpoint = (_as_text(
        payload.get("referenceStorageS3Endpoint")
        or payload.get("reference_storage_s3_endpoint")
        or payload.get("referenceStorageEndpoint")
        or payload.get("reference_storage_endpoint")
        or payload.get("referenceImageS3Endpoint")
        or payload.get("reference_image_s3_endpoint")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_S3_ENDPOINT")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_ENDPOINT")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_ENDPOINT")
        or ""
    ) or "").rstrip("/")
    bucket = (_as_text(
        payload.get("referenceStorageBucket")
        or payload.get("reference_storage_bucket")
        or payload.get("referenceImageS3Bucket")
        or payload.get("reference_image_s3_bucket")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_BUCKET")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_BUCKET")
        or ""
    ) or "").strip("/")
    access_key = _as_text(
        payload.get("referenceStorageAccessKey")
        or payload.get("reference_storage_access_key")
        or payload.get("referenceImageS3AccessKey")
        or payload.get("reference_image_s3_access_key")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_ACCESS_KEY")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_ACCESS_KEY")
        or ""
    ) or ""
    secret_key = _as_text(
        payload.get("referenceStorageSecretKey")
        or payload.get("reference_storage_secret_key")
        or payload.get("referenceImageS3SecretKey")
        or payload.get("reference_image_s3_secret_key")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_SECRET_KEY")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_SECRET_KEY")
        or ""
    ) or ""
    if not (endpoint and bucket and access_key and secret_key):
        return None
    public_base_url = (_as_text(
        payload.get("referenceStoragePublicBaseUrl")
        or payload.get("reference_storage_public_base_url")
        or payload.get("referenceImageS3PublicBaseUrl")
        or payload.get("reference_image_s3_public_base_url")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_PUBLIC_BASE_URL")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_PUBLIC_BASE_URL")
        or ""
    ) or "").rstrip("/")
    region = _as_text(
        payload.get("referenceStorageRegion")
        or payload.get("reference_storage_region")
        or payload.get("referenceImageS3Region")
        or payload.get("reference_image_s3_region")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_REGION")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_REGION")
        or "us-east-1"
    )
    prefix = _as_text(
        payload.get("referenceStorageImagePrefix")
        or payload.get("reference_storage_image_prefix")
        or payload.get("referenceStoragePrefix")
        or payload.get("reference_storage_prefix")
        or payload.get("referenceImageS3Prefix")
        or payload.get("reference_image_s3_prefix")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_IMAGE_PREFIX")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_PREFIX")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_PREFIX")
        or "reference-images"
    ).strip("/")
    addressing_style = _as_text(
        payload.get("referenceStorageAddressingStyle")
        or payload.get("reference_storage_addressing_style")
        or payload.get("referenceImageS3AddressingStyle")
        or payload.get("reference_image_s3_addressing_style")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_ADDRESSING_STYLE")
        or os.environ.get("LIBAI_REFERENCE_IMAGE_S3_ADDRESSING_STYLE")
        or ""
    )
    if addressing_style not in {"path", "virtual"}:
        addressing_style = "virtual" if provider == "tencent-cos" else "path"
    return {
        "provider": provider,
        "endpoint": endpoint,
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "prefix": prefix,
        "public_base_url": public_base_url,
        "addressing_style": addressing_style,
    }


def _reference_media_s3_config(payload: Dict[str, Any], kind: str) -> Optional[Dict[str, str]]:
    config = _reference_image_s3_config(payload)
    if config is None:
        return None
    clean_kind = "audio" if kind == "audio" else "video"
    title = "Audio" if clean_kind == "audio" else "Video"
    prefix = _as_text(
        payload.get(f"reference{title}S3Prefix")
        or payload.get(f"reference_{clean_kind}_s3_prefix")
        or payload.get(f"reference{title}StoragePrefix")
        or payload.get(f"reference_{clean_kind}_storage_prefix")
        or os.environ.get(f"LIBAI_REFERENCE_STORAGE_{clean_kind.upper()}_PREFIX")
        or os.environ.get(f"LIBAI_REFERENCE_{clean_kind.upper()}_S3_PREFIX")
        or payload.get("referenceMediaS3Prefix")
        or payload.get("reference_media_s3_prefix")
        or payload.get("referenceMediaStoragePrefix")
        or payload.get("reference_media_storage_prefix")
        or os.environ.get("LIBAI_REFERENCE_STORAGE_MEDIA_PREFIX")
        or os.environ.get("LIBAI_REFERENCE_MEDIA_S3_PREFIX")
        or f"reference-{clean_kind}s"
    ).strip("/")
    return {**config, "prefix": prefix}


def _reference_image_hosting_timeout() -> float:
    return _env_float("LIBAI_REFERENCE_IMAGE_HOSTING_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_SECONDS)


def _reference_image_hosting_max_bytes() -> int:
    return _env_int("LIBAI_REFERENCE_IMAGE_HOSTING_MAX_BYTES", DEFAULT_REFERENCE_IMAGE_HOSTING_MAX_BYTES)


def _reference_media_hosting_max_bytes(kind: str) -> int:
    clean_kind = "AUDIO" if kind == "audio" else "VIDEO"
    return _env_int(
        f"LIBAI_REFERENCE_{clean_kind}_HOSTING_MAX_BYTES",
        _env_int("LIBAI_REFERENCE_MEDIA_HOSTING_MAX_BYTES", DEFAULT_REFERENCE_MEDIA_HOSTING_MAX_BYTES),
    )


def _reference_image_hosting_concurrency() -> int:
    return max(1, _env_int("LIBAI_REFERENCE_IMAGE_HOSTING_CONCURRENCY", DEFAULT_REFERENCE_IMAGE_HOSTING_CONCURRENCY))


def _reference_image_hosting_retry_attempts() -> int:
    return max(1, _env_int("LIBAI_REFERENCE_IMAGE_HOSTING_RETRY_ATTEMPTS", DEFAULT_REFERENCE_IMAGE_HOSTING_RETRY_ATTEMPTS))


def _reference_image_hosting_retry_delay() -> float:
    return _env_float("LIBAI_REFERENCE_IMAGE_HOSTING_RETRY_DELAY_SECONDS", DEFAULT_REFERENCE_IMAGE_HOSTING_RETRY_DELAY_SECONDS)


def _reference_image_compress_threshold_bytes() -> int:
    return max(0, _env_int("LIBAI_REFERENCE_IMAGE_COMPRESS_THRESHOLD_BYTES", DEFAULT_REFERENCE_IMAGE_COMPRESS_THRESHOLD_BYTES))


def _reference_image_compress_target_bytes() -> int:
    return max(1, _env_int("LIBAI_REFERENCE_IMAGE_COMPRESS_TARGET_BYTES", DEFAULT_REFERENCE_IMAGE_COMPRESS_TARGET_BYTES))


def _reference_image_compress_min_quality() -> int:
    return min(95, max(30, _env_int("LIBAI_REFERENCE_IMAGE_COMPRESS_MIN_QUALITY", DEFAULT_REFERENCE_IMAGE_COMPRESS_MIN_QUALITY)))


def _reference_image_hosting_lock() -> asyncio.Lock:
    global _REFERENCE_IMAGE_HOSTING_LOCK, _REFERENCE_IMAGE_HOSTING_LOCK_LOOP_ID
    loop_id = id(asyncio.get_running_loop())
    if _REFERENCE_IMAGE_HOSTING_LOCK is None or _REFERENCE_IMAGE_HOSTING_LOCK_LOOP_ID != loop_id:
        _REFERENCE_IMAGE_HOSTING_LOCK = asyncio.Lock()
        _REFERENCE_IMAGE_HOSTING_LOCK_LOOP_ID = loop_id
        _REFERENCE_IMAGE_HOSTING_INFLIGHT.clear()
    return _REFERENCE_IMAGE_HOSTING_LOCK


def _reference_image_hosting_semaphore() -> asyncio.Semaphore:
    global _REFERENCE_IMAGE_HOSTING_SEMAPHORE, _REFERENCE_IMAGE_HOSTING_SEMAPHORE_LOOP_ID
    loop_id = id(asyncio.get_running_loop())
    if _REFERENCE_IMAGE_HOSTING_SEMAPHORE is None or _REFERENCE_IMAGE_HOSTING_SEMAPHORE_LOOP_ID != loop_id:
        _REFERENCE_IMAGE_HOSTING_SEMAPHORE = asyncio.Semaphore(_reference_image_hosting_concurrency())
        _REFERENCE_IMAGE_HOSTING_SEMAPHORE_LOOP_ID = loop_id
    return _REFERENCE_IMAGE_HOSTING_SEMAPHORE


def _remember_reference_image_hosting_cache(digest: str, url: str) -> None:
    _REFERENCE_IMAGE_HOSTING_CACHE[digest] = url
    while len(_REFERENCE_IMAGE_HOSTING_CACHE) > REFERENCE_IMAGE_HOSTING_CACHE_LIMIT:
        _REFERENCE_IMAGE_HOSTING_CACHE.pop(next(iter(_REFERENCE_IMAGE_HOSTING_CACHE)))


def _guess_image_mime_from_bytes(raw: bytes, fallback: Optional[str] = None) -> str:
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"BM"):
        return "image/bmp"
    return fallback or "image/png"


def _image_extension_from_mime(mime: str) -> str:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime == "image/jpeg":
        return ".jpg"
    suffix = mimetypes.guess_extension(mime) if mime else None
    return suffix or ".png"


def _s3_path_segment(value: str) -> str:
    return quote(value, safe="")


def _s3_object_path(bucket: str, key: str) -> str:
    return "/" + "/".join(_s3_path_segment(part) for part in [bucket, *key.split("/")])


def _reference_image_s3_key(config: Dict[str, str], digest: str, filename: str, mime: str) -> str:
    extension = Path(filename or "").suffix or _image_extension_from_mime(mime)
    if not extension.startswith("."):
        extension = f".{extension}"
    prefix = config.get("prefix", "").strip("/")
    day = time.strftime("%Y%m%d", time.gmtime())
    name = f"{digest[:24]}{extension.lower()}"
    return "/".join(part for part in (prefix, day, name) if part)


def _media_extension_from_mime(mime: str, kind: str) -> str:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    suffix = mimetypes.guess_extension(mime) if mime else None
    if suffix:
        return suffix
    return ".wav" if kind == "audio" else ".mp4"


def _reference_media_s3_key(config: Dict[str, str], kind: str, digest: str, filename: str, mime: str) -> str:
    extension = Path(filename or "").suffix or _media_extension_from_mime(mime, kind)
    if not extension.startswith("."):
        extension = f".{extension}"
    prefix = config.get("prefix", "").strip("/")
    day = time.strftime("%Y%m%d", time.gmtime())
    name = f"{digest[:24]}{extension.lower()}"
    return "/".join(part for part in (prefix, day, name) if part)


def _reference_image_s3_public_url(config: Dict[str, str], key: str) -> str:
    endpoint = config["endpoint"].rstrip("/")
    bucket = config["bucket"].strip("/")
    public_base_url = config.get("public_base_url", "").rstrip("/")
    base = public_base_url or f"{endpoint}/{_s3_path_segment(bucket)}"
    return f"{base}/{quote(key, safe='/')}"


def _reference_image_jpeg_filename(filename: str, index: int) -> str:
    stem = Path(filename or f"reference-{index}").stem or f"reference-{index}"
    return f"{stem}.jpg"


def _to_jpeg_ready_image(raw: bytes):
    from PIL import Image as PILImage, ImageOps

    with safe_image_open(io.BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = PILImage.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            return background.convert("RGB")
        return image.convert("RGB")


def _encode_reference_image_jpeg(image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    return output.getvalue()


def _compress_reference_image_for_hosting(
    raw: bytes,
    mime: str,
    filename: str,
    index: int,
) -> tuple[bytes, str, str]:
    threshold = _reference_image_compress_threshold_bytes()
    if threshold <= 0 or len(raw) <= threshold:
        return raw, mime, filename

    target_bytes = _reference_image_compress_target_bytes()
    min_quality = _reference_image_compress_min_quality()
    try:
        image = _to_jpeg_ready_image(raw)
    except Exception as error:
        raise ProviderAdapterError(f"第 {index + 1} 张参考图压缩失败：{type(error).__name__}") from error

    best = b""
    quality = 90
    while quality >= min_quality:
        encoded = _encode_reference_image_jpeg(image, quality)
        best = encoded
        if len(encoded) <= target_bytes:
            break
        quality -= 10

    resize_rounds = 0
    while len(best) > target_bytes and resize_rounds < 6 and min(image.size) > 256:
        scale = max(0.5, min(0.95, math.sqrt(target_bytes / max(len(best), 1)) * 0.95))
        next_size = (
            max(256, int(image.size[0] * scale)),
            max(256, int(image.size[1] * scale)),
        )
        if next_size == image.size:
            break
        from PIL import Image as PILImage
        resample = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", 1)
        image = image.resize(next_size, resample)
        best = _encode_reference_image_jpeg(image, max(min_quality, 75))
        resize_rounds += 1

    return best, "image/jpeg", _reference_image_jpeg_filename(filename, index)


def _is_sora2_video_model(model: Dict[str, Any]) -> bool:
    names = (
        model.get("id"),
        model.get("modelName"),
        model.get("model"),
        _first_model_param(model, "modelName", "model"),
    )
    return any(str(name or "").strip().lower() == "sora2" for name in names)


def _sora2_reference_target_size(
    job_type: str,
    payload: Dict[str, Any],
    model: Dict[str, Any],
) -> Optional[tuple[int, int]]:
    if job_type != "video.generate" or not _is_sora2_video_model(model):
        return None
    mode = str(
        _first_payload_value(payload, "generationMode", "generation_mode", "mode")
        or ""
    ).strip().lower()
    if mode and mode not in {"image-video", "image_video", "image-to-video", "image_to_video"}:
        return None
    profile = _public_video_api_profile(model) or {}
    ratio = _public_video_ratio(profile, _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio"))
    if ratio == "9:16":
        return (720, 1280)
    if ratio == "16:9":
        return (1280, 720)
    return None


def _sora2_reference_filename(filename: str, target_size: tuple[int, int], index: int) -> str:
    stem = Path(filename or f"reference-{index}").stem or f"reference-{index}"
    return f"{stem}-sora2-{target_size[0]}x{target_size[1]}.png"


def _fit_sora2_reference_image_bytes(
    raw: bytes,
    mime: str,
    filename: str,
    target_size: tuple[int, int],
    index: int,
) -> tuple[bytes, str, str, bool]:
    from PIL import Image as PILImage, ImageFilter, ImageOps

    try:
        with safe_image_open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            if image.size == target_size:
                return raw, mime or _guess_image_mime_from_bytes(raw), filename, False
            source = image.convert("RGBA")
    except Exception as error:
        raise ProviderAdapterError(f"第 {index + 1} 张 Sora2 参考图尺寸处理失败：{type(error).__name__}") from error

    resample = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", 1)
    background = ImageOps.fit(source, target_size, method=resample, centering=(0.5, 0.5))
    background = background.filter(ImageFilter.GaussianBlur(radius=max(target_size) / 48)).convert("RGBA")

    scale = min(target_size[0] / max(source.width, 1), target_size[1] / max(source.height, 1))
    foreground_size = (
        max(1, round(source.width * scale)),
        max(1, round(source.height * scale)),
    )
    foreground = source.resize(foreground_size, resample)
    offset = (
        (target_size[0] - foreground.width) // 2,
        (target_size[1] - foreground.height) // 2,
    )
    background.alpha_composite(foreground, offset)

    output = io.BytesIO()
    background.save(output, format="PNG", optimize=True)
    return output.getvalue(), "image/png", _sora2_reference_filename(filename, target_size, index), True


def _model_identity_values(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> List[str]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    values = [
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        model.get("model"),
        params.get("modelName"),
        params.get("upstreamModelName"),
        params.get("model"),
        params.get("videoProtocol"),
    ]
    if isinstance(payload, dict):
        values.extend([
            payload.get("model"),
            payload.get("modelName"),
            payload.get("providerModelId"),
            payload.get("displayName"),
        ])
    normalized: List[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _is_mgdb_nine_image_special_model(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> bool:
    return any(
        value == "mgdb"
        or value == "sd_2.0_fast"
        or value == "seedence（九图特价版）"
        or "九图特价" in value
        or "特价九图" in value
        for value in _model_identity_values(model, payload)
    )


def _parse_aspect_ratio_pair(value: Any) -> Optional[tuple[float, float]]:
    text = _as_text(value).replace("：", ":").replace("/", ":").lower()
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x]\s*(\d+(?:\.\d+)?)\s*", text)
    if not match:
        return None
    width = float(match.group(1))
    height = float(match.group(2))
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        return None
    return width, height


def _ratio_filename_token(value: Any) -> str:
    text = _as_text(value).replace("：", ":").replace("/", ":")
    return re.sub(r"[^A-Za-z0-9]+", "x", text).strip("x") or "ratio"


def _mgdb_nine_image_letterbox_ratio(
    job_type: str,
    payload: Dict[str, Any],
    model: Dict[str, Any],
) -> Optional[str]:
    if job_type != "video.generate" or not _is_mgdb_nine_image_special_model(model, payload):
        return None
    ratio = (
        _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio")
        or _first_model_param(model, "defaultRatio", "ratio", "aspectRatio", "aspect_ratio")
        or "16:9"
    )
    return _as_text(ratio) or "16:9"


def _mgdb_nine_image_letterbox_filename(filename: str, ratio: str, index: int) -> str:
    stem = Path(filename or f"reference-{index}").stem or f"reference-{index}"
    return f"{stem}-letterbox-{_ratio_filename_token(ratio)}.png"


def _letterbox_reference_image_bytes(
    raw: bytes,
    mime: str,
    filename: str,
    ratio: str,
    index: int,
) -> tuple[bytes, str, str, bool]:
    from PIL import Image as PILImage, ImageOps

    parsed_ratio = _parse_aspect_ratio_pair(ratio)
    if parsed_ratio is None:
        return raw, mime or _guess_image_mime_from_bytes(raw), filename, False
    ratio_w, ratio_h = parsed_ratio
    target_ratio = ratio_w / ratio_h
    try:
        with safe_image_open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            source = image.convert("RGBA")
    except Exception as error:
        raise ProviderAdapterError(f"第 {index + 1} 张参考图补黑边失败：{type(error).__name__}") from error

    width, height = source.size
    if width <= 0 or height <= 0:
        raise ProviderAdapterError(f"第 {index + 1} 张参考图尺寸无效")
    current_ratio = width / height
    if abs(current_ratio - target_ratio) < 0.001:
        return raw, mime or _guess_image_mime_from_bytes(raw), filename, False

    scale = max(1, math.ceil(max(width / ratio_w, height / ratio_h)))
    canvas_width = max(width, int(math.ceil(scale * ratio_w)))
    canvas_height = max(height, int(math.ceil(scale * ratio_h)))

    canvas = PILImage.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 255))
    offset = ((canvas_width - width) // 2, (canvas_height - height) // 2)
    canvas.alpha_composite(source, offset)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return (
        output.getvalue(),
        "image/png",
        _mgdb_nine_image_letterbox_filename(filename, ratio, index),
        True,
    )


async def _prepare_mgdb_nine_image_reference_for_hosting(
    value: Any,
    payload: Dict[str, Any],
    index: int,
    ratio: str,
) -> Any:
    source = _as_text(value)
    if not source or source.startswith(("asset://", "file_")):
        return value
    cache = payload.setdefault("_mgdbNineImageLetterboxCache", {})
    cache_key = f"{_as_text(ratio)}:{source}"
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    raw, mime, filename = await _read_reference_image_bytes(source, payload, index)
    fitted_raw, fitted_mime, fitted_filename, changed = await asyncio.to_thread(
        _letterbox_reference_image_bytes,
        raw,
        mime,
        filename,
        ratio,
        index,
    )
    result: Any = value
    if changed:
        encoded = base64.b64encode(fitted_raw).decode("ascii")
        result = f"data:{fitted_mime};name={quote(fitted_filename)};base64,{encoded}"
    if isinstance(cache, dict):
        cache[cache_key] = result
    return result


async def _letterbox_mgdb_reference_path(
    path: Path,
    target_dir: Path,
    ratio: str,
    index: int,
) -> Path:
    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or _guess_image_mime_from_bytes(raw)
    fitted_raw, fitted_mime, fitted_filename, changed = await asyncio.to_thread(
        _letterbox_reference_image_bytes,
        raw,
        mime,
        path.name,
        ratio,
        index,
    )
    if not changed:
        return path
    target = target_dir / fitted_filename
    if target.exists():
        target = target_dir / f"{Path(fitted_filename).stem}-{uuid.uuid4().hex[:8]}{Path(fitted_filename).suffix or '.png'}"
    target.write_bytes(fitted_raw)
    return target


async def _prepare_sora2_reference_image_for_hosting(
    value: Any,
    payload: Dict[str, Any],
    index: int,
    target_size: tuple[int, int],
) -> Any:
    source = _as_text(value)
    if not source or source.startswith(("asset://", "file_")):
        return value
    cache = payload.setdefault("_sora2ReferenceFitCache", {})
    cache_key = f"{target_size[0]}x{target_size[1]}:{source}"
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    raw, mime, filename = await _read_reference_image_bytes(source, payload, index)
    fitted_raw, fitted_mime, fitted_filename, changed = await asyncio.to_thread(
        _fit_sora2_reference_image_bytes,
        raw,
        mime,
        filename,
        target_size,
        index,
    )
    result: Any = value
    if changed:
        encoded = base64.b64encode(fitted_raw).decode("ascii")
        result = f"data:{fitted_mime};name={quote(fitted_filename)};base64,{encoded}"
    if isinstance(cache, dict):
        cache[cache_key] = result
    return result


async def _read_reference_image_bytes(
    value: str,
    payload: Dict[str, Any],
    index: int,
) -> tuple[bytes, str, str]:
    source = _as_text(value)
    if not source:
        raise ProviderAdapterError("参考图为空")
    if source.startswith("data:image/"):
        if "," not in source:
            raise ProviderAdapterError("图片 data URL 无效")
        header, encoded = source.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"
        try:
            raw = base64.b64decode(encoded)
        except Exception as error:
            raise ProviderAdapterError("图片 data URL 解析失败") from error
        return raw, mime, f"reference-{index}{_image_extension_from_mime(mime)}"
    if _looks_like_base64_image(source):
        try:
            raw = base64.b64decode(source.replace("-", "+").replace("_", "/"), validate=False)
        except Exception as error:
            raise ProviderAdapterError("图片 base64 解析失败") from error
        mime = _guess_image_mime_from_bytes(raw)
        return raw, mime, f"reference-{index}{_image_extension_from_mime(mime)}"
    if source.startswith("libai-asset:"):
        asset_path = _path_from_libai_asset_url(source)
        if asset_path:
            raw = asset_path.read_bytes()
            mime = mimetypes.guess_type(str(asset_path))[0] or _guess_image_mime_from_bytes(raw)
            return raw, mime, asset_path.name or f"reference-{index}{_image_extension_from_mime(mime)}"
    if source.startswith("file:"):
        file_path = _path_from_file_url(source)
        if file_path:
            raw = file_path.read_bytes()
            mime = mimetypes.guess_type(str(file_path))[0] or _guess_image_mime_from_bytes(raw)
            return raw, mime, file_path.name or f"reference-{index}{_image_extension_from_mime(mime)}"
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    allow_local_asset = False
    download_source = source
    if source.startswith("/assets/"):
        download_source = _local_asset_url(source, backend_base_url)
        allow_local_asset = is_local_backend_asset_url(download_source)
    if re.match(r"^https?://", download_source, re.I):
        try:
            response = await public_http_get(
                download_source,
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                trust_env=_http_trust_env(),
                allow_private=allow_local_asset or is_local_backend_asset_url(download_source),
            )
        except UnsafeRemoteUrlError as error:
            raise ProviderAdapterError(unsafe_remote_url_message("参考图读取失败")) from error
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            raise ProviderAdapterError(f"参考图读取失败：{message}") from error
        if not response.is_success:
            fallback_source = _asset_original_url_from_thumb_url(download_source)
            if response.status_code == 404 and fallback_source and fallback_source != download_source:
                try:
                    fallback_response = await public_http_get(
                        fallback_source,
                        timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                        trust_env=_http_trust_env(),
                        allow_private=allow_local_asset or is_local_backend_asset_url(fallback_source),
                    )
                except UnsafeRemoteUrlError as error:
                    raise ProviderAdapterError(unsafe_remote_url_message("参考图读取失败")) from error
                except Exception as error:
                    message = str(error).strip() or type(error).__name__
                    raise ProviderAdapterError(f"参考图读取失败：{message}") from error
                if fallback_response.is_success:
                    response = fallback_response
                else:
                    raise ProviderAdapterError(f"参考图读取失败：HTTP {fallback_response.status_code}")
            else:
                raise ProviderAdapterError(f"参考图读取失败：HTTP {response.status_code}")
        if not response.is_success:
            raise ProviderAdapterError(f"参考图读取失败：HTTP {response.status_code}")
        raw = response.content
        mime = response.headers.get("content-type", "").split(";", 1)[0] or _guess_image_mime_from_bytes(raw)
        return raw, mime, f"reference-{index}{_image_extension_from_mime(mime)}"
    candidate = Path(source)
    if candidate.exists() and candidate.is_file():
        raw = candidate.read_bytes()
        mime = mimetypes.guess_type(str(candidate))[0] or _guess_image_mime_from_bytes(raw)
        return raw, mime, candidate.name or f"reference-{index}{_image_extension_from_mime(mime)}"
    raise ProviderAdapterError(f"无法读取第 {index + 1} 张参考图")


async def _upload_reference_image_to_host(source: str, payload: Dict[str, Any], index: int) -> str:
    has_explicit_upload_url = _has_explicit_reference_image_upload_url(payload)
    s3_config = None if has_explicit_upload_url else _reference_image_s3_config(payload)
    if s3_config is None and _reference_image_require_s3(payload) and not has_explicit_upload_url:
        raise ProviderAdapterError("参考图必须上传对象存储，但当前未配置对象存储")
    upload_url = _reference_image_upload_url(payload) if s3_config is None else ""
    if s3_config is None and not upload_url:
        raise ProviderAdapterError("参考图图床上传地址未配置")
    raw, mime, filename = await _read_reference_image_bytes(source, payload, index)
    raw, mime, filename = await asyncio.to_thread(
        _compress_reference_image_for_hosting,
        raw,
        mime,
        filename,
        index,
    )
    max_bytes = _reference_image_hosting_max_bytes()
    if max_bytes > 0 and len(raw) > max_bytes:
        raise ProviderAdapterError(f"第 {index + 1} 张参考图超过图床限制（{len(raw)} bytes > {max_bytes} bytes）")
    digest = hashlib.sha256(raw).hexdigest()
    cache = payload.setdefault("_referenceImageHostingCache", {})
    if isinstance(cache, dict) and digest in cache:
        return str(cache[digest])
    if digest in _REFERENCE_IMAGE_HOSTING_CACHE:
        hosted_url = _REFERENCE_IMAGE_HOSTING_CACHE[digest]
        if isinstance(cache, dict):
            cache[digest] = hosted_url
        return hosted_url

    task: Optional[asyncio.Task] = None
    owns_task = False
    lock = _reference_image_hosting_lock()
    async with lock:
        hosted_url = _REFERENCE_IMAGE_HOSTING_CACHE.get(digest)
        if hosted_url:
            if isinstance(cache, dict):
                cache[digest] = hosted_url
            return hosted_url
        task = _REFERENCE_IMAGE_HOSTING_INFLIGHT.get(digest)
        if task is None:
            if s3_config is not None:
                task = asyncio.create_task(_put_reference_image_to_s3(s3_config, raw, mime, filename, digest))
            else:
                task = asyncio.create_task(_post_reference_image_to_host(upload_url, raw, mime, filename))
            _REFERENCE_IMAGE_HOSTING_INFLIGHT[digest] = task
            owns_task = True

    try:
        hosted_url = str(await task)
    finally:
        if owns_task:
            async with lock:
                if _REFERENCE_IMAGE_HOSTING_INFLIGHT.get(digest) is task:
                    _REFERENCE_IMAGE_HOSTING_INFLIGHT.pop(digest, None)

    if not hosted_url:
        raise ProviderAdapterError("参考图上传图床失败：响应缺少 url")
    if isinstance(cache, dict):
        cache[digest] = hosted_url
    _remember_reference_image_hosting_cache(digest, hosted_url)
    return hosted_url


async def _post_reference_image_to_host(upload_url: str, raw: bytes, mime: str, filename: str) -> str:
    attempts = _reference_image_hosting_retry_attempts()
    delay = _reference_image_hosting_retry_delay()
    async with _reference_image_hosting_semaphore():
        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=_reference_image_hosting_timeout(),
                    follow_redirects=True,
                    trust_env=_http_trust_env(),
                ) as client:
                    response = await client.post(upload_url, files={"file": (filename, raw, mime)})
            except httpx.HTTPError as error:
                if attempt < attempts:
                    await asyncio.sleep(delay * attempt)
                    continue
                message = str(error).strip() or type(error).__name__
                raise ProviderAdapterError(f"参考图上传图床失败：{message}") from error

            if response.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(delay * attempt)
                continue
            if response.status_code >= 400:
                raise ProviderAdapterError(f"参考图上传图床失败：HTTP {response.status_code}")
            try:
                data = response.json()
            except Exception as error:
                raise ProviderAdapterError("参考图上传图床失败：响应不是 JSON") from error
            if not isinstance(data, dict):
                raise ProviderAdapterError("参考图上传图床失败：响应不是 JSON")
            nested = data.get("data")
            return _as_text(data.get("url") or (nested.get("url") if isinstance(nested, dict) else ""))
    raise ProviderAdapterError("参考图上传图床失败：响应缺少 url")


async def _put_reference_image_to_s3(
    config: Dict[str, str],
    raw: bytes,
    mime: str,
    filename: str,
    digest: str,
) -> str:
    bucket = config["bucket"].strip("/")
    key = _reference_image_s3_key(config, digest, filename, mime)
    public_url = _reference_image_s3_public_url(config, key)
    attempts = _reference_image_hosting_retry_attempts()
    delay = _reference_image_hosting_retry_delay()
    async with _reference_image_hosting_semaphore():
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(_sync_put_reference_image_to_s3, config, key, raw, mime)
            except Exception as error:
                if attempt < attempts:
                    await asyncio.sleep(delay * attempt)
                    continue
                message = str(error).strip() or type(error).__name__
                raise ProviderAdapterError(f"参考图上传对象存储失败：{message}") from error
            return public_url
    raise ProviderAdapterError("参考图上传对象存储失败：未返回有效结果")


def _sync_put_reference_image_to_s3(config: Dict[str, str], key: str, raw: bytes, mime: str) -> None:
    try:
        import boto3
        from botocore.config import Config
    except Exception as error:
        raise ProviderAdapterError("缺少 boto3，无法上传对象存储参考图") from error

    client = boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        region_name=config["region"],
        config=_build_s3_upload_client_config(Config, config),
    )
    client.put_object(
        Bucket=config["bucket"],
        Key=key,
        Body=raw,
        ContentType=mime or "application/octet-stream",
    )


def _build_s3_upload_client_config(Config, config: Dict[str, str]):
    kwargs = {
        "signature_version": "s3v4",
        "s3": {
            "addressing_style": config.get("addressing_style") or "path",
            "payload_signing_enabled": False,
        },
        "retries": {"max_attempts": 1},
        "proxies": {},
    }
    try:
        return Config(**kwargs, request_checksum_calculation="when_required")
    except TypeError as error:
        if "request_checksum_calculation" not in str(error):
            raise
        return Config(**kwargs)


def _reference_image_hosted_base_urls(payload: Dict[str, Any]) -> List[str]:
    config = _reference_image_s3_config(payload)
    if config is None:
        return []
    public_base_url = config.get("public_base_url", "").rstrip("/")
    if public_base_url:
        return [public_base_url]
    endpoint = config["endpoint"].rstrip("/")
    bucket = config["bucket"].strip("/")
    if not endpoint or not bucket:
        return []
    return [f"{endpoint}/{_s3_path_segment(bucket)}"]


def _is_current_reference_image_hosted_url(source: str, payload: Dict[str, Any]) -> bool:
    if not re.match(r"^https?://", source, re.I):
        return False
    source_url = source.rstrip("/")
    for base in _reference_image_hosted_base_urls(payload):
        base_url = base.rstrip("/")
        if source_url == base_url or source.startswith(f"{base_url}/"):
            return True
    return False


REFERENCE_IMAGE_PAYLOAD_FIELD_NAMES = (
    "image",
    "images",
    "image_url",
    "imageUrl",
    "referenceImages",
    "reference_images",
    "reference_image_urls",
    "referenceImageUrls",
    "inputUrls",
    "input_urls",
    "input_reference",
    "inputReference",
    "sourceImage",
    "source_image",
    "baseImage",
    "base_image",
    "startFrame",
    "start_frame",
    "startFrameUrl",
    "start_frame_url",
    "endFrame",
    "end_frame",
    "endFrameUrl",
    "end_frame_url",
)

REFERENCE_IMAGE_OBJECT_SOURCE_KEYS = (
    "assetPath",
    "asset_path",
    "localPath",
    "local_path",
    "path",
    "image_url",
    "imageUrl",
    "url",
    "src",
    "assetUrl",
    "asset_url",
)


def _reference_image_object_source(value: Dict[str, Any]) -> str:
    for key in ("assetPath", "asset_path", "localPath", "local_path", "path"):
        text = _as_text(value.get(key))
        if text and _is_existing_local_reference(text):
            return text
    for key in REFERENCE_IMAGE_OBJECT_SOURCE_KEYS:
        item = value.get(key)
        if isinstance(item, dict):
            nested = _reference_image_object_source(item)
            if nested:
                return nested
            continue
        text = _as_text(item)
        if text:
            return text
    return ""


def _replace_reference_image_object_sources(value: Dict[str, Any], hosted_source: str) -> Dict[str, Any]:
    replaced = False
    normalized = dict(value)
    for key in REFERENCE_IMAGE_OBJECT_SOURCE_KEYS:
        if key not in normalized:
            continue
        item = normalized.get(key)
        if isinstance(item, dict):
            if _reference_image_object_source(item):
                normalized[key] = _replace_reference_image_object_sources(item, hosted_source)
                replaced = True
            continue
        if _as_text(item):
            normalized[key] = hosted_source
            replaced = True
    if not replaced:
        normalized["url"] = hosted_source
    return normalized


def _reference_image_item_sources(value: Any) -> List[str]:
    if isinstance(value, list):
        sources: List[str] = []
        for item in value:
            sources.extend(_reference_image_item_sources(item))
        return sources
    if isinstance(value, dict):
        source = _reference_image_object_source(value)
        return [source] if source else []
    text = _as_text(value)
    return [text] if text else []


def _reference_image_needs_hosting(value: Any, payload: Optional[Dict[str, Any]] = None) -> bool:
    source = _as_text(value)
    if not source:
        return False
    if source.startswith(("asset://", "file_")):
        return False
    if source.startswith("libai-asset:"):
        return _path_from_libai_asset_url(source) is not None
    current_payload = payload or {}
    if re.match(r"^https?://", source, re.I):
        return not _is_current_reference_image_hosted_url(source, current_payload)
    if source.startswith(("data:image/", "file:", "/assets/")) or _looks_like_base64_image(source):
        return True
    try:
        return Path(source).exists() and Path(source).is_file()
    except Exception:
        return False


async def _host_reference_image_value(
    value: Any,
    payload: Dict[str, Any],
    counter: List[int],
    preprocessor: Optional[Callable[[Any, Dict[str, Any], int], Awaitable[Any]]] = None,
) -> Any:
    if isinstance(value, list):
        hosted_items: List[Any] = []
        for item in value:
            hosted_items.append(await _host_reference_image_value(item, payload, counter, preprocessor))
        return hosted_items
    if isinstance(value, dict):
        source = _reference_image_object_source(value)
        if not source:
            return value
        hosted_source = await _host_reference_image_value(source, payload, counter, preprocessor)
        return _replace_reference_image_object_sources(value, str(hosted_source))
    if preprocessor is not None:
        value = await preprocessor(value, payload, counter[0])
    if not _reference_image_needs_hosting(value, payload):
        return value
    index = counter[0]
    counter[0] += 1
    return await _upload_reference_image_to_host(str(value), payload, index)


def _reference_asset_is_image(item: Dict[str, Any]) -> bool:
    kind = _as_text(item.get("kind") or item.get("type") or item.get("assetKind")).lower()
    if kind in {"image", "photo", "picture"}:
        return True
    if kind and kind not in {"media", "asset"}:
        return False
    for key in ("mime", "contentType", "content_type"):
        if _as_text(item.get(key)).lower().startswith("image/"):
            return True
    source = _as_text(item.get("src") or item.get("url") or item.get("assetUrl") or item.get("assetPath") or item.get("path"))
    return bool(re.search(r"\.(png|jpe?g|webp|gif|bmp)(\?|#|$)", source, re.I))


async def prepare_outgoing_reference_image_payload(
    job_type: str,
    payload: Dict[str, Any],
    provider: Dict[str, Any],
    model: Dict[str, Any],
) -> Dict[str, Any]:
    del provider
    if job_type not in {"image.generate", "video.generate"}:
        return payload
    if not _reference_image_hosting_enabled(payload):
        return payload
    prepared = copy.deepcopy(payload)
    counter = [0]
    preprocessors: List[Callable[[Any, Dict[str, Any], int], Awaitable[Any]]] = []
    sora2_target_size = _sora2_reference_target_size(job_type, prepared, model)
    if sora2_target_size is not None:
        async def sora2_preprocessor(value: Any, current_payload: Dict[str, Any], index: int) -> Any:
            return await _prepare_sora2_reference_image_for_hosting(value, current_payload, index, sora2_target_size)
        preprocessors.append(sora2_preprocessor)
    mgdb_letterbox_ratio = _mgdb_nine_image_letterbox_ratio(job_type, prepared, model)
    if mgdb_letterbox_ratio is not None:
        async def mgdb_preprocessor(value: Any, current_payload: Dict[str, Any], index: int) -> Any:
            return await _prepare_mgdb_nine_image_reference_for_hosting(value, current_payload, index, mgdb_letterbox_ratio)
        preprocessors.append(mgdb_preprocessor)

    preprocessor = None
    if preprocessors:
        async def preprocessor(value: Any, current_payload: Dict[str, Any], index: int) -> Any:
            result = value
            for item in preprocessors:
                result = await item(result, current_payload, index)
            return result

    for field_name in REFERENCE_IMAGE_PAYLOAD_FIELD_NAMES:
        if field_name in prepared:
            prepared[field_name] = await _host_reference_image_value(prepared[field_name], prepared, counter, preprocessor)
    inputs = prepared.get("inputs")
    if isinstance(inputs, dict):
        for field_name in REFERENCE_IMAGE_PAYLOAD_FIELD_NAMES:
            if field_name in inputs:
                inputs[field_name] = await _host_reference_image_value(inputs[field_name], prepared, counter, preprocessor)
    reference_assets = prepared.get("referenceAssets")
    if isinstance(reference_assets, list):
        normalized_assets: List[Any] = []
        for item in reference_assets:
            if not isinstance(item, dict) or not _reference_asset_is_image(item):
                normalized_assets.append(item)
                continue
            normalized = dict(item)
            source = _preferred_reference_asset_source(normalized)
            if source:
                hosted_source = await _host_reference_image_value(source, prepared, counter, preprocessor)
                for source_key in ("src", "url", "assetUrl", "assetPath", "path"):
                    if _as_text(normalized.get(source_key)):
                        normalized[source_key] = hosted_source
                normalized.setdefault("src", hosted_source)
                normalized.setdefault("url", hosted_source)
                normalized.setdefault("assetUrl", hosted_source)
            normalized_assets.append(normalized)
        prepared["referenceAssets"] = normalized_assets
    if mgdb_letterbox_ratio is not None:
        prepared["_mgdbNineImageReferencesLetterboxed"] = True
    prepared.pop("_referenceImageHostingCache", None)
    prepared.pop("_sora2ReferenceFitCache", None)
    prepared.pop("_mgdbNineImageLetterboxCache", None)
    return prepared


async def _media_reference_to_path(
    value: str,
    target_dir: Path,
    index: int,
    *,
    kind: str,
    backend_base_url: Optional[str] = None,
) -> Path:
    source = _as_text(value)
    if not source:
        raise ProviderAdapterError("参考素材为空")
    if source.startswith("data:"):
        return _decode_data_url(source, target_dir)
    if source.startswith("file:"):
        file_path = _path_from_file_url(source)
        if file_path:
            return file_path
    allow_local_asset = False
    if source.startswith("/assets/"):
        source = _local_asset_url(source, backend_base_url)
        allow_local_asset = is_local_backend_asset_url(source)
    if re.match(r"^https?://", source, re.I):
        return await _download_reference_file(
            source,
            target_dir,
            kind=kind,
            allow_local_asset=allow_local_asset or is_local_backend_asset_url(source),
        )
    candidate = Path(source)
    if candidate.exists() and candidate.is_file():
        return candidate
    raise ProviderAdapterError(f"无法读取第 {index + 1} 个参考素材")


def _reference_media_label(kind: str) -> str:
    return "音频" if kind == "audio" else "视频"


async def _read_reference_media_bytes(
    value: str,
    payload: Dict[str, Any],
    index: int,
    *,
    kind: str,
) -> tuple[bytes, str, str]:
    target_dir = _output_dir(payload)
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    path = await _media_reference_to_path(
        value,
        target_dir,
        index,
        kind=kind,
        backend_base_url=backend_base_url,
    )
    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or f"{kind}/{'wav' if kind == 'audio' else 'mp4'}"
    return raw, mime, path.name or f"reference-{kind}-{index}{_media_extension_from_mime(mime, kind)}"


async def _media_reference_to_data_url(
    value: Any,
    payload: Dict[str, Any],
    index: int,
    *,
    kind: str,
) -> str:
    source = _as_text(value) or ""
    if not source:
        return source
    if source.startswith("data:"):
        return source
    if source.startswith("file_"):
        return source
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return source
    raw, mime, _filename = await _read_reference_media_bytes(source, payload, index, kind=kind)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


async def _upload_reference_media_to_s3(
    source: str,
    payload: Dict[str, Any],
    index: int,
    *,
    kind: str,
) -> str:
    label = _reference_media_label(kind)
    s3_config = _reference_media_s3_config(payload, kind)
    if s3_config is None:
        raise ProviderAdapterError(f"Muse 视频接口的{label}素材需要公网 URL")
    raw, mime, filename = await _read_reference_media_bytes(source, payload, index, kind=kind)
    max_bytes = _reference_media_hosting_max_bytes(kind)
    if max_bytes > 0 and len(raw) > max_bytes:
        raise ProviderAdapterError(f"第 {index + 1} 个{label}素材超过对象存储上传限制（{len(raw)} bytes > {max_bytes} bytes）")
    digest = hashlib.sha256(raw).hexdigest()
    cache_key = f"{kind}:{digest}"
    cache = payload.setdefault("_referenceMediaHostingCache", {})
    if isinstance(cache, dict) and cache_key in cache:
        return str(cache[cache_key])
    if cache_key in _REFERENCE_IMAGE_HOSTING_CACHE:
        hosted_url = _REFERENCE_IMAGE_HOSTING_CACHE[cache_key]
        if isinstance(cache, dict):
            cache[cache_key] = hosted_url
        return hosted_url

    task: Optional[asyncio.Task] = None
    owns_task = False
    lock = _reference_image_hosting_lock()
    async with lock:
        hosted_url = _REFERENCE_IMAGE_HOSTING_CACHE.get(cache_key)
        if hosted_url:
            if isinstance(cache, dict):
                cache[cache_key] = hosted_url
            return hosted_url
        task = _REFERENCE_IMAGE_HOSTING_INFLIGHT.get(cache_key)
        if task is None:
            task = asyncio.create_task(_put_reference_media_to_s3(s3_config, raw, mime, filename, digest, kind=kind))
            _REFERENCE_IMAGE_HOSTING_INFLIGHT[cache_key] = task
            owns_task = True

    try:
        hosted_url = str(await task)
    finally:
        if owns_task:
            async with lock:
                if _REFERENCE_IMAGE_HOSTING_INFLIGHT.get(cache_key) is task:
                    _REFERENCE_IMAGE_HOSTING_INFLIGHT.pop(cache_key, None)

    if not hosted_url:
        raise ProviderAdapterError(f"{label}素材上传对象存储失败：未返回有效结果")
    if isinstance(cache, dict):
        cache[cache_key] = hosted_url
    _remember_reference_image_hosting_cache(cache_key, hosted_url)
    return hosted_url


async def _put_reference_media_to_s3(
    config: Dict[str, str],
    raw: bytes,
    mime: str,
    filename: str,
    digest: str,
    *,
    kind: str,
) -> str:
    label = _reference_media_label(kind)
    key = _reference_media_s3_key(config, kind, digest, filename, mime)
    public_url = _reference_image_s3_public_url(config, key)
    attempts = _reference_image_hosting_retry_attempts()
    delay = _reference_image_hosting_retry_delay()
    async with _reference_image_hosting_semaphore():
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(_sync_put_reference_image_to_s3, config, key, raw, mime)
            except Exception as error:
                if attempt < attempts:
                    await asyncio.sleep(delay * attempt)
                    continue
                message = str(error).strip() or type(error).__name__
                raise ProviderAdapterError(f"{label}素材上传对象存储失败：{message}") from error
            return public_url
    raise ProviderAdapterError(f"{label}素材上传对象存储失败：未返回有效结果")


def _multipart_fields(body: Dict[str, Any]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for key, value in body.items():
        if value is None or key in {"images", "mask"}:
            continue
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            fields[key] = json.dumps(value, ensure_ascii=False)
        else:
            fields[key] = str(value)
    return fields


def _safe_multipart_filename(field_name: str, path: Path) -> str:
    suffix = path.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        suffix = mimetypes.guess_extension(mimetypes.guess_type(str(path))[0] or "") or ""
    safe_field = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(field_name or "file")).strip("._-")
    return f"{safe_field or 'file'}{suffix}"


async def _multipart_named_request(
    url: str,
    api_key: str,
    body: Dict[str, Any],
    file_fields: List[tuple[str, Path]],
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = _auth_headers(api_key)
    _merge_extra_headers(headers, extra_headers)
    files = []
    opened = []
    try:
        for field_name, path in file_fields:
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            handle = path.open("rb")
            opened.append(handle)
            files.append((field_name, (_safe_multipart_filename(field_name, path), handle, mime)))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
                response = await client.post(url, data=_multipart_fields(body), files=files, headers=headers)
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            raise ProviderAdapterError(f"供应商视频接口连接失败：{url}（{message}）") from error
    finally:
        for handle in opened:
            handle.close()
    text = response.text or ""
    if not response.is_success:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": text}
        raise ProviderAdapterError(_error_message(payload, f"HTTP {response.status_code}"))
    if not text.strip():
        return {}
    try:
        payload = response.json()
    except Exception:
        return {"raw": text}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ProviderAdapterError(_error_message(payload, "Provider request failed"))
    if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {"data": payload}


async def _multipart_request(
    url: str,
    api_key: str,
    body: Dict[str, Any],
    image_paths: List[Path],
    mask_path: Optional[Path] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = _auth_headers(api_key)
    _merge_extra_headers(headers, extra_headers)
    files = []
    opened = []
    try:
        for path in image_paths:
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            handle = path.open("rb")
            opened.append(handle)
            files.append(("image", (path.name, handle, mime)))
        if mask_path:
            mime = mimetypes.guess_type(str(mask_path))[0] or "image/png"
            handle = mask_path.open("rb")
            opened.append(handle)
            files.append(("mask", (mask_path.name, handle, mime)))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
                response = await client.post(url, data=_multipart_fields(body), files=files, headers=headers)
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            raise ProviderAdapterError(f"供应商图像接口连接失败：{url}（{message}）") from error
    finally:
        for handle in opened:
            handle.close()
    text = response.text or ""
    if not response.is_success:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": text}
        raise ProviderAdapterError(_error_message(payload, f"HTTP {response.status_code}"))
    if not text.strip():
        return {}
    try:
        payload = response.json()
    except Exception:
        return {"raw": text}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ProviderAdapterError(_error_message(payload, "Provider request failed"))
    if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {"data": payload}


async def _streaming_multipart_request(
    url: str,
    api_key: str,
    body: Dict[str, Any],
    image_paths: List[Path],
    mask_path: Optional[Path] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = _auth_headers(api_key)
    _merge_extra_headers(headers, extra_headers)
    headers["Accept"] = "text/event-stream"
    files = []
    opened = []
    try:
        for path in image_paths:
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            handle = path.open("rb")
            opened.append(handle)
            files.append(("image", (path.name, handle, mime)))
        if mask_path:
            mime = mimetypes.guess_type(str(mask_path))[0] or "image/png"
            handle = mask_path.open("rb")
            opened.append(handle)
            files.append(("mask", (mask_path.name, handle, mime)))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=_http_trust_env()) as client:
                async with client.stream("POST", url, data=_multipart_fields(body), files=files, headers=headers) as response:
                    return await _consume_streaming_response(response, url)
        except ProviderAdapterError:
            raise
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            raise ProviderAdapterError(f"供应商图像接口连接失败：{url}（{message}）") from error
    finally:
        for handle in opened:
            handle.close()


async def _resolve_upscale_input(payload: Dict[str, Any], target_dir: Path) -> Path:
    local_path = _as_text(
        payload.get("_sourceAssetPath")
        or payload.get("inputPath")
        or payload.get("filePath")
        or payload.get("path")
    )
    if local_path:
        candidate = Path(local_path)
        if candidate.exists() and candidate.is_file():
            return candidate

    data_url = _as_text(payload.get("dataUrl") or payload.get("data_url"))
    if data_url and data_url.startswith("data:image/"):
        return _decode_data_url(data_url, target_dir)

    source_url = _as_text(
        payload.get("imageUrl")
        or payload.get("image_url")
        or payload.get("assetUrl")
        or payload.get("asset_url")
        or payload.get("src")
        or payload.get("url")
    )
    if not source_url:
        raise ProviderAdapterError("高清放大缺少输入图片")
    if source_url.startswith("data:image/"):
        return _decode_data_url(source_url, target_dir)
    if source_url.startswith("file:"):
        file_path = _path_from_file_url(source_url)
        if file_path:
            return file_path
    allow_local_asset = False
    if source_url.startswith("/assets/"):
        source_url = _local_asset_url(source_url, None)
        allow_local_asset = is_local_backend_asset_url(source_url)
    if re.match(r"^https?://", source_url, re.I):
        return await _download_image(
            source_url,
            target_dir,
            allow_local_asset=allow_local_asset or is_local_backend_asset_url(source_url),
        )
    candidate = Path(source_url)
    if candidate.exists() and candidate.is_file():
        return candidate
    raise ProviderAdapterError("无法读取高清放大输入图片")


async def _run_realesrgan(
    *,
    binary: Path,
    input_path: Path,
    output_path: Path,
    scale: int,
    model_name: str,
    tile_size: int,
    progress: ProgressCallback,
) -> None:
    await progress(22, "realesrgan-start")
    command = [
        str(binary),
        "-i", str(input_path),
        "-o", str(output_path),
        "-n", model_name,
        "-s", str(scale),
    ]
    if tile_size:
        command.extend(["-t", str(tile_size)])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = (stderr or stdout or b"").decode("utf-8", errors="ignore").strip()
        raise ProviderAdapterError(detail or "Real-ESRGAN 执行失败")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise ProviderAdapterError("Real-ESRGAN 未生成有效输出文件")
    await progress(92, "realesrgan-finished")


async def _run_pillow_lanczos(input_path: Path, output_path: Path, scale: int, output_format: str, progress: ProgressCallback) -> None:
    await progress(24, "lanczos-start")
    try:
        from PIL import Image
    except Exception as error:
        raise ProviderAdapterError("未检测到 Real-ESRGAN，本地兜底放大需要安装 Pillow：pip install pillow") from error

    def resize() -> None:
        with safe_image_open(input_path) as image:
            width, height = image.size
            target_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            resized = image.resize(target_size, Image.Resampling.LANCZOS)
            if output_format in {"jpg", "jpeg"} and resized.mode in {"RGBA", "LA", "P"}:
                resized = resized.convert("RGB")
            save_format = "JPEG" if output_format in {"jpg", "jpeg"} else output_format.upper()
            resized.save(output_path, format=save_format, quality=96)

    await asyncio.to_thread(resize)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise ProviderAdapterError("Lanczos 放大未生成有效输出文件")
    await progress(90, "lanczos-finished")


async def _run_local_upscale(payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    scale = _int_choice(payload.get("scale") or payload.get("factor"), 2, {2, 3, 4})
    model_name = _normalize_upscale_model(payload)
    work_dir = _output_dir(payload)
    input_path = await _resolve_upscale_input(payload, work_dir)
    tile_size = _tile_size(payload, input_path, scale)
    output_format = _output_format(payload.get("outputFormat") or payload.get("format"), input_path)
    output_path = work_dir / f"upscale_{uuid.uuid4().hex[:12]}.{output_format}"
    binary = _find_realesrgan_binary(payload)
    if binary:
        await _run_realesrgan(
            binary=binary,
            input_path=input_path,
            output_path=output_path,
            scale=scale,
            model_name=model_name,
            tile_size=tile_size,
            progress=progress,
        )
        engine = "Real-ESRGAN ncnn Vulkan"
        fallback = False
    else:
        await _run_pillow_lanczos(input_path, output_path, scale, output_format, progress)
        engine = "Pillow Lanczos"
        fallback = True
    mime = mimetypes.guess_type(str(output_path))[0] or f"image/{'jpeg' if output_format == 'jpg' else output_format}"
    return {
        "provider": "local.upscale",
        "status": "completed",
        "assetKind": "image",
        "localPath": str(output_path),
        "filename": output_path.name,
        "mime": mime,
        "scale": scale,
        "tile": tile_size,
        "engine": engine,
        "fallback": fallback,
        "model": model_name,
        "sourceAssetId": _as_text(payload.get("assetId") or payload.get("asset_id")),
        "sourceNodeId": _as_text(payload.get("sourceNodeId") or payload.get("nodeId")),
        "title": _as_text(payload.get("title")) or f"高清放大 {scale}x",
        "message": "Local upscale job completed.",
    }


async def _poll_remote_task(
    *,
    status_url: str,
    api_key: str,
    progress: ProgressCallback,
    timeout_seconds: int = 3000,
    request_timeout_seconds: Optional[float] = None,
    media_kind: Optional[str] = None,
    output_format: str = "png",
) -> Dict[str, Any]:
    waited = 0
    step = 5
    request_timeout = request_timeout_seconds if request_timeout_seconds is not None else DEFAULT_REQUEST_TIMEOUT_SECONDS
    await progress(18, "remote-submitted")
    while waited < timeout_seconds:
        await asyncio.sleep(step)
        waited += step
        try:
            payload = await _json_get_with_transient_retries(
                status_url,
                api_key,
                timeout=request_timeout,
                attempts=3,
            )
        except ProviderAdapterError as error:
            if not _is_transient_provider_connection_error(error):
                raise
            await progress(min(90, 18 + waited // 6), "retrying-remote")
            continue
        remote_error = _remote_task_error_message(payload)
        if remote_error:
            raise ProviderAdapterError(remote_error)
        status = _extract_status(payload)
        result_urls = _extract_result_media_urls(payload, media_kind=media_kind, output_format=output_format)
        urls = result_urls or _extract_urls(payload)
        if result_urls or (status == "completed" and urls):
            await progress(96, "remote-completed")
            return {"status": "completed", "url": urls[0], "urls": urls, "raw": payload}
        if status == "failed":
            raise ProviderAdapterError(_error_message(payload, "远程任务失败"))
        await progress(min(90, 18 + waited // 6), status)
    raise ProviderAdapterError("远程任务超时，请稍后在任务历史中重试或查询")


def _normalize_chat_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def _extract_chat_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                text = _normalize_chat_content(message.get("content"))
                if text.strip():
                    return text.strip()
            delta = first.get("delta")
            if isinstance(delta, dict):
                text = _normalize_chat_content(delta.get("content"))
                if text.strip():
                    return text.strip()
            text = _normalize_chat_content(first.get("text") or first.get("content"))
            if text.strip():
                return text.strip()
    for key in ("output_text", "text", "content", "message", "result"):
        text = _normalize_chat_content(payload.get(key))
        if text.strip():
            return text.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_chat_text(data)
    return ""


def _extract_responses_text(payload: Dict[str, Any]) -> str:
    output_text = _normalize_chat_content(payload.get("output_text"))
    if output_text.strip():
        return output_text.strip()

    parts: List[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"}:
                    text = _normalize_chat_content(part.get("text") or part.get("content"))
                    if text:
                        parts.append(text)
    if parts:
        return "".join(parts).strip()

    for key in ("text", "content", "message", "result"):
        text = _normalize_chat_content(payload.get(key))
        if text.strip():
            return text.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_responses_text(data)
    return ""


def _effective_prompt(payload: Dict[str, Any], fallback: str = "") -> str:
    explicit = str(payload.get("effectivePrompt") or payload.get("composedPrompt") or "").strip()
    if explicit:
        return explicit
    prompt = str(payload.get("prompt") or payload.get("inputText") or fallback).strip()
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    style_prompt = str(payload.get("stylePrompt") or params.get("stylePrompt") or "").strip()
    if style_prompt and prompt:
        return f"{style_prompt}\n\n{prompt}"
    return style_prompt or prompt


def _build_chat_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return [item for item in messages if isinstance(item, dict)]
    prompt = _effective_prompt(payload)
    system_prompt = str(payload.get("systemPrompt") or payload.get("system") or "").strip()
    reference_images = [x for x in payload.get("referenceImages", []) if isinstance(x, str) and x.strip()]
    built: List[Dict[str, Any]] = []
    if system_prompt:
        built.append({"role": "system", "content": system_prompt})
    if reference_images:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in reference_images[:8])
        built.append({"role": "user", "content": content})
    else:
        built.append({"role": "user", "content": prompt or "请生成一段文本。"})
    return built


def _build_responses_input(payload: Dict[str, Any]) -> Any:
    explicit = payload.get("input")
    if isinstance(explicit, (str, list)) and explicit:
        return explicit
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return [item for item in messages if isinstance(item, dict)]

    prompt = _effective_prompt(payload)
    reference_images = [x for x in payload.get("referenceImages", []) if isinstance(x, str) and x.strip()]
    if reference_images:
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt or "请根据参考图片完成请求。"}]
        content.extend({"type": "input_image", "image_url": url} for url in reference_images[:8])
        return [{"role": "user", "content": content}]
    return prompt or "请生成一段文本。"


def _strip_json_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I).strip()
        value = re.sub(r"\s*```$", "", value).strip()
    return value


def _parse_json_object(text: str) -> Dict[str, Any]:
    value = _strip_json_fence(text)
    try:
        parsed = json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ProviderAdapterError("图片标签分析未返回 JSON")
        try:
            parsed = json.loads(value[start : end + 1])
        except Exception as error:
            raise ProviderAdapterError("图片标签分析 JSON 解析失败") from error
    if not isinstance(parsed, dict):
        raise ProviderAdapterError("图片标签分析 JSON 不是对象")
    return parsed


def _clamp_unit(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = fallback
    if number > 1:
        number = number / 100
    return max(0.0, min(1.0, number))


def _normalize_analysis_tags(raw_tags: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_tags, dict):
        raw_items = list(raw_tags.values())
    elif isinstance(raw_tags, list):
        raw_items = raw_tags
    else:
        raw_items = []
    tags: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_items[:24]):
        if not isinstance(raw, dict):
            raw = {"label": str(raw)}
        bbox = raw.get("bbox") if isinstance(raw.get("bbox"), dict) else {}
        point = raw.get("point") if isinstance(raw.get("point"), dict) else {}
        fallback_w = _clamp_unit(bbox.get("w", raw.get("w", 0.16)), 0.16)
        fallback_h = _clamp_unit(bbox.get("h", raw.get("h", 0.16)), 0.16)
        fallback_x = _clamp_unit(bbox.get("x", 0.42), 0.42) + fallback_w / 2
        fallback_y = _clamp_unit(bbox.get("y", 0.42), 0.42) + fallback_h / 2
        x = _clamp_unit(point.get("x", raw.get("x", fallback_x)), 0.5)
        y = _clamp_unit(point.get("y", raw.get("y", fallback_y)), 0.5)
        w = fallback_w
        h = fallback_h
        bx = _clamp_unit(bbox.get("x", max(0.0, x - w / 2)), max(0.0, x - w / 2))
        by = _clamp_unit(bbox.get("y", max(0.0, y - h / 2)), max(0.0, y - h / 2))
        if bx + w > 1:
            w = max(0.01, 1 - bx)
        if by + h > 1:
            h = max(0.01, 1 - by)
        label = str(raw.get("label") or raw.get("name") or raw.get("tag") or f"标签 {index + 1}").strip()
        prompt_token = str(raw.get("promptToken") or raw.get("prompt_token") or raw.get("tag") or f"@{label}").strip()
        if prompt_token and not prompt_token.startswith("@"):
            prompt_token = f"@{prompt_token}"
        tags.append({
            **raw,
            "id": str(raw.get("id") or f"tag_{index}"),
            "label": label.replace("@", "") or f"标签 {index + 1}",
            "kind": str(raw.get("kind") or raw.get("type") or "region"),
            "tag": prompt_token or f"@标签{index + 1}",
            "promptToken": prompt_token or f"@标签{index + 1}",
            "point": {"x": round(x, 4), "y": round(y, 4)},
            "bbox": {"x": round(bx, 4), "y": round(by, 4), "w": round(w, 4), "h": round(h, 4)},
            "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
        })
    return tags


def _analysis_source_image(payload: Dict[str, Any]) -> Optional[str]:
    references = payload.get("referenceImages")
    if isinstance(references, list):
        for item in references:
            text = _as_text(item)
            if text:
                return text
    for key in ("imageUrl", "image_url", "assetUrl", "asset_url", "src", "url"):
        text = _as_text(payload.get(key))
        if text:
            return text
    return None


async def _image_reference_to_data_url(value: str, target_dir: Path, index: int, backend_base_url: Optional[str] = None) -> str:
    source = _as_text(value)
    if not source:
        raise ProviderAdapterError("图片标签分析缺少输入图片")
    if source.startswith("data:image/"):
        return source
    path = await _image_reference_to_path(source, target_dir, index, backend_base_url=backend_base_url)
    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _is_local_http_reference(source: str) -> bool:
    return is_private_or_local_http_url(source)


def _looks_like_base64_image(value: str) -> bool:
    text = value.strip()
    if len(text) < 80 or re.search(r"\s", text):
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", text):
        return False
    try:
        raw = base64.b64decode(text.replace("-", "+").replace("_", "/"), validate=False)
    except Exception:
        return False
    return raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8", b"BM"))


async def _normalize_json_image_reference(value: str, payload: Dict[str, Any], index: int) -> str:
    source = _as_text(value) or ""
    if not source:
        return source
    if source.startswith(("asset://", "file_")):
        return source
    if source.startswith("data:image/") or _looks_like_base64_image(source):
        return source
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return source
    target_dir = _output_dir(payload)
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    return await _image_reference_to_data_url(source, target_dir, index, backend_base_url=backend_base_url)


def _gpt_image_reference_request_mode(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    raw = (_as_text(
        payload.get("gptImageReferenceRequestMode")
        or payload.get("imageReferenceRequestMode")
        or payload.get("referenceRequestMode")
        or params.get("gptImageReferenceRequestMode")
        or params.get("imageReferenceRequestMode")
        or params.get("referenceRequestMode")
    ) or "").strip().lower()
    value = re.sub(r"[\s-]+", "_", raw)
    if value in {"json", "json_generation", "generation_json", "generations_json", "legacy", "legacy_json"}:
        return "json_generation"
    if value in {"multipart", "multipart_edit", "edit", "edits", "image_edit", "images_edit", "images_edits", "official"}:
        return "multipart_edit"
    return "auto"


async def _run_gpt_image_reference_json_generation(
    base: str,
    api_key: str,
    body: Dict[str, Any],
    references: List[str],
    payload: Dict[str, Any],
    image_timeout: float,
    progress: ProgressCallback,
    reference_limit: int,
) -> Dict[str, Any]:
    reference_urls = [str(item).strip() for item in references[:reference_limit] if str(item).strip()]
    json_images = [
        await _normalize_json_image_reference(item, payload, index)
        for index, item in enumerate(reference_urls)
    ]
    return await _await_image_response(
        _streaming_json_request(
            "POST",
            f"{base}/v1/images/generations",
            api_key,
            body={**body, "image": json_images},
            timeout=image_timeout,
        ),
        progress,
    )


async def _run_gpt_image_async_request(
    base: str,
    api_key: str,
    body: Dict[str, Any],
    references: List[str],
    mask_image: Any,
    payload: Dict[str, Any],
    model: Dict[str, Any],
    endpoint: str,
    image_timeout: float,
) -> Dict[str, Any]:
    request_body = _gpt_image_non_stream_body(body)
    if references:
        reference_request_mode = _gpt_image_reference_request_mode(model, payload)
        reference_limit = _model_reference_limit(model, "maxReferenceImages", DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT)
        if reference_request_mode == "json_generation":
            reference_urls = [str(item).strip() for item in references[:reference_limit] if str(item).strip()]
            json_images = [
                await _normalize_json_image_reference(item, payload, index)
                for index, item in enumerate(reference_urls)
            ]
            return await _json_request(
                "POST",
                f"{base}/v1/images/generations",
                api_key,
                body={**request_body, "image": json_images},
                timeout=image_timeout,
            )
        target_dir = _output_dir(payload)
        backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
        reference_paths = [
            await _image_reference_to_path(str(item).strip(), target_dir, index, backend_base_url=backend_base_url)
            for index, item in enumerate(references[:reference_limit])
            if str(item).strip()
        ]
        mask_path = None
        if mask_image:
            mask_path = await _image_reference_to_path(
                str(mask_image),
                target_dir,
                len(reference_paths),
                backend_base_url=backend_base_url,
            )
        return await _multipart_request(
            f"{base}{endpoint}",
            api_key,
            body=request_body,
            image_paths=reference_paths,
            mask_path=mask_path,
            timeout=image_timeout,
        )
    return await _json_request(
        "POST",
        f"{base}{endpoint}",
        api_key,
        body=request_body,
        timeout=image_timeout,
    )


async def _normalize_newapi_json_image_body(body: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized_body = dict(body)
    for field_name in ("image", "reference_images"):
        images = normalized_body.get(field_name)
        if not images:
            continue
        values = images if isinstance(images, list) else [images]
        normalized_body[field_name] = [
            await _normalize_json_image_reference(item, payload, index)
            for index, item in enumerate(values)
            if _as_text(item)
        ]
    return normalized_body


async def _build_image_analyze_input(payload: Dict[str, Any]) -> Any:
    source = _analysis_source_image(payload)
    if not source:
        raise ProviderAdapterError("图片标签分析缺少输入图片")
    target_dir = _output_dir(payload)
    image_data_url = await _image_reference_to_data_url(source, target_dir, 0, payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    prompt = str(payload.get("prompt") or "").strip()
    request_text = "\n".join([
        "分析这张图像，提取可用于局部编辑和提示词引用的视觉标签。",
        "返回 6 到 16 个标签，优先包含人物、主体、关键物体、背景区域、光源、画面风格。",
        "bbox 和 point 必须是 0 到 1 的归一化坐标，bbox 使用左上角 x/y 与宽高 w/h。",
        "promptToken 使用中文 @标签，例如 @女孩、@门、@书架。",
        "只返回 JSON，不要 Markdown，不要解释。",
        "JSON 结构：{\"tags\":[{\"id\":\"tag_0\",\"label\":\"女孩\",\"kind\":\"person\",\"tag\":\"@女孩\",\"promptToken\":\"@女孩\",\"point\":{\"x\":0.5,\"y\":0.5},\"bbox\":{\"x\":0.4,\"y\":0.3,\"w\":0.2,\"h\":0.4},\"confidence\":0.9}]}",
        prompt,
    ]).strip()
    return [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": request_text},
            {"type": "input_image", "image_url": image_data_url},
        ],
    }]


async def _run_openai_image_analyze(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    params = model.get("params") or {}
    body: Dict[str, Any] = {
        "model": model.get("modelName") or payload.get("modelName") or payload.get("model") or "gpt-5.5",
        "input": await _build_image_analyze_input(payload),
        "stream": False,
    }
    effort = payload.get("reasoningEffort") or params.get("reasoningEffort")
    if effort and str(effort).strip().lower() != "none":
        body["reasoning"] = {"effort": str(effort).strip()}
    verbosity = payload.get("textVerbosity") or params.get("textVerbosity")
    if verbosity:
        body["text"] = {"verbosity": str(verbosity).strip()}
    max_tokens = payload.get("maxOutputTokens") or payload.get("max_output_tokens") or params.get("maxOutputTokens")
    if max_tokens is not None:
        body["max_output_tokens"] = max_tokens

    await progress(12, "submitting")
    response = await _streaming_json_request("POST", f"{base}/v1/responses", api_key, body=body, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    await progress(86, "parsing")
    text = _extract_responses_text(response)
    if not text:
        raise ProviderAdapterError(_error_message(response, "图片标签分析未返回文本内容"))
    parsed = _parse_json_object(text)
    tags = _normalize_analysis_tags(parsed.get("tags") or parsed.get("regions") or parsed.get("objects"))
    if not tags:
        raise ProviderAdapterError("图片标签分析没有返回可用标签")
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "providerModelName": model.get("modelName"),
        "status": "completed",
        "assetKind": "metadata",
        "sourceUrl": _analysis_source_image(payload),
        "sourceAssetId": payload.get("assetId") or payload.get("asset_id") or payload.get("sourceAssetId"),
        "tags": tags,
        "regions": tags,
        "text": text,
        "raw": response,
    }


def _should_fallback_responses_to_chat(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if re.search(r"\b(401|403)\b", text):
        return False
    blocked_markers = ("quota", "余额", "insufficient", "unauthorized", "forbidden", "api key")
    if any(marker in text for marker in blocked_markers):
        return False
    fallback_markers = (
        "upstream request failed",
        "upstream_error",
        "bad_response_status_code",
        "unsupported endpoint",
        "unsupported path",
        "responses endpoint",
        "not found",
        "404",
        "502",
    )
    return any(marker in text for marker in fallback_markers)


def _chat_completion_max_tokens(payload: Dict[str, Any], params: Dict[str, Any]) -> Optional[Any]:
    for key in ("maxTokens", "max_tokens", "maxOutputTokens", "max_output_tokens"):
        value = payload.get(key)
        if value is not None:
            return value
    for key in ("maxTokens", "max_tokens", "maxOutputTokens", "max_output_tokens"):
        value = params.get(key)
        if value is not None:
            return value
    return None


def _text_request_timeout(payload: Dict[str, Any], params: Dict[str, Any]) -> float:
    keys = ("requestTimeoutSeconds", "timeoutSeconds", "timeout_seconds", "timeout")
    for source in (payload, params):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            try:
                timeout = float(value)
            except (TypeError, ValueError):
                return DEFAULT_REQUEST_TIMEOUT_SECONDS
            if timeout <= 0:
                return DEFAULT_REQUEST_TIMEOUT_SECONDS
            return max(30.0, min(DEFAULT_REQUEST_TIMEOUT_SECONDS, timeout))
    return DEFAULT_REQUEST_TIMEOUT_SECONDS


def _prefers_chat_completions_endpoint(params: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    endpoint = str(
        payload.get("apiEndpoint")
        or payload.get("api_endpoint")
        or params.get("apiEndpoint")
        or params.get("api_endpoint")
        or ""
    ).strip().lower().replace("_", "-")
    return endpoint in {"chat.completions", "chat-completions", "/v1/chat/completions", "v1/chat/completions"}


async def _run_openai_chat_completion_fallback(
    provider: Dict[str, Any],
    model: Dict[str, Any],
    payload: Dict[str, Any],
    progress: ProgressCallback,
    params: Dict[str, Any],
    fallback: bool = True,
) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    body: Dict[str, Any] = {
        "model": model.get("modelName") or payload.get("modelName") or payload.get("model") or "gpt-5.5",
        "messages": _build_chat_messages(payload),
        "stream": False,
    }
    max_tokens = _chat_completion_max_tokens(payload, params)
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    temperature = payload.get("temperature", params.get("temperature"))
    if temperature is not None:
        body["temperature"] = temperature
    top_p = payload.get("topP", payload.get("top_p", params.get("topP", params.get("top_p"))))
    if top_p is not None:
        body["top_p"] = top_p

    await progress(18, "fallback-chat-completions")
    request_timeout = _text_request_timeout(payload, params)
    response = await _streaming_json_request("POST", f"{base}/v1/chat/completions", api_key, body=body, timeout=request_timeout)
    await progress(86, "parsing")
    text = _extract_chat_text(response) or _extract_responses_text(response)
    if not text:
        raise ProviderAdapterError(_error_message(response, "Chat Completions 模型未返回文本内容"))
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "status": "completed",
        "assetKind": "text",
        "text": text,
        "content": text,
        "raw": response,
        "endpoint": "chat.completions",
        **({"fallbackEndpoint": "chat.completions"} if fallback else {}),
    }


async def _run_openai_responses(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    params = model.get("params") or {}
    if _prefers_chat_completions_endpoint(params, payload):
        return await _run_openai_chat_completion_fallback(provider, model, payload, progress, params, fallback=False)
    body: Dict[str, Any] = {
        "model": model.get("modelName") or payload.get("modelName") or payload.get("model") or "gpt-5.5",
        "input": _build_responses_input(payload),
        "stream": False,
    }
    instructions = str(payload.get("instructions") or payload.get("systemPrompt") or payload.get("system") or "").strip()
    if instructions:
        body["instructions"] = instructions

    reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else {}
    effort = (
        payload.get("reasoningEffort")
        or payload.get("reasoning_effort")
        or reasoning.get("effort")
        or params.get("reasoningEffort")
    )
    if effort and str(effort).strip().lower() != "none":
        body["reasoning"] = {"effort": str(effort).strip()}

    text_options = payload.get("text") if isinstance(payload.get("text"), dict) else {}
    verbosity = (
        payload.get("textVerbosity")
        or payload.get("text_verbosity")
        or text_options.get("verbosity")
        or params.get("textVerbosity")
    )
    if verbosity:
        body["text"] = {"verbosity": str(verbosity).strip()}

    max_tokens = payload.get("maxOutputTokens") or payload.get("max_output_tokens") or payload.get("maxTokens") or payload.get("max_tokens")
    if max_tokens is not None:
        body["max_output_tokens"] = max_tokens

    await progress(12, "submitting")
    request_timeout = _text_request_timeout(payload, params)
    try:
        response = await _streaming_json_request("POST", f"{base}/v1/responses", api_key, body=body, timeout=request_timeout)
    except ProviderAdapterError as error:
        if not _should_fallback_responses_to_chat(error):
            raise
        return await _run_openai_chat_completion_fallback(provider, model, payload, progress, params)
    await progress(86, "parsing")
    text = _extract_responses_text(response)
    if not text:
        raise ProviderAdapterError(_error_message(response, "Responses 模型未返回文本内容"))
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "status": "completed",
        "assetKind": "text",
        "text": text,
        "content": text,
        "raw": response,
    }


def _api_image_format(value: Any) -> str:
    normalized = _normalize_image_format(value) or "png"
    return "jpeg" if normalized == "jpg" else normalized


def _image_mime(output_format: str) -> str:
    return "image/jpeg" if output_format == "jpeg" else f"image/{output_format}"


def _image_suffix(output_format: str) -> str:
    return ".jpg" if output_format == "jpeg" else f".{output_format}"


def _image_response_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = dict(payload)
    data = summary.get("data")
    if isinstance(data, list):
        summarized = []
        for item in data:
            if isinstance(item, dict):
                clone = dict(item)
                if clone.get("b64_json"):
                    clone["b64_json"] = "[base64 omitted]"
                summarized.append(clone)
            else:
                summarized.append(item)
        summary["data"] = summarized
    return summary


async def _await_image_response(
    request: Awaitable[Dict[str, Any]],
    progress: ProgressCallback,
) -> Dict[str, Any]:
    task = asyncio.create_task(request)
    marks = [
        (24, "waiting-provider"),
        (36, "generating-image"),
        (48, "generating-image"),
        (60, "generating-image"),
        (72, "saving-image"),
        (84, "saving-image"),
    ]
    try:
        for value, stage in marks:
            await asyncio.sleep(10)
            if task.done():
                break
            await progress(value, stage)
        return await task
    except Exception:
        if not task.done():
            task.cancel()
        raise


def _extract_openai_image_urls(payload: Dict[str, Any], output_format: str) -> List[str]:
    urls: List[str] = []
    data = payload.get("data")
    items = data if isinstance(data, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_preview_media_item(item):
            continue
        url = _as_text(item.get("url"))
        if url and url not in urls:
            urls.append(url)
        b64_json = _as_text(item.get("b64_json"))
        if b64_json:
            data_url = f"data:{_image_mime(output_format)};base64,{b64_json}"
            if data_url not in urls:
                urls.append(data_url)
    return urls


def _first_payload_value(payload: Dict[str, Any], *keys: str) -> Any:
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
        if key in params and params.get(key) is not None:
            return params.get(key)
    return None


def _first_model_param(model: Dict[str, Any], *keys: str) -> Any:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    for key in keys:
        if key in params and params.get(key) is not None:
            return params.get(key)
    return None


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _falsey_value(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def _prefer_concrete_grok2api_variant_model(model: Dict[str, Any]) -> bool:
    value = _first_model_param(model, "preferConcreteVariantModel", "useConcreteVariantModel")
    return not _falsey_value(value)


def _float_timeout(value: Any, default: float) -> float:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


def _image_request_timeout(model: Dict[str, Any], payload: Dict[str, Any]) -> float:
    configured = _env_float("LIBAI_IMAGE_REQUEST_TIMEOUT_SECONDS", DEFAULT_IMAGE_REQUEST_TIMEOUT_SECONDS)
    value = (
        _first_payload_value(payload, "requestTimeoutSeconds", "timeoutSeconds", "timeout_seconds", "timeout")
        or _first_model_param(model, "requestTimeoutSeconds", "timeoutSeconds", "timeout_seconds", "timeout")
    )
    return max(DEFAULT_REQUEST_TIMEOUT_SECONDS, _float_timeout(value, configured))


def _bool_payload_value(payload: Dict[str, Any], model: Dict[str, Any], *keys: str, default: Optional[bool] = None) -> Optional[bool]:
    value = _first_payload_value(payload, *keys)
    if value is None:
        value = _first_model_param(model, *keys)
    if value is None:
        value = _first_model_param(model, "defaultGenerateAudio")
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _video_reference_inputs(payload: Dict[str, Any]) -> List[str]:
    def collect(
        *,
        keys: tuple[str, ...] = (),
        input_keys: tuple[str, ...] = (),
        include_reference_assets: bool = False,
        add_reference_assets: bool = True,
    ) -> List[str]:
        refs: List[str] = []
        aliases: Dict[str, int] = {}
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    _append_media_reference(refs, aliases, item)
            elif value is not None:
                _append_media_reference(refs, aliases, value)
        inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
        for key in input_keys:
            value = inputs.get(key)
            if isinstance(value, list):
                for item in value:
                    _append_media_reference(refs, aliases, item)
            elif value is not None:
                _append_media_reference(refs, aliases, value)
        if include_reference_assets:
            for asset in payload.get("referenceAssets") or []:
                if not isinstance(asset, dict):
                    continue
                asset_kind = str(asset.get("kind") or asset.get("type") or "").strip().lower()
                if asset_kind != "image":
                    continue
                preferred = _preferred_reference_asset_source(asset)
                known_as = [asset.get("id"), *_reference_asset_candidates(asset)]
                alias_keys: List[str] = []
                for item in [preferred, *known_as]:
                    key = _reference_alias_key(item)
                    if key and key not in alias_keys:
                        alias_keys.append(key)
                existing_index = next((aliases[key] for key in alias_keys if key in aliases), None)
                if existing_index is not None:
                    current = refs[existing_index]
                    current_is_public_url = bool(
                        re.match(r"^https?://", current, re.I) and not _is_local_http_reference(current)
                    )
                    if not (current_is_public_url and _is_existing_local_reference(preferred)):
                        refs[existing_index] = preferred
                    for key in alias_keys:
                        aliases[key] = existing_index
                    continue
                if add_reference_assets:
                    _append_media_reference(
                        refs,
                        aliases,
                        preferred,
                        also_known_as=known_as,
                        replace_existing=True,
                    )
        return refs

    # Canvas nodes can expose the same image through several aliases. Prefer the
    # explicit request fields and use the other aliases only as fallbacks.
    direct_refs = collect(
        keys=("image", "images", "referenceImages", "reference_images"),
        include_reference_assets=True,
        add_reference_assets=False,
    )
    if direct_refs:
        return direct_refs
    input_refs = collect(
        input_keys=("referenceImages", "reference_images"),
        include_reference_assets=True,
        add_reference_assets=False,
    )
    if input_refs:
        return input_refs
    frame_refs = collect(keys=(
        "startFrame",
        "start_frame",
        "startFrameUrl",
        "start_frame_url",
        "endFrame",
        "end_frame",
        "endFrameUrl",
        "end_frame_url",
    ))
    if frame_refs:
        return frame_refs
    return collect(include_reference_assets=True)


def _reference_alias_key(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    parsed = urlparse(normalized)
    if parsed.scheme.lower() == "asset":
        asset_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
        if re.fullmatch(r"asset_[A-Za-z0-9_-]+", asset_id):
            return f"asset:{asset_id}"
    path = unquote(parsed.path or normalized)
    asset_match = re.search(r"(?:^|/)assets/(asset_[A-Za-z0-9_-]+)(?:$|/)", path)
    if asset_match:
        return f"asset:{asset_match.group(1)}"
    if re.fullmatch(r"asset_[A-Za-z0-9_-]+", normalized):
        return f"asset:{normalized}"
    return normalized


def _reference_asset_candidates(asset: Dict[str, Any]) -> List[Any]:
    return [
        asset.get("src"),
        asset.get("url"),
        asset.get("assetUrl"),
        asset.get("assetPath"),
        asset.get("path"),
    ]


def _is_existing_local_reference(value: Any) -> bool:
    text = _as_text(value)
    if not text:
        return False
    if text.startswith("file:"):
        return True
    if re.match(r"^https?://|^data:|^blob:|^/assets/", text, re.I):
        return False
    try:
        candidate = Path(text)
    except (TypeError, ValueError):
        return False
    return candidate.exists() and candidate.is_file()


def _preferred_reference_asset_source(asset: Dict[str, Any]) -> str:
    for value in (asset.get("assetPath"), asset.get("path")):
        if _is_existing_local_reference(value):
            return _as_text(value)
    for value in _reference_asset_candidates(asset):
        text = _as_text(value)
        if text:
            return text
    return ""


def _append_media_reference(
    refs: List[str],
    aliases: Dict[str, int],
    value: Any,
    *,
    also_known_as: Optional[List[Any]] = None,
    replace_existing: bool = False,
) -> None:
    text = _as_text(value)
    if not text:
        return
    keys: List[str] = []
    for item in [text, *(also_known_as or [])]:
        key = _reference_alias_key(item)
        if key and key not in keys:
            keys.append(key)
    existing_index = next((aliases[key] for key in keys if key in aliases), None)
    if existing_index is not None:
        if replace_existing:
            refs[existing_index] = text
        for key in keys:
            aliases[key] = existing_index
        return
    index = len(refs)
    refs.append(text)
    for key in keys:
        aliases[key] = index


def _media_reference_inputs(
    payload: Dict[str, Any],
    *,
    kind: str,
    keys: tuple[str, ...],
    input_keys: tuple[str, ...],
) -> List[str]:
    refs: List[str] = []
    aliases: Dict[str, int] = {}
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                _append_media_reference(refs, aliases, item)
        elif value is not None:
            _append_media_reference(refs, aliases, value)
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    for key in input_keys:
        value = inputs.get(key)
        if isinstance(value, list):
            for item in value:
                _append_media_reference(refs, aliases, item)
        elif value is not None:
            _append_media_reference(refs, aliases, value)
    for asset in payload.get("referenceAssets") or []:
        if not isinstance(asset, dict):
            continue
        asset_kind = str(asset.get("kind") or asset.get("type") or "").strip().lower()
        if asset_kind != kind:
            continue
        _append_media_reference(
            refs,
            aliases,
            _preferred_reference_asset_source(asset),
            also_known_as=[asset.get("id"), *_reference_asset_candidates(asset)],
            replace_existing=True,
        )
    return refs


def _video_file_reference_inputs(payload: Dict[str, Any]) -> List[str]:
    return _media_reference_inputs(
        payload,
        kind="video",
        keys=("video", "videos", "referenceVideos", "reference_videos", "sourceVideoUrl", "source_video_url"),
        input_keys=("referenceVideos", "reference_videos"),
    )


def _audio_file_reference_inputs(payload: Dict[str, Any]) -> List[str]:
    return _media_reference_inputs(
        payload,
        kind="audio",
        keys=("audio", "audios", "referenceAudios", "reference_audios", "sourceAudioUrl", "source_audio_url"),
        input_keys=("referenceAudios", "reference_audios"),
    )


def _image_reference_inputs(payload: Dict[str, Any], backend_base_url: Optional[str] = None) -> List[str]:
    values: List[Any] = []
    for key in REFERENCE_IMAGE_PAYLOAD_FIELD_NAMES:
        value = payload.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    for key in REFERENCE_IMAGE_PAYLOAD_FIELD_NAMES:
        value = inputs.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    refs: List[str] = []
    for value in values:
        for text in _reference_image_item_sources(value):
            if not text:
                continue
            if text.startswith("/assets/"):
                text = _local_asset_url(text, backend_base_url)
            if text not in refs:
                refs.append(text)
    return refs


def _is_newapi_image_edit(payload: Dict[str, Any]) -> bool:
    mode = str(_first_payload_value(payload, "mode", "taskType", "task_type", "operation") or "").strip().lower()
    if "edit" in mode:
        return True
    return any(payload.get(key) for key in ("maskImage", "mask_image", "maskUrl", "mask_url"))


def _build_newapi_image_body(model: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    model_name = str(model.get("modelName") or payload.get("modelName") or payload.get("model") or "").strip()
    aspect_ratio = _first_payload_value(payload, "aspectRatio", "aspect_ratio", "ratio")
    output_resolution = _first_payload_value(payload, "outputResolution", "output_resolution", "resolution")
    xinghe_resolution = _xinghe_stable_image_resolution(model_name)
    images = _image_reference_inputs(payload, _as_text(payload.get("_backendBaseUrl")))
    max_refs = _first_model_param(model, "maxReferenceImages")
    if max_refs is not None:
        try:
            max_refs_int = int(max_refs)
        except (TypeError, ValueError):
            max_refs_int = 0
        if max_refs_int > 0 and len(images) > max_refs_int:
            raise ProviderAdapterError(f"{model_name or '图片模型'} 最多支持 {max_refs_int} 张参考图，当前提供了 {len(images)} 张")
    if xinghe_resolution:
        body: Dict[str, Any] = {
            "model": model_name,
            "prompt": _effective_prompt(payload),
            "aspect_ratio": aspect_ratio or _first_model_param(model, "defaultRatio", "defaultAspectRatio") or "1:1",
            "resolution": xinghe_resolution,
            "reasoning_effort": (
                _first_payload_value(payload, "reasoningEffort", "reasoning_effort")
                or _first_model_param(model, "reasoningEffort", "reasoning_effort")
                or "medium"
            ),
        }
        if images:
            body["reference_images"] = images
        return body
    mapped_size = _newapi_image_size_from_ratio_resolution(model_name, aspect_ratio, output_resolution)
    explicit_size = _first_payload_value(payload, "size")
    prefers_size_mapping = _nanobanana_size_table(model_name) is not None
    body: Dict[str, Any] = {
        "model": model_name,
        "prompt": _effective_prompt(payload),
        "n": _first_payload_value(payload, "n", "count") or 1,
        "response_format": _first_payload_value(payload, "responseFormat", "response_format") or "url",
    }
    task_type = _first_payload_value(payload, "taskType", "task_type")
    if task_type:
        body["task_type"] = task_type
    if aspect_ratio and not prefers_size_mapping:
        body["aspect_ratio"] = aspect_ratio
    if output_resolution and not prefers_size_mapping:
        body["output_resolution"] = output_resolution
    size = explicit_size or mapped_size or _first_model_param(model, "defaultSize")
    if size:
        body["size"] = size
    quality = (
        _first_payload_value(payload, "quality")
        or _first_model_param(model, "quality", "defaultQuality")
        or (mapped_size and _newapi_image_quality_from_resolution(output_resolution))
    )
    if quality:
        body["quality"] = quality
    negative_prompt = _first_payload_value(payload, "negativePrompt", "negative_prompt")
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    images = _image_reference_inputs(payload, _as_text(payload.get("_backendBaseUrl")))
    reference_mode = _first_payload_value(payload, "referenceMode", "reference_mode") or _first_model_param(
        model,
        "defaultReferenceMode",
        "referenceMode",
        "reference_mode",
    )
    if reference_mode and images:
        body["reference_mode"] = reference_mode
    if images:
        body["image"] = images
    return body


def _normalize_seedance_reference_mode(value: Any) -> str:
    if value is None:
        return "omni_reference"
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"", "text", "text_video", "none", "no_reference"}:
        return ""
    if text in {"first_last_frames", "first_last_frame", "first_last", "keyframe", "key_frame", "首尾帧"}:
        return "first_last_frames"
    if text in {"omni_reference", "omni", "image_video", "image_ref", "image", "frame", "全能参考", "图生视频", "图片参考"}:
        return "omni_reference"
    return text or "omni_reference"


def _newapi_video_model_key(model: Dict[str, Any]) -> str:
    return str(model.get("id") or model.get("modelName") or "").strip().lower()


SORA3_SEEDANCE_MODEL_ALIASES = {
    "sora-3-fast": "seedance-2.0-fast",
    "sora-3-pro": "seedance-2.0-pro",
}
SEEDANCE_VIDEO_MODEL_KEYS = {
    "seedance-2.0-fast",
    "seedance-2.0-pro",
    "seedance-2-0",
    "seedance-2-0-fast",
    "seedance-2-0-pro",
    *SORA3_SEEDANCE_MODEL_ALIASES,
}
CHANNEL31_FACE_PRO_MODEL_NAME = "seedence2.0（过人脸渠道五）"
CHANNEL31_FACE_FAST_MODEL_NAME = "seedence2.0fast（过人脸渠道五）"
CHANNEL31_FACE_VIDEO_MODEL_KEYS = {
    CHANNEL31_FACE_PRO_MODEL_NAME.lower(),
    CHANNEL31_FACE_FAST_MODEL_NAME.lower(),
    "seedence2.0(过人脸渠道五)",
    "seedence2.0fast(过人脸渠道五)",
}
HERMES_SEEDANCE_MODEL_KEYS = {
    "seedance-2",
    "seedance-2（满血）",
    "seedance-2(满血)",
    "不过真人满血渠道",
    "不过真人满血",
}
HERMES_SEEDANCE_NEWAPI_MODEL_NAME = "seedance-2（满血）"
HERMES_SEEDANCE_RATIOS = {"16:9", "9:16", "1:1", "4:3", "21:9", "adaptive"}
HERMES_SEEDANCE_RESOLUTIONS = {"480p", "720p", "1080p"}
MUSE_VIDEO_V1_MODEL_KEYS = {
    "seedence2.0-m-c",
    "seedence2.0fast-m-c",
    "muse_seedance20",
    "muse_seedance20_fast",
}
CHANNEL22_FAST_MODEL_NAME = "seedence2.0-fast（满血过人脸渠道2）"
CHANNEL22_PRO_MODEL_NAME = "seedence2.0-pro（满血过人脸渠道2）"
MUSE_CHANNEL22_PROMPT_CHAR_LIMIT = 1400
MUSE_CHANNEL22_FAST_POR_MODEL_KEYS = {
    "muse_sd2_channel22_fast_por",
    CHANNEL22_FAST_MODEL_NAME,
    "seedence2.0-fast-2",
    "videos_fast-por",
}
MUSE_CHANNEL22_PRO_POR_MODEL_KEYS = {
    "seedence2.0-pro-2",
    "muse_sd2_channel22_pro_por",
    "muse_sd2_channel22_fset_por",
    CHANNEL22_PRO_MODEL_NAME,
    "seedence2.0-fset（满血过人脸渠道2）",
    "seedence2.0-fset-2",
    "videos-por",
}
MUSE_VIDEO_V2_MODEL_KEYS = {
    "seedence2.0-real-person-m-c",
    "seedence2.0-company-m-c",
    "seedence2.0人脸-m-c",
    "seedence2.0企业-m-c",
    "muse_see_dance_2_0_real_person",
    "muse_see_dance_2_0_real_person_company",
    "muse_sd2_fast_real_full",
    "muse_sd2_real_full",
    *MUSE_CHANNEL22_FAST_POR_MODEL_KEYS,
    *MUSE_CHANNEL22_PRO_POR_MODEL_KEYS,
    "muse_sd2_fast_full",
    "muse_sd2_full",
    "muse_sd2_fast_four",
    "muse_sd2_four",
}
MUSE_VIDEO_MODEL_KEYS = MUSE_VIDEO_V1_MODEL_KEYS | MUSE_VIDEO_V2_MODEL_KEYS
MUSE_CHANNEL22_POR_MODEL_KEYS = MUSE_CHANNEL22_FAST_POR_MODEL_KEYS | MUSE_CHANNEL22_PRO_POR_MODEL_KEYS


def _is_veo_video_model(model: Dict[str, Any]) -> bool:
    key = _newapi_video_model_key(model)
    return key in {"veo31", "veo31-fast", "veo31ref"} or key.startswith("veo")


def _is_seedance_video_model(model: Dict[str, Any]) -> bool:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    if str(params.get("videoProtocol") or "").strip().lower() == "public_video_api":
        return False
    return (
        _newapi_video_model_key(model) in SEEDANCE_VIDEO_MODEL_KEYS
        or str(params.get("upstreamModelName") or "").strip().lower() in SEEDANCE_VIDEO_MODEL_KEYS
    )


def _is_channel31_face_video_model(model: Dict[str, Any]) -> bool:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    values = (
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        params.get("modelName"),
        params.get("upstreamModelName"),
    )
    return any(str(value or "").strip().lower() in CHANNEL31_FACE_VIDEO_MODEL_KEYS for value in values)


def _hermes_video_model_tokens(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> set[str]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    values = [
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        model.get("model"),
        params.get("modelName"),
        params.get("requestModelName"),
        params.get("upstreamModelName"),
        params.get("model"),
        params.get("videoProtocol"),
    ]
    if isinstance(payload, dict):
        values.extend([
            payload.get("model"),
            payload.get("modelName"),
            payload.get("providerModelId"),
            payload.get("displayName"),
        ])
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def _is_hermes_video_model(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> bool:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    if str(params.get("videoProtocol") or "").strip().lower() == "hermes_video":
        return True
    return bool(_hermes_video_model_tokens(model, payload) & HERMES_SEEDANCE_MODEL_KEYS)


def _hermes_request_model_name(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    text = _as_text(params.get("requestModelName"))
    if text:
        return text
    if _hermes_video_model_tokens(model, payload) & HERMES_SEEDANCE_MODEL_KEYS:
        return HERMES_SEEDANCE_NEWAPI_MODEL_NAME
    for value in (
        model.get("modelName"),
        payload.get("modelName"),
        payload.get("model"),
        model.get("displayName"),
        params.get("modelName"),
        params.get("model"),
        params.get("upstreamModelName"),
    ):
        text = _as_text(value)
        if text:
            return text
    return str(model.get("modelName") or payload.get("modelName") or payload.get("model") or model.get("id") or "").strip()


def _seedance_request_model_name(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    raw = str(model.get("modelName") or payload.get("modelName") or payload.get("model") or model.get("id") or "").strip()
    if raw.lower() in SORA3_SEEDANCE_MODEL_ALIASES:
        return raw
    explicit = str(params.get("upstreamModelName") or "").strip()
    if explicit:
        return explicit
    return raw


def _muse_video_model_tokens(model: Dict[str, Any]) -> set[str]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    values = (
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        params.get("modelName"),
        params.get("upstreamModelName"),
    )
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def _is_muse_video_model(model: Dict[str, Any]) -> bool:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    if str(params.get("videoProtocol") or "").strip().lower() == "muse_video":
        return True
    return bool(_muse_video_model_tokens(model) & MUSE_VIDEO_MODEL_KEYS)


def _is_muse_channel22_por_model(model: Dict[str, Any]) -> bool:
    return bool(_muse_video_model_tokens(model) & MUSE_CHANNEL22_POR_MODEL_KEYS)


def _muse_video_api_version(model: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    explicit = str(params.get("museApiVersion") or params.get("apiVersion") or "").strip().lower()
    if explicit in {"2", "v2", "api_v2"}:
        return "v2"
    if explicit in {"1", "v1", "api_v1"}:
        return "v1"
    tokens = _muse_video_model_tokens(model)
    if tokens & MUSE_VIDEO_V2_MODEL_KEYS:
        return "v2"
    return "v1"


FIREFLY_VIDEO_API_PROFILES: Dict[str, Dict[str, Any]] = {
    "firefly-sora2": {
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 6,
        "supportsStartEndFrames": False,
    },
    "firefly-sora2-pro": {
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 6,
        "supportsStartEndFrames": False,
    },
    "firefly-veo31": {
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
    },
    "firefly-veo31-fast": {
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
    },
    "firefly-veo31-ref": {
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
    },
    "firefly-kling30omni": {
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 7,
        "supportsStartEndFrames": True,
    },
    "firefly-kling30": {
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
    },
}

OTU_VIDEO_RATIO_SIZE_MAP = {
    "16:9": "1280x720",
    "9:16": "720x1280",
    "1:1": "1024x1024",
}
OTU_VEO_RATIO_SIZE_MAP = {
    "16:9": "1280x720",
    "9:16": "720x1280",
}
GROK15_VIDEO_SIZES = ["1024x1024", "1024x1792", "1792x1024", "1280x720", "720x1280"]
GROK15_VIDEO_RATIO_SIZE_MAP = {
    "1:1": "1024x1024",
    "9:16": "720x1280",
    "16:9": "1280x720",
    "4:7": "1024x1792",
    "7:4": "1792x1024",
}


def _firefly_video_model_key(model: Dict[str, Any]) -> str:
    key = _newapi_video_model_key(model)
    if key.startswith("newapi."):
        key = key.split(".", 1)[1]
    return key


def _is_firefly_video_model(model: Dict[str, Any]) -> bool:
    key = _firefly_video_model_key(model)
    return key in FIREFLY_VIDEO_API_PROFILES or key.startswith(("firefly-sora", "firefly-veo", "firefly-kling"))


def _firefly_video_profile(model: Dict[str, Any]) -> Dict[str, Any]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    key = _firefly_video_model_key(model)
    if key in FIREFLY_VIDEO_API_PROFILES:
        return {**FIREFLY_VIDEO_API_PROFILES[key], **params}
    return dict(params)


PUBLIC_VIDEO_API_PROFILES: Dict[str, Dict[str, Any]] = {
    "grok3-video": {
        "supportedDurations": [6, 10],
        "defaultDuration": 6,
        "ratios": ["16:9", "9:16", "3:2", "2:3", "1:1"],
        "defaultRatio": "16:9",
        "supportedResolutions": ["480p", "720p"],
        "defaultResolutionName": "480p",
        "multiImageReferenceField": "reference_images",
        "includePreset": True,
        "defaultPreset": "normal",
        "maxReferenceImages": 7,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
        "includeGenerateAudio": False,
    },
    "grok1.5-video": {
        "supportedDurations": list(range(1, 16)),
        "defaultDuration": 4,
        "ratios": list(GROK15_VIDEO_RATIO_SIZE_MAP),
        "defaultRatio": "9:16",
        "supportedResolutions": GROK15_VIDEO_SIZES,
        "defaultResolutionName": "720x1280",
        "defaultSize": "720x1280",
        "sizes": GROK15_VIDEO_SIZES,
        "ratioSizeMap": GROK15_VIDEO_RATIO_SIZE_MAP,
        "durationField": "seconds",
        "durationAsString": True,
        "sizeField": "size",
        "includeSize": True,
        "includeRatio": False,
        "includeResolution": True,
        "singleImageReferenceField": "image_url",
        "maxReferenceImages": 1,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
        "includeGenerateAudio": False,
        "requiresImageReference": True,
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "requestModelName": "grok1.5-video",
        "upstreamModelName": "grok-imagine-video-1.5-preview",
    },
    "kling-video-3.0": {
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["1:1", "16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "720p",
        "maxReferenceImages": 1,
        "maxReferenceVideos": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "kling-video-o3-omni": {
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["1:1", "16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "720p",
        "maxReferenceImages": 7,
        "maxReferenceVideos": 1,
        "supportsStartEndFrames": True,
        "supportsVideoReference": True,
    },
    "sora2": {
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "includeResolution": False,
        "includeGenerateAudio": False,
        "imageReferenceField": "input_reference",
        "maxReferenceImages": 1,
        "maxReferenceVideos": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "veo_3_1-fl": {
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 8,
        "ratios": ["16:9", "9:16"],
        "defaultRatio": "16:9",
        "defaultSize": "1280x720",
        "ratioSizeMap": OTU_VEO_RATIO_SIZE_MAP,
        "sizeField": "size",
        "includeSize": True,
        "includeDuration": False,
        "includeRatio": False,
        "includeResolution": False,
        "includeGenerateAudio": False,
        "multiImageReferenceField": "images",
        "supportedReferenceModes": ["first_last_frames", "image"],
        "defaultReferenceMode": "first_last_frames",
        "firstLastAsImageArray": True,
        "maxReferenceImages": 2,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "veo_3_1-fast-fl": {
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 8,
        "ratios": ["16:9", "9:16"],
        "defaultRatio": "16:9",
        "defaultSize": "1280x720",
        "ratioSizeMap": OTU_VEO_RATIO_SIZE_MAP,
        "sizeField": "size",
        "includeSize": True,
        "includeDuration": False,
        "includeRatio": False,
        "includeResolution": False,
        "includeGenerateAudio": False,
        "multiImageReferenceField": "images",
        "supportedReferenceModes": ["first_last_frames", "image"],
        "defaultReferenceMode": "first_last_frames",
        "firstLastAsImageArray": True,
        "maxReferenceImages": 2,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "omni_flash-10s": {
        "supportedDurations": [10],
        "defaultDuration": 10,
        "ratios": ["16:9", "9:16", "1:1"],
        "defaultRatio": "16:9",
        "defaultSize": "1280x720",
        "ratioSizeMap": OTU_VIDEO_RATIO_SIZE_MAP,
        "sizeField": "size",
        "includeSize": True,
        "includeDuration": False,
        "includeRatio": False,
        "includeResolution": False,
        "includeGenerateAudio": False,
        "multiImageReferenceField": "images",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 7,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "seedence2-pro（特价版2）": {
        "supportedDurations": list(range(4, 16)),
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16", "1:1"],
        "defaultRatio": "16:9",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "duration",
        "ratioField": "ratio",
        "multiImageReferenceField": "referenceImages",
        "videoReferenceField": "referenceVideos",
        "videoReferenceAsList": True,
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 3,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": True,
        "supportsVideoReference": True,
    },
    "seedence2-fast（特价版2）": {
        "supportedDurations": list(range(4, 16)),
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16", "1:1"],
        "defaultRatio": "16:9",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "duration",
        "ratioField": "ratio",
        "multiImageReferenceField": "referenceImages",
        "videoReferenceField": "referenceVideos",
        "videoReferenceAsList": True,
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 3,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": True,
        "supportsVideoReference": True,
    },
    "sora-vip3-pro-720p": {
        "supportedDurations": [5, 10, 15],
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
        "defaultRatio": "16:9",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "videoReferenceField": "reference_video",
        "audioReferenceField": "audio_url",
        "supportedReferenceModes": ["image", "video_reference"],
        "defaultReferenceMode": "image",
        "includeGenerateAudio": False,
        "maxReferenceImages": 9,
        "maxReferenceVideos": 1,
        "maxReferenceAudios": 1,
        "supportsStartEndFrames": False,
        "supportsVideoReference": True,
    },
    "sora-vip3-pro-1080p": {
        "supportedDurations": [5, 10, 15],
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
        "defaultRatio": "16:9",
        "supportedResolutions": ["480p", "720p", "1080p"],
        "defaultResolutionName": "1080p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "videoReferenceField": "reference_video",
        "audioReferenceField": "audio_url",
        "supportedReferenceModes": ["image", "video_reference"],
        "defaultReferenceMode": "image",
        "includeGenerateAudio": False,
        "maxReferenceImages": 9,
        "maxReferenceVideos": 1,
        "maxReferenceAudios": 1,
        "supportsStartEndFrames": False,
        "supportsVideoReference": True,
    },
    "sora-v3-pro": {
        "supportedDurations": list(range(5, 16)),
        "defaultDuration": 10,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "sora-v3-fast": {
        "supportedDurations": list(range(5, 16)),
        "defaultDuration": 10,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "seedance-2-0-pro": {
        "supportedDurations": list(range(4, 16)),
        "defaultDuration": 5,
        "ratios": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
        "defaultRatio": "auto",
        "supportedResolutions": ["480p", "720p", "1080p"],
        "defaultResolutionName": "720p",
        "ratioField": "ratio",
        "defaultGenerateAudio": True,
        "includeGenerateAudio": True,
        "includeSeed": True,
        "multiImageReferenceField": "images",
        "maxReferenceImages": 9,
        "maxReferenceAudios": 9,
        "maxReferenceVideos": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
}


PUBLIC_VIDEO_API_PROFILES["seedance-2-0-fast"] = {
    **PUBLIC_VIDEO_API_PROFILES["seedance-2-0-pro"],
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultRatio": "16:9",
}
PUBLIC_VIDEO_API_PROFILES["seedance-2-0(火山官转)"] = PUBLIC_VIDEO_API_PROFILES["seedance-2-0-pro"]
PUBLIC_VIDEO_API_PROFILES["seedance-2-0(火山版本)"] = PUBLIC_VIDEO_API_PROFILES["seedance-2-0-pro"]
PUBLIC_VIDEO_API_PROFILES["seedance-2-0-pro(火山版本)"] = PUBLIC_VIDEO_API_PROFILES["seedance-2-0-pro"]
PUBLIC_VIDEO_API_PROFILES["seedance-2-0-fast(火山官转)"] = PUBLIC_VIDEO_API_PROFILES["seedance-2-0-fast"]
PUBLIC_VIDEO_API_PROFILES["seedance-2-0-fast(火山版本)"] = PUBLIC_VIDEO_API_PROFILES["seedance-2-0-fast"]
SEEDANCE_DASH_FORCE_AUDIO_PROFILE_KEYS = {
    "seedance-2-0(火山官转)",
    "seedance-2-0(火山版本)",
    "seedance-2-0-pro(火山官转)",
    "seedance-2-0-pro(火山版本)",
    "seedance-2-0-fast(火山官转)",
}

CHANNEL31_FACE_PUBLIC_PROFILE_BASE: Dict[str, Any] = {
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 6,
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultRatio": "16:9",
    "supportedResolutions": ["480p", "720p", "1080p"],
    "defaultResolutionName": "720p",
    "durationField": "duration",
    "ratioField": "aspect_ratio",
    "multiImageReferenceField": "images",
    "audioReferenceField": "audios",
    "audioReferenceAsList": True,
    "referenceTokenPrefixes": {"image": "image", "video": "video", "audio": "audio"},
    "includeGenerateAudio": True,
    "defaultGenerateAudio": True,
    "includeSeed": True,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 0,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": False,
}
PUBLIC_VIDEO_API_PROFILES[CHANNEL31_FACE_PRO_MODEL_NAME.lower()] = {
    **CHANNEL31_FACE_PUBLIC_PROFILE_BASE,
}
PUBLIC_VIDEO_API_PROFILES[CHANNEL31_FACE_FAST_MODEL_NAME.lower()] = {
    **CHANNEL31_FACE_PUBLIC_PROFILE_BASE,
    "supportedResolutions": ["480p", "720p"],
}

CHANNEL27_911_SEEDANCE_MODEL_SPECS: Dict[str, Dict[str, str]] = {
    "seedance-2.0-16-9-720-15": {
        "requestModelName": "seedance-2.0+16:9+720+15",
        "ratio": "16:9",
    },
    "seedance-2.0-9-16-720-15": {
        "requestModelName": "seedance-2.0+9:16+720+15",
        "ratio": "9:16",
    },
}
CHANNEL27_911_SEEDANCE_PROFILE_BASE: Dict[str, Any] = {
    "supportedDurations": [15],
    "defaultDuration": 15,
    "supportedResolutions": ["720p"],
    "defaultResolutionName": "720p",
    "durationField": "duration",
    "ratioField": "aspect_ratio",
    "includeRatio": False,
    "multiImageReferenceField": "input_images",
    "videoReferenceField": "reference_video_ids",
    "videoReferenceAsList": False,
    "audioReferenceField": "reference_audio_ids",
    "audioReferenceAsList": False,
    "supportedReferenceModes": ["image", "video_reference"],
    "defaultReferenceMode": "image",
    "includeGenerateAudio": False,
    "defaultAsync": True,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 1,
    "maxReferenceAudios": 1,
    "audioReferenceMaxDurationSeconds": SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
for _channel27_model_id, _channel27_spec in CHANNEL27_911_SEEDANCE_MODEL_SPECS.items():
    PUBLIC_VIDEO_API_PROFILES[_channel27_model_id] = {
        **CHANNEL27_911_SEEDANCE_PROFILE_BASE,
        "requestModelName": _channel27_spec["requestModelName"],
        "ratios": [_channel27_spec["ratio"]],
        "defaultRatio": _channel27_spec["ratio"],
    }

ARTIFEX_SEEDENCE20_PUBLIC_PROFILE_BASE = {
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 6,
    "ratios": ["16:9", "9:16", "1:1", "21:9", "3:4", "4:3"],
    "defaultRatio": "16:9",
    "durationField": "duration",
    "ratioField": "aspect_ratio",
    "multiImageReferenceField": "extra_images",
    "videoReferenceField": "extra_videos",
    "videoReferenceAsList": True,
    "audioReferenceField": "extra_audios",
    "audioReferenceAsList": True,
    "supportedReferenceModes": ["image", "video_reference"],
    "defaultReferenceMode": "image",
    "includeResolution": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "audioReferenceMaxDurationSeconds": SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
    "referenceTokenPrefixes": {"image": "Image", "video": "Video", "audio": "Audio"},
}
ARTIFEX_SEEDENCE20_PUBLIC_MODEL_SPECS = {
    "seedence2.0fast-480": {"upstreamModelName": "video-fast-480p", "supportedResolutions": ["480p"], "defaultResolutionName": "480p"},
    "seedence2.0fast-720": {"upstreamModelName": "video-fast-720p", "supportedResolutions": ["720p"], "defaultResolutionName": "720p"},
    "seedence2.0pro-480": {"upstreamModelName": "video-pro-480p", "supportedResolutions": ["480p"], "defaultResolutionName": "480p"},
    "seedence2.0pro-720": {"upstreamModelName": "video-pro-720p", "supportedResolutions": ["720p"], "defaultResolutionName": "720p"},
}
for _artifex_model_id, _artifex_spec in ARTIFEX_SEEDENCE20_PUBLIC_MODEL_SPECS.items():
    _artifex_profile = {
        **ARTIFEX_SEEDENCE20_PUBLIC_PROFILE_BASE,
        **_artifex_spec,
    }
    PUBLIC_VIDEO_API_PROFILES[_artifex_model_id] = _artifex_profile
    PUBLIC_VIDEO_API_PROFILES[_artifex_spec["upstreamModelName"]] = _artifex_profile

SHISHIKEJI_CHANNEL4_PUBLIC_PROFILE_BASE = {
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 4,
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultRatio": "9:16",
    "supportedResolutions": ["480p", "720p"],
    "defaultResolutionName": "720p",
    "durationField": "duration",
    "ratioField": "ratio",
    "multiImageReferenceField": "images",
    "videoReferenceField": "videos",
    "videoReferenceAsList": True,
    "audioReferenceField": "audios",
    "audioReferenceAsList": True,
    "supportedReferenceModes": ["omni_reference", "image", "video_reference"],
    "defaultReferenceMode": "omni_reference",
    "includeResolution": True,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "audioReferenceMaxDurationSeconds": SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
SHISHIKEJI_CHANNEL4_PUBLIC_MODEL_SPECS = {
    "seedence-fast（满血人脸渠道4）": {"upstreamModelName": "transit9-fast"},
    "seedence-pro（满血人脸渠道4）": {"upstreamModelName": "transit9-2.0"},
}
for _shishikeji_model_id, _shishikeji_spec in SHISHIKEJI_CHANNEL4_PUBLIC_MODEL_SPECS.items():
    _shishikeji_profile = {
        **SHISHIKEJI_CHANNEL4_PUBLIC_PROFILE_BASE,
        **_shishikeji_spec,
    }
    PUBLIC_VIDEO_API_PROFILES[_shishikeji_model_id] = _shishikeji_profile
    PUBLIC_VIDEO_API_PROFILES[_shishikeji_spec["upstreamModelName"]] = _shishikeji_profile

CHANNEL35_NO_AUDIO_SPECIAL_PUBLIC_PROFILE_BASE: Dict[str, Any] = {
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 4,
    "ratios": ["16:9", "9:16", "1:1"],
    "defaultRatio": "16:9",
    "supportedResolutions": ["720p"],
    "defaultResolutionName": "720p",
    "durationField": "duration",
    "ratioField": "ratio",
    "multiImageReferenceField": "referenceImages",
    "videoReferenceField": "referenceVideos",
    "videoReferenceAsList": True,
    "supportedReferenceModes": ["image", "first_last_frames", "video_reference"],
    "defaultReferenceMode": "image",
    "includeGenerateAudio": False,
    "defaultGenerateAudio": False,
    "maxReferenceImages": 4,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 0,
    "supportsStartEndFrames": True,
    "supportsVideoReference": True,
}
CHANNEL35_NO_AUDIO_SPECIAL_PUBLIC_MODEL_SPECS: Dict[str, Dict[str, str]] = {
    "seedence2.0（特价版不带音频）": {
        "requestModelName": "seedence2.0（特价版不带音频）",
        "upstreamModelName": "videos_stable",
    },
    "seedence2.0-fast（特价版不带音频）": {
        "requestModelName": "seedence2.0-fast（特价版不带音频）",
        "upstreamModelName": "videos_stable_fast",
    },
    "seedence2.0（新特价渠道）": {
        "requestModelName": "seedence2.0（特价版不带音频）",
        "upstreamModelName": "videos_stable",
    },
    "seedence2.0-fast（新特价渠道）": {
        "requestModelName": "seedence2.0-fast（特价版不带音频）",
        "upstreamModelName": "videos_stable_fast",
    },
}
for _channel35_model_id, _channel35_spec in CHANNEL35_NO_AUDIO_SPECIAL_PUBLIC_MODEL_SPECS.items():
    _channel35_profile = {
        **CHANNEL35_NO_AUDIO_SPECIAL_PUBLIC_PROFILE_BASE,
        **_channel35_spec,
    }
    _channel35_normalized_id = _channel35_model_id.strip().lower().replace("（", "(").replace("）", ")")
    PUBLIC_VIDEO_API_PROFILES[_channel35_model_id] = _channel35_profile
    PUBLIC_VIDEO_API_PROFILES[_channel35_normalized_id] = _channel35_profile
    PUBLIC_VIDEO_API_PROFILES[_channel35_spec["upstreamModelName"]] = _channel35_profile


PUBLIC_VIDEO_MODEL_ALIASES: Dict[str, str] = {
    "grok-imagine-video-1.5-preview": "grok1.5-video",
    "veo3.1": "veo_3_1-fl",
    "veo3.1-fast": "veo_3_1-fast-fl",
    "google-omin": "omni_flash-10s",
    "seedence-2.0-16-9-720-15": "seedance-2.0-16-9-720-15",
    "seedence-2.0-9-16-720-15": "seedance-2.0-9-16-720-15",
    "seedence-2.0+16:9+720+15": "seedance-2.0-16-9-720-15",
    "seedence-2.0+9:16+720+15": "seedance-2.0-9-16-720-15",
    "seedence2-fast（特价版1）": "sora-v3-fast",
    "seedence2-pro（特价版1）": "sora-v3-pro",
    "seedence2.0-720（满血）": "sora-vip3-pro-720p",
    "真人人脸即梦满血版": "sora-vip3-pro-720p",
    "seedence-fast-4": "seedence-fast（满血人脸渠道4）",
    "seedence-pro-4": "seedence-pro（满血人脸渠道4）",
    "transit9-fast": "seedence-fast（满血人脸渠道4）",
    "transit9-2.0": "seedence-pro（满血人脸渠道4）",
    "seedence2.0(过人脸渠道五)": CHANNEL31_FACE_PRO_MODEL_NAME.lower(),
    "seedence2.0fast(过人脸渠道五)": CHANNEL31_FACE_FAST_MODEL_NAME.lower(),
}


def _seedance_dash_huoshan_guanzhuan_profile_key(value: Any) -> str:
    key = str(value or "").strip().lower().replace("（", "(").replace("）", ")")
    if "seedance-2-0" not in key or "火山官转" not in key:
        return ""
    if "fast" in key:
        return "seedance-2-0-fast(火山官转)"
    return "seedance-2-0(火山官转)"


def _seedance_dash_force_audio_profile_key(model: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    for value in (
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        params.get("upstreamModelName"),
        params.get("modelName"),
    ):
        suffix_key = _seedance_dash_huoshan_guanzhuan_profile_key(value)
        if suffix_key in SEEDANCE_DASH_FORCE_AUDIO_PROFILE_KEYS:
            return suffix_key
    return ""


def _public_video_model_key(model: Dict[str, Any]) -> str:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    for value in (
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
        params.get("upstreamModelName"),
        params.get("modelName"),
    ):
        key = _seedance_dash_identity(value)
        if key in PUBLIC_VIDEO_API_PROFILES:
            return key
        alias = PUBLIC_VIDEO_MODEL_ALIASES.get(key)
        if alias in PUBLIC_VIDEO_API_PROFILES:
            return alias
        suffix_key = _seedance_dash_huoshan_guanzhuan_profile_key(value)
        if suffix_key in PUBLIC_VIDEO_API_PROFILES:
            return suffix_key
    return ""


def _public_video_api_profile(model: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    key = _public_video_model_key(model)
    if key:
        profile = {**PUBLIC_VIDEO_API_PROFILES[key], **params}
        if key in SEEDANCE_DASH_FORCE_AUDIO_PROFILE_KEYS or _seedance_dash_force_audio_profile_key(model):
            profile["includeGenerateAudio"] = True
            profile["defaultGenerateAudio"] = True
        if key == "grok1.5-video":
            for stale_key in (
                "defaultPreset",
                "imageReferenceField",
                "includePreset",
                "multiImageReferenceField",
                "referenceImageField",
            ):
                profile.pop(stale_key, None)
        return profile
    if str(params.get("videoProtocol") or "").strip().lower() == "public_video_api":
        return dict(params)
    return None


def _is_public_video_api_model(model: Dict[str, Any]) -> bool:
    return _public_video_api_profile(model) is not None


def _is_grok15_direct_upstream_provider(provider: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(provider, dict):
        return False
    base_url = str(provider.get("baseUrl") or provider.get("base_url") or "").strip()
    if not base_url:
        return False
    host = (urlparse(base_url).hostname or "").strip().lower()
    return host == "snumom.com" or host.endswith(".snumom.com")


def _public_video_request_model_name(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    profile: Dict[str, Any],
    provider: Optional[Dict[str, Any]] = None,
) -> str:
    if _public_video_model_key(model) == "grok1.5-video" and _is_grok15_direct_upstream_provider(provider):
        upstream_model_name = str(profile.get("upstreamModelName") or "").strip()
        if upstream_model_name:
            return upstream_model_name
    return str(
        profile.get("requestModelName")
        or model.get("modelName")
        or payload.get("modelName")
        or payload.get("model")
        or model.get("id")
        or ""
    ).strip()


def _normalize_veo_reference_mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"", "text", "text_video", "none", "no_reference"}:
        return ""
    if text in {"image", "image_video", "image_ref", "omni_reference", "图生视频", "图片参考"}:
        return "image"
    if text in {"frame", "keyframe", "key_frame", "first_last_frames", "first_last_frame", "first_last", "首尾帧"}:
        return "frame"
    return text


def _normalize_newapi_video_reference_mode(model: Dict[str, Any], value: Any) -> str:
    if _is_seedance_video_model(model):
        return _normalize_seedance_reference_mode(value)
    if _is_veo_video_model(model):
        return _normalize_veo_reference_mode(value)
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"text", "text_video", "none", "no_reference"}:
        return ""
    if text in {"keyframe", "key_frame", "first_last_frames", "first_last_frame", "first_last", "首尾帧"}:
        return "first_last_frames"
    if text in {"image_video", "image_ref", "图生视频", "图片参考"}:
        return "image"
    return text


def _normalized_video_generation_mode(payload: Dict[str, Any]) -> str:
    value = _first_payload_value(payload, "mode", "generationMode", "generation_mode")
    return str(value or "").strip().lower().replace("-", "_")


def _video_reference_mode_from_payload(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    explicit = _first_payload_value(payload, "referenceMode", "reference_mode")
    mode_text = _normalized_video_generation_mode(payload)
    if _is_veo_video_model(model) and mode_text in {"image_video", "image_ref", "图生视频", "图片参考"}:
        explicit_text = str(explicit or "").strip().lower().replace("-", "_")
        if explicit is None or explicit_text in {"", "image", "image_video", "image_ref", "frame"}:
            return "frame"
    if explicit is not None and str(explicit).strip():
        return _normalize_newapi_video_reference_mode(model, explicit)
    if mode_text:
        return _normalize_newapi_video_reference_mode(model, mode_text)
    return _normalize_newapi_video_reference_mode(
        model,
        _first_model_param(model, "defaultReferenceMode", "referenceMode", "reference_mode"),
    )


def _normalize_seedance_duration(value: Any) -> int:
    try:
        parsed = float(str(value).strip())
    except Exception as error:
        raise ProviderAdapterError("Seedance 视频时长必须是 4-15 秒整数") from error
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise ProviderAdapterError("Seedance 视频时长必须是 4-15 秒整数")
    duration = int(parsed)
    if duration < 4 or duration > 15:
        raise ProviderAdapterError("Seedance 视频时长必须是 4-15 秒整数")
    return duration


SEEDANCE_FAST_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
SEEDANCE_DEFAULT_RATIO = "16:9"


def _normalize_seedance_ratio(value: Any) -> str:
    text = str(value or "").strip().replace("：", ":").replace("/", ":")
    return text if text in SEEDANCE_FAST_RATIOS else SEEDANCE_DEFAULT_RATIO


def _normalize_hermes_ratio(value: Any) -> str:
    text = str(value or "").strip().replace("：", ":").replace("/", ":").lower()
    return text if text in HERMES_SEEDANCE_RATIOS else "16:9"


def _normalize_hermes_resolution(value: Any) -> str:
    text = _newapi_video_output_resolution(value)
    return text if text in HERMES_SEEDANCE_RESOLUTIONS else "720p"


def _hermes_asset_id(value: Any) -> str:
    source = _as_text(value) or ""
    if not source:
        return ""
    parsed = urlparse(source)
    if parsed.scheme.lower() == "asset":
        candidate = (parsed.netloc or parsed.path.lstrip("/")).strip()
        if re.fullmatch(r"asset_[A-Za-z0-9_-]+", candidate):
            return candidate
    if re.fullmatch(r"asset_[A-Za-z0-9_-]+", source):
        return source
    return ""


def _hermes_content_item(kind: str, source: str) -> Dict[str, Any]:
    asset_id = _hermes_asset_id(source)
    if asset_id:
        field = f"{kind}_asset"
        return {"type": field, field: {"asset_id": asset_id}}
    field = f"{kind}_url"
    return {"type": field, field: {"url": source}}


def _normalize_video_duration_for_model(model: Dict[str, Any], value: Any) -> int:
    if _is_hermes_video_model(model):
        return 15
    if _is_seedance_video_model(model):
        return _normalize_seedance_duration(value)
    raw_supported = _first_model_param(model, "supportedDurations", "durations")
    supported: List[int] = []
    if isinstance(raw_supported, list):
        for item in raw_supported:
            try:
                parsed = int(float(str(item).strip()))
            except Exception:
                continue
            if parsed > 0 and parsed not in supported:
                supported.append(parsed)
    supported.sort()
    if not supported:
        return _normalize_seedance_duration(value)
    try:
        duration = int(float(str(value).strip()))
    except Exception:
        duration = 0
    if duration in supported:
        return duration
    try:
        default_duration = int(float(str(_first_model_param(model, "defaultDuration", "defaultSeconds") or "").strip()))
    except Exception:
        default_duration = 0
    if default_duration in supported:
        return default_duration
    return supported[0]


def _parse_video_size(value: Any) -> Optional[tuple[int, int]]:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", text)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _even_dimension(value: float) -> int:
    parsed = max(2, int(round(value)))
    return parsed if parsed % 2 == 0 else parsed + 1


def _newapi_video_size_from_ratio_resolution(ratio: Any, resolution: Any) -> str:
    explicit = _parse_video_size(resolution)
    if explicit:
        return f"{explicit[0]}x{explicit[1]}"
    ratio_text = str(ratio or "").strip().lower().replace("：", ":").replace("/", ":") or "16:9"
    ratio_map = {
        "16:9": (16, 9),
        "9:16": (9, 16),
        "1:1": (1, 1),
        "4:3": (4, 3),
        "3:4": (3, 4),
    }
    rw, rh = ratio_map.get(ratio_text, (16, 9))
    resolution_text = str(resolution or "").strip().lower()
    if "4k" in resolution_text or "2160" in resolution_text:
        short_edge = 2160
    elif "1080" in resolution_text:
        short_edge = 1080
    else:
        short_edge = 720
    if rw >= rh:
        width = _even_dimension(short_edge * rw / rh)
        height = short_edge
    else:
        width = short_edge
        height = _even_dimension(short_edge * rh / rw)
    return f"{width}x{height}"


def _newapi_video_output_resolution(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not lowered:
        return "720p"
    if lowered in {"720p", "1080p", "1k", "2k", "4k"}:
        return lowered
    if "4k" in lowered or "2160" in lowered:
        return "4k"
    if "2k" in lowered or "1440" in lowered:
        return "2k"
    if "1080" in lowered:
        return "1080p"
    if "720" in lowered:
        return "720p"
    if "1k" in lowered:
        return "1k"
    return lowered


def _normalize_seedance_ui_token(value: Any) -> str:
    text = (_as_text(value) or "").strip()
    if not text:
        return ""
    if not text.startswith("@"):
        text = f"@{text}"
    return text


def _seedance_title_token(asset: Dict[str, Any]) -> str:
    title = (_as_text(asset.get("title") or asset.get("name") or asset.get("filename")) or "").strip().lstrip("@")
    if not title:
        return ""
    text = re.sub(r"\s+", "", title)
    text = re.sub(r"[，。！？、：；\"'“”‘’()\[\]{}<>|\\/#?]+", "", text)[:24]
    return f"@{text}" if text else ""


def _seedance_reference_token_aliases(
    payload: Dict[str, Any],
    field_refs: List[tuple[str, str, str]],
) -> Dict[str, str]:
    token_lookup: Dict[tuple[str, str], List[str]] = {}

    def add_asset(asset: Any) -> None:
        if not isinstance(asset, dict):
            return
        kind = str(asset.get("kind") or asset.get("type") or "").strip().lower()
        source = _as_text(
            asset.get("assetPath")
            or asset.get("path")
            or asset.get("src")
            or asset.get("url")
            or asset.get("assetUrl")
        )
        if not kind or not source:
            return
        tokens: List[str] = []
        for key in ("token", "promptToken", "tag"):
            token = _normalize_seedance_ui_token(asset.get(key))
            if token and token not in tokens:
                tokens.append(token)
        title_token = _seedance_title_token(asset)
        if title_token and title_token not in tokens:
            tokens.append(title_token)
        if not tokens:
            return
        token_lookup.setdefault((kind, source), [])
        for token in tokens:
            if token not in token_lookup[(kind, source)]:
                token_lookup[(kind, source)].append(token)

    for asset in payload.get("referenceAssets") or []:
        add_asset(asset)
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    for asset in inputs.get("references") or []:
        add_asset(asset)

    aliases: Dict[str, str] = {}
    for kind, source, field_name in field_refs:
        for token in token_lookup.get((kind, source), []):
            aliases[token] = field_name
    return aliases


def _append_seedance_reference_tokens(prompt: str, field_names: List[str], token_aliases: Optional[Dict[str, str]] = None) -> str:
    text = str(prompt or "").strip()
    aliases = token_aliases or {}
    for token, field_name in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        field_token = f"@{field_name}"
        if token and token != field_token and token in text:
            text = text.replace(token, field_token)
    missing = [f"@{field}" for field in field_names if f"@{field}" not in text]
    if not missing:
        return text
    return " ".join([*missing, text]).strip()


def _add_limited_reference_fields(body: Dict[str, Any], prefix: str, refs: List[str], limit: int, label: str) -> List[str]:
    if len(refs) > limit:
        raise ProviderAdapterError(f"{label}最多支持 {limit} 个，当前提供了 {len(refs)} 个")
    fields: List[str] = []
    for index, ref in enumerate(refs[:limit], start=1):
        field_name = f"{prefix}_{index}"
        body[field_name] = ref
        fields.append(field_name)
    return fields


def _build_hermes_newapi_video_body(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    images: List[str],
    videos: List[str],
    audios: List[str],
) -> Dict[str, Any]:
    image_limit = _model_reference_limit(model, "maxReferenceImages", 9)
    video_limit = _model_reference_limit(model, "maxReferenceVideos", 3)
    audio_limit = _model_reference_limit(model, "maxReferenceAudios", 3)
    if len(images) > image_limit:
        raise ProviderAdapterError(f"Hermes 图片素材最多支持 {image_limit} 个，当前提供了 {len(images)} 个")
    if len(videos) > video_limit:
        raise ProviderAdapterError(f"Hermes 视频素材最多支持 {video_limit} 个，当前提供了 {len(videos)} 个")
    if len(audios) > audio_limit:
        raise ProviderAdapterError(f"Hermes 音频素材最多支持 {audio_limit} 个，当前提供了 {len(audios)} 个")

    content: List[Dict[str, Any]] = []
    field_names: List[str] = []
    field_refs: List[tuple[str, str, str]] = []
    for index, image in enumerate(images, start=1):
        field_name = f"Image{index}"
        content.append(_hermes_content_item("image", image))
        field_names.append(field_name)
        field_refs.append(("image", image, field_name))
    for index, video in enumerate(videos, start=1):
        field_name = f"Video{index}"
        content.append(_hermes_content_item("video", video))
        field_names.append(field_name)
        field_refs.append(("video", video, field_name))
    for index, audio in enumerate(audios, start=1):
        field_name = f"Audio{index}"
        content.append(_hermes_content_item("audio", audio))
        field_names.append(field_name)
        field_refs.append(("audio", audio, field_name))

    prompt = _append_seedance_reference_tokens(
        _effective_prompt(payload),
        field_names,
        _seedance_reference_token_aliases(payload, field_refs),
    )
    content.append({"type": "text", "text": prompt})

    body: Dict[str, Any] = {
        "model": _hermes_request_model_name(model, payload),
        "prompt": prompt,
        "content": content,
        "resolution": _normalize_hermes_resolution(
            _first_payload_value(payload, "resolution", "resolutionName", "resolution_name", "outputResolution", "output_resolution")
            or _first_model_param(model, "defaultResolutionName", "resolution")
            or "720p"
        ),
        "aspect_ratio": _normalize_hermes_ratio(
            _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio")
            or _first_model_param(model, "defaultRatio", "aspectRatio", "aspect_ratio")
            or "16:9"
        ),
        "duration": 15,
        "generate_audio": bool(_bool_payload_value(
            payload,
            model,
            "generateAudio",
            "generate_audio",
            "audioOn",
            "audio_on",
            default=True,
        )),
        "watermark": bool(_bool_payload_value(payload, model, "watermark", "enableWatermark", "enable_watermark", default=False)),
        "_hermes_video_json_body": True,
    }
    seed = _first_payload_value(payload, "seed", "randomSeed", "random_seed")
    if seed is not None and str(seed).strip():
        try:
            parsed_seed = int(float(str(seed).strip()))
        except Exception:
            parsed_seed = None
        if parsed_seed is not None:
            body["seed"] = parsed_seed
    return body


def _model_reference_limit(model: Dict[str, Any], key: str, default: int) -> int:
    value = _first_model_param(model, key)
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return default
    return max(0, parsed)


def _public_video_int_param(profile: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        parsed = int(float(str(profile.get(key) or "").strip()))
    except Exception:
        parsed = 0
    return parsed if parsed > 0 else default


def _public_video_positive_float_param(profile: Dict[str, Any], key: str) -> Optional[float]:
    value = profile.get(key)
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _public_video_duration(profile: Dict[str, Any], value: Any) -> int:
    supported: List[int] = []
    raw_supported = profile.get("supportedDurations") or profile.get("durations")
    if isinstance(raw_supported, list):
        for item in raw_supported:
            try:
                parsed = int(float(str(item).strip()))
            except Exception:
                continue
            if parsed > 0 and parsed not in supported:
                supported.append(parsed)
    supported.sort()
    try:
        requested = int(float(str(value).strip()))
    except Exception:
        requested = 0
    if supported:
        if requested in supported:
            return requested
        if requested > 0:
            for duration in supported:
                if duration >= requested:
                    return duration
            return supported[-1]
        default_duration = _public_video_int_param(profile, "defaultDuration")
        if default_duration in supported:
            return default_duration
        return supported[0]
    if requested > 0:
        return requested
    default_duration = _public_video_int_param(profile, "defaultDuration")
    return default_duration if default_duration > 0 else 4


def _public_video_duration_value(profile: Dict[str, Any], value: Any) -> Any:
    duration = _public_video_duration(profile, value)
    if _truthy_value(profile.get("durationAsString")):
        return str(duration)
    return duration


def _public_video_resolution(profile: Dict[str, Any], value: Any) -> str:
    requested = str(value or "").strip().lower()
    raw_supported = profile.get("supportedResolutions") or profile.get("resolutions") or profile.get("resolutionNames")
    supported = [
        str(item or "").strip().lower()
        for item in (raw_supported if isinstance(raw_supported, list) else [])
        if str(item or "").strip()
    ]
    default_resolution = str(profile.get("defaultResolutionName") or "").strip().lower()
    selected = requested
    if supported:
        if requested not in supported:
            selected = default_resolution if default_resolution in supported else supported[0]
    elif not selected:
        selected = default_resolution
    return _newapi_video_output_resolution(selected) if selected else ""


def _public_video_ratio(profile: Dict[str, Any], value: Any) -> str:
    requested = str(value or "").strip().replace("/", ":")
    raw_ratios = profile.get("ratios") if isinstance(profile.get("ratios"), list) else []
    ratios = [str(item or "").strip().replace("/", ":") for item in raw_ratios if str(item or "").strip()]
    if requested and (not ratios or requested in ratios):
        return requested
    default_ratio = str(profile.get("defaultRatio") or profile.get("defaultAspectRatio") or "").strip().replace("/", ":")
    if default_ratio and (not ratios or default_ratio in ratios):
        return default_ratio
    return ratios[0] if ratios else "16:9"


def _public_video_size(profile: Dict[str, Any], payload: Dict[str, Any], ratio: str) -> str:
    raw_sizes = profile.get("sizes") or profile.get("supportedSizes") or profile.get("supported_sizes")
    allowed_sizes = [
        f"{parsed[0]}x{parsed[1]}"
        for parsed in (_parse_video_size(item) for item in (raw_sizes if isinstance(raw_sizes, list) else []))
        if parsed
    ]

    def normalize_size(value: Any) -> str:
        parsed = _parse_video_size(value)
        if not parsed:
            return ""
        candidate = f"{parsed[0]}x{parsed[1]}"
        if allowed_sizes and candidate not in allowed_sizes:
            return ""
        return candidate

    for key in ("size", "videoSize", "video_size", "resolution", "outputResolution", "output_resolution"):
        size = normalize_size(_first_payload_value(payload, key))
        if size:
            return size
    ratio_key = str(ratio or "").strip().replace("/", ":")
    ratio_map = profile.get("ratioSizeMap")
    if isinstance(ratio_map, dict):
        mapped = normalize_size(ratio_map.get(ratio_key))
        if mapped:
            return mapped
    default_size = normalize_size(profile.get("defaultSize"))
    if default_size:
        return default_size
    return allowed_sizes[0] if allowed_sizes else ""


SEEDANCE_DASH_PRO_MODEL_IDS = {
    "seedance-2-0-pro",
    "seedance-2-0-fast",
    "seedance-2-0(火山官转)",
    "seedance-2-0(火山版本)",
    "seedance-2-0-pro(火山版本)",
    "seedance-2-0-fast(火山官转)",
    "seedance-2-0-fast(火山版本)",
}
SEEDANCE_DASH_FAST_MODEL_IDS = {
    "seedance-2-0-fast",
    "seedance-2-0-fast(火山官转)",
    "seedance-2-0-fast(火山版本)",
}
SEEDANCE_PORTRAIT_PROMPT_ALIASES_KEY = "_seedancePortraitPromptAliases"


def _seedance_dash_identity(value: Any) -> str:
    return str(value or "").strip().lower().replace("（", "(").replace("）", ")")


def _is_seedance_dash_pro_model(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> bool:
    values = [
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
    ]
    if isinstance(payload, dict):
        values.extend([payload.get("modelId"), payload.get("providerModelId"), payload.get("modelName"), payload.get("model")])
    normalized = {_seedance_dash_identity(value) for value in values if str(value or "").strip()}
    return bool(normalized & SEEDANCE_DASH_PRO_MODEL_IDS) or any(
        _seedance_dash_huoshan_guanzhuan_profile_key(value) in PUBLIC_VIDEO_API_PROFILES
        for value in values
        if str(value or "").strip()
    )


def _is_seedance_dash_fast_model(model: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> bool:
    values = [
        model.get("id"),
        model.get("modelName"),
        model.get("displayName"),
    ]
    if isinstance(payload, dict):
        values.extend([payload.get("modelId"), payload.get("providerModelId"), payload.get("modelName"), payload.get("model")])
    normalized = {_seedance_dash_identity(value) for value in values if str(value or "").strip()}
    return bool(normalized & SEEDANCE_DASH_FAST_MODEL_IDS) or any(
        _seedance_dash_huoshan_guanzhuan_profile_key(value) == "seedance-2-0-fast(火山官转)"
        for value in values
        if str(value or "").strip()
    )


def _seedance_dash_upstream_mode_options(model: Dict[str, Any], payload: Dict[str, Any], resolution: Any) -> List[str]:
    if not _is_seedance_dash_pro_model(model, payload):
        return []
    resolution_text = _newapi_video_output_resolution(resolution).lower()
    if resolution_text == "480p":
        return ["pro"]
    if resolution_text == "1080p" and _is_seedance_dash_fast_model(model, payload):
        return ["std"]
    return ["std", "pro"]


def _normalize_seedance_dash_upstream_mode(model: Dict[str, Any], payload: Dict[str, Any], resolution: Any) -> str:
    options = _seedance_dash_upstream_mode_options(model, payload, resolution)
    if not options:
        return ""
    raw = _first_payload_value(
        payload,
        "seedanceMode",
        "seedance_mode",
        "officialMode",
        "official_mode",
        "volcMode",
        "volc_mode",
        "upstreamMode",
        "upstream_mode",
    )
    text = str(raw or "").strip().lower()
    aliases = {
        "standard": "std",
        "normal": "std",
        "default": "std",
        "标准": "std",
        "标清": "std",
        "professional": "pro",
        "premium": "pro",
        "专业": "pro",
    }
    normalized = aliases.get(text, text)
    if normalized in options:
        return normalized
    return options[0]


def _seedance_portrait_asset_public_url(asset: Dict[str, Any]) -> str:
    for key in ("url", "assetUrl", "asset_url", "publicUrl", "public_url", "src"):
        value = _as_text(asset.get(key)) or ""
        if re.match(r"^https?://", value, re.I):
            return value
    return ""


def _seedance_portrait_asset_items(
    payload: Dict[str, Any],
    *,
    prefer_public_url: bool = False,
) -> List[Dict[str, Any]]:
    raw_assets = payload.get("seedancePortraitAssets") or payload.get("seedance_portrait_assets") or []
    if isinstance(raw_assets, dict):
        raw_assets = [raw_assets]
    items: List[Dict[str, Any]] = []
    items_by_ref: Dict[str, Dict[str, Any]] = {}
    for asset in raw_assets if isinstance(raw_assets, list) else []:
        tokens: List[str] = []
        if isinstance(asset, str):
            ref = asset.strip()
        elif isinstance(asset, dict):
            public_url = _seedance_portrait_asset_public_url(asset)
            asset_ref = _as_text(asset.get("assetRef") or asset.get("asset_ref") or "")
            ref = public_url if prefer_public_url and public_url else asset_ref
            if not ref:
                asset_id = _as_text(asset.get("assetId") or asset.get("asset_id") or asset.get("id") or "")
                ref = f"asset://{asset_id}" if asset_id else ""
            for key in ("token", "promptToken", "tag"):
                token = _normalize_seedance_ui_token(asset.get(key))
                if token and token not in tokens:
                    tokens.append(token)
            title_token = _seedance_title_token(asset)
            if title_token and title_token not in tokens:
                tokens.append(title_token)
        else:
            ref = ""
        if not ref:
            continue
        if not re.match(r"^(asset://|https?://|data:image/)", ref, re.I):
            ref = f"asset://{ref}"
        item = items_by_ref.get(ref)
        if item is None:
            item = {"ref": ref, "tokens": []}
            items_by_ref[ref] = item
            items.append(item)
        for token in tokens:
            if token not in item["tokens"]:
                item["tokens"].append(token)
    return items


def _seedance_portrait_asset_refs(payload: Dict[str, Any], *, prefer_public_url: bool = False) -> List[str]:
    return [
        item["ref"]
        for item in _seedance_portrait_asset_items(payload, prefer_public_url=prefer_public_url)
        if item.get("ref")
    ]


def _merge_seedance_portrait_assets_into_images(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    images: List[str],
    *,
    require_seedance_dash: bool = True,
    prefer_public_url: bool = False,
) -> tuple[List[str], Dict[str, str]]:
    merged = list(images)
    prompt_aliases: Dict[str, str] = {}
    if require_seedance_dash and not _is_seedance_dash_pro_model(model, payload):
        return merged, prompt_aliases

    for item in _seedance_portrait_asset_items(payload, prefer_public_url=prefer_public_url):
        ref = _as_text(item.get("ref"))
        if not ref:
            continue
        if ref in merged:
            image_index = merged.index(ref) + 1
        else:
            merged.append(ref)
            image_index = len(merged)
        replacement = f"@图片{image_index}"
        for token in item.get("tokens") or []:
            if token and token != replacement and token not in prompt_aliases:
                prompt_aliases[token] = replacement
    return merged, prompt_aliases


def _apply_seedance_portrait_prompt_aliases(prompt: str, payload: Dict[str, Any]) -> str:
    text = str(prompt or "").strip()
    aliases = payload.get(SEEDANCE_PORTRAIT_PROMPT_ALIASES_KEY)
    if not isinstance(aliases, dict) or not aliases:
        return text
    for token, replacement in sorted(aliases.items(), key=lambda item: len(str(item[0])), reverse=True):
        source = str(token or "")
        target = str(replacement or "")
        if source and target and source != target and source in text:
            text = text.replace(source, target)
    return text


def _append_seedance_portrait_refs(prompt: str, refs: List[str]) -> str:
    text = str(prompt or "").strip()
    missing = [ref for ref in refs if ref and ref not in text]
    if not missing:
        return text
    prefix = "角色参考：" + " ".join(missing)
    return "\n".join([prefix, text]).strip()


def _is_remote_reference_url(value: Any) -> bool:
    source = _as_text(value) or ""
    return bool(re.match(r"^https?://", source, re.I)) and not _is_local_http_reference(source)


def _set_public_json_image_reference(body: Dict[str, Any], field_name: str, value: Any) -> None:
    body[field_name] = value
    fields = body.setdefault("_json_image_reference_fields", [])
    if field_name not in fields:
        fields.append(field_name)


def _set_public_json_video_reference(body: Dict[str, Any], field_name: str, value: Any) -> None:
    body[field_name] = value
    fields = body.setdefault("_json_video_reference_fields", [])
    if field_name not in fields:
        fields.append(field_name)


def _set_public_json_audio_reference(body: Dict[str, Any], field_name: str, value: Any) -> None:
    body[field_name] = value
    fields = body.setdefault("_json_audio_reference_fields", [])
    if field_name not in fields:
        fields.append(field_name)


def _build_public_newapi_video_body(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    duration: Any,
    reference_mode: str,
    images: List[str],
    videos: List[str],
    audios: List[str],
    provider: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    profile = _public_video_api_profile(model) or {}
    model_name = _public_video_request_model_name(model, payload, profile, provider)

    ratio = _public_video_ratio(profile, _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio"))
    resolution = (
        _first_payload_value(payload, "resolution", "outputResolution", "output_resolution")
        or profile.get("defaultResolutionName")
    )
    resolution = _public_video_resolution(profile, resolution)
    ratio_field = str(profile.get("ratioField") or "aspect_ratio").strip() or "aspect_ratio"
    prompt = _effective_prompt(payload)
    if _is_seedance_dash_pro_model(model, payload) or payload.get(SEEDANCE_PORTRAIT_PROMPT_ALIASES_KEY):
        prompt = _apply_seedance_portrait_prompt_aliases(prompt, payload)
    if _truthy_value(profile.get("requiresImageReference")) and not images:
        raise ProviderAdapterError(f"{model_name or 'Video model'} requires one image reference")
    body: Dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
    }
    audio_max_duration = _public_video_positive_float_param(profile, "audioReferenceMaxDurationSeconds")
    if audio_max_duration is not None:
        body["_json_audio_reference_max_duration_seconds"] = audio_max_duration
    async_default = profile.get("defaultAsync")
    if async_default is not None:
        body["async"] = not _falsey_value(async_default)
    seedance_mode = _normalize_seedance_dash_upstream_mode(model, payload, resolution)
    if seedance_mode:
        body["mode"] = seedance_mode
    if not _falsey_value(profile.get("includeDuration", True)):
        duration_field = str(profile.get("durationField") or "duration").strip() or "duration"
        body[duration_field] = _public_video_duration_value(profile, duration)
    if not _falsey_value(profile.get("includeRatio", True)):
        body[ratio_field] = ratio
    size_field = str(profile.get("sizeField") or "").strip()
    if size_field or _truthy_value(profile.get("includeSize")):
        size = _public_video_size(profile, payload, ratio)
        if size:
            body[size_field or "size"] = size
    # VolcTokens Seedance pricing depends on resolution. Remote model sync may
    # mark includeResolution=false, but omitting it makes NewAPI bill the
    # default 720p tier even when the user selected 480p or 1080p.
    if resolution and (
        _is_seedance_dash_pro_model(model, payload)
        or (
            not _falsey_value(profile.get("includeResolution", True))
            and not (size_field or _truthy_value(profile.get("includeSize")))
        )
    ):
        body["resolution"] = resolution
    if not _falsey_value(profile.get("includeGenerateAudio", True)):
        profile_model = {**model, "params": profile}
        generate_audio = _bool_payload_value(
            payload,
            profile_model,
            "generateAudio",
            "generate_audio",
            "audioOn",
            "audio_on",
            default=bool(profile.get("defaultGenerateAudio", True)),
        )
        if generate_audio is not None:
            body["generate_audio"] = bool(generate_audio)
    if _truthy_value(profile.get("includeSeed")):
        seed = _first_payload_value(payload, "seed", "randomSeed", "random_seed")
        if seed is not None and str(seed).strip() != "":
            try:
                parsed_seed = float(str(seed).strip())
            except Exception as error:
                raise ProviderAdapterError("seed 必须是整数") from error
            if not math.isfinite(parsed_seed) or not parsed_seed.is_integer():
                raise ProviderAdapterError("seed 必须是整数")
            body["seed"] = int(parsed_seed)

    preset_field = str(profile.get("presetField") or "").strip()
    if preset_field or _truthy_value(profile.get("includePreset")):
        preset = _first_payload_value(payload, "preset", "stylePreset", "style_preset") or profile.get("defaultPreset")
        if preset is not None and str(preset).strip():
            body[preset_field or "preset"] = str(preset).strip()

    supports_start_end = bool(profile.get("supportsStartEndFrames"))
    supports_video_reference = bool(profile.get("supportsVideoReference"))
    use_start_end = reference_mode == "first_last_frames"
    if use_start_end and _truthy_value(profile.get("firstLastAsImageArray")):
        use_start_end = False
    if use_start_end:
        if not supports_start_end:
            raise ProviderAdapterError(f"{model_name or 'Video model'} does not support start/end frames")
        if len(images) != 2:
            raise ProviderAdapterError("Start/end frame mode requires exactly 2 image references")
        _set_public_json_image_reference(body, "start_frame", images[0])
        _set_public_json_image_reference(body, "end_frame", images[1])
    else:
        max_images = _public_video_int_param(profile, "maxReferenceImages", 1)
        if max_images > 0 and len(images) > max_images:
            raise ProviderAdapterError(f"{model_name or 'Video model'} supports at most {max_images} image references")
        image_reference_field = str(
            profile.get("imageReferenceField")
            or profile.get("referenceImageField")
            or ""
        ).strip()
        single_image_field = str(profile.get("singleImageReferenceField") or "").strip()
        multi_image_field = str(profile.get("multiImageReferenceField") or "").strip()
        if (single_image_field or multi_image_field) and images:
            if len(images) == 1 and single_image_field:
                _set_public_json_image_reference(body, single_image_field, images[0])
            elif multi_image_field:
                _set_public_json_image_reference(body, multi_image_field, images)
            elif single_image_field:
                raise ProviderAdapterError(f"{model_name or 'Video model'} supports only one image reference")
        elif image_reference_field == "input_reference" and images:
            _set_public_json_image_reference(body, "input_reference", images[0] if len(images) == 1 else images)
        elif image_reference_field in {"image_file", "image_file_*"}:
            _add_limited_reference_fields(body, "image_file", images, max_images, "图片素材")
        else:
            remote_images = [item for item in images if _is_remote_reference_url(item)]
            json_images = [item for item in images if not _is_remote_reference_url(item)]
            if remote_images:
                body["image_urls"] = remote_images
            if json_images:
                _set_public_json_image_reference(body, "input_reference", json_images[0] if len(json_images) == 1 else json_images)

    if videos:
        if not supports_video_reference:
            raise ProviderAdapterError(f"{model_name or 'Video model'} does not support video reference assets")
        max_videos = _public_video_int_param(profile, "maxReferenceVideos", 1)
        if max_videos > 0 and len(videos) > max_videos:
            raise ProviderAdapterError(f"{model_name or 'Video model'} supports at most {max_videos} video references")
        video_reference_field = str(profile.get("videoReferenceField") or "video_reference").strip() or "video_reference"
        video_items = videos[:max_videos or len(videos)]
        video_value = video_items if profile.get("videoReferenceAsList") else (video_items[0] if len(video_items) == 1 else video_items)
        _set_public_json_video_reference(body, video_reference_field, video_value)
    if audios:
        max_audios = _public_video_int_param(profile, "maxReferenceAudios", 0)
        if max_audios <= 0:
            raise ProviderAdapterError(f"{model_name or 'Video model'} does not support audio reference assets")
        if len(audios) > max_audios:
            raise ProviderAdapterError(f"{model_name or 'Video model'} supports at most {max_audios} audio references")
        audio_reference_field = str(profile.get("audioReferenceField") or "").strip()
        if audio_reference_field:
            audio_items = audios[:max_audios or len(audios)]
            audio_value = audio_items if profile.get("audioReferenceAsList") else (audio_items[0] if len(audio_items) == 1 else audio_items)
            _set_public_json_audio_reference(body, audio_reference_field, audio_value)
        else:
            audio_fields = _add_limited_reference_fields(body, "audio_file", audios, max_audios, "音频素材")
            if _is_seedance_dash_pro_model(model, payload):
                body["_json_audio_data_url_fields"] = audio_fields
    return body


def _normalize_muse_duration(value: Any) -> int:
    try:
        parsed = float(str(value).strip())
    except Exception as error:
        raise ProviderAdapterError("Muse 视频时长必须是 4-15 秒整数") from error
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise ProviderAdapterError("Muse 视频时长必须是 4-15 秒整数")
    duration = int(parsed)
    if duration < 4 or duration > 15:
        raise ProviderAdapterError("Muse 视频时长必须是 4-15 秒整数")
    return duration


def _normalize_muse_ratio(value: Any) -> str:
    text = str(value or "").strip().replace("：", ":").replace("/", ":")
    return text if text in SEEDANCE_FAST_RATIOS else SEEDANCE_DEFAULT_RATIO


def _muse_request_model_name(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    return str(model.get("modelName") or payload.get("modelName") or payload.get("model") or model.get("id") or "").strip()


def _channel22_por_request_model_name(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    payload_tokens = {
        str(value or "").strip().lower()
        for value in (payload.get("modelName"), payload.get("model"))
        if str(value or "").strip()
    }
    tokens = _muse_video_model_tokens(model) | payload_tokens
    if tokens & MUSE_CHANNEL22_FAST_POR_MODEL_KEYS:
        return CHANNEL22_FAST_MODEL_NAME
    if tokens & MUSE_CHANNEL22_PRO_POR_MODEL_KEYS:
        return CHANNEL22_PRO_MODEL_NAME
    return _muse_request_model_name(model, payload)


def _muse_media_tag(label: str, index: int) -> str:
    return f"{label}{index}"


def _append_missing_muse_tags(prompt: str, tags: List[str]) -> str:
    text = str(prompt or "").strip()
    missing = [f"@{tag}" for tag in tags if tag and f"@{tag}" not in text]
    return " ".join([*missing, text]).strip()


def _channel22_por_prompt(prompt: str, image_count: int, audio_count: int) -> str:
    text = str(prompt or "").strip()
    text = re.sub(r"@图片\s*(\d+)", lambda match: f"@[image{match.group(1)}]", text)
    image_tags = [f"[image{index}]" for index in range(1, image_count + 1)]
    audio_tags = [f"音频{index}" for index in range(1, audio_count + 1)]
    return _append_missing_muse_tags(text, [*image_tags, *audio_tags])


def _validate_channel22_por_prompt_length(prompt: str) -> None:
    count = len(str(prompt or ""))
    if count > MUSE_CHANNEL22_PROMPT_CHAR_LIMIT:
        raise ProviderAdapterError(
            f"Muse 渠道22视频提示词最多支持 {MUSE_CHANNEL22_PROMPT_CHAR_LIMIT} 字，当前 {count} 字"
        )


def _muse_tagged_media_items(label: str, refs: List[str], limit: int) -> List[Dict[str, str]]:
    if len(refs) > limit:
        raise ProviderAdapterError(f"Muse 视频{label}素材最多支持 {limit} 个，当前提供了 {len(refs)} 个")
    return [
        {"tag": _muse_media_tag(label, index), "url": ref}
        for index, ref in enumerate(refs, start=1)
        if _as_text(ref)
    ]


def _build_channel22_por_video_body(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    duration: Any,
    images: List[str],
    videos: List[str],
    audios: List[str],
) -> Dict[str, Any]:
    max_images = _model_reference_limit(model, "maxReferenceImages", 9)
    max_audios = _model_reference_limit(model, "maxReferenceAudios", 3)
    max_videos = _model_reference_limit(model, "maxReferenceVideos", 0)
    if len(images) > max_images:
        raise ProviderAdapterError(f"Muse 视频图片素材最多支持 {max_images} 个，当前提供了 {len(images)} 个")
    if len(audios) > max_audios:
        raise ProviderAdapterError(f"Muse 视频音频素材最多支持 {max_audios} 个，当前提供了 {len(audios)} 个")
    if len(videos) > max_videos:
        raise ProviderAdapterError(f"Muse 视频视频素材最多支持 {max_videos} 个，当前提供了 {len(videos)} 个")
    prompt = _channel22_por_prompt(_effective_prompt(payload), len(images), len(audios))
    _validate_channel22_por_prompt_length(prompt)
    ratio = _normalize_muse_ratio(_first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio") or SEEDANCE_DEFAULT_RATIO)
    body: Dict[str, Any] = {
        "model": _channel22_por_request_model_name(model, payload),
        "prompt": prompt,
        "duration": _normalize_muse_duration(duration),
        "ratio": ratio,
    }
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    resolution_profile = {
        **params,
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
    }
    resolution = _public_video_resolution(
        resolution_profile,
        _first_payload_value(payload, "resolution", "resolutionName", "resolution_name")
        or _first_model_param(model, "defaultResolutionName", "resolutionName", "resolution_name"),
    )
    if resolution:
        body["resolution"] = resolution
    if images:
        body["referenceImages"] = images
        body["_json_image_reference_fields"] = ["referenceImages"]
    if audios:
        body["referenceAudio"] = audios
        body["_json_audio_reference_fields"] = ["referenceAudio"]
    return body


def _muse_v1_image_role(reference_mode: str, index: int, total: int) -> str:
    if reference_mode == "first_last_frames":
        if total == 2:
            return "first_frame" if index == 0 else "last_frame"
        return "first_frame" if total == 1 else "reference_image"
    if reference_mode in {"reference_image", "omni_reference", "image"}:
        return "reference_image"
    if total > 1:
        return "reference_image"
    return "first_frame" if index == 0 else "reference_image"


def _build_muse_newapi_video_body(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    duration: Any,
    reference_mode: str,
    images: List[str],
    videos: List[str],
    audios: List[str],
) -> Dict[str, Any]:
    if _is_muse_channel22_por_model(model):
        return _build_channel22_por_video_body(
            model,
            payload,
            duration=duration,
            images=images,
            videos=videos,
            audios=audios,
        )
    api_version = _muse_video_api_version(model)
    prompt = _effective_prompt(payload)
    ratio = _normalize_muse_ratio(_first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio") or SEEDANCE_DEFAULT_RATIO)
    body: Dict[str, Any] = {
        "model": _muse_request_model_name(model, payload),
        "ratio": ratio,
        "duration": _normalize_muse_duration(duration),
        "_muse_video_json_body": True,
        "_muse_video_api_version": api_version,
    }
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    resolution = _public_video_resolution(
        params,
        _first_payload_value(payload, "resolution", "resolutionName", "resolution_name")
        or _first_model_param(model, "defaultResolutionName", "resolutionName", "resolution_name"),
    )
    if resolution:
        body["resolution"] = resolution
    if api_version == "v2":
        image_items = _muse_tagged_media_items("图片", images, _model_reference_limit(model, "maxReferenceImages", 9))
        audio_items = _muse_tagged_media_items("音频", audios, _model_reference_limit(model, "maxReferenceAudios", 3))
        video_items = _muse_tagged_media_items("视频", videos, _model_reference_limit(model, "maxReferenceVideos", 3))
        if _is_muse_channel22_por_model(model):
            body["prompt"] = _channel22_por_prompt(prompt, len(image_items), len(audio_items))
        else:
            body["prompt"] = _append_missing_muse_tags(
                prompt,
                [item["tag"] for item in [*image_items, *audio_items, *video_items]],
            )
        body["images"] = image_items
        if audio_items:
            body["audios"] = audio_items
        if video_items:
            body["videos"] = video_items
        return body

    if videos:
        raise ProviderAdapterError("Muse v1 视频接口当前不支持视频素材，请使用真人/企业真人模型")
    if len(images) > 9:
        raise ProviderAdapterError(f"Muse 视频图片素材最多支持 9 个，当前提供了 {len(images)} 个")
    if len(audios) > 3:
        raise ProviderAdapterError(f"Muse 视频音频素材最多支持 3 个，当前提供了 {len(audios)} 个")
    contents: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for index, image in enumerate(images):
        contents.append({
            "type": "image_url",
            "imageUrl": image,
            "role": _muse_v1_image_role(reference_mode, index, len(images)),
        })
    for audio in audios:
        contents.append({"type": "audio_url", "audioUrl": audio})
    body["contents"] = contents
    return body


def _build_newapi_video_body(
    model: Dict[str, Any],
    payload: Dict[str, Any],
    provider: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    duration = (
        _first_payload_value(payload, "duration", "durationSeconds", "duration_seconds")
        or _first_model_param(model, "defaultDuration", "duration")
        or 5
    )
    reference_mode = _video_reference_mode_from_payload(model, payload)
    images = _video_reference_inputs(payload)
    if _is_seedance_dash_pro_model(model, payload):
        images, portrait_prompt_aliases = _merge_seedance_portrait_assets_into_images(model, payload, images)
        if portrait_prompt_aliases:
            payload = {**payload, SEEDANCE_PORTRAIT_PROMPT_ALIASES_KEY: portrait_prompt_aliases}
    elif _is_public_video_api_model(model) and _seedance_portrait_asset_refs(payload):
        images, portrait_prompt_aliases = _merge_seedance_portrait_assets_into_images(
            model,
            payload,
            images,
            require_seedance_dash=False,
        )
        if portrait_prompt_aliases:
            payload = {**payload, SEEDANCE_PORTRAIT_PROMPT_ALIASES_KEY: portrait_prompt_aliases}
    videos = _video_file_reference_inputs(payload)
    audios = _audio_file_reference_inputs(payload)
    if _is_hermes_video_model(model, payload):
        return _build_hermes_newapi_video_body(
            model,
            payload,
            images=images,
            videos=videos,
            audios=audios,
        )
    if _is_muse_video_model(model):
        return _build_muse_newapi_video_body(
            model,
            payload,
            duration=duration,
            reference_mode=reference_mode,
            images=images,
            videos=videos,
            audios=audios,
        )
    if _is_public_video_api_model(model):
        return _build_public_newapi_video_body(
            model,
            payload,
            duration=duration,
            reference_mode=reference_mode,
            images=images,
            videos=videos,
            audios=audios,
            provider=provider,
        )
    prompt = _effective_prompt(payload)
    if _is_seedance_dash_pro_model(model, payload):
        prompt = _apply_seedance_portrait_prompt_aliases(prompt, payload)
    body: Dict[str, Any] = {
        "model": _seedance_request_model_name(model, payload) if _is_seedance_video_model(model) else str(model.get("modelName") or payload.get("modelName") or payload.get("model") or "").strip(),
        "prompt": prompt,
        "duration": _normalize_video_duration_for_model(model, duration),
        "ratio": _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio") or SEEDANCE_DEFAULT_RATIO,
    }
    if _is_mgdb_nine_image_special_model(model, payload) and not payload.get("_mgdbNineImageReferencesLetterboxed"):
        body["_mgdb_nine_image_letterbox_ratio"] = body["ratio"] or SEEDANCE_DEFAULT_RATIO
    if _is_seedance_video_model(model):
        body["ratio"] = _normalize_seedance_ratio(body.get("ratio"))
        body["resolution"] = _newapi_video_output_resolution(
            _first_payload_value(payload, "resolution", "outputResolution", "output_resolution")
            or _first_model_param(model, "defaultResolutionName", "resolution")
            or "720p"
        )
    if _is_veo_video_model(model):
        if videos or audios:
            raise ProviderAdapterError("Veo 图生视频只支持图片素材")
        if len(images) > 1:
            raise ProviderAdapterError("当前 NewAPI VEO 通道只支持 1 张起始帧；首尾帧和多参考图暂未接入")
        if images:
            ratio = body.pop("ratio", None)
            duration_value = body.pop("duration", None)
            resolution = _first_payload_value(payload, "resolution", "outputResolution", "output_resolution")
            body["task_type"] = "video_generation"
            body["n"] = 1
            body["aspect_ratio"] = str(ratio or "16:9")
            body["output_resolution"] = _newapi_video_output_resolution(resolution)
            body["duration"] = duration_value
            body["reference_mode"] = reference_mode or "frame"
            body["generate_audio"] = bool(_bool_payload_value(
                payload,
                model,
                "generateAudio",
                "generate_audio",
                "audioOn",
                "audio_on",
                default=True,
            ))
            body["response_format"] = "url"
            body["image"] = images[0]
        return body
    if reference_mode:
        body["reference_mode"] = reference_mode
    field_names: List[str] = []
    field_refs: List[tuple[str, str, str]] = []
    if reference_mode == "first_last_frames":
        if len(images) != 2:
            raise ProviderAdapterError("Seedance 首尾帧模式必须正好提供 2 张图片")
        if videos or audios:
            raise ProviderAdapterError("Seedance 首尾帧模式只支持图片素材")
        body["first_frame_image"] = images[0]
        body["last_frame_image"] = images[1]
        field_names.extend(["first_frame_image", "last_frame_image"])
        field_refs.extend([
            ("image", images[0], "first_frame_image"),
            ("image", images[1], "last_frame_image"),
        ])
    else:
        image_fields = _add_limited_reference_fields(body, "image_file", images, _model_reference_limit(model, "maxReferenceImages", 9), "图片素材")
        video_fields = _add_limited_reference_fields(body, "video_file", videos, _model_reference_limit(model, "maxReferenceVideos", 3), "视频素材")
        audio_fields = _add_limited_reference_fields(body, "audio_file", audios, _model_reference_limit(model, "maxReferenceAudios", 3), "音频素材")
        field_names.extend(image_fields)
        field_names.extend(video_fields)
        field_names.extend(audio_fields)
        field_refs.extend(("image", ref, field_name) for ref, field_name in zip(images, image_fields))
        field_refs.extend(("video", ref, field_name) for ref, field_name in zip(videos, video_fields))
        field_refs.extend(("audio", ref, field_name) for ref, field_name in zip(audios, audio_fields))
    body["prompt"] = _append_seedance_reference_tokens(
        body["prompt"],
        field_names,
        _seedance_reference_token_aliases(payload, field_refs),
    )
    return body


def _firefly_request_model_name(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    raw_model_name = str(model.get("modelName") or payload.get("modelName") or payload.get("model") or model.get("id") or "").strip()
    base = raw_model_name.lower()
    variant = str(
        _first_payload_value(payload, "variant", "modelVariant", "model_variant", "quality")
        or _first_model_param(model, "defaultVariant", "variant")
        or ""
    ).strip().lower()
    if base in {"firefly-sora2", "firefly-sora2-pro"}:
        if base == "firefly-sora2-pro" or variant in {"pro", "professional"}:
            return "firefly-sora2-pro"
        return "firefly-sora2"
    if base in {"firefly-veo31", "firefly-veo31-fast", "firefly-veo31-ref"}:
        if base in {"firefly-veo31-fast", "firefly-veo31-ref"}:
            return base
        if variant in {"fast", "快速"}:
            return "firefly-veo31-fast"
        if variant in {"ref", "reference", "reference-only", "参考"}:
            return "firefly-veo31-ref"
        return "firefly-veo31"
    return raw_model_name


def _firefly_effective_profile(model: Dict[str, Any], request_model: str) -> Dict[str, Any]:
    base_profile = _firefly_video_profile(model)
    request_profile = FIREFLY_VIDEO_API_PROFILES.get(str(request_model or "").strip().lower())
    if request_profile:
        return {**base_profile, **request_profile}
    return base_profile


async def _build_firefly_video_chat_body(model: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    request_model = _firefly_request_model_name(model, payload)
    profile = _firefly_effective_profile(model, request_model)
    duration = (
        _first_payload_value(payload, "duration", "durationSeconds", "duration_seconds")
        or profile.get("defaultDuration")
        or 4
    )
    ratio = _public_video_ratio(profile, _first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio"))
    resolution = (
        _first_payload_value(payload, "resolution", "outputResolution", "output_resolution")
        or profile.get("defaultResolutionName")
    )
    images = _video_reference_inputs(payload)
    videos = _video_file_reference_inputs(payload)
    audios = _audio_file_reference_inputs(payload)
    if videos or audios:
        raise ProviderAdapterError("Firefly 视频接口只支持图片参考素材")

    reference_mode = _video_reference_mode_from_payload(model, payload)
    if not reference_mode and images:
        reference_mode = "image"
    max_images = _public_video_int_param(profile, "maxReferenceImages", 1)
    if reference_mode == "first_last_frames":
        if not bool(profile.get("supportsStartEndFrames")):
            raise ProviderAdapterError(f"{request_model or 'Firefly 视频模型'} 不支持首尾帧")
        if len(images) != 2:
            raise ProviderAdapterError("Firefly 首尾帧模式必须正好提供 2 张图片")
    elif max_images > 0 and len(images) > max_images:
        raise ProviderAdapterError(f"{request_model or 'Firefly 视频模型'} 最多支持 {max_images} 张参考图，当前提供了 {len(images)} 张")

    content: List[Dict[str, Any]] = []
    for index, image in enumerate(images[:max_images or len(images)]):
        content.append({
            "type": "image_url",
            "image_url": {
                "url": await _normalize_json_image_reference(str(image), payload, index),
            },
        })
    content.append({"type": "text", "text": _effective_prompt(payload)})

    body: Dict[str, Any] = {
        "model": request_model,
        "messages": [{"role": "user", "content": content}],
        "stream": True,
        "duration": _public_video_duration(profile, duration),
        "aspect_ratio": ratio,
    }
    if resolution and str(resolution).strip() != "-":
        body["resolution"] = _newapi_video_output_resolution(resolution)
    return body


GROK2API_VIDEO_SECONDS = (10,)
GROK2API_VIDEO_SIZES = {"720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"}
GROK2API_VIDEO_PRESETS = {"fun", "normal", "spicy", "custom"}


def _grok2api_video_variant_defaults(model_name: str) -> Dict[str, Any]:
    raw_model_name = str(model_name or "").strip()
    text = raw_model_name.lower()
    defaults: Dict[str, Any] = {}
    if text.startswith("grok-imagine-1.0-video-") or text.startswith("grok-imagine-video-square-"):
        defaults["model"] = "grok-imagine-video"
        match = re.search(r"-(\d+)s(?:$|\b)", text)
        if match:
            defaults["seconds"] = int(match.group(1))
        is_hd = "[hd]" in text
        if "portrait" in text:
            defaults["size"] = "1024x1792" if is_hd else "720x1280"
        elif "landscape" in text:
            defaults["size"] = "1792x1024" if is_hd else "1280x720"
        elif "square" in text:
            defaults["size"] = "1024x1024"
    return defaults


def _normalize_grok2api_video_seconds(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return GROK2API_VIDEO_SECONDS[0]
    if parsed in GROK2API_VIDEO_SECONDS:
        return parsed
    return min(GROK2API_VIDEO_SECONDS, key=lambda item: (abs(item - parsed), item < parsed))


def _grok2api_payload_ratio(payload: Dict[str, Any]) -> str:
    text = str(_first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio") or "").strip()
    if text in {"16:9", "1.7777777778"}:
        return "16:9"
    if text in {"9:16", "0.5625"}:
        return "9:16"
    if text in {"1:1", "1"}:
        return "1:1"
    return text


def _grok2api_video_variant_specs(model: Dict[str, Any]) -> List[Dict[str, Any]]:
    specs = _first_model_param(model, "grokVariantSpecs")
    if not isinstance(specs, list):
        return []
    clean: List[Dict[str, Any]] = []
    for item in specs:
        if not isinstance(item, dict):
            continue
        model_name = str(item.get("model") or item.get("modelName") or "").strip()
        ratio = str(item.get("ratio") or "").strip()
        size = str(item.get("size") or "").strip()
        try:
            seconds = int(str(item.get("seconds") or item.get("duration") or "").strip())
        except Exception:
            continue
        if model_name and ratio and size in GROK2API_VIDEO_SIZES and seconds in GROK2API_VIDEO_SECONDS:
            clean.append({**item, "model": model_name, "ratio": ratio, "size": size, "seconds": seconds})
    return clean


def _select_grok2api_video_variant(model: Dict[str, Any], payload: Dict[str, Any], requested_seconds: Any) -> Optional[Dict[str, Any]]:
    specs = _grok2api_video_variant_specs(model)
    if not specs:
        return None
    ratio = _grok2api_payload_ratio(payload)
    seconds = _normalize_grok2api_video_seconds(requested_seconds)
    candidates = [item for item in specs if not ratio or item.get("ratio") == ratio] or specs
    exact = [item for item in candidates if item.get("seconds") == seconds]
    if exact:
        candidates = exact
    else:
        candidates = sorted(candidates, key=lambda item: (abs(int(item.get("seconds") or 0) - seconds), int(item.get("seconds") or 0) < seconds))
    resolution = str(_first_payload_value(payload, "resolution", "resolutionName", "resolution_name") or "").strip().lower()
    if resolution in {"hd", "high", "1080p"}:
        preferred = next((item for item in candidates if str(item.get("quality") or "").lower() == "hd"), None)
        if preferred:
            return preferred
    return next((item for item in candidates if str(item.get("quality") or "").lower() != "hd"), None) or candidates[0]


def _grok2api_video_size_from_ratio(ratio: Any) -> str:
    text = str(ratio or "").strip()
    if text in {"16:9", "1.7777777778"}:
        return "1792x1024"
    if text in {"9:16", "0.5625"}:
        return "1024x1792"
    if text in {"1:1", "1"}:
        return "1024x1024"
    return "720x1280"


def _grok2api_video_model_for_size(size: str) -> str:
    if size == "720x1280":
        return "grok-imagine-1.0-video-portrait-10s"
    if size == "1792x1024":
        return "grok-imagine-1.0-video-landscape[hd]-10s"
    return "grok-imagine-1.0-video-landscape-10s"


def _normalize_grok2api_video_size(model: Dict[str, Any], payload: Dict[str, Any]) -> str:
    variant_defaults = _grok2api_video_variant_defaults(str(model.get("modelName") or payload.get("modelName") or payload.get("model") or ""))
    raw = (
        _first_payload_value(payload, "size", "videoSize", "video_size")
        or _first_model_param(model, "defaultSize", "size")
        or variant_defaults.get("size")
    )
    if raw:
        text = str(raw).strip()
        if text in GROK2API_VIDEO_SIZES:
            return text
    return _grok2api_video_size_from_ratio(_first_payload_value(payload, "ratio", "aspectRatio", "aspect_ratio"))


def _normalize_grok2api_resolution(value: Any, size: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"480p", "720p"}:
        return text
    if size.startswith(("720x", "1280x")):
        return "720p"
    return "720p"


def _normalize_grok2api_preset(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in GROK2API_VIDEO_PRESETS else "normal"


def _build_grok2api_video_body(model: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_model_name = str(model.get("modelName") or payload.get("modelName") or payload.get("model") or "").strip()
    variant_defaults = _grok2api_video_variant_defaults(raw_model_name)
    seconds = (
        _first_payload_value(payload, "seconds", "duration", "durationSeconds", "duration_seconds")
        or variant_defaults.get("seconds")
        or _first_model_param(model, "defaultSeconds", "defaultDuration", "seconds", "duration")
        or 6
    )
    selected_variant = _select_grok2api_video_variant(model, payload, seconds)
    if selected_variant:
        seconds = selected_variant["seconds"]
        size = selected_variant["size"]
        request_model = (
            selected_variant["model"]
            if _prefer_concrete_grok2api_variant_model(model)
            else "grok-imagine-video"
        )
    elif variant_defaults:
        seconds = _normalize_grok2api_video_seconds(seconds)
        size = _normalize_grok2api_video_size(model, payload)
        concrete_model = _grok2api_video_model_for_size(size)
        request_model = (
            concrete_model
            if _prefer_concrete_grok2api_variant_model(model)
            else str(variant_defaults.get("model") or raw_model_name).strip()
        )
    else:
        size = _normalize_grok2api_video_size(model, payload)
        request_model = raw_model_name
    resolution = (
        _first_payload_value(payload, "resolution_name", "resolutionName", "resolution")
        or _first_model_param(model, "defaultResolutionName", "resolutionName", "resolution_name")
    )
    preset = (
        _first_payload_value(payload, "preset")
        or _first_model_param(model, "defaultPreset", "preset")
        or "normal"
    )
    return {
        "model": request_model,
        "prompt": _effective_prompt(payload),
        "seconds": _normalize_grok2api_video_seconds(seconds),
        "size": size,
        "resolution_name": _normalize_grok2api_resolution(resolution, size),
        "preset": _normalize_grok2api_preset(preset),
    }


async def _prepare_grok2api_video_body(
    body: Dict[str, Any],
    payload: Dict[str, Any],
) -> tuple[Dict[str, Any], List[tuple[str, Path]]]:
    images = _video_reference_inputs(payload)
    videos = _video_file_reference_inputs(payload)
    audios = _audio_file_reference_inputs(payload)
    if videos or audios:
        raise ProviderAdapterError("Grok2API 视频接口只支持图片参考素材")
    if len(images) > 7:
        raise ProviderAdapterError(f"Grok2API 视频最多支持 7 张参考图，当前提供了 {len(images)} 张")
    target_dir = _output_dir(payload)
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    file_fields: List[tuple[str, Path]] = []
    for index, image in enumerate(images):
        file_fields.append((
            "input_reference[]",
            await _image_reference_to_path(str(image), target_dir, index, backend_base_url),
        ))
    return dict(body), file_fields


async def _build_grok2api_video_chat_messages(payload: Dict[str, Any], prompt: str) -> List[Dict[str, Any]]:
    images = _video_reference_inputs(payload)
    videos = _video_file_reference_inputs(payload)
    audios = _audio_file_reference_inputs(payload)
    if videos or audios:
        raise ProviderAdapterError("Grok2API 视频接口只支持图片参考素材")
    if len(images) > 7:
        raise ProviderAdapterError(f"Grok2API 视频最多支持 7 张参考图，当前提供了 {len(images)} 张")
    if not images:
        return [{"role": "user", "content": prompt}]

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for index, image in enumerate(images):
        content.append({
            "type": "image_url",
            "image_url": {
                "url": await _normalize_json_image_reference(str(image), payload, index),
            },
        })
    return [{"role": "user", "content": content}]


async def _build_grok2api_video_chat_body(body: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": body["model"],
        "messages": await _build_grok2api_video_chat_messages(payload, body["prompt"]),
        "stream": True,
    }


async def _poll_grok2api_video_task(
    *,
    status_url: str,
    api_key: str,
    progress: ProgressCallback,
    timeout_seconds: int = 600,
) -> Dict[str, Any]:
    waited = 0
    step = 5
    await progress(18, "remote-submitted")
    while waited < timeout_seconds:
        await asyncio.sleep(step)
        waited += step
        try:
            payload = await _json_get_with_transient_retries(
                status_url,
                api_key,
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                attempts=3,
            )
        except ProviderAdapterError as error:
            if not _is_transient_provider_connection_error(error):
                raise
            await progress(min(88, 18 + waited // 6), "retrying-remote")
            continue
        status = _extract_status(payload)
        if status == "completed":
            await progress(92, "remote-completed")
            return payload
        if status == "failed":
            raise ProviderAdapterError(_error_message(payload, "Grok2API 视频任务失败"))
        await progress(min(88, 18 + waited // 6), status)
    raise ProviderAdapterError("Grok2API 视频任务超时，请稍后在任务历史中重试或查询")


async def _download_grok2api_video_content(content_url: str, api_key: str, payload: Dict[str, Any], video_id: str) -> Path:
    headers = _auth_headers(api_key)
    headers["Accept"] = "video/mp4,*/*"
    try:
        response = await public_http_get(
            content_url,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            headers=headers,
            trust_env=_http_trust_env(),
        )
    except UnsafeRemoteUrlError as error:
        raise ProviderAdapterError(unsafe_remote_url_message("Grok2API 视频文件下载失败")) from error
    except Exception as error:
        message = str(error).strip() or type(error).__name__
        raise ProviderAdapterError(f"Grok2API 视频文件下载失败：{message}") from error
    if not response.is_success:
        message = response.text.strip() if response.text else f"HTTP {response.status_code}"
        raise ProviderAdapterError(f"Grok2API 视频文件下载失败：{message}")
    if not response.content:
        raise ProviderAdapterError("Grok2API 视频文件为空")
    base = _as_text(payload.get("_projectCacheDir"))
    target_dir = (Path(base) if base else Path(tempfile.mkdtemp(prefix="libai-grok2api-video-"))) / "videos"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", video_id).strip(".-") or uuid.uuid4().hex[:12]
    target = target_dir / f"grok2api-{safe_id}.mp4"
    target.write_bytes(response.content)
    return target


def _can_send_reference_as_json(value: Any) -> bool:
    source = _as_text(value) or ""
    if not source:
        return True
    if source.startswith("asset://"):
        return True
    if source.startswith("file_"):
        return True
    if re.match(r"^https?://", source, re.I):
        return not _is_local_http_reference(source)
    return False


def _can_send_video_reference_as_json(value: Any) -> bool:
    source = _as_text(value) or ""
    if not source:
        return True
    if source.startswith("data:"):
        return True
    return _can_send_reference_as_json(value)


def _seedance_file_kind(field_name: str) -> str:
    if field_name.startswith("video_file"):
        return "video"
    if field_name.startswith("audio_file"):
        return "audio"
    return "image"


def _is_seedance_file_field(field_name: str) -> bool:
    return (
        re.fullmatch(r"image_file_[1-9]", field_name) is not None
        or re.fullmatch(r"video_file_[1-3]", field_name) is not None
        or re.fullmatch(r"audio_file_[1-9]", field_name) is not None
        or field_name in {"first_frame_image", "last_frame_image"}
    )


def _resolve_ffprobe_path() -> Optional[str]:
    binary_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    explicit = os.environ.get("LIBAI_FFPROBE_PATH")
    if explicit and Path(explicit).exists():
        return explicit
    root = Path(__file__).resolve().parent.parent
    candidates = [
        root / "tools" / "ffmpeg" / "win32-x64" / binary_name,
        root / "release" / "win-unpacked" / "resources" / "tools" / "ffmpeg" / "win32-x64" / binary_name,
        root / "node_modules" / "ffprobe-static" / "bin" / "win32" / "x64" / binary_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which(binary_name)


def _wav_duration_seconds(path: Path) -> Optional[float]:
    try:
        import wave

        with wave.open(str(path), "rb") as wav_file:
            rate = wav_file.getframerate()
            if rate <= 0:
                return None
            return wav_file.getnframes() / float(rate)
    except Exception:
        return None


def _media_duration_seconds(path: Path) -> Optional[float]:
    ffprobe_path = _resolve_ffprobe_path()
    if ffprobe_path:
        try:
            result = subprocess.run(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
            )
            text = (result.stdout or "").strip()
            if result.returncode == 0 and text:
                duration = float(text)
                if duration > 0:
                    return duration
        except Exception:
            pass
    if path.suffix.lower() == ".wav":
        return _wav_duration_seconds(path)
    return None


def _validate_seedance_reference_durations(file_fields: List[tuple[str, Path]]) -> None:
    video_total = 0.0
    for field_name, path in file_fields:
        if not _is_seedance_file_field(field_name):
            continue
        kind = _seedance_file_kind(field_name)
        duration = _media_duration_seconds(path)
        if not duration or duration <= 0:
            continue
        if kind == "audio":
            if duration > SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS + 0.01:
                raise ProviderAdapterError(
                    f"Seedance 单条音频素材不能超过 {SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS:g} 秒，当前约 {duration:.1f} 秒"
                )
            continue
        if kind == "video":
            video_total += duration
    if video_total > SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS + 0.01:
        raise ProviderAdapterError(
            f"Seedance 视频素材总时长不能超过 {SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS:g} 秒，当前约 {video_total:.1f} 秒"
        )


async def _validate_local_audio_reference_duration(
    source: str,
    payload: Dict[str, Any],
    index: int,
    max_duration_seconds: float,
) -> None:
    if max_duration_seconds <= 0:
        return
    if source.startswith("file_"):
        return
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return
    target_dir = _output_dir(payload)
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    try:
        path = await _media_reference_to_path(
            source,
            target_dir,
            index,
            kind="audio",
            backend_base_url=backend_base_url,
        )
    except ProviderAdapterError:
        return
    duration = _media_duration_seconds(path)
    if duration and duration > max_duration_seconds + 0.01:
        raise ProviderAdapterError(
            f"单条音频素材不能超过 {max_duration_seconds:g} 秒，当前约 {duration:.1f} 秒"
        )


def _muse_json_media_url(value: Any, label: str) -> str:
    source = _as_text(value) or ""
    if not source:
        return source
    if source.startswith("file_"):
        return source
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return source
    raise ProviderAdapterError(f"Muse 视频接口的{label}素材需要公网 URL")


async def _normalize_muse_json_media_url(
    value: Any,
    payload: Dict[str, Any],
    label: str,
    index: int,
    *,
    max_audio_duration_seconds: Optional[float] = None,
) -> str:
    source = _as_text(value) or ""
    if not source:
        return source
    if source.startswith("file_"):
        return source
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return source
    kind = "audio" if label == "音频" else "video"
    if kind == "audio" and max_audio_duration_seconds is not None:
        await _validate_local_audio_reference_duration(source, payload, index, max_audio_duration_seconds)
    return await _upload_reference_media_to_s3(source, payload, index, kind=kind)


async def _prepare_muse_video_json_body(request_body: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(request_body)
    body.pop("_muse_video_json_body", None)
    body.pop("_muse_video_api_version", None)
    contents = body.get("contents")
    if isinstance(contents, list):
        normalized_contents: List[Dict[str, Any]] = []
        for index, item in enumerate(contents):
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            if _as_text(normalized.get("imageUrl")):
                normalized["imageUrl"] = await _normalize_json_image_reference(str(normalized["imageUrl"]), payload, index)
            if _as_text(normalized.get("audioUrl")):
                normalized["audioUrl"] = await _normalize_muse_json_media_url(normalized.get("audioUrl"), payload, "音频", index)
            if _as_text(normalized.get("videoUrl")):
                normalized["videoUrl"] = await _normalize_muse_json_media_url(normalized.get("videoUrl"), payload, "视频", index)
            normalized_contents.append(normalized)
        body["contents"] = normalized_contents
    images = body.get("images")
    if isinstance(images, list):
        normalized_images: List[Dict[str, str]] = []
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                continue
            url = _as_text(item.get("url"))
            tag = _as_text(item.get("tag"))
            if not url or not tag:
                continue
            normalized_images.append({
                "tag": tag,
                "url": await _normalize_json_image_reference(url, payload, index),
            })
        body["images"] = normalized_images
    for field_name, label in (("audios", "音频"), ("videos", "视频")):
        items = body.get(field_name)
        if not isinstance(items, list):
            continue
        normalized_items: List[Dict[str, str]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            tag = _as_text(item.get("tag"))
            url = _as_text(item.get("url"))
            if not tag or not url:
                continue
            normalized_items.append({"tag": tag, "url": await _normalize_muse_json_media_url(url, payload, label, index)})
        if normalized_items:
            body[field_name] = normalized_items
        else:
            body.pop(field_name, None)
    return body


def _hermes_nested_url(item: Dict[str, Any], kind: str) -> str:
    field = f"{kind}_url"
    nested = item.get(field)
    if isinstance(nested, dict):
        return _as_text(nested.get("url")) or ""
    camel_field = f"{kind}Url"
    return _as_text(item.get(camel_field) or item.get(field)) or ""


async def _normalize_hermes_image_url(value: Any, payload: Dict[str, Any], index: int) -> str:
    source = _as_text(value) or ""
    if not source:
        return source
    if re.match(r"^https?://", source, re.I) and not _is_local_http_reference(source):
        return source
    return await _upload_reference_image_to_host(source, payload, index)


async def _prepare_hermes_video_json_body(request_body: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    body = dict(request_body)
    body.pop("_hermes_video_json_body", None)
    content = body.get("content")
    if not isinstance(content, list):
        return body
    normalized_content: List[Dict[str, Any]] = []
    counters = {"image": 0, "video": 0, "audio": 0}
    for item in content:
        if not isinstance(item, dict):
            continue
        kind_type = str(item.get("type") or "").strip().lower()
        normalized = dict(item)
        if kind_type in {"image_asset", "video_asset", "audio_asset", "text"}:
            normalized_content.append(normalized)
            continue
        if kind_type == "image_url":
            source = _hermes_nested_url(normalized, "image")
            if source:
                counters["image"] += 1
                normalized["image_url"] = {"url": await _normalize_hermes_image_url(source, payload, counters["image"] - 1)}
            normalized_content.append(normalized)
            continue
        if kind_type == "video_url":
            source = _hermes_nested_url(normalized, "video")
            if source:
                counters["video"] += 1
                normalized["video_url"] = {"url": await _normalize_muse_json_media_url(source, payload, "视频", counters["video"] - 1)}
            normalized_content.append(normalized)
            continue
        if kind_type == "audio_url":
            source = _hermes_nested_url(normalized, "audio")
            if source:
                counters["audio"] += 1
                normalized["audio_url"] = {
                    "url": await _normalize_muse_json_media_url(
                        source,
                        payload,
                        "音频",
                        counters["audio"] - 1,
                        max_audio_duration_seconds=SEEDANCE_REFERENCE_DURATION_LIMIT_SECONDS,
                    )
                }
            normalized_content.append(normalized)
            continue
        normalized_content.append(normalized)
    body["content"] = normalized_content
    return body


async def _prepare_newapi_video_body(
    body: Dict[str, Any],
    payload: Dict[str, Any],
) -> tuple[Dict[str, Any], List[tuple[str, Path]]]:
    request_body = dict(body)
    if request_body.get("_hermes_video_json_body"):
        return await _prepare_hermes_video_json_body(request_body, payload), []
    if request_body.get("_muse_video_json_body"):
        return await _prepare_muse_video_json_body(request_body, payload), []
    json_audio_reference_max_duration_seconds = _public_video_positive_float_param(
        {"value": request_body.pop("_json_audio_reference_max_duration_seconds", None)},
        "value",
    )
    mgdb_letterbox_ratio = _as_text(request_body.pop("_mgdb_nine_image_letterbox_ratio", ""))
    json_image_fields = set(request_body.pop("_json_image_reference_fields", []) or [])
    json_video_fields = set(request_body.pop("_json_video_reference_fields", []) or [])
    json_audio_fields = set(request_body.pop("_json_audio_reference_fields", []) or [])
    json_audio_data_url_fields = set(request_body.pop("_json_audio_data_url_fields", []) or [])
    file_fields: List[tuple[str, Path]] = []
    target_dir = _output_dir(payload)
    backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
    for field_name, value in list(request_body.items()):
        if field_name in json_image_fields:
            if isinstance(value, list):
                request_body[field_name] = [
                    await _normalize_json_image_reference(str(item), payload, index)
                    for index, item in enumerate(value)
                    if _as_text(item)
                ]
            elif _as_text(value):
                request_body[field_name] = await _normalize_json_image_reference(str(value), payload, 0)
            continue
        if field_name in json_video_fields:
            if isinstance(value, list):
                json_values: List[str] = []
                for index, item in enumerate(value):
                    if not _as_text(item):
                        continue
                    json_values.append(await _normalize_muse_json_media_url(item, payload, "视频", index))
                if json_values:
                    request_body[field_name] = json_values
                else:
                    request_body.pop(field_name, None)
            elif _as_text(value):
                request_body[field_name] = await _normalize_muse_json_media_url(value, payload, "视频", 0)
            else:
                request_body.pop(field_name, None)
            continue
        if field_name in json_audio_fields:
            if isinstance(value, list):
                json_values = [
                    await _normalize_muse_json_media_url(
                        item,
                        payload,
                        "音频",
                        index,
                        max_audio_duration_seconds=json_audio_reference_max_duration_seconds,
                    )
                    for index, item in enumerate(value)
                    if _as_text(item)
                ]
                if json_values:
                    request_body[field_name] = json_values
                else:
                    request_body.pop(field_name, None)
            elif _as_text(value):
                request_body[field_name] = await _normalize_muse_json_media_url(
                    value,
                    payload,
                    "音频",
                    0,
                    max_audio_duration_seconds=json_audio_reference_max_duration_seconds,
                )
            else:
                request_body.pop(field_name, None)
            continue
        if field_name in json_audio_data_url_fields:
            match = re.search(r"_(\d+)$", field_name)
            field_index = max(0, int(match.group(1)) - 1) if match else 0
            if isinstance(value, list):
                json_values = [
                    await _media_reference_to_data_url(item, payload, index, kind="audio")
                    for index, item in enumerate(value)
                    if _as_text(item)
                ]
                if json_values:
                    request_body[field_name] = json_values
                else:
                    request_body.pop(field_name, None)
            elif _as_text(value):
                request_body[field_name] = await _media_reference_to_data_url(value, payload, field_index, kind="audio")
            else:
                request_body.pop(field_name, None)
            continue
        if field_name == "image":
            if isinstance(value, list):
                request_body[field_name] = [
                    await _normalize_json_image_reference(str(item), payload, index)
                    for index, item in enumerate(value)
                    if _as_text(item)
                ]
            elif _as_text(value):
                request_body[field_name] = await _normalize_json_image_reference(str(value), payload, 0)
            continue
        if field_name == "input_reference":
            file_index = len(file_fields)
            path = await _media_reference_to_path(
                str(value),
                target_dir,
                file_index,
                kind="image",
                backend_base_url=backend_base_url,
            )
            if mgdb_letterbox_ratio:
                path = await _letterbox_mgdb_reference_path(path, target_dir, mgdb_letterbox_ratio, file_index)
            file_fields.append((field_name, path))
            request_body.pop(field_name, None)
            continue
        if not _is_seedance_file_field(field_name):
            continue
        field_kind = _seedance_file_kind(field_name)
        should_letterbox_image = bool(
            mgdb_letterbox_ratio
            and field_kind == "image"
            and not _as_text(value).startswith(("asset://", "file_"))
        )
        if _can_send_reference_as_json(value) and not should_letterbox_image:
            continue
        file_index = len(file_fields)
        path = await _media_reference_to_path(
            str(value),
            target_dir,
            file_index,
            kind=field_kind,
            backend_base_url=backend_base_url,
        )
        if should_letterbox_image:
            path = await _letterbox_mgdb_reference_path(path, target_dir, mgdb_letterbox_ratio, file_index)
        file_fields.append((field_name, path))
        request_body.pop(field_name, None)
    return request_body, file_fields


def _write_first_openai_b64_image(payload: Dict[str, Any], job_payload: Dict[str, Any], output_format: str) -> Optional[str]:
    data = payload.get("data")
    items = data if isinstance(data, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_preview_media_item(item):
            continue
        b64_json = _as_text(item.get("b64_json"))
        if not b64_json:
            continue
        try:
            raw = base64.b64decode(b64_json, validate=True)
        except Exception as error:
            raise ProviderAdapterError("图片模型返回了无效的 base64 数据") from error
        cache_dir = Path(str(job_payload.get("_projectCacheDir") or tempfile.mkdtemp(prefix="libai-image-")))
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = cache_dir / f"gpt-image-2-{uuid.uuid4().hex[:12]}{_image_suffix(output_format)}"
        output_path.write_bytes(raw)
        return str(output_path)
    return None


async def _run_openai_image(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    ratio = str(payload.get("ratio") or payload.get("aspectRatio") or "1:1")
    references = _image_reference_inputs(payload, _as_text(payload.get("_backendBaseUrl")))
    mask_image = payload.get("maskImage") or payload.get("mask_image") or payload.get("maskUrl") or payload.get("mask_url")
    model_name = str(model.get("modelName") or payload.get("modelName") or payload.get("model") or "gpt-image-2").strip()
    image_timeout = _image_request_timeout(model, payload)
    if _is_otu_gpt_image_video_model(model):
        result = await _run_otu_gpt_image_video_request(base, api_key, model, payload, progress, image_timeout)
        return {**result, "provider": provider["id"], "providerModelId": model["id"], "assetKind": "image"}
    if not _is_legacy_gpt_image_model(model_name):
        body = _build_newapi_image_body(model, payload)
        body = await _normalize_newapi_json_image_body(body, payload)
        if not body["model"]:
            raise ProviderAdapterError("图片模型名称未配置")
        if not body["prompt"]:
            raise ProviderAdapterError("图片生成缺少提示词")
        endpoint = "/v1/images/generations"
        await progress(12, "submitting")
        response = await _json_request("POST", f"{base}{endpoint}", api_key, body=body, timeout=image_timeout)
        task_id = _extract_task_id(response)
        status = _extract_status(response)
        if task_id and status != "completed":
            status_result = await _poll_remote_task(
                status_url=f"{base}/v1/tasks/{task_id}",
                api_key=api_key,
                progress=progress,
                timeout_seconds=int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, image_timeout)),
                request_timeout_seconds=image_timeout,
                media_kind="image",
                output_format=str(body.get("output_format") or "png"),
            )
            return {**status_result, "provider": provider["id"], "providerModelId": model["id"], "remoteTaskId": task_id, "assetKind": "image"}
        urls = _extract_result_media_urls(response, media_kind="image", output_format=str(body.get("output_format") or "png"))
        if not urls:
            urls = _extract_openai_image_urls(response, str(body.get("output_format") or "png"))
        if not urls:
            urls = _extract_urls(response)
        if urls:
            return {
                "provider": provider["id"],
                "providerModelId": model["id"],
                "status": "completed",
                "url": urls[0],
                "urls": urls,
                "assetKind": "image",
                "raw": _image_response_summary(response),
            }
        if task_id:
            status_result = await _poll_remote_task(
                status_url=f"{base}/v1/tasks/{task_id}",
                api_key=api_key,
                progress=progress,
                timeout_seconds=int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, image_timeout)),
                request_timeout_seconds=image_timeout,
                media_kind="image",
                output_format=str(body.get("output_format") or "png"),
            )
            return {**status_result, "provider": provider["id"], "providerModelId": model["id"], "remoteTaskId": task_id, "assetKind": "image"}
        raise ProviderAdapterError(_error_message(response, "图片模型未返回 requestId 或结果 URL"))

    output_format = _api_image_format(payload.get("outputFormat") or payload.get("output_format") or payload.get("format") or "png")
    reference_limit = _model_reference_limit(model, "maxReferenceImages", DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT)
    if len(references) > reference_limit:
        raise ProviderAdapterError(f"GPT Image 2 最多支持 {reference_limit} 张参考图，当前提供了 {len(references)} 张")
    body: Dict[str, Any] = {
        "model": model_name,
        "prompt": _effective_prompt(payload),
        "size": _normalize_gpt_image_size(
            payload.get("size"),
            ratio,
            _first_payload_value(payload, "outputResolution", "output_resolution", "resolution"),
        ),
        "quality": payload.get("quality") or (model.get("params") or {}).get("quality") or "medium",
        "output_format": output_format,
        "response_format": payload.get("responseFormat") or payload.get("response_format") or "b64_json",
        "stream": True,
        "partial_images": _gpt_image_partial_images(payload),
    }
    if payload.get("background") and payload.get("background") != "transparent":
        body["background"] = payload.get("background")
    if payload.get("moderation"):
        body["moderation"] = payload.get("moderation")
    if output_format in {"jpeg", "webp"} and payload.get("outputCompression") is not None:
        body["output_compression"] = payload.get("outputCompression")
    elif output_format in {"jpeg", "webp"} and payload.get("output_compression") is not None:
        body["output_compression"] = payload.get("output_compression")

    if references:
        endpoint = "/v1/images/edits"
    else:
        endpoint = "/v1/images/generations"

    await progress(12, "submitting")
    response: Optional[Dict[str, Any]] = None
    if _gpt_image_async_mode(model, payload):
        try:
            async_response = await _run_gpt_image_async_request(
                base,
                api_key,
                body,
                references,
                mask_image,
                payload,
                model,
                endpoint,
                image_timeout,
            )
            if _gpt_image_response_has_result_or_task(async_response, output_format):
                response = async_response
            else:
                await progress(18, "fallback-streaming")
        except ProviderAdapterError:
            await progress(18, "fallback-streaming")

    if response is None and references:
        reference_request_mode = _gpt_image_reference_request_mode(model, payload)
        if reference_request_mode == "json_generation":
            response = await _run_gpt_image_reference_json_generation(
                base,
                api_key,
                body,
                references,
                payload,
                image_timeout,
                progress,
                reference_limit,
            )
        else:
            try:
                target_dir = _output_dir(payload)
                backend_base_url = _as_text(payload.get("_backendBaseUrl") or payload.get("backendBaseUrl") or payload.get("backend_base_url"))
                reference_paths = [
                    await _image_reference_to_path(str(item).strip(), target_dir, index, backend_base_url=backend_base_url)
                    for index, item in enumerate(references[:reference_limit])
                    if str(item).strip()
                ]
                mask_path = None
                if mask_image:
                    mask_path = await _image_reference_to_path(
                        str(mask_image),
                        target_dir,
                        len(reference_paths),
                        backend_base_url=backend_base_url,
                    )
                response = await _await_image_response(
                    _streaming_multipart_request(
                        f"{base}{endpoint}",
                        api_key,
                        body=body,
                        image_paths=reference_paths,
                        mask_path=mask_path,
                        timeout=image_timeout,
                    ),
                    progress,
                )
            except ProviderAdapterError as multipart_error:
                if reference_request_mode == "multipart_edit":
                    raise
                await progress(18, "fallback")
                try:
                    response = await _run_gpt_image_reference_json_generation(
                        base,
                        api_key,
                        body,
                        references,
                        payload,
                        image_timeout,
                        progress,
                        reference_limit,
                    )
                except ProviderAdapterError as json_error:
                    raise ProviderAdapterError(
                        f"参考图自动兼容失败：edits/multipart：{multipart_error}；generations/json：{json_error}"
                    ) from json_error
    elif response is None:
        response = await _await_image_response(
            _streaming_json_request(
                "POST",
                f"{base}{endpoint}",
                api_key,
                body=body,
                timeout=image_timeout,
            ),
            progress,
        )
    local_path = _write_first_openai_b64_image(response, payload, output_format)
    if local_path:
        return {
            "provider": provider["id"],
            "providerModelId": model["id"],
            "status": "completed",
            "localPath": local_path,
            "assetKind": "image",
            "mime": _image_mime(output_format),
            "filename": Path(local_path).name,
            "raw": _image_response_summary(response),
        }
    task_id = _extract_task_id(response)
    status = _extract_status(response)
    if task_id and status != "completed":
        status_result = await _poll_remote_task(
            status_url=f"{base}/v1/tasks/{task_id}",
            api_key=api_key,
            progress=progress,
            timeout_seconds=int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, image_timeout)),
            request_timeout_seconds=image_timeout,
            media_kind="image",
            output_format=output_format,
        )
        return {**status_result, "provider": provider["id"], "providerModelId": model["id"], "remoteTaskId": task_id, "assetKind": "image"}
    urls = _extract_result_media_urls(response, media_kind="image", output_format=output_format)
    if not urls:
        urls = _extract_openai_image_urls(response, output_format)
    if not urls:
        urls = _extract_urls(response)
    if urls:
        return {"provider": provider["id"], "providerModelId": model["id"], "status": "completed", "url": urls[0], "urls": urls, "assetKind": "image", "raw": _image_response_summary(response)}
    if task_id:
        status_result = await _poll_remote_task(
            status_url=f"{base}/v1/tasks/{task_id}",
            api_key=api_key,
            progress=progress,
            timeout_seconds=int(max(DEFAULT_REQUEST_TIMEOUT_SECONDS, image_timeout)),
            request_timeout_seconds=image_timeout,
            media_kind="image",
            output_format=output_format,
        )
        return {**status_result, "provider": provider["id"], "providerModelId": model["id"], "remoteTaskId": task_id, "assetKind": "image"}
    raise ProviderAdapterError(_error_message(response, "GPT Image 2 未返回图片或任务 ID"))


async def _run_newapi_video(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    body = _build_newapi_video_body(model, payload, provider=provider)
    is_muse_video = _is_muse_video_model(model)
    is_seedance_video = _is_seedance_video_model(model)
    is_hermes_video = _is_hermes_video_model(model, payload)
    uses_generations_endpoint = is_seedance_video or is_hermes_video or _is_channel31_face_video_model(model)
    if not body["model"]:
        raise ProviderAdapterError("视频模型名称未配置")
    if is_hermes_video:
        has_prompt = any(
            isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() == "text"
            and bool(_as_text(item.get("text")))
            for item in body.get("content") or []
        )
    else:
        has_prompt = bool(_effective_prompt(payload) if is_muse_video else body.get("prompt"))
    if not has_prompt:
        raise ProviderAdapterError("视频生成缺少提示词")

    await progress(12, "submitting")
    request_body, file_fields = await _prepare_newapi_video_body(body, payload)
    if is_seedance_video:
        _validate_seedance_reference_durations(file_fields)
    endpoint_path = "/v1/video/generations" if uses_generations_endpoint else "/v1/videos"
    endpoint = f"{base}{endpoint_path}"
    client_task_id = _newapi_client_task_id(payload)
    client_task_headers = _newapi_client_task_headers(payload)
    recovery_status_path = (
        f"/v1/video/generations/{client_task_id}"
        if uses_generations_endpoint and client_task_id
        else None
    )
    submit_timeout = _seedance_video_submit_timeout() if uses_generations_endpoint else _newapi_video_submit_timeout()

    async def submit_once() -> Dict[str, Any]:
        if file_fields:
            multipart_kwargs: Dict[str, Any] = {"timeout": submit_timeout}
            if client_task_headers:
                multipart_kwargs["extra_headers"] = client_task_headers
            return await _multipart_named_request(endpoint, api_key, request_body, file_fields, **multipart_kwargs)
        json_kwargs: Dict[str, Any] = {"body": request_body, "timeout": submit_timeout}
        if client_task_headers:
            json_kwargs["extra_headers"] = client_task_headers
        return await _json_request("POST", endpoint, api_key, **json_kwargs)

    async def submit_or_recover_once() -> Dict[str, Any]:
        try:
            return await submit_once()
        except ProviderAdapterError as error:
            if is_muse_video or not client_task_id or not _is_retryable_newapi_video_submit_error(error):
                raise
            return await _recover_newapi_video_submission(
                base=base,
                api_key=api_key,
                client_task_id=client_task_id,
                progress=progress,
                original_error=error,
                status_path=recovery_status_path,
            )

    async def submit_stage_once() -> Dict[str, Any]:
        if not uses_generations_endpoint:
            return await submit_or_recover_once()
        await progress(13, "waiting-seedance-submit")
        async with _seedance_video_submit_semaphore():
            await progress(13, "submitting-seedance")
            return await submit_or_recover_once()

    attempts = 1
    response: Optional[Dict[str, Any]] = None
    last_submit_error: Optional[ProviderAdapterError] = None
    for attempt in range(attempts):
        try:
            response = await submit_stage_once()
            break
        except ProviderAdapterError as error:
            last_submit_error = error
            if attempt >= attempts - 1 or not _is_retryable_newapi_video_submit_error(error):
                raise
            await progress(13, "retrying-submit")
            await asyncio.sleep(_newapi_video_submit_retry_delay_seconds() * (attempt + 1))
    if response is None:
        raise last_submit_error or ProviderAdapterError("视频模型提交失败")
    urls = _extract_urls(response)
    task_id = _extract_task_id(response)
    status = _extract_status(response)
    if status == "completed" and urls:
        return {
            "provider": provider["id"],
            "providerModelId": model["id"],
            "status": "completed",
            "url": urls[0],
            "urls": urls,
            "assetKind": "video",
            "raw": response,
        }
    if not task_id:
        raise ProviderAdapterError(_error_message(response, "视频模型未返回 requestId 或结果 URL"))
    status_path = f"/v1/video/generations/{task_id}" if uses_generations_endpoint else f"/v1/videos/{task_id}"
    status_result = await _poll_remote_task(
        status_url=f"{base}{status_path}",
        api_key=api_key,
        progress=progress,
        media_kind="video",
        timeout_seconds=_newapi_video_result_timeout_seconds(),
        request_timeout_seconds=_newapi_video_status_request_timeout_seconds(),
    )
    return {
        **status_result,
        "provider": provider["id"],
        "providerModelId": model["id"],
        "remoteTaskId": task_id,
        "assetKind": "video",
    }


async def _run_grok2api_video(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    body = _build_grok2api_video_body(model, payload)
    if not body["model"]:
        raise ProviderAdapterError("Grok2API 视频模型名称未配置")
    if not body["prompt"]:
        raise ProviderAdapterError("Grok2API 视频生成缺少提示词")

    await progress(12, "submitting")
    request_body = await _build_grok2api_video_chat_body(body, payload)
    endpoint = f"{base}/v1/chat/completions"
    response = await _streaming_json_request("POST", endpoint, api_key, body=request_body, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    urls = _extract_urls(response)
    if not urls:
        raise ProviderAdapterError(_error_message(response, "Grok2API Chat Completions 未返回视频 URL"))
    await progress(96, "remote-completed")
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "status": "completed",
        "assetKind": "video",
        "url": urls[0],
        "urls": urls,
        "remoteTaskId": _extract_task_id(response),
        "raw": response,
    }


async def _run_firefly_video(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    body = await _build_firefly_video_chat_body(model, payload)
    if not body["model"]:
        raise ProviderAdapterError("Firefly 视频模型名称未配置")
    content = body["messages"][0]["content"]
    prompt_text = ""
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                prompt_text = str(item.get("text") or "").strip()
                break
    if not prompt_text:
        raise ProviderAdapterError("Firefly 视频生成缺少提示词")

    await progress(12, "submitting")
    endpoint = f"{base}/v1/chat/completions"
    response = await _streaming_json_request("POST", endpoint, api_key, body=body, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    urls = _extract_urls(response)
    if not urls:
        raise ProviderAdapterError(_error_message(response, "Firefly Chat Completions 未返回视频 URL"))
    await progress(96, "remote-completed")
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "status": "completed",
        "assetKind": "video",
        "url": urls[0],
        "urls": urls,
        "remoteTaskId": _extract_task_id(response),
        "raw": response,
    }


def _join_provider_path(base: str, path: str) -> str:
    suffix = str(path or "").strip() or "/"
    if re.match(r"^https?://", suffix, re.I):
        return suffix
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{base.rstrip('/')}{suffix}"


def _public_video_url_from_payload(payload: Dict[str, Any]) -> str:
    for key in ("videoUrl", "video_url", "sourceVideoUrl", "source_video_url", "assetUrl", "asset_url", "url", "src"):
        value = _as_text(payload.get(key))
        if not value or not re.match(r"^https?://", value, re.I):
            continue
        if is_private_or_local_http_url(value):
            continue
        return value
    return ""


def _local_video_path_from_payload(payload: Dict[str, Any]) -> Optional[Path]:
    for key in (
        "_sourceAssetPath",
        "assetPath",
        "asset_path",
        "videoPath",
        "video_path",
        "filePath",
        "file_path",
        "localPath",
        "local_path",
        "path",
    ):
        value = _as_text(payload.get(key))
        if not value:
            continue
        try:
            candidate = Path(value)
        except Exception:
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _ghostcut_subtitle_body(model: Dict[str, Any], payload: Dict[str, Any], source_url: str = "") -> Dict[str, Any]:
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    model_name = (
        _as_text(payload.get("ghostcutModel"))
        or _as_text(payload.get("ghostcut_model"))
        or _as_text(params.get("ghostcutModel"))
        or _as_text(params.get("defaultModel"))
        or "advanced_lite"
    )
    body: Dict[str, Any] = {
        "model": model_name,
        "region": payload.get("region") or payload.get("subtitleRegion") or payload.get("subtitle_region") or {},
        "expandPixels": payload.get("expandPixels") if payload.get("expandPixels") is not None else payload.get("expand_pixels", 0),
        "inpaintMode": payload.get("inpaintMode") or payload.get("inpaint_mode") or "auto",
        "qualityPreset": payload.get("qualityPreset") or payload.get("quality_preset") or "balanced",
        "preserveAudio": payload.get("preserveAudio") if "preserveAudio" in payload else payload.get("preserve_audio", True),
        "timeRange": payload.get("timeRange") or payload.get("time_range") or {"mode": "full"},
        "videoWidth": payload.get("videoWidth") or payload.get("video_width"),
        "videoHeight": payload.get("videoHeight") or payload.get("video_height"),
        "outputTitle": payload.get("outputTitle") or payload.get("output_title") or payload.get("title"),
        "sourceAssetId": payload.get("sourceAssetId") or payload.get("assetId") or payload.get("asset_id"),
        "sourceNodeId": payload.get("sourceNodeId") or payload.get("source_node_id") or payload.get("nodeId") or payload.get("node_id"),
        "projectId": payload.get("_projectId") or payload.get("projectId") or payload.get("project_id"),
        "jobId": payload.get("_jobId") or payload.get("jobId") or payload.get("job_id"),
    }
    if source_url:
        body["videoUrl"] = source_url
    return {key: value for key, value in body.items() if value is not None}


async def _run_ghostcut_subtitle(provider: Dict[str, Any], model: Dict[str, Any], payload: Dict[str, Any], progress: ProgressCallback) -> Dict[str, Any]:
    api_key = _provider_key(provider)
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    base = _clean_base_url(str(provider.get("baseUrl") or DEFAULT_NEWAPI_BASE_URL))
    submit_url = _join_provider_path(base, str(params.get("submitPath") or "/api/ghostcut/subtitle-remove"))
    source_url = _public_video_url_from_payload(payload)
    local_path = None if source_url else _local_video_path_from_payload(payload)
    if not source_url and local_path is None:
        raise ProviderAdapterError("GhostCut 去字幕需要公网视频 URL，或可读取的本地视频文件")

    body = _ghostcut_subtitle_body(model, payload, source_url=source_url)
    await progress(12, "ghostcut-submit")
    timeout = float(params.get("submitTimeoutSeconds") or DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if local_path is not None:
        response = await _multipart_named_request(submit_url, api_key, body, [("video", local_path)], timeout=timeout)
    else:
        response = await _json_request("POST", submit_url, api_key, body=body, timeout=timeout)

    direct_urls = _extract_result_media_urls(response, media_kind="video", output_format="mp4") or _extract_urls(response)
    task_id = _extract_task_id(response) or ""
    if direct_urls and _extract_status(response) == "completed":
        await progress(96, "remote-completed")
        return {
            "provider": provider["id"],
            "providerModelId": model["id"],
            "providerModelName": model.get("modelName"),
            "displayName": model.get("displayName") or model.get("modelName"),
            "status": "completed",
            "assetKind": "video",
            "url": direct_urls[0],
            "urls": direct_urls,
            "remoteTaskId": task_id,
            "raw": response,
            "source": "video.subtitle.remove",
            "engine": "GhostCut",
        }

    if not task_id:
        raise ProviderAdapterError(_error_message(response, "GhostCut 中转站未返回任务 ID"))

    status_path_template = str(params.get("statusPathTemplate") or "/api/ghostcut/subtitle-remove/{taskId}")
    status_url = _join_provider_path(base, status_path_template.replace("{taskId}", quote(task_id, safe="")))
    result = await _poll_remote_task(
        status_url=status_url,
        api_key=api_key,
        progress=progress,
        timeout_seconds=int(params.get("timeoutSeconds") or 3600),
        request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        media_kind="video",
        output_format="mp4",
    )
    return {
        "provider": provider["id"],
        "providerModelId": model["id"],
        "providerModelName": model.get("modelName"),
        "displayName": model.get("displayName") or model.get("modelName"),
        "status": "completed",
        "assetKind": "video",
        "url": result["url"],
        "urls": result.get("urls") or [result["url"]],
        "remoteTaskId": task_id,
        "raw": result.get("raw"),
        "source": "video.subtitle.remove",
        "engine": "GhostCut",
    }


async def run_provider_job(
    job_type: str,
    payload: Dict[str, Any],
    provider: Dict[str, Any],
    model: Dict[str, Any],
    progress: ProgressCallback,
) -> Dict[str, Any]:
    adapter = str(model.get("adapter") or provider.get("adapter") or "")
    if provider.get("id") == "local.upscale" or adapter == "local.upscale.realesrgan":
        return await _run_local_upscale(payload, progress)
    if not provider.get("enabled"):
        raise ProviderAdapterError(f"供应商 {provider.get('name') or provider.get('id')} 未启用")
    if adapter == "openai.image":
        return await _run_openai_image(provider, model, payload, progress)
    if adapter == "newapi.video":
        return await _run_newapi_video(provider, model, payload, progress)
    if adapter == "grok2api.video":
        return await _run_grok2api_video(provider, model, payload, progress)
    if adapter == "yunzhi.firefly.video":
        return await _run_firefly_video(provider, model, payload, progress)
    if adapter == "ghostcut.subtitle":
        return await _run_ghostcut_subtitle(provider, model, payload, progress)
    if adapter == "openai.responses":
        if job_type == "image.analyze":
            return await _run_openai_image_analyze(provider, model, payload, progress)
        return await _run_openai_responses(provider, model, payload, progress)
    raise ProviderAdapterError(f"未实现的 Provider Adapter: {adapter}")


async def test_provider_connection(provider: Dict[str, Any]) -> Dict[str, Any]:
    if provider.get("id") == "local.upscale":
        binary = _find_realesrgan_binary({})
        if binary:
            return {"ok": True, "message": f"Real-ESRGAN 可用：{binary}"}
        return {"ok": True, "message": "未检测到 Real-ESRGAN，将使用 Pillow/Lanczos 兜底"}
    api_key = str(provider.get("apiKey") or "").strip()
    if not api_key:
        return {"ok": False, "message": "API Key 未配置"}
    base = _clean_base_url(str(provider.get("baseUrl") or ""))
    try:
        await _json_request("GET", f"{base}/v1/models", api_key, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        return {"ok": True, "message": "连接正常"}
    except Exception as error:
        message = str(error)
        if re.search(r"\b(401|403)\b", message):
            return {"ok": False, "message": "认证失败，请检查 API Key"}
        return {"ok": False, "message": message or "连接失败"}

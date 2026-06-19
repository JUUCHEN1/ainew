from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx


class UnsafeRemoteUrlError(ValueError):
    pass


LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "0",
    "0.0.0.0",
}
PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
SENSITIVE_REDIRECT_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
}


def _normal_host(hostname: Optional[str]) -> str:
    return (hostname or "").strip().strip(".").lower()


def is_private_or_local_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any((
        address.is_loopback,
        address.is_private,
        address.is_link_local,
        address.is_multicast,
        address.is_reserved,
        address.is_unspecified,
    ))


def is_proxy_fake_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in PROXY_FAKE_IP_NETWORKS)


def _resolve_hostname(hostname: str) -> list[str]:
    addresses: list[str] = []
    for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def is_private_or_local_http_url(url: str, *, resolve: bool = False) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    hostname = _normal_host(parsed.hostname)
    if not hostname or hostname in LOCAL_HOSTNAMES or hostname.endswith(".localhost"):
        return True
    if is_private_or_local_ip(hostname):
        return True
    if resolve:
        try:
            return any(
                is_private_or_local_ip(address) and not is_proxy_fake_ip(address)
                for address in _resolve_hostname(hostname)
            )
        except OSError:
            return False
    return False


def assert_public_http_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> None:
    try:
        parsed = urlparse(url)
    except Exception as error:
        raise UnsafeRemoteUrlError("远程地址格式无效") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeRemoteUrlError("远程地址必须是 HTTP/HTTPS")
    if parsed.username or parsed.password:
        raise UnsafeRemoteUrlError("远程地址不能包含用户名或密码")
    if allow_private:
        return
    if is_private_or_local_http_url(url, resolve=resolve):
        raise UnsafeRemoteUrlError("远程地址必须是公网 HTTP/HTTPS，不能访问本机、内网或云元数据地址")


def unsafe_remote_url_message(prefix: str = "下载失败") -> str:
    return f"{prefix}：远程地址必须是公网 HTTP/HTTPS，不能访问本机、内网或云元数据地址"


def _http_origin(url: str) -> tuple[str, str, Optional[int]]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
    return scheme, _normal_host(parsed.hostname), port


def _redirect_headers(headers: Dict[str, str], previous_url: str, next_url: str) -> Dict[str, str]:
    if _http_origin(previous_url) == _http_origin(next_url):
        return dict(headers)
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in SENSITIVE_REDIRECT_HEADER_NAMES
    }


async def public_http_get(
    url: str,
    *,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    trust_env: bool = False,
    max_redirects: int = 5,
    allow_private: bool = False,
) -> httpx.Response:
    current = str(url or "").strip()
    request_headers = dict(headers or {})
    assert_public_http_url(current, allow_private=allow_private)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=trust_env) as client:
        for _ in range(max_redirects + 1):
            response = await client.get(current, headers=request_headers)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            previous = str(response.url or current)
            current = urljoin(previous, location)
            assert_public_http_url(current, allow_private=allow_private)
            request_headers = _redirect_headers(request_headers, previous, current)
        raise UnsafeRemoteUrlError("远程地址重定向次数过多")


def is_local_backend_asset_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.path.startswith("/assets/"):
        return False
    return is_private_or_local_http_url(url)

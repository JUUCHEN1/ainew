from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from typing import Optional


LEGACY_SECRET_PREFIX = "local:v1:"
DPAPI_SECRET_PREFIX = "local:v2:dpapi:"
DPAPI_ENTROPY = b"LibAI:local-secret:v2"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def dpapi_protect(data: bytes) -> Optional[bytes]:
    if os.name != "nt":
        return None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    data_blob, data_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(DPAPI_ENTROPY)
    output_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(data_blob),
        "LibAI secret",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    )
    _ = data_buffer, entropy_buffer
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return _blob_to_bytes(output_blob)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)


def dpapi_unprotect(data: bytes) -> Optional[bytes]:
    if os.name != "nt":
        return None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    data_blob, data_buffer = _blob_from_bytes(data)
    entropy_blob, entropy_buffer = _blob_from_bytes(DPAPI_ENTROPY)
    output_blob = DATA_BLOB()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output_blob),
    )
    _ = data_buffer, entropy_buffer
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return _blob_to_bytes(output_blob)
    finally:
        if description:
            kernel32.LocalFree(description)
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def encode_secret_value(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    raw = text.encode("utf-8")
    try:
        protected = dpapi_protect(raw)
    except Exception:
        protected = None
    if protected:
        return DPAPI_SECRET_PREFIX + _b64(protected)
    return LEGACY_SECRET_PREFIX + _b64(raw)


def decode_secret_value(value: Optional[str]) -> str:
    text = value or ""
    if not text:
        return ""
    if text.startswith(DPAPI_SECRET_PREFIX):
        try:
            raw = base64.b64decode(text[len(DPAPI_SECRET_PREFIX):])
            unprotected = dpapi_unprotect(raw)
            return (unprotected or b"").decode("utf-8")
        except Exception:
            return ""
    if text.startswith(LEGACY_SECRET_PREFIX):
        try:
            return base64.b64decode(text[len(LEGACY_SECRET_PREFIX):]).decode("utf-8")
        except Exception:
            return ""
    return text

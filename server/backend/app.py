import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import url2pathname

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask
from backend.image_security import safe_image_open
from pydantic import BaseModel, Field

from backend.runtime_config import cors_allowed_origins
from backend.network_security import (
    UnsafeRemoteUrlError,
    is_private_or_local_http_url,
    public_http_get,
    unsafe_remote_url_message,
)
from backend.new_api_client import NewApiClient, NewApiError, stream_desktop_announcements
from backend.provider_adapters import (
    ProviderAdapterError,
    _http_trust_env as provider_http_trust_env,
    prepare_outgoing_reference_image_payload,
    run_provider_job,
    test_provider_connection,
)
from backend.jianying_export import router as jianying_router
from backend.secret_store import LEGACY_SECRET_PREFIX, decode_secret_value, encode_secret_value
from backend.subtitle_removal import SubtitleRemovalError, remove_subtitles_from_video
from backend.video_analysis import VideoAnalysisError, analyze_video_reference, binary_runtime_status, trim_video_reference


APP_NAME = "LibAI"
PROJECT_STORAGE_VERSION = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3000.0
SEEDANCE_PORTRAIT_ASSET_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_QUEUE_LIMIT = 5
DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_RETRY_ATTEMPTS = 2
DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_RETRY_DELAY_SECONDS = 8
DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT = 10
DEFAULT_TEXT_INFERENCE_QUEUE_LIMIT = 2


PROVIDER_JOB_QUEUE_SEMAPHORES: Dict[str, asyncio.Semaphore] = {}
PROVIDER_JOB_QUEUE_LIMITS: Dict[str, int] = {}
ACTIVE_JOB_TASKS: Dict[str, asyncio.Task] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def app_data_dir() -> Path:
    configured = os.environ.get("LIBAI_APP_DATA_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".libai"


def backend_base_url() -> str:
    port = str(os.environ.get("LIBAI_BACKEND_PORT") or "8765").strip() or "8765"
    return f"http://127.0.0.1:{port}"


def exception_message(error: Exception, fallback: str) -> str:
    message = str(error).strip()
    if message:
        return message
    name = type(error).__name__
    return f"{fallback}（{name}）" if name else fallback


def register_active_job_task(job_id: str, task: asyncio.Task) -> None:
    if not job_id or task is None:
        return
    ACTIVE_JOB_TASKS[job_id] = task

    def cleanup(done_task: asyncio.Task) -> None:
        if ACTIVE_JOB_TASKS.get(job_id) is done_task:
            ACTIVE_JOB_TASKS.pop(job_id, None)

    task.add_done_callback(cleanup)


def unregister_active_job_task(job_id: str, task: Optional[asyncio.Task] = None) -> None:
    if not job_id:
        return
    if task is not None and ACTIVE_JOB_TASKS.get(job_id) is not task:
        return
    ACTIVE_JOB_TASKS.pop(job_id, None)


def cancel_active_job_task(job_id: str) -> bool:
    task = ACTIVE_JOB_TASKS.get(job_id)
    if not task or task.done():
        ACTIVE_JOB_TASKS.pop(job_id, None)
        return False
    task.cancel()
    ACTIVE_JOB_TASKS.pop(job_id, None)
    return True


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


DATA_DIR = app_data_dir()
DB_PATH = DATA_DIR / "libai.sqlite3"
SETTINGS_PATH = DATA_DIR / "settings.json"
SEEDANCE_PORTRAIT_ASSETS_PATH = DATA_DIR / "seedance-portrait-assets.json"
CHANNEL24_PORTRAIT_ASSETS_PATH = DATA_DIR / "channel24-portrait-assets.json"
SEEDANCE_PORTRAIT_PREVIEWS_DIR = DATA_DIR / "seedance-portrait-previews"
REFERENCE_MEDIA_DIR = DATA_DIR / "reference-media"
SEEDANCE_PORTRAIT_ASSET_BRIDGE_PATH = "/v1/libai/seedance/assets"
SEEDANCE_PORTRAIT_ASSET_LEGACY_PATH = "/v1/volc/assets"
CHANNEL24_PORTRAIT_DEFAULT_BASE_URL = "https://manchuang.xyz/v1/libai/channel24"
SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_PREFIX = LEGACY_SECRET_PREFIX
NEWAPI_ACCOUNT_ID = "primary"
CURRENT_NEWAPI_HOST = "manchuang.xyz"
DIRECT_NEWAPI_HOST = ".".join(["103", "207", "68", "225"])
MAINLAND_NEWAPI_HOST = ".".join(["210", "16", "166", "38"])
PREVIOUS_NEWAPI_HOST = ".".join(["69", "30", "252", "146"])
DEFAULT_NEWAPI_BASE_URL = f"https://{CURRENT_NEWAPI_HOST}"
NEWAPI_PASSWORD_MIN_LENGTH = 8
LEGACY_NEWAPI_BASE_URLS = {
    f"http://{CURRENT_NEWAPI_HOST}",
    f"http://{CURRENT_NEWAPI_HOST}:3000",
    f"https://{CURRENT_NEWAPI_HOST}:3000",
    f"http://{DIRECT_NEWAPI_HOST}:3000",
    f"https://{DIRECT_NEWAPI_HOST}:3000",
    "https://api.yueying01.cn",
    "http://api.yueying01.cn",
    f"http://{MAINLAND_NEWAPI_HOST}:3000",
    f"https://{MAINLAND_NEWAPI_HOST}:3000",
    f"http://{PREVIOUS_NEWAPI_HOST}:3000",
    f"https://{PREVIOUS_NEWAPI_HOST}:3000",
    f"http://{PREVIOUS_NEWAPI_HOST}:8317",
    f"https://{PREVIOUS_NEWAPI_HOST}:8317",
}
NEWAPI_BASE_URL_SUFFIXES = (
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/images/compositions",
    "/v1/videos/generations",
    "/v1/videos",
    "/v1/video/create",
    "/v1/video/query",
    "/v1/models",
    "/v1",
)
GLOBAL_ASSET_PROJECT_ID = "global"
MANUAL_LIBRARY_SOURCES = {
    "asset-library",
    "canvas.save.global",
    "canvas.save.project",
    "global-import",
    "global.import",
    "global.promote",
    "global.saved",
    "global.write",
    "library-import",
    "library-upload",
    "saved",
    "subject-upscale",
    "user-saved",
}
AUTO_OUTPUT_SOURCES = (
    "canvas-image-tool",
    "generated",
    "job.output",
    "job.remote",
    "job.upscale",
    "local.preview",
    "panorama.generate",
    "preview",
    "yunwu",
)
def default_design_prompt_template_dir() -> Path:
    configured = os.environ.get("LIBAI_DESIGN_PROMPT_TEMPLATE_DIR")
    if configured:
        return Path(configured).expanduser()

    candidates: List[Path] = []
    resource_base = os.environ.get("LIBAI_BACKEND_RESOURCE_DIR")
    if resource_base:
        candidates.append(Path(resource_base).expanduser() / "design_prompt_templates")

    pyinstaller_base = getattr(sys, "_MEIPASS", "")
    if pyinstaller_base:
        candidates.append(Path(pyinstaller_base) / "backend" / "design_prompt_templates")

    here = Path(__file__).resolve().parent
    candidates.append(here / "design_prompt_templates")
    candidates.append(here.parent / "backend" / "design_prompt_templates")

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return candidates[0]


DESIGN_PROMPT_TEMPLATE_DIR = default_design_prompt_template_dir()
DESIGN_PROMPT_TEMPLATE_FALLBACKS = [
    {
        "id": "expression",
        "name": "表情包",
        "sourcePath": "",
        "content": "生成 3x3 照片网格。在所有面板中完整保留角色的面部、发型和服装；每个网格都是角色头像正视图；服装、脸型和发型保持一致；姿势和表情各不相同。",
    },
    {
        "id": "three-view",
        "name": "三视图",
        "sourcePath": "",
        "content": "参考当前角色设定，制作同一角色的三视图，正面、侧面、背面平行排开，画幅16:9。注意输出是一张完整三视图图片。",
    },
    {
        "id": "character-split",
        "name": "角色拆分",
        "sourcePath": "",
        "content": "横图，白色背景，创作角色设计图。分解服装、饰品、表情、全身图和局部放大细节，用中文标注，保持角色脸型、发色、身材和画风一致。",
    },
    {
        "id": "color-card",
        "name": "线稿色卡",
        "sourcePath": "",
        "content": "生成清晰线稿与色卡。提取角色主要配色，色块约6-12色，每个颜色标注HEX或RGB数值，准确反映整体色彩风格。",
    },
    {
        "id": "design-split",
        "name": "角色设计拆分",
        "sourcePath": "",
        "content": "生成专业级角色三视图及细节设定图。包含全身三视图、服装结构、材质特写、配饰、表情与色彩说明，风格与当前角色设定一致。",
    },
    {
        "id": "super-split",
        "name": "超级拆分",
        "sourcePath": "",
        "content": "生成全景式角色深度概念分解图。中心放置角色全身立绘，周围展示服装分层、不同表情、核心道具、材质特写和随身物品展示。",
    },
]
NEWAPI_PROVIDER_ID = "newapi"
GHOSTCUT_PROVIDER_ID = "ghostcut"
LOCAL_UPSCALE_PROVIDER_ID = "local.upscale"
LOCAL_UPSCALE_MODEL_ID = "local.upscale.realesrgan"
GHOSTCUT_SUBTITLE_MODEL_ID = "ghostcut.subtitle.remove"
GHOSTCUT_SUBTITLE_ADAPTER = "ghostcut.subtitle"
IMAGE_ANALYZE_MODEL_ID = "gpt-5.5-image-analyze"
SEEDANCE_FAST_MODEL_ID = "seedance-2.0-fast"
SEEDANCE_PRO_MODEL_ID = "seedance-2.0-pro"
SEEDANCE_DASH_MODEL_ID = "seedance-2-0"
SEEDANCE_DASH_FAST_MODEL_ID = "seedance-2-0-fast"
SEEDANCE_DASH_PRO_MODEL_ID = "seedance-2-0-pro"
SEEDANCE_CURRENT_MODEL_IDS = {SEEDANCE_DASH_MODEL_ID, SEEDANCE_DASH_FAST_MODEL_ID, SEEDANCE_DASH_PRO_MODEL_ID}
CHANNEL31_FACE_PRO_MODEL_ID = "seedence2.0（过人脸渠道五）"
CHANNEL31_FACE_FAST_MODEL_ID = "seedence2.0fast（过人脸渠道五）"
CHANNEL31_FACE_MODEL_IDS = {CHANNEL31_FACE_PRO_MODEL_ID, CHANNEL31_FACE_FAST_MODEL_ID}
CHANNEL31_FACE_MODEL_ID_ALIASES = {
    "seedence2.0(过人脸渠道五)": CHANNEL31_FACE_PRO_MODEL_ID,
    "seedence2.0fast(过人脸渠道五)": CHANNEL31_FACE_FAST_MODEL_ID,
}
CHANNEL31_FACE_RESOLUTION_PRICES = {
    CHANNEL31_FACE_PRO_MODEL_ID: {
        "480p": {"modelPrice": 5.0, "adminPrice": 4.5},
        "720p": {"modelPrice": 6.0, "adminPrice": 5.5},
        "1080p": {"modelPrice": 10.0, "adminPrice": 9.0},
    },
    CHANNEL31_FACE_FAST_MODEL_ID: {
        "480p": {"modelPrice": 3.0, "adminPrice": 2.5},
        "720p": {"modelPrice": 5.5, "adminPrice": 5.0},
    },
}
HERMES_SEEDANCE_MODEL_ID = "seedance-2"
HERMES_SEEDANCE_REQUEST_MODEL_NAME = "seedance-2（满血）"
HERMES_SEEDANCE_MODEL_IDS = {HERMES_SEEDANCE_MODEL_ID}
HERMES_SEEDANCE_MODEL_ID_ALIASES = {
    HERMES_SEEDANCE_REQUEST_MODEL_NAME: HERMES_SEEDANCE_MODEL_ID,
    "seedance-2(满血)": HERMES_SEEDANCE_MODEL_ID,
    "不过真人满血渠道": HERMES_SEEDANCE_MODEL_ID,
    "不过真人满血": HERMES_SEEDANCE_MODEL_ID,
}
SORA3_SEEDANCE_MODEL_ALIASES = {
    "sora-3-fast": SEEDANCE_FAST_MODEL_ID,
    "sora-3-pro": SEEDANCE_PRO_MODEL_ID,
}
SEEDANCE_PROTOCOL_MODEL_IDS = {
    SEEDANCE_FAST_MODEL_ID,
    SEEDANCE_PRO_MODEL_ID,
    SEEDANCE_DASH_MODEL_ID,
    SEEDANCE_DASH_FAST_MODEL_ID,
    *SORA3_SEEDANCE_MODEL_ALIASES,
}
MUSE_VIDEO_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "seedence2.0-m-c": {
        "modelName": "seedence2.0-M-C",
        "displayName": "seedence2.0-M-C",
        "apiVersion": "v1",
        "aliases": ["seedence2.0-M-C", "MUSE_SEEDANCE20"],
    },
    "seedence2.0fast-m-c": {
        "modelName": "seedence2.0fast-M-C",
        "displayName": "seedence2.0fast-M-C",
        "apiVersion": "v1",
        "aliases": ["seedence2.0fast-M-C", "MUSE_SEEDANCE20_FAST"],
    },
    "seedence2.0-real-person-m-c": {
        "modelName": "seedence2.0人脸-M-C",
        "displayName": "seedence2.0人脸-M-C",
        "apiVersion": "v2",
        "enabled": False,
        "aliases": ["seedence2.0人脸-M-C", "MUSE_SEE_DANCE_2_0_REAL_PERSON"],
    },
    "seedence2.0-company-m-c": {
        "modelName": "seedence2.0企业-M-C",
        "displayName": "seedence2.0企业-M-C",
        "apiVersion": "v2",
        "aliases": ["seedence2.0企业-M-C", "MUSE_SEE_DANCE_2_0_REAL_PERSON_COMPANY"],
    },
    "muse_sd2_fast_real_full": {
        "modelName": "MUSE_SD2_FAST_REAL_FULL",
        "displayName": "seedence2.0fast（满血可人脸）",
        "apiVersion": "v2",
        "aliases": ["MUSE_SD2_FAST_REAL_FULL", "seedence2.0fast（满血可人脸）"],
        "params": {
            "supportedResolutions": ["480p", "720p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
        },
    },
    "muse_sd2_real_full": {
        "modelName": "MUSE_SD2_REAL_FULL",
        "displayName": "seedence2.0pro（满血可人脸）",
        "apiVersion": "v2",
        "aliases": ["MUSE_SD2_REAL_FULL", "seedence2.0pro（满血可人脸）"],
        "params": {
            "supportedResolutions": ["480p", "720p", "1080p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
        },
    },
    "muse_sd2_channel22_fast_por": {
        "modelName": "seedence2.0-fast（满血过人脸渠道2）",
        "displayName": "seedence2.0-fast（满血过人脸渠道2）",
        "apiVersion": "v2",
        "aliases": ["seedence2.0-fast（满血过人脸渠道2）", "seedence2.0-fast-2", "videos_fast-por"],
        "params": {
            "supportedResolutions": ["720p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
            "maxReferenceAudios": 3,
            "maxReferenceVideos": 0,
            "maxPromptCharacters": 1400,
            "supportsVideoReference": False,
        },
    },
    "seedence2.0-pro-2": {
        "modelName": "seedence2.0-pro（满血过人脸渠道2）",
        "displayName": "seedence2.0-pro（满血过人脸渠道2）",
        "apiVersion": "v2",
        "aliases": [
            "seedence2.0-pro（满血过人脸渠道2）",
            "seedence2.0-pro-2",
            "muse_sd2_channel22_pro_por",
            "muse_sd2_channel22_fset_por",
            "seedence2.0-fset（满血过人脸渠道2）",
            "seedence2.0-fset-2",
            "videos-por",
        ],
        "params": {
            "supportedResolutions": ["720p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
            "maxReferenceAudios": 3,
            "maxReferenceVideos": 0,
            "maxPromptCharacters": 1400,
            "supportsVideoReference": False,
        },
    },
    "muse_sd2_fast_full": {
        "modelName": "MUSE_SD2_FAST_FULL",
        "displayName": "seedence2.0fast（满血）",
        "apiVersion": "v2",
        "seed": False,
        "aliases": ["MUSE_SD2_FAST_FULL", "seedence2.0fast（满血）"],
        "params": {
            "supportedResolutions": ["480p", "720p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
        },
    },
    "muse_sd2_full": {
        "modelName": "MUSE_SD2_FULL",
        "displayName": "seedence2.0pro（满血）",
        "apiVersion": "v2",
        "seed": False,
        "aliases": ["MUSE_SD2_FULL", "seedence2.0pro（满血）"],
        "params": {
            "supportedResolutions": ["480p", "720p", "1080p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 9,
        },
    },
    "muse_sd2_fast_four": {
        "modelName": "MUSE_SD2_FAST_FOUR",
        "displayName": "seedence2.0fast（4张参考）",
        "apiVersion": "v2",
        "aliases": ["MUSE_SD2_FAST_FOUR", "seedence2.0fast（4张参考）"],
        "params": {
            "supportedResolutions": ["480p", "720p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 4,
        },
    },
    "muse_sd2_four": {
        "modelName": "MUSE_SD2_FOUR",
        "displayName": "seedence2.0pro（4张参考）",
        "apiVersion": "v2",
        "aliases": ["MUSE_SD2_FOUR", "seedence2.0pro（4张参考）"],
        "params": {
            "supportedResolutions": ["480p", "720p", "1080p"],
            "defaultResolutionName": "720p",
            "includeResolution": True,
            "maxReferenceImages": 4,
        },
    },
}
MUSE_VIDEO_MODEL_IDS = set(MUSE_VIDEO_MODEL_SPECS)
SERVER_SCOPED_MUSE_VIDEO_MODEL_IDS = {
    model_id
    for model_id, spec in MUSE_VIDEO_MODEL_SPECS.items()
    if spec.get("seed") is False
}
XINGHE_SORA_VIDEO_MODEL_ALIAS_TARGETS = {
    "seedence2-fast": "sora-v3-fast",
    "seedence2-pro": "sora-v3-pro",
    "seedence2-fast（特价版1）": "sora-v3-fast",
    "seedence2-pro（特价版1）": "sora-v3-pro",
}
XINGHE_SORA_VIDEO_MODEL_IDS = {
    "sora-v3-fast",
    "sora-v3-pro",
    *XINGHE_SORA_VIDEO_MODEL_ALIAS_TARGETS.keys(),
}
XINGHE_SORA_VIDEO_PRICE_FOR_15_SECONDS = {
    "sora-v3-fast": 4.0,
    "sora-v3-pro": 5.0,
    "seedence2-fast": 4.0,
    "seedence2-pro": 5.0,
    "seedence2-fast（特价版1）": 3.0,
    "seedence2-pro（特价版1）": 4.5,
}
XINGHE_STABLE_IMAGE_PRICES = {
    "1K": {"modelPrice": 0.1, "adminPrice": 0.08},
    "2K": {"modelPrice": 0.1, "adminPrice": 0.08},
    "4K": {"modelPrice": 0.15, "adminPrice": 0.12},
}
ZEXITONGXUE_SORA_VIP3_MODEL_SPECS = {
    "sora-vip3-pro-720p": {
        "displayName": "seedence2.0-720（满血）",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "modelPrice": 8.5,
    },
    "seedence2.0-720（满血）": {
        "displayName": "seedence2.0-720（满血）",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "modelPrice": 8.5,
    },
    "sora-vip3-pro-1080p": {
        "displayName": "sora-vip3-pro-1080p",
        "supportedResolutions": ["480p", "720p", "1080p"],
        "defaultResolutionName": "1080p",
    },
}
ZEXITONGXUE_SORA_VIP3_MODEL_IDS = set(ZEXITONGXUE_SORA_VIP3_MODEL_SPECS)
ZEXITONGXUE_SORA_VIP3_MODEL_ID_ALIASES = {
    "seedence2.0-720": "seedence2.0-720（满血）",
}
ZEXITONGXUE_SORA_VIP3_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
    "supportedDurations": [5, 10, 15],
    "defaultDuration": 5,
    "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
    "defaultRatio": "16:9",
    "durationField": "seconds",
    "durationAsString": True,
    "singleImageReferenceField": "image_url",
    "multiImageReferenceField": "reference_image_urls",
    "videoReferenceField": "reference_video",
    "audioReferenceField": "audio_url",
    "supportedReferenceModes": ["image", "video_reference"],
    "defaultReferenceMode": "image",
    "defaultGenerateAudio": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 1,
    "maxReferenceAudios": 1,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
ARTIFEX_SEEDENCE20_MODEL_SPECS = {
    "seedence2.0fast-480": {
        "displayName": "seedence2.0fast-480",
        "upstreamModelName": "video-fast-480p",
        "supportedResolutions": ["480p"],
        "defaultResolutionName": "480p",
        "modelPrice": 4.5,
        "adminPrice": 4.0,
    },
    "seedence2.0fast-720": {
        "displayName": "seedence2.0fast-720",
        "upstreamModelName": "video-fast-720p",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "modelPrice": 6.5,
        "adminPrice": 6.0,
        "description": "支持 4-15 秒时长，按秒计费；适合高质量视频生成与创意内容包装",
    },
    "seedence2.0pro-480": {
        "displayName": "seedence2.0pro-480",
        "upstreamModelName": "video-pro-480p",
        "supportedResolutions": ["480p"],
        "defaultResolutionName": "480p",
        "modelPrice": 6.0,
        "adminPrice": 5.5,
    },
    "seedence2.0pro-720": {
        "displayName": "seedence2.0pro-720",
        "upstreamModelName": "video-pro-720p",
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "modelPrice": 8.0,
        "adminPrice": 7.5,
        "description": "支持 4-15 秒时长，按秒计费；适合高质量视频生成与创意内容包装",
    },
}
ARTIFEX_SEEDENCE20_MODEL_IDS = set(ARTIFEX_SEEDENCE20_MODEL_SPECS)
ARTIFEX_SEEDENCE20_PROTOCOL_BASE_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "defaultGenerateAudio": False,
    "includeResolution": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
    "referenceTokenPrefixes": {"image": "Image", "video": "Video", "audio": "Audio"},
}
SHISHIKEJI_CHANNEL4_MODEL_SPECS = {
    "seedence-fast（满血人脸渠道4）": {
        "displayName": "seedence-fast（满血人脸渠道4）",
        "upstreamModelName": "transit9-fast",
        "modelPrice": 6.0,
        "adminPrice": 5.0,
    },
    "seedence-pro（满血人脸渠道4）": {
        "displayName": "seedence-pro（满血人脸渠道4）",
        "upstreamModelName": "transit9-2.0",
        "modelPrice": 8.0,
        "adminPrice": 7.0,
    },
}
SHISHIKEJI_CHANNEL4_MODEL_IDS = set(SHISHIKEJI_CHANNEL4_MODEL_SPECS)
SHISHIKEJI_CHANNEL4_MODEL_ID_ALIASES = {
    "transit9-fast": "seedence-fast（满血人脸渠道4）",
    "transit9-2.0": "seedence-pro（满血人脸渠道4）",
    "seedence-fast-4": "seedence-fast（满血人脸渠道4）",
    "seedence-pro-4": "seedence-pro（满血人脸渠道4）",
}
SHISHIKEJI_CHANNEL4_PROTOCOL_BASE_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
LOW_PRICE_JIMENG_VIDEO_MODEL_SPECS = {
    "seedence2-fast（特价版2）": {
        "modelName": "seedence2-fast（特价版2）",
        "displayName": "seedence2-fast（特价版2）",
        "aliases": ["videos_fast"],
        "priceFor15Seconds": 2.8,
    },
    "seedence2-pro（特价版2）": {
        "modelName": "seedence2-pro（特价版2）",
        "displayName": "seedence2-pro（特价版2）",
        "aliases": ["videos"],
        "priceFor15Seconds": 3.5,
    },
}
LOW_PRICE_JIMENG_VIDEO_MODEL_IDS = set(LOW_PRICE_JIMENG_VIDEO_MODEL_SPECS)
LOW_PRICE_JIMENG_VIDEO_PRICE_FOR_15_SECONDS = {
    model_id: float(spec["priceFor15Seconds"])
    for model_id, spec in LOW_PRICE_JIMENG_VIDEO_MODEL_SPECS.items()
}
CHANNEL35_NO_AUDIO_SPECIAL_MODEL_SPECS = {
    "seedence2.0（特价版不带音频）": {
        "displayName": "seedence2.0（特价版不带音频）",
        "upstreamModelName": "videos_stable",
        "modelPrice": 4.5,
        "adminPrice": 4.0,
    },
    "seedence2.0-fast（特价版不带音频）": {
        "displayName": "seedence2.0-fast（特价版不带音频）",
        "upstreamModelName": "videos_stable_fast",
        "modelPrice": 3.8,
        "adminPrice": 3.3,
    },
}
CHANNEL35_NO_AUDIO_SPECIAL_MODEL_IDS = set(CHANNEL35_NO_AUDIO_SPECIAL_MODEL_SPECS)
CHANNEL35_NO_AUDIO_SPECIAL_MODEL_ID_ALIASES = {
    "seedence2.0(特价版不带音频)": "seedence2.0（特价版不带音频）",
    "seedence2.0-fast(特价版不带音频)": "seedence2.0-fast（特价版不带音频）",
    "seedence2.0（新特价渠道）": "seedence2.0（特价版不带音频）",
    "seedence2.0-fast（新特价渠道）": "seedence2.0-fast（特价版不带音频）",
    "seedence2.0(新特价渠道)": "seedence2.0（特价版不带音频）",
    "seedence2.0-fast(新特价渠道)": "seedence2.0-fast（特价版不带音频）",
    "seedence2-new-special-channel35": "seedence2.0（特价版不带音频）",
    "seedence2-fast-new-special-channel35": "seedence2.0-fast（特价版不带音频）",
}
CHANNEL35_NO_AUDIO_SPECIAL_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
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
CHANNEL24_SEEDANCE_RESOLUTION_PRICES = {
    "fast": {
        "480p": {"modelPrice": 6.0, "adminPrice": 5.0},
        "720p": {"modelPrice": 6.0, "adminPrice": 5.0},
        "1080p": {"modelPrice": 6.0, "adminPrice": 5.0},
    },
    "pro": {
        "480p": {"modelPrice": 8.0, "adminPrice": 7.0},
        "720p": {"modelPrice": 8.0, "adminPrice": 7.0},
        "1080p": {"modelPrice": 8.0, "adminPrice": 7.0},
    },
}
CHANNEL24_SEEDANCE_MODEL_IDS = {
    SEEDANCE_DASH_FAST_MODEL_ID,
    SEEDANCE_DASH_PRO_MODEL_ID,
    "seedence2.0-fast",
    "seedence2.0-pro",
    "seedence20-fast-gumen",
    "seedence20-pro-gumen",
    "seedence2.0（火山备用版本）",
    "seedence2.0（火山fast备用版本）",
    "seedence2.0(火山备用版本)",
    "seedence2.0(火山fast备用版本)",
}
STALE_CHANNEL24_LOCAL_SEEDANCE_MODEL_IDS = {
    "seedence20-fast-gumen",
    "seedence20-pro-gumen",
}
CHANNEL24_SEEDANCE_FAST_RESOLUTIONS = ("720p",)
CHANNEL24_SEEDANCE_PRO_RESOLUTIONS = ("480p", "720p", "1080p")
CHANNEL24_SEEDANCE_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 4,
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultRatio": "16:9",
    "supportedResolutions": list(CHANNEL24_SEEDANCE_PRO_RESOLUTIONS),
    "defaultResolutionName": "720p",
    "includeDuration": True,
    "includeResolution": True,
    "includeGenerateAudio": True,
    "defaultGenerateAudio": True,
    "durationField": "duration",
    "durationAsString": True,
    "ratioField": "aspect_ratio",
    "multiImageReferenceField": "files",
    "videoReferenceField": "videos",
    "videoReferenceAsList": True,
    "audioReferenceField": "audios",
    "audioReferenceAsList": True,
    "supportedReferenceModes": ["image", "video_reference"],
    "defaultReferenceMode": "image",
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
HONGNIAO_CHANNEL36_MODEL_SPECS = {
    "seedence2.0-h": {
        "modelName": "seedence2.0（满血过人脸h）",
        "displayName": "seedence2.0（满血过人脸h）",
        "upstreamModelName": "sdquan-2",
        "modelPrice": 8.0,
        "adminPrice": 7.5,
    },
    "seedence2.0-fast-audio": {
        "modelName": "seedence2.0-fast（特价版带音频）",
        "displayName": "seedence2.0-fast（特价版带音频）",
        "upstreamModelName": "quanneng2.0",
        "modelPrice": 4.0,
        "adminPrice": 3.5,
        "maxReferenceImages": 4,
        "modelIntroduction": "带音频，支持 4-15 秒，最多 4 张参考图和 3 个音频",
    },
    "seedence2.0-2": {
        "modelName": "seedence2.0（满血版不过人脸2）",
        "displayName": "seedence2.0（满血版不过人脸2）",
        "upstreamModelName": "quanneng-j",
        "modelPrice": 5.5,
        "adminPrice": 4.5,
    },
}
HONGNIAO_CHANNEL36_MODEL_IDS = set(HONGNIAO_CHANNEL36_MODEL_SPECS)
HONGNIAO_CHANNEL36_MODEL_ID_ALIASES = {
    spec["modelName"]: model_id
    for model_id, spec in HONGNIAO_CHANNEL36_MODEL_SPECS.items()
}
HONGNIAO_CHANNEL36_MODEL_ID_ALIASES.update({
    spec["modelName"].replace("（", "(").replace("）", ")"): model_id
    for model_id, spec in HONGNIAO_CHANNEL36_MODEL_SPECS.items()
})
HONGNIAO_CHANNEL36_PROTOCOL_PARAMS = {
    "taskType": "video_generation",
    "videoProtocol": "public_video_api",
    "supportedDurations": list(range(4, 16)),
    "defaultDuration": 10,
    "ratios": ["16:9", "9:16", "1:1"],
    "defaultRatio": "9:16",
    "supportedResolutions": ["720p"],
    "defaultResolutionName": "720p",
    "includeDuration": True,
    "includeResolution": True,
    "includeGenerateAudio": False,
    "defaultGenerateAudio": False,
    "durationField": "seconds",
    "durationAsString": True,
    "ratioField": "aspect_ratio",
    "multiImageReferenceField": "images",
    "videoReferenceField": "video_urls",
    "videoReferenceAsList": True,
    "audioReferenceField": "audio_urls",
    "audioReferenceAsList": True,
    "supportedReferenceModes": ["image", "video_reference"],
    "defaultReferenceMode": "image",
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
SPECIAL_PRICE_MODEL_ID_ALIASES = {
    "seedence2-fast-1": "seedence2-fast（特价版1）",
    "seedence2-pro-1": "seedence2-pro（特价版1）",
    "seedence2-fast-2": "seedence2-fast（特价版2）",
    "seedence2-pro-2": "seedence2-pro（特价版2）",
}
SECOND_BASED_VIDEO_MODEL_IDS = {
    *SEEDANCE_PROTOCOL_MODEL_IDS,
    SEEDANCE_DASH_PRO_MODEL_ID,
    *MUSE_VIDEO_MODEL_IDS,
    *XINGHE_SORA_VIDEO_MODEL_IDS,
    *ARTIFEX_SEEDENCE20_MODEL_IDS,
    *LOW_PRICE_JIMENG_VIDEO_MODEL_IDS,
    *CHANNEL31_FACE_MODEL_IDS,
}
MUSE_VIDEO_MODEL_NAME_TO_ID = {
    str(value).strip().lower(): model_id
    for model_id, spec in MUSE_VIDEO_MODEL_SPECS.items()
    for value in (model_id, spec["modelName"], *(spec.get("aliases") or []))
}
VEO31_FAST_MODEL_ID = "veo31-fast"
GROK3_VIDEO_MODEL_ID = "grok3-video"
GROK15_VIDEO_MODEL_ID = "grok1.5-video"
GROK15_VIDEO_UPSTREAM_MODEL_ID = "grok-imagine-video-1.5-preview"
GROK_VIDEO_MODEL_ID = "grok-imagine-video"
FIREFLY_VIDEO_ADAPTER = "yunzhi.firefly.video"
GROK15_VIDEO_SIZES = ["1024x1024", "1024x1792", "1792x1024", "1280x720", "720x1280"]
GROK15_VIDEO_RATIO_SIZE_MAP = {
    "1:1": "1024x1024",
    "9:16": "720x1280",
    "16:9": "1280x720",
    "4:7": "1024x1792",
    "7:4": "1792x1024",
}
GROK3_VIDEO_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
    "supportedDurations": [6, 10],
    "defaultDuration": 6,
    "ratios": ["16:9", "9:16", "3:2", "2:3", "1:1"],
    "defaultRatio": "16:9",
    "supportedResolutions": ["480p", "720p"],
    "defaultResolutionName": "480p",
    "multiImageReferenceField": "reference_images",
    "includePreset": True,
    "defaultPreset": "normal",
    "presets": ["fun", "normal", "spicy", "custom"],
    "maxReferenceImages": 7,
    "maxReferenceVideos": 0,
    "maxReferenceAudios": 0,
    "supportsStartEndFrames": False,
    "supportsVideoReference": False,
    "includeGenerateAudio": False,
}
GROK15_VIDEO_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "includeGenerateAudio": False,
    "singleImageReferenceField": "image_url",
    "requiresImageReference": True,
    "supportedReferenceModes": ["image"],
    "defaultReferenceMode": "image",
    "requestModelName": GROK15_VIDEO_MODEL_ID,
    "upstreamModelName": GROK15_VIDEO_UPSTREAM_MODEL_ID,
    "maxReferenceImages": 1,
    "maxReferenceVideos": 0,
    "maxReferenceAudios": 0,
    "supportsStartEndFrames": False,
    "supportsVideoReference": False,
}
GROK_VIDEO_VARIANT_SPECS = [
    {"model": "grok-imagine-1.0-video-landscape-10s", "ratio": "16:9", "seconds": 10, "size": "1280x720"},
    {"model": "grok-imagine-1.0-video-landscape[hd]-10s", "ratio": "16:9", "seconds": 10, "size": "1792x1024", "quality": "hd"},
    {"model": "grok-imagine-1.0-video-portrait-10s", "ratio": "9:16", "seconds": 10, "size": "720x1280"},
]
GROK_VIDEO_VARIANT_IDS = {item["model"] for item in GROK_VIDEO_VARIANT_SPECS}
GROK_VIDEO_DEFAULT_PARAMS = {
    "defaultDuration": 10,
    "defaultSeconds": 10,
    "supportedDurations": [10],
    "defaultSize": "1280x720",
    "sizes": ["1280x720", "720x1280", "1792x1024"],
    "ratios": ["16:9", "9:16"],
    "defaultResolutionName": "720p",
    "defaultPreset": "normal",
    "maxReferenceImages": 7,
    "upstreamModelName": GROK_VIDEO_MODEL_ID,
    "preferProviderCredentials": False,
    "grokVariants": [item["model"] for item in GROK_VIDEO_VARIANT_SPECS],
    "grokVariantSpecs": GROK_VIDEO_VARIANT_SPECS,
}
SEEDANCE_FAST_PROTOCOL_PARAMS = {
    "supportedDurations": list(range(4, 16)),
    "supportedReferenceModes": ["omni_reference", "first_last_frames"],
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultDuration": 5,
    "defaultRatio": "16:9",
    "defaultReferenceMode": "omni_reference",
    "defaultGenerateAudio": False,
    "includeResolution": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": True,
    "supportsVideoReference": True,
}
HERMES_SEEDANCE_PROTOCOL_PARAMS = {
    "videoProtocol": "hermes_video",
    "requestModelName": HERMES_SEEDANCE_REQUEST_MODEL_NAME,
    "upstreamModelName": HERMES_SEEDANCE_MODEL_ID,
    "supportedDurations": [15],
    "defaultDuration": 15,
    "ratios": ["16:9", "9:16", "1:1", "4:3", "21:9", "adaptive"],
    "defaultRatio": "16:9",
    "supportedResolutions": ["480p", "720p", "1080p"],
    "defaultResolutionName": "720p",
    "supportedReferenceModes": ["omni_reference", "image", "video_reference"],
    "defaultReferenceMode": "omni_reference",
    "includeResolution": True,
    "includeGenerateAudio": True,
    "defaultGenerateAudio": True,
    "maxReferenceImages": 9,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
    "referenceTokenPrefixes": {"image": "Image", "video": "Video", "audio": "Audio"},
}
MUSE_VIDEO_BASE_PROTOCOL_PARAMS = {
    "videoProtocol": "muse_video",
    "supportedDurations": list(range(4, 16)),
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultDuration": 5,
    "defaultRatio": "16:9",
    "defaultResolutionName": "720p",
    "includeResolution": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 9,
    "maxReferenceAudios": 3,
}
MUSE_VIDEO_V1_PROTOCOL_PARAMS = {
    **MUSE_VIDEO_BASE_PROTOCOL_PARAMS,
    "museApiVersion": "v1",
    "supportedReferenceModes": ["first_frame", "first_last_frames", "reference_image"],
    "defaultReferenceMode": "first_frame",
    "maxReferenceVideos": 0,
    "supportsStartEndFrames": True,
    "supportsVideoReference": False,
}
MUSE_VIDEO_V2_PROTOCOL_PARAMS = {
    **MUSE_VIDEO_BASE_PROTOCOL_PARAMS,
    "museApiVersion": "v2",
    "supportedReferenceModes": ["reference_image"],
    "defaultReferenceMode": "reference_image",
    "maxReferenceVideos": 3,
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
LOW_PRICE_JIMENG_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "defaultGenerateAudio": False,
    "includeGenerateAudio": False,
    "maxReferenceImages": 4,
    "maxReferenceVideos": 3,
    "maxReferenceAudios": 0,
    "supportsStartEndFrames": True,
    "supportsVideoReference": True,
}
VEO31_FAST_PROTOCOL_PARAMS = {
    "taskType": "video_generation",
    "supportedDurations": [4, 6, 8],
    "defaultDuration": 4,
    "ratios": ["16:9", "9:16"],
    "supportedResolutions": ["720p", "1080p"],
    "supportedReferenceModes": ["frame", "image"],
    "defaultReferenceMode": "frame",
    "defaultGenerateAudio": True,
    "maxReferenceImages": 1,
    "supportsStartEndFrames": False,
    "supportsVideoReference": False,
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
OTU_VEO_FL_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
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
}
OTU_OMNI_FLASH_PROTOCOL_PARAMS = {
    "videoProtocol": "public_video_api",
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
}
PUBLIC_VIDEO_PROTOCOL_PARAMS: Dict[str, Dict[str, Any]] = {
    GROK3_VIDEO_MODEL_ID: {**GROK3_VIDEO_PROTOCOL_PARAMS},
    GROK15_VIDEO_MODEL_ID: {**GROK15_VIDEO_PROTOCOL_PARAMS},
    "kling-video-3.0": {
        "videoProtocol": "public_video_api",
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["1:1", "16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "720p",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": True,
        "maxReferenceImages": 1,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "kling-video-o3-omni": {
        "videoProtocol": "public_video_api",
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["1:1", "16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "720p",
        "supportedReferenceModes": ["image", "first_last_frames", "video_reference"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": True,
        "maxReferenceImages": 7,
        "maxReferenceVideos": 1,
        "supportsStartEndFrames": True,
        "supportsVideoReference": True,
    },
    "sora2": {
        "videoProtocol": "public_video_api",
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "includeResolution": False,
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": False,
        "includeGenerateAudio": False,
        "imageReferenceField": "input_reference",
        "maxReferenceImages": 1,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "veo_3_1-fl": {**OTU_VEO_FL_PROTOCOL_PARAMS},
    "veo_3_1-fast-fl": {**OTU_VEO_FL_PROTOCOL_PARAMS},
    "omni_flash-10s": {**OTU_OMNI_FLASH_PROTOCOL_PARAMS},
    "sora-v3-pro": {
        "videoProtocol": "public_video_api",
        "supportedDurations": list(range(5, 16)),
        "defaultDuration": 10,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": False,
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "sora-v3-fast": {
        "videoProtocol": "public_video_api",
        "supportedDurations": list(range(5, 16)),
        "defaultDuration": 10,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p"],
        "defaultResolutionName": "720p",
        "durationField": "seconds",
        "durationAsString": True,
        "singleImageReferenceField": "image_url",
        "multiImageReferenceField": "reference_image_urls",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": False,
        "includeGenerateAudio": False,
        "maxReferenceImages": 4,
        "maxReferenceVideos": 0,
        "maxReferenceAudios": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    SEEDANCE_DASH_PRO_MODEL_ID: {
        "videoProtocol": "public_video_api",
        "supportedDurations": list(range(4, 16)),
        "defaultDuration": 5,
        "ratios": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
        "defaultRatio": "auto",
        "supportedResolutions": ["480p", "720p", "1080p"],
        "defaultResolutionName": "720p",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "defaultGenerateAudio": True,
        "includeGenerateAudio": True,
        "includeSeed": True,
        "ratioField": "ratio",
        "multiImageReferenceField": "images",
        "maxReferenceImages": 9,
        "maxReferenceAudios": 9,
        "maxReferenceVideos": 0,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    VEO31_FAST_MODEL_ID: {
        "videoProtocol": "newapi_veo_json",
        **VEO31_FAST_PROTOCOL_PARAMS,
    },
}

PUBLIC_VIDEO_PROTOCOL_PARAMS[SEEDANCE_DASH_FAST_MODEL_ID] = {
    **PUBLIC_VIDEO_PROTOCOL_PARAMS[SEEDANCE_DASH_PRO_MODEL_ID],
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "defaultRatio": "16:9",
}

CHANNEL31_FACE_PROTOCOL_BASE_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "supportedReferenceModes": ["image"],
    "defaultReferenceMode": "image",
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
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    CHANNEL31_FACE_PRO_MODEL_ID: {
        **CHANNEL31_FACE_PROTOCOL_BASE_PARAMS,
    },
    CHANNEL31_FACE_FAST_MODEL_ID: {
        **CHANNEL31_FACE_PROTOCOL_BASE_PARAMS,
        "supportedResolutions": ["480p", "720p"],
    },
})

CHANNEL27_911_SEEDANCE_MODEL_SPECS = {
    "seedance-2.0-16-9-720-15": {
        "requestModelName": "seedance-2.0+16:9+720+15",
        "ratio": "16:9",
    },
    "seedance-2.0-9-16-720-15": {
        "requestModelName": "seedance-2.0+9:16+720+15",
        "ratio": "9:16",
    },
}
CHANNEL27_911_SEEDANCE_MODEL_IDS = set(CHANNEL27_911_SEEDANCE_MODEL_SPECS)
CHANNEL27_911_SEEDANCE_MODEL_ID_ALIASES = {
    "seedence-2.0-16-9-720-15": "seedance-2.0-16-9-720-15",
    "seedence-2.0-9-16-720-15": "seedance-2.0-9-16-720-15",
    "seedence-2.0+16:9+720+15": "seedance-2.0-16-9-720-15",
    "seedence-2.0+9:16+720+15": "seedance-2.0-9-16-720-15",
}
CHANNEL27_911_SEEDANCE_PRICE_FOR_15_SECONDS = 7.5
CHANNEL27_911_SEEDANCE_ADMIN_PRICE_FOR_15_SECONDS = 6.5
CHANNEL27_911_SEEDANCE_MODEL_INTRODUCTION = "按次计费，固定 720P / 15秒；适合高质量视频生成与创意内容包装"
CHANNEL27_911_SEEDANCE_PROTOCOL_BASE_PARAMS = {
    "videoProtocol": "public_video_api",
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
    "supportsStartEndFrames": False,
    "supportsVideoReference": True,
}
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **CHANNEL27_911_SEEDANCE_PROTOCOL_BASE_PARAMS,
        "requestModelName": spec["requestModelName"],
        "ratios": [spec["ratio"]],
        "defaultRatio": spec["ratio"],
    }
    for model_id, spec in CHANNEL27_911_SEEDANCE_MODEL_SPECS.items()
})

OTU_PUBLIC_VIDEO_MODEL_ALIAS_TARGETS = {
    "veo3.1": "veo_3_1-fl",
    "veo3.1-fast": "veo_3_1-fast-fl",
    "google-omin": "omni_flash-10s",
}
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    alias_id: {**PUBLIC_VIDEO_PROTOCOL_PARAMS[target_id]}
    for alias_id, target_id in OTU_PUBLIC_VIDEO_MODEL_ALIAS_TARGETS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    alias_id: {**PUBLIC_VIDEO_PROTOCOL_PARAMS[target_id]}
    for alias_id, target_id in XINGHE_SORA_VIDEO_MODEL_ALIAS_TARGETS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **ZEXITONGXUE_SORA_VIP3_PROTOCOL_PARAMS,
        "supportedResolutions": list(spec["supportedResolutions"]),
        "defaultResolutionName": spec["defaultResolutionName"],
    }
    for model_id, spec in ZEXITONGXUE_SORA_VIP3_MODEL_SPECS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **ARTIFEX_SEEDENCE20_PROTOCOL_BASE_PARAMS,
        "supportedResolutions": list(spec["supportedResolutions"]),
        "defaultResolutionName": spec["defaultResolutionName"],
        "upstreamModelName": spec["upstreamModelName"],
    }
    for model_id, spec in ARTIFEX_SEEDENCE20_MODEL_SPECS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **SHISHIKEJI_CHANNEL4_PROTOCOL_BASE_PARAMS,
        "upstreamModelName": spec["upstreamModelName"],
    }
    for model_id, spec in SHISHIKEJI_CHANNEL4_MODEL_SPECS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {**LOW_PRICE_JIMENG_PROTOCOL_PARAMS}
    for model_id in LOW_PRICE_JIMENG_VIDEO_MODEL_IDS
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **CHANNEL35_NO_AUDIO_SPECIAL_PROTOCOL_PARAMS,
        "requestModelName": model_id,
        "upstreamModelName": spec["upstreamModelName"],
    }
    for model_id, spec in CHANNEL35_NO_AUDIO_SPECIAL_MODEL_SPECS.items()
})
PUBLIC_VIDEO_PROTOCOL_PARAMS.update({
    model_id: {
        **HONGNIAO_CHANNEL36_PROTOCOL_PARAMS,
        "upstreamModelName": spec["upstreamModelName"],
        **({"maxReferenceImages": spec["maxReferenceImages"]} if "maxReferenceImages" in spec else {}),
    }
    for model_id, spec in HONGNIAO_CHANNEL36_MODEL_SPECS.items()
})
FIREFLY_VIDEO_PROTOCOL_PARAMS: Dict[str, Dict[str, Any]] = {
    "firefly-sora2": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 6,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "firefly-sora2-pro": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": [4, 8, 12],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 6,
        "supportsStartEndFrames": False,
        "supportsVideoReference": False,
    },
    "firefly-veo31": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image", "first_last_frames"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "firefly-veo31-fast": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image", "first_last_frames"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "firefly-veo31-ref": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": [4, 6, 8],
        "defaultDuration": 4,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image", "first_last_frames"],
        "defaultReferenceMode": "first_last_frames",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "firefly-kling30omni": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image", "first_last_frames"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 7,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
    "firefly-kling30": {
        "videoProtocol": "yunzhi_firefly_chat",
        "supportedDurations": list(range(3, 16)),
        "defaultDuration": 5,
        "ratios": ["16:9", "9:16"],
        "supportedResolutions": ["720p", "1080p"],
        "defaultResolutionName": "1080p",
        "supportedReferenceModes": ["image", "first_last_frames"],
        "defaultReferenceMode": "image",
        "maxReferenceImages": 2,
        "supportsStartEndFrames": True,
        "supportsVideoReference": False,
    },
}
FIREFLY_VIDEO_MODEL_IDS = set(FIREFLY_VIDEO_PROTOCOL_PARAMS)
NANOBANANA_PROTOCOL_MODEL_IDS = {"nanobanana2", "nanobananapro"}
NANOBANANA_IMAGE_PARAMS = {
    "ratios": ["21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "8:1", "4:1", "1:4", "1:8"],
    "resolutions": ["1K", "2K", "4K"],
    "maxReferenceImages": 6,
    "responseFormat": "url",
    "requestTimeoutSeconds": 1200,
}
PUBLIC_PROVIDER_IDS = {NEWAPI_PROVIDER_ID, GHOSTCUT_PROVIDER_ID}
ACTIVE_PROVIDER_IDS = {LOCAL_UPSCALE_PROVIDER_ID, NEWAPI_PROVIDER_ID, GHOSTCUT_PROVIDER_ID}
INTERNAL_PROVIDER_MODEL_IDS = {LOCAL_UPSCALE_MODEL_ID}
DEFAULT_PUBLIC_PROVIDER_MODEL_IDS = {"gpt-5.5", "gpt-image-2", IMAGE_ANALYZE_MODEL_ID}
# NewAPI account availability is the source of truth for the software catalog.
# Built-in adapter support alone must not make a model visible to users.
ALWAYS_AVAILABLE_NEWAPI_MODEL_IDS = set()
REMOVED_PROVIDER_MODEL_IDS = {
    SEEDANCE_FAST_MODEL_ID,
    SEEDANCE_PRO_MODEL_ID,
    "seedence2-fast（残血版）",
    "seedence2-pro（残血版）",
    "seedence2-fast（特价版）",
    "seedence2-pro（特价版）",
}
MAIN_ACCOUNT_GROK_MODEL_IDS = {GROK_VIDEO_MODEL_ID, GROK15_VIDEO_MODEL_ID, *GROK_VIDEO_VARIANT_IDS}
PUBLIC_MODEL_CAPABILITIES = {"text.generate", "text.reason", "inference.generate", "image.analyze", "image.generate", "video.generate", "video.subtitle.remove"}
TEXT_MODEL_CAPABILITIES = {"text.generate", "text.reason", "inference.generate", "image.analyze"}
CAPABILITY_DEFAULT_ADAPTERS = {
    "text.generate": "openai.responses",
    "text.reason": "openai.responses",
    "inference.generate": "openai.responses",
    "image.analyze": "openai.responses",
    "image.generate": "openai.image",
    "video.generate": "newapi.video",
    "video.subtitle.remove": GHOSTCUT_SUBTITLE_ADAPTER,
}
CAPABILITY_ALLOWED_ADAPTERS = {
    **{capability: {adapter} for capability, adapter in CAPABILITY_DEFAULT_ADAPTERS.items()},
    "video.generate": {"newapi.video", "grok2api.video", FIREFLY_VIDEO_ADAPTER},
    "video.subtitle.remove": {GHOSTCUT_SUBTITLE_ADAPTER},
}
SUPPORTED_PROVIDER_ADAPTERS = {
    adapter
    for adapters in CAPABILITY_ALLOWED_ADAPTERS.values()
    for adapter in adapters
}
MODEL_ID_ALIASES = {
    f"{NEWAPI_PROVIDER_ID}.gpt-5-5": "gpt-5.5",
    "gpt-5-5": "gpt-5.5",
    "gpt-5.5": "gpt-5.5",
    "veo31fast": "veo31-fast",
    "veo-3.1-fast": "veo31-fast",
    IMAGE_ANALYZE_MODEL_ID: IMAGE_ANALYZE_MODEL_ID,
    f"{NEWAPI_PROVIDER_ID}.{IMAGE_ANALYZE_MODEL_ID}": IMAGE_ANALYZE_MODEL_ID,
    f"{NEWAPI_PROVIDER_ID}.gpt-image-2": "gpt-image-2",
    "gpt-image-2": "gpt-image-2",
    "nanobanana2": "nanobanana2",
    "nano-banana2": "nanobanana2",
    "nano-banana-2": "nanobanana2",
    "nanobananapro": "nanobananapro",
    "nanobanana-pro": "nanobananapro",
    "nano-banana-pro": "nanobananapro",
    "nanobanana pro": "nanobananapro",
    "nano banana pro": "nanobananapro",
    **SPECIAL_PRICE_MODEL_ID_ALIASES,
    **ZEXITONGXUE_SORA_VIP3_MODEL_ID_ALIASES,
    **CHANNEL27_911_SEEDANCE_MODEL_ID_ALIASES,
    **CHANNEL35_NO_AUDIO_SPECIAL_MODEL_ID_ALIASES,
    **SHISHIKEJI_CHANNEL4_MODEL_ID_ALIASES,
    **CHANNEL31_FACE_MODEL_ID_ALIASES,
    **HERMES_SEEDANCE_MODEL_ID_ALIASES,
    **MUSE_VIDEO_MODEL_NAME_TO_ID,
}
OLD_PROVIDER_ID = "yun" + "wu"
JIANYING_DRAFT_MARKERS = ("draft_content.json", "draft_meta_info.json")
JIANYING_DRAFT_DIR_NAMES = (
    "JianyingPro Drafts",
    "JianyingProDrafts",
    "Jianying Drafts",
    "JianyingDrafts",
    "剪映专业版草稿",
    "剪映草稿",
)
JIANYING_TEMPLATE_HINTS = ("template", "templates", "resource", "resources", "sample", "samples", "demo", "example")
JIANYING_TEMPLATE_HINTS_CN = ("模板", "样例", "示例", "范例")
JIANYING_SKIP_DIR_NAMES = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
}
JIANYING_MAX_SCAN_DEPTH = 4
JIANYING_MAX_SCAN_DIRS = 8000


def default_project_storage_dir() -> Path:
    return DATA_DIR / "projects"


def read_settings_file() -> Dict[str, Any]:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def write_settings_file(settings: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_file(SETTINGS_PATH, settings)


def design_template_name_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    return re.sub(r"^\d+[-_ ]*", "", stem).strip() or stem or "模板"


def design_template_id_from_name(name: str) -> str:
    aliases = {
        "表情包": "expression",
        "三视图": "three-view",
        "角色拆分": "character-split",
        "线稿色卡": "color-card",
        "角色设计拆分": "design-split",
        "超级拆分": "super-split",
    }
    return aliases.get(name, re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower() or "template")


def parse_design_prompt_template_markdown(markdown: str) -> str:
    text = str(markdown or "").lstrip("\ufeff").strip()
    if not text:
        return ""
    fenced = re.search(r"```(?:text|txt|prompt)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    lines = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if index == 0 and stripped.startswith("# "):
            continue
        if stripped.startswith("来源："):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def list_design_prompt_templates() -> Dict[str, Any]:
    directory = DESIGN_PROMPT_TEMPLATE_DIR
    if not directory.exists() or not directory.is_dir():
        return {
            "templates": DESIGN_PROMPT_TEMPLATE_FALLBACKS,
            "usingFallback": True,
            "error": f"模板目录不存在：{directory}",
        }

    templates = []
    for path in sorted(directory.glob("*.md"), key=lambda item: item.name):
        try:
            content = parse_design_prompt_template_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            content = ""
        if not content:
            continue
        name = design_template_name_from_filename(path.name)
        templates.append({
            "id": design_template_id_from_name(name),
            "name": name,
            "sourcePath": str(path),
            "content": content,
        })

    return {
        "templates": templates or DESIGN_PROMPT_TEMPLATE_FALLBACKS,
        "usingFallback": len(templates) == 0,
        "error": "" if templates else f"模板目录为空：{directory}",
    }


def configured_project_storage_dir() -> Path:
    configured = os.environ.get("LIBAI_PROJECT_STORAGE_DIR")
    if configured:
        return Path(configured).expanduser()
    configured = read_settings_file().get("projectStorageDir")
    if configured:
        return Path(configured).expanduser()
    return default_project_storage_dir()


PROJECTS_DIR = configured_project_storage_dir()


def set_project_storage_dir(path: Path) -> None:
    global PROJECTS_DIR
    PROJECTS_DIR = path


def configured_jianying_drafts_root() -> str:
    settings = read_settings_file()
    value = (
        os.environ.get("JIANYING_DRAFTS_ROOT")
        or settings.get("jianyingDraftsRoot")
        or settings.get("jianying_drafts_root")
        or ""
    )
    return str(Path(value).expanduser().resolve()) if value else ""


def set_jianying_drafts_root(path: str) -> None:
    clean = str(Path(path).expanduser().resolve()) if path else ""
    if clean:
        os.environ["JIANYING_DRAFTS_ROOT"] = clean
    else:
        os.environ.pop("JIANYING_DRAFTS_ROOT", None)


def _normalize_jianying_dir_name(name: str) -> str:
    return name.replace(" ", "").lower()


JIANYING_DRAFT_DIR_NAMES_NORMALIZED = {_normalize_jianying_dir_name(name) for name in JIANYING_DRAFT_DIR_NAMES}


def _is_jianying_dir_name(name: str) -> bool:
    return _normalize_jianying_dir_name(name) in JIANYING_DRAFT_DIR_NAMES_NORMALIZED


def _should_skip_jianying_scan_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    return name in JIANYING_SKIP_DIR_NAMES


def _name_has_jianying_hint(name: str) -> bool:
    lower = name.lower()
    return "jianying" in lower or "剪映" in name


def _path_has_template_hint(path: Path) -> bool:
    for part in path.parts:
        lower = part.lower()
        if any(hint in lower for hint in JIANYING_TEMPLATE_HINTS):
            return True
        if any(hint in part for hint in JIANYING_TEMPLATE_HINTS_CN):
            return True
    return False


def _count_jianying_draft_children(root: Path) -> int:
    try:
        count = 0
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if any((entry / marker).exists() for marker in JIANYING_DRAFT_MARKERS):
                count += 1
        return count
    except Exception:
        return 0


def _score_jianying_root(path: Path) -> int:
    score = 0
    name = path.name
    name_lower = name.lower()
    if _is_jianying_dir_name(name):
        score += 200
    if "draft" in name_lower or "草稿" in name:
        score += 20
    if _name_has_jianying_hint(name):
        score += 40
    if _path_has_template_hint(path):
        score -= 200
    direct_markers = any((path / marker).exists() for marker in JIANYING_DRAFT_MARKERS)
    if direct_markers and not _is_jianying_dir_name(name):
        score -= 20
    elif direct_markers:
        score += 20
    score += min(_count_jianying_draft_children(path), 10) * 10
    return score


def _prefer_jianying_candidate(candidate: str, best: str) -> bool:
    if not best:
        return True
    candidate_path = Path(candidate)
    best_path = Path(best)
    if _is_jianying_dir_name(candidate_path.name) and not _is_jianying_dir_name(best_path.name):
        return True
    return len(candidate) < len(best)


def is_jianying_drafts_root(path: str) -> bool:
    if not path:
        return False
    try:
        root = Path(path).expanduser()
        if not root.exists() or not root.is_dir():
            return False
        if _path_has_template_hint(root) and not _is_jianying_dir_name(root.name):
            return False
        if _is_jianying_dir_name(root.name):
            return True
        if any((root / marker).exists() for marker in JIANYING_DRAFT_MARKERS):
            return True
        for entry in root.iterdir():
            if entry.is_dir() and any((entry / marker).exists() for marker in JIANYING_DRAFT_MARKERS):
                return True
        return False
    except Exception:
        return False


def _iter_jianying_search_roots() -> List[str]:
    roots: List[str] = []
    seen: set[str] = set()

    def add_path(path: Optional[str]) -> None:
        if not path:
            return
        try:
            normalized = os.path.normcase(os.path.abspath(path))
        except Exception:
            normalized = path
        if normalized in seen:
            return
        if os.path.isdir(path):
            roots.append(path)
            seen.add(normalized)

    env_candidates = [
        os.environ.get("USERPROFILE"),
        os.environ.get("HOME"),
        os.path.expanduser("~"),
        os.environ.get("ONEDRIVE"),
        os.environ.get("ONEDRIVECONSUMER"),
        os.environ.get("ONEDRIVECOMMERCIAL"),
        os.environ.get("PUBLIC"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
    ]
    for candidate in env_candidates:
        add_path(candidate)
        if candidate:
            for extra_name in ("Documents", "Desktop", "Downloads", "Videos", "Pictures"):
                add_path(os.path.join(candidate, extra_name))
            add_path(os.path.join(candidate, "AppData", "Local"))

    for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        add_path(f"{drive_letter}:\\")
    return roots


def _is_drive_root(path: Path) -> bool:
    try:
        return path.parent == path and bool(path.drive)
    except Exception:
        return False


def _scan_for_jianying_root(base: Path, max_depth: int, max_dirs: int) -> tuple[str, int]:
    if not base.exists() or not base.is_dir():
        return "", -10_000

    queue: deque[tuple[Path, int]] = deque([(base, 0)])
    visited = 0
    best_path = ""
    best_score = -10_000

    def consider(path: Path) -> None:
        nonlocal best_path, best_score
        if not is_jianying_drafts_root(str(path)):
            return
        score = _score_jianying_root(path)
        if score > best_score or (score == best_score and _prefer_jianying_candidate(str(path), best_path)):
            best_path = str(path)
            best_score = score

    while queue and visited < max_dirs:
        current, depth = queue.popleft()
        visited += 1
        if _should_skip_jianying_scan_dir(current.name):
            continue
        if _is_jianying_dir_name(current.name) or _name_has_jianying_hint(current.name):
            consider(current)
        try:
            entries = list(current.iterdir())
        except Exception:
            continue
        for entry in entries:
            if entry.is_dir() and not _should_skip_jianying_scan_dir(entry.name):
                if _is_jianying_dir_name(entry.name) or _name_has_jianying_hint(entry.name):
                    consider(entry)
            elif entry.is_file() and entry.name in JIANYING_DRAFT_MARKERS:
                consider(current)
                if current.parent != current:
                    consider(current.parent)
        if depth >= max_depth:
            continue
        prioritized: List[Path] = []
        normal: List[Path] = []
        for entry in entries:
            if not entry.is_dir() or _should_skip_jianying_scan_dir(entry.name):
                continue
            if _name_has_jianying_hint(entry.name):
                prioritized.append(entry)
            else:
                normal.append(entry)
        for entry in prioritized:
            queue.append((entry, depth + 1))
        for entry in normal:
            queue.append((entry, depth + 1))

    return best_path, best_score


def find_jianying_drafts_root() -> str:
    env_root = configured_jianying_drafts_root()
    if is_jianying_drafts_root(env_root):
        return env_root

    for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive_root = Path(f"{drive_letter}:\\")
        if not drive_root.exists():
            continue
        for name in JIANYING_DRAFT_DIR_NAMES:
            candidate = drive_root / "jianyingcaogao" / name
            if is_jianying_drafts_root(str(candidate)):
                return str(candidate)

    roots = _iter_jianying_search_roots()
    best_path = ""
    best_score = -10_000
    for base in roots:
        for name in JIANYING_DRAFT_DIR_NAMES:
            candidate = Path(base) / name
            if is_jianying_drafts_root(str(candidate)):
                score = _score_jianying_root(candidate)
                if score > best_score or (score == best_score and _prefer_jianying_candidate(str(candidate), best_path)):
                    best_path = str(candidate)
                    best_score = score
    for base in roots:
        base_path = Path(base)
        max_depth = JIANYING_MAX_SCAN_DEPTH - 1 if _is_drive_root(base_path) else JIANYING_MAX_SCAN_DEPTH
        found, score = _scan_for_jianying_root(base_path, max_depth=max_depth, max_dirs=JIANYING_MAX_SCAN_DIRS)
        if found and (score > best_score or (score == best_score and _prefer_jianying_candidate(found, best_path))):
            best_path = found
            best_score = score
    return best_path


def jianying_path_status(path: str) -> Dict[str, Any]:
    clean = str(Path(path).expanduser().resolve()) if path else ""
    exists = bool(clean and os.path.isdir(clean))
    return {
        "path": clean,
        "exists": exists,
        "writable": bool(exists and os.access(clean, os.W_OK)),
        "valid": bool(exists and is_jianying_drafts_root(clean)),
    }


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS nodes (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              type TEXT NOT NULL,
              title TEXT,
              x REAL NOT NULL DEFAULT 0,
              y REAL NOT NULL DEFAULT 0,
              w REAL NOT NULL DEFAULT 320,
              h REAL NOT NULL DEFAULT 220,
              params_json TEXT NOT NULL DEFAULT '{}',
              state_json TEXT NOT NULL DEFAULT '{}',
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS edges (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              from_node_id TEXT NOT NULL,
              to_node_id TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              thumb_path TEXT,
              mime TEXT,
              size INTEGER NOT NULL DEFAULT 0,
              meta_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              node_id TEXT,
              type TEXT NOT NULL,
              status TEXT NOT NULL,
              progress INTEGER NOT NULL DEFAULT 0,
              input_json TEXT NOT NULL DEFAULT '{}',
              output_json TEXT NOT NULL DEFAULT '{}',
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS history (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              action TEXT NOT NULL,
              target_id TEXT,
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS prompts (
              id TEXT PRIMARY KEY,
              project_id TEXT,
              scope TEXT NOT NULL DEFAULT 'global',
              category TEXT NOT NULL DEFAULT 'general',
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              tags_json TEXT NOT NULL DEFAULT '[]',
              meta_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS providers (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              base_url TEXT NOT NULL,
              auth_type TEXT NOT NULL DEFAULT 'bearer',
              api_key_encrypted TEXT,
              enabled INTEGER NOT NULL DEFAULT 0,
              capabilities_json TEXT NOT NULL DEFAULT '[]',
              meta_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_models (
              id TEXT PRIMARY KEY,
              provider_id TEXT NOT NULL,
              capability TEXT NOT NULL,
              model_name TEXT NOT NULL,
              display_name TEXT NOT NULL,
              adapter TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              params_json TEXT NOT NULL DEFAULT '{}',
              meta_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS newapi_accounts (
              id TEXT PRIMARY KEY,
              base_url TEXT NOT NULL,
              user_id INTEGER,
              username TEXT,
              role INTEGER,
              group_name TEXT,
              access_token_encrypted TEXT,
              default_api_key_encrypted TEXT,
              meta_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        ensure_column(conn, "projects", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        seed_provider_defaults(conn)
        conn.commit()
    sync_provider_files()
    recover_projects_from_storage_files()
    recover_interrupted_jobs()


def recover_interrupted_jobs() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error = 'Application closed before the job finished.',
                updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (utc_now(),),
        )
        conn.commit()


def validate_project_id(project_id: str) -> str:
    if not project_id or not SAFE_PROJECT_ID.match(project_id):
        raise HTTPException(status_code=400, detail="Invalid project id")
    return project_id


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / validate_project_id(project_id)


def is_global_asset_project(project_id: str) -> bool:
    return project_id == GLOBAL_ASSET_PROJECT_ID


def asset_scope_dir(project_id: str) -> Path:
    project_id = validate_project_id(project_id)
    if is_global_asset_project(project_id):
        return DATA_DIR / "global-assets"
    return project_dir(project_id)


def ensure_project_dirs(project_id: str) -> None:
    root = asset_scope_dir(project_id)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "thumbs").mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(parents=True, exist_ok=True)


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def project_metadata_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    if not row or "metadata_json" not in row.keys():
        return {}
    metadata = json_load(row["metadata_json"], {})
    return metadata if isinstance(metadata, dict) else {}


def design_space_package_from_graph(graph: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = graph.get("designSpacePackage")
    if isinstance(raw, dict):
        return raw
    legacy = graph.get("designSpace")
    if isinstance(legacy, dict) and isinstance(legacy.get("package"), dict):
        return legacy["package"]
    return None


def truthy_flag(value: Any) -> Optional[bool]:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def first_truthy_flag(*values: Any) -> Optional[bool]:
    for value in values:
        parsed = truthy_flag(value)
        if parsed is not None:
            return parsed
    return None


def is_asset_library_record(asset: Optional[Dict[str, Any]]) -> bool:
    if not asset:
        return False
    meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    explicit = first_truthy_flag(
        asset.get("inLibrary"),
        asset.get("libraryAsset"),
        asset.get("savedToLibrary"),
        meta.get("inLibrary"),
        meta.get("libraryAsset"),
        metadata.get("inLibrary"),
        metadata.get("libraryAsset"),
    )
    if explicit is not None:
        return explicit

    source = str(asset.get("source") or meta.get("source") or metadata.get("source") or "").strip().lower()
    if not source:
        return False
    if source in MANUAL_LIBRARY_SOURCES:
        return True
    if source.startswith("global."):
        return True
    if source.startswith("job."):
        return False
    if any(marker in source for marker in AUTO_OUTPUT_SOURCES):
        return False
    return False


def ensure_graph_asset_library_meta(asset: Dict[str, Any]) -> Dict[str, Any]:
    record = dict(asset or {})
    if truthy_flag(record.get("inLibrary")) is None and truthy_flag(record.get("libraryAsset")) is None:
        record["inLibrary"] = True
        record["libraryAsset"] = True
    if not record.get("source"):
        record["source"] = "saved"
    return record


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    backup_path = path.with_name(f"{path.name}.bak")
    last_error: Optional[PermissionError] = None
    for attempt in range(12):
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(text, encoding="utf-8")
            if path.exists():
                try:
                    shutil.copy2(path, backup_path)
                except PermissionError:
                    # The JSON mirror is secondary to SQLite. A locked backup source
                    # should not block replacing the current snapshot.
                    pass
            os.replace(temp_path, path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(min(0.05 * (attempt + 1), 0.5))
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
    if last_error is not None:
        raise last_error


def encode_secret(value: Optional[str]) -> str:
    return encode_secret_value(value)


def decode_secret(value: Optional[str]) -> str:
    return decode_secret_value(value)


def row_provider(row: sqlite3.Row, include_secret: bool = False) -> Dict[str, Any]:
    record = {
        "id": row["id"],
        "name": row["name"],
        "baseUrl": row["base_url"],
        "authType": row["auth_type"],
        "enabled": bool(row["enabled"]),
        "capabilities": json_load(row["capabilities_json"], []),
        "meta": json_load(row["meta_json"], {}),
        "hasApiKey": bool(row["api_key_encrypted"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_secret:
        record["apiKey"] = decode_secret(row["api_key_encrypted"])
    return record


def row_provider_model(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "providerId": row["provider_id"],
        "capability": row["capability"],
        "modelName": row["model_name"],
        "displayName": row["display_name"],
        "adapter": row["adapter"],
        "enabled": bool(row["enabled"]),
        "params": json_load(row["params_json"], {}),
        "meta": json_load(row["meta_json"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_prompt(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "scope": row["scope"],
        "category": row["category"],
        "title": row["title"],
        "content": row["content"],
        "tags": json_load(row["tags_json"], []),
        "meta": json_load(row["meta_json"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_newapi_account(row: Optional[sqlite3.Row], include_secret: bool = False) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    meta = json_load(row["meta_json"], {})
    default_key = decode_secret(row["default_api_key_encrypted"])
    default_key_usable = is_usable_newapi_api_key(default_key)
    record = {
        "id": row["id"],
        "baseUrl": row["base_url"],
        "userId": row["user_id"],
        "username": row["username"],
        "role": row["role"],
        "group": row["group_name"],
        "status": "connected" if row["access_token_encrypted"] else "disconnected",
        "hasAccessToken": bool(row["access_token_encrypted"]),
        "hasDefaultApiKey": default_key_usable,
        "defaultApiKeyUsable": default_key_usable,
        "defaultApiKeyPreview": default_key[:10] + "..." if default_key else "",
        "user": meta.get("user") or {},
        "availableModelCount": int(meta.get("availableModelCount") or 0),
        "tokenCount": int(meta.get("tokenCount") or 0),
        "lastRefreshAt": meta.get("lastRefreshAt"),
        "meta": meta,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_secret:
        record["accessToken"] = decode_secret(row["access_token_encrypted"])
        record["defaultApiKey"] = decode_secret(row["default_api_key_encrypted"])
    return record


def row_project(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "version": row["version"],
        "path": str(project_dir(row["id"])),
    }


def row_history(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json_load(row["payload_json"], {})
    return {
        **payload,
        "id": row["id"],
        "projectId": row["project_id"],
        "project_id": row["project_id"],
        "action": row["action"],
        "targetId": row["target_id"],
        "createdAt": payload.get("createdAt") or row["created_at"],
    }


def row_job(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "nodeId": row["node_id"],
        "type": row["type"],
        "status": row["status"],
        "progress": row["progress"],
        "input": json_load(row["input_json"], {}),
        "output": json_load(row["output_json"], {}),
        "error": row["error"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def history_media_kind(item: Dict[str, Any]) -> str:
    explicit = str(item.get("kind") or item.get("mediaKind") or "").strip().lower()
    if explicit in {"image", "video", "audio", "text"}:
        return explicit
    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "type",
            "action",
            "capability",
            "mime",
            "source",
            "assetUrl",
            "url",
            "src",
            "path",
        )
    ).lower()
    if "video" in text or re.search(r"\.(mp4|mov|webm|mkv|avi|m4v)(?:$|\?)", text):
        return "video"
    if "audio" in text or re.search(r"\.(mp3|wav|m4a|flac|ogg|aac)(?:$|\?)", text):
        return "audio"
    if "text" in text or "reason" in text or "inference" in text:
        return "text"
    return "image"


def history_status(item: Dict[str, Any]) -> str:
    raw = str(item.get("status") or "").strip().lower()
    if raw in {"queued", "running", "processing", "generating"}:
        return "generating"
    if raw in {"success", "completed"}:
        return "completed"
    if raw in {"cancelled", "canceled"}:
        return "canceled"
    if raw in {"error", "failed"}:
        return "failed"
    return raw or "completed"


def sanitize_newapi_user(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source = user or {}
    try:
        inviter_id = int(source.get("inviter_id") or source.get("inviterId") or 0)
    except (TypeError, ValueError):
        inviter_id = 0
    return {
        "id": source.get("id"),
        "username": source.get("username") or source.get("display_name") or "",
        "role": source.get("role"),
        "group": source.get("group") or source.get("group_name") or "",
        "affCode": source.get("aff_code") or source.get("affCode") or "",
        "inviterId": inviter_id,
        "quota": int(source.get("quota") or 0),
        "usedQuota": int(source.get("used_quota") or source.get("usedQuota") or 0),
        "requestCount": int(source.get("request_count") or source.get("requestCount") or 0),
        "status": source.get("status"),
    }


def newapi_token_response(tokens: List[Any]) -> List[Dict[str, Any]]:
    return [token.to_dict(include_key=False) for token in tokens]


def normalize_newapi_api_key(value: str) -> str:
    text = (value or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if text and not text.startswith("sk-"):
        text = f"sk-{text}"
    return text


def is_masked_newapi_api_key(value: str) -> bool:
    text = (value or "").strip()
    return "*" in text or "…" in text


def is_usable_newapi_api_key(value: str) -> bool:
    text = normalize_newapi_api_key(value)
    return bool(text) and not is_masked_newapi_api_key(text)


def require_usable_newapi_api_key(value: str) -> str:
    api_key = normalize_newapi_api_key(value)
    if not api_key:
        raise HTTPException(status_code=400, detail="请输入完整 API Key")
    if is_masked_newapi_api_key(api_key):
        raise HTTPException(status_code=400, detail="这是中转站脱敏后的 API Key，不能用于模型调用。请粘贴完整 sk- 开头的 API Key。")
    return api_key


def clean_newapi_base_url(value: str) -> str:
    base = (value or DEFAULT_NEWAPI_BASE_URL).strip().rstrip("/")
    for suffix in NEWAPI_BASE_URL_SUFFIXES:
        while base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base or DEFAULT_NEWAPI_BASE_URL


def normalize_newapi_base_url(base_url: str) -> str:
    value = clean_newapi_base_url(base_url)
    if not re.match(r"^https?://", value):
        raise HTTPException(status_code=400, detail="中转站 Base URL 必须以 http:// 或 https:// 开头")
    if value in LEGACY_NEWAPI_BASE_URLS:
        return DEFAULT_NEWAPI_BASE_URL
    if value == DEFAULT_NEWAPI_BASE_URL:
        return value
    parsed = urlparse(value)
    if parsed.scheme.lower() == "http" and not is_private_or_local_http_url(value):
        raise HTTPException(status_code=400, detail="中转站 Base URL 传输登录信息和 API Key，公网地址必须使用 https://")
    return value


def get_newapi_account(include_secret: bool = False) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM newapi_accounts WHERE id = ?", (NEWAPI_ACCOUNT_ID,)).fetchone()
    return row_newapi_account(row, include_secret=include_secret)


def require_seedance_asset_credentials() -> Dict[str, str]:
    account = get_newapi_account(include_secret=True)
    api_key = normalize_newapi_api_key((account or {}).get("defaultApiKey") or "")
    if not is_usable_newapi_api_key(api_key):
        raise HTTPException(status_code=400, detail="请先在账号中心绑定可用 API Key")
    base_url = normalize_newapi_base_url((account or {}).get("baseUrl") or DEFAULT_NEWAPI_BASE_URL)
    return {"apiKey": api_key, "baseUrl": base_url}


def seedance_portrait_asset_legacy_fallback_enabled() -> bool:
    raw = os.environ.get("LIBAI_SEEDANCE_PORTRAIT_ASSET_LEGACY_FALLBACK_ENABLED")
    if raw is None:
        return True
    parsed = truthy_flag(raw)
    return True if parsed is None else parsed


def seedance_portrait_asset_clean_suffix(suffix: str = "") -> str:
    return suffix if suffix.startswith("/") or not suffix else f"/{suffix}"


def seedance_portrait_asset_urls(credentials: Dict[str, str], suffix: str = "") -> List[str]:
    clean_suffix = seedance_portrait_asset_clean_suffix(suffix)
    urls = [f"{credentials['baseUrl']}{SEEDANCE_PORTRAIT_ASSET_BRIDGE_PATH}{clean_suffix}"]
    if seedance_portrait_asset_legacy_fallback_enabled():
        urls.append(f"{credentials['baseUrl']}{SEEDANCE_PORTRAIT_ASSET_LEGACY_PATH}{clean_suffix}")
    return urls


def seedance_portrait_asset_metadata_urls(credentials: Dict[str, str], suffix: str = "") -> List[str]:
    clean_suffix = seedance_portrait_asset_clean_suffix(suffix)
    model_name = seedance_portrait_asset_upload_model_name()
    model_query = f"?model={quote(model_name, safe='')}" if model_name and model_name != SEEDANCE_DASH_PRO_MODEL_ID else ""
    return [f"{credentials['baseUrl']}{SEEDANCE_PORTRAIT_ASSET_BRIDGE_PATH}{clean_suffix}{model_query}"]


def seedance_portrait_asset_model_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("（", "(").replace("）", ")")


def seedance_portrait_asset_model_score(values: List[Any]) -> int:
    raw_text = " ".join(str(value or "").strip() for value in values if str(value or "").strip())
    text = seedance_portrait_asset_model_text(raw_text)
    if "seedance-2-0" not in text:
        return -1
    score = 0
    if "火山" in raw_text:
        score += 100
    if "官转" in raw_text:
        score += 30
    if "版本" in raw_text:
        score += 10
    if "pro" in text or "seedance-2-0(" in text:
        score += 20
    if "fast" in text:
        score += 10
    canonical_values = {canonical_provider_model_id(value) for value in values if str(value or "").strip()}
    if SEEDANCE_DASH_PRO_MODEL_ID in canonical_values:
        score += 5
    if SEEDANCE_DASH_FAST_MODEL_ID in canonical_values:
        score += 3
    return score


def seedance_portrait_asset_upload_model_name() -> str:
    override = (
        os.environ.get("LIBAI_SEEDANCE_PORTRAIT_ASSET_MODEL")
        or os.environ.get("LIBAI_SEEDANCE_PORTRAIT_ASSET_UPLOAD_MODEL")
        or ""
    ).strip()
    if override:
        return override

    candidates: List[tuple[int, str]] = []
    try:
        models = list_provider_models(
            provider_id=NEWAPI_PROVIDER_ID,
            capability="video.generate",
            enabled_only=True,
            include_internal=True,
        )
    except Exception:
        models = []
    for model in models:
        values = [
            model.get("id"),
            model.get("modelName"),
            model.get("displayName"),
            *((model.get("meta") or {}).get("aliases") or []),
        ]
        score = seedance_portrait_asset_model_score(values)
        model_name = str(model.get("modelName") or model.get("id") or "").strip()
        if score >= 0 and model_name:
            candidates.append((score, model_name))
    if not candidates:
        account = get_newapi_account() or {}
        available_models = ((account.get("meta") or {}).get("availableModels") or [])
        for value in available_models if isinstance(available_models, list) else []:
            model_name = str(value or "").strip()
            score = seedance_portrait_asset_model_score([model_name])
            if score >= 0 and model_name:
                candidates.append((score, model_name))
    if candidates:
        return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]
    return SEEDANCE_DASH_PRO_MODEL_ID


def seedance_portrait_asset_upload_data(
    *,
    name: str,
    description: str = "",
) -> Dict[str, str]:
    data = {
        "asset_type": "Image",
        "name": name,
        "description": (description or "").strip(),
    }
    model_name = seedance_portrait_asset_upload_model_name()
    if model_name and model_name != SEEDANCE_DASH_PRO_MODEL_ID:
        data["model"] = model_name
    return data


def extract_seedance_asset_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if message:
                message = str(message)
                if "key_concurrency_limited" in message.lower():
                    return "上游角色库上传并发限制，请稍后重试"
                return message
        if error:
            message = str(error)
            if "key_concurrency_limited" in message.lower():
                return "上游角色库上传并发限制，请稍后重试"
            return message
        for key in ("message", "detail", "msg"):
            if payload.get(key):
                message = str(payload[key])
                if "key_concurrency_limited" in message.lower():
                    return "上游角色库上传并发限制，请稍后重试"
                return message
    text = response.text.strip()[:300]
    if "key_concurrency_limited" in text.lower():
        return "上游角色库上传并发限制，请稍后重试"
    return text or f"上游素材上传失败（HTTP {response.status_code}）"


def format_seedance_asset_size_limit(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024 and max_bytes % (1024 * 1024) == 0:
        return f"{max_bytes // (1024 * 1024)}MB"
    if max_bytes >= 1024 and max_bytes % 1024 == 0:
        return f"{max_bytes // 1024}KB"
    return f"{max_bytes} 字节"


def seedance_portrait_asset_upload_retry_attempts() -> int:
    return max(1, env_int("LIBAI_SEEDANCE_PORTRAIT_ASSET_UPLOAD_RETRY_ATTEMPTS", 5))


def seedance_portrait_asset_upload_timeout_seconds() -> float:
    return max(10.0, env_float("LIBAI_SEEDANCE_PORTRAIT_ASSET_UPLOAD_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS))


def seedance_portrait_asset_metadata_timeout_seconds() -> float:
    return max(1.0, env_float("LIBAI_SEEDANCE_PORTRAIT_ASSET_METADATA_TIMEOUT_SECONDS", 5.0))


def seedance_portrait_asset_upload_retry_delay(attempt: int) -> float:
    return min(3.0, max(0.2, 0.5 * max(1, attempt)))


def seedance_portrait_asset_upload_error_is_retryable(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    return (
        isinstance(error, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.TimeoutException, httpx.RemoteProtocolError))
        or "winerror 10053" in text
        or "winerror 10054" in text
        or "connection reset" in text
        or "connection aborted" in text
        or "remote host" in text
        or "server disconnected" in text
    )


def seedance_portrait_asset_response_is_retryable(response: httpx.Response) -> bool:
    detail = extract_seedance_asset_error_message(response).lower()
    if "key_concurrency_limited" in detail or "并发限制" in detail:
        return False
    return response.status_code in {408, 429, 500, 502, 503, 504}


def seedance_portrait_asset_reference_fallback_enabled() -> bool:
    raw = os.environ.get("LIBAI_SEEDANCE_PORTRAIT_ASSET_REFERENCE_FALLBACK_ENABLED")
    if raw is None:
        return True
    parsed = truthy_flag(raw)
    return True if parsed is None else parsed


def seedance_portrait_asset_response_supports_reference_fallback(response: httpx.Response) -> bool:
    detail = extract_seedance_asset_error_message(response).lower()
    return response.status_code in {400, 404, 405, 501, 502, 503, 504} and (
        "invalid url" in detail
        or "/v1/volc/assets" in detail
        or "/v1/libai/seedance/assets" in detail
        or "not found" in detail
        or "unsupported" in detail
    )


def seedance_portrait_asset_should_try_legacy_url(response: httpx.Response, url: str) -> bool:
    if not seedance_portrait_asset_legacy_fallback_enabled():
        return False
    if SEEDANCE_PORTRAIT_ASSET_BRIDGE_PATH not in url:
        return False
    detail = extract_seedance_asset_error_message(response).lower()
    return response.status_code in {400, 404, 405, 501} and (
        SEEDANCE_PORTRAIT_ASSET_BRIDGE_PATH in detail
        or "invalid url" in detail
        or "not found" in detail
        or "unsupported" in detail
    )


def seedance_portrait_asset_lookup_terms(*values: str) -> set[str]:
    terms: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lower = text.lower()
        terms.add(lower)
        stem = Path(text).stem.strip().lower()
        if stem:
            terms.add(stem)
    return terms


def seedance_portrait_asset_id_from_dict(asset: Dict[str, Any]) -> str:
    return str(asset.get("asset_id") or asset.get("assetId") or asset.get("id") or asset.get("Id") or "").strip()


def seedance_portrait_asset_preview_url(asset_id: str) -> str:
    return f"/seedance/portrait-assets/{quote(str(asset_id or '').strip(), safe='')}/preview"


def seedance_portrait_asset_preview_path(asset_id: str, filename: str, content_type: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(asset_id or "").strip()).strip("._")
    if not safe_id:
        safe_id = hashlib.sha256(str(asset_id or "").encode("utf-8")).hexdigest()[:16]
    suffix = safe_asset_suffix(filename or "portrait.png", content_type or "image/png")
    return SEEDANCE_PORTRAIT_PREVIEWS_DIR / f"{safe_id[:120]}{suffix}"


def attach_seedance_portrait_local_preview(
    asset: Dict[str, Any],
    *,
    content: bytes,
    filename: str,
    content_type: str,
) -> Dict[str, Any]:
    asset_id = seedance_portrait_asset_id_from_dict(asset)
    if not asset_id or not content:
        return asset
    SEEDANCE_PORTRAIT_PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    target = seedance_portrait_asset_preview_path(asset_id, filename, content_type)
    target.write_bytes(content)
    preview_url = seedance_portrait_asset_preview_url(asset_id)
    return {
        **asset,
        "localPreviewPath": str(target),
        "local_preview_path": str(target),
        "localPreviewUrl": preview_url,
        "local_preview_url": preview_url,
        "previewUrl": preview_url,
        "preview_url": preview_url,
    }


def extract_seedance_portrait_asset_payload(payload: Any) -> Any:
    seen: set[int] = set()

    def walk(value: Any) -> Optional[Dict[str, Any]]:
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return None
            seen.add(marker)
            if seedance_portrait_asset_id_from_dict(value):
                return value
            for key in ("asset", "data", "result", "payload", "item", "record"):
                found = walk(value.get(key))
                if found is not None:
                    return found
            for key in ("assets", "items", "records", "list"):
                found = walk(value.get(key))
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(payload)
    if found is not None:
        return found
    if isinstance(payload, dict) and "asset" in payload:
        return payload.get("asset")
    return payload if isinstance(payload, dict) else None


def recover_seedance_portrait_asset_from_upstream(
    credentials: Dict[str, str],
    *,
    name: str,
    filename: str,
    description: str = "",
) -> Optional[Dict[str, Any]]:
    targets = seedance_portrait_asset_lookup_terms(name, filename)
    if not targets:
        return None
    payload: Any = None
    for list_url in seedance_portrait_asset_metadata_urls(credentials):
        try:
            response = httpx.get(
                list_url,
                headers={"Authorization": f"Bearer {credentials['apiKey']}"},
                timeout=seedance_portrait_asset_metadata_timeout_seconds(),
                follow_redirects=True,
                trust_env=provider_http_trust_env(),
            )
        except httpx.HTTPError:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        break
    if payload is None:
        return None
    for asset in seedance_portrait_assets_from_payload(payload):
        candidates = seedance_portrait_asset_lookup_terms(
            asset.get("name", ""),
            asset.get("title", ""),
            asset.get("filename", ""),
        )
        if targets.isdisjoint(candidates):
            continue
        if not asset.get("name"):
            asset["name"] = name or filename
        clean_description = (description or "").strip()
        if clean_description:
            asset["description"] = clean_description
        return register_seedance_portrait_asset(asset)
    return None


def create_seedance_portrait_reference_asset(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    name: str,
    description: str = "",
) -> Optional[Dict[str, Any]]:
    if not seedance_portrait_asset_reference_fallback_enabled():
        return None
    mime = content_type or "image/png"
    encoded = base64.b64encode(content).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    try:
        model_name = seedance_portrait_asset_upload_model_name()
        prepared = asyncio.run(prepare_outgoing_reference_image_payload(
            "video.generate",
            {"image": data_url},
            {"id": "newapi"},
            {"id": model_name, "modelName": model_name, "params": {}},
        ))
    except Exception:
        return None
    hosted_url = str((prepared or {}).get("image") or "").strip()
    if not hosted_url or hosted_url == data_url:
        return None
    digest = hashlib.sha256(content).hexdigest()
    asset: Dict[str, Any] = {
        "asset_id": f"reference_{digest[:24]}",
        "asset_ref": hosted_url,
        "asset_type": "Image",
        "name": (name or filename or "角色图").strip() or "角色图",
        "url": hosted_url,
        "status": "reference",
    }
    asset = attach_seedance_portrait_local_preview(
        asset,
        content=content,
        filename=filename,
        content_type=content_type,
    )
    clean_description = (description or "").strip()
    if clean_description:
        asset["description"] = clean_description
    return register_seedance_portrait_asset(asset)


def upload_seedance_portrait_asset_bytes(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    name: str = "",
    description: str = "",
) -> Dict[str, Any]:
    if not content:
        raise HTTPException(status_code=400, detail="请选择要上传的角色图片")
    if len(content) > SEEDANCE_PORTRAIT_ASSET_MAX_BYTES:
        limit = format_seedance_asset_size_limit(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES)
        raise HTTPException(status_code=413, detail=f"角色图片不能超过 {limit}，请压缩后重试")

    credentials = require_seedance_asset_credentials()
    clean_filename = filename or "portrait.png"
    clean_content_type = content_type or "application/octet-stream"
    upload_name = (name or clean_filename).strip() or clean_filename
    attempts = seedance_portrait_asset_upload_retry_attempts()
    response: Optional[httpx.Response] = None
    last_error: Optional[httpx.HTTPError] = None
    for upload_url in seedance_portrait_asset_urls(credentials):
        response = None
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {credentials['apiKey']}"},
                    files={"file": (clean_filename, content, clean_content_type)},
                    data=seedance_portrait_asset_upload_data(
                        name=upload_name,
                        description=description,
                    ),
                    timeout=seedance_portrait_asset_upload_timeout_seconds(),
                    follow_redirects=True,
                    trust_env=provider_http_trust_env(),
                )
            except httpx.HTTPError as error:
                last_error = error
                retryable = seedance_portrait_asset_upload_error_is_retryable(error)
                if retryable:
                    recovered = recover_seedance_portrait_asset_from_upstream(
                        credentials,
                        name=upload_name,
                        filename=clean_filename,
                        description=description,
                    )
                    if recovered:
                        return {"asset": recovered}
                    if attempt < attempts:
                        time.sleep(seedance_portrait_asset_upload_retry_delay(attempt))
                        continue
                    fallback = create_seedance_portrait_reference_asset(
                        content=content,
                        filename=clean_filename,
                        content_type=clean_content_type,
                        name=upload_name,
                        description=description,
                    )
                    if fallback:
                        return {"asset": fallback}
                raise HTTPException(
                    status_code=502,
                    detail=exception_message(error, "上游素材上传失败"),
                ) from error
            if attempt < attempts and seedance_portrait_asset_response_is_retryable(response):
                recovered = recover_seedance_portrait_asset_from_upstream(
                    credentials,
                    name=upload_name,
                    filename=clean_filename,
                    description=description,
                )
                if recovered:
                    return {"asset": recovered}
                time.sleep(seedance_portrait_asset_upload_retry_delay(attempt))
                continue
            break
        if response is not None and seedance_portrait_asset_should_try_legacy_url(response, upload_url):
            continue
        break
    if response is None:
        raise HTTPException(status_code=502, detail=exception_message(last_error, "上游素材上传失败") if last_error else "上游素材上传失败")

    if response.status_code < 200 or response.status_code >= 300:
        if seedance_portrait_asset_response_is_retryable(response):
            recovered = recover_seedance_portrait_asset_from_upstream(
                credentials,
                name=upload_name,
                filename=clean_filename,
                description=description,
            )
            if recovered:
                return {"asset": recovered}
        if seedance_portrait_asset_response_supports_reference_fallback(response):
            fallback = create_seedance_portrait_reference_asset(
                content=content,
                filename=clean_filename,
                content_type=clean_content_type,
                name=upload_name,
                description=description,
            )
            if fallback:
                return {"asset": fallback}
        raise HTTPException(
            status_code=response.status_code,
            detail=extract_seedance_asset_error_message(response),
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="上游未返回素材 ID") from error
    asset_payload = extract_seedance_portrait_asset_payload(payload)
    try:
        asset = normalize_seedance_portrait_asset(asset_payload)
    except HTTPException:
        recovered = recover_seedance_portrait_asset_from_upstream(
            credentials,
            name=upload_name,
            filename=clean_filename,
            description=description,
        )
        if recovered:
            return {"asset": recovered}
        fallback = create_seedance_portrait_reference_asset(
            content=content,
            filename=clean_filename,
            content_type=clean_content_type,
            name=upload_name,
            description=description,
        )
        if fallback:
            return {"asset": fallback}
        raise
    asset = attach_seedance_portrait_local_preview(
        asset,
        content=content,
        filename=clean_filename,
        content_type=clean_content_type,
    )
    if not asset.get("name"):
        asset["name"] = upload_name
    clean_description = (description or "").strip()
    if clean_description:
        asset["description"] = clean_description
    asset = register_seedance_portrait_asset(asset)
    return {"asset": asset}


def normalize_seedance_portrait_asset(asset: Any) -> Dict[str, Any]:
    if not isinstance(asset, dict):
        raise HTTPException(status_code=502, detail="上游未返回素材 ID")
    asset_id = str(asset.get("asset_id") or asset.get("assetId") or asset.get("id") or asset.get("Id") or "").strip()
    if not asset_id:
        raise HTTPException(status_code=502, detail="上游未返回素材 ID")
    asset_type = asset.get("asset_type") or asset.get("assetType") or asset.get("type") or "Image"
    url = asset.get("url") or asset.get("previewUrl") or asset.get("downloadUrl") or asset.get("uri") or ""
    status = asset.get("status") or asset.get("state") or "uploaded"
    name = str(asset.get("name") or asset.get("title") or asset.get("filename") or "").strip()
    description = str(asset.get("description") or asset.get("desc") or "").strip()
    created_at = str(asset.get("createdAt") or asset.get("created_at") or asset.get("created") or "").strip()
    asset_ref = str(asset.get("asset_ref") or asset.get("assetRef") or "").strip() or f"asset://{asset_id}"
    local_preview_path = str(asset.get("localPreviewPath") or asset.get("local_preview_path") or "").strip()
    local_preview_url = str(asset.get("localPreviewUrl") or asset.get("local_preview_url") or "").strip()
    preview_url = str(asset.get("previewUrl") or asset.get("preview_url") or "").strip()
    record = {
        "asset_id": asset_id,
        "asset_ref": asset_ref,
        "asset_type": asset_type,
        "name": name,
        "url": url,
        "status": status,
    }
    if local_preview_path:
        record["localPreviewPath"] = local_preview_path
        record["local_preview_path"] = local_preview_path
    if local_preview_url:
        record["localPreviewUrl"] = local_preview_url
        record["local_preview_url"] = local_preview_url
    if preview_url:
        record["previewUrl"] = preview_url
        record["preview_url"] = preview_url
    if description:
        record["description"] = description
    if created_at:
        record["createdAt"] = created_at
    return record


def unique_seedance_portrait_assets(raw_assets: Any) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_assets if isinstance(raw_assets, list) else []:
        try:
            asset = normalize_seedance_portrait_asset(item)
        except HTTPException:
            continue
        asset_id = asset.get("asset_id") or ""
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        assets.append(asset)
    return assets


def read_seedance_portrait_asset_registry() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(SEEDANCE_PORTRAIT_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw_assets = payload.get("assets") if isinstance(payload, dict) else payload
    return unique_seedance_portrait_assets(raw_assets)


def write_seedance_portrait_asset_registry(assets: List[Dict[str, Any]]) -> None:
    cleaned = unique_seedance_portrait_assets(assets)
    write_json_file(SEEDANCE_PORTRAIT_ASSETS_PATH, {
        "assets": cleaned,
        "updatedAt": utc_now(),
    })


def register_seedance_portrait_asset(asset: Dict[str, Any]) -> Dict[str, Any]:
    record = normalize_seedance_portrait_asset(asset)
    if not record.get("createdAt"):
        record["createdAt"] = utc_now()
    current = read_seedance_portrait_asset_registry()
    write_seedance_portrait_asset_registry([record, *current])
    return record


def remove_seedance_portrait_asset_from_registry(asset_id: str) -> bool:
    clean_id = str(asset_id or "").strip()
    if not clean_id:
        return False
    current = read_seedance_portrait_asset_registry()
    next_assets = [asset for asset in current if asset.get("asset_id") != clean_id]
    if len(next_assets) == len(current):
        return False
    write_seedance_portrait_asset_registry(next_assets)
    return True


def merge_seedance_portrait_asset(local_asset: Dict[str, Any], upstream_asset: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(local_asset or {})
    for key, value in (upstream_asset or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    asset_id = str(merged.get("asset_id") or merged.get("assetId") or "").strip()
    if asset_id:
        merged["asset_id"] = asset_id
        merged["asset_ref"] = f"asset://{asset_id}"
    return normalize_seedance_portrait_asset(merged)


def refresh_seedance_portrait_assets_from_upstream(local_assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    registered_assets = unique_seedance_portrait_assets(local_assets)
    if not registered_assets:
        return []
    try:
        credentials = require_seedance_asset_credentials()
    except HTTPException:
        return registered_assets

    payload: Any = None
    for list_url in seedance_portrait_asset_metadata_urls(credentials):
        try:
            response = httpx.get(
                list_url,
                headers={"Authorization": f"Bearer {credentials['apiKey']}"},
                timeout=seedance_portrait_asset_metadata_timeout_seconds(),
                follow_redirects=True,
                trust_env=provider_http_trust_env(),
            )
        except httpx.HTTPError:
            return registered_assets

        if response.status_code < 200 or response.status_code >= 300:
            return registered_assets

        try:
            payload = response.json()
        except ValueError:
            return registered_assets
        break
    if payload is None:
        return registered_assets

    upstream_by_id = {
        asset.get("asset_id"): asset
        for asset in seedance_portrait_assets_from_payload(payload)
        if asset.get("asset_id")
    }
    refreshed = [
        merge_seedance_portrait_asset(asset, upstream_by_id[asset["asset_id"]])
        if asset.get("asset_id") in upstream_by_id else asset
        for asset in registered_assets
    ]
    write_seedance_portrait_asset_registry(refreshed)
    return refreshed


def seedance_portrait_assets_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        raw_assets = payload
    elif isinstance(payload, dict):
        raw_assets = (
            payload.get("assets")
            or payload.get("data")
            or payload.get("items")
            or []
        )
        if isinstance(raw_assets, dict):
            raw_assets = raw_assets.get("assets") or raw_assets.get("items") or []
    else:
        raw_assets = []
    return unique_seedance_portrait_assets(raw_assets)


def channel24_portrait_base_url() -> str:
    base = str(os.environ.get("LIBAI_CHANNEL24_PORTRAIT_BASE_URL") or CHANNEL24_PORTRAIT_DEFAULT_BASE_URL).strip()
    return base.rstrip("/") or CHANNEL24_PORTRAIT_DEFAULT_BASE_URL


def require_channel24_portrait_credentials() -> Dict[str, str]:
    base_url = channel24_portrait_base_url()
    use_newapi_proxy = channel24_portrait_uses_newapi_proxy(base_url)
    token = ""
    account: Optional[Dict[str, Any]] = None
    if use_newapi_proxy:
        account = get_newapi_account(include_secret=True)
        token = normalize_newapi_api_key((account or {}).get("defaultApiKey") or "")
    else:
        token = str(os.environ.get("LIBAI_CHANNEL24_PORTRAIT_TOKEN") or "").strip()
    if not token and not use_newapi_proxy:
        account = account or get_newapi_account(include_secret=True)
        token = str((account or {}).get("accessToken") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    if not token:
        account = account or get_newapi_account(include_secret=True)
        token = normalize_newapi_api_key((account or {}).get("defaultApiKey") or "")
    if not token:
        raise HTTPException(status_code=400, detail="请先配置渠道24人像库 token 或账号中心默认 API Key")
    return {"token": token, "baseUrl": base_url}


def channel24_portrait_uses_newapi_proxy(base_url: str) -> bool:
    parsed = urlparse(str(base_url or ""))
    return parsed.path.rstrip("/").endswith("/v1/libai/channel24")


def channel24_portrait_headers(credentials: Dict[str, str]) -> Dict[str, str]:
    if channel24_portrait_uses_newapi_proxy(credentials.get("baseUrl") or ""):
        return {
            "Authorization": "Bearer " + credentials["token"],
            "Content-Type": "application/json",
        }
    return {
        "token": credentials["token"],
        "Content-Type": "application/json",
    }


def channel24_portrait_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "msg"):
            value = payload.get(key)
            if value:
                return str(value)
    text = response.text.strip()[:300]
    return text or f"渠道24人像库请求失败（HTTP {response.status_code}）"


def channel24_portrait_assert_success(response: httpx.Response) -> Dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(status_code=response.status_code, detail=channel24_portrait_error_message(response))
    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="渠道24人像库返回了无效 JSON") from error
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
        raise HTTPException(status_code=502, detail=str(payload.get("message") or "渠道24人像库请求失败"))
    return payload if isinstance(payload, dict) else {"data": payload}


def channel24_resource_status(resource: Dict[str, Any], asset_id: str) -> str:
    raw_status = str(resource.get("Status") or resource.get("status") or "").strip()
    status_text = str(resource.get("StatusText") or resource.get("statusText") or resource.get("Message") or "").strip()
    lowered = f"{raw_status} {status_text}".lower()
    if asset_id:
        return "active"
    if any(text in lowered for text in ("失败", "fail", "error", "deleted", "删除")):
        return "failed"
    return "processing"


def normalize_channel24_portrait_resource(resource: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(resource, dict):
        return None
    resource_id_raw = (
        resource.get("Id")
        or resource.get("id")
        or resource.get("ResourceId")
        or resource.get("resourceId")
        or resource.get("channel24ResourceId")
        or resource.get("channel24_resource_id")
    )
    resource_id = str(resource_id_raw or "").strip()
    upstream_asset_id = str(resource.get("AssetId") or resource.get("assetId") or "").strip()
    stored_asset_id = str(resource.get("asset_id") or "").strip()
    if not resource_id and not upstream_asset_id and not stored_asset_id:
        return None
    explicit_asset_ref = str(resource.get("asset_ref") or resource.get("assetRef") or "").strip()
    ready = bool(upstream_asset_id or explicit_asset_ref)
    fallback_id = f"channel24-resource-{resource_id}" if resource_id else f"channel24-resource-{hashlib.sha256(json_dump(resource).encode('utf-8')).hexdigest()[:16]}"
    clean_asset_id = upstream_asset_id or stored_asset_id or fallback_id
    url = str(resource.get("OssPath") or resource.get("ossPath") or resource.get("url") or resource.get("Url") or "").strip()
    name = str(resource.get("Name") or resource.get("name") or resource.get("title") or clean_asset_id).strip() or clean_asset_id
    status = channel24_resource_status(resource, upstream_asset_id or (clean_asset_id if ready else ""))
    selectable = resource.get("selectable")
    if selectable is None:
        selectable = ready
    record: Dict[str, Any] = {
        "asset_id": clean_asset_id,
        "asset_ref": explicit_asset_ref or (f"asset://{upstream_asset_id}" if upstream_asset_id else ""),
        "asset_type": "Image",
        "name": name,
        "url": url,
        "status": status,
        "source": "channel24",
        "portraitSource": "channel24",
        "selectable": bool(selectable),
    }
    if resource_id:
        try:
            record["channel24ResourceId"] = int(resource_id)
        except ValueError:
            record["channel24ResourceId"] = resource_id
        record["channel24_resource_id"] = record["channel24ResourceId"]
    status_text = str(resource.get("StatusText") or resource.get("statusText") or "").strip()
    if status_text:
        record["statusText"] = status_text
    message = str(resource.get("Message") or resource.get("message") or "").strip()
    if message:
        record["message"] = message
    description = str(resource.get("Desc") or resource.get("description") or resource.get("Prompt") or "").strip()
    if description:
        record["description"] = description
    created_at = str(resource.get("CreatedAt") or resource.get("createdAt") or "").strip()
    if created_at:
        record["createdAt"] = created_at
    return record


def channel24_portrait_assets_from_payload(payload: Any) -> List[Dict[str, Any]]:
    raw_assets: Any = []
    if isinstance(payload, list):
        raw_assets = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            raw_assets = data.get("data") or data.get("items") or data.get("list") or []
        else:
            raw_assets = payload.get("assets") or payload.get("items") or payload.get("list") or []
    assets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_assets if isinstance(raw_assets, list) else []:
        asset = normalize_channel24_portrait_resource(item)
        if not asset:
            continue
        key = asset.get("asset_id") or ""
        if key in seen:
            continue
        seen.add(key)
        assets.append(asset)
    return assets


def channel24_portrait_asset_keys(asset: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    asset_id = str(asset.get("asset_id") or asset.get("assetId") or "").strip()
    if asset_id:
        keys.add(f"asset:{asset_id}")
    asset_ref = str(asset.get("asset_ref") or asset.get("assetRef") or "").strip()
    if asset_ref:
        keys.add(f"ref:{asset_ref}")
    resource_id = str(
        asset.get("channel24ResourceId")
        or asset.get("channel24_resource_id")
        or asset.get("ResourceId")
        or asset.get("resourceId")
        or asset.get("Id")
        or ""
    ).strip()
    if resource_id:
        keys.add(f"resource:{resource_id}")
    url = str(asset.get("url") or asset.get("OssPath") or asset.get("ossPath") or "").strip()
    if url:
        keys.add(f"url:{url}")
    return keys


def unique_channel24_portrait_assets(raw_assets: Any) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_assets if isinstance(raw_assets, list) else []:
        asset = normalize_channel24_portrait_resource(item)
        if not asset:
            continue
        keys = channel24_portrait_asset_keys(asset)
        key = next((value for value in keys if value in seen), "") or (asset.get("asset_id") or "")
        if key in seen:
            continue
        seen.update(keys)
        if key:
            seen.add(key)
        assets.append(asset)
    return assets


def read_channel24_portrait_asset_registry() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(CHANNEL24_PORTRAIT_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw_assets = payload.get("assets") if isinstance(payload, dict) else payload
    return unique_channel24_portrait_assets(raw_assets)


def write_channel24_portrait_asset_registry(assets: List[Dict[str, Any]]) -> None:
    cleaned = unique_channel24_portrait_assets(assets)
    write_json_file(CHANNEL24_PORTRAIT_ASSETS_PATH, {
        "assets": cleaned,
        "updatedAt": utc_now(),
    })


def register_channel24_portrait_asset(asset: Dict[str, Any]) -> Dict[str, Any]:
    record = normalize_channel24_portrait_resource(asset)
    if not record:
        raise HTTPException(status_code=502, detail="渠道24人像库未返回有效资源")
    if not record.get("createdAt"):
        record["createdAt"] = utc_now()
    current = read_channel24_portrait_asset_registry()
    write_channel24_portrait_asset_registry([record, *current])
    return record


def remove_channel24_portrait_asset_from_registry(resource_id: str) -> bool:
    clean_id = str(resource_id or "").strip()
    if not clean_id:
        return False
    current = read_channel24_portrait_asset_registry()
    target_keys = {f"asset:{clean_id}", f"resource:{clean_id}"}
    next_assets = [
        asset for asset in current
        if not (channel24_portrait_asset_keys(asset) & target_keys)
    ]
    if len(next_assets) == len(current):
        return False
    write_channel24_portrait_asset_registry(next_assets)
    return True


def merge_channel24_portrait_asset(local_asset: Dict[str, Any], upstream_asset: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(local_asset or {})
    for key, value in (upstream_asset or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    return normalize_channel24_portrait_resource(merged) or merged


def refresh_channel24_portrait_assets_from_upstream(
    local_assets: List[Dict[str, Any]],
    upstream_assets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    registered_assets = unique_channel24_portrait_assets(local_assets)
    if not registered_assets:
        return []
    upstream_by_key: Dict[str, Dict[str, Any]] = {}
    for upstream in unique_channel24_portrait_assets(upstream_assets):
        for key in channel24_portrait_asset_keys(upstream):
            upstream_by_key[key] = upstream
    refreshed = []
    for local_asset in registered_assets:
        match = next(
            (upstream_by_key[key] for key in channel24_portrait_asset_keys(local_asset) if key in upstream_by_key),
            None,
        )
        refreshed.append(merge_channel24_portrait_asset(local_asset, match) if match else local_asset)
    return unique_channel24_portrait_assets(refreshed)


def channel24_portrait_total_from_payload(payload: Any, assets: List[Dict[str, Any]]) -> int:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            try:
                return int(data.get("total"))
            except (TypeError, ValueError):
                pass
    return len(assets)


def list_channel24_portrait_upstream_assets(page: int = 1) -> Dict[str, Any]:
    credentials = require_channel24_portrait_credentials()
    try:
        response = httpx.post(
            f"{credentials['baseUrl']}/resources/user/ResourcesList",
            headers=channel24_portrait_headers(credentials),
            json={"Page": max(1, int(page or 1))},
            timeout=seedance_portrait_asset_metadata_timeout_seconds(),
            follow_redirects=True,
            trust_env=provider_http_trust_env(),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=exception_message(error, "渠道24人像库列表加载失败")) from error
    payload = channel24_portrait_assert_success(response)
    assets = channel24_portrait_assets_from_payload(payload)
    return {
        "source": "channel24",
        "assets": assets,
        "total": channel24_portrait_total_from_payload(payload, assets),
    }


def list_channel24_portrait_assets(page: int = 1) -> Dict[str, Any]:
    local_assets = read_channel24_portrait_asset_registry()
    if not local_assets:
        return {"source": "channel24", "assets": [], "total": 0}
    upstream_listing = list_channel24_portrait_upstream_assets(page=page)
    refreshed = refresh_channel24_portrait_assets_from_upstream(
        local_assets,
        upstream_listing.get("assets") or [],
    )
    write_channel24_portrait_asset_registry(refreshed)
    return {
        "source": "channel24",
        "assets": refreshed,
        "total": len(refreshed),
    }


def channel24_portrait_min_public_url_ttl_seconds() -> int:
    try:
        return max(0, int(os.environ.get("LIBAI_CHANNEL24_PORTRAIT_MIN_PUBLIC_URL_TTL_SECONDS", "3600")))
    except ValueError:
        return 3600


def tos_signed_url_remaining_seconds(value: str, now: Optional[datetime] = None) -> Optional[float]:
    text = str(value or "").strip()
    if not re.match(r"^https?://", text, re.I):
        return None
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    date_value = (query.get("X-Tos-Date") or query.get("x-tos-date") or [""])[0]
    expires_value = (query.get("X-Tos-Expires") or query.get("x-tos-expires") or [""])[0]
    if not date_value or not expires_value:
        return None
    try:
        start = datetime.strptime(str(date_value), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expires = int(str(expires_value))
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(timezone.utc)
    return (start.timestamp() + expires) - current.timestamp()


def channel24_portrait_public_image_url_is_usable(value: str) -> bool:
    text = str(value or "").strip()
    if not re.match(r"^https?://", text, re.I):
        return False
    remaining = tos_signed_url_remaining_seconds(text)
    if remaining is None:
        return True
    return remaining > channel24_portrait_min_public_url_ttl_seconds()


def seedance_portrait_public_image_url(asset: Any) -> str:
    if not isinstance(asset, dict):
        return ""
    for key in (
        "url",
        "previewUrl",
        "preview_url",
        "downloadUrl",
        "download_url",
        "asset_ref",
        "assetRef",
    ):
        value = str(asset.get(key) or "").strip()
        if channel24_portrait_public_image_url_is_usable(value):
            return value
    return ""


def channel24_portrait_bridge_error_message(error: Optional[BaseException]) -> str:
    if error is None:
        return ""
    detail = getattr(error, "detail", None)
    if detail:
        return str(detail)
    return exception_message(error, "本地火山角色库图片托管失败")


def host_channel24_portrait_image_url(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    name: str = "",
    description: str = "",
) -> str:
    bridge_error: Optional[BaseException] = None
    try:
        bridge_result = upload_seedance_portrait_asset_bytes(
            content=content,
            filename=filename,
            content_type=content_type,
            name=name,
            description=description,
        )
        bridge_asset = bridge_result.get("asset") if isinstance(bridge_result, dict) else bridge_result
        hosted_url = seedance_portrait_public_image_url(bridge_asset)
        if hosted_url:
            return hosted_url
        bridge_error = HTTPException(status_code=502, detail="本地火山角色库未返回公网图片 URL")
    except Exception as error:
        bridge_error = error

    mime = content_type or mimetypes.guess_type(filename or "")[0] or "image/png"
    encoded = base64.b64encode(content).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    try:
        prepared = asyncio.run(prepare_outgoing_reference_image_payload(
            "video.generate",
            {"image": data_url},
            {"id": "channel24-portrait"},
            {
                "id": "seedence2.0（火山备用版本）",
                "modelName": "seedence2.0（火山备用版本）",
                "params": {"videoProtocol": "public_video_api"},
            },
        ))
    except Exception as error:
        bridge_message = channel24_portrait_bridge_error_message(bridge_error)
        if bridge_message:
            raise HTTPException(status_code=502, detail=f"渠道24人像库图片托管失败：{bridge_message}") from error
        raise HTTPException(status_code=502, detail=exception_message(error, "渠道24人像库图片托管失败")) from error
    hosted_url = str((prepared or {}).get("image") or "").strip()
    if not re.match(r"^https?://", hosted_url, re.I):
        bridge_message = channel24_portrait_bridge_error_message(bridge_error)
        if bridge_message:
            raise HTTPException(status_code=502, detail=f"渠道24人像库需要公网图片 URL：{bridge_message}")
        raise HTTPException(status_code=502, detail="渠道24人像库需要公网图片 URL")
    return hosted_url


def find_channel24_portrait_resource(upload_name: str, oss_path: str) -> Optional[Dict[str, Any]]:
    listing = list_channel24_portrait_upstream_assets(page=1)
    upload_name_clean = str(upload_name or "").strip()
    oss_path_clean = str(oss_path or "").strip()
    for asset in listing.get("assets") or []:
        if oss_path_clean and str(asset.get("url") or "").strip() == oss_path_clean:
            return asset
        if upload_name_clean and str(asset.get("name") or "").strip() == upload_name_clean:
            return asset
    return None


def upload_channel24_portrait_asset_bytes(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    name: str = "",
    description: str = "",
    group_id: Optional[int] = None,
) -> Dict[str, Any]:
    if not content:
        raise HTTPException(status_code=400, detail="请选择要上传的角色图片")
    if len(content) > SEEDANCE_PORTRAIT_ASSET_MAX_BYTES:
        limit = format_seedance_asset_size_limit(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES)
        raise HTTPException(status_code=413, detail=f"角色图片不能超过 {limit}，请压缩后重试")

    clean_filename = filename or "portrait.png"
    upload_name = (name or Path(clean_filename).stem or clean_filename).strip() or "角色图"
    clean_description = str(description or "").strip()
    hosted_url = host_channel24_portrait_image_url(
        content=content,
        filename=clean_filename,
        content_type=content_type or "application/octet-stream",
        name=upload_name,
        description=clean_description,
    )
    credentials = require_channel24_portrait_credentials()
    body: Dict[str, Any] = {
        "Name": upload_name,
        "OssPath": hosted_url,
    }
    if group_id:
        body["UserResourcesTypeId"] = int(group_id)
    if clean_description:
        body["Desc"] = clean_description
    try:
        response = httpx.post(
            f"{credentials['baseUrl']}/resources/user/Resources",
            headers=channel24_portrait_headers(credentials),
            json=body,
            timeout=seedance_portrait_asset_upload_timeout_seconds(),
            follow_redirects=True,
            trust_env=provider_http_trust_env(),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=exception_message(error, "渠道24人像库添加资源失败")) from error
    channel24_portrait_assert_success(response)
    recovered = find_channel24_portrait_resource(upload_name, hosted_url)
    if recovered:
        return {"source": "channel24", "asset": register_channel24_portrait_asset(recovered)}
    fallback = normalize_channel24_portrait_resource({
        "Id": hashlib.sha256(f"{upload_name}:{hosted_url}".encode("utf-8")).hexdigest()[:16],
        "Name": upload_name,
        "OssPath": hosted_url,
        "Status": 0,
        "StatusText": "处理中",
        "AssetId": "",
    })
    return {"source": "channel24", "asset": register_channel24_portrait_asset(fallback or {})}


def delete_channel24_portrait_resource(resource_id: str) -> Dict[str, Any]:
    clean_id = str(resource_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="缺少渠道24资源 ID")
    try:
        body_id: Any = int(clean_id)
    except ValueError:
        body_id = clean_id
    credentials = require_channel24_portrait_credentials()
    try:
        response = httpx.request(
            "DELETE",
            f"{credentials['baseUrl']}/resources/user/Resources",
            headers=channel24_portrait_headers(credentials),
            json={"Id": body_id},
            timeout=seedance_portrait_asset_metadata_timeout_seconds(),
            follow_redirects=True,
            trust_env=provider_http_trust_env(),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=exception_message(error, "渠道24人像库删除失败")) from error
    channel24_portrait_assert_success(response)
    remove_channel24_portrait_asset_from_registry(clean_id)
    return {
        "ok": True,
        "source": "channel24",
        "deleted": clean_id,
        "upstreamDeleted": True,
    }


def sync_newapi_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_file(DATA_DIR / "newapi-account.json", {
        "account": get_newapi_account(),
        "updatedAt": utc_now(),
    })


def translate_newapi_error_message(message: str) -> str:
    text = message or ""
    lowered = text.lower()
    if "invalid smtp account" in lowered:
        return "中转站邮件服务 SMTP 未配置或不可用，无法发送邮箱验证码。可以先尝试不填验证码直接注册；如果后台强制邮箱验证，需要先在中转站服务端配置 SMTP。"
    if "user.password" in lowered and "min" in lowered:
        return "密码长度不符合中转站要求，请至少输入 8 位密码。"
    return text


def newapi_http_exception(error: NewApiError) -> HTTPException:
    status_code = error.status_code if 400 <= int(error.status_code or 0) < 500 else 502
    return HTTPException(status_code=status_code, detail=translate_newapi_error_message(error.message))


def parse_newapi_log_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[int]:
    clean = (value or "").strip()
    if not clean:
        return None
    if clean.isdigit():
        return int(clean)
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if end_of_day and len(clean) <= 10:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999000)
        return int(parsed.timestamp())
    except ValueError as error:
        raise HTTPException(status_code=400, detail="消费账单时间格式不正确") from error


def newapi_client_task_id_for_job(job_id: str) -> str:
    raw = str(job_id or "").strip()
    if raw.startswith("job_"):
        raw = raw[4:]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_-")
    return f"task_libai_{safe}" if safe else ""


def newapi_task_item_is_failed(item: Dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().upper()
    status_label = str(item.get("statusLabel") or item.get("status_label") or "").strip()
    return status in {"FAILURE", "FAILED", "FAIL", "ERROR"} or status_label == "失败"


def newapi_task_item_is_success(item: Dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().upper()
    status_label = str(item.get("statusLabel") or item.get("status_label") or "").strip()
    return status in {"SUCCESS", "COMPLETED", "COMPLETE", "SUCCEEDED", "SUCCEED"} or status_label == "成功"


def newapi_task_item_matches_id(item: Dict[str, Any], task_id: str) -> bool:
    return newapi_task_id_from_item(item) == task_id


def newapi_task_id_from_item(item: Dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    candidates = [
        item.get("taskId"),
        item.get("task_id"),
        item.get("id"),
        raw.get("task_id"),
        raw.get("taskId"),
        raw.get("id"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def newapi_job_id_from_client_task_id(task_id: str) -> str:
    text = str(task_id or "").strip()
    prefix = "task_libai_"
    if not text.startswith(prefix):
        return ""
    suffix = text[len(prefix):].strip()
    return f"job_{suffix}" if suffix else ""


def local_asset_id_from_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r"/assets/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else ""


def local_asset_id_from_output(output: Dict[str, Any]) -> str:
    explicit = str(output.get("assetId") or output.get("asset_id") or "").strip()
    if explicit:
        return explicit
    candidates: List[Any] = [
        output.get("assetUrl"),
        output.get("asset_url"),
        output.get("url"),
        output.get("src"),
    ]
    asset = output.get("asset") if isinstance(output.get("asset"), dict) else {}
    candidates.extend([
        asset.get("assetUrl"),
        asset.get("asset_url"),
        asset.get("url"),
        asset.get("src"),
    ])
    urls = output.get("urls")
    if isinstance(urls, list):
        candidates.extend(urls)
    for candidate in candidates:
        if isinstance(candidate, dict):
            nested = local_asset_id_from_output(candidate)
            if nested:
                return nested
            continue
        asset_id = local_asset_id_from_text(candidate)
        if asset_id:
            return asset_id
    return ""


def newapi_local_asset_preview(asset_id: str) -> Optional[Dict[str, Any]]:
    clean_id = str(asset_id or "").strip()
    if not clean_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (clean_id,)).fetchone()
    if not row:
        return None
    path = Path(row["path"])
    if not path.exists() or not path.is_file():
        return None
    kind = str(row["kind"] or "").strip().lower()
    if kind not in {"image", "video", "audio"}:
        kind = guess_kind(path, row["mime"])
    if kind not in {"image", "video", "audio"}:
        return None
    asset_url = f"/assets/{row['id']}"
    return {
        "assetId": row["id"],
        "assetUrl": asset_url,
        "previewUrl": asset_url,
        "resultUrl": asset_url,
        "previewKind": kind,
        "previewLabel": {
            "video": "预览视频",
            "audio": "预览音频",
            "image": "预览图片",
        }.get(kind, "打开结果"),
    }


def newapi_local_job_preview_for_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not newapi_task_item_is_success(item):
        return None
    job_id = newapi_job_id_from_client_task_id(newapi_task_id_from_item(item))
    if not job_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = row_job(row)
    if job.get("status") != "completed":
        return None
    output = job.get("output") if isinstance(job.get("output"), dict) else {}
    asset_id = local_asset_id_from_output(output)
    return newapi_local_asset_preview(asset_id)


def enrich_newapi_usage_item_with_local_preview(item: Dict[str, Any]) -> Dict[str, Any]:
    local_preview = newapi_local_job_preview_for_item(item)
    if not local_preview:
        return item
    enriched = dict(item)
    original_preview_url = str(enriched.get("previewUrl") or "").strip()
    original_result_url = str(enriched.get("resultUrl") or enriched.get("result_url") or "").strip()
    if original_preview_url and original_preview_url != local_preview["previewUrl"]:
        enriched.setdefault("remotePreviewUrl", original_preview_url)
    if original_result_url and original_result_url != local_preview["resultUrl"]:
        enriched.setdefault("remoteResultUrl", original_result_url)
    enriched.update(local_preview)
    return enriched


def enrich_newapi_usage_with_local_previews(usage: Dict[str, Any]) -> Dict[str, Any]:
    items = usage.get("items") if isinstance(usage.get("items"), list) else []
    enriched_items = [
        enrich_newapi_usage_item_with_local_preview(item) if isinstance(item, dict) else item
        for item in items
    ]
    return {
        **usage,
        "items": enriched_items,
    }


def newapi_local_media_proxy_url(value: Any, *, download: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path if parsed.scheme else text.split("?", 1)[0]
    media_match = re.match(r"^/api/media-assets/(\d+)/content$", path, re.I)
    if media_match:
        suffix = "?download=1" if download else ""
        return f"/newapi/media-assets/{media_match.group(1)}/content{suffix}"
    task_match = re.match(r"^/v1/videos/([^/?#]+)/content$", path, re.I)
    if task_match:
        task_id = quote(unquote(task_match.group(1)), safe="")
        suffix = "?download=1" if download else ""
        return f"/newapi/task-videos/{task_id}/content{suffix}"
    return ""


def rewrite_newapi_media_asset_for_local_proxy(asset: Dict[str, Any]) -> Dict[str, Any]:
    rewritten = dict(asset)
    preview_url = str(rewritten.get("previewUrl") or rewritten.get("preview_url") or "").strip()
    download_url = str(rewritten.get("downloadUrl") or rewritten.get("download_url") or preview_url).strip()
    local_preview_url = newapi_local_media_proxy_url(preview_url, download=False)
    local_download_url = newapi_local_media_proxy_url(download_url, download=True) or newapi_local_media_proxy_url(preview_url, download=True)
    if local_preview_url:
        rewritten.setdefault("remotePreviewUrl", preview_url)
        rewritten["previewUrl"] = local_preview_url
    if local_download_url:
        rewritten.setdefault("remoteDownloadUrl", download_url)
        rewritten["downloadUrl"] = local_download_url
    return rewritten


def enrich_newapi_consumption_item_with_media_proxy(item: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(item)
    assets = enriched.get("mediaAssets") if isinstance(enriched.get("mediaAssets"), list) else []
    rewritten_assets = [
        rewrite_newapi_media_asset_for_local_proxy(asset) if isinstance(asset, dict) else asset
        for asset in assets
    ]
    enriched["mediaAssets"] = rewritten_assets

    preview_url = str(enriched.get("previewUrl") or "").strip()
    download_url = str(enriched.get("downloadUrl") or preview_url).strip()
    local_preview_url = newapi_local_media_proxy_url(preview_url, download=False)
    local_download_url = newapi_local_media_proxy_url(download_url, download=True) or newapi_local_media_proxy_url(preview_url, download=True)
    if local_preview_url:
        enriched.setdefault("remotePreviewUrl", preview_url)
        enriched["previewUrl"] = local_preview_url
    if local_download_url:
        enriched.setdefault("remoteDownloadUrl", download_url)
        enriched["downloadUrl"] = local_download_url
    return enriched


def enrich_newapi_consumption_with_media_proxy(consumption: Dict[str, Any]) -> Dict[str, Any]:
    items = consumption.get("items") if isinstance(consumption.get("items"), list) else []
    enriched_items = [
        enrich_newapi_consumption_item_with_media_proxy(item) if isinstance(item, dict) else item
        for item in items
    ]
    return {
        **consumption,
        "items": enriched_items,
    }


def proxy_newapi_remote_content(remote_path: str, *, download: bool = False) -> StreamingResponse:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    api_key = str(account.get("defaultApiKey") or "").strip()
    if not is_usable_newapi_api_key(api_key):
        raise HTTPException(status_code=400, detail="当前账号没有可用于预览下载的默认 API Key")
    clean_path = "/" + str(remote_path or "").lstrip("/")
    if not re.match(r"^/(api/media-assets/\d+/content|v1/videos/[^/?#]+/content)$", clean_path):
        raise HTTPException(status_code=400, detail="预览下载地址不合法")

    base_url = normalize_newapi_base_url(account["baseUrl"])
    token = api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"
    client = httpx.Client(timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS, follow_redirects=True, trust_env=provider_http_trust_env())
    try:
        request = client.build_request(
            "GET",
            f"{base_url}{clean_path}",
            headers={"Authorization": token},
            params={"download": "1"} if download else None,
        )
        response = client.send(request, stream=True)
    except httpx.HTTPError as error:
        client.close()
        raise HTTPException(status_code=502, detail=f"读取中转站媒体失败：{error}") from error

    if response.status_code >= 400:
        try:
            detail = response.read().decode("utf-8", errors="ignore")[:300]
        finally:
            response.close()
            client.close()
        raise HTTPException(status_code=response.status_code if response.status_code < 500 else 502, detail=detail or "读取中转站媒体失败")

    headers: Dict[str, str] = {}
    for header_name in ("content-disposition", "cache-control", "accept-ranges"):
        header_value = response.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    def close_remote_response() -> None:
        response.close()
        client.close()

    return StreamingResponse(
        response.iter_bytes(),
        media_type=response.headers.get("content-type") or "application/octet-stream",
        headers=headers,
        background=BackgroundTask(close_remote_response),
    )


def newapi_task_result_url_from_item(item: Dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    candidates = [
        item.get("previewUrl"),
        item.get("resultUrl"),
        item.get("result_url"),
        item.get("url"),
        raw.get("result_url"),
        raw.get("resultUrl"),
        raw.get("video_url"),
        raw.get("videoUrl"),
        raw.get("image_url"),
        raw.get("imageUrl"),
        raw.get("url"),
    ]
    for value in candidates:
        if isinstance(value, str):
            text = value.strip()
            if re.match(r"^(https?://|data:|/assets/)", text, re.I):
                return text
    return ""


def newapi_task_asset_kind_from_item(item: Dict[str, Any], job_type: str) -> str:
    raw = str(item.get("previewKind") or item.get("assetKind") or item.get("kind") or "").strip().lower()
    if raw in {"image", "video", "audio"}:
        return raw
    text = " ".join(str(item.get(key) or "") for key in ("type", "action", "detail", "previewLabel")).lower()
    if "audio" in text or "音频" in text:
        return "audio"
    if "image" in text or "图片" in text:
        return "image"
    if "video" in text or "视频" in text:
        return "video"
    return "video" if str(job_type or "").startswith("video.") else "image"


def newapi_task_failure_reason_from_item(item: Dict[str, Any]) -> str:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    candidates = [
        item.get("errorDetail"),
        item.get("error_detail"),
        item.get("failReason"),
        item.get("fail_reason"),
        item.get("detail"),
        item.get("message"),
        raw.get("fail_reason"),
        raw.get("failReason"),
        raw.get("detail"),
        raw.get("result_url"),
        raw.get("resultUrl"),
    ]
    for value in candidates:
        if isinstance(value, str):
            text = value.strip()
            if text and text != "-" and not text.startswith("点击预览"):
                return text
        elif value:
            return json.dumps(value, ensure_ascii=False)
    return ""


def lookup_newapi_video_task_terminal_result(job_id: str, job_type: str, provider: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not str(job_type or "").strip().lower().startswith("video."):
        return None
    if str((provider or {}).get("id") or "").strip().lower() != "newapi":
        return None
    task_id = newapi_client_task_id_for_job(job_id)
    if not task_id:
        return None
    client: Optional[NewApiClient] = None
    try:
        client = get_newapi_client()
        usage = client.get_task_history(limit=10, task_id=task_id)
    except Exception:
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    items = usage.get("items") if isinstance(usage, dict) else []
    if not isinstance(items, list):
        return None
    fallback: Optional[Dict[str, Any]] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        terminal: Optional[Dict[str, Any]] = None
        if newapi_task_item_is_success(item):
            url = newapi_task_result_url_from_item(item)
            if url:
                terminal = {
                    "status": "completed",
                    "output": {
                        "provider": "newapi",
                        "providerModelId": item.get("modelName") or item.get("model") or "",
                        "providerModelName": item.get("modelName") or "",
                        "status": "completed",
                        "assetKind": newapi_task_asset_kind_from_item(item, job_type),
                        "url": url,
                        "urls": [url],
                        "remoteTaskId": task_id,
                        "raw": {"officialTask": item},
                    },
                }
        elif newapi_task_item_is_failed(item):
            reason = newapi_task_failure_reason_from_item(item)
            if reason:
                terminal = {"status": "failed", "error": reason}
        if not terminal:
            continue
        if newapi_task_item_matches_id(item, task_id):
            return terminal
        if fallback is None:
            fallback = terminal
    return fallback


def lookup_newapi_video_task_failure_reason(job_id: str, job_type: str, provider: Dict[str, Any]) -> Optional[str]:
    terminal = lookup_newapi_video_task_terminal_result(job_id, job_type, provider)
    if terminal and terminal.get("status") == "failed":
        return str(terminal.get("error") or "").strip() or None
    return None


def get_newapi_client() -> NewApiClient:
    account = get_newapi_account(include_secret=True)
    if not account or not account.get("accessToken") or account.get("userId") is None:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    return NewApiClient(
        account["baseUrl"],
        access_token=account["accessToken"],
        user_id=int(account["userId"]),
    )


def get_logged_in_newapi_client() -> NewApiClient:
    account = get_newapi_account(include_secret=True)
    if not account or not account.get("accessToken") or account.get("userId") is None:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    return NewApiClient(
        account["baseUrl"],
        access_token=account["accessToken"],
        user_id=int(account["userId"]),
    )


def upsert_newapi_account(
    *,
    base_url: str,
    login_info: Optional[Dict[str, Any]] = None,
    user: Optional[Dict[str, Any]] = None,
    models: Optional[List[str]] = None,
    tokens: Optional[List[Dict[str, Any]]] = None,
    default_api_key: Optional[str] = None,
    registration_aff_code: Optional[str] = None,
) -> Dict[str, Any]:
    base_url = normalize_newapi_base_url(base_url)
    existing = get_newapi_account(include_secret=True)
    now = utc_now()
    login_info = login_info or {}
    safe_user = sanitize_newapi_user(user)
    meta = {**((existing or {}).get("meta") or {})}
    if user is not None:
        meta["user"] = safe_user
    if models is not None:
        filtered_models = [model for model in models if not is_removed_provider_model_id(model)]
        meta["availableModels"] = filtered_models[:500]
        meta["availableModelCount"] = len(filtered_models)
    if tokens is not None:
        meta["tokens"] = tokens[:200]
        meta["tokenCount"] = len(tokens)
    meta["lastRefreshAt"] = now

    access_token = login_info.get("access_token") or (existing or {}).get("accessToken") or ""
    user_id = login_info.get("user_id") or safe_user.get("id") or (existing or {}).get("userId")
    username = login_info.get("username") or safe_user.get("username") or (existing or {}).get("username") or ""
    role = login_info.get("role") if login_info.get("role") is not None else safe_user.get("role")
    if role is None:
        role = (existing or {}).get("role")
    group_name = login_info.get("group") or safe_user.get("group") or (existing or {}).get("group") or ""
    existing_user_id = (existing or {}).get("userId")
    new_identity = bool(user_id and existing_user_id and str(user_id) != str(existing_user_id))
    registration_aff_code = (registration_aff_code or "").strip()
    safe_inviter_id = int(safe_user.get("inviterId") or 0)
    if registration_aff_code or safe_inviter_id:
        invite_binding = dict(meta.get("inviteBinding") or {})
        invite_binding["adminBound"] = True
        invite_binding["source"] = "registration_aff_code" if registration_aff_code else "inviter_id"
        invite_binding["updatedAt"] = now
        if registration_aff_code:
            invite_binding["code"] = registration_aff_code
        if safe_inviter_id:
            invite_binding["inviterId"] = safe_inviter_id
        meta["inviteBinding"] = invite_binding
    elif user is not None and new_identity:
        meta.pop("inviteBinding", None)
    stored_default_key = default_api_key if default_api_key is not None else (existing or {}).get("defaultApiKey") or ""
    created_at = (existing or {}).get("createdAt") or now

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO newapi_accounts(
              id, base_url, user_id, username, role, group_name,
              access_token_encrypted, default_api_key_encrypted, meta_json, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              base_url = excluded.base_url,
              user_id = excluded.user_id,
              username = excluded.username,
              role = excluded.role,
              group_name = excluded.group_name,
              access_token_encrypted = excluded.access_token_encrypted,
              default_api_key_encrypted = excluded.default_api_key_encrypted,
              meta_json = excluded.meta_json,
              updated_at = excluded.updated_at
            """,
            (
                NEWAPI_ACCOUNT_ID,
                base_url,
                user_id,
                username,
                role,
                group_name,
                encode_secret(access_token),
                encode_secret(stored_default_key),
                json_dump(meta),
                created_at,
                now,
            ),
        )
        conn.commit()
    sync_newapi_file()
    account = get_newapi_account()
    if not account:
        raise HTTPException(status_code=500, detail="中转站账号保存失败")
    return account


def sync_newapi_provider_from_account(base_url: str, api_key: str) -> Dict[str, Any]:
    clean_base_url = normalize_newapi_base_url(base_url)
    api_key = require_usable_newapi_api_key(api_key)
    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT meta_json FROM providers WHERE id = ?", (NEWAPI_PROVIDER_ID,)).fetchone()
        meta = json_load(row["meta_json"], {}) if row else {}
        meta.update({
            "adapterFamily": "openai",
            "managedBy": "newapi",
            "managedAt": now,
            "description": meta.get("description") or "漫创AI 中转站，OpenAI 兼容请求，后续只需替换 Base URL",
        })
        conn.execute(
            """
            INSERT INTO providers(id, name, base_url, auth_type, api_key_encrypted, enabled, capabilities_json, meta_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              base_url = excluded.base_url,
              auth_type = excluded.auth_type,
              api_key_encrypted = excluded.api_key_encrypted,
              enabled = excluded.enabled,
              capabilities_json = excluded.capabilities_json,
              meta_json = excluded.meta_json,
              updated_at = excluded.updated_at
            """,
            (
                NEWAPI_PROVIDER_ID,
                "漫创AI 中转站",
                clean_base_url,
                "bearer",
                encode_secret(api_key),
                1,
                json_dump(["text.generate", "image.generate", "video.generate"]),
                json_dump(meta),
                now,
                now,
            ),
        )
        conn.commit()
    sync_provider_files()
    sync_result: Optional[Dict[str, Any]] = None
    try:
        client = NewApiClient(clean_base_url)
        try:
            records = client.list_openai_model_records(api_key)
        finally:
            client.close()
        sync_result = sync_newapi_remote_provider_models(records)
        existing_account = get_newapi_account()
        if existing_account and normalize_newapi_base_url(existing_account.get("baseUrl") or "") == clean_base_url:
            existing_available_models = clean_newapi_model_codes(
                ((existing_account.get("meta") or {}).get("availableModels"))
            )
            upsert_newapi_account(
                base_url=clean_base_url,
                models=newapi_model_codes_from_sources(records, existing_available_models),
            )
    except Exception:
        sync_result = None
    provider = get_provider(NEWAPI_PROVIDER_ID)
    if sync_result:
        provider["modelSync"] = sync_result
    return provider


def clear_newapi_account() -> None:
    managed_by_newapi = False
    with connect() as conn:
        row = conn.execute("SELECT meta_json FROM providers WHERE id = ?", (NEWAPI_PROVIDER_ID,)).fetchone()
        if row:
            managed_by_newapi = json_load(row["meta_json"], {}).get("managedBy") == "newapi"
        conn.execute("DELETE FROM newapi_accounts WHERE id = ?", (NEWAPI_ACCOUNT_ID,))
        if managed_by_newapi:
            now = utc_now()
            meta = json_load(row["meta_json"], {}) if row else {}
            meta["managedBy"] = ""
            meta["managedAt"] = now
            conn.execute(
                """
                UPDATE providers
                SET api_key_encrypted = '', enabled = 0, meta_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dump(meta), now, NEWAPI_PROVIDER_ID),
            )
        conn.commit()
    sync_newapi_file()
    sync_provider_files()


def read_newapi_snapshot(client: NewApiClient, token_size: int = 50) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "user": {},
        "models": [],
        "tokens": [],
        "warnings": [],
    }
    try:
        snapshot["user"] = client.get_user_info()
    except NewApiError as error:
        snapshot["warnings"].append(f"用户信息读取失败：{error.message}")
    try:
        snapshot["models"] = client.list_available_models()
    except NewApiError as error:
        snapshot["warnings"].append(f"模型列表读取失败：{error.message}")
    try:
        snapshot["tokens"] = newapi_token_response(client.list_api_keys(size=token_size))
    except NewApiError as error:
        snapshot["warnings"].append(f"API Key 列表读取失败：{error.message}")
    return snapshot


def newapi_token_field(token: Any, *names: str) -> Any:
    if isinstance(token, dict):
        for name in names:
            if name in token:
                return token.get(name)
        return None
    for name in names:
        if hasattr(token, name):
            return getattr(token, name)
    return None


def newapi_token_is_active(token: Any) -> bool:
    status = newapi_token_field(token, "status")
    if status is None or status == "":
        return True
    try:
        return int(status) == 1
    except (TypeError, ValueError):
        return bool(status)


def newapi_token_is_unlimited(token: Any) -> bool:
    return bool(newapi_token_field(token, "unlimitedQuota", "unlimited_quota"))


def resolve_newapi_default_key_from_tokens(
    client: NewApiClient,
    tokens: List[Any],
    *,
    require_unlimited: bool = False,
) -> str:
    get_full_key = getattr(client, "get_api_key", None)
    for token in tokens or []:
        if not newapi_token_is_active(token):
            continue
        if require_unlimited and not newapi_token_is_unlimited(token):
            continue
        raw_key = (
            newapi_token_field(token, "skKey", "sk_key", "key", "apiKey", "api_key")
            or ""
        )
        candidate = normalize_newapi_api_key(str(raw_key))
        if is_usable_newapi_api_key(candidate):
            return candidate

        token_id = newapi_token_field(token, "id", "tokenId", "token_id")
        has_key = bool(newapi_token_field(token, "hasKey", "has_key") or raw_key or newapi_token_field(token, "keyPreview"))
        if not token_id or not has_key or not callable(get_full_key):
            continue
        try:
            full_key = normalize_newapi_api_key(get_full_key(int(token_id)))
        except (NewApiError, TypeError, ValueError):
            continue
        if is_usable_newapi_api_key(full_key):
            return full_key
    return ""


def existing_newapi_default_key_for_base(
    base_url: str,
    *,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
) -> str:
    existing = get_newapi_account(include_secret=True)
    if not existing:
        return ""
    if normalize_newapi_base_url(existing.get("baseUrl") or "") != normalize_newapi_base_url(base_url):
        return ""
    if user_id is not None and existing.get("userId") is not None:
        try:
            if int(existing.get("userId")) != int(user_id):
                return ""
        except (TypeError, ValueError):
            return ""
    clean_username = (username or "").strip()
    if clean_username and (existing.get("username") or "").strip() and (existing.get("username") or "").strip() != clean_username:
        return ""
    default_key = normalize_newapi_api_key(existing.get("defaultApiKey") or "")
    return default_key if is_usable_newapi_api_key(default_key) else ""


def ensure_newapi_default_key(
    client: NewApiClient,
    snapshot: Dict[str, Any],
    *,
    base_url: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    create_if_missing: bool = True,
) -> str:
    tokens = snapshot.get("tokens") or []
    default_key = resolve_newapi_default_key_from_tokens(client, tokens, require_unlimited=True)
    if default_key:
        return default_key

    if not create_if_missing:
        return existing_newapi_default_key_for_base(base_url, user_id=user_id, username=username)

    try:
        created = client.create_api_key(
            name="漫创AI 默认 Key",
            remain_quota=0,
            unlimited_quota=True,
        )
    except NewApiError as error:
        snapshot.setdefault("warnings", []).append(f"默认 API Key 创建失败：{error.message}")
        return existing_newapi_default_key_for_base(base_url, user_id=user_id, username=username)

    created_tokens = newapi_token_response([created])
    snapshot["tokens"] = created_tokens + [token for token in tokens if newapi_token_field(token, "id") != created.id]
    created_key = normalize_newapi_api_key(str(
        newapi_token_field(created, "skKey", "sk_key", "key", "apiKey", "api_key") or ""
    ))
    if is_usable_newapi_api_key(created_key):
        return created_key
    return resolve_newapi_default_key_from_tokens(client, [created], require_unlimited=True)


def sync_newapi_default_key_if_usable(base_url: str, api_key: str) -> Optional[Dict[str, Any]]:
    clean_key = normalize_newapi_api_key(api_key or "")
    if not is_usable_newapi_api_key(clean_key):
        return None
    return sync_newapi_provider_from_account(base_url, clean_key)


def get_project_payload(project_id: str) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        nodes = [
            json_load(row["state_json"], {})
            for row in conn.execute("SELECT * FROM nodes WHERE project_id = ? ORDER BY rowid", (project_id,))
        ]
        edges = [
            {"id": row["id"], "from": row["from_node_id"], "to": row["to_node_id"]}
            for row in conn.execute("SELECT * FROM edges WHERE project_id = ? ORDER BY rowid", (project_id,))
        ]
        asset_rows = conn.execute("SELECT * FROM assets WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
        assets = [
            record
            for record in (
                {
                **json_load(row["meta_json"], {}),
                "id": row["id"],
                "projectId": row["project_id"],
                "kind": row["kind"],
                "path": row["path"],
                "thumbPath": row["thumb_path"],
                "mime": row["mime"],
                "size": row["size"],
                }
                for row in asset_rows
            )
            if is_asset_library_record(record)
        ]
        history = [
            row_history(row)
            for row in conn.execute("SELECT * FROM history WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        ]
        jobs = [
            {
                "id": row["id"],
                "projectId": row["project_id"],
                "nodeId": row["node_id"],
                "type": row["type"],
                "status": row["status"],
                "progress": row["progress"],
                "input": json_load(row["input_json"], {}),
                "output": json_load(row["output_json"], {}),
                "error": row["error"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in conn.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        ]
        prompts = [
            row_prompt(row)
            for row in conn.execute(
                "SELECT * FROM prompts WHERE project_id = ? OR scope = 'global' ORDER BY updated_at DESC",
                (project_id,),
            )
        ]
        metadata = project_metadata_from_row(project)
    return {
        "project": row_project(project),
        "nodes": nodes,
        "edges": edges,
        "assets": assets,
        "history": history,
        "jobs": jobs,
        "prompts": prompts,
        "designSpacePackage": metadata.get("designSpacePackage"),
    }


def list_project_summaries() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE id != ? ORDER BY updated_at DESC",
            (GLOBAL_ASSET_PROJECT_ID,),
        ).fetchall()
        summaries: List[Dict[str, Any]] = []
        for row in rows:
            project_id = row["id"]
            node_count = conn.execute("SELECT COUNT(*) AS count FROM nodes WHERE project_id = ?", (project_id,)).fetchone()["count"]
            edge_count = conn.execute("SELECT COUNT(*) AS count FROM edges WHERE project_id = ?", (project_id,)).fetchone()["count"]
            asset_counts = conn.execute(
                """
                SELECT
                    COUNT(*) AS asset_count,
                    SUM(CASE WHEN kind = 'image' THEN 1 ELSE 0 END) AS image_count,
                    SUM(CASE WHEN kind = 'video' THEN 1 ELSE 0 END) AS video_count
                FROM assets
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            history_count = conn.execute("SELECT COUNT(*) AS count FROM history WHERE project_id = ?", (project_id,)).fetchone()["count"]
            summaries.append({
                **row_project(row),
                "nodeCount": node_count,
                "edgeCount": edge_count,
                "assetCount": int(asset_counts["asset_count"] or 0),
                "imageCount": int(asset_counts["image_count"] or 0),
                "videoCount": int(asset_counts["video_count"] or 0),
                "historyCount": history_count,
            })
    return summaries


def restore_project_graph_to_db(project_id: str, graph: Dict[str, Any]) -> None:
    project_id = validate_project_id(project_id)
    project = graph.get("project") if isinstance(graph.get("project"), dict) else {}
    now = utc_now()
    project_name = (project.get("name") or "未命名").strip() or "未命名"
    created_at = project.get("createdAt") or project.get("created_at") or now
    updated_at = graph.get("updatedAt") or project.get("updatedAt") or project.get("updated_at") or now
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    assets = graph.get("assets") if isinstance(graph.get("assets"), list) else []
    history = graph.get("history") if isinstance(graph.get("history"), list) else []
    metadata = {}
    design_space_package = design_space_package_from_graph(graph)
    if design_space_package is not None:
        metadata["designSpacePackage"] = design_space_package

    def as_float(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def as_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    ensure_project_dirs(project_id)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(id, name, created_at, updated_at, version, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (project_id, project_name, created_at, updated_at, PROJECT_STORAGE_VERSION, json_dump(metadata)),
        )
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id") or f"node_{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO nodes(id, project_id, type, title, x, y, w, h, params_json, state_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    project_id,
                    node.get("type") or "unknown",
                    node.get("title") or "",
                    as_float(node.get("x"), 0),
                    as_float(node.get("y"), 0),
                    as_float(node.get("w"), 320),
                    as_float(node.get("h"), 220),
                    json_dump(node.get("params") or {}),
                    json_dump({**node, "id": node_id}),
                ),
            )
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            if not edge.get("from") or not edge.get("to"):
                continue
            conn.execute(
                "INSERT INTO edges(id, project_id, from_node_id, to_node_id) VALUES(?, ?, ?, ?)",
                (
                    edge.get("id") or f"edge_{uuid.uuid4().hex[:8]}",
                    project_id,
                    edge.get("from"),
                    edge.get("to"),
                ),
            )
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            asset_id = asset.get("id") or f"asset_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    project_id,
                    asset.get("kind") or "file",
                    asset.get("path") or asset.get("src") or "",
                    asset.get("thumbPath"),
                    asset.get("mime"),
                    as_int(asset.get("size")),
                    json_dump({**asset, "id": asset_id, "projectId": project_id}),
                    asset.get("createdAt") or now,
                ),
            )
        for item in history:
            if not isinstance(item, dict):
                continue
            history_id = item.get("id") or f"hist_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO history(id, project_id, action, target_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    history_id,
                    project_id,
                    item.get("action") or "asset.record",
                    item.get("targetId") or item.get("assetId") or item.get("nodeId"),
                    json_dump({**item, "id": history_id, "projectId": project_id}),
                    item.get("createdAt") or item.get("time") or now,
                ),
            )
        conn.commit()


def recover_projects_from_storage_files() -> None:
    if not PROJECTS_DIR.exists():
        return
    with connect() as conn:
        existing_ids = {
            row["id"]
            for row in conn.execute("SELECT id FROM projects")
        }
    for child in PROJECTS_DIR.iterdir():
        if not child.is_dir() or child.name in existing_ids:
            continue
        if not SAFE_PROJECT_ID.match(child.name) or child.name == GLOBAL_ASSET_PROJECT_ID:
            continue
        graph_path = child / "graph.json"
        if not graph_path.exists():
            continue
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            if not isinstance(graph, dict):
                continue
            restore_project_graph_to_db(child.name, graph)
            existing_ids.add(child.name)
        except Exception as error:
            print(f"Failed to recover project from {graph_path}: {error}", flush=True)


def sync_project_files(project_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
    project_id = validate_project_id(project_id)
    ensure_project_dirs(project_id)
    data = payload or get_project_payload(project_id)
    root = project_dir(project_id)
    project = data.get("project") or {}
    project_manifest = {
        **project,
        "storageVersion": PROJECT_STORAGE_VERSION,
        "folders": {
            "assets": "assets",
            "thumbs": "thumbs",
            "exports": "exports",
            "cache": "cache",
        },
    }
    write_json_file(root / "project.json", project_manifest)
    write_json_file(root / "graph.json", {
        "version": PROJECT_STORAGE_VERSION,
        "project": project,
        "nodes": data.get("nodes") or [],
        "edges": data.get("edges") or [],
        "assets": data.get("assets") or [],
        "history": data.get("history") or [],
        "designSpacePackage": data.get("designSpacePackage"),
        "updatedAt": utc_now(),
    })
    write_json_file(root / "assets" / "index.json", data.get("assets") or [])
    write_json_file(root / "history.json", data.get("history") or [])
    write_json_file(root / "jobs.json", data.get("jobs") or [])
    write_json_file(root / "prompts.json", data.get("prompts") or [])
    sync_global_prompt_file()


def sync_saved_graph_files(project_id: str, payload: Dict[str, Any]) -> None:
    project_id = validate_project_id(project_id)
    ensure_project_dirs(project_id)
    root = project_dir(project_id)
    project = payload.get("project") or {}
    project_manifest = {
        **project,
        "storageVersion": PROJECT_STORAGE_VERSION,
        "folders": {
            "assets": "assets",
            "thumbs": "thumbs",
            "exports": "exports",
            "cache": "cache",
        },
    }
    write_json_file(root / "project.json", project_manifest)
    write_json_file(root / "graph.json", {
        "version": PROJECT_STORAGE_VERSION,
        "project": project,
        "nodes": payload.get("nodes") or [],
        "edges": payload.get("edges") or [],
        "assets": payload.get("assets") or [],
        "history": payload.get("history") or [],
        "designSpacePackage": payload.get("designSpacePackage"),
        "updatedAt": payload.get("updatedAt") or utc_now(),
    })
    write_json_file(root / "assets" / "index.json", payload.get("assets") or [])
    write_json_file(root / "history.json", payload.get("history") or [])


def sync_project_library_files(project_id: str, assets: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> None:
    project_id = validate_project_id(project_id)
    ensure_project_dirs(project_id)
    root = project_dir(project_id)
    write_json_file(root / "assets" / "index.json", assets or [])
    write_json_file(root / "history.json", history or [])


def try_sync_project_files(project_id: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    try:
        sync_project_files(project_id, payload)
        return True
    except PermissionError as error:
        print(f"[libai] project snapshot sync skipped: {error}")
        return False


def asset_record_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    meta = json_load(row["meta_json"], {})
    kind = normalized_asset_kind(row["kind"], row["path"], row["mime"])
    return {
        **meta,
        "id": row["id"],
        "projectId": row["project_id"],
        "kind": kind,
        "path": row["path"],
        "thumbPath": row["thumb_path"],
        "mime": row["mime"],
        "size": row["size"],
        "assetUrl": meta.get("assetUrl") or f"/assets/{row['id']}",
        "createdAt": meta.get("createdAt") or row["created_at"],
    }


def sync_global_asset_file() -> None:
    ensure_project_dirs(GLOBAL_ASSET_PROJECT_ID)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM assets WHERE project_id = ? ORDER BY created_at DESC",
            (GLOBAL_ASSET_PROJECT_ID,),
        ).fetchall()
    root = asset_scope_dir(GLOBAL_ASSET_PROJECT_ID)
    write_json_file(root / "index.json", {
        "scope": "global",
        "projectId": GLOBAL_ASSET_PROJECT_ID,
        "updatedAt": utc_now(),
        "assets": [record for record in (asset_record_from_row(row) for row in rows) if is_asset_library_record(record)],
    })


def sync_asset_scope_files(project_id: str) -> None:
    project_id = validate_project_id(project_id)
    if is_global_asset_project(project_id):
        sync_global_asset_file()
        return
    sync_project_files(project_id)


def sync_asset_record_files(project_id: str, record: Optional[Dict[str, Any]]) -> None:
    project_id = validate_project_id(project_id)
    if is_global_asset_project(project_id) or is_asset_library_record(record):
        sync_asset_scope_files(project_id)


def list_prompts(scope: Optional[str] = None, project_id: Optional[str] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM prompts WHERE 1 = 1"
    params: List[Any] = []
    if scope:
        query += " AND scope = ?"
        params.append(scope)
    if project_id:
        validate_project_id(project_id)
        query += " AND (project_id = ? OR scope = 'global')"
        params.append(project_id)
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY updated_at DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [row_prompt(row) for row in rows]


def sync_global_prompt_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_file(DATA_DIR / "prompt-library.json", list_prompts(scope="global"))


def sync_prompt_files_for_record(record: Dict[str, Any]) -> None:
    sync_global_prompt_file()
    project_id = record.get("projectId")
    if project_id:
        try:
            sync_project_files(project_id)
        except HTTPException:
            pass


def guess_kind(file_path: Path, mime: Optional[str]) -> str:
    value = mime or ""
    if value.startswith("image/"):
        return "image"
    if value.startswith("video/"):
        return "video"
    if value.startswith("audio/"):
        return "audio"
    suffix = file_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix in {".mp4", ".mov", ".webm", ".avi", ".mkv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".m4a", ".flac", ".aac"}:
        return "audio"
    return "file"


IMPORTABLE_ASSET_KINDS = {"image", "video", "audio"}


def normalized_asset_kind(kind: Optional[str], file_path: Any, mime: Optional[str]) -> str:
    declared = (kind or "").strip().lower()
    guessed = guess_kind(Path(str(file_path or "")), mime)
    if guessed in IMPORTABLE_ASSET_KINDS:
        return guessed
    return declared or guessed


IMPORTABLE_ASSET_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
    ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v",
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg",
}


def validate_importable_asset(source: Path, mime: Optional[str], kind: str) -> None:
    normalized_kind = (kind or "").strip().lower()
    suffix = source.suffix.lower()
    mime_text = (mime or "").lower()
    if normalized_kind not in IMPORTABLE_ASSET_KINDS:
        raise HTTPException(status_code=400, detail="不支持导入该类型文件")
    if suffix not in IMPORTABLE_ASSET_SUFFIXES and not mime_text.startswith(("image/", "video/", "audio/")):
        raise HTTPException(status_code=400, detail="不支持导入该类型文件")


def decode_data_url(data_url: str) -> tuple[bytes, Optional[str]]:
    if "," not in data_url:
        raise HTTPException(status_code=400, detail="Invalid data url")
    header, encoded = data_url.split(",", 1)
    mime = None
    if header.startswith("data:"):
        mime = header[5:].split(";", 1)[0] or None
    try:
        return base64.b64decode(encoded, validate=True), mime
    except Exception as error:
        raise HTTPException(status_code=400, detail="Invalid base64 payload") from error


def safe_asset_suffix(filename: str, mime: Optional[str]) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    guessed = mimetypes.guess_extension(mime or "") or ".bin"
    return guessed if re.fullmatch(r"\.[a-z0-9]{1,10}", guessed) else ".bin"


def path_within(child: Path, parent: Path) -> bool:
    try:
        child_path = os.path.normcase(str(Path(child).resolve()))
        parent_path = os.path.normcase(str(Path(parent).resolve()))
        return os.path.commonpath([child_path, parent_path]) == parent_path
    except (OSError, ValueError):
        return False


def is_managed_asset_storage_path(project_id: str, candidate: Path) -> bool:
    root = asset_scope_dir(project_id)
    return (
        path_within(candidate, root / "assets")
        or path_within(candidate, root / "thumbs")
    )


def schedule_deferred_asset_copy(asset_id: str, project_id: str, source: Path, target: Path) -> None:
    def worker() -> None:
        try:
            if not source.exists() or not source.is_file():
                raise FileNotFoundError(str(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            if Path(source).resolve() != Path(target).resolve():
                shutil.copy2(source, target)
            size = target.stat().st_size
            with connect() as conn:
                row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
                if not row:
                    return
                meta = json_load(row["meta_json"], {})
                if meta.get("storageMode") != "linked" or not meta.get("copyPending"):
                    return
                meta["storageMode"] = "managed"
                meta["copyPending"] = False
                meta["managedPath"] = str(target)
                meta.pop("copyError", None)
                conn.execute(
                    "UPDATE assets SET path = ?, size = ?, meta_json = ? WHERE id = ?",
                    (str(target), size, json_dump(meta), asset_id),
                )
                conn.commit()
            sync_asset_record_files(project_id, meta)
        except Exception as error:
            try:
                with connect() as conn:
                    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
                    if not row:
                        return
                    meta = json_load(row["meta_json"], {})
                    meta["storageMode"] = "linked"
                    meta["copyPending"] = False
                    meta["copyError"] = exception_message(error, "Deferred asset copy failed")
                    conn.execute(
                        "UPDATE assets SET meta_json = ? WHERE id = ?",
                        (json_dump(meta), asset_id),
                    )
                    conn.commit()
                sync_asset_record_files(project_id, meta)
            except Exception:
                pass

    threading.Thread(
        target=worker,
        name=f"libai-asset-copy-{asset_id}",
        daemon=True,
    ).start()


NODE_DEFINITIONS = [
    {
        "type": "text.note",
        "legacyType": "text",
        "title": "文本节点",
        "category": "text",
        "inputs": [],
        "outputs": ["text"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "text_passthrough",
        "panel": "TextNodePanel",
    },
    {
        "type": "text.reason",
        "legacyType": "text",
        "title": "文本推理",
        "category": "text",
        "inputs": ["text", "image"],
        "outputs": ["text"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID},
        "executor": "text_reason_executor",
        "panel": "TextReasonPanel",
    },
    {
        "type": "inference.generate",
        "legacyType": "text",
        "title": "通用推理",
        "category": "utility",
        "inputs": ["text", "image", "video", "audio"],
        "outputs": ["text"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID},
        "executor": "inference_generate_executor",
        "panel": "InferenceGeneratePanel",
    },
    {
        "type": "image.input",
        "legacyType": "image",
        "title": "图片输入",
        "category": "image",
        "inputs": ["image"],
        "outputs": ["image"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "asset_passthrough",
        "panel": "ImageNodePanel",
    },
    {
        "type": "image.analyze",
        "legacyType": None,
        "title": "图片标签分析",
        "category": "utility",
        "inputs": ["image"],
        "outputs": ["metadata"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID},
        "executor": "image_analyze_executor",
        "panel": "FocusAnalyzePanel",
    },
    {
        "type": "image.generate",
        "legacyType": "image",
        "title": "图片生成",
        "category": "image",
        "inputs": ["text", "image"],
        "outputs": ["image"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID, "ratio": "16:9"},
        "executor": "image_generate_executor",
        "panel": "ImageGeneratePanel",
    },
    {
        "type": "image.upscale",
        "legacyType": None,
        "title": "高清放大",
        "category": "image",
        "inputs": ["image"],
        "outputs": ["image"],
        "paramsSchema": {},
        "defaultParams": {"provider": LOCAL_UPSCALE_PROVIDER_ID, "scale": 2, "contentType": "portrait", "model": "realesrgan-x4plus"},
        "executor": "image_upscale_executor",
        "panel": "ImageUpscalePanel",
    },
    {
        "type": "video.generate",
        "legacyType": "video",
        "title": "视频生成",
        "category": "video",
        "inputs": ["text", "image", "video"],
        "outputs": ["video"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID, "duration": 5},
        "executor": "video_generate_executor",
        "panel": "VideoGeneratePanel",
    },
    {
        "type": "audio.input",
        "legacyType": "audio",
        "title": "音频输入",
        "category": "audio",
        "inputs": ["audio"],
        "outputs": ["audio"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "asset_passthrough",
        "panel": "AudioNodePanel",
    },
    {
        "type": "audio.generate",
        "legacyType": "audio",
        "title": "音频生成",
        "category": "audio",
        "inputs": ["text", "audio"],
        "outputs": ["audio"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID},
        "executor": "audio_generate_executor",
        "panel": "AudioGeneratePanel",
    },
    {
        "type": "script.storyboard",
        "legacyType": "script",
        "title": "分镜脚本",
        "category": "text",
        "inputs": ["text", "image"],
        "outputs": ["text"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "script_storyboard_executor",
        "panel": "StoryboardPanel",
    },
    {
        "type": "panorama.generate",
        "legacyType": "vr720-gen",
        "title": "720空间场景",
        "category": "image",
        "inputs": ["image"],
        "outputs": ["image"],
        "paramsSchema": {},
        "defaultParams": {"provider": NEWAPI_PROVIDER_ID, "ratio": "16:9", "style": "realistic_cinematic"},
        "executor": "panorama_image_generate_executor",
        "panel": "VR720GenPanel",
    },
    {
        "type": "panorama.viewer",
        "legacyType": "panorama-viewer",
        "title": "720全景预览",
        "category": "image",
        "inputs": ["image"],
        "outputs": ["image"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "panorama_viewer_local",
        "panel": "PanoramaViewerPanel",
    },
    {
        "type": "stage.director",
        "legacyType": "director-stage",
        "title": "旧版720工作台",
        "category": "utility",
        "inputs": ["image"],
        "outputs": ["text", "image"],
        "paramsSchema": {},
        "defaultParams": {},
        "executor": "director_stage_local",
        "panel": "DirectorStageWorkbench",
    },
]

PROVIDER_SEEDS = [
    {
        "id": LOCAL_UPSCALE_PROVIDER_ID,
        "name": "本地高清放大",
        "baseUrl": "local://upscale",
        "authType": "none",
        "enabled": True,
        "capabilities": ["image.upscale"],
        "meta": {"local": True, "description": "Real-ESRGAN ncnn Vulkan 本地高清放大，自动识别图片格式和显存分块"},
    },
    {
        "id": NEWAPI_PROVIDER_ID,
        "name": "漫创AI 中转站",
        "baseUrl": DEFAULT_NEWAPI_BASE_URL,
        "authType": "bearer",
        "enabled": False,
        "capabilities": ["text.generate", "image.analyze", "image.generate", "video.generate"],
        "meta": {"adapterFamily": "openai", "description": "漫创AI 中转站，OpenAI 兼容请求，后续只需替换 Base URL"},
    },
    {
        "id": GHOSTCUT_PROVIDER_ID,
        "name": "GhostCut 去字幕中转",
        "baseUrl": DEFAULT_NEWAPI_BASE_URL,
        "authType": "bearer",
        "enabled": True,
        "capabilities": ["video.subtitle.remove"],
        "meta": {"adapterFamily": "ghostcut", "description": "通过漫创AI中转站调用 GhostCut 去字幕接口，桌面端不保存 GhostCut 密钥"},
    },
]

def grok_video_variant_seed(spec: Dict[str, Any]) -> Dict[str, Any]:
    model_id = spec["model"]
    seconds = int(spec["seconds"])
    ratio = spec["ratio"]
    label_ratio = "Portrait" if ratio == "9:16" else "Landscape"
    quality_suffix = " HD" if spec.get("quality") == "hd" else ""
    return {
        "id": model_id,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": model_id,
        "displayName": f"Grok Imagine 1.0 Video {label_ratio}{quality_suffix} {seconds}s",
        "adapter": "grok2api.video",
        "enabled": True,
        "params": {
            "defaultDuration": seconds,
            "defaultSeconds": seconds,
            "supportedDurations": [seconds],
            "defaultSize": spec["size"],
            "sizes": [spec["size"]],
            "ratios": [ratio],
            "defaultResolutionName": "720p",
            "defaultPreset": "normal",
            "maxReferenceImages": 7,
            "upstreamModelName": GROK_VIDEO_MODEL_ID,
            "preferProviderCredentials": False,
        },
        "meta": {
            "aliases": [model_id, f"grok {label_ratio.lower()} {seconds}s", f"Grok {label_ratio} {seconds} 秒视频"],
            "managedBy": "codex.grok2api",
            "remoteModel": True,
            "source": "grok2api-compatible",
            "hiddenFromPicker": True,
            "groupedBy": GROK_VIDEO_MODEL_ID,
            "pricing": {"label": "按当前 NewAPI 后台价格表计费"},
        },
    }


GROK_VIDEO_MODEL_SEEDS = [
    {
        "id": GROK_VIDEO_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": GROK_VIDEO_MODEL_ID,
        "displayName": "Grok Imagine Video",
        "adapter": "grok2api.video",
        "enabled": True,
        "params": {**GROK_VIDEO_DEFAULT_PARAMS},
        "meta": {
            "aliases": ["grok", GROK_VIDEO_MODEL_ID, "Grok 视频", "Grok Imagine Video"],
            "managedBy": "codex.grok2api",
            "remoteModel": True,
            "source": "grok2api-compatible",
            "pricing": {"label": "按当前 NewAPI 后台价格表计费"},
        },
    },
    *[grok_video_variant_seed(spec) for spec in GROK_VIDEO_VARIANT_SPECS],
]


def muse_video_model_seed(model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    api_version = spec.get("apiVersion") or "v1"
    params = MUSE_VIDEO_V2_PROTOCOL_PARAMS if api_version == "v2" else MUSE_VIDEO_V1_PROTOCOL_PARAMS
    spec_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    return {
        "id": model_id,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": spec["modelName"],
        "displayName": spec.get("displayName") or spec["modelName"],
        "adapter": "newapi.video",
        "enabled": spec.get("enabled", True),
        "params": {**params, **spec_params},
        "meta": {
            "aliases": [spec["modelName"], *(spec.get("aliases") or [])],
            "managedBy": "codex.muse-video",
            "remoteModel": True,
            "source": "muse-apifox",
            "pricing": {"label": "按当前 NewAPI 后台价格表计费"},
        },
    }


def low_price_jimeng_video_model_seed(model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": model_id,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": spec["modelName"],
        "displayName": spec.get("displayName") or spec["modelName"],
        "adapter": "newapi.video",
        "enabled": True,
        "params": {**LOW_PRICE_JIMENG_PROTOCOL_PARAMS},
        "meta": {
            "aliases": [spec["modelName"], *(spec.get("aliases") or [])],
            "managedBy": "codex.low-price-jimeng",
            "remoteModel": True,
            "source": "low-price-jimeng",
            "pricing": {"label": "按时长固定收费（4s-15s）"},
        },
    }


def shishikeji_channel4_video_model_seed(model_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    model_price = float(spec["modelPrice"])
    admin_price = float(spec["adminPrice"])
    display_price = int(model_price) if model_price.is_integer() else model_price
    label = f"模型价格 ￥{display_price} / 次"
    return {
        "id": model_id,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": model_id,
        "displayName": spec.get("displayName") or model_id,
        "adapter": "newapi.video",
        "enabled": True,
        "params": {
            **SHISHIKEJI_CHANNEL4_PROTOCOL_BASE_PARAMS,
            "upstreamModelName": spec["upstreamModelName"],
        },
        "meta": {
            "aliases": [model_id, spec["upstreamModelName"]],
            "managedBy": "codex.shishikeji-channel4",
            "remoteModel": True,
            "source": "shishikeji-channel4",
            "pricing": {
                "quotaType": 1,
                "modelPrice": model_price,
                "adminPrice": admin_price,
                "billingUnit": "request",
                "source": "shishikeji-channel4",
                "label": label,
            },
            "priceLabel": label,
        },
    }


PROVIDER_MODEL_SEEDS = [
    {
        "id": LOCAL_UPSCALE_MODEL_ID,
        "providerId": LOCAL_UPSCALE_PROVIDER_ID,
        "capability": "image.upscale",
        "modelName": "realesrgan-x4plus",
        "displayName": "Real-ESRGAN 高清放大",
        "adapter": LOCAL_UPSCALE_MODEL_ID,
        "enabled": True,
        "params": {"assetKind": "image", "scales": [2, 3, 4], "contentTypes": ["portrait", "anime", "general"], "format": "auto", "tile": "auto", "fallback": "pillow-lanczos"},
        "meta": {"aliases": ["高清放大", "真人照片放大", "动漫插画放大", "Real-ESRGAN", "realesrgan-x4plus", "本地放大"], "pricing": {"label": "本地处理 / 不消耗中转站余额"}},
    },
    {
        "id": GHOSTCUT_SUBTITLE_MODEL_ID,
        "providerId": GHOSTCUT_PROVIDER_ID,
        "capability": "video.subtitle.remove",
        "modelName": "ghostcut-subtitle-remove",
        "displayName": "GhostCut 去字幕",
        "adapter": GHOSTCUT_SUBTITLE_ADAPTER,
        "enabled": True,
        "params": {
            "submitPath": "/api/ghostcut/subtitle-remove",
            "statusPathTemplate": "/api/ghostcut/subtitle-remove/{taskId}",
            "defaultModel": "advanced_lite",
            "timeoutSeconds": 3600,
            "submitTimeoutSeconds": 600,
        },
        "meta": {
            "aliases": ["去字幕", "GhostCut", "字幕擦除", "字幕去除"],
            "pricing": {"label": "按 GhostCut / 中转站实际计费"},
        },
    },
    {
        "id": "gpt-5.5",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "text.generate",
        "modelName": "gpt-5.5",
        "displayName": "GPT 5.5",
        "adapter": "openai.responses",
        "enabled": True,
        "params": {"supportsImageInput": True, "apiEndpoint": "chat.completions"},
        "meta": {"aliases": ["GPT 5.5", "gpt-5.5", "gpt-5-5", "漫创AI GPT 5.5"], "pricing": {"label": "￥5.00 输入 / ￥30.00 输出 · 1M tokens", "source": "openai-pricing"}},
    },
    {
        "id": IMAGE_ANALYZE_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "image.analyze",
        "modelName": "gpt-5.5",
        "displayName": "GPT 5.5 视觉分析",
        "adapter": "openai.responses",
        "enabled": True,
        "params": {"supportsImageInput": True},
        "meta": {"aliases": ["视觉分析", "图片标签分析", "image.analyze", "focus analyze", "GPT 5.5 视觉分析"], "pricing": {"label": "￥5.00 输入 / ￥30.00 输出 · 1M tokens", "source": "openai-pricing"}},
    },
    {
        "id": "gpt-image-2",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "image.generate",
        "modelName": "gpt-image-2",
        "displayName": "GPT Image 2",
        "adapter": "openai.image",
        "enabled": True,
        "params": {
            "sizes": ["1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152", "3840x2160", "2160x3840", "2880x2880", "auto"],
            "supportedResolutions": ["1K", "2K", "4K"],
            "defaultResolutionName": "2K",
            "maxReferenceImages": DEFAULT_GPT_IMAGE_2_REFERENCE_LIMIT,
            "quality": "medium",
        },
        "meta": {"aliases": ["GPT Image 2", "gpt-image-2", "漫创AI Image"], "pricing": {"label": "图像输入 ￥8.00 / 1M tokens · 输出 ￥30.00 / 1M tokens", "source": "openai-pricing"}},
    },
    {
        "id": "nanobanana2",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "image.generate",
        "modelName": "nanobanana2",
        "displayName": "NanoBanana 2",
        "adapter": "openai.image",
        "enabled": True,
        "params": {**NANOBANANA_IMAGE_PARAMS},
        "meta": {
            "aliases": ["nanobanana2", "NanoBanana 2"],
            "pricing": {"label": "1K 0.02 / 2K 0.04 / 4K 0.06 积分/次", "source": "aiapidev-docs"},
        },
    },
    {
        "id": "nanobananapro",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "image.generate",
        "modelName": "nanobananapro",
        "displayName": "NanoBanana Pro",
        "adapter": "openai.image",
        "enabled": True,
        "params": {**NANOBANANA_IMAGE_PARAMS},
        "meta": {
            "aliases": ["nanobananapro", "NanoBanana Pro"],
            "pricing": {"label": "1K 0.02 / 2K 0.04 / 4K 0.06 积分/次", "source": "aiapidev-docs"},
        },
    },
    {
        "id": SEEDANCE_FAST_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": SEEDANCE_FAST_MODEL_ID,
        "displayName": "Seedance 2.0 Fast",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {
            **SEEDANCE_FAST_PROTOCOL_PARAMS,
        },
        "meta": {
            "aliases": ["seedance-2.0-fast", "Seedance 2.0 Fast", "即梦视频"],
            "pricing": {"label": "按时长固定收费（4s-15s）", "source": "seedance-guide"},
            "priceLabel": "按时长固定收费（4s-15s）",
        },
    },
    {
        "id": HERMES_SEEDANCE_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": HERMES_SEEDANCE_REQUEST_MODEL_NAME,
        "displayName": HERMES_SEEDANCE_REQUEST_MODEL_NAME,
        "adapter": "newapi.video",
        "enabled": True,
        "params": {
            **HERMES_SEEDANCE_PROTOCOL_PARAMS,
        },
        "meta": {
            "aliases": [HERMES_SEEDANCE_MODEL_ID, HERMES_SEEDANCE_REQUEST_MODEL_NAME, "不过真人满血渠道"],
            "remoteModel": True,
            "source": "hermes-video",
            "pricing": {
                "billingUnit": "request",
                "billingMode": "fixed_15s",
                "durationSeconds": 15,
                "label": "按次计费，固定 15秒",
                "source": "hermes-docs",
            },
            "priceLabel": "按次计费，固定 15秒",
            "modelIntroduction": "按次计费，固定 15秒；支持图片、视频、音频全能参考",
        },
    },
    *[
        muse_video_model_seed(model_id, spec)
        for model_id, spec in MUSE_VIDEO_MODEL_SPECS.items()
        if spec.get("seed", True)
    ],
    *[
        low_price_jimeng_video_model_seed(model_id, spec)
        for model_id, spec in LOW_PRICE_JIMENG_VIDEO_MODEL_SPECS.items()
    ],
    *[
        shishikeji_channel4_video_model_seed(model_id, spec)
        for model_id, spec in SHISHIKEJI_CHANNEL4_MODEL_SPECS.items()
    ],
    {
        "id": GROK3_VIDEO_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": GROK3_VIDEO_MODEL_ID,
        "displayName": "Grok3 Video",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {**GROK3_VIDEO_PROTOCOL_PARAMS},
        "meta": {
            "aliases": ["grok3-video", "Grok3 Video", "Grok 视频"],
            "remoteModel": True,
            "source": "newapi-video-relay",
            "pricing": {
                "quotaType": 1,
                "modelPrice": 0.3,
                "adminPrice": 0.2,
                "source": "manual",
                "label": "模型价格 $0.30 / 次，管理员成本 $0.20 / 次",
            },
            "priceLabel": "模型价格 $0.30 / 次",
        },
    },
    {
        "id": GROK15_VIDEO_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": GROK15_VIDEO_MODEL_ID,
        "displayName": "Grok Imagine Video 1.5",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {**GROK15_VIDEO_PROTOCOL_PARAMS},
        "meta": {
            "aliases": [
                GROK15_VIDEO_MODEL_ID,
                GROK15_VIDEO_UPSTREAM_MODEL_ID,
                "Grok Imagine Video 1.5",
                "Grok 1.5 Video",
                "Grok 1.5 视频",
            ],
            "remoteModel": True,
            "source": "newapi-video-relay",
            "pricing": {
                "quotaType": 1,
                "modelPrice": 0.35,
                "adminPrice": 0.3,
                "source": "manual",
                "label": "模型价格 $0.35 / 次，管理员成本 $0.30 / 次",
            },
            "priceLabel": "模型价格 $0.35 / 次",
        },
    },
    *GROK_VIDEO_MODEL_SEEDS,
    {
        "id": "grok-imagine-1.0-video-landscape[hd]-10s",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": "grok-imagine-1.0-video-landscape[hd]-10s",
        "displayName": "Grok Imagine 1.0 Video Landscape HD 10s",
        "adapter": "grok2api.video",
        "enabled": True,
        "params": {
            "defaultDuration": 10,
            "defaultSeconds": 10,
            "supportedDurations": [10],
            "defaultSize": "1792x1024",
            "sizes": ["1792x1024"],
            "ratios": ["16:9"],
            "defaultResolutionName": "720p",
            "defaultPreset": "normal",
            "maxReferenceImages": 7,
            "upstreamModelName": "grok-imagine-video",
            "preferProviderCredentials": False,
        },
        "meta": {
            "aliases": [
                "grok-imagine-1.0-video-landscape[hd]-10s",
                "grok landscape hd 10s",
                "Grok 横屏 HD 10 秒视频",
            ],
            "managedBy": "codex.grok2api",
            "remoteModel": True,
            "source": "grok2api-compatible",
            "pricing": {"label": "按当前 NewAPI 后台价格表计费"},
        },
    },
    {
        "id": "veo31",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": "veo31",
        "displayName": "Veo 3.1",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {
            "taskType": "video_generation",
            "supportedDurations": [4, 6, 8],
            "supportedReferenceModes": ["frame", "image"],
            "defaultDuration": 8,
            "defaultReferenceMode": "frame",
            "defaultGenerateAudio": True,
            "maxReferenceImages": 3,
        },
        "meta": {
            "aliases": ["veo31"],
            "pricing": {"label": "按后台价格表计费", "source": "aiapidev-docs"},
        },
    },
    {
        "id": VEO31_FAST_MODEL_ID,
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": VEO31_FAST_MODEL_ID,
        "displayName": "Veo 3.1 Fast",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {**VEO31_FAST_PROTOCOL_PARAMS},
        "meta": {
            "aliases": ["veo31-fast", "veo31fast"],
            "pricing": {"label": "按后台价格表计费", "source": "aiapidev-docs"},
        },
    },
    {
        "id": "veo31ref",
        "providerId": NEWAPI_PROVIDER_ID,
        "capability": "video.generate",
        "modelName": "veo31ref",
        "displayName": "Veo 3.1 Ref",
        "adapter": "newapi.video",
        "enabled": True,
        "params": {
            "taskType": "video_generation",
            "supportedDurations": [4, 6, 8],
            "supportedReferenceModes": ["frame", "image"],
            "defaultDuration": 8,
            "defaultReferenceMode": "frame",
            "defaultGenerateAudio": True,
            "maxReferenceImages": 3,
        },
        "meta": {
            "aliases": ["veo31ref", "veo3.1-ref", "veo-3.1-ref", "Veo 3.1 Ref"],
            "pricing": {"label": "按后台价格表计费", "source": "aiapidev-docs"},
        },
    },
]


def canonical_provider_model_id(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered.startswith(f"{OLD_PROVIDER_ID}."):
        lowered = f"{NEWAPI_PROVIDER_ID}.{lowered.split('.', 1)[1]}"
    if lowered.startswith(f"{NEWAPI_PROVIDER_ID}."):
        lowered = lowered.split(".", 1)[1]
    return MODEL_ID_ALIASES.get(lowered, lowered if lowered else text)


def is_removed_provider_model_id(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    canonical = canonical_provider_model_id(text)
    if canonical in REMOVED_PROVIDER_MODEL_IDS:
        return True
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-").lower()
    return bool(cleaned and canonical_provider_model_id(cleaned) in REMOVED_PROVIDER_MODEL_IDS)


def removed_provider_model_replacement_ids(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    canonical = canonical_provider_model_id(text)
    if canonical not in REMOVED_PROVIDER_MODEL_IDS:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-").lower()
        canonical = canonical_provider_model_id(cleaned)
    if canonical == SEEDANCE_FAST_MODEL_ID:
        return [SEEDANCE_DASH_FAST_MODEL_ID, SEEDANCE_DASH_MODEL_ID]
    if canonical == SEEDANCE_PRO_MODEL_ID:
        return [SEEDANCE_DASH_PRO_MODEL_ID]
    return []


def sanitize_model_label(value: Any, model_id: str = "", model_name: str = "") -> str:
    text = str(value or "").strip()
    canonical = canonical_provider_model_id(model_id or model_name)
    if canonical == "gpt-image-2":
        return "GPT Image 2"
    if canonical == "gpt-5.5":
        return "GPT 5.5"
    if canonical == "nanobanana2":
        return "NanoBanana 2"
    if canonical == "nanobananapro":
        return "NanoBanana Pro"
    if "GPT Image 2" in text:
        return "GPT Image 2"
    if "GPT 5.5" in text or "gpt-5.5" in text.lower() or "gpt-5-5" in text.lower():
        return "GPT 5.5"
    if has_legacy_model_text(text):
        return ""
    return text.split(" · ", 1)[0].strip()


def has_legacy_model_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if ("本地" in text and "预览" in text) or "加载模型" in text:
        return True
    old_markers = (
        OLD_PROVIDER_ID,
        "local" + "-preview",
        "ge" + "mini",
        "cla" + "ude",
        "kl" + "ing",
        "gr" + "ok",
        "gpt 5." + "1",
        "gpt 5." + "2",
        "gpt image 2 " + "all",
        "lib " + "image",
        "lib" + "nano",
        "gv" + "lm",
        "qwen" + "3",
        "openai." + "image",
    )
    return any(marker in text for marker in old_markers)


def fallback_node_title(node_type: str) -> str:
    if node_type == "image":
        return "图片生成"
    if node_type == "video":
        return "视频生成"
    if node_type == "audio":
        return "音频节点"
    if node_type == "script":
        return "分镜脚本"
    if node_type == "text":
        return "文本节点"
    return "节点"


def sanitize_node_state(state: Dict[str, Any]) -> Dict[str, Any]:
    changed = False
    next_state = dict(state)
    provider_model_id = canonical_provider_model_id(next_state.get("providerModelId") or next_state.get("modelId") or "")
    model_name = str(next_state.get("modelName") or "").strip()
    if provider_model_id in DEFAULT_PUBLIC_PROVIDER_MODEL_IDS:
        for key in ("providerModelId", "modelId"):
            if next_state.get(key) != provider_model_id:
                next_state[key] = provider_model_id
                changed = True
        expected_name = provider_model_id
        if provider_model_id == "gpt-5.5":
            expected_name = "gpt-5.5"
        if next_state.get("modelName") != expected_name:
            next_state["modelName"] = expected_name
            changed = True
        if next_state.get("provider") != NEWAPI_PROVIDER_ID:
            next_state["provider"] = NEWAPI_PROVIDER_ID
            changed = True
    elif str(next_state.get("provider") or "").strip().lower() == OLD_PROVIDER_ID:
        next_state.pop("provider", None)
        next_state.pop("providerModelId", None)
        next_state.pop("modelId", None)
        next_state.pop("modelName", None)
        changed = True

    provider_id = str(next_state.get("provider") or "").strip()
    if provider_id and provider_id not in ACTIVE_PROVIDER_IDS:
        next_state.pop("provider", None)
        changed = True

    for key in ("model", "workbenchModel"):
        cleaned = sanitize_model_label(next_state.get(key), provider_model_id, model_name)
        if cleaned != (next_state.get(key) or ""):
            if cleaned:
                next_state[key] = cleaned
            else:
                next_state.pop(key, None)
            changed = True

    node_type = str(next_state.get("type") or "").strip()
    for key in ("title", "displayName"):
        if has_legacy_model_text(next_state.get(key)):
            next_state[key] = fallback_node_title(node_type)
            changed = True
    for key in ("body", "generatedText", "prompt", "promptDraft"):
        if has_legacy_model_text(next_state.get(key)):
            next_state.pop(key, None)
            changed = True
    if has_legacy_model_text(next_state.get("error")):
        next_state["error"] = "旧生成任务失败，已清理，请重新生成"
        changed = True

    return next_state if changed else state


def sanitize_generation_record(record: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(record, dict):
        return record
    next_record = dict(record)
    changed = False
    candidate_model_id = canonical_provider_model_id(
        next_record.get("providerModelId")
        or next_record.get("provider_model_id")
        or next_record.get("modelId")
        or next_record.get("model_id")
        or next_record.get("modelName")
        or next_record.get("model")
        or next_record.get("providerModelName")
    )

    if candidate_model_id in DEFAULT_PUBLIC_PROVIDER_MODEL_IDS:
        label = "GPT Image 2" if candidate_model_id == "gpt-image-2" else "GPT 5.5"
        for key in ("providerModelId", "modelId", "modelName"):
            if next_record.get(key) != candidate_model_id:
                next_record[key] = candidate_model_id
                changed = True
        if next_record.get("provider") != NEWAPI_PROVIDER_ID:
            next_record["provider"] = NEWAPI_PROVIDER_ID
            changed = True
        for key in ("model", "providerModelName", "displayName"):
            if next_record.get(key) and next_record.get(key) != label:
                next_record[key] = label
                changed = True
    else:
        legacy_provider = str(next_record.get("provider") or "").strip().lower() == OLD_PROVIDER_ID
        legacy_text = any(
            has_legacy_model_text(next_record.get(key))
            for key in ("model", "modelName", "providerModelName", "displayName", "providerModelId", "modelId")
        )
        if legacy_provider or legacy_text:
            for key in ("provider", "providerModelId", "provider_model_id", "modelId", "model_id", "modelName", "model", "providerModelName", "displayName"):
                if key in next_record:
                    del next_record[key]
                    changed = True

    return next_record if changed else record


def cleanup_provider_defaults(conn: sqlite3.Connection) -> None:
    now = utc_now()
    current = conn.execute(
        "SELECT base_url FROM providers WHERE id = ?",
        (NEWAPI_PROVIDER_ID,),
    ).fetchone()
    if current:
        current_base_url = str(current["base_url"] or "").rstrip("/")
        if not current_base_url or current_base_url in LEGACY_NEWAPI_BASE_URLS:
            conn.execute(
                "UPDATE providers SET base_url = ?, updated_at = ? WHERE id = ?",
                (DEFAULT_NEWAPI_BASE_URL, now, NEWAPI_PROVIDER_ID),
            )
    account = conn.execute("SELECT base_url FROM newapi_accounts WHERE id = ?", (NEWAPI_ACCOUNT_ID,)).fetchone()
    if account and str(account["base_url"] or "").rstrip("/") in LEGACY_NEWAPI_BASE_URLS:
        conn.execute(
            "UPDATE newapi_accounts SET base_url = ?, updated_at = ? WHERE id = ?",
            (DEFAULT_NEWAPI_BASE_URL, now, NEWAPI_ACCOUNT_ID),
        )

    conn.execute(
        f"DELETE FROM provider_models WHERE provider_id NOT IN ({','.join('?' for _ in ACTIVE_PROVIDER_IDS)})",
        tuple(ACTIVE_PROVIDER_IDS),
    )
    conn.execute(
        "DELETE FROM provider_models WHERE provider_id = ? AND id <> ?",
        (LOCAL_UPSCALE_PROVIDER_ID, LOCAL_UPSCALE_MODEL_ID),
    )
    if REMOVED_PROVIDER_MODEL_IDS:
        conn.execute(
            f"DELETE FROM provider_models WHERE id IN ({','.join('?' for _ in REMOVED_PROVIDER_MODEL_IDS)})",
            tuple(REMOVED_PROVIDER_MODEL_IDS),
        )
    if SERVER_SCOPED_MUSE_VIDEO_MODEL_IDS:
        placeholders = ",".join("?" for _ in SERVER_SCOPED_MUSE_VIDEO_MODEL_IDS)
        rows = conn.execute(
            f"SELECT id, meta_json FROM provider_models WHERE id IN ({placeholders})",
            tuple(SERVER_SCOPED_MUSE_VIDEO_MODEL_IDS),
        ).fetchall()
        for row in rows:
            meta = json_load(row["meta_json"], {})
            if meta.get("managedBy") == "newapi.remote-models":
                continue
            conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
    migrate_seedance_video_provider_models(conn, now)
    migrate_public_video_provider_models(conn, now)
    migrate_channel24_seedance_provider_models(conn, now)
    remove_stale_channel24_local_seedance_models(conn)
    migrate_muse_video_provider_models(conn, now)
    migrate_firefly_video_provider_models(conn, now)
    legacy_model_rows = conn.execute(
        "SELECT id, provider_id, model_name, display_name, meta_json FROM provider_models"
    ).fetchall()
    for row in legacy_model_rows:
        if row["provider_id"] == LOCAL_UPSCALE_PROVIDER_ID:
            continue
        canonical_id = canonical_provider_model_id(row["id"])
        canonical_name_id = canonical_provider_model_id(row["model_name"])
        if canonical_id == row["id"] and canonical_name_id != row["model_name"]:
            canonical_id = canonical_name_id
        if row["provider_id"] == NEWAPI_PROVIDER_ID and canonical_id != row["id"]:
            canonical_exists = conn.execute("SELECT 1 FROM provider_models WHERE id = ?", (canonical_id,)).fetchone()
            if canonical_exists:
                conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
                continue
        if row["provider_id"] == NEWAPI_PROVIDER_ID and is_unsupported_grok_video_model(row["id"], row["model_name"]):
            conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
            continue
        if row["provider_id"] == NEWAPI_PROVIDER_ID and any(
            is_removed_provider_model_id(row[key])
            for key in ("id", "model_name", "display_name")
        ):
            conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
            continue
        meta = json_load(row["meta_json"], {})
        if row["provider_id"] == NEWAPI_PROVIDER_ID and (
            meta.get("managedBy") == "newapi.remote-models" or meta.get("remoteModel") is True
        ):
            continue
        if row["provider_id"] == OLD_PROVIDER_ID or any(
            has_legacy_model_text(row[key])
            for key in ("id", "model_name", "display_name")
        ):
            conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
    conn.execute(
        f"DELETE FROM providers WHERE id NOT IN ({','.join('?' for _ in ACTIVE_PROVIDER_IDS)})",
        tuple(ACTIVE_PROVIDER_IDS),
    )

    forced_output_defaults = {
        "gpt-5.5": {
            "reasoningEffort": "medium",
            "textVerbosity": "medium",
        },
        IMAGE_ANALYZE_MODEL_ID: {
            "reasoningEffort": "medium",
            "textVerbosity": "low",
            "maxOutputTokens": 1800,
        },
    }
    for model_id, defaults in forced_output_defaults.items():
        row = conn.execute("SELECT params_json FROM provider_models WHERE id = ?", (model_id,)).fetchone()
        if not row:
            continue
        params = json_load(row["params_json"], {})
        cleaned = dict(params)
        for key, default_value in defaults.items():
            current_value = cleaned.get(key)
            if current_value == default_value or str(current_value or "") == str(default_value):
                cleaned.pop(key, None)
        if cleaned != params:
            conn.execute(
                "UPDATE provider_models SET params_json = ?, updated_at = ? WHERE id = ?",
                (json_dump(cleaned), now, model_id),
            )

    rows = conn.execute("SELECT id, state_json FROM nodes").fetchall()
    for row in rows:
        state = json_load(row["state_json"], {})
        cleaned = sanitize_node_state(state)
        if cleaned != state:
            conn.execute(
                "UPDATE nodes SET state_json = ? WHERE id = ?",
                (json_dump(cleaned), row["id"]),
            )

    job_rows = conn.execute("SELECT id, input_json, output_json, error FROM jobs").fetchall()
    for row in job_rows:
        input_payload = json_load(row["input_json"], {})
        output_payload = json_load(row["output_json"], {})
        cleaned_input = sanitize_generation_record(input_payload)
        cleaned_output = sanitize_generation_record(output_payload)
        error = row["error"]
        cleaned_error = "" if has_legacy_model_text(error) else error
        if cleaned_input != input_payload or cleaned_output != output_payload or cleaned_error != error:
            conn.execute(
                "UPDATE jobs SET input_json = ?, output_json = ?, error = ? WHERE id = ?",
                (json_dump(cleaned_input), json_dump(cleaned_output), cleaned_error, row["id"]),
            )


def migrate_firefly_video_provider_models(conn: sqlite3.Connection, now: str) -> None:
    rows = conn.execute(
        """
        SELECT id, provider_id, capability, model_name, adapter, params_json, meta_json
        FROM provider_models
        WHERE provider_id = ?
          AND (LOWER(id) LIKE 'firefly-%' OR LOWER(model_name) LIKE 'firefly-%')
        """,
        (NEWAPI_PROVIDER_ID,),
    ).fetchall()
    for row in rows:
        model_id = canonical_provider_model_id(row["id"])
        model_name = row["model_name"]
        if not (looks_like_firefly_video_model(model_id) or looks_like_firefly_video_model(model_name)):
            continue
        params = normalize_firefly_video_params(model_id, json_load(row["params_json"], {}))
        meta = json_load(row["meta_json"], {})
        normalized_meta = meta
        if (
            row["capability"] == "video.generate"
            and row["adapter"] == FIREFLY_VIDEO_ADAPTER
            and params == json_load(row["params_json"], {})
            and normalized_meta == meta
        ):
            continue
        conn.execute(
            """
            UPDATE provider_models
            SET capability = ?, adapter = ?, params_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            ("video.generate", FIREFLY_VIDEO_ADAPTER, json_dump(params), json_dump(normalized_meta), now, row["id"]),
        )


def normalize_second_based_video_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    if canonical_id not in SECOND_BASED_VIDEO_MODEL_IDS:
        return meta
    pricing = meta.get("pricing")
    if not isinstance(pricing, dict):
        return meta
    if newapi_int_value(pricing.get("quotaType")) != 0 or newapi_float_value(pricing.get("modelRatio")) is None:
        return meta
    normalized_pricing = newapi_remote_pricing_meta({"modelCode": model_id, "pricing": pricing})
    if not normalized_pricing:
        return meta
    merged_pricing = {**pricing, **normalized_pricing}
    label = str(merged_pricing.get("label") or "").strip()
    if not label:
        return meta
    return {**meta, "pricing": merged_pricing, "priceLabel": label}


def migrate_seedance_video_provider_models(conn: sqlite3.Connection, now: str) -> None:
    placeholders = ",".join("?" for _ in SEEDANCE_PROTOCOL_MODEL_IDS)
    rows = conn.execute(
        f"""
        SELECT id, capability, adapter, params_json, meta_json
        FROM provider_models
        WHERE id IN ({placeholders})
        """,
        tuple(SEEDANCE_PROTOCOL_MODEL_IDS),
    ).fetchall()
    for row in rows:
        params = json_load(row["params_json"], {})
        normalized = normalize_seedance_fast_params(row["id"], params)
        meta = json_load(row["meta_json"], {})
        normalized_meta = normalize_second_based_video_pricing_meta(row["id"], meta)
        if (
            normalized == params
            and normalized_meta == meta
            and row["capability"] == "video.generate"
            and row["adapter"] == "newapi.video"
        ):
            continue
        conn.execute(
            """
            UPDATE provider_models
            SET capability = ?, adapter = ?, params_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            ("video.generate", "newapi.video", json_dump(normalized), json_dump(normalized_meta), now, row["id"]),
        )


def migrate_public_video_provider_models(conn: sqlite3.Connection, now: str) -> None:
    placeholders = ",".join("?" for _ in PUBLIC_VIDEO_PROTOCOL_PARAMS)
    rows = conn.execute(
        f"""
        SELECT id, capability, adapter, params_json, meta_json
        FROM provider_models
        WHERE id IN ({placeholders})
        """,
        tuple(PUBLIC_VIDEO_PROTOCOL_PARAMS),
    ).fetchall()
    for row in rows:
        params = json_load(row["params_json"], {})
        normalized = normalize_public_video_params(row["id"], params)
        meta = json_load(row["meta_json"], {})
        normalized_meta = normalize_second_based_video_pricing_meta(row["id"], meta)
        normalized_meta = apply_xinghe_sora_pricing_meta(row["id"], normalized_meta)
        normalized_meta = apply_low_price_jimeng_pricing_meta(row["id"], normalized_meta)
        normalized_meta = apply_channel27_911_seedance_pricing_meta(row["id"], normalized_meta)
        normalized_meta = apply_channel31_face_pricing_meta(row["id"], normalized_meta)
        normalized_meta = apply_channel35_no_audio_special_pricing_meta(row["id"], normalized_meta)
        normalized_meta = apply_shishikeji_channel4_pricing_meta(row["id"], normalized_meta)
        if (
            normalized == params
            and normalized_meta == meta
            and row["capability"] == "video.generate"
            and row["adapter"] == "newapi.video"
        ):
            continue
        conn.execute(
            """
            UPDATE provider_models
            SET capability = ?, adapter = ?, params_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            ("video.generate", "newapi.video", json_dump(normalized), json_dump(normalized_meta), now, row["id"]),
        )


def migrate_channel24_seedance_provider_models(conn: sqlite3.Connection, now: str) -> None:
    placeholders = ",".join("?" for _ in CHANNEL24_SEEDANCE_MODEL_IDS)
    rows = conn.execute(
        f"""
        SELECT id, capability, adapter, model_name, display_name, params_json, meta_json
        FROM provider_models
        WHERE provider_id = ? AND id IN ({placeholders})
        """,
        (NEWAPI_PROVIDER_ID, *tuple(CHANNEL24_SEEDANCE_MODEL_IDS)),
    ).fetchall()
    for row in rows:
        params = json_load(row["params_json"], {})
        meta = json_load(row["meta_json"], {})
        normalized = apply_channel24_seedance_params(row["id"], row["model_name"], params)
        normalized_meta = apply_channel24_seedance_pricing_meta(row["id"], row["model_name"], meta)
        if (
            normalized == params
            and normalized_meta == meta
            and row["capability"] == "video.generate"
            and row["adapter"] == "newapi.video"
        ):
            continue
        conn.execute(
            """
            UPDATE provider_models
            SET capability = ?, adapter = ?, params_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            ("video.generate", "newapi.video", json_dump(normalized), json_dump(normalized_meta), now, row["id"]),
        )


def remove_stale_channel24_local_seedance_models(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in STALE_CHANNEL24_LOCAL_SEEDANCE_MODEL_IDS)
    rows = conn.execute(
        f"""
        SELECT id, meta_json
        FROM provider_models
        WHERE provider_id = ? AND id IN ({placeholders})
        """,
        (NEWAPI_PROVIDER_ID, *tuple(STALE_CHANNEL24_LOCAL_SEEDANCE_MODEL_IDS)),
    ).fetchall()
    for row in rows:
        meta = json_load(row["meta_json"], {})
        if meta.get("managedBy") != "codex.channel24.local":
            continue
        conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))


def migrate_muse_video_provider_models(conn: sqlite3.Connection, now: str) -> None:
    placeholders = ",".join("?" for _ in MUSE_VIDEO_MODEL_IDS)
    rows = conn.execute(
        f"""
        SELECT id, capability, adapter, params_json
        FROM provider_models
        WHERE id IN ({placeholders})
        """,
        tuple(MUSE_VIDEO_MODEL_IDS),
    ).fetchall()
    for row in rows:
        params = json_load(row["params_json"], {})
        normalized = normalize_muse_video_params(row["id"], params)
        canonical_id = canonical_provider_model_id(row["id"])
        target_enabled = 0 if MUSE_VIDEO_MODEL_SPECS.get(canonical_id, {}).get("enabled") is False else None
        if normalized == params and row["capability"] == "video.generate" and row["adapter"] == "newapi.video":
            if target_enabled is None:
                continue
            current_enabled = conn.execute("SELECT enabled FROM provider_models WHERE id = ?", (row["id"],)).fetchone()
            if current_enabled and int(current_enabled["enabled"]) == target_enabled:
                continue
        set_enabled_sql = ", enabled = ?" if target_enabled is not None else ""
        values: tuple[Any, ...]
        if target_enabled is not None:
            values = ("video.generate", "newapi.video", json_dump(normalized), target_enabled, now, row["id"])
        else:
            values = ("video.generate", "newapi.video", json_dump(normalized), now, row["id"])
        conn.execute(
            f"""
            UPDATE provider_models
            SET capability = ?, adapter = ?, params_json = ?{set_enabled_sql}, updated_at = ?
            WHERE id = ?
            """,
            values,
        )


def seed_provider_defaults(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for item in PROVIDER_SEEDS:
        conn.execute(
            """
            INSERT OR IGNORE INTO providers(id, name, base_url, auth_type, api_key_encrypted, enabled, capabilities_json, meta_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["name"],
                item["baseUrl"],
                item.get("authType", "bearer"),
                "",
                1 if item.get("enabled") else 0,
                json_dump(item.get("capabilities") or []),
                json_dump(item.get("meta") or {}),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT name, base_url, api_key_encrypted, capabilities_json, meta_json FROM providers WHERE id = ?", (item["id"],)).fetchone()
        if row:
            merged_capabilities = (
                item.get("capabilities") or []
                if item["id"] == NEWAPI_PROVIDER_ID
                else sorted(set(json_load(row["capabilities_json"], [])) | set(item.get("capabilities") or []))
            )
            merged_meta = {**json_load(row["meta_json"], {}), **(item.get("meta") or {})}
            conn.execute(
                "UPDATE providers SET capabilities_json = ?, meta_json = ?, updated_at = ? WHERE id = ?",
                (json_dump(merged_capabilities), json_dump(merged_meta), now, item["id"]),
            )
    for item in PROVIDER_MODEL_SEEDS:
        if is_removed_provider_model_id(item.get("id")):
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO provider_models(
              id, provider_id, capability, model_name, display_name, adapter, enabled,
              params_json, meta_json, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["providerId"],
                item["capability"],
                item["modelName"],
                item["displayName"],
                item["adapter"],
                1 if item.get("enabled", True) else 0,
                json_dump(item.get("params") or {}),
                json_dump(item.get("meta") or {}),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT params_json, meta_json FROM provider_models WHERE id = ?", (item["id"],)).fetchone()
        if row:
            merged_params = {**(item.get("params") or {}), **json_load(row["params_json"], {})}
            merged_params = normalize_nanobanana_params(item["id"], merged_params)
            merged_params = normalize_seedance_fast_params(item["id"], merged_params)
            merged_params = normalize_hermes_seedance_params(item["id"], merged_params)
            merged_params = normalize_muse_video_params(item["id"], merged_params)
            merged_params = normalize_veo31_fast_params(item["id"], merged_params)
            merged_params = normalize_public_video_params(item["id"], merged_params)
            merged_params = normalize_grok2api_main_account_params(item["id"], merged_params)
            merged_meta = {**(item.get("meta") or {}), **json_load(row["meta_json"], {})}
            merged_meta = normalize_provider_model_meta(item["id"], merged_meta)
            if canonical_provider_model_id(item["id"]) in GROK_VIDEO_VARIANT_IDS:
                merged_meta["hiddenFromPicker"] = True
                merged_meta["groupedBy"] = GROK_VIDEO_MODEL_ID
                merged_meta["managedBy"] = "codex.grok2api"
                merged_meta["source"] = "grok2api-compatible"
            elif canonical_provider_model_id(item["id"]) == GROK_VIDEO_MODEL_ID:
                merged_meta.pop("hiddenFromPicker", None)
                merged_meta["managedBy"] = "codex.grok2api"
                merged_meta["source"] = "grok2api-compatible"
            if (
                canonical_provider_model_id(item["id"]) in MAIN_ACCOUNT_GROK_MODEL_IDS
                or canonical_provider_model_id(item["id"]) in {"veo31", VEO31_FAST_MODEL_ID}
            ):
                conn.execute(
                    """
                    UPDATE provider_models
                    SET provider_id = ?, capability = ?, model_name = ?, display_name = ?, adapter = ?,
                        enabled = ?, params_json = ?, meta_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        item["providerId"],
                        item["capability"],
                        item["modelName"],
                        item["displayName"],
                        item["adapter"],
                        1 if item.get("enabled", True) else 0,
                        json_dump(merged_params),
                        json_dump(merged_meta),
                        now,
                        item["id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE provider_models SET params_json = ?, meta_json = ?, updated_at = ? WHERE id = ?",
                    (json_dump(merged_params), json_dump(merged_meta), now, item["id"]),
                )
    cleanup_provider_defaults(conn)


def normalize_provider_model_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    if canonical_id in {"veo31", VEO31_FAST_MODEL_ID}:
        seed = next(
            (
                item
                for item in PROVIDER_MODEL_SEEDS
                if canonical_provider_model_id(item["id"]) == canonical_id
            ),
            None,
        )
        seed_aliases = ((seed or {}).get("meta") or {}).get("aliases")
        if isinstance(seed_aliases, list):
            return {**meta, "aliases": seed_aliases}
        return meta
    if canonical_provider_model_id(model_id) == SEEDANCE_FAST_MODEL_ID:
        seed = next(
            (
                item
                for item in PROVIDER_MODEL_SEEDS
                if canonical_provider_model_id(item["id"]) == SEEDANCE_FAST_MODEL_ID
            ),
            None,
        )
        seed_meta = (seed or {}).get("meta") or {}
        seed_pricing = seed_meta.get("pricing")
        if isinstance(seed_pricing, dict):
            return {
                **meta,
                "aliases": seed_meta.get("aliases", meta.get("aliases", [])),
                "pricing": seed_pricing,
                "priceLabel": str(seed_pricing.get("label") or "").strip() or "按时长固定收费（4s-15s）",
            }
        return meta
    if canonical_provider_model_id(model_id) not in NANOBANANA_PROTOCOL_MODEL_IDS:
        return meta
    seed = next(
        (
            item
            for item in PROVIDER_MODEL_SEEDS
            if canonical_provider_model_id(item["id"]) == canonical_provider_model_id(model_id)
        ),
        None,
    )
    seed_aliases = ((seed or {}).get("meta") or {}).get("aliases")
    if not isinstance(seed_aliases, list):
        return meta
    return {**meta, "aliases": seed_aliases}


def normalize_nanobanana_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if canonical_provider_model_id(model_id) not in NANOBANANA_PROTOCOL_MODEL_IDS:
        return params
    seed = next(
        (
            item
            for item in PROVIDER_MODEL_SEEDS
            if canonical_provider_model_id(item["id"]) == canonical_provider_model_id(model_id)
        ),
        None,
    )
    if not seed:
        cleaned = dict(params)
        cleaned.pop("apiEndpoint", None)
        return cleaned
    cleaned = {**(seed.get("params") or {})}
    cleaned.pop("apiEndpoint", None)
    return cleaned


def normalize_seedance_fast_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    if canonical_id not in SEEDANCE_PROTOCOL_MODEL_IDS:
        return params
    normalized = {**SEEDANCE_FAST_PROTOCOL_PARAMS}
    upstream_model_name = SORA3_SEEDANCE_MODEL_ALIASES.get(canonical_id)
    if upstream_model_name:
        normalized["upstreamModelName"] = upstream_model_name
    return normalized


def normalize_hermes_seedance_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if canonical_provider_model_id(model_id) not in HERMES_SEEDANCE_MODEL_IDS:
        return params
    return {**params, **HERMES_SEEDANCE_PROTOCOL_PARAMS}


def normalize_muse_video_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    spec = MUSE_VIDEO_MODEL_SPECS.get(canonical_id)
    if not spec:
        return params
    defaults = MUSE_VIDEO_V2_PROTOCOL_PARAMS if spec.get("apiVersion") == "v2" else MUSE_VIDEO_V1_PROTOCOL_PARAMS
    spec_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    return {**params, **defaults, **spec_params}


def normalize_veo31_fast_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if canonical_provider_model_id(model_id) != VEO31_FAST_MODEL_ID:
        return params
    return {**params, **VEO31_FAST_PROTOCOL_PARAMS}


def normalize_firefly_video_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    profile = FIREFLY_VIDEO_PROTOCOL_PARAMS.get(canonical_provider_model_id(model_id))
    if not profile:
        return params
    return {**params, **profile}


def normalize_public_video_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    if canonical_id == SEEDANCE_DASH_FAST_MODEL_ID:
        return params
    profile = PUBLIC_VIDEO_PROTOCOL_PARAMS.get(canonical_id)
    if not profile:
        return params
    normalized = {**params, **profile}
    if canonical_id in {SEEDANCE_DASH_FAST_MODEL_ID, SEEDANCE_DASH_PRO_MODEL_ID}:
        normalized.pop("imageReferenceField", None)
    if canonical_id == GROK15_VIDEO_MODEL_ID:
        for key in (
            "defaultPreset",
            "imageReferenceField",
            "includePreset",
            "multiImageReferenceField",
            "referenceImageField",
        ):
            normalized.pop(key, None)
    return normalized


def normalize_grok2api_main_account_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if canonical_provider_model_id(model_id) not in MAIN_ACCOUNT_GROK_MODEL_IDS:
        return params
    return {**params, "preferProviderCredentials": False}


def merge_remote_meta_with_seed_defaults(meta: Dict[str, Any], seed_meta: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**meta, **seed_meta}
    if meta.get("pricing"):
        merged["pricing"] = meta["pricing"]
    if isinstance(meta.get("priceLabel"), str) and meta["priceLabel"].strip():
        merged["priceLabel"] = meta["priceLabel"].strip()
    return merged


def list_providers(include_secret: bool = False, include_internal: bool = False) -> List[Dict[str, Any]]:
    provider_ids = ACTIVE_PROVIDER_IDS if include_internal else PUBLIC_PROVIDER_IDS
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM providers
            WHERE id IN ({','.join('?' for _ in provider_ids)})
            ORDER BY
              CASE
                WHEN id = ? THEN 0
                WHEN id = ? THEN 1
                ELSE 2
              END,
              name
            """,
            (*provider_ids, NEWAPI_PROVIDER_ID, LOCAL_UPSCALE_PROVIDER_ID),
        ).fetchall()
    return [row_provider(row, include_secret=include_secret) for row in rows]


def get_provider(provider_id: str, include_secret: bool = False, include_internal: bool = True) -> Dict[str, Any]:
    provider_ids = ACTIVE_PROVIDER_IDS if include_internal else PUBLIC_PROVIDER_IDS
    if provider_id not in provider_ids:
        raise HTTPException(status_code=404, detail="Provider not found")
    with connect() as conn:
        row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Provider not found")
    return row_provider(row, include_secret=include_secret)


def truthy_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def prefers_provider_credentials(model_or_provider: Dict[str, Any]) -> bool:
    params = model_or_provider.get("params") if isinstance(model_or_provider.get("params"), dict) else {}
    meta = model_or_provider.get("meta") if isinstance(model_or_provider.get("meta"), dict) else {}
    if "preferProviderCredentials" in params:
        return truthy_config_value(params.get("preferProviderCredentials"))
    if "preferProviderCredentials" in meta:
        return truthy_config_value(meta.get("preferProviderCredentials"))
    return False


def resolve_runtime_provider(model_or_provider: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = (
        model_or_provider.get("providerId")
        or model_or_provider.get("provider_id")
        or model_or_provider.get("id")
        or ""
    )
    provider = get_provider(str(provider_id), include_secret=True)
    if provider["id"] == GHOSTCUT_PROVIDER_ID:
        account = get_newapi_account(include_secret=True)
        account_base_url = normalize_newapi_base_url((account or {}).get("baseUrl") or "")
        provider_base_url = normalize_newapi_base_url(provider.get("baseUrl") or "")
        default_key = (account or {}).get("defaultApiKey") or ""
        provider_key = provider.get("apiKey") or ""
        if is_usable_newapi_api_key(default_key):
            provider["apiKey"] = normalize_newapi_api_key(default_key)
        elif is_usable_newapi_api_key(provider_key):
            provider["apiKey"] = normalize_newapi_api_key(provider_key)
        else:
            provider["apiKey"] = ""
        provider["baseUrl"] = account_base_url or provider_base_url
        return provider
    if provider["id"] != NEWAPI_PROVIDER_ID:
        return provider
    if str(model_or_provider.get("adapter") or "").strip() == "grok2api.video" and prefers_provider_credentials(model_or_provider):
        provider_key = provider.get("apiKey") or ""
        provider["apiKey"] = normalize_newapi_api_key(provider_key) if is_usable_newapi_api_key(provider_key) else ""
        provider["baseUrl"] = normalize_newapi_base_url(provider.get("baseUrl") or "")
        return provider

    account = get_newapi_account(include_secret=True)
    account_base_url = normalize_newapi_base_url((account or {}).get("baseUrl") or "")
    provider_base_url = normalize_newapi_base_url(provider.get("baseUrl") or "")
    default_key = (account or {}).get("defaultApiKey") or ""
    if is_usable_newapi_api_key(default_key):
        provider["apiKey"] = normalize_newapi_api_key(default_key)
    elif (not account_base_url or provider_base_url == account_base_url) and is_usable_newapi_api_key(provider.get("apiKey") or ""):
        provider["apiKey"] = normalize_newapi_api_key(provider.get("apiKey") or "")
    else:
        provider["apiKey"] = ""
    if account_base_url:
        provider["baseUrl"] = account_base_url
    else:
        provider["baseUrl"] = provider_base_url
    return provider


def list_provider_models(
    provider_id: Optional[str] = None,
    capability: Optional[str] = None,
    enabled_only: bool = False,
    include_internal: bool = False,
    respect_account_availability: bool = True,
) -> List[Dict[str, Any]]:
    provider_ids = ACTIVE_PROVIDER_IDS if include_internal else PUBLIC_PROVIDER_IDS
    if provider_id and provider_id not in provider_ids:
        return []
    query = f"SELECT * FROM provider_models WHERE provider_id IN ({','.join('?' for _ in provider_ids)})"
    params: List[Any] = list(provider_ids)
    if provider_id:
        query += " AND provider_id = ?"
        params.append(provider_id)
    if capability:
        query += " AND capability = ?"
        params.append(capability)
    if enabled_only:
        query += " AND enabled = 1"
    query += """
      ORDER BY
        CASE WHEN provider_id = ? THEN 0 WHEN provider_id = ? THEN 1 ELSE 2 END,
        enabled DESC,
        provider_id,
        display_name
    """
    params.extend([NEWAPI_PROVIDER_ID, LOCAL_UPSCALE_PROVIDER_ID])
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    models = [normalize_grok_provider_model(row_provider_model(row)) for row in rows]
    if respect_account_availability:
        models = [
            model for model in models
            if provider_model_available_for_account(model)
            and not (model.get("meta") or {}).get("hiddenFromPicker")
        ]
        models = dedupe_newapi_catalog_remote_duplicates(models)
    return models


def get_provider_model(model_id: str, include_internal: bool = True) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    with connect() as conn:
        row = conn.execute("SELECT * FROM provider_models WHERE id = ?", (canonical_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Provider model not found")
    provider_ids = ACTIVE_PROVIDER_IDS if include_internal else PUBLIC_PROVIDER_IDS
    if row["provider_id"] not in provider_ids:
        raise HTTPException(status_code=404, detail="Provider model not found")
    return normalize_grok_provider_model(row_provider_model(row))


def sync_provider_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_file(DATA_DIR / "providers.json", {"providers": list_providers(), "models": list_provider_models()})


def normalize_job_type(raw_type: str, payload: Dict[str, Any]) -> str:
    value = (raw_type or "").strip()
    if value in {
        "image.analyze",
        "image.generate",
        "image.upscale",
        "storyboard.video.analyze",
        "storyboard.video.trim",
        "video.subtitle.remove",
        "video.generate",
        "text.generate",
        "text.reason",
        "inference.generate",
        "audio.generate",
    }:
        return value
    if value in {"analyze", "image.tag", "image.tags", "image.detect"}:
        return "image.analyze"
    if value in {"upscale", "image.upscale", "image.enhance", "image.superres"}:
        return "image.upscale"
    if value in {"subtitle.remove", "subtitles.remove", "video.remove-subtitle", "video.remove-subtitles"}:
        return "video.subtitle.remove"
    if value in {"reason", "reason.generate", "text.reasoning"}:
        return "text.reason"
    if value in {"inference", "infer", "infer.generate"}:
        return "inference.generate"
    if value in {"chat", "chat.generate"}:
        return "text.generate"
    if value in {"image", "video", "text", "audio"}:
        return f"{value}.generate"
    tab = (payload or {}).get("tab")
    if tab == "chat":
        return "text.generate"
    if tab in {"reason", "text.reason", "text.reasoning"}:
        return "text.reason"
    if tab in {"inference", "inference.generate", "infer"}:
        return "inference.generate"
    if tab in {"upscale", "image.upscale"}:
        return "image.upscale"
    if tab in {"image", "video", "text", "audio"}:
        return f"{tab}.generate"
    return value or "image.generate"


def _normalized_model_tokens(model: Dict[str, Any]) -> set[str]:
    tokens = {
        str(model.get("id") or "").lower(),
        str(model.get("modelName") or "").lower(),
        str(model.get("displayName") or "").lower(),
    }
    meta = model.get("meta") or {}
    for alias in meta.get("aliases") or []:
        tokens.add(str(alias).lower())
    return {item for item in tokens if item}


def _account_available_model_tokens() -> Optional[set[str]]:
    account = get_newapi_account()
    meta = (account or {}).get("meta") or {}
    available = meta.get("availableModels")
    if not isinstance(available, list):
        return None
    tokens: set[str] = set()
    for value in available:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        tokens.add(lowered)
        tokens.add(canonical_provider_model_id(text).lower())
        tokens.add(re.sub(r"[^A-Za-z0-9_.-]+", "-", lowered).strip(".-"))
    return {item for item in tokens if item}


def _newapi_model_match_tokens(model: Dict[str, Any]) -> set[str]:
    tokens = set(_normalized_model_tokens(model))
    for value in list(tokens):
        tokens.add(canonical_provider_model_id(value).lower())
        tokens.add(re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-"))
    return {item for item in tokens if item}


def is_newapi_remote_synced_model(model: Dict[str, Any]) -> bool:
    meta = model.get("meta") if isinstance(model.get("meta"), dict) else {}
    return meta.get("managedBy") == "newapi.remote-models"


def _provider_model_catalog_duplicate_keys(model: Dict[str, Any]) -> set[str]:
    capability = str(model.get("capability") or "").strip().lower() or "*"
    keys: set[str] = set()
    for value in (model.get("displayName"), model.get("modelName")):
        text = str(value or "").strip().lower()
        if not text:
            continue
        normalized = re.sub(r"\s+", " ", text)
        keys.add(f"{capability}:{normalized}")
    return keys


def dedupe_newapi_catalog_remote_duplicates(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remote_keys: set[str] = set()
    for model in models:
        if model.get("providerId") != NEWAPI_PROVIDER_ID or not is_newapi_remote_synced_model(model):
            continue
        remote_keys.update(_provider_model_catalog_duplicate_keys(model))
    if not remote_keys:
        return models

    filtered: List[Dict[str, Any]] = []
    for model in models:
        if (
            model.get("providerId") == NEWAPI_PROVIDER_ID
            and not is_newapi_remote_synced_model(model)
            and _provider_model_catalog_duplicate_keys(model) & remote_keys
        ):
            continue
        filtered.append(model)
    return filtered


def _grok_variant_match_tokens(variant_id: str) -> set[str]:
    lowered = str(variant_id or "").strip().lower()
    return {
        lowered,
        canonical_provider_model_id(lowered).lower(),
        re.sub(r"[^A-Za-z0-9_.-]+", "-", lowered).strip(".-"),
    } - {""}


def grok_video_variant_spec_for_model_code(model_code: Any) -> Optional[Dict[str, Any]]:
    tokens = _grok_variant_match_tokens(str(model_code or ""))
    for item in GROK_VIDEO_VARIANT_SPECS:
        if _grok_variant_match_tokens(item["model"]) & tokens:
            return dict(item)
    return None


def is_grok_video_unified_model(model_code: Any) -> bool:
    return GROK_VIDEO_MODEL_ID in _grok_variant_match_tokens(str(model_code or ""))


def is_grok15_video_model(model_code: Any) -> bool:
    tokens = _grok_variant_match_tokens(str(model_code or ""))
    return bool(tokens & {GROK15_VIDEO_MODEL_ID, GROK15_VIDEO_UPSTREAM_MODEL_ID})


def looks_like_grok_video_model(model_code: Any) -> bool:
    lowered = str(model_code or "").strip().lower()
    if not lowered:
        return False
    return (
        is_grok_video_unified_model(lowered)
        or is_grok15_video_model(lowered)
        or lowered.startswith("grok-imagine-1.0-video-")
        or lowered.startswith("grok-imagine-video-")
    )


def looks_like_firefly_video_model(model_code: Any) -> bool:
    lowered = str(model_code or "").strip().lower()
    if lowered.startswith(f"{NEWAPI_PROVIDER_ID}."):
        lowered = lowered.split(".", 1)[1]
    return lowered in FIREFLY_VIDEO_MODEL_IDS or lowered.startswith(("firefly-sora", "firefly-veo", "firefly-kling"))


def is_supported_grok_video_identifier(value: Any) -> bool:
    return (
        is_grok_video_unified_model(value)
        or is_grok15_video_model(value)
        or bool(grok_video_variant_spec_for_model_code(value))
    )


def is_unsupported_grok_video_model(model_id: Any, model_name: Any = "") -> bool:
    id_text = str(model_id or "").strip().lower()
    name_text = str(model_name or "").strip()
    if not looks_like_grok_video_model(id_text) and not looks_like_grok_video_model(name_text):
        return False
    if looks_like_grok_video_model(id_text):
        variant_spec = grok_video_variant_spec_for_model_code(id_text)
        if is_grok15_video_model(id_text):
            return not (_grok_variant_match_tokens(id_text) & {GROK15_VIDEO_MODEL_ID, GROK15_VIDEO_UPSTREAM_MODEL_ID})
        if is_grok_video_unified_model(id_text):
            return id_text != GROK_VIDEO_MODEL_ID
        if variant_spec:
            return id_text != str(variant_spec["model"]).lower()
        return True
    return not is_supported_grok_video_identifier(name_text)


def available_grok_variant_specs() -> List[Dict[str, Any]]:
    available_tokens = _account_available_model_tokens()
    if available_tokens is None:
        return [dict(item) for item in GROK_VIDEO_VARIANT_SPECS]
    if GROK_VIDEO_MODEL_ID in available_tokens:
        return [dict(item) for item in GROK_VIDEO_VARIANT_SPECS]
    return [
        dict(item)
        for item in GROK_VIDEO_VARIANT_SPECS
        if _grok_variant_match_tokens(item["model"]) & available_tokens
    ]


def grok_unified_params(base_params: Dict[str, Any]) -> Dict[str, Any]:
    available_tokens = _account_available_model_tokens()
    variants = available_grok_variant_specs()
    if not variants:
        variants = [dict(item) for item in GROK_VIDEO_VARIANT_SPECS]
    prefer_concrete_variant_model = bool(variants)
    durations = sorted({int(item["seconds"]) for item in variants})
    ratio_order = ["16:9", "9:16", "1:1"]
    ratios = [ratio for ratio in ratio_order if any(item.get("ratio") == ratio for item in variants)]
    sizes = []
    for item in variants:
        size = item.get("size")
        if size and size not in sizes:
            sizes.append(size)
    default_variant = variants[0]
    default_seconds = int(default_variant.get("seconds") or durations[0] or 10)
    params = {
        **base_params,
        "defaultDuration": default_seconds,
        "defaultSeconds": default_seconds,
        "supportedDurations": durations,
        "defaultSize": default_variant.get("size") or base_params.get("defaultSize") or "1280x720",
        "sizes": sizes or base_params.get("sizes") or ["1280x720"],
        "ratios": ratios or base_params.get("ratios") or ["16:9"],
        "defaultResolutionName": base_params.get("defaultResolutionName") or "720p",
        "defaultPreset": base_params.get("defaultPreset") or "normal",
        "maxReferenceImages": 7,
        "upstreamModelName": default_variant.get("model") if prefer_concrete_variant_model else GROK_VIDEO_MODEL_ID,
        "preferConcreteVariantModel": prefer_concrete_variant_model,
        "preferProviderCredentials": False,
        "grokVariants": [item["model"] for item in variants],
        "availableVariantModels": [item["model"] for item in variants],
        "grokVariantSpecs": variants,
    }
    return params


def grok_unified_pricing_meta(params: Dict[str, Any], base_meta: Dict[str, Any]) -> Dict[str, Any]:
    candidates: List[str] = []
    upstream = str(params.get("upstreamModelName") or "").strip()
    if upstream and canonical_provider_model_id(upstream) != GROK_VIDEO_MODEL_ID:
        candidates.append(upstream)
    for value in params.get("availableVariantModels") or params.get("grokVariants") or []:
        text = str(value or "").strip()
        if text and canonical_provider_model_id(text) != GROK_VIDEO_MODEL_ID and text not in candidates:
            candidates.append(text)
    if not candidates:
        return base_meta

    generic_label = "按当前 NewAPI 后台价格表计费"
    with connect() as conn:
        for candidate in candidates:
            row = conn.execute(
                "SELECT meta_json FROM provider_models WHERE id = ?",
                (canonical_provider_model_id(candidate),),
            ).fetchone()
            if not row:
                continue
            variant_meta = json_load(row["meta_json"], {})
            pricing = variant_meta.get("pricing")
            label = str(variant_meta.get("priceLabel") or "").strip()
            if not label and isinstance(pricing, dict):
                label = str(pricing.get("label") or "").strip()
            if not label or label == generic_label:
                continue
            next_meta = dict(base_meta)
            if isinstance(pricing, dict) and pricing:
                next_meta["pricing"] = pricing
            next_meta["priceLabel"] = label
            return next_meta
    return base_meta


def normalize_grok_provider_model(model: Dict[str, Any]) -> Dict[str, Any]:
    model_id = canonical_provider_model_id(model.get("id"))
    params = model.get("params") or {}
    meta = model.get("meta") or {}
    if model_id == GROK_VIDEO_MODEL_ID:
        params = grok_unified_params(params)
        meta = grok_unified_pricing_meta(params, meta)
    params = normalize_hermes_seedance_params(model_id, params)
    params = normalize_muse_video_params(model_id, params)
    params = normalize_firefly_video_params(model_id, params)
    params = normalize_public_video_params(model_id, params)
    params = apply_guanzhuan_seedance_dash_params(model_id, model.get("modelName") or model.get("displayName") or "", params)
    params = apply_channel24_seedance_params(model_id, model.get("modelName") or model.get("displayName") or "", params)
    meta = apply_channel27_911_seedance_pricing_meta(model_id, meta)
    meta = apply_channel31_face_pricing_meta(model_id, meta)
    meta = apply_artifex_seedence20_pricing_meta(model_id, meta)
    meta = apply_shishikeji_channel4_pricing_meta(model_id, meta)
    meta = apply_hongniao_channel36_pricing_meta(model_id, meta)
    meta = apply_channel24_seedance_pricing_meta(model_id, model.get("modelName") or model.get("displayName") or "", meta)
    if params is model.get("params") and meta is model.get("meta"):
        return model
    return {**model, "params": params, "meta": meta}


def provider_model_available_for_account(model: Dict[str, Any]) -> bool:
    if model.get("providerId") != NEWAPI_PROVIDER_ID:
        return True
    if any(
        is_removed_provider_model_id(value)
        for value in (model.get("id"), model.get("modelName"), model.get("displayName"))
    ):
        return False
    if canonical_provider_model_id(model.get("id")) in ALWAYS_AVAILABLE_NEWAPI_MODEL_IDS:
        return True
    if canonical_provider_model_id(model.get("id")) == GROK_VIDEO_MODEL_ID:
        available_tokens = _account_available_model_tokens()
        if available_tokens is None:
            return True
        return bool(available_grok_variant_specs())
    available_tokens = _account_available_model_tokens()
    if available_tokens is None:
        return not is_newapi_remote_synced_model(model)
    return bool(_newapi_model_match_tokens(model) & available_tokens)


def account_available_replacement_model(
    unavailable_model: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    capability: str,
) -> Optional[Dict[str, Any]]:
    if canonical_provider_model_id(unavailable_model.get("id")) == SEEDANCE_FAST_MODEL_ID:
        for preferred_id in (SEEDANCE_DASH_FAST_MODEL_ID, SEEDANCE_DASH_MODEL_ID):
            replacement = next(
                (
                    model
                    for model in candidates
                    if canonical_provider_model_id(model.get("id")) == preferred_id
                    and model.get("enabled")
                    and model_supports_capability(model, capability)
                ),
                None,
            )
            if replacement:
                return replacement
    if str(unavailable_model.get("adapter") or "").strip() != "grok2api.video":
        return None
    grok_candidates = [
        model for model in candidates
        if str(model.get("adapter") or "").strip() == "grok2api.video"
        and model_supports_capability(model, capability)
    ]
    return grok_candidates[0] if len(grok_candidates) == 1 else None


def is_prefixed_newapi_model_id(model_id: str) -> bool:
    return str(model_id or "").lower().startswith(f"{NEWAPI_PROVIDER_ID}.")


def compatible_model_capabilities(capability: str) -> set[str]:
    normalized = normalize_job_type(capability, {})
    if normalized in TEXT_MODEL_CAPABILITIES:
        return {normalized, "text.generate"}
    return {normalized}


def model_supports_capability(model: Dict[str, Any], capability: str) -> bool:
    return str(model.get("capability") or "") in compatible_model_capabilities(capability)


def default_adapter_for_capability(capability: str) -> str:
    normalized = normalize_job_type(capability, {})
    return CAPABILITY_DEFAULT_ADAPTERS.get(normalized, "")


def allowed_adapters_for_capability(capability: str) -> set[str]:
    normalized = normalize_job_type(capability, {})
    return CAPABILITY_ALLOWED_ADAPTERS.get(normalized, set())


def _remote_model_schema_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    return parse_newapi_param_schema(
        record.get("paramSchemaJson") or record.get("param_schema_json") or record.get("params")
    )


def explicit_software_capability(record: Dict[str, Any]) -> str:
    schema = _remote_model_schema_dict(record)
    for value in (
        record.get("softwareCapability"),
        record.get("software_capability"),
        schema.get("softwareCapability"),
        schema.get("software_capability"),
    ):
        text = str(value or "").strip()
        if text and text in PUBLIC_MODEL_CAPABILITIES:
            return text
    return ""


def explicit_software_adapter(record: Dict[str, Any], capability: str) -> str:
    schema = _remote_model_schema_dict(record)
    allowed = allowed_adapters_for_capability(capability)
    for value in (
        record.get("softwareAdapter"),
        record.get("software_adapter"),
        schema.get("softwareAdapter"),
        schema.get("software_adapter"),
    ):
        text = str(value or "").strip()
        if text and text in allowed:
            return text
    return ""


def infer_adapter_for_remote_model(record: Dict[str, Any], capability: str) -> str:
    explicit = explicit_software_adapter(record, capability)
    if explicit:
        return explicit
    code = newapi_remote_model_code(record).lower()
    display_name = str(record.get("displayName") or record.get("display_name") or record.get("name") or "").lower()
    fields = f"{code} {display_name} {record.get('provider') or ''} {record.get('source') or ''}".lower()
    if capability == "video.generate" and (
        looks_like_firefly_video_model(code)
        or "adobe firefly video" in fields
        or "yunzhi firefly video" in fields
        or "firefly 视频" in fields
    ):
        return FIREFLY_VIDEO_ADAPTER
    if capability == "video.generate" and (
        "grok-imagine-video" in fields
        or "grok-imagine-1.0-video-" in fields
        or "grok2api" in fields
    ):
        return "grok2api.video"
    return default_adapter_for_capability(capability)


def newapi_remote_model_code(record: Dict[str, Any]) -> str:
    for key in ("modelCode", "model_code", "id", "model", "name"):
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""


def request_model_code_for_provider_model(model_code: str) -> str:
    canonical = canonical_provider_model_id(model_code)
    if canonical in HERMES_SEEDANCE_MODEL_IDS:
        return HERMES_SEEDANCE_REQUEST_MODEL_NAME
    muse_spec = MUSE_VIDEO_MODEL_SPECS.get(canonical)
    if muse_spec:
        return muse_spec["modelName"]
    if canonical in SHISHIKEJI_CHANNEL4_MODEL_SPECS:
        return canonical
    if canonical == "veo31-fast":
        return "veo31-fast"
    return str(model_code or "").strip()


def newapi_remote_model_id(model_code: str) -> str:
    raw_model_code = str(model_code or "").strip()
    canonical_raw_model_code = canonical_provider_model_id(raw_model_code)
    if canonical_raw_model_code in CHANNEL35_NO_AUDIO_SPECIAL_MODEL_IDS:
        return canonical_raw_model_code
    hongniao_channel36_model_id = (
        HONGNIAO_CHANNEL36_MODEL_ID_ALIASES.get(raw_model_code)
        or HONGNIAO_CHANNEL36_MODEL_ID_ALIASES.get(raw_model_code.replace("（", "(").replace("）", ")"))
    )
    if hongniao_channel36_model_id:
        return hongniao_channel36_model_id
    if (
        raw_model_code in XINGHE_SORA_VIDEO_MODEL_IDS
        or raw_model_code in LOW_PRICE_JIMENG_VIDEO_MODEL_IDS
        or raw_model_code in ZEXITONGXUE_SORA_VIP3_MODEL_IDS
        or raw_model_code in SHISHIKEJI_CHANNEL4_MODEL_IDS
        or raw_model_code in CHANNEL31_FACE_MODEL_IDS
    ):
        return raw_model_code
    muse_model_id = MUSE_VIDEO_MODEL_NAME_TO_ID.get(str(model_code or "").strip().lower())
    if muse_model_id:
        return muse_model_id
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_model_code).strip(".-").lower()
    return canonical_provider_model_id(cleaned or model_code)


def parse_newapi_param_schema(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def newapi_first_value(source: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source.get(key) is not None and source.get(key) != "":
            return source.get(key)
    return None


def newapi_float_value(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def newapi_int_value(value: Any) -> Optional[int]:
    number = newapi_float_value(value)
    if number is None:
        return None
    return int(number)


def format_newapi_number(value: Any) -> str:
    number = newapi_float_value(value)
    if number is None:
        return str(value or "").strip()
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def normalize_display_currency_label(value: Any) -> str:
    return str(value or "").replace("$", "￥").strip()


def is_newapi_second_based_video_record(record: Dict[str, Any]) -> bool:
    candidates: List[str] = []
    for source in (record, record.get("pricing") if isinstance(record.get("pricing"), dict) else {}):
        for key in ("model_name", "modelName", "modelCode", "model_code", "id", "model", "name"):
            value = source.get(key) if isinstance(source, dict) else None
            if value:
                candidates.append(str(value).strip())
    for value in candidates:
        lowered = value.lower()
        canonical = canonical_provider_model_id(value)
        if canonical in SECOND_BASED_VIDEO_MODEL_IDS:
            return True
        if (
            re.search(r"seedence2[.]0.*m-c", lowered)
            or re.search(r"seedence2-(?:fast|pro)（特价版[12]）", lowered)
            or re.search(r"\bseedance-2-0(?:-fast|-pro)?\b", lowered)
            or re.search(r"\bsora-v3-(?:fast|pro)\b", lowered)
        ):
            return True
    return False


def newapi_pricing_model_code(record: Dict[str, Any]) -> str:
    value = newapi_first_value(
        record,
        "model_name",
        "modelName",
        "modelCode",
        "model_code",
        "id",
        "model",
        "name",
    )
    return str(value or "").strip()


def merge_newapi_pricing_records(
    records: List[Dict[str, Any]],
    pricing_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not pricing_records:
        return records
    pricing_by_code: Dict[str, Dict[str, Any]] = {}
    for pricing_record in pricing_records:
        if not isinstance(pricing_record, dict):
            continue
        code = newapi_pricing_model_code(pricing_record)
        if not code:
            continue
        pricing_by_code[code] = pricing_record
        pricing_by_code[newapi_remote_model_id(code)] = pricing_record
        pricing_by_code[canonical_provider_model_id(code)] = pricing_record

    merged_records: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            merged_records.append(record)
            continue
        code = newapi_remote_model_code(record)
        pricing_record = (
            pricing_by_code.get(code)
            or pricing_by_code.get(newapi_remote_model_id(code))
            or pricing_by_code.get(canonical_provider_model_id(code))
        )
        if not pricing_record:
            merged_records.append(record)
            continue
        merged = dict(record)
        existing_pricing = merged.get("pricing")
        if isinstance(existing_pricing, dict):
            pricing = {**existing_pricing, **pricing_record}
        else:
            pricing = dict(pricing_record)
            if isinstance(existing_pricing, str) and existing_pricing.strip():
                pricing["label"] = existing_pricing.strip()
        merged["pricing"] = pricing
        merged_records.append(merged)
    seen_codes = {
        code
        for record in merged_records
        if isinstance(record, dict)
        for code in (
            newapi_remote_model_code(record),
            newapi_remote_model_id(newapi_remote_model_code(record)),
            canonical_provider_model_id(newapi_remote_model_id(newapi_remote_model_code(record))),
        )
        if code
    }
    for pricing_record in sorted(pricing_records, key=lambda item: newapi_pricing_model_code(item)):
        if not isinstance(pricing_record, dict):
            continue
        code = newapi_pricing_model_code(pricing_record)
        model_id = newapi_remote_model_id(code)
        canonical_id = canonical_provider_model_id(model_id)
        if canonical_id not in CHANNEL27_911_SEEDANCE_MODEL_IDS:
            continue
        if code in seen_codes or model_id in seen_codes or canonical_id in seen_codes:
            continue
        merged_records.append({
            "id": code,
            "modelCode": code,
            "displayName": code,
            "capabilityType": "视频生成",
            "supported_endpoint_types": pricing_record.get("supported_endpoint_types") or ["openai-video"],
            "pricing": dict(pricing_record),
        })
        seen_codes.update({code, model_id, canonical_id})
    return merged_records


def newapi_model_code_merge_key(model_code: Any) -> str:
    text = str(model_code or "").strip()
    if not text:
        return ""
    grok_spec = grok_video_variant_spec_for_model_code(text)
    if grok_spec:
        return str(grok_spec["model"]).lower()
    if is_grok15_video_model(text):
        return GROK15_VIDEO_MODEL_ID
    if is_grok_video_unified_model(text):
        return GROK_VIDEO_MODEL_ID
    return canonical_provider_model_id(newapi_remote_model_id(text) or text).lower()


def clean_newapi_model_codes(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    models: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = newapi_model_code_merge_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        models.append(text)
    return models


def read_newapi_available_models(client: Any) -> List[str]:
    reader = getattr(client, "list_available_models", None)
    if not callable(reader):
        return []
    try:
        return clean_newapi_model_codes(reader())
    except (HTTPException, NewApiError):
        return []


def merge_newapi_model_codes(*model_groups: List[str]) -> List[str]:
    models: List[str] = []
    seen: set[str] = set()
    for group in model_groups:
        for model_code in clean_newapi_model_codes(group):
            key = newapi_model_code_merge_key(model_code)
            if not key or key in seen:
                continue
            seen.add(key)
            models.append(model_code)
    return models


def merge_newapi_available_model_records(records: List[Dict[str, Any]], available_models: List[str]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = list(records or [])
    seen = {
        newapi_model_code_merge_key(newapi_remote_model_code(record))
        for record in merged
        if isinstance(record, dict)
    }
    for model_code in clean_newapi_model_codes(available_models):
        key = newapi_model_code_merge_key(model_code)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append({"id": model_code, "displayName": model_code})
    return merged


def newapi_model_codes_from_sources(records: List[Dict[str, Any]], available_models: List[str]) -> List[str]:
    explicit_available_models = merge_newapi_model_codes(available_models or [])
    if explicit_available_models:
        return explicit_available_models
    record_codes = [
        newapi_remote_model_code(record)
        for record in records or []
        if isinstance(record, dict) and newapi_remote_model_code(record)
    ]
    return merge_newapi_model_codes(record_codes)


def merge_newapi_remote_model_records(*record_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for records in record_groups:
        for record in records or []:
            if not isinstance(record, dict):
                continue
            code = newapi_remote_model_code(record)
            if not code:
                continue
            key = newapi_model_code_merge_key(code)
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
    return merged


def read_newapi_pricing_records(client: Any) -> List[Dict[str, Any]]:
    reader = getattr(client, "list_pricing_records", None)
    if not callable(reader):
        return []
    try:
        records = reader()
    except NewApiError:
        return []
    return records if isinstance(records, list) else []


def infer_newapi_remote_capability(record: Dict[str, Any]) -> str:
    explicit = explicit_software_capability(record)
    if explicit:
        return explicit
    code = newapi_remote_model_code(record).lower()
    if canonical_provider_model_id(code) in HERMES_SEEDANCE_MODEL_IDS:
        return "video.generate"
    if canonical_provider_model_id(code) in PUBLIC_VIDEO_PROTOCOL_PARAMS:
        return "video.generate"
    param_schema = parse_newapi_param_schema(record.get("paramSchemaJson") or record.get("param_schema_json") or record.get("params"))
    fields = " ".join(
        str(record.get(key) or "").lower()
        for key in (
            "capabilityType",
            "capability_type",
            "modelType",
            "model_type",
            "taskType",
            "task_type",
            "type",
            "category",
            "displayName",
            "remark",
        )
    )
    schema_fields = " ".join(
        str(param_schema.get(key) or "").lower()
        for key in (
            "taskType",
            "task_type",
            "capabilityType",
            "capability_type",
            "type",
            "category",
            "description",
        )
    )
    text = f"{code} {fields} {schema_fields}"
    video_param_keys = {
        "supportedDurations",
        "durations",
        "defaultDuration",
        "defaultGenerateAudio",
        "supportedReferenceModes",
        "referenceModes",
        "maxReferenceVideos",
    }
    image_param_keys = {
        "maxReferenceImages",
        "maxOutputCount",
        "supportedQualities",
        "variants",
        "allowCustomSize",
        "resolutionPrices",
    }
    if any(key in param_schema for key in video_param_keys):
        return "video.generate"
    if (
        "video_generation" in text
        or any(token in text for token in ("video", "视频", "firefly-sora", "firefly-veo", "firefly-kling", "firefly video", "firefly 视频", "veo", "sora", "kling", "可灵", "seedance", "seedence", "muse_seedance", "muse_see_dance", "hailuo", "海螺", "runway", "pika", "vidu", "luma", "pixverse", "minimax"))
    ):
        return "video.generate"
    if any(key in param_schema for key in image_param_keys):
        return "image.generate"
    if (
        "image_generation" in text
        or "image_edit" in text
        or any(token in text for token in ("image", "图片", "图像", "生图", "绘图", "nanobanana", "nano-banana", "banana", "flux", "midjourney", "dall", "ideogram", "stable-diffusion", "imagen", "recraft", "seedream", "jimeng", "即梦"))
    ):
        return "image.generate"
    if any(token in text for token in ("embedding", "rerank", "moderation", "audio", "tts", "asr", "whisper")):
        return ""
    return "text.generate"


def newapi_remote_model_params(record: Dict[str, Any]) -> Dict[str, Any]:
    params = parse_newapi_param_schema(record.get("paramSchemaJson") or record.get("param_schema_json") or record.get("params"))
    for key in ("maxReferenceImages", "maxOutputCount", "defaultDuration", "defaultReferenceMode", "defaultGenerateAudio"):
        if record.get(key) is not None and key not in params:
            params[key] = record.get(key)
    model_code = newapi_remote_model_code(record)
    model_id = newapi_remote_model_id(model_code)
    params = normalize_nanobanana_params(model_id, params)
    params = normalize_seedance_fast_params(model_id, params)
    params = normalize_hermes_seedance_params(model_id, params)
    params = normalize_muse_video_params(model_id, params)
    params = normalize_veo31_fast_params(model_id, params)
    params = normalize_firefly_video_params(model_id, params)
    params = normalize_deepseek_text_params(model_id, params)
    return normalize_public_video_params(model_id, params)


def normalize_deepseek_text_params(model_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id).lower()
    if not canonical_id.startswith("deepseek"):
        return params
    normalized = dict(params)
    normalized.setdefault("apiEndpoint", "chat.completions")
    normalized.setdefault("taskType", "text_generation")
    return normalized


def seeded_provider_model(model_id: str) -> Optional[Dict[str, Any]]:
    canonical_id = canonical_provider_model_id(model_id)
    return next(
        (
            item
            for item in PROVIDER_MODEL_SEEDS
            if canonical_provider_model_id(item.get("id")) == canonical_id
        ),
        None,
    )


def preserve_builtin_remote_model_defaults(model_id: str, params: Dict[str, Any], meta: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    canonical_id = canonical_provider_model_id(model_id)
    if (
        canonical_id not in DEFAULT_PUBLIC_PROVIDER_MODEL_IDS
        and canonical_id not in MUSE_VIDEO_MODEL_IDS
        and canonical_id not in LOW_PRICE_JIMENG_VIDEO_MODEL_IDS
        and canonical_id not in SHISHIKEJI_CHANNEL4_MODEL_IDS
        and canonical_id not in CHANNEL31_FACE_MODEL_IDS
        and canonical_id not in CHANNEL35_NO_AUDIO_SPECIAL_MODEL_IDS
        and canonical_id not in HERMES_SEEDANCE_MODEL_IDS
    ):
        return params, meta
    seed = seeded_provider_model(canonical_id)
    if not seed:
        return params, meta
    return {**(seed.get("params") or {}), **params}, {**(seed.get("meta") or {}), **meta}


def newapi_remote_pricing_meta(record: Dict[str, Any]) -> Dict[str, Any]:
    nested_pricing = record.get("pricing")
    sources: List[Dict[str, Any]] = []
    explicit_label = ""
    if isinstance(nested_pricing, dict):
        sources.append(nested_pricing)
    elif isinstance(nested_pricing, str) and nested_pricing.strip():
        explicit_label = nested_pricing.strip()
    sources.append(record)

    pricing: Dict[str, Any] = {}
    raw: Dict[str, Any] = {}
    field_aliases = {
        "quotaType": ("quota_type", "quotaType", "quota", "billing_type", "billingType"),
        "modelRatio": ("model_ratio", "modelRatio", "ratio", "input_ratio", "inputRatio", "prompt_ratio", "promptRatio"),
        "completionRatio": ("completion_ratio", "completionRatio", "output_ratio", "outputRatio"),
        "cacheRatio": ("cache_ratio", "cacheRatio"),
        "createCacheRatio": ("create_cache_ratio", "createCacheRatio"),
        "imageRatio": ("image_ratio", "imageRatio"),
        "audioRatio": ("audio_ratio", "audioRatio"),
        "audioCompletionRatio": ("audio_completion_ratio", "audioCompletionRatio"),
        "modelPrice": ("model_price", "modelPrice", "price", "unit_price", "unitPrice"),
        "billingMode": ("billing_mode", "billingMode"),
        "billingExpr": ("billing_expr", "billingExpr"),
    }

    for source in sources:
        label = newapi_first_value(source, "priceLabel", "price_label", "label", "priceText", "price_text", "displayPrice", "display_price")
        if isinstance(label, str) and label.strip():
            explicit_label = label.strip()
        for target_key, aliases in field_aliases.items():
            value = newapi_first_value(source, *aliases)
            if value is None:
                continue
            raw[target_key] = value
            if target_key == "quotaType":
                int_value = newapi_int_value(value)
                if int_value is not None:
                    pricing[target_key] = int_value
            elif target_key in {"billingMode", "billingExpr"}:
                pricing[target_key] = str(value).strip()
            else:
                number = newapi_float_value(value)
                if number is not None:
                    pricing[target_key] = number

    resolution_prices = record.get("resolutionPrices") or record.get("resolution_prices")
    if isinstance(resolution_prices, dict):
        pricing["resolutionPrices"] = resolution_prices
        raw["resolutionPrices"] = resolution_prices
    if record.get("unitPrice") is not None and "modelPrice" not in pricing:
        number = newapi_float_value(record.get("unitPrice"))
        if number is not None:
            pricing["unitPrice"] = number
            raw["unitPrice"] = record.get("unitPrice")

    if not pricing and not explicit_label:
        return {}

    second_based_ratio_pricing = (
        pricing.get("quotaType") == 0
        and pricing.get("modelRatio") is not None
        and is_newapi_second_based_video_record(record)
    )
    if second_based_ratio_pricing:
        price_per_second = float(pricing["modelRatio"]) / 2
        pricing["billingUnit"] = "second"
        pricing["pricePerSecond"] = price_per_second
        pricing["priceFor15Seconds"] = price_per_second * 15

    label = "" if second_based_ratio_pricing else explicit_label
    if not label and pricing.get("billingMode") == "tiered_expr" and pricing.get("billingExpr"):
        label = "动态计费"
    if not label and pricing.get("modelPrice") is not None and (
        pricing.get("quotaType") == 1 or pricing.get("modelRatio") in (None, 0)
    ):
        label = f"模型价格 ￥{format_newapi_number(pricing.get('modelPrice'))} / 次"
    if not label and pricing.get("billingUnit") == "second":
        label = (
            f"每秒价格 ￥{format_newapi_number(pricing.get('pricePerSecond'))} / 秒"
            f" · 15秒价格 ￥{format_newapi_number(pricing.get('priceFor15Seconds'))}"
        )
    if not label and pricing.get("modelRatio") is not None:
        input_price = float(pricing["modelRatio"]) * 2
        parts = [f"输入 ￥{format_newapi_number(input_price)} / 1M Tokens"]
        if pricing.get("completionRatio") is not None:
            parts.append(f"输出 ￥{format_newapi_number(input_price * float(pricing['completionRatio']))} / 1M Tokens")
        else:
            parts.append(f"模型倍率 {format_newapi_number(pricing['modelRatio'])}")
        label = " · ".join(parts)
    if not label and pricing.get("unitPrice") is not None:
        label = f"单价 {format_newapi_number(pricing.get('unitPrice'))}"

    pricing["source"] = "newapi.pricing" if (
        isinstance(nested_pricing, dict)
        or any(key in raw for key in ("quotaType", "modelRatio", "completionRatio", "modelPrice", "billingMode", "billingExpr"))
    ) else "newapi.remote-models"
    if label:
        pricing["label"] = normalize_display_currency_label(label)
    if raw:
        pricing["raw"] = raw
    return pricing


def newapi_remote_model_meta(record: Dict[str, Any]) -> Dict[str, Any]:
    pricing = newapi_remote_pricing_meta(record)
    meta = {
        "managedBy": "newapi.remote-models",
        "remoteModel": True,
        "rawCapabilityType": record.get("capabilityType") or record.get("capability_type") or record.get("type") or "",
    }
    if pricing:
        meta["pricing"] = pricing
        if isinstance(pricing.get("label"), str) and pricing.get("label").strip():
            meta["priceLabel"] = normalize_display_currency_label(pricing["label"])
    remark = str(record.get("remark") or record.get("description") or "").strip()
    if remark:
        meta["description"] = remark
    return meta


def merge_existing_remote_pricing_meta(meta: Dict[str, Any], existing_meta: Dict[str, Any]) -> Dict[str, Any]:
    if meta.get("pricing"):
        return meta
    next_meta = dict(meta)
    existing_pricing = existing_meta.get("pricing")
    if isinstance(existing_pricing, dict) and existing_pricing:
        next_pricing = dict(existing_pricing)
        if isinstance(next_pricing.get("label"), str) and next_pricing.get("label").strip():
            next_pricing["label"] = normalize_display_currency_label(next_pricing["label"])
        next_meta["pricing"] = next_pricing
    existing_label = existing_meta.get("priceLabel")
    if isinstance(existing_label, str) and existing_label.strip():
        next_meta["priceLabel"] = normalize_display_currency_label(existing_label)
    return next_meta


def apply_fixed_duration_pricing_meta(
    model_id: str,
    meta: Dict[str, Any],
    price_for_15: float,
    source: str,
) -> Dict[str, Any]:
    price_per_second = price_for_15 / 15.0
    label = (
        f"每秒价格 ￥{format_newapi_number(price_per_second)} / 秒"
        f" · 15秒价格 ￥{format_newapi_number(price_for_15)}"
    )
    pricing = {
        "quotaType": 1,
        "modelPrice": price_for_15,
        "billingUnit": "second",
        "pricePerSecond": price_per_second,
        "priceFor15Seconds": price_for_15,
        "source": source,
        "label": label,
    }
    return {**meta, "pricing": pricing, "priceLabel": label}


def apply_xinghe_sora_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    price_for_15 = XINGHE_SORA_VIDEO_PRICE_FOR_15_SECONDS.get(canonical_id)
    if price_for_15 is None:
        return meta
    return apply_fixed_duration_pricing_meta(model_id, meta, price_for_15, "xinghe-sora")


def apply_low_price_jimeng_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    price_for_15 = LOW_PRICE_JIMENG_VIDEO_PRICE_FOR_15_SECONDS.get(canonical_id)
    if price_for_15 is None:
        return meta
    return apply_fixed_duration_pricing_meta(model_id, meta, price_for_15, "low-price-jimeng")


def apply_channel27_911_seedance_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if canonical_provider_model_id(model_id) not in CHANNEL27_911_SEEDANCE_MODEL_IDS:
        return meta
    price_for_15 = CHANNEL27_911_SEEDANCE_PRICE_FOR_15_SECONDS
    admin_price_for_15 = CHANNEL27_911_SEEDANCE_ADMIN_PRICE_FOR_15_SECONDS
    label = f"用户 ￥{format_newapi_number(price_for_15)} / 15秒"
    pricing = {
        "quotaType": 1,
        "modelPrice": price_for_15,
        "adminPrice": admin_price_for_15,
        "billingUnit": "request",
        "billingMode": "fixed_15s",
        "durationSeconds": 15,
        "priceFor15Seconds": price_for_15,
        "adminPriceFor15Seconds": admin_price_for_15,
        "source": "channel27-911-seedance",
        "label": label,
    }
    return {
        **meta,
        "pricing": pricing,
        "priceLabel": label,
        "modelIntroduction": CHANNEL27_911_SEEDANCE_MODEL_INTRODUCTION,
    }


def apply_channel31_face_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    tiers = CHANNEL31_FACE_RESOLUTION_PRICES.get(canonical_id)
    if not tiers:
        return meta
    params = PUBLIC_VIDEO_PROTOCOL_PARAMS.get(canonical_id) or {}
    resolutions = [
        str(resolution).strip().lower()
        for resolution in (params.get("supportedResolutions") or tiers.keys())
        if str(resolution).strip().lower() in tiers
    ]
    parts = ["按实际秒数折算"]
    resolution_prices: Dict[str, Dict[str, float]] = {}
    for resolution in resolutions:
        tier = tiers[resolution]
        price_for_15 = float(tier["modelPrice"])
        admin_price_for_15 = float(tier["adminPrice"])
        price_per_second = price_for_15 / 15.0
        resolution_prices[resolution] = {
            "modelPrice": price_for_15,
            "adminPrice": admin_price_for_15,
            "pricePerSecond": price_per_second,
            "adminPricePerSecond": admin_price_for_15 / 15.0,
        }
        parts.append(
            f"{resolution} ￥{format_newapi_number(price_for_15)} / 15秒"
            f"（￥{format_newapi_number(price_per_second)} / 秒）"
        )
    label = " · ".join(parts)
    pricing = {
        "quotaType": 1,
        "billingUnit": "second",
        "source": "channel31-face-resolution",
        "label": label,
        "resolutionPrices": resolution_prices,
    }
    return {**meta, "pricing": pricing, "priceLabel": label}


def apply_channel35_no_audio_special_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    spec = CHANNEL35_NO_AUDIO_SPECIAL_MODEL_SPECS.get(canonical_id)
    if not spec:
        return meta
    model_price = newapi_float_value(spec.get("modelPrice"))
    admin_price = newapi_float_value(spec.get("adminPrice"))
    if model_price is None:
        return meta
    label = f"模型价格 ￥{format_newapi_number(model_price)} / 次"
    pricing = {
        "quotaType": 1,
        "modelPrice": model_price,
        "billingUnit": "request",
        "source": "channel35-no-audio-special",
        "label": label,
    }
    if admin_price is not None:
        pricing["adminPrice"] = admin_price
    return {
        **meta,
        "pricing": pricing,
        "priceLabel": label,
        "modelIntroduction": "不带音频，支持 4-15 秒、720P、最多 4 张参考图或 3 个参考视频",
    }


def channel24_seedance_kind(*values: Any) -> str:
    canonical_values = {
        canonical_provider_model_id(value)
        for value in values
        if value and canonical_provider_model_id(value) in CHANNEL24_SEEDANCE_MODEL_IDS
    }
    if (
        SEEDANCE_DASH_FAST_MODEL_ID in canonical_values
        or "seedence2.0-fast" in canonical_values
        or "seedence20-fast-gumen" in canonical_values
        or "seedence2.0（火山fast备用版本）" in canonical_values
        or "seedence2.0(火山fast备用版本)" in canonical_values
    ):
        return "fast"
    if (
        SEEDANCE_DASH_PRO_MODEL_ID in canonical_values
        or "seedence2.0-pro" in canonical_values
        or "seedence20-pro-gumen" in canonical_values
        or "seedence2.0（火山备用版本）" in canonical_values
        or "seedence2.0(火山备用版本)" in canonical_values
    ):
        return "pro"
    return ""


def channel24_seedance_is_backup_volcano(*values: Any) -> bool:
    canonical_values = [
        canonical_provider_model_id(value)
        for value in values
        if value
    ]
    return any("火山" in value and "备用版本" in value for value in canonical_values)


def channel24_seedance_resolutions(kind: str, *values: Any) -> tuple[str, ...]:
    if channel24_seedance_is_backup_volcano(*values):
        return ("720p",)
    return (
        CHANNEL24_SEEDANCE_FAST_RESOLUTIONS
        if kind == "fast"
        else CHANNEL24_SEEDANCE_PRO_RESOLUTIONS
    )


GUANZHUAN_SEEDANCE_DASH_PRO_ALIASES = {
    "seedance-2-0(火山官转)",
    "seedance-2-0(火山版本)",
    "seedance-2-0-pro(火山官转)",
    "seedance-2-0-pro(火山版本)",
}
GUANZHUAN_SEEDANCE_DASH_FAST_ALIASES = {
    "seedance-2-0-fast(火山官转)",
}


def guanzhuan_seedance_dash_pro_alias(*values: Any) -> bool:
    normalized_values = {
        seedance_portrait_asset_model_text(value)
        for value in values
        if str(value or "").strip()
    }
    return bool(normalized_values & GUANZHUAN_SEEDANCE_DASH_PRO_ALIASES) or any(
        "seedance-2-0" in value and "火山官转" in value and "fast" not in value
        for value in normalized_values
    )


def guanzhuan_seedance_dash_fast_alias(*values: Any) -> bool:
    normalized_values = {
        seedance_portrait_asset_model_text(value)
        for value in values
        if str(value or "").strip()
    }
    return bool(normalized_values & GUANZHUAN_SEEDANCE_DASH_FAST_ALIASES) or any(
        "seedance-2-0" in value and "火山官转" in value and "fast" in value
        for value in normalized_values
    )


def apply_guanzhuan_seedance_dash_params(model_id: str, model_code: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if guanzhuan_seedance_dash_fast_alias(model_id, model_code):
        normalized = {
            **params,
            **PUBLIC_VIDEO_PROTOCOL_PARAMS[SEEDANCE_DASH_FAST_MODEL_ID],
            "includeGenerateAudio": True,
            "defaultGenerateAudio": True,
        }
        normalized.pop("imageReferenceField", None)
        return normalized
    if not guanzhuan_seedance_dash_pro_alias(model_id, model_code):
        return params
    normalized = normalize_public_video_params(SEEDANCE_DASH_PRO_MODEL_ID, params)
    return {
        **normalized,
        "includeGenerateAudio": True,
        "defaultGenerateAudio": True,
    }


def apply_channel24_seedance_params(model_id: str, model_code: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if guanzhuan_seedance_dash_fast_alias(model_id, model_code) or guanzhuan_seedance_dash_pro_alias(model_id, model_code):
        return params
    kind = channel24_seedance_kind(model_id, model_code)
    if not kind:
        return params
    resolutions = channel24_seedance_resolutions(kind, model_id, model_code)
    return {
        **params,
        **CHANNEL24_SEEDANCE_PROTOCOL_PARAMS,
        "supportedResolutions": list(resolutions),
        "defaultResolutionName": "720p",
    }


def apply_channel24_seedance_pricing_meta(model_id: str, model_code: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if guanzhuan_seedance_dash_fast_alias(model_id, model_code) or guanzhuan_seedance_dash_pro_alias(model_id, model_code):
        return meta
    kind = channel24_seedance_kind(model_id, model_code)
    tiers = CHANNEL24_SEEDANCE_RESOLUTION_PRICES.get(kind)
    if not tiers:
        return meta
    parts = ["按实际秒数折算"]
    resolution_prices: Dict[str, Dict[str, float]] = {}
    resolutions = channel24_seedance_resolutions(kind, model_id, model_code)
    for resolution in resolutions:
        tier = tiers[resolution]
        price_for_15 = float(tier["modelPrice"])
        price_per_second = price_for_15 / 15.0
        admin_price_for_15 = float(tier["adminPrice"])
        resolution_prices[resolution] = {
            "modelPrice": price_for_15,
            "adminPrice": admin_price_for_15,
            "pricePerSecond": price_per_second,
            "adminPricePerSecond": admin_price_for_15 / 15.0,
        }
        parts.append(
            f"{resolution} ￥{format_newapi_number(price_for_15)} / 15秒"
            f"（￥{format_newapi_number(price_per_second)} / 秒）"
        )
    label = " · ".join(parts)
    pricing = {
        "quotaType": 1,
        "billingUnit": "second",
        "source": "channel24-seedance-resolution",
        "label": label,
        "resolutionPrices": resolution_prices,
    }
    return {**meta, "pricing": pricing, "priceLabel": label}


def apply_zexitongxue_sora_vip3_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if meta.get("pricing"):
        return meta
    canonical_id = canonical_provider_model_id(model_id)
    spec = ZEXITONGXUE_SORA_VIP3_MODEL_SPECS.get(canonical_id)
    price = newapi_float_value(spec.get("modelPrice")) if spec else None
    if price is None:
        return meta
    label = f"模型价格 ￥{format_newapi_number(price)} / 次"
    pricing = {
        "quotaType": 1,
        "modelPrice": price,
        "source": "zexitongxue-sora-vip3",
        "label": label,
    }
    return {**meta, "pricing": pricing, "priceLabel": label}


def apply_artifex_seedence20_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    spec = ARTIFEX_SEEDENCE20_MODEL_SPECS.get(canonical_id)
    if not spec:
        return meta
    next_meta = dict(meta)
    description = str(spec.get("description") or "").strip()
    if description:
        next_meta["description"] = description
    if next_meta.get("pricing"):
        return next_meta
    model_price = newapi_float_value(spec.get("modelPrice")) if spec else None
    if model_price is None:
        return next_meta
    admin_price = newapi_float_value(spec.get("adminPrice"))
    label = f"模型价格 ￥{format_newapi_number(model_price)} / 次"
    pricing = {
        "quotaType": 1,
        "modelPrice": model_price,
        "source": "artifex-seedence20",
        "label": label,
    }
    if admin_price is not None:
        pricing["adminPrice"] = admin_price
    return {**next_meta, "pricing": pricing, "priceLabel": label}


def apply_shishikeji_channel4_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    spec = SHISHIKEJI_CHANNEL4_MODEL_SPECS.get(canonical_id)
    if not spec:
        return meta
    next_meta = dict(meta)
    model_price = newapi_float_value(spec.get("modelPrice"))
    admin_price = newapi_float_value(spec.get("adminPrice"))
    if model_price is None:
        return next_meta
    label = f"模型价格 ￥{format_newapi_number(model_price)} / 次"
    pricing = {
        "quotaType": 1,
        "modelPrice": model_price,
        "billingUnit": "request",
        "source": "shishikeji-channel4",
        "label": label,
    }
    if admin_price is not None:
        pricing["adminPrice"] = admin_price
    return {**next_meta, "pricing": pricing, "priceLabel": label}


def apply_hongniao_channel36_pricing_meta(model_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    canonical_id = canonical_provider_model_id(model_id)
    spec = HONGNIAO_CHANNEL36_MODEL_SPECS.get(canonical_id)
    if not spec:
        return meta
    model_price = newapi_float_value(spec.get("modelPrice"))
    admin_price = newapi_float_value(spec.get("adminPrice"))
    if model_price is None:
        return meta
    label = f"模型价格 ￥{format_newapi_number(model_price)} / 次"
    aliases = [
        spec["modelName"],
        spec["displayName"],
        canonical_id,
        spec["upstreamModelName"],
    ]
    pricing = {
        "quotaType": 1,
        "modelPrice": model_price,
        "billingUnit": "request",
        "source": "hongniao-channel36",
        "label": label,
    }
    if admin_price is not None:
        pricing["adminPrice"] = admin_price
    next_meta = {**meta, "pricing": pricing, "priceLabel": label}
    model_introduction = str(spec.get("modelIntroduction") or "").strip()
    if model_introduction:
        next_meta["modelIntroduction"] = model_introduction
    existing_aliases = [
        str(value).strip()
        for value in (meta.get("aliases") if isinstance(meta.get("aliases"), list) else [])
        if str(value).strip()
    ]
    next_meta["aliases"] = list(dict.fromkeys([*existing_aliases, *aliases]))
    return next_meta


def xinghe_stable_image_resolution(model_id: str, model_code: str = "") -> str:
    text = f"{model_id or ''} {model_code or ''}".lower()
    text = text.replace("（", "(").replace("）", ")").replace(" ", "")
    if "gpt-image-2" not in text:
        return ""
    if "4k" in text:
        return "4K"
    if "2k" in text:
        return "2K"
    if "1k" in text or "稳定版" in text:
        return "1K"
    return ""


def apply_xinghe_stable_image_pricing_meta(model_id: str, model_code: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    resolution = xinghe_stable_image_resolution(model_id, model_code)
    price = XINGHE_STABLE_IMAGE_PRICES.get(resolution)
    if not price:
        return meta
    model_price = price["modelPrice"]
    admin_price = price["adminPrice"]
    label = f"模型价格 ￥{format_newapi_number(model_price)} / 次"
    pricing = {
        "quotaType": 1,
        "modelPrice": model_price,
        "adminPrice": admin_price,
        "resolution": resolution,
        "source": "xinghe-stable-image",
        "label": label,
    }
    return {**meta, "pricing": pricing, "priceLabel": label}


def sync_newapi_remote_provider_models(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = utc_now()
    created = 0
    updated = 0
    skipped = 0
    synced_ids: List[str] = []
    with connect() as conn:
        for record in records:
            if not isinstance(record, dict):
                skipped += 1
                continue
            model_code = newapi_remote_model_code(record)
            model_id = newapi_remote_model_id(model_code)
            if is_removed_provider_model_id(model_code) or is_removed_provider_model_id(model_id):
                skipped += 1
                continue
            model_code = request_model_code_for_provider_model(model_code) or model_code
            grok_variant_spec = grok_video_variant_spec_for_model_code(model_code)
            grok15_model = is_grok15_video_model(model_code)
            grok_unified = is_grok_video_unified_model(model_code)
            if grok_variant_spec:
                model_id = grok_variant_spec["model"]
                model_code = grok_variant_spec["model"]
            elif grok15_model:
                model_id = GROK15_VIDEO_MODEL_ID
                model_code = GROK15_VIDEO_MODEL_ID
            elif grok_unified:
                model_id = GROK_VIDEO_MODEL_ID
                model_code = GROK_VIDEO_MODEL_ID
            elif looks_like_grok_video_model(model_code):
                skipped += 1
                continue
            known_model_id = (
                canonical_provider_model_id(model_id) in DEFAULT_PUBLIC_PROVIDER_MODEL_IDS
                or canonical_provider_model_id(model_id) in MUSE_VIDEO_MODEL_IDS
                or canonical_provider_model_id(model_id) in LOW_PRICE_JIMENG_VIDEO_MODEL_IDS
                or canonical_provider_model_id(model_id) in XINGHE_SORA_VIDEO_MODEL_IDS
                or canonical_provider_model_id(model_id) in ZEXITONGXUE_SORA_VIP3_MODEL_IDS
                or canonical_provider_model_id(model_id) in ARTIFEX_SEEDENCE20_MODEL_IDS
                or canonical_provider_model_id(model_id) in CHANNEL27_911_SEEDANCE_MODEL_IDS
                or canonical_provider_model_id(model_id) in SHISHIKEJI_CHANNEL4_MODEL_IDS
                or canonical_provider_model_id(model_id) in CHANNEL31_FACE_MODEL_IDS
                or canonical_provider_model_id(model_id) in CHANNEL35_NO_AUDIO_SPECIAL_MODEL_IDS
                or canonical_provider_model_id(model_id) in HONGNIAO_CHANNEL36_MODEL_IDS
                or canonical_provider_model_id(model_id) in HERMES_SEEDANCE_MODEL_IDS
                or canonical_provider_model_id(model_id) in CHANNEL24_SEEDANCE_MODEL_IDS
            )
            if not model_code or (not grok_variant_spec and not known_model_id and not re.fullmatch(r"^[A-Za-z0-9_.-]+$", model_id)):
                skipped += 1
                continue
            channel24_seedance_model = canonical_provider_model_id(model_id) in CHANNEL24_SEEDANCE_MODEL_IDS
            capability = "video.generate" if (grok_variant_spec or grok15_model or grok_unified or channel24_seedance_model) else infer_newapi_remote_capability(record)
            if capability not in PUBLIC_MODEL_CAPABILITIES:
                skipped += 1
                continue
            adapter = (
                "grok2api.video"
                if (grok_variant_spec or grok_unified)
                else "newapi.video"
                if grok15_model
                else infer_adapter_for_remote_model(record, capability)
            )
            display_name = str(record.get("displayName") or record.get("display_name") or record.get("name") or model_code).strip()
            params = newapi_remote_model_params(record)
            meta = newapi_remote_model_meta(record)
            params, meta = preserve_builtin_remote_model_defaults(model_id, params, meta)
            params = apply_guanzhuan_seedance_dash_params(model_id, model_code, params)
            params = apply_channel24_seedance_params(model_id, model_code, params)
            low_price_seed = seeded_provider_model(model_id) if canonical_provider_model_id(model_id) in LOW_PRICE_JIMENG_VIDEO_MODEL_IDS else None
            if low_price_seed:
                model_code = low_price_seed.get("modelName") or model_code
                display_name = low_price_seed.get("displayName") or display_name
            vip3_spec = ZEXITONGXUE_SORA_VIP3_MODEL_SPECS.get(canonical_provider_model_id(model_id))
            if vip3_spec:
                display_name = vip3_spec.get("displayName") or display_name
            artifex_spec = ARTIFEX_SEEDENCE20_MODEL_SPECS.get(canonical_provider_model_id(model_id))
            if artifex_spec:
                display_name = artifex_spec.get("displayName") or display_name
            shishikeji_spec = SHISHIKEJI_CHANNEL4_MODEL_SPECS.get(canonical_provider_model_id(model_id))
            if shishikeji_spec:
                display_name = shishikeji_spec.get("displayName") or display_name
            channel35_spec = CHANNEL35_NO_AUDIO_SPECIAL_MODEL_SPECS.get(canonical_provider_model_id(model_id))
            if channel35_spec:
                display_name = channel35_spec.get("displayName") or display_name
            if grok_variant_spec:
                seed = grok_video_variant_seed(grok_variant_spec)
                display_name = seed["displayName"]
                params = {**params, **(seed.get("params") or {})}
                meta = merge_remote_meta_with_seed_defaults(meta, seed.get("meta") or {})
            elif grok15_model:
                seed = seeded_provider_model(GROK15_VIDEO_MODEL_ID)
                if seed:
                    display_name = seed["displayName"]
                    params = {**params, **(seed.get("params") or {})}
                    meta = merge_remote_meta_with_seed_defaults(meta, seed.get("meta") or {})
            elif grok_unified:
                seed = GROK_VIDEO_MODEL_SEEDS[0]
                display_name = seed["displayName"]
                params = {**params, **(seed.get("params") or {})}
                meta = merge_remote_meta_with_seed_defaults(meta, seed.get("meta") or {})
            existing = conn.execute("SELECT enabled, meta_json FROM provider_models WHERE id = ?", (model_id,)).fetchone()
            if existing:
                meta = merge_existing_remote_pricing_meta(meta, json_load(existing["meta_json"], {}))
            meta = apply_xinghe_sora_pricing_meta(model_id, meta)
            meta = apply_low_price_jimeng_pricing_meta(model_id, meta)
            meta = apply_channel27_911_seedance_pricing_meta(model_id, meta)
            meta = apply_channel31_face_pricing_meta(model_id, meta)
            meta = apply_channel35_no_audio_special_pricing_meta(model_id, meta)
            meta = apply_zexitongxue_sora_vip3_pricing_meta(model_id, meta)
            meta = apply_artifex_seedence20_pricing_meta(model_id, meta)
            meta = apply_shishikeji_channel4_pricing_meta(model_id, meta)
            meta = apply_hongniao_channel36_pricing_meta(model_id, meta)
            meta = apply_xinghe_stable_image_pricing_meta(model_id, model_code, meta)
            meta = apply_channel24_seedance_pricing_meta(model_id, model_code, meta)
            if existing:
                updated += 1
            else:
                created += 1
            conn.execute(
                """
                INSERT INTO provider_models(
                  id, provider_id, capability, model_name, display_name, adapter, enabled,
                  params_json, meta_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  provider_id = excluded.provider_id,
                  capability = excluded.capability,
                  model_name = excluded.model_name,
                  display_name = excluded.display_name,
                  adapter = excluded.adapter,
                  params_json = excluded.params_json,
                  meta_json = excluded.meta_json,
                  updated_at = excluded.updated_at
                """,
                (
                    model_id,
                    NEWAPI_PROVIDER_ID,
                    capability,
                    model_code,
                    display_name or model_code,
                    adapter,
                    int(existing["enabled"]) if existing else 1,
                    json_dump(params),
                    json_dump(meta),
                    now,
                    now,
                ),
            )
            synced_ids.append(model_id)
        synced_canonical_ids = {canonical_provider_model_id(model_id) for model_id in synced_ids}
        if synced_canonical_ids:
            alias_rows = conn.execute(
                "SELECT id FROM provider_models WHERE provider_id = ?",
                (NEWAPI_PROVIDER_ID,),
            ).fetchall()
            for row in alias_rows:
                row_id = row["id"]
                canonical_id = canonical_provider_model_id(row_id)
                if canonical_id != row_id and canonical_id in synced_canonical_ids:
                    conn.execute("DELETE FROM provider_models WHERE id = ?", (row_id,))
        conn.commit()
    sync_provider_files()
    return {"created": created, "updated": updated, "skipped": skipped, "modelIds": synced_ids}


def prune_newapi_remote_provider_models_to_catalog(model_ids: List[str]) -> Dict[str, int]:
    keep_ids = {
        canonical_provider_model_id(model_id)
        for model_id in (model_ids or [])
        if str(model_id or "").strip()
    }
    deleted = 0
    kept = 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, meta_json
            FROM provider_models
            WHERE provider_id = ?
            """,
            (NEWAPI_PROVIDER_ID,),
        ).fetchall()
        for row in rows:
            canonical_id = canonical_provider_model_id(row["id"])
            if canonical_id in SERVER_SCOPED_MUSE_VIDEO_MODEL_IDS and canonical_id not in keep_ids:
                conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
                deleted += 1
                continue
            meta = json_load(row["meta_json"], {})
            if meta.get("managedBy") != "newapi.remote-models":
                continue
            if canonical_id in keep_ids:
                kept += 1
                continue
            conn.execute("DELETE FROM provider_models WHERE id = ?", (row["id"],))
            deleted += 1
        conn.commit()
    if deleted:
        sync_provider_files()
    return {"deleted": deleted, "kept": kept}


def resolve_provider_model(job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    capability = normalize_job_type(job_type, payload)
    all_enabled_models = list_provider_models(enabled_only=True, include_internal=True)
    models = [model for model in all_enabled_models if model_supports_capability(model, capability)]
    all_models = list_provider_models(include_internal=True, respect_account_availability=False)
    explicit_ids = [
        payload.get("providerModelId"),
        payload.get("provider_model_id"),
        payload.get("modelId"),
        payload.get("model_id"),
    ]
    for explicit_id in explicit_ids:
        if not explicit_id:
            continue
        candidate_id = canonical_provider_model_id(explicit_id)
        if is_removed_provider_model_id(explicit_id):
            for replacement_id in removed_provider_model_replacement_ids(explicit_id):
                replacement = next(
                    (
                        model
                        for model in models
                        if canonical_provider_model_id(model.get("id")) == replacement_id
                        and model.get("enabled")
                        and provider_model_available_for_account(model)
                        and model_supports_capability(model, capability)
                    ),
                    None,
                )
                if replacement:
                    return replacement
            raise HTTPException(status_code=400, detail=f"模型 {explicit_id} 已下架")
        exact_model = next((model for model in all_models if model["id"] == candidate_id), None)
        if exact_model:
            if (exact_model.get("meta") or {}).get("hiddenFromPicker"):
                replacement = account_available_replacement_model(exact_model, models, capability)
                if replacement:
                    return replacement
            if not provider_model_available_for_account(exact_model):
                replacement = account_available_replacement_model(exact_model, models, capability)
                if replacement:
                    return replacement
                raise HTTPException(status_code=400, detail=f"模型 {exact_model.get('displayName') or exact_model.get('modelName') or exact_model.get('id')} 不在当前主站账号可用模型列表中")
            if not exact_model.get("enabled"):
                raise HTTPException(status_code=400, detail=f"模型 {exact_model.get('displayName') or exact_model.get('modelName') or exact_model.get('id')} 已禁用")
            if not model_supports_capability(exact_model, capability):
                raise HTTPException(status_code=400, detail=f"模型 {exact_model.get('displayName') or exact_model.get('modelName') or exact_model.get('id')} 不支持 {capability}")
            return exact_model

        name_matches = [
            model for model in all_models
            if model["modelName"] == explicit_id or model["modelName"] == candidate_id
        ]
        for model in name_matches:
            if model.get("enabled") and provider_model_available_for_account(model) and model_supports_capability(model, capability):
                return model
        for model in name_matches:
            if model.get("enabled") and not provider_model_available_for_account(model) and model_supports_capability(model, capability):
                replacement = account_available_replacement_model(model, models, capability)
                if replacement:
                    return replacement
                raise HTTPException(status_code=400, detail=f"模型 {model.get('displayName') or model.get('modelName') or model.get('id')} 不在当前主站账号可用模型列表中")
        for model in name_matches:
            if not model.get("enabled"):
                raise HTTPException(status_code=400, detail=f"模型 {model.get('displayName') or model.get('modelName') or model.get('id')} 已禁用")
        if name_matches:
            model = name_matches[0]
            raise HTTPException(status_code=400, detail=f"模型 {model.get('displayName') or model.get('modelName') or model.get('id')} 不支持 {capability}")

    label = str(payload.get("model") or payload.get("modelName") or "").strip().lower()
    if label:
        if is_removed_provider_model_id(label):
            for replacement_id in removed_provider_model_replacement_ids(label):
                replacement = next(
                    (
                        model
                        for model in models
                        if canonical_provider_model_id(model.get("id")) == replacement_id
                        and model.get("enabled")
                        and provider_model_available_for_account(model)
                        and model_supports_capability(model, capability)
                    ),
                    None,
                )
                if replacement:
                    return replacement
            raise HTTPException(status_code=400, detail=f"模型 {label} 已下架")
        canonical_label = canonical_provider_model_id(label)
        for model in models:
            if model["id"] == canonical_label and model.get("enabled") and model_supports_capability(model, capability):
                return model
        for model in models:
            if label in _normalized_model_tokens(model):
                return model

    provider_id = str(payload.get("provider") or "").strip()
    if provider_id and provider_id != OLD_PROVIDER_ID:
        for model in models:
            if model["providerId"] == provider_id and model["enabled"]:
                return model

    fallback_id = (
        LOCAL_UPSCALE_MODEL_ID if capability == "image.upscale"
        else IMAGE_ANALYZE_MODEL_ID if capability == "image.analyze"
        else "gpt-image-2" if capability == "image.generate"
        else "gpt-5.5" if capability in TEXT_MODEL_CAPABILITIES
        else ""
    )
    for model in models:
        if model["id"] == fallback_id:
            return model
    if models:
        return models[0]
    raise HTTPException(status_code=400, detail=f"暂无 {capability} 可用模型，请到模型配置新增或启用模型")


class ProjectCreate(BaseModel):
    id: Optional[str] = None
    name: str = "未命名"


class ProjectStorageUpdate(BaseModel):
    path: str


class JianyingSettingsUpdate(BaseModel):
    path: str = ""


class GraphSave(BaseModel):
    project: Optional[Dict[str, Any]] = None
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    assets: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    designSpacePackage: Optional[Dict[str, Any]] = None
    allowEmptyOverwrite: bool = False


class GraphPatch(BaseModel):
    nodesUpsert: List[Dict[str, Any]] = Field(default_factory=list)
    nodeIdsDelete: List[str] = Field(default_factory=list)
    edgesUpsert: List[Dict[str, Any]] = Field(default_factory=list)
    edgeIdsDelete: List[str] = Field(default_factory=list)


class LibrarySave(BaseModel):
    assets: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []


class NodeSave(BaseModel):
    node: Dict[str, Any] = Field(default_factory=dict)


class HistoryCreate(BaseModel):
    project_id: str = "local-default"
    action: str = "job.generate"
    target_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class HistoryUpdate(BaseModel):
    action: Optional[str] = None
    target_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class AssetImport(BaseModel):
    project_id: str = "local-default"
    file_path: str
    kind: Optional[str] = None
    copy_file: bool = Field(default=True, alias="copy")
    defer_copy: bool = False
    meta: Dict[str, Any] = {}


class AssetWrite(BaseModel):
    project_id: str = "local-default"
    filename: str = "asset.png"
    data_url: str
    kind: Optional[str] = None
    mime: Optional[str] = None
    meta: Dict[str, Any] = {}


class AssetPromote(BaseModel):
    title: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class JobCreate(BaseModel):
    project_id: str = "local-default"
    node_id: Optional[str] = None
    type: str = "image.generate"
    payload: Dict[str, Any] = {}


class ProviderCreate(BaseModel):
    id: str
    name: str
    base_url: str = ""
    auth_type: str = "bearer"
    enabled: bool = False
    capabilities: List[str] = []
    meta: Dict[str, Any] = {}
    api_key: Optional[str] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    enabled: Optional[bool] = None
    capabilities: Optional[List[str]] = None
    meta: Optional[Dict[str, Any]] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False


class ProviderModelUpdate(BaseModel):
    capability: Optional[str] = None
    model_name: Optional[str] = None
    display_name: Optional[str] = None
    adapter: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class ProviderModelCreate(BaseModel):
    id: Optional[str] = None
    provider_id: str = NEWAPI_PROVIDER_ID
    capability: str
    model_name: str
    display_name: Optional[str] = None
    adapter: Optional[str] = None
    enabled: bool = True
    params: Dict[str, Any] = {}
    meta: Dict[str, Any] = {}


class NewApiLogin(BaseModel):
    base_url: str = DEFAULT_NEWAPI_BASE_URL
    username: str
    password: str


class NewApiRegister(BaseModel):
    base_url: str = DEFAULT_NEWAPI_BASE_URL
    username: str
    password: str
    email: Optional[str] = None
    verification_code: Optional[str] = None
    aff_code: Optional[str] = None
    auto_login: bool = True


class NewApiVerification(BaseModel):
    base_url: str = DEFAULT_NEWAPI_BASE_URL
    email: str


class NewApiTokenCreate(BaseModel):
    name: str = "漫创AI"
    remain_quota: int = 0
    unlimited_quota: bool = True
    expired_time: int = -1
    model_limits: List[str] = []
    allow_ips: List[str] = []
    group: str = ""
    set_default: bool = True


class NewApiDefaultKeyUpdate(BaseModel):
    api_key: str
    verify: bool = True


class NewApiRedeem(BaseModel):
    code: str


class PromptCreate(BaseModel):
    project_id: Optional[str] = None
    scope: str = "global"
    category: str = "general"
    title: str
    content: str
    tags: List[str] = []
    meta: Dict[str, Any] = {}


class PromptUpdate(BaseModel):
    project_id: Optional[str] = None
    scope: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    meta: Optional[Dict[str, Any]] = None


class SeedancePortraitAssetFromPathUpload(BaseModel):
    file_path: Optional[str] = None
    filePath: Optional[str] = None
    name: str = ""
    description: str = ""
    group_id: Optional[int] = None
    groupId: Optional[int] = None


app = FastAPI(title="漫创AI Local Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(jianying_router)


@app.middleware("http")
async def electron_asset_cors(request, call_next):
    origin = request.headers.get("origin")
    is_electron_file_origin = origin == "null"
    is_asset_request = request.url.path.startswith("/assets/")
    if is_electron_file_origin and is_asset_request:
        if request.method == "OPTIONS":
            requested_method = request.headers.get("access-control-request-method", "GET").upper()
            if requested_method in {"GET", "HEAD", "OPTIONS"}:
                return Response(
                    status_code=200,
                    headers={
                        "Access-Control-Allow-Origin": "null",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", ""),
                        "Vary": "Origin",
                    },
                )
        response = await call_next(request)
        if request.method in {"GET", "HEAD"}:
            response.headers["Access-Control-Allow-Origin"] = "null"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response
    return await call_next(request)

websockets: List[WebSocket] = []


@app.on_event("startup")
def on_startup() -> None:
    set_jianying_drafts_root(configured_jianying_drafts_root())
    init_db()


async def broadcast(event: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    for ws in list(websockets):
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in websockets:
            websockets.remove(ws)


def ensure_project(project_id: str, name: Optional[str] = None, *, sync_files: bool = True) -> None:
    project_id = validate_project_id(project_id)
    now = utc_now()
    ensure_project_dirs(project_id)
    clean_name = (name or "").strip()
    with connect() as conn:
        if clean_name:
            conn.execute(
                """
                INSERT INTO projects(id, name, created_at, updated_at, version)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  updated_at = excluded.updated_at,
                  version = excluded.version
                """,
                (project_id, clean_name, now, now, PROJECT_STORAGE_VERSION),
            )
        else:
            conn.execute(
                """
                INSERT INTO projects(id, name, created_at, updated_at, version)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  updated_at = excluded.updated_at,
                  version = excluded.version
                """,
                (project_id, "未命名", now, now, PROJECT_STORAGE_VERSION),
            )
        conn.commit()
    if sync_files:
        sync_project_files(project_id)


def ensure_global_asset_project(*, sync_files: bool = True) -> None:
    project_id = GLOBAL_ASSET_PROJECT_ID
    now = utc_now()
    ensure_project_dirs(project_id)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(id, name, created_at, updated_at, version)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              updated_at = excluded.updated_at,
              version = excluded.version
            """,
            (project_id, "全局资产库", now, now, PROJECT_STORAGE_VERSION),
        )
        conn.commit()
    if sync_files:
        sync_global_asset_file()


def ensure_asset_scope(project_id: str, name: Optional[str] = None, *, sync_files: bool = True) -> None:
    project_id = validate_project_id(project_id)
    if is_global_asset_project(project_id):
        ensure_global_asset_project(sync_files=sync_files)
        return
    ensure_project(project_id, name, sync_files=sync_files)


def project_storage_payload() -> Dict[str, Any]:
    default_dir = default_project_storage_dir()
    try:
        is_default = PROJECTS_DIR.resolve() == default_dir.resolve()
    except Exception:
        is_default = str(PROJECTS_DIR) == str(default_dir)
    return {
        "path": str(PROJECTS_DIR),
        "projectStorageDir": str(PROJECTS_DIR),
        "defaultPath": str(default_dir),
        "database": str(DB_PATH),
        "settingsPath": str(SETTINGS_PATH),
        "isDefault": is_default,
    }


def ensure_writable_directory(path: Path) -> Path:
    target = path.expanduser()
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=400, detail="Selected path is not a folder")
    target.mkdir(parents=True, exist_ok=True)
    probe = target / ".libai-write-test"
    try:
      probe.write_text("ok", encoding="utf-8")
      probe.unlink(missing_ok=True)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Cannot write to selected folder: {error}") from error
    return target


def copy_project_storage(old_root: Path, new_root: Path) -> None:
    try:
        if old_root.resolve() == new_root.resolve():
            return
    except Exception:
        if str(old_root) == str(new_root):
            return
    if not old_root.exists():
        return
    new_root.mkdir(parents=True, exist_ok=True)
    try:
        new_resolved = new_root.resolve()
    except Exception:
        new_resolved = None

    def is_new_root(candidate: Path) -> bool:
        if new_resolved is None:
            return False
        try:
            return candidate.resolve() == new_resolved
        except Exception:
            return False

    def ignore_new_root(current_dir: str, names: List[str]) -> set[str]:
        return {
            name
            for name in names
            if is_new_root(Path(current_dir) / name)
        }

    for child in old_root.iterdir():
        if child.is_dir() and is_new_root(child):
            continue
        target = new_root / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True, ignore=ignore_new_root)
        elif child.is_file() and not target.exists():
            shutil.copy2(child, target)


def rewrite_asset_paths_for_storage(old_root: Path, new_root: Path) -> None:
    try:
        old_resolved = old_root.resolve()
        new_resolved = new_root.resolve()
    except Exception:
        return
    if old_resolved == new_resolved:
        return

    def rewrite(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return raw
        try:
            rel = Path(raw).resolve().relative_to(old_resolved)
            return str(new_resolved / rel)
        except Exception:
            return raw

    with connect() as conn:
        rows = conn.execute("SELECT id, path, thumb_path, meta_json FROM assets").fetchall()
        for row in rows:
            new_path = rewrite(row["path"])
            new_thumb = rewrite(row["thumb_path"])
            meta = json_load(row["meta_json"], {})
            if meta.get("path"):
                meta["path"] = rewrite(meta.get("path"))
            if meta.get("thumbPath"):
                meta["thumbPath"] = rewrite(meta.get("thumbPath"))
            conn.execute(
                "UPDATE assets SET path = ?, thumb_path = ?, meta_json = ? WHERE id = ?",
                (new_path, new_thumb, json_dump(meta), row["id"]),
            )
        conn.commit()


def sync_all_project_files() -> None:
    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM projects ORDER BY updated_at DESC")]
    for project_id in ids:
        try:
            sync_project_files(project_id)
        except HTTPException:
            pass


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "appDataDir": str(DATA_DIR),
        "projectStorageDir": str(PROJECTS_DIR),
        "database": str(DB_PATH),
        "ffmpeg": {
            "ffmpeg": binary_runtime_status("ffmpeg", "LIBAI_FFMPEG_PATH"),
            "ffprobe": binary_runtime_status("ffprobe", "LIBAI_FFPROBE_PATH"),
        },
        "vsr": subtitle_removal_runtime_status(),
        "time": utc_now(),
    }


@app.get("/design-space/prompt-templates")
def read_design_prompt_templates() -> Dict[str, Any]:
    return list_design_prompt_templates()


@app.get("/settings/project-storage")
def read_project_storage_settings() -> Dict[str, Any]:
    return project_storage_payload()


@app.post("/settings/project-storage")
def update_project_storage_settings(body: ProjectStorageUpdate) -> Dict[str, Any]:
    raw_path = (body.path or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Project storage path is required")
    old_root = PROJECTS_DIR
    new_root = ensure_writable_directory(Path(raw_path))
    copy_project_storage(old_root, new_root)
    rewrite_asset_paths_for_storage(old_root, new_root)
    settings = read_settings_file()
    settings["projectStorageDir"] = str(new_root)
    settings["updatedAt"] = utc_now()
    write_settings_file(settings)
    set_project_storage_dir(new_root)
    sync_all_project_files()
    return {
        "ok": True,
        "settings": project_storage_payload(),
        "projects": list_project_summaries(),
    }


@app.get("/jianying/settings")
def read_jianying_settings() -> Dict[str, Any]:
    path = configured_jianying_drafts_root()
    return {
        "jianyingDraftsRoot": path,
        "jianying_drafts_root": path,
        "status": jianying_path_status(path),
    }


@app.post("/jianying/settings")
def update_jianying_settings(body: JianyingSettingsUpdate) -> Dict[str, Any]:
    raw_path = (body.path or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="剪映草稿路径不能为空")
    normalized = str(Path(raw_path).expanduser().resolve())
    if not os.path.isdir(normalized):
        raise HTTPException(status_code=400, detail="剪映草稿目录不存在")
    if not is_jianying_drafts_root(normalized):
        raise HTTPException(status_code=400, detail="这不是有效的剪映草稿目录")
    if not os.access(normalized, os.W_OK):
        raise HTTPException(status_code=400, detail="剪映草稿目录不可写")

    settings = read_settings_file()
    settings["jianyingDraftsRoot"] = normalized
    settings["jianying_drafts_root"] = normalized
    settings["updatedAt"] = utc_now()
    write_settings_file(settings)
    set_jianying_drafts_root(normalized)
    return {
        "ok": True,
        "jianyingDraftsRoot": normalized,
        "jianying_drafts_root": normalized,
        "status": jianying_path_status(normalized),
    }


@app.get("/jianying/drafts-root/auto")
@app.post("/jianying/drafts-root/auto")
def auto_detect_jianying_root() -> Dict[str, Any]:
    detected = find_jianying_drafts_root()
    return {
        "found": bool(detected),
        "path": detected or None,
        "status": jianying_path_status(detected) if detected else jianying_path_status(""),
    }


@app.get("/system/config")
def read_system_config_compat() -> Dict[str, Any]:
    jianying_root = configured_jianying_drafts_root()
    return {
        "projects_root": str(PROJECTS_DIR),
        "assets_root": str(DATA_DIR / "global-assets"),
        "jianying_drafts_root": jianying_root,
    }


@app.post("/system/config")
def update_system_config_compat(body: Dict[str, Any]) -> Dict[str, Any]:
    jianying_root = str(body.get("jianying_drafts_root") or body.get("jianyingDraftsRoot") or "").strip()
    if jianying_root:
        update_jianying_settings(JianyingSettingsUpdate(path=jianying_root))
    return read_system_config_compat()


@app.get("/system/jianying-drafts-root/auto")
@app.post("/system/jianying-drafts-root/auto")
def auto_detect_jianying_root_compat() -> Dict[str, Any]:
    return auto_detect_jianying_root()


@app.get("/newapi/account")
def read_newapi_account() -> Dict[str, Any]:
    account = get_newapi_account()
    return {
        "connected": bool(account and account.get("hasAccessToken")),
        "account": account,
    }


@app.get("/desktop-announcements")
def read_desktop_announcements() -> Dict[str, Any]:
    client = get_logged_in_newapi_client()
    try:
        data = client.desktop_announcements()
        announcements = data.get("data") if isinstance(data, dict) else data
        if not isinstance(announcements, list):
            announcements = []
        server_time = data.get("serverTime") if isinstance(data, dict) else None
        return {"announcements": announcements, "serverTime": server_time}
    except NewApiError as error:
        status_code = int(error.status_code or 0) or 502
        raise HTTPException(status_code=status_code, detail=error.message) from error
    finally:
        client.close()


def desktop_announcement_stream_error_event(error: Exception) -> str:
    if isinstance(error, NewApiError):
        message = error.message
    else:
        message = exception_message(error, "桌面公告连接失败")
    payload = {
        "type": "desktop.announcement.connection-error",
        "message": message,
    }
    return f"event: desktop-announcement\ndata: {json_dump(payload)}\n\n"


@app.get("/desktop-announcements/events")
def stream_desktop_announcement_events():
    account = get_newapi_account(include_secret=True)
    if not account or not account.get("accessToken") or account.get("userId") is None:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")

    async def event_lines():
        try:
            async for line in stream_desktop_announcements(
                account["baseUrl"],
                account["accessToken"],
                int(account["userId"]),
            ):
                yield f"{line}\n" if line else "\n"
        except Exception as error:
            yield desktop_announcement_stream_error_event(error)

    return StreamingResponse(event_lines(), media_type="text/event-stream")


@app.post("/seedance/portrait-assets")
def upload_seedance_portrait_asset(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
) -> Dict[str, Any]:
    content = file.file.read(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES + 1)
    return upload_seedance_portrait_asset_bytes(
        content=content,
        filename=file.filename or "portrait.png",
        content_type=file.content_type or "application/octet-stream",
        name=name,
        description=description,
    )


@app.post("/seedance/portrait-assets/from-path")
def upload_seedance_portrait_asset_from_path(body: SeedancePortraitAssetFromPathUpload) -> Dict[str, Any]:
    raw_path = str(body.file_path or body.filePath or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="请选择要上传的角色图片")
    try:
        source = Path(raw_path).expanduser()
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"无效的角色图片路径：{error}") from error
    if not source.is_file():
        raise HTTPException(status_code=400, detail="角色图片文件不存在")

    try:
        with source.open("rb") as handle:
            content = handle.read(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES + 1)
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"角色图片读取失败：{error}") from error
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return upload_seedance_portrait_asset_bytes(
        content=content,
        filename=source.name or "portrait.png",
        content_type=content_type,
        name=body.name,
        description=body.description,
    )


@app.get("/seedance/portrait-assets")
def list_seedance_portrait_assets() -> Dict[str, Any]:
    assets = read_seedance_portrait_asset_registry()
    return {"assets": refresh_seedance_portrait_assets_from_upstream(assets)}


@app.post("/seedance/portrait-assets/channel24")
def upload_channel24_portrait_asset(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
    group_id: Optional[int] = Form(None),
) -> Dict[str, Any]:
    content = file.file.read(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES + 1)
    return upload_channel24_portrait_asset_bytes(
        content=content,
        filename=file.filename or "portrait.png",
        content_type=file.content_type or "application/octet-stream",
        name=name,
        description=description,
        group_id=group_id,
    )


@app.post("/seedance/portrait-assets/channel24/from-path")
def upload_channel24_portrait_asset_from_path(body: SeedancePortraitAssetFromPathUpload) -> Dict[str, Any]:
    raw_path = str(body.file_path or body.filePath or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="请选择要上传的角色图片")
    try:
        source = Path(raw_path).expanduser()
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"无效的角色图片路径：{error}") from error
    if not source.is_file():
        raise HTTPException(status_code=400, detail="角色图片文件不存在")

    try:
        with source.open("rb") as handle:
            content = handle.read(SEEDANCE_PORTRAIT_ASSET_MAX_BYTES + 1)
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"角色图片读取失败：{error}") from error
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return upload_channel24_portrait_asset_bytes(
        content=content,
        filename=source.name or "portrait.png",
        content_type=content_type,
        name=body.name,
        description=body.description,
        group_id=body.group_id or body.groupId,
    )


@app.get("/seedance/portrait-assets/channel24")
def list_channel24_portrait_asset_route(page: int = 1) -> Dict[str, Any]:
    return list_channel24_portrait_assets(page=page)


@app.delete("/seedance/portrait-assets/channel24/{resource_id}")
def delete_channel24_portrait_asset_route(resource_id: str) -> Dict[str, Any]:
    return delete_channel24_portrait_resource(resource_id)


@app.get("/seedance/portrait-assets/{asset_id}/preview")
def read_seedance_portrait_asset_preview(asset_id: str):
    clean_id = str(asset_id or "").strip()
    for asset in read_seedance_portrait_asset_registry():
        if asset.get("asset_id") != clean_id:
            continue
        preview_path = str(asset.get("localPreviewPath") or asset.get("local_preview_path") or "").strip()
        if not preview_path:
            break
        candidate = Path(preview_path)
        if not path_within(candidate, SEEDANCE_PORTRAIT_PREVIEWS_DIR) or not candidate.is_file():
            break
        return FileResponse(
            candidate,
            media_type=mimetypes.guess_type(str(candidate))[0] or "image/png",
        )
    raise HTTPException(status_code=404, detail="Seedance portrait preview not found")


@app.delete("/seedance/portrait-assets/{asset_id}")
def delete_seedance_portrait_asset(asset_id: str) -> Dict[str, Any]:
    clean_id = str(asset_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="缺少素材 ID")

    result: Dict[str, Any] = {
        "ok": True,
        "deleted": clean_id,
        "removed": False,
        "upstreamDeleted": False,
    }
    if clean_id.startswith("reference_"):
        result["removed"] = remove_seedance_portrait_asset_from_registry(clean_id)
        result["upstreamSkipped"] = True
        return result

    try:
        credentials = require_seedance_asset_credentials()
    except HTTPException as error:
        result["upstreamWarning"] = str(error.detail or "上游素材删除未执行")
        return result

    last_warning = ""
    for delete_url in seedance_portrait_asset_metadata_urls(credentials, f"/{quote(clean_id, safe='')}"):
        try:
            response = httpx.delete(
                delete_url,
                headers={"Authorization": f"Bearer {credentials['apiKey']}"},
                timeout=seedance_portrait_asset_metadata_timeout_seconds(),
                follow_redirects=True,
                trust_env=provider_http_trust_env(),
            )
        except httpx.HTTPError as error:
            last_warning = exception_message(error, "上游素材删除失败")
            continue
        if response.status_code == 404 or 200 <= response.status_code < 300:
            result["removed"] = remove_seedance_portrait_asset_from_registry(clean_id)
            result["upstreamDeleted"] = True
            return result
        last_warning = extract_seedance_asset_error_message(response)

    if last_warning:
        raise HTTPException(status_code=response.status_code if 'response' in locals() else 502, detail=last_warning)
    raise HTTPException(status_code=502, detail="上游素材删除失败")


@app.post("/newapi/login")
def login_newapi_account(body: NewApiLogin) -> Dict[str, Any]:
    base_url = normalize_newapi_base_url(body.base_url)
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入中转站账号和密码")

    client = NewApiClient(base_url)
    default_api_key = ""
    try:
        login_info = client.login(username, password)
        snapshot = read_newapi_snapshot(client, token_size=50)
        default_api_key = ensure_newapi_default_key(
            client,
            snapshot,
            base_url=base_url,
            user_id=login_info.get("user_id"),
            username=login_info.get("username") or username,
        )
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()

    account = upsert_newapi_account(
        base_url=base_url,
        login_info=login_info,
        user=snapshot["user"],
        models=snapshot["models"],
        tokens=snapshot["tokens"],
        default_api_key=default_api_key,
    )
    provider = sync_newapi_default_key_if_usable(base_url, default_api_key)
    return {
        "connected": True,
        "account": account,
        "provider": provider,
        "defaultApplied": bool(provider),
        "user": sanitize_newapi_user(snapshot["user"]),
        "models": snapshot["models"],
        "tokens": snapshot["tokens"],
        "warnings": snapshot["warnings"],
    }


@app.post("/newapi/register")
def register_newapi_account(body: NewApiRegister) -> Dict[str, Any]:
    base_url = normalize_newapi_base_url(body.base_url)
    username = (body.username or "").strip()
    password = body.password or ""
    email = (body.email or "").strip() or None
    verification_code = (body.verification_code or "").strip() or None
    aff_code = (body.aff_code or "").strip() or None
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入注册账号和密码")
    if not aff_code:
        raise HTTPException(status_code=400, detail="请输入邀请码")
    if len(password) < NEWAPI_PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=400, detail=f"密码长度不符合中转站要求，请至少输入 {NEWAPI_PASSWORD_MIN_LENGTH} 位密码。")

    client = NewApiClient(base_url)
    default_api_key = ""
    try:
        client.register(
            username,
            password,
            email=email,
            verification_code=verification_code,
            aff_code=aff_code,
        )
        if not body.auto_login:
            return {"registered": True, "connected": False, "account": None}
        login_info = client.login(username, password)
        snapshot = read_newapi_snapshot(client, token_size=50)
        default_api_key = ensure_newapi_default_key(
            client,
            snapshot,
            base_url=base_url,
            user_id=login_info.get("user_id"),
            username=login_info.get("username") or username,
        )
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()

    account = upsert_newapi_account(
        base_url=base_url,
        login_info=login_info,
        user=snapshot["user"],
        models=snapshot["models"],
        tokens=snapshot["tokens"],
        default_api_key=default_api_key,
        registration_aff_code=aff_code,
    )
    provider = sync_newapi_default_key_if_usable(base_url, default_api_key)
    return {
        "registered": True,
        "connected": True,
        "account": account,
        "provider": provider,
        "defaultApplied": bool(provider),
        "user": sanitize_newapi_user(snapshot["user"]),
        "models": snapshot["models"],
        "tokens": snapshot["tokens"],
        "warnings": snapshot["warnings"],
    }


@app.post("/newapi/verification")
def send_newapi_verification(body: NewApiVerification) -> Dict[str, Any]:
    base_url = normalize_newapi_base_url(body.base_url)
    email = (body.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="请输入邮箱")
    client = NewApiClient(base_url)
    try:
        client.send_email_verification(email)
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    return {"sent": True}


@app.post("/newapi/logout")
def logout_newapi_account() -> Dict[str, Any]:
    clear_newapi_account()
    return {"connected": False, "account": None}


@app.post("/newapi/refresh")
def refresh_newapi_account() -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    client = get_newapi_client()
    try:
        snapshot = read_newapi_snapshot(client, token_size=50)
        default_api_key = ensure_newapi_default_key(
            client,
            snapshot,
            base_url=account["baseUrl"],
            user_id=account.get("userId"),
            username=account.get("username"),
        )
    finally:
        client.close()

    saved = upsert_newapi_account(
        base_url=account["baseUrl"],
        user=snapshot["user"],
        models=snapshot["models"],
        tokens=snapshot["tokens"],
        default_api_key=default_api_key if default_api_key else None,
    )
    provider = sync_newapi_default_key_if_usable(account["baseUrl"], default_api_key)
    return {
        "connected": True,
        "account": saved,
        "provider": provider,
        "defaultApplied": bool(provider),
        "user": sanitize_newapi_user(snapshot["user"]),
        "models": snapshot["models"],
        "tokens": snapshot["tokens"],
        "warnings": snapshot["warnings"],
    }


@app.get("/newapi/models")
def read_newapi_models() -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    provider_key = ""
    try:
        provider_key = get_provider(NEWAPI_PROVIDER_ID, include_secret=True).get("apiKey") or ""
        provider_base_url = get_provider(NEWAPI_PROVIDER_ID, include_secret=True).get("baseUrl") or ""
    except HTTPException:
        provider_key = ""
        provider_base_url = ""
    account_base_url = normalize_newapi_base_url(account["baseUrl"])
    default_key = normalize_newapi_api_key(account.get("defaultApiKey") or "")
    if is_usable_newapi_api_key(default_key):
        api_key = default_key
    elif normalize_newapi_base_url(provider_base_url) == account_base_url and is_usable_newapi_api_key(provider_key):
        api_key = normalize_newapi_api_key(provider_key)
    else:
        api_key = ""
    model_sync: Optional[Dict[str, Any]] = None
    models: List[str] = []
    portal_records: List[Dict[str, Any]] = []
    portal_pricing_records: List[Dict[str, Any]] = []
    portal_loaded = False
    if account.get("accessToken") and account.get("userId"):
        portal_client = None
        try:
            portal_client = get_newapi_client()
            portal_records = portal_client.list_portal_model_records()
            portal_pricing_records = read_newapi_pricing_records(portal_client)
            portal_loaded = True
        except (HTTPException, NewApiError):
            portal_records = []
            portal_pricing_records = []
        finally:
            if portal_client is not None:
                portal_client.close()

    if portal_loaded:
        records = merge_newapi_pricing_records(portal_records, portal_pricing_records)
        models = [newapi_remote_model_code(record) for record in records if newapi_remote_model_code(record)]
        model_sync = sync_newapi_remote_provider_models(records)
        prune_result = prune_newapi_remote_provider_models_to_catalog(model_sync.get("modelIds") or [])
        model_sync = {**model_sync, "pruned": prune_result.get("deleted", 0)}
    else:
        existing_models = (account.get("meta") or {}).get("availableModels") if isinstance(account.get("meta"), dict) else []
        models = clean_newapi_model_codes(existing_models)
        prune_result = prune_newapi_remote_provider_models_to_catalog(models)
        if prune_result.get("deleted"):
            model_sync = {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "modelIds": models,
                "pruned": prune_result.get("deleted", 0),
            }
    saved = upsert_newapi_account(base_url=account["baseUrl"], models=models)
    return {"account": saved, "models": models, "modelSync": model_sync}


@app.get("/newapi/tokens")
def read_newapi_tokens() -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    client = get_newapi_client()
    try:
        snapshot = {"tokens": newapi_token_response(client.list_api_keys(size=100)), "warnings": []}
        default_api_key = ensure_newapi_default_key(
            client,
            snapshot,
            base_url=account["baseUrl"],
            user_id=account.get("userId"),
            username=account.get("username"),
        )
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    token_payloads = snapshot["tokens"]
    saved = upsert_newapi_account(
        base_url=account["baseUrl"],
        tokens=token_payloads,
        default_api_key=default_api_key if default_api_key else None,
    )
    provider = sync_newapi_default_key_if_usable(account["baseUrl"], default_api_key)
    return {"account": saved, "tokens": token_payloads, "provider": provider, "defaultApplied": bool(provider)}


@app.get("/newapi/consumption")
def read_newapi_consumption(start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    start_timestamp = parse_newapi_log_date(start)
    end_timestamp = parse_newapi_log_date(end, end_of_day=True)
    client = get_newapi_client()
    try:
        consumption = client.get_consumption_history(
            limit=None,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    consumption = enrich_newapi_consumption_with_media_proxy(consumption)
    return {
        "account": get_newapi_account(),
        "consumption": consumption,
        "summary": consumption.get("summary") or {},
        "items": consumption.get("items") or [],
    }


@app.get("/newapi/media-assets/{asset_id}/content")
def read_newapi_media_asset_content(asset_id: int, download: Optional[str] = None):
    if asset_id <= 0:
        raise HTTPException(status_code=400, detail="媒体资源 ID 不正确")
    return proxy_newapi_remote_content(f"/api/media-assets/{asset_id}/content", download=str(download or "") == "1")


@app.get("/newapi/task-videos/{task_id}/content")
def read_newapi_task_video_content(task_id: str, download: Optional[str] = None):
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id or not re.match(r"^[A-Za-z0-9_.:-]+$", clean_task_id):
        raise HTTPException(status_code=400, detail="任务 ID 不正确")
    encoded_task_id = quote(clean_task_id, safe="")
    return proxy_newapi_remote_content(f"/v1/videos/{encoded_task_id}/content", download=str(download or "") == "1")


@app.get("/newapi/usage-logs")
def read_newapi_usage_logs(
    start: Optional[str] = None,
    end: Optional[str] = None,
    request_id: Optional[str] = None,
    token_name: Optional[str] = None,
    model_name: Optional[str] = None,
    group: Optional[str] = None,
    log_type: Optional[int] = 0,
    task_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    start_timestamp = parse_newapi_log_date(start)
    end_timestamp = parse_newapi_log_date(end, end_of_day=True)
    client = get_newapi_client()
    try:
        usage = client.get_usage_history(
            limit=None,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            request_id=request_id or task_id or "",
            token_name=token_name or "",
            model_name=model_name or "",
            channel_id=channel_id or "",
            group=group or "",
            log_type=int(log_type or 0),
        )
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    usage = enrich_newapi_usage_with_local_previews(usage)
    usage = enrich_newapi_consumption_with_media_proxy(usage)
    return {
        "account": get_newapi_account(),
        "usage": usage,
        "summary": usage.get("summary") or {},
        "items": usage.get("items") or [],
    }


@app.post("/newapi/tokens")
def create_newapi_token(body: NewApiTokenCreate) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="令牌名称不能为空")
    client = get_newapi_client()
    try:
        token = client.create_api_key(
            name,
            remain_quota=max(0, int(body.remain_quota or 0)),
            unlimited_quota=bool(body.unlimited_quota),
            expired_time=int(body.expired_time or -1),
            model_limits=body.model_limits or [],
            allow_ips=body.allow_ips or [],
            group=body.group or "",
        )
        tokens = client.list_api_keys(size=100)
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()

    token_payloads = newapi_token_response(tokens)
    token_key = normalize_newapi_api_key(token.sk_key)
    can_apply_default = bool(body.set_default and is_usable_newapi_api_key(token_key))
    existing_key = account.get("defaultApiKey") or ""
    if can_apply_default:
        default_key: Optional[str] = token_key
    elif body.set_default and is_masked_newapi_api_key(existing_key):
        default_key = ""
    else:
        default_key = None
    saved = upsert_newapi_account(base_url=account["baseUrl"], tokens=token_payloads, default_api_key=default_key)
    provider = sync_newapi_provider_from_account(account["baseUrl"], token_key) if can_apply_default else None
    return {
        "account": saved,
        "token": token.to_dict(include_key=False),
        "tokens": token_payloads,
        "provider": provider,
        "defaultApplied": can_apply_default,
        "requiresManualApiKey": bool(body.set_default and not can_apply_default),
        "message": "" if can_apply_default else "中转站接口只返回脱敏 API Key，未写入默认调用密钥。请粘贴完整 sk- API Key 后再生成。",
    }


@app.post("/newapi/tokens/{token_id}/default")
def set_newapi_default_token(token_id: int) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    client = get_newapi_client()
    try:
        tokens = client.list_api_keys(size=100)
        token = next((item for item in tokens if item.id == token_id), None)
        if not token:
            raise HTTPException(status_code=404, detail="API Key 不存在")
        token_key = normalize_newapi_api_key(token.sk_key)
        if not is_usable_newapi_api_key(token_key):
            get_full_key = getattr(client, "get_api_key", None)
            if not callable(get_full_key):
                raise HTTPException(status_code=400, detail="中转站列表返回的是脱敏 API Key，不能设为默认生成密钥。请粘贴完整 sk- API Key。")
            token_key = normalize_newapi_api_key(get_full_key(token_id))
            if not is_usable_newapi_api_key(token_key):
                raise HTTPException(status_code=400, detail="中转站返回的是脱敏 API Key，不能设为默认生成密钥。请粘贴完整 sk- API Key。")
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    token_payloads = newapi_token_response(tokens)
    saved = upsert_newapi_account(base_url=account["baseUrl"], tokens=token_payloads, default_api_key=token_key)
    provider = sync_newapi_provider_from_account(account["baseUrl"], token_key)
    return {
        "account": saved,
        "token": token.to_dict(include_key=False),
        "tokens": token_payloads,
        "provider": provider,
        "defaultApplied": True,
    }


@app.post("/newapi/default-key")
def set_newapi_default_key(body: NewApiDefaultKeyUpdate) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    api_key = require_usable_newapi_api_key(body.api_key)
    model_sync: Optional[Dict[str, Any]] = None
    if body.verify:
        client = NewApiClient(account["baseUrl"])
        try:
            records = client.list_openai_model_records(api_key)
        except NewApiError as error:
            raise newapi_http_exception(error) from error
        finally:
            client.close()
        model_sync = sync_newapi_remote_provider_models(records)
    saved = upsert_newapi_account(base_url=account["baseUrl"], default_api_key=api_key)
    provider = sync_newapi_provider_from_account(account["baseUrl"], api_key)
    return {
        "account": saved,
        "provider": provider,
        "modelSync": model_sync,
        "defaultApplied": True,
    }


@app.delete("/newapi/tokens/{token_id}")
def delete_newapi_token(token_id: int) -> Dict[str, Any]:
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    client = get_newapi_client()
    try:
        client.delete_api_key(token_id)
        tokens = client.list_api_keys(size=100)
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    token_payloads = newapi_token_response(tokens)
    saved = upsert_newapi_account(base_url=account["baseUrl"], tokens=token_payloads)
    return {"account": saved, "tokens": token_payloads}


@app.post("/newapi/redeem")
def redeem_newapi_code(body: NewApiRedeem) -> Dict[str, Any]:
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入兑换码")
    account = get_newapi_account(include_secret=True)
    if not account:
        raise HTTPException(status_code=401, detail="尚未登录中转站账号")
    client = get_newapi_client()
    try:
        added_quota = client.redeem(code)
        user = client.get_user_info()
    except NewApiError as error:
        raise newapi_http_exception(error) from error
    finally:
        client.close()
    saved = upsert_newapi_account(base_url=account["baseUrl"], user=user)
    return {
        "account": saved,
        "user": sanitize_newapi_user(user),
        "addedQuota": added_quota,
    }


@app.get("/registry/nodes")
def node_registry() -> Dict[str, Any]:
    return {"nodes": NODE_DEFINITIONS, "providers": list_providers(), "models": list_provider_models()}


@app.get("/providers")
def read_providers() -> Dict[str, Any]:
    return {"providers": list_providers(), "models": list_provider_models()}


@app.post("/providers")
def create_provider(body: ProviderCreate) -> Dict[str, Any]:
    provider_id = (body.id or "").strip()
    if not provider_id or not re.fullmatch(r"^[A-Za-z0-9_.-]+$", provider_id):
        raise HTTPException(status_code=400, detail="Invalid provider id")
    if provider_id != NEWAPI_PROVIDER_ID:
        raise HTTPException(status_code=400, detail="当前版本只保留漫创AI中转站供应商")
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO providers(id, name, base_url, auth_type, api_key_encrypted, enabled, capabilities_json, meta_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                body.name.strip() or provider_id,
                (body.base_url or "").strip(),
                body.auth_type or "bearer",
                encode_secret(body.api_key),
                1 if body.enabled else 0,
                json_dump(body.capabilities or []),
                json_dump(body.meta or {}),
                now,
                now,
            ),
        )
        conn.commit()
    sync_provider_files()
    return get_provider(provider_id)


@app.put("/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderUpdate) -> Dict[str, Any]:
    current = get_provider(provider_id, include_secret=True, include_internal=False)
    patch = {
        "name": body.name if body.name is not None else current["name"],
        "base_url": body.base_url if body.base_url is not None else current["baseUrl"],
        "auth_type": body.auth_type if body.auth_type is not None else current["authType"],
        "enabled": body.enabled if body.enabled is not None else current["enabled"],
        "capabilities": body.capabilities if body.capabilities is not None else current["capabilities"],
        "meta": body.meta if body.meta is not None else current["meta"],
    }
    if body.clear_api_key:
        api_key_encrypted = ""
    elif body.api_key is not None:
        api_key_encrypted = encode_secret(body.api_key)
    else:
        api_key_encrypted = encode_secret(current.get("apiKey") or "")
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE providers
            SET name = ?, base_url = ?, auth_type = ?, api_key_encrypted = ?, enabled = ?,
                capabilities_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                (patch["name"] or provider_id).strip(),
                (patch["base_url"] or "").strip(),
                patch["auth_type"] or "bearer",
                api_key_encrypted,
                1 if patch["enabled"] else 0,
                json_dump(patch["capabilities"] or []),
                json_dump(patch["meta"] or {}),
                now,
                provider_id,
            ),
        )
        conn.commit()
    sync_provider_files()
    return get_provider(provider_id)


@app.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> Dict[str, Any]:
    provider = get_provider(provider_id, include_secret=True, include_internal=False)
    if provider["id"] == NEWAPI_PROVIDER_ID:
        provider = resolve_runtime_provider(provider)
    return await test_provider_connection(provider)


@app.get("/provider-models")
def read_provider_models(provider_id: Optional[str] = None, capability: Optional[str] = None) -> Dict[str, Any]:
    return {"models": list_provider_models(provider_id=provider_id, capability=capability)}


@app.post("/provider-models")
def create_provider_model(body: ProviderModelCreate) -> Dict[str, Any]:
    provider = get_provider(body.provider_id, include_internal=False)
    model_name = body.model_name.strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="Model name is required")
    capability = normalize_job_type(body.capability, {})
    if capability not in PUBLIC_MODEL_CAPABILITIES:
        raise HTTPException(status_code=400, detail="Unsupported capability")
    model_id = canonical_provider_model_id(
        body.id
        or re.sub(r"[^A-Za-z0-9_.-]+", "-", model_name).strip(".-").lower()
        or f"model-{uuid.uuid4().hex[:10]}"
    ).strip()
    if provider["id"] != NEWAPI_PROVIDER_ID:
        raise HTTPException(status_code=400, detail="当前版本只允许添加到漫创AI中转站")
    if not re.fullmatch(r"^[A-Za-z0-9_.-]+$", model_id):
        raise HTTPException(status_code=400, detail="Invalid model id")
    adapter = body.adapter or default_adapter_for_capability(capability)
    if adapter not in SUPPORTED_PROVIDER_ADAPTERS:
        raise HTTPException(status_code=400, detail="Unsupported adapter")
    allowed_adapters = allowed_adapters_for_capability(capability)
    if allowed_adapters and adapter not in allowed_adapters:
        raise HTTPException(status_code=400, detail=f"{capability} must use one of {', '.join(sorted(allowed_adapters))}")
    now = utc_now()
    with connect() as conn:
        exists = conn.execute("SELECT id FROM provider_models WHERE id = ?", (model_id,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Provider model already exists")
        conn.execute(
            """
            INSERT INTO provider_models(
              id, provider_id, capability, model_name, display_name, adapter, enabled,
              params_json, meta_json, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                provider["id"],
                capability,
                model_name,
                (body.display_name or model_name).strip(),
                adapter,
                1 if body.enabled else 0,
                json_dump(body.params or {}),
                json_dump(body.meta or {}),
                now,
                now,
            ),
        )
        conn.commit()
    sync_provider_files()
    return get_provider_model(model_id)


@app.put("/provider-models/{model_id}")
def update_provider_model(model_id: str, body: ProviderModelUpdate) -> Dict[str, Any]:
    model_id = canonical_provider_model_id(model_id)
    current = get_provider_model(model_id, include_internal=False)
    capability = normalize_job_type(body.capability, {}) if body.capability is not None else current["capability"]
    model_name = body.model_name if body.model_name is not None else current["modelName"]
    display_name = body.display_name if body.display_name is not None else current["displayName"]
    adapter = body.adapter if body.adapter is not None else (current["adapter"] or default_adapter_for_capability(capability))
    enabled = body.enabled if body.enabled is not None else current["enabled"]
    params = body.params if body.params is not None else current["params"]
    meta = body.meta if body.meta is not None else current["meta"]
    if not model_name.strip():
        raise HTTPException(status_code=400, detail="Model name is required")
    if capability not in PUBLIC_MODEL_CAPABILITIES:
        raise HTTPException(status_code=400, detail="Unsupported capability")
    if adapter not in SUPPORTED_PROVIDER_ADAPTERS:
        raise HTTPException(status_code=400, detail="Unsupported adapter")
    allowed_adapters = allowed_adapters_for_capability(capability)
    if allowed_adapters and adapter not in allowed_adapters:
        raise HTTPException(status_code=400, detail=f"{capability} must use one of {', '.join(sorted(allowed_adapters))}")
    if model_id == "gpt-5.5":
        capability = "text.generate"
        adapter = "openai.responses"
    elif model_id == "gpt-image-2":
        capability = "image.generate"
        adapter = "openai.image"
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE provider_models
            SET capability = ?, model_name = ?, display_name = ?, adapter = ?, enabled = ?,
                params_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                capability,
                model_name.strip(),
                display_name.strip() or model_name.strip(),
                adapter,
                1 if enabled else 0,
                json_dump(params or {}),
                json_dump(meta or {}),
                now,
                model_id,
            ),
        )
        conn.commit()
    sync_provider_files()
    return get_provider_model(model_id)


@app.delete("/provider-models/{model_id}")
def delete_provider_model(model_id: str) -> Dict[str, Any]:
    model_id = canonical_provider_model_id(model_id)
    get_provider_model(model_id, include_internal=False)
    if model_id in DEFAULT_PUBLIC_PROVIDER_MODEL_IDS:
        raise HTTPException(status_code=400, detail="内置模型不能删除")
    with connect() as conn:
        conn.execute("DELETE FROM provider_models WHERE id = ?", (model_id,))
        conn.commit()
    sync_provider_files()
    return {"ok": True, "id": model_id}


@app.get("/prompts")
def read_prompts(scope: Optional[str] = None, project_id: Optional[str] = None, category: Optional[str] = None) -> Dict[str, Any]:
    return {"prompts": list_prompts(scope=scope, project_id=project_id, category=category)}


@app.post("/prompts")
def create_prompt(body: PromptCreate) -> Dict[str, Any]:
    scope = body.scope if body.scope in {"global", "project"} else "global"
    project_id = validate_project_id(body.project_id) if body.project_id else None
    if scope == "project" and not project_id:
        raise HTTPException(status_code=400, detail="Project prompt requires project_id")
    if project_id:
        ensure_project(project_id)
    title = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="Prompt title and content are required")
    prompt_id = f"prompt_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prompts(id, project_id, scope, category, title, content, tags_json, meta_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prompt_id,
                project_id,
                scope,
                body.category or "general",
                title,
                content,
                json_dump(body.tags or []),
                json_dump(body.meta or {}),
                now,
                now,
            ),
        )
        if project_id:
            conn.execute(
                "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"hist_{uuid.uuid4().hex[:12]}", project_id, "prompt.create", prompt_id, json_dump({"title": title, "category": body.category}), now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    record = row_prompt(row)
    sync_prompt_files_for_record(record)
    return record


@app.put("/prompts/{prompt_id}")
def update_prompt(prompt_id: str, body: PromptUpdate) -> Dict[str, Any]:
    with connect() as conn:
        current = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Prompt not found")
        current_record = row_prompt(current)
        scope = body.scope if body.scope in {"global", "project"} else current_record["scope"]
        project_id = body.project_id if body.project_id is not None else current_record["projectId"]
        project_id = validate_project_id(project_id) if project_id else None
        if scope == "project" and not project_id:
            raise HTTPException(status_code=400, detail="Project prompt requires project_id")
        title = (body.title if body.title is not None else current_record["title"]).strip()
        content = (body.content if body.content is not None else current_record["content"]).strip()
        if not title or not content:
            raise HTTPException(status_code=400, detail="Prompt title and content are required")

    if project_id:
        ensure_project(project_id)

    with connect() as conn:
        now = utc_now()
        conn.execute(
            """
            UPDATE prompts
            SET project_id = ?, scope = ?, category = ?, title = ?, content = ?,
                tags_json = ?, meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                project_id,
                scope,
                body.category or current_record["category"] or "general",
                title,
                content,
                json_dump(body.tags if body.tags is not None else current_record["tags"]),
                json_dump(body.meta if body.meta is not None else current_record["meta"]),
                now,
                prompt_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    record = row_prompt(row)
    sync_prompt_files_for_record(record)
    return record


@app.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Prompt not found")
        record = row_prompt(row)
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        conn.commit()
    sync_prompt_files_for_record(record)
    return {"ok": True, "id": prompt_id}


@app.post("/projects")
def create_project(body: ProjectCreate) -> Dict[str, Any]:
    project_id = body.id or f"project_{uuid.uuid4().hex[:12]}"
    if project_id == GLOBAL_ASSET_PROJECT_ID:
        raise HTTPException(status_code=400, detail="Reserved project id")
    ensure_project(project_id, body.name)
    payload = get_project_payload(project_id)
    sync_project_files(project_id, payload)
    return payload


@app.get("/projects")
def list_projects() -> Dict[str, Any]:
    return {"projects": list_project_summaries()}


@app.get("/projects/{project_id}")
def open_project(project_id: str) -> Dict[str, Any]:
    return get_project_payload(project_id)


@app.delete("/projects/{project_id}")
def delete_project(project_id: str) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    if project_id in {"local-default", GLOBAL_ASSET_PROJECT_ID}:
        raise HTTPException(status_code=400, detail="Default project cannot be deleted")
    with connect() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()

    root = project_dir(project_id).resolve()
    projects_root = PROJECTS_DIR.resolve()
    try:
        root.relative_to(projects_root)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid project path") from error
    if root.exists():
        shutil.rmtree(root)
    return {"ok": True, "projectId": project_id}


@app.put("/projects/{project_id}/graph")
def save_graph(project_id: str, body: GraphSave) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    project_name = (body.project or {}).get("name") or "未命名"
    ensure_project(project_id, project_name, sync_files=False)
    now = utc_now()
    snapshot_nodes: List[Dict[str, Any]] = []
    snapshot_edges: List[Dict[str, Any]] = []
    snapshot_assets: List[Dict[str, Any]] = []
    snapshot_history: List[Dict[str, Any]] = []
    with connect() as conn:
        project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not body.allowEmptyOverwrite and not (body.nodes or []) and not (body.edges or []):
            existing_node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            existing_edge_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            if existing_node_count > 0 or existing_edge_count > 0:
                return {
                    "ok": True,
                    "projectId": project_id,
                    "updatedAt": project_row["updated_at"] if project_row else now,
                    "skipped": "empty_canvas_overwrite_guard",
                }
        metadata = project_metadata_from_row(project_row)
        body_fields_set = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
        if "designSpacePackage" in body_fields_set:
            if body.designSpacePackage is None:
                metadata.pop("designSpacePackage", None)
            else:
                metadata["designSpacePackage"] = body.designSpacePackage
        conn.execute(
            "UPDATE projects SET name = ?, updated_at = ?, version = ?, metadata_json = ? WHERE id = ?",
            (project_name, now, PROJECT_STORAGE_VERSION, json_dump(metadata), project_id),
        )
        conn.execute("DELETE FROM nodes WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM edges WHERE project_id = ?", (project_id,))
        existing_asset_rows = conn.execute("SELECT id, meta_json FROM assets WHERE project_id = ?", (project_id,)).fetchall()
        for row in existing_asset_rows:
            if is_asset_library_record(json_load(row["meta_json"], {})):
                conn.execute("DELETE FROM assets WHERE id = ?", (row["id"],))
        conn.execute("DELETE FROM history WHERE project_id = ?", (project_id,))
        for node in body.nodes:
            node_id = node.get("id") or f"node_{uuid.uuid4().hex[:8]}"
            node_record = {**node, "id": node_id}
            snapshot_nodes.append(node_record)
            conn.execute(
                """
                INSERT INTO nodes(id, project_id, type, title, x, y, w, h, params_json, state_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    project_id,
                    node.get("type") or "unknown",
                    node.get("title") or "",
                    float(node.get("x") or 0),
                    float(node.get("y") or 0),
                    float(node.get("w") or 320),
                    float(node.get("h") or 220),
                    json_dump(node.get("params") or {}),
                    json_dump(node_record),
                ),
            )
        for edge in body.edges:
            edge_id = edge.get("id") or f"edge_{uuid.uuid4().hex[:8]}"
            edge_record = {**edge, "id": edge_id}
            snapshot_edges.append(edge_record)
            conn.execute(
                "INSERT INTO edges(id, project_id, from_node_id, to_node_id) VALUES(?, ?, ?, ?)",
                (
                    edge_id,
                    project_id,
                    edge.get("from"),
                    edge.get("to"),
                ),
            )
        for raw_asset in body.assets:
            asset = ensure_graph_asset_library_meta(raw_asset)
            asset_id = asset.get("id") or f"asset_{uuid.uuid4().hex[:12]}"
            asset_record = {**asset, "id": asset_id, "projectId": project_id}
            snapshot_assets.append(asset_record)
            conn.execute(
                """
                INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET meta_json = excluded.meta_json
                """,
                (
                    asset_id,
                    project_id,
                    asset.get("kind") or "file",
                    asset.get("path") or asset.get("src") or "",
                    asset.get("thumbPath"),
                    asset.get("mime"),
                    int(asset.get("size") or 0),
                    json_dump(asset_record),
                    asset.get("createdAt") or now,
                ),
            )
        for item in body.history:
            history_id = item.get("id") or f"hist_{uuid.uuid4().hex[:12]}"
            history_record = {**item, "id": history_id, "projectId": project_id, "project_id": project_id}
            snapshot_history.append(history_record)
            conn.execute(
                """
                INSERT INTO history(id, project_id, action, target_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  action = excluded.action,
                  target_id = excluded.target_id,
                  payload_json = excluded.payload_json
                """,
                (
                    history_id,
                    project_id,
                    item.get("action") or "asset.record",
                    item.get("targetId") or item.get("assetId") or item.get("nodeId"),
                    json_dump(history_record),
                    item.get("createdAt") or item.get("time") or now,
                ),
            )
        conn.commit()
    project_snapshot = {
        **(body.project or {}),
        "id": project_id,
        "name": project_name,
        "createdAt": (body.project or {}).get("createdAt") or (project_row["created_at"] if project_row else now),
        "updatedAt": now,
        "version": PROJECT_STORAGE_VERSION,
        "path": str(project_dir(project_id)),
    }
    sync_saved_graph_files(project_id, {
        "project": project_snapshot,
        "nodes": snapshot_nodes,
        "edges": snapshot_edges,
        "assets": snapshot_assets,
        "history": snapshot_history,
        "designSpacePackage": metadata.get("designSpacePackage"),
        "updatedAt": now,
    })
    return {"ok": True, "projectId": project_id, "updatedAt": now}


def clean_patch_id(value: Any, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return clean


def sync_patched_graph_file(
    project_id: str,
    project: Dict[str, Any],
    *,
    nodes_upsert: List[Dict[str, Any]],
    node_ids_delete: List[str],
    edges_upsert: List[Dict[str, Any]],
    edge_ids_delete: List[str],
    updated_at: str,
) -> None:
    ensure_project_dirs(project_id)
    graph_path = project_dir(project_id) / "graph.json"
    if graph_path.exists():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            graph = {}
    else:
        graph = {}
    if not isinstance(graph, dict):
        graph = {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    deleted_nodes = set(node_ids_delete or [])
    deleted_edges = set(edge_ids_delete or [])
    upsert_nodes_by_id = {node["id"]: node for node in nodes_upsert if node.get("id")}
    upsert_edges_by_id = {edge["id"]: edge for edge in edges_upsert if edge.get("id")}
    next_nodes = []
    seen_nodes = set()
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not node_id or node_id in deleted_nodes:
            continue
        if node_id in upsert_nodes_by_id:
            next_nodes.append(upsert_nodes_by_id[node_id])
            seen_nodes.add(node_id)
        else:
            next_nodes.append(node)
    for node in nodes_upsert:
        node_id = node.get("id")
        if node_id and node_id not in seen_nodes and node_id not in deleted_nodes:
            next_nodes.append(node)
    next_edges = []
    seen_edges = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        edge_id = edge.get("id")
        if not edge_id or edge_id in deleted_edges or edge.get("from") in deleted_nodes or edge.get("to") in deleted_nodes:
            continue
        if edge_id in upsert_edges_by_id:
            next_edges.append(upsert_edges_by_id[edge_id])
            seen_edges.add(edge_id)
        else:
            next_edges.append(edge)
    for edge in edges_upsert:
        edge_id = edge.get("id")
        if (
            edge_id
            and edge_id not in seen_edges
            and edge_id not in deleted_edges
            and edge.get("from") not in deleted_nodes
            and edge.get("to") not in deleted_nodes
        ):
            next_edges.append(edge)
    write_json_file(graph_path, {
        "version": PROJECT_STORAGE_VERSION,
        "project": project,
        "nodes": next_nodes,
        "edges": next_edges,
        "assets": graph.get("assets") if isinstance(graph.get("assets"), list) else [],
        "history": graph.get("history") if isinstance(graph.get("history"), list) else [],
        "designSpacePackage": graph.get("designSpacePackage"),
        "updatedAt": updated_at,
    })
    project_manifest = {
        **project,
        "storageVersion": PROJECT_STORAGE_VERSION,
        "folders": {
            "assets": "assets",
            "thumbs": "thumbs",
            "exports": "exports",
            "cache": "cache",
        },
    }
    write_json_file(project_dir(project_id) / "project.json", project_manifest)


@app.patch("/projects/{project_id}/graph")
def patch_project_graph(project_id: str, body: GraphPatch) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    ensure_project(project_id, sync_files=False)
    now = utc_now()
    nodes_upsert: List[Dict[str, Any]] = []
    edges_upsert: List[Dict[str, Any]] = []
    node_ids_delete = [clean_patch_id(item, "node id") for item in (body.nodeIdsDelete or [])]
    edge_ids_delete = [clean_patch_id(item, "edge id") for item in (body.edgeIdsDelete or [])]
    for raw_node in body.nodesUpsert or []:
        if not isinstance(raw_node, dict):
            raise HTTPException(status_code=400, detail="Node patch item must be an object")
        node_id = clean_patch_id(raw_node.get("id"), "node id")
        nodes_upsert.append({**raw_node, "id": node_id})
    for raw_edge in body.edgesUpsert or []:
        if not isinstance(raw_edge, dict):
            raise HTTPException(status_code=400, detail="Edge patch item must be an object")
        edge_id = clean_patch_id(raw_edge.get("id"), "edge id")
        from_node_id = clean_patch_id(raw_edge.get("from"), "edge from")
        to_node_id = clean_patch_id(raw_edge.get("to"), "edge to")
        edges_upsert.append({"id": edge_id, "from": from_node_id, "to": to_node_id})

    with connect() as conn:
        conn.execute(
            "UPDATE projects SET updated_at = ?, version = ? WHERE id = ?",
            (now, PROJECT_STORAGE_VERSION, project_id),
        )
        for node_id in node_ids_delete:
            conn.execute(
                "DELETE FROM edges WHERE project_id = ? AND (from_node_id = ? OR to_node_id = ?)",
                (project_id, node_id, node_id),
            )
            conn.execute("DELETE FROM nodes WHERE project_id = ? AND id = ?", (project_id, node_id))
        for edge_id in edge_ids_delete:
            conn.execute("DELETE FROM edges WHERE project_id = ? AND id = ?", (project_id, edge_id))
        for node in nodes_upsert:
            conn.execute(
                """
                INSERT INTO nodes(id, project_id, type, title, x, y, w, h, params_json, state_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  project_id = excluded.project_id,
                  type = excluded.type,
                  title = excluded.title,
                  x = excluded.x,
                  y = excluded.y,
                  w = excluded.w,
                  h = excluded.h,
                  params_json = excluded.params_json,
                  state_json = excluded.state_json
                """,
                (
                    node["id"],
                    project_id,
                    node.get("type") or "unknown",
                    node.get("title") or "",
                    float(node.get("x") or 0),
                    float(node.get("y") or 0),
                    float(node.get("w") or 320),
                    float(node.get("h") or 220),
                    json_dump(node.get("params") or {}),
                    json_dump(node),
                ),
            )
        for edge in edges_upsert:
            conn.execute(
                """
                INSERT INTO edges(id, project_id, from_node_id, to_node_id)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  project_id = excluded.project_id,
                  from_node_id = excluded.from_node_id,
                  to_node_id = excluded.to_node_id
                """,
                (edge["id"], project_id, edge["from"], edge["to"]),
            )
        conn.commit()
        project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    project_snapshot = row_project(project_row)
    sync_patched_graph_file(
        project_id,
        project_snapshot,
        nodes_upsert=nodes_upsert,
        node_ids_delete=node_ids_delete,
        edges_upsert=edges_upsert,
        edge_ids_delete=edge_ids_delete,
        updated_at=now,
    )
    return {
        "ok": True,
        "projectId": project_id,
        "updatedAt": now,
        "nodesUpserted": len(nodes_upsert),
        "nodesDeleted": len(node_ids_delete),
        "edgesUpserted": len(edges_upsert),
        "edgesDeleted": len(edge_ids_delete),
    }


@app.put("/projects/{project_id}/library")
def save_project_library(project_id: str, body: LibrarySave) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    ensure_project(project_id, sync_files=False)
    now = utc_now()
    snapshot_assets: List[Dict[str, Any]] = []
    snapshot_history: List[Dict[str, Any]] = []
    with connect() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        existing_asset_rows = conn.execute("SELECT id, meta_json FROM assets WHERE project_id = ?", (project_id,)).fetchall()
        for row in existing_asset_rows:
            if is_asset_library_record(json_load(row["meta_json"], {})):
                conn.execute("DELETE FROM assets WHERE id = ?", (row["id"],))
        for raw_asset in body.assets:
            asset = ensure_graph_asset_library_meta(raw_asset)
            asset_id = asset.get("id") or f"asset_{uuid.uuid4().hex[:12]}"
            asset_record = {**asset, "id": asset_id, "projectId": project_id, "project_id": project_id}
            snapshot_assets.append(asset_record)
            conn.execute(
                """
                INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  meta_json = excluded.meta_json
                """,
                (
                    asset_id,
                    project_id,
                    asset_record.get("kind") or "file",
                    asset_record.get("path") or asset_record.get("src") or "",
                    asset_record.get("thumbPath"),
                    asset_record.get("mime"),
                    int(asset_record.get("size") or 0),
                    json_dump(asset_record),
                    asset_record.get("createdAt") or now,
                ),
            )
        conn.execute("DELETE FROM history WHERE project_id = ?", (project_id,))
        for item in body.history:
            history_id = item.get("id") or f"hist_{uuid.uuid4().hex[:12]}"
            history_record = {**item, "id": history_id, "projectId": project_id, "project_id": project_id}
            snapshot_history.append(history_record)
            conn.execute(
                """
                INSERT INTO history(id, project_id, action, target_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  action = excluded.action,
                  target_id = excluded.target_id,
                  payload_json = excluded.payload_json
                """,
                (
                    history_id,
                    project_id,
                    history_record.get("action") or "asset.record",
                    history_record.get("targetId") or history_record.get("assetId") or history_record.get("nodeId"),
                    json_dump(history_record),
                    history_record.get("createdAt") or history_record.get("time") or now,
                ),
            )
        conn.commit()
    sync_project_library_files(project_id, snapshot_assets, snapshot_history)
    return {"ok": True, "projectId": project_id, "updatedAt": now}


@app.put("/projects/{project_id}/nodes/{node_id}")
def save_project_node(project_id: str, node_id: str, body: NodeSave) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    clean_node_id = str(node_id or "").strip()
    if not clean_node_id:
        raise HTTPException(status_code=400, detail="Node id is required")
    ensure_project(project_id, sync_files=False)
    now = utc_now()
    node = {**(body.node or {}), "id": clean_node_id}
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET updated_at = ?, version = ? WHERE id = ?",
            (now, PROJECT_STORAGE_VERSION, project_id),
        )
        conn.execute(
            """
            INSERT INTO nodes(id, project_id, type, title, x, y, w, h, params_json, state_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              project_id = excluded.project_id,
              type = excluded.type,
              title = excluded.title,
              x = excluded.x,
              y = excluded.y,
              w = excluded.w,
              h = excluded.h,
              params_json = excluded.params_json,
              state_json = excluded.state_json
            """,
            (
                clean_node_id,
                project_id,
                node.get("type") or "unknown",
                node.get("title") or "",
                float(node.get("x") or 0),
                float(node.get("y") or 0),
                float(node.get("w") or 320),
                float(node.get("h") or 220),
                json_dump(node.get("params") or {}),
                json_dump(node),
            ),
        )
        conn.commit()
    return {"ok": True, "projectId": project_id, "nodeId": clean_node_id, "node": node, "updatedAt": now}


@app.post("/history")
def create_history(body: HistoryCreate) -> Dict[str, Any]:
    project_id = validate_project_id(body.project_id)
    ensure_project(project_id)
    now = utc_now()
    history_id = body.payload.get("id") or f"hist_{uuid.uuid4().hex[:12]}"
    payload = {
        **body.payload,
        "id": history_id,
        "projectId": project_id,
        "project_id": project_id,
        "action": body.action,
        "targetId": body.target_id or body.payload.get("targetId") or body.payload.get("assetId") or body.payload.get("jobId") or body.payload.get("nodeId"),
        "createdAt": body.payload.get("createdAt") or now,
        "updatedAt": body.payload.get("updatedAt") or now,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO history(id, project_id, action, target_id, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              action = excluded.action,
              target_id = excluded.target_id,
              payload_json = excluded.payload_json
            """,
            (
                history_id,
                project_id,
                body.action,
                payload["targetId"],
                json_dump(payload),
                payload["createdAt"],
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
    sync_project_files(project_id)
    return row_history(row)


@app.get("/history/project/{project_id}")
def list_project_history(
    project_id: str,
    type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    conditions = ["project_id = ?"]
    params: List[Any] = [project_id]
    query_limit = max(1, min(int(limit or 200), 500))
    query_offset = max(0, int(offset or 0))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM history WHERE {' AND '.join(conditions)} ORDER BY created_at DESC",
            params,
        ).fetchall()
    items = [row_history(row) for row in rows]
    if type:
        wanted_type = str(type).strip().lower()
        items = [item for item in items if history_media_kind(item) == wanted_type]
    if status:
        wanted_status = str(status).strip().lower()
        items = [item for item in items if history_status(item) == wanted_status]
    total = len(items)
    return {"total": total, "items": items[query_offset:query_offset + query_limit]}


@app.get("/history/{history_id}")
def get_history(history_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="History record not found")
    return row_history(row)


@app.put("/history/{history_id}")
def update_history(history_id: str, body: HistoryUpdate) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="History record not found")
        payload = row_history(row)
        if body.payload is not None:
            payload.update(body.payload)
        action = body.action or payload.get("action") or row["action"]
        target_id = body.target_id or payload.get("targetId") or row["target_id"]
        payload.update({
            "id": history_id,
            "projectId": row["project_id"],
            "project_id": row["project_id"],
            "action": action,
            "targetId": target_id,
            "updatedAt": utc_now(),
        })
        conn.execute(
            "UPDATE history SET action = ?, target_id = ?, payload_json = ? WHERE id = ?",
            (action, target_id, json_dump(payload), history_id),
        )
        conn.commit()
        next_row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
    sync_project_files(payload["projectId"])
    return row_history(next_row)


@app.delete("/history/{history_id}")
def delete_history(history_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="History record not found")
        project_id = row["project_id"]
        conn.execute("DELETE FROM history WHERE id = ?", (history_id,))
        conn.commit()
    sync_project_files(project_id)
    return {"ok": True, "id": history_id}


@app.delete("/history/project/{project_id}")
def clear_project_history(project_id: str, type: Optional[str] = None) -> Dict[str, Any]:
    project_id = validate_project_id(project_id)
    with connect() as conn:
        if type:
            rows = conn.execute("SELECT * FROM history WHERE project_id = ?", (project_id,)).fetchall()
            wanted_type = str(type).strip().lower()
            target_ids = [
                row["id"]
                for row in rows
                if history_media_kind(row_history(row)) == wanted_type
            ]
            if target_ids:
                placeholders = ",".join("?" for _ in target_ids)
                conn.execute(f"DELETE FROM history WHERE id IN ({placeholders})", target_ids)
            deleted_count = len(target_ids)
        else:
            deleted_count = conn.execute("SELECT COUNT(*) AS count FROM history WHERE project_id = ?", (project_id,)).fetchone()["count"]
            conn.execute("DELETE FROM history WHERE project_id = ?", (project_id,))
        conn.commit()
    sync_project_files(project_id)
    return {"ok": True, "deletedCount": deleted_count}


@app.post("/assets/import")
def import_asset(body: AssetImport) -> Dict[str, Any]:
    project_id = validate_project_id(body.project_id)
    ensure_asset_scope(project_id, sync_files=False)
    source = Path(body.file_path)
    if not source.exists() or not source.is_file():
        raise HTTPException(status_code=400, detail="File not found")
    mime, _ = mimetypes.guess_type(str(source))
    kind = body.kind or guess_kind(source, mime)
    validate_importable_asset(source, mime, kind)
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    target = asset_scope_dir(project_id) / "assets" / f"{asset_id}{source.suffix.lower()}"
    copy_requested = bool(body.copy_file)
    copy_pending = bool(copy_requested and body.defer_copy)
    if copy_requested and not copy_pending:
        shutil.copy2(source, target)
        stored_path = target
        size = target.stat().st_size
    else:
        stored_path = source
        size = source.stat().st_size
    meta = dict(body.meta or {})
    for key in (
        "id", "projectId", "kind", "path", "thumbPath", "mime", "size", "assetUrl", "createdAt",
        "storageMode", "originalPath", "managedPath", "copyPending", "copyError",
    ):
        meta.pop(key, None)
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": kind,
        "path": str(stored_path),
        "thumbPath": None,
        "mime": mime,
        "size": size,
        "title": source.stem,
        "source": "global.import" if is_global_asset_project(project_id) else "import",
        "scope": "global" if is_global_asset_project(project_id) else "project",
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": utc_now(),
    }
    if stored_path == source:
        record.update({
            "storageMode": "linked",
            "originalPath": str(source),
            "copyPending": copy_pending,
        })
        if copy_pending:
            record["managedPath"] = str(target)
    record.update(meta)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, kind, str(stored_path), None, mime, size, json_dump(record), record["createdAt"]),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.import", asset_id, json_dump(record), record["createdAt"]),
        )
        conn.commit()
    sync_asset_record_files(project_id, record)
    if copy_pending:
        schedule_deferred_asset_copy(asset_id, project_id, source, target)
    return record


@app.post("/assets/write")
def write_asset(body: AssetWrite) -> Dict[str, Any]:
    project_id = validate_project_id(body.project_id)
    ensure_asset_scope(project_id, sync_files=False)
    raw, data_mime = decode_data_url(body.data_url)
    mime = body.mime or data_mime or mimetypes.guess_type(body.filename)[0] or "application/octet-stream"
    kind = body.kind or guess_kind(Path(body.filename), mime)
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    suffix = safe_asset_suffix(body.filename, mime)
    target = asset_scope_dir(project_id) / "assets" / f"{asset_id}{suffix}"
    target.write_bytes(raw)
    created_at = utc_now()
    meta = dict(body.meta or {})
    for key in ("id", "projectId", "kind", "path", "thumbPath", "mime", "size", "assetUrl", "createdAt"):
        meta.pop(key, None)
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": kind,
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": len(raw),
        "title": Path(body.filename).stem or asset_id,
        "source": "global.write" if is_global_asset_project(project_id) else "write",
        "scope": "global" if is_global_asset_project(project_id) else "project",
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": created_at,
    }
    record.update(meta)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, kind, str(target), None, mime, len(raw), json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.write", asset_id, json_dump(record), created_at),
        )
        conn.commit()
    sync_asset_record_files(project_id, record)
    return record


@app.get("/assets")
def list_assets(project_id: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    query = "SELECT * FROM assets WHERE 1 = 1"
    params: List[Any] = []
    if project_id:
        validate_project_id(project_id)
        query += " AND project_id = ?"
        params.append(project_id)
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"assets": [asset_record_from_row(row) for row in rows]}


def delete_asset_record(asset_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Asset not found")
        project_id = row["project_id"]
        record = asset_record_from_row(row)
        paths = [row["path"], row["thumb_path"]]
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    for raw_path in paths:
        if not raw_path:
            continue
        try:
            path = Path(raw_path)
            if path.exists() and path.is_file() and is_managed_asset_storage_path(project_id, path):
                path.unlink()
        except OSError:
            pass
    sync_asset_record_files(project_id, record)
    return {"ok": True, "id": asset_id}


@app.delete("/assets/{asset_id}")
def delete_asset(asset_id: str) -> Dict[str, Any]:
    return delete_asset_record(asset_id)


@app.post("/assets/{asset_id}/delete")
def delete_asset_fallback(asset_id: str) -> Dict[str, Any]:
    return delete_asset_record(asset_id)


@app.post("/assets/{asset_id}/promote")
def promote_asset_to_global(asset_id: str, body: Optional[AssetPromote] = None) -> Dict[str, Any]:
    body = body or AssetPromote()
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    if row["project_id"] == GLOBAL_ASSET_PROJECT_ID:
        return asset_record_from_row(row)

    source = Path(row["path"])
    if not source.exists() or not source.is_file():
        raise HTTPException(status_code=404, detail="Asset file missing")

    ensure_global_asset_project()
    old_meta = json_load(row["meta_json"], {})
    mime = row["mime"] or mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    kind = row["kind"] or guess_kind(source, mime)
    promoted_id = f"asset_{uuid.uuid4().hex[:12]}"
    suffix = safe_asset_suffix(source.name, mime)
    target = asset_scope_dir(GLOBAL_ASSET_PROJECT_ID) / "assets" / f"{promoted_id}{suffix}"
    shutil.copy2(source, target)
    created_at = utc_now()
    inherited_tags = old_meta.get("tags") if isinstance(old_meta.get("tags"), list) else []
    tags = list(dict.fromkeys([*inherited_tags, *(body.tags or []), "全局资产"]))
    meta = dict(body.meta or {})
    for key in ("id", "projectId", "kind", "path", "thumbPath", "mime", "size", "assetUrl", "createdAt"):
        meta.pop(key, None)
    record = {
        **old_meta,
        **meta,
        "id": promoted_id,
        "projectId": GLOBAL_ASSET_PROJECT_ID,
        "kind": kind,
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": target.stat().st_size,
        "title": (body.title or old_meta.get("title") or source.stem or promoted_id),
        "source": "global.promote",
        "scope": "global",
        "tags": tags,
        "sourceAssetId": row["id"],
        "sourceProjectId": row["project_id"],
        "assetUrl": f"/assets/{promoted_id}",
        "createdAt": created_at,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (promoted_id, GLOBAL_ASSET_PROJECT_ID, kind, str(target), None, mime, record["size"], json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", GLOBAL_ASSET_PROJECT_ID, "asset.promote", promoted_id, json_dump(record), created_at),
        )
        conn.commit()
    sync_global_asset_file()
    return record


def ensure_asset_thumbnail(row: sqlite3.Row) -> Optional[Path]:
    asset_id = row["id"]
    existing = Path(row["thumb_path"]) if row["thumb_path"] else None
    if existing and existing.exists():
        return existing

    source = Path(row["path"])
    if not source.exists():
        return None
    mime = row["mime"] or mimetypes.guess_type(str(source))[0] or ""
    if not str(mime).lower().startswith("image/"):
        return None

    ensure_project_dirs(row["project_id"])
    target = asset_scope_dir(row["project_id"]) / "thumbs" / f"{asset_id}.webp"
    if not target.exists():
        try:
            with safe_image_open(source) as image:
                image = ImageOps.exif_transpose(image)
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                image.thumbnail((320, 320), resample)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(target, "WEBP", quality=72, method=4)
        except (OSError, UnidentifiedImageError):
            return None

    meta = json_load(row["meta_json"], {})
    meta["thumbPath"] = str(target)
    meta["thumbUrl"] = f"/assets/{asset_id}/thumb"
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET thumb_path = ?, meta_json = ? WHERE id = ?",
            (str(target), json_dump(meta), asset_id),
        )
        conn.commit()
    if row["project_id"] == GLOBAL_ASSET_PROJECT_ID:
        sync_global_asset_file()
    return target


@app.get("/assets/{asset_id}/thumb")
def get_asset_thumb(asset_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    thumb = ensure_asset_thumbnail(row)
    if not thumb:
        raise HTTPException(status_code=404, detail="Asset thumbnail unavailable")
    return FileResponse(thumb, media_type="image/webp")


@app.get("/assets/{asset_id}")
def get_asset(asset_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset file missing")
    return FileResponse(path, media_type=row["mime"])


SAFE_REFERENCE_MEDIA_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@app.head("/media/references/{name}")
@app.get("/media/references/{name}")
def get_reference_media(name: str):
    """对外提供自托管的参考素材（图生视频等本地上传的参考图）。
    上游模型/中转站会直接 GET 这个地址拉取参考图，因此必须是公开可访问的。
    通过文件名白名单 + 目录约束防止路径穿越。"""
    clean = str(name or "").strip()
    if not clean or clean != os.path.basename(clean) or ".." in clean:
        raise HTTPException(status_code=404, detail="Reference media not found")
    if not SAFE_REFERENCE_MEDIA_NAME.match(clean):
        raise HTTPException(status_code=404, detail="Reference media not found")
    candidate = REFERENCE_MEDIA_DIR / clean
    if not path_within(candidate, REFERENCE_MEDIA_DIR) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Reference media not found")
    media_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
    return FileResponse(
        candidate,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


def asset_id_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    explicit = payload.get("assetId") or payload.get("asset_id") or payload.get("sourceAssetId")
    if explicit:
        return str(explicit)
    for key in ("assetUrl", "asset_url", "imageUrl", "image_url", "videoUrl", "video_url", "src", "url"):
        value = str(payload.get(key) or "")
        match = re.search(r"/assets/([A-Za-z0-9_-]+)", value)
        if match:
            return match.group(1)
    return None


def get_asset_record(asset_id: str) -> Optional[Dict[str, Any]]:
    if not asset_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        return None
    meta = json_load(row["meta_json"], {})
    kind = normalized_asset_kind(row["kind"], row["path"], row["mime"])
    return {
        **meta,
        "id": row["id"],
        "projectId": row["project_id"],
        "kind": kind,
        "path": row["path"],
        "thumbPath": row["thumb_path"],
        "mime": row["mime"],
        "size": row["size"],
        "assetUrl": meta.get("assetUrl") or f"/assets/{row['id']}",
        "createdAt": meta.get("createdAt") or row["created_at"],
    }


OUTPUT_ORIGINAL_MEDIA_URL_KEYS = (
    "originalUrl",
    "original_url",
    "downloadUrl",
    "download_url",
    "resultUrl",
    "result_url",
    "outputUrl",
    "output_url",
    "fileUrl",
    "file_url",
    "mediaUrl",
    "media_url",
    "assetUrl",
    "asset_url",
    "imageUrl",
    "image_url",
    "videoUrl",
    "video_url",
    "audioUrl",
    "audio_url",
)

OUTPUT_FALLBACK_MEDIA_URL_KEYS = (
    "url",
    "src",
    "previewUrl",
    "preview_url",
    "posterUrl",
    "poster_url",
    "thumbUrl",
    "thumb_url",
    "thumbnailUrl",
    "thumbnail_url",
    "thumb",
    "thumbnail",
)

OUTPUT_MEDIA_NESTED_KEYS = (
    "asset",
    "result",
    "output",
    "data",
    "items",
    "images",
    "videos",
    "audios",
    "files",
    "urls",
    "outputs",
    "mediaAssets",
    "media_assets",
)


def _first_output_media_url_for_keys(
    value: Any,
    keys: tuple[str, ...],
    *,
    allow_bare_strings: bool = False,
) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text if allow_bare_strings and text else None
    if isinstance(value, list):
        for item in value:
            found = _first_output_media_url_for_keys(item, keys, allow_bare_strings=allow_bare_strings)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    for nested_key in OUTPUT_MEDIA_NESTED_KEYS:
        if nested_key in value:
            found = _first_output_media_url_for_keys(
                value.get(nested_key),
                keys,
                allow_bare_strings=allow_bare_strings,
            )
            if found:
                return found
    return None


def first_output_media_url(output: Dict[str, Any]) -> Optional[str]:
    if not isinstance(output, dict):
        return None
    return (
        _first_output_media_url_for_keys(output, OUTPUT_ORIGINAL_MEDIA_URL_KEYS)
        or _first_output_media_url_for_keys(output, OUTPUT_FALLBACK_MEDIA_URL_KEYS, allow_bare_strings=True)
    )


def local_path_from_file_url(url: str) -> Optional[Path]:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path_text = url2pathname(unquote(parsed.path or ""))
    if re.match(r"^/[A-Za-z]:/", path_text):
        path_text = path_text[1:]
    candidate = Path(path_text)
    return candidate if candidate.exists() and candidate.is_file() else None


def is_provider_loopback_output_url(url: str) -> bool:
    return is_private_or_local_http_url(url)


def provider_output_url_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or parsed.hostname or "本机地址"


NEWAPI_MEDIA_CONTENT_PATH_RE = re.compile(
    r"^/(api/media-assets/\d+/content|v1/videos/[^/?#]+/content)$",
    re.I,
)


def newapi_media_accept_header(kind_hint: str) -> str:
    kind = str(kind_hint or "").strip().lower()
    if kind == "image":
        return "image/*,*/*"
    if kind == "audio":
        return "audio/*,*/*"
    return "video/mp4,*/*"


def newapi_authenticated_media_download(url: str, kind_hint: str = "") -> Optional[Dict[str, Any]]:
    text = str(url or "").strip()
    if not text:
        return None
    account = get_newapi_account(include_secret=True)
    api_key = normalize_newapi_api_key((account or {}).get("defaultApiKey") or "")
    if not account or not is_usable_newapi_api_key(api_key):
        return None

    base_url = normalize_newapi_base_url((account or {}).get("baseUrl") or DEFAULT_NEWAPI_BASE_URL).rstrip("/")
    base = urlparse(base_url)
    parsed = urlparse(text)
    remote_path = ""
    target_url = ""

    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.scheme.lower() != base.scheme.lower() or parsed.netloc.lower() != base.netloc.lower():
            return None
        base_path = (base.path or "").rstrip("/")
        path = parsed.path or ""
        if base_path:
            if path == base_path:
                remote_path = "/"
            elif path.startswith(f"{base_path}/"):
                remote_path = path[len(base_path):]
            else:
                return None
        else:
            remote_path = path
        target_url = text
    elif text.startswith("/"):
        remote_path = parsed.path or ""
        suffix = f"?{parsed.query}" if parsed.query else ""
        target_url = f"{base_url}{remote_path}{suffix}"
    else:
        return None

    if not NEWAPI_MEDIA_CONTENT_PATH_RE.match(remote_path):
        return None

    return {
        "url": target_url,
        "headers": {
            "Authorization": f"Bearer {api_key}",
            "Accept": newapi_media_accept_header(kind_hint),
        },
        "allowPrivate": is_private_or_local_http_url(target_url),
    }


def newapi_video_task_content_url(output: Dict[str, Any]) -> str:
    if not isinstance(output, dict):
        return ""
    if str(output.get("assetKind") or "").strip().lower() != "video":
        return ""
    if str(output.get("provider") or "").strip().lower() != NEWAPI_PROVIDER_ID:
        return ""
    task_id = str(
        output.get("remoteTaskId")
        or output.get("remote_task_id")
        or output.get("taskId")
        or output.get("task_id")
        or ""
    ).strip()
    if not task_id:
        return ""
    account = get_newapi_account(include_secret=True)
    api_key = normalize_newapi_api_key((account or {}).get("defaultApiKey") or "")
    if not account or not is_usable_newapi_api_key(api_key):
        return ""
    base_url = normalize_newapi_base_url((account or {}).get("baseUrl") or DEFAULT_NEWAPI_BASE_URL).rstrip("/")
    return f"{base_url}/v1/videos/{quote(task_id, safe='')}/content"


async def read_output_media_bytes(url: str, kind_hint: str = "") -> tuple[bytes, Optional[str], str]:
    if url.startswith("data:"):
        raw, mime = decode_data_url(url)
        return raw, mime, "generated-output"
    if url.startswith("file:"):
        raise ProviderAdapterError("生成结果 file URL 不允许作为远程供应商输出")
    newapi_download = newapi_authenticated_media_download(url, kind_hint)
    if newapi_download:
        try:
            response = await public_http_get(
                newapi_download["url"],
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                headers=newapi_download["headers"],
                trust_env=provider_http_trust_env(),
                allow_private=bool(newapi_download["allowPrivate"]),
            )
        except UnsafeRemoteUrlError as error:
            raise ProviderAdapterError(unsafe_remote_url_message("生成结果下载失败")) from error
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            host = provider_output_url_host(newapi_download["url"])
            raise ProviderAdapterError(f"生成结果下载失败：无法连接 {host}（{message}）") from error
        if not response.is_success:
            raise ProviderAdapterError(f"生成结果下载失败：HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0] or None
        parsed = urlparse(newapi_download["url"])
        filename = Path(unquote(parsed.path or "")).name or "generated-output"
        return response.content, content_type, filename
    if re.match(r"^https?://", url, re.I):
        try:
            response = await public_http_get(url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except UnsafeRemoteUrlError as error:
            raise ProviderAdapterError(unsafe_remote_url_message("生成结果下载失败")) from error
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            host = provider_output_url_host(url)
            raise ProviderAdapterError(f"生成结果下载失败：无法连接 {host}（{message}）") from error
        if not response.is_success:
            raise ProviderAdapterError(f"生成结果下载失败：HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0] or None
        parsed = urlparse(url)
        filename = Path(unquote(parsed.path or "")).name or "generated-output"
        return response.content, content_type, filename
    candidate = Path(url)
    if candidate.exists() and candidate.is_file():
        return candidate.read_bytes(), mimetypes.guess_type(str(candidate))[0], candidate.name
    raise ProviderAdapterError("生成结果不是可落盘的本地文件或远程 URL")


async def materialize_job_output_asset(project_id: str, output: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    if output.get("assetId") or output.get("assetPath"):
        return None
    kind_hint = str(output.get("assetKind") or "").strip().lower()
    if kind_hint not in {"image", "video", "audio"}:
        return None
    url = first_output_media_url(output)
    if not url or url.startswith("/assets/"):
        return None
    download_url = newapi_video_task_content_url(output) or url
    raw, mime, filename = await read_output_media_bytes(download_url, kind_hint)
    if not raw:
        raise ProviderAdapterError("生成结果为空，无法保存到本地")

    validate_project_id(project_id)
    ensure_project(project_id)
    mime = mime or mimetypes.guess_type(str(filename))[0] or "application/octet-stream"
    kind = normalized_asset_kind(output.get("assetKind"), filename, mime)
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    suffix = safe_asset_suffix(output.get("filename") or filename, mime)
    target = project_dir(project_id) / "assets" / f"{asset_id}{suffix}"
    target.write_bytes(raw)
    size = target.stat().st_size
    created_at = utc_now()
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": kind,
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": size,
        "title": output.get("title") or Path(filename).stem or asset_id,
        "source": "job.remote",
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": created_at,
        "jobId": job_id,
        "provider": output.get("provider"),
        "providerModelId": output.get("providerModelId"),
        "remoteSourceUrl": download_url,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, kind, str(target), None, record["mime"], size, json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.job-output", asset_id, json_dump(record), created_at),
        )
        conn.commit()
    return record


def register_job_output_asset(project_id: str, local_path: str, output: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    validate_project_id(project_id)
    ensure_project(project_id)
    source = Path(local_path)
    if not source.exists() or not source.is_file():
        raise ProviderAdapterError("生成输出文件不存在")
    mime = output.get("mime") or mimetypes.guess_type(str(source))[0] or "image/png"
    kind = normalized_asset_kind(output.get("assetKind"), source, mime)
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    suffix = safe_asset_suffix(output.get("filename") or source.name, mime)
    target = project_dir(project_id) / "assets" / f"{asset_id}{suffix}"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    size = target.stat().st_size
    created_at = utc_now()
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": kind,
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": size,
        "title": output.get("title") or source.stem,
        "source": output.get("source") or ("job.upscale" if output.get("engine") else "job.output"),
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": created_at,
        "jobId": job_id,
        "provider": output.get("provider"),
        "providerModelId": output.get("providerModelId"),
        "scale": output.get("scale"),
        "engine": output.get("engine"),
        "sourceAssetId": output.get("sourceAssetId"),
        "sourceNodeId": output.get("sourceNodeId"),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, kind, str(target), None, mime, size, json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.upscale", asset_id, json_dump(record), created_at),
        )
        conn.commit()
    return record


def register_trimmed_video_asset(
    project_id: str,
    local_path: str,
    output: Dict[str, Any],
    job_id: str,
    source_asset: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    validate_project_id(project_id)
    ensure_project(project_id)
    source = Path(local_path)
    if not source.exists() or not source.is_file():
        raise ProviderAdapterError("裁剪视频文件不存在")
    mime = output.get("mime") or mimetypes.guess_type(str(source))[0] or "video/mp4"
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    suffix = safe_asset_suffix(output.get("filename") or source.name, mime)
    target = project_dir(project_id) / "assets" / f"{asset_id}{suffix}"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    size = target.stat().st_size
    created_at = utc_now()
    source_title = (
        (source_asset or {}).get("title")
        or output.get("sourceTitle")
        or Path((source_asset or {}).get("path") or "").stem
        or "参考视频"
    )
    title = output.get("title") or f"{source_title} 裁剪片段"
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": "video",
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": size,
        "title": title,
        "source": "storyboard.video.trim",
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": created_at,
        "jobId": job_id,
        "sourceAssetId": (source_asset or {}).get("id") or output.get("sourceAssetId"),
        "sourceVideoAssetId": (source_asset or {}).get("id") or output.get("sourceAssetId"),
        "trimStartSec": output.get("trimStartSec"),
        "trimEndSec": output.get("trimEndSec"),
        "trimDurationSec": output.get("trimDurationSec") or output.get("durationSeconds"),
        "durationSeconds": output.get("durationSeconds"),
        "width": output.get("width"),
        "height": output.get("height"),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, "video", str(target), None, mime, size, json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.video-trim", asset_id, json_dump(record), created_at),
        )
        conn.commit()
    return record


def register_video_analysis_frame_asset(
    project_id: str,
    local_path: str,
    output: Dict[str, Any],
    job_id: str,
    frame: Dict[str, Any],
) -> Dict[str, Any]:
    validate_project_id(project_id)
    ensure_project(project_id)
    source = Path(local_path)
    if not source.exists() or not source.is_file():
        raise ProviderAdapterError("视频关键帧文件不存在")
    mime = mimetypes.guess_type(str(source))[0] or "image/jpeg"
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    target = project_dir(project_id) / "assets" / f"{asset_id}.jpg"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    size = target.stat().st_size
    created_at = utc_now()
    source_asset_id = output.get("sourceAssetId") or output.get("sourceVideoAssetId")
    title = f"视频关键帧 {int(frame.get('index') or 0) + 1}"
    if frame.get("timestampSec") is not None:
        title = f"{title} · {frame.get('timestampSec')}s"
    record = {
        "id": asset_id,
        "projectId": project_id,
        "kind": "image",
        "path": str(target),
        "thumbPath": None,
        "mime": mime,
        "size": size,
        "title": title,
        "source": "storyboard.video-analysis.frame",
        "scope": "project",
        "assetUrl": f"/assets/{asset_id}",
        "createdAt": created_at,
        "jobId": job_id,
        "sourceAssetId": source_asset_id,
        "sourceVideoAssetId": source_asset_id,
        "timestampSec": frame.get("timestampSec"),
        "sceneIndex": frame.get("sceneIndex"),
        "sceneId": frame.get("sceneId"),
        "frameIndex": frame.get("index"),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO assets(id, project_id, kind, path, thumb_path, mime, size, meta_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (asset_id, project_id, "image", str(target), None, mime, size, json_dump(record), created_at),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", project_id, "asset.video-frame", asset_id, json_dump(record), created_at),
        )
        conn.commit()
    return record


def resolve_video_analysis_source(payload: Dict[str, Any]) -> tuple[Path, Optional[Dict[str, Any]]]:
    asset_id = asset_id_from_payload(payload) or ""
    asset = get_asset_record(asset_id) if asset_id else None
    if asset and asset.get("path"):
        candidate = Path(asset["path"])
        if candidate.exists() and candidate.is_file():
            return candidate, asset

    for key in ("videoPath", "video_path", "filePath", "file_path", "path"):
        value = payload.get(key)
        if not value:
            continue
        candidate = Path(str(value))
        if candidate.exists() and candidate.is_file():
            return candidate, asset

    raise VideoAnalysisError("视频解析缺少可读取的本地视频资产")


def subtitle_removal_runtime_status() -> Dict[str, Any]:
    try:
        provider = get_provider(GHOSTCUT_PROVIDER_ID)
        model = get_provider_model(GHOSTCUT_SUBTITLE_MODEL_ID)
    except Exception as error:
        return {
            "available": False,
            "engine": "GhostCut API",
            "error": exception_message(error, "去字幕引擎不可用"),
        }
    return {
        "available": bool(provider.get("enabled") and model.get("enabled")),
        "engine": "GhostCut API",
        "provider": provider.get("id"),
        "model": model.get("id"),
        "baseUrl": provider.get("baseUrl"),
    }


def resolve_subtitle_removal_source(payload: Dict[str, Any]) -> tuple[Path, Optional[Dict[str, Any]]]:
    asset_id = asset_id_from_payload(payload) or ""
    asset = get_asset_record(asset_id) if asset_id else None
    if asset and asset.get("path"):
        candidate = Path(asset["path"])
        if candidate.exists() and candidate.is_file():
            return candidate, asset

    for key in ("videoPath", "video_path", "filePath", "file_path", "path", "videoUrl", "video_url", "src", "url"):
        value = payload.get(key)
        if not value:
            continue
        candidate = Path(str(value))
        if candidate.exists() and candidate.is_file():
            return candidate, asset

    raise SubtitleRemovalError("去字幕缺少可读取的本地视频资产")


def optional_positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return None
    return parsed if parsed > 0 else None


async def run_video_subtitle_remove_job(
    job_id: str,
    job: Dict[str, Any],
    payload: Dict[str, Any],
    progress,
) -> Dict[str, Any]:
    await progress(8, "subtitle-source")
    source_path, source_asset = resolve_subtitle_removal_source(payload)
    source_asset_id = (
        payload.get("assetId")
        or payload.get("asset_id")
        or payload.get("sourceAssetId")
        or (source_asset.get("id") if source_asset else "")
        or ""
    )
    source_node_id = (
        payload.get("sourceNodeId")
        or payload.get("source_node_id")
        or payload.get("nodeId")
        or job.get("nodeId")
        or ""
    )
    return await remove_subtitles_from_video(
        input_path=source_path,
        cache_dir=project_dir(job["projectId"]) / "cache" / "subtitle-removal" / job_id,
        region=payload.get("region") or payload.get("subtitleRegion") or {},
        source_asset_id=str(source_asset_id),
        source_node_id=str(source_node_id),
        title=str(payload.get("outputTitle") or payload.get("output_title") or payload.get("title") or (source_asset.get("title") if source_asset else "") or source_path.stem),
        inpaint_mode=str(payload.get("inpaintMode") or payload.get("inpaint_mode") or "sttn-auto"),
        expand_pixels=payload.get("expandPixels") or payload.get("expand_pixels") or 0,
        time_range=payload.get("timeRange") or payload.get("time_range"),
        preserve_audio=payload.get("preserveAudio") if "preserveAudio" in payload else payload.get("preserve_audio", True),
        video_width=optional_positive_int(payload.get("videoWidth") or payload.get("video_width")),
        video_height=optional_positive_int(payload.get("videoHeight") or payload.get("video_height")),
        progress=progress,
    )


async def run_storyboard_video_trim_job(
    job_id: str,
    job: Dict[str, Any],
    payload: Dict[str, Any],
    progress,
) -> None:
    source_path, source_asset = resolve_video_analysis_source(payload)
    source_asset_id = (
        payload.get("assetId")
        or payload.get("asset_id")
        or payload.get("sourceAssetId")
        or (source_asset.get("id") if source_asset else None)
    )
    await progress(8, "video-trim-source")
    trim_output = await trim_video_reference(
        source_path,
        project_dir(job["projectId"]) / "cache" / "video-trim" / job_id,
        {
            **payload,
            "sourceAssetId": source_asset_id,
            "sourceTitle": source_asset.get("title") if source_asset else source_path.stem,
        },
        progress=progress,
    )
    trim_output["sourceAssetId"] = source_asset_id
    asset = register_trimmed_video_asset(
        job["projectId"],
        trim_output.get("path") or "",
        trim_output,
        job_id,
        source_asset,
    )
    output = {
        "asset": asset,
        "assetId": asset["id"],
        "assetKind": "video",
        "assetUrl": asset["assetUrl"],
        "url": asset["assetUrl"],
        "urls": [asset["assetUrl"]],
        "title": asset["title"],
        "mime": asset["mime"],
        "trim": {
            "sourceAssetId": source_asset_id,
            "startSec": trim_output.get("trimStartSec"),
            "endSec": trim_output.get("trimEndSec"),
            "durationSec": trim_output.get("trimDurationSec"),
        },
        "trimmedVideo": {
            **trim_output,
            "assetId": asset["id"],
            "assetUrl": asset["assetUrl"],
            "url": asset["assetUrl"],
            "src": asset["assetUrl"],
            "title": asset["title"],
        },
        "stage": "completed",
        "provider": "local.ffmpeg",
        "providerModelId": "storyboard.video.trim",
        "providerModelName": "FFmpeg 视频裁剪",
    }
    await update_job(job_id, "completed", 100, output=output)


async def run_storyboard_video_analysis_job(
    job_id: str,
    job: Dict[str, Any],
    payload: Dict[str, Any],
    progress,
) -> None:
    source_path, source_asset = resolve_video_analysis_source(payload)
    source_asset_id = (
        payload.get("assetId")
        or payload.get("asset_id")
        or payload.get("sourceAssetId")
        or (source_asset.get("id") if source_asset else None)
    )
    source_video_url = (
        payload.get("videoUrl")
        or payload.get("video_url")
        or payload.get("url")
        or (source_asset.get("assetUrl") if source_asset else None)
    )
    await progress(8, "video-analysis-source")
    analysis = await analyze_video_reference(
        source_path,
        project_dir(job["projectId"]) / "cache" / "video-analysis" / job_id,
        {
            **payload,
            "sourceAssetId": source_asset_id,
            "sourceVideoUrl": source_video_url,
        },
        progress=progress,
    )
    analysis["sourceAssetId"] = source_asset_id
    analysis["sourceVideoAssetId"] = source_asset_id
    analysis["sourceVideoUrl"] = source_video_url
    analysis["sourceVideoTitle"] = source_asset.get("title") if source_asset else source_path.stem

    frame_records: List[Dict[str, Any]] = []
    next_frames: List[Dict[str, Any]] = []
    for frame in analysis.get("frames") or []:
        record = register_video_analysis_frame_asset(
            job["projectId"],
            frame.get("path") or "",
            analysis,
            job_id,
            frame,
        )
        frame_record = {
            **frame,
            "id": record["id"],
            "assetId": record["id"],
            "assetUrl": record["assetUrl"],
            "url": record["assetUrl"],
            "src": record["assetUrl"],
            "path": record["path"],
            "title": record["title"],
        }
        frame_records.append(record)
        next_frames.append(frame_record)

    analysis["frames"] = next_frames
    analysis["frameCandidates"] = next_frames
    for scene in analysis.get("scenes") or []:
        frame = next((item for item in next_frames if item.get("sceneIndex") == scene.get("index")), None)
        if frame:
            scene["frameAssetId"] = frame["assetId"]
            scene["frameUrl"] = frame["assetUrl"]

    output = {
        "videoReferenceAnalysis": analysis,
        "frames": next_frames,
        "assets": frame_records,
        "sourceAsset": source_asset,
        "stage": "completed",
    }
    await update_job(job_id, "completed", 100, output=output)


def get_job(job_id: str) -> Dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return row_job(row)


async def update_job(job_id: str, status: str, progress: int, output: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, progress = ?, output_json = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, progress, json_dump(output or {}), error, now, job_id),
        )
        conn.commit()
    job = get_job(job_id)
    try_sync_project_files(job["projectId"])
    await broadcast({"type": "job.updated", "job": job})


class JobCanceled(RuntimeError):
    pass


def payload_has_design_space_origin(payload: Dict[str, Any]) -> bool:
    if isinstance(payload.get("designSpace"), dict):
        return True
    if isinstance(payload.get("design_space"), dict):
        return True
    return bool(payload.get("designSpaceProjectId") or payload.get("_designSpace"))


def model_identity_values(model: Dict[str, Any]) -> List[str]:
    return [
        str(model.get("id") or ""),
        str(model.get("modelName") or ""),
        str(model.get("displayName") or ""),
        str(model.get("name") or ""),
    ]


def provider_job_queue_profile(
    job_type: str,
    payload: Dict[str, Any],
    provider: Dict[str, Any],
    model: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    provider_id = str(provider.get("id") or "").strip().lower()
    model_names = {value.strip().lower() for value in model_identity_values(model) if value.strip()}
    storyboard_tag = str((payload or {}).get("_storyboard") or "").strip()
    if job_type in {"text.generate", "text.reason", "inference.generate"} and storyboard_tag:
        limit = env_int("LIBAI_TEXT_INFERENCE_CONCURRENCY", DEFAULT_TEXT_INFERENCE_QUEUE_LIMIT)
        return {
            "key": f"storyboard:text-inference:{provider_id or 'provider'}",
            "limit": max(1, limit),
            "retryAttempts": 1,
            "retryDelaySeconds": 1,
        }
    if (
        job_type == "image.generate"
        and provider_id == "newapi"
        and "gpt-image-2" in model_names
        and payload_has_design_space_origin(payload)
    ):
        limit = env_int(
            "LIBAI_NEWAPI_GPT_IMAGE2_DESIGN_CONCURRENCY",
            DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_QUEUE_LIMIT,
        )
        return {
            "key": "newapi:gpt-image-2:design-space",
            "limit": max(1, limit),
            "retryAttempts": max(
                1,
                env_int(
                    "LIBAI_NEWAPI_GPT_IMAGE2_DESIGN_RETRY_ATTEMPTS",
                    DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_RETRY_ATTEMPTS,
                ),
            ),
            "retryDelaySeconds": max(
                1,
                env_int(
                    "LIBAI_NEWAPI_GPT_IMAGE2_DESIGN_RETRY_DELAY_SECONDS",
                    DEFAULT_DESIGN_SPACE_GPT_IMAGE_2_RETRY_DELAY_SECONDS,
                ),
            ),
        }
    return None


def provider_job_error_is_retryable(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if "server disconnected" in text or "broken pipe" in text:
        return False
    return (
        "upstream request failed" in text
        or "status_code=502" in text
        or "status code: 502" in text
        or "http 502" in text
    )


def provider_job_queue_semaphore(profile: Dict[str, Any]) -> asyncio.Semaphore:
    key = str(profile.get("key") or "").strip()
    if not key:
        raise ValueError("provider job queue key required")
    limit = max(1, int(profile.get("limit") or 1))
    current_limit = PROVIDER_JOB_QUEUE_LIMITS.get(key)
    if key not in PROVIDER_JOB_QUEUE_SEMAPHORES or current_limit != limit:
        PROVIDER_JOB_QUEUE_SEMAPHORES[key] = asyncio.Semaphore(limit)
        PROVIDER_JOB_QUEUE_LIMITS[key] = limit
    return PROVIDER_JOB_QUEUE_SEMAPHORES[key]


async def run_provider_job_with_profile_retries(
    job_type: str,
    payload: Dict[str, Any],
    provider: Dict[str, Any],
    model: Dict[str, Any],
    progress,
    queue_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    attempts = max(1, int((queue_profile or {}).get("retryAttempts") or 1))
    retry_delay_seconds = max(1, int((queue_profile or {}).get("retryDelaySeconds") or 1))
    for attempt in range(attempts):
        try:
            return await run_provider_job(job_type, payload, provider, model, progress)
        except ProviderAdapterError as error:
            if attempt >= attempts - 1 or not provider_job_error_is_retryable(error):
                raise
            await progress(8, "retrying-provider")
            await asyncio.sleep(retry_delay_seconds)
    raise ProviderAdapterError("供应商任务失败")


async def finalize_successful_provider_job(
    job_id: str,
    job: Dict[str, Any],
    output: Dict[str, Any],
    provider: Dict[str, Any],
    model: Dict[str, Any],
) -> None:
    output = dict(output or {})
    local_output_path = output.pop("localPath", None)
    if local_output_path:
        asset = register_job_output_asset(job["projectId"], local_output_path, output, job_id)
        output.update({
            "assetId": asset["id"],
            "assetUrl": asset["assetUrl"],
            "url": asset["assetUrl"],
            "urls": [asset["assetUrl"]],
            "assetPath": asset["path"],
            "asset": asset,
        })
    else:
        remote_url = first_output_media_url(output) or ""
        media_kinds = {"image", "video", "audio"}
        media_type_prefixes = ("image.", "video.", "audio.")
        kind_hint = str(output.get("assetKind") or "").strip().lower()
        job_type_hint = str(job.get("type") or "").strip().lower()
        if kind_hint not in media_kinds:
            for prefix in media_type_prefixes:
                if job_type_hint.startswith(prefix):
                    kind_hint = prefix[:-1]
                    output["assetKind"] = kind_hint
                    break
        try:
            asset = await materialize_job_output_asset(job["projectId"], output, job_id)
        except ProviderAdapterError as error:
            if not kind_hint and remote_url:
                kind_hint = guess_kind(Path(urlparse(remote_url).path or ""), output.get("mime"))
            if kind_hint in media_kinds or job_type_hint.startswith(media_type_prefixes):
                raise
            if remote_url and re.match(r"^https?://", remote_url, re.I) and not is_provider_loopback_output_url(remote_url):
                output["assetCopyError"] = exception_message(error, "生成结果本地保存失败")
            else:
                raise
        else:
            if asset:
                output.update({
                    "assetId": asset["id"],
                    "assetUrl": asset["assetUrl"],
                    "url": asset["assetUrl"],
                    "urls": [asset["assetUrl"]],
                    "assetPath": asset["path"],
                    "asset": asset,
                })

    current = get_job(job_id)
    if current["status"] == "canceled":
        await broadcast({"type": "job.canceled", "job": current})
        return
    await update_job(
        job_id,
        "completed",
        100,
        output={
            **output,
            "provider": output.get("provider") or provider.get("id"),
            "providerModelId": output.get("providerModelId") or model.get("id"),
            "providerModelName": output.get("providerModelName") or model.get("modelName"),
            "displayName": output.get("displayName") or model.get("displayName") or model.get("modelName"),
        },
    )


async def run_queued_job(job_id: str) -> None:
    job: Dict[str, Any] = {}
    job_type = ""
    provider: Dict[str, Any] = {}
    model: Dict[str, Any] = {}
    try:
        job = get_job(job_id)
        payload = dict(job.get("input") or {})
        job_type = normalize_job_type(job["type"], payload)
        payload["_jobId"] = job_id
        payload["_projectId"] = job["projectId"]
        payload["_projectCacheDir"] = str(project_dir(job["projectId"]) / "cache")
        payload["_backendBaseUrl"] = backend_base_url()
        local_model = {
            "provider": "local.ffmpeg",
            "providerModelId": "storyboard.video.analyze",
            "providerModelName": "FFmpeg 视频解析",
        }

        async def local_progress(value: int, stage: Optional[str] = None) -> None:
            current = get_job(job_id)
            if current["status"] == "canceled":
                raise JobCanceled()
            await update_job(
                job_id,
                "running",
                max(1, min(99, int(value))),
                output={
                    **local_model,
                    "stage": stage or "running",
                },
            )

        if job_type == "storyboard.video.analyze":
            await run_storyboard_video_analysis_job(job_id, job, payload, local_progress)
            return

        if job_type == "storyboard.video.trim":
            await run_storyboard_video_trim_job(job_id, job, payload, local_progress)
            return

        if job_type in {"image.upscale", "video.subtitle.remove"}:
            asset = get_asset_record(asset_id_from_payload(payload) or "")
            if asset and asset.get("path"):
                payload["_sourceAssetPath"] = asset["path"]
        model = resolve_provider_model(job_type, payload)
        provider = resolve_runtime_provider(model)
        payload = await prepare_outgoing_reference_image_payload(job_type, payload, provider, model)

        async def progress(value: int, stage: Optional[str] = None) -> None:
            current = get_job(job_id)
            if current["status"] == "canceled":
                raise JobCanceled()
            output = {
                "provider": provider["id"],
                "providerModelId": model["id"],
                "providerModelName": model["modelName"],
                "stage": stage or "running",
            }
            await update_job(job_id, "running", max(1, min(99, int(value))), output=output)

        queue_profile = provider_job_queue_profile(job_type, payload, provider, model)
        if queue_profile:
            current = get_job(job_id)
            if current["status"] == "canceled":
                raise JobCanceled()
            await update_job(
                job_id,
                "queued",
                max(1, min(99, int(current.get("progress") or 1))),
                output={
                    "provider": provider["id"],
                    "providerModelId": model["id"],
                    "providerModelName": model["modelName"],
                    "stage": "waiting-provider-queue",
                    "queueKey": queue_profile["key"],
                    "queueLimit": queue_profile["limit"],
                },
            )
            async with provider_job_queue_semaphore(queue_profile):
                await progress(6, "resolved-provider")
                output = await run_provider_job_with_profile_retries(
                    job_type,
                    payload,
                    provider,
                    model,
                    progress,
                    queue_profile,
                )
        else:
            await progress(6, "resolved-provider")
            output = await run_provider_job_with_profile_retries(job_type, payload, provider, model, progress)
        await finalize_successful_provider_job(job_id, job, output, provider, model)
    except JobCanceled:
        await broadcast({"type": "job.canceled", "job": get_job(job_id)})
    except VideoAnalysisError as error:
        await update_job(job_id, "failed", 100, output={}, error=exception_message(error, "视频解析失败"))
    except SubtitleRemovalError as error:
        await update_job(job_id, "failed", 100, output={}, error=exception_message(error, "去字幕处理失败"))
    except ProviderAdapterError as error:
        fallback_error = exception_message(error, "供应商任务失败")
        official_terminal = await asyncio.to_thread(
            lookup_newapi_video_task_terminal_result,
            job_id,
            job_type,
            provider,
        )
        if official_terminal and official_terminal.get("status") == "completed" and job:
            await finalize_successful_provider_job(
                job_id,
                job,
                official_terminal.get("output") or {},
                provider,
                model,
            )
            return
        official_error = ""
        if official_terminal and official_terminal.get("status") == "failed":
            official_error = str(official_terminal.get("error") or "").strip()
        await update_job(job_id, "failed", 100, output={}, error=official_error or fallback_error)
    except Exception as error:
        await update_job(job_id, "failed", 100, output={}, error=exception_message(error, "生成任务失败"))
    finally:
        unregister_active_job_task(job_id)


@app.post("/jobs")
async def create_job(body: JobCreate) -> Dict[str, Any]:
    validate_project_id(body.project_id)
    ensure_project(body.project_id)
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    job_type = normalize_job_type(body.type, body.payload or {})
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs(id, project_id, node_id, type, status, progress, input_json, output_json, error, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, body.project_id, body.node_id, job_type, "queued", 0, json_dump(body.payload), "{}", None, now, now),
        )
        conn.execute(
            "INSERT INTO history(id, project_id, action, target_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", body.project_id, "job.create", job_id, json_dump({"type": job_type, **(body.payload or {})}), now),
        )
        conn.commit()
    try_sync_project_files(body.project_id)
    job = get_job(job_id)
    await broadcast({"type": "job.created", "job": job})
    task = asyncio.create_task(run_queued_job(job_id))
    register_active_job_task(job_id, task)
    return job


@app.get("/jobs")
def list_jobs(project_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 80) -> Dict[str, Any]:
    clauses: List[str] = []
    params: List[Any] = []
    if project_id:
        validate_project_id(project_id)
        clauses.append("project_id = ?")
        params.append(project_id)
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind in {"image", "video"}:
        if normalized_kind == "image":
            clauses.append("type LIKE 'image.%'")
        else:
            clauses.append("(type LIKE 'video.%' OR type = 'storyboard.video.analyze')")
    elif normalized_kind:
        raise HTTPException(status_code=400, detail="Unsupported job kind")
    safe_limit = max(1, min(200, int(limit or 80)))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT *
        FROM jobs
        {where}
        ORDER BY created_at DESC, rowid ASC
        LIMIT ?
    """
    params.append(safe_limit)
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return {"jobs": [row_job(row) for row in rows]}


@app.get("/jobs/{job_id}")
def read_job(job_id: str) -> Dict[str, Any]:
    return get_job(job_id)


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    if job["status"] in {"completed", "failed", "canceled"}:
        return job
    cancel_active_job_task(job_id)
    await update_job(
        job_id,
        "canceled",
        job["progress"],
        output={**(job.get("output") or {}), "cancelRequested": True},
    )
    canceled = get_job(job_id)
    await broadcast({"type": "job.canceled", "job": canceled})
    return canceled


@app.websocket("/jobs/events")
async def job_events(websocket: WebSocket) -> None:
    await websocket.accept()
    websockets.append(websocket)
    try:
        await websocket.send_json({"type": "connected", "time": utc_now()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in websockets:
            websockets.remove(websocket)

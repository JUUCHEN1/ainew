"""
剪映草稿导出 API。
功能：1) 按顺序导出视频到剪映时间轴。2) 可选导出图片素材到素材库（不进入时间轴）。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from types import SimpleNamespace
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.network_security import UnsafeRemoteUrlError, public_http_get, unsafe_remote_url_message

router = APIRouter(prefix="/jianying", tags=["剪映导出"])
DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0


def _runtime_data_dir() -> Path:
    configured = os.environ.get("LIBAI_APP_DATA_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "LibAI"
    return Path.home() / ".libai"


def _read_runtime_settings() -> dict:
    settings_path = _runtime_data_dir() / "settings.json"
    try:
        if settings_path.exists():
            return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def get_settings():
    """兼容旧项目的 get_settings，只读取当前漫创AI的本地运行配置。"""
    settings = _read_runtime_settings()
    project_root = (
        os.environ.get("LIBAI_PROJECT_STORAGE_DIR")
        or settings.get("projectStorageDir")
        or str(_runtime_data_dir() / "projects")
    )
    jianying_root = (
        os.environ.get("JIANYING_DRAFTS_ROOT")
        or settings.get("jianyingDraftsRoot")
        or settings.get("jianying_drafts_root")
        or ""
    )
    return SimpleNamespace(
        projects_root_path=os.path.abspath(os.path.expanduser(project_root)),
        jianying_drafts_root=os.path.abspath(os.path.expanduser(jianying_root)) if jianying_root else "",
    )


class VideoItem(BaseModel):
    """视频导出条目。"""

    url: str
    duration: float
    order: int
    panelIndex: Optional[str] = None
    timeline_start_ms: Optional[int] = None
    timeline_end_ms: Optional[int] = None
    source_segment_ids: Optional[List[int]] = None


class ImageItem(BaseModel):
    """图片导出条目。默认仅进素材库，as_timeline=True 时进入时间线。"""

    url: str
    order: int
    duration: float = 3.0
    panelIndex: Optional[str] = None
    timeline_start_ms: Optional[int] = None
    timeline_end_ms: Optional[int] = None
    as_timeline: bool = False


class AudioItem(BaseModel):
    """整段音频导出条目。"""

    url: str
    order: int = 0
    start_ms: int = 0
    end_ms: Optional[int] = None
    duration: Optional[float] = None


class SubtitleItem(BaseModel):
    """字幕导出条目。"""

    segment_id: int
    start_ms: int
    end_ms: int
    text: str
    order: int = 0


class ExportRequest(BaseModel):
    """剪映导出请求。"""

    draft_name: str
    videos: List[VideoItem]
    images: List[ImageItem] = Field(default_factory=list)
    subtitles: List[SubtitleItem] = Field(default_factory=list)
    audios: List[AudioItem] = Field(default_factory=list)
    output_path: str
    width: int = 1920
    height: int = 1080
    use_requested_duration: bool = False
    overwrite: bool = False

class ExportResponse(BaseModel):
    """Export response."""

    success: bool
    draft_path: str
    message: str


def _sanitize_draft_name(name: str) -> str:
    cleaned = (name or "").strip()
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        raise HTTPException(status_code=400, detail="草稿名称不能为空")

    if cleaned in {".", ".."}:
            raise HTTPException(status_code=400, detail="草稿路径非法")
    return cleaned


def _normalize_path_for_compare(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(cleaned)))


def _validate_draft_root(configured_root: str, requested_root: str) -> str:
    root = (configured_root or "").strip()
    if not root:
        raise HTTPException(status_code=400, detail="请先在设置中配置剪映草稿目录")

    normalized_configured = _normalize_path_for_compare(root)
    if not os.path.isdir(normalized_configured):
        raise HTTPException(status_code=400, detail="剪映草稿根目录不存在，请检查全局设置")

    normalized_requested = _normalize_path_for_compare(requested_root)
    if normalized_requested and normalized_requested != normalized_configured:
        raise HTTPException(status_code=400, detail="Export path must match configured JianYing drafts root")

    return normalized_configured


def _resolve_local_media_path(url: str, projects_root: str) -> str:
    if not url:
        return url

    clean = url.split("?", 1)[0].split("#", 1)[0]

    if clean.startswith("file://"):
        parsed = urlparse(clean)
        path_text = url2pathname(unquote(parsed.path or ""))
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        return path_text

    if clean.startswith("libai-asset://"):
        stripped = re.sub(r"^libai-asset:///?", "", clean)
        path_text = unquote(stripped)
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        return path_text

    if clean.startswith("/static/projects/"):
        relative = clean[len("/static/projects/") :]
        return os.path.join(projects_root, relative.replace("/", os.sep))

    if clean.startswith("static/projects/"):
        relative = clean[len("static/projects/") :]
        return os.path.join(projects_root, relative.replace("/", os.sep))

    parsed = urlparse(clean)
    asset_path = parsed.path if parsed.scheme in ("http", "https") else clean
    asset_match = re.match(r"^/assets/([A-Za-z0-9_-]+)$", asset_path)
    if asset_match:
        resolved = _resolve_asset_id_to_path(asset_match.group(1))
        if resolved:
            return resolved

    return clean


def _resolve_asset_id_to_path(asset_id: str) -> str:
    if not asset_id:
        return ""
    db_path = _runtime_data_dir() / "libai.sqlite3"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT path FROM assets WHERE id = ?", (asset_id,)).fetchone()
        conn.close()
        if not row:
            return ""
        path = str(row[0] or "")
        return path if os.path.isfile(path) else ""
    except Exception:
        return ""


async def _download_remote_file(url: str, save_path: str) -> None:
    try:
        response = await public_http_get(url, timeout=DEFAULT_WAIT_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"下载资源失败: {url}")
        with open(save_path, "wb") as file:
            file.write(response.content)
    except UnsafeRemoteUrlError as exc:
        raise HTTPException(status_code=400, detail=unsafe_remote_url_message("下载资源失败")) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"下载资源失败: {url} ({exc})") from exc


async def _copy_or_download_media(url: str, save_path: str, projects_root: str) -> None:
    resolved_path = _resolve_local_media_path(url, projects_root)
    if resolved_path and os.path.exists(resolved_path):
        try:
            shutil.copy2(resolved_path, save_path)
            return
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"复制本地资源失败: {url} ({exc})") from exc

    if url.startswith("http://") or url.startswith("https://"):
        await _download_remote_file(url, save_path)
        return

    if not os.path.exists(resolved_path):
        raise HTTPException(status_code=400, detail=f"本地资源不存在: {url}")


def _guess_extension_from_url(url: str, default_ext: str) -> str:
    path = urlparse(url).path if url else ""
    ext = os.path.splitext(path)[1].lower()
    if ext and re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        return ext

    guessed_type, _ = mimetypes.guess_type(path or url or "")
    guessed_ext = mimetypes.guess_extension(guessed_type or "")
    if guessed_ext:
        if guessed_ext == ".jpe":
            return ".jpg"
        return guessed_ext.lower()

    return default_ext


def _resolve_binary_path(name: str) -> str:
    binary_name = f"{name}.exe" if os.name == "nt" else name
    env_key = "LIBAI_FFPROBE_PATH" if name == "ffprobe" else "LIBAI_FFMPEG_PATH"
    explicit = os.environ.get(env_key)
    if explicit and os.path.exists(explicit):
        return explicit
    here = Path(__file__).resolve()
    candidates = [
        here.parent / "bin" / binary_name,
        here.parent.parent / "bin" / binary_name,
        here.parent / binary_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name


def _resolve_ffprobe_path() -> str:
    return _resolve_binary_path("ffprobe")


def _resolve_ffmpeg_path() -> str:
    return _resolve_binary_path("ffmpeg")


def _parse_ffmpeg_duration(output: str) -> Optional[float]:
    match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", output or "")
    if not match:
        return None
    hours = float(match.group(1))
    minutes = float(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def get_video_duration_fallback(video_path: str, default_duration: float) -> int:
    """Return video duration in microseconds, with fallback to requested duration."""

    fallback_seconds = default_duration if default_duration and default_duration > 0 else 15.0

    try:
        ffprobe_path = _resolve_ffprobe_path()
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(float(result.stdout.strip()) * 1_000_000)
    except Exception:
        pass

    try:
        ffmpeg_path = _resolve_ffmpeg_path()
        result = subprocess.run(
            [ffmpeg_path, "-i", video_path],
            capture_output=True,
            text=True,
            timeout=DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        duration_sec = _parse_ffmpeg_duration(result.stderr or result.stdout)
        if duration_sec and duration_sec > 0:
            return int(duration_sec * 1_000_000)
    except Exception:
        pass

    return int(fallback_seconds * 1_000_000)


def _normalize_jianying_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


def _build_video_material(v: dict, width: int, height: int) -> dict:
    material_id = v["material_id"]
    now_sec = int(time.time())
    now_ms = int(time.time() * 1000)
    return {
        "id": material_id,
        "path": v["path"],
        "duration": v["duration"],
        "type": "video",
        "material_type": "video",
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "create_time": now_sec,
        "import_time": now_sec,
        "import_time_ms": now_ms,
        "crop": {
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "extra_type_option": 0,
        "formula_id": "",
        "freeze": None,
        "has_audio": True,
        "height": height,
        "width": width,
        "intensifies_audio_path": "",
        "intensifies_path": "",
        "is_ai_generate_content": False,
        "is_copyright": False,
        "is_text_edit_overdub": False,
        "is_unified_beauty_mode": False,
        "local_id": "",
        "local_material_id": material_id,
        "material_id": material_id,
        "material_name": v.get("filename", os.path.basename(v["path"])),
        "material_url": "",
        "matting": {
            "flag": 0,
            "has_use_quick_brush": False,
            "has_use_quick_eraser": False,
            "interactiveTime": [],
            "path": "",
            "strokes": [],
        },
        "media_path": "",
        "object_locked": None,
        "origin_material_id": "",
        "picture_from": "none",
        "picture_set_category_id": "",
        "picture_set_category_name": "",
        "request_id": "",
        "reverse_intensifies_path": "",
        "reverse_path": "",
        "smart_motion": None,
        "source": 0,
        "source_platform": 0,
        "stable": {
            "matrix_path": "",
            "stable_level": 0,
            "time_range": None,
        },
        "team_id": "",
        "video_algorithm": {
            "algorithms": [],
            "complement_frame_config": None,
            "deflicker": None,
            "gameplay_configs": [],
            "motion_blur_config": None,
            "noise_reduction": None,
            "path": "",
            "quality_enhance": None,
            "super_resolution": None,
            "time_range": None,
        },
    }


def _build_image_material(v: dict, width: int, height: int) -> dict:
    material_id = v["material_id"]
    now_sec = int(time.time())
    now_ms = int(time.time() * 1000)
    return {
        "id": material_id,
        "path": v["path"],
        "duration": v.get("duration", 3_000_000),
        "type": "photo",
        "material_type": "image",
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "create_time": now_sec,
        "import_time": now_sec,
        "import_time_ms": now_ms,
        "crop": {
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "extra_type_option": 0,
        "formula_id": "",
        "height": height,
        "width": width,
        "is_ai_generate_content": False,
        "is_copyright": False,
        "local_id": "",
        "local_material_id": material_id,
        "material_id": material_id,
        "material_name": v.get("filename", os.path.basename(v["path"])),
        "material_url": "",
        "media_path": "",
        "picture_from": "none",
        "request_id": "",
        "source": 0,
        "source_platform": 0,
        "team_id": "",
    }




def _build_audio_material(v: dict) -> dict:
    material_id = v["material_id"]
    now_sec = int(time.time())
    now_ms = int(time.time() * 1000)
    return {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "create_time": now_sec,
        "duration": int(v.get("duration", 0)),
        "effect_id": "",
        "formula_id": "",
        "id": material_id,
        "import_time": now_sec,
        "import_time_ms": now_ms,
        "intensifies_path": "",
        "local_material_id": material_id,
        "music_id": "",
        "name": v.get("filename", os.path.basename(v.get("path", ""))),
        "path": v.get("path", ""),
        "request_id": "",
        "resource_id": "",
        "source": 0,
        "source_platform": 0,
        "team_id": "",
        "text_id": "",
        "type": "extract_music",
        "wave_points": [],
    }
def _build_meta_material_entry(item: dict, width: int, height: int) -> dict:
    """构建 draft_meta_info.json 中 draft_materials 的单条素材条目。"""
    now_sec = int(time.time())
    now_ms = int(time.time() * 1000)
    material_type = item.get("material_type", "video")
    if material_type == "image":
        metetype = "photo"
    elif material_type == "audio":
        metetype = "music"
    else:
        metetype = material_type
    return {
        "create_time": now_sec,
        "duration": item.get("duration", 0),
        "extra_info": "",
        "file_Path": item.get("path", ""),
        "height": height,
        "id": item.get("material_id", uuid.uuid4().hex),
        "import_time": now_sec,
        "import_time_ms": now_ms,
        "item_source": 1,
        "md5": "",
        "metetype": metetype,
        "roughcut_time_range": {"duration": -1, "start": -1},
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0,
        "width": width,
    }

def generate_draft_meta_info(
    draft_name: str,
    draft_path: str,
    draft_root: str,
    videos: Optional[List[dict]] = None,
    images: Optional[List[dict]] = None,
    audios: Optional[List[dict]] = None,
    width: int = 1920,
    height: int = 1080,
) -> dict:
    current_time = int(time.time() * 1000)

    media_entries: List[dict] = []
    for v in (videos or []):
        media_entries.append(_build_meta_material_entry(v, width, height))
    for img in (images or []):
        entry = _build_meta_material_entry(img, width, height)
        entry["metetype"] = "photo"
        media_entries.append(entry)

    audio_entries: List[dict] = []
    for audio in (audios or []):
        entry = _build_meta_material_entry(audio, 0, 0)
        entry["metetype"] = "music"
        audio_entries.append(entry)

    total_duration = 0
    for v in (videos or []):
        start_us = int(v.get("timeline_start_us") or 0)
        total_duration = max(total_duration, start_us + int(v.get("duration", 0)))
    for img in (images or []):
        if not img.get("as_timeline"):
            continue
        start_us = int(img.get("timeline_start_us") or 0)
        total_duration = max(total_duration, start_us + int(img.get("duration", 0)))
    for audio in (audios or []):
        start_us = int(audio.get("start_us") or 0)
        total_duration = max(total_duration, start_us + int(audio.get("duration", 0)))

    return {
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": _normalize_jianying_path(draft_path),
        "draft_id": str(uuid.uuid4()).upper(),
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [
            {"type": 0, "value": media_entries},
            {"type": 1, "value": []},
            {"type": 2, "value": audio_entries},
            {"type": 3, "value": []},
            {"type": 6, "value": []},
            {"type": 7, "value": []},
            {"type": 8, "value": []},
        ],
        "draft_materials_copied_info": [],
        "draft_name": draft_name,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": _normalize_jianying_path(draft_root),
        "draft_segment_extra_info": [],
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_create": current_time,
        "tm_draft_modified": current_time,
        "tm_draft_removed": 0,
        "tm_duration": total_duration,
    }


def _build_visual_segment(material_id: str, segment_id: str, duration: int, target_start: int, render_index: int) -> dict:
    return {
        "id": segment_id,
        "material_id": material_id,
        "source_timerange": {"start": 0, "duration": duration},
        "target_timerange": {"start": target_start, "duration": duration},
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "extra_material_refs": [material_id],
        "render_index": render_index,
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_lut": False,
        "enable_smart_color_adjust": False,
        "group_id": "",
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "normalize_speed": 1.0,
        "responsive_layout": {
            "enable": False,
            "horizontal_pos_layout": 0,
            "size_layout": 0,
            "target_follow": "",
            "vertical_pos_layout": 0,
        },
        "reverse": False,
        "speed": 1.0,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": 1.0,
    }


def generate_draft_content(
    videos: List[dict],
    width: int,
    height: int,
    images: Optional[List[dict]] = None,
    subtitles: Optional[List[dict]] = None,
    audios: Optional[List[dict]] = None,
) -> dict:
    """生成 draft_content.json。普通图片只进素材库，收集节点图片可创建轨道片段。"""

    images = images or []
    subtitles = subtitles or []
    audios = audios or []

    video_materials = [_build_video_material(v, width, height) for v in videos]
    image_materials = [_build_image_material(v, width, height) for v in images]
    audio_materials = [_build_audio_material(v) for v in audios]

    video_segments = []
    audio_segments = []
    text_segments = []
    text_materials = []

    current_start = 0
    timeline_end = 0

    for idx, v in enumerate(videos):
        material_id = v.get("material_id") or v.get("id") or ""
        segment_id = v.get("segment_id") or str(uuid.uuid4())
        duration = int(v.get("duration") or 0)
        if duration <= 0:
            continue

        timeline_start_us = v.get("timeline_start_us")
        has_absolute_timeline = isinstance(timeline_start_us, int) and timeline_start_us >= 0
        target_start = timeline_start_us if has_absolute_timeline else current_start

        segment = {
            "id": segment_id,
            "material_id": material_id,
            "source_timerange": {"start": 0, "duration": duration},
            "target_timerange": {"start": target_start, "duration": duration},
            "clip": {
                "alpha": 1.0,
                "flip": {"horizontal": False, "vertical": False},
                "rotation": 0.0,
                "scale": {"x": 1.0, "y": 1.0},
                "transform": {"x": 0.0, "y": 0.0},
            },
            "extra_material_refs": [material_id],
            "render_index": idx,
            "enable_adjust": True,
            "enable_color_correct_adjust": False,
            "enable_lut": False,
            "enable_smart_color_adjust": False,
            "group_id": "",
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
            "intensifies_audio": False,
            "is_placeholder": False,
            "is_tone_modify": False,
            "keyframe_refs": [],
            "last_nonzero_volume": 1.0,
            "normalize_speed": 1.0,
            "responsive_layout": {
                "enable": False,
                "horizontal_pos_layout": 0,
                "size_layout": 0,
                "target_follow": "",
                "vertical_pos_layout": 0,
            },
            "reverse": False,
            "speed": 1.0,
            "template_id": "",
            "template_scene": "default",
            "track_attribute": 0,
            "track_render_index": 0,
            "uniform_scale": {"on": True, "value": 1.0},
            "visible": True,
            "volume": 1.0,
        }
        video_segments.append(segment)

        segment_end = target_start + duration
        timeline_end = max(timeline_end, segment_end)
        current_start = max(current_start, segment_end)

    for idx, image in enumerate(images):
        if not image.get("as_timeline"):
            continue

        material_id = image.get("material_id") or image.get("id") or ""
        segment_id = image.get("segment_id") or str(uuid.uuid4())
        duration = int(image.get("duration") or 0)
        if duration <= 0:
            continue

        timeline_start_us = image.get("timeline_start_us")
        has_absolute_timeline = isinstance(timeline_start_us, int) and timeline_start_us >= 0
        target_start = timeline_start_us if has_absolute_timeline else current_start

        video_segments.append(
            _build_visual_segment(
                material_id=material_id,
                segment_id=segment_id,
                duration=duration,
                target_start=target_start,
                render_index=len(video_segments) + idx,
            )
        )
        segment_end = target_start + duration
        timeline_end = max(timeline_end, segment_end)
        current_start = max(current_start, segment_end)

    video_segments.sort(key=lambda item: (
        int(item.get("target_timerange", {}).get("start") or 0),
        int(item.get("render_index") or 0),
    ))
    for index, segment in enumerate(video_segments):
        segment["render_index"] = index
        segment["track_render_index"] = 0

    for idx, audio in enumerate(audios):
        material_id = audio.get("material_id") or audio.get("id") or ""
        segment_id = audio.get("segment_id") or str(uuid.uuid4())
        duration = int(audio.get("duration") or 0)
        if duration <= 0:
            continue

        start_us = max(0, int(audio.get("start_us") or 0))
        audio_segments.append(
            {
                "id": segment_id,
                "material_id": material_id,
                "source_timerange": {"start": 0, "duration": duration},
                "target_timerange": {"start": start_us, "duration": duration},
                "extra_material_refs": [material_id],
                "render_index": idx,
                "track_render_index": idx,
                "visible": True,
                "volume": 1.0,
                "speed": 1.0,
            }
        )
        timeline_end = max(timeline_end, start_us + duration)

    for idx, subtitle in enumerate(subtitles):
        text = str(subtitle.get("text") or "").strip()
        start_us = int(subtitle.get("start_us") or 0)
        duration_us = int(subtitle.get("duration_us") or 0)
        if not text or duration_us <= 0:
            continue

        material_id = subtitle.get("material_id") or uuid.uuid4().hex
        text_materials.append(
            {
                "id": material_id,
                "content": text,
                "font": "SourceHanSansCN-Regular",
                "font_size": 8.0,
                "font_color": "#FFFFFFFF",
                "background_color": "",
                "line_spacing": 1.0,
                "letter_spacing": 0.0,
                "alignment": 1,
                "bold": False,
                "italic": False,
                "underline": False,
                "vertical": False,
                "type": "text",
            }
        )

        text_segments.append(
            {
                "id": subtitle.get("segment_ref") or str(uuid.uuid4()),
                "material_id": material_id,
                "source_timerange": {"start": 0, "duration": duration_us},
                "target_timerange": {"start": start_us, "duration": duration_us},
                "extra_material_refs": [material_id],
                "render_index": idx,
                "track_render_index": idx,
                "visible": True,
            }
        )

        timeline_end = max(timeline_end, start_us + duration_us)

    video_track = {
        "id": str(uuid.uuid4()),
        "type": "video",
        "attribute": 0,
        "is_default_name": True,
        "flag": 0,
        "segments": video_segments,
    }

    tracks = [video_track]
    if audio_segments:
        tracks.append(
            {
                "id": str(uuid.uuid4()),
                "type": "audio",
                "attribute": 0,
                "is_default_name": True,
                "flag": 0,
                "segments": audio_segments,
            }
        )
    if text_segments:
        tracks.append(
            {
                "id": str(uuid.uuid4()),
                "type": "text",
                "attribute": 0,
                "is_default_name": True,
                "flag": 0,
                "segments": text_segments,
            }
        )

    return {
        "canvas_config": {"height": height, "width": width, "ratio": f"{width}:{height}"},
        "color_space": 0,
        "config": {},
        "cover": "",
        "creation_source": "tapnow_studio",
        "duration": timeline_end,
        "extra_info": "",
        "fps": 30.0,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": str(uuid.uuid4()),
        "keyframe_graph_list": [],
        "keyframes": {},
        "last_modified_platform": {},
        "materials": {
            "audios": audio_materials,
            "beats": [],
            "canvases": [],
            "chromas": [],
            "color_curves": [],
            "digital_humans": [],
            "drafts": [],
            "effects": [],
            "flowers": [],
            "green_screens": [],
            "handwrite": [],
            "hsl": [],
            "images": image_materials,
            "log_color_wheels": [],
            "loudness": [],
            "manual_deformations": [],
            "masks": [],
            "material_animations": [],
            "material_colors": [],
            "multi_language_refs": [],
            "placeholders": [],
            "plugin_effects": [],
            "primary_color_wheels": [],
            "realtime_denoises": [],
            "shapes": [],
            "smart_crops": [],
            "smart_relights": [],
            "sound_channel_mappings": [],
            "speeds": [],
            "stickers": [],
            "tail_leaders": [],
            "text_templates": [],
            "texts": text_materials,
            "transitions": [],
            "video_effects": [],
            "video_trackings": [],
            "videos": video_materials,
            "vocal_beautifys": [],
            "vocal_separations": [],
        },
        "mutable_config": None,
        "name": "",
        "new_version": "",
        "platform": {},
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "tapnow_studio",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": int(time.time()),
        "version": 360000,
    }

def _build_key_value_material_entry(material_id: str, material_name: str, rank: int, is_image: bool) -> dict:
    return {
        "commerce_template_cate": "",
        "commerce_template_pay_status": "",
        "commerce_template_pay_type": "",
        "douyin_music_is_avaliable": False,
        "enter_from": "",
        "filter_category": "",
        "filter_detail": "",
        "is_brand": 0,
        "is_favorite": False,
        "is_from_artist_shop": 0,
        "is_limited": False,
        "is_similar_music": False,
        "is_vip": "0",
        "keywordSource": "",
        "materialCategory": "media" if not is_image else "photo",
        "materialId": material_id,
        "materialName": material_name,
        "materialSubcategory": "local",
        "materialSubcategoryId": "",
        "materialThirdcategory": "导入",
        "materialThirdcategoryId": "",
        "material_copyright": "",
        "material_is_purchased": "",
        "music_source": "",
        "original_song_id": "",
        "original_song_name": "",
        "pgc_id": "",
        "pgc_name": "",
        "previewed": 0,
        "previewed_before_added": 0,
        "rank": str(rank),
        "rec_id": "",
        "requestId": "",
        "role": "",
        "searchId": "",
        "searchKeyword": "",
        "special_effect_loading_type": "",
        "team_id": "",
        "template_author_id": "",
        "template_drafts_price": 0,
        "template_duration": 0,
        "template_fragment_cnt": 0,
        "template_need_purcahse": True,
        "template_pay_type": "",
        "template_type": "",
        "template_use_cnt": 0,
        "textTemplateVersion": "",
    }


def generate_key_value(videos: List[dict], images: Optional[List[dict]] = None, audios: Optional[List[dict]] = None) -> dict:
    """
    生成 key_value.json。

    - 视频：写 materialId 和 segmentId 映射。
    - 图片：仅写 materialId（不写 segmentId）。
    """
    images = images or []
    audios = audios or []
    result: dict = {}

    for idx, v in enumerate(videos):
        material_id = v.get("material_id") or v.get("id") or ""
        segment_id = v.get("segment_id") or ""
        material_name = v.get("filename", os.path.basename(v.get("path", "")))
        result[material_id] = _build_key_value_material_entry(material_id, material_name, idx, is_image=False)

        if segment_id:
            result[segment_id] = {
                "filter_category": "",
                "filter_detail": "",
                "is_brand": 0,
                "is_from_artist_shop": 0,
                "is_vip": "0",
                "keywordSource": "",
                "materialCategory": "media",
                "materialId": material_id,
                "materialName": material_name,
                "materialSubcategory": "local",
                "materialSubcategoryId": "",
                "materialThirdcategory": "导入",
                "materialThirdcategoryId": "",
                "material_copyright": "",
                "material_is_purchased": "",
                "rank": str(idx),
                "rec_id": "",
                "requestId": "",
                "role": "",
                "searchId": "",
                "searchKeyword": "",
                "segmentId": segment_id,
                "team_id": "",
                "textTemplateVersion": "",
            }

    base_rank = len(videos)
    for idx, image in enumerate(images):
        material_id = image.get("material_id") or image.get("id") or ""
        segment_id = image.get("segment_id") or ""
        material_name = image.get("filename", os.path.basename(image.get("path", "")))
        result[material_id] = _build_key_value_material_entry(
            material_id,
            material_name,
            base_rank + idx,
            is_image=True,
        )
        if segment_id:
            result[segment_id] = {
                "filter_category": "",
                "filter_detail": "",
                "is_brand": 0,
                "is_from_artist_shop": 0,
                "is_vip": "0",
                "keywordSource": "",
                "materialCategory": "media",
                "materialId": material_id,
                "materialName": material_name,
                "materialSubcategory": "local",
                "materialSubcategoryId": "",
                "materialThirdcategory": "导入",
                "materialThirdcategoryId": "",
                "material_copyright": "",
                "material_is_purchased": "",
                "rank": str(base_rank + idx),
                "rec_id": "",
                "requestId": "",
                "role": "",
                "searchId": "",
                "searchKeyword": "",
                "segmentId": segment_id,
                "team_id": "",
                "textTemplateVersion": "",
            }

    audio_rank = len(videos) + len(images)
    for idx, audio in enumerate(audios):
        material_id = audio.get("material_id") or audio.get("id") or ""
        material_name = audio.get("filename", os.path.basename(audio.get("path", "")))
        result[material_id] = _build_key_value_material_entry(
            material_id,
            material_name,
            audio_rank + idx,
            is_image=False,
        )

    return result

def generate_draft_virtual_store(
    videos: Optional[List[dict]] = None,
    images: Optional[List[dict]] = None,
    audios: Optional[List[dict]] = None,
) -> dict:
    material_entries: List[dict] = []
    now_sec = int(time.time())
    now_ms = now_sec * 1000000  # microseconds

    for v in (videos or []):
        material_entries.append({
            "create_time": now_sec,
            "file_Path": v.get("path", ""),
            "id": v.get("material_id", ""),
            "import_time": now_sec,
            "import_time_us": now_ms,
        })
    for img in (images or []):
        material_entries.append({
            "create_time": now_sec,
            "file_Path": img.get("path", ""),
            "id": img.get("material_id", ""),
            "import_time": now_sec,
            "import_time_us": now_ms,
        })
    for audio in (audios or []):
        material_entries.append({
            "create_time": now_sec,
            "file_Path": audio.get("path", ""),
            "id": audio.get("material_id", ""),
            "import_time": now_sec,
            "import_time_us": now_ms,
        })

    return {
        "draft_materials": material_entries,
        "draft_virtual_store": [
            {
                "type": 0,
                "value": [
                    {
                        "creation_time": 0,
                        "display_name": "",
                        "filter_type": 0,
                        "id": "",
                        "import_time": 0,
                        "import_time_us": 0,
                        "sort_sub_type": 0,
                        "sort_type": 0,
                        "subdraft_filter_type": 0,
                    }
                ],
            },
            {"type": 1, "value": []},
            {"type": 2, "value": []},
        ],
    }

def _rewrite_resource_path(value: str, draft_folder: str) -> str:
    if not value:
        return value

    normalized = value.replace("\\", "/")
    if normalized.startswith("file://") or re.match(r"^[a-zA-Z]:/", normalized) or normalized.startswith("//"):
        return value

    if re.match(r"^[./\\\\]*Resources/", normalized, re.IGNORECASE):
        relative = re.sub(r"^[./\\\\]+", "", normalized)
        absolute = os.path.abspath(os.path.join(draft_folder, relative))
        return absolute.replace("\\", "/")

    if re.match(r"^/Resources/", normalized, re.IGNORECASE):
        relative = normalized.lstrip("/")
        absolute = os.path.abspath(os.path.join(draft_folder, relative))
        return absolute.replace("\\", "/")

    return value


def _rewrite_paths_in_obj(obj: Any, draft_folder: str) -> bool:
    changed = False

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                new_value = _rewrite_resource_path(value, draft_folder)
                if new_value != value:
                    obj[key] = new_value
                    changed = True
            elif isinstance(value, (list, dict)):
                if _rewrite_paths_in_obj(value, draft_folder):
                    changed = True

    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            if isinstance(item, str):
                new_value = _rewrite_resource_path(item, draft_folder)
                if new_value != item:
                    obj[index] = new_value
                    changed = True
            elif isinstance(item, (list, dict)):
                if _rewrite_paths_in_obj(item, draft_folder):
                    changed = True

    return changed


def _rewrite_paths_in_file(file_path: str, draft_folder: str) -> None:
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            text = file.read()
        if not text.strip().startswith(("{", "[")):
            return
        obj = json.loads(text)
    except Exception:
        return

    if not _rewrite_paths_in_obj(obj, draft_folder):
        return

    is_compact = ("\n" not in text) and ("\r" not in text)
    with open(file_path, "w", encoding="utf-8") as file:
        if is_compact:
            json.dump(obj, file, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, file, ensure_ascii=False, indent=2)


def _rewrite_paths_in_draft_folder(draft_folder: str) -> None:
    for root, _, files in os.walk(draft_folder):
        for filename in files:
            if filename in ("draft_content.json", "template-2.tmp"):
                _rewrite_paths_in_file(os.path.join(root, filename), draft_folder)


def _clean_existing_draft_folder(draft_folder: str) -> None:
    if not os.path.isdir(draft_folder):
        return

    for filename in (
        "template-2.tmp",
        "draft_content.json.bak",
        "timeline_layout.json",
        "draft_cover.jpg",
        ".locked",
    ):
        file_path = os.path.join(draft_folder, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

    for dirname in (
        "Timelines",
        "subdraft",
        "adjust_mask",
        "matting",
        "smart_crop",
        "qr_upload",
        "common_attachment",
    ):
        dir_path = os.path.join(draft_folder, dirname)
        if os.path.isdir(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)

    resources_dir = os.path.join(draft_folder, "Resources")
    if os.path.isdir(resources_dir):
        shutil.rmtree(resources_dir, ignore_errors=True)


def _resolve_draft_folder(draft_root: str, draft_name: str, overwrite: bool = False) -> tuple[str, str]:
    """返回安全草稿目录；默认不覆盖已有剪映草稿。"""
    base_folder = os.path.join(draft_root, draft_name)
    absolute_base = os.path.abspath(base_folder)
    if os.path.commonpath([draft_root, absolute_base]) != draft_root:
        raise HTTPException(status_code=400, detail="草稿路径非法")
    if overwrite or not os.path.exists(absolute_base):
        return absolute_base, draft_name

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    for index in range(1, 100):
        suffix = timestamp if index == 1 else f"{timestamp}_{index}"
        next_name = f"{draft_name}_{suffix}"
        next_folder = os.path.abspath(os.path.join(draft_root, next_name))
        if os.path.commonpath([draft_root, next_folder]) != draft_root:
            raise HTTPException(status_code=400, detail="草稿路径非法")
        if not os.path.exists(next_folder):
            return next_folder, next_name
    raise HTTPException(status_code=409, detail="无法创建不重名的剪映草稿目录")


@router.post("/export", response_model=ExportResponse)
async def export_jianying_draft(request: ExportRequest):
    """
    导出剪映草稿。

    - videos: 进入时间轴
    - images: 仅导入素材库，不进入时间轴
    """

    try:
        settings = get_settings()
        configured_root = settings.jianying_drafts_root or os.environ.get("JIANYING_DRAFTS_ROOT", "")
        draft_root = _validate_draft_root(configured_root, request.output_path)
        draft_name = _sanitize_draft_name(request.draft_name)
        draft_folder, draft_name = _resolve_draft_folder(draft_root, draft_name, request.overwrite)

        resources_folder = os.path.join(draft_folder, "Resources")
        if request.overwrite:
            _clean_existing_draft_folder(draft_folder)
        os.makedirs(resources_folder, exist_ok=True)

        sorted_videos = sorted(
            request.videos,
            key=lambda item: (
                item.timeline_start_ms if item.timeline_start_ms is not None else 10**18,
                item.order,
            ),
        )
        timeline_image_count = sum(1 for item in request.images if item.as_timeline)
        if len(sorted_videos) == 0 and timeline_image_count == 0:
            raise HTTPException(status_code=400, detail="至少需要 1 个视频或时间线图片用于导出时间轴")


        processed_videos: List[dict] = []
        for idx, video in enumerate(sorted_videos):
            ext = _guess_extension_from_url(video.url, ".mp4")
            filename = f"shot_{idx + 1:03d}{ext}"
            local_path = os.path.join(resources_folder, filename)
            absolute_path = os.path.abspath(local_path).replace("\\", "/")

            await _copy_or_download_media(video.url, local_path, settings.projects_root_path)

            actual_us = get_video_duration_fallback(local_path, video.duration)
            desired_us = int(video.duration * 1_000_000) if video.duration and video.duration > 0 else actual_us

            if (
                video.timeline_start_ms is not None
                and video.timeline_end_ms is not None
                and video.timeline_end_ms > video.timeline_start_ms
            ):
                desired_us = int((video.timeline_end_ms - video.timeline_start_ms) * 1000)

            clip_us = min(actual_us, desired_us) if desired_us > 0 else actual_us
            if clip_us <= 0:
                continue

            source_segment_ids: List[int] = []
            if video.source_segment_ids:
                source_segment_ids = [
                    int(segment_id)
                    for segment_id in video.source_segment_ids
                    if isinstance(segment_id, int) or (isinstance(segment_id, float) and segment_id.is_integer())
                ]

            processed_videos.append(
                {
                    "material_id": uuid.uuid4().hex,
                    "segment_id": str(uuid.uuid4()),
                    "path": absolute_path,
                    "duration": int(clip_us),
                    "actual_duration": int(actual_us),
                    "desired_duration": int(desired_us),
                    "filename": filename,
                    "panel_index": video.panelIndex,
                    "timeline_start_us": int(video.timeline_start_ms * 1000)
                    if video.timeline_start_ms is not None and video.timeline_start_ms >= 0
                    else None,
                    "timeline_end_us": int(video.timeline_end_ms * 1000)
                    if video.timeline_end_ms is not None and video.timeline_end_ms >= 0
                    else None,
                    "source_segment_ids": source_segment_ids,
                }
            )

        sorted_images = sorted(request.images, key=lambda item: item.order)
        processed_images: List[dict] = []
        for idx, image in enumerate(sorted_images):
            ext = _guess_extension_from_url(image.url, ".jpg")
            filename = f"image_{idx + 1:03d}{ext}"
            local_path = os.path.join(resources_folder, filename)
            absolute_path = os.path.abspath(local_path).replace("\\", "/")

            await _copy_or_download_media(image.url, local_path, settings.projects_root_path)

            desired_us = int(image.duration * 1_000_000) if image.duration and image.duration > 0 else 3_000_000
            if (
                image.timeline_start_ms is not None
                and image.timeline_end_ms is not None
                and image.timeline_end_ms > image.timeline_start_ms
            ):
                desired_us = int((image.timeline_end_ms - image.timeline_start_ms) * 1000)
            if desired_us <= 0:
                desired_us = 3_000_000

            processed_images.append(
                {
                    "material_id": uuid.uuid4().hex,
                    "segment_id": str(uuid.uuid4()) if image.as_timeline else "",
                    "path": absolute_path,
                    "duration": int(desired_us),
                    "filename": filename,
                    "panel_index": image.panelIndex,
                    "as_timeline": bool(image.as_timeline),
                    "timeline_start_us": int(image.timeline_start_ms * 1000)
                    if image.timeline_start_ms is not None and image.timeline_start_ms >= 0
                    else None,
                    "timeline_end_us": int(image.timeline_end_ms * 1000)
                    if image.timeline_end_ms is not None and image.timeline_end_ms >= 0
                    else None,
                }
            )
        sorted_audios = sorted(request.audios, key=lambda item: (item.start_ms, item.order))
        processed_audios: List[dict] = []
        for idx, audio in enumerate(sorted_audios):
            ext = _guess_extension_from_url(audio.url, ".mp3")
            filename = f"audio_{idx + 1:03d}{ext}"
            local_path = os.path.join(resources_folder, filename)
            absolute_path = os.path.abspath(local_path).replace("\\", "/")

            await _copy_or_download_media(audio.url, local_path, settings.projects_root_path)

            actual_us = get_video_duration_fallback(local_path, audio.duration or 0)
            desired_us = int(audio.duration * 1_000_000) if audio.duration and audio.duration > 0 else actual_us
            if audio.end_ms is not None and audio.end_ms > audio.start_ms:
                desired_us = int((audio.end_ms - audio.start_ms) * 1000)
            clip_us = min(actual_us, desired_us) if desired_us > 0 else actual_us
            if clip_us <= 0:
                continue

            processed_audios.append(
                {
                    "material_id": uuid.uuid4().hex,
                    "segment_id": str(uuid.uuid4()),
                    "path": absolute_path,
                    "duration": int(clip_us),
                    "filename": filename,
                    "start_us": max(0, int(audio.start_ms * 1000)),
                    "material_type": "audio",
                }
            )
        sorted_subtitles = sorted(request.subtitles, key=lambda item: (item.start_ms, item.order))
        processed_subtitles: List[dict] = []
        seen_subtitle_segment_ids: set[int] = set()
        for subtitle in sorted_subtitles:
            if subtitle.segment_id in seen_subtitle_segment_ids:
                continue
            seen_subtitle_segment_ids.add(subtitle.segment_id)

            text = (subtitle.text or "").strip()
            if not text:
                continue
            if subtitle.end_ms <= subtitle.start_ms:
                continue

            start_us = max(0, int(subtitle.start_ms * 1000))
            end_us = max(start_us, int(subtitle.end_ms * 1000))
            duration_us = end_us - start_us
            if duration_us <= 0:
                continue

            processed_subtitles.append(
                {
                    "segment_id": subtitle.segment_id,
                    "material_id": uuid.uuid4().hex,
                    "segment_ref": str(uuid.uuid4()),
                    "start_us": start_us,
                    "end_us": end_us,
                    "duration_us": duration_us,
                    "text": text,
                }
            )

        meta_info = generate_draft_meta_info(
            draft_name, draft_folder, draft_root,
            videos=processed_videos,
            images=processed_images,
            audios=processed_audios,
            width=request.width,
            height=request.height,
        )
        with open(os.path.join(draft_folder, "draft_meta_info.json"), "w", encoding="utf-8") as file:
            json.dump(meta_info, file, ensure_ascii=False, indent=2)

        draft_content = generate_draft_content(
            processed_videos,
            request.width,
            request.height,
            processed_images,
            processed_subtitles,
            processed_audios,
        )
        with open(os.path.join(draft_folder, "draft_content.json"), "w", encoding="utf-8") as file:
            json.dump(draft_content, file, ensure_ascii=False, indent=2)

        key_value = generate_key_value(processed_videos, processed_images, processed_audios)
        with open(os.path.join(draft_folder, "key_value.json"), "w", encoding="utf-8") as file:
            json.dump(key_value, file, ensure_ascii=False, separators=(",", ":"))

        virtual_store = generate_draft_virtual_store(
            videos=processed_videos,
            images=processed_images,
            audios=processed_audios,
        )
        with open(os.path.join(draft_folder, "draft_virtual_store.json"), "w", encoding="utf-8") as file:
            json.dump(virtual_store, file, ensure_ascii=False, separators=(",", ":"))

        _rewrite_paths_in_draft_folder(draft_folder)

        # 将新草稿注册到 root_meta_info 的最前面，使其在剪映中排第一
        _register_draft_in_root_meta(draft_root, draft_folder, draft_name)

        return ExportResponse(
            success=True,
            draft_path=draft_folder,
            message=f"成功导出 {len(processed_videos)} 个视频，额外导出 {len(processed_images)} 张图片素材，字幕 {len(processed_subtitles)} 条，音频 {len(processed_audios)} 条",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导出失败: {exc}") from exc


def _register_draft_in_root_meta(draft_root: str, draft_folder: str, draft_name: str) -> None:
    """将新草稿插入到 root_meta_info.json 的最前面，使其在剪映中排第一。"""
    root_meta_path = os.path.join(draft_root, "root_meta_info.json")
    now_ms = int(time.time() * 1000)
    draft_path_normalized = _normalize_jianying_path(draft_folder)

    # 读取已有的 root_meta_info 或创建新的
    root_meta: dict = {}
    if os.path.isfile(root_meta_path):
        try:
            with open(root_meta_path, "r", encoding="utf-8") as f:
                root_meta = json.load(f)
        except Exception:
            root_meta = {}

    # 确保 all_draft_store 列表存在
    if "all_draft_store" not in root_meta or not isinstance(root_meta.get("all_draft_store"), list):
        root_meta["all_draft_store"] = []

    draft_store: list = root_meta["all_draft_store"]

    # 移除同名旧条目（如果有）
    draft_store[:] = [
        entry for entry in draft_store
        if entry.get("draft_fold_path", "") != draft_path_normalized
        and entry.get("draft_name", "") != draft_name
    ]

    # 构建新条目并插入到列表最前面
    new_entry = {
        "draft_fold_path": draft_path_normalized,
        "draft_id": str(uuid.uuid4()).upper(),
        "draft_name": draft_name,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": _normalize_jianying_path(draft_root),
        "tm_draft_create": now_ms,
        "tm_draft_modified": now_ms,
        "tm_duration": 0,
    }
    draft_store.insert(0, new_entry)

    try:
        with open(root_meta_path, "w", encoding="utf-8") as f:
            json.dump(root_meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 非关键操作，写入失败不影响导出

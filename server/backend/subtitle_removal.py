import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional


SUPPORTED_INPAINT_MODES = {"sttn-auto", "sttn-det", "lama", "opencv"}
DEFAULT_INPAINT_MODE = "sttn-auto"
MISSING_ENGINE_MESSAGE = "去字幕引擎缺失，请重新安装完整版本"
MAX_EXPAND_PIXELS = 120


class SubtitleRemovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class VsrRuntime:
    root_dir: Path
    python_path: Path
    main_path: Path


ProgressCallback = Callable[[int, Optional[str]], Awaitable[None]]


def _number(value: Any, label: str) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception as error:
        raise SubtitleRemovalError(f"字幕区域无效：{label}") from error
    if parsed != parsed:
        raise SubtitleRemovalError(f"字幕区域无效：{label}")
    return parsed


def _optional_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if parsed != parsed:
        return None
    return parsed


def normalize_expand_pixels(value: Any) -> int:
    parsed = _optional_number(value)
    if parsed is None:
        return 0
    return max(0, min(MAX_EXPAND_PIXELS, round(parsed)))


def normalize_region(
    region: Dict[str, Any],
    *,
    video_width: Optional[int] = None,
    video_height: Optional[int] = None,
    expand_pixels: Any = 0,
) -> Dict[str, int]:
    if not isinstance(region, dict):
        raise SubtitleRemovalError("字幕区域无效")

    x = round(_number(region.get("x"), "x"))
    y = round(_number(region.get("y"), "y"))
    width = round(_number(region.get("width"), "width"))
    height = round(_number(region.get("height"), "height"))
    if width <= 0 or height <= 0:
        raise SubtitleRemovalError("字幕区域无效：宽高必须大于 0")

    expand = normalize_expand_pixels(expand_pixels)
    left = x - expand
    top = y - expand
    right = x + width + expand
    bottom = y + height + expand

    x = max(0, left)
    y = max(0, top)
    width = right - x
    height = bottom - y
    if video_width and video_width > 0:
        x = min(x, video_width - 1)
        width = min(width, video_width - x)
    if video_height and video_height > 0:
        y = min(y, video_height - 1)
        height = min(height, video_height - y)
    if width <= 0 or height <= 0:
        raise SubtitleRemovalError("字幕区域无效：超出视频范围")

    return {"x": x, "y": y, "width": width, "height": height}


def region_to_vsr_coords(region: Dict[str, int]) -> List[int]:
    return [
        int(region["y"]),
        int(region["y"]) + int(region["height"]),
        int(region["x"]),
        int(region["x"]) + int(region["width"]),
    ]


def normalize_inpaint_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SUPPORTED_INPAINT_MODES else DEFAULT_INPAINT_MODE


def normalize_preserve_audio(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def normalize_time_range(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"mode": "full", "startSeconds": 0, "endSeconds": None}
    mode = str(value.get("mode") or value.get("scope") or "full").strip().lower()
    if mode not in {"full", "custom"}:
        mode = "full"
    start = max(0.0, _optional_number(value.get("startSeconds") or value.get("start_seconds")) or 0.0)
    end = _optional_number(value.get("endSeconds") or value.get("end_seconds"))
    if end is not None:
        end = max(0.0, end)
    if mode == "full":
        return {"mode": "full", "startSeconds": 0, "endSeconds": None}
    if end is not None and end <= start:
        end = None
    return {"mode": "custom", "startSeconds": start, "endSeconds": end}


def _format_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def default_vsr_candidate_dirs() -> List[Path]:
    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        project_root / "tools" / "vsr" / "windows-x64-cpu",
    ]

    backend_resource_dir = os.environ.get("LIBAI_BACKEND_RESOURCE_DIR")
    if backend_resource_dir:
        candidates.append(Path(backend_resource_dir).resolve().parent / "tools" / "vsr" / "windows-x64-cpu")

    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir.parent.parent / "tools" / "vsr" / "windows-x64-cpu")
    return candidates


def _runtime_from_root(root: Path) -> Optional[VsrRuntime]:
    root = root.resolve()
    python_candidates = [
        root / "python.exe",
        root / "python" / "python.exe",
        root / "runtime" / "python.exe",
        root / ".venv" / "Scripts" / "python.exe",
    ]
    main_candidates = [
        root / "backend" / "main.py",
        root / "video-subtitle-remover" / "backend" / "main.py",
    ]
    python_path = next((item for item in python_candidates if item.exists()), None)
    main_path = next((item for item in main_candidates if item.exists()), None)
    if python_path and main_path:
        return VsrRuntime(root_dir=root, python_path=python_path, main_path=main_path)
    return None


def resolve_vsr_runtime(candidate_dirs: Optional[Iterable[Path]] = None) -> VsrRuntime:
    env_dir = os.environ.get("LIBAI_VSR_DIR")
    candidates: List[Path] = []
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(candidate_dirs or default_vsr_candidate_dirs())
    for candidate in candidates:
        runtime = _runtime_from_root(Path(candidate))
        if runtime:
            return runtime
    raise SubtitleRemovalError(MISSING_ENGINE_MESSAGE)


def build_vsr_command(
    runtime: VsrRuntime,
    *,
    input_path: Path,
    output_path: Path,
    coords: List[int],
    inpaint_mode: str,
    time_range: Optional[Dict[str, Any]] = None,
    preserve_audio: Any = True,
) -> List[str]:
    command = [
        str(runtime.python_path),
        str(runtime.main_path),
        "-i",
        str(input_path),
        "-o",
        str(output_path),
        "-c",
        str(coords[0]),
        str(coords[1]),
        str(coords[2]),
        str(coords[3]),
        "--inpaint-mode",
        normalize_inpaint_mode(inpaint_mode),
    ]
    normalized_time = normalize_time_range(time_range)
    if normalized_time["mode"] == "custom":
        if normalized_time["startSeconds"] > 0:
            command.extend(["--start-second", _format_seconds(normalized_time["startSeconds"])])
        if normalized_time["endSeconds"] is not None:
            command.extend(["--end-second", _format_seconds(normalized_time["endSeconds"])])
    if not normalize_preserve_audio(preserve_audio):
        command.append("--no-audio")
    return command


def subtitle_output_path(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"subtitle_removed_{uuid.uuid4().hex[:12]}.mp4"


def build_output_payload(
    *,
    output_path: Path,
    source_asset_id: str = "",
    source_node_id: str = "",
    title: str = "",
    inpaint_mode: str = DEFAULT_INPAINT_MODE,
    region: Optional[Dict[str, int]] = None,
    expanded_region: Optional[Dict[str, int]] = None,
    expand_pixels: Any = 0,
    time_range: Optional[Dict[str, Any]] = None,
    preserve_audio: Any = True,
) -> Dict[str, Any]:
    clean_title = str(title or "视频").strip() or "视频"
    if not clean_title.endswith("去字幕"):
        clean_title = f"{clean_title} · 去字幕"
    return {
        "provider": "local.vsr",
        "providerModelId": "local.vsr.cpu",
        "providerModelName": "VSR CPU",
        "status": "completed",
        "assetKind": "video",
        "source": "video.subtitle.remove",
        "localPath": str(output_path),
        "filename": output_path.name,
        "mime": "video/mp4",
        "engine": "VSR CPU",
        "inpaintMode": normalize_inpaint_mode(inpaint_mode),
        "region": region or {},
        "expandedRegion": expanded_region or region or {},
        "expandPixels": normalize_expand_pixels(expand_pixels),
        "timeRange": normalize_time_range(time_range),
        "preserveAudio": normalize_preserve_audio(preserve_audio),
        "sourceAssetId": source_asset_id,
        "sourceNodeId": source_node_id,
        "title": clean_title,
    }


async def run_vsr_command(command: List[str], cwd: Path, progress: ProgressCallback) -> None:
    await progress(22, "vsr-start")
    last_progress = {"value": 22}

    async def emit_stage(text: str) -> None:
        lower = text.lower()
        next_value = None
        stage = None
        if "detecting subtitles" in lower or "subtitle finding" in lower:
            next_value, stage = 36, "vsr-detecting"
        elif "subtitle detected" in lower or "subtitle timeline" in lower:
            next_value, stage = 48, "vsr-detected"
        elif "removing subtitles" in lower or "processing frame" in lower or "processing:" in lower:
            next_value, stage = 66, "vsr-inpainting"
        elif "audio" in lower:
            next_value, stage = 82, "vsr-audio"
        elif "complete" in lower or "output saved" in lower or "finished" in lower:
            next_value, stage = 88, "vsr-finished"
        if next_value is not None and next_value > last_progress["value"]:
            last_progress["value"] = next_value
            await progress(next_value, stage)

    async def read_stream(stream: Optional[asyncio.StreamReader]) -> List[str]:
        lines: List[str] = []
        if stream is None:
            return lines
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                lines.append(text)
                await emit_stage(text)
        return lines

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_lines, stderr_lines = await asyncio.gather(
        read_stream(process.stdout),
        read_stream(process.stderr),
    )
    await process.wait()
    if process.returncode != 0:
        detail = "\n".join(stderr_lines or stdout_lines).strip()
        raise SubtitleRemovalError(detail or "去字幕处理失败")


async def remove_subtitles_from_video(
    *,
    input_path: Path,
    cache_dir: Path,
    region: Dict[str, Any],
    source_asset_id: str = "",
    source_node_id: str = "",
    title: str = "",
    inpaint_mode: str = DEFAULT_INPAINT_MODE,
    expand_pixels: Any = 0,
    time_range: Optional[Dict[str, Any]] = None,
    preserve_audio: Any = True,
    video_width: Optional[int] = None,
    video_height: Optional[int] = None,
    progress: ProgressCallback,
) -> Dict[str, Any]:
    await progress(15, "vsr-runtime")
    runtime = resolve_vsr_runtime()
    selected_region = normalize_region(region, video_width=video_width, video_height=video_height)
    normalized_expand = normalize_expand_pixels(expand_pixels)
    expanded_region = normalize_region(
        selected_region,
        video_width=video_width,
        video_height=video_height,
        expand_pixels=normalized_expand,
    )
    normalized_time = normalize_time_range(time_range)
    normalized_audio = normalize_preserve_audio(preserve_audio)
    coords = region_to_vsr_coords(expanded_region)
    mode = normalize_inpaint_mode(inpaint_mode)
    output_path = subtitle_output_path(cache_dir)
    command = build_vsr_command(
        runtime,
        input_path=input_path,
        output_path=output_path,
        coords=coords,
        inpaint_mode=mode,
        time_range=normalized_time,
        preserve_audio=normalized_audio,
    )
    await run_vsr_command(command, runtime.root_dir, progress)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise SubtitleRemovalError("去字幕处理失败")
    await progress(90, "vsr-output-ready")
    return build_output_payload(
        output_path=output_path,
        source_asset_id=source_asset_id,
        source_node_id=source_node_id,
        title=title,
        inpaint_mode=mode,
        region=selected_region,
        expanded_region=expanded_region,
        expand_pixels=normalized_expand,
        time_range=normalized_time,
        preserve_audio=normalized_audio,
    )

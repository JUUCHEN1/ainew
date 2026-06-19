import asyncio
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional


ProgressCallback = Callable[[int, Optional[str]], Awaitable[None]]
DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0


class VideoAnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoAnalysisOptions:
    max_frames: int = 8
    max_dimension: int = 1280
    scene_detection_strength: int = 60
    scene_threshold: float = 0.228
    min_scene_gap_sec: float = 0.8
    strength_preset: str = "medium"
    extraction_strategy: str = "scene_representative"


SCENE_STRENGTH_PRESET_VALUES = {
    "low": 30,
    "medium": 60,
    "high": 85,
}

EXTRACTION_STRATEGIES = {
    "scene_representative",
    "motion_peak",
    "uniform_fill",
    "mixed",
}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def normalize_strength_preset(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"low", "medium", "high", "custom"}:
        return normalized
    return "medium"


def normalize_extraction_strategy(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in EXTRACTION_STRATEGIES else "scene_representative"


def scene_threshold_from_strength(strength: Any) -> float:
    """Map UI strength to FFmpeg scene threshold.

    FFmpeg's scene score threshold is inverse to user-facing sensitivity:
    higher user strength means more scene cuts are detected, so the threshold is lower.
    """
    value = _as_int(strength, 60, 0, 100)
    threshold = 0.45 - ((value / 100) * 0.37)
    return round(max(0.08, min(0.45, threshold)), 3)


def normalize_video_analysis_options(payload: Optional[Dict[str, Any]] = None) -> VideoAnalysisOptions:
    data = payload or {}
    scene_detect = _as_dict(data.get("sceneDetect") or data.get("scene_detect"))
    preset = normalize_strength_preset(_first_defined(
        scene_detect.get("strengthPreset"),
        scene_detect.get("strength_preset"),
        data.get("strengthPreset"),
        data.get("sceneDetectionPreset"),
        data.get("scene_detection_preset"),
    ))
    preset_strength = SCENE_STRENGTH_PRESET_VALUES.get(preset, 60)
    strength = _as_int(
        _first_defined(
            scene_detect.get("strength"),
            scene_detect.get("sceneDetectionStrength"),
            scene_detect.get("sceneDetectStrength"),
            data.get("sceneDetectionStrength"),
            data.get("scene_detect_strength"),
            data.get("sceneStrength"),
        ),
        preset_strength,
        0,
        100,
    )
    explicit_threshold = _first_defined(
        scene_detect.get("threshold"),
        scene_detect.get("sceneThreshold"),
        data.get("sceneThreshold"),
        data.get("scene_detection_threshold"),
    )
    threshold = (
        round(_as_float(explicit_threshold, scene_threshold_from_strength(strength), 0.08, 0.6), 3)
        if explicit_threshold is not None
        else scene_threshold_from_strength(strength)
    )
    max_frames = _as_int(
        _first_defined(scene_detect.get("maxFrames"), scene_detect.get("max_frames"), data.get("maxFrames"), data.get("max_frames")),
        8,
        1,
        32,
    )
    min_scene_gap_sec = _as_float(
        _first_defined(
            scene_detect.get("minSceneDuration"),
            scene_detect.get("minSceneGapSec"),
            scene_detect.get("min_scene_gap_sec"),
            data.get("minSceneDuration"),
            data.get("minSceneGapSec"),
            data.get("min_scene_gap_sec"),
        ),
        0.8,
        0.2,
        10.0,
    )
    strategy = normalize_extraction_strategy(_first_defined(
        scene_detect.get("strategy"),
        scene_detect.get("extractionStrategy"),
        scene_detect.get("extraction_strategy"),
        data.get("strategy"),
        data.get("extractionStrategy"),
        data.get("extraction_strategy"),
    ))
    return VideoAnalysisOptions(
        max_frames=max_frames,
        max_dimension=_as_int(data.get("maxDimension") or data.get("max_dimension"), 1280, 320, 2160),
        scene_detection_strength=strength,
        scene_threshold=threshold,
        min_scene_gap_sec=min_scene_gap_sec,
        strength_preset=preset,
        extraction_strategy=strategy,
    )


def resolve_binary_path(name: str, env_key: str) -> str:
    explicit = os.environ.get(env_key)
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which(name)
    if found:
        return found
    raise VideoAnalysisError(f"未检测到 {name}，请安装 FFmpeg 或配置 {env_key}")


def binary_runtime_status(name: str, env_key: str) -> Dict[str, Any]:
    explicit = os.environ.get(env_key)
    if explicit and Path(explicit).exists():
        return {
            "name": name,
            "available": True,
            "source": "env",
            "path": explicit,
        }
    found = shutil.which(name)
    if found:
        return {
            "name": name,
            "available": True,
            "source": "path",
            "path": found,
        }
    return {
        "name": name,
        "available": False,
        "source": "missing",
        "path": explicit or "",
    }


def build_ffprobe_command(ffprobe_path: str, video_path: str) -> List[str]:
    return [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        video_path,
    ]


def build_scene_detect_command(ffmpeg_path: str, video_path: str, scene_threshold: float) -> List[str]:
    filter_expr = f"select='gt(scene,{scene_threshold:.3f})',metadata=print"
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-i",
        video_path,
        "-vf",
        filter_expr,
        "-an",
        "-f",
        "null",
        "-",
    ]


def build_extract_frame_command(
    ffmpeg_path: str,
    video_path: str,
    timestamp_sec: float,
    output_path: str,
    max_dimension: int,
) -> List[str]:
    scale_expr = (
        f"scale='if(gt(iw,ih),min({max_dimension},iw),-2)'"
        f":'if(gt(iw,ih),-2,min({max_dimension},ih))'"
    )
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(0, timestamp_sec):.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        scale_expr,
        "-q:v",
        "2",
        output_path,
    ]


def normalize_video_trim_range(
    payload: Optional[Dict[str, Any]] = None,
    duration: float = 0,
    min_duration: float = 0.25,
) -> Dict[str, float]:
    data = payload or {}
    video_duration = _as_float(duration, 0, 0, 24 * 60 * 60)
    if video_duration <= 0:
        raise VideoAnalysisError("视频时长读取失败，无法裁剪")
    start = _as_float(
        _first_defined(
            data.get("trimStartSec"),
            data.get("trim_start_sec"),
            data.get("startSec"),
            data.get("start"),
        ),
        0,
        0,
        video_duration,
    )
    end = _as_float(
        _first_defined(
            data.get("trimEndSec"),
            data.get("trim_end_sec"),
            data.get("endSec"),
            data.get("end"),
        ),
        video_duration,
        0,
        video_duration,
    )
    if end <= start:
        raise VideoAnalysisError("裁剪结束时间必须晚于开始时间")
    trimmed_duration = end - start
    if trimmed_duration < min_duration:
        raise VideoAnalysisError(f"裁剪片段至少需要 {min_duration:.2f} 秒")
    return {
        "start": _round(start, 3),
        "end": _round(end, 3),
        "duration": _round(trimmed_duration, 3),
    }


def build_trim_video_command(
    ffmpeg_path: str,
    video_path: str,
    output_path: str,
    start_sec: float,
    duration_sec: float,
) -> List[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(0, start_sec):.3f}",
        "-i",
        video_path,
        "-t",
        f"{max(0, duration_sec):.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        output_path,
    ]


async def run_command(command: List[str], timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SECONDS) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise VideoAnalysisError(f"执行文件不存在：{command[0]}") from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError as error:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise VideoAnalysisError("FFmpeg 视频解析超时") from error

    text = (stdout or b"").decode("utf-8", errors="ignore")
    err = (stderr or b"").decode("utf-8", errors="ignore")
    combined = "\n".join(item for item in (text, err) if item)
    if process.returncode != 0:
        raise VideoAnalysisError(combined.strip() or f"命令执行失败：{command[0]}")
    return combined


async def probe_video_metadata(video_path: Path, ffprobe_path: str) -> Dict[str, Any]:
    output = await run_command(build_ffprobe_command(ffprobe_path, str(video_path)), timeout_sec=DEFAULT_WAIT_TIMEOUT_SECONDS)
    try:
        payload = json.loads(output)
    except Exception as error:
        raise VideoAnalysisError("ffprobe 元数据解析失败") from error

    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}

    def number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except Exception:
            return None
        return parsed if math.isfinite(parsed) else None

    duration = number(fmt.get("duration")) or number(video_stream.get("duration")) or 0
    return {
        "durationSeconds": round(duration, 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "codec": video_stream.get("codec_name") or "",
        "formatName": fmt.get("format_name") or "",
        "bitRate": fmt.get("bit_rate") or video_stream.get("bit_rate") or "",
    }


def parse_scene_detection_output(output: str) -> List[Dict[str, float]]:
    cuts: List[Dict[str, float]] = []
    current_time: Optional[float] = None

    def try_append(score: float) -> None:
        nonlocal current_time
        if current_time is None:
            return
        if not any(abs(item["time"] - current_time) < 0.001 for item in cuts):
            cuts.append({"time": round(current_time, 3), "score": round(score, 6)})
        current_time = None

    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        time_match = re.search(r"pts_time[:=]\s*([0-9]+(?:\.[0-9]+)?)", line)
        if time_match:
            current_time = float(time_match.group(1))
        score_match = re.search(r"lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)", line)
        if score_match:
            try_append(float(score_match.group(1)))
    return cuts


def _filter_cuts(cuts: List[Dict[str, float]], duration: float, min_scene_gap_sec: float) -> List[Dict[str, float]]:
    result: List[Dict[str, float]] = []
    for cut in sorted(cuts, key=lambda item: (item.get("time", 0), -item.get("score", 0))):
        time = float(cut.get("time") or 0)
        if time <= 0.08 or time >= max(0.1, duration - 0.08):
            continue
        if result and abs(time - result[-1]["time"]) < min_scene_gap_sec:
            if float(cut.get("score") or 0) > float(result[-1].get("score") or 0):
                result[-1] = {"time": round(time, 3), "score": round(float(cut.get("score") or 0), 6)}
            continue
        result.append({"time": round(time, 3), "score": round(float(cut.get("score") or 0), 6)})
    return result


def _round(value: Any, digits: int = 3) -> float:
    try:
        number = float(value)
    except Exception:
        number = 0
    if not math.isfinite(number):
        number = 0
    return round(number, digits)


def build_scene_segments(duration: float, cuts: List[Dict[str, float]]) -> List[Dict[str, Any]]:
    safe_duration = max(0.1, float(duration or 0))
    cut_times = [max(0, min(safe_duration, float(cut.get("time") or 0))) for cut in cuts]
    boundaries = [0, *sorted(set(cut_times)), safe_duration]
    scenes: List[Dict[str, Any]] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = max(start, boundaries[index + 1])
        if end - start <= 0.08:
            continue
        cut = cuts[index - 1] if index > 0 and index - 1 < len(cuts) else None
        representative = start + ((end - start) * 0.5)
        scenes.append({
            "id": f"scene_{index + 1}",
            "index": index,
            "start": _round(start),
            "end": _round(end),
            "duration": _round(end - start),
            "representativeTime": _round(representative),
            "changeScore": _round(cut.get("score") if cut else 0, 6),
            "timeRange": f"{_round(start, 1)}s-{_round(end, 1)}s",
            "userSelected": True,
        })
    return scenes


def select_uniform_frame_times(duration: float, max_frames: int) -> List[float]:
    count = max(1, max_frames)
    safe_duration = max(0.1, float(duration or 0))
    return [
        _round((safe_duration * (index + 0.5)) / count)
        for index in range(count)
    ]


def select_representative_frame_times(
    scenes: List[Dict[str, Any]],
    duration: float,
    max_frames: int,
) -> List[float]:
    if not scenes:
        return select_uniform_frame_times(duration, max_frames)
    limit = max(1, max_frames)
    first_scene = scenes[0]
    selected_scenes = [first_scene]
    remaining = [
        scene
        for scene in scenes[1:]
        if scene.get("id") != first_scene.get("id")
    ]
    selected_scenes.extend(sorted(
        remaining,
        key=lambda scene: (
            -float(scene.get("duration") or 0),
            float(scene.get("start") or 0),
        ),
    )[:max(0, limit - 1)])
    times = [_round(scene.get("representativeTime")) for scene in selected_scenes]
    if len(times) < limit:
        for index in range(limit):
            candidate = _round((max(0.1, duration) * (index + 0.5)) / limit)
            min_gap = max(0.25, max(0.1, duration) / max(6, limit * 3))
            if all(abs(candidate - existing) >= min_gap for existing in times):
                times.append(candidate)
            if len(times) >= limit:
                break
    return sorted(times[:limit])


def _unique_frame_times(times: List[float], duration: float, max_frames: int) -> List[float]:
    limit = max(1, max_frames)
    safe_duration = max(0.1, float(duration or 0))
    result: List[float] = []
    min_gap = max(0.08, safe_duration / max(20, limit * 5))
    for value in times:
        time = _round(max(0, min(safe_duration, float(value or 0))))
        if all(abs(time - existing) >= min_gap for existing in result):
            result.append(time)
        if len(result) >= limit:
            break
    return sorted(result)


def select_motion_peak_frame_times(
    scenes: List[Dict[str, Any]],
    duration: float,
    max_frames: int,
) -> List[float]:
    if not scenes:
        return select_uniform_frame_times(duration, max_frames)
    peak_scenes = sorted(
        scenes,
        key=lambda scene: (
            -float(scene.get("changeScore") or 0),
            float(scene.get("start") or 0),
        ),
    )
    times: List[float] = []
    for scene in peak_scenes:
        start = float(scene.get("start") or 0)
        scene_duration = max(0.1, float(scene.get("duration") or 0))
        if float(scene.get("changeScore") or 0) > 0:
            times.append(_round(start + (scene_duration * 0.2)))
        else:
            times.append(_round(scene.get("representativeTime")))
    if len(times) < max_frames:
        times.extend(select_uniform_frame_times(duration, max_frames))
    return _unique_frame_times(times, duration, max_frames)


def select_frame_times_by_strategy(
    scenes: List[Dict[str, Any]],
    duration: float,
    max_frames: int,
    strategy: str = "scene_representative",
) -> List[float]:
    normalized_strategy = normalize_extraction_strategy(strategy)
    if normalized_strategy == "uniform_fill":
        return select_uniform_frame_times(duration, max_frames)
    if normalized_strategy == "motion_peak":
        return select_motion_peak_frame_times(scenes, duration, max_frames)
    if normalized_strategy == "mixed":
        representative = select_representative_frame_times(scenes, duration, max_frames)
        motion_peak = select_motion_peak_frame_times(scenes, duration, max_frames)
        uniform = select_uniform_frame_times(duration, max_frames)
        mixed: List[float] = []
        for index in range(max(1, max_frames)):
            for source in (representative, motion_peak, uniform):
                if index < len(source):
                    mixed.append(source[index])
            if len(_unique_frame_times(mixed, duration, max_frames)) >= max_frames:
                break
        return _unique_frame_times(mixed, duration, max_frames)
    return select_representative_frame_times(scenes, duration, max_frames)


def build_video_analysis_prompt(result: Dict[str, Any]) -> str:
    scenes = result.get("scenes") if isinstance(result.get("scenes"), list) else []
    scene_lines = [
        f"{index + 1}. {scene.get('timeRange')}，代表帧 {scene.get('representativeTime')}s，变化强度 {scene.get('changeScore')}"
        for index, scene in enumerate(scenes[:24])
    ]
    reference_intent = result.get("referenceIntent") or "structure_reference"
    return "\n".join([
        "请基于后端 FFmpeg 对参考视频的镜头检测结果，提炼可商业化落地的分镜生产信息。",
        f"参考意图：{reference_intent}",
        f"视频时长：{result.get('durationSeconds')}s；场景段：{len(scenes)}；关键帧：{len(result.get('frames') or [])}",
        "需要输出：镜头节奏、运镜语言、构图层次、可复用的场景规则、连续性规则，以及后续图片/视频生成节点可直接使用的提示词方向。",
        "检测到的场景段：",
        *scene_lines,
    ]).strip()


async def analyze_video_reference(
    video_path: Path,
    output_dir: Path,
    payload: Optional[Dict[str, Any]] = None,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    data = payload or {}
    source = Path(video_path)
    if not source.exists() or not source.is_file():
        raise VideoAnalysisError("参考视频文件不存在")

    options = normalize_video_analysis_options(data)
    ffprobe_path = resolve_binary_path("ffprobe", "LIBAI_FFPROBE_PATH")
    ffmpeg_path = resolve_binary_path("ffmpeg", "LIBAI_FFMPEG_PATH")
    output_dir.mkdir(parents=True, exist_ok=True)

    async def report(value: int, stage: str) -> None:
        if progress:
            await progress(value, stage)

    await report(10, "ffprobe-metadata")
    metadata = await probe_video_metadata(source, ffprobe_path)
    duration = float(metadata.get("durationSeconds") or 0)
    if duration <= 0:
        raise VideoAnalysisError("视频时长读取失败")

    await report(24, "ffmpeg-scene-detect")
    detection_output = await run_command(
        build_scene_detect_command(ffmpeg_path, str(source), options.scene_threshold),
        timeout_sec=DEFAULT_WAIT_TIMEOUT_SECONDS,
    )
    raw_cuts = parse_scene_detection_output(detection_output)
    cuts = _filter_cuts(raw_cuts, duration, options.min_scene_gap_sec)
    scenes = build_scene_segments(duration, cuts)
    frame_times = select_frame_times_by_strategy(
        scenes,
        duration,
        options.max_frames,
        options.extraction_strategy,
    )

    frames: List[Dict[str, Any]] = []
    for index, time_sec in enumerate(frame_times):
        await report(35 + int((index / max(1, len(frame_times))) * 50), "ffmpeg-extract-frame")
        frame_path = output_dir / f"video-frame-{index + 1:03d}-{uuid.uuid4().hex[:8]}.jpg"
        await run_command(
            build_extract_frame_command(
                ffmpeg_path,
                str(source),
                float(time_sec),
                str(frame_path),
                options.max_dimension,
            ),
            timeout_sec=DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        scene = next(
            (
                item
                for item in scenes
                if float(item.get("start") or 0) <= float(time_sec) <= float(item.get("end") or 0)
            ),
            None,
        )
        frames.append({
            "index": index,
            "path": str(frame_path),
            "timestampSec": _round(time_sec),
            "sceneIndex": scene.get("index") if scene else None,
            "sceneId": scene.get("id") if scene else None,
            "userSelected": True,
        })

    for scene in scenes:
        frame = next((item for item in frames if item.get("sceneIndex") == scene.get("index")), None)
        if frame:
            scene["frameIndex"] = frame["index"]

    result: Dict[str, Any] = {
        "status": "ready",
        "source": "backend.ffmpeg",
        "referenceIntent": data.get("referenceIntent") or data.get("reference_intent") or "structure_reference",
        "durationSeconds": _round(duration),
        "duration": f"{_round(duration, 1)}s",
        "width": metadata.get("width") or 0,
        "height": metadata.get("height") or 0,
        "codec": metadata.get("codec") or "",
        "frameCount": len(frames),
        "sceneCount": len(scenes),
        "sceneDetectStrength": options.scene_detection_strength,
        "sceneDetectionStrength": options.scene_detection_strength,
        "sceneDetectionThreshold": options.scene_threshold,
        "sceneDetect": {
            "threshold": options.scene_threshold,
            "strengthPreset": options.strength_preset,
            "minSceneDuration": options.min_scene_gap_sec,
            "maxFrames": options.max_frames,
            "strategy": options.extraction_strategy,
        },
        "scenes": scenes,
        "frames": frames,
        "frameCandidates": frames,
        "analysis": {
            "method": "ffmpeg-scene-detection",
            "sampleCount": len(frame_times),
            "cutCount": len(cuts),
            "rawCutCount": len(raw_cuts),
            "threshold": options.scene_threshold,
            "selectedFrameTimes": frame_times,
            "minSceneGapSec": options.min_scene_gap_sec,
            "minSceneDuration": options.min_scene_gap_sec,
            "strategy": options.extraction_strategy,
        },
    }
    result["analysisPrompt"] = build_video_analysis_prompt(result)
    await report(92, "video-analysis-ready")
    return result


async def trim_video_reference(
    video_path: Path,
    output_dir: Path,
    payload: Optional[Dict[str, Any]] = None,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    data = payload or {}
    source = Path(video_path)
    if not source.exists() or not source.is_file():
        raise VideoAnalysisError("参考视频文件不存在")

    ffprobe_path = resolve_binary_path("ffprobe", "LIBAI_FFPROBE_PATH")
    ffmpeg_path = resolve_binary_path("ffmpeg", "LIBAI_FFMPEG_PATH")
    output_dir.mkdir(parents=True, exist_ok=True)

    async def report(value: int, stage: str) -> None:
        if progress:
            await progress(value, stage)

    await report(10, "ffprobe-trim-source")
    source_metadata = await probe_video_metadata(source, ffprobe_path)
    trim = normalize_video_trim_range(data, float(source_metadata.get("durationSeconds") or 0))
    target = output_dir / f"trimmed-reference-{uuid.uuid4().hex[:10]}.mp4"

    await report(32, "ffmpeg-trim-video")
    await run_command(
        build_trim_video_command(
            ffmpeg_path,
            str(source),
            str(target),
            trim["start"],
            trim["duration"],
        ),
        timeout_sec=DEFAULT_WAIT_TIMEOUT_SECONDS,
    )
    if not target.exists() or not target.is_file() or target.stat().st_size <= 0:
        raise VideoAnalysisError("裁剪视频输出为空")

    await report(86, "ffprobe-trim-output")
    output_metadata = await probe_video_metadata(target, ffprobe_path)
    await report(94, "video-trim-ready")
    return {
        "status": "ready",
        "source": "backend.ffmpeg.trim",
        "path": str(target),
        "filename": target.name,
        "mime": "video/mp4",
        "trimStartSec": trim["start"],
        "trimEndSec": trim["end"],
        "trimDurationSec": trim["duration"],
        "durationSeconds": output_metadata.get("durationSeconds") or trim["duration"],
        "width": output_metadata.get("width") or source_metadata.get("width") or 0,
        "height": output_metadata.get("height") or source_metadata.get("height") or 0,
        "codec": output_metadata.get("codec") or "",
        "sourceDurationSeconds": source_metadata.get("durationSeconds") or 0,
    }

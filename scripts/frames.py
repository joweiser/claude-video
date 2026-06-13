#!/usr/bin/env python3
"""Probe video metadata and extract frames with content-aware sampling.

Default mode is scene-aware: keep a frame on a scene change OR after a minimum
gap ("temporal floor") since the last kept frame. A duration -> (max_frames,
temporal_floor) table caps total output and guarantees coverage on static
content. A uniform-fps path is available by using the --fps parameter.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_FPS = 2.0

# Claude's Read tool rejects images whose width or height exceeds 2000px.
# Keep below the actual ceiling so we're robust to off-by-one rounding inside
# ffmpeg's scaler.
READ_TOOL_MAX_EDGE = 1998

# Scene-detect threshold for ffmpeg's `scene` metric. 0.3 = sensitive (catches
# subtle changes), 0.4 = conservative (only obvious cuts). 0.35 is the
# literature default; revisit if validation surfaces an obvious miscalibration.
# Could be exposed as a CLI flag to allow the skill making adjustments.
SCENE_THRESHOLD = 0.35

# stderr line from ffmpeg's `showinfo` filter; one per emitted frame.
_SHOWINFO_PTS_RE = re.compile(r"Parsed_showinfo.*?\bpts_time:(\d+(?:\.\d+)?)")


def _clamp_fps(fps: float, duration_seconds: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration_seconds))))
    return fps, target


def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def get_metadata(video_path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(Path(video_path).resolve()),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
        "has_audio": audio_stream is not None,
    }


def auto_fps(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps that targets a sensible frame budget for full-video scans."""
    if duration_seconds <= 0:
        return 1.0, 1

    if duration_seconds <= 30:
        target = min(max_frames, max(12, int(round(duration_seconds))))
    elif duration_seconds <= 60:
        target = min(max_frames, 40)
    elif duration_seconds <= 180:  # 3 min
        target = min(max_frames, 60)
    elif duration_seconds <= 600:  # 10 min
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def auto_fps_focus(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges — they are zooming in for detail."""
    if duration_seconds <= 0:
        return min(MAX_FPS, 2.0), 2

    if duration_seconds <= 5:
        target = min(max_frames, max(10, int(round(duration_seconds * 6))))
    elif duration_seconds <= 15:
        target = min(max_frames, max(30, int(round(duration_seconds * 4))))
    elif duration_seconds <= 30:
        target = min(max_frames, 60)
    elif duration_seconds <= 60:
        target = min(max_frames, 80)
    elif duration_seconds <= 180:
        target = max_frames
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def scene_budget(duration_seconds: float) -> tuple[int, float]:
    """Full-mode scene-aware budget: (max_frames_ceiling, temporal_floor_seconds).

    Used when ffmpeg picks frames via scene-detect OR temporal-floor (scene mode).
    The max_frames ceiling protects token budget; the temporal-floor guarantees
    coverage on static content where no scene cuts fire.
    """
    if duration_seconds <= 30:
        return (30, 1.0)
    if duration_seconds <= 60:
        return (40, 2.0)
    if duration_seconds <= 180:  # 3 min
        return (60, 3.0)
    if duration_seconds <= 600:  # 10 min
        return (80, 8.0)
    if duration_seconds <= 1800:  # 30 min
        return (100, 30.0)
    if duration_seconds <= 3600:  # 1 hr
        return (120, 60.0)
    if duration_seconds <= 7200:  # 2 hr
        return (150, 90.0)
    return (180, 120.0)


def scene_budget_focus(duration_seconds: float) -> tuple[int, float]:
    """Focus-mode scene-aware budget: denser temporal-floor than full mode.

    Once the user has zoomed in via --start/--end they want every meaningful
    frame, so the minimum gap between kept frames is tighter at every band.
    """
    if duration_seconds <= 5:
        return (30, 0.5)
    if duration_seconds <= 15:
        return (60, 1.0)
    if duration_seconds <= 30:
        return (60, 2.0)
    if duration_seconds <= 60:
        return (80, 3.0)
    if duration_seconds <= 180:
        return (100, 5.0)
    return (100, 10.0)


def _parse_pts_from_stderr(stderr: str) -> list[float]:
    """Pull pts_time values from ffmpeg's showinfo filter output (one per emitted frame)."""
    return [float(m.group(1)) for m in _SHOWINFO_PTS_RE.finditer(stderr)]


def compute_target_dims(
    src_w: int,
    src_h: int,
    requested_width: int,
    max_edge: int = READ_TOOL_MAX_EDGE,
) -> tuple[int, int, bool]:
    """Pick output (width, height) so neither edge exceeds max_edge.

    Preserves source aspect ratio. Returns (w, h, clamped) where clamped=True
    means the requested width was reduced to keep the longer edge under
    max_edge. Both dimensions are forced even (h264/libx264 require it).

    Falls back to (requested_width, -2, False) when source dims are unknown —
    -2 tells ffmpeg's scale filter to pick an even height matching aspect.
    """
    if src_w <= 0 or src_h <= 0 or requested_width <= 0:
        return requested_width, -2, False

    aspect = src_w / src_h
    w = requested_width
    h = int(round(w / aspect))

    if w % 2:
        w -= 1
    if h % 2:
        h -= 1

    clamped = False
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / longest
        w = int(w * scale)
        h = int(h * scale)
        if w % 2:
            w -= 1
        if h % 2:
            h -= 1
        clamped = True

    return max(2, w), max(2, h), clamped


def extract(
    video_path: str,
    out_dir: Path,
    *,
    mode: str = "scene",
    fps: float | None = None,
    scene_threshold: float = SCENE_THRESHOLD,
    temporal_floor: float = 30.0,
    resolution: int = 1024,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    """Extract frames either scene-aware (default) or at uniform fps (legacy --fps).

    Scene mode: ffmpeg's `select` keeps frames on scene-change OR after a
    temporal-floor gap; first frame is always included.
    Uniform mode: legacy `fps={fps}` filter, identical to pre-3a behavior.

    In both modes, `showinfo` emits per-frame pts to stderr; timestamps come
    from those values instead of i/fps math (which is wrong as soon as frames
    are non-uniformly spaced, and even drifts on uniform sampling).
    """
    if mode not in ("scene", "uniform"):
        raise ValueError(f"extract() mode must be 'scene' or 'uniform', got {mode!r}")
    if mode == "uniform" and (fps is None or fps <= 0):
        raise ValueError("extract() mode='uniform' requires a positive fps")

    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    try:
        meta = get_metadata(video_path)
        src_w = meta.get("width") or 0
        src_h = meta.get("height") or 0
    except SystemExit:
        src_w, src_h = 0, 0

    target_w, target_h, clamped = compute_target_dims(src_w, src_h, resolution)

    if clamped:
        natural_h = int(round(resolution / (src_w / src_h))) if src_w and src_h else 0
        print(
            f"[watch] source {src_w}x{src_h} at requested width {resolution} would have "
            f"produced {resolution}x{natural_h} (Claude's Read tool rejects any edge "
            f">{READ_TOOL_MAX_EDGE}px). Clamped to {target_w}x{target_h}.",
            file=sys.stderr,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        # `info` (not `error`) so showinfo lines reach stderr. We parse them and
        # discard the surrounding muxer/codec noise.
        "-loglevel", "info",
        "-y",
    ]

    # -ss before -i = fast seek (keyframe-snap, good enough for preview frames).
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    scale_expr = (
        f"scale={target_w}:{target_h}" if target_h > 0
        else f"scale={target_w}:-2"
    )

    if mode == "scene":
        # `+` = logical OR in ffmpeg's select expression. Commas inside gt()/gte()
        # are escaped so they aren't parsed as filter-chain separators. eq(n,0)
        # guarantees the very first frame is kept (scene metric for frame 0 is 0).
        select_expr = (
            f"gt(scene\\,{scene_threshold})"
            f"+gte(t-prev_selected_t\\,{temporal_floor})"
            f"+eq(n\\,0)"
        )
        vf = f"select={select_expr},showinfo,{scale_expr}"
    else:
        vf = f"fps={fps},showinfo,{scale_expr}"

    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
        # Without -fps_mode vfr, ffmpeg's image2 muxer pads sparse `select` output
        # by writing the previous frame repeatedly to match a target framerate,
        # producing many duplicate JPEGs. vfr passes each filtered frame through
        # with its real PTS, one file per frame.
        "-fps_mode", "vfr",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # stderr now contains info-level noise too; surface the tail so the
        # actual error isn't drowned out.
        tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise SystemExit(f"ffmpeg frame extraction failed:\n{tail}")

    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    pts_values = _parse_pts_from_stderr(result.stderr)

    out: list[dict] = []
    for i, p in enumerate(frames):
        if i < len(pts_values):
            t = offset + pts_values[i]
        elif mode == "uniform" and fps:
            # Fallback for the unlikely case showinfo didn't emit for every frame.
            t = offset + (i / fps)
        else:
            # Scene mode + missing pts: best-effort, mark as offset only.
            t = offset
        out.append({"index": i, "timestamp_seconds": round(t, 2), "path": str(p)})
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: frames.py <video-path> <out-dir> [--fps F] [--resolution W] "
            "[--max-frames N] [--start T] [--end T]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    out = Path(sys.argv[2])
    args = sys.argv[3:]

    fps_override = None
    resolution = 1024
    max_frames_cli = None  # None = take from scene_budget table
    start_arg = None
    end_arg = None
    i = 0
    while i < len(args):
        if args[i] == "--fps":
            fps_override = float(args[i + 1]); i += 2
        elif args[i] == "--resolution":
            resolution = int(args[i + 1]); i += 2
        elif args[i] == "--max-frames":
            max_frames_cli = int(args[i + 1]); i += 2
        elif args[i] == "--start":
            start_arg = args[i + 1]; i += 2
        elif args[i] == "--end":
            end_arg = args[i + 1]; i += 2
        else:
            i += 1

    meta = get_metadata(video)
    start_sec = parse_time(start_arg)
    end_sec = parse_time(end_arg)
    full_duration = meta["duration_seconds"]

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)

    focused = start_sec is not None or end_sec is not None

    if fps_override is not None:
        # Legacy uniform-sampling escape hatch.
        mode = "uniform"
        fps = min(fps_override, MAX_FPS)
        # max_frames: keep prior default of 100 when user didn't supply one.
        max_frames = max_frames_cli if max_frames_cli is not None else 100
        target = max(1, int(round(fps * effective_duration)))
        scene_threshold_used: float | None = None
        temporal_floor_used: float | None = None
    else:
        mode = "scene"
        fps = None
        if focused:
            table_max, temporal_floor_used = scene_budget_focus(effective_duration)
        else:
            table_max, temporal_floor_used = scene_budget(effective_duration)
        # User --max-frames is an explicit cap; otherwise use the table.
        max_frames = min(max_frames_cli, table_max) if max_frames_cli is not None else table_max
        scene_threshold_used = SCENE_THRESHOLD
        target = None  # actual count only known after extraction

    frames = extract(
        video, out,
        mode=mode,
        fps=fps,
        scene_threshold=scene_threshold_used or SCENE_THRESHOLD,
        temporal_floor=temporal_floor_used or 30.0,
        resolution=resolution,
        max_frames=max_frames,
        start_seconds=start_sec,
        end_seconds=end_sec,
    )

    if target is None:
        target = len(frames)

    print(json.dumps(
        {
            "meta": meta,
            "fps": fps,
            "target": target,
            "focused": focused,
            "sampling": {
                "mode": mode,
                "scene_threshold": scene_threshold_used,
                "temporal_floor_seconds": temporal_floor_used,
                "max_frames": max_frames,
            },
            "frames": frames,
        },
        indent=2,
    ))

#!/usr/bin/env python3
"""/watch entry point: download video, extract frames, parse transcript.

Prints a markdown report to stdout listing frame paths + transcript. Claude
then Reads each frame path to see the video.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from download import DEFAULT_SUB_LANGS, download, is_url  # noqa: E402
from frames import (  # noqa: E402
    MAX_FPS,
    SCENE_THRESHOLD,
    extract,
    format_time,
    get_metadata,
    parse_time,
    scene_budget,
    scene_budget_focus,
)
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import load_api_key, transcribe_video  # noqa: E402


# Windows: Python defaults stdout/stderr to cp1252, which crashes on the
# em-dashes and arrows this script emits in titles and report markdown.
# Reconfigure to UTF-8 so downstream tools see what we intended.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


TRANSCRIPT_HEAD_LINES = 30
TRANSCRIPT_TAIL_LINES = 10


def _write_transcript_files(
    work: Path,
    segments: list[dict],
    transcript_source: str,
    transcript_text: str,
) -> tuple[Path, Path]:
    """Write transcript.json (machine-readable) and transcript.md (human) to the work dir."""
    transcript_json = work / "transcript.json"
    transcript_md = work / "transcript.md"
    transcript_json.write_text(
        json.dumps({"source": transcript_source, "segments": segments}, indent=2),
        encoding="utf-8",
    )
    transcript_md.write_text(transcript_text + "\n", encoding="utf-8")
    return transcript_json, transcript_md


def _abbreviated_transcript(segments: list[dict]) -> str:
    """First TRANSCRIPT_HEAD_LINES + last TRANSCRIPT_TAIL_LINES segments for the preview."""
    total = len(segments)
    if total <= TRANSCRIPT_HEAD_LINES + TRANSCRIPT_TAIL_LINES + 5:
        return format_transcript(segments)
    head = format_transcript(segments[:TRANSCRIPT_HEAD_LINES])
    tail = format_transcript(segments[-TRANSCRIPT_TAIL_LINES:])
    omitted = total - TRANSCRIPT_HEAD_LINES - TRANSCRIPT_TAIL_LINES
    return (
        f"{head}\n"
        f"... [{omitted} segments omitted — read transcript.md for full text] ...\n"
        f"{tail}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="watch",
        description="Download a video, extract auto-scaled frames, and surface the transcript.",
    )
    ap.add_argument("source", help="Video URL or local file path")
    ap.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Cap on frame count (default: per-duration table, up to 180 on >2hr videos)",
    )
    ap.add_argument("--resolution", type=int, default=1024, help="Frame width in pixels (default 1024)")
    ap.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Escape hatch to legacy uniform sampling (clamped to 2 fps). Disables scene-aware mode.",
    )
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable Whisper fallback. Report frames-only if no captions available.",
    )
    ap.add_argument(
        "--no-frames",
        action="store_true",
        help="Skip frame extraction. Useful for audio-only content (podcasts, interviews) — transcript-only output.",
    )
    ap.add_argument(
        "--whisper",
        choices=["elevenlabs", "groq", "openai"],
        default=None,
        help="Force a specific transcription backend. Default: prefer ElevenLabs, then Groq, then OpenAI.",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as one JSON object on stdout instead of markdown.",
    )
    ap.add_argument(
        "--sub-lang",
        type=str,
        default=None,
        help="Comma-separated subtitle languages to fetch (e.g. 'ko' or 'ja,en'). "
        "Default: English variants. Lets non-English videos use free captions instead of Whisper.",
    )
    args = ap.parse_args()

    if args.out_dir:
        work = Path(args.out_dir).expanduser().resolve()
    else:
        work = Path(tempfile.mkdtemp(prefix="watch-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[watch] working dir: {work}", file=sys.stderr)

    print(
        "[watch] downloading via yt-dlp…" if is_url(args.source) else "[watch] using local file…",
        file=sys.stderr,
    )
    dl = download(args.source, work / "download", sub_langs=args.sub_lang or DEFAULT_SUB_LANGS)
    video_path = dl["video_path"]

    meta = get_metadata(video_path)
    full_duration = meta["duration_seconds"]

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)

    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    if args.no_frames:
        sampling_mode = "none"
        fps: float | None = None
        temporal_floor: float | None = None
        target: int | None = 0
        max_frames = 0
        frames: list[dict] = []
        print("[watch] --no-frames set — skipping frame extraction", file=sys.stderr)
    else:
        if args.fps is not None:
            # Legacy uniform-sampling escape hatch (backward compat with --fps).
            sampling_mode = "uniform"
            fps = min(args.fps, MAX_FPS)
            max_frames = args.max_frames if args.max_frames is not None else 80
            target = max(1, int(round(fps * effective_duration)))
            temporal_floor = None
        else:
            sampling_mode = "scene"
            fps = None
            if focused:
                table_max, temporal_floor = scene_budget_focus(effective_duration)
            else:
                table_max, temporal_floor = scene_budget(effective_duration)
            # User --max-frames is an explicit cap; otherwise use the per-duration table.
            max_frames = min(args.max_frames, table_max) if args.max_frames is not None else table_max
            target = None  # actual count only known after extraction

        scope = (
            f"{format_time(effective_start)}-{format_time(effective_end)} ({effective_duration:.1f}s)"
            if focused else f"full {effective_duration:.1f}s"
        )
        if sampling_mode == "scene":
            print(
                f"[watch] extracting up to {max_frames} frames "
                f"(scene-aware, min {temporal_floor:.0f}s gap) over {scope}…",
                file=sys.stderr,
            )
        else:
            print(
                f"[watch] extracting ~{target} frames at {fps:.3f} fps (uniform) over {scope}…",
                file=sys.stderr,
            )

        frames = extract(
            video_path,
            work / "frames",
            mode=sampling_mode,
            fps=fps,
            scene_threshold=SCENE_THRESHOLD,
            temporal_floor=temporal_floor if temporal_floor is not None else 30.0,
            resolution=args.resolution,
            max_frames=max_frames,
            start_seconds=start_sec,
            end_seconds=end_sec,
        )

    transcript_segments: list[dict] = []
    transcript_text: str | None = None
    transcript_source: str | None = None
    if dl.get("subtitle_path"):
        try:
            all_segments = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)

    if not transcript_segments and not args.no_whisper:
        backend, api_key = load_api_key(args.whisper)
        if backend and api_key:
            try:
                all_segments, used_backend = transcribe_video(
                    video_path,
                    work / "audio.mp3",
                    backend=backend,
                    api_key=api_key,
                )
                transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
                transcript_text = format_transcript(transcript_segments)
                transcript_source = f"whisper ({used_backend})"
            except SystemExit as exc:
                print(f"[watch] whisper fallback failed: {exc}", file=sys.stderr)
        else:
            hint = (
                f"--whisper {args.whisper} was set but the matching API key is missing"
                if args.whisper else
                "no subtitles and no Whisper API key found"
            )
            setup_py = SCRIPT_DIR / "setup.py"
            print(
                f"[watch] {hint} — run `python3 {setup_py}` to enable the Whisper fallback",
                file=sys.stderr,
            )

    transcript_md_path: Path | None = None
    if transcript_segments and transcript_text is not None and transcript_source is not None:
        _, transcript_md_path = _write_transcript_files(
            work, transcript_segments, transcript_source, transcript_text,
        )

    info = dl.get("info") or {}

    if args.json:
        long_unfocused = not focused and full_duration > 600
        report = {
            "schema_version": "1.0.0",
            "source": {
                "kind": "url" if is_url(args.source) else "file",
                "raw": args.source,
                "url": info.get("url") if is_url(args.source) else None,
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "subtitle_path": dl.get("subtitle_path"),
            },
            "video": {
                "path": str(video_path),
                "duration_seconds": meta["duration_seconds"],
                "width": meta.get("width"),
                "height": meta.get("height"),
                "codec": meta.get("codec"),
                "size_bytes": meta["size_bytes"],
                "has_audio": meta["has_audio"],
            },
            "focus": {
                "applied": focused,
                "start_seconds": effective_start if focused else None,
                "end_seconds": effective_end if focused else None,
                "duration_seconds": effective_duration if focused else None,
            },
            "frames": {
                "count": len(frames),
                "fps": fps,
                "target": target,
                "max_frames": max_frames,
                "resolution_px": args.resolution,
                "mode": "focused" if focused else "full",
                "items": [
                    {"index": f["index"], "timestamp_seconds": f["timestamp_seconds"], "path": f["path"]}
                    for f in frames
                ],
            },
            "transcript": {
                "source": transcript_source,
                "filtered_to_focus": bool(focused and transcript_segments),
                "segments": [
                    {"start_seconds": s["start"], "end_seconds": s["end"], "text": s["text"]}
                    for s in transcript_segments
                ],
            },
            "warnings": (
                [{
                    "code": "long_unfocused_video",
                    "message": (
                        f"This is a {int(full_duration // 60)}-minute video. Frame coverage is sparse "
                        "at this length — accuracy degrades noticeably on anything over 10 minutes. "
                        "For better results, re-run with `--start HH:MM:SS --end HH:MM:SS` to zoom "
                        "into a specific section."
                    ),
                }]
                if long_unfocused
                else []
            ),
            "work_dir": str(work),
        }
        sys.stdout.buffer.write(
            (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        sys.stdout.flush()
        return 0

    print()
    print("# watch: video report")
    print()
    print(f"- **Source:** {args.source}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")
    if args.no_frames:
        print("- **Frames:** skipped (`--no-frames`)")
    else:
        scope_mode = "focused" if focused else "full"
        if sampling_mode == "scene":
            print(
                f"- **Frames:** {len(frames)} / {max_frames} max — "
                f"scene-aware sampling (min {temporal_floor:.0f}s between frames), {scope_mode} mode"
            )
        else:
            print(
                f"- **Frames:** {len(frames)} @ {fps:.3f} fps — "
                f"uniform sampling (--fps override), {scope_mode} mode"
            )
        print(f"- **Frame size:** {args.resolution}px wide")
    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
        if transcript_md_path is not None:
            print(f"- **Transcript file:** `{transcript_md_path}`")
    else:
        print("- **Transcript:** none available")

    if not focused and not args.no_frames and full_duration > 600:
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Warning:** This is a {mins}-minute video. Frame coverage is sparse at this length — "
            "accuracy degrades noticeably on anything over 10 minutes. For better results, "
            "re-run with `--start HH:MM:SS --end HH:MM:SS` to zoom into a specific section."
        )

    print()
    print("## Frames")
    print()
    if args.no_frames:
        print("_Skipped — `--no-frames` set._")
    else:
        print(f"Frames live at: `{work / 'frames'}`")
        print()
        print(
            "**Read each frame path below with the Read tool to view the image.** "
            "Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source video."
        )
        print()
        for frame in frames:
            print(f"- `{frame['path']}` (t={format_time(frame['timestamp_seconds'])})")

    print()
    print("## Transcript")
    print()
    if transcript_text and transcript_segments:
        label = transcript_source or "captions"
        scope_note = (
            f"Filtered to {format_time(effective_start)} → {format_time(effective_end)}. "
            if focused else ""
        )
        md_ref = f"`{transcript_md_path}`" if transcript_md_path else "transcript.md"
        print(
            f"_Source: {label}. {scope_note}{len(transcript_segments)} segments — "
            f"full text in_ {md_ref}_, preview below:_"
        )
        print()
        print("```")
        print(_abbreviated_transcript(transcript_segments))
        print("```")
    elif focused and dl.get("subtitle_path"):
        print(f"_No transcript lines fell inside {format_time(effective_start)} → {format_time(effective_end)}._")
    else:
        setup_py = SCRIPT_DIR / "setup.py"
        print(
            "_No transcript available — proceed with frames only. "
            "Captions were missing and the Whisper fallback was unavailable "
            "(no API key set, or `--no-whisper` was used). "
            f"Run `python3 {setup_py}` to enable Whisper, then re-run._"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

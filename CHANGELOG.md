# Changelog

All notable changes to `/watch` are documented here.

## [0.2.0] — 2026-06-13

### Changed
- **Readable high-res frames by default.** Default frame width raised 512 → 1024 px so on-screen text, slides, terminals, and code are legible without a manual `--resolution` bump, and the download quality cap raised 720p → 1080p so the source is sharp enough to benefit. The existing Read-tool clamp still applies — portrait sources are auto-sized so no edge exceeds 1998 px. Higher resolution ~quadruples image tokens per frame (lower with `--resolution 768`/`512` on long videos); bumping toward 1456 px helps dense code, but the API downsamples beyond ~1568 px. (Adopts the readable-frame intent of upstream [`3c9229a`](https://github.com/bradautomates/claude-video/commit/3c9229a2c19ed00e2e49808c1ef8716af4783743); its scene-detection was already present here in a better single-pass form, and its machine-specific Ollama frame classifier was intentionally not ported.)

### Fixed
- Frame dimensions could exceed Claude's Read tool 2000 px-per-edge cap for portrait sources at higher `--resolution` values. A 1320×2868 phone recording at `--resolution 1024` previously produced 1024×2224 frames that Read silently rejected, leaving Claude unable to see them. `frames.extract` now probes source dims via ffprobe and computes an explicit `W:H` so the longer edge stays ≤1998 px, preserving aspect ratio. A stderr warning fires when clamping triggers so the cause isn't opaque.
- Windows: `setup.py --check` reported yt-dlp as missing when it was pip-installed into a user `Scripts` directory not on PATH (common in git-bash sessions), printing a `pip install` hint that was a no-op. `_which` now falls back to probing `python -m yt_dlp --version` before declaring yt-dlp absent. Scoped to yt-dlp only; no behavior change on macOS/Linux or for the ffmpeg/ffprobe checks. (Backports the still-open upstream [#14](https://github.com/bradautomates/claude-video/pull/14); its UTF-8 half was already covered by this fork's broader console fix.)

### Added
- **Transcript-to-file**: `transcript.json` (machine-readable) and `transcript.md` (timestamped) are written to the working directory whenever a transcript is available (captions or Whisper). The report now shows only a head/tail preview (first 30 + last 10 segments) instead of the full text, saving tens of thousands of context tokens on long-video runs. Read `transcript.md` from the work dir when the full text is needed.
- **ElevenLabs Scribe** as preferred transcription backend (`scribe_v1`): word-level timestamps grouped into ~8 s segments on sentence boundaries; higher accuracy at comparable cost. New preference order: **ElevenLabs → Groq → OpenAI**. Enable via `ELEVENLABS_API_KEY` in `~/.config/watch/.env`; `--whisper` now also accepts `elevenlabs`.
- **Long-audio chunking**: audio over ~22 MB is split at silence boundaries (`ffmpeg silencedetect`, −30 dB / 0.5 s) into ≤22 MB chunks, transcribed serially, and restitched with timestamp offsets. Removes the previous ~52-minute upload ceiling. Adjacent identical-text segments at chunk boundaries are deduped.
- **`--no-frames` flag**: skip frame extraction for audio-only content (podcasts, interviews, lectures). Symmetric to `--no-whisper`. Also suppresses the long-video sparse-frames warning.
- **`--sub-lang` flag**: comma-separated subtitle languages to fetch (e.g. `ko`, `ja,en`). Defaults to English variants. Lets non-English videos pull free native captions instead of falling back to the paid Whisper backends. `download._pick_subtitle` now selects the VTT matching the requested languages in priority order.
- **`--json` output mode** (schema locked at v1.0.0): structured output for wrapper skills and programmatic consumers. Markdown report is byte-identical when the flag is absent.

### Changed
- **Scene-aware frame sampling** replaces fixed-fps extraction: `ffmpeg select` filter keeps a frame on scene change *or* after a per-duration time gap. Per-duration ceilings raise the max frame budget (up to 180 frames). Timestamps parsed from real decoded PTS (`showinfo`). `--fps` remains as an escape hatch to uniform extraction; `--max-frames` now caps the per-duration ceiling.
- Transcription preference order: **ElevenLabs → Groq → OpenAI** (was Groq → OpenAI).
- Windows: stdout and stderr reconfigured to UTF-8 on console, preventing cp1252 crashes on em-dashes and arrows in titles, hints, and report markdown.

### Removed
- **Codex plugin support** (`.codex-plugin/plugin.json`): Claude Code is now the only supported surface.
- **GitHub Actions release workflow** (`.github/workflows/release.yml`): no longer builds or publishes `.skill` bundles on tag push.
- **`scripts/build-skill.sh`**: packaging script for claude.ai `.skill` bundles removed alongside the workflow.

## [0.1.3] — 2026-05-09

### Fixed
- Windows: `video.info.json` is read as UTF-8 (#4). Previously `Path.read_text()` defaulted to cp1252 on Windows and crashed on yt-dlp's UTF-8 output, silently dropping Title/Uploader from the report. Same fix applied to `.env` reads/writes in `whisper.py` and `setup.py`.
- `download.py` now logs info.json parse failures to stderr instead of swallowing them.

### Security
- Hardened subprocess argv against option injection (#2): inserted `--` before the URL in the yt-dlp argv, and tightened `is_url` to reject `-`-prefixed sources and require a non-empty netloc. Resolved video/audio paths to absolute via `Path.resolve()` before passing to `ffmpeg`/`ffprobe`, so a relative path starting with `-` can't be misinterpreted as a flag.

## [0.1.2] — 2026-04-24

### Fixed
- Windows console crash: removed the emoji from the long-video warning in `watch.py`; cp1252 consoles couldn't encode it.
- `setup.py` now prints `winget` / `pip` install commands on Windows instead of "unsupported platform" — matches what the README already promised.

### Changed
- `SKILL.md` notes that on Windows the scripts must be invoked with `python`, not `python3` (the latter is the Microsoft Store stub on Windows).

## [0.1.1] — 2026-04-24

### Fixed
- Added `commands/watch.md` shim so `/watch` is callable when installed as a Claude Code plugin. Without it, the plugin loaded but the skill wasn't exposed as a slash command.
- `scripts/build-skill.sh` now strips `commands/` from the claude.ai `.skill` bundle alongside `hooks/` and `.claude-plugin/`.

## [0.1.0] — 2026-04-24

Initial marketplace release.

### Added
- `/watch <url-or-path> [question]` slash command.
- yt-dlp download with native caption extraction (manual + auto-subs).
- ffmpeg frame extraction with auto-scaled fps (≤2 fps, ≤100 frames, duration-aware budget).
- `--start` / `--end` focused mode with denser frame budget and transcript range filtering.
- Whisper fallback (Groq preferred, OpenAI secondary) for videos without captions.
- `setup.py` preflight: silent `--check`, structured `--json`, and installer that auto-runs `brew install` on macOS.
- Session-start hook that prints a one-line status on first run / partial config.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.

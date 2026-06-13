# /watch

> **Opinionated fork maintained by [@joweiser](https://github.com/joweiser).** Original skill by [@bradautomates](https://github.com/bradautomates) - see [bradautomates/claude-video](https://github.com/bradautomates/claude-video). See [Fork changes](#fork-changes).

**Give Claude the ability to watch any video.**

Claude Code:
```
/plugin marketplace add joweiser/claude-video
/plugin install watch@claude-video
```

Zero config to start — `yt-dlp` and `ffmpeg` install on first run via `brew` on macOS (Linux/Windows print exact commands). Captions cover most public videos for free. Whisper API key is only needed when a video has no captions.

---

Claude can read a webpage, run a script, browse a repo. What it can't do, out of the box, is *watch a video*. You paste a YouTube link and it has to either guess from the title or pull a transcript that's missing 90% of what's on screen.

With Claude Video `/watch` you can paste a URL or a local path, ask a question, and Claude downloads the video, extracts frames at an auto-scaled rate, pulls a timestamped transcript (free captions when available, Whisper API as fallback), and `Read`s every frame as an image. By the time it answers, it has *seen* the video and *heard* the audio.

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
```

## Fork changes

Enhancements and modifications this fork adds on top of upstream `bradautomates/claude-video`:

**Transcription**
- **ElevenLabs Scribe added as the preferred backend** — word-level timestamps grouped into ~8s segments on sentence boundaries, higher accuracy at comparable cost. New preference order: **ElevenLabs → Groq → OpenAI**; existing Whisper fallbacks remain. Enable by setting `ELEVENLABS_API_KEY` in `~/.config/watch/.env`; `--whisper` now also accepts `elevenlabs`.
- **Long-audio chunking** — audio over ~22 MB is split at silence boundaries (ffmpeg `silencedetect`) into ≤22 MB chunks, transcribed serially, and restitched with timestamp offsets. Removes the previous ~52-minute upload ceiling; boundary-duplicate segments are deduped to defend against Whisper's start-of-clip hallucination. Public `transcribe_video()` signature unchanged.

**Frame sampling**
- **Scene-aware sampling replaces fixed-fps extraction** — ffmpeg's `select` filter keeps a frame on scene-change *or* after a per-duration time gap, so static talking-head footage still gets even coverage and fast-cut content doesn't miss cuts. Per-duration ceilings raise the max frame budget (up to 180), and timestamps are parsed from real decoded PTS (`showinfo`) instead of `i/fps` math. `--fps` remains as an escape hatch to the legacy uniform path (still clamped to 2 fps); `--max-frames` now caps the per-duration ceiling.

**Output & CLI flags**
- **`--json` output mode** (schema locked at v1.0.0) for wrapper skills that consume `/watch` programmatically and need structured data instead of the markdown report. Markdown output is byte-identical when the flag is absent.
- **`--no-frames` flag** — skips frame extraction entirely for audio-only content (podcasts, interviews, lectures); symmetric to the existing `--no-whisper`. Also suppresses the long-video sparse-frames warning.

**Windows**
- **UTF-8 stdout/stderr** — both streams are reconfigured to UTF-8 so em-dashes and arrows in titles, hints, and report markdown don't crash cp1252 consoles.
- **yt-dlp PATH fallback** — `setup.py --check` no longer reports yt-dlp as missing when it's pip-installed into a user `Scripts` dir off PATH (common in git-bash); `_which` probes `python -m yt_dlp` before declaring it absent. Backports still-open upstream [#14](https://github.com/bradautomates/claude-video/pull/14).

---

## Why this exists

I built this because I'm constantly using video to keep up with content. If I see a YouTube video that's blowing up, I want to know how the creator structured the hook — what's on screen in the first 3 seconds, what they said, why it worked. That used to mean watching it myself with a notepad. Now I just paste the URL and ask.

The other half is summarization. Most YouTube videos don't deserve 20 minutes of my attention. I hand the URL to Claude, it pulls the transcript, and tells me what actually happened. If the visual matters, frames come along too. If it's a podcast or a talking head, transcript is enough.

Claude is great at reading and synthesizing — but until now, video was the one input I couldn't hand it. Pasting a YouTube link got you nothing useful. `/watch` closes that gap.

## What people actually use it for

**Analyze someone else's content.** `/watch https://youtu.be/<viral-video> what hook did they open with?` Claude looks at the first frames, reads the opening transcript, breaks down the structure. Same for ad creative, competitor launches, podcast intros, anything where the *how* matters as much as the *what*.

**Diagnose a bug from a video.** Someone sends you a screen recording of something broken. `/watch bug-repro.mov what's going wrong?` Claude watches the recording, finds the frame where the issue appears, describes what's on screen, often catches the cause without you ever opening the file.

**Summarize a video.** `/watch https://youtu.be/<long-thing> summarize this` does the obvious thing — pulls the structure, the key moments, what was actually said and shown. Faster than watching at 2x.

## How it works

1. **You paste a video and a question.** URL (anything yt-dlp supports — YouTube, Loom, TikTok, X, Instagram, plus a few hundred more) or a local path (`.mp4`, `.mov`, `.mkv`, `.webm`).
2. **`yt-dlp` downloads it.** For URLs, into a temp working directory. For local files, no download — just probed in place.
3. **`ffmpeg` extracts frames using scene-aware sampling.** The `select` filter keeps a frame on scene change *or* after a per-duration time gap, so static footage gets even coverage and fast-cut content doesn't miss cuts. The frame budget scales by duration: ≤30s gets ~30 frames, 30-60s gets ~40, 1-3min gets ~60, 3-10min gets ~80, longer up to ~180 sparsely. Hard ceiling: 2 fps. JPEGs at 1024px wide by default (readable for most slides and terminals) — bump toward `--resolution 1456` for dense code/text, or lower it for a tighter token budget.
4. **The transcript comes from one of two places.** First try: `yt-dlp` pulls native captions (manual or auto-generated) from the source. Free, instant, accurate-ish. Fallback: extract a mono 16 kHz audio clip and ship it to a transcription API — ElevenLabs Scribe (`scribe_v1`, preferred — word-level timestamps), Groq's `whisper-large-v3`, or OpenAI's `whisper-1`.
5. **Frames + transcript are handed to Claude.** The script prints frame paths with `t=MM:SS` markers and the transcript with timestamps. Claude `Read`s each frame in parallel — JPEGs render directly as images in its context.
6. **Claude answers grounded in what's actually on screen and in the audio.** Not "based on the description" or "according to the title." It saw the frames. It heard the transcript. It answers the way someone who watched the video would.
7. **Cleanup.** The script prints a working directory at the end. If you're not asking follow-ups, Claude removes it.

## Frame budget — why it matters

Token cost is dominated by frames. Every frame is an image; image tokens add up fast. The script's auto-fps logic exists so you don't blow your context budget on a sparse scan of a 30-minute video that would have been better answered by a focused 30-second window.

| Duration | Default frame budget | What you get |
|----------|---------------------|--------------|
| ≤30 s | ~30 frames | Dense — basically every key moment |
| 30 s - 1 min | ~40 frames | Still dense |
| 1 - 3 min | ~60 frames | Comfortable |
| 3 - 10 min | ~80 frames | Sparse but workable |
| > 10 min | up to ~180 frames | "Sparse scan" warning — re-run focused |

When the user names a moment ("around 2:30", "the last 30 seconds", "from 0:45 to 1:00"), pass `--start` / `--end`. Focused mode gets denser per-second budgets, capped at 2 fps. Far more useful than a sparse pass over the whole thing.

## Install

| Surface | Install |
|---------|---------|
| **Claude Code** | `/plugin marketplace add joweiser/claude-video` then `/plugin install watch@claude-video` |
| **Manual / dev** | `git clone https://github.com/joweiser/claude-video.git ~/.claude/skills/watch` |

### Claude Code

```
/plugin marketplace add joweiser/claude-video
/plugin install watch@claude-video
```

Update later with `/plugin update watch@claude-video`.

### Manual (developer)

```bash
git clone https://github.com/joweiser/claude-video.git ~/.claude/skills/watch
```

## First run

On the first `/watch` call, the skill runs `scripts/setup.py --check`. If `ffmpeg` / `yt-dlp` aren't on your PATH, or no Whisper API key is set, it walks you through fixing it:

- **macOS** — auto-runs `brew install ffmpeg yt-dlp`.
- **Linux** — prints the exact `apt` / `dnf` / `pipx` commands.
- **Windows** — prints the `winget` / `pip` commands.
- **API key** — scaffolds `~/.config/watch/.env` (mode `0600`) with commented placeholders for `ELEVENLABS_API_KEY` (preferred), `GROQ_API_KEY`, and `OPENAI_API_KEY`.

After setup, preflight is silent and `/watch` just works. The check is a sub-100ms lookup, so it doesn't slow you down on subsequent runs.

## Bring your own keys

Captions cover the majority of public videos for free. The Whisper fallback only kicks in when a video genuinely has no caption track — typically local files, TikToks, some Vimeos, and the occasional caption-less YouTube upload.

| Capability | What you need | Cost |
|------------|---------------|------|
| Download + native captions | `yt-dlp` + `ffmpeg` | Free |
| Transcription fallback (preferred) | [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) — `scribe_v1`, word-level timestamps | Comparable, more accurate |
| Transcription fallback (alt) | [Groq API key](https://console.groq.com/keys) — `whisper-large-v3` | Cheap, fast |
| Transcription fallback (alt) | [OpenAI API key](https://platform.openai.com/api-keys) — `whisper-1` | Standard pricing |
| Disable transcription entirely | `--no-whisper` | Free, frames-only when no captions |
| Non-English captions | `--sub-lang ko` | Free — pulls native captions, skips Whisper |

## Usage

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
/watch https://www.tiktok.com/@user/video/123 summarize this
/watch ~/Movies/screen-recording.mp4 when does the UI break?
/watch https://vimeo.com/123 what tools does she mention?
```

Focused on a specific section — denser frame budget, lower token cost:
```
/watch https://youtu.be/abc --start 2:15 --end 2:45
/watch video.mp4 --start 50 --end 60
/watch "$URL" --start 1:12:00            # from 1h12m to end
```

Other knobs (passed to `scripts/watch.py`):

- `--max-frames N` — lower the frame cap for a tighter token budget.
- `--resolution W` — frame width in px (default 1024). Bump toward ~1456 px for dense on-screen code/text; the API downsamples beyond ~1568 px so higher values mostly waste tokens. Lower it (e.g. 768) for a tighter budget.
- `--fps F` — override the auto-fps calculation (still capped at 2 fps).
- `--whisper elevenlabs|groq|openai` — force a specific transcription backend.
- `--no-whisper` — disable transcription entirely; frames only.
- `--sub-lang LANGS` — comma-separated caption languages to fetch (e.g. `ko`, `ja,en`). Defaults to English; lets non-English videos use free captions instead of the paid Whisper fallback.
- `--out-dir DIR` — keep working files somewhere specific (default: auto-generated tmp dir).

## Limits

- **Best accuracy: under 10 minutes.** Past that the script prints a "sparse scan" warning — re-run focused on the part you actually care about with `--start`/`--end`.
- **Hard cap: 2 fps; up to ~180 frames per-duration.** `--max-frames` lowers the ceiling. Frame count drives token cost.
- **Long audio is chunked automatically.** Audio over ~22 MB is split at silence boundaries into ≤22 MB pieces, transcribed serially, and restitched — no effective upload ceiling.
- **No private platforms.** This skill doesn't log into anything. Public URLs and local files only. If yt-dlp can't reach it without auth, neither can `/watch`.

## Structure

```
.
├── SKILL.md                 # skill contract — Claude Code only
├── scripts/
│   ├── watch.py             # entry point — orchestrates download → frames → transcript
│   ├── download.py          # yt-dlp wrapper
│   ├── frames.py            # ffmpeg frame extraction + auto-fps logic
│   ├── transcribe.py        # VTT parsing + dedupe + Whisper orchestration
│   ├── whisper.py           # ElevenLabs / Groq / OpenAI clients (pure stdlib)
│   └── setup.py             # preflight + installer
└── hooks/                   # SessionStart status hook (Claude Code only)
```

## Develop

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Open source

MIT license.

Built on `yt-dlp`, `ffmpeg`, and Claude's multimodal `Read` tool. Transcription via [ElevenLabs](https://elevenlabs.io), [Groq](https://groq.com), or [OpenAI](https://openai.com).

---

[github.com/joweiser/claude-video](https://github.com/joweiser/claude-video) · [LICENSE](LICENSE)

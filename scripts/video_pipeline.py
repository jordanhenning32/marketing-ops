"""Shadow Edge Tools — Video Pipeline.

End-to-end Phase A flow for a long-form video:

    1. Probe duration (ffmpeg)
    2. Transcribe (faster-whisper local OR cloud)
    3. Run Content Multiplier on the transcript -> 6 markdown drafts
    4. Parse Shorts timestamps from 02-shorts-scripts.md
    5. Cut each Shorts range into a vertical 9:16 .mp4 with ffmpeg, burning in
       brand-styled subtitles from the transcript (toggle via BURN_SUBTITLES=0)
    6. Stage each clip + caption + tags into per-platform distribution folders
       so Phase B (YouTube auto-upload) and later (TikTok/Instagram) can ship.

Public entry point:
    process_video_job(job_id, video_path, slug, product, transcribe_provider, premium)

The function expects to run inside a `jobs.run_job(...)` thread; it advances
job state via jobs.update_job / jobs.log_job.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Load env from repo/shared Shadow Edge locations.
try:
    from dotenv import load_dotenv
    _env_root = Path(__file__).resolve().parent.parent
    for candidate in (
        _env_root / ".env",
        _env_root.parent / ".ENV",
        _env_root.parent / ".env",
        Path("E:/futures-bot/.env"),
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass

import imageio_ffmpeg

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import content_multiplier  # noqa: E402
import compliance  # noqa: E402
import jobs  # noqa: E402

ROOT = _HERE.parent
CONTENT_DIR = ROOT / "content"
INCOMING_VIDEOS_DIR = CONTENT_DIR / "incoming-videos"
CONFIG_DIR = ROOT / "config"


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

# Transcription quality drives caption + content quality. large-v3 is the most
# accurate Whisper model; on CPU it favors accuracy over speed (fine for short
# clips). Drop to "medium"/"small" or set WHISPER_DEVICE=cuda for long-form.
DEFAULT_WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
# Bias transcription toward Shadow Edge / NinjaTrader vocabulary so brand and
# trading terms come out spelled right (e.g. "Drawdown", not "Draw down").
# Override with WHISPER_PROMPT if the product vocabulary changes.
WHISPER_INITIAL_PROMPT = os.environ.get(
    "WHISPER_PROMPT",
    "Shadow Edge Tools tutorial for NinjaTrader 8 futures traders. Tools: Bracket Boss, "
    "Drawdown Guardian. Terms: prop firm, drawdown, trailing drawdown, daily loss limit, "
    "stop loss, bracket order, auto-flat, lockout, ES, NQ, ticks, P&L.",
)

# Distribution platforms we generate assets for. Phase B/C will wire up the
# actual uploads; Phase A produces a "ready-to-post" kit per platform.
PLATFORMS = ["youtube", "tiktok", "instagram", "rumble"]
SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920


# ---------------------------------------------------------------------------
# ffmpeg helpers (use the imageio-ffmpeg bundled binary)
# ---------------------------------------------------------------------------

def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *args]
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_ffmpeg_probe(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "info", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _font_path(*names: str) -> str:
    for name in names:
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return str(path).replace("\\", "/").replace(":", r"\:")
    return ""


TITLE_FONT = _font_path("arialbd.ttf", "seguibl.ttf", "arial.ttf")
BODY_FONT = _font_path("arial.ttf", "seguisb.ttf")
# The branded title card occupies the first few seconds; captions start after it
# so the two never overlap. Keep the enable expr derived from one number.
TITLE_DURATION_SEC = 4.25
TITLE_ENABLE_EXPR = rf"between(t\,0\,{TITLE_DURATION_SEC})"

# Burned-in subtitle styling. Captions are short single-line cues, lower-third,
# white bold on a semi-transparent pill — legible on muted autoplay (where most
# Shorts/Reels/TikTok views happen). Disable globally with BURN_SUBTITLES=0.
CAPTION_Y = "h*0.80"
CAPTION_MAX_WORDS_PER_CUE = 6
CAPTION_MIN_CUE_SEC = 0.7


def probe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds, parsed from ffmpeg stderr."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        return 0.0
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def _ff_filter_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("'", "")
        .replace("’", "")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace("%", r"\%")
    )


def _overlay_title_from_short(title: str) -> str:
    title = re.sub(r"^Short\s*\d+\s*[—\-:]\s*", "", title, flags=re.IGNORECASE).strip()
    title = title.strip('"')
    title = re.sub(r"\s+", " ", title)
    return title[:70] or "Shadow Edge Tools"


def _split_overlay_title(title: str) -> tuple[str, str]:
    words = title.upper().split()
    if len(words) <= 3:
        return " ".join(words), ""
    best_idx = 1
    best_score: tuple[int, int] | None = None
    for idx in range(1, len(words)):
        left = " ".join(words[:idx])
        right = " ".join(words[idx:])
        score = (abs(len(left) - len(right)), max(len(left), len(right)))
        if best_score is None or score < best_score:
            best_score = score
            best_idx = idx
    return " ".join(words[:best_idx]), " ".join(words[best_idx:])


def _overlay_label(title: str, body_md: str = "") -> str:
    haystack = f"{title}\n{body_md}".lower()
    if "drawdown guardian" in haystack:
        return "DRAWDOWN GUARDIAN"
    if "bracket boss" in haystack:
        return "BRACKET BOSS"
    if "ninjatrader" in haystack or "nt8" in haystack:
        return "NINJATRADER 8"
    return "SHADOW EDGE TOOLS"


def _clean_short_title(title: str) -> str:
    title = re.sub(r"^Short\s*\d+\s*[â€”\-:]\s*", "", title or "", flags=re.IGNORECASE).strip()
    title = title.strip('"')
    title = re.sub(r"#\w+", "", title)
    title = re.sub(r"\bLockfinished\b", "Lockout", title, flags=re.IGNORECASE)
    title = re.sub(r"\bAuto Flat\b", "Auto-Flat", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    return title or "Shadow Edge Tools"


def _clean_hook_text(text: str) -> str:
    text = re.sub(r"\*\*", "", text or "")
    text = text.strip().strip('"')
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -:|")


def _compact_title(text: str, *, max_len: int = 86) -> str:
    text = _clean_hook_text(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0].rstrip(".,;:-")
    return cut or text[:max_len].rstrip(".,;:-")


def _youtube_title_for_short(short: "ShortRange") -> str:
    base = _clean_short_title(short.title)
    hook = _clean_hook_text(_short_field(short.body_md, "Hook"))
    on_screen = _clean_hook_text(_short_field(short.body_md, "On-screen text"))
    haystack = f"{base}\n{hook}\n{on_screen}\n{short.body_md}".lower()

    if "$" in haystack and "liquidation" in haystack:
        amount = re.search(r"\$\s?[\d,]+", f"{base} {hook} {short.body_md}")
        prefix = f"{amount.group(0).replace(' ', '')} Until Liquidation" if amount else "Until Liquidation"
        return _compact_title(f"{prefix}. Walk Away.")
    if "revenge" in haystack and ("lock" in haystack or "lockout" in haystack):
        return "The Lock That Stops Revenge Trades"
    if "trailing drawdown" in haystack or "drawdown line" in haystack:
        return "Your Drawdown Line Just Got Real"
    if "discipline score" in haystack:
        return "Your P&L Lies. Discipline Does Not."
    if "move" in haystack and "stop" in haystack:
        return "Do Not Move That Stop"
    if "scale-out" in haystack or "scale out" in haystack:
        return "Scale Out Without Panic"
    if len(base) < 26 and len(hook) >= 28:
        return _compact_title(hook)
    return _compact_title(base)


def _drawtext(text: str, *, y: str, fontsize: int, color: str, font: str = "") -> str:
    font_part = f"fontfile='{font}':" if font else ""
    return (
        "drawtext="
        f"{font_part}text='{_ff_filter_text(text)}':"
        f"x=(w-text_w)/2:y={y}:fontsize={fontsize}:fontcolor={color}:"
        "borderw=3:bordercolor=black@0.9:shadowx=3:shadowy=3:shadowcolor=black@0.75:"
        f"enable='{TITLE_ENABLE_EXPR}'"
    )


def _title_overlay_filter(title: str, body_md: str = "") -> str:
    clean_title = _overlay_title_from_short(title)
    line1, line2 = _split_overlay_title(clean_title)
    label = _overlay_label(clean_title, body_md)
    longest_line = max(len(line1), len(line2))
    title_font_size = 78 if longest_line <= 22 else 68 if longest_line <= 30 else 58
    filters = [
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.56:t=fill:enable='{TITLE_ENABLE_EXPR}'",
        f"drawbox=x=iw*0.28:y=ih*0.615:w=iw*0.44:h=5:color=0x22d3ee@0.90:t=fill:enable='{TITLE_ENABLE_EXPR}'",
        _drawtext("SHADOW EDGE TOOLS", y="h*0.135", fontsize=24, color="0xa9b9ff", font=BODY_FONT),
    ]
    if line2:
        filters.append(_drawtext(line1, y="h*0.305", fontsize=title_font_size, color="white", font=TITLE_FONT))
        filters.append(_drawtext(line2, y="h*0.425", fontsize=title_font_size, color="0x22d3ee", font=TITLE_FONT))
    else:
        filters.append(_drawtext(line1, y="h*0.365", fontsize=min(84, title_font_size + 6), color="white", font=TITLE_FONT))
    filters.extend([
        _drawtext(label, y="h*0.675", fontsize=34, color="white", font=TITLE_FONT),
        _drawtext("shadowedgetools.com", y="h*0.735", fontsize=24, color="0x22d3ee", font=BODY_FONT),
    ])
    return ",".join(filters)


@dataclass
class CaptionCue:
    start: float   # clip-local seconds
    end: float
    text: str


def _split_segment_into_cues(seg: "TranscribedSegment") -> list[tuple[float, float, str]]:
    """Break one transcript segment into short word-groups with proportional timing
    (source-timeline seconds), so captions read as snappy phrases, not paragraphs."""
    words = (seg.text or "").split()
    if not words:
        return []
    span = max(0.1, seg.end - seg.start)
    groups = [words[i:i + CAPTION_MAX_WORDS_PER_CUE] for i in range(0, len(words), CAPTION_MAX_WORDS_PER_CUE)]
    out: list[tuple[float, float, str]] = []
    cursor = seg.start
    for g in groups:
        c_end = min(seg.end, cursor + span * (len(g) / len(words)))
        out.append((cursor, c_end, " ".join(g)))
        cursor = c_end
    return out


def clip_caption_cues(
    segments: list["TranscribedSegment"],
    clip_start: float,
    clip_end: float,
    *,
    skip_under_title: bool = True,
) -> list[CaptionCue]:
    """Caption cues for one clip: windowed to [clip_start, clip_end], shifted to
    clip-local time, held until the title card clears, de-overlapped, min-duration."""
    floor = TITLE_DURATION_SEC if skip_under_title else 0.0
    cues: list[CaptionCue] = []
    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        for (s, e, text) in _split_segment_into_cues(seg):
            s = max(s, clip_start)
            e = min(e, clip_end)
            if e <= s or not text.strip():
                continue
            ls = max(round(s - clip_start, 3), floor)
            le = round(e - clip_start, 3)
            if le - ls < 0.05:
                continue
            cues.append(CaptionCue(ls, le, text.strip()))
    # de-overlap + enforce a gentle minimum so single words don't flash
    cues.sort(key=lambda c: c.start)
    clip_len = clip_end - clip_start
    for i, c in enumerate(cues):
        next_start = cues[i + 1].start if i + 1 < len(cues) else clip_len
        if c.end < c.start + CAPTION_MIN_CUE_SEC:
            c.end = round(min(c.start + CAPTION_MIN_CUE_SEC, next_start), 3)
        if c.end > next_start:
            c.end = round(next_start, 3)
    return [c for c in cues if c.end > c.start]


def _caption_font_size(text: str) -> int:
    n = len(text)
    if n <= 22:
        return 58
    if n <= 32:
        return 50
    return 42


def _caption_drawtext(cue: CaptionCue) -> str:
    font_part = f"fontfile='{TITLE_FONT}':" if TITLE_FONT else ""
    enable = rf"between(t\,{cue.start}\,{cue.end})"
    return (
        "drawtext="
        f"{font_part}text='{_ff_filter_text(cue.text)}':"
        f"x=(w-text_w)/2:y={CAPTION_Y}:fontsize={_caption_font_size(cue.text)}:fontcolor=white:"
        "box=1:boxcolor=black@0.55:boxborderw=18:"
        "borderw=2:bordercolor=black@0.9:shadowx=2:shadowy=2:shadowcolor=black@0.6:"
        f"enable='{enable}'"
    )


def _caption_overlay_filter(cues: list[CaptionCue]) -> str:
    return ",".join(_caption_drawtext(c) for c in cues)


def _vertical_shorts_filter(
    title: str | None = None,
    body_md: str = "",
    caption_cues: list[CaptionCue] | None = None,
) -> str:
    """Create a 9:16 Shorts frame while preserving the full source image."""
    base = (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={SHORTS_WIDTH}:{SHORTS_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={SHORTS_WIDTH}:{SHORTS_HEIGHT},boxblur=32:1,"
        "eq=brightness=-0.08:saturation=0.85[bg];"
        f"[fg]scale={SHORTS_WIDTH}:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    if title:
        base += "," + _title_overlay_filter(title, body_md)
    if caption_cues:
        base += "," + _caption_overlay_filter(caption_cues)
    return base + ",format=yuv420p[vout]"


def extract_audio(video_path: Path, audio_path: Path) -> None:
    """Extract a 16kHz mono WAV from the video for whisper input."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    res = _run_ffmpeg([
        "-y",
        "-i", str(video_path),
        "-ac", "1",
        "-ar", "16000",
        "-vn",
        str(audio_path),
    ])
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extract failed: {res.stderr}")


def cut_clip(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path,
    *,
    title_overlay: str | None = None,
    title_body_md: str = "",
    caption_cues: list["CaptionCue"] | None = None,
    crf: int = 23,
    preset: str = "veryfast",
) -> None:
    """Cut a single clip with re-encoding for accurate frame boundaries."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.5, end_sec - start_sec)
    args = [
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", str(video_path),
        "-t", f"{duration:.3f}",
    ]
    args += [
        "-filter_complex", _vertical_shorts_filter(title_overlay, title_body_md, caption_cues),
        "-map", "[vout]",
        "-map", "0:a?",
    ]
    args += [
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    res = _run_ffmpeg(args)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed for {output_path.name}: {res.stderr}")


def _thumbnail_lines(title: str, *, max_lines: int = 3, max_chars: int = 18) -> list[str]:
    words = re.sub(r"[^A-Za-z0-9$%&+.' -]", " ", title.upper()).split()
    lines: list[str] = []
    cur: list[str] = []
    for word in words:
        candidate = " ".join([*cur, word]).strip()
        if cur and len(candidate) > max_chars and len(lines) < max_lines - 1:
            lines.append(" ".join(cur))
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(" ".join(cur))
    if len(lines) > max_lines:
        merged = " ".join(lines[max_lines - 1:])
        lines = [*lines[:max_lines - 1], merged]
    return [line[:28].rstrip() for line in lines if line.strip()] or ["SHADOW EDGE"]


def _thumbnail_font_size(lines: list[str]) -> int:
    longest = max((len(line) for line in lines), default=12)
    if len(lines) <= 2 and longest <= 14:
        return 78
    if longest <= 18:
        return 66
    if longest <= 22:
        return 58
    return 50


def _thumbnail_drawtext(
    text: str,
    *,
    x: int,
    y: int,
    fontsize: int,
    color: str,
    font: str = "",
    borderw: int = 4,
    shadow: bool = True,
) -> str:
    font_part = f"fontfile='{font}':" if font else ""
    shadow_part = "shadowx=4:shadowy=4:shadowcolor=black@0.70" if shadow else "shadowx=0:shadowy=0:shadowcolor=black@0"
    return (
        "drawtext="
        f"{font_part}text='{_ff_filter_text(text)}':"
        f"x={x}:y={y}:fontsize={fontsize}:fontcolor={color}:"
        f"borderw={borderw}:bordercolor=black@0.90:{shadow_part}"
    )


def _thumbnail_filter(title: str, body_md: str = "") -> str:
    lines = _thumbnail_lines(_compact_title(title, max_len=72))
    font_size = _thumbnail_font_size(lines)
    label = _overlay_label(title, body_md)
    filters = [
        "[0:v]split=2[bg][phone];"
        "[bg]scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
        "boxblur=28:2,eq=brightness=-0.18:saturation=1.18[bg];"
        "[phone]scale=430:-1,crop=430:720:(iw-ow)/2:(ih-oh)/2,"
        "eq=brightness=0.03:saturation=1.10[phone];"
        "[bg][phone]overlay=x=820:y=0[base];"
        "[base]drawbox=x=0:y=0:w=820:h=720:color=black@0.64:t=fill",
        "drawbox=x=54:y=52:w=260:h=48:color=0x22d3ee@0.96:t=fill",
        _thumbnail_drawtext("SHADOW EDGE", x=74, y=61, fontsize=28, color="black", font=TITLE_FONT, borderw=0, shadow=False),
        "drawbox=x=792:y=28:w=452:h=664:color=0x22d3ee@0.92:t=6",
        "drawbox=x=58:y=588:w=420:h=54:color=0x7c3aed@0.96:t=fill",
        _thumbnail_drawtext(label, x=78, y=599, fontsize=31, color="white", font=TITLE_FONT),
        _thumbnail_drawtext("WATCH BEFORE YOU TRADE", x=58, y=656, fontsize=27, color="0x22d3ee", font=TITLE_FONT),
    ]
    y = 172
    for idx, line in enumerate(lines):
        color = "white" if idx % 2 == 0 else "0x22d3ee"
        filters.append(_thumbnail_drawtext(line, x=58, y=y, fontsize=font_size, color=color, font=TITLE_FONT))
        y += int(font_size * 1.08)
    return ",".join(filters) + ",format=yuv420p[vout]"


def write_thumbnail(
    clip_path: Path,
    thumbnail_path: Path,
    *,
    title: str = "",
    body_md: str = "",
    at_sec: float = 1.0,
) -> None:
    """Render a designed YouTube Shorts thumbnail instead of a padded frame grab."""
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_title = title or clip_path.stem
    res = _run_ffmpeg([
        "-y",
        "-ss", f"{at_sec:.3f}",
        "-i", str(clip_path),
        "-frames:v", "1",
        "-filter_complex", _thumbnail_filter(thumb_title, body_md),
        "-map", "[vout]",
        "-q:v", "3",
        str(thumbnail_path),
    ])
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg thumbnail failed for {clip_path.name}: {res.stderr}")


# ---------------------------------------------------------------------------
# Transcription providers
# ---------------------------------------------------------------------------

@dataclass
class TranscribedSegment:
    start: float
    end: float
    text: str


def transcribe_local(
    video_path: Path,
    *,
    model_size: str = DEFAULT_WHISPER_MODEL,
    progress_cb=None,
) -> tuple[list[TranscribedSegment], str]:
    """Transcribe with faster-whisper. Returns (segments, language)."""
    from faster_whisper import WhisperModel

    if progress_cb:
        progress_cb(f"Loading whisper model: {model_size} ({WHISPER_COMPUTE})")
    model = WhisperModel(model_size, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)

    if progress_cb:
        progress_cb("Extracting audio")
    audio_path = video_path.with_suffix(".wav")
    extract_audio(video_path, audio_path)

    if progress_cb:
        progress_cb(f"Transcribing with {model_size} (accuracy-first; slower on CPU)")

    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        initial_prompt=WHISPER_INITIAL_PROMPT or None,
    )
    segments: list[TranscribedSegment] = []
    last_pct = -1
    duration = info.duration if info.duration else probe_duration(video_path) or 1.0
    for seg in segments_iter:
        segments.append(TranscribedSegment(start=seg.start, end=seg.end, text=seg.text.strip()))
        if progress_cb:
            pct = int(min(99, (seg.end / duration) * 100))
            if pct >= last_pct + 5:
                progress_cb(f"Transcribing: {pct}% ({_fmt_ts(seg.end)} / {_fmt_ts(duration)})")
                last_pct = pct

    # Tidy up audio file
    try:
        audio_path.unlink()
    except OSError:
        pass

    return segments, info.language or "en"


def transcribe_cloud_assemblyai(video_path: Path, progress_cb=None) -> tuple[list[TranscribedSegment], str]:
    """Cloud STT via AssemblyAI. Optional path; requires ASSEMBLYAI_API_KEY."""
    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ASSEMBLYAI_API_KEY not set. Add it to marketing-ops/.env or use the local provider."
        )
    import httpx

    if progress_cb:
        progress_cb("Uploading to AssemblyAI")
    upload_url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": api_key}
    with httpx.Client(timeout=httpx.Timeout(600.0)) as client:
        with open(video_path, "rb") as f:
            up = client.post(upload_url, headers=headers, content=f.read())
        up.raise_for_status()
        audio_url = up.json()["upload_url"]

        if progress_cb:
            progress_cb("Submitting transcript request")
        sub = client.post(
            "https://api.assemblyai.com/v2/transcript",
            headers=headers,
            json={"audio_url": audio_url, "punctuate": True, "format_text": True},
        )
        sub.raise_for_status()
        tid = sub.json()["id"]

        # Poll
        if progress_cb:
            progress_cb("Polling for completion")
        while True:
            poll = client.get(f"https://api.assemblyai.com/v2/transcript/{tid}", headers=headers)
            poll.raise_for_status()
            data = poll.json()
            status = data.get("status")
            if status == "completed":
                break
            if status == "error":
                raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
            import time as _t
            _t.sleep(3)

    segments: list[TranscribedSegment] = []
    for w in data.get("words", []) or []:
        segments.append(TranscribedSegment(
            start=w["start"] / 1000.0,
            end=w["end"] / 1000.0,
            text=w["text"],
        ))
    # Optional: collapse word-level into ~3-second utterances
    segments = _collapse_words(segments, max_gap=0.6, max_dur=3.0)
    return segments, data.get("language_code", "en")


def _collapse_words(words: list[TranscribedSegment], *, max_gap: float, max_dur: float) -> list[TranscribedSegment]:
    """Merge word-level segments into short utterances for cleaner SRT chunks."""
    out: list[TranscribedSegment] = []
    cur_start: float | None = None
    cur_end: float = 0.0
    cur_text: list[str] = []
    for w in words:
        if cur_start is None:
            cur_start = w.start
            cur_text = [w.text]
            cur_end = w.end
            continue
        gap = w.start - cur_end
        if gap > max_gap or (w.end - cur_start) > max_dur:
            out.append(TranscribedSegment(cur_start, cur_end, " ".join(cur_text)))
            cur_start = w.start
            cur_text = [w.text]
        else:
            cur_text.append(w.text)
        cur_end = w.end
    if cur_text and cur_start is not None:
        out.append(TranscribedSegment(cur_start, cur_end, " ".join(cur_text)))
    return out


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def segments_to_srt(segments: list[TranscribedSegment]) -> str:
    """Serialize whisper/assemblyai segments to SRT text."""
    lines: list[str] = []
    for i, s in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(s.start)} --> {_fmt_srt_time(s.end)}")
        lines.append(s.text)
        lines.append("")
    return "\n".join(lines)


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Transcript cleanup
# ---------------------------------------------------------------------------
# Conservative, deterministic, per-segment. Removes non-lexical filler and
# stutters and tidies spacing/caps WITHOUT paraphrasing — captions must stay
# the spoken words (minus the junk) and keep their original timing. Disable
# globally with CLEAN_TRANSCRIPT=0.

# Non-lexical fillers only. Deliberately NOT "like"/"you know"/"so" — those
# carry real meaning and removing them mangles sentences.
_FILLER_WORDS = {
    "um", "umm", "ummm", "uh", "uhh", "uhhh", "uhm", "erm", "er", "err",
    "ah", "ahh", "mm", "mmm", "hmm", "hmmm", "mhm", "mmhm", "uh-huh",
}
_BRACKET_NOISE_RE = re.compile(
    r"[\[(]\s*(music|applause|laughter|inaudible|silence|noise|background\s*noise|crosstalk)[^\])]*[\])]",
    re.IGNORECASE,
)
_WORD_CORE_RE = re.compile(r"[^\w']")


def _clean_segment_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _BRACKET_NOISE_RE.sub(" ", text)
    tokens = cleaned.split()
    started_upper = bool(tokens) and tokens[0][:1].isupper()
    kept: list[str] = []
    for tok in tokens:
        core = _WORD_CORE_RE.sub("", tok).lower()
        if core in _FILLER_WORDS:          # drop whole-word fillers (keeps "her", "water", "yeah")
            continue
        prev_core = _WORD_CORE_RE.sub("", kept[-1]).lower() if kept else None
        if core and core == prev_core:     # collapse an immediate stutter ("the the" -> "the")
            continue
        kept.append(tok)
    out = " ".join(kept)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)   # no space before punctuation
    out = re.sub(r"\s{2,}", " ", out).strip()
    # Only restore a capital when the segment opened with a (now-removed) filler;
    # never force-capitalize a genuine mid-sentence fragment.
    if out and started_upper and tokens and tokens[0] not in kept and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def clean_transcript_segments(segments: list[TranscribedSegment]) -> list[TranscribedSegment]:
    """Return cleaned segments (original timing preserved). Segments that were
    pure filler collapse to empty and are dropped."""
    out: list[TranscribedSegment] = []
    for s in segments:
        text = _clean_segment_text(s.text)
        if text:
            out.append(TranscribedSegment(start=s.start, end=s.end, text=text))
    return out


# ---------------------------------------------------------------------------
# Shorts ranges parser
# ---------------------------------------------------------------------------

_SHORTS_BLOCK_RE = re.compile(
    r"###\s*(?P<heading>Short[^\n]*?)\n"
    r"(?P<body>.*?)(?=^###\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_SHORTS_TIME_RE = re.compile(
    # Tolerates **Source:** ... 00:00:30 → 00:00:45 (with or without bold markers)
    r"Source:\*{0,2}\s*(\d{2}:\d{2}:\d{2})\s*(?:[→\->]+|to)\s*(\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)


def _parse_hhmmss(s: str) -> float | None:
    parts = [int(part) for part in s.split(":")]
    if len(parts) == 2:
        h = 0
        m, ss = parts
    elif len(parts) == 3:
        h, m, ss = parts
    else:
        return None
    if m >= 60 or ss >= 60:
        return None
    return h * 3600 + m * 60 + ss


_SHORTS_TIME_RE = re.compile(
    r"Source:\*{0,2}\s*\[?((?:\d{2}:)?\d{2}:\d{2})\]?\s*(?:[^\w\s]+|to)\s*\[?((?:\d{2}:)?\d{2}:\d{2})\]?",
    re.IGNORECASE,
)


@dataclass
class ShortRange:
    title: str
    start_sec: float
    end_sec: float
    body_md: str


def parse_shorts_ranges(shorts_md: str) -> list[ShortRange]:
    """Read 02-shorts-scripts.md and pull the timestamp ranges."""
    out: list[ShortRange] = []
    for m in _SHORTS_BLOCK_RE.finditer(shorts_md):
        body = m.group("body")
        time_match = _SHORTS_TIME_RE.search(body)
        if not time_match:
            continue
        start_sec = _parse_hhmmss(time_match.group(1))
        end_sec = _parse_hhmmss(time_match.group(2))
        if start_sec is None or end_sec is None or end_sec <= start_sec:
            continue
        out.append(ShortRange(
            title=m.group("heading").strip(),
            start_sec=start_sec,
            end_sec=end_sec,
            body_md=body.strip(),
        ))
    return out


_SHORTS_SOURCE_LINE_RE = re.compile(
    r"(?im)^(?P<prefix>\s*(?:-\s*)?(?:\*\*)?Source:(?:\*\*)?\s*)"
    r"\[?(?:\d{2}:)?\d{2}:\d{2}\]?\s*(?:[^\w\s]+|to)\s*"
    r"\[?(?:\d{2}:)?\d{2}:\d{2}\]?(?:\s*\([^)]*\))?\s*$"
)


def _replace_source_range(body_md: str, start_sec: float, end_sec: float) -> str:
    duration = max(0, int(end_sec - start_sec))
    source = f"{_fmt_ts(start_sec)} -> {_fmt_ts(end_sec)} ({duration} seconds)"
    repaired, count = _SHORTS_SOURCE_LINE_RE.subn(
        lambda m: f"{m.group('prefix')}{source}",
        body_md,
        count=1,
    )
    if count:
        return repaired
    return f"- **Source:** {source}\n{body_md.strip()}"


def repair_short_ranges_to_duration(
    shorts: list[ShortRange],
    duration_sec: float | None,
) -> tuple[list[ShortRange], dict]:
    """Clamp LLM-produced source ranges to the actual video duration.

    The exporter already clamps at cut time; this keeps the earlier markdown
    validation gate and sidecar files in sync with what will actually render.
    """
    meta = {
        "attempted": False,
        "succeeded": True,
        "duration_sec": round(float(duration_sec or 0.0), 3),
        "clamped_indexes": [],
        "dropped_indexes": [],
        "notes": [],
    }
    if not duration_sec or duration_sec <= 0:
        return shorts, meta

    max_end_sec = float(int(duration_sec))
    repaired: list[ShortRange] = []
    for idx, short in enumerate(shorts, start=1):
        start = max(0.0, short.start_sec)
        end = short.end_sec
        if start >= max_end_sec:
            meta["attempted"] = True
            meta["dropped_indexes"].append(idx)
            meta["notes"].append(
                f"short {idx}: dropped source {_fmt_ts(short.start_sec)} -> {_fmt_ts(short.end_sec)}; starts after transcript duration"
            )
            continue
        if end > max_end_sec:
            end = max_end_sec
            meta["attempted"] = True
            meta["clamped_indexes"].append(idx)
            meta["notes"].append(
                f"short {idx}: clamped source end from {_fmt_ts(short.end_sec)} to {_fmt_ts(end)}"
            )
        if end <= start:
            meta["attempted"] = True
            meta["dropped_indexes"].append(idx)
            meta["notes"].append(
                f"short {idx}: dropped source {_fmt_ts(short.start_sec)} -> {_fmt_ts(short.end_sec)} after duration repair"
            )
            continue
        body = short.body_md
        if start != short.start_sec or end != short.end_sec:
            body = _replace_source_range(body, start, end)
        repaired.append(ShortRange(short.title, start, end, body))

    return repaired, meta


def shorts_quality_report(shorts: list[ShortRange], duration_sec: float) -> dict:
    target_min, target_max = content_multiplier.shorts_target_range(duration_sec)
    warnings: list[str] = []
    clip_lengths = [max(0.0, s.end_sec - s.start_sec) for s in shorts]
    if len(shorts) < target_min:
        warnings.append(f"only {len(shorts)} shorts found; target is {target_min}-{target_max}")
    if len(shorts) > target_max:
        warnings.append(f"{len(shorts)} shorts found; target is {target_min}-{target_max}")
    if any(length > 70 for length in clip_lengths):
        warnings.append("one or more shorts exceed 70 seconds")
    if duration_sec >= 12 * 60 and shorts:
        half = duration_sec / 2
        late_count = sum(1 for s in shorts if s.start_sec >= half)
        if late_count < 4:
            warnings.append(f"only {late_count} shorts start after halfway; target is at least 4")
        third = duration_sec / 3
        thirds = [
            sum(1 for s in shorts if 0 <= s.start_sec < third),
            sum(1 for s in shorts if third <= s.start_sec < third * 2),
            sum(1 for s in shorts if s.start_sec >= third * 2),
        ]
        if min(thirds) < 3:
            warnings.append(f"weak timeline coverage across thirds: {thirds}")
    return {
        "target_min": target_min,
        "target_max": target_max,
        "count": len(shorts),
        "clip_lengths_sec": [round(v, 2) for v in clip_lengths],
        "warnings": warnings,
    }


def shorts_quality_failures(report: dict) -> list[str]:
    failures = []
    for warning in report.get("warnings", []):
        if warning.startswith("only 0 shorts found") or "exceed 70 seconds" in warning:
            failures.append(warning)
    return failures


COMMERCIAL_QA_THRESHOLD = 85
COMMERCIAL_QA_WEIGHTS = {
    "hook": 15,
    "clarity": 12,
    "product_proof": 18,
    "visual_quality": 12,
    "audio_captions": 12,
    "cta": 10,
    "compliance": 11,
    "uniqueness": 10,
}
_PRODUCT_TERMS = (
    "bracket boss",
    "drawdown guardian",
    "shadow edge",
    "ninjatrader",
    "nt8",
)
_PAYOFF_TERMS = (
    "risk",
    "drawdown",
    "liquidation",
    "discipline",
    "target",
    "stop",
    "entry",
    "filled",
    "cancel",
    "invalid",
    "break even",
    "breakeven",
    "lockout",
    "auto-flat",
    "automation",
    "decision",
    "payoff",
)
_GENERIC_SHORT_TERMS = (
    "generic",
    "market update",
    "welcome back",
    "quick look",
    "talk about",
    "thing",
    "stuff",
)
_CTA_TERMS = (
    "shadowedgetools.com",
    "full guide",
    "full workflow",
    "full session",
    "watch",
    "download",
    "learn more",
    "see how",
)
_HIGH_PRESSURE_TERMS = (
    "limited time",
    "act now",
    "guaranteed",
    "risk-free",
    "secret",
)


def _qa_norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _qa_words(value: str) -> list[str]:
    return _qa_norm(value).split()


def _qa_has_any(value: str, terms: tuple[str, ...]) -> bool:
    value = (value or "").lower()
    return any(term in value for term in terms)


def _qa_has_number(value: str) -> bool:
    return bool(re.search(r"(?:\$|#)?\d", value or ""))


def _qa_category(score: int, max_score: int, notes: list[str]) -> dict:
    score = max(0, min(max_score, score))
    return {
        "score": score,
        "max": max_score,
        "passed": score == max_score,
        "notes": notes,
    }


def _qa_transcript_overlaps(
    segments: list[TranscribedSegment] | None,
    start_sec: float,
    end_sec: float,
) -> bool | None:
    if segments is None:
        return None
    return any(seg.end > start_sec and seg.start < end_sec for seg in segments)


def _commercial_qa_uniqueness_context(shorts: list[ShortRange]) -> dict[str, Counter[str]]:
    return {
        "titles": Counter(_qa_norm(_overlay_title_from_short(s.title)) for s in shorts),
        "hooks": Counter(_qa_norm(_short_field(s.body_md, "Hook")) for s in shorts if _short_field(s.body_md, "Hook")),
        "quotes": Counter(_qa_norm(_short_field(s.body_md, "Source quote")) for s in shorts if _short_field(s.body_md, "Source quote")),
    }


def _commercial_qa_item(
    short: ShortRange,
    *,
    index: int,
    uniqueness: dict[str, Counter[str]],
    transcript_segments: list[TranscribedSegment] | None,
) -> dict:
    title = _overlay_title_from_short(short.title)
    body = short.body_md
    hook = _short_field(body, "Hook")
    why = _short_field(body, "Why this clip works")
    quote = _short_field(body, "Source quote")
    on_screen = _short_field(body, "On-screen text")
    cta = _short_field(body, "Outro CTA")
    haystack = f"{title}\n{body}"
    duration = max(0.0, short.end_sec - short.start_sec)
    categories: dict[str, dict] = {}

    score = 0
    notes: list[str] = []
    hook_words = _qa_words(hook)
    if hook:
        score += 6
    else:
        notes.append("missing hook")
    if len(hook_words) >= 5:
        score += 3
    else:
        notes.append("hook is too short")
    if _qa_has_any(hook, _PAYOFF_TERMS) or _qa_has_number(hook):
        score += 4
    else:
        notes.append("hook lacks a concrete stake, number, or decision")
    if hook and len(hook_words) <= 22 and not _qa_has_any(hook, _GENERIC_SHORT_TERMS):
        score += 2
    else:
        notes.append("hook is generic or too long")
    categories["hook"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["hook"], notes)

    score = 0
    notes = []
    why_words = _qa_words(why)
    if title and len(_qa_words(title)) >= 2:
        score += 3
    else:
        notes.append("title is too thin")
    if why and len(why_words) >= 6:
        score += 4
    else:
        notes.append("why-this-works rationale is too thin")
    if _qa_has_any(why, _PAYOFF_TERMS) or _qa_has_any(title, _PAYOFF_TERMS):
        score += 3
    else:
        notes.append("rationale lacks a clear trading decision or payoff")
    if not _qa_has_any(f"{title} {why}", _GENERIC_SHORT_TERMS):
        score += 2
    else:
        notes.append("title/rationale reads generic")
    categories["clarity"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["clarity"], notes)

    score = 0
    notes = []
    if _qa_has_any(haystack, _PRODUCT_TERMS):
        score += 6
    else:
        notes.append("no product/platform proof")
    if _qa_has_any(haystack, _PAYOFF_TERMS) or _qa_has_number(haystack):
        score += 6
    else:
        notes.append("no concrete risk-control payoff")
    if len(_qa_words(quote)) >= 6:
        score += 4
    else:
        notes.append("source quote is too thin")
    if why and (_qa_has_any(why, _PRODUCT_TERMS) or _qa_has_any(why, _PAYOFF_TERMS)):
        score += 2
    else:
        notes.append("rationale does not tie proof to the buyer outcome")
    categories["product_proof"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["product_proof"], notes)

    score = 0
    notes = []
    screen_words = _qa_words(on_screen)
    if on_screen:
        score += 5
    else:
        notes.append("missing on-screen text")
    if on_screen and len(on_screen) <= 45:
        score += 3
    else:
        notes.append("on-screen text is too long for Shorts")
    if len(screen_words) >= 2 or _qa_has_number(on_screen):
        score += 2
    else:
        notes.append("on-screen text lacks a readable visual beat")
    if title and len(title) <= 70:
        score += 2
    else:
        notes.append("visual title is too long")
    categories["visual_quality"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["visual_quality"], notes)

    score = 0
    notes = []
    if 15 <= duration <= 60:
        score += 4
    else:
        notes.append(f"duration {duration:.0f}s is outside the 15-60s Shorts range")
    quote_words = _qa_words(quote)
    if len(quote_words) >= 6:
        score += 4
    else:
        notes.append("source quote is too short for captions")
    overlap = _qa_transcript_overlaps(transcript_segments, short.start_sec, short.end_sec)
    if overlap is True:
        score += 2
    elif overlap is None and quote:
        score += 2
    else:
        notes.append("no transcript segment overlaps the source range")
    if quote and len(quote_words) <= 26:
        score += 2
    else:
        notes.append("source quote may be too dense for burned captions")
    categories["audio_captions"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["audio_captions"], notes)

    score = 0
    notes = []
    if cta:
        score += 5
    else:
        notes.append("missing CTA")
    if _qa_has_any(cta, _CTA_TERMS):
        score += 3
    else:
        notes.append("CTA does not point to a clear next step")
    if cta and not _qa_has_any(cta, _HIGH_PRESSURE_TERMS):
        score += 2
    else:
        notes.append("CTA is high-pressure or absent")
    categories["cta"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["cta"], notes)

    result = compliance.check_text(haystack, source=f"short {index}")
    compliance_notes = [issue.reason for issue in result.issues]
    categories["compliance"] = _qa_category(
        COMMERCIAL_QA_WEIGHTS["compliance"] if result.status == "pass" else 0,
        COMMERCIAL_QA_WEIGHTS["compliance"],
        compliance_notes or ["local compliance check passed"],
    )

    score = 0
    notes = []
    title_key = _qa_norm(title)
    hook_key = _qa_norm(hook)
    quote_key = _qa_norm(quote)
    if uniqueness["titles"].get(title_key, 0) <= 1:
        score += 4
    else:
        notes.append("duplicate title")
    if quote_key and uniqueness["quotes"].get(quote_key, 0) <= 1:
        score += 4
    else:
        notes.append("duplicate or missing source quote")
    if hook_key and uniqueness["hooks"].get(hook_key, 0) <= 1:
        score += 2
    else:
        notes.append("duplicate or missing hook")
    categories["uniqueness"] = _qa_category(score, COMMERCIAL_QA_WEIGHTS["uniqueness"], notes)

    total = sum(category["score"] for category in categories.values())
    if result.status != "pass":
        total = min(total, COMMERCIAL_QA_THRESHOLD - 1)
    reasons = [
        f"{name}: {', '.join(data['notes'][:2])}"
        for name, data in categories.items()
        if data["score"] < data["max"]
    ]
    status = "pass" if total >= COMMERCIAL_QA_THRESHOLD else "manual_review"
    return {
        "index": index,
        "title": short.title,
        "score": total,
        "threshold": COMMERCIAL_QA_THRESHOLD,
        "status": status,
        "categories": categories,
        "reasons": reasons,
        "compliance_status": result.status,
        "compliance_risk_level": result.risk_level,
    }


def commercial_qa_report(
    shorts: list[ShortRange],
    *,
    transcript_segments: list[TranscribedSegment] | None = None,
) -> dict:
    uniqueness = _commercial_qa_uniqueness_context(shorts)
    clips = [
        _commercial_qa_item(
            short,
            index=idx,
            uniqueness=uniqueness,
            transcript_segments=transcript_segments,
        )
        for idx, short in enumerate(shorts, start=1)
    ]
    failures = [
        f"short {item['index']}: commercial QA score {item['score']} below {COMMERCIAL_QA_THRESHOLD}"
        for item in clips
        if item["score"] < COMMERCIAL_QA_THRESHOLD
    ]
    approved_count = sum(1 for item in clips if item["status"] == "pass")
    manual_review_count = sum(1 for item in clips if item["status"] == "manual_review")
    return {
        "threshold": COMMERCIAL_QA_THRESHOLD,
        "passed": not failures,
        "manual_review_required": bool(failures),
        "approved_count": approved_count,
        "manual_review_count": manual_review_count,
        "failures": failures,
        "clips": clips,
    }


def _renumber_short_heading(title: str, index: int) -> str:
    if re.match(r"^Short\s+\d+\b", title or "", flags=re.IGNORECASE):
        return re.sub(r"^Short\s+\d+\b", f"Short {index}", title, count=1, flags=re.IGNORECASE)
    return f"Short {index} - {title or 'Selected Trading Moment'}"


def shorts_to_markdown(shorts: list[ShortRange]) -> str:
    blocks = [
        f"### {_renumber_short_heading(short.title, idx)}\n{short.body_md.strip()}"
        for idx, short in enumerate(shorts, start=1)
    ]
    text = "\n\n---\n\n".join(blocks).strip()
    return text + "\n" if text else ""


def filter_commercial_qa_failures(
    shorts: list[ShortRange],
    commercial_qa: dict,
    *,
    transcript_segments: list[TranscribedSegment] | None = None,
) -> tuple[list[ShortRange], dict, dict]:
    failed_indexes = {
        int(item.get("index", 0))
        for item in commercial_qa.get("clips", [])
        if item.get("score", 0) < COMMERCIAL_QA_THRESHOLD or item.get("status") != "pass"
    }
    if not failed_indexes:
        return shorts, commercial_qa, {"attempted": False, "succeeded": True, "dropped_indexes": []}
    kept = [
        short
        for idx, short in enumerate(shorts, start=1)
        if idx not in failed_indexes
    ]
    repaired_qa = commercial_qa_report(kept, transcript_segments=transcript_segments) if kept else {
        "threshold": COMMERCIAL_QA_THRESHOLD,
        "passed": False,
        "manual_review_required": True,
        "approved_count": 0,
        "manual_review_count": 0,
        "failures": ["no commercial QA approved Shorts remain after filtering"],
        "clips": [],
    }
    repair = {
        "attempted": True,
        "succeeded": bool(kept) and repaired_qa.get("passed") is True,
        "original_count": len(shorts),
        "repaired_count": len(kept),
        "dropped_indexes": sorted(failed_indexes),
        "notes": [
            f"dropped short {item.get('index')}: commercial QA score {item.get('score')} below {COMMERCIAL_QA_THRESHOLD}"
            for item in commercial_qa.get("clips", [])
            if int(item.get("index", 0)) in failed_indexes
        ],
        "remaining_failures": repaired_qa.get("failures", []),
    }
    return kept, repaired_qa, repair


# ---------------------------------------------------------------------------
# Distribution kit (Phase A: prepare; Phase B/C: actually upload)
# ---------------------------------------------------------------------------

PLATFORM_LIMITS = {
    "youtube": {"max_seconds": 180, "aspect_label": "Shorts: 9:16 vertical"},
    "tiktok":  {"max_seconds": 90, "aspect_label": "9:16"},
    "instagram": {"max_seconds": 90, "aspect_label": "9:16"},
    "rumble":  {"max_seconds": None, "aspect_label": "Any (no auto-upload — manual kit only)"},
}


SITE_URL = os.environ.get("SHADOW_EDGE_SITE_URL", "https://shadowedgetools.com").rstrip("/") or "https://shadowedgetools.com"
LONG_FORM_MIN_SECONDS = _env_int("LONG_FORM_MIN_SECONDS", PLATFORM_LIMITS["youtube"]["max_seconds"])


def _public_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2192": ">",
        "â†’": ">",
        "â€”": "-",
        "â€“": "-",
        "Â·": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    return text.strip().strip('"')


def _short_field(body_md: str, label: str) -> str:
    label_re = re.escape(label)
    patterns = [
        rf"^\s*-\s*\*\*{label_re}[^:]*:\*\*\s*(.+)$",
        rf"^\s*-\s*{label_re}[^:]*:\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_md, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return _public_text(match.group(1))
    return ""


def tracked_site_url(*, source: str, campaign_id: str, content_id: str, medium: str = "video", path: str = "/") -> str:
    from urllib.parse import urlencode

    query = urlencode({
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign_id,
        "utm_content": content_id,
    })
    return f"{SITE_URL}{path}?{query}"


def _link_cta(platform: str, campaign_id: str, content_id: str) -> tuple[str, str]:
    # Cold traffic leads with the FREE checklist (lead magnet), not the $249 buy
    # flow — see state/goal.md "Lead capture live". The nurture sells from there.
    url = tracked_site_url(source=platform, campaign_id=campaign_id, content_id=content_id, path="/checklist")
    return url, f"Free risk-control checklist: {url}"


# Bare HOMEPAGE mention only — "shadowedgetools.com" or "https://www.shadowedgetools.com/"
# (optional lone trailing slash). Deliberately does NOT match real deep links like
# ".../checklist?utm=…", "/products/bracket-boss", or "/guides" — those are kept.
_BARE_SITE_RE = re.compile(r"(?i)(?<![\w.-])(?:https?://)?(?:www\.)?shadowedgetools\.com/?(?![\w/?#])")


def _retarget_bare_site_links(text: str, tracked_url: str) -> str:
    """Rewrite bare homepage mentions to the tracked lead-magnet URL so every
    published caption funnels to /checklist AND is attributable in GA4.

    The LLM-written Outro CTA (and older copy) often names the bare homepage,
    which sends untracked traffic to a page with no email capture. Links that
    already carry a path/query (the tracked /checklist link the pipeline injects)
    are left untouched."""
    if not text:
        return text
    return _BARE_SITE_RE.sub(tracked_url, text)


RENDERED_QA_THRESHOLD = 85
RENDERED_QA_WEIGHTS = {
    "file_integrity": 10,
    "crop": 12,
    "black_frames": 12,
    "blur": 12,
    "audio": 16,
    "captions": 14,
    "brand_cta": 10,
    "motion_continuity": 14,
}


def _rendered_category(score: int, max_score: int, notes: list[str]) -> dict:
    score = max(0, min(max_score, score))
    return {
        "score": score,
        "max": max_score,
        "passed": score == max_score,
        "notes": notes,
    }


def _analysis_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or "") + "\n" + (proc.stdout or "")


def _probe_media_info(path: Path) -> dict:
    proc = _run_ffmpeg_probe(["-i", str(path)])
    out = _analysis_text(proc)
    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if m:
        h, mn, s = m.groups()
        duration = int(h) * 3600 + int(mn) * 60 + float(s)
    video_match = re.search(
        r"Video:\s*[^,\n]+(?:,[^,\n]+)*,\s*(?P<width>\d{2,5})x(?P<height>\d{2,5})\b",
        out,
    )
    fps_match = re.search(r"(?P<fps>\d+(?:\.\d+)?)\s+fps\b", out)
    return {
        "duration_sec": round(duration, 3),
        "width": int(video_match.group("width")) if video_match else 0,
        "height": int(video_match.group("height")) if video_match else 0,
        "fps": float(fps_match.group("fps")) if fps_match else 0.0,
        "has_video": bool(video_match),
        "has_audio": "Audio:" in out,
        "probe_returncode": proc.returncode,
    }


def _video_filter_output(path: Path, vf: str) -> str:
    proc = _run_ffmpeg_probe(["-i", str(path), "-an", "-vf", vf, "-f", "null", "-"])
    return _analysis_text(proc)


def _audio_filter_output(path: Path, af: str) -> str:
    proc = _run_ffmpeg_probe(["-i", str(path), "-vn", "-af", af, "-f", "null", "-"])
    return _analysis_text(proc)


def _sum_float_matches(pattern: str, text: str) -> float:
    return sum(float(match) for match in re.findall(pattern, text))


def _finite_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def rendered_clip_qa(
    clip_path: Path,
    *,
    index: int,
    title: str = "",
    visual_label: str = "",
    cta: str = "",
    caption_cues: list[CaptionCue] | None = None,
    captions_burned: bool = True,
) -> dict:
    """Score the actual rendered MP4, using FFmpeg probes plus render context."""
    clip_path = Path(clip_path)
    categories: dict[str, dict] = {}
    metrics: dict[str, object] = {"path": str(clip_path)}
    notes: list[str] = []
    exists = clip_path.exists()
    size_bytes = clip_path.stat().st_size if exists else 0
    info = _probe_media_info(clip_path) if exists and size_bytes > 0 else {
        "duration_sec": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "has_video": False,
        "has_audio": False,
        "probe_returncode": None,
    }
    metrics.update(info)
    metrics["size_bytes"] = size_bytes
    duration = float(info.get("duration_sec") or 0.0)
    if exists and size_bytes > 0 and info.get("has_video") and duration > 0:
        score = RENDERED_QA_WEIGHTS["file_integrity"]
    else:
        score = 0
        notes.append("rendered MP4 is missing, empty, or unreadable")
    categories["file_integrity"] = _rendered_category(score, RENDERED_QA_WEIGHTS["file_integrity"], notes)

    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    aspect = width / height if width and height else 0.0
    metrics["aspect_ratio"] = round(aspect, 4) if aspect else 0
    notes = []
    score = 0
    target_aspect = SHORTS_WIDTH / SHORTS_HEIGHT
    if width >= 720 and height >= 1280:
        score += 4
    else:
        notes.append("render is below 720x1280 vertical quality")
    if height > width and abs(aspect - target_aspect) <= 0.025:
        score += 5
    else:
        notes.append("render is not a clean 9:16 vertical crop")
    if width == SHORTS_WIDTH and height == SHORTS_HEIGHT:
        score += 3
    else:
        notes.append(f"rendered dimensions are {width}x{height}, expected {SHORTS_WIDTH}x{SHORTS_HEIGHT}")
    categories["crop"] = _rendered_category(score, RENDERED_QA_WEIGHTS["crop"], notes)

    if duration > 0:
        black_out = _video_filter_output(clip_path, "blackdetect=d=0.20:pic_th=0.98")
        black_duration = _sum_float_matches(r"black_duration:([0-9.]+)", black_out)
        longest_black = max([float(v) for v in re.findall(r"black_duration:([0-9.]+)", black_out)] or [0.0])
    else:
        black_duration = 0.0
        longest_black = 0.0
    black_ratio = black_duration / duration if duration else 1.0
    metrics["black_duration_sec"] = round(black_duration, 3)
    metrics["black_ratio"] = round(black_ratio, 4)
    notes = []
    score = RENDERED_QA_WEIGHTS["black_frames"]
    if black_ratio > 0.12 or longest_black > 1.5:
        score = 0
        notes.append("black frames dominate the rendered clip")
    elif black_ratio > 0.04 or longest_black > 0.6:
        score = 7
        notes.append("render includes noticeable black-frame intervals")
    categories["black_frames"] = _rendered_category(score, RENDERED_QA_WEIGHTS["black_frames"], notes)

    if duration > 0:
        blur_out = _video_filter_output(clip_path, "fps=2,blurdetect")
        blur_match = re.search(r"blur mean:\s*([0-9.+-]+|nan)", blur_out, flags=re.IGNORECASE)
        blur_mean = _finite_float(blur_match.group(1) if blur_match else None)
    else:
        blur_mean = None
    metrics["blur_mean"] = None if blur_mean is None else round(blur_mean, 3)
    notes = []
    if blur_mean is None:
        score = 0
        notes.append("blur analysis could not find enough sharp detail")
    elif blur_mean <= 9:
        score = RENDERED_QA_WEIGHTS["blur"]
    elif blur_mean <= 14:
        score = 7
        notes.append("render appears somewhat soft")
    else:
        score = 0
        notes.append("render appears blurry")
    categories["blur"] = _rendered_category(score, RENDERED_QA_WEIGHTS["blur"], notes)

    notes = []
    if info.get("has_audio") and duration > 0:
        audio_out = _audio_filter_output(clip_path, "volumedetect,silencedetect=n=-45dB:d=0.5")
        mean_match = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", audio_out)
        max_match = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", audio_out)
        mean_volume = _finite_float(mean_match.group(1) if mean_match else None)
        max_volume = _finite_float(max_match.group(1) if max_match else None)
        silence_duration = _sum_float_matches(r"silence_duration:\s*([0-9.]+)", audio_out)
    else:
        mean_volume = None
        max_volume = None
        silence_duration = 0.0
    silence_ratio = silence_duration / duration if duration else 1.0
    metrics["mean_volume_db"] = mean_volume
    metrics["max_volume_db"] = max_volume
    metrics["silence_ratio"] = round(silence_ratio, 4)
    score = 0
    if mean_volume is None or max_volume is None:
        notes.append("audio track is missing or unreadable")
    else:
        if mean_volume >= -34:
            score += 6
        else:
            notes.append("audio is too quiet")
        if max_volume <= -0.2:
            score += 5
        else:
            notes.append("audio appears clipped near 0 dB")
        if silence_ratio <= 0.20:
            score += 5
        else:
            notes.append("too much silence in the rendered clip")
    categories["audio"] = _rendered_category(score, RENDERED_QA_WEIGHTS["audio"], notes)

    cue_count = len(caption_cues or [])
    longest_caption = max((len(cue.text) for cue in caption_cues or []), default=0)
    metrics["caption_cues"] = cue_count
    metrics["longest_caption_chars"] = longest_caption
    notes = []
    score = 0
    if captions_burned:
        score += 4
    else:
        notes.append("captions were not burned into this render")
    if cue_count > 0:
        score += 4
    else:
        notes.append("no caption cues were rendered")
    if str(CAPTION_Y) in {"h*0.80", "h*0.8"}:
        score += 3
    else:
        notes.append("caption y-position is outside the known lower-third safe zone")
    if longest_caption <= 38:
        score += 3
    else:
        notes.append("caption text may be too long to read on mobile")
    categories["captions"] = _rendered_category(score, RENDERED_QA_WEIGHTS["captions"], notes)

    notes = []
    score = 0
    if visual_label:
        score += 4
    else:
        notes.append("brand/product label was not configured for the render")
    if title:
        score += 2
    else:
        notes.append("title overlay was not configured for the render")
    if _qa_has_any(cta, _CTA_TERMS):
        score += 4
    else:
        notes.append("CTA does not point to a clear rendered next step")
    categories["brand_cta"] = _rendered_category(score, RENDERED_QA_WEIGHTS["brand_cta"], notes)

    if duration > 0:
        freeze_out = _video_filter_output(clip_path, "freezedetect=n=-60dB:d=0.75")
        freeze_durations = [float(v) for v in re.findall(r"freeze_duration:\s*([0-9.]+)", freeze_out)]
        freeze_duration = sum(freeze_durations)
        longest_freeze = max(freeze_durations or [0.0])
        scene_out = _video_filter_output(clip_path, "select='gt(scene,0.45)',showinfo")
        scene_changes = len(re.findall(r"Parsed_showinfo_\d+.*\bn:\s*\d+", scene_out))
    else:
        freeze_duration = 0.0
        longest_freeze = 0.0
        scene_changes = 0
    freeze_ratio = freeze_duration / duration if duration else 1.0
    scene_rate = scene_changes / duration if duration else 0.0
    metrics["freeze_ratio"] = round(freeze_ratio, 4)
    metrics["longest_freeze_sec"] = round(longest_freeze, 3)
    metrics["scene_changes"] = scene_changes
    metrics["scene_changes_per_sec"] = round(scene_rate, 4)
    notes = []
    score = RENDERED_QA_WEIGHTS["motion_continuity"]
    if freeze_ratio > 0.65 or longest_freeze > 8:
        score = 4
        notes.append("render appears frozen for too much of the clip")
    elif freeze_ratio > 0.35 or longest_freeze > 4:
        score = 9
        notes.append("render has a long static interval")
    if scene_rate > 2.0:
        score = min(score, 6)
        notes.append("render has unusually rapid scene changes")
    elif scene_rate > 1.2:
        score = min(score, 10)
        notes.append("render may feel jumpy")
    categories["motion_continuity"] = _rendered_category(score, RENDERED_QA_WEIGHTS["motion_continuity"], notes)

    total = sum(category["score"] for category in categories.values())
    reasons = [
        f"{name}: {', '.join(data['notes'][:2])}"
        for name, data in categories.items()
        if data["score"] < data["max"]
    ]
    status = "pass" if total >= RENDERED_QA_THRESHOLD else "manual_review"
    return {
        "index": index,
        "title": title,
        "clip": str(clip_path),
        "score": total,
        "threshold": RENDERED_QA_THRESHOLD,
        "status": status,
        "categories": categories,
        "metrics": metrics,
        "reasons": reasons,
    }


def rendered_qa_report(clip_inputs: list[dict], *, captions_burned: bool = True) -> dict:
    clips = [
        rendered_clip_qa(
            Path(item["clip_path"]),
            index=int(item["index"]),
            title=str(item.get("visual_title") or item.get("title") or ""),
            visual_label=str(item.get("visual_label") or ""),
            cta=str(item.get("cta") or ""),
            caption_cues=item.get("caption_cues") or [],
            captions_burned=captions_burned,
        )
        for item in clip_inputs
    ]
    failures = [
        f"clip {item['index']}: rendered QA score {item['score']} below {RENDERED_QA_THRESHOLD}"
        for item in clips
        if item["score"] < RENDERED_QA_THRESHOLD
    ]
    approved_count = sum(1 for item in clips if item["status"] == "pass")
    manual_review_count = sum(1 for item in clips if item["status"] == "manual_review")
    return {
        "threshold": RENDERED_QA_THRESHOLD,
        "passed": not failures,
        "manual_review_required": bool(failures),
        "approved_count": approved_count,
        "manual_review_count": manual_review_count,
        "failures": failures,
        "clips": clips,
    }


# Internal production-note labels that must NEVER appear in a public caption.
_INTERNAL_CAPTION_LABELS = ("score", "source", "why this clip works", "duration", "platform", "rationale")


def _social_caption(short: ShortRange, title_line: str, website_cta: str) -> str:
    """A clean, viewer-facing caption for TikTok/IG/Rumble.

    Leads with the hook (the actual compelling line) + the on-screen value, then the
    checklist CTA. Deliberately EXCLUDES the internal Score / Source / 'Why this clip
    works' production notes that live in short.body_md — those are strategy, not copy.
    """
    hook = _short_field(short.body_md, "Hook")
    on_screen = _short_field(short.body_md, "On-screen text")
    lead = hook or title_line
    parts = [lead]
    if on_screen and on_screen.strip().lower() != lead.strip().lower():
        parts.append(on_screen)
    return "\n\n".join(p for p in parts if p) + "\n\n" + website_cta


def clean_caption_for_posting(text: str) -> str:
    """Strip internal production notes from a caption so only viewer-facing copy posts.

    Drops `- **Score:** …` / `**Source:** …` / `**Why this clip works:** …` style
    bullets entirely, and removes the `**Label:**` prefix from kept lines (Hook,
    On-screen text, Outro CTA) so the caption reads naturally. Safe on an already-clean
    caption (no such lines → returned essentially unchanged). Used as a publish-time
    safety net for older kits whose caption.txt predates the clean generator.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*[-*]?\s*\*\*([^:*]+?)\s*(?:\([^)]*\))?:\*\*\s*(.*)$", line)
        if m:
            label = m.group(1).strip().lower()
            value = m.group(2).strip().strip('"')
            if any(label.startswith(bad) for bad in _INTERNAL_CAPTION_LABELS):
                continue  # internal note — drop the whole line
            if value:
                out.append(value)  # keep the copy, drop the **Label:** prefix
            continue
        out.append(line)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _youtube_description(short: ShortRange, title_line: str, *, website_url: str) -> str:
    hook = _short_field(short.body_md, "Hook")
    on_screen = _short_field(short.body_md, "On-screen text")
    outro = _short_field(short.body_md, "Outro CTA")

    bullets = []
    if hook:
        bullets.append(hook)
    if on_screen:
        bullets.append(on_screen)

    lines = [
        f"Free risk-control checklist: {website_url}",
        "",
        title_line,
        "",
        "A quick Shadow Edge Tools walkthrough for NinjaTrader 8 traders.",
    ]
    if bullets:
        lines += ["", "In this clip:"]
        lines += [f"- {item}" for item in bullets[:3]]
    lines += [
        "",
        outro if outro else f"Free risk-control checklist and tools: {website_url}",
        "",
        "Shadow Edge Tools builds practical risk-management add-ons for NinjaTrader 8.",
        "Tools and education only. Not financial advice.",
        "",
        f"Learn more: {website_url}",
        "",
        "#Shorts #NinjaTrader #FuturesTrading #PropFirm #TradingTools",
    ]
    return "\n".join(lines).strip() + "\n"


def write_distribution_kit(
    out_dir: Path,
    short: ShortRange,
    clip_path: Path,
    *,
    short_index: int,
    campaign_id: str | None = None,
) -> dict:
    """Write a per-platform 'distribution kit' for one Shorts clip.

    Each platform gets a folder under `<out_dir>/distribution/<platform>/<index>/`
    with the clip mp4 (symlink-ish: same path), a caption.txt with the title +
    body, and a hashtags.txt. Phase B will read these to actually upload.
    """
    campaign_id = campaign_id or out_dir.name
    dist = {}
    for platform in PLATFORMS:
        platform_dir = out_dir / "distribution" / platform / f"{short_index:02d}"
        platform_dir.mkdir(parents=True, exist_ok=True)
        website_url, website_cta = _link_cta(platform, campaign_id, f"short-{short_index:02d}")

        title_line = _youtube_title_for_short(short) if platform == "youtube" else _clean_short_title(short.title)
        title_line = title_line[:90]  # platform-safe length

        if platform == "youtube":
            caption = _youtube_description(short, title_line, website_url=website_url)
        else:
            caption = _social_caption(short, title_line, website_cta)
        if platform == "tiktok":
            caption += "\n\n#daytrader #propfirm #futurestrading #fyp"
        elif platform == "instagram":
            caption += "\n\n#daytrader #propfirm #futurestrading #reels"
        elif platform == "rumble":
            caption += "\n\nfutures trading · prop firm · NinjaTrader"

        # Deterministic funnel guarantee: any bare homepage mention (LLM outro,
        # legacy copy) becomes the tracked /checklist link so nothing leaks to an
        # untracked page with no email capture.
        caption = _retarget_bare_site_links(caption, website_url)
        (platform_dir / "caption.txt").write_text(caption, encoding="utf-8")
        (platform_dir / "title.txt").write_text(title_line, encoding="utf-8")
        (platform_dir / "source-clip.txt").write_text(
            str(clip_path.resolve()) + "\n",
            encoding="utf-8",
        )
        thumbnail_relpath = None
        thumbnail_error = None
        if platform == "youtube":
            thumbnail_path = platform_dir / "thumbnail.jpg"
            try:
                write_thumbnail(
                    clip_path,
                    thumbnail_path,
                    title=title_line,
                    body_md=short.body_md,
                )
                thumbnail_relpath = str(thumbnail_path.relative_to(out_dir)).replace("\\", "/")
            except Exception as exc:  # noqa: BLE001
                thumbnail_error = str(exc)
        # Per-platform metadata stub
        metadata = {
            "platform": platform,
            "title": title_line,
            "visual_title": _overlay_title_from_short(short.title),
            "visual_label": _overlay_label(short.title, short.body_md),
            "clip_relpath": str(clip_path.relative_to(out_dir)).replace("\\", "/"),
            "limits": PLATFORM_LIMITS.get(platform, {}),
            "website_url": website_url,
            "website_cta": website_cta,
            "uploaded_at": None,
            "platform_url": None,
        }
        if thumbnail_relpath:
            metadata["thumbnail_relpath"] = thumbnail_relpath
            metadata["thumbnail_style"] = "designed_short_card"
            metadata["thumbnail_title"] = title_line
        if thumbnail_error:
            metadata["thumbnail_error"] = thumbnail_error
        (platform_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )
        dist[platform] = str(platform_dir.relative_to(out_dir)).replace("\\", "/")
    return dist


def should_prepare_long_form(duration_sec: float) -> bool:
    return duration_sec > LONG_FORM_MIN_SECONDS


def _long_form_title(slug: str, product: str) -> str:
    base = re.sub(r"[-_]+", " ", slug).strip().title() or "Shadow Edge Tools Walkthrough"
    suffix = {
        "bb": "Bracket Boss",
        "dg": "Drawdown Guardian",
        "both": "Shadow Edge Tools",
    }.get(product, "Shadow Edge Tools")
    if suffix.lower() not in base.lower():
        base = f"{base} - {suffix}"
    return _compact_title(base, max_len=92)


def _long_form_description(
    *,
    title: str,
    website_url: str,
    duration_sec: float,
    shorts_count: int,
) -> str:
    lines = [
        f"Free risk-control checklist: {website_url}",
        "",
        title,
        "",
        "Full Shadow Edge Tools walkthrough for NinjaTrader 8 traders.",
        "This longer version keeps the context, setup, and decision process together instead of only showing the Shorts cutdowns.",
        "",
        f"Source length: {_fmt_ts(duration_sec)}",
    ]
    if shorts_count:
        lines.append(f"Shorts prepared from this session: {shorts_count}")
    lines += [
        "",
        "Shadow Edge Tools builds practical risk-management add-ons for NinjaTrader 8.",
        "Tools and education only. Not financial advice.",
        "",
        "#NinjaTrader #FuturesTrading #PropFirm #TradingTools",
    ]
    return "\n".join(lines).strip() + "\n"


def write_long_form_youtube_kit(
    out_dir: Path,
    source_video: Path,
    *,
    slug: str,
    product: str,
    duration_sec: float,
    shorts_count: int,
    campaign_id: str | None = None,
) -> dict:
    campaign_id = campaign_id or out_dir.name
    platform_dir = out_dir / "distribution" / "youtube" / "00"
    platform_dir.mkdir(parents=True, exist_ok=True)
    website_url, website_cta = _link_cta("youtube", campaign_id, "long-form")
    title = _long_form_title(slug, product)
    caption = _long_form_description(
        title=title,
        website_url=website_url,
        duration_sec=duration_sec,
        shorts_count=shorts_count,
    )
    (platform_dir / "caption.txt").write_text(caption, encoding="utf-8")
    (platform_dir / "title.txt").write_text(title, encoding="utf-8")
    (platform_dir / "source-clip.txt").write_text(str(source_video.resolve()) + "\n", encoding="utf-8")

    thumbnail_relpath = None
    thumbnail_error = None
    thumbnail_path = platform_dir / "thumbnail.jpg"
    try:
        write_thumbnail(source_video, thumbnail_path, title=title, body_md=caption)
        thumbnail_relpath = str(thumbnail_path.relative_to(out_dir)).replace("\\", "/")
    except Exception as exc:  # noqa: BLE001
        thumbnail_error = str(exc)

    metadata = {
        "platform": "youtube",
        "content_type": "long_form",
        "title": title,
        "clip_relpath": None,
        "source_video_path": str(source_video.resolve()),
        "duration_sec": round(duration_sec, 2),
        "limits": {"max_seconds": None, "aspect_label": "Long-form YouTube video"},
        "website_url": website_url,
        "website_cta": website_cta,
        "uploaded_at": None,
        "platform_url": None,
    }
    if thumbnail_relpath:
        metadata["thumbnail_relpath"] = thumbnail_relpath
        metadata["thumbnail_style"] = "designed_long_form_card"
        metadata["thumbnail_title"] = title
    if thumbnail_error:
        metadata["thumbnail_error"] = thumbnail_error
    (platform_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {
        "enabled": True,
        "content_type": "long_form",
        "title": title,
        "duration_sec": round(duration_sec, 2),
        "distribution": {"youtube": str(platform_dir.relative_to(out_dir)).replace("\\", "/")},
        "website_url": website_url,
    }


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s\-]", "", s.strip().lower())
    s = re.sub(r"\s+", "-", s)
    return s[:60].strip("-") or "untitled"


def _output_dir(slug: str) -> Path:
    today = date.today()
    iso = today.isocalendar()
    return CONTENT_DIR / f"W{iso.week:02d}-{slug}-{today.isoformat()}"


_PIPELINE_EXPORT_DIRS = ("clips", "distribution")
_PIPELINE_EXPORT_FILES = (
    "00-pipeline-manifest.json",
    "00-commercial-qa.json",
    "00-rendered-qa.json",
)


def _reset_pipeline_export_artifacts(out_dir: Path) -> None:
    """Remove stale export artifacts before a rerun writes fresh clip assets."""
    root = Path(out_dir).resolve()
    for name in (*_PIPELINE_EXPORT_DIRS, *_PIPELINE_EXPORT_FILES):
        target = out_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        if target.parent.resolve() != root:
            raise RuntimeError(f"Refusing to reset export artifact outside run folder: {target}")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def process_video_job(
    job_id: str,
    video_path: str | Path,
    *,
    slug: str | None = None,
    product: str = "both",
    transcribe_provider: str = "local",
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    premium: bool = False,
    burn_captions: bool = True,
    clean_transcript: bool = True,
    creator_guidance: str = "",
) -> None:
    """Run the full pipeline. Designed to be the target of jobs.run_job()."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")
    if video_path.stat().st_size <= 0:
        raise ValueError(
            f"Video file is empty: {video_path}. Re-upload the original MP4; the previous upload did not write any bytes."
        )
    creator_guidance = (creator_guidance or "").strip()[:2000]

    slug = slug or _slugify(video_path.stem)
    out_dir = _output_dir(slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    def step(label: str, pct: int) -> None:
        jobs.update_job(job_id, current_step=label, progress_pct=pct)
        jobs.log_job(job_id, label)

    def log(line: str) -> None:
        jobs.log_job(job_id, line)

    # === Step 1: probe duration ===
    step("Probing video", 2)
    duration = probe_duration(video_path)
    log(f"Duration: {_fmt_ts(duration)} ({duration:.1f}s)")

    # === Step 2: transcribe ===
    step(f"Transcribing ({transcribe_provider})", 5)
    if transcribe_provider == "local":
        segments, lang = transcribe_local(video_path, model_size=whisper_model, progress_cb=log)
    elif transcribe_provider == "assemblyai":
        segments, lang = transcribe_cloud_assemblyai(video_path, progress_cb=log)
    else:
        raise ValueError(f"Unknown transcribe_provider: {transcribe_provider}")
    log(f"Transcribed {len(segments)} segments, language={lang}")

    # === Step 2b: clean the transcript (filler/stutters) before it feeds
    # captions + content. Keep the verbatim version as a sidecar for reference.
    if clean_transcript and _env_flag("CLEAN_TRANSCRIPT", True):
        raw_segments = segments
        segments = clean_transcript_segments(raw_segments)
        if len(segments) != len(raw_segments) or any(
            a.text != b.text for a, b in zip(segments, raw_segments)
        ):
            (out_dir / "00-source.raw.srt").write_text(segments_to_srt(raw_segments), encoding="utf-8")
            log(f"Cleaned transcript: {len(raw_segments)} -> {len(segments)} segments "
                "(verbatim saved to 00-source.raw.srt)")

    srt_text = segments_to_srt(segments)
    transcript_txt_path = out_dir / "00-source.txt"
    transcript_txt_path.write_text(" ".join(s.text for s in segments), encoding="utf-8")
    transcript_srt_path = out_dir / "00-source.srt"
    transcript_srt_path.write_text(srt_text, encoding="utf-8")

    # === Step 3: Content Multiplier ===
    step("Generating drafts (Content Multiplier)", 55)
    if creator_guidance:
        log("Creator guidance applied to Shorts selection and titles")
    result = content_multiplier.process_transcript(
        transcript_srt_path,
        slug=slug,
        product=product,
        premium=premium,
        creator_guidance=creator_guidance,
    )
    if result.error:
        raise RuntimeError(f"Multiplier failed: {result.error}")
    log(f"Drafts written to {result.out_dir}")

    # IMPORTANT: the multiplier creates its OWN folder. Re-point our out_dir
    # to that one so all subsequent steps land alongside the markdown files.
    out_dir = result.out_dir
    _reset_pipeline_export_artifacts(out_dir)

    # === Step 4: parse Shorts ranges from the generated md ===
    step("Parsing Shorts ranges", 70)
    shorts_md_path = out_dir / "02-shorts-scripts.md"
    shorts_md = shorts_md_path.read_text(encoding="utf-8") if shorts_md_path.exists() else ""
    shorts = parse_shorts_ranges(shorts_md)
    log(f"Detected {len(shorts)} Shorts ranges")
    duration_repair: dict | None = None
    if shorts:
        repaired_shorts, duration_repair = repair_short_ranges_to_duration(shorts, duration)
        if duration_repair.get("attempted"):
            (out_dir / "02-shorts-scripts.pre-duration-repair.md").write_text(shorts_md, encoding="utf-8")
            shorts = repaired_shorts
            shorts_md = shorts_to_markdown(shorts)
            shorts_md_path.write_text(shorts_md, encoding="utf-8")
            for note in duration_repair.get("notes", []):
                log(f"Shorts duration repair: {note}")
    shorts_quality = shorts_quality_report(shorts, duration)
    script_errors = content_multiplier.validate_shorts_script(shorts_md, duration_sec=duration)
    commercial_qa = commercial_qa_report(shorts, transcript_segments=segments)
    commercial_qa_repair: dict | None = None
    if commercial_qa.get("failures"):
        filtered_shorts, filtered_qa, commercial_qa_repair = filter_commercial_qa_failures(
            shorts,
            commercial_qa,
            transcript_segments=segments,
        )
        if commercial_qa_repair.get("succeeded"):
            (out_dir / "02-shorts-scripts.pre-pipeline-qa.md").write_text(shorts_md, encoding="utf-8")
            shorts = filtered_shorts
            shorts_md = shorts_to_markdown(shorts)
            shorts_md_path.write_text(shorts_md, encoding="utf-8")
            shorts_quality = shorts_quality_report(shorts, duration)
            script_errors = content_multiplier.validate_shorts_script(shorts_md, duration_sec=duration)
            commercial_qa = filtered_qa
            for note in commercial_qa_repair.get("notes", []):
                log(f"Commercial QA repair: {note}")
        else:
            log("Commercial QA repair failed: " + "; ".join(commercial_qa_repair.get("remaining_failures", [])[:4]))
    commercial_failures = commercial_qa["failures"]
    (out_dir / "00-commercial-qa.json").write_text(
        json.dumps(commercial_qa, indent=2),
        encoding="utf-8",
    )
    shorts_quality["script_errors"] = script_errors
    shorts_quality["commercial_qa"] = {
        "threshold": commercial_qa.get("threshold"),
        "approved_count": commercial_qa.get("approved_count"),
        "manual_review_count": commercial_qa.get("manual_review_count"),
        "passed": commercial_qa.get("passed"),
    }
    if duration_repair and duration_repair.get("attempted"):
        shorts_quality["duration_repair"] = duration_repair
    if commercial_qa_repair:
        shorts_quality["commercial_qa_repair"] = commercial_qa_repair
    for warning in shorts_quality["warnings"]:
        log(f"Shorts quality warning: {warning}")
    for error in script_errors:
        log(f"Shorts script quality error: {error}")
    for item in commercial_qa["clips"]:
        log(f"Commercial QA short {item['index']}: {item['score']}/100 ({item['status']})")
        if item["score"] < COMMERCIAL_QA_THRESHOLD:
            log(f"Commercial QA review short {item['index']}: {'; '.join(item['reasons'][:4])}")
    for failure in commercial_failures:
        log(f"Commercial QA gate failure: {failure}")
    structural_failures = shorts_quality_failures(shorts_quality) + script_errors
    quality_failures = structural_failures + commercial_failures
    quality_gate_enabled = _env_flag("SHORTS_QUALITY_GATE", True)
    commercial_qa_gate_enabled = _env_flag("COMMERCIAL_QA_GATE", quality_gate_enabled)
    rendered_qa_gate_enabled = _env_flag("RENDERED_QA_GATE", quality_gate_enabled)
    captions_burned = burn_captions and _env_flag("BURN_SUBTITLES", True)
    transcript_cleaned = bool(clean_transcript and _env_flag("CLEAN_TRANSCRIPT", True))
    rendered_qa: dict | None = None
    long_form_info: dict = {
        "enabled": False,
        "reason": f"source duration is under {LONG_FORM_MIN_SECONDS}s",
    }
    shorts_quality["quality_failures"] = quality_failures
    shorts_quality["quality_gate_enabled"] = quality_gate_enabled
    shorts_quality["commercial_qa_gate_enabled"] = commercial_qa_gate_enabled
    shorts_quality["rendered_qa_gate_enabled"] = rendered_qa_gate_enabled
    shorts_quality["quality_gate_passed"] = not quality_failures
    gate_failures = []
    if structural_failures and quality_gate_enabled:
        gate_failures.extend(structural_failures)
    if commercial_failures and commercial_qa_gate_enabled:
        gate_failures.extend(commercial_failures)
    shorts_quality["manual_review_required"] = bool(quality_failures and not gate_failures)
    shorts_quality["quality_gate_status"] = (
        "pass" if not quality_failures else "blocked" if gate_failures else "manual_review"
    )

    def write_manifest(clip_records: list[dict]) -> None:
        manifest = {
            "slug": out_dir.name,
            "source_video": str(video_path.resolve()),
            "duration_sec": duration,
            "language": lang,
            "transcribe_provider": transcribe_provider,
            "whisper_model": whisper_model if transcribe_provider == "local" else None,
            "clips": clip_records,
            "long_form": long_form_info,
            "platforms_prepared": PLATFORMS,
            "captions_burned": captions_burned,
            "transcript_cleaned": transcript_cleaned,
            "shorts_quality": shorts_quality,
            "commercial_qa": commercial_qa,
            "rendered_qa": rendered_qa,
        }
        (out_dir / "00-pipeline-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    if gate_failures:
        write_manifest([])
        raise RuntimeError("Shorts quality gate failed: " + "; ".join(gate_failures))

    # === Step 5: cut clips ===
    burn_captions = captions_burned
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_records: list[dict] = []
    render_inputs: list[dict] = []
    distribution_inputs: list[tuple[int, ShortRange, Path]] = []
    qa_by_index = {clip.get("index"): clip for clip in commercial_qa.get("clips", [])}
    for i, short in enumerate(shorts, start=1):
        step(f"Cutting clip {i}/{len(shorts)}", 70 + int((i / max(1, len(shorts))) * 25))
        # Sanity check: range within video bounds
        start = max(0, short.start_sec)
        end = min(duration, short.end_sec)
        if end <= start:
            log(f"Skipping clip {i}: invalid range {short.start_sec} → {short.end_sec}")
            continue
        clip_path = clips_dir / f"{i:02d}-{_slugify(short.title)[:30]}.mp4"
        visual_title = _overlay_title_from_short(short.title)
        visual_label = _overlay_label(visual_title, short.body_md)
        caption_cues = clip_caption_cues(segments, start, end) if burn_captions else []
        cut_clip(
            video_path,
            start,
            end,
            clip_path,
            title_overlay=visual_title,
            title_body_md=short.body_md,
            caption_cues=caption_cues,
        )
        log(f"Wrote {clip_path.name} ({end - start:.1f}s, {len(caption_cues)} caption cues)")

        cta = _short_field(short.body_md, "Outro CTA")
        render_inputs.append({
            "index": i,
            "clip_path": clip_path,
            "title": short.title,
            "visual_title": visual_title,
            "visual_label": visual_label,
            "cta": cta,
            "caption_cues": caption_cues,
        })
        distribution_inputs.append((i, short, clip_path))
        clip_records.append({
            "index": i,
            "title": short.title,
            "visual_title": visual_title,
            "visual_label": visual_label,
            "start": _fmt_ts(start),
            "end": _fmt_ts(end),
            "duration_sec": round(end - start, 2),
            "clip": str(clip_path.relative_to(out_dir)).replace("\\", "/"),
            "caption_cues": len(caption_cues),
            "distribution": {},
            "commercial_qa": qa_by_index.get(i),
        })

    rendered_qa = rendered_qa_report(render_inputs, captions_burned=captions_burned)
    rendered_failures = rendered_qa["failures"]
    (out_dir / "00-rendered-qa.json").write_text(
        json.dumps(rendered_qa, indent=2),
        encoding="utf-8",
    )
    rendered_by_index = {clip.get("index"): clip for clip in rendered_qa.get("clips", [])}
    for record in clip_records:
        record["rendered_qa"] = rendered_by_index.get(record["index"])
    shorts_quality["rendered_qa"] = {
        "threshold": rendered_qa.get("threshold"),
        "approved_count": rendered_qa.get("approved_count"),
        "manual_review_count": rendered_qa.get("manual_review_count"),
        "passed": rendered_qa.get("passed"),
    }
    quality_failures = structural_failures + commercial_failures + rendered_failures
    shorts_quality["quality_failures"] = quality_failures
    shorts_quality["quality_gate_passed"] = not quality_failures
    rendered_gate_failures = rendered_failures if rendered_qa_gate_enabled else []
    gate_failures = []
    if structural_failures and quality_gate_enabled:
        gate_failures.extend(structural_failures)
    if commercial_failures and commercial_qa_gate_enabled:
        gate_failures.extend(commercial_failures)
    if rendered_gate_failures:
        gate_failures.extend(rendered_gate_failures)
    shorts_quality["manual_review_required"] = bool(quality_failures and not gate_failures)
    shorts_quality["quality_gate_status"] = (
        "pass" if not quality_failures else "blocked" if gate_failures else "manual_review"
    )
    for item in rendered_qa["clips"]:
        log(f"Rendered QA clip {item['index']}: {item['score']}/100 ({item['status']})")
        if item["score"] < RENDERED_QA_THRESHOLD:
            log(f"Rendered QA review clip {item['index']}: {'; '.join(item['reasons'][:4])}")
    if rendered_gate_failures:
        write_manifest(clip_records)
        raise RuntimeError("Rendered video QA gate failed: " + "; ".join(rendered_gate_failures))

    if should_prepare_long_form(duration):
        step("Preparing long-form YouTube kit", 96)
        long_form_info = write_long_form_youtube_kit(
            out_dir,
            video_path,
            slug=slug,
            product=product,
            duration_sec=duration,
            shorts_count=len(clip_records),
            campaign_id=out_dir.name,
        )
        log("Prepared long-form YouTube kit with tracked website link")

    for i, short, clip_path in distribution_inputs:
        dist = write_distribution_kit(out_dir, short, clip_path, short_index=i, campaign_id=out_dir.name)
        clip_records[i - 1]["distribution"] = dist

    # Write a manifest summarizing clips + distribution kits
    write_manifest(clip_records)

    step("Done", 100)
    jobs.update_job(
        job_id,
        state="done",
        result_slug=out_dir.name,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        progress_pct=100,
    )


# ---------------------------------------------------------------------------
# CLI for local testing
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Shadow Edge Video Pipeline")
    p.add_argument("video", type=Path)
    p.add_argument("--slug", default=None)
    p.add_argument("--product", default="both", choices=["bb", "dg", "both"])
    p.add_argument("--provider", default="local", choices=["local", "assemblyai"])
    p.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL,
                   choices=["tiny", "base", "small", "medium", "large-v3"])
    p.add_argument("--premium", action="store_true")
    args = p.parse_args()

    job = jobs.create_job(
        kind="video",
        source_filename=args.video.name,
        meta={
            "provider": args.provider,
            "whisper_model": args.whisper_model,
            "product": args.product,
        },
    )
    print(f"Job {job.id} created.")
    process_video_job(
        job.id,
        args.video,
        slug=args.slug,
        product=args.product,
        transcribe_provider=args.provider,
        whisper_model=args.whisper_model,
        premium=args.premium,
    )
    final = jobs.get_job(job.id)
    print(f"Final state: {final.state if final else 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

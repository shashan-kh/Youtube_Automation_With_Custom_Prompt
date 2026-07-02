"""
handle_comment.py — processes all /commands posted on GitHub Issues.

Command flow:
  /approve-topic N  or  /custom-topic <text>
        ↓
  Bot posts "Please provide your script prompt" + default template
  Bot labels issue: await-prompt
        ↓
  User replies:
      /set-prompt <their custom prompt>   — use custom prompt
      /use-default-prompt                 — use the built-in template
        ↓
  Bot generates script → renders video → uploads unlisted preview
  Bot labels: await-video-approval
        ↓
  /approve-video  → schedules private → auto-publish
  /reject-video   → delete preview, reset
  /regenerate-video → rebuild same topic
  /reject-topic / /new-topic → fresh topic suggestions
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from gtts import gTTS
from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips

from common import (
    REPO,
    add_label,
    extract_json_block,
    get_logger,
    get_metadata_from_issue_body,
    get_slot_from_labels,
    gh,
    is_authorized_commenter,
    jaccard,
    normalize_key,
    post_comment,
    remove_label,
    set_metadata_in_issue_body,
)
from seo import generate_seo_package

log = get_logger("handle_comment")

# ── Optional OpenCV ────────────────────────────────────────────────────────────
try:
    import cv2
except Exception:
    cv2 = None

# ── Env ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")
GROQ_FALLBACKS = [
    m.strip()
    for m in os.getenv(
        "GROQ_FALLBACK_MODELS",
        "llama-3.3-70b-versatile,llama-3.1-8b-instant,"
        "openai/gpt-oss-20b,openai/gpt-oss-120b,"
        "groq/compound-mini,moonshotai/kimi-k2-instruct",
    ).split(",")
    if m.strip()
]

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
TTS_LANG       = os.getenv("TTS_LANG", "en")
BGM_URL        = os.getenv("BGM_URL", "").strip()
try:
    BGM_VOL = float(os.getenv("BGM_VOL", "0.07"))
except Exception:
    BGM_VOL = 0.07

IST        = timezone(timedelta(hours=5, minutes=30))
TARGET_LOW  = 35.0
TARGET_HIGH = 58.0
PREVIEW_MAX = 57.3
SAFE_TAGS   = ["health", "wellness", "habits", "selfcare", "sleep", "hydration"]

# ── Default script prompt (shown to user, editable) ───────────────────────────
DEFAULT_SCRIPT_PROMPT_TEMPLATE = """\
Act as a viral YouTube Shorts scriptwriter specializing in health content.

Write a script for a YouTube Short (under 60 seconds, ~130-150 words spoken)
on the topic: "{topic}"

Structure the flow internally (do not label or show these sections in the output):
- Open with a scroll-stopping hook (shocking stat, myth-bust, or bold question).
  No greetings — start mid-thought.
- Name the relatable pain point the viewer feels right now.
- Deliver a surprising cause or myth-busting reveal.
- Give 2-3 punchy, specific, actionable tips. Short sentences (max 8-10 words).
- End with a loop-back to the hook or a final twist for rewatch value.
- Close with one natural, non-salesy CTA (follow, comment a word, or save this).

Rules:
- Spoken, punchy, simple language — no complex clauses.
- No medical guarantees, but keep tone confident and energetic.
- Total script must read aloud in under 60 seconds.

Output ONLY the plain spoken script as continuous text.\
"""

PROMPT_REMINDER_HOURS = 1  # post reminder if no /set-prompt within this many hours


# ══════════════════════════════════════════════════════════════════════════════
# Context dataclass
# ══════════════════════════════════════════════════════════════════════════════

class Ctx:
    def __init__(self, event: dict, owner: str, repo: str) -> None:
        self.event        = event
        self.owner        = owner
        self.repo         = repo
        self.issue        = event["issue"]
        self.number: int  = self.issue["number"]
        self.comment_body = (event["comment"]["body"] or "").strip()
        self.issue_body   = (self.issue.get("body") or "")
        self.labels       = self.issue.get("labels", [])
        self.slot         = get_slot_from_labels(self.labels)


# ══════════════════════════════════════════════════════════════════════════════
# LLM script generation
# ══════════════════════════════════════════════════════════════════════════════

def _list_groq_models() -> list[str]:
    if not GROQ_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=30,
        )
        if r.status_code == 401:
            raise RuntimeError("GROQ_API_KEY invalid (401 Unauthorized).")
        if r.status_code == 403:
            raise RuntimeError("GROQ_API_KEY lacks permission (403 Forbidden).")
        if r.status_code == 200:
            return [d["id"] for d in r.json().get("data", []) if d.get("id")]
    except RuntimeError:
        raise
    except Exception as exc:
        log.warning("list_groq_models: %s", exc)
    return []


def _build_model_list() -> list[str]:
    env_models: list[str] = []
    if GROQ_MODEL:
        env_models.append(GROQ_MODEL)
    env_models.extend(GROQ_FALLBACKS)
    # deduplicate preserving order
    seen: set[str] = set()
    env_models = [m for m in env_models if not (m in seen or seen.add(m))]  # type: ignore

    available = _list_groq_models()
    if not available:
        return env_models or [GROQ_MODEL]

    ordered: list[str] = [m for m in env_models if m in available]
    for m in available:
        if m not in ordered:
            ordered.append(m)
    return ordered[:12]


def generate_script(topic: str, user_prompt: str) -> str:
    """
    Call Groq LLM with the user-supplied (or default) prompt.
    Returns plain text voiceover script.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret.")

    models = _build_model_list()
    last_err: Exception | None = None

    for model in models:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": user_prompt}],
                    "temperature": 0.85,
                },
                timeout=120,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
                if content:
                    return content
                last_err = RuntimeError(f"Model '{model}' returned empty content.")
            else:
                msg = r.json().get("error", {}).get("message", r.text)
                last_err = RuntimeError(f"Model '{model}' failed: {msg}")
        except Exception as exc:
            last_err = exc

    raise last_err or RuntimeError("All Groq models failed.")


# ══════════════════════════════════════════════════════════════════════════════
# Audio / TTS
# ══════════════════════════════════════════════════════════════════════════════

def synthesize_speech(text: str, out_path: str, lang: str = "en") -> str:
    """Try gTTS → fallback system espeak."""
    try:
        gTTS(text, lang=lang).save(out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            return out_path
    except Exception as exc:
        log.warning("gTTS failed: %s — trying espeak", exc)

    wav = out_path.replace(".mp3", "_espeak.wav")
    try:
        subprocess.run(
            ["espeak-ng", "-v", lang, "-s", "155", "-w", wav, text],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav, out_path],
            check=True, capture_output=True,
        )
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            return out_path
    except Exception as exc:
        log.warning("espeak failed: %s", exc)

    raise RuntimeError("All TTS engines failed.")


def ensure_voice_in_range(
    voice_path: str,
    min_sec: float = TARGET_LOW,
    max_sec: float = PREVIEW_MAX,
) -> tuple[str, float]:
    a = AudioFileClip(voice_path)
    dur = a.duration
    a.close()
    if min_sec <= dur <= max_sec:
        return voice_path, dur
    target = max(min_sec, min(max_sec, dur))
    factor = max(0.5, min(2.0, target / dur if dur else 1.0))
    out = voice_path.replace(".mp3", "_adj.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", voice_path, "-filter:a", f"atempo={factor}", "-vn", out],
        check=True,
    )
    a2 = AudioFileClip(out)
    d2 = a2.duration
    a2.close()
    return out, d2


# ══════════════════════════════════════════════════════════════════════════════
# Captions (faster-whisper)
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_time(t: float) -> str:
    h  = int(t // 3600)
    m  = int((t % 3600) // 60)
    s  = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe_to_srt(audio_path: str, srt_path: str, lang: str = "en") -> bool:
    try:
        a = AudioFileClip(audio_path)
        audio_dur = float(a.duration)
        a.close()
    except Exception:
        audio_dur = 10.0

    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(
            os.getenv("WHISPER_MODEL", "tiny.en"),
            device="cpu", compute_type="int8",
        )
        segments, _ = model.transcribe(
            audio_path, language=lang, beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 200},
            word_timestamps=True,
        )
        words: list[tuple[str, float, float]] = []
        for seg in segments:
            for w in seg.words or []:
                txt = (w.word or "").strip()
                if txt:
                    words.append((txt, float(w.start), float(w.end)))

        if not words:
            raise RuntimeError("No words from whisper.")

        # Group into 2-3 word chunks
        chunks: list[tuple[str, float, float]] = []
        i = 0
        while i < len(words):
            remaining = len(words) - i
            take = min(3, max(2, remaining))
            if remaining - take == 1 and take > 2:
                take -= 1
            grp = words[i : i + take]
            chunks.append((" ".join(w[0] for w in grp), grp[0][1], grp[-1][2]))
            i += take

        lines: list[str] = []
        idx = 1
        cur_t = max(0.0, chunks[0][1]) if chunks else 0.0
        gap   = 0.02
        for text, w_start, w_end in chunks:
            start = max(cur_t, w_start)
            end   = max(start + 0.35, w_end)
            start = min(start, max(0.0, audio_dur - 0.01))
            end   = min(end, audio_dur)
            if end <= start:
                continue
            lines.append(f"{idx}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{text}\n\n")
            idx  += 1
            cur_t = end + gap
            if cur_t >= audio_dur:
                break

        if not lines:
            lines.append(f"1\n{_fmt_time(0.0)} --> {_fmt_time(audio_dur)}\n \n\n")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True

    except Exception as exc:
        log.warning("Transcription failed: %s", exc)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{_fmt_time(0.0)} --> {_fmt_time(audio_dur)}\n \n\n")
        return False


def _avfilter_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")


def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve())
    style = (
        "Fontname=DejaVu Sans,Fontsize=13,Bold=1,"
        "PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,"
        "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=100,Spacing=0"
    )
    vf = f"subtitles={_avfilter_escape(srt_abs)}:force_style={_avfilter_escape(style)}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", in_mp4, "-vf", vf, "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4],
        check=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Smart cover-crop (face-aware)
# ══════════════════════════════════════════════════════════════════════════════

_cascade = None


def _get_cascade():
    global _cascade
    if _cascade is None and cv2 is not None:
        try:
            _cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
        except Exception:
            pass
    return _cascade


def smart_cover_crop(clip, target_w: int = 1080, target_h: int = 1920):
    scale = max(target_w / clip.w, target_h / clip.h)
    resized = clip.resize(scale)
    cx, cy = resized.w / 2, resized.h / 2
    try:
        t = min(max(0.0, 0.5 * resized.duration), max(0.0, resized.duration - 0.05))
        frame = resized.get_frame(t)
        if cv2 is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            cas  = _get_cascade()
            faces = cas.detectMultiScale(gray, 1.1, 5, minSize=(60, 60)) if cas is not None else []
            if len(faces):
                best = max(faces, key=lambda f: f[2] * f[3])
                cx = best[0] + best[2] / 2
                cy = best[1] + best[3] / 2
    except Exception:
        pass
    cx = max(target_w / 2, min(resized.w - target_w / 2, cx))
    cy = max(target_h / 2, min(resized.h - target_h / 2, cy))
    return resized.crop(x_center=cx, y_center=cy, width=target_w, height=target_h).set_audio(None)


# ══════════════════════════════════════════════════════════════════════════════
# B-roll (parallel Pexels)
# ══════════════════════════════════════════════════════════════════════════════

_BROLL_FALLBACKS = [
    "healthy lifestyle", "fitness", "sleep", "hydration",
    "walking", "stretching", "posture", "nutrition", "yoga", "meditation",
]


def fetch_broll(query: str, need: int = 8) -> list[str]:
    if not PEXELS_API_KEY:
        raise RuntimeError("Missing PEXELS_API_KEY.")
    headers = {"Authorization": PEXELS_API_KEY}
    all_queries = [query] + _BROLL_FALLBACKS

    def _one(q: str) -> list[str]:
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": q, "per_page": 15, "min_height": 1080},
                timeout=30,
            )
            if r.status_code in (401, 403):
                raise RuntimeError(f"Pexels auth error {r.status_code}")
            links = []
            for v in r.json().get("videos", []):
                files = sorted(
                    v.get("video_files", []),
                    key=lambda f: f.get("height", 0),
                    reverse=True,
                )
                for f in files:
                    if f.get("height", 0) >= 1080:
                        links.append(f["link"])
                        break
            return links
        except RuntimeError:
            raise
        except Exception as exc:
            log.warning("Pexels [%s]: %s", q, exc)
            return []

    collected: list[str] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_one, q): q for q in all_queries}
        for fut in as_completed(futs):
            try:
                for link in fut.result():
                    if link not in seen:
                        seen.add(link)
                        collected.append(link)
            except RuntimeError:
                raise
    import random
    random.shuffle(collected)
    return collected[: need * 2][:need]


# ══════════════════════════════════════════════════════════════════════════════
# Background music
# ══════════════════════════════════════════════════════════════════════════════

def _validate_bgm_url(url: str) -> None:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"BGM_URL must use https, got: {parsed.scheme!r}")


def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    if BGM_URL:
        try:
            _validate_bgm_url(BGM_URL)
            ext = ".mp3"
            for suffix, ct_check in [(".m4a", "aac"), (".ogg", "ogg")]:
                if BGM_URL.lower().endswith(suffix):
                    ext = suffix
            raw = "bgm_raw" + ext
            with requests.get(BGM_URL, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(raw, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-stream_loop", "-1", "-i", raw,
                    "-t", f"{duration + 0.5:.2f}",
                    "-af",
                    f"afade=t=in:st=0:d=0.8,afade=t=out:st={max(0.0, duration - 0.8):.2f}:d=0.8",
                    "-c:a", "aac", "-b:a", "128k", out_path,
                ],
                check=True,
            )
            return out_path
        except Exception as exc:
            log.warning("BGM fetch failed (%s), using synthesized tone.", exc)

    # Synthesised ambient tone
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-t", f"{duration + 0.3:.2f}", "-i", "sine=frequency=432:sample_rate=44100",
            "-f", "lavfi", "-t", f"{duration + 0.3:.2f}", "-i", "sine=frequency=528:sample_rate=44100",
            "-filter_complex",
            f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,"
            f"lowpass=f=1200,"
            f"afade=t=in:st=0:d=0.8,"
            f"afade=t=out:st={max(0.0, duration - 0.8):.2f}:d=0.8",
            "-c:a", "aac", "-b:a", "128k", out_path,
        ],
        check=True,
    )
    return out_path


def add_bgm(in_mp4: str, out_mp4: str, duration: float) -> None:
    bgm = ensure_bgm_track(duration)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", in_mp4, "-i", bgm,
            "-filter_complex",
            f"[1:a]volume={BGM_VOL}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=async=1[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", out_mp4,
        ],
        check=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════════════

def _download_broll(urls: list[str], tmp: Path) -> list[str]:
    local: list[str] = []
    for i, u in enumerate(urls):
        if not u:
            continue
        p = tmp / f"b{i}.mp4"
        try:
            with requests.get(u, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
            local.append(str(p))
        except Exception as exc:
            log.warning("B-roll download [%d]: %s", i, exc)
    return local


def render_and_cap(
    broll_urls: list[str],
    voice_mp3: str,
    temp_mp4: str,
    final_mp4: str,
    target_w: int = 1080,
    target_h: int = 1920,
) -> float:
    tmp = Path(tempfile.mkdtemp())
    local = _download_broll(broll_urls, tmp)
    if not local:
        raise RuntimeError("No b-roll clips downloaded.")

    raw_clips: list[VideoFileClip] = []
    cropped:   list              = []
    voice: AudioFileClip | None  = None
    merged = None
    try:
        for p in local:
            c = VideoFileClip(p)
            raw_clips.append(c)
            take = min(8, max(4, int(c.duration)))
            comp = smart_cover_crop(c.subclip(0, take), target_w=target_w, target_h=target_h).set_audio(None)
            cropped.append(comp)

        merged = concatenate_videoclips(cropped, method="compose")
        voice  = AudioFileClip(voice_mp3)

        eps = 1e-2
        end = max(0.2, min(PREVIEW_MAX, min(merged.duration, voice.duration) - eps))
        out_clip = merged.subclip(0, end).set_audio(voice.subclip(0, end))
        out_clip.write_videofile(
            temp_mp4, fps=30, codec="libx264", audio_codec="aac",
            threads=2, preset="fast", verbose=False, logger=None,
            ffmpeg_params=["-pix_fmt", "yuv420p"],
        )
        out_clip.close()
    finally:
        for c in raw_clips:
            try: c.close()
            except Exception: pass
        for c in cropped:
            try: c.close()
            except Exception: pass
        if voice is not None:
            try: voice.close()
            except Exception: pass
        if merged is not None:
            try: merged.close()
            except Exception: pass
        shutil.rmtree(tmp, ignore_errors=True)

    # Captions
    srt = "cap.srt"
    transcribe_to_srt(voice_mp3, srt)
    subbed = "subbed.mp4"
    burn_captions(temp_mp4, srt, subbed)

    # BGM
    bgm_out = "with_bgm.mp4"
    add_bgm(subbed, bgm_out, end)

    # Safety trim
    v = VideoFileClip(bgm_out)
    d = v.duration
    v.close()
    out_path = bgm_out
    if d >= 58.0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", bgm_out, "-t", str(PREVIEW_MAX), "-c", "copy", "trim_final.mp4"],
            check=False,
        )
        if os.path.exists("trim_final.mp4"):
            out_path = "trim_final.mp4"

    os.replace(out_path, final_mp4)
    v2 = VideoFileClip(final_mp4)
    d2 = v2.duration
    v2.close()
    return d2


# ══════════════════════════════════════════════════════════════════════════════
# YouTube upload
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_google():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return Credentials, build, MediaFileUpload
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--upgrade",
             "google-api-python-client", "google-auth", "google-auth-oauthlib", "packaging>=23.1"],
            check=True,
        )
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return Credentials, build, MediaFileUpload


def _yt_client():
    for v in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        if not os.getenv(v):
            raise RuntimeError(f"Missing secret: {v}")
    Credentials, build, _ = _ensure_google()
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID"),
        client_secret=os.getenv("YT_CLIENT_SECRET"),
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
        ],
    )
    return build("youtube", "v3", credentials=creds)


def upload_unlisted(video_path: str, title: str, description: str, tags_csv: str) -> tuple[str, str]:
    _, _, MediaFileUpload = _ensure_google()
    yt = _yt_client()
    tags = [t.strip() for t in tags_csv.split(",") if t.strip()][:12]
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": "27",
            "defaultLanguage": "en",
        },
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req  = _yt_client().videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid_id = resp["id"]
    return vid_id, f"https://youtu.be/{vid_id}"


def _compress_preview(in_path: str, out_path: str = "preview_small.mp4") -> str:
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", in_path,
            "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_path,
        ],
        check=True,
    )
    return out_path


def _fallback_upload(video_path: str) -> tuple[str | None, str | None]:
    try:
        small = _compress_preview(video_path)
    except Exception:
        small = video_path

    for host, fn in [
        ("0x0.st",    lambda p: requests.post("https://0x0.st",    files={"file": (os.path.basename(p), open(p,"rb"), "video/mp4")}, timeout=180)),
        ("transfer.sh", lambda p: requests.put(f"https://transfer.sh/{os.path.basename(p)}", data=open(p,"rb"), timeout=300)),
    ]:
        try:
            r = fn(small)
            if r.status_code in (200, 201) and r.text.strip().startswith("http"):
                return r.text.strip(), host
        except Exception:
            pass
    return None, None


def upload_preview(video_path: str, title: str, description: str, tags_csv: str) -> dict:
    try:
        vid_id, link = upload_unlisted(video_path, title, description, tags_csv)
        return {"preview_video_id": vid_id, "preview_link": link, "yt_upload_blocked": False}
    except Exception as exc:
        msg = str(exc)
        blocked = "uploadLimitExceeded" in msg
        link, host = _fallback_upload(video_path)
        return {
            "preview_video_id": None,
            "preview_link": link,
            "yt_upload_blocked": blocked,
            "fallback": host,
            "error": msg if not link else None,
        }


def schedule_video(video_id: str, slot: str) -> str:
    yt = _yt_client()
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    hour = 16 if slot == "afternoon" else 9
    ist_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, 0, tzinfo=IST)
    publish_utc = ist_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    yt.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_utc,
                "selfDeclaredMadeForKids": False,
            },
        },
    ).execute()
    return publish_utc


def yt_delete(video_id: str) -> None:
    _yt_client().videos().delete(id=video_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# Preview video lookup helpers
# ══════════════════════════════════════════════════════════════════════════════

def _extract_yt_id(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"<!--\s*preview_video_id:\s*([A-Za-z0-9_-]{11})\s*-->",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/(?:watch\?[^ \n]*v=|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def find_preview_id(owner: str, repo: str, number: int, issue_body: str) -> str | None:
    meta = get_metadata_from_issue_body(issue_body) or {}
    if meta.get("preview_video_id"):
        return meta["preview_video_id"]
    vid = _extract_yt_id(issue_body)
    if vid:
        return vid
    try:
        r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
               params={"per_page": 100})
        for c in reversed(r.json() if isinstance(r.json(), list) else []):
            vid = _extract_yt_id(c.get("body") or "")
            if vid:
                return vid
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Build preview (with retry loop)
# ══════════════════════════════════════════════════════════════════════════════

def _duration_score(d: float) -> float:
    if TARGET_LOW <= d < TARGET_HIGH:
        return 0.0
    return (TARGET_LOW - d) if d < TARGET_LOW else (d - TARGET_HIGH)


def build_preview(
    topic: str,
    user_prompt: str,
    slot: str,
    issue_body: str,
    max_attempts: int = 3,
) -> tuple[dict | None, str]:
    best: dict = {
        "path": None, "dur": None, "score": float("inf"),
        "attempt": 0, "script": None, "seo": None,
    }

    for attempt in range(1, max_attempts + 1):
        log.info("Render attempt %d/%d for topic: %s", attempt, max_attempts, topic)

        # 1. Generate script
        try:
            script_text = generate_script(topic, user_prompt)
        except Exception as exc:
            log.error("Script gen attempt %d: %s", attempt, exc)
            continue

        # Add sign-off if missing
        if "thank you for watching" not in script_text.lower():
            script_text = script_text.rstrip(" .!?,") + ". Thank you for watching."

        # 2. Generate SEO package
        try:
            seo = generate_seo_package(topic, script_text)
        except Exception as exc:
            log.warning("SEO gen attempt %d: %s", attempt, exc)
            seo = {
                "title": f"{topic} #Shorts",
                "description": f"{topic}. Educational only, not medical advice.",
                "tags_csv": "health,wellness,shorts",
            }

        # 3. TTS
        voice_raw = f"voice_{attempt}.mp3"
        try:
            synthesize_speech(script_text, voice_raw, lang=TTS_LANG)
            voice, vdur = ensure_voice_in_range(voice_raw)
        except Exception as exc:
            log.error("TTS attempt %d: %s", attempt, exc)
            continue

        # 4. B-roll
        try:
            broll = fetch_broll(topic, need=8)
        except Exception as exc:
            log.error("B-roll attempt %d: %s", attempt, exc)
            continue
        if not broll:
            log.warning("No b-roll on attempt %d.", attempt)
            continue

        # 5. Render
        temp_mp4  = f"temp_{attempt}.mp4"
        final_mp4 = f"short_{attempt}.mp4"
        try:
            dur = render_and_cap(broll, voice, temp_mp4, final_mp4)
        except Exception as exc:
            log.error("Render attempt %d: %s", attempt, exc)
            continue

        score = _duration_score(dur)
        if score < best["score"]:
            best_path = f"best_{attempt}.mp4"
            shutil.copyfile(final_mp4, best_path)
            best.update({
                "path": best_path, "dur": round(dur, 2), "score": score,
                "attempt": attempt, "script": script_text, "seo": seo,
            })

        if TARGET_LOW <= dur < TARGET_HIGH:
            return _package_and_upload(
                final_mp4, seo, topic, slot, issue_body, attempt, dur, outside_target=False
            )

    # Best candidate (possibly outside target)
    if best["path"] and os.path.exists(best["path"]):
        return _package_and_upload(
            best["path"], best["seo"], topic, slot, issue_body,
            best["attempt"], best["dur"] or 0.0, outside_target=True
        )
    return None, issue_body


def _package_and_upload(
    video_path: str,
    seo: dict,
    topic: str,
    slot: str,
    issue_body: str,
    attempt: int,
    dur: float,
    outside_target: bool,
) -> tuple[dict, str]:
    info = upload_preview(video_path, seo["title"], seo["description"], seo["tags_csv"])
    if not info.get("preview_link"):
        raise RuntimeError(
            f"Upload failed and fallback unavailable: {info.get('error', 'unknown')}"
        )
    meta = {
        "topic": topic,
        "title": seo["title"],
        "description": seo["description"],
        "tags_csv": seo["tags_csv"],
        "preview_video_id": info.get("preview_video_id"),
        "preview_link": info["preview_link"],
        "yt_upload_blocked": bool(info.get("yt_upload_blocked")),
        "fallback_host": info.get("fallback"),
        "slot": slot,
        "created_at": datetime.utcnow().isoformat(),
        "duration_sec": round(dur, 2),
        "attempt": attempt,
        "outside_target": outside_target,
    }
    new_body = set_metadata_in_issue_body(issue_body, meta)
    return meta, new_body


# ══════════════════════════════════════════════════════════════════════════════
# Comment helpers
# ══════════════════════════════════════════════════════════════════════════════

def _post_preview_comment(owner: str, repo: str, number: int, meta: dict, slot: str, prefix: str = "") -> None:
    blocked = "\n⚠️ YouTube upload limit reached. Preview hosted temporarily." if meta.get("yt_upload_blocked") else ""
    outside = f"\n⚠️ Duration {meta['duration_sec']}s is outside target 35–58s. You can still approve." if meta.get("outside_target") else ""
    body = (
        f"{prefix}\n\n" if prefix else ""
    ) + (
        f"🎬 **Preview ready** (attempt {meta['attempt']}, **{meta['duration_sec']}s**)\n"
        f"{meta['preview_link']}{blocked}{outside}\n\n"
        f"Reply:\n"
        f"- `/approve-video` — schedule PRIVATE → auto-publish next day ({slot} slot)\n"
        f"- `/reject-video` — delete preview and pick a new topic\n"
        f"- `/regenerate-video` — rebuild the same topic\n\n"
        f"<!-- preview_video_id: {meta.get('preview_video_id', '')} -->"
    )
    post_comment(owner, repo, number, body)


def _post_prompt_request(owner: str, repo: str, number: int, topic: str) -> None:
    default = DEFAULT_SCRIPT_PROMPT_TEMPLATE.format(topic=topic)
    body = (
        f"✅ **Topic approved:** `{topic}`\n\n"
        f"Please provide your **script prompt** so I can write the video script.\n\n"
        f"**Option A — Use your own prompt:**\n"
        f"Reply with:\n"
        f"```\n/set-prompt <your full prompt here>\n```\n\n"
        f"**Option B — Use the default template (pre-filled below):**\n"
        f"Reply with exactly:\n"
        f"```\n/use-default-prompt\n```\n\n"
        f"---\n"
        f"**Default prompt (copy, edit if needed, then reply with `/set-prompt ...`):**\n"
        f"```\n{default}\n```\n\n"
        f"⏰ If no prompt is received within **{PROMPT_REMINDER_HOURS} hour(s)**, "
        f"I will post a reminder."
    )
    post_comment(owner, repo, number, body)


def _parse_topics_from_body(body: str) -> list[str]:
    lines = body.splitlines()
    out: list[str] = []
    for ln in lines:
        m = re.match(r"^\s*(?:[-*•]\s*)?(\d+)[\)\.\-:]\s+(.+\S)", ln)
        if m:
            out.append(m.group(2).strip())
    if not out:
        meta = get_metadata_from_issue_body(body) or {}
        out = [str(t) for t in (meta.get("topics") or []) if str(t).strip()]
    return out[:6]


# ══════════════════════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════════════════════

def handle_reject_topic(ctx: Ctx) -> None:
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-IN", tz=330)
        seeds = [
            "how to wake up early", "deep sleep techniques", "gut health tips",
            "morning energy routine", "stress relief breathing",
            "posture correction", "intermittent fasting", "cold shower benefits",
            "brain health habits", "back pain relief desk",
        ]
        found: list[str] = []
        try:
            df = pt.realtime_trending_searches(pn="IN")
            if df is not None and "title" in df.columns:
                from propose import is_english, has_health_signal, clean_to_topic
                for t in df["title"].tolist():
                    if is_english(t) and has_health_signal(t):
                        found.append(clean_to_topic(t))
        except Exception:
            pass
        options = list(dict.fromkeys(found + seeds))[:6]
    except Exception:
        options = [
            "The 4-7-8 breathing method that fixes insomnia in 7 days",
            "3 morning habits that boost energy without caffeine",
            "The gut bacteria mistake 90% of people make daily",
        ]

    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topics"] = options
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(options, 1))
    new_body += f"\n\nNew topic options:\n{numbered}\n\nReply with /approve-topic 1 (or 2-6), or /custom-topic Your Topic"
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": new_body})
    add_label(ctx.owner, ctx.repo, ctx.number, "await-topic-approval")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-prompt")
    post_comment(ctx.owner, ctx.repo, ctx.number,
                 f"Fresh topic options:\n{numbered}\n\nReply `/approve-topic N` or `/custom-topic Your Topic`")


def _start_after_topic(ctx: Ctx, topic: str) -> None:
    """Called after any topic is confirmed. Posts prompt request, sets label."""
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topic"] = topic
    meta["prompt_requested_at"] = datetime.utcnow().isoformat()
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": new_body})
    add_label(ctx.owner, ctx.repo, ctx.number, "await-prompt")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-topic-approval")
    _post_prompt_request(ctx.owner, ctx.repo, ctx.number, topic)


def handle_custom_topic(ctx: Ctx) -> None:
    topic = ctx.comment_body[len("/custom-topic"):].strip()
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "Please provide a topic. Example:\n`/custom-topic The 4-7-8 breathing trick for deep sleep`")
        return
    _start_after_topic(ctx, topic)


def handle_approve_topic(ctx: Ctx) -> None:
    topics = _parse_topics_from_body(ctx.issue_body)
    parts = ctx.comment_body.split(maxsplit=1)
    if len(parts) > 1 and not parts[1].strip().isdigit():
        topic = parts[1].strip()
    else:
        if not topics:
            post_comment(ctx.owner, ctx.repo, ctx.number,
                         "Couldn't detect topics in issue body. Use `/new-topic` or `/custom-topic Your Topic`.")
            return
        idx = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 1
        if not (1 <= idx <= len(topics)):
            post_comment(ctx.owner, ctx.repo, ctx.number,
                         f"Invalid index {idx}. Valid range: 1–{len(topics)}.")
            return
        topic = topics[idx - 1]
    _start_after_topic(ctx, topic)


def handle_set_prompt(ctx: Ctx) -> None:
    """User supplies their custom script prompt."""
    user_prompt = ctx.comment_body[len("/set-prompt"):].strip()
    if not user_prompt:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "Prompt was empty. Reply `/set-prompt <your full prompt>` or `/use-default-prompt`.")
        return
    _run_build_with_prompt(ctx, user_prompt)


def handle_use_default_prompt(ctx: Ctx) -> None:
    """User wants to use the default built-in prompt."""
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = meta.get("topic", "")
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "No approved topic found. Please `/approve-topic` or `/custom-topic` first.")
        return
    user_prompt = DEFAULT_SCRIPT_PROMPT_TEMPLATE.format(topic=topic)
    _run_build_with_prompt(ctx, user_prompt)


def _run_build_with_prompt(ctx: Ctx, user_prompt: str) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = meta.get("topic", "")
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "No approved topic found in metadata. Please `/approve-topic` or `/custom-topic` first.")
        return

    # Save prompt to metadata
    meta["user_prompt"] = user_prompt
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": new_body})

    remove_label(ctx.owner, ctx.repo, ctx.number, "await-prompt")
    post_comment(ctx.owner, ctx.repo, ctx.number,
                 f"✅ Prompt received. Building video for: **{topic}**\nThis may take 3–5 minutes…")

    video_meta, new_body2 = build_preview(
        topic, user_prompt, ctx.slot, new_body, max_attempts=3
    )
    if not video_meta:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "❌ Tried 3 times but couldn't produce a valid preview. "
                     "Reply `/regenerate-video`, `/new-topic`, or `/custom-topic Another Topic`.")
        return

    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": new_body2})
    add_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
    _post_preview_comment(ctx.owner, ctx.repo, ctx.number, video_meta, ctx.slot)


def handle_reject_video(ctx: Ctx) -> None:
    vid_id = find_preview_id(ctx.owner, ctx.repo, ctx.number, ctx.issue_body)
    deletion_msg = ""
    if vid_id:
        try:
            yt_delete(vid_id)
            deletion_msg = f"🗑️ Deleted unlisted preview (ID: `{vid_id}`)."
        except Exception as exc:
            deletion_msg = f"⚠️ Couldn't delete preview `{vid_id}`: {exc}"
    else:
        deletion_msg = "ℹ️ No preview video ID found to delete."

    try:
        meta = get_metadata_from_issue_body(ctx.issue_body) or {}
        for k in ["preview_video_id", "preview_link", "title", "description",
                  "tags_csv", "attempt", "duration_sec", "yt_upload_blocked",
                  "fallback_host", "outside_target"]:
            meta.pop(k, None)
        new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
        gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
           json={"body": new_body})
    except Exception as exc:
        deletion_msg += f"\n⚠️ Metadata cleanup error: {exc}"

    add_label(ctx.owner, ctx.repo, ctx.number, "await-topic-approval")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-prompt")
    post_comment(ctx.owner, ctx.repo, ctx.number,
                 f"{deletion_msg}\n\nReply `/new-topic` for fresh options or `/custom-topic Your Topic`.")


def handle_regenerate_video(ctx: Ctx) -> None:
    meta_old = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = meta_old.get("topic", "")
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "No topic in metadata. Use `/approve-topic` or `/custom-topic` first.")
        return

    # Retrieve previously stored prompt (or ask again)
    user_prompt = meta_old.get("user_prompt", "")

    # Delete old preview
    prev_vid = meta_old.get("preview_video_id") or find_preview_id(
        ctx.owner, ctx.repo, ctx.number, ctx.issue_body
    )
    deleted_msg = ""
    if prev_vid:
        try:
            yt_delete(prev_vid)
            deleted_msg = f"🗑️ Deleted previous preview (ID: `{prev_vid}`)."
        except Exception as exc:
            deleted_msg = f"⚠️ Couldn't delete `{prev_vid}`: {exc}"

    # Clear video fields but keep topic + prompt
    for k in ["preview_video_id", "preview_link", "title", "description",
              "tags_csv", "attempt", "duration_sec", "yt_upload_blocked",
              "fallback_host", "outside_target"]:
        meta_old.pop(k, None)
    cleared_body = set_metadata_in_issue_body(ctx.issue_body, meta_old)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": cleared_body})

    if not user_prompt:
        # No stored prompt — ask again
        add_label(ctx.owner, ctx.repo, ctx.number, "await-prompt")
        remove_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     f"{deleted_msg}\n\nNo stored prompt found. Please provide your script prompt again.")
        _post_prompt_request(ctx.owner, ctx.repo, ctx.number, topic)
        return

    post_comment(ctx.owner, ctx.repo, ctx.number,
                 f"{deleted_msg}\n\n🔄 Regenerating video for: **{topic}**…")
    video_meta, new_body = build_preview(topic, user_prompt, ctx.slot, cleared_body, max_attempts=3)
    if not video_meta:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "❌ Regeneration failed after 3 attempts. Try `/new-topic` or `/custom-topic`.")
        return
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"body": new_body})
    add_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
    _post_preview_comment(ctx.owner, ctx.repo, ctx.number, video_meta, ctx.slot, prefix=deleted_msg)


def handle_approve_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    if meta.get("yt_upload_blocked"):
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "⚠️ YouTube upload limit was reached. Please `/regenerate-video` after the limit resets (usually 24 h).")
        return
    if meta.get("fallback") and not meta.get("preview_video_id"):
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "⚠️ Preview is on a temporary host, not YouTube. Please `/regenerate-video` to get a proper YT preview before approving.")
        return

    vid_id = meta.get("preview_video_id") or find_preview_id(
        ctx.owner, ctx.repo, ctx.number, ctx.issue_body
    )
    if not vid_id:
        post_comment(ctx.owner, ctx.repo, ctx.number,
                     "No YouTube preview found. Please `/regenerate-video`.")
        return

    publish_utc = schedule_video(vid_id, meta.get("slot", "morning"))
    ist_display = (
        datetime.strptime(publish_utc, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .astimezone(IST)
        .strftime("%Y-%m-%d %H:%M")
    )
    link = f"https://youtu.be/{vid_id}"
    post_comment(ctx.owner, ctx.repo, ctx.number,
                 f"✅ **Scheduled!** {link}\n📅 Publishes at (IST): `{ist_display}`\n\nClosing this issue.")
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-video-approval")
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
       json={"state": "closed"})


# ══════════════════════════════════════════════════════════════════════════════
# Dispatch table
# ══════════════════════════════════════════════════════════════════════════════

_COMMANDS: list[tuple[str, object]] = [
    ("/reject-topic",      handle_reject_topic),
    ("/new-topic",         handle_reject_topic),
    ("/custom-topic",      handle_custom_topic),
    ("/approve-topic",     handle_approve_topic),
    ("/use-default-prompt", handle_use_default_prompt),
    ("/set-prompt",        handle_set_prompt),
    ("/reject-video",      handle_reject_video),
    ("/regenerate-video",  handle_regenerate_video),
    ("/approve-video",     handle_approve_video),
]


def safe_main() -> None:
    import json as _json

    with open(os.environ["GITHUB_EVENT_PATH"], "r", encoding="utf-8") as f:
        event = _json.load(f)

    # Validate repo
    event_repo = (event.get("repository") or {}).get("full_name", "")
    if event_repo != REPO:
        log.warning("Repo mismatch: %s != %s. Aborting.", event_repo, REPO)
        return

    if not is_authorized_commenter(event):
        log.info("Ignoring comment from non-collaborator/owner.")
        return

    owner, repo = REPO.split("/", 1)
    ctx = Ctx(event, owner, repo)
    comment_lower = ctx.comment_body.lower()

    for prefix, handler in _COMMANDS:
        if comment_lower.startswith(prefix):
            log.info("Dispatching command: %s", prefix)
            handler(ctx)
            return

    log.info("No matching command in comment: %r", ctx.comment_body[:80])


def main() -> None:
    try:
        safe_main()
    except Exception as exc:
        log.error("FATAL: %s\n%s", exc, traceback.format_exc())
        try:
            import json as _json
            with open(os.environ["GITHUB_EVENT_PATH"], "r", encoding="utf-8") as f:
                event = _json.load(f)
            owner, repo = REPO.split("/", 1)
            number = event["issue"]["number"]
            avail = _list_groq_models()
            note = ""
            if avail:
                note = "\n\nAvailable Groq models:\n- " + "\n- ".join(avail[:20])
            post_comment(owner, repo, number,
                         f"❌ **Error:** {exc}{note}\n```\n{traceback.format_exc()}\n```")
        except Exception as inner:
            log.error("Could not post error comment: %s", inner)


if __name__ == "__main__":
    main()

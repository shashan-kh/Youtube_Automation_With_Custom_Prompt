"""
handle_comment.py — processes all /commands posted on GitHub Issues.
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
    log.info("OpenCV available")
except Exception:
    cv2 = None
    log.info("OpenCV not available, using center crop")

# ── Env ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
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

IST         = timezone(timedelta(hours=5, minutes=30))
TARGET_LOW  = 35.0
TARGET_HIGH = 58.0
PREVIEW_MAX = 57.3
SAFE_TAGS   = ["health", "wellness", "habits", "selfcare", "sleep", "hydration"]

_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

def load_default_prompt() -> str:
    try:
        content = _PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content and "{topic}" in content:
            return content
    except Exception:
        pass
    return "Write a spoken voiceover script for a 50-second YouTube Short about: {topic}"

PROMPT_REMINDER_HOURS = 1

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

def _list_groq_models() -> list[str]:
    if not GROQ_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=30,
        )
        if r.status_code == 200:
            return [d["id"] for d in r.json().get("data", []) if d.get("id")]
    except Exception:
        pass
    return []

def _build_model_list() -> list[str]:
    env_models = []
    if GROQ_MODEL: env_models.append(GROQ_MODEL)
    env_models.extend(GROQ_FALLBACKS)
    seen = set()
    env_models = [m for m in env_models if not (m in seen or seen.add(m))]
    available = _list_groq_models()
    if not available: return env_models or [GROQ_MODEL]
    ordered = [m for m in env_models if m in available]
    for m in available:
        if m not in ordered: ordered.append(m)
    return ordered[:12]

# ── Meta-commentary detection and removal ─────────────────────────────────────
_META_LINE_PATTERNS = [
    re.compile(
        r"^(here(\s+is|\s*'s)\s+(the\s+)?(script|voiceover|short|video)|"
        r"sure[!,.]|of\s+course[!,.]|absolutely[!,.]|"
        r"let\s+me\s+|i\s+will\s+|i\s+'ll\s+|"
        r"the\s+(user|viewer|topic|following|hook)|"
        r"below\s+is|here\s+you\s+go|"
        r"as\s+requested|certainly[!,.])",
        re.I,
    ),
    re.compile(
        r"^(hook|pain\s*point|reveal|tip\s*\d*|cta|call\s+to\s+action|"
        r"script|voiceover|section|part\s*\d*|intro|outro|"
        r"loop|closing|opening)\s*[:\-–]",
        re.I,
    ),
    re.compile(r"^#{1,6}\s+", re.I),
    re.compile(r"^\[.*\]$", re.I),
    re.compile(r"^\(.*\)$", re.I),
    re.compile(r"^[-=*_]{3,}$"),
    re.compile(
        r"(the\s+user\s+wants|youtube\s+shorts?\s+script\s+(about|for|on)|"
        r"write\s+a\s+script|this\s+script\s+is|the\s+topic\s+is|"
        r"for\s+this\s+script|in\s+this\s+video\s+we\s+will\s+write)",
        re.I,
    ),
]

_INLINE_PREFIX_RE = re.compile(
    r"^(here(\s+is|\s*'s)\s+(the\s+)?(script|voiceover)[:\s]*|"
    r"sure[!,.]?\s*|of\s+course[!,.]?\s*|absolutely[!,.]?\s*|"
    r"script\s*:\s*|voiceover\s*:\s*)",
    re.I,
)

def _clean_script_output(raw: str) -> str:
    lines = raw.strip().splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped: continue
        # Skip full lines of meta commentary
        if any(p.search(stripped) for p in _META_LINE_PATTERNS):
            continue
        # Strip inline headers like "**Hook:**" or "Intro:"
        stripped = re.sub(r"(?i)^\**((The )?(Hook|Intro|Body|CTA|Voiceover|Script|Title|Subtitle|Part \d+|Section \d+))\**\s*[:-]\s*", "", stripped)
        kept.append(stripped)
        
    if not kept: return raw.strip()
    text = " ".join(kept)
    text = _INLINE_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"^[:\-–\s]+", "", text).strip()
    text = text.replace("**", "") # Remove bold markdown formatting
    text = text.strip('"\'') # Remove wrapping quotes
    return text

def _ensure_complete_ending(text: str) -> str:
    text = text.strip()
    if not text: return text
    if "thank you for watching" not in text.lower():
        text = text.rstrip(".!? ")
        text += ". Thank you for watching. Follow for more daily health tips."
    return text

def generate_script(topic: str, user_prompt: str) -> str:
    models = _build_model_list()
    last_err = None
    system_msg = (
        "You are a voiceover artist reading a script aloud. "
        "You only ever output the exact spoken words. "
        "You never output labels, headers, preamble, meta-commentary, "
        "or any text that would not be spoken aloud. "
        "Every response starts with the very first spoken word."
    )
    for model in models:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "temperature": 0.80,
                    "max_tokens": 400,
                },
                timeout=120,
            )
            if r.status_code != 200:
                last_err = RuntimeError(f"Model {model} HTTP {r.status_code}")
                continue
            raw = r.json()["choices"][0]["message"]["content"].strip()
            cleaned = _clean_script_output(raw)
            cleaned = _ensure_complete_ending(cleaned)
            if len(cleaned.split()) < 40:
                last_err = RuntimeError(f"Too few words from {model}")
                continue
            return cleaned
        except Exception as exc:
            last_err = exc
    raise last_err or RuntimeError("All Groq models failed to generate a script.")

# ══════════════════════════════════════════════════════════════════════════════
# TTS
# ══════════════════════════════════════════════════════════════════════════════

def _try_gtts(text: str, out_path: str, lang: str) -> bool:
    try:
        from gtts import gTTS
        gTTS(text, lang=lang, slow=False).save(out_path)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except Exception:
        return False

def _try_espeak(text: str, out_path: str, lang: str) -> bool:
    try:
        wav = out_path.replace(".mp3", "_espeak.wav")
        subprocess.run(["espeak-ng", "-v", lang, "-s", "150", "-w", wav, text], capture_output=True, timeout=60)
        if not os.path.exists(wav) or os.path.getsize(wav) < 512: return False
        subprocess.run(["ffmpeg", "-y", "-i", wav, "-ar", "22050", "-ac", "1", out_path], capture_output=True)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 512
    except Exception:
        return False

def synthesize_speech(text: str, out_path: str, lang: str = "en") -> str:
    for _ in range(3):
        if _try_gtts(text, out_path, lang): return out_path
        time.sleep(2)
    if _try_espeak(text, out_path, lang): return out_path
    raise RuntimeError("TTS failed")

def get_audio_duration(path: str) -> float:
    try:
        a = AudioFileClip(path)
        dur = float(a.duration)
        a.close()
        return dur
    except Exception:
        return 45.0

# ══════════════════════════════════════════════════════════════════════════════
# Captions (Restored Original Logic)
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_time(t: float) -> str:
    h  = int(t // 3600)
    m  = int((t % 3600) // 60)
    s  = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def transcribe_to_srt(audio_path: str, srt_path: str, lang: str = "en") -> bool:
    log.info("[srt] transcribing %s", audio_path)
    try:
        a         = AudioFileClip(audio_path)
        audio_dur = float(a.duration)
        a.close()
    except Exception:
        audio_dur = 45.0

    try:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "tiny.en")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")

        segments, _info = model.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
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
            raise RuntimeError("No word-level timestamps from whisper.")

        chunks: list[tuple[str, float, float]] = []
        i = 0
        while i < len(words):
            remaining = len(words) - i
            take = max(2, min(3, remaining))
            if remaining - take == 1 and take > 2:
                take -= 1
            grp = words[i : i + take]
            chunks.append(
                (" ".join(w[0] for w in grp), grp[0][1], grp[-1][2])
            )
            i += take

        out:    list[str] = []
        idx     = 1
        cur_t   = max(0.0, chunks[0][1]) if chunks else 0.0
        gap     = 0.02
        min_dur = 0.35

        for text, w_start, w_end in chunks:
            start = max(cur_t, w_start)
            end   = max(start + min_dur, w_end)
            start = min(start, max(0.0, audio_dur - 0.01))
            end   = min(end, audio_dur)
            if end <= start:
                continue
            out.append(
                f"{idx}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{text}\n\n"
            )
            idx  += 1
            cur_t = end + gap
            if cur_t >= audio_dur:
                break

        if not out:
            out.append(f"1\n{_fmt_time(0.0)} --> {_fmt_time(max(0.8, audio_dur))}\n \n\n")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        return True

    except Exception as exc:
        log.warning("[srt] failed: %s — writing blank srt", exc)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{_fmt_time(0.0)} --> {_fmt_time(max(0.8, audio_dur))}\n \n\n")
        return False

def _ffmpeg_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
         .replace(":", "\\:")
         .replace(",", "\\,")
         .replace("'", "\\'")
    )

def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve())
    style = (
        "Fontname=DejaVu Sans,Fontsize=12,Bold=1,"
        "PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,"
        "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=96,Spacing=0"
    )
    vf = (
        f"subtitles={_ffmpeg_escape(srt_abs)}"
        f":force_style={_ffmpeg_escape(style)}"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", in_mp4,
        "-vf", vf,
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        out_mp4,
    ], check=True, capture_output=True)

# ══════════════════════════════════════════════════════════════════════════════
# Rendering Helpers
# ══════════════════════════════════════════════════════════════════════════════

_cascade = None
def smart_cover_crop(clip, target_w=1080, target_h=1920):
    global _cascade
    if _cascade is None and cv2 is not None:
        try: _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception: pass
    
    scale = max(target_w / clip.w, target_h / clip.h)
    resized = clip.resize(scale)
    cx, cy = resized.w / 2, resized.h / 2
    
    try:
        if cv2 is not None and _cascade is not None:
            gray = cv2.cvtColor(resized.get_frame(resized.duration / 2), cv2.COLOR_RGB2GRAY)
            faces = _cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
            if len(faces):
                best = max(faces, key=lambda f: f[2] * f[3])
                cx = best[0] + best[2] / 2
                cy = best[1] + best[3] / 2
    except Exception: pass
    
    cx = max(target_w / 2, min(resized.w - target_w / 2, cx))
    cy = max(target_h / 2, min(resized.h - target_h / 2, cy))
    return resized.crop(x_center=cx, y_center=cy, width=target_w, height=target_h).set_audio(None)

def fetch_broll(query: str, need: int = 6) -> list[str]:
    headers = {"Authorization": PEXELS_API_KEY}
    all_queries = [query, "healthy lifestyle", "fitness exercise", "meditation"]
    collected, seen = [], set()
    def _one(q):
        try:
            r = requests.get("https://api.pexels.com/videos/search", headers=headers,
                             params={"query": q, "per_page": 15, "min_height": 720, "orientation": "portrait"}, timeout=30)
            if r.status_code == 200:
                links = []
                for v in r.json().get("videos", []):
                    for f in sorted(v.get("video_files", []), key=lambda x: x.get("height", 0), reverse=True):
                        if f.get("height", 0) >= 720 and f.get("link"):
                            links.append(f["link"])
                            break
                return links
        except Exception: pass
        return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in as_completed({ex.submit(_one, q): q for q in all_queries}):
            for link in fut.result():
                if link not in seen:
                    seen.add(link)
                    collected.append(link)
    import random
    random.shuffle(collected)
    return collected[:need]

def _download_broll(urls: list[str], tmp: Path) -> list[str]:
    local = []
    for i, u in enumerate(urls):
        p = tmp / f"b{i}.mp4"
        try:
            with requests.get(u, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in r.iter_content(1024*1024): f.write(chunk)
            if os.path.getsize(p) > 10240: local.append(str(p))
        except Exception: pass
    return local

def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 0.3:.2f}", "-i", "sine=frequency=432:sample_rate=44100",
        "-c:a", "aac", "-b:a", "128k", out_path
    ], check=True, capture_output=True)
    return out_path

def render_and_cap(broll_urls: list[str], voice_mp3: str, temp_mp4: str, final_mp4: str) -> float:
    tmp = Path(tempfile.mkdtemp())
    local = _download_broll(broll_urls, tmp)
    if not local: raise RuntimeError("No b-roll clips downloaded.")

    raw_clips, cropped, merged, voice_clip = [], [], None, None
    try:
        for p in local:
            try:
                c = VideoFileClip(p)
                raw_clips.append(c)
                comp = smart_cover_crop(c.subclip(0, min(8, max(4, int(c.duration))))).set_audio(None)
                cropped.append(comp)
            except Exception: pass
        if not cropped: raise RuntimeError("No clips loaded.")

        merged = concatenate_videoclips(cropped, method="compose")
        voice_clip = AudioFileClip(voice_mp3)
        end = max(0.2, min(PREVIEW_MAX, min(merged.duration, voice_clip.duration) - 0.01))
        
        out_clip = merged.subclip(0, end).set_audio(voice_clip.subclip(0, end))
        out_clip.write_videofile(temp_mp4, fps=30, codec="libx264", audio_codec="aac", threads=2, preset="fast", verbose=False, logger=None)
    finally:
        for c in raw_clips + cropped:
            try: c.close()
            except Exception: pass
        if voice_clip: voice_clip.close()
        if merged: merged.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # Restored Original Caption Logic Block
    srt_path = "cap.srt"
    srt_ok   = transcribe_to_srt(voice_mp3, srt_path, lang=TTS_LANG)
    log.info("[render] srt ok=%s", srt_ok)

    subbed = "subbed.mp4"
    try:
        burn_captions(temp_mp4, srt_path, subbed)
        log.info("[render] captions burned OK")
    except Exception as exc:
        log.error("[render] caption burn failed: %s", exc)
        try:
            log.info("[render] retrying with minimal SRT")
            v_dur = get_audio_duration(voice_mp3)
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(
                    f"1\n{_fmt_time(0.0)} --> "
                    f"{_fmt_time(max(1.0, v_dur - 0.5))}\n"
                    "Watch till end\n\n"
                )
            burn_captions(temp_mp4, srt_path, subbed)
            log.info("[render] minimal SRT captions OK")
        except Exception as exc2:
            log.error("[render] minimal caption also failed: %s", exc2)
            log.warning("[render] proceeding without captions")
            shutil.copyfile(temp_mp4, subbed)

    bgm_out = "with_bgm.mp4"
    try:
        bgm = ensure_bgm_track(end)
        subprocess.run([
            "ffmpeg", "-y", "-i", subbed, "-i", bgm, "-filter_complex",
            f"[1:a]volume={BGM_VOL}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=async=1[aout]",
            "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-shortest", bgm_out
        ], check=True, capture_output=True)
    except Exception: shutil.copyfile(subbed, bgm_out)

    os.replace(bgm_out, final_mp4)
    return get_audio_duration(final_mp4)

# ══════════════════════════════════════════════════════════════════════════════
# YouTube Upload & Integration
# ══════════════════════════════════════════════════════════════════════════════

def _yt_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=os.getenv("YT_REFRESH_TOKEN"), token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.getenv("YT_CLIENT_ID"), client_secret=os.getenv("YT_CLIENT_SECRET"))
    return build("youtube", "v3", credentials=creds)

def upload_unlisted(video_path: str, title: str, desc: str, tags: str) -> tuple[str, str]:
    from googleapiclient.http import MediaFileUpload
    yt = _yt_client()
    body = {
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": [t.strip() for t in tags.split(",")][:12], "categoryId": "27"},
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None: _, resp = req.next_chunk()
    return resp["id"], f"https://youtu.be/{resp['id']}"

def build_preview(topic: str, user_script: str | None, slot: str, issue_body: str) -> tuple[dict | None, str]:
    best = {"path": None, "dur": None, "score": float("inf"), "attempt": 0, "seo": None}
    for attempt in range(1, 4):
        try:
            if user_script:
                script_text = _ensure_complete_ending(user_script)
            else:
                prompt_tpl = load_default_prompt()
                actual_prompt = prompt_tpl.format(topic=topic)
                script_text = generate_script(topic, actual_prompt)
            
            seo = generate_seo_package(topic, script_text)
            voice_raw = f"voice_{attempt}.mp3"
            synthesize_speech(script_text, voice_raw, lang=TTS_LANG)
            
            broll = fetch_broll(topic, need=6)
            if not broll: broll = fetch_broll("healthy lifestyle", need=6)
            if not broll: continue
            
            temp_mp4, final_mp4 = f"temp_{attempt}.mp4", f"short_{attempt}.mp4"
            dur = render_and_cap(broll, voice_raw, temp_mp4, final_mp4)
            
            score = 0.0 if TARGET_LOW <= dur < TARGET_HIGH else abs(dur - 45.0)
            if score < best["score"]:
                best_path = f"best_{attempt}.mp4"
                shutil.copyfile(final_mp4, best_path)
                best.update({"path": best_path, "dur": round(dur, 2), "score": score, "attempt": attempt, "seo": seo})
            
            if TARGET_LOW <= dur < TARGET_HIGH:
                return _package_and_upload(final_mp4, seo, topic, slot, issue_body, attempt, dur)
        except Exception as exc:
            log.error("[build] attempt %d failed: %s", attempt, exc)
            
    if best["path"] and os.path.exists(best["path"]):
        return _package_and_upload(best["path"], best["seo"], topic, slot, issue_body, best["attempt"], best["dur"])
    return None, issue_body

def _package_and_upload(video_path, seo, topic, slot, issue_body, attempt, dur):
    try:
        vid_id, link = upload_unlisted(video_path, seo["title"], seo["description"], seo["tags_csv"])
        meta = {
            "topic": topic, "title": seo["title"], "description": seo["description"],
            "preview_video_id": vid_id, "preview_link": link, "slot": slot, "duration_sec": dur, "attempt": attempt
        }
        return meta, set_metadata_in_issue_body(issue_body, meta)
    except Exception as exc:
        raise RuntimeError(f"YT upload failed: {exc}")

# ══════════════════════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════════════════════

def _post_script_request(owner: str, repo: str, number: int, topic: str) -> None:
    body = (
        f"Topic approved: {topic}\n\n"
        "Please provide your script text or generate one automatically.\n\n"
        "Option A - Provide your own complete script:\n"
        "Reply with: `/set-script` followed by your full voiceover text.\n\n"
        "Option B - Use the AI to generate a script (default):\n"
        "Reply with exactly: `/use-default-prompt`\n\n"
        f"A reminder will be posted if no script is received within {PROMPT_REMINDER_HOURS} hour."
    )
    post_comment(owner, repo, number, body)

def handle_reject_topic(ctx: Ctx) -> None:
    try:
        from propose import gather_topics
        options = gather_topics(REGION, need=6, exclude=set())
    except Exception:
        options = ["Gut health basics", "Fixing posture in 2 minutes", "Better sleep habits", "Hydration tips", "Mental clarity routine", "Metabolism boosters"]
    
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topics"] = options
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(options, 1))
    new_body += f"\n\nNew topic options:\n{numbered}\n\nReply with /approve-topic 1 (or 2-6), or /custom-topic Your Topic"
    
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    post_comment(ctx.owner, ctx.repo, ctx.number, f"Fresh topic options:\n{numbered}\n\nReply /approve-topic N or /custom-topic Your Topic")

def _start_after_topic(ctx: Ctx, topic: str) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topic"] = topic
    meta.pop("user_script", None)
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    _post_script_request(ctx.owner, ctx.repo, ctx.number, topic)

def handle_custom_topic(ctx: Ctx) -> None:
    topic = ctx.comment_body[len("/custom-topic"):].strip()
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Please provide a topic. Example: /custom-topic The 4-7-8 breathing trick")
        return
    _start_after_topic(ctx, topic)

def handle_approve_topic(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topics = meta.get("topics", [])
    parts = ctx.comment_body.split(maxsplit=1)
    
    if len(parts) > 1 and not parts[1].strip().isdigit():
        topic = parts[1].strip()
    else:
        idx = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 1
        if not (1 <= idx <= len(topics)):
            post_comment(ctx.owner, ctx.repo, ctx.number, f"Invalid index {idx}.")
            return
        topic = topics[idx - 1]
    _start_after_topic(ctx, topic)

def handle_use_default_prompt(ctx: Ctx) -> None:
    _run_build_with_script(ctx, None)

def handle_set_script(ctx: Ctx) -> None:
    user_script = ctx.comment_body[len("/set-script"):].strip()
    if not user_script:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Script was empty. Reply `/set-script your full text` or `/use-default-prompt`.")
        return
    _run_build_with_script(ctx, user_script)

def _run_build_with_script(ctx: Ctx, user_script: str | None) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = meta.get("topic", "")
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number, "No approved topic found.")
        return

    if user_script: meta["user_script"] = user_script
    else: meta.pop("user_script", None)
    
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    post_comment(ctx.owner, ctx.repo, ctx.number, f"Script received. Building video for: {topic}\nThis takes 5-10 mins.")

    video_meta, new_body2 = build_preview(topic, user_script, ctx.slot, new_body)
    if not video_meta:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Could not produce a valid preview. Try /regenerate-video.")
        return
    
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body2})
    post_comment(ctx.owner, ctx.repo, ctx.number, f"Preview ready (attempt {video_meta['attempt']}, {video_meta['duration_sec']}s)\n{video_meta['preview_link']}\n\nReply:\n- /approve-video\n- /reject-video\n- /regenerate-video")

def handle_reject_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if vid_id:
        try: _yt_client().videos().delete(id=vid_id).execute()
        except Exception: pass
    
    for k in ["preview_video_id", "preview_link", "title", "description", "attempt", "duration_sec"]: meta.pop(k, None)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": set_metadata_in_issue_body(ctx.issue_body, meta)})
    post_comment(ctx.owner, ctx.repo, ctx.number, "Deleted preview. Reply /new-topic for fresh options or /custom-topic Your Topic.")

def handle_regenerate_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    if not meta.get("topic"): return
    vid_id = meta.get("preview_video_id")
    if vid_id:
        try: _yt_client().videos().delete(id=vid_id).execute()
        except Exception: pass
    
    user_script = meta.get("user_script")
    post_comment(ctx.owner, ctx.repo, ctx.number, "Regenerating video...")
    video_meta, new_body = build_preview(meta["topic"], user_script, ctx.slot, ctx.issue_body)
    if video_meta:
        gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
        post_comment(ctx.owner, ctx.repo, ctx.number, f"Preview ready (attempt {video_meta['attempt']}, {video_meta['duration_sec']}s)\n{video_meta['preview_link']}\n\nReply:\n- /approve-video\n- /reject-video\n- /regenerate-video")

def handle_approve_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if not vid_id: return
    
    yt = _yt_client()
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    hour = 16 if meta.get("slot") == "afternoon" else 9
    ist_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, 0, tzinfo=IST)
    publish_utc = ist_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    yt.videos().update(part="status", body={"id": vid_id, "status": {"privacyStatus": "private", "publishAt": publish_utc}}).execute()
    post_comment(ctx.owner, ctx.repo, ctx.number, f"Scheduled: https://youtu.be/{vid_id}\nPublishes at (IST): {ist_dt.strftime('%Y-%m-%d %H:%M')}\n\nClosing this issue.")
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"state": "closed"})

_COMMANDS = [
    ("/reject-topic",       handle_reject_topic),
    ("/new-topic",          handle_reject_topic),
    ("/custom-topic",       handle_custom_topic),
    ("/approve-topic",      handle_approve_topic),
    ("/use-default-prompt", handle_use_default_prompt),
    ("/set-script",         handle_set_script),
    ("/reject-video",       handle_reject_video),
    ("/regenerate-video",   handle_regenerate_video),
    ("/approve-video",      handle_approve_video),
]

def main() -> None:
    with open(os.environ["GITHUB_EVENT_PATH"], "r", encoding="utf-8") as f: event = json.load(f)
    if not is_authorized_commenter(event): return
    owner, repo = REPO.split("/", 1)
    ctx = Ctx(event, owner, repo)
    comment_lower = ctx.comment_body.lower()
    for prefix, handler in _COMMANDS:
        if comment_lower.startswith(prefix):
            handler(ctx)
            return

if __name__ == "__main__":
    main()

"""
handle_comment.py — processes all /commands posted on GitHub Issues.
"""
from __future__ import annotations

<<<<<<< HEAD
=======
import asyncio
>>>>>>> origin/main
import json
import os
import random
import re
import shutil
<<<<<<< HEAD
=======
import edge_tts
>>>>>>> origin/main
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
    REPO, add_label, extract_json_block, get_logger,
    get_metadata_from_issue_body, get_slot_from_labels, gh,
    is_authorized_commenter, jaccard, normalize_key, post_comment,
    remove_label, set_metadata_in_issue_body
)
from seo import generate_seo_package

log = get_logger("handle_comment")

try:
    import cv2
except Exception:
    cv2 = None

# ── Env ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACKS = [
    m.strip() for m in os.getenv(
        "GROQ_FALLBACK_MODELS",
        "llama-3.3-70b-versatile,llama-3.1-8b-instant,"
<<<<<<< HEAD
        "qwen/qwen3-32b,openai/gpt-oss-20b,openai/gpt-oss-120b,"
=======
        "openai/gpt-oss-20b,openai/gpt-oss-120b,"
>>>>>>> origin/main
        "groq/compound-mini,moonshotai/kimi-k2-instruct",
    ).split(",") if m.strip()
]

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
<<<<<<< HEAD
REGION         = os.getenv("REGION", "IN")
=======
>>>>>>> origin/main
TTS_LANG       = os.getenv("TTS_LANG", "en")
BGM_VOL        = 0.07

IST         = timezone(timedelta(hours=5, minutes=30))
<<<<<<< HEAD
TARGET_LOW  = 35.0
TARGET_HIGH = 58.0
PREVIEW_MAX = 57.3
=======
TARGET_LOW  = 45.0
TARGET_HIGH = 59.8
PREVIEW_MAX = 59.5
>>>>>>> origin/main
PROMPT_REMINDER_HOURS = 1

_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

<<<<<<< HEAD

=======
>>>>>>> origin/main
def load_default_prompt() -> str:
    try:
        content = _PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content and "{topic}" in content:
            return content
    except Exception:
        pass
    return "Write a spoken voiceover script for a 50-second YouTube Short about: {topic}"

<<<<<<< HEAD

=======
>>>>>>> origin/main
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

<<<<<<< HEAD

# ── LLM & Script Cleaning ──────────────────────────────────────────────────────
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


=======
# ── LLM & Script Cleaning ──────────────────────────────────────────────────────
def _list_groq_models() -> list[str]:
    if not GROQ_API_KEY: return []
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=30)
        if r.status_code == 200:
            return [d["id"] for d in r.json().get("data", []) if d.get("id")]
    except Exception: pass
    return []

>>>>>>> origin/main
def _build_model_list() -> list[str]:
    env_models = [GROQ_MODEL] + GROQ_FALLBACKS
    seen = set()
    env_models = [m for m in env_models if not (m in seen or seen.add(m))]
    available = _list_groq_models()
<<<<<<< HEAD
    if not available:
        return env_models[:6]
    ordered = [m for m in env_models if m in available]
    for m in available:
        if m not in ordered:
            ordered.append(m)
    return ordered[:12]


def _clean_script(text: str) -> str:
    # Remove <think>...</think> blocks from reasoning models like Qwen
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    # Remove markdown code fences if the LLM wrapped the output
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(
        r"(?i)^(Title|Topic|Script|Intro|Hook|Body|CTA|Voiceover|The hook is|The script starts here):?\s*",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(r"(?i)(Here is the script|Sure!|Certainly!|The topic is)[^\n]*\n?", "", text)
    text = text.replace("**", "").replace("*", "").strip('"\' \n')
    conclusion = "Thank you for watching. Follow for more daily health tips."
    if conclusion.lower() not in text.lower():
        text = text.rstrip(".!? ") + f". {conclusion}"
    return text


def generate_script(topic: str, user_prompt: str) -> str:
    models = _build_model_list()
    last_err = None
    system_msg = (
        "You are a professional voiceover artist and scriptwriter for YouTube Shorts. "
        "Output ONLY the exact speakable voiceover content for the video, exclusively and directly "
        "related to the chosen topic. Do NOT include <think> tags or meta commentary. "
        "Start DIRECTLY with the first spoken word."
    )
    for model in models:
        try:
            log.info("[script] Trying Groq model: %s", model)
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.75,
                    "max_tokens": 800,
                },
                timeout=120,
            )
            if r.status_code != 200:
                last_err = RuntimeError(f"Model {model} HTTP {r.status_code}: {r.text[:200]}")
                continue
            raw = r.json()["choices"][0]["message"]["content"].strip()
            cleaned = _clean_script(raw)
            if len(cleaned.split()) < 35:
                last_err = RuntimeError(f"Too few words ({len(cleaned.split())}) from {model}")
                continue
            log.info("[script] Generated %d words using %s", len(cleaned.split()), model)
=======
    if not available: return env_models[:6]
    ordered = [m for m in env_models if m in available]
    for m in available:
        if m not in ordered: ordered.append(m)
    return ordered[:12]

def _clean_script(text: str) -> str:
    text = re.sub(r"(?i)^(Title|Topic|Script|Intro|Hook|Body|CTA|Voiceover|The hook is|The script starts here):?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"(?i)(Here is the script|Sure!|Certainly!|The topic is)", "", text)
    text = text.replace("**", "").strip('"\' ')
    if not text.lower().endswith("thank you for watching. follow for more daily health tips."):
        text = text.rstrip(".!? ") + ". Thank you for watching. Follow for more daily health tips."
    return text

def generate_script(topic: str, user_prompt: str) -> str:
    models = _build_model_list()
    last_err = None
    system_msg = "You are a voiceover artist. Output ONLY the exact spoken words. Start DIRECTLY with the first word."
    for model in models:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": model, "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}], "temperature": 0.80, "max_tokens": 400},
                timeout=120,
            )
            if r.status_code != 200:
                last_err = RuntimeError(f"Model {model} HTTP {r.status_code}")
                continue
            raw = r.json()["choices"][0]["message"]["content"].strip()
            cleaned = _clean_script(raw)
            if len(cleaned.split()) < 40:
                last_err = RuntimeError(f"Too few words from {model}")
                continue
>>>>>>> origin/main
            return cleaned
        except Exception as exc:
            last_err = exc
    raise last_err or RuntimeError("All Groq models failed to generate a script.")

<<<<<<< HEAD

=======
>>>>>>> origin/main
# ── TTS & Audio Tools ─────────────────────────────────────────────────────────
def _try_gtts(text: str, out_path: str, lang: str) -> bool:
    try:
        from gtts import gTTS
        gTTS(text, lang=lang, slow=False).save(out_path)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
<<<<<<< HEAD
    except Exception as exc:
        log.warning("[tts] gTTS error: %s", exc)
        return False

=======
    except Exception: return False
>>>>>>> origin/main

def _try_espeak(text: str, out_path: str, lang: str) -> bool:
    try:
        wav = out_path.replace(".mp3", "_espeak.wav")
        subprocess.run(["espeak-ng", "-v", lang, "-s", "150", "-w", wav, text], capture_output=True, timeout=60)
<<<<<<< HEAD
        if not os.path.exists(wav) or os.path.getsize(wav) < 512:
            return False
        subprocess.run(["ffmpeg", "-y", "-i", wav, "-ar", "22050", "-ac", "1", out_path], capture_output=True)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 512
    except Exception as exc:
        log.warning("[tts] espeak error: %s", exc)
        return False


def synthesize_speech(text: str, out_path: str, lang: str = "en") -> str:
    for _ in range(3):
        if _try_gtts(text, out_path, lang):
            return out_path
        time.sleep(2)
    if _try_espeak(text, out_path, lang):
        return out_path
    raise RuntimeError("TTS synthesis failed with both gTTS and espeak-ng")

=======
        if not os.path.exists(wav) or os.path.getsize(wav) < 512: return False
        subprocess.run(["ffmpeg", "-y", "-i", wav, "-ar", "22050", "-ac", "1", out_path], capture_output=True)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 512
    except Exception: return False

def _try_edge_tts(text: str, out_path: str) -> bool:
    try:
        # en-US-ChristopherNeural or en-US-AriaNeural
        voice = "en-US-ChristopherNeural"
        # snappy Shorts pacing: +10%
        communicate = edge_tts.Communicate(text, voice, rate="+10%")
        asyncio.run(communicate.save(out_path))
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except Exception as exc:
        log.warning("[edge-tts] failed: %s", exc)
        return False

def synthesize_speech(text: str, out_path: str, lang: str = "en") -> str:
    # Try edge-tts first for high quality
    if _try_edge_tts(text, out_path):
        return out_path
    
    # Fallbacks
    for _ in range(3):
        if _try_gtts(text, out_path, lang): return out_path
        time.sleep(2)
    if _try_espeak(text, out_path, lang): return out_path
    raise RuntimeError("TTS failed")
>>>>>>> origin/main

def get_audio_duration(path: str) -> float:
    try:
        a = AudioFileClip(path)
        dur = float(a.duration)
        a.close()
        return dur
<<<<<<< HEAD
    except Exception:
        return 45.0

=======
    except Exception: return 45.0
>>>>>>> origin/main

def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    """Generates a calm instrumental ambient pad using FFmpeg synthesis."""
    subprocess.run([
<<<<<<< HEAD
        "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}",
        "-i", "anoisesrc=c=pink:a=0.03", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}",
=======
        "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
        "-i", "anoisesrc=c=pink:a=0.03", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
>>>>>>> origin/main
        "-i", "sine=frequency=216:sample_rate=44100", "-filter_complex",
        "[0:a]lowpass=f=600[noise];[1:a]volume=0.02[tone];[noise][tone]amix=inputs=2,"
        f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, duration - 1.0):.2f}:d=1.0",
        "-c:a", "aac", "-b:a", "128k", out_path
    ], check=True, capture_output=True)
    return out_path

<<<<<<< HEAD

# ── Captions ──────────────────────────────────────────────────────────────────
=======
# ── Captions (Restored) ───────────────────────────────────────────────────────
>>>>>>> origin/main
def _fmt_time(t: float) -> str:
    h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

<<<<<<< HEAD

=======
>>>>>>> origin/main
def transcribe_to_srt(audio_path: str, srt_path: str, lang: str = "en") -> bool:
    try:
        audio_dur = get_audio_duration(audio_path)
        from faster_whisper import WhisperModel
        model = WhisperModel(os.getenv("WHISPER_MODEL", "tiny.en"), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, language=lang, beam_size=5, vad_filter=True, word_timestamps=True)

        words = []
        for seg in segments:
            for w in seg.words or []:
                if (w.word or "").strip():
                    words.append((w.word.strip(), float(w.start), float(w.end)))

<<<<<<< HEAD
        if not words:
            raise RuntimeError("No timestamps extracted by Whisper.")
=======
        if not words: raise RuntimeError("No timestamps.")
>>>>>>> origin/main

        chunks = []
        i = 0
        while i < len(words):
            take = max(2, min(3, len(words) - i))
<<<<<<< HEAD
            if len(words) - i - take == 1 and take > 2:
                take -= 1
=======
            if len(words) - i - take == 1 and take > 2: take -= 1
>>>>>>> origin/main
            grp = words[i : i + take]
            chunks.append((" ".join(w[0] for w in grp), grp[0][1], grp[-1][2]))
            i += take

        out, idx, cur_t = [], 1, max(0.0, chunks[0][1]) if chunks else 0.0
        for text, w_start, w_end in chunks:
            start = max(cur_t, w_start)
            end = max(start + 0.35, w_end)
            start, end = min(start, max(0.0, audio_dur - 0.01)), min(end, audio_dur)
            if end > start:
                out.append(f"{idx}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{text}\n\n")
                idx += 1
                cur_t = end + 0.02
<<<<<<< HEAD
        if not out:
            raise RuntimeError("No subtitle chunks generated.")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        return True
    except Exception as exc:
        log.warning("[srt] Whisper failed (%s), falling back to blank SRT", exc)
=======
        if not out: raise RuntimeError("No chunks.")
        
        with open(srt_path, "w", encoding="utf-8") as f: f.writelines(out)
        return True
    except Exception as exc:
        log.warning("[srt] failed: %s", exc)
>>>>>>> origin/main
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{_fmt_time(0.0)} --> {_fmt_time(max(0.8, get_audio_duration(audio_path)))}\n \n\n")
        return False

<<<<<<< HEAD

def _ffmpeg_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,").replace("'", "\\'")


def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve())
    style = (
        "Fontname=DejaVu Sans,Fontsize=12,Bold=1,PrimaryColour=&H0000FFFF&,"
        "OutlineColour=&H00000000&,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=96,Spacing=0"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", in_mp4,
            "-vf", f"subtitles={_ffmpeg_escape(srt_abs)}:force_style={_ffmpeg_escape(style)}",
            "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4
        ],
        check=True,
        capture_output=True,
    )


# ── Video Processing ──────────────────────────────────────────────────────────
_cascade = None


def smart_cover_crop(clip, target_w=1080, target_h=1920):
    global _cascade
    if _cascade is None and cv2 is not None:
        try:
            _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:
            pass

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
    except Exception:
        pass

    cx = max(target_w / 2, min(resized.w - target_w / 2, cx))
    cy = max(target_h / 2, min(resized.h - target_h / 2, cy))
    return resized.crop(x_center=cx, y_center=cy, width=target_w, height=target_h).set_audio(None)


def fetch_broll(query: str, need: int = 6) -> list[str]:
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", " ", query or "")
    clean_q = " ".join(clean_q.split()[:4]) or "healthy lifestyle"
    all_queries = [clean_q, "healthy lifestyle", "fitness exercise", "meditation", "wellness nature"]
    collected, seen = [], set()

    if PEXELS_API_KEY:
        headers = {"Authorization": PEXELS_API_KEY}

        def _one(q):
            try:
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers=headers,
                    params={"query": q, "per_page": 15, "min_height": 720, "orientation": "portrait"},
                    timeout=30,
                )
                if r.status_code == 200:
                    links = []
                    for v in r.json().get("videos", []):
                        for f in sorted(v.get("video_files", []), key=lambda x: x.get("height", 0), reverse=True):
                            if 720 <= f.get("height", 0) <= 2160 and f.get("link"):
                                links.append(f["link"])
                                break
                    return links
            except Exception as exc:
                log.warning("[pexels] query '%s' failed: %s", q, exc)
            return []

        with ThreadPoolExecutor(max_workers=4) as ex:
            for fut in as_completed({ex.submit(_one, q): q for q in all_queries}):
                for link in fut.result():
                    if link not in seen:
                        seen.add(link)
                        collected.append(link)

    random.shuffle(collected)
    return collected[:need]


def _generate_fallback_video_clips(tmp: Path, count: int = 3) -> list[str]:
    """Generates clean vertical dark-teal ambient background clips via FFmpeg if downloads fail."""
    generated = []
    colors = ["0x0f2027", "0x134e5e", "0x1a2a6c"]
    for i in range(count):
        out_p = tmp / f"synth_{i}.mp4"
        color = colors[i % len(colors)]
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=1080x1920:r=30:d=8",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                    str(out_p),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            if out_p.exists() and out_p.stat().st_size > 1024:
                generated.append(str(out_p))
        except Exception as exc:
            log.warning("[broll] synthetic fallback clip %d failed: %s", i, exc)
    return generated


def _download_broll(urls: list[str], tmp: Path) -> list[str]:
    local = []
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for i, u in enumerate(urls):
        p = tmp / f"b{i}.mp4"
        try:
            with requests.get(u, headers=req_headers, stream=True, timeout=90) as r:
                r.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
            if os.path.getsize(p) > 10240:
                local.append(str(p))
        except Exception as exc:
            log.warning("[broll] download failed for %s: %s", u, exc)

    if not local:
        log.warning("[broll] No remote B-roll downloaded; generating synthetic vertical clips.")
        local = _generate_fallback_video_clips(tmp, count=3)
    return local


def render_and_cap(broll_urls: list[str], voice_mp3: str, temp_mp4: str, final_mp4: str) -> float:
    tmp = Path(tempfile.mkdtemp())
    local = _download_broll(broll_urls, tmp)
    if not local:
        raise RuntimeError("No B-roll clips could be downloaded or synthesized.")

    raw_clips, cropped, merged, voice_clip = [], [], None, None
    try:
        base_comps = []
        for p in local:
            try:
                c = VideoFileClip(p)
                raw_clips.append(c)
                sub_dur = min(6.0, max(2.5, float(c.duration)))
                comp = smart_cover_crop(c.subclip(0, sub_dur)).set_audio(None)
                base_comps.append(comp)
            except Exception as exc:
                log.warning("[render] clip load error on %s: %s", p, exc)

        if not base_comps:
            raise RuntimeError("No valid video clips could be decoded by MoviePy.")

        cropped = list(base_comps)
        voice_clip = AudioFileClip(voice_mp3)
        target_dur = float(voice_clip.duration)

        while sum(c.duration for c in cropped) < target_dur and len(cropped) < 50:
            for comp in base_comps:
                cropped.append(comp)
                if sum(c.duration for c in cropped) >= target_dur:
                    break

        merged = concatenate_videoclips(cropped, method="compose")
        end = max(0.5, min(PREVIEW_MAX, target_dur))

        out_clip = merged.subclip(0, end).set_audio(voice_clip.subclip(0, end))
        out_clip.write_videofile(
            temp_mp4,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            threads=2,
            preset="fast",
            verbose=False,
            logger=None,
        )
    finally:
        for c in raw_clips + cropped:
            try:
                c.close()
            except Exception:
                pass
        if voice_clip:
            try:
                voice_clip.close()
            except Exception:
                pass
        if merged:
            try:
                merged.close()
            except Exception:
                pass
=======
def _ffmpeg_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,").replace("'", "\\'")

def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve())
    # MarginV=540 positions captions ~28% up from bottom (1920 * 0.28 = 537)
    style = "Fontname=DejaVu Sans,Fontsize=12,Bold=1,PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=540,Spacing=0"
    subprocess.run(["ffmpeg", "-y", "-i", in_mp4, "-vf", f"subtitles={_ffmpeg_escape(srt_abs)}:force_style={_ffmpeg_escape(style)}", "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4], check=True)

# ── Video Processing ──────────────────────────────────────────────────────────
_cascade = None
def smart_cover_crop(clip, target_w=1080, target_h=1920):
    global _cascade
    if _cascade is None and cv2 is not None:
        try: _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception: pass
    
    # Base scale to fill frame
    base_scale = max(target_w / clip.w, target_h / clip.h)
    
    # Ken Burns: Start at base_scale, zoom in slightly over time (+10% zoom)
    def make_frame(t):
        # We don't use a lambda for resize directly because we need smart cropping per frame
        # Instead, we'll apply a simple zoompan-like effect by scaling the crop area
        pass

    # Simplified Ken Burns using MoviePy's resize(lambda)
    # We first resize to fill, then apply the animated zoom, then crop.
    resized = clip.resize(base_scale)
    
    # Apply a subtle slow zoom (1.0 to 1.1)
    zoomed = resized.resize(lambda t: 1 + 0.05 * (t / clip.duration))
    
    cx, cy = zoomed.w / 2, zoomed.h / 2
    try:
        if cv2 is not None and _cascade is not None:
            # Detect face on middle frame of the original resized clip for stable centering
            mid_frame = resized.get_frame(resized.duration / 2)
            gray = cv2.cvtColor(mid_frame, cv2.COLOR_RGB2GRAY)
            faces = _cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
            if len(faces):
                best = max(faces, key=lambda f: f[2] * f[3])
                cx = (best[0] + best[2] / 2) * (zoomed.w / resized.w)
                cy = (best[1] + best[3] / 2) * (zoomed.h / resized.h)
    except Exception: pass
    
    # Ensure crop stays within bounds
    # (Note: crop center might shift as 'zoomed' size changes, but MoviePy handles relative centering)
    return zoomed.crop(x_center=cx, y_center=cy, width=target_w, height=target_h).set_audio(None)

def fetch_broll(topic: str, script_text: str = "", need: int = 18) -> list[str]:
    headers = {"Authorization": PEXELS_API_KEY}
    
    # Generate action-oriented visual keywords using LLM if script is available
    visual_queries = [topic]
    if script_text and GROQ_API_KEY:
        try:
            prompt = (
                f"Analyze this health script and break it into a sequence of {need} specific visual scenes. "
                f"For each scene, provide a highly specific Pexels search query (e.g., 'close up of fresh blueberries', 'person meditating on beach'). "
                f"The sequence MUST follow the script's narrative chronological order perfectly. "
                f"Output ONLY a JSON array of strings."
                f"\n\nScript: {script_text}"
            )
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.5},
                timeout=30
            )
            if r.status_code == 200:
                visual_queries = json.loads(extract_json_block(r.json()["choices"][0]["message"]["content"]))
        except Exception: pass
    
    if not visual_queries or len(visual_queries) < 3:
        visual_queries = [topic, f"{topic} health", "wellness habits", "healthy lifestyle", "fitness exercise", "nutrition health"]

    collected, seen = [], set()
    def _one(q):
        try:
            r = requests.get("https://api.pexels.com/videos/search", headers=headers, params={"query": q, "per_page": 5, "min_height": 720, "orientation": "portrait"}, timeout=30)
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

    # Maintain strict chronological order
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_to_idx = {ex.submit(_one, q): i for i, q in enumerate(visual_queries)}
        ordered_results = [[] for _ in range(len(visual_queries))]
        for fut in as_completed(future_to_idx):
            ordered_results[future_to_idx[fut]] = fut.result()
        
        for links in ordered_results:
            for link in links:
                if link not in seen:
                    seen.add(link)
                    collected.append(link)
                    break # Take the first matching video per query for relevance
    
    # Fallback if too few
    if len(collected) < need:
        fallback = fetch_broll(f"{topic} wellness", script_text="", need=need - len(collected))
        collected.extend(fallback)
        
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

def render_and_cap(broll_urls: list[str], voice_mp3: str, temp_mp4: str, final_mp4: str) -> float:
    tmp = Path(tempfile.mkdtemp())
    local = _download_broll(broll_urls, tmp)
    if not local: raise RuntimeError("No b-roll clips downloaded.")

    raw_clips, cropped, merged, voice_clip = [], [], None, None
    try:
        voice_clip = AudioFileClip(voice_mp3)
        total_duration = voice_clip.duration
        
        # Chronological matching: distribute total duration across all clips
        current_t = 0.0
        clip_idx = 0
        num_local = len(local)
        
        while current_t < total_duration and clip_idx < num_local:
            p = local[clip_idx]
            try:
                c = VideoFileClip(p)
                raw_clips.append(c)
                
                # Calculate target duration for this clip based on remaining time and clips
                rem_dur = total_duration - current_t
                rem_clips = num_local - clip_idx
                target_cut = rem_dur / rem_clips if rem_clips > 0 else rem_dur
                
                # Add slight variation (±10%) but cap at clip's own length
                cut_dur = random.uniform(target_cut * 0.9, target_cut * 1.1)
                cut_dur = min(cut_dur, c.duration - 0.1, rem_dur)
                
                if cut_dur < 0.2: # Extremely short, skip
                    clip_idx += 1
                    continue
                
                # Start at a random point if clip is longer than needed, else start at 0
                start_p = random.uniform(0, max(0, c.duration - cut_dur - 0.1))
                sub = c.subclip(start_p, start_p + cut_dur)
                
                comp = smart_cover_crop(sub).set_audio(None)
                cropped.append(comp)
                
                current_t += cut_dur
                clip_idx += 1
            except Exception as e:
                log.warning(f"Failed to process clip {p}: {e}")
                clip_idx += 1

        if not cropped: raise RuntimeError("No clips loaded.")

        merged = concatenate_videoclips(cropped, method="compose")
        # Final trim to match voice exactly (and stay under 60s)
        end = min(PREVIEW_MAX, voice_clip.duration)
        
        out_clip = merged.subclip(0, end).set_audio(voice_clip.subclip(0, end))
        out_clip.write_videofile(temp_mp4, fps=30, codec="libx264", audio_codec="aac", threads=2, preset="fast", verbose=False, logger=None)
    finally:
        for c in raw_clips + cropped:
            try: c.close()
            except Exception: pass
        if voice_clip: voice_clip.close()
        if merged: merged.close()
>>>>>>> origin/main
        shutil.rmtree(tmp, ignore_errors=True)

    srt_path = "cap.srt"
    transcribe_to_srt(voice_mp3, srt_path, lang=TTS_LANG)

    subbed = "subbed.mp4"
    try:
        burn_captions(temp_mp4, srt_path, subbed)
<<<<<<< HEAD
    except Exception as exc:
        log.warning("[captions] burn_captions failed (%s), using unsubbed video", exc)
=======
    except Exception:
>>>>>>> origin/main
        shutil.copyfile(temp_mp4, subbed)

    bgm_out = "with_bgm.mp4"
    try:
        bgm = ensure_bgm_track(end)
<<<<<<< HEAD
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", subbed, "-i", bgm, "-filter_complex",
                f"[1:a]volume={BGM_VOL}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=async=1[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-shortest", bgm_out,
            ],
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        log.warning("[bgm] mixing failed (%s), using video without BGM", exc)
        shutil.copyfile(subbed, bgm_out)

    os.replace(bgm_out, final_mp4)
    return round(end, 2)

=======
        subprocess.run([
            "ffmpeg", "-y", "-i", subbed, "-i", bgm, "-filter_complex",
            f"[1:a]volume={BGM_VOL}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=async=1[aout]",
            "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-shortest", bgm_out
        ], check=True, capture_output=True)
    except Exception: shutil.copyfile(subbed, bgm_out)

    os.replace(bgm_out, final_mp4)
    return get_audio_duration(final_mp4)
>>>>>>> origin/main

# ── YouTube Upload ────────────────────────────────────────────────────────────
def _yt_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
<<<<<<< HEAD
    creds = Credentials(
        None,
        refresh_token=os.getenv("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID"),
        client_secret=os.getenv("YT_CLIENT_SECRET"),
    )
    return build("youtube", "v3", credentials=creds)


=======
    creds = Credentials(None, refresh_token=os.getenv("YT_REFRESH_TOKEN"), token_uri="https://oauth2.googleapis.com/token", client_id=os.getenv("YT_CLIENT_ID"), client_secret=os.getenv("YT_CLIENT_SECRET"))
    return build("youtube", "v3", credentials=creds)

>>>>>>> origin/main
def upload_unlisted(video_path: str, title: str, desc: str, tags: str) -> tuple[str, str]:
    from googleapiclient.http import MediaFileUpload
    yt = _yt_client()
    body = {
<<<<<<< HEAD
        "snippet": {
            "title": title[:100],
            "description": desc[:4900],
            "tags": [t.strip() for t in tags.split(",") if t.strip()][:12],
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
=======
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": [t.strip() for t in tags.split(",")][:12], "categoryId": "27"},
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
>>>>>>> origin/main
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
<<<<<<< HEAD
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"], f"https://youtu.be/{resp['id']}"


def build_preview(topic: str, user_script: str | None, slot: str, issue_body: str) -> tuple[dict | None, str]:
    best = {"path": None, "dur": None, "score": float("inf"), "attempt": 0, "seo": None}
    last_error = None

    # Only run 1 attempt if user provided an exact custom script
    max_attempts = 1 if user_script else 2

    for attempt in range(1, max_attempts + 1):
        try:
            log.info("[build] Starting attempt %d/%d for topic: %s", attempt, max_attempts, topic)
=======
    while resp is None: _, resp = req.next_chunk()
    return resp["id"], f"https://youtu.be/{resp['id']}"

def build_preview(topic: str, user_script: str | None, slot: str, issue_body: str) -> tuple[dict | None, str]:
    best = {"path": None, "dur": None, "score": float("inf"), "attempt": 0, "seo": None}
    for attempt in range(1, 4):
        try:
>>>>>>> origin/main
            if user_script:
                script_text = _clean_script(user_script)
            else:
                prompt_tpl = load_default_prompt()
                actual_prompt = prompt_tpl.format(topic=topic)
                script_text = generate_script(topic, actual_prompt)
<<<<<<< HEAD

            seo = generate_seo_package(topic, script_text)
            voice_raw = f"voice_{attempt}.mp3"
            synthesize_speech(script_text, voice_raw, lang=TTS_LANG)

            broll = fetch_broll(topic, need=6)
            temp_mp4, final_mp4 = f"temp_{attempt}.mp4", f"short_{attempt}.mp4"
            dur = render_and_cap(broll, voice_raw, temp_mp4, final_mp4)

=======
            
            seo = generate_seo_package(topic, script_text)
            voice_raw = f"voice_{attempt}.mp3"
            synthesize_speech(script_text, voice_raw, lang=TTS_LANG)
            
            broll = fetch_broll(topic, script_text=script_text, need=15)
            if not broll: broll = fetch_broll("healthy lifestyle", script_text="", need=15)
            if not broll: continue
            
            temp_mp4, final_mp4 = f"temp_{attempt}.mp4", f"short_{attempt}.mp4"
            dur = render_and_cap(broll, voice_raw, temp_mp4, final_mp4)
            
>>>>>>> origin/main
            score = 0.0 if TARGET_LOW <= dur < TARGET_HIGH else abs(dur - 45.0)
            if score < best["score"]:
                best_path = f"best_{attempt}.mp4"
                shutil.copyfile(final_mp4, best_path)
                best.update({"path": best_path, "dur": round(dur, 2), "score": score, "attempt": attempt, "seo": seo})
<<<<<<< HEAD

            if TARGET_LOW <= dur < TARGET_HIGH or user_script:
                return _package_and_upload(final_mp4, seo, topic, slot, issue_body, attempt, dur)
        except Exception as exc:
            last_error = exc
            log.error("[build] attempt %d failed: %s", attempt, exc, exc_info=True)

    if best["path"] and os.path.exists(best["path"]):
        return _package_and_upload(best["path"], best["seo"], topic, slot, issue_body, best["attempt"], best["dur"])

    if last_error:
        raise RuntimeError(f"Video generation failed: {last_error}")
    return None, issue_body


def _package_and_upload(video_path, seo, topic, slot, issue_body, attempt, dur):
    try:
        log.info("[upload] Uploading %s to YouTube as unlisted...", video_path)
        vid_id, link = upload_unlisted(video_path, seo["title"], seo["description"], seo["tags_csv"])
        log.info("[upload] Uploaded successfully: %s", link)
        meta = get_metadata_from_issue_body(issue_body) or {}
        meta.update({
            "topic": topic,
            "title": seo["title"],
            "description": seo["description"],
            "preview_video_id": vid_id,
            "preview_link": link,
            "slot": slot,
            "duration_sec": dur,
            "attempt": attempt,
        })
        return meta, set_metadata_in_issue_body(issue_body, meta)
    except Exception as exc:
        raise RuntimeError(f"YouTube upload failed: {exc}")


# ── Command Parsing & Handlers ────────────────────────────────────────────────
def _post_script_request(owner: str, repo: str, number: int, topic: str) -> None:
    body = (
        f"✅ **Topic approved:** {topic}\n\n"
        "Please provide your script text or generate one automatically:\n\n"
        "- **Option A — Use AI Default Prompt:**\n"
        "  Reply with: `/use-default-prompt`\n\n"
        "- **Option B — Provide Your Own Script:**\n"
        "  Reply with: `/set-script` followed by your full voiceover text."
    )
    post_comment(owner, repo, number, body)


def _clean_issue_title_to_topic(title: str) -> str:
    s = re.sub(r"(?i)#?shorts?|\[[^\]]+\]|\([^)]+\)", "", (title or "").strip())
    s = re.split(r"\s+[|\-–—]\s+", s)[0].strip()
    return (s[0].upper() + s[1:])[:120].rstrip() if s else "Health and wellness tips"


def _extract_topics_from_visible_body(issue_body: str) -> list[str]:
    """Fallback parser if the HTML metadata comment is missing or corrupted."""
    if not issue_body:
        return []
    matches = re.findall(r"^\s*\d+\)\s+(.+)$", issue_body, flags=re.MULTILINE)
    return [m.strip() for m in matches if m.strip()]


def _extract_user_script_if_present(comment_body: str) -> str | None:
    idx = comment_body.lower().find("/set-script")
    if idx != -1:
        script = comment_body[idx + len("/set-script"):].strip()
        # Strip trailing commands if someone wrote /set-script ... /approve-topic 1
        for cmd in ["/approve-topic", "/custom-topic", "/use-default-prompt"]:
            c_idx = script.lower().find(cmd)
            if c_idx != -1:
                script = script[:c_idx].strip()
        return script if script else None
    return None


def _parse_topic_from_approve(comment_body: str, topics: list[str], issue_title: str = "") -> str:
    lower = comment_body.lower()
    idx_kw = lower.find("/approve-topic")
    body_part = comment_body[idx_kw + len("/approve-topic"):].strip() if idx_kw != -1 else comment_body.strip()

    # Strip any chained command keywords from the topic argument
    for kw in ["/use-default-prompt", "/set-script", "/custom-topic"]:
        idx = body_part.lower().find(kw)
        if idx != -1:
            body_part = body_part[:idx].strip()

    # Take only the first line in case extra comments follow on newlines
    first_line = body_part.splitlines()[0].strip() if body_part else ""

    if not first_line:
        return topics[0] if topics else _clean_issue_title_to_topic(issue_title)

    # Check if the user passed a number like "2" or "2)"
    m_num = re.match(r"^(\d+)\)?$", first_line)
    if m_num:
        idx = int(m_num.group(1))
        if topics and 1 <= idx <= len(topics):
            return topics[idx - 1]
        elif topics:
            return topics[0]
        return _clean_issue_title_to_topic(issue_title)

    return first_line


def _parse_topic_from_custom(comment_body: str, issue_title: str = "") -> str:
    lower = comment_body.lower()
    idx_c = lower.find("/custom-topic")
    body_part = comment_body[idx_c + len("/custom-topic"):].strip() if idx_c != -1 else comment_body.strip()

    for kw in ["/use-default-prompt", "/set-script", "/approve-topic"]:
        pos = body_part.lower().find(kw)
        if pos != -1:
            body_part = body_part[:pos].strip()

    first_line = body_part.splitlines()[0].strip() if body_part else ""
    return first_line if first_line else _clean_issue_title_to_topic(issue_title)


def _save_topic_to_issue(ctx: Ctx, topic: str) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    if not meta.get("topics"):
        meta["topics"] = _extract_topics_from_visible_body(ctx.issue_body)
    meta["topic"] = topic
    meta.pop("user_script", None)
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    ctx.issue_body = new_body


def _run_build_with_script(ctx: Ctx, user_script: str | None) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topics = meta.get("topics") or _extract_topics_from_visible_body(ctx.issue_body)
    topic = meta.get("topic", "")
    if not topic:
        topic = topics[0] if topics else _clean_issue_title_to_topic(ctx.issue.get("title", ""))
        meta["topic"] = topic

    if user_script:
        meta["user_script"] = user_script
    else:
        meta.pop("user_script", None)

    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    ctx.issue_body = new_body

    mode_str = "custom script" if user_script else "AI default prompt"
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"🎬 Building video preview for **{topic}** (using {mode_str}).\nThis takes ~2–5 minutes...",
    )

    try:
        video_meta, new_body2 = build_preview(topic, user_script, ctx.slot, new_body)
    except Exception as exc:
        log.error("[build] error: %s", exc, exc_info=True)
        post_comment(ctx.owner, ctx.repo, ctx.number, f"❌ **Error building preview:**\n```\n{exc}\n```\nTry `/regenerate-video` or check repository secrets.")
        return

    if not video_meta:
        post_comment(ctx.owner, ctx.repo, ctx.number, "❌ Could not produce a valid preview. Try `/regenerate-video`.")
        return

    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body2})
    ctx.issue_body = new_body2
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"✅ **Preview ready!** (Attempt {video_meta['attempt']}, {video_meta['duration_sec']}s)\n"
        f"🔗 **Watch Unlisted Preview:** {video_meta['preview_link']}\n\n"
        f"**Title:** `{video_meta['title']}`\n\n"
        "Reply with:\n"
        "- `/approve-video` — schedule for publication\n"
        "- `/regenerate-video` — rebuild with fresh B-roll\n"
        "- `/reject-video` — delete preview and pick a new topic",
    )


=======
            
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

# ── Command Handlers ──────────────────────────────────────────────────────────
def _post_script_request(owner: str, repo: str, number: int, topic: str) -> None:
    body = (
        f"Topic approved: **{topic}**\n\n"
        "### ⚡ TIP: You can do this in ONE comment next time!\n"
        "Instead of separate steps, you can approve and set your script simultaneously by replying with:\n"
        "- `/approve-topic N /use-default-prompt`\n"
        "- `/approve-topic N /set-script [your full text]`\n\n"
        "---\n\n"
        "**For this issue, please reply with your script choice:**\n\n"
        "**Option A - Custom Script:**\n"
        "Reply with: `/set-script` followed by your full voiceover text.\n\n"
        "**Option B - AI Default Script:**\n"
        "Reply with exactly: `/use-default-prompt`\n\n"
        f"A reminder will be posted if no script is received within {PROMPT_REMINDER_HOURS} hour."
    )
    post_comment(owner, repo, number, body)

>>>>>>> origin/main
def handle_reject_topic(ctx: Ctx) -> None:
    try:
        from propose import gather_topics
        options = gather_topics(REGION, need=6, exclude=set())
    except Exception:
<<<<<<< HEAD
        options = []

    if not options:
        options = [
            "Gut health basics",
            "Fixing posture in 2 minutes",
            "Better sleep habits",
            "Hydration tips for energy",
            "Mental clarity morning routine",
            "Natural metabolism boosters",
        ]

    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topics"] = options
    meta.pop("topic", None)
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(options, 1))

    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"🔄 **Fresh topic options:**\n{numbered}\n\n"
        "Reply `/approve-topic N` (or `/approve-topic N /use-default-prompt`) or `/custom-topic Your Topic`.",
    )


def handle_approve_topic(ctx: Ctx, trigger_build: bool = False, user_script: str | None = None) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topics = meta.get("topics") or _extract_topics_from_visible_body(ctx.issue_body)
    issue_title = ctx.issue.get("title", "")
    topic = _parse_topic_from_approve(ctx.comment_body, topics, issue_title)
    _save_topic_to_issue(ctx, topic)

    if trigger_build:
        _run_build_with_script(ctx, user_script)
    else:
        _post_script_request(ctx.owner, ctx.repo, ctx.number, topic)


def handle_custom_topic(ctx: Ctx, trigger_build: bool = False, user_script: str | None = None) -> None:
    issue_title = ctx.issue.get("title", "")
    topic = _parse_topic_from_custom(ctx.comment_body, issue_title)
    _save_topic_to_issue(ctx, topic)

    if trigger_build:
        _run_build_with_script(ctx, user_script)
    else:
        _post_script_request(ctx.owner, ctx.repo, ctx.number, topic)


def handle_use_default_prompt(ctx: Ctx) -> None:
    _run_build_with_script(ctx, None)


def handle_set_script(ctx: Ctx) -> None:
    user_script = _extract_user_script_if_present(ctx.comment_body)
    if not user_script:
        post_comment(
            ctx.owner,
            ctx.repo,
            ctx.number,
            "⚠️ Script text was empty. Reply with `/set-script Your full voiceover text` or `/use-default-prompt`.",
        )
        return
    _run_build_with_script(ctx, user_script)

=======
        options = ["Gut health basics", "Fixing posture in 2 minutes", "Better sleep habits", "Hydration tips", "Mental clarity routine", "Metabolism boosters"]
    
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topics"] = options
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(options, 1))
    
    instructions = (
        f"\n\n### Fresh Topic Options:\n{numbered}\n\n"
        "⚡ **FASTEST (One Comment):**\n"
        "- `/approve-topic 1 /use-default-prompt` (Approve & build immediately with AI script)\n"
        "- `/approve-topic 1 /set-script [your text]` (Approve & build with custom script)\n\n"
        "**Standard:**\n"
        "- `/approve-topic N` (Approve and pick script next)\n"
        "- `/custom-topic Your Topic`"
    )
    
    new_body += instructions
    
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    post_comment(ctx.owner, ctx.repo, ctx.number, instructions)

def _start_after_topic(ctx: Ctx, topic: str) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    meta["topic"] = topic
    meta.pop("user_script", None)
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    _post_script_request(ctx.owner, ctx.repo, ctx.number, topic)

def handle_custom_topic(ctx: Ctx) -> None:
    topic = ""
    for line in ctx.comment_body.splitlines():
        if line.strip().lower().startswith("/custom-topic"):
            topic = line.strip()[len("/custom-topic"):].strip()
            break
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Please provide a topic. Example: /custom-topic The 4-7-8 breathing trick")
        return
    _start_after_topic(ctx, topic)

def handle_approve_topic(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topics = meta.get("topics", [])
    
    # Pre-check for combined flags in the ENTIRE comment body
    comment_lower = ctx.comment_body.lower()
    use_default = "/use-default-prompt" in comment_lower
    user_script = None
    if "/set-script" in comment_lower:
        parts = re.split(r"(?i)/set-script", ctx.comment_body, 1)
        user_script = parts[1].strip()

    # Extract topic index or title from the /approve-topic line specifically
    # or from the first line if it's the only line.
    approve_line = ""
    for line in ctx.comment_body.splitlines():
        if line.strip().lower().startswith("/approve-topic"):
            approve_line = line.strip()
            break
    
    if not approve_line: approve_line = ctx.comment_body.splitlines()[0].strip()

    parts = approve_line.split(maxsplit=1)
    topic = ""
    if len(parts) > 1:
        # Check if it's a number followed by other stuff
        subparts = parts[1].split()
        if subparts and subparts[0].isdigit():
            idx = int(subparts[0])
            if 1 <= idx <= len(topics):
                topic = topics[idx - 1]
            else:
                post_comment(ctx.owner, ctx.repo, ctx.number, f"Invalid index {idx}. Available: 1-{len(topics)}")
                return
        else:
            # It's a custom title on the same line, e.g. "/approve-topic My Title /use-default-prompt"
            # We need to strip the flags from the topic title
            topic = parts[1]
            topic = re.sub(r"(?i)/use-default-prompt", "", topic)
            topic = re.sub(r"(?i)/set-script.*", "", topic)
            topic = topic.strip()
    else:
        # Just "/approve-topic" -> default to first
        if topics: topic = topics[0]
        else:
            post_comment(ctx.owner, ctx.repo, ctx.number, "No topics found in metadata.")
            return

    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Could not determine topic.")
        return

    # Success: approve and move forward
    remove_label(ctx.owner, ctx.repo, ctx.number, "await-approval")
    add_label(ctx.owner, ctx.repo, ctx.number, "approved")

    if use_default:
        _run_build_with_script(ctx, None, topic_override=topic)
    elif user_script is not None:
        _run_build_with_script(ctx, user_script, topic_override=topic)
    else:
        _start_after_topic(ctx, topic)

def handle_use_default_prompt(ctx: Ctx) -> None:
    # If this is called alone, it uses the approved topic from metadata
    _run_build_with_script(ctx, None)

def handle_set_script(ctx: Ctx) -> None:
    # If this is called alone, it uses the approved topic from metadata
    user_script = ""
    for line in ctx.comment_body.splitlines():
        if line.strip().lower().startswith("/set-script"):
            # For standalone /set-script, we might want to take EVERYTHING after it
            # including potentially multi-line scripts.
            parts = re.split(r"(?i)/set-script", ctx.comment_body, 1)
            user_script = parts[1].strip()
            break
    
    if not user_script:
        post_comment(ctx.owner, ctx.repo, ctx.number, "Script was empty. Reply `/set-script your full text` or `/use-default-prompt`.")
        return
    _run_build_with_script(ctx, user_script)

def _run_build_with_script(ctx: Ctx, user_script: str | None, topic_override: str | None = None) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = topic_override or meta.get("topic", "")
    if not topic:
        # Fallback: maybe it was JUST approved in this comment but we are in a separate handler
        # But handle_approve_topic now handles combined commands, so this is for standalone.
        post_comment(ctx.owner, ctx.repo, ctx.number, "No approved topic found. Please approve a topic first.")
        return

    if user_script: meta["user_script"] = user_script
    else: meta.pop("user_script", None)
    
    if topic_override: meta["topic"] = topic
    
    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    
    status_msg = f"Building video for: **{topic}**\n(Script: {'Custom' if user_script else 'AI Generated'})\nThis takes 5-10 mins. Please wait..."
    post_comment(ctx.owner, ctx.repo, ctx.number, status_msg)

    # Add a 'building' label
    add_label(ctx.owner, ctx.repo, ctx.number, "building")

    try:
        video_meta, new_body2 = build_preview(topic, user_script, ctx.slot, new_body)
        if not video_meta:
            remove_label(ctx.owner, ctx.repo, ctx.number, "building")
            post_comment(ctx.owner, ctx.repo, ctx.number, "❌ Could not produce a valid preview. Try /regenerate-video or check logs.")
            return
        
        gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body2})
        
        preview_msg = (
            f"✅ **Preview ready!** (Attempt {video_meta['attempt']}, {video_meta['duration_sec']}s)\n"
            f"🔗 [Watch Preview]({video_meta['preview_link']})\n\n"
            "**Next Steps:**\n"
            "- `/approve-video` (Schedule for tomorrow)\n"
            "- `/regenerate-video` (New B-roll/cuts)\n"
            "- `/reject-video` (Start over)"
        )
        post_comment(ctx.owner, ctx.repo, ctx.number, preview_msg)
    except Exception as exc:
        log.error("Build failed: %s", exc, exc_info=True)
        post_comment(ctx.owner, ctx.repo, ctx.number, f"❌ Error during video creation: {exc}")
    finally:
        remove_label(ctx.owner, ctx.repo, ctx.number, "building")

>>>>>>> origin/main

def handle_reject_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if vid_id:
<<<<<<< HEAD
        try:
            _yt_client().videos().delete(id=vid_id).execute()
        except Exception:
            pass

    for k in ["preview_video_id", "preview_link", "title", "description", "attempt", "duration_sec"]:
        meta.pop(k, None)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": set_metadata_in_issue_body(ctx.issue_body, meta)})
    post_comment(ctx.owner, ctx.repo, ctx.number, "🗑️ Deleted preview video. Reply `/new-topic` for fresh options or `/custom-topic Your Topic`.")


def handle_regenerate_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if vid_id:
        try:
            _yt_client().videos().delete(id=vid_id).execute()
        except Exception:
            pass

    user_script = meta.get("user_script")
    _run_build_with_script(ctx, user_script)
=======
        try: _yt_client().videos().delete(id=vid_id).execute()
        except Exception: pass
    
    for k in ["preview_video_id", "preview_link", "title", "description", "attempt", "duration_sec"]: meta.pop(k, None)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": set_metadata_in_issue_body(ctx.issue_body, meta)})
    post_comment(ctx.owner, ctx.repo, ctx.number, "Deleted preview. Reply /new-topic for fresh options or /custom-topic Your Topic.")

def handle_regenerate_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    topic = meta.get("topic")
    if not topic:
        post_comment(ctx.owner, ctx.repo, ctx.number, "No topic found in metadata to regenerate.")
        return

    vid_id = meta.get("preview_video_id")
    if vid_id:
        try: _yt_client().videos().delete(id=vid_id).execute()
        except Exception: pass
    
    user_script = meta.get("user_script")
    post_comment(ctx.owner, ctx.repo, ctx.number, "🔄 Regenerating video with new B-roll... Please wait.")
    add_label(ctx.owner, ctx.repo, ctx.number, "building")

    try:
        video_meta, new_body = build_preview(topic, user_script, ctx.slot, ctx.issue_body)
        if video_meta:
            gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
            preview_msg = (
                f"✅ **Regeneration complete!** (Attempt {video_meta['attempt']}, {video_meta['duration_sec']}s)\n"
                f"🔗 [Watch Preview]({video_meta['preview_link']})\n\n"
                "**Next Steps:**\n"
                "- `/approve-video` (Schedule for tomorrow)\n"
                "- `/regenerate-video` (New B-roll again)\n"
                "- `/reject-video` (Start over)"
            )
            post_comment(ctx.owner, ctx.repo, ctx.number, preview_msg)
        else:
            post_comment(ctx.owner, ctx.repo, ctx.number, "❌ Could not produce a valid preview during regeneration.")
    except Exception as exc:
        log.error("Regenerate failed: %s", exc, exc_info=True)
        post_comment(ctx.owner, ctx.repo, ctx.number, f"❌ Error during regeneration: {exc}")
    finally:
        remove_label(ctx.owner, ctx.repo, ctx.number, "building")
>>>>>>> origin/main


def handle_approve_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
<<<<<<< HEAD
    if not vid_id:
        user_script = meta.get("user_script")
        _run_build_with_script(ctx, user_script)
        meta = get_metadata_from_issue_body(ctx.issue_body) or {}
        vid_id = meta.get("preview_video_id")
        if not vid_id:
            return

=======
    if not vid_id: return
    
>>>>>>> origin/main
    yt = _yt_client()
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    hour = 16 if meta.get("slot") == "afternoon" else 9
    ist_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, 0, tzinfo=IST)
    publish_utc = ist_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
<<<<<<< HEAD

    yt.videos().update(
        part="status",
        body={"id": vid_id, "status": {"privacyStatus": "private", "publishAt": publish_utc}},
    ).execute()
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"🚀 **Scheduled:** https://youtu.be/{vid_id}\n"
        f"📅 **Publishes at (IST):** `{ist_dt.strftime('%Y-%m-%d %H:%M')}`\n\n"
        "Closing this issue.",
    )
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"state": "closed"})


def main() -> None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        log.error("GITHUB_EVENT_PATH not found.")
        return

    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)

    if not is_authorized_commenter(event):
        return

    owner, repo = REPO.split("/", 1)
    ctx = Ctx(event, owner, repo)
    comment_body = ctx.comment_body
    comment_lower = comment_body.lower()

    log.info("Processing comment on issue #%d: %s", ctx.number, comment_body[:120])

    # 1. Direct video lifecycle commands
    if "/approve-video" in comment_lower:
        handle_approve_video(ctx)
        return
    if "/reject-video" in comment_lower:
        handle_reject_video(ctx)
        return
    if "/regenerate-video" in comment_lower:
        handle_regenerate_video(ctx)
        return
    if "/reject-topic" in comment_lower or "/new-topic" in comment_lower:
        handle_reject_topic(ctx)
        return

    # 2. Topic & Script commands (supports both single and combined in one comment)
    has_approve = "/approve-topic" in comment_lower
    has_custom = "/custom-topic" in comment_lower
    has_default = "/use-default-prompt" in comment_lower
    has_set_script = "/set-script" in comment_lower

    user_script = _extract_user_script_if_present(comment_body) if has_set_script else None
    trigger_build = has_default or has_set_script

    if has_approve:
        handle_approve_topic(ctx, trigger_build=trigger_build, user_script=user_script)
        return

    if has_custom:
        handle_custom_topic(ctx, trigger_build=trigger_build, user_script=user_script)
        return

    if has_set_script:
        handle_set_script(ctx)
        return

    if has_default:
        handle_use_default_prompt(ctx)
        return

    log.info("No matching /command found in comment.")

=======
    
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
    
    # Normalize for matching but keep original in ctx
    lines = [l.strip().lower() for l in ctx.comment_body.splitlines()]
    
    # Prioritize /approve-topic if combined
    for prefix, handler in _COMMANDS:
        if any(l.startswith(prefix) for l in lines):
            log.info("Matched command: %s", prefix)
            handler(ctx)
            return
>>>>>>> origin/main

if __name__ == "__main__":
    main()

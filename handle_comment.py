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
        "openai/gpt-oss-20b,openai/gpt-oss-120b,"
        "groq/compound-mini,moonshotai/kimi-k2-instruct",
    ).split(",") if m.strip()
]

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
TTS_LANG       = os.getenv("TTS_LANG", "en")
BGM_VOL        = 0.07

IST         = timezone(timedelta(hours=5, minutes=30))
TARGET_LOW  = 35.0
TARGET_HIGH = 58.0
PREVIEW_MAX = 57.3
PROMPT_REMINDER_HOURS = 1

_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

def load_default_prompt() -> str:
    try:
        content = _PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content and "{topic}" in content:
            return content
    except Exception:
        pass
    return "Write a spoken voiceover script for a 50-second YouTube Short about: {topic}"

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

# ── LLM & Script Cleaning ──────────────────────────────────────────────────────
def _list_groq_models() -> list[str]:
    if not GROQ_API_KEY: return []
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=30)
        if r.status_code == 200:
            return [d["id"] for d in r.json().get("data", []) if d.get("id")]
    except Exception: pass
    return []

def _build_model_list() -> list[str]:
    env_models = [GROQ_MODEL] + GROQ_FALLBACKS
    seen = set()
    env_models = [m for m in env_models if not (m in seen or seen.add(m))]
    available = _list_groq_models()
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
            return cleaned
        except Exception as exc:
            last_err = exc
    raise last_err or RuntimeError("All Groq models failed to generate a script.")

# ── TTS & Audio Tools ─────────────────────────────────────────────────────────
def _try_gtts(text: str, out_path: str, lang: str) -> bool:
    try:
        from gtts import gTTS
        gTTS(text, lang=lang, slow=False).save(out_path)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except Exception: return False

def _try_espeak(text: str, out_path: str, lang: str) -> bool:
    try:
        wav = out_path.replace(".mp3", "_espeak.wav")
        subprocess.run(["espeak-ng", "-v", lang, "-s", "150", "-w", wav, text], capture_output=True, timeout=60)
        if not os.path.exists(wav) or os.path.getsize(wav) < 512: return False
        subprocess.run(["ffmpeg", "-y", "-i", wav, "-ar", "22050", "-ac", "1", out_path], capture_output=True)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 512
    except Exception: return False

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
    except Exception: return 45.0

def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    """Generates a calm instrumental ambient pad using FFmpeg synthesis."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
        "-i", "anoisesrc=c=pink:a=0.03", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
        "-i", "sine=frequency=216:sample_rate=44100", "-filter_complex",
        "[0:a]lowpass=f=600[noise];[1:a]volume=0.02[tone];[noise][tone]amix=inputs=2,"
        f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, duration - 1.0):.2f}:d=1.0",
        "-c:a", "aac", "-b:a", "128k", out_path
    ], check=True, capture_output=True)
    return out_path

# ── Captions (Restored) ───────────────────────────────────────────────────────
def _fmt_time(t: float) -> str:
    h, m, s = int(t // 3600), int((t % 3600) // 60), int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

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

        if not words: raise RuntimeError("No timestamps.")

        chunks = []
        i = 0
        while i < len(words):
            take = max(2, min(3, len(words) - i))
            if len(words) - i - take == 1 and take > 2: take -= 1
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
        if not out: raise RuntimeError("No chunks.")
        
        with open(srt_path, "w", encoding="utf-8") as f: f.writelines(out)
        return True
    except Exception as exc:
        log.warning("[srt] failed: %s", exc)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{_fmt_time(0.0)} --> {_fmt_time(max(0.8, get_audio_duration(audio_path)))}\n \n\n")
        return False

def _ffmpeg_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,").replace("'", "\\'")

def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve())
    style = "Fontname=DejaVu Sans,Fontsize=12,Bold=1,PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=96,Spacing=0"
    subprocess.run(["ffmpeg", "-y", "-i", in_mp4, "-vf", f"subtitles={_ffmpeg_escape(srt_abs)}:force_style={_ffmpeg_escape(style)}", "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4], check=True)

# ── Video Processing ──────────────────────────────────────────────────────────
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
            r = requests.get("https://api.pexels.com/videos/search", headers=headers, params={"query": q, "per_page": 15, "min_height": 720, "orientation": "portrait"}, timeout=30)
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

    srt_path = "cap.srt"
    transcribe_to_srt(voice_mp3, srt_path, lang=TTS_LANG)

    subbed = "subbed.mp4"
    try:
        burn_captions(temp_mp4, srt_path, subbed)
    except Exception:
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

# ── YouTube Upload ────────────────────────────────────────────────────────────
def _yt_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=os.getenv("YT_REFRESH_TOKEN"), token_uri="https://oauth2.googleapis.com/token", client_id=os.getenv("YT_CLIENT_ID"), client_secret=os.getenv("YT_CLIENT_SECRET"))
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
                script_text = _clean_script(user_script)
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

# ── Command Handlers ──────────────────────────────────────────────────────────
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

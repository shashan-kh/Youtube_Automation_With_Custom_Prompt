"""
handle_comment.py — Processes all /commands posted on GitHub Issues, sanitizes spoken scripts
to be clean, curious, and informative, maximizes duration just below 59 seconds (PREVIEW_MAX = 58.5s),
appends a proper 'Thank you' conclusion strictly once, renders the vertical video, and uploads to YouTube.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips

from brand_config import (
    NEGATIVE_PROMPT_RULES,
    UNIFIED_SINGLE_OUTRO,
    VISUAL_IDENTITY,
    enforce_policy_guardrails,
    select_credible_sources,
)
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
from thumbnail_generator import generate_shorts_thumbnail

log = get_logger("handle_comment")

try:
    import cv2
except Exception:
    cv2 = None

# ── Env ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACKS = [
    m.strip()
    for m in os.getenv(
        "GROQ_FALLBACK_MODELS",
        "llama-3.3-70b-versatile,llama-3.1-8b-instant,"
        "qwen/qwen3-32b,openai/gpt-oss-20b,openai/gpt-oss-120b,"
        "groq/compound-mini,moonshotai/kimi-k2-instruct",
    ).split(",")
    if m.strip()
]

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
REGION = os.getenv("REGION", "IN")
TTS_LANG = os.getenv("TTS_LANG", "en")
BGM_VOL = 0.07

IST = timezone(timedelta(hours=5, minutes=30))
TARGET_LOW = 45.0
TARGET_HIGH = 58.5
PREVIEW_MAX = 58.5  # Maximized to just below 59 seconds

_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

_OUTRO_STRIP_PATTERNS = [
    r"(?i)\bthank\s+you\s+(so\s+much\s+)?for\s+(watching|listening|tuning\s+in)[^.!?]*[.!?]?",
    r"(?i)\bthanks\s+for\s+(watching|listening)[^.!?]*[.!?]?",
    r"(?i)\bfollow\s+for\s+more\s+(daily\s+)?(health|wellness)\s+tips[^.!?]*[.!?]?",
    r"(?i)\bdon'?t\s+forget\s+to\s+(like\s+and\s+)?subscribe[^.!?]*[.!?]?",
    r"(?i)\blike\s+and\s+subscribe\s+for\s+more[^.!?]*[.!?]?",
    r"(?i)\bsubscribe\s+for\s+more\s+(daily\s+)?(wellness|health|science)[^.!?]*[.!?]?",
]


def load_default_prompt() -> str:
    try:
        content = _PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content and "{topic}" in content:
            return content
    except Exception:
        pass
    return "Write a spoken voiceover script for a 55-second health and wellness YouTube Short about: {topic}"


class Ctx:
    def __init__(self, event: dict, owner: str, repo: str) -> None:
        self.event = event
        self.owner = owner
        self.repo = repo
        self.issue = event["issue"]
        self.number: int = self.issue["number"]
        self.comment_body = (event["comment"]["body"] or "").strip()
        self.issue_body = self.issue.get("body") or ""
        self.labels = self.issue.get("labels", [])
        self.slot = get_slot_from_labels(self.labels)


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
    env_models = [GROQ_MODEL] + GROQ_FALLBACKS
    seen = set()
    env_models = [m for m in env_models if not (m in seen or seen.add(m))]
    available = _list_groq_models()
    if not available:
        return env_models[:6]
    ordered = [m for m in env_models if m in available]
    for m in available:
        if m not in ordered:
            ordered.append(m)
    return ordered[:12]


def sanitize_spoken_script(raw_text: str, topic: str = "", enforce_policy_blocks: bool = False) -> str:
    """
    Sanitizes spoken narration:
    1. Strips markdown, stage directions, and timestamps.
    2. Strips all duplicate sign-offs.
    3. Appends UNIFIED_SINGLE_OUTRO strictly ONCE at the end containing the proper 'Thank you' conclusion.
    """
    text = raw_text or ""

    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```[a-zA-Z]*", " ", text)
    text = text.replace("```", " ")

    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(
        r"\((?:visual|scene|cut|music|sfx|audio|pause|b-roll|on-screen|outro|intro)[^)]*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d{1,2}:\d{2}(?:\s*[-–—to]+\s*\d{1,2}:\d{2})?\b", " ", text)

    text = re.sub(
        r"(?i)(?:^|\n)\s*(?:Title|Topic|Script|Intro|Hook|Body|Point\s*\d+|Recap|CTA|Outro|Disclaimer|Voiceover|Narrator|Host|Speaker|Visual|Audio|On-screen\s+text)\s*:\s*",
        " ",
        text,
    )
    text = re.sub(r"(?i)^(Here is the script|Sure!|Certainly!|Here's your script)[^\n]*\n?", " ", text).strip()

    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?:^|\s)#\w+", " ", text)
    text = re.sub(r"[*_~`#>|]+", " ", text)
    text = re.sub(r"[^\x00-\x7F]+", " ", text)

    for pat in _OUTRO_STRIP_PATTERNS:
        text = re.sub(pat, " ", text)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    deduped_sentences: list[str] = []
    seen_norm: set[str] = set()
    for s in sentences:
        norm = normalize_key(s)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped_sentences.append(s if s[-1] in ".!?" else s + ".")

    body_text = " ".join(deduped_sentences).strip()
    body_text = enforce_policy_guardrails(body_text)

    # Append UNIFIED_SINGLE_OUTRO strictly ONCE (contains proper Thank You conclusion)
    final_spoken = body_text.rstrip(".!? ") + ". " + UNIFIED_SINGLE_OUTRO
    final_spoken = re.sub(r"\s+", " ", final_spoken).strip()
    return final_spoken


def build_viral_shorts_segments(spoken_script: str, topic: str) -> list[dict]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", spoken_script) if s.strip()]
    hook_sent = sentences[0] if sentences else spoken_script
    outro_sents = " ".join(sentences[-2:]) if len(sentences) >= 3 else UNIFIED_SINGLE_OUTRO
    body_sents = " ".join(sentences[1:-2]) if len(sentences) > 3 else " ".join(sentences[1:-1])

    short_topic_words = " ".join(re.findall(r"[a-zA-Z0-9]+", topic or "Wellness Habit")[:3]).upper()

    return [
        {
            "segment": "1. Curiosity Hook (0:00–0:03)",
            "visual_direction": (
                f"High-contrast 9:16 wellness B-roll highlighting {topic} "
                f"in clinical navy ({VISUAL_IDENTITY['color_palette']['deep_clinical_navy']}) with dynamic zoom."
            ),
            "on_screen_text": f"WHY {short_topic_words} MATTERS",
            "spoken_narration": hook_sent,
            "audio_cue": "Subtle low-frequency whoosh transition into calm ambient clinical pad",
        },
        {
            "segment": "2. High-Retention Informative Body (0:03–0:50)",
            "visual_direction": "Fast-paced 2.4s B-roll cuts showing healthy habits, nutrition, and research highlights.",
            "on_screen_text": "SCIENCE-BACKED WELLNESS",
            "spoken_narration": body_sents or hook_sent,
            "audio_cue": "Steady rhythmic ambient pulse supporting spoken cadence",
        },
        {
            "segment": "3. Proper Thank You Conclusion (0:50–End)",
            "visual_direction": "Clean wellness summary card with subscriber callout.",
            "on_screen_text": "Subscribe for daily wellness tips.",
            "spoken_narration": outro_sents,
            "audio_cue": "Gentle 1.0s audio fade-out",
        },
    ]


def generate_script(topic: str, user_prompt: str) -> str:
    models = _build_model_list()
    last_err = None
    sources = select_credible_sources(topic, count=2)
    source_orgs = f"{sources[0]['org']} or {sources[1]['org']}"

    system_msg = (
        "You are an elite, engaging YouTube Shorts scriptwriter for health and wellness. "
        "Write a rich, detailed, high-retention script (~140-150 words) that maximizes duration up to 57 seconds. "
        f"Cite {source_orgs} naturally in speech. "
        "Output ONLY the raw speakable body sentences. Do NOT include <think> tags, markdown, "
        "speaker labels, timestamps, or sign-offs.\n"
        f"{NEGATIVE_PROMPT_RULES}"
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
                    "temperature": 0.72,
                    "max_tokens": 800,
                },
                timeout=120,
            )
            if r.status_code != 200:
                last_err = RuntimeError(f"Model {model} HTTP {r.status_code}: {r.text[:200]}")
                continue
            raw = r.json()["choices"][0]["message"]["content"].strip()
            cleaned = sanitize_spoken_script(raw, topic=topic, enforce_policy_blocks=False)
            if len(cleaned.split()) < 50:
                last_err = RuntimeError(f"Too few words ({len(cleaned.split())}) from {model}")
                continue
            log.info("[script] Generated %d sanitized spoken words using %s", len(cleaned.split()), model)
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
    except Exception as exc:
        log.warning("[tts] gTTS error: %s", exc)
        return False


def _try_espeak(text: str, out_path: str, lang: str) -> bool:
    try:
        wav = out_path.replace(".mp3", "_espeak.wav")
        subprocess.run(["espeak-ng", "-v", lang, "-s", "150", "-w", wav, text], capture_output=True, timeout=60)
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


def get_audio_duration(path: str) -> float:
    try:
        a = AudioFileClip(path)
        dur = float(a.duration)
        a.close()
        return dur
    except Exception:
        return 50.0


def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}",
            "-i", "anoisesrc=c=pink:a=0.03", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}",
            "-i", "sine=frequency=216:sample_rate=44100", "-filter_complex",
            "[0:a]lowpass=f=600[noise];[1:a]volume=0.02[tone];[noise][tone]amix=inputs=2,"
            f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, duration - 1.0):.2f}:d=1.0",
            "-c:a", "aac", "-b:a", "128k", out_path,
        ],
        check=True,
        capture_output=True,
    )
    return out_path


# ── Captions ──────────────────────────────────────────────────────────────────
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

        if not words:
            raise RuntimeError("No timestamps extracted by Whisper.")

        chunks = []
        i = 0
        while i < len(words):
            take = max(2, min(3, len(words) - i))
            if len(words) - i - take == 1 and take > 2:
                take -= 1
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
        if not out:
            raise RuntimeError("No subtitle chunks generated.")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        return True
    except Exception as exc:
        log.warning("[srt] Whisper failed (%s), falling back to blank SRT", exc)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{_fmt_time(0.0)} --> {_fmt_time(max(0.8, get_audio_duration(audio_path)))}\n \n\n")
        return False


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
            "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4,
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
    clean_q = " ".join(clean_q.split()[:4]) or "healthy lifestyle wellness"
    all_queries = [clean_q, "healthy lifestyle", "morning sunlight", "wellness nature", "nutrition fresh"]
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
    generated = []
    colors = ["0x0A2540", "0x00A896", "0x134E5E"]
    for i in range(count):
        out_p = tmp / f"synth_{i}.mp4"
        color = colors[i % len(colors)]
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=1080x1920:r=30:d=10",
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


def render_and_cap(
    broll_urls: list[str],
    voice_mp3: str,
    temp_mp4: str,
    final_mp4: str,
    thumbnail_text: str = "WHAT SCIENCE SAYS",
    topic: str = "",
    thumb_out_path: str = "thumbnail.jpg",
) -> float:
    """
    Renders video maximized to just below 59 seconds (PREVIEW_MAX = 58.5s) to capture maximum retention.
    """
    tmp = Path(tempfile.mkdtemp())
    local = _download_broll(broll_urls, tmp)
    if not local:
        raise RuntimeError("No B-roll clips could be downloaded or synthesized.")

    try:
        generate_shorts_thumbnail(
            thumbnail_text=thumbnail_text,
            topic=topic,
            video_path=local[0],
            out_path=thumb_out_path,
        )
    except Exception as tex:
        log.warning("[thumbnail] Thumbnail generation failed (%s)", tex)

    raw_clips, cropped, merged, voice_clip = [], [], None, None
    try:
        base_comps = []
        for p in local:
            try:
                c = VideoFileClip(p)
                raw_clips.append(c)
                clip_dur = float(c.duration)
                step = 2.4
                t_cursor = 0.0
                while t_cursor + 1.8 <= min(clip_dur, 8.5):
                    sub_end = min(clip_dur, t_cursor + step)
                    comp = smart_cover_crop(c.subclip(t_cursor, sub_end)).set_audio(None)
                    base_comps.append(comp)
                    t_cursor += step
                if not base_comps and clip_dur > 0.5:
                    comp = smart_cover_crop(c.subclip(0, min(clip_dur, 2.5))).set_audio(None)
                    base_comps.append(comp)
            except Exception as exc:
                log.warning("[render] clip load error on %s: %s", p, exc)

        if not base_comps:
            raise RuntimeError("No valid video clips could be decoded by MoviePy.")

        cropped = list(base_comps)
        voice_clip = AudioFileClip(voice_mp3)
        target_dur = float(voice_clip.duration)

        while sum(c.duration for c in cropped) < target_dur and len(cropped) < 80:
            for comp in base_comps:
                cropped.append(comp)
                if sum(c.duration for c in cropped) >= target_dur:
                    break

        merged = concatenate_videoclips(cropped, method="compose")
        end = max(45.0, min(PREVIEW_MAX, target_dur))

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
        shutil.rmtree(tmp, ignore_errors=True)

    srt_path = "cap.srt"
    transcribe_to_srt(voice_mp3, srt_path, lang=TTS_LANG)

    subbed = "subbed.mp4"
    try:
        burn_captions(temp_mp4, srt_path, subbed)
    except Exception as exc:
        log.warning("[captions] burn_captions failed (%s), using unsubbed video", exc)
        shutil.copyfile(temp_mp4, subbed)

    bgm_out = "with_bgm.mp4"
    try:
        bgm = ensure_bgm_track(end)
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


# ── YouTube Upload ────────────────────────────────────────────────────────────
def _yt_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        None,
        refresh_token=os.getenv("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID"),
        client_secret=os.getenv("YT_CLIENT_SECRET"),
    )
    return build("youtube", "v3", credentials=creds)


def upload_unlisted(
    video_path: str,
    title: str,
    desc: str,
    tags: str,
    thumbnail_path: str | None = None,
) -> tuple[str, str, bool]:
    from googleapiclient.http import MediaFileUpload
    yt = _yt_client()
    body = {
        "snippet": {
            "title": title[:100],
            "description": desc[:4900],
            "tags": [t.strip() for t in tags.split(",") if t.strip()][:12],
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()

    vid_id = resp["id"]
    thumb_uploaded = False
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            thumb_media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg", resumable=False)
            yt.thumbnails().set(videoId=vid_id, media_body=thumb_media).execute()
            thumb_uploaded = True
            log.info("[upload] Custom Pillow thumbnail uploaded for video %s", vid_id)
        except Exception as exc:
            log.warning("[upload] Custom thumbnail upload failed (%s)", exc)

    return vid_id, f"https://youtu.be/{vid_id}", thumb_uploaded


def build_preview(topic: str, user_script: str | None, slot: str, issue_body: str) -> tuple[dict | None, str]:
    best = {"path": None, "dur": None, "score": float("inf"), "attempt": 0, "seo": None, "segments": None, "thumb": None}
    last_error = None

    max_attempts = 1 if user_script else 2

    for attempt in range(1, max_attempts + 1):
        try:
            log.info("[build] Starting attempt %d/%d for topic: %s", attempt, max_attempts, topic)
            if user_script:
                script_text = sanitize_spoken_script(user_script, topic=topic, enforce_policy_blocks=False)
            else:
                prompt_tpl = load_default_prompt()
                actual_prompt = prompt_tpl.format(topic=topic)
                script_text = generate_script(topic, actual_prompt)

            segments = build_viral_shorts_segments(script_text, topic)
            seo = generate_seo_package(topic, script_text)

            voice_raw = f"voice_{attempt}.mp3"
            synthesize_speech(script_text, voice_raw, lang=TTS_LANG)

            broll = fetch_broll(topic, need=6)
            temp_mp4, final_mp4 = f"temp_{attempt}.mp4", f"short_{attempt}.mp4"
            thumb_path = f"thumb_{attempt}.jpg"

            dur = render_and_cap(
                broll_urls=broll,
                voice_mp3=voice_raw,
                temp_mp4=temp_mp4,
                final_mp4=final_mp4,
                thumbnail_text=seo.get("thumbnail_text", "WHAT SCIENCE SAYS"),
                topic=topic,
                thumb_out_path=thumb_path,
            )

            if not os.path.exists(thumb_path):
                thumb_path = None

            score = abs(57.5 - dur)
            if score < best["score"]:
                best_path = f"best_{attempt}.mp4"
                shutil.copyfile(final_mp4, best_path)
                best.update({
                    "path": best_path,
                    "dur": round(dur, 2),
                    "score": score,
                    "attempt": attempt,
                    "seo": seo,
                    "segments": segments,
                    "thumb": thumb_path,
                })

            if TARGET_LOW <= dur <= TARGET_HIGH or user_script:
                return _package_and_upload(final_mp4, seo, segments, topic, slot, issue_body, attempt, dur, thumb_path)
        except Exception as exc:
            last_error = exc
            log.error("[build] attempt %d failed: %s", attempt, exc, exc_info=True)

    if best["path"] and os.path.exists(best["path"]):
        return _package_and_upload(
            best["path"],
            best["seo"],
            best["segments"],
            topic,
            slot,
            issue_body,
            best["attempt"],
            best["dur"],
            best["thumb"],
        )

    if last_error:
        raise RuntimeError(f"Video generation failed: {last_error}")
    return None, issue_body


def _package_and_upload(video_path, seo, segments, topic, slot, issue_body, attempt, dur, thumb_path=None):
    try:
        log.info("[upload] Uploading %s to YouTube as unlisted (containsSyntheticMedia=True)...", video_path)
        vid_id, link, thumb_ok = upload_unlisted(
            video_path, seo["title"], seo["description"], seo["tags_csv"], thumbnail_path=thumb_path
        )
        log.info("[upload] Uploaded successfully: %s", link)
        meta = get_metadata_from_issue_body(issue_body) or {}
        meta.update({
            "topic": topic,
            "title": seo["title"],
            "punchy_short_title": seo.get("punchy_short_title", ""),
            "thumbnail_text": seo.get("thumbnail_text", ""),
            "thumbnail_prompt": seo.get("thumbnail_prompt", ""),
            "thumbnail_uploaded": thumb_ok,
            "description": seo["description"],
            "preview_video_id": vid_id,
            "preview_link": link,
            "slot": slot,
            "duration_sec": dur,
            "attempt": attempt,
            "viral_segments": segments,
        })
        return meta, set_metadata_in_issue_body(issue_body, meta)
    except Exception as exc:
        raise RuntimeError(f"YouTube upload failed: {exc}")


# ── Command Parsing & Handlers ────────────────────────────────────────────────
def _post_script_request(owner: str, repo: str, number: int, topic: str) -> None:
    body = (
        f"✅ **Topic approved:** {topic}\n\n"
        "Please provide your script text or generate a wellness script automatically:\n\n"
        "- **Option A — Use AI Default Prompt:**\n"
        "  Reply with: `/use-default-prompt`\n\n"
        "- **Option B — Provide Your Own Script:**\n"
        "  Reply with: `/set-script` followed by your full voiceover text."
    )
    post_comment(owner, repo, number, body)


def _clean_issue_title_to_topic(title: str) -> str:
    s = re.sub(r"(?:^|\s)#\w+", " ", (title or "").strip())
    s = re.sub(r"(?i)\bshorts?\b|\[[^\]]*\]|\([^)]*\)", " ", s)
    s = re.split(r"\s+\|\s+|\s+[-–—]{1,2}\s+", s)[0].strip()
    s = re.sub(r"\s+", " ", s).strip(" :;,-–—.!\"'")
    if len(re.findall(r"[a-zA-Z]{2,}", s)) < 3:
        return "Daily Health and Wellness Habits"
    return (s[0].upper() + s[1:])[:120].rstrip()


def _extract_topics_from_visible_body(issue_body: str) -> list[str]:
    if not issue_body:
        return []
    matches = re.findall(r"^\s*\d+[\)\.]\s+(.+)$", issue_body, flags=re.MULTILINE)
    return [m.strip() for m in matches if len(re.findall(r"[a-zA-Z]{2,}", m)) >= 3]


def _extract_user_script_if_present(comment_body: str) -> str | None:
    idx = comment_body.lower().find("/set-script")
    if idx != -1:
        script = comment_body[idx + len("/set-script") :].strip()
        for cmd in ["/approve-topic", "/custom-topic", "/use-default-prompt"]:
            c_idx = script.lower().find(cmd)
            if c_idx != -1:
                script = script[:c_idx].strip()
        return script if script else None
    return None


def _parse_topic_from_approve(comment_body: str, topics: list[str], issue_title: str = "") -> str:
    lower = comment_body.lower()
    idx_kw = lower.find("/approve-topic")
    body_part = comment_body[idx_kw + len("/approve-topic") :].strip() if idx_kw != -1 else comment_body.strip()

    for kw in ["/use-default-prompt", "/set-script", "/custom-topic"]:
        idx = body_part.lower().find(kw)
        if idx != -1:
            body_part = body_part[:idx].strip()

    first_line = body_part.splitlines()[0].strip() if body_part else ""

    if not first_line:
        return topics[0] if topics else _clean_issue_title_to_topic(issue_title)

    m_num = re.match(r"^(\d+)\)?$", first_line)
    if m_num:
        idx = int(m_num.group(1))
        if topics and 1 <= idx <= len(topics):
            candidate = topics[idx - 1]
            if len(re.findall(r"[a-zA-Z]{2,}", candidate)) >= 3:
                return candidate
        if topics and len(re.findall(r"[a-zA-Z]{2,}", topics[0])) >= 3:
            return topics[0]
        return _clean_issue_title_to_topic(issue_title)

    return first_line


def _parse_topic_from_custom(comment_body: str, issue_title: str = "") -> str:
    lower = comment_body.lower()
    idx_c = lower.find("/custom-topic")
    body_part = comment_body[idx_c + len("/custom-topic") :].strip() if idx_c != -1 else comment_body.strip()

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
    if not topic or len(re.findall(r"[a-zA-Z]{2,}", topic)) < 2:
        topic = topics[0] if topics else _clean_issue_title_to_topic(ctx.issue.get("title", ""))
        meta["topic"] = topic

    if user_script:
        meta["user_script"] = user_script
    else:
        meta.pop("user_script", None)

    new_body = set_metadata_in_issue_body(ctx.issue_body, meta)
    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body})
    ctx.issue_body = new_body

    mode_str = "custom script (sanitized)" if user_script else "AI wellness script"
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"🎬 Building max-retention video preview for **{topic}** (using {mode_str}).\nThis takes ~3–5 minutes...",
    )

    try:
        video_meta, new_body2 = build_preview(topic, user_script, ctx.slot, new_body)
    except Exception as exc:
        log.error("[build] error: %s", exc, exc_info=True)
        post_comment(
            ctx.owner,
            ctx.repo,
            ctx.number,
            f"❌ **Error building preview:**\n```\n{exc}\n```\nTry `/regenerate-video` or check repository secrets.",
        )
        return

    if not video_meta:
        post_comment(ctx.owner, ctx.repo, ctx.number, "❌ Could not produce a valid preview. Try `/regenerate-video`.")
        return

    gh("PATCH", f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}", json={"body": new_body2})
    ctx.issue_body = new_body2

    seg_rows = []
    for seg in video_meta.get("viral_segments", []):
        seg_rows.append(
            f"| **{seg['segment']}** | {seg['on_screen_text']} | {seg['spoken_narration']} | *{seg['audio_cue']}* |"
        )
    seg_table = (
        "| Segment | On-Screen Hook/Text | Spoken Narration | Audio Cue |\n"
        "|---|---|---|---|\n" + "\n".join(seg_rows)
        if seg_rows
        else ""
    )

    thumb_status = "Uploaded via YouTube API ✓" if video_meta.get("thumbnail_uploaded") else "Generated locally ✓"

    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"✅ **Preview Ready!** (Attempt {video_meta['attempt']}, {video_meta['duration_sec']}s < 59s)\n"
        f"🔗 **Watch Unlisted Preview:** {video_meta['preview_link']}\n\n"
        f"### 📦 Viral Packaging & Compliance Matrix\n"
        f"- **SEO Video Title:** `{video_meta['title']}`\n"
        f"- **Punchy Alt Title (<6 words):** `{video_meta.get('punchy_short_title', '')}`\n"
        f"- **3-Word Complementary Thumbnail Text:** `{video_meta.get('thumbnail_text', '')}` *(0% word overlap with title)*\n"
        f"- **Thumbnail Status:** `{thumb_status}`\n"
        f"- **AI Thumbnail Prompt:** `{video_meta.get('thumbnail_prompt', '')}`\n"
        f"- **2026 Synthetic Media Flag:** `containsSyntheticMedia = True`\n\n"
        f"### 🎙️ Shorts Script Breakdown\n"
        f"{seg_table}\n\n"
        "Reply with:\n"
        "- `/approve-video` — schedule for publication\n"
        "- `/regenerate-video` — rebuild with fresh B-roll\n"
        "- `/reject-video` — delete preview and pick a new topic",
    )


def handle_reject_topic(ctx: Ctx) -> None:
    try:
        from propose import gather_topics
        options = gather_topics(REGION, need=6, exclude=set())
    except Exception:
        options = []

    if len(options) < 6:
        options = [
            "How 10 Minutes of Morning Sunlight Optimizes Your Circadian Clock",
            "Why Delaying Morning Caffeine by 60 Minutes Prevents Afternoon Crashes",
            "Zone 2 Cardio: The Science Behind Longevity and Mitochondrial Health",
            "Magnesium Glycinate and Its Role in Deep Restorative Sleep Architecture",
            "How Walking 10 Minutes After Meals Regulates Postprandial Glucose",
            "Prebiotic Fiber and the Gut Microbiome: What Harvard Research Shows",
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
        f"🔄 **Fresh Wellness Topic Options:**\n{numbered}\n\n"
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


def handle_reject_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if vid_id:
        try:
            _yt_client().videos().delete(id=vid_id).execute()
        except Exception:
            pass

    for k in [
        "preview_video_id", "preview_link", "title", "punchy_short_title",
        "thumbnail_text", "thumbnail_prompt", "thumbnail_uploaded", "description",
        "attempt", "duration_sec", "viral_segments",
    ]:
        meta.pop(k, None)
    gh(
        "PATCH",
        f"https://api.github.com/repos/{ctx.owner}/{ctx.repo}/issues/{ctx.number}",
        json={"body": set_metadata_in_issue_body(ctx.issue_body, meta)},
    )
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        "🗑️ Deleted preview video. Reply `/new-topic` for fresh options or `/custom-topic Your Topic`.",
    )


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


def handle_approve_video(ctx: Ctx) -> None:
    meta = get_metadata_from_issue_body(ctx.issue_body) or {}
    vid_id = meta.get("preview_video_id")
    if not vid_id:
        user_script = meta.get("user_script")
        _run_build_with_script(ctx, user_script)
        meta = get_metadata_from_issue_body(ctx.issue_body) or {}
        vid_id = meta.get("preview_video_id")
        if not vid_id:
            return

    yt = _yt_client()
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    hour = 16 if meta.get("slot") == "afternoon" else 9
    ist_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, 0, tzinfo=IST)
    publish_utc = ist_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    yt.videos().update(
        part="status",
        body={
            "id": vid_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_utc,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
        },
    ).execute()
    post_comment(
        ctx.owner,
        ctx.repo,
        ctx.number,
        f"🚀 **Scheduled:** https://youtu.be/{vid_id}\n"
        f"📅 **Publishes at (IST):** `{ist_dt.strftime('%Y-%m-%d %H:%M')}`\n"
        f"🛡️ **2026 Synthetic Media Disclosure:** `Enabled`\n\n"
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


if __name__ == "__main__":
    main()

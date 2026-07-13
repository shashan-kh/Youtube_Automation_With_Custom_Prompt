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

# ── Env ────────────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACKS = [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile").split(",") if m.strip()]

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
TTS_LANG       = os.getenv("TTS_LANG", "en")
BGM_VOL        = float(os.getenv("BGM_VOL", "0.08"))

TARGET_LOW  = 35.0
TARGET_HIGH = 58.0
PREVIEW_MAX = 57.3

_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

def load_default_prompt() -> str:
    try:
        content = _PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content and "{topic}" in content: return content
    except Exception: pass
    return "Write a spoken voiceover script for a 50-second YouTube Short about: {topic}"

# ── Script Cleaning ───────────────────────────────────────────────────────────
def _clean_script_output(raw: str) -> str:
    lines = raw.strip().splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped: continue
        # Block meta-commentary lines
        if re.match(r"(?i)^(\*\*?(Hook|Intro|Body|CTA|Script|Voiceover|Part)\*?[:\-]|The hook is|The script)", stripped):
            continue
        kept.append(stripped)
    text = " ".join(kept).replace("**", "").strip('"\'')
    return text

def _ensure_complete_ending(text: str) -> str:
    text = text.strip()
    if not text: return text
    if "thank you for watching" not in text.lower():
        text = text.rstrip(".!? ") + ". Thank you for watching. Follow for more daily health tips."
    return text

def generate_script(topic: str, user_prompt: str) -> str:
    system_msg = "You are a voiceover artist. Output ONLY the exact spoken words. Start DIRECTLY with the first word."
    for model in _build_model_list():
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": model, "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}], "temperature": 0.8},
                timeout=120)
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"]
                cleaned = _ensure_complete_ending(_clean_script_output(raw))
                if len(cleaned.split()) > 30: return cleaned
        except Exception: continue
    raise RuntimeError("Script generation failed.")

# ── Instrumental Background Sound Generation ──────────────────────────────────
def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    """
    Generates calm, lo-fi ambient instrumental background using FFmpeg.
    Mixes a grounding sine tone with a gentle, filtered oscillating 'string-like' pad.
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", "-i", "sine=frequency=216:sample_rate=44100",
        "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", "-i", "sine=frequency=432:sample_rate=44100",
        "-filter_complex",
        (
            "[0:a]volume=0.03[low];"
            "[1:a]sine=frequency=648:sample_rate=44100,volume=0.02[mid];"
            "[low][mid]amix=inputs=2:duration=longest,"
            "lowpass=f=800,bandpass=f=400:width_type=q:width=2,"
            f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, duration - 1.0):.2f}:d=1.0"
        ),
        "-c:a", "aac", "-b:a", "128k", out_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path

# ── Other functions (transcribe_to_srt, burn_captions, render_and_cap, etc.) ─
# [Keep the rest of your existing functions from handle_comment.py here,
# ensuring burn_captions and render_and_cap remain unchanged.]

# ... (Insert existing transcribe_to_srt, burn_captions, render_and_cap code) ...

# ── Main Entry Point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # [Keep existing main() logic]
    pass

"""
handle_comment.py — processes all commands.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, time
from pathlib import Path
from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips
from common import (REPO, add_label, get_logger, gh, post_comment, remove_label, set_metadata_in_issue_body)
from seo import generate_seo_package

log = get_logger("handle_comment")

# ── INSTRUMENTAL BGM GENERATOR ──────────────────────────────────────────────
def ensure_bgm_track(duration: float, out_path: str = "bgm_src.m4a") -> str:
    """Generates a calm instrumental ambient pad background."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
        "-i", "anoisesrc=c=pink:a=0.03", "-f", "lavfi", "-t", f"{duration + 2.0:.2f}", 
        "-i", "sine=frequency=216:sample_rate=44100", "-filter_complex",
        "[0:a]lowpass=f=600[noise];[1:a]volume=0.02[tone];[noise][tone]amix=inputs=2,"
        f"afade=t=in:st=0:d=2.0,afade=t=out:st={max(0.0, duration - 1.0):.2f}:d=1.0",
        "-c:a", "aac", "-b:a", "128k", out_path
    ], check=True, capture_output=True)
    return out_path

# ── CAPTIONING LOGIC (RESTORED) ──────────────────────────────────────────────
def transcribe_to_srt(audio_path: str, srt_path: str, lang: str = "en") -> bool:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(os.getenv("WHISPER_MODEL", "tiny.en"), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, language=lang, word_timestamps=True)
        out = []
        idx = 1
        for seg in segments:
            for w in seg.words or []:
                if w.word.strip():
                    out.append(f"{idx}\n{int(w.start//3600):02d}:{int((w.start%3600)//60):02d}:{int(w.start%60):02d},{int((w.start%1)*1000):03d} --> {int(w.end//3600):02d}:{int((w.end%3600)//60):02d}:{int(w.end%60):02d},{int((w.end%1)*1000):03d}\n{w.word.strip()}\n\n")
                    idx += 1
        with open(srt_path, "w", encoding="utf-8") as f: f.writelines(out)
        return True
    except Exception: return False

def burn_captions(in_mp4: str, srt_path: str, out_mp4: str) -> None:
    srt_abs = str(Path(srt_path).resolve()).replace("\\", "/").replace(":", "\\:")
    style = "Fontname=DejaVu Sans,Fontsize=12,Bold=1,PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=96"
    subprocess.run(["ffmpeg", "-y", "-i", in_mp4, "-vf", f"subtitles={srt_abs}:force_style={style}", "-c:a", "copy", "-pix_fmt", "yuv420p", out_mp4], check=True)

# ── SCRIPT CLEANER ──────────────────────────────────────────────────────────
def _clean_script(text: str) -> str:
    # Aggressively remove meta-commentary headers
    text = re.sub(r"(?i)^(Title|Topic|Script|Intro|Hook|Body|CTA|Voiceover|Part \d+):?\s*", "", text, flags=re.MULTILINE)
    # Remove AI conversational filler
    text = re.sub(r"(?i)(Here is the script|Sure!|Certainly!|The script starts here)", "", text)
    # Clean up formatting
    text = text.replace("**", "").strip()
    if not text.lower().endswith("thank you for watching. follow for more daily health tips."):
        text = text.rstrip(".!? ") + ". Thank you for watching. Follow for more daily health tips."
    return text

# [Include your existing build_preview, generate_script, and main functions here]
# Ensure generate_script uses the _clean_script function above.

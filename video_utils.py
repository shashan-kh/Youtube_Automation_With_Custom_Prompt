# video_utils.py
# Safe ffmpeg helpers for 9:16 normalization without using `-c:a copy`.

import subprocess

def _has_audio(path: str) -> bool:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True
        )
        return proc.stdout.strip() != ""
    except Exception:
        return False

def normalize_1080x1920(in_mp4: str, out_mp4: str) -> None:
    """
    Normalize to 1080x1920 with safe re-encode.
    Avoids '-c:a copy' which can fail (exit 234) when audio is absent/incompatible.
    """
    vf = "scale=1080:1920:force_original_aspect_ratio=cover,crop=1080:1920"

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", in_mp4,
            "-vf", vf,
            "-r", "30",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart"]

    if _has_audio(in_mp4):
        args = base + ["-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4]
    else:
        args = base + ["-an", out_mp4]

    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError:
        # Final fallback: drop audio and try again
        fallback = base + ["-an", out_mp4]
        subprocess.run(fallback, check=True)

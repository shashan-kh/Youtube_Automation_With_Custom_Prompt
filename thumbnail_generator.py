"""
thumbnail_generator.py — Automated 9:16 (1080x1920) & 16:9 High-CTR Thumbnail Generator using Pillow.
Renders the 3-word complementary thumbnail text in bold all-caps typography with high-contrast stroke,
clinical navy (#0A2540) and medical teal (#00A896) grading, and optional video frame extraction.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from common import get_logger

log = get_logger("thumbnail_generator")

try:
    import cv2
except Exception:
    cv2 = None

# Brand Palette (RGB)
NAVY_RGB: Tuple[int, int, int] = (10, 37, 64)       # #0A2540
TEAL_RGB: Tuple[int, int, int] = (0, 168, 150)      # #00A896
AMBER_RGB: Tuple[int, int, int] = (245, 158, 11)    # #F59E0B
WHITE_RGB: Tuple[int, int, int] = (248, 250, 252)   # #F8FAFC
BLACK_RGB: Tuple[int, int, int] = (10, 14, 20)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/impact.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in _FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _extract_best_video_frame(video_path: str | None, width: int, height: int) -> Image.Image | None:
    """Extracts a sharp, well-lit frame from the rendered MP4 video using OpenCV."""
    if not video_path or not os.path.exists(video_path) or cv2 is None:
        return None
    try:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None

        # Sample candidate frames at 20%, 35%, and 50% of the video
        best_frame = None
        best_sharpness = -1.0
        for ratio in (0.20, 0.35, 0.50):
            target_idx = max(0, min(total_frames - 1, int(total_frames * ratio)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if sharpness > best_sharpness:
                best_sharpness = sharpness
                best_frame = frame

        cap.release()
        if best_frame is None:
            return None

        rgb = cv2.cvtColor(best_frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        return img.resize((width, height), Image.LANCZOS)
    except Exception as exc:
        log.warning("[thumbnail] frame extraction failed: %s", exc)
        return None


def _build_clinical_gradient_canvas(width: int, height: int) -> Image.Image:
    """Creates a clean clinical navy-to-teal vertical gradient canvas."""
    base = Image.new("RGB", (width, height), NAVY_RGB)
    top_r, top_g, top_b = NAVY_RGB
    bot_r, bot_g, bot_b = (8, 92, 98)

    draw = ImageDraw.Draw(base)
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(top_r + (bot_r - top_r) * t)
        g = int(top_g + (bot_g - top_g) * t)
        b = int(top_b + (bot_b - top_b) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    return base


def _apply_clinical_vignette(img: Image.Image) -> Image.Image:
    """Applies high-contrast color grading and a dark clinical gradient overlay for text legibility."""
    width, height = img.size
    enhanced = ImageEnhance.Contrast(img).enhance(1.18)
    enhanced = ImageEnhance.Color(enhanced).enhance(1.10)
    enhanced = enhanced.filter(ImageFilter.GaussianBlur(radius=1.2))

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    for y in range(height):
        rel = y / max(1, height - 1)
        # Stronger darkening in the center/bottom third where the 3-word text sits
        if rel < 0.25:
            alpha = int(140 * (1.0 - rel / 0.25) + 55)
        elif rel > 0.45:
            alpha = int(75 + 155 * ((rel - 0.45) / 0.55))
        else:
            alpha = 85
        odraw.line([(0, y), (width, y)], fill=(10, 30, 52, min(230, max(40, alpha))))

    combined = Image.alpha_composite(enhanced.convert("RGBA"), overlay)
    return combined.convert("RGB")


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=6)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def generate_shorts_thumbnail(
    thumbnail_text: str,
    topic: str = "",
    video_path: str | None = None,
    out_path: str = "thumbnail.jpg",
    size: Tuple[int, int] = (1080, 1920),
) -> str:
    """
    Renders the 3-word complementary thumbnail text onto a 9:16 (1080x1920) frame.
    Automatically compresses below 2MB for YouTube Data API compliance.
    """
    width, height = size
    frame = _extract_best_video_frame(video_path, width, height)
    if frame is None:
        canvas = _build_clinical_gradient_canvas(width, height)
    else:
        canvas = _apply_clinical_vignette(frame)

    draw = ImageDraw.Draw(canvas)

    # 1. Top & Bottom Medical Teal Accent Bars
    bar_h = 18
    draw.rectangle([(0, 0), (width, bar_h)], fill=TEAL_RGB)
    draw.rectangle([(0, height - bar_h), (width, height)], fill=TEAL_RGB)

    # 2. Top Pill Badge ("SCIENCE & HEALTH AWARENESS")
    badge_font = _load_font(34)
    badge_text = "HEALTH & SYMPTOM AWARENESS"
    bw, bh = _measure_text(draw, badge_text, badge_font)
    pad_x, pad_y = 36, 18
    bx0 = (width - (bw + pad_x * 2)) // 2
    by0 = int(height * 0.12)
    bx1 = bx0 + bw + pad_x * 2
    by1 = by0 + bh + pad_y * 2

    draw.rounded_rectangle([(bx0, by0), (bx1, by1)], radius=28, fill=TEAL_RGB, outline=WHITE_RGB, width=3)
    draw.text(
        (bx0 + pad_x, by0 + pad_y - 2),
        badge_text,
        font=badge_font,
        fill=WHITE_RGB,
    )

    # 3. 3-Word Complementary Thumbnail Text (Stacked 1 word per line for maximum 9:16 impact)
    clean_words = [w.strip().upper() for w in (thumbnail_text or "WHAT SCIENCE SAYS").split() if w.strip()][:3]
    if not clean_words:
        clean_words = ["WHAT", "SCIENCE", "SAYS"]

    # Dynamically fit font size so the longest word fills ~82% of width
    font_size = 148
    headline_font = _load_font(font_size)
    while font_size > 72:
        headline_font = _load_font(font_size)
        max_w = max(_measure_text(draw, w, headline_font)[0] for w in clean_words)
        if max_w <= int(width * 0.84):
            break
        font_size -= 6

    line_heights = [_measure_text(draw, w, headline_font)[1] for w in clean_words]
    line_gap = 28
    total_text_h = sum(line_heights) + line_gap * (len(clean_words) - 1)
    cur_y = int(height * 0.50) - (total_text_h // 2)

    # Subtle glowing backdrop box behind the 3 words
    box_pad = 44
    draw.rounded_rectangle(
        [(64, cur_y - box_pad), (width - 64, cur_y + total_text_h + box_pad)],
        radius=36,
        fill=(10, 37, 64),
        outline=AMBER_RGB,
        width=5,
    )

    for idx, word in enumerate(clean_words):
        ww, wh = _measure_text(draw, word, headline_font)
        wx = (width - ww) // 2
        # Highlight the final word in high-CTR Amber/Yellow, first words in crisp White
        fill_color = AMBER_RGB if idx == len(clean_words) - 1 else WHITE_RGB
        # Drop shadow
        draw.text(
            (wx + 6, cur_y + 8),
            word,
            font=headline_font,
            fill=BLACK_RGB,
            stroke_width=10,
            stroke_fill=BLACK_RGB,
        )
        # Main bold text
        draw.text(
            (wx, cur_y),
            word,
            font=headline_font,
            fill=fill_color,
            stroke_width=8,
            stroke_fill=BLACK_RGB,
        )
        cur_y += wh + line_gap

    # 4. Bottom YMYL Disclaimer Footer
    footer_font = _load_font(28)
    footer_text = "Educational Information Only • Not Medical Advice"
    fw, fh = _measure_text(draw, footer_text, footer_font)
    fx = (width - fw) // 2
    fy = int(height * 0.88)
    draw.text(
        (fx, fy),
        footer_text,
        font=footer_font,
        fill=WHITE_RGB,
        stroke_width=3,
        stroke_fill=BLACK_RGB,
    )

    # Save JPEG < 2MB for YouTube API compatibility
    out_p = Path(out_path)
    quality = 92
    canvas.save(out_p, format="JPEG", quality=quality, optimize=True)
    while out_p.stat().st_size > 1_950_000 and quality > 60:
        quality -= 8
        canvas.save(out_p, format="JPEG", quality=quality, optimize=True)

    log.info("[thumbnail] Rendered 9:16 thumbnail (%d bytes) -> %s", out_p.stat().st_size, out_p)
    return str(out_p)

import os, re, json, tempfile, subprocess, requests, traceback, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from moviepy.editor import (
    VideoFileClip, concatenate_videoclips, AudioFileClip
)
from gtts import gTTS

# Optional: OpenCV for simple face detection to focus crop (subject-aware cover)
try:
    import cv2
except Exception:
    cv2 = None

# Env
REGION = os.getenv("REGION", "IN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Models (override via workflow env)
DEFAULT_PRIMARY = "qwen/qwen3-32b"
DEFAULT_FALLBACKS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "groq/compound-mini",
    "groq/compound",
    "moonshotai/kimi-k2-instruct",
]
GROQ_MODEL = os.getenv("GROQ_MODEL", DEFAULT_PRIMARY).strip()
GROQ_FALLBACK_MODELS_ENV = os.getenv("GROQ_FALLBACK_MODELS", ",".join(DEFAULT_FALLBACKS)).strip()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
TTS_LANG = os.getenv("TTS_LANG", "en")

# Background music source (optional CC0/royalty-free URL). If not set, synthesize.
BGM_URL = os.getenv("BGM_URL", "").strip()
try:
    BGM_VOL = float(os.getenv("BGM_VOL", "0.07"))
except Exception:
    BGM_VOL = 0.07

SAFE_TAGS = ["health","wellness","habits","selfcare","sleep","hydration","movement","posture","stress","nutrition"]
PREVIEW_MIN = 35.0         # Accept videos >= 35s
PREVIEW_TARGET_MIN = 50.0  # Aim to hit 50–58s first
PREVIEW_MAX = 57.3
IST = timezone(timedelta(hours=5, minutes=30))

def is_authorized_commenter(event):
    assoc = (event.get("comment", {}).get("author_association") or "").upper()
    commenter = event.get("comment", {}).get("user", {}).get("login", "")
    repo_owner = (event.get("repository", {}).get("owner", {}) or {}).get("login", "")
    return assoc in {"OWNER", "MEMBER", "COLLABORATOR"} or commenter == repo_owner

def gh(method, url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"})
    r = requests.request(method, url, headers=headers, timeout=120, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
    return r

def ev():
    with open(os.getenv("GITHUB_EVENT_PATH"), "r", encoding="utf-8") as f:
        return json.load(f)

def post_comment(owner, repo, number, body):
    gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})

def add_label(owner, repo, number, label):
    gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels", json={"labels":[label]})

def remove_label(owner, repo, number, label):
    requests.delete(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels/{label}",
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"})

def get_issue(owner, repo, number):
    return gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}").json()

def extract_json_block(text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if m: return m.group(1)
    m = re.search(r"```\s*(\{.*?\})\s*```", text, re.S)
    if m: return m.group(1)
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else None

def parse_topics_from_body(body):
    lines = body.splitlines()

    def collect_after_marker(marker):
        idxs = [i for i, l in enumerate(lines) if marker in l.lower()]
        if not idxs:
            return []
        start = idxs[-1] + 1
        out = []
        for ln in lines[start:]:
            s = ln.strip()
            if not s:
                break
            if "reply with" in s.lower():
                break
            m = re.match(r"^\s*(?:[-*•]\s*)?(?:\d+[\)\.\-:])\s+(.*\S)", s)
            if not m:
                if re.match(r"^[A-Z].*:$", s) or s.startswith("/"):
                    break
                continue
            out.append(m.group(1).strip())
        return out[:3]

    t = collect_after_marker("new topic options:")
    if t: return t
    t = collect_after_marker("choose one:")
    if t: return t

    try:
        block = extract_json_block(body)
        if block:
            data = json.loads(block)
            if isinstance(data, dict) and isinstance(data.get("topics"), list) and data["topics"]:
                return [str(x).strip() for x in data["topics"] if str(x).strip()][:3]
    except Exception:
        pass

    fallback = []
    for ln in lines:
        m = re.match(r"^\s*(?:[-*•]\s*)?(?:\d+[\)\.\-:])\s+(.*\S)", ln)
        if m:
            fallback.append(m.group(1).strip())
    return fallback[:3]

def get_slot_from_labels(labels):
    for lbl in labels or []:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.startswith("slot:"):
            return name.split(":", 1)[1].strip() or "morning"
    return "morning"

def call_groq(prompt, model):
    return requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": model, "messages": [{"role":"user","content":prompt}], "temperature": 0.8},
        timeout=120
    )

def list_groq_models():
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                         timeout=60)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return [d.get("id") for d in data if isinstance(d, dict) and d.get("id")]
    except Exception:
        pass
    return []

def build_model_list():
    env_models = []
    if GROQ_MODEL:
        env_models.append(GROQ_MODEL)
    if GROQ_FALLBACK_MODELS_ENV:
        env_models.extend([m.strip() for m in GROQ_FALLBACK_MODELS_ENV.split(",") if m.strip()])
    seen = set(); env_models = [m for m in env_models if not (m in seen or seen.add(m))]
    available = list_groq_models()
    if not available:
        return env_models if env_models else [DEFAULT_PRIMARY] + DEFAULT_FALLBACKS
    ordered = [m for m in env_models if m in available]
    patterns = [
        r"^qwen.*32b", r"^qwen.*14b", r"^qwen.*7b",
        r"llama-3\.3.*versatile", r"llama-3\.1.*instant",
        r"openai/gpt-oss-120b", r"openai/gpt-oss-20b",
        r"groq/compound-mini", r"groq/compound",
        r"moonshotai/kimi-k2-instruct"
    ]
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for mid in available:
            if rx.search(mid) and mid not in ordered:
                ordered.append(mid)
    for mid in available:
        if mid not in ordered:
            ordered.append(mid)
    return ordered[:10]

def llm_script(trending_query, word_hint="130–160"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret")
    prompt = f"""
You are a careful health educator. Create a strictly under-58s YouTube Short based on this trending query:
"{trending_query}"

Rules:
- General wellness only (sleep, hydration, movement, posture, stress, basic nutrition).
- No disease claims, diagnoses, dosages, or supplement promises. Avoid COVID/vaccines.
- If the query is unsafe/specific (e.g., drugs/diseases), pivot to a safe, related habit.
- Style: energetic, plain language, second-person; target {word_hint} words so the final video is 50–58 seconds.

Output pure JSON with:
- voiceover: string
- overlay_lines: array (ignored for captions; we use exact voiceover words)
- title: catchy, <=90 chars, include #Shorts
- description: 2–3 sentences + “Educational only, not medical advice.” + 1 credible source (WHO/CDC/NIH/NHS)
- tags: 6–10 comma-separated general tags
"""
    models_to_try = build_model_list()
    last_err = None
    for model in models_to_try:
        r = call_groq(prompt, model)
        if r.status_code == 200:
            raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not raw:
                last_err = RuntimeError(f"Groq '{model}' returned empty content")
                continue
            block = extract_json_block(raw) or raw
            try:
                data = json.loads(block)
                if isinstance(data.get("tags"), list):
                    data["tags"] = ",".join(data["tags"])
                return data
            except Exception as e:
                last_err = RuntimeError(f"Failed to parse LLM JSON from '{model}': {e}\nRaw: {raw[:500]}")
                continue
        else:
            try:
                errj = r.json()
            except Exception:
                errj = {"error": {"message": r.text}}
            msg = str(errj.get("error", {}).get("message", r.text))
            last_err = RuntimeError(f"Groq model '{model}' failed: {msg}")
            continue
    avail = list_groq_models()
    if avail:
        raise RuntimeError(f"{last_err}\nAvailable models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL/GROQ_FALLBACK_MODELS to one of the above.")
    raise last_err or RuntimeError("Groq call failed; no models available to try")

# ---------- Audio and captions ----------
def ensure_voice_in_range(voice_path, min_sec=PREVIEW_MIN, max_sec=PREVIEW_MAX):
    """Normalize voice length into target band using ffmpeg atempo when feasible."""
    a = AudioFileClip(voice_path); dur = a.duration; a.close()
    if min_sec <= dur <= max_sec:
        return voice_path, dur
    target = max(min_sec, min(max_sec, dur))
    factor = max(0.5, min(2.0, (target / dur) if dur else 1.0))
    if abs(factor - 1.0) < 1e-3:
        return voice_path, dur
    out = "voice_adj.mp3"
    subprocess.run(["ffmpeg","-y","-i",voice_path,"-filter:a",f"atempo={factor}","-vn",out], check=True)
    a2 = AudioFileClip(out); d2 = a2.duration; a2.close()
    return out, d2

def fetch_broll(query, need=6):
    if not PEXELS_API_KEY:
        raise RuntimeError("Missing PEXELS_API_KEY secret")
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    vids = []
    for q in [query, "healthy lifestyle", "fitness", "sleep", "hydration", "walking", "stretching", "posture", "nutrition", "yoga"]:
        try:
            r = requests.get(url, headers=headers, params={"query": q, "per_page": 30, "min_height": 1080}, timeout=60)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Pexels API auth error {r.status_code}: {r.text}")
            for v in r.json().get("videos", []):
                files = sorted(v.get("video_files", []), key=lambda f: f.get("height",0), reverse=True)
                for f in files:
                    if f.get("height",0) >= 1080:
                        vids.append(f["link"]); break
            if len(vids) >= need: break
        except Exception:
            continue
    import random
    random.shuffle(vids)
    return vids[:need]

def fmt_time(t):
    h = int(t // 3600); m = int((t % 3600)//60); s = int(t % 60); ms = int((t - int(t))*1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def transcribe_to_srt_faster_whisper(audio_path, srt_path, lang="en", min_chunk_words=2, max_chunk_words=3, min_dur=0.35, gap=0.02):
    """
    Transcribe the voice audio and write an SRT where each caption shows only 2–3 words,
    tightly synced to the narration using word-level timestamps.
    """
    # Duration for clamping
    try:
        a = AudioFileClip(audio_path)
        audio_dur = float(a.duration)
        a.close()
    except Exception:
        audio_dur = None

    try:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "tiny.en").strip() or "tiny.en"
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 200},
            word_timestamps=True
        )

        words = []
        for seg in segments:
            for w in (seg.words or []):
                txt = (w.word or "").strip()
                if not txt:
                    continue
                words.append((txt, float(w.start), float(w.end)))

        if not words:
            raise RuntimeError("No word-level timestamps available")

        # Group into 2–3 word chunks
        chunks = []
        i = 0
        while i < len(words):
            remaining = len(words) - i
            take = max(min_chunk_words, min(max_chunk_words, remaining))
            # Avoid leaving a 1-word tail
            if remaining - take == 1 and take > min_chunk_words:
                take -= 1
            group = words[i:i+take]
            start = group[0][1]
            end = group[-1][2]
            text = " ".join(w[0] for w in group)
            chunks.append((text, start, end))
            i += take

        out = []
        idx = 1
        cur_t = chunks[0][1] if chunks else 0.0
        if cur_t < 0:
            cur_t = 0.0

        for text, w_start, w_end in chunks:
            start = max(cur_t, w_start)
            end = max(start + min_dur, w_end)

            if audio_dur is not None:
                start = min(start, max(0.0, audio_dur - 0.01))
                end = min(end, audio_dur)

            if end <= start:
                continue

            out.append(f"{idx}\n{fmt_time(start)} --> {fmt_time(end)}\n{text}\n\n")
            idx += 1
            cur_t = end + gap
            if audio_dur is not None and cur_t >= audio_dur:
                break

        if not out:
            if audio_dur is None:
                audio_dur = 1.0
            out.append(f"1\n{fmt_time(0.0)} --> {fmt_time(max(0.8, audio_dur))}\n\n")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        return True

    except Exception as e:
        print("Word-level transcription failed; falling back to naive 2–3 word chunks:", e)
        # Fallback: evenly distribute 2–3 word chunks across duration
        try:
            from faster_whisper import WhisperModel
            model_name = os.getenv("WHISPER_MODEL", "tiny.en").strip() or "tiny.en"
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            segments, _info = model.transcribe(audio_path, language=lang, beam_size=5)
            text = " ".join([(s.text or "").strip() for s in segments]).strip()
        except Exception:
            text = ""

        if audio_dur is None:
            audio_dur = 10.0

        words = [w for w in re.split(r"\s+", text) if w]
        if not words:
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(f"1\n{fmt_time(0.0)} --> {fmt_time(audio_dur)}\n\n")
            return False

        chunks = []
        i = 0
        while i < len(words):
            remaining = len(words) - i
            take = 3 if remaining >= 3 else remaining
            if take == 1 and chunks:
                last = chunks.pop()
                chunks.append(last + " " + words[i])
                i += 1
                break
            chunks.append(" ".join(words[i:i+take]))
            i += take

        per = max(min_dur, (audio_dur - 0.1) / max(1, len(chunks)))
        out = []
        t = 0.05
        for idx, c in enumerate(chunks, 1):
            start = min(t, max(0.0, audio_dur - 0.05))
            end = min(start + per, audio_dur)
            if end <= start:
                break
            out.append(f"{idx}\n{fmt_time(start)} --> {fmt_time(end)}\n{c}\n\n")
            t = end + gap
            if t >= audio_dur:
                break

        with open(srt_path, "w", encoding="utf-8") as f:
            f.writelines(out)
        return False

def _ffmpeg_escape(s):
    return (
        s.replace("\\", "\\\\")
         .replace(":", "\\:")
         .replace(",", "\\,")
         .replace("'", "\\'")
    )

def burn_captions(in_mp4, srt_path, out_mp4):
    # Bottom-center (Alignment=2). Small (12pt), yellow with black stroke, no filled box.
    # ASS color: &HAABBGGRR&; yellow = &H0000FFFF&, black = &H00000000&
    style = (
        "Fontname=DejaVu Sans,Fontsize=12,Bold=1,"
        "PrimaryColour=&H0000FFFF&,OutlineColour=&H00000000&,"
        "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=100,Spacing=0"
    )
    vf = f"subtitles={_ffmpeg_escape(srt_path)}:force_style={_ffmpeg_escape(style)}"
    subprocess.run([
        "ffmpeg","-y","-i",in_mp4,"-vf",vf,"-c:a","copy","-pix_fmt","yuv420p",out_mp4
    ], check=True)

def ensure_bgm_track(duration, out_path="bgm_src.m4a"):
    # Download provided CC0/royalty-free track or synthesize a gentle tone bed
    if BGM_URL:
        raw = "bgm_raw"
        ext = ".mp3"
        try:
            with requests.get(BGM_URL, stream=True, timeout=120) as r:
                r.raise_for_status()
                ct = r.headers.get("content-type","").lower()
                if "mpeg" in ct or BGM_URL.lower().endswith(".mp3"): ext = ".mp3"
                elif "aac" in ct or BGM_URL.lower().endswith(".m4a"): ext = ".m4a"
                elif "ogg" in ct or BGM_URL.lower().endswith(".ogg"): ext = ".ogg"
                with open(raw+ext, "wb") as f:
                    for ch in r.iter_content(1024*256):
                        f.write(ch)
        except Exception:
            pass
        else:
            subprocess.run([
                "ffmpeg","-y","-stream_loop","-1","-i",raw+ext,
                "-t", f"{duration+0.5:.2f}",
                "-af", f"afade=t=in:st=0:d=0.8,afade=t=out:st={max(0.0, duration-0.8):.2f}:d=0.8",
                "-c:a","aac","-b:a","128k", out_path
            ], check=True)
            return out_path
    # synthesize soft dual-sine bed
    subprocess.run([
        "ffmpeg","-y",
        "-f","lavfi","-t", f"{duration+0.3:.2f}", "-i","sine=frequency=432:sample_rate=44100",
        "-f","lavfi","-t", f"{duration+0.3:.2f}", "-i","sine=frequency=528:sample_rate=44100",
        "-filter_complex", f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,lowpass=f=1200,"
                           f"afade=t=in:st=0:d=0.8,afade=t=out:st={max(0.0, duration-0.8):.2f}:d=0.8",
        "-c:a","aac","-b:a","128k", out_path
    ], check=True)
    return out_path

def add_bgm_to_video(in_mp4, out_mp4, duration):
    bgm = ensure_bgm_track(duration)
    subprocess.run([
        "ffmpeg","-y",
        "-i", in_mp4, "-i", bgm,
        "-filter_complex", f"[1:a]volume={BGM_VOL}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0,aresample=async=1[aout]",
        "-map","0:v","-map","[aout]",
        "-c:v","copy","-c:a","aac","-shortest", out_mp4
    ], check=True)

# ---------- Visual composition (cover using subject-focused crop; no stretching) ----------
_cascade = None
def _get_face_cascade():
    global _cascade
    if _cascade is None and cv2 is not None:
        try:
            _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:
            _cascade = None
    return _cascade

def smart_cover_crop(clip, target_w=1080, target_h=1920):
    """Scale-to-cover then crop to 1080x1920. Crop window centered on detected face if any, else center."""
    # Scale to cover (no stretching)
    s_cover = max(target_w / clip.w, target_h / clip.h)
    resized = clip.resize(s_cover)

    # Pick a frame to analyze
    try:
        t = 0.5 * max(0.0, resized.duration)
        frame = resized.get_frame(min(max(0.0, t), max(0.0, resized.duration - 0.05)))  # RGB
        if cv2 is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            cascade = _get_face_cascade()
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)) if cascade is not None else []
        else:
            faces = []
        if isinstance(faces, (list, tuple)) or getattr(faces, "shape", None) is not None:
            best = None; best_area = 0
            for (x,y,w,h) in faces:
                area = w*h
                if area > best_area:
                    best = (x,y,w,h); best_area = area
            if best:
                x,y,w,h = best
                cx = x + w/2
                cy = y + h/2
            else:
                cx, cy = resized.w/2, resized.h/2
        else:
            cx, cy = resized.w/2, resized.h/2
    except Exception:
        cx, cy = resized.w/2, resized.h/2

    # Clamp crop center so window stays within bounds
    half_w = target_w/2; half_h = target_h/2
    cx = max(half_w, min(resized.w - half_w, cx))
    cy = max(half_h, min(resized.h - half_h, cy))

    cropped = resized.crop(x_center=cx, y_center=cy, width=target_w, height=target_h)
    return cropped.set_audio(None)

def render_and_cap(broll_urls, voice_mp3, voice_duration, temp_mp4, final_mp4, target_h=1920, target_w=1080):
    """Build a 1080x1920 video, subject-focused cover crop; never exceed voice duration."""
    # Download b-roll
    tmp = Path(tempfile.mkdtemp()); local = []
    for i,u in enumerate(broll_urls):
        p = tmp / f"b{i}.mp4"
        with requests.get(u, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for ch in r.iter_content(1024*256): f.write(ch)
        local.append(str(p))

    unit_clips = []
    for p in local:
        c = VideoFileClip(p)
        take = min(8, max(4, int(c.duration)))
        c = c.subclip(0, take)
        comp = smart_cover_crop(c, target_w=target_w, target_h=target_h)
        unit_clips.append(comp)
    if not unit_clips:
        raise RuntimeError("No valid b-roll clips to render")

    # Build up to min(voice_duration, PREVIEW_MAX), with tiny epsilon safety
    epsilon = 1e-2
    target_end = max(0.2, min(PREVIEW_MAX, max(0.0, voice_duration) - epsilon))
    timeline, total, idx = [], 0.0, 0
    while total < target_end and unit_clips:
        clip = unit_clips[idx % len(unit_clips)]
        timeline.append(clip)
        total += clip.duration
        idx += 1

    merged = concatenate_videoclips(timeline, method="compose").subclip(0, target_end)

    # Attach voice audio (trimmed to 'end')
    voice = AudioFileClip(voice_mp3)
    end = min(target_end, voice.duration)
    merged = merged.subclip(0, end)
    voice_trim = voice.subclip(0, end)
    merged = merged.set_audio(voice_trim)

    # Write temp video with narration only
    merged.write_videofile(
        temp_mp4, fps=30, codec="libx264", audio_codec="aac",
        threads=2, preset="fast", verbose=False, logger=None, ffmpeg_params=["-pix_fmt","yuv420p"]
    )
    voice.close()
    for c in unit_clips:
        c.close()
    merged.close()

    # Generate perfectly synced captions from actual narration (2–3 words per chunk)
    srt_path = "cap.srt"
    ok = transcribe_to_srt_faster_whisper(voice_mp3, srt_path, lang="en")
    if not ok:
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(f"1\n{fmt_time(0.0)} --> {fmt_time(end)}\n\n")

    # Burn captions bottom-center
    subbed = "subbed.mp4"
    burn_captions(temp_mp4, srt_path, subbed)

    # Add low-volume background music under narration
    with_bgm = "with_bgm.mp4"
    add_bgm_to_video(subbed, with_bgm, end)

    # Final safety trim if needed
    vtmp = VideoFileClip(with_bgm); d = vtmp.duration; vtmp.close()
    out_path = with_bgm
    if d >= 58.0 or d > PREVIEW_MAX + 0.2:
        subprocess.run(["ffmpeg","-y","-i",with_bgm,"-t",str(PREVIEW_MAX),"-c","copy","short_trim.mp4"], check=False)
        if os.path.exists("short_trim.mp4"):
            out_path = "short_trim.mp4"
    os.replace(out_path, final_mp4)
    v = VideoFileClip(final_mp4); d2 = v.duration; v.close()
    return d2

# ---------- Ensure Google API client (lazy import with fallback) ----------
def ensure_google_client():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return Credentials, build, MediaFileUpload
    except Exception:
        pkgs = []
        try:
            import packaging  # noqa: F401
        except Exception:
            pkgs.append("packaging>=23.1")
        pkgs += ["google-api-python-client", "google-auth", "google-auth-oauthlib"]
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "-q"] + pkgs, check=True)
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return Credentials, build, MediaFileUpload

# ---------- YouTube helpers ----------
def yt_client():
    for v in ["YT_CLIENT_ID","YT_CLIENT_SECRET","YT_REFRESH_TOKEN"]:
        if not os.getenv(v):
            raise RuntimeError(f"Missing {v} secret")
    Credentials, build, _ = ensure_google_client()
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID"),
        client_secret=os.getenv("YT_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload","https://www.googleapis.com/auth/youtube"]
    )
    return build("youtube", "v3", credentials=creds)

def upload_youtube_unlisted(video_path, title, description, tags):
    _, _, MediaFileUpload = ensure_google_client()
    yt = yt_client()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t.strip() for t in (tags or ",".join(SAFE_TAGS)).split(",") if t.strip()][:10],
            "categoryId": "27",
            "defaultLanguage": "en"
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
    vid_id = resp.get("id")
    return vid_id, f"https://youtu.be/{vid_id}"

def compress_for_preview(in_path, out_path="preview_small.mp4", target_w=720, target_h=1280):
    # Scale-to-fit within 720x1280 and pad if needed (keep AR), lower bitrate for smaller size
    vf = f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
    subprocess.run([
        "ffmpeg","-y","-i",in_path,
        "-vf",vf,"-c:v","libx264","-preset","veryfast","-profile:v","high","-level","4.1",
        "-crf","30","-pix_fmt","yuv420p","-r","30",
        "-c:a","aac","-b:a","96k","-movflags","+faststart",
        out_path
    ], check=True)
    return out_path

def upload_preview_fallback(video_path):
    # Try smaller preview first to improve success on free hosts
    path_small = "preview_small.mp4"
    try:
        path_to_send = compress_for_preview(video_path, path_small)
    except Exception:
        path_to_send = video_path

    # 1) 0x0.st (simple and reliable)
    try:
        with open(path_to_send, "rb") as f:
            r = requests.post("https://0x0.st", files={"file": (os.path.basename(path_to_send), f, "video/mp4")}, timeout=120)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip(), "0x0.st"
    except Exception:
        pass

    # 2) transfer.sh
    try:
        with open(path_to_send, "rb") as f:
            r = requests.put(f"https://transfer.sh/{os.path.basename(path_to_send)}", data=f, timeout=300)
        if r.status_code in (200, 201):
            link = r.text.strip()
            if link.startswith("http"):
                return link, "transfer.sh"
    except Exception:
        pass

    # 3) file.io (expires 1 day, sometimes rate-limited)
    try:
        with open(path_to_send, "rb") as f:
            r = requests.post("https://file.io", files={"file": (os.path.basename(path_to_send), f, "video/mp4")}, data={"expires": "1d"}, timeout=300)
        if r.status_code == 200:
            j = r.json()
            if j.get("success") and j.get("link"):
                return j["link"], "file.io"
    except Exception:
        pass

    return None, None

def upload_preview_youtube_or_fallback(video_path, title, description, tags):
    # Try YouTube unlisted first
    try:
        vid_id, link = upload_youtube_unlisted(video_path, title, description, tags)
        return {"preview_video_id": vid_id, "preview_link": link, "yt_upload_blocked": False, "fallback": None}
    except Exception as e:
        msg = str(e)
        blocked = ("uploadLimitExceeded" in msg) or ("exceeded the number of videos" in msg)
        # For preview convenience, also fallback for other upload errors
        link, host = upload_preview_fallback(video_path)
        if link:
            return {"preview_video_id": None, "preview_link": link, "yt_upload_blocked": bool(blocked), "fallback": host or "fallback"}
        # Could not fallback; return explicit error info
        return {"preview_video_id": None, "preview_link": None, "yt_upload_blocked": bool(blocked), "fallback": None, "error": msg}

def upload_preview_youtube(video_path, title, description, tags):
    info = upload_preview_youtube_or_fallback(video_path, title, description, tags)
    # Do not raise here; let caller handle info/error
    return info.get("preview_video_id"), info.get("preview_link"), info

def schedule_existing_video(video_id, slot):
    yt = yt_client()
    tomorrow_ist = datetime.now(IST).date() + timedelta(days=1)
    ist_dt = datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, (16 if slot == "afternoon" else 9), 0, tzinfo=IST)
    publish_at_utc = ist_dt.astimezone(timezone.utc).isoformat()
    body = {
        "id": video_id,
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at_utc,
            "selfDeclaredMadeForKids": False
        }
    }
    yt.videos().update(part="status", body=body).execute()
    return publish_at_utc

def yt_delete_video(video_id):
    yt = yt_client()
    yt.videos().delete(id=video_id).execute()

# ---------- Metadata helpers ----------
def upload_preview_youtube_wrapper(video_path, title, description, tags):
    vid_id, link, info = upload_preview_youtube(video_path, title, description, tags)
    return vid_id, link, info

def get_metadata_from_issue_body(issue_body):
    m = re.search(r"```json\s*(\{.*?\})\s*```", issue_body, re.S)
    return json.loads(m.group(1)) if m else None

def set_metadata_in_issue_body(issue_body, meta):
    block = "```json\n" + json.dumps(meta, indent=2) + "\n```"
    if "```json" in issue_body:
        return re.sub(r"```json\s*\{.*?\}\s*```", lambda m: block, issue_body, flags=re.S)
    return issue_body + "\n\nMetadata:\n" + block

def extract_youtube_id(text):
    if not text:
        return None
    m = re.search(r"<!--\s*preview_video_id:\s*([A-Za-z0-9_-]{11})\s*-->", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtube\.com/watch\?[^ \n\r]*v=([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    return None

def find_preview_video_id(owner, repo, number, issue_body):
    try:
        meta = get_metadata_from_issue_body(issue_body) or {}
        if "preview_video_id" in meta and meta["preview_video_id"]:
            return meta["preview_video_id"]
        if "preview_link" in meta and meta["preview_link"]:
            vid = extract_youtube_id(meta["preview_link"])
            if vid:
                return vid
    except Exception:
        pass
    vid = extract_youtube_id(issue_body)
    if vid:
        return vid
    try:
        r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments", params={"per_page": 100})
        comments = r.json()
        if isinstance(comments, list):
            for c in reversed(comments):
                body = c.get("body", "") or ""
                vid = extract_youtube_id(body)
                if vid:
                    return vid
    except Exception:
        pass
    return None

def post_preview_comment(owner, repo, number, meta, slot, prefix_msg=""):
    prefix = (prefix_msg + "\n\n") if prefix_msg else ""
    blocked_note = ""
    if meta.get("yt_upload_blocked"):
        blocked_note = ("\nNote: YouTube upload limit was reached. The preview is hosted temporarily here. "
                        "Approving upload will work once the limit resets (typically within 24 hours).")
    msg = (
        f"{prefix}Preview ready (attempt {meta['attempt']}, {meta['duration_sec']}s): {meta['preview_link']}{blocked_note}\n"
        f"Reply:\n- /approve-video (schedule PRIVATE → auto-publish next day {slot})\n"
        f"- /reject-video (delete preview and pick a new topic)\n\n"
        f"<!-- preview_video_id: {meta.get('preview_video_id','')} -->"
    )
    post_comment(owner, repo, number, msg)

# ---------- Build preview and schedule ----------
def build_preview_until_under_58(topic, slot, issue_body, max_attempts=3):
    # Aim for 50–58s from the first attempt, but accept 35–58s
    word_hint = "130–160"
    for attempt in range(1, max_attempts+1):
        s = llm_script(topic, word_hint=word_hint)
        text = s["voiceover"].strip()
        # Ensure the outro phrase is spoken
        if "thank you for watching" not in text.lower():
            if not text.endswith((".", "!", "?")):
                text += "."
            text += " Thank you for watching."
        voice = "voice.mp3"; gTTS(text, lang=TTS_LANG).save(voice)
        voice, vdur = ensure_voice_in_range(voice, min_sec=PREVIEW_MIN, max_sec=PREVIEW_MAX)
        broll = fetch_broll(topic, need=6)
        if not broll:
            continue
        temp, final = "temp.mp4", "short.mp4"
        dur = render_and_cap(broll, voice, vdur, temp, final)
        if PREVIEW_MIN <= dur < 58.0:
            desc = f"""{s['description']}

Educational only, not medical advice. Consult a qualified professional for personal guidance.
#Shorts #health #wellness"""
            vid_id, link, info = upload_preview_youtube_wrapper(final, s["title"], desc, s.get("tags",""))
            if not link:
                # Could not provide a preview link; embed error and bail gracefully
                raise RuntimeError(f"Preview upload failed and fallback unavailable: {info.get('error','unknown error')}")
            meta = {
                "topic": topic,
                "title": s["title"],
                "description": desc,
                "tags": s.get("tags",""),
                "preview_video_id": vid_id,
                "preview_link": link,
                "yt_upload_blocked": bool(info.get("yt_upload_blocked")),
                "fallback_host": info.get("fallback"),
                "slot": slot,
                "created_at": datetime.utcnow().isoformat(),
                "duration_sec": round(dur,2),
                "attempt": attempt
            }
            new_body = set_metadata_in_issue_body(issue_body, meta)
            return meta, new_body
        word_hint = "145–170"
    return None, issue_body

def safe_main():
    e = ev()
    owner, repo = REPO.split("/")
    issue = e["issue"]; number = issue["number"]
    if not is_authorized_commenter(e):
        print("Ignoring comment from non-collaborator/owner")
        return
    comment = e["comment"]["body"].strip()
    body = issue["body"]
    labels = issue.get("labels", [])
    slot = get_slot_from_labels(labels)

    if comment.lower().startswith("/reject-topic") or comment.lower().startswith("/new-topic"):
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-IN", tz=330)
        seeds = ["sleep","hydration","walking","posture","stretching","mobility","stress","breathing","sunlight","protein","fiber","yoga"]
        found = []
        try:
            df = pt.realtime_trending_searches(pn="IN")
            if df is not None and "title" in df.columns:
                found += df["title"].tolist()[:10]
        except Exception:
            pass
        options = list(dict.fromkeys([s.title() for s in found + seeds]))[:3]
        try:
            meta = get_metadata_from_issue_body(body) or {}
        except Exception:
            meta = {}
        meta["topics"] = options
        new_body = set_metadata_in_issue_body(body, meta)
        new_body += "\n\nNew topic options:\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3), or provide your own with:\n/custom-topic Your Topic"
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-topic-approval")
        remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, "New topic options:\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3)\nOr provide your own topic with:\n/custom-topic Your Topic")
        return

    if comment.lower().startswith("/custom-topic"):
        topic = comment[len("/custom-topic"):].strip()
        if not topic:
            post_comment(owner, repo, number, "Please provide a topic. Example:\n/custom-topic Morning hydration habit")
            return
        meta, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=3)
        if not meta:
            post_comment(owner, repo, number, "Couldn't get to 35–58s after attempts. Try a simpler topic or /new-topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval"); remove_label(owner, repo, number, "await-topic-approval")
        post_preview_comment(owner, repo, number, meta, slot)
        return

    if comment.lower().startswith("/approve-topic"):
        topics = parse_topics_from_body(body)
        parts = comment.split(maxsplit=1)
        if len(parts) > 1 and not parts[1].strip().isdigit():
            topic = parts[1].strip()
        else:
            if not topics:
                post_comment(owner, repo, number, "Couldn't detect topics. Use /new-topic or /custom-topic Your Topic.")
                return
            idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            if idx < 1 or idx > len(topics):
                post_comment(owner, repo, number, "Invalid index. Use 1/2/3 or /custom-topic Your Topic.")
                return
            topic = topics[idx-1]
        meta, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=3)
        if not meta:
            post_comment(owner, repo, number, "Couldn't get to 35–58s after attempts. Reply /new-topic or /custom-topic Your Topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval"); remove_label(owner, repo, number, "await-topic-approval")
        post_preview_comment(owner, repo, number, meta, slot)
        return

    if comment.lower().startswith("/reject-video"):
        vid_id = find_preview_video_id(owner, repo, number, body)
        deletion_msg = ""
        if vid_id:
            try:
                yt_delete_video(vid_id); deletion_msg = f"Deleted unlisted preview video (ID: {vid_id})."
            except Exception as de:
                deletion_msg = f"Couldn't delete preview on YouTube (ID: {vid_id}): {de}"
        else:
            deletion_msg = "No preview video ID found (looked in metadata, body, recent comments)."
        try:
            meta = get_metadata_from_issue_body(body) or {}
            for k in ["preview_video_id","preview_link","title","description","tags","attempt","duration_sec","topic","yt_upload_blocked","fallback_host"]:
                if k in meta: del meta[k]
            new_body = set_metadata_in_issue_body(body, meta)
            gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        except Exception as e2:
            deletion_msg += f"\nMetadata cleanup error: {e2}"
        add_label(owner, repo, number, "await-topic-approval"); remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, f"{deletion_msg}\nOK. Reply /new-topic for fresh options or /custom-topic Your Topic.")
        return

    if comment.lower().startswith("/regenerate-video"):
        # Delete previously unlisted (not yet approved) preview first
        prev_vid = None
        meta_old = get_metadata_from_issue_body(body) or {}
        topic = meta_old["topic"] if "topic" in meta_old else None
        if not topic:
            post_comment(owner, repo, number, "No topic to regenerate. Use /approve-topic or /custom-topic first.")
            return
        if "preview_video_id" in meta_old and meta_old["preview_video_id"]:
            prev_vid = meta_old["preview_video_id"]
        if not prev_vid:
            prev_vid = find_preview_video_id(owner, repo, number, body)
        deleted_msg = ""
        if prev_vid:
            try:
                yt_delete_video(prev_vid)
                deleted_msg = f"Deleted previous unlisted preview (ID: {prev_vid})."
            except Exception as de:
                deleted_msg = f"Couldn't delete previous preview on YouTube (ID: {prev_vid}): {de}"
        # Clear preview fields in metadata before regenerating
        try:
            for k in ["preview_video_id","preview_link","title","description","tags","attempt","duration_sec","yt_upload_blocked","fallback_host"]:
                if k in meta_old: del meta_old[k]
            new_body_clear = set_metadata_in_issue_body(body, meta_old)
            gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body_clear})
            body = new_body_clear
        except Exception:
            pass

        meta2, new_body2 = build_preview_until_under_58(topic, slot, body, max_attempts=3)
        if not meta2:
            post_comment(owner, repo, number, f"{deleted_msg}\nCouldn't get to 35–58s after attempts. Use /new-topic or /custom-topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body2})
        add_label(owner, repo, number, "await-video-approval")
        post_preview_comment(owner, repo, number, meta2, slot, prefix_msg=deleted_msg or "")
        return

    if comment.lower().startswith("/approve-video"):
        meta = get_metadata_from_issue_body(body) or {}
        if meta.get("yt_upload_blocked"):
            post_comment(owner, repo, number, "YouTube upload limit was reached when generating this preview. Please try /regenerate-video after the limit resets (typically within 24 hours).")
            return
        vid_id = None
        if meta and "preview_video_id" in meta:
            vid_id = meta["preview_video_id"]
        if not vid_id:
            vid_id = find_preview_video_id(owner, repo, number, body)
        if not vid_id:
            post_comment(owner, repo, number, "No preview video found on YouTube. Please /regenerate-video.")
            return
        publish_at_utc = schedule_existing_video(vid_id, (meta or {}).get("slot","morning"))
        ist_time = datetime.fromisoformat(publish_at_utc.replace("Z","")).astimezone(IST).strftime("%Y-%m-%d %H:%M")
        link = f"https://youtu.be/{vid_id}"
        post_comment(owner, repo, number, f"Scheduled ✅ {link}\nPublishes at (IST): {ist_time}\nClosing this thread.")
        remove_label(owner, repo, number, "await-video-approval")
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"state":"closed"})
        return

def main():
    try:
        safe_main()
    except Exception as e:
        try:
            e_json = ev()
            owner, repo = REPO.split("/")
            issue = e_json["issue"]; number = issue["number"]
            avail = list_groq_models()
            addendum = "\n\nIf YouTube upload limit is reached, I now auto-host the preview temporarily (0x0.st/transfer.sh/file.io)."
            if avail:
                addendum += "\nAvailable Groq models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL / GROQ_FALLBACK_MODELS to one of the above."
            msg = f"❌ Error: {e}{addendum}\n```\n{traceback.format_exc()}\n```"
            post_comment(owner, repo, number, msg)
        except Exception:
            print("FATAL:", e)
            print(traceback.format_exc())
        return

if __name__ == "__main__":
    main()

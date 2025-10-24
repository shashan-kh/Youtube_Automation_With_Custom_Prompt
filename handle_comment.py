import os, re, json, tempfile, subprocess, requests, traceback, sys, math, random, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from moviepy.editor import VideoFileClip, concatenate_videoclips, AudioFileClip
from gtts import gTTS

# Optional: gTTS can raise its own error class
try:
    from gtts.tts import gTTSError
except Exception:
    gTTSError = Exception

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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

SAFE_TAGS = ["health","wellness","habits","selfcare","sleep","hydration","movement","posture","stress","nutrition"]
# Duration limits (Shorts-compliant)
PREVIEW_MIN = 50.0
PREVIEW_MAX = 57.3
TARGET_DUR = 56.0
IST = timezone(timedelta(hours=5, minutes=30))

# Speech rate (words per second) estimate to size script before TTS
# Typical English narration ~2.4–3.0 WPS. We use 2.6 as a conservative default.
WPS_EST = float(os.getenv("SCRIPT_WPS", "2.6"))

def is_authorized_commenter(event):
    assoc = (event.get("comment", {}).get("author_association") or "").upper()
    commenter = event.get("comment", {}).get("user", {}).get("login", "")
    repo_owner = event.get("repository", {}).get("owner", {}).get("login", "")
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
        if not idxs: return []
        start = idxs[-1] + 1
        out = []
        for ln in lines[start:]:
            s = ln.strip()
            if not s: break
            if "reply with" in s.lower(): break
            m = re.match(r"^\s*(?:[-*•]\s*)?(?:\d+[\)\.\-:])\s+(.*\S)", s)
            if not m:
                if re.match(r"^[A-Z].*:$", s) or s.startswith("/"): break
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
        if m: fallback.append(m.group(1).strip())
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

def llm_script(trending_query, word_hint="135–150"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret")
    prompt = f"""
You are a careful health educator. Create a strictly 50–58s YouTube Short based on this wellness topic:
"{trending_query}"

Rules:
- General wellness only (sleep, hydration, movement, posture, stress, basic nutrition).
- No disease claims, diagnoses, dosages, or supplement promises. Avoid COVID/vaccines and specific diseases.
- If the topic is unsafe/specific, pivot to a safe, related habit.
- Style: energetic, plain language, second-person; target {word_hint} words for ~50–58s spoken length.

Output pure JSON with:
- voiceover: string (the exact narration; no stage directions)
- overlay_lines: array (optional; can be empty)
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

# ---------- Duration and TTS helpers (minimize TTS usage) ----------
def words_in(text):
    return len(re.findall(r"[A-Za-z']+", text or ""))

def estimate_seconds_from_text(text, wps=WPS_EST):
    return words_in(text) / max(0.5, float(wps))

def compute_word_target_for_window(min_s=PREVIEW_MIN, max_s=PREVIEW_MAX, wps=WPS_EST):
    low = int(math.ceil(min_s * wps))
    high = int(math.floor(max_s * wps))
    # keep a little buffer
    return max(100, low), min(200, high + 4)

def script_for_target_duration(topic, max_rounds=6, wps=WPS_EST):
    # Size script BEFORE TTS: loop LLM until estimated duration is in [50,58]
    word_low, word_high = compute_word_target_for_window(PREVIEW_MIN, PREVIEW_MAX, wps)
    hint = f"{word_low}–{word_high}"
    last = None
    for _ in range(max_rounds):
        s = llm_script(topic, word_hint=hint)
        v = (s.get("voiceover") or "").strip()
        if not v:
            continue
        est = estimate_seconds_from_text(v, wps)
        if PREVIEW_MIN <= est <= PREVIEW_MAX:
            return s  # Good: estimated duration fits window
        # Adjust hint for next round
        if est < PREVIEW_MIN:
            # too short -> ask for more words
            need = int(math.ceil((PREVIEW_MIN - est) * wps * 1.05))
            target = words_in(v) + max(20, need)
        else:
            # too long -> ask for fewer words
            cut = int(math.ceil((est - PREVIEW_MAX) * wps * 1.05))
            target = max(100, words_in(v) - max(20, cut))
        hint = f"{max(90, target-10)}–{min(190, target+10)}"
        last = s
    # Fallback: return best-attempt script
    return last

def audio_duration(path):
    a = AudioFileClip(path); d = a.duration; a.close(); return d

def tts_save_once(text, out_path="voice.mp3"):
    # Single TTS call with light retry if transient; avoids 429 storms
    try:
        gTTS(text, lang=TTS_LANG).save(out_path)
        return out_path, audio_duration(out_path)
    except gTTSError as e:
        # gTTS specific error, may include 429
        raise RuntimeError(f"TTS error: {e}")
    except Exception as e:
        msg = str(e)
        if "429" in msg or "Too Many Requests" in msg:
            raise RuntimeError("TTS rate limited (429). Script prepared first to minimize TTS calls. Please retry in ~1–2 minutes.")
        raise

def maybe_speed_up_audio(voice_path, dur, target=TARGET_DUR, max_s=PREVIEW_MAX, max_factor=1.15):
    if dur <= max_s:
        return voice_path, dur
    factor = max(1.02, min(max_factor, dur / max(target, max_s - 0.3)))
    out = "voice_fast.mp3"
    subprocess.run(["ffmpeg","-y","-i",voice_path,"-filter:a",f"atempo={factor}","-vn",out], check=True)
    d2 = audio_duration(out)
    return out, d2

# ---------- Smart B-roll based on narration ----------
KEYWORD_QUERIES = [
    (["hydrate","hydration","water","sip","bottle","glass"], ["drinking water", "pouring water into glass", "water bottle"]),
    (["sleep","bed","night","screen","blue light","caffeine","bedtime","wind-down"], ["sleeping at night", "bedtime routine", "turning off screens"]),
    (["walk","walking","steps","move","stroll","breaks"], ["walking in park", "city walking", "walking outdoors"]),
    (["posture","desk","spine","shoulder","ergonomic","sit","sitting"], ["desk posture", "working at desk", "standing desk"]),
    (["stretch","stretches","mobility","flexibility","hamstring","hip"], ["stretching at home", "morning stretches", "yoga stretching"]),
    (["breath","breathe","breathing","inhale","exhale","diaphragm"], ["deep breathing", "breathwork", "meditation breathing"]),
    (["sun","sunlight","daylight","morning light","outdoor"], ["morning sunlight", "walking in sunlight"]),
    (["protein","egg","eggs","beans","dal","chicken","paneer","lentil"], ["healthy protein foods", "meal prep protein"]),
    (["fiber","veggies","vegetables","fruit","fruits","oats","whole grain"], ["fresh vegetables", "fruit bowl", "salad making"]),
    (["stress","calm","mindful","mindfulness","meditate","meditation"], ["meditation calm", "relaxing yoga"]),
    (["stairs","steps","climb"], ["climbing stairs", "walking stairs"]),
]
GENERIC_FALLBACKS = ["healthy lifestyle", "fitness", "stretching", "walking", "hydration", "posture", "yoga", "nutrition"]

def derive_queries_from_voice(voice_text, segments=8):
    text = re.sub(r"[\r\n]+", " ", voice_text or "").lower()
    words = [w for w in re.findall(r"[A-Za-z']+", text) if w]
    if not words:
        return [random.choice(GENERIC_FALLBACKS)] * 6, [""]*6
    segs = max(6, min(9, segments))
    chunk = math.ceil(len(words) / segs)
    seg_texts = []
    for i in range(segs):
        part = " ".join(words[i*chunk:(i+1)*chunk]).strip()
        if part:
            seg_texts.append(part)
    if not seg_texts:
        seg_texts = [" ".join(words)]
    queries = []
    for seg in seg_texts:
        q = None
        for keys, opts in KEYWORD_QUERIES:
            if any(k in seg for k in keys):
                q = random.choice(opts); break
        if not q:
            q = random.choice(GENERIC_FALLBACKS)
        queries.append(q)
    return queries, seg_texts

def fetch_broll_for_queries(queries):
    if not PEXELS_API_KEY:
        raise RuntimeError("Missing PEXELS_API_KEY secret")
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    urls = []
    for q in queries:
        try:
            r = requests.get(url, headers=headers, params={"query": q, "per_page": 25}, timeout=60)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Pexels API auth error {r.status_code}: {r.text}")
            candidates = []
            for v in r.json().get("videos", []):
                files = sorted(v.get("video_files", []), key=lambda f: (f.get("height",0), f.get("width",0)), reverse=True)
                for f in files:
                    h, w = f.get("height",0), f.get("width",0)
                    if h >= 1080 and w <= h:
                        candidates.append((h, w, f.get("link"))); break
            random.shuffle(candidates)
            if candidates:
                urls.append(candidates[0][2])
        except Exception:
            continue
    if len(urls) < 6:
        for g in GENERIC_FALLBACKS:
            if len(urls) >= 6: break
            try:
                r = requests.get(url, headers=headers, params={"query": g, "per_page": 20}, timeout=60)
                for v in r.json().get("videos", []):
                    files = sorted(v.get("video_files", []), key=lambda f: f.get("height",0), reverse=True)
                    for f in files:
                        h, w = f.get("height",0), f.get("width",0)
                        if h >= 1080 and w <= h:
                            urls.append(f["link"]); break
                    if len(urls) >= 6: break
            except Exception:
                continue
    return urls[:max(6, min(9, len(queries)))]

def fmt_time(t):
    h = int(t // 3600); m = int((t % 3600)//60); s = int(t % 60); ms = int((t - int(t))*1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def make_srt_from_transcript(transcript, duration, path):
    text = re.sub(r"\s+", " ", (transcript or "").strip())
    words = text.split()
    if not words:
        words = ["Simple", "wellness", "tip", "Move", "hydrate", "rest"]
    n_lines = max(6, min(9, math.ceil(len(words) / 14)))  # ~4–7 words per line
    chunk = max(4, min(7, math.ceil(len(words)/n_lines)))
    lines = []
    i = 0
    while i < len(words):
        lines.append(" ".join(words[i:i+chunk]))
        i += chunk
    lines = lines[:n_lines]
    step = max(1.0, duration / len(lines))
    t = 0.4
    out = []
    for idx, line in enumerate(lines, start=1):
        start = min(t, duration - 0.2)
        end = min(start + step, duration)
        out.append(f"{idx}\n{fmt_time(start)} --> {fmt_time(end)}\n{line}\n\n")
        t = end - 0.05
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)

def burn_subs(in_mp4, srt_path, out_mp4):
    # Bottom, small, stroked, bright
    vf = f"subtitles={srt_path}:force_style='Fontname=DejaVu Sans,Fontsize=34,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Outline=3,Shadow=1,Alignment=2,MarginV=90'"
    subprocess.run(["ffmpeg","-y","-i",in_mp4,"-vf",vf,"-c:a","copy",out_mp4], check=True)

def render_segmented(broll_urls, segments_texts, voice_mp3, temp_mp4, final_mp4, target_h=1920, target_w=1080):
    tmp = Path(tempfile.mkdtemp()); local = []
    for i,u in enumerate(broll_urls):
        p = tmp / f"b{i}.mp4"
        with requests.get(u, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for ch in r.iter_content(1024*256): f.write(ch)
        local.append(str(p))
    voice = AudioFileClip(voice_mp3)
    vdur = voice.duration
    nseg = max(6, min(9, len(segments_texts)))
    per = max(4.5, min(9.0, vdur / nseg))
    clips = []
    for i in range(nseg):
        src = local[i % len(local)]
        c = VideoFileClip(src)
        take = min(per, max(4.0, min(10.0, c.duration)))
        sub = c.subclip(0, take).resize(height=target_h)
        if sub.w != target_w:
            sub = sub.crop(x_center=sub.w/2, width=target_w, height=target_h)
        clips.append(sub)
    merged = concatenate_videoclips(clips, method="compose")
    end = min(merged.duration, vdur + 0.25, PREVIEW_MAX)
    merged = merged.subclip(0, end).set_audio(voice)
    merged.write_videofile(temp_mp4, fps=30, codec="libx264", audio_codec="aac", threads=2, preset="fast", verbose=False, logger=None)
    voice.close(); [c.close() for c in clips]; merged.close()
    srt_path = "cap.srt"
    make_srt_from_transcript(" ".join(segments_texts), end, srt_path)
    burn_subs(temp_mp4, srt_path, final_mp4)
    v = VideoFileClip(final_mp4); d = v.duration; v.close()
    if d > PREVIEW_MAX + 0.05:
        subprocess.run(["ffmpeg","-y","-i",final_mp4,"-t",str(PREVIEW_MAX),"-c","copy","short_trim.mp4"], check=False)
        if os.path.exists("short_trim.mp4"):
            os.replace("short_trim.mp4", final_mp4)
            v2 = VideoFileClip(final_mp4); d2 = v2.duration; v2.close()
            d = d2
    return d

# ---------- YouTube helpers ----------
def yt_client():
    for v in ["YT_CLIENT_ID","YT_CLIENT_SECRET","YT_REFRESH_TOKEN"]:
        if not os.getenv(v):
            raise RuntimeError(f"Missing {v} secret")
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
def upload_preview_youtube(video_path, title, description, tags):
    vid_id, link = upload_youtube_unlisted(video_path, title, description, tags)
    return vid_id, link

def get_metadata_from_issue_body(issue_body):
    m = re.search(r"```json\s*(\{.*?\})\s*```", issue_body, re.S)
    return json.loads(m.group(1)) if m else None

def set_metadata_in_issue_body(issue_body, meta):
    block = "```json\n" + json.dumps(meta, indent=2) + "\n```"
    if "```json" in issue_body:
        # Avoid interpreting backslashes (e.g. \u) in replacement
        return re.sub(r"```json\s*\{.*?\}\s*```", lambda m: block, issue_body, flags=re.S)
    return issue_body + "\n\nMetadata:\n" + block

def extract_youtube_id(text):
    if not text: return None
    m = re.search(r"<!--\s*preview_video_id:\s*([A-Za-z0-9_-]{11})\s*-->", text)
    if m: return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})", text)
    if m: return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtube\.com/watch\?[^ \n\r]*v=([A-Za-z0-9_-]{11})", text)
    if m: return m.group(1)
    m = re.search(r"(?:https?://)?(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})", text)
    if m: return m.group(1)
    return None

def find_preview_video_id(owner, repo, number, issue_body):
    try:
        meta = get_metadata_from_issue_body(issue_body) or {}
        if "preview_video_id" in meta and meta["preview_video_id"]:
            return meta["preview_video_id"]
        if "preview_link" in meta and meta["preview_link"]:
            vid = extract_youtube_id(meta["preview_link"])
            if vid: return vid
    except Exception:
        pass
    vid = extract_youtube_id(issue_body)
    if vid: return vid
    try:
        r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments", params={"per_page": 100})
        comments = r.json()
        if isinstance(comments, list):
            for c in reversed(comments):
                body = c.get("body", "") or ""
                vid = extract_youtube_id(body)
                if vid: return vid
    except Exception:
        pass
    return None

def post_preview_comment(owner, repo, number, meta, slot):
    msg = (
        f"Preview ready (attempt {meta['attempt']}, {meta['duration_sec']}s): {meta['preview_link']}\n"
        f"Reply:\n- /approve-video (schedule PRIVATE → auto-publish next day {slot})\n"
        f"- /reject-video (delete preview and pick a new topic)\n\n"
        f"<!-- preview_video_id: {meta['preview_video_id']} -->"
    )
    post_comment(owner, repo, number, msg)

# ---------- Used topics history (exclude only topics previously approved for upload) ----------
def norm_topic(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def list_used_topics_uploaded_only(owner, repo, max_pages=5):
    used = set()
    page = 1
    while page <= max_pages:
        r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues",
               params={"state": "all", "per_page": 100, "page": page})
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        for it in items:
            if "pull_request" in it:
                continue
            body = it.get("body") or ""
            try:
                meta = get_metadata_from_issue_body(body) or {}
            except Exception:
                meta = {}
            if meta.get("upload_approved") is True:
                t = meta.get("topic")
                if isinstance(t, str) and t.strip():
                    used.add(norm_topic(t))
        if len(items) < 100:
            break
        page += 1
    return used

# ---------- Build preview with "script-first, TTS-last" strategy ----------
def build_preview_until_under_58(topic, slot, issue_body, max_attempts=4):
    """
    1) Generate/adjust script until estimated duration (WPS-based) fits 50–58s.
    2) Call TTS once (at most twice if small speed-up needed).
    3) Build video and upload preview if final 50–58s achieved.
    """
    # Step 1: script-sized by estimation
    script = script_for_target_duration(topic, max_rounds=max_attempts, wps=WPS_EST)
    if not script or not (script.get("voiceover") or "").strip():
        return None, issue_body
    voice_text = script["voiceover"].strip()

    # Step 2: single TTS (minimize API usage to avoid 429)
    try:
        voice_mp3, dur = tts_save_once(voice_text, out_path="voice.mp3")
    except Exception as e:
        # Bubble up; caller will post a friendly comment already
        raise

    # If slightly over limit, we may speed up gently once (never slow)
    sped_once = False
    if dur > PREVIEW_MAX:
        voice_mp3, dur2 = maybe_speed_up_audio(voice_mp3, dur, target=TARGET_DUR, max_s=PREVIEW_MAX, max_factor=1.15)
        sped_once = True
        dur = dur2

    # If still out of range, do one more script refinement pass then one more TTS
    if not (PREVIEW_MIN <= dur <= PREVIEW_MAX):
        adj_script = script_for_target_duration(topic, max_rounds=2, wps=WPS_EST)
        if adj_script and (adj_script.get("voiceover") or "").strip():
            voice_text = adj_script["voiceover"].strip()
            # second and final TTS attempt (avoid 429)
            voice_mp3, dur = tts_save_once(voice_text, out_path="voice.mp3")
            if dur > PREVIEW_MAX and not sped_once:
                voice_mp3, dur2 = maybe_speed_up_audio(voice_mp3, dur, target=TARGET_DUR, max_s=PREVIEW_MAX, max_factor=1.12)
                dur = dur2
            # Replace script with adjusted for rest of pipeline
            script = adj_script

    # Enforce duration window
    if not (PREVIEW_MIN <= dur <= PREVIEW_MAX):
        return None, issue_body

    # Step 3: visuals and captions exactly from narration
    queries, seg_texts = derive_queries_from_voice(voice_text, segments=8)
    broll = fetch_broll_for_queries(queries)
    if len(broll) < 3:
        return None, issue_body

    temp, final = "temp.mp4", "short.mp4"
    dur_final = render_segmented(broll, seg_texts, voice_mp3, temp, final)
    if not (PREVIEW_MIN <= dur_final <= 58.0):
        return None, issue_body

    desc = f"""{script['description']}

Educational only, not medical advice. Consult a qualified professional for personal guidance.
#Shorts #health #wellness"""
    vid_id, link = upload_preview_youtube(final, script["title"], desc, script.get("tags",""))
    meta = {
        "topic": topic,
        "title": script["title"],
        "description": desc,
        "tags": script.get("tags",""),
        "preview_video_id": vid_id,
        "preview_link": link,
        "slot": slot,
        "created_at": datetime.utcnow().isoformat(),
        "duration_sec": round(dur_final,2),
        "attempt": 1
    }
    new_body = set_metadata_in_issue_body(issue_body, meta)
    return meta, new_body

# ---------- Main handler ----------
def safe_main():
    e = ev()
    owner, repo = REPO.split("/")
    issue = e["issue"]; number = issue["number"]
    if not is_authorized_commenter(e):
        print("Ignoring comment from non-collaborator/owner"); return

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
        used = list_used_topics_uploaded_only(owner, repo)
        cleaned, seen_norm = [], set()
        for s_ in (found + seeds):
            t = str(s_).strip()
            if not t: continue
            n = norm_topic(t)
            if n in used or n in seen_norm: continue
            seen_norm.add(n); cleaned.append(t.title())
        fallbacks = [
            "Desk posture routine","Breath to reduce stress","Get morning sunlight",
            "Protein with every meal","High-fiber snack ideas","Simple mobility flow",
            "Screen-time wind-down","Take more walking breaks","Wind-down sleep ritual"
        ]
        for f in fallbacks:
            if len(cleaned) >= 3: break
            if norm_topic(f) not in used and norm_topic(f) not in seen_norm:
                cleaned.append(f); seen_norm.add(norm_topic(f))
        options = cleaned[:3] if cleaned else ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine"]

        try:
            meta = get_metadata_from_issue_body(body) or {}
        except Exception:
            meta = {}
        meta["topics"] = options
        new_body = set_metadata_in_issue_body(body, meta)
        new_body += "\n\nNew topic options (excluding topics previously approved for upload):\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3), or provide your own with:\n/custom-topic Your Topic"
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-topic-approval")
        remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, "New topic options (excluding topics previously approved for upload):\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3)\nOr provide your own topic with:\n/custom-topic Your Topic")
        return

    if comment.lower().startswith("/custom-topic"):
        topic = comment[len("/custom-topic"):].strip()
        if not topic:
            post_comment(owner, repo, number, "Please provide a topic. Example:\n/custom-topic Morning hydration habit")
            return
        meta, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=6)
        if not meta:
            post_comment(owner, repo, number, "Couldn't meet 50–58s window after attempts (script-first sizing). Try /new-topic or a simpler /custom-topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval"); remove_label(owner, repo, number, "await-topic-approval")
        post_preview_comment(owner, repo, number, meta, slot)
        return

    if comment.lower().startswith("/approve-topic"):
        topics = parse_topics_from_body(body)
        parts = comment.split(maxsplit=1)
        topic = None
        if len(parts) > 1:
            arg = parts[1].strip()
            if arg.isdigit():
                idx = int(arg)
                if not topics or not (1 <= idx <= len(topics)):
                    post_comment(owner, repo, number, "Invalid index. Use 1/2/3, or provide your own with:\n/custom-topic Your Topic")
                    return
                topic = topics[idx-1]
            else:
                topic = arg
        else:
            if not topics:
                post_comment(owner, repo, number, "Couldn't detect topics in the Issue. Use /new-topic or /custom-topic Your Topic.")
                return
            topic = topics[0]
        meta, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=6)
        if not meta:
            post_comment(owner, repo, number, "Couldn't meet 50–58s window after attempts (script-first sizing). Reply /new-topic or /custom-topic Your Topic.")
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
                yt_delete_video(vid_id)
                deletion_msg = f"Deleted unlisted preview video (ID: {vid_id})."
            except Exception as de:
                deletion_msg = f"Couldn't delete preview on YouTube (ID: {vid_id}): {de}"
        else:
            deletion_msg = "No preview video ID found (looked in metadata, body, and recent comments)."
        try:
            meta = get_metadata_from_issue_body(body) or {}
            for k in ["preview_video_id","preview_link","title","description","tags","attempt","duration_sec","topic"]:
                if k in meta:
                    del meta[k]
            new_body = set_metadata_in_issue_body(body, meta)
            gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        except Exception as e2:
            deletion_msg += f"\nMetadata cleanup error: {e2}"
        add_label(owner, repo, number, "await-topic-approval")
        remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, f"{deletion_msg}\nOK. Reply /new-topic for fresh options or /custom-topic Your Topic.")
        return

    if comment.lower().startswith("/regenerate-video"):
        meta = get_metadata_from_issue_body(body)
        topic = meta["topic"] if meta and "topic" in meta else None
        if not topic:
            post_comment(owner, repo, number, "No topic to regenerate. Use /approve-topic or /custom-topic first.")
            return
        meta2, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=6)
        if not meta2:
            post_comment(owner, repo, number, "Couldn't meet 50–58s window after attempts. Use /new-topic or /custom-topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval")
        post_preview_comment(owner, repo, number, meta2, slot)
        return

    if comment.lower().startswith("/approve-video"):
        vid_id = None
        meta = get_metadata_from_issue_body(body)
        if meta and "preview_video_id" in meta:
            vid_id = meta["preview_video_id"]
        if not vid_id:
            vid_id = find_preview_video_id(owner, repo, number, body)
        if not vid_id:
            post_comment(owner, repo, number, "No preview video found. Please /regenerate-video.")
            return
        publish_at_utc = schedule_existing_video(vid_id, (meta or {}).get("slot","morning"))
        # Mark this topic as upload-approved for future filtering
        try:
            latest_issue = get_issue(owner, repo, number)
            latest_body = latest_issue.get("body") or body
            meta2 = get_metadata_from_issue_body(latest_body) or {}
            meta2["upload_approved"] = True
            meta2["publish_at_utc"] = publish_at_utc
            new_body2 = set_metadata_in_issue_body(latest_body, meta2)
            gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body2})
        except Exception:
            pass
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
            addendum = ""
            if avail:
                addendum = "\n\nAvailable models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL / GROQ_FALLBACK_MODELS in the workflow env to one of the above."
            msg = f"❌ Error: {e}{addendum}\n```\n{traceback.format_exc()}\n```"
            post_comment(owner, repo, number, msg)
        except Exception:
            print("FATAL:", e)
            print(traceback.format_exc())
        return

if __name__ == "__main__":
    main()

import os, re, json, tempfile, subprocess, requests, traceback, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from moviepy.editor import VideoFileClip, concatenate_videoclips, AudioFileClip
from gtts import gTTS
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
PREVIEW_MAX = 57.3
IST = timezone(timedelta(hours=5, minutes=30))

def is_authorized_commenter(event):
    """
    Allow OWNER, MEMBER, COLLABORATOR or the repo owner login.
    This makes commands work in org repos and for collaborators.
    """
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
    """
    Prefer the latest numbered list under 'New topic options:' or 'Choose one:'.
    Fallback to Metadata JSON topics; final fallback to any numbered lines.
    """
    lines = body.splitlines()

    def collect_after_marker(marker):
        idxs = [i for i, l in enumerate(lines) if marker in l.lower()]
        if not idxs:
            return []
        start = idxs[-1] + 1  # last occurrence
        out = []
        for ln in lines[start:]:
            s = ln.strip()
            if not s:
                break
            if "reply with" in s.lower():
                break
            m = re.match(r"^\s*(?:[-*•]\s*)?(?:\d+[\)\.\-:])\s+(.*\S)", s)
            if not m:
                if re.match(r"^[A-Z].*:$", s):
                    break
                if s.startswith("/"):
                    break
                continue
            out.append(m.group(1).strip())
        return out[:3]

    # 1) Latest 'New topic options:'
    t = collect_after_marker("new topic options:")
    if t:
        return t
    # 2) Latest 'Choose one:'
    t = collect_after_marker("choose one:")
    if t:
        return t
    # 3) Metadata JSON topics
    try:
        block = extract_json_block(body)
        if block:
            data = json.loads(block)
            if isinstance(data, dict) and isinstance(data.get("topics"), list) and data["topics"]:
                return [str(x).strip() for x in data["topics"] if str(x).strip()][:3]
    except Exception:
        pass
    # 4) Any numbered lines (fallback)
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
    # De-dup
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

def llm_script(trending_query, word_hint="90–105"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret")
    prompt = f"""
You are a careful health educator. Create a strictly under-58s YouTube Short based on this trending query:
"{trending_query}"

Rules:
- General wellness only (sleep, hydration, movement, posture, stress, basic nutrition).
- No disease claims, diagnoses, dosages, or supplement promises. Avoid COVID/vaccines.
- If the query is unsafe/specific (e.g., drugs/diseases), pivot to a safe, related habit.
- Style: energetic, plain language, second-person; target {word_hint} words.

Output pure JSON with:
- voiceover: string
- overlay_lines: array of 7–9 short lines (4–7 words each) for captions
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

def ensure_voice_under_target(voice_path, target=PREVIEW_MAX):
    a = AudioFileClip(voice_path); dur = a.duration; a.close()
    if dur <= target:
        return voice_path, dur
    factor = max(0.5, min(2.0, target / dur))
    out = "voice_fast.mp3"
    subprocess.run(["ffmpeg","-y","-i",voice_path,"-filter:a",f"atempo={factor}","-vn",out], check=True)
    a2 = AudioFileClip(out); d2 = a2.duration; a2.close()
    return out, d2

def fetch_broll(query, need=4):
    if not PEXELS_API_KEY:
        raise RuntimeError("Missing PEXELS_API_KEY secret")
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    vids = []
    for q in [query, "healthy lifestyle", "fitness", "sleep", "hydration", "walking", "stretching", "posture", "nutrition", "yoga"]:
        try:
            r = requests.get(url, headers=headers, params={"query": q, "per_page": 20, "orientation": "portrait"}, timeout=60)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Pexels API auth error {r.status_code}: {r.text}")
            for v in r.json().get("videos", []):
                files = sorted(v.get("video_files", []), key=lambda f: f.get("height",0), reverse=True)
                for f in files:
                    if f.get("height",0) >= 1080 and f.get("width",0) <= f.get("height",0):
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

def make_srt(lines, duration, path):
    lines = [l.strip() for l in (lines or []) if str(l).strip()]
    if not lines:
        lines = ["Simple wellness tip", "Small steps add up", "Move, hydrate, rest", "You’ve got this!"]
    n = max(1, min(9, len(lines)))
    step = max(1.2, duration / n)
    t = 0.5
    out = []
    for i in range(n):
        start = min(t, duration-0.2)
        end = min(start + step, duration)
        out.append(f"{i+1}\n{fmt_time(start)} --> {fmt_time(end)}\n{lines[i]}\n\n")
        t = end - 0.1
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)

def burn_subs(in_mp4, srt_path, out_mp4):
    vf = f"subtitles={srt_path}:force_style='Fontname=DejaVu Sans,Fontsize=44,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Outline=2,Shadow=1,Alignment=8,MarginV=80'"
    subprocess.run(["ffmpeg","-y","-i",in_mp4,"-vf",vf,"-c:a","copy",out_mp4], check=True)

def render_and_cap(broll_urls, voice_mp3, temp_mp4, final_mp4, overlay_lines, target_h=1920, target_w=1080):
    tmp = Path(tempfile.mkdtemp()); local = []
    for i,u in enumerate(broll_urls):
        p = tmp / f"b{i}.mp4"
        with requests.get(u, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for ch in r.iter_content(1024*256): f.write(ch)
        local.append(str(p))
    clips = []
    for p in local:
        c = VideoFileClip(p)
        take = min(8, max(4, int(c.duration)))
        c = c.subclip(0, take).resize(height=target_h)  # pillow<10 via requirements fixes ANTIALIAS removal
        if c.w != target_w:
            c = c.crop(x_center=c.w/2, width=target_w, height=target_h)
        clips.append(c)
    merged = concatenate_videoclips(clips)
    voice = AudioFileClip(voice_mp3)
    end = min(merged.duration, voice.duration + 0.3, PREVIEW_MAX)
    merged = merged.subclip(0, end).set_audio(voice)
    merged.write_videofile(temp_mp4, fps=30, codec="libx264", audio_codec="aac", threads=2, preset="fast", verbose=False, logger=None)
    voice.close(); [c.close() for c in clips]; merged.close()
    srt_path = "cap.srt"
    make_srt(overlay_lines, end, srt_path)
    burn_subs(temp_mp4, srt_path, final_mp4)
    v = VideoFileClip(final_mp4); d = v.duration; v.close()
    if d >= 58.0 or d > PREVIEW_MAX + 0.2:
        subprocess.run(["ffmpeg","-y","-i",final_mp4,"-t",str(PREVIEW_MAX),"-c","copy","short_trim.mp4"], check=False)
        if os.path.exists("short_trim.mp4"):
            os.replace("short_trim.mp4", final_mp4)
            v2 = VideoFileClip(final_mp4); d2 = v2.duration; v2.close()
            return d2
    return d

# ---------- YouTube helpers (preview as UNLISTED; schedule by updating the same video) ----------
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

# ---------- Build preview and schedule ----------
def upload_preview_youtube(video_path, title, description, tags):
    vid_id, link = upload_youtube_unlisted(video_path, title, description, tags)
    return vid_id, link

def get_metadata_from_issue_body(issue_body):
    m = re.search(r"```json\s*(\{.*?\})\s*```", issue_body, re.S)
    return json.loads(m.group(1)) if m else None

def set_metadata_in_issue_body(issue_body, meta):
    block = "```json\n" + json.dumps(meta, indent=2) + "\n```"
    if "```json" in issue_body:
        return re.sub(r"```json\s*\{.*?\}\s*```", block, issue_body, flags=re.S)
    return issue_body + "\n\nMetadata:\n" + block

def build_preview_until_under_58(topic, slot, issue_body, max_attempts=5):
    word_targets = ["90–105","75–90","60–75","55–65","50–60"]
    for attempt in range(1, max_attempts+1):
        s = llm_script(topic, word_hint=word_targets[min(attempt-1, len(word_targets)-1)])
        voice = "voice.mp3"; gTTS(s["voiceover"], lang=TTS_LANG).save(voice)
        voice, _ = ensure_voice_under_target(voice, target=PREVIEW_MAX)
        broll = fetch_broll(topic, need=4)
        if not broll:
            continue
        temp, final = "temp.mp4", "short.mp4"
        dur = render_and_cap(broll, voice, temp, final, s.get("overlay_lines", []))
        if dur < 58.0:
            desc = f"""{s['description']}

Educational only, not medical advice. Consult a qualified professional for personal guidance.
#Shorts #health #wellness"""
            # Upload preview to YouTube as UNLISTED
            vid_id, link = upload_preview_youtube(final, s["title"], desc, s.get("tags",""))
            meta = {
                "topic": topic,
                "title": s["title"],
                "description": desc,
                "tags": s.get("tags",""),
                "preview_video_id": vid_id,
                "preview_link": link,
                "slot": slot,
                "created_at": datetime.utcnow().isoformat(),
                "duration_sec": round(dur,2),
                "attempt": attempt
            }
            new_body = set_metadata_in_issue_body(issue_body, meta)
            return meta, new_body
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
        # Update metadata JSON topics as well to keep everything consistent
        try:
            meta = get_metadata_from_issue_body(body) or {}
        except Exception:
            meta = {}
        meta["topics"] = options
        new_body = set_metadata_in_issue_body(body, meta)
        new_body += "\n\nNew topic options:\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3)."
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        return

    if comment.lower().startswith("/approve-topic"):
        topics = parse_topics_from_body(body)
        if not topics:
            post_comment(owner, repo, number, "Couldn't detect topics in the Issue. Please reply /new-topic to get fresh options.")
            return
        parts = comment.split()
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        if not (1 <= idx <= len(topics)):
            post_comment(owner, repo, number, "Invalid index. Use 1/2/3.\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(topics[:3])]))
            return
        topic = topics[idx-1]
        meta, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=5)
        if not meta:
            post_comment(owner, repo, number, "Couldn't get under 58s after several attempts. Reply /new-topic for different topics.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval"); remove_label(owner, repo, number, "await-topic-approval")
        post_comment(owner, repo, number, f"Preview ready (attempt {meta['attempt']}, {meta['duration_sec']}s): {meta['preview_link']}\nReply:\n- /approve-video (schedule PRIVATE → auto-publish next day {slot})\n- /reject-video (pick a new topic)")
        return

    if comment.lower().startswith("/reject-video"):
        add_label(owner, repo, number, "await-topic-approval")
        remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, "OK. Reply /new-topic for fresh options.")
        return

    if comment.lower().startswith("/regenerate-video"):
        meta = get_metadata_from_issue_body(body)
        topic = meta["topic"] if meta and "topic" in meta else None
        if not topic:
            post_comment(owner, repo, number, "No topic to regenerate. Use /approve-topic first.")
            return
        meta2, new_body = build_preview_until_under_58(topic, slot, body, max_attempts=5)
        if not meta2:
            post_comment(owner, repo, number, "Couldn't get under 58s after several attempts. Use /new-topic.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, f"New preview ready (attempt {meta2['attempt']}, {meta2['duration_sec']}s): {meta2['preview_link']}\nReply /approve-video to schedule.")
        return

    if comment.lower().startswith("/approve-video"):
        meta = get_metadata_from_issue_body(body)
        if not meta or "preview_video_id" not in meta:
            post_comment(owner, repo, number, "No preview video found. Please /regenerate-video.")
            return
        vid_id = meta["preview_video_id"]
        publish_at_utc = schedule_existing_video(vid_id, meta.get("slot","morning"))
        ist_time = datetime.fromisoformat(publish_at_utc.replace("Z","")).astimezone(IST).strftime("%Y-%m-%d %H:%M")

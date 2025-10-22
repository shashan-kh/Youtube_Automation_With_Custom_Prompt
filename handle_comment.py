import os, re, json, tempfile, subprocess, requests, traceback, sys, math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from moviepy.editor import VideoFileClip, concatenate_videoclips, AudioFileClip
from gtts import gTTS
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ------------ Config / Env ------------
REGION = os.getenv("REGION", "IN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Models (override via workflow env)
DEFAULT_PRIMARY = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")
DEFAULT_FALLBACKS = os.getenv("GROQ_FALLBACK_MODELS",
                              "llama-3.3-70b-versatile,llama-3.1-8b-instant,openai/gpt-oss-20b,openai/gpt-oss-120b,groq/compound-mini,groq/compound,moonshotai/kimi-k2-instruct")

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
TTS_LANG = os.getenv("TTS_LANG", "en")

# Target duration and visuals
PREVIEW_MIN = 50.0            # minimum strict target (seconds)
PREVIEW_MAX = 57.8            # hard cap to remain < 58s
TARGET_H = 1920               # 9:16 vertical
TARGET_W = 1080
CAPTION_FONTSIZE = 34         # smaller captions
CAPTION_MARGIN_V = 220        # further from bottom edge
APPLY_STABILIZE = True        # ffmpeg deshake on each clip
SAFE_TAGS = ["health","wellness","habits","selfcare","sleep","hydration","movement","posture","stress","nutrition"]
IST = timezone(timedelta(hours=5, minutes=30))

# ------------ GitHub Helpers ------------
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

# ------------ Parsing helpers ------------
def extract_json_block(text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if m: return m.group(1)
    m = re.search(r"```\s*(\{.*?\})\s*```", text, re.S)
    if m: return m.group(1)
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else None

def parse_topics_from_body(body):
    # 1) JSON metadata with topics
    try:
        block = extract_json_block(body)
        if block:
            data = json.loads(block)
            if isinstance(data, dict) and isinstance(data.get("topics"), list) and data["topics"]:
                return [str(t).strip() for t in data["topics"] if str(t).strip()][:3]
    except Exception:
        pass
    # 2) Numbered/bulleted lines (e.g., "1) topic", "1. topic")
    topics = []
    for line in body.splitlines():
        m = re.match(r"\s*(?:[-*•]\s*)?(\d+)[\)\.\-:]\s+(.*\S)", line)
        if m: topics.append(m.group(2).strip())
    if topics:
        return topics[:3]
    # 3) After "Choose one:" until "Reply with"
    lines = body.splitlines()
    cleaned, flag = [], False
    for ln in lines:
        if not flag and "Choose one" in ln:
            flag = True
            continue
        if flag:
            s_ln = ln.strip()
            if not s_ln or s_ln.lower().startswith("reply with"):
                break
            s = re.sub(r"^\s*(?:[-*•]\s*)?(?:\d+[\)\.\-:])?\s*", "", s_ln).strip()
            if s: cleaned.append(s)
    if cleaned:
        return cleaned[:3]
    return []

def get_slot_from_labels(labels):
    for lbl in labels or []:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.startswith("slot:"):
            return name.split(":", 1)[1].strip() or "morning"
    return "morning"

# ------------ Groq LLM ------------
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
    env_primary = DEFAULT_PRIMARY
    env_fallbacks = [m.strip() for m in DEFAULT_FALLBACKS.split(",") if m.strip()]
    models = []
    if env_primary: models.append(env_primary)
    models += env_fallbacks
    # de-dup
    seen=set(); models=[m for m in models if not (m in seen or seen.add(m))]
    available = list_groq_models()
    if not available:
        return models
    # keep only available
    return [m for m in models if m in available] or available[:8]

def llm_script(trending_query, word_hint="130–160"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret")
    prompt = f"""
You are a careful health educator. Create a STRICTLY under-58s YouTube Short based on this trending topic:
"{trending_query}"

Rules:
- General wellness only (sleep, hydration, movement, posture, stress, basic nutrition).
- No disease claims, diagnoses, dosages, or supplement promises. Avoid COVID/vaccines.
- If the query is unsafe/specific (e.g., drugs/diseases), pivot to a safe, related habit.
- Style: energetic, plain language, second-person; target {word_hint} words to land ~50–58 seconds with TTS.

Output PURE JSON with keys:
- voiceover: string
- overlay_lines: array of 7–9 very short lines (3–6 words each), suitable for bottom captions
- title: catchy, <=90 chars
- description: 2 short sentences + “Educational only, not medical advice.” + 1 credible source (WHO/CDC/NIH/NHS)
- tags: 6–10 comma-separated general tags
"""
    models_to_try = build_model_list()
    last_err = None
    for model in models_to_try:
        r = call_groq(prompt, model)
        if r.status_code == 200:
            raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not raw:
                last_err = RuntimeError(f"Groq '{model}' returned empty content"); continue
            block = extract_json_block(raw) or raw
            try:
                data = json.loads(block)
                if isinstance(data.get("tags"), list):
                    data["tags"] = ",".join(data["tags"])
                return data
            except Exception as e:
                last_err = RuntimeError(f"Failed to parse LLM JSON from '{model}': {e}\nRaw: {raw[:500]}"); continue
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
        raise RuntimeError(f"{last_err}\nAvailable models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL/GROQ_FALLBACK_MODELS env to one of the above.")
    raise last_err or RuntimeError("Groq call failed; no models available")

# ------------ Media helpers ------------
def ensure_voice_under_target(voice_path, target=PREVIEW_MAX):
    a = AudioFileClip(voice_path); dur = a.duration; a.close()
    if dur <= target:
        return voice_path, dur
    factor = max(0.5, min(2.0, target / dur))
    out = "voice_fast.mp3"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",voice_path,"-filter:a",f"atempo={factor}","-vn",out], check=True)
    a2 = AudioFileClip(out); d2 = a2.duration; a2.close()
    return out, d2

def stabilize_video(in_path, out_path):
    # Deshake filter to reduce shaky footage
    cmd = ["ffmpeg","-hide_banner","-loglevel","error","-y","-i",in_path,
           "-vf","deshake=rx=64:ry=64:edge=mirror","-c:v","libx264","-preset","veryfast","-crf","20","-an",out_path]
    subprocess.run(cmd, check=True)

def fetch_broll(query, need=4):
    if not PEXELS_API_KEY:
        raise RuntimeError("Missing PEXELS_API_KEY secret")
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    vids = []
    for q in [query, "healthy lifestyle", "fitness", "sleep", "hydration", "walking", "stretching", "posture", "nutrition", "yoga"]:
        try:
            r = requests.get(url, headers=headers, params={"query": q, "per_page": 30, "orientation": "portrait"}, timeout=60)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Pexels API auth error {r.status_code}: {r.text}")
            for v in r.json().get("videos", []):
                dur = int(v.get("duration", 0))
                if dur < 6:  # skip micro-clips (often shakier)
                    continue
                files = sorted(v.get("video_files", []), key=lambda f: f.get("height",0), reverse=True)
                for f in files:
                    if f.get("height",0) >= 1080 and f.get("width",0) <= f.get("height",0):
                        vids.append((f["link"], dur)); break
            if len(vids) >= need*2: break
        except Exception:
            continue
    vids = sorted(vids, key=lambda x: x[1], reverse=True)
    return [link for link,_ in vids[:max(need,4)]]

def fmt_time(t):
    h = int(t // 3600); m = int((t % 3600)//60); s = int(t % 60); cs = int((t - int(t))*100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"  # ASS times are centiseconds

def make_ass(lines, duration, path, width=TARGET_W, height=TARGET_H, fontsize=CAPTION_FONTSIZE, margin_v=CAPTION_MARGIN_V):
    # Create ASS with a bottom-centered style (white text + black stroke), no box
    lines = [l.strip() for l in (lines or []) if str(l).strip()]
    if not lines:
        lines = ["Small steps add up","Move, hydrate, rest","Focus on form","You've got this!"]
    n = max(1, min(9, len(lines)))
    step = max(1.6, duration / n)
    t = 0.5

    ass = []
    ass.append("[Script Info]")
    ass.append("ScriptType: v4.00+")
    ass.append(f"PlayResX: {width}")
    ass.append(f"PlayResY: {height}")
    ass.append("")
    ass.append("[V4+ Styles]")
    ass.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
               "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
               "Alignment, MarginL, MarginR, MarginV, Encoding")
    # PrimaryColour &H00FFFFFF& (white), OutlineColour &H00000000& (black)
    ass.append(f"Style: Bot,DejaVu Sans,{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,1,2,30,30,{margin_v},1")
    ass.append("")
    ass.append("[Events]")
    ass.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
    for i, text in enumerate(lines):
        start = fmt_time(min(t, max(0, duration-0.5)))
        end = fmt_time(min(t + step, duration))
        # Prevent too-long lines (keep small bottom captions)
        clean = re.sub(r"\s+", " ", text)
        if len(clean) > 34:
            clean = clean[:32] + "…"
        ass.append(f"Dialogue: 0,{start},{end},Bot,,0,0,0,,{clean}")
        t = min(end_time := (t + step - 0.2), duration - 0.2)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(ass))

def burn_ass(in_mp4, ass_path, out_mp4):
    # Burn ASS with style (bottom, small font, stroke)
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",in_mp4,"-vf",f"subtitles={ass_path}", "-c:a","copy", out_mp4], check=True)

def normalize_1080x1920(in_mp4, out_mp4):
    # Force exact 1080x1920 (cover + crop)
    vf = "scale=1080:1920:force_original_aspect_ratio=cover,crop=1080:1920"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",in_mp4,"-vf",vf,"-c:a","copy",out_mp4], check=True)

def render_and_cap(broll_urls, voice_mp3, temp_mp4, final_mp4, overlay_lines):
    tmp = Path(tempfile.mkdtemp())
    local = []
    # Download + optionally stabilize
    for i,u in enumerate(broll_urls):
        p = tmp / f"b{i}.mp4"
        with requests.get(u, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for ch in r.iter_content(1024*256): f.write(ch)
        sp = tmp / f"s{i}.mp4"
        if APPLY_STABILIZE:
            try:
                stabilize_video(str(p), str(sp))
            except Exception:
                sp = p  # fallback
        else:
            sp = p
        local.append(str(sp))

    # Determine take length per clip based on voice length for smooth pacing
    voice = AudioFileClip(voice_mp3)
    voice_len = voice.duration
    num = max(1, len(local))
    per_clip = min(12, max(7, math.ceil((voice_len + 1.5) / num)))
    clips = []
    for p in local:
        c = VideoFileClip(p)
        take = min(per_clip, max(6, int(c.duration)))
        c = c.subclip(0, take).resize(height=TARGET_H)
        if c.w != TARGET_W:
            c = c.crop(x_center=c.w/2, width=TARGET_W, height=TARGET_H)
        clips.append(c)

    merged = concatenate_videoclips(clips)
    # Follow voice length but clamp to [PREVIEW_MIN, PREVIEW_MAX]
    end = min(merged.duration, voice_len + 0.4, PREVIEW_MAX)
    merged = merged.subclip(0, end).set_audio(voice)
    merged.write_videofile(temp_mp4, fps=30, codec="libx264", audio_codec="aac", threads=2, preset="fast", verbose=False, logger=None)
    voice.close(); [c.close() for c in clips]; merged.close()

    # Make ASS captions and burn
    ass_path = str(tmp / "cap.ass")
    make_ass(overlay_lines, end, ass_path, width=TARGET_W, height=TARGET_H, fontsize=CAPTION_FONTSIZE, margin_v=CAPTION_MARGIN_V)
    burned = str(tmp / "burned.mp4")
    burn_ass(temp_mp4, ass_path, burned)

    # Normalize to exact 1080x1920 (prevents any odd sizing)
    normalize_1080x1920(burned, final_mp4)

    v = VideoFileClip(final_mp4); d = v.duration; v.close()
    # Final safety cap (rare)
    if d > PREVIEW_MAX + 0.15:
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",final_mp4,"-t",str(PREVIEW_MAX),"-c","copy","short_trim.mp4"], check=False)
        if os.path.exists("short_trim.mp4"):
            os.replace("short_trim.mp4", final_mp4)
            v2 = VideoFileClip(final_mp4); d2 = v2.duration; v2.close()
            return d2
    return d

# ------------ YouTube helpers (UNLISTED preview → schedule) ------------
def yt_client():
    for v in ["YT_CLIENT_ID","YT_CLIENT_SECRET","YT_REFRESH_TOKEN"]:
        if not os.getenv(v): raise RuntimeError(f"Missing {v} secret")
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
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
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
    ist_hour = 16 if slot == "afternoon" else 9
    ist_dt = datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, ist_hour, 0, tzinfo=IST)
    publish_at_utc = ist_dt.astimezone(timezone.utc).isoformat()
    body = {"id": video_id, "status": {"privacyStatus": "private", "publishAt": publish_at_utc, "selfDeclaredMadeForKids": False}}
    yt.videos().update(part="status", body=body).execute()
    return publish_at_utc

# ------------ Build preview in band ------------
def set_metadata_in_issue_body(issue_body, meta):
    block = "```json\n" + json.dumps(meta, indent=2) + "\n```"
    if "```json" in issue_body:
        return re.sub(r"```json\s*\{.*?\}\s*```", block, issue_body, flags=re.S)
    return issue_body + "\n\nMetadata:\n" + block

def get_metadata_from_issue_body(issue_body):
    m = re.search(r"```json\s*(\{.*?\})\s*```", issue_body, re.S)
    return json.loads(m.group(1)) if m else None

def build_preview_until_in_band(topic, slot, issue_body, max_attempts=6):
    word_ranges = ["130–160","140–170","120–150","150–180","110–140","100–130"]
    for attempt in range(1, max_attempts+1):
        s = llm_script(topic, word_hint=word_ranges[min(attempt-1, len(word_ranges)-1)])
        voice = "voice.mp3"; gTTS(s["voiceover"], lang=TTS_LANG).save(voice)
        voice, vdur = ensure_voice_under_target(voice, target=PREVIEW_MAX)
        if vdur < PREVIEW_MIN and attempt < max_attempts:
            continue
        broll = fetch_broll(topic, need=4)
        if not broll:
            continue
        temp, final = "temp.mp4", "short.mp4"
        dur = render_and_cap(broll, voice, temp, final, s.get("overlay_lines", []))
        if PREVIEW_MIN <= dur <= PREVIEW_MAX:
            desc = f"""{s['description']}

Educational only, not medical advice. Consult a qualified professional for personal guidance.
#Shorts #health #wellness"""
            vid_id, link = upload_youtube_unlisted(final, s["title"], desc, s.get("tags",""))
            meta = {
                "topic": topic, "title": s["title"], "description": desc, "tags": s.get("tags",""),
                "preview_video_id": vid_id, "preview_link": link, "slot": slot,
                "created_at": datetime.utcnow().isoformat(), "duration_sec": round(dur,2), "attempt": attempt
            }
            new_body = set_metadata_in_issue_body(issue_body, meta)
            return meta, new_body
    return None, issue_body

# ------------ Main flow ------------
def safe_main():
    e = ev()
    owner, repo = REPO.split("/")
    issue = e["issue"]; number = issue["number"]
    commenter = e["comment"]["user"]["login"]
    repo_owner = e["repository"]["owner"]["login"]
    if commenter != repo_owner:
        print("Ignoring comment from non-owner"); return
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
        body += "\n\nNew topic options:\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(options, 1)]) + "\n\nReply with /approve-topic 1 (or 2/3)."
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": body})
        return

    if comment.lower().startswith("/approve-topic"):
        topics = parse_topics_from_body(body)
        if not topics:
            post_comment(owner, repo, number, "Couldn't detect topics. Please reply /new-topic to get fresh options."); return
        parts = comment.split()
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        if not (1 <= idx <= len(topics)):
            post_comment(owner, repo, number, "Invalid index. Use 1/2/3.\n" + "\n".join([f"{i+1}) {t}" for i,t in enumerate(topics[:3])]))
            return
        topic = topics[idx-1]
        meta, new_body = build_preview_until_in_band(topic, slot, body, max_attempts=6)
        if not meta:
            post_comment(owner, repo, number, f"Couldn't produce a 50–58s preview after several tries. Reply /new-topic to try different topics.")
            return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval"); remove_label(owner, repo, number, "await-topic-approval")
        post_comment(owner, repo, number, f"Preview ready (attempt {meta['attempt']}, {meta['duration_sec']}s): {meta['preview_link']}\nReply:\n- /approve-video (schedule PRIVATE → auto‑publish next day {slot})\n- /reject-video (pick a new topic)")
        return

    if comment.lower().startswith("/reject-video"):
        add_label(owner, repo, number, "await-topic-approval"); remove_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, "OK. Reply /new-topic for fresh options."); return

    if comment.lower().startswith("/regenerate-video"):
        meta = get_metadata_from_issue_body(body)
        topic = meta["topic"] if meta and "topic" in meta else None
        if not topic:
            post_comment(owner, repo, number, "No topic to regenerate. Use /approve-topic first."); return
        meta2, new_body = build_preview_until_in_band(topic, slot, body, max_attempts=6)
        if not meta2:
            post_comment(owner, repo, number, "Couldn't get 50–58s after several attempts. Use /new-topic."); return
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"body": new_body})
        add_label(owner, repo, number, "await-video-approval")
        post_comment(owner, repo, number, f"New preview ready (attempt {meta2['attempt']}, {meta2['duration_sec']}s): {meta2['preview_link']}\nReply /approve-video to schedule.")
        return

    if comment.lower().startswith("/approve-video"):
        meta = get_metadata_from_issue_body(body)
        if not meta or "preview_video_id" not in meta:
            post_comment(owner, repo, number, "No preview video found. Please /regenerate-video."); return
        publish_at_utc = schedule_existing_video(meta["preview_video_id"], meta.get("slot","morning"))
        ist_time = datetime.fromisoformat(publish_at_utc.replace("Z","")).astimezone(IST).strftime("%Y-%m-%d %H:%M")
        link = f"https://youtu.be/{meta['preview_video_id']}"
        post_comment(owner, repo, number, f"Scheduled ✅ {link}\nPublishes at (IST): {ist_time}\nClosing this thread.")
        remove_label(owner, repo, number, "await-video-approval")
        gh("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", json={"state":"closed"})
        return

# ------------ Entrypoint ------------
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
                addendum = "\n\nAvailable models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL/GROQ_FALLBACK_MODELS in workflow env to one of the above."
            msg = f"❌ Error: {e}{addendum}\n```\n{traceback.format_exc()}\n```"
            post_comment(owner, repo, number, msg)
        except Exception:
            print("FATAL:", e); print(traceback.format_exc())
        return

if __name__ == "__main__":
    main()

import os, re, json, requests, sys
from datetime import datetime, timedelta, timezone

REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")  # morning or afternoon
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
DEBUG = os.getenv("DEBUG", "0") == "1"

# YouTube OAuth (read-only for fetch)
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

IST = timezone(timedelta(hours=5, minutes=30))
MORNING_IST = (9, 0)
AFTERNOON_IST = (16, 0)

def log(*args):
    if DEBUG:
        print("[propose]", *args)

def next_slot_ist():
    tomorrow_ist = datetime.now(IST).date() + timedelta(days=1)
    if SLOT == "afternoon":
        return datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, AFTERNOON_IST[0], AFTERNOON_IST[1], tzinfo=IST)
    return datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, MORNING_IST[0], MORNING_IST[1], tzinfo=IST)

def gh(method, url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update({"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"})
    if DEBUG and method != "GET":
        log("HTTP", method, url, "payload:", kwargs.get("json") or kwargs.get("data"))
    r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if DEBUG:
        log("HTTP", method, url, "->", r.status_code)
        if r.status_code >= 400:
            log("Response body:", r.text[:2000])
    r.raise_for_status()
    return r

def ensure_labels(owner, repo, labels):
    try:
        existing = []
        page = 1
        while True:
            r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/labels", params={"per_page": 100, "page": page})
            arr = r.json()
            if not isinstance(arr, list) or not arr:
                break
            existing += [lbl.get("name","") for lbl in arr if isinstance(lbl, dict)]
            if len(arr) < 100:
                break
            page += 1
        log("Existing labels:", existing)
    except Exception as e:
        log("Failed to list labels:", e)
        existing = []
    for name, (color, desc) in labels.items():
        if name in existing:
            continue
        try:
            gh("POST", f"https://api.github.com/repos/{owner}/{repo}/labels",
               json={"name": name, "color": color, "description": desc})
            log("Created label:", name)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (409, 422):
                log("Label exists/validation (ok):", name)
                continue
            raise

def open_issues_with_labels(owner, repo, labels):
    r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues",
           params={"state": "open", "labels": ",".join(labels), "per_page": 100})
    items = r.json()
    if not isinstance(items, list):
        log("Unexpected issues response:", items)
        return []
    wanted = set(labels)
    issues = []
    for it in items:
        if "pull_request" in it:
            continue
        names = {lbl.get("name","") for lbl in it.get("labels", []) if isinstance(lbl, dict)}
        if wanted.issubset(names):
            issues.append(it)
    log(f"Open issues with labels {labels}:", [(i.get('number'), i.get('title')) for i in issues])
    return issues

# ---------- Duplicate prevention helpers ----------
def normalize_topic(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\W_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def jaccard(a: str, b: str) -> float:
    sa = set(normalize_topic(a).split())
    sb = set(normalize_topic(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def load_recent_approved_topics(owner, repo, max_issues=300, min_sim_threshold=0.8):
    approved = set()
    page = 1
    fetched = 0
    while fetched < max_issues:
        per_page = min(100, max_issues - fetched)
        r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues",
               params={"state": "closed", "per_page": per_page, "page": page, "sort":"updated", "direction":"desc"})
        arr = r.json()
        if not isinstance(arr, list) or not arr:
            break
        for it in arr:
            if "pull_request" in it:
                continue
            num = it.get("number")
            # Quick scan: any comment saying "Scheduled ✅"?
            try:
                cr = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{num}/comments", params={"per_page": 100})
                comments = cr.json() if isinstance(cr.json(), list) else []
                scheduled = any(("Scheduled ✅" in (c.get("body") or "")) for c in comments)
            except Exception:
                scheduled = False
            if not scheduled:
                continue
            # Extract topic from metadata block if present
            body = it.get("body") or ""
            m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
            topic = None
            if m:
                try:
                    md = json.loads(m.group(1))
                    if isinstance(md, dict) and md.get("topic"):
                        topic = str(md.get("topic")).strip()
                except Exception:
                    pass
            if topic:
                duplicate = any(jaccard(topic, t2) >= min_sim_threshold for t2 in approved)
                if not duplicate:
                    approved.add(topic)
        fetched += len(arr)
        if len(arr) < per_page:
            break
        page += 1
    log("Previously approved topics (normalized):", [normalize_topic(t) for t in list(approved)[:10]])
    return approved

# ---------- YouTube client (OAuth) ----------
def ensure_google_client():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        return Credentials, build
    except Exception:
        # Best-effort install in CI
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "-q",
                        "google-api-python-client", "google-auth", "google-auth-oauthlib", "packaging>=23.1"], check=True)
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        return Credentials, build

def yt_client():
    for v in ["YT_CLIENT_ID","YT_CLIENT_SECRET","YT_REFRESH_TOKEN"]:
        if not os.getenv(v):
            raise RuntimeError(f"Missing {v} secret (required to fetch YouTube Trending)")
    Credentials, build = ensure_google_client()
    creds = Credentials(
        token=None, refresh_token=os.getenv("YT_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID"), client_secret=os.getenv("YT_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.readonly"]
    )
    return build("youtube", "v3", credentials=creds)

# ---------- Language + health filtering ----------
# We do NOT treat the word "english" as a keyword. We detect English language from text.
from langdetect import detect_langs, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException
DetectorFactory.seed = 0  # deterministic

EN_STOP = {"the","and","you","your","with","from","this","that","for","not","are","can","how","tips","health","sleep","water","posture","daily","habit","routine","simple","easy"}

HEALTH_KEYWORDS = [
    "health","healthy","wellness","fitness","workout","exercise","gym","yoga","meditation",
    "posture","sleep","insomnia","nutrition","diet","protein","hydration","water",
    "stress","mindfulness","stretch","mobility","steps","walking","running","cardio",
    "strength","back pain","neck pain","core","ergonomics","breathing","sunlight","recovery","flexibility"
]
BANNED = re.compile(
    r"(covid|vaccine|cancer|diabetes|ozempic|semaglutide|hiv|flu|tumor|depress|adhd|autism|arthritis|ibd|crohn|pcos|pregnan|detox|steroid|pill|drug|supplement|dosage|cure|therapy|remedy|weight\s*loss\s*drugs?)",
    re.I
)

def is_english_text(text: str, prob_threshold=0.85, ascii_threshold=0.97, allow_short=False) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    # Remove URLs and extra noise
    s = re.sub(r"https?://\S+|www\.\S+", " ", s)
    # Try langdetect
    try:
        langs = detect_langs(s)
        if langs:
            top = max(langs, key=lambda x: x.prob)
            if top.lang == "en" and top.prob >= prob_threshold:
                return True
    except LangDetectException:
        pass
    # Fallback heuristic for short/noisy text
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return False
    ascii_letters = sum(1 for ch in letters if ord(ch) < 128)
    ascii_ratio = ascii_letters / max(1, len(letters))
    has_en_stop = any((" " + w + " ") in (" " + s.lower() + " ") for w in EN_STOP)
    if allow_short:
        return ascii_ratio >= 0.98 or (ascii_ratio >= ascii_threshold and has_en_stop)
    return (ascii_ratio >= ascii_threshold and has_en_stop)

def text_has_health_signal(title: str, desc: str, tags: list) -> bool:
    blob = " ".join([title or "", desc or "", " ".join(tags or [])]).lower()
    if BANNED.search(blob):
        return False
    return any(kw in blob for kw in HEALTH_KEYWORDS)

def clean_title_to_topic(title: str) -> str:
    s = (title or "").strip()
    s = re.sub(r"(?i)#?shorts?", "", s)             # remove #Shorts
    s = re.sub(r"\[[^\]]+\]|\([^)]+\)", "", s)      # remove bracketed
    s = re.split(r"\s+[|\-–—]\s+", s)[0].strip()    # keep left-most main part
    s = re.sub(r"\s+", " ", s).strip(" -–—:|")      # tidy
    if s:
        s = s[0].upper() + s[1:]
    return s[:100].rstrip()

# ---------- Primary: YouTube Trending (strict EN + health) ----------
def fetch_youtube_trending_health_topics(region="IN", max_items=12, exclude_topics=None):
    exclude_topics = exclude_topics or set()
    try:
        yt = yt_client()
    except Exception as e:
        log("YouTube client init failed:", e)
        return []

    items = []
    try:
        req = yt.videos().list(part="snippet", chart="mostPopular", regionCode=region, maxResults=50)
        while req is not None and len(items) < 150:
            resp = req.execute()
            items.extend(resp.get("items", []) or [])
            token = resp.get("nextPageToken")
            req = yt.videos().list(part="snippet", chart="mostPopular", regionCode=region, maxResults=50, pageToken=token) if token else None
    except Exception as e:
        log("YouTube Trending fetch error:", e)
        return []

    # Newest first
    def pub_dt(it):
        try:
            return datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z","+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=pub_dt, reverse=True)

    topics, seen = [], set()
    for it in items:
        sn = it.get("snippet", {}) or {}
        title = sn.get("title") or ""
        desc = sn.get("description") or ""
        tags = sn.get("tags") if isinstance(sn.get("tags"), list) else []

        if not is_english_text(title + " " + desc + " " + " ".join(tags or [])):   # true language detection
            continue
        if not text_has_health_signal(title, desc, tags):  # strictly health
            continue

        topic = clean_title_to_topic(title)
        if not topic or len(normalize_topic(topic)) < 4:
            continue

        norm = normalize_topic(topic)
        if norm in seen:
            continue
        if any(jaccard(topic, ex) >= 0.8 for ex in exclude_topics):
            continue

        seen.add(norm)
        topics.append(topic)
        if len(topics) >= max_items:
            break
    log("YT Trending EN health:", topics[:5])
    return topics

# ---------- Fallback 1: YouTube Search (recent, EN + health) ----------
SEARCH_SEEDS = [
    "health tips", "sleep tips", "hydration habit", "posture fixes", "yoga routine",
    "stretching routine", "mobility routine", "mindfulness", "breathing exercises",
    "desk ergonomics", "healthy snacks", "back pain relief", "neck pain relief", "walking benefits",
    "core strength", "morning routine health"
]

def fetch_youtube_search_health_topics(region="IN", max_items=12, days=7, exclude_topics=None):
    exclude_topics = exclude_topics or set()
    try:
        yt = yt_client()
    except Exception as e:
        log("YouTube client init failed (search):", e)
        return []

    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found, seen = [], set()
    seeds = SEARCH_SEEDS[:8]  # limit to control quota

    for q in seeds:
        try:
            req = yt.search().list(
                part="snippet",
                q=q,
                type="video",
                order="viewCount",
                publishedAfter=cutoff,
                maxResults=15,
                regionCode=region,
                relevanceLanguage="en",
                safeSearch="none",
            )
            resp = req.execute()
            items = resp.get("items", []) or []
        except Exception as e:
            log("YouTube search error:", q, e)
            continue

        for it in items:
            sn = (it.get("snippet") or {})
            title = sn.get("title") or ""
            desc = sn.get("description") or ""

            if not is_english_text(title + " " + desc):
                continue
            if not text_has_health_signal(title, desc, []):
                continue

            topic = clean_title_to_topic(title)
            if not topic or len(normalize_topic(topic)) < 4:
                continue

            norm = normalize_topic(topic)
            if norm in seen:
                continue
            if any(jaccard(topic, ex) >= 0.8 for ex in exclude_topics):
                continue

            seen.add(norm)
            found.append(topic)
            if len(found) >= max_items:
                break
        if len(found) >= max_items:
            break

    log("YT Search EN health:", found[:5])
    return found

# ---------- Fallback 2: Google Trends (health-related, EN only) ----------
def fetch_google_trends_health_topics(region="IN", max_items=12, exclude_topics=None):
    exclude_topics = exclude_topics or set()
    topics, seen = [], set()
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-IN", tz=330)
    except Exception as e:
        log("pytrends init error:", e)
        return []

    seeds = [
        "sleep","hydration","walking","steps","posture","stretching","mobility","stress","breathing",
        "morning sunlight","protein","fiber","yoga","desk ergonomics","healthy snacks","mindfulness",
        "core strength","back pain relief","neck pain","bedtime routine"
    ]

    # 1) realtime trending searches
    try:
        df = pt.realtime_trending_searches(pn=region)
        if df is not None and "title" in df.columns:
            for t in df["title"].tolist():
                if not isinstance(t, str): continue
                if not is_english_text(t, allow_short=True): continue
                if not text_has_health_signal(t, "", []): continue
                topic = clean_title_to_topic(t)
                norm = normalize_topic(topic)
                if not topic or norm in seen: continue
                if any(jaccard(topic, ex) >= 0.8 for ex in exclude_topics): continue
                seen.add(norm); topics.append(topic)
                if len(topics) >= max_items: break
    except Exception as e:
        log("pytrends realtime error:", e)

    # 2) related queries rising/top for seeds (last 1 day)
    for s in seeds:
        if len(topics) >= max_items: break
        try:
            pt.build_payload([s], timeframe="now 1-d", geo=region)
            rq = pt.related_queries() or {}
            rq_s = rq.get(s, {})
            for k in ("rising","top"):
                df2 = rq_s.get(k)
                if df2 is not None and "query" in df2.columns:
                    for q in df2.head(15)["query"].tolist():
                        if not isinstance(q, str): continue
                        if not is_english_text(q, allow_short=True): continue
                        if not text_has_health_signal(q, "", []): continue
                        topic = clean_title_to_topic(q)
                        norm = normalize_topic(topic)
                        if not topic or norm in seen: continue
                        if any(jaccard(topic, ex) >= 0.8 for ex in exclude_topics): continue
                        seen.add(norm); topics.append(topic)
                        if len(topics) >= max_items: break
        except Exception as e:
            log("pytrends related error:", s, e)
            continue

    log("Google Trends EN health:", topics[:5])
    return topics[:max_items]

# ---------- Aggregator with fallbacks ----------
def gather_trending_health_topics(region="IN", need=3, exclude_topics=None):
    exclude_topics = exclude_topics or set()
    collected = []

    # 1) YouTube Trending
    yt_trending = fetch_youtube_trending_health_topics(region, max_items=12, exclude_topics=exclude_topics)
    collected.extend(yt_trending)

    # 2) YouTube Search (if still short)
    if len(collected) < need:
        yt_search = fetch_youtube_search_health_topics(region, max_items=12, days=7, exclude_topics=exclude_topics | set(collected))
        collected.extend([t for t in yt_search if t not in collected])

    # 3) Google Trends (if still short)
    if len(collected) < need:
        gtr = fetch_google_trends_health_topics(region, max_items=12, exclude_topics=exclude_topics | set(collected))
        collected.extend([t for t in gtr if t not in collected])

    return collected[:12]

# ---------- Issue creation ----------
def create_topic_issue(owner, repo, topics, scheduled_ist, note=""):
    slot_label = f"slot:{SLOT}"
    ensure_labels(owner, repo, {
        "await-topic-approval": ("ededed", "Awaiting topic approval"),
        slot_label: ("bfd4f2" if SLOT == "morning" else "c2e0c6", f"Issue for {SLOT} slot")
    })

    shown = topics[:3]  # show up to 3 trending options
    if shown:
        numbered = "\n".join([f"{i}) {t}" for i, t in enumerate(shown, 1)])
        opts_str = "/".join(str(i) for i in range(1, len(shown) + 1))
        guidance = ""
    else:
        numbered = "(No eligible English health topics found via YouTube Trending/Search/Google Trends.)"
        opts_str = "N/A"
        guidance = (
            "\nNote:\n"
            "- No strictly-English health topics detected right now.\n"
            "- You can wait and rerun, or reply with:\n"
            "  • /reject-topic (I’ll try again later)\n"
            "  • /custom-topic Your Topic (provide a safe wellness topic)\n"
        )
    if note:
        guidance += f"\nDebug: {note}"

    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    body = f"""Proposed latest trending topics strictly in the health niche and strictly in English (YouTube-driven; excludes previously approved).
Scheduled publish (IST): {scheduled_ist.strftime('%Y-%m-%d %H:%M')}.

Choose one:
{numbered}{guidance}

Reply with:
- /approve-topic 1   (or {opts_str})
- /reject-topic      (I’ll propose new fresh topics)
- /custom-topic Your Topic   (use your own safe wellness topic)

/regenerate-video (rebuild same topic under 58s) and /approve-video (schedule upload) are used after preview is ready.

Metadata:
```json
{json.dumps({"slot": SLOT, "scheduled_ist": scheduled_ist.strftime('%Y-%m-%d %H:%M'), "topics": shown}, indent=2)}
```"""
    log("Creating issue:", title)
    gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues",
       json={"title": title, "body": body, "labels": ["await-topic-approval", slot_label]})

def main():
    log("Region:", REGION, "| Slot:", SLOT, "| Repo:", REPO)
    if not REPO or "/" not in REPO:
        print("GITHUB_REPOSITORY not set.")
        sys.exit(2)
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not available.")
        sys.exit(2)
    owner, repo = REPO.split("/")

    try:
        open_slot = open_issues_with_labels(owner, repo, [f"slot:{SLOT}","await-topic-approval"])
    except requests.HTTPError as e:
        print("Failed to query issues:", e)
        sys.exit(4)
    if isinstance(open_slot, list) and open_slot:
        print("An approval issue for this slot is already open. Skipping.")
        return

    # Previously approved topics (from closed issues with Scheduled ✅)
    try:
        approved = load_recent_approved_topics(owner, repo, max_issues=300, min_sim_threshold=0.8)
    except Exception as e:
        log("Failed to load approved topics:", e)
        approved = set()

    # Aggregate trending topics with fallbacks
    try:
        candidates = gather_trending_health_topics(REGION, need=3, exclude_topics=approved)
    except Exception as e:
        log("Aggregator error:", e)
        candidates = []

    topics = candidates[:3]

    scheduled_ist = next_slot_ist()
    # Always create an issue (even if zero options), with guidance
    try:
        note = "" if topics else "Found 0 topics after YouTube Trending, YouTube Search, and Google Trends."
        create_topic_issue(owner, repo, topics, scheduled_ist, note=note)
        log("Using topics:", topics if topics else ["<none>"])
        print("Created topic approval issue for", SLOT)
    except requests.HTTPError as e:
        print("Failed to create issue:", e.response.text if e.response is not None else str(e))
        sys.exit(5)

if __name__ == "__main__":
    main()
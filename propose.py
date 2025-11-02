import os, re, json, requests, sys
from datetime import datetime, timedelta, timezone

REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")  # morning or afternoon
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
DEBUG = os.getenv("DEBUG", "0") == "1"

# YouTube OAuth secrets for Data API
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

# ---------- English + health filtering ----------
HEALTH_KEYWORDS = [
    "health","healthy","fitness","workout","exercise","gym","yoga","meditation",
    "posture","sleep","insomnia","nutrition","diet","protein","hydration","water",
    "stress","mindfulness","stretch","mobility","steps","walking","running","cardio",
    "strength","back pain","neck pain","core","ergonomics","breathing","sunlight","wellness"
]
BANNED = re.compile(
    r"(covid|vaccine|cancer|diabetes|ozempic|semaglutide|hiv|flu|tumor|depress|adhd|autism|arthritis|ibd|crohn|pcos|pregnan|detox|steroid|pill|drug|supplement|dosage|cure|therapy|remedy|weight\s*loss\s*drugs?)",
    re.I
)

def is_mostly_english(s: str, threshold=0.85) -> bool:
    if not s:
        return False
    ascii_chars = sum(1 for ch in s if ord(ch) < 128)
    return (ascii_chars / max(1, len(s))) >= threshold

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
    # Keep natural casing but ensure first char uppercase
    if s:
        s = s[0].upper() + s[1:]
    # Keep concise
    return s[:100].rstrip()

# ---------- Fetch YouTube trending health topics (strictly English) ----------
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
            if token:
                req = yt.videos().list(part="snippet", chart="mostPopular", regionCode=region, maxResults=50, pageToken=token)
            else:
                break
    except Exception as e:
        log("YouTube trending fetch error:", e)
        return []

    # Sort newest first
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

        # Strict English
        if not is_mostly_english(title + " " + desc):
            continue
        # Strict health niche
        if not text_has_health_signal(title, desc, tags):
            continue

        topic = clean_title_to_topic(title)
        if not topic or len(normalize_topic(topic)) < 4:
            continue

        # Dedup within this batch
        norm = normalize_topic(topic)
        if norm in seen:
            continue

        # Exclude previously approved topics (near-duplicates)
        if any(jaccard(topic, ex) >= 0.8 for ex in exclude_topics):
            continue

        seen.add(norm)
        topics.append(topic)
        if len(topics) >= max_items:
            break

    log("Trending EN health topics:", topics[:5])
    return topics

# ---------- Issue creation ----------
def create_topic_issue(owner, repo, topics, scheduled_ist):
    slot_label = f"slot:{SLOT}"
    ensure_labels(owner, repo, {
        "await-topic-approval": ("ededed", "Awaiting topic approval"),
        slot_label: ("bfd4f2" if SLOT == "morning" else "c2e0c6", f"Issue for {SLOT} slot")
    })

    shown = topics[:3]  # show up to 3 trending options (no padding)
    numbered = "\n".join([f"{i}) {t}" for i, t in enumerate(shown, 1)]) if shown else "(no eligible topics found)"
    opts_str = "/".join(str(i) for i in range(1, len(shown) + 1)) if shown else "N/A"

    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    body = f"""Proposed latest YouTube trending health topics (strictly English; excludes previously approved).
Scheduled publish (IST): {scheduled_ist.strftime('%Y-%m-%d %H:%M')}.

Choose one:
{numbered}

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

    # Fetch strictly English, health-niche YouTube trending topics
    try:
        candidates = fetch_youtube_trending_health_topics(REGION, max_items=12, exclude_topics=approved)
    except Exception as e:
        log("Trending fetch error:", e)
        candidates = []

    topics = candidates[:3]  # strictly trending only; no padding

    if not topics:
        print("No English YouTube trending health topics found right now. Skipping issue creation.")
        return

    scheduled_ist = next_slot_ist()
    try:
        create_topic_issue(owner, repo, topics, scheduled_ist)
        log("Using topics:", topics)
        print("Created topic approval issue for", SLOT)
    except requests.HTTPError as e:
        print("Failed to create issue:", e.response.text if e.response is not None else str(e))
        sys.exit(5)

if __name__ == "__main__":
    main()
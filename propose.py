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
                duplicate = any(jaccard(topic, t2) >= min_sim_threshold 
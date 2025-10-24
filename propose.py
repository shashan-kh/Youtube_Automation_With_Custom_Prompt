import os, re, json, requests, sys
from datetime import datetime, timedelta, timezone
from pytrends.request import TrendReq

REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")  # morning or afternoon
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
DEBUG = os.getenv("DEBUG", "0") == "1"

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

def extract_json_block(text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.S)
    if m: return m.group(1)
    m = re.search(r"```\s*(\{.*?\})\s*```", text or "", re.S)
    if m: return m.group(1)
    m = re.search(r"\{.*\}", text or "", re.S)
    return m.group(0) if m else None

def get_metadata_from_issue_body(issue_body):
    blk = extract_json_block(issue_body or "")
    if not blk:
        return None
    try:
        return json.loads(blk)
    except Exception:
        return None

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
            meta = get_metadata_from_issue_body(body) or {}
            # Only count topics that were actually approved for upload
            if meta.get("upload_approved") is True:
                t = meta.get("topic")
                if isinstance(t, str) and t.strip():
                    used.add(norm_topic(t))
        if len(items) < 100:
            break
        page += 1
    log("Used topics (approved for upload):", list(sorted(used))[:10], "...")
    return used

def fetch_trending_candidates(region="IN", max_items=24):
    pt = TrendReq(hl="en-IN", tz=330)
    seeds = ["sleep","hydration","walking","steps","posture","stretching","mobility","stress","breathing","morning sunlight","protein","fiber","yoga","desk ergonomics","screen time","healthy snacks"]
    banned = re.compile(r"(covid|vaccine|cancer|diabetes|ozempic|semaglutide|hiv|flu|tumor|depress|adhd|autism|arthritis|ibd|crohn|pcos|pregnan|detox|steroid|pill|drug|supplement|dosage|cure|therapy)", re.I)
    found = []
    try:
        df = pt.realtime_trending_searches(pn=region)
        if df is not None and "title" in df.columns:
            for t in df["title"].tolist():
                if isinstance(t, str) and not banned.search(t) and any(w in t.lower() for w in ["sleep","diet","workout","walk","steps","hydrate","posture","stress","breath","sunlight","healthy","fitness","yoga"]):
                    found.append(t)
    except Exception as e:
        log("pytrends realtime_trending_searches error:", e)
    for s in seeds:
        try:
            pt.build_payload([s], timeframe="now 1-d", geo=region)
            rq = pt.related_queries() or {}
            rq_s = rq.get(s, {})
            for k in ("rising","top"):
                df2 = rq_s.get(k)
                if df2 is not None and "query" in df2.columns:
                    for q in df2.head(12)["query"].tolist():
                        if isinstance(q, str) and not banned.search(q):
                            found.append(q)
        except Exception as e:
            log(f"pytrends related_queries error for seed '{s}':", e)
            continue
    out, seen = [], set()
    for q in found + seeds:
        key = str(q).strip()
        n = norm_topic(key)
        if n and n not in seen:
            seen.add(n); out.append(key)
        if len(out) >= max_items: break
    final = out or ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine","Take more walking breaks","Easy stretch flow"]
    log("Candidate topics:", final[:6])
    return final

def create_topic_issue(owner, repo, topics, scheduled_ist):
    slot_label = f"slot:{SLOT}"
    ensure_labels(owner, repo, {
        "await-topic-approval": ("ededed", "Awaiting topic approval"),
        slot_label: ("bfd4f2" if SLOT == "morning" else "c2e0c6", f"Issue for {SLOT} slot")
    })
    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    body = f"""Proposed topics for tomorrow's {SLOT} slot.
Scheduled publish (IST): {scheduled_ist.strftime('%Y-%m-%d %H:%M')}.

(Excluding topics previously approved for upload.)

Choose one:
1) {topics[0]}
2) {topics[1]}
3) {topics[2]}

Reply with:
- /approve-topic 1   (or 2/3)
- /reject-topic      (I’ll propose new topics)
- /custom-topic Your Topic   (use your own safe wellness topic)

/regenerate-video (rebuild same topic under 58s) and /approve-video (schedule upload) are used after preview is ready.

Metadata:
```json
{json.dumps({"slot": SLOT, "scheduled_ist": scheduled_ist.strftime('%Y-%m-%d %H:%M'), "topics": topics[:3]}, indent=2)}
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

    # Exclude only topics previously approved for upload
    used = list_used_topics_uploaded_only(owner, repo)
    candidates = fetch_trending_candidates(REGION, max_items=30)
    fresh = []
    seen = set()
    for t in candidates:
        n = norm_topic(t)
        if n in used or n in seen:
            continue
        seen.add(n); fresh.append(t)
        if len(fresh) >= 3:
            break
    # Fallbacks if needed
    if len(fresh) < 3:
        fallbacks = [
            "Desk posture routine","Breath to reduce stress","Get morning sunlight",
            "Protein with every meal","High-fiber snack ideas","Simple mobility flow",
            "Screen-time wind-down","Take more walking breaks","Wind-down sleep ritual"
        ]
        for f in fallbacks:
            if len(fresh) >= 3: break
            if norm_topic(f) not in used and norm_topic(f) not in seen:
                fresh.append(f); seen.add(norm_topic(f))

    if len(fresh) < 3:
        base = ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine","Take more walking breaks","Easy stretch flow"]
        for b in base:
            if len(fresh) >= 3: break
            if norm_topic(b) not in used and norm_topic(b) not in seen:
                fresh.append(b); seen.add(norm_topic(b))

    topics = fresh[:3]
    scheduled_ist = next_slot_ist()
    try:
        create_topic_issue(owner, repo, topics, scheduled_ist)
        print("Created topic approval issue for", SLOT)
    except requests.HTTPError as e:
        print("Failed to create issue:", e.response.text if e.response is not None else str(e))
        sys.exit(5)

if __name__ == "__main__":
    main()

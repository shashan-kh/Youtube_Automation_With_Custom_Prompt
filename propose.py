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

def load_recent_approved_topics(owner, repo, max_issues=200, min_sim_threshold=0.8):
    approved = set()
    titles_seen = set()
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
            # If not found, fallback to title parsing
            if not topic:
                title = it.get("title","")
                titles_seen.add(title)
            if topic:
                norm = normalize_topic(topic)
                # Avoid almost duplicates by Jaccard similarity
                duplicate = any(jaccard(topic, t2) >= min_sim_threshold for t2 in approved)
                if not duplicate:
                    approved.add(topic)
        fetched += len(arr)
        if len(arr) < per_page:
            break
        page += 1
    log("Previously approved topics (normalized):", [normalize_topic(t) for t in list(approved)[:10]])
    return approved

# ---------- Helper: ensure we always have at least 3 safe topics ----------
FALLBACK_POOL = [
    "Desk posture checklist",
    "Healthy snack swaps at work",
    "Walking breaks every hour",
    "Quick mobility routine for hips",
    "Breathing reset for stress",
    "Screen-time eye care tips",
    "Core stability basics",
    "Neck and shoulder stretch break",
    "Morning sunlight routine",
    "Water intake habit tracker",
    "Simple bedtime wind-down",
    "Gentle back mobility flow",
]

def pad_topics_to_three(topics, exclude_topics):
    out = [t for t in topics if isinstance(t, str) and t.strip()]
    if len(out) >= 3:
        return out[:3]

    # Try to add from pool avoiding near-duplicates with both out and previously approved
    def can_add(cand, thr=0.8):
        if any(jaccard(cand, t) >= thr for t in out):
            return False
        if any(jaccard(cand, ex) >= thr for ex in exclude_topics):
            return False
        return True

    # Two passes with decreasing strictness
    for thr in (0.8, 0.7):
        for cand in FALLBACK_POOL:
            if len(out) >= 3:
                break
            if can_add(cand, thr=thr):
                out.append(cand)
        if len(out) >= 3:
            break

    # Absolute fallback: generic uniques
    i = 1
    while len(out) < 3:
        cand = f"Healthy daily habit idea {i}"
        if can_add(cand, thr=0.5):
            out.append(cand)
        i += 1
    return out[:3]

# ---------- Trending fetch ----------
def fetch_trending_candidates(region="IN", max_items=20, exclude_topics=None):
    exclude_topics = exclude_topics or set()
    exclude_norms = {normalize_topic(x) for x in exclude_topics}
    pt = TrendReq(hl="en-IN", tz=330)
    seeds = [
        "sleep","hydration","walking","steps","posture","stretching","mobility","stress","breathing",
        "morning sunlight","protein","fiber","yoga","desk ergonomics","screen time","healthy snacks",
        "core strength","back pain relief","neck pain","mindfulness","step count","water intake","bedtime routine"
    ]
    banned = re.compile(r"(covid|vaccine|cancer|diabetes|ozempic|semaglutide|hiv|flu|tumor|depress|adhd|autism|arthritis|ibd|crohn|pcos|pregnan|detox|steroid|pill|drug|supplement|dosage|cure|therapy|weight loss drugs?)", re.I)
    def is_ok(s):
        return bool(s) and not banned.search(s) and len(normalize_topic(s)) >= 6
    found = []

    # 1) realtime_trending_searches
    try:
        df = pt.realtime_trending_searches(pn=region)
        if df is not None and "title" in df.columns:
            for t in df["title"].tolist():
                if isinstance(t, str) and is_ok(t):
                    found.append(t)
    except Exception as e:
        log("pytrends realtime_trending_searches error:", e)

    # 2) related queries for seeds (rising first for freshness)
    for s in seeds:
        try:
            pt.build_payload([s], timeframe="now 1-d", geo=region)
            rq = pt.related_queries() or {}
            rq_s = rq.get(s, {})
            for k in ("rising","top"):
                df2 = rq_s.get(k)
                if df2 is not None and "query" in df2.columns:
                    for q in df2.head(15)["query"].tolist():
                        if isinstance(q, str) and is_ok(q):
                            found.append(q)
        except Exception as e:
            log(f"pytrends related_queries error for seed '{s}':", e)
            continue

    # Deduplicate while excluding previously approved
    out, seen = [], set()
    for q in found:
        nq = normalize_topic(q)
        if nq in seen:
            continue
        # Skip if similar to any previously approved topic
        too_close = any(jaccard(q, ex) >= 0.8 for ex in exclude_topics)
        if too_close:
            continue
        seen.add(nq)
        # Title-case lightly (but keep acronyms)
        nice = q.strip()
        nice = nice[0].upper() + nice[1:] if nice else q
        out.append(nice)
        if len(out) >= max_items:
            break

    # Ensure at least some safe fallback topics that are not previously approved and not dupes
    fallbacks = ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine","Take more walking breaks","Easy stretch flow"]
    for f in fallbacks:
        if len(out) >= max_items:
            break
        if all(jaccard(f, ex) < 0.8 for ex in exclude_topics) and all(jaccard(f, q) < 0.8 for q in out):
            out.append(f)

    final = out[:max_items]
    log("Candidate topics (filtered, fresh):", final[:5])
    return final

def create_topic_issue(owner, repo, topics, scheduled_ist):
    slot_label = f"slot:{SLOT}"
    ensure_labels(owner, repo, {
        "await-topic-approval": ("ededed", "Awaiting topic approval"),
        slot_label: ("bfd4f2" if SLOT == "morning" else "c2e0c6", f"Issue for {SLOT} slot")
    })
    # Dynamically render as many as we have (always padded to 3 upstream)
    numbered = "\n".join([f"{i}) {t}" for i, t in enumerate(topics[:3], 1)])
    opts_str = "/".join(str(i) for i in range(1, min(3, len(topics)) + 1))
    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    body = f"""Proposed fresh topics for tomorrow's {SLOT} slot (older approved topics excluded).
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

    # Load previously approved topics (from closed issues with Scheduled ✅)
    try:
        approved = load_recent_approved_topics(owner, repo, max_issues=200, min_sim_threshold=0.8)
    except Exception as e:
        log("Failed to load approved topics:", e)
        approved = set()

    candidates = fetch_trending_candidates(REGION, max_items=12, exclude_topics=approved)
    topics = pad_topics_to_three(candidates, approved)  # ensure 3 safe options
    scheduled_ist = next_slot_ist()
    try:
        create_topic_issue(owner, repo, topics, scheduled_ist)
        log("Using topics:", topics[:3])
        print("Created topic approval issue for", SLOT)
    except requests.HTTPError as e:
        print("Failed to create issue:", e.response.text if e.response is not None else str(e))
        sys.exit(5)

if __name__ == "__main__":
    main()
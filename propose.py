import os, re, json, requests, sys
from datetime import datetime, timedelta, timezone
from pytrends.request import TrendReq

REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")  # morning or afternoon
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
DEBUG = os.getenv("DEBUG", "0") == "1"

IST = timezone(timedelta(hours=5, minutes=30))
MORNING_IST = (9, 0)    # 09:00
AFTERNOON_IST = (16, 0) # 16:00

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
    # labels: dict of name -> (color, description)
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
        log("Failed to list labels (will try creating anyway):", e)
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
            # If we can't create labels (permissions) we'll still try creating the issue below.
            log("Failed to create label:", name, code, e.response.text[:500] if e.response is not None else "")
            continue

def open_issues_with_labels(owner, repo, labels):
    # Use params to ensure proper URL encoding (labels like "slot:morning")
    r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues",
           params={"state": "open", "labels": ",".join(labels), "per_page": 100})
    items = r.json()
    if not isinstance(items, list):
        log("Unexpected issues response:", items)
        return []
    # Filter out PRs and double-check label inclusion
    wanted = set(labels)
    issues = []
    for it in items:
        if "pull_request" in it:
            continue  # only issues, not PRs
        names = {lbl.get("name","") for lbl in it.get("labels", []) if isinstance(lbl, dict)}
        if wanted.issubset(names):
            issues.append(it)
    log(f"Open issues with labels {labels}:", [(i.get('number'), i.get('title')) for i in issues])
    return issues

def fetch_trending_candidates(region="IN", max_items=12):
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
                    for q in df2.head(10)["query"].tolist():
                        if isinstance(q, str) and not banned.search(q):
                            found.append(q)
        except Exception as e:
            log(f"pytrends related_queries error for seed '{s}':", e)
            continue
    out, seen = [], set()
    for q in found:
        key = q.lower().strip()
        if key not in seen:
            seen.add(key); out.append(q.strip())
        if len(out) >= max_items: break
    final = out or ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine","Take more walking breaks","Easy stretch flow"]
    log("Candidate topics:", final[:3])
    return final

def create_topic_issue(owner, repo, topics, scheduled_ist):
    slot_label = f"slot:{SLOT}"
    # Try to ensure labels (ignore failures; we'll fall back if needed)
    ensure_labels(owner, repo, {
        "await-topic-approval": ("ededed", "Awaiting topic approval"),
        slot_label: ("bfd4f2" if SLOT == "morning" else "c2e0c6", f"Issue for {SLOT} slot")
    })
    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    body = f"""Proposed topics for tomorrow's {SLOT} slot.
Scheduled publish (IST): {scheduled_ist.strftime('%Y-%m-%d %H:%M')}.

Choose one:
1) {topics[0]}
2) {topics[1]}
3) {topics[2]}

Reply with:
- /approve-topic 1   (or 2/3)
- /reject-topic      (I’ll propose new topics)

/regenerate-video (rebuild same topic under 58s) and /approve-video (schedule upload) are used after preview is ready.

Metadata:
```json
{json.dumps({"slot": SLOT, "scheduled_ist": scheduled_ist.strftime('%Y-%m-%d %H:%M'), "topics": topics[:3]}, indent=2)}
```"""
    payload = {"title": title, "body": body, "labels": ["await-topic-approval", slot_label]}
    log("Creating issue (with labels):", title)
    try:
        resp = gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues", json=payload)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        text = e.response.text if e.response is not None else str(e)
        # If labels cause 422 (labels don't exist), retry without labels
        if code == 422 and "label" in text.lower():
            log("Label validation failed (422). Retrying without labels.")
            resp = gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues",
                      json={"title": title, "body": body})
        else:
            raise
    data = resp.json()
    url = data.get("html_url")
    number = data.get("number")
    log("Created issue:", number, url)
    print(f"Created topic approval issue for {SLOT}: {url}")

def main():
    log("Region:", REGION, "| Slot:", SLOT, "| Repo:", REPO)
    if not REPO or "/" not in REPO:
        print("GITHUB_REPOSITORY not set. This workflow must run on GitHub Actions.")
        sys.exit(2)
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not available. Ensure the workflow maps GITHUB_TOKEN: ${{ github.token }} and has 'issues: write' permission.")
        sys.exit(2)

    owner, repo = REPO.split("/")

    # Soft-check repository info (don't abort on missing granular permissions field)
    try:
        info = gh("GET", f"https://api.github.com/repos/{owner}/{repo}").json()
        has_issues = bool(info.get("has_issues", True))
        perms = info.get("permissions", {})
        log("Repo has_issues:", has_issues, "| token permissions field:", perms)
        if not has_issues:
            print("Issues are disabled on this repository. Enable Issues in Settings.")
            sys.exit(3)
    except requests.HTTPError as e:
        log("Failed to read repo info (continuing):", e)

    # Skip if an approval issue for this slot is already open (ISSUES ONLY; PRs ignored)
    try:
        open_slot = open_issues_with_labels(owner, repo, [f"slot:{SLOT}","await-topic-approval"])
    except requests.HTTPError as e:
        print("Failed to query issues:", e)
        sys.exit(4)
    if isinstance(open_slot, list) and open_slot:
        print("An approval issue for this slot is already open. Skipping.")
        return

    topics = fetch_trending_candidates(REGION, max_items=12)[:3]
    scheduled_ist = next_slot_ist()
    try:
        create_topic_issue(owner, repo, topics, scheduled_ist)
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else str(e)
        # Helpful hints for common 403 cases
        if e.response is not None and e.response.status_code == 403:
            print("Failed to create issue (403 Forbidden).")
            print("Check: Actions → General → Workflow permissions set to 'Read and write',")
            print("and that this workflow/job has 'permissions: issues: write'.")
        else:
            print("Failed to create issue:", body)
        sys.exit(5)

if __name__ == "__main__":
    main()

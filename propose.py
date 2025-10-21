import os, re, json, requests
from datetime import datetime, timedelta, timezone
from pytrends.request import TrendReq

REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")  # morning or afternoon
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo

IST = timezone(timedelta(hours=5, minutes=30))
MORNING_IST = (9, 0)    # 09:00
AFTERNOON_IST = (16, 0) # 16:00

def next_slot_ist():
    tomorrow_ist = datetime.now(IST).date() + timedelta(days=1)
    if SLOT == "afternoon":
        return datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, AFTERNOON_IST[0], AFTERNOON_IST[1], tzinfo=IST)
    return datetime(tomorrow_ist.year, tomorrow_ist.month, tomorrow_ist.day, MORNING_IST[0], MORNING_IST[1], tzinfo=IST)

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
    except Exception:
        pass
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
        except Exception:
            continue
    out, seen = [], set()
    for q in found:
        key = q.lower().strip()
        if key not in seen:
            seen.add(key); out.append(q.strip())
        if len(out) >= max_items: break
    return out or ["Morning hydration habit","3 simple posture fixes","Sleep wind-down routine","Take more walking breaks","Easy stretch flow"]

def open_issues_with_labels(owner, repo, labels):
    label_q = ",".join(labels)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&labels={label_q}"
    r = requests.get(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"}, timeout=60)
    r.raise_for_status()
    return r.json()

def create_topic_issue(owner, repo, topics, scheduled_ist):
    slot_label = f"slot:{SLOT}"
    title = f"Topic approval for {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d')} {scheduled_ist.strftime('%H:%M')} IST)"
    # Provide both numbered list and JSON metadata to make parsing robust
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
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    data = {"title": title, "body": body, "labels": ["await-topic-approval", slot_label]}
    r = requests.post(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"}, json=data, timeout=60)
    r.raise_for_status()

def main():
    owner, repo = REPO.split("/")
    open_slot = open_issues_with_labels(owner, repo, [f"slot:{SLOT}","await-topic-approval"])
    if open_slot:
        print("An approval issue for this slot is already open. Skipping.")
        return
    topics = fetch_trending_candidates(REGION, max_items=12)[:3]
    scheduled_ist = next_slot_ist()
    create_topic_issue(owner, repo, topics, scheduled_ist)
    print("Created topic approval issue for", SLOT)

if __name__ == "__main__":
    main()

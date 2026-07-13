"""
propose.py — discovers topics and manages the dynamic topics_pool.json.
"""
from __future__ import annotations
import base64
import json
import os
import re
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests
from common import (
    gh, post_comment, get_metadata_from_issue_body,
    set_metadata_in_issue_body, normalize_key, jaccard, get_logger, REPO
)

log = get_logger("propose")

# ── Config ─────────────────────────────────────────────────────────────────────
REGION        = os.getenv("REGION", "IN")
SLOT          = os.getenv("SLOT", "morning")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN")

YT_CLIENT_ID     = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

IST = timezone(timedelta(hours=5, minutes=30))
REGION_ROTATION = ["IN", "US", "GB", "AU", "CA"]

# ── Dynamic Fallback Pool ──────────────────────────────────────────────────────
def get_topics_pool(owner: str, repo: str) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/topics_pool.json"
    try:
        r = gh("GET", url)
        if r.status_code == 200:
            content_b64 = r.json().get("content", "")
            return json.loads(base64.b64decode(content_b64).decode())
    except Exception:
        pass
    return []

def save_topics_pool(owner: str, repo: str, pool: list[str]) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/topics_pool.json"
    sha = None
    try:
        r = gh("GET", url)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    
    unique_pool = list(dict.fromkeys(pool))[-150:]
    content = base64.b64encode(json.dumps(unique_pool, indent=2).encode()).decode()
    payload = {"message": "chore: update dynamic topics pool [skip ci]", "content": content}
    if sha: payload["sha"] = sha
    
    try: gh("PUT", url, json=payload)
    except Exception as exc: log.warning("Failed to save topics pool: %s", exc)

# ── Utilities ──────────────────────────────────────────────────────────────────
def next_slot_ist() -> datetime:
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    h, mn = (16, 0) if SLOT == "afternoon" else (9, 0)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, mn, tzinfo=IST)

def load_recent_approved_topics(owner: str, repo: str) -> set[str]:
    approved, page = set(), 1
    query = f'repo:{owner}/{repo} is:issue is:closed "Scheduled" in:comments'
    while len(approved) < 100:
        try:
            r = gh("GET", "https://api.github.com/search/issues", params={"q": query, "per_page": 30, "page": page, "sort": "updated"})
            items = r.json().get("items", [])
            for it in items:
                meta = get_metadata_from_issue_body(it.get("body") or "")
                if meta and meta.get("topic"):
                    approved.add(str(meta["topic"]).strip())
            if len(items) < 30: break
            page += 1
        except Exception: break
    return approved

def open_issues_with_labels(owner: str, repo: str, labels: list[str]) -> list[dict]:
    r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues", params={"state": "open", "labels": ",".join(labels), "per_page": 100})
    items = r.json()
    if not isinstance(items, list): return []
    return [it for it in items if set(labels).issubset({lbl.get("name", "") for lbl in it.get("labels", []) if isinstance(lbl, dict)})]

def is_english(text: str) -> bool:
    s = (text or "").strip()
    return bool(s and sum(1 for c in s if ord(c) < 128) / len(s) >= 0.9)

def has_health_signal(title: str, desc: str = "") -> bool:
    keywords = ["health", "fitness", "yoga", "sleep", "nutrition", "diet", "water", "stress", 
                "cardio", "back pain", "neck pain", "mindfulness", "brain", "focus", "gut", 
                "fasting", "posture", "skin", "hormones", "longevity", "metabolism", "anxiety"]
    blob = f"{title} {desc}".lower()
    return any(k in blob for k in keywords)

def clean_to_topic(title: str) -> str:
    s = re.sub(r"(?i)#?shorts?|\[[^\]]+\]|\([^)]+\)", "", (title or "").strip())
    s = re.split(r"\s+[|\-–—]\s+", s)[0].strip()
    return (s[0].upper() + s[1:])[:120].rstrip() if s else ""

# ── Data Fetching ──────────────────────────────────────────────────────────────
SEARCH_SEEDS = [
    "how to wake up early naturally", "circadian rhythm reset tips", 
    "gut health improvement tips", "intermittent fasting benefits", 
    "how to reduce cortisol naturally", "breathing exercises for stress", 
    "posture correction exercises", "how to fix neck pain at desk", 
    "how to boost immunity naturally", "cold shower benefits science",
    "skincare anti aging tips", "how to reduce screen fatigue"
]

def fetch_yt_search(region_pref: str, exclude: set) -> list[str]:
    from datetime import datetime, timedelta, timezone
    yt, found = None, []
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(None, refresh_token=YT_REFRESH_TOKEN, token_uri="https://oauth2.googleapis.com/token", client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET)
        yt = build("youtube", "v3", credentials=creds)
    except Exception: return []

    cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for q in random.sample(SEARCH_SEEDS, 5):
        try:
            resp = yt.search().list(part="snippet", q=q, type="video", order="viewCount", publishedAfter=cutoff, maxResults=10, regionCode=region_pref).execute()
            for it in resp.get("items", []):
                t = clean_to_topic(it.get("snippet", {}).get("title", ""))
                if t and is_english(t) and has_health_signal(t) and not any(jaccard(t, ex) > 0.75 for ex in exclude):
                    found.append(t)
        except Exception: pass
    return found

def refine_topics_with_llm(raw: list[str]) -> list[str]:
    if not GROQ_API_KEY or not raw: return raw[:6]
    prompt = f"Convert these raw health trends into 6 SPECIFIC, ACTIONABLE, HIGH-CTR YouTube Shorts topics. Output ONLY a valid JSON array of 6 strings.\n\nTrends: {raw[:20]}"
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                          json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}, timeout=60)
        if r.status_code == 200:
            m = re.search(r"\[.*\]", r.json()["choices"][0]["message"]["content"].strip(), re.S)
            if m: return [str(t).strip() for t in json.loads(m.group(0))][:6]
    except Exception: pass
    return raw[:6]

def gather_topics(region_pref: str, need: int, exclude: set) -> list[str]:
    pool = fetch_yt_search(region_pref, exclude)
    random.shuffle(pool)
    return refine_topics_with_llm(pool)[:need]

# ── Issue Creation ─────────────────────────────────────────────────────────────
def create_topic_issue(owner: str, repo: str, topics: list[str], scheduled_ist: datetime, note: str) -> None:
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(topics, 1)) if topics else "(No eligible topics found)"
    title = f"Topic approval — {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d %H:%M')} IST)"
    
    body = f"""## Proposed Health Short Topics
Trending, actionable topics. Scheduled publish (IST): **{scheduled_ist.strftime('%Y-%m-%d %H:%M')}**

### Choose one:
{numbered}

---
**Reply with:**
- `/approve-topic 1` (or 2-6) — approve a numbered topic
- `/custom-topic Your Specific Topic` — use your own topic
- `/reject-topic` — get fresh suggestions

> **After approval**, the bot will prompt you for the script text.
> You can reply `/set-script [your voiceover text]` or `/use-default-prompt`.

{note}
"""
    meta_block = set_metadata_in_issue_body(body, {"slot": SLOT, "scheduled_ist": scheduled_ist.strftime("%Y-%m-%d %H:%M"), "topics": topics})
    gh("POST", f"https://api.github.com/repos/{owner}/{repo}/issues", json={"title": title, "body": meta_block})

def main() -> None:
    owner, repo = REPO.split("/", 1)
    if open_issues_with_labels(owner, repo, [f"slot:{SLOT}", "await-topic-approval"]): return
    
    approved = load_recent_approved_topics(owner, repo)
    pool = get_topics_pool(owner, repo)
    
    pool = [t for t in pool if not any(jaccard(t, a) >= 0.75 for a in approved)]
    
    try:
        live_candidates = gather_topics(REGION, need=10, exclude=approved.union(set(pool)))
    except Exception:
        live_candidates = []
        
    if live_candidates:
        pool.extend(live_candidates)
        save_topics_pool(owner, repo, pool)
        
    issue_topics = live_candidates[:6] if live_candidates else random.sample(pool, min(6, len(pool)))
    if len(issue_topics) < 6:
        rem = [t for t in pool if t not in issue_topics]
        random.shuffle(rem)
        issue_topics.extend(rem[:6-len(issue_topics)])
        
    note = "" if live_candidates else "\n*Note: Pulled from dynamic fallback pool.*"
    create_topic_issue(owner, repo, issue_topics, next_slot_ist(), note)

if __name__ == "__main__":
    main()

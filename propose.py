"""
propose.py — Discovers policy-safe health topics aligned with the 4 Weekly Content Pillars
(Symptom Awareness, Myth vs Fact, Lifestyle & Prevention, Explainer / How the Body Works)
and manages the dynamic topics_pool.json.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone

import requests

from brand_config import (
    CHANNEL_POSITIONING_STATEMENT,
    CONTENT_PILLARS,
    NEGATIVE_PROMPT_RULES,
    enforce_policy_guardrails,
    get_pillar_for_date,
)
from common import (
    REPO,
    extract_json_block,
    get_logger,
    get_metadata_from_issue_body,
    gh,
    jaccard,
    set_metadata_in_issue_body,
)

log = get_logger("propose")

# ── Config ─────────────────────────────────────────────────────────────────────
REGION = os.getenv("REGION", "IN")
SLOT = os.getenv("SLOT", "morning")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

IST = timezone(timedelta(hours=5, minutes=30))


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
    if sha:
        payload["sha"] = sha

    try:
        gh("PUT", url, json=payload)
    except Exception as exc:
        log.warning("Failed to save topics pool: %s", exc)


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
            if len(items) < 30:
                break
            page += 1
        except Exception:
            break
    return approved


def open_issues_with_labels(owner: str, repo: str, labels: list[str]) -> list[dict]:
    r = gh("GET", f"https://api.github.com/repos/{owner}/{repo}/issues", params={"state": "open", "labels": ",".join(labels), "per_page": 100})
    items = r.json()
    if not isinstance(items, list):
        return []
    return [it for it in items if set(labels).issubset({lbl.get("name", "") for lbl in it.get("labels", []) if isinstance(lbl, dict)})]


def is_english(text: str) -> bool:
    s = (text or "").strip()
    return bool(s and sum(1 for c in s if ord(c) < 128) / len(s) >= 0.9)


def has_health_signal(title: str, desc: str = "") -> bool:
    keywords = [
        "health", "symptom", "sleep", "nutrition", "diet", "water", "stress",
        "cardio", "back pain", "neck pain", "mindfulness", "brain", "focus", "gut",
        "fasting", "posture", "hormones", "metabolism", "vagus", "circadian",
        "hydration", "digestion", "eye strain", "cortisol",
    ]
    blob = f"{title} {desc}".lower()
    # Reject policy-risky topics (miracle cures, whitening creams, extreme detoxes)
    red_flags = ["whitening", "fairness", "miracle cure", "reverses disease", "never visit doctors", "ozempic"]
    if any(rf in blob for rf in red_flags):
        return False
    return any(k in blob for k in keywords)


def clean_to_topic(title: str) -> str:
    s = re.sub(r"(?i)#?\w+|\[[^\]]+\]|\([^)]+\)", "", (title or "").strip())
    s = re.sub(r"[^\x00-\x7F]+", " ", s)
    s = re.split(r"\s+[|\-–—]\s+", s)[0].strip()
    s = enforce_policy_guardrails(s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[0].upper() + s[1:])[:110].rstrip() if s else ""


# ── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_yt_search(region_pref: str, exclude: set, pillar: dict) -> list[str]:
    yt, found = None, []
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(
            None,
            refresh_token=YT_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=YT_CLIENT_ID,
            client_secret=YT_CLIENT_SECRET,
        )
        yt = build("youtube", "v3", credentials=creds)
    except Exception:
        return []

    seeds = list(pillar.get("seed_queries", []))
    for p_info in CONTENT_PILLARS.values():
        seeds.extend(p_info["seed_queries"][:2])
    random.shuffle(seeds)

    cutoff = (datetime.utcnow() - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for q in seeds[:5]:
        try:
            resp = yt.search().list(
                part="snippet",
                q=q,
                type="video",
                order="viewCount",
                publishedAfter=cutoff,
                maxResults=10,
                regionCode=region_pref,
            ).execute()
            for it in resp.get("items", []):
                t = clean_to_topic(it.get("snippet", {}).get("title", ""))
                if t and is_english(t) and has_health_signal(t) and not any(jaccard(t, ex) > 0.75 for ex in exclude):
                    found.append(t)
        except Exception:
            pass
    return found


def refine_topics_with_llm(raw: list[str], pillar: dict) -> list[str]:
    if not GROQ_API_KEY:
        return raw[:6]
    prompt = (
        f"You are a YouTube Health & Symptoms Niche Strategist.\n"
        f"Scheduled Content Pillar for this slot: **{pillar['name']}** — {pillar['framing']}\n\n"
        f"{NEGATIVE_PROMPT_RULES}\n\n"
        "Convert these raw health signals into 6 SPECIFIC, HIGH-CTR, POLICY-SAFE YouTube Shorts topics.\n"
        "Use proven curiosity-driven educational formulas such as:\n"
        "- '5 Subtle [Condition/Habit] Signals People Often Ignore'\n"
        "- 'Myth vs Fact: Does [Habit] Actually Affect Your [Organ/Hormone]?'\n"
        "- 'Why Does This Happen? [Symptom/Mechanism] Explained Simply'\n"
        "- 'What Happens Inside Your Body When You [Daily Habit]'\n\n"
        f"Raw Signals: {raw[:20]}\n\n"
        "Output ONLY a valid JSON array of 6 strings."
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.75},
            timeout=60,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"].strip()
            block = extract_json_block(content) or content
            parsed = json.loads(block)
            if isinstance(parsed, list):
                return [enforce_policy_guardrails(str(t).strip()) for t in parsed if str(t).strip()][:6]
    except Exception as exc:
        log.warning("LLM topic refinement failed: %s", exc)
    return [clean_to_topic(x) for x in raw if clean_to_topic(x)][:6]


def gather_topics(region_pref: str, need: int, exclude: set, target_dt: datetime | None = None) -> list[str]:
    pillar = get_pillar_for_date(target_dt)
    pool = fetch_yt_search(region_pref, exclude, pillar)
    random.shuffle(pool)
    refined = refine_topics_with_llm(pool if pool else pillar["seed_queries"], pillar)
    return refined[:need]


# ── Issue Creation ─────────────────────────────────────────────────────────────
def create_topic_issue(owner: str, repo: str, topics: list[str], scheduled_ist: datetime, note: str) -> None:
    pillar = get_pillar_for_date(scheduled_ist)
    numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(topics, 1)) if topics else "(No eligible topics found)"
    title = f"Topic approval — {SLOT} slot ({scheduled_ist.strftime('%Y-%m-%d %H:%M')} IST) [{pillar['name']}]"

    body = f"""## Proposed Health Short Topics
- **Scheduled Publish (IST):** `{scheduled_ist.strftime('%Y-%m-%d %H:%M')}`
- **Scheduled Content Pillar:** **{pillar['name']}** — *{pillar['framing']}*
- **Channel Positioning:** *{CHANNEL_POSITIONING_STATEMENT}*

### Choose one:
{numbered}

---
**Reply with either:**
1. **Combined (Topic + Script in one comment):**
   - `/approve-topic 1 /use-default-prompt` (or `/approve-topic 2 /set-script Your custom script text...`)
   - `/custom-topic Your Topic /use-default-prompt` (or `/custom-topic Your Topic /set-script Your script text...`)

2. **Step-by-step:**
   - `/approve-topic 1` (or 2-6) — approve a numbered topic (bot will then prompt for script)
   - `/custom-topic Your Specific Topic` — use your own topic
   - `/reject-topic` — get fresh suggestions
{note}
"""
    meta_block = set_metadata_in_issue_body(
        body,
        {
            "slot": SLOT,
            "pillar": pillar["name"],
            "scheduled_ist": scheduled_ist.strftime("%Y-%m-%d %H:%M"),
            "topics": topics,
        },
    )
    gh(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        json={
            "title": title,
            "body": meta_block,
            "labels": [f"slot:{SLOT}", "await-topic-approval"],
        },
    )


def main() -> None:
    owner, repo = REPO.split("/", 1)
    if open_issues_with_labels(owner, repo, [f"slot:{SLOT}"]):
        return

    sched_dt = next_slot_ist()
    approved = load_recent_approved_topics(owner, repo)
    pool = [clean_to_topic(t) for t in get_topics_pool(owner, repo) if has_health_signal(t)]
    pool = [t for t in pool if t and not any(jaccard(t, a) >= 0.75 for a in approved)]

    try:
        live_candidates = gather_topics(REGION, need=10, exclude=approved.union(set(pool)), target_dt=sched_dt)
    except Exception:
        live_candidates = []

    if live_candidates:
        pool.extend(live_candidates)
        save_topics_pool(owner, repo, pool)

    issue_topics = live_candidates[:6] if live_candidates else random.sample(pool, min(6, len(pool)))
    if len(issue_topics) < 6:
        rem = [t for t in pool if t not in issue_topics]
        random.shuffle(rem)
        issue_topics.extend(rem[: 6 - len(issue_topics)])

    note = "" if live_candidates else "\n*Note: Pulled from policy-safe dynamic fallback pool.*"
    create_topic_issue(owner, repo, issue_topics, sched_dt, note)


if __name__ == "__main__":
    main()

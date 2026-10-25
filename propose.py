"""
propose.py — Discovers policy-safe health topics aligned with the 4 Weekly Content Pillars
(Symptom Awareness, Myth vs Fact, Lifestyle & Prevention, Explainer / How the Body Works)
and manages the dynamic topics_pool.json.
"""
from __future__ import annotations

import base64
import html
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

# Built-in emergency fallback topics in case topics_pool.json or APIs ever fail
BUILTIN_SAFE_TOPICS = [
    "5 Subtle Dehydration Signals People Often Ignore",
    "Why Your Neck Hurts at a Desk: Forward Head Posture Explained",
    "How 3 Minutes of Alternate Nostril Breathing Calms the Nervous System",
    "Myth vs Fact: Does Skipping Breakfast Slow Down Your Metabolism?",
    "Why Looking Down at Your Phone Adds 40 Pounds of Cervical Spine Load",
    "How Digital Eye Strain Makes Your Brain Feel Exhausted",
    "Why 10 Minutes of Morning Sunlight Resets Your Melatonin Timer",
    "How 4-7-8 Breathing Activates the Parasympathetic Nervous System",
    "What Happens to Your Brain Glymphatic System During Deep Sleep",
    "Why Walking 10 Minutes After Meals Helps Regulate Blood Glucose",
]


# ── Topic Validation & Cleaning ────────────────────────────────────────────────
def is_valid_topic(text: str) -> bool:
    """Ensures a topic has at least 3 actual words and 15+ characters (prevents punctuation-only bugs)."""
    if not text or len(text.strip()) < 15:
        return False
    alpha_words = re.findall(r"[a-zA-Z]{2,}", text)
    return len(alpha_words) >= 3


def clean_to_topic(title: str) -> str:
    """
    Cleans raw YouTube/LLM titles into crisp topic strings while preserving all words and numbers.
    Fixes the previous `#?\\w+` regex bug that stripped alphanumeric words.
    """
    s = html.unescape((title or "").strip())
    # Remove leading numbering or bullets like "1) ", "1. ", "- "
    s = re.sub(r"^\s*(?:\d+[\)\.\:]|\-|\*)\s*", "", s)
    # Remove ONLY actual hashtags (requiring the '#' prefix)
    s = re.sub(r"(?:^|\s)#\w+", " ", s)
    # Remove standalone "shorts" word if left over
    s = re.sub(r"(?i)\bshorts?\b", " ", s)
    # Remove bracketed or parenthesized tags like [Shorts] or (Official Video)
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s)
    # Strip emojis and non-ASCII characters
    s = re.sub(r"[^\x00-\x7F]+", " ", s)
    # Split off channel names after pipes or spaced dashes (e.g. "Topic | Dr. Name" or "Topic - Channel")
    s = re.split(r"\s+\|\s+|\s+[-–—]{1,2}\s+", s)[0].strip()
    # Apply YMYL policy guardrails
    s = enforce_policy_guardrails(s)
    # Clean stray leading/trailing punctuation while keeping trailing '?'
    s = re.sub(r"\s+", " ", s).strip(" :;,-–—.!\"'")
    if not is_valid_topic(s):
        return ""
    return (s[0].upper() + s[1:])[:110].rstrip()


def is_english(text: str) -> bool:
    s = (text or "").strip()
    return bool(s and sum(1 for c in s if ord(c) < 128) / len(s) >= 0.9)


def has_health_signal(title: str, desc: str = "") -> bool:
    keywords = [
        "health", "symptom", "sleep", "nutrition", "diet", "water", "stress",
        "cardio", "back", "neck", "pain", "mindfulness", "brain", "focus", "gut",
        "fast", "fasting", "posture", "hormone", "metabolism", "vagus", "circadian",
        "hydration", "dehydration", "digestion", "eye", "cortisol", "breathing",
        "sunlight", "melatonin", "magnesium", "fiber", "glucose", "blood", "spine",
        "shoulder", "nervous", "nerve", "stretch", "walk", "walking", "caffeine",
    ]
    blob = f"{title} {desc}".lower()
    red_flags = ["whitening", "fairness", "miracle cure", "reverses disease", "never visit doctors", "ozempic"]
    if any(rf in blob for rf in red_flags):
        return False
    return any(k in blob for k in keywords)


# ── Dynamic Fallback Pool ──────────────────────────────────────────────────────
def get_topics_pool(owner: str, repo: str) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/topics_pool.json"
    try:
        r = gh("GET", url)
        if r.status_code == 200:
            content_b64 = r.json().get("content", "")
            raw_list = json.loads(base64.b64decode(content_b64).decode())
            if isinstance(raw_list, list):
                valid = [clean_to_topic(str(x)) for x in raw_list]
                valid = [x for x in valid if is_valid_topic(x)]
                if valid:
                    return valid
    except Exception as exc:
        log.warning("Could not load remote topics_pool.json: %s", exc)

    # Local file fallback
    try:
        with open("topics_pool.json", "r", encoding="utf-8") as f:
            local_list = json.load(f)
            valid = [clean_to_topic(str(x)) for x in local_list if is_valid_topic(clean_to_topic(str(x)))]
            if valid:
                return valid
    except Exception:
        pass

    return list(BUILTIN_SAFE_TOPICS)


def save_topics_pool(owner: str, repo: str, pool: list[str]) -> None:
    clean_pool = [clean_to_topic(t) for t in pool]
    clean_pool = [t for t in clean_pool if is_valid_topic(t)]
    if len(clean_pool) < 6:
        log.warning("Refusing to overwrite topics_pool.json with fewer than 6 valid topics.")
        return

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/topics_pool.json"
    sha = None
    try:
        r = gh("GET", url)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    unique_pool = list(dict.fromkeys(clean_pool))[-150:]
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
                if is_valid_topic(t) and is_english(t) and has_health_signal(t) and not any(jaccard(t, ex) > 0.75 for ex in exclude):
                    found.append(t)
        except Exception:
            pass
    return found


def refine_topics_with_llm(raw: list[str], pillar: dict) -> list[str]:
    cleaned_raw = [clean_to_topic(x) for x in raw if is_valid_topic(clean_to_topic(x))]
    if not GROQ_API_KEY or not cleaned_raw:
        return cleaned_raw[:6]

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
        f"Raw Signals: {cleaned_raw[:20]}\n\n"
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
                out = [clean_to_topic(str(t)) for t in parsed]
                out = [t for t in out if is_valid_topic(t)]
                if len(out) >= 4:
                    return out[:6]
    except Exception as exc:
        log.warning("LLM topic refinement failed: %s", exc)
    return cleaned_raw[:6]


def gather_topics(region_pref: str, need: int, exclude: set, target_dt: datetime | None = None) -> list[str]:
    pillar = get_pillar_for_date(target_dt)
    pool = fetch_yt_search(region_pref, exclude, pillar)
    random.shuffle(pool)
    refined = refine_topics_with_llm(pool if pool else pillar["seed_queries"], pillar)
    return [t for t in refined if is_valid_topic(t)][:need]


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
    pool = [clean_to_topic(t) for t in get_topics_pool(owner, repo)]
    pool = [t for t in pool if is_valid_topic(t) and has_health_signal(t) and not any(jaccard(t, a) >= 0.75 for a in approved)]

    if len(pool) < 6:
        for bt in BUILTIN_SAFE_TOPICS:
            if bt not in pool:
                pool.append(bt)

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

    if len(issue_topics) < 6:
        for bt in BUILTIN_SAFE_TOPICS:
            if bt not in issue_topics:
                issue_topics.append(bt)
            if len(issue_topics) >= 6:
                break

    note = "" if live_candidates else "\n*Note: Pulled from policy-safe dynamic fallback pool.*"
    create_topic_issue(owner, repo, issue_topics[:6], sched_dt, note)


if __name__ == "__main__":
    main()

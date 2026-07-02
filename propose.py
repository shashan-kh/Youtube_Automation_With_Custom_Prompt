"""
propose.py — discovers specific, actionable health topics and opens a GitHub
approval issue.  Sources (all tried, results merged & deduplicated):
  A) YouTube Trending (multi-region)
  B) YouTube Search  (recent, health seeds)
  C) Top Health Channels (latest uploads)
  D) Google Trends    (related queries on health seeds)
  E) Reddit           (r/health, r/sleep, r/fitness, r/nutrition …)

Topics are refined to be *specific* (e.g. "Sleeping techniques to wake up
early" not just "sleep") via an LLM-rewriting step before the issue is filed.
"""

from __future__ import annotations

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
    gh,
    post_comment,
    add_label,
    get_metadata_from_issue_body,
    set_metadata_in_issue_body,
    normalize_key,
    jaccard,
    get_logger,
    REPO,
)

log = get_logger("propose")

# ── Config ─────────────────────────────────────────────────────────────────────
REGION        = os.getenv("REGION", "IN")
SLOT          = os.getenv("SLOT", "morning")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN")
DEBUG         = os.getenv("DEBUG", "0") == "1"

YT_CLIENT_ID     = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

IST            = timezone(timedelta(hours=5, minutes=30))
MORNING_IST    = (9, 0)
AFTERNOON_IST  = (16, 0)
REGION_ROTATION = ["IN", "US", "GB", "AU", "CA"]

# ── Slot helpers ───────────────────────────────────────────────────────────────

def next_slot_ist() -> datetime:
    tomorrow = datetime.now(IST).date() + timedelta(days=1)
    h, mn = AFTERNOON_IST if SLOT == "afternoon" else MORNING_IST
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, mn, tzinfo=IST)


# ── Label bootstrap ───────────────────────────────────────────────────────────

def ensure_labels(owner: str, repo: str) -> None:
    wanted = {
        "await-topic-approval":  ("ededed", "Awaiting topic approval"),
        "await-prompt":          ("fbca04", "Awaiting user script prompt"),
        "await-video-approval":  ("0075ca", "Awaiting video approval"),
        f"slot:{SLOT}":          (
            "bfd4f2" if SLOT == "morning" else "c2e0c6",
            f"Issue for {SLOT} slot",
        ),
    }
    try:
        existing: set[str] = set()
        page = 1
        while True:
            r = gh(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/labels",
                params={"per_page": 100, "page": page},
            )
            arr = r.json()
            if not isinstance(arr, list) or not arr:
                break
            existing.update(lbl.get("name", "") for lbl in arr if isinstance(lbl, dict))
            if len(arr) < 100:
                break
            page += 1
    except Exception as exc:
        log.warning("Could not list labels: %s", exc)
        existing = set()

    for name, (color, desc) in wanted.items():
        if name in existing:
            continue
        try:
            gh(
                "POST",
                f"https://api.github.com/repos/{owner}/{repo}/labels",
                json={"name": name, "color": color, "description": desc},
            )
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (409, 422):
                pass  # already exists
            else:
                raise


# ── Duplicate guard ───────────────────────────────────────────────────────────

def load_recent_approved_topics(owner: str, repo: str, max_issues: int = 100) -> set[str]:
    """Return topics from issues that received a 'Scheduled ✅' comment."""
    approved: set[str] = set()
    page = 1
    per_page = 30
    query = f'repo:{owner}/{repo} is:issue is:closed "Scheduled" in:comments'
    while len(approved) < max_issues:
        try:
            r = gh(
                "GET",
                "https://api.github.com/search/issues",
                params={"q": query, "per_page": per_page, "page": page, "sort": "updated"},
            )
            items = r.json().get("items", [])
        except Exception as exc:
            log.warning("Search API failed: %s", exc)
            break
        for it in items:
            body = it.get("body") or ""
            meta = get_metadata_from_issue_body(body)
            if meta and meta.get("topic"):
                topic = str(meta["topic"]).strip()
                if topic and not any(jaccard(topic, t2) >= 0.75 for t2 in approved):
                    approved.add(topic)
        if len(items) < per_page:
            break
        page += 1
    log.info("Previously approved topics: %d", len(approved))
    return approved


def open_issues_with_labels(owner: str, repo: str, labels: list[str]) -> list[dict]:
    r = gh(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        params={"state": "open", "labels": ",".join(labels), "per_page": 100},
    )
    items = r.json()
    if not isinstance(items, list):
        return []
    wanted = set(labels)
    return [
        it for it in items
        if "pull_request" not in it
        and wanted.issubset(
            {lbl.get("name", "") for lbl in it.get("labels", []) if isinstance(lbl, dict)}
        )
    ]


# ── English / health filters ───────────────────────────────────────────────────
try:
    from langdetect import detect_langs, DetectorFactory
    from langdetect.lang_detect_exception import LangDetectException
    DetectorFactory.seed = 0
    _LANGDETECT_OK = True
except Exception:
    _LANGDETECT_OK = False

EN_STOP = {
    "the", "and", "you", "your", "with", "from", "this", "that", "for",
    "not", "are", "can", "how", "tips", "sleep", "water", "posture",
    "daily", "habit", "routine", "simple", "easy", "health", "wellness",
}

HEALTH_KEYWORDS = [
    "health", "healthy", "wellness", "fitness", "workout", "exercise",
    "gym", "yoga", "meditation", "posture", "sleep", "insomnia", "nutrition",
    "diet", "protein", "hydration", "water", "stress", "mindfulness",
    "stretch", "mobility", "steps", "walking", "running", "cardio",
    "strength", "back pain", "neck pain", "core", "ergonomics", "breathing",
    "sunlight", "recovery", "flexibility", "doctor", "clinic", "hospital",
    "medical", "therapy", "physio", "physiotherapy", "gut", "digestion",
    "immune", "vitamin", "mineral", "energy", "fatigue", "weight",
    "obesity", "anxiety", "depression", "mental health", "brain", "focus",
    "memory", "eye", "skin", "hair", "posture", "spine", "joint",
    "inflammation", "sugar", "insulin", "cortisol", "hormone", "fasting",
    "intermittent", "cold shower", "morning", "evening", "circadian",
    "wake up", "alarm", "nap", "deep sleep", "REM", "melatonin",
]


def is_english(text: str, threshold: float = 0.80) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    s = re.sub(r"https?://\S+", " ", s)
    if _LANGDETECT_OK:
        try:
            langs = detect_langs(s)
            if langs:
                top = max(langs, key=lambda x: x.prob)
                if top.lang == "en" and top.prob >= threshold:
                    return True
        except Exception:
            pass
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    ascii_r = sum(1 for c in letters if ord(c) < 128) / len(letters)
    has_stop = any((" " + w + " ") in (" " + s.lower() + " ") for w in EN_STOP)
    return ascii_r >= 0.95 and has_stop


def has_health_signal(title: str, desc: str = "", tags: list | None = None) -> bool:
    blob = " ".join([title or "", desc or "", " ".join(tags or [])]).lower()
    return any(kw in blob for kw in HEALTH_KEYWORDS)


def clean_to_topic(title: str) -> str:
    s = (title or "").strip()
    s = re.sub(r"(?i)#?shorts?", "", s)
    s = re.sub(r"\[[^\]]+\]|\([^)]+\)", "", s)
    s = re.split(r"\s+[|\-–—]\s+", s)[0].strip()
    s = re.sub(r"\s+", " ", s).strip(" -–—:|")
    if s:
        s = s[0].upper() + s[1:]
    return s[:120].rstrip()


# ── YouTube client ─────────────────────────────────────────────────────────────

def _ensure_google():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        return Credentials, build
    except Exception:
        import subprocess
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "-q", "--upgrade",
                "google-api-python-client", "google-auth",
                "google-auth-oauthlib", "packaging>=23.1",
            ],
            check=True,
        )
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        return Credentials, build


def yt_client():
    for v in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        if not os.getenv(v):
            raise RuntimeError(f"Missing secret: {v}")
    Credentials, build = _ensure_google()
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.readonly"],
    )
    return build("youtube", "v3", credentials=creds)


# ── Source A: YouTube Trending ─────────────────────────────────────────────────

def fetch_yt_trending(region_pref: str = "IN", cap: int = 80, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    try:
        yt = yt_client()
    except Exception as exc:
        log.warning("YT client: %s", exc)
        return []

    regions = [region_pref] + [r for r in REGION_ROTATION if r != region_pref]
    collected: list[str] = []
    seen: set[str] = set()

    for rc in regions:
        try:
            resp = yt.videos().list(
                part="snippet", chart="mostPopular",
                regionCode=rc, maxResults=50,
            ).execute()
        except Exception as exc:
            log.warning("YT trending %s: %s", rc, exc)
            continue
        for it in resp.get("items", []):
            sn = it.get("snippet", {}) or {}
            title = sn.get("title") or ""
            desc  = sn.get("description") or ""
            tags  = sn.get("tags") or []
            if not is_english(title + " " + desc):
                continue
            if not has_health_signal(title, desc, tags):
                continue
            topic = clean_to_topic(title)
            if not topic or len(normalize_key(topic)) < 6:
                continue
            nk = normalize_key(topic)
            if nk in seen or any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                continue
            seen.add(nk)
            collected.append(topic)
        if len(collected) >= cap:
            break

    log.info("YT Trending → %d", len(collected))
    return collected[:cap]


# ── Source B: YouTube Search ───────────────────────────────────────────────────

SEARCH_SEEDS = [
    "how to wake up early naturally",
    "best sleep techniques for deep sleep",
    "morning routine for energy",
    "gut health improvement tips",
    "how to reduce cortisol naturally",
    "intermittent fasting benefits",
    "posture correction exercises",
    "breathing exercises for stress",
    "how to boost immunity naturally",
    "cold shower benefits science",
    "how to improve focus and memory",
    "best foods for brain health",
    "how to fix neck pain at desk",
    "circadian rhythm reset tips",
    "how to fall asleep in 2 minutes",
    "best stretches for back pain",
    "how to increase energy without caffeine",
    "anti-inflammatory foods list",
    "how to reduce belly fat science",
    "mental health daily habits",
]


def fetch_yt_search(region_pref: str = "IN", days: int = 14, cap: int = 80, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    try:
        yt = yt_client()
    except Exception as exc:
        log.warning("YT client (search): %s", exc)
        return []

    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    regions = list(dict.fromkeys([region_pref] + REGION_ROTATION))
    found: list[str] = []
    seen: set[str] = set()

    for rc in regions[:3]:
        for q in SEARCH_SEEDS:
            try:
                resp = yt.search().list(
                    part="snippet", q=q, type="video",
                    order="viewCount", publishedAfter=cutoff,
                    maxResults=10, regionCode=rc, relevanceLanguage="en",
                ).execute()
            except Exception as exc:
                log.warning("YT search [%s] '%s': %s", rc, q, exc)
                continue
            for it in resp.get("items", []):
                sn = it.get("snippet") or {}
                title = sn.get("title") or ""
                desc  = sn.get("description") or ""
                if not is_english(title):
                    continue
                if not has_health_signal(title, desc):
                    continue
                topic = clean_to_topic(title)
                if not topic or len(normalize_key(topic)) < 6:
                    continue
                nk = normalize_key(topic)
                if nk in seen or any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                    continue
                seen.add(nk)
                found.append(topic)
            if len(found) >= cap:
                break
        if len(found) >= cap:
            break

    log.info("YT Search → %d", len(found))
    return found[:cap]


# ── Source C: Top Health Channels ─────────────────────────────────────────────

TOP_CHANNELS = [
    "Doctor Mike", "Andrew Huberman", "Peter Attia MD",
    "Mayo Clinic", "Cleveland Clinic", "NHS",
    "Jeff Nippard", "Jeremy Ethier", "Yoga With Adriene",
    "Thomas DeLauer", "Dr. Eric Berg DC", "Dr. Rangan Chatterjee",
    "Well+Good", "mindbodygreen", "Healthline",
]


def fetch_top_channels(days: int = 14, cap: int = 60, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    try:
        yt = yt_client()
    except Exception as exc:
        log.warning("YT client (channels): %s", exc)
        return []

    cutoff = datetime.utcnow() - timedelta(days=days)
    collected: list[str] = []
    seen: set[str] = set()

    for name in TOP_CHANNELS:
        # find channel id
        try:
            r = yt.search().list(part="snippet", q=name, type="channel", maxResults=1).execute()
            items = r.get("items") or []
            if not items:
                continue
            cid = items[0]["snippet"]["channelId"]
        except Exception:
            continue
        # uploads playlist
        try:
            r2 = yt.channels().list(part="contentDetails", id=cid, maxResults=1).execute()
            pid = (
                (r2.get("items") or [{}])[0]
                .get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            if not pid:
                continue
        except Exception:
            continue
        try:
            r3 = yt.playlistItems().list(
                part="snippet", playlistId=pid, maxResults=10
            ).execute()
        except Exception:
            continue
        for it in r3.get("items", []):
            sn = it.get("snippet") or {}
            title = sn.get("title") or ""
            desc  = sn.get("description") or ""
            pub   = sn.get("publishedAt") or ""
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt.replace(tzinfo=None) < cutoff:
                    continue
            except Exception:
                pass
            if not is_english(title):
                continue
            if not has_health_signal(title, desc):
                continue
            topic = clean_to_topic(title)
            if not topic or len(normalize_key(topic)) < 6:
                continue
            nk = normalize_key(topic)
            if nk in seen or any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                continue
            seen.add(nk)
            collected.append(topic)
        if len(collected) >= cap:
            break

    log.info("Top Channels → %d", len(collected))
    return collected[:cap]


# ── Source D: Google Trends ────────────────────────────────────────────────────

TREND_SEEDS = [
    "sleep", "wake up early", "gut health", "stress relief",
    "morning routine", "posture", "breathing", "hydration",
    "intermittent fasting", "cold shower", "brain health",
    "back pain", "neck pain", "immunity", "energy boost",
    "anxiety relief", "focus", "memory", "weight loss", "metabolism",
]


def fetch_google_trends(region: str = "IN", cap: int = 40, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    topics: list[str] = []
    seen:   set[str]  = set()
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-IN", tz=330, timeout=(10, 30), retries=3, backoff_factor=0.5)
    except Exception as exc:
        log.warning("pytrends init: %s", exc)
        return []

    # realtime
    try:
        df = pt.realtime_trending_searches(pn=region)
        if df is not None and "title" in df.columns:
            for t in df["title"].tolist():
                if not isinstance(t, str):
                    continue
                if not is_english(t) or not has_health_signal(t):
                    continue
                topic = clean_to_topic(t)
                nk = normalize_key(topic)
                if not topic or nk in seen or any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                    continue
                seen.add(nk)
                topics.append(topic)
    except Exception as exc:
        log.warning("pytrends realtime: %s", exc)

    # related queries per seed
    for seed in TREND_SEEDS:
        if len(topics) >= cap:
            break
        try:
            pt.build_payload([seed], timeframe="now 7-d", geo=region)
            rq = pt.related_queries() or {}
            for kind in ("rising", "top"):
                df2 = (rq.get(seed) or {}).get(kind)
                if df2 is None or "query" not in df2.columns:
                    continue
                for q in df2.head(10)["query"].tolist():
                    if not isinstance(q, str):
                        continue
                    if not is_english(q) or not has_health_signal(q):
                        continue
                    topic = clean_to_topic(q)
                    nk = normalize_key(topic)
                    if not topic or nk in seen or any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                        continue
                    seen.add(nk)
                    topics.append(topic)
                    if len(topics) >= cap:
                        break
        except Exception as exc:
            log.warning("pytrends related [%s]: %s", seed, exc)
        time.sleep(0.6)

    log.info("Google Trends → %d", len(topics))
    return topics[:cap]


# ── Source E: Reddit ───────────────────────────────────────────────────────────

REDDIT_SUBS = [
    "health", "Fitness", "sleep", "nutrition", "loseit",
    "bodyweightfitness", "intermittentfasting", "yoga",
    "running", "flexibility", "Anxiety", "mentalhealth",
]


def fetch_reddit(cap: int = 40, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    topics: list[str] = []
    seen:   set[str]  = set()

    # Use pushshift-compatible JSON endpoint (no API key needed)
    headers = {"User-Agent": "HealthShortsBot/1.0"}
    for sub in REDDIT_SUBS:
        if len(topics) >= cap:
            break
        for sort in ("hot", "top"):
            url = f"https://www.reddit.com/r/{sub}/{sort}.json"
            try:
                r = requests.get(
                    url, headers=headers,
                    params={"limit": 25, "t": "week"},
                    timeout=20,
                )
                if r.status_code != 200:
                    continue
                children = r.json().get("data", {}).get("children", [])
            except Exception as exc:
                log.warning("Reddit [%s/%s]: %s", sub, sort, exc)
                continue
            for child in children:
                d = child.get("data") or {}
                title  = d.get("title") or ""
                selftext = d.get("selftext") or ""
                # skip non-question / low-signal posts
                if not is_english(title):
                    continue
                if not has_health_signal(title, selftext):
                    continue
                # prefer question-style posts — more specific
                topic = clean_to_topic(title)
                nk = normalize_key(topic)
                if not topic or len(nk) < 8 or nk in seen:
                    continue
                if any(jaccard(topic, ex) >= 0.75 for ex in exclude):
                    continue
                seen.add(nk)
                topics.append(topic)
                if len(topics) >= cap:
                    break
            time.sleep(0.5)  # be polite to Reddit
        if len(topics) >= cap:
            break

    log.info("Reddit → %d", len(topics))
    return topics[:cap]


# ── LLM topic refinement ───────────────────────────────────────────────────────

_REFINE_PROMPT = """\
You are a YouTube Shorts research analyst specialising in health & wellness.

Below is a list of raw health topic signals gathered from YouTube, Reddit, and
Google Trends. Your job is to convert them into SPECIFIC, ACTIONABLE, HIGH-CTR
YouTube Shorts topic titles that a general audience will click on.

Rules:
- Each output topic must be a *specific technique or habit* (not a vague keyword).
  BAD : "sleep"        GOOD: "The 4-7-8 breathing trick that puts you to sleep in 2 minutes"
  BAD : "back pain"    GOOD: "3 desk stretches that erase lower back pain in 5 minutes"
  BAD : "gut health"   GOOD: "The morning drink that fixes bloating in 3 days"
- Every topic must be strictly health/wellness — no drugs, no diseases, no dosages.
- Output ONLY a JSON array of exactly 6 strings (the refined topic titles).
- No numbering, no extra keys, no markdown — raw JSON array only.

Raw signals:
{signals}
"""


def refine_topics_with_llm(raw: list[str]) -> list[str]:
    """Use Groq LLM to turn raw signals into specific, high-CTR topic titles."""
    if not GROQ_API_KEY or not raw:
        return raw[:6]
    signals_text = "\n".join(f"- {t}" for t in raw[:20])
    prompt = _REFINE_PROMPT.format(signals=signals_text)
    models = [GROQ_MODEL, "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    for model in models:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.85,
                },
                timeout=60,
            )
            if r.status_code != 200:
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            # extract JSON array
            m = re.search(r"\[.*\]", content, re.S)
            if not m:
                continue
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and arr:
                return [str(t).strip() for t in arr if str(t).strip()][:6]
        except Exception as exc:
            log.warning("LLM refine [%s]: %s", model, exc)
    return raw[:6]


# ── Aggregator ─────────────────────────────────────────────────────────────────

def gather_topics(region_pref: str = "IN", need: int = 6, exclude: set | None = None) -> list[str]:
    exclude = exclude or set()
    pool: list[str] = []
    seen: set[str]  = set()

    def _add(source: list[str]) -> None:
        for t in source:
            nk = normalize_key(t)
            if nk not in seen and not any(jaccard(t, ex) >= 0.75 for ex in exclude):
                seen.add(nk)
                pool.append(t)

    _add(fetch_yt_trending(region_pref, cap=60, exclude=exclude))
    if len(pool) < need:
        _add(fetch_yt_search(region_pref, cap=60, exclude=exclude | set(pool)))
    if len(pool) < need:
        _add(fetch_top_channels(cap=40, exclude=exclude | set(pool)))
    if len(pool) < need:
        _add(fetch_google_trends(region_pref, cap=40, exclude=exclude | set(pool)))
    if len(pool) < need:
        _add(fetch_reddit(cap=40, exclude=exclude | set(pool)))

    # Shuffle to avoid always picking the same source-A topics
    random.shuffle(pool)
    # LLM refinement: turn vague signals into specific, actionable titles
    refined = refine_topics_with_llm(pool[:20])
    return refined[:need]


# ── Issue creation ─────────────────────────────────────────────────────────────

DEFAULT_PROMPT_TEMPLATE = """\
Act as a viral YouTube Shorts scriptwriter specializing in health content.

Write a script for a YouTube Short (under 60 seconds, ~130-150 words spoken)
on the topic: "{topic}"

Structure the flow internally (do not label or show these sections in the output):
- Open with a scroll-stopping hook (shocking stat, myth-bust, or bold question).
  No greetings — start mid-thought.
- Name the relatable pain point the viewer feels right now.
- Deliver a surprising cause or myth-busting reveal.
- Give 2-3 punchy, specific, actionable tips. Short sentences (max 8-10 words).
- End with a loop-back to the hook or a final twist for rewatch value.
- Close with one natural, non-salesy CTA (follow, comment a word, or save this).

Rules:
- Spoken, punchy, simple language — no complex clauses.
- No medical guarantees, but keep tone confident and energetic.
- Total script must read aloud in under 60 seconds.

Output ONLY the plain spoken script as continuous text.
"""


def create_topic_issue(
    owner: str,
    repo: str,
    topics: list[str],
    scheduled_ist: datetime,
    note: str = "",
) -> None:
    slot_label = f"slot:{SLOT}"
    ensure_labels(owner, repo)

    shown = topics[:6]
    if shown:
        numbered = "\n".join(f"{i}) {t}" for i, t in enumerate(shown, 1))
        opts_str  = "/".join(str(i) for i in range(1, len(shown) + 1))
        guidance  = ""
    else:
        numbered = "(No eligible topics found — all sources returned empty.)"
        opts_str  = "N/A"
        guidance  = (
            "\nNote: Reply with /reject-topic to retry, "
            "or /custom-topic Your Specific Topic."
        )
    if note:
        guidance += f"\n\nDebug: {note}"

    title = (
        f"Topic approval — {SLOT} slot "
        f"({scheduled_ist.strftime('%Y-%m-%d %H:%M')} IST)"
    )
    default_prompt_placeholder = DEFAULT_PROMPT_TEMPLATE.format(topic="[TOPIC WILL BE INSERTED]")

    body = f"""## Proposed Health Short Topics
Trending, specific, actionable topics (multi-source: YouTube Trending + Search + Top Channels + Google Trends + Reddit).
Scheduled publish (IST): **{scheduled_ist.strftime('%Y-%m-%d %H:%M')}**

### Choose one:
{numbered}{guidance}

---
**Reply with:**
- `/approve-topic 1` (or {opts_str}) — approve a numbered topic
- `/reject-topic` — get fresh topic suggestions
- `/custom-topic Your Specific Topic` — use your own topic

> After topic approval, the bot will ask you for your **script prompt**.
> You can customise it or just reply `/use-default-prompt` to use the built-in template.

---

### Default script prompt (will be shown again after topic approval):
{default_prompt_placeholder}
  
{METADATA_SENTINEL if False else ""}
"""
    import json as _json
    meta_block = set_metadata_in_issue_body(
        body,
        {
            "slot": SLOT,
            "scheduled_ist": scheduled_ist.strftime("%Y-%m-%d %H:%M"),
            "topics": shown,
        },
    )

    log.info("Creating issue: %s", title)
    gh(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        json={
            "title": title,
            "body": meta_block,
            "labels": ["await-topic-approval", slot_label],
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Region=%s Slot=%s Repo=%s", REGION, SLOT, REPO)
    if not REPO or "/" not in REPO:
        print("GITHUB_REPOSITORY not set."); sys.exit(2)
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not available."); sys.exit(2)

    owner, repo = REPO.split("/", 1)

    # Guard: skip if an open approval issue already exists for this slot
    try:
        open_slot = open_issues_with_labels(owner, repo, [f"slot:{SLOT}", "await-topic-approval"])
    except requests.HTTPError as exc:
        print("Failed to query issues:", exc); sys.exit(4)
    if open_slot:
        print("An approval issue for this slot is already open. Skipping.")
        return

    approved = load_recent_approved_topics(owner, repo)

    try:
        candidates = gather_topics(REGION, need=6, exclude=approved)
    except Exception as exc:
        log.error("Aggregator error: %s", exc)
        candidates = []

    scheduled_ist = next_slot_ist()
    note = "" if candidates else "All sources returned 0 eligible topics."
    try:
        create_topic_issue(owner, repo, candidates, scheduled_ist, note=note)
        print(f"Created topic approval issue for {SLOT} slot.")
    except requests.HTTPError as exc:
        body_txt = exc.response.text if exc.response is not None else str(exc)
        print("Failed to create issue:", body_txt)
        sys.exit(5)


if __name__ == "__main__":
    main()

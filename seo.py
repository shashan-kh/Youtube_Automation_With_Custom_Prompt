"""
seo.py — SEO title, description, and 350-tag generation for YouTube Shorts.
All LLM-powered, topic-specific, viral-optimised.
"""
from __future__ import annotations

import os
import re
import json
import requests
from common import get_logger, extract_json_block

log = get_logger("seo")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct",
]

# ── Evergreen base tags always appended ───────────────────────────────────────
_BASE_TAGS = [
    "youtube shorts", "shorts", "health", "wellness", "healthy habits",
    "health tips", "daily habits", "lifestyle", "fitness", "nutrition",
    "natural health", "holistic health", "healthy living", "self care",
    "wellbeing", "mind body", "preventive health", "health education",
    "health facts", "health shorts", "viral health", "health motivation",
    "health hack", "life hack", "productivity", "morning routine",
    "evening routine", "sleep better", "sleep tips", "deep sleep",
    "stress relief", "anxiety relief", "mental health", "mental wellness",
    "brain health", "focus tips", "memory improvement", "gut health",
    "digestion", "immune system", "vitamin tips", "mineral rich foods",
    "anti inflammatory", "inflammation", "hydration", "water intake",
    "posture", "spine health", "back pain relief", "neck pain relief",
    "stretching", "mobility", "flexibility", "yoga", "meditation",
    "breathing exercises", "mindfulness", "cold shower benefits",
    "morning sunlight", "circadian rhythm", "melatonin", "cortisol",
    "hormone balance", "intermittent fasting", "weight loss tips",
    "metabolism boost", "energy boost", "fatigue relief", "no caffeine",
    "healthy diet", "balanced diet", "plant based", "protein intake",
    "fiber foods", "healthy snacks", "meal prep", "clean eating",
    "sugar free", "insulin resistance", "blood sugar", "heart health",
    "cardio fitness", "strength training", "bodyweight exercise",
    "home workout", "gym tips", "walking benefits", "running tips",
    "steps per day", "active lifestyle", "sedentary lifestyle", "desk job health",
    "ergonomics", "eye strain", "screen time", "digital detox",
    "skin health", "hair health", "bone health", "joint pain",
    "india health", "indian wellness", "ayurveda", "desi health tips",
    "shorts india", "health india", "wellness india",
    "educational", "informative", "science backed", "evidence based",
    "doctor tips", "medical facts", "health myths busted",
    "health 2024", "health 2025", "trending health", "viral wellness",
    "reels", "short video", "quick tips", "60 second health",
]


# ── Prompt ────────────────────────────────────────────────────────────────────

_SEO_PROMPT = """\
You are an elite YouTube SEO strategist and viral content specialist.

Topic: "{topic}"
Script excerpt (first 200 chars for context): "{script_excerpt}"

Generate a complete YouTube Shorts SEO package. Return ONLY a raw JSON object
with these exact keys (no markdown, no extra text):

{{
  "title": "<video title>",
  "description": "<video description>",
  "topic_tags": ["<tag1>", "<tag2>", ... ]
}}

TITLE rules:
- Start with the primary keyword (first 3 words = most important keyword phrase).
- Use Title Case.
- Include ONE of these power words: Secret, Science, Proven, Hack, Fix, Boost,
  Never, Always, Stop, Start, Why, How, Best, Worst, Simple, Instant.
- End with " #Shorts".
- Max 90 characters total.
- NO emojis. English only.

DESCRIPTION rules:
- Line 1 (first 157 chars shown before "show more"): primary keyword + bold hook
  sentence that makes them want to watch.
- Lines 2-4: 2-3 sentences expanding the value. Include the main keyword at
  least twice in natural language.
- Line 5: One credible source reference (WHO / CDC / NIH / NHS / PubMed) if
  relevant to the topic.
- Line 6: CTA — "Like & subscribe for daily health tips."
- Line 7: Disclaimer — "Educational only, not medical advice."
- After the disclaimer, add a blank line then a dense block of 350 hashtag tags
  all on separate lines starting from #1 most relevant to #350 least relevant.
  Tags must be a MIX of:
    * ultra-specific (e.g. #WakeUpEarlyTips, #CircadianRhythmReset)
    * medium (e.g. #SleepHacks, #MorningRoutine)
    * broad (#Health, #Wellness, #Shorts, #YouTubeShorts)
  No duplicates. No spaces inside a hashtag. CamelCase preferred.
  English only. No emojis.
- Total description (excluding tags) max 800 chars.

TOPIC_TAGS rules (for the YouTube tags field, NOT the description):
- 12 short, comma-style keyword phrases (no # symbol).
- Most specific first.
- English only.
"""


def _call_groq(prompt: str, model: str) -> requests.Response:
    return requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.75,
        },
        timeout=120,
    )


def generate_seo_package(topic: str, script_text: str) -> dict:
    """
    Returns:
        {
            "title":        str,          # ≤90 chars, #Shorts at end
            "description":  str,          # full description with 350 #hashtags at bottom
            "tags_csv":     str,          # 12 YT tags, comma-separated
        }
    """
    excerpt = (script_text or "")[:200].replace("\n", " ")
    prompt  = _SEO_PROMPT.format(topic=topic, script_excerpt=excerpt)

    models = [GROQ_MODEL] + [m for m in FALLBACK_MODELS if m != GROQ_MODEL]
    last_err: Exception | None = None

    for model in models:
        try:
            r = _call_groq(prompt, model)
            if r.status_code != 200:
                last_err = RuntimeError(f"Groq {model} → HTTP {r.status_code}: {r.text[:300]}")
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            raw = extract_json_block(content) or content
            data = json.loads(raw)

            title       = _clean_title(data.get("title", ""), topic)
            description = _assemble_description(data.get("description", ""), topic, data.get("topic_tags", []))
            tags_csv    = _clean_tags(data.get("topic_tags", []), topic)

            return {"title": title, "description": description, "tags_csv": tags_csv}

        except Exception as exc:
            last_err = exc
            log.warning("SEO gen [%s]: %s", model, exc)
            continue

    # Fallback: basic handcrafted SEO
    log.error("All SEO models failed (%s). Using fallback.", last_err)
    return _fallback_seo(topic, script_text)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_title(raw: str, topic: str) -> str:
    t = (raw or "").strip()
    # Remove emojis (non-ASCII)
    t = "".join(c for c in t if ord(c) < 128)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = topic
    # Ensure #Shorts
    if "#Shorts" not in t:
        t = t.rstrip(" .!?,;:") + " #Shorts"
    # Enforce length
    if len(t) > 90:
        keep = 90 - len(" #Shorts")
        t = t[:keep].rstrip() + " #Shorts"
    return t


def _assemble_description(raw_desc: str, topic: str, topic_tags: list) -> str:
    """Ensure description has proper structure + 350 hashtags at the bottom."""
    desc = (raw_desc or "").strip()
    desc = "".join(c for c in desc if ord(c) < 128)  # ASCII only

    # Ensure disclaimer
    disclaimer = "Educational only, not medical advice."
    if disclaimer.lower() not in desc.lower():
        desc = desc.rstrip() + "\n" + disclaimer

    # Ensure CTA
    if "like & subscribe" not in desc.lower() and "like and subscribe" not in desc.lower():
        desc = desc.rstrip() + "\nLike & subscribe for daily health tips."

    # Count existing hashtags in desc
    existing_hashtags = set(re.findall(r"#\w[\w-]*", desc))

    # Build 350 hashtags: topic-specific ones first, then base tags
    topic_specific = _generate_topic_hashtags(topic)
    all_tags: list[str] = []
    seen_lower: set[str] = set()

    for ht in topic_specific + _BASE_TAGS:
        ht_clean = _to_hashtag(ht)
        if not ht_clean:
            continue
        key = ht_clean.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        all_tags.append(ht_clean)
        if len(all_tags) >= 350:
            break

    # Pad with numbered variants if still short
    if len(all_tags) < 350:
        padders = [
            f"#HealthTip{i}" for i in range(1, 400)
            if f"#healthtip{i}" not in seen_lower
        ]
        for p in padders:
            all_tags.append(p)
            if len(all_tags) >= 350:
                break

    tag_block = "\n".join(all_tags[:350])
    return desc.rstrip() + "\n\n" + tag_block


def _to_hashtag(s: str) -> str:
    """Convert a string to a valid CamelCase hashtag."""
    s = (s or "").strip().lstrip("#")
    # Remove non-alphanumeric except spaces
    s = re.sub(r"[^a-zA-Z0-9 ]", "", s)
    words = s.split()
    if not words:
        return ""
    return "#" + "".join(w.capitalize() for w in words)


def _generate_topic_hashtags(topic: str) -> list[str]:
    """
    Generate ~200 topic-specific hashtag variations from the topic string.
    These are rule-based (no LLM call) so they are instant and free.
    """
    words = re.findall(r"[a-zA-Z0-9]+", topic)
    tags: list[str] = []

    # Full topic as one tag
    full = "".join(w.capitalize() for w in words)
    tags.append(f"#{full}")

    # Bi-grams
    for i in range(len(words) - 1):
        pair = words[i].capitalize() + words[i + 1].capitalize()
        tags.append(f"#{pair}")

    # Individual words ≥4 chars
    for w in words:
        if len(w) >= 4:
            tags.append(f"#{w.capitalize()}")

    # Common suffix/prefix variations
    root_word = words[0].capitalize() if words else "Health"
    suffixes = [
        "Tips", "Hacks", "Facts", "Tricks", "Benefits", "Science",
        "Routine", "Habit", "Method", "Technique", "Guide", "101",
        "ForBeginners", "At Home", "Daily", "Morning", "Night",
        "ForMen", "ForWomen", "ForSeniors", "ForStudents",
        "In5Minutes", "In2Minutes", "Instantly", "Naturally",
        "Without Medicine", "WithoutDrugs", "Scientifically",
        "Backed", "Proven", "Evidence", "Research",
    ]
    for suf in suffixes:
        tags.append(f"#{full}{suf.replace(' ', '')}")
        tags.append(f"#{root_word}{suf.replace(' ', '')}")

    # Topic-adjacent health tags
    adjacent = [
        "HealthyHabits", "HealthyLifestyle", "HealthyLiving",
        "WellnessTips", "WellnessJourney", "WellnessRoutine",
        "FitnessMotivation", "FitnessTips", "FitnessLife",
        "NutritionTips", "NutritionFacts", "NutritionScience",
        "SelfCare", "SelfCareRoutine", "SelfCareDay",
        "MindBodySoul", "MindBodyConnection", "MindBodyHealth",
        "NaturalHealth", "NaturalRemedies", "NaturalWellness",
        "PreventiveHealth", "PreventiveCare", "PreventiveMedicine",
        "HealthyMindset", "HealthyBody", "HealthyFood",
        "CleanEating", "EatHealthy", "EatClean",
        "ViralHealth", "TrendingHealth", "HealthViral",
        "HealthShorts", "WellnessShorts", "FitnessShorts",
        "HealthContent", "HealthCreator", "HealthInfluencer",
        "DailyHealth", "DailyWellness", "DailyFitness",
        "HealthGoals", "WellnessGoals", "FitnessGoals",
        "HealthIsWealth", "YourHealth", "MyHealth",
        "GetHealthy", "StayHealthy", "BeHealthy",
        "HealthMatters", "HealthFirst", "PutHealthFirst",
        "ScienceBackedHealth", "EvidenceBasedHealth",
        "DoctorApproved", "MedicalFacts", "HealthFacts",
        "HealthMyths", "MythBusted", "HealthTruth",
        "HealthAlert", "HealthWarning", "HealthReminder",
        "HealthChallenge", "HealthJourney", "HealthTransformation",
        "IndiaHealth", "IndianWellness", "IndianHealth",
        "DesiHealth", "DesiWellness", "AyurvedaTips",
        "YoutubeShorts", "Shorts", "ShortVideo",
        "ViralShort", "TrendingShorts", "ReelsHealth",
        "60Seconds", "QuickTips", "FastFacts",
        "LearnSomethingNew", "DidYouKnow", "MindBlown",
        "HealthMindBlown", "WellnessWednesday", "HealthMonday",
        "TipOfTheDay", "FactOfTheDay", "HabitOfTheDay",
    ]
    tags.extend([f"#{t}" for t in adjacent])
    return tags


def _clean_tags(raw: list, topic: str) -> str:
    """Clean the 12 YouTube tags field (no # symbols, comma-separated)."""
    out: list[str] = []
    seen: set[str] = set()

    topic_words = re.findall(r"[a-zA-Z0-9]+", topic)
    candidates = list(raw) + [topic] + [" ".join(topic_words[:3])]

    for t in candidates:
        t = (str(t) or "").strip().lstrip("#")
        t = re.sub(r"[^a-zA-Z0-9 ]", "", t).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
        if len(out) >= 12:
            break

    return ",".join(out[:12])


def _fallback_seo(topic: str, script_text: str) -> dict:
    """Minimal handcrafted SEO when all LLM calls fail."""
    title = f"{topic[:75]} #Shorts"
    description = (
        f"{topic} — quick science-backed tips for your daily wellness routine.\n"
        "Like & subscribe for daily health tips.\n"
        "Educational only, not medical advice.\n\n"
        + "\n".join(_generate_topic_hashtags(topic)[:350])
    )
    tags_csv = ",".join(
        re.findall(r"[a-zA-Z0-9]+", topic)[:6]
        + ["health", "wellness", "shorts", "healthy habits", "fitness", "tips"]
    )[:12]
    return {"title": title, "description": description, "tags_csv": tags_csv}

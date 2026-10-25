"""
seo.py — SEO title, description, and 350-tag generation for YouTube Shorts.
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
    "steps per day", "active lifestyle", "sedentary lifestyle",
    "desk job health", "ergonomics", "eye strain", "screen time",
    "digital detox", "skin health", "hair health", "bone health",
    "joint pain", "india health", "indian wellness", "ayurveda",
    "desi health tips", "shorts india", "health india", "wellness india",
    "educational", "informative", "science backed", "evidence based",
    "doctor tips", "medical facts", "health myths busted",
    "health 2025", "trending health", "viral wellness",
    "reels", "short video", "quick tips", "60 second health",
]

_SEO_PROMPT = """\
You are an elite YouTube SEO strategist for health content.

Topic: "{topic}"
Script excerpt: "{script_excerpt}"

Return ONLY a valid JSON object with exactly these three keys.
No markdown fences, no extra text, no trailing commas.

{{
  "title": "keyword-first title in Title Case, max 90 chars, ends with #Shorts, no emojis",
  "description": "2-4 sentence description. First 157 chars must contain main keyword. Include CTA and disclaimer: Educational only, not medical advice.",
  "topic_tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10", "tag11", "tag12"]
}}

Title rules:
- First 3 words must be the main keyword phrase
- Include one power word: Secret, Science, Proven, Hack, Fix, Boost, Stop, Start, Why, How, Best, Simple
- End with #Shorts
- Max 90 characters
- No emojis, English only

Description rules:
- Keep under 800 characters (excluding hashtags)
- First line: hook sentence with main keyword
- Include: Like and subscribe for daily health tips.
- Include: Educational only, not medical advice.
- English only, no emojis

topic_tags rules:
- Exactly 12 items
- Short keyword phrases, no # symbol
- Most specific first
- English only
"""


def _call_groq(prompt: str, model: str) -> requests.Response:
    return requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=120,
    )


def generate_seo_package(topic: str, script_text: str) -> dict:
    """
    Returns dict with keys: title, description, tags_csv
    Never raises — always returns a valid fallback.
    """
    excerpt = (script_text or "")[:200].replace("\n", " ")
    # Remove any characters that could break JSON parsing
    excerpt = re.sub(r'["\\\x00-\x1f]', " ", excerpt)
    topic_safe = re.sub(r'["\\\x00-\x1f]', " ", topic or "")

    prompt  = _SEO_PROMPT.format(
        topic=topic_safe,
        script_excerpt=excerpt,
    )

    models = [GROQ_MODEL] + [m for m in FALLBACK_MODELS if m != GROQ_MODEL]

    for model in models:
        try:
            log.info("[SEO] trying model: %s", model)
            r = _call_groq(prompt, model)
            if r.status_code != 200:
                log.warning("[SEO] model %s HTTP %s", model, r.status_code)
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            log.debug("[SEO] raw response: %s", content[:300])

            # Try to extract JSON
            raw = extract_json_block(content) or content
            # Strip any BOM or weird chars before parsing
            raw = raw.strip().lstrip("\ufeff")
            data = json.loads(raw)

            title       = _clean_title(data.get("title", ""), topic)
            description = _assemble_description(
                data.get("description", ""), topic, data.get("topic_tags", [])
            )
            tags_csv    = _clean_tags(data.get("topic_tags", []), topic)

            log.info("[SEO] success with model: %s", model)
            return {
                "title": title,
                "description": description,
                "tags_csv": tags_csv,
            }

        except json.JSONDecodeError as exc:
            log.warning("[SEO] JSON parse error [%s]: %s", model, exc)
            continue
        except Exception as exc:
            log.warning("[SEO] error [%s]: %s", model, exc)
            continue

    log.error("[SEO] all models failed, using fallback")
    return _fallback_seo(topic, script_text)


def _clean_title(raw: str, topic: str) -> str:
    t = (raw or "").strip()
    t = re.sub(r"[^\x00-\x7F]", "", t)   # ASCII only
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = topic or "Health Tips"
    if "#Shorts" not in t:
        t = t.rstrip(" .!?,;:") + " #Shorts"
    if len(t) > 90:
        keep = 90 - len(" #Shorts")
        t = t[:keep].rstrip() + " #Shorts"
    return t


def _assemble_description(raw_desc: str, topic: str, topic_tags: list) -> str:
    desc = (raw_desc or "").strip()
    desc = re.sub(r"[^\x00-\x7F]", "", desc)  # ASCII only

    disclaimer = "Educational only, not medical advice."
    if disclaimer.lower() not in desc.lower():
        desc = desc.rstrip() + "\n" + disclaimer

    if "like" not in desc.lower() and "subscribe" not in desc.lower():
        desc = desc.rstrip() + "\nLike and subscribe for daily health tips."

    # Build 350 hashtags
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

    # Pad if needed
    if len(all_tags) < 350:
        for i in range(1, 500):
            candidate = f"#HealthTip{i}"
            if candidate.lower() not in seen_lower:
                all_tags.append(candidate)
                seen_lower.add(candidate.lower())
            if len(all_tags) >= 350:
                break

    tag_block = "\n".join(all_tags[:350])
    return desc.rstrip() + "\n\n" + tag_block


def _to_hashtag(s: str) -> str:
    s = (s or "").strip().lstrip("#")
    s = re.sub(r"[^a-zA-Z0-9 ]", "", s)
    words = s.split()
    if not words:
        return ""
    return "#" + "".join(w.capitalize() for w in words)


def _generate_topic_hashtags(topic: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9]+", topic or "")
    tags: list[str] = []

    if words:
        full = "".join(w.capitalize() for w in words)
        tags.append(f"#{full}")

        for i in range(len(words) - 1):
            pair = words[i].capitalize() + words[i + 1].capitalize()
            tags.append(f"#{pair}")

        for w in words:
            if len(w) >= 4:
                tags.append(f"#{w.capitalize()}")

        root = words[0].capitalize()
        suffixes = [
            "Tips", "Hacks", "Facts", "Tricks", "Benefits", "Science",
            "Routine", "Habit", "Method", "Technique", "Guide", "101",
            "ForBeginners", "AtHome", "Daily", "Morning", "Night",
            "ForMen", "ForWomen", "ForSeniors", "ForStudents",
            "In5Minutes", "In2Minutes", "Instantly", "Naturally",
            "WithoutMedicine", "Scientifically", "Backed", "Proven",
            "EvidenceBased", "Research",
        ]
        full_no_space = full
        for suf in suffixes:
            tags.append(f"#{full_no_space}{suf}")
            tags.append(f"#{root}{suf}")

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
    out: list[str] = []
    seen: set[str] = set()
    topic_words = re.findall(r"[a-zA-Z0-9]+", topic or "")
    candidates  = list(raw or []) + [topic] + [" ".join(topic_words[:3])]
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
    safe_topic = re.sub(r"[^\x00-\x7F]", "", topic or "Health Tips")
    title = f"{safe_topic[:75]} #Shorts"
    description = (
        f"{safe_topic} - quick science-backed wellness tips for your daily routine.\n"
        "Like and subscribe for daily health tips.\n"
        "Educational only, not medical advice.\n\n"
        + "\n".join(_generate_topic_hashtags(safe_topic)[:350])
    )
    words    = re.findall(r"[a-zA-Z0-9]+", safe_topic)
    tags_csv = ",".join(
        (words[:6] + ["health", "wellness", "shorts",
                      "healthy habits", "fitness", "tips"])[:12]
    )
    return {
        "title": title,
        "description": description,
        "tags_csv": tags_csv,
    }

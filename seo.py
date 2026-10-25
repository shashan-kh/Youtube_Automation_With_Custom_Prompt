"""
seo.py — Policy-Safe YouTube Health SEO, High-CTR Title & 3-Word Complementary Thumbnail Matrix,
E-E-A-T Source Citations, YMYL Description Box Builder, and 350-Hashtag Generator.
"""
from __future__ import annotations

import json
import os
import re
import requests

from brand_config import (
    NEGATIVE_PROMPT_RULES,
    VISUAL_IDENTITY,
    enforce_policy_guardrails,
    select_credible_sources,
)
from common import extract_json_block, get_logger

log = get_logger("seo")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
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
    "health awareness", "symptom awareness", "morning routine",
    "evening routine", "sleep better", "sleep tips", "deep sleep",
    "stress relief", "anxiety relief", "mental health", "mental wellness",
    "brain health", "focus tips", "memory improvement", "gut health",
    "digestion", "immune system", "vitamin tips", "mineral rich foods",
    "anti inflammatory", "inflammation", "hydration", "water intake",
    "posture", "spine health", "back pain relief", "neck pain relief",
    "stretching", "mobility", "flexibility", "yoga", "meditation",
    "breathing exercises", "mindfulness", "cold shower benefits",
    "morning sunlight", "circadian rhythm", "melatonin", "cortisol",
    "hormone balance", "intermittent fasting", "weight management",
    "metabolism", "energy boost", "fatigue relief", "physiology",
    "healthy diet", "balanced diet", "plant based", "protein intake",
    "fiber foods", "healthy snacks", "meal prep", "clean eating",
    "blood sugar", "heart health", "cardio fitness", "strength training",
    "bodyweight exercise", "home workout", "walking benefits",
    "steps per day", "active lifestyle", "desk job health", "ergonomics",
    "eye strain", "screen time", "digital detox", "skin health",
    "bone health", "joint health", "india health", "indian wellness",
    "educational", "informative", "science backed", "evidence based",
    "medical facts", "health myths busted", "how the body works",
    "trending health", "viral wellness", "short video", "60 second health",
]

_SEO_PROMPT = """\
You are an elite, policy-safe YouTube Health & Symptoms Niche Strategist.

Topic: "{topic}"
Spoken Script Excerpt: "{script_excerpt}"
Credible Sources Cited: {sources_names}

{negative_rules}

Return ONLY a valid JSON object with these exact keys (no markdown fences, no extra commentary):

{{
  "title": "Curiosity-driven, policy-safe title (max 82 chars) ending with #Shorts. Use proven frameworks like 'Why Does This Happen? [Topic] Explained #Shorts' or 'Is [Symptom] Serious? What Research Says #Shorts'",
  "punchy_short_title": "Punchy curiosity title under 6 words",
  "thumbnail_text_3_words": "EXACTLY 3 words for the thumbnail overlay. STRICT COMPLEMENTARITY RULE: Must NEVER repeat ANY word from 'title' or 'punchy_short_title'. Must complete the thought or raise a second curiosity question (e.g., 'CHECK THIS NOW', 'YOUR BODY REACTS', 'WHAT SCIENCE SAYS').",
  "thumbnail_image_prompt": "Clean 3D educational anatomical or bright studio lifestyle image prompt in clinical navy and teal, zero medical gore, no text",
  "summary": "2-3 sentence educational summary explaining the physiological mechanism covered in the video without diagnosing the viewer.",
  "topic_tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10", "tag11", "tag12"]
}}
"""


def _call_groq(prompt: str, model: str) -> requests.Response:
    return requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.65,
        },
        timeout=120,
    )


def _enforce_thumbnail_complementarity(title: str, punchy_title: str, raw_thumb_text: str) -> str:
    """
    Guarantees the 3-word thumbnail text NEVER repeats any word from the video title.
    """
    title_words = {
        w.lower()
        for w in re.findall(r"[a-zA-Z0-9]+", f"{title} {punchy_title}")
        if len(w) >= 2
    }
    candidate_words = [
        w.upper()
        for w in re.findall(r"[a-zA-Z0-9]+", raw_thumb_text or "")
        if w.lower() not in title_words
    ]
    if len(candidate_words) == 3:
        return " ".join(candidate_words)

    # Safe complementary 3-word fallbacks tested against title_words
    complementary_pool = [
        ["WHAT", "RESEARCH", "REVEALS"],
        ["INSIDE", "YOUR", "BODY"],
        ["WATCH", "FOR", "THIS"],
        ["SCIENCE", "EXPLAINS", "WHY"],
        ["OFTEN", "OVERLOOKED", "SIGNALS"],
        ["CHECK", "THESE", "CLUES"],
        ["HOW", "IT", "WORKS"],
        ["KNOW", "THE", "FACTS"],
    ]
    for phrase in complementary_pool:
        if all(w.lower() not in title_words for w in phrase):
            return " ".join(phrase)
    return "LOOK CLOSER TODAY"


def generate_seo_package(topic: str, script_text: str) -> dict:
    """
    Returns dict with keys:
      title, punchy_short_title, thumbnail_text, thumbnail_prompt,
      description, tags_csv, sources
    Never raises — always returns a policy-safe package.
    """
    sources = select_credible_sources(topic, script_text, count=3)
    sources_names = ", ".join(s["org"] for s in sources)

    excerpt = re.sub(r'["\\\x00-\x1f]', " ", (script_text or "")[:280].replace("\n", " "))
    topic_safe = re.sub(r'["\\\x00-\x1f]', " ", topic or "Health Awareness")

    prompt = _SEO_PROMPT.format(
        topic=topic_safe,
        script_excerpt=excerpt,
        sources_names=sources_names,
        negative_rules=NEGATIVE_PROMPT_RULES,
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
            raw = extract_json_block(content) or content
            raw = raw.strip().lstrip("\ufeff")
            data = json.loads(raw)

            title = _clean_title(data.get("title", ""), topic_safe)
            punchy = enforce_policy_guardrails(str(data.get("punchy_short_title", "")).strip())[:55]
            if not punchy:
                punchy = "What Research Says"

            thumb_text = _enforce_thumbnail_complementarity(
                title, punchy, str(data.get("thumbnail_text_3_words", ""))
            )
            thumb_prompt = str(data.get("thumbnail_image_prompt", "")).strip() or (
                VISUAL_IDENTITY["thumbnail_prompt_template"].format(topic=topic_safe)
            )

            summary = enforce_policy_guardrails(str(data.get("summary", "")).strip())
            description = _assemble_policy_safe_description(
                summary=summary,
                topic=topic_safe,
                sources=sources,
            )
            tags_csv = _clean_tags(data.get("topic_tags", []), topic_safe)

            log.info("[SEO] success with model: %s", model)
            return {
                "title": title,
                "punchy_short_title": punchy,
                "thumbnail_text": thumb_text,
                "thumbnail_prompt": thumb_prompt,
                "description": description,
                "tags_csv": tags_csv,
                "sources": sources,
            }
        except Exception as exc:
            log.warning("[SEO] model %s failed: %s", model, exc)
            continue

    log.error("[SEO] all models failed, using policy-safe fallback")
    return _fallback_seo(topic_safe, script_text, sources)


def _clean_title(raw: str, topic: str) -> str:
    t = enforce_policy_guardrails((raw or "").strip())
    t = re.sub(r"[^\x00-\x7F]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = f"Why Does This Happen? {topic[:45]} Explained"
    if "#Shorts" not in t and "#shorts" not in t:
        t = t.rstrip(" .!?,;:") + " #Shorts"
    if len(t) > 90:
        keep = 90 - len(" #Shorts")
        t = t[:keep].rstrip() + " #Shorts"
    return t


def _assemble_policy_safe_description(summary: str, topic: str, sources: list[dict]) -> str:
    clean_sum = re.sub(r"[^\x00-\x7F]", "", (summary or "").strip())
    if not clean_sum:
        clean_sum = (
            f"{topic}: Explore the physiological mechanisms and peer-reviewed research behind "
            "how your body responds to everyday habits and signals."
        )

    sources_lines = "\n".join(f"- {s['name']}: {s['url']}" for s in sources)

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

    if len(all_tags) < 350:
        for i in range(1, 500):
            candidate = f"#HealthAwareness{i}"
            if candidate.lower() not in seen_lower:
                all_tags.append(candidate)
                seen_lower.add(candidate.lower())
            if len(all_tags) >= 350:
                break

    tag_block = "\n".join(all_tags[:350])

    structured_desc = (
        f"{clean_sum}\n\n"
        "⚠️ Disclaimer: This video is for general informational and educational purposes only. "
        "It is not intended as medical advice. Always consult a licensed healthcare provider "
        "for diagnosis and treatment.\n\n"
        f"📚 Sources:\n{sources_lines}\n\n"
        "⏱️ Timestamps:\n"
        "0:00 Curiosity Hook\n"
        "0:04 Educational Disclaimer & Context\n"
        "0:10 Physiological Mechanism & Research\n"
        "0:48 Key Takeaway & Consultation Reminder\n\n"
        f"{tag_block}"
    )
    return structured_desc[:4900]


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
            "Awareness", "Science", "Facts", "Explained", "Physiology",
            "Research", "Education", "Tips", "Habits", "Routine",
            "Guide", "101", "ForBeginners", "Daily", "Morning",
            "Night", "Naturally", "EvidenceBased", "Backed", "Health",
        ]
        for suf in suffixes:
            tags.append(f"#{full}{suf}")
            tags.append(f"#{root}{suf}")

    adjacent = [
        "SymptomAwareness", "HealthEducation", "HowTheBodyWorks",
        "PreventiveHealth", "MedicalFacts", "ScienceBackedHealth",
        "EvidenceBasedHealth", "HealthyHabits", "HealthyLifestyle",
        "WellnessTips", "WellnessJourney", "MindBodyConnection",
        "NaturalWellness", "DailyHealth", "DailyWellness",
        "HealthShorts", "WellnessShorts", "YoutubeShorts", "Shorts",
    ]
    tags.extend([f"#{t}" for t in adjacent])
    return tags


def _clean_tags(raw: list, topic: str) -> str:
    out: list[str] = []
    seen: set[str] = set()
    topic_words = re.findall(r"[a-zA-Z0-9]+", topic or "")
    candidates = list(raw or []) + [topic] + [" ".join(topic_words[:3])]
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


def _fallback_seo(topic: str, script_text: str, sources: list[dict]) -> dict:
    safe_topic = re.sub(r"[^\x00-\x7F]", "", topic or "Health Awareness").strip()
    title = _clean_title(f"Why Does This Happen? {safe_topic[:45]} Explained", safe_topic)
    punchy = "What Science Says"
    thumb_text = _enforce_thumbnail_complementarity(title, punchy, "INSIDE YOUR BODY")
    thumb_prompt = VISUAL_IDENTITY["thumbnail_prompt_template"].format(topic=safe_topic)
    summary = (
        f"Explore what clinical research from {sources[0]['org']} and {sources[1]['org']} reveals "
        f"about {safe_topic} and how your body responds."
    )
    description = _assemble_policy_safe_description(summary, safe_topic, sources)
    words = re.findall(r"[a-zA-Z0-9]+", safe_topic)
    tags_csv = ",".join(
        (words[:6] + ["health education", "symptom awareness", "shorts", "physiology", "wellness", "science"])[:12]
    )
    return {
        "title": title,
        "punchy_short_title": punchy,
        "thumbnail_text": thumb_text,
        "thumbnail_prompt": thumb_prompt,
        "description": description,
        "tags_csv": tags_csv,
        "sources": sources,
    }

"""
brand_config.py — Health and Wellness Channel Positioning, Daily Habit Pillars,
Full-Form Credible E-E-A-T Sources, and Visual Identity Constants.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# ── 1. Channel Positioning (Health & Wellness Focus) ───────────────────────────
CHANNEL_POSITIONING_STATEMENT = (
    "This channel shares general health and wellness inspiration for educational purposes only. "
    "Discover science-backed lifestyle, nutrition, sleep, and longevity habits. "
    "Always consult a qualified professional before making major lifestyle changes."
)

CHANNEL_SEO_DESCRIPTIONS = [
    (
        "Welcome to your daily destination for science-backed health and wellness mastery. "
        "Every 60-second Short breaks down actionable daily habits, metabolic nutrition science, "
        "sleep optimization, and longevity research cited from institutions like the Mayo Clinic, "
        "Centers for Disease Control and Prevention, World Health Organization, and National Institutes of Health. "
        "Subscribe for daily wellness insights! "
        f"{CHANNEL_POSITIONING_STATEMENT}"
    ),
]

# ── 2. Guaranteed Single "Thank You" Conclusion ────────────────────────────────
UNIFIED_SINGLE_OUTRO = (
    "Thank you for watching this video. "
    "Subscribe for more daily wellness tips to elevate your health and longevity."
)

# ── 3. Content Pillars & Weekly Calendar Logic (Health & Wellness) ─────────────
CONTENT_PILLARS = {
    "daily_habits": {
        "name": "Daily Habits & Biohacking",
        "framing": "Actionable morning and evening routines, hydration, sunlight, and everyday performance rituals.",
        "seed_queries": [
            "morning sunlight circadian rhythm optimization habits",
            "hydration routine morning energy science",
            "cold exposure dopamine focus habits science",
            "digital detox evening wind down routine",
        ],
    },
    "nutrition_metabolism": {
        "name": "Nutrition & Metabolic Wellness",
        "framing": "Clean eating, gut health, macronutrients, and metabolic energy balance.",
        "seed_queries": [
            "gut health prebiotic foods metabolism science",
            "intermittent fasting cellular autophagy benefits",
            "protein pacing muscle recovery science",
            "anti inflammatory whole foods nutrition",
        ],
    },
    "rest_recovery": {
        "name": "Rest & Deep Recovery",
        "framing": "Sleep architecture, nervous system regulation, and stress resilience.",
        "seed_queries": [
            "deep sleep slow wave sleep optimization hacks",
            "vagus nerve breathing nervous system reset",
            "magnesium sleep relaxation science",
            "circadian rhythm sleep hygiene habits",
        ],
    },
    "movement_longevity": {
        "name": "Movement & Longevity Science",
        "framing": "Zone 2 cardio, mobility, strength, and healthspan extension.",
        "seed_queries": [
            "zone 2 cardio mitochondrial health longevity",
            "daily walking blood sugar glucose regulation",
            "mobility stretching posture desk longevity",
            "strength training healthspan benefits science",
        ],
    },
}

WEEKDAY_TO_PILLAR_KEY = {
    0: "daily_habits",
    1: "nutrition_metabolism",
    2: "rest_recovery",
    3: "movement_longevity",
    4: "daily_habits",
    5: "nutrition_metabolism",
    6: "rest_recovery",
}


def get_pillar_for_date(target_date: date | datetime | None = None) -> dict:
    if target_date is None:
        target_date = datetime.utcnow().date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()
    key = WEEKDAY_TO_PILLAR_KEY.get(target_date.weekday(), "daily_habits")
    return {"key": key, **CONTENT_PILLARS[key]}


# ── 4. Full-Form Credible E-E-A-T Medical Sources (No Abbreviations) ───────────
CREDIBLE_SOURCES_POOL = [
    {
        "org": "Mayo Clinic",
        "name": "Mayo Clinic — Healthy Lifestyle & Wellness",
        "url": "https://www.mayoclinic.org/healthy-lifestyle",
        "keywords": ["sleep", "nutrition", "fitness", "wellness", "habit", "stress"],
    },
    {
        "org": "Centers for Disease Control and Prevention",
        "name": "Centers for Disease Control and Prevention — Healthy Living",
        "url": "https://www.cdc.gov/healthy-weight-growth/about/index.html",
        "keywords": ["hydration", "activity", "sleep", "nutrition", "prevention"],
    },
    {
        "org": "National Institutes of Health",
        "name": "National Institutes of Health — Wellness Research",
        "url": "https://health.nih.gov/",
        "keywords": ["brain", "longevity", "metabolism", "circadian", "vitamin", "mitochondria"],
    },
    {
        "org": "Harvard Health Publishing",
        "name": "Harvard Health Publishing — Daily Health & Nutrition",
        "url": "https://www.health.harvard.edu/",
        "keywords": ["diet", "heart", "gut", "exercise", "mindfulness", "longevity"],
    },
]


def select_credible_sources(topic: str, script_text: str = "", count: int = 2) -> list[dict]:
    blob = f"{topic} {script_text}".lower()
    scored = []
    for idx, src in enumerate(CREDIBLE_SOURCES_POOL):
        score = sum(2 for kw in src["keywords"] if kw in blob)
        scored.append((score, -idx, src))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item[2] for item in scored[:max(1, min(count, len(CREDIBLE_SOURCES_POOL)))]]


NEGATIVE_PROMPT_RULES = """
STRICT HEALTH & WELLNESS GUIDELINES:
1. Focus entirely on positive lifestyle optimization, vitality, nutrition, sleep, and longevity.
2. ALWAYS spell out full organization names (e.g., Centers for Disease Control and Prevention, National Institutes of Health, Harvard Health Publishing, World Health Organization). NEVER use abbreviations like CDC, NIH, WHO, or FDA.
3. Maintain an inspiring, curious, high-retention educational tone with a proper Thank You conclusion.
"""


def enforce_policy_guardrails(text: str) -> str:
    return text or ""


VISUAL_IDENTITY = {
    "color_palette": {
        "deep_clinical_navy": "#0A2540",
        "trust_medical_teal": "#00A896",
        "clean_sterile_white": "#F8FAFC",
        "alert_amber_accent": "#F59E0B",
    },
    "broll_mood_board": (
        "Clean, high-contrast health and wellness lifestyle B-roll, morning sunlight, fresh superfoods, "
        "restorative sleep environments, and active mobility in calm turquoise and navy grading."
    ),
    "thumbnail_prompt_template": (
        "Clean 3D educational or bright studio lifestyle photography representing {topic}, "
        "trustworthy clinical navy (#0A2540) and teal (#00A896) color grading, minimalist composition "
        "with negative space for 3-word typography, soft rim lighting, 8k resolution --ar 9:16"
    ),
}

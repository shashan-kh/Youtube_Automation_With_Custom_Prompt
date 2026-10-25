"""
brand_config.py — Channel Positioning, YMYL Medical Policy Guardrails, E-E-A-T Sources,
Weekly Content Pillars, and Visual Identity Constants for YouTube Health Shorts.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# ── 1. Channel Positioning (Awareness, NOT Diagnosis) ─────────────────────────
CHANNEL_POSITIONING_STATEMENT = (
    "This channel shares general health information for educational purposes only. "
    "It is not a substitute for professional medical advice, diagnosis, or treatment. "
    "Always consult a qualified doctor."
)

CHANNEL_SEO_DESCRIPTIONS = [
    (
        "Welcome to your daily source for science-backed health awareness and human physiology explained simply. "
        "Every 60-second Short breaks down everyday bodily signals, debunks common health myths, and shares "
        "evidence-based lifestyle habits cited from institutions like the Mayo Clinic, CDC, WHO, and NIH. "
        "Subscribe for daily educational health insights! "
        f"{CHANNEL_POSITIONING_STATEMENT}"
    ),
    (
        "Curious about how your body works and what everyday symptoms actually mean? We translate peer-reviewed "
        "medical research from PubMed, Cleveland Clinic, and the NIH into clear, 60-second educational Shorts. "
        "Follow along every week for Symptom Awareness, Myth vs. Fact, and Preventive Lifestyle science. "
        f"{CHANNEL_POSITIONING_STATEMENT}"
    ),
    (
        "Empowering your wellness journey through clear, objective health education. In under 60 seconds a day, "
        "learn the physiological mechanisms behind sleep, digestion, posture, stress, and cardiovascular wellness "
        "without fear-mongering or medical jargon. Subscribe to stay informed! "
        f"{CHANNEL_POSITIONING_STATEMENT}"
    ),
]

# ── 2. Mandatory Disclaimers & Single Unified Outro ───────────────────────────
MANDATORY_VERBAL_DISCLAIMER = (
    "This video is for general information only, not medical advice."
)

MID_CONTENT_DOCTOR_REMINDER = (
    "If you experience this persistently, consult a doctor."
)

ON_SCREEN_OUTRO_DISCLAIMER = (
    "Not a substitute for professional medical advice."
)

# Enforced strictly ONCE at the end of every spoken narration
UNIFIED_SINGLE_OUTRO = (
    "Keep monitoring your health, and consult a medical professional for any persistent symptoms. "
    "Subscribe for more science-backed health awareness."
)

# ── 3. Content Pillars & Weekly Calendar Logic ────────────────────────────────
CONTENT_PILLARS = {
    "symptom_awareness": {
        "name": "Symptom Awareness",
        "framing": (
            "Informational only (e.g., '5 subtle signals people often ignore'). "
            "Always frame as 'this could happen, consult a doctor' — never diagnose."
        ),
        "seed_queries": [
            "subtle vitamin deficiency signs mayo clinic",
            "dehydration symptoms body signals",
            "sleep deprivation effects on nervous system",
            "signs of high cortisol stress response",
            "eye strain symptoms screen fatigue",
        ],
    },
    "myth_vs_fact": {
        "name": "Myth vs Fact",
        "framing": (
            "High engagement, low legal risk. Debunk common everyday health misconceptions "
            "using peer-reviewed physiological facts."
        ),
        "seed_queries": [
            "common nutrition myths debunked science",
            "sleep myths vs facts circadian rhythm",
            "hydration myths 8 glasses of water",
            "stretching and posture myths physical therapy",
            "metabolism myths vs science",
        ],
    },
    "lifestyle_prevention": {
        "name": "Lifestyle & Prevention",
        "framing": (
            "Evergreen, advertiser-friendly daily habits, sleep hygiene, ergonomics, "
            "hydration, and movement."
        ),
        "seed_queries": [
            "morning sunlight circadian rhythm benefits",
            "walking after meals blood sugar science",
            "desk posture ergonomics neck relief",
            "box breathing parasympathetic nervous system",
            "sleep hygiene habits deep sleep",
        ],
    },
    "explainer_body_works": {
        "name": "Explainer / How the Body Works",
        "framing": (
            "Curiosity-driven physiological explanations of how organs, hormones, "
            "nerves, and digestion operate."
        ),
        "seed_queries": [
            "how the vagus nerve calms heart rate",
            "what happens to brain during deep sleep",
            "how gut microbiome communicates with brain",
            "why muscles get sore after exercise physiology",
            "how posture affects lung capacity",
        ],
    },
}

# Weekly Calendar Alignment:
# Mon (0): Symptom Awareness | Tue (1): Symptom Awareness
# Wed (2): Myth vs Fact      | Thu (3): Myth vs Fact
# Fri (4): Lifestyle         | Sat (5): Explainer / Q&A | Sun (6): Lifestyle
WEEKDAY_TO_PILLAR_KEY = {
    0: "symptom_awareness",
    1: "symptom_awareness",
    2: "myth_vs_fact",
    3: "myth_vs_fact",
    4: "lifestyle_prevention",
    5: "explainer_body_works",
    6: "lifestyle_prevention",
}


def get_pillar_for_date(target_date: date | datetime | None = None) -> dict:
    if target_date is None:
        target_date = datetime.utcnow().date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()
    key = WEEKDAY_TO_PILLAR_KEY.get(target_date.weekday(), "lifestyle_prevention")
    return {"key": key, **CONTENT_PILLARS[key]}


# ── 4. Credible E-E-A-T Medical Sources ───────────────────────────────────────
CREDIBLE_SOURCES_POOL = [
    {
        "org": "Mayo Clinic",
        "name": "Mayo Clinic — Patient Care & Health Information",
        "url": "https://www.mayoclinic.org/patient-care-and-health-information",
        "keywords": ["symptom", "pain", "posture", "neck", "back", "digest", "gut", "hydration", "fatigue"],
    },
    {
        "org": "CDC",
        "name": "Centers for Disease Control and Prevention (CDC) — Healthy Living",
        "url": "https://www.cdc.gov/healthy-weight-growth/about/index.html",
        "keywords": ["sleep", "water", "nutrition", "activity", "habit", "walking", "prevention", "screen"],
    },
    {
        "org": "NIH",
        "name": "National Institutes of Health (NIH) — Health Information",
        "url": "https://health.nih.gov/",
        "keywords": ["brain", "cortisol", "stress", "hormone", "nerve", "metabolism", "circadian", "vitamin"],
    },
    {
        "org": "Cleveland Clinic",
        "name": "Cleveland Clinic — Health Library & Physiological Guides",
        "url": "https://my.clevelandclinic.org/health",
        "keywords": ["muscle", "breathing", "vagus", "heart", "eye", "stomach", "inflammation", "stretch"],
    },
    {
        "org": "WHO",
        "name": "World Health Organization (WHO) — Health Topics",
        "url": "https://www.who.int/health-topics",
        "keywords": ["global", "lifestyle", "cardio", "mental", "wellbeing", "diet", "immune"],
    },
    {
        "org": "PubMed",
        "name": "National Library of Medicine (PubMed Central)",
        "url": "https://pubmed.ncbi.nlm.nih.gov/",
        "keywords": ["study", "research", "mechanism", "autophagy", "fasting", "microbiome", "physiology"],
    },
]


def select_credible_sources(topic: str, script_text: str = "", count: int = 3) -> list[dict]:
    """Selects 2-3 relevant credible medical sources based on topic and script keywords."""
    blob = f"{topic} {script_text}".lower()
    scored = []
    for idx, src in enumerate(CREDIBLE_SOURCES_POOL):
        score = sum(2 for kw in src["keywords"] if kw in blob)
        if src["org"].lower() in blob:
            score += 5
        scored.append((score, -idx, src))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item[2] for item in scored[: max(2, min(count, len(CREDIBLE_SOURCES_POOL)))]]


# ── 5. Red Flags Guardrail (Negative Prompts & Sanitization) ──────────────────
FORBIDDEN_PATTERNS = [
    (r"(?i)\bif you have this symptom,?\s*you have\b", "If you notice this signal, it may be worth discussing with a doctor"),
    (r"(?i)\bthis cures\b", "research explores how this supports"),
    (r"(?i)\bcures\s+(cancer|diabetes|alzheimer|heart disease)\b", "relates to overall cellular wellness"),
    (r"(?i)\bdoctors\s+don'?t\s+want\s+you\s+to\s+know\b", "clinical research highlights"),
    (r"(?i)\bsecret\s+cure\b", "evidence-based habit"),
    (r"(?i)\b100%\s+guaranteed\b", "supported by physiological studies"),
    (r"(?i)\bdr\.?\s*ai\b", "health education"),
    (r"(?i)\bi\s+diagnose\s+you\b", "this information is educational"),
    (r"(?i)\breverses\s+disease\b", "supports healthy physiology"),
    (r"(?i)\bnever\s+visit\s+doctors\b", "support your daily wellbeing"),
]

NEGATIVE_PROMPT_RULES = """
STRICT YOUTUBE YMYL & RED-FLAG GUARDRAILS (NEVER VIOLATE):
1. NEVER make diagnostic claims (e.g., NEVER say "If you have X symptom, you have Y disease").
2. NEVER claim to cure, reverse, or treat serious conditions (cancer, diabetes, heart disease, etc.).
3. NEVER use anti-doctor conspiracy angles (e.g., "Doctors don't want you to know this" or "Big Pharma hides this").
4. NEVER adopt a fake "Dr. AI" persona or claim personal clinical authority.
5. NEVER promote uncertified supplements, miracle pills, or extreme starvation diets.
6. NEVER describe or request graphic medical gore, open wounds, or fear-mongering imagery.
"""


def enforce_policy_guardrails(text: str) -> str:
    """Programmatically replaces any accidental policy-violating phrasing."""
    cleaned = text or ""
    for pattern, replacement in FORBIDDEN_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned)
    return cleaned


# ── 6. Visual Identity Constants ──────────────────────────────────────────────
VISUAL_IDENTITY = {
    "color_palette": {
        "deep_clinical_navy": "#0A2540",
        "trust_medical_teal": "#00A896",
        "clean_sterile_white": "#F8FAFC",
        "alert_amber_accent": "#F59E0B",
        "caption_highlight_yellow": "&H0000FFFF&",
    },
    "typography": {
        "thumbnail_headline_font": "Montserrat ExtraBold / Impact (All-Caps, High-Contrast Stroke)",
        "video_caption_font": "DejaVu Sans Bold (Size 12, Outline 3, Bottom Margin 96)",
    },
    "broll_mood_board": (
        "Clean, high-contrast educational lifestyle B-roll or subtle 3D anatomical illumination "
        "in cool clinical blues and teals. Bright natural lighting, calm pacing, zero graphic medical gore."
    ),
    "thumbnail_prompt_template": (
        "Clean 3D educational medical illustration or high-contrast studio lifestyle photography representing {topic}, "
        "trustworthy clinical navy (#0A2540) and bioluminescent teal (#00A896) color grading, minimalist composition "
        "with negative space for 3-word typography, soft rim lighting, 8k resolution, strictly no gore, no text, "
        "no watermarks --ar 9:16"
    ),
}

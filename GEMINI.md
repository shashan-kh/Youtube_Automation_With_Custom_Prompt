# Wellness Wonders: Policy-Safe YouTube Health Shorts Automation

This project automates the creation, packaging, and scheduling of educational **Health & Symptoms Niche YouTube Shorts**. It strictly enforces YouTube YMYL (Your Money Your Life) medical policies, E-E-A-T credible sourcing, 2026 Synthetic Media disclosures, and a single-outro audio pipeline.

## Architecture Overview
- **Language & Runtime:** Python 3.11+ executed via GitHub Actions workflows (`.github/workflows/`).
- **Channel Positioning:** Strict **Awareness, NOT Diagnosis** positioning (`brand_config.py`). Never uses a fake "Dr. AI" persona or makes diagnostic/cure claims.
- **Weekly Content Pillars (`propose.py` & `brand_config.py`):**
  - **Mon/Tue:** Symptom Awareness (*"5 subtle signals people often ignore — consult a doctor"*)
  - **Wed/Thu:** Myth vs. Fact (*High engagement, low legal risk*)
  - **Fri/Sun:** Lifestyle & Prevention (*Evergreen, advertiser-friendly habits*)
  - **Sat:** Explainer / How the Body Works (*Physiological curiosity*)
- **Sanitized Spoken Script Pipeline (`handle_comment.py` & `prompt.txt`):**
  - Strips all markdown (`**`, `##`), stage directions (`[Visual]`), timestamps (`[0:00]`), and speaker labels (`Narrator:`).
  - Eliminates LLM-generated "Thank you for watching" loops and appends `UNIFIED_SINGLE_OUTRO` **strictly once**.
  - Structures every Short into the **4-Attribute Viral Shorts System**: `visual_direction`, `on_screen_text`, `spoken_narration`, and `audio_cue`.
- **High-CTR Title & 3-Word Complementary Thumbnail Matrix (`seo.py`):**
  - Generates curiosity-driven titles alongside a **3-word thumbnail overlay** that is programmatically verified to share **zero overlapping words** with the video title.
  - Formats descriptions with `⚠️ Disclaimer`, `📚 Sources` (Mayo Clinic, CDC, NIH, WHO, Cleveland Clinic, PubMed), and `⏱️ Timestamps`.
- **2026 YouTube AI Disclosure:** Automatically sets `containsSyntheticMedia = True` on all YouTube Data API v3 uploads and updates.

## Key Files
- `brand_config.py`: Channel positioning, 4 content pillars, E-E-A-T source registry, red-flag regex guardrails, and visual identity constants.
- `common.py`: Shared utilities for GitHub API and issue metadata handling.
- `prompt.txt`: Policy-safe 60-second Shorts scriptwriter prompt (no LLM outro generation).
- `handle_comment.py`: Central command processor, script sanitizer, MoviePy/FFmpeg renderer, and YouTube uploader.
- `propose.py`: Pillar-aware topic discovery and refinement.
- `seo.py`: SEO title, complementary 3-word thumbnail text, AI thumbnail prompt, and YMYL description builder.
- `topics_pool.json`: Sanitized persistent fallback pool of policy-safe topics.

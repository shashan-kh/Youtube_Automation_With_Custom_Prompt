# Project Context: Youtube_Automation (Wellness Wonders)

## Architecture Overview
This is a semi-automated YouTube Shorts production and scheduling pipeline for the health & wellness channel "Wellness Wonders".
- **Language & Runtime:** Python 3.x executed via GitHub Actions workflows (`.github/workflows/`).
- **Control Mechanism:** Triggered via scheduled cron jobs and GitHub Issue comments (e.g., `/set-script`, `/approve-video`).
- **Script & SEO Generation:** Uses the Groq API to generate high-retention Shorts scripts, titles, descriptions, and tags from a dynamic topics pool.
- **Audio Pipeline:** Automated Text-to-Speech (TTS) synthesis mixed with calm instrumental background audio generated/mixed via FFmpeg.
- **Visual Pipeline:** Vertical (9:16) stock B-roll fetched dynamically via the Pexels API, stitched and processed with FFmpeg.
- **Captions:** Automated word/phrase-level subtitle generation using Whisper and burned into the video via FFmpeg.
- **Publishing:** Automated YouTube Data API v3 upload with SEO metadata and scheduling.

## Coding & Optimization Guidelines
1. **Retention & Monetization Compliance:** Avoid static, uncut B-roll. When updating FFmpeg video filters, prefer dynamic cuts (every 1.5–2.5 seconds), subtle zoompan (Ken Burns) effects, and high-contrast highlighted captions so videos pass YouTube Partner Program "Reused/Repetitious Content" checks.
2. **Script Structure:** All Groq prompts must enforce a 0–3s pattern-interrupt hook (never repeating the title verbatim), actionable body steps, and a seamless loop ending.
3. **Workflow Stability:** Ensure all external API calls (Groq, Pexels, YouTube) include proper error handling, retries, and clean logging for GitHub Actions debugging.

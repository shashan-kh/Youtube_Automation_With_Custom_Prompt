# Wellness Wonders: YouTube Shorts Automation

This project automates the creation and scheduling of health and wellness YouTube Shorts. It leverages AI for scriptwriting, SEO, and transcription, while fetching high-quality vertical B-roll and rendering the final video using FFmpeg.

## Architecture Overview
- **Language & Runtime:** Python 3.11+ executed via GitHub Actions workflows (`.github/workflows/`).
- **Control Mechanism:** Triggered via scheduled cron jobs and GitHub Issue comments (e.g., `/approve-topic`, `/set-script`, `/approve-video`).
- **Script & SEO Generation:** Uses the Groq API (`llama-3.3-70b-versatile`) to generate high-retention Shorts scripts, titles, descriptions, and tags from a dynamic topics pool.
- **Audio Pipeline:** Automated Text-to-Speech (TTS) using `gTTS` or `espeak-ng` mixed with background audio via FFmpeg.
- **Visual Pipeline:** Vertical (9:16) stock B-roll fetched dynamically via the Pexels API, processed with MoviePy and FFmpeg.
- **Captions:** Automated word/phrase-level subtitle generation using `faster-whisper` and burned into the video via FFmpeg.
- **Publishing:** Automated YouTube Data API v3 upload with SEO metadata and scheduling.

## Commands (GitHub Issues)
The system is controlled via GitHub Issue comments:
- `/approve-topic <N>`: Selects a proposed topic.
- `/set-script <text>`: Provides a custom script for the video.
- `/use-default-prompt`: Generates a script based on `prompt.txt`.
- `/approve-video`: Final approval to upload/schedule to YouTube.
- `/regenerate-video`: Re-renders the current video (e.g., to get different B-roll).

## Coding & Optimization Guidelines
1. **Retention & Monetization Compliance:** Avoid static, uncut B-roll. When updating FFmpeg video filters, prefer dynamic cuts (every 1.5–2.5 seconds), subtle zoompan (Ken Burns) effects, and high-contrast highlighted captions so videos pass YouTube Partner Program "Reused/Repetitious Content" checks.
2. **Script Structure:** All Groq prompts must enforce a 0–3s pattern-interrupt hook (never repeating the title verbatim), actionable body steps, and a seamless loop ending.
3. **Workflow Stability:** Ensure all external API calls (Groq, Pexels, YouTube) include proper error handling, retries, and clean logging for GitHub Actions debugging.
4. **Technical Standards:** Use `ThreadPoolExecutor` for parallel asset downloads. Final video must be 1080x1920, 30fps, and < 58 seconds.

## Key Files
- `common.py`: Shared utilities for GitHub API and metadata handling.
- `topics_pool.json`: Persistent storage for fallback topics.
- `requirements.txt`: Project dependencies (MoviePy, gTTS, faster-whisper, etc.).
- `.github/workflows/`: Automation schedules and comment listeners.
- `handle_comment.py`: The central logic for processing issue commands.
- `propose.py`: Topic discovery and refinement.
- `seo.py`: SEO package generation.

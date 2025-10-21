# YouTube Health Shorts Automation (India, IST)

What it does
- Proposes trending health-safe topics the previous evening (IST).
- After you approve a topic, it AUTO-REGENERATES the video until it is strictly <58s (target ~57.3s), vertical 1080x1920, with voice + burned captions, then sends a preview link.
- Only after a valid (<58s) preview is ready does it ask you to confirm upload the first time.
- After you approve the video, it uploads PRIVATE and schedules next-day publish:
  - Morning slot: 9:00 AM IST
  - Afternoon slot: 4:00 PM IST

Daily timings (IST)
- 7:00 PM IST → propose topics for next day 9:00 AM
- 9:00 PM IST → propose topics for next day 4:00 PM

Commands in GitHub Issue
- /approve-topic 1 (or 2/3)
- /reject-topic or /new-topic
- /approve-video (schedules PRIVATE → auto-publish next day)
- /reject-video
- /regenerate-video (optional; auto-regeneration already runs after topic approval)

Secrets to add (Settings → Secrets and variables → Actions)
- YT_CLIENT_ID
- YT_CLIENT_SECRET
- YT_REFRESH_TOKEN
- GROQ_API_KEY
- PEXELS_API_KEY

Repo Settings
- Actions → General → Workflow permissions → Read and write permissions.

Notes
- Content is general wellness only; includes “Educational only, not medical advice.”
- Strictly under 58 seconds enforced with auto-regenerate and final safety trim if needed.
- Uses Pexels for vertical stock clips and gTTS for voice (you can swap to Piper later).

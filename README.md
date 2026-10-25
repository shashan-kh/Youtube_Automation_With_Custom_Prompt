# YouTube Health Shorts Automation

Fully automated pipeline: trending topic discovery → custom script or AI script → TTS voice → b-roll render → captions → YouTube upload → scheduled publish.  
All controlled via GitHub Issue comments.

---

## How It Works

1. **Evening (IST):** `propose` workflow runs, discovers 6 specific, actionable health topics, and opens a GitHub Issue.
2. **Topic & Script Selection (Individual OR Combined):**
   - **Combined (One-Step):** Reply `/approve-topic 2 /use-default-prompt` or `/approve-topic 2 /set-script <your voiceover text>` to approve a topic and immediately build the video preview in a single comment.
   - **Step-by-Step (Two-Step):** Reply `/approve-topic 2` first. The bot confirms the topic and asks for your script choice. Then reply with `/use-default-prompt` or `/set-script <your voiceover text>` to start rendering.
3. **Preview Build:**
   - Script is generated (or cleaned)
   - `gTTS` / `espeak-ng` renders the voiceover
   - Pexels vertical b-roll is downloaded in parallel (with automatic synthetic fallback)
   - Video is rendered (1080×1920, ≤58 s) with burned `faster-whisper` captions and ambient BGM
   - SEO title, description, and 350 hashtags are generated
   - Uploaded as **Unlisted** to YouTube and the preview link is posted to the Issue
4. **Final Approval:** Reply `/approve-video` to schedule the video to publish automatically at the next slot.

---

## Daily Schedule (IST)

| Time | Action |
|------|--------|
| 19:00 | Propose topics for next day 9:00 AM slot |
| 21:00 | Propose topics for next day 4:00 PM slot |

---

## Commands (GitHub Issue Comments)

| Command | What it does |
|---------|-------------|
| `/approve-topic N` | Approve topic number N (1–6) and wait for script command |
| `/approve-topic My Custom Title` | Approve inline custom topic and wait for script command |
| `/approve-topic N /use-default-prompt` | **Combined:** Approve topic N and immediately build video using AI script |
| `/approve-topic N /set-script <text>` | **Combined:** Approve topic N and immediately build video with your script |
| `/custom-topic <topic> /use-default-prompt` | **Combined:** Use custom topic and immediately build video using AI script |
| `/custom-topic <topic> /set-script <text>` | **Combined:** Use custom topic and immediately build video with your script |
| `/use-default-prompt` | Generate script using AI (`prompt.txt`) for the currently approved topic |
| `/set-script <full text>` | Bypass AI and build video using your exact voiceover script |
| `/reject-topic` or `/new-topic` | Get 6 fresh topic suggestions |
| `/approve-video` | Schedule the unlisted preview video for next-day public release |
| `/regenerate-video` | Rebuild the preview video for the current topic |
| `/reject-video` | Delete unlisted preview and choose a new topic |

---

## Secrets Required
Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Description |
|--------|-------------|
| `YT_CLIENT_ID` | YouTube OAuth client ID |
| `YT_CLIENT_SECRET` | YouTube OAuth client secret |
| `YT_REFRESH_TOKEN` | YouTube OAuth refresh token |
| `GROQ_API_KEY` | Groq LLM API key |
| `PEXELS_API_KEY` | Pexels video API key |

---

## Repository Settings
**Settings → Actions → General → Workflow permissions →**
✅ Read and write permissions



================================================

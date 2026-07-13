# YouTube Health Shorts Automation

Fully automated pipeline: trending topic discovery → custom script or AI script
→ TTS voice → b-roll render → captions → YouTube upload → scheduled publish. 
All controlled via GitHub Issue comments.

---

## How It Works

Evening (IST)
-> Propose workflow runs
-> Finds 6 specific, actionable health topics
(YouTube Trending + Search + Top Channels + Google Trends + Reddit + Dynamic Fallback Pool)
-> LLM refines vague signals into high-CTR titles
-> Creates GitHub Issue for approval

You comment: /approve-topic 2
-> Bot asks for your script or default generation
-> You reply: /set-script <your complete voiceover text>
OR /use-default-prompt
-> Script is processed
-> gTTS renders voiceover
-> Pexels b-roll downloaded (parallel)
-> Video rendered (1080×1920, ≤58 s)
-> Whisper captions burned
-> BGM mixed
-> SEO package generated (title + description + 350 hashtags)
-> Uploaded unlisted to YouTube

You comment: /approve-video
-> Video scheduled PRIVATE → auto-publish next day

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
| `/approve-topic N` | Approve topic number N (1–6) |
| `/approve-topic My Custom Title` | Approve with inline topic text |
| `/reject-topic` | Get 6 fresh topic suggestions |
| `/new-topic` | Same as /reject-topic |
| `/custom-topic <text>` | Use your own topic directly |
| `/set-script <full text>` | Bypass AI and use your own exact script text |
| `/use-default-prompt` | Auto-generate script using AI and prompt.txt |
| `/approve-video` | Schedule video for next-day publish |
| `/reject-video` | Delete preview, pick a new topic |
| `/regenerate-video` | Rebuild video for same topic |

---

## Topic Discovery Sources
1. **YouTube Trending**
2. **YouTube Search**
3. **Top Health Channels** 4. **Google Trends**
5. **Reddit**
6. **topics_pool.json** — A dynamic repository fallback list populated continuously by previous runs.

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
| `BACKGROUND_MUSIC_URL` | (Optional) HTTPS URL to CC0 BGM mp3/m4a |

---

## Repository Settings
**Settings → Actions → General → Workflow permissions →**
✅ Read and write permissions

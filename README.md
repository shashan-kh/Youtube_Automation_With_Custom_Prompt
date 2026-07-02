# YouTube Health Shorts Automation

Fully automated pipeline: trending topic discovery → your script prompt
→ AI script → TTS voice → b-roll render → captions → YouTube upload →
scheduled publish. All controlled via GitHub Issue comments.

---

## How It Works

Evening (IST)
-> Propose workflow runs
└─ Finds 6 specific, actionable health topics
(YouTube Trending + Search + Top Channels + Google Trends + Reddit)
└─ LLM refines vague signals into high-CTR titles
└─ Creates GitHub Issue for approval

You comment: /approve-topic 2
└─ Bot asks for your script prompt
└─ You reply: /set-prompt <your prompt>
OR /use-default-prompt
└─ LLM writes script using YOUR prompt
└─ gTTS renders voiceover
└─ Pexels b-roll downloaded (parallel)
└─ Video rendered (1080×1920, ≤58 s)
└─ Whisper captions burned
└─ BGM mixed
└─ SEO package generated
(title + description + 350 hashtags)
└─ Uploaded unlisted to YouTube

You comment: /approve-video
└─ Video scheduled PRIVATE → auto-publish next day

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
| `/set-prompt <full prompt>` | Supply your script generation prompt |
| `/use-default-prompt` | Use the built-in default prompt |
| `/approve-video` | Schedule video for next-day publish |
| `/reject-video` | Delete preview, pick a new topic |
| `/regenerate-video` | Rebuild video for same topic |

---

## Topic Discovery Sources

1. **YouTube Trending** — multi-region (IN, US, GB, AU, CA), health-filtered
2. **YouTube Search** — 20 specific health query seeds, last 14 days, by view count
3. **Top Health Channels** — Doctor Mike, Huberman, Peter Attia, Mayo Clinic + 12 more
4. **Google Trends** — realtime + related queries on 20 health seeds
5. **Reddit** — r/health, r/Fitness, r/sleep, r/nutrition + 9 more subreddits

All raw signals → **LLM refinement** → specific, actionable, high-CTR titles.

Example transformation:
- Raw: `"sleep"` → Refined: `"The 4-7-8 breathing trick that puts you to sleep in 2 minutes"`
- Raw: `"back pain"` → Refined: `"3 desk stretches that erase lower back pain in 5 minutes"`

---

## SEO Strategy

- **Title**: keyword-first, power word, ≤90 chars, `#Shorts` suffix
- **Description**: keyword in first 157 chars, CTA, disclaimer, then **350 topic-specific hashtags**
- **YouTube tags field**: 12 most relevant keyword phrases
- All SEO generated fresh by LLM per video (topic-specific, not a fixed list)

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

---

## Notes

- Content is general wellness only. Always includes: *"Educational only, not medical advice."*
- Video strictly enforced ≤ 58 seconds with auto-regenerate + safety trim.
- Face-aware smart crop for vertical 1080×1920 (OpenCV, falls back to center crop).
- gTTS voice with espeak-ng fallback if Google blocks the runner IP.
- Pip dependency caching — faster runs on cache hit (~10 s vs ~90 s).
- Action SHAs pinned for supply-chain security.

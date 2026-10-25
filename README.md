# YouTube Health Shorts Automation (YMYL Policy-Safe Edition)

Fully automated pipeline for the **Health & Symptoms Niche**: pillar-based topic discovery → policy-safe AI script (or custom script) → strict TTS sanitization (zero repeated "Thank you" loops) → 4-attribute viral Shorts packaging → dynamic B-roll render → burned captions → YouTube upload with **2026 Synthetic Media disclosure** (`containsSyntheticMedia: True`) → scheduled publish.  
All controlled via GitHub Issue comments.

---

## Channel Positioning & YMYL Safeguards

> *"This channel shares general health information for educational purposes only. It is not a substitute for professional medical advice, diagnosis, or treatment. Always consult a qualified doctor."*

1. **Awareness, NOT Diagnosis:** Every script includes the verbal disclaimer (`"This video is for general information only, not medical advice"`), cites a verified institution in speech (**Mayo Clinic, CDC, NIH, WHO, Cleveland Clinic, or PubMed**), explains the physiological mechanism, and reminds viewers to consult a doctor for persistent symptoms.
2. **Single Unified Outro (No "Thank You" Loops):** `sanitize_spoken_script()` strips all markdown, stage directions, speaker tags, and LLM-generated sign-offs before appending the single closing CTA strictly once.
3. **High-CTR Title + 3-Word Complementary Thumbnail Matrix:** Every preview comment generates a curiosity title, a punchy `<6 word` alt title, a **3-word thumbnail text** guaranteed to never repeat words from the title, and a clean 3D/studio AI image prompt.
4. **Weekly Content Pillar Calendar:**
   - **Mon / Tue:** Symptom Awareness
   - **Wed / Thu:** Myth vs. Fact
   - **Fri / Sun:** Lifestyle & Prevention
   - **Sat:** Explainer / How the Body Works

---

## Commands (GitHub Issue Comments)

| Command | What it does |
|---------|-------------|
| `/approve-topic N` | Approve topic number N (1–6) and wait for script command |
| `/approve-topic My Custom Title` | Approve inline custom topic and wait for script command |
| `/approve-topic N /use-default-prompt` | **Combined:** Approve topic N and immediately build video using policy-safe AI script |
| `/approve-topic N /set-script <text>` | **Combined:** Approve topic N and immediately build video with your sanitized script |
| `/custom-topic <topic> /use-default-prompt` | **Combined:** Use custom topic and immediately build video using policy-safe AI script |
| `/custom-topic <topic> /set-script <text>` | **Combined:** Use custom topic and immediately build video with your sanitized script |
| `/use-default-prompt` | Generate script using AI (`prompt.txt`) for the currently approved topic |
| `/set-script <full text>` | Bypass AI and build video using your custom script (auto-sanitized for TTS) |
| `/reject-topic` or `/new-topic` | Get 6 fresh policy-safe topic suggestions |
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

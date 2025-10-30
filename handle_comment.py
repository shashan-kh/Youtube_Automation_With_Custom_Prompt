# ... keep your current handle_comment.py, but replace the llm_script() and upload_youtube_unlisted() functions with the versions below ...

def llm_script(trending_query, word_hint="130–160"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY secret")
    prompt = f"""
You are a careful health educator and a seasoned YouTube Shorts SEO copywriter.
Create a strictly under-58s YouTube Short based on this trending query:
"{trending_query}"

Content rules (safety):
- General wellness only (sleep, hydration, movement, posture, stress, basic nutrition).
- No disease claims, diagnoses, dosages, or supplement promises. Avoid COVID/vaccines and medical advice.
- If the query is unsafe/specific (e.g., drugs/diseases), pivot to a safe, related habit.
- Tone: energetic, plain language, second-person; target {word_hint} words so the final video is 50–58 seconds.

SEO requirements:
- Title: SEO-optimized and compelling. Start with the main keyword/phrase; keep <= 90 chars; include #Shorts at the end.
- Description: 3–5 concise lines with keyword-rich phrasing, benefits, and simple steps; include 3–5 relevant hashtags, and “Educational only, not medical advice.”
- Tags: 10–15 comma-separated SEO keywords (short, generic + mid-tail) relevant to the topic; no duplicates; no hashtags in tags.

Output pure JSON with:
- voiceover: string
- title: string (SEO-optimized, includes #Shorts at end)
- description: string (3–5 lines + hashtags + “Educational only, not medical advice.”)
- tags: array or comma-separated list (10–15 items)
"""
    models_to_try = build_model_list()
    last_err = None
    for model in models_to_try:
        r = call_groq(prompt, model)
        if r.status_code == 200:
            raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not raw:
                last_err = RuntimeError(f"Groq '{model}' returned empty content")
                continue
            block = extract_json_block(raw) or raw
            try:
                data = json.loads(block)
                # Normalize tags to comma-separated string
                if isinstance(data.get("tags"), list):
                    tags = [str(t).strip() for t in data["tags"] if str(t).strip()]
                    data["tags"] = ",".join(tags[:15])
                else:
                    # Ensure max 15 if comma string
                    parts = [p.strip() for p in str(data.get("tags","")).split(",") if p.strip()]
                    data["tags"] = ",".join(parts[:15])
                # Ensure #Shorts in title
                if "#Shorts" not in data.get("title",""):
                    data["title"] = (data.get("title","").strip() + " #Shorts").strip()
                return data
            except Exception as e:
                last_err = RuntimeError(f"Failed to parse LLM JSON from '{model}': {e}\nRaw: {raw[:500]}")
                continue
        else:
            try:
                errj = r.json()
            except Exception:
                errj = {"error": {"message": r.text}}
            msg = str(errj.get("error", {}).get("message", r.text))
            last_err = RuntimeError(f"Groq model '{model}' failed: {msg}")
            continue
    avail = list_groq_models()
    if avail:
        raise RuntimeError(f"{last_err}\nAvailable models for your key:\n- " + "\n- ".join(avail[:30]) + "\nSet GROQ_MODEL/GROQ_FALLBACK_MODELS to one of the above.")
    raise last_err or RuntimeError("Groq call failed; no models available to try")

def upload_youtube_unlisted(video_path, title, description, tags):
    yt = yt_client()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()][:15],
            "categoryId": "27",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en"
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
    vid_id = resp.get("id")
    return vid_id, f"https://youtu.be/{vid_id}"

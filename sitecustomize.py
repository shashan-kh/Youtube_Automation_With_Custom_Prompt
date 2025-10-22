# sitecustomize.py
# Ensures default models are set if the environment doesn't provide them.

import os

os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault(
    "GROQ_FALLBACK_MODELS",
    "llama-3.1-8b-instant,groq/compound-mini,qwen/qwen3-32b,allam-2-7b"
)

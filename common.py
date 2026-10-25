"""
common.py — Shared utilities for GitHub API, metadata persistence, and text helpers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests

REPO = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

_META_REGEX = re.compile(r"<!--\s*meta:\s*(\{.*?\})\s*-->", re.DOTALL)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s]: %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    debug_env = os.getenv("DEBUG", "0").strip().lower()
    logger.setLevel(logging.DEBUG if debug_env in ("1", "true", "yes") else logging.INFO)
    return logger


log = get_logger("common")


def gh(method: str, url: str, **kwargs) -> requests.Response:
    """Wrapper for GitHub REST API requests with automatic auth headers and retries."""
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/vnd.github+json")
    if GITHUB_TOKEN:
        headers.setdefault("Authorization", f"Bearer {GITHUB_TOKEN}")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    kwargs["headers"] = headers
    kwargs.setdefault("timeout", 30)

    last_resp = None
    for attempt in range(1, 4):
        try:
            r = requests.request(method, url, **kwargs)
            last_resp = r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * attempt)
                continue
            return r
        except Exception as exc:
            log.warning("GitHub API %s %s attempt %d failed: %s", method, url, attempt, exc)
            time.sleep(2 * attempt)
    if last_resp is not None:
        return last_resp
    raise RuntimeError(f"GitHub API request failed after retries: {method} {url}")


def post_comment(owner: str, repo: str, issue_number: int, body: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    r = gh("POST", url, json={"body": body})
    if r.status_code not in (200, 201):
        log.error("Failed to post comment on #%d: %s %s", issue_number, r.status_code, r.text[:300])


def add_label(owner: str, repo: str, issue_number: int, label: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/labels"
    gh("POST", url, json={"labels": [label]})


def remove_label(owner: str, repo: str, issue_number: int, label: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/labels/{label}"
    gh("DELETE", url)


def get_slot_from_labels(labels: list[dict | str]) -> str:
    for lbl in labels or []:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.startswith("slot:"):
            return name.split(":", 1)[1].strip()
    return os.getenv("SLOT", "morning")


def is_authorized_commenter(event: dict) -> bool:
    comment = event.get("comment") or {}
    user = comment.get("user") or {}
    login = (user.get("login") or "").lower()
    user_type = (user.get("type") or "").lower()

    # Ignore bot comments to prevent infinite workflow loops
    if user_type == "bot" or login.endswith("[bot]") or login == "github-actions":
        log.info("Ignoring comment from bot user: %s", login)
        return False

    assoc = (comment.get("author_association") or "").upper()
    repo_owner = ((event.get("repository") or {}).get("owner") or {}).get("login", "").lower()

    if login and login == repo_owner:
        return True
    if assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        return True

    log.warning("Unauthorized commenter %s (association=%s)", login, assoc)
    return False


def get_metadata_from_issue_body(body: str) -> dict[str, Any]:
    if not body:
        return {}
    m = _META_REGEX.search(body)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception as exc:
        log.warning("Failed to parse metadata from issue body: %s", exc)
        return {}


def set_metadata_in_issue_body(body: str, meta: dict[str, Any]) -> str:
    body = body or ""
    meta_json = json.dumps(meta, ensure_ascii=True)
    block = f"<!-- meta: {meta_json} -->"
    if _META_REGEX.search(body):
        return _META_REGEX.sub(lambda _: block, body)
    return body.rstrip() + f"\n\n{block}\n"


def normalize_key(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def jaccard(a: str, b: str) -> float:
    sa = set(normalize_key(a).split())
    sb = set(normalize_key(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def extract_json_block(text: str) -> str | None:
    if not text:
        return None
    # Strip <think>...</think> blocks from reasoning models
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    obj = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if obj:
        return obj.group(1).strip()
    return None

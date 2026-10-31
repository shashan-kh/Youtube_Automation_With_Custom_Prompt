"""
<<<<<<< HEAD
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


=======
common.py — shared utilities for handle_comment.py and propose.py
"""
import os
import re
import sys
import time
import logging
import requests
from typing import Callable, TypeVar

# ── Logging ────────────────────────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger

log = get_logger("common")

# ── GitHub API ─────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO         = os.getenv("GITHUB_REPOSITORY", "")
_RETRIABLE   = (429, 500, 502, 503, 504)


def gh(
    method: str,
    url: str,
    max_retries: int = 3,
    backoff: float = 2.0,
    **kwargs,
) -> requests.Response:
    """GitHub API call with exponential-backoff retry on transient errors."""
    headers = kwargs.pop("headers", {})
    headers.update(
        {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.request(
                method, url, headers=headers, timeout=60, **kwargs
            )
            if r.status_code in _RETRIABLE and attempt < max_retries:
                wait = int(r.headers.get("Retry-After", backoff ** attempt))
                log.warning("GitHub API %s retry in %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise requests.HTTPError(
                    f"GitHub API error {r.status_code}: {r.text[:400]}",
                    response=r,
                )
            return r
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code not in _RETRIABLE:
                raise
            last_exc = exc
            time.sleep(backoff ** attempt)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            time.sleep(backoff ** attempt)
    raise last_exc or RuntimeError(
        f"gh({method} {url}) failed after {max_retries} retries"
    )


def post_comment(owner: str, repo: str, number: int, body: str) -> None:
    gh(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        json={"body": body},
    )


def add_label(owner: str, repo: str, number: int, label: str) -> None:
    try:
        gh(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": [label]},
        )
    except Exception as exc:
        log.warning("add_label '%s' failed: %s", label, exc)


def remove_label(owner: str, repo: str, number: int, label: str) -> None:
    try:
        requests.delete(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels/{requests.utils.quote(label, safe='')}",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
    except Exception as exc:
        log.warning("remove_label '%s' failed: %s", label, exc)


def get_issue(owner: str, repo: str, number: int) -> dict:
    return gh(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
    ).json()


# ── Metadata helpers ───────────────────────────────────────────────────────────
METADATA_SENTINEL = "<!-- automation-metadata -->"


def get_metadata_from_issue_body(issue_body: str) -> dict | None:
    import json
    pattern = re.compile(
        re.escape(METADATA_SENTINEL) + r"\s*```json\s*(\{.*?\})\s*```",
        re.S,
    )
    m = pattern.search(issue_body or "")
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Legacy fallback
    m2 = re.search(r"```json\s*(\{.*?\})\s*```", issue_body or "", re.S)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            pass
    return None


def set_metadata_in_issue_body(issue_body: str, meta: dict) -> str:
    """
    Insert or replace the metadata JSON block in the issue body.
    Uses lambda in re.sub to prevent backslash interpretation in
    replacement strings (fixes: re.error bad escape).
    """
    import json
    block   = f"{METADATA_SENTINEL}\n```json\n{json.dumps(meta, indent=2)}\n```"
    pattern = re.compile(
        re.escape(METADATA_SENTINEL) + r"\s*```json\s*\{.*?\}\s*```",
        re.S,
    )
    if pattern.search(issue_body or ""):
        # lambda prevents re from interpreting backslashes in block
        return pattern.sub(lambda _: block, issue_body, count=1)
    return (issue_body or "").rstrip() + f"\n\n{block}"


def extract_json_block(text: str) -> str | None:
    for pat in (
        r"```json\s*(\{.*?\})\s*```",
        r"```\s*(\{.*?\})\s*```",
        r"(\{.*\})",
    ):
        m = re.search(pat, text or "", re.S)
        if m:
            return m.group(1)
    return None


def normalize_key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_display(s: str) -> str:
    s = (s or "").replace("\r", "")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


>>>>>>> origin/main
def jaccard(a: str, b: str) -> float:
    sa = set(normalize_key(a).split())
    sb = set(normalize_key(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


<<<<<<< HEAD
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
=======
def is_authorized_commenter(event: dict) -> bool:
    assoc = (
        event.get("comment", {}).get("author_association") or ""
    ).upper()
    commenter = (
        event.get("comment", {}).get("user", {}).get("login", "") or ""
    )
    repo_owner = (
        (event.get("repository", {}) or {}).get("owner", {}) or {}
    ).get("login", "")
    return (
        assoc in {"OWNER", "MEMBER", "COLLABORATOR"}
        or commenter == repo_owner
    )


def get_slot_from_labels(labels: list) -> str:
    for lbl in labels or []:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        if name.startswith("slot:"):
            return name.split(":", 1)[1].strip() or "morning"
    return "morning"
>>>>>>> origin/main

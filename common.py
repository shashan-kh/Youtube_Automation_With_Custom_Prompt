"""
common.py — shared utilities for handle_comment.py and propose.py
"""
import os
import re
import sys
import time
import logging
import requests
from typing import Any, Callable, TypeVar

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
    level = logging.DEBUG if os.getenv("DEBUG", "0") == "1" else logging.INFO
    logger.setLevel(level)
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
                log.warning("GitHub API %s → retrying in %ss…", r.status_code, wait)
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

    IMPORTANT: uses a lambda replacement to avoid re.sub interpreting
    backslashes in the replacement string (e.g. \\u, \\n from prompt text)
    as regex escape sequences, which causes re.error: bad escape.
    """
    import json
    block = f"{METADATA_SENTINEL}\n```json\n{json.dumps(meta, indent=2)}\n```"
    pattern = re.compile(
        re.escape(METADATA_SENTINEL) + r"\s*```json\s*\{.*?\}\s*```",
        re.S,
    )
    if pattern.search(issue_body or ""):
        # ── KEY FIX: lambda prevents re from interpreting backslashes
        #    in `block` as regex escape sequences (\u, \n, \t, etc.)
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
    """Lowercase, alphanumeric-only key for deduplication."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_display(s: str) -> str:
    """Normalize whitespace for display (preserves case)."""
    s = (s or "").replace("\r", "")
    s = re.sub(r"[ \t\f\v]+", " ", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


def jaccard(a: str, b: str) -> float:
    sa = set(normalize_key(a).split())
    sb = set(normalize_key(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


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

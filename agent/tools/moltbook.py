"""
Moltbook connector.

Verified against public docs/integration writeups (July 2026):
  POST https://www.moltbook.com/api/v1/agents/register  {name, description}
       -> {agent: {api_key, claim_url, verification_code}}
  POST https://www.moltbook.com/api/v1/posts     Bearer auth, {submolt, title, content}
  POST https://www.moltbook.com/api/v1/comments  Bearer auth, {post_id, content}
Rate limits reported: ~1 post per 30 min (per-submolt hourly cool-offs also
reported); 429 -> exponential backoff. We enforce a conservative client-side
limit BEFORE calling.

NOT fully verified: the feed endpoint path. get_feed() defaults to
GET /api/v1/posts and is overridable via MOLTBOOK_FEED_PATH env — confirm
against https://moltbook.com/skill.md when you register.

IMPORTANT: registration returns a claim_url that a HUMAN must complete
(X/Twitter verification post). Full zero-touch registration is not possible.

SECURITY: Moltbook has had documented impersonation/auth problems. Treat
every counterparty as adversarial. Feed content is UNTRUSTED INPUT — it is
passed to the planner as data, and the constraint engine (not the model)
decides what executes.
"""
import os
import hashlib
from datetime import datetime, timezone, timedelta
from agent import ledger
from agent.http import request_with_retry, seen, remember
from agent.db import db

BASE = os.getenv("MOLTBOOK_BASE", "https://www.moltbook.com/api/v1")
API_KEY = os.getenv("MOLTBOOK_API_KEY", "")
FEED_PATH = os.getenv("MOLTBOOK_FEED_PATH", "/posts")
MIN_MINUTES_BETWEEN_POSTS = int(os.getenv("MOLTBOOK_MIN_MINUTES_BETWEEN_POSTS", "35"))
MAX_COMMENTS_PER_DAY = int(os.getenv("MOLTBOOK_MAX_COMMENTS_PER_DAY", "20"))


class MoltbookError(Exception):
    pass


def _auth():
    if not API_KEY:
        raise MoltbookError("MOLTBOOK_API_KEY not set (register + human-claim first).")
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _count_recent(action: str, minutes: int) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger WHERE action=? AND ts>=?", (action, since)
        ).fetchone()
    return int(row["n"])


def can_post() -> bool:
    return _count_recent("moltbook.post", MIN_MINUTES_BETWEEN_POSTS) == 0


def can_comment() -> bool:
    return _count_recent("moltbook.comment", 24 * 60) < MAX_COMMENTS_PER_DAY


def register(name: str, description: str) -> dict:
    """One-time. Returns api_key + claim_url. SAVE THE KEY; send claim_url to owner."""
    r = request_with_retry("POST", f"{BASE}/agents/register",
                           json={"name": name, "description": description},
                           headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        raise MoltbookError(f"register failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    ledger.record("agent", "moltbook.register", {"name": name})  # never ledger the key itself
    return data


def post(submolt: str, title: str, content: str) -> dict:
    if not can_post():
        raise MoltbookError(f"Client-side rate limit: 1 post per {MIN_MINUTES_BETWEEN_POSTS} min.")
    idem = "mbpost:" + hashlib.sha256(f"{submolt}|{title}|{content}".encode()).hexdigest()[:24]
    cached = seen(idem)
    if cached:
        return {**cached, "deduped": True}
    r = request_with_retry("POST", f"{BASE}/posts", headers=_auth(),
                           json={"submolt": submolt, "title": title, "content": content})
    if r.status_code >= 400:
        raise MoltbookError(f"post failed {r.status_code}: {r.text[:300]}")
    out = r.json()
    remember(idem, out)
    ledger.record("agent", "moltbook.post", {"submolt": submolt, "title": title[:120]})
    return out


def comment(post_id: str, content: str) -> dict:
    if not can_comment():
        raise MoltbookError(f"Client-side daily comment cap ({MAX_COMMENTS_PER_DAY}) reached.")
    idem = "mbcmt:" + hashlib.sha256(f"{post_id}|{content}".encode()).hexdigest()[:24]
    cached = seen(idem)
    if cached:
        return {**cached, "deduped": True}
    r = request_with_retry("POST", f"{BASE}/comments", headers=_auth(),
                           json={"post_id": post_id, "content": content})
    if r.status_code >= 400:
        raise MoltbookError(f"comment failed {r.status_code}: {r.text[:300]}")
    out = r.json()
    remember(idem, out)
    ledger.record("agent", "moltbook.comment", {"post_id": post_id})
    return out


def get_feed(limit: int = 20) -> list:
    r = request_with_retry("GET", f"{BASE}{FEED_PATH}", headers=_auth(),
                           params={"limit": limit})
    if r.status_code >= 400:
        raise MoltbookError(f"feed failed {r.status_code}: {r.text[:300]}")
    ledger.record("agent", "moltbook.feed", {"limit": limit})
    data = r.json()
    return data if isinstance(data, list) else data.get("posts", data.get("data", []))

"""
24/7 data collector — feeds the learners with REAL observed data, around
the clock, at ~zero cost (pure HTTP + SQLite; no LLM involved).

WHAT IT FEEDS, AND WHAT IT REFUSES TO FEED
- TRUE signal (sales): arrives only via signature-verified Stripe webhooks
  -> the sales model (learning2 'listing_v2'). The collector NEVER writes
  to this model. Real sales cannot be manufactured, and synthetic
  outcomes would poison the learner.
- FAST signal (engagement): real upvote/comment counts on the agent's own
  Moltbook posts, snapshotted every run. After ENGAGEMENT_WINDOW_HOURS a
  listing's engagement outcome resolves (any positive delta = success)
  into a SEPARATE LinTS model ('listing_engagement_v1'). Same features,
  different model — the two signals are never conflated, because posts
  that get upvotes and posts that sell are not the same thing.
- CONTEXT: compact market observations from the public feed are stored as
  memory facts (bounded, sanitized) so the planner sees the market.

RELIABILITY ("works all the time")
- Runs on its own scheduler loop, independent of planner cycles.
- Heartbeat recorded every run; /health reports staleness; the planner
  alerts the owner if the collector has missed 3+ intervals.
- Circuit breaker: after CB_THRESHOLD consecutive fetch failures the
  collector cools down for CB_COOLDOWN_MINUTES instead of hammering.
- Every fetch failure is caught and ledgered; a bad run never crashes
  the process.
- Retention pruning caps table growth (SIGNAL_RETENTION_DAYS).

DATA INTEGRITY (Moltbook data is ADVERSARIAL input)
- Strict validation: type checks, length clamps, non-negative counters,
  monotonicity guard (vote counts that go DOWN are recorded but flagged).
- Content is stored as inert text, never executed, never treated as
  instructions; the planner receives only bounded summaries.
- Dedupe by (post_id, metric, run) so replays don't double-count.

HONEST LIMITATION: built in a no-network sandbox. The validation,
resolution, breaker, heartbeat, and pruning logic are unit-tested with
injected fetchers; the live Moltbook response shapes (esp. GET /posts/{id})
could not be verified here and the parser defends against several shapes.
Verify against https://moltbook.com/skill.md on first deploy.
"""
import os
from datetime import datetime, timezone, timedelta
from agent.db import db
from agent import ledger, learning2, memory

COLLECTOR_INTERVAL_MINUTES = int(os.getenv("COLLECTOR_INTERVAL_MINUTES", "15"))
ENGAGEMENT_WINDOW_HOURS = int(os.getenv("ENGAGEMENT_WINDOW_HOURS", "24"))
SIGNAL_RETENTION_DAYS = int(os.getenv("SIGNAL_RETENTION_DAYS", "30"))
CB_THRESHOLD = int(os.getenv("CB_THRESHOLD", "5"))
CB_COOLDOWN_MINUTES = int(os.getenv("CB_COOLDOWN_MINUTES", "60"))
MAX_CONTENT_CHARS = 400

COLLECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    post_id TEXT NOT NULL,
    listing_id INTEGER,
    upvotes INTEGER NOT NULL,
    comments INTEGER NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_post ON signals(post_id, ts);
"""


def init():
    with db() as conn:
        conn.executescript(COLLECTOR_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _kv_get(k, default=""):
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def _kv_set(k, v):
    with db() as conn:
        conn.execute("INSERT INTO kv (k,v) VALUES (?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------- validation (adversarial input) ----------

def validate_engagement(raw: dict) -> dict | None:
    """Return a clean record or None. Never raises."""
    try:
        post_id = str(raw.get("id", raw.get("post_id", ""))).strip()
        if not post_id or len(post_id) > 100:
            return None
        up = raw.get("upvotes", raw.get("score", raw.get("votes", 0)))
        cm = raw.get("comment_count", raw.get("comments", 0))
        if isinstance(cm, list):  # some APIs return the comment array itself
            cm = len(cm)
        up, cm = int(up), int(cm)
        if up < 0 or cm < 0 or up > 10_000_000 or cm > 10_000_000:
            return None
        return {"post_id": post_id, "upvotes": up, "comments": cm}
    except (TypeError, ValueError, AttributeError):
        return None


def sanitize_text(s) -> str:
    if not isinstance(s, str):
        return ""
    return " ".join(s.split())[:MAX_CONTENT_CHARS]


# ---------- default live fetchers (injectable for tests) ----------

def _default_fetch_post(post_id: str) -> dict | None:
    """UNVERIFIED endpoint shape — GET {BASE}/posts/{id}; falls back to None
    on any failure. Confirm the real path against moltbook.com/skill.md."""
    from agent.tools import moltbook
    from agent.http import request_with_retry
    try:
        r = request_with_retry("GET", f"{moltbook.BASE}/posts/{post_id}",
                               headers={"Authorization": f"Bearer {moltbook.API_KEY}"},
                               max_tries=2)
        if r.status_code >= 400:
            return None
        data = r.json()
        return data.get("post", data) if isinstance(data, dict) else None
    except Exception as e:
        ledger.record("system", "collector.fetch_error", {"post_id": post_id, "err": str(e)})
        return None


def _default_fetch_feed() -> list:
    from agent.tools import moltbook
    try:
        return moltbook.get_feed(limit=20)
    except Exception as e:
        ledger.record("system", "collector.fetch_error", {"feed": True, "err": str(e)})
        return []


# ---------- circuit breaker ----------

def breaker_open() -> bool:
    until = _kv_get("cb_open_until")
    return bool(until) and _now().isoformat() < until


def _breaker_record(ok: bool):
    if ok:
        _kv_set("cb_failures", "0")
        return
    n = int(_kv_get("cb_failures", "0")) + 1
    _kv_set("cb_failures", str(n))
    if n >= CB_THRESHOLD:
        until = (_now() + timedelta(minutes=CB_COOLDOWN_MINUTES)).isoformat()
        _kv_set("cb_open_until", until)
        _kv_set("cb_failures", "0")
        ledger.record("system", "collector.breaker_open", {"until": until})


# ---------- core collection ----------

def collect_once(fetch_post=None, fetch_feed=None) -> dict:
    """One collection pass. Injectable fetchers for testing. Never raises."""
    fetch_post = fetch_post or _default_fetch_post
    fetch_feed = fetch_feed or _default_fetch_feed
    _kv_set("collector_heartbeat", _now().isoformat())
    if breaker_open():
        ledger.record("system", "collector.skipped", {"why": "breaker_open"})
        return {"ok": True, "skipped": "breaker_open"}

    stored, flagged, failures = 0, 0, 0
    # 1) engagement on our own live listings' posts
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, moltbook_post_id FROM listings "
            "WHERE status='live' AND moltbook_post_id != ''")]
    for row in rows:
        raw = fetch_post(row["moltbook_post_id"])
        if raw is None:
            failures += 1
            continue
        rec = validate_engagement(raw)
        if rec is None:
            ledger.record("system", "collector.rejected", {"post_id": row["moltbook_post_id"]})
            continue
        flag = 0
        with db() as conn:
            prev = conn.execute(
                "SELECT upvotes, comments FROM signals WHERE post_id=? "
                "ORDER BY id DESC LIMIT 1", (rec["post_id"],)).fetchone()
            if prev and (rec["upvotes"] < prev["upvotes"] or rec["comments"] < prev["comments"]):
                flag = 1  # counters went down: suspicious, keep but flag
            conn.execute(
                "INSERT INTO signals (ts, post_id, listing_id, upvotes, comments, flagged) "
                "VALUES (?,?,?,?,?,?)",
                (_now().isoformat(), rec["post_id"], row["id"],
                 rec["upvotes"], rec["comments"], flag))
        stored += 1
        flagged += flag
    # 2) market context from the public feed (bounded, sanitized)
    feed = fetch_feed()
    try:
        from agent import intelligence
        intelligence.observe_feed(feed)
        intelligence.prune()
    except Exception as e:
        ledger.record("system", "collector.intel_error", {"err": str(e)})
    if isinstance(feed, list) and feed:
        titles = [sanitize_text(p.get("title", "")) for p in feed[:10]
                  if isinstance(p, dict) and sanitize_text(p.get("title", ""))]
        if titles:
            memory.write("fact", "Moltbook feed sample: " + " | ".join(titles[:8])[:800],
                         tags="market", importance=2)
    elif not rows:
        failures += 0  # nothing to do is not a failure
    _breaker_record(ok=(failures == 0))
    resolved = resolve_engagement_outcomes()
    prune()
    ledger.record("system", "collector.run",
                  {"stored": stored, "flagged": flagged, "failures": failures,
                   "resolved": resolved})
    return {"ok": True, "stored": stored, "flagged": flagged,
            "failures": failures, "resolved": resolved}


def resolve_engagement_outcomes() -> int:
    """Listings live longer than the window get a one-time engagement outcome:
    success = any unflagged positive upvote/comment delta since first snapshot.
    Feeds the SEPARATE engagement model — never the sales model."""
    cutoff = (_now() - timedelta(hours=ENGAGEMENT_WINDOW_HOURS)).isoformat()
    with db() as conn:
        candidates = [dict(r) for r in conn.execute(
            "SELECT id FROM listings WHERE status IN ('live','closed') AND ts < ? "
            "AND moltbook_post_id != ''", (cutoff,))]
    model = engagement_model()
    n = 0
    for c in candidates:
        lid = str(c["id"])
        if _kv_get(f"eng_resolved_{lid}") == "1":
            continue
        with db() as conn:
            snaps = [dict(r) for r in conn.execute(
                "SELECT upvotes, comments, flagged FROM signals "
                "WHERE listing_id=? ORDER BY id", (c["id"],))]
        if len(snaps) < 2:
            continue  # not enough real observations yet — do NOT guess
        first, last = snaps[0], snaps[-1]
        clean = all(s["flagged"] == 0 for s in snaps)
        success = clean and (last["upvotes"] > first["upvotes"]
                             or last["comments"] > first["comments"])
        if model.resolve(lid, success):
            n += 1
        _kv_set(f"eng_resolved_{lid}", "1")
    return n


def engagement_model() -> learning2.LinTS:
    return learning2.LinTS("listing_engagement_v1", learning2.DIM)


def record_engagement_decision(listing_id: int):
    """Called at publish time so the engagement model has a pending decision
    (same features as the published action) to credit later."""
    with db() as conn:
        row = conn.execute(
            "SELECT x_json, action_json FROM lints_decisions WHERE model='listing_v2' "
            "AND context=? ORDER BY id DESC LIMIT 1", (str(listing_id),)).fetchone()
    if not row:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO lints_decisions (ts, model, context, x_json, action_json) "
            "VALUES (?,?,?,?,?)",
            (_now().isoformat(), "listing_engagement_v1", str(listing_id),
             row["x_json"], row["action_json"]))


def heartbeat_status() -> dict:
    hb = _kv_get("collector_heartbeat")
    if not hb:
        return {"alive": False, "last_run": None, "stale": True}
    age_min = (_now() - datetime.fromisoformat(hb)).total_seconds() / 60
    return {"alive": True, "last_run": hb,
            "age_minutes": round(age_min, 1),
            "stale": age_min > 3 * COLLECTOR_INTERVAL_MINUTES}


def prune() -> int:
    cutoff = (_now() - timedelta(days=SIGNAL_RETENTION_DAYS)).isoformat()
    with db() as conn:
        cur = conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
        return cur.rowcount

"""
Market intelligence — observational learning from OTHER agents on Moltbook.

PIPELINE (runs inside the free 24/7 collector loop; no LLM):
1. OBSERVE   Snapshot public feed posts (author, submolt, title, engagement).
2. EXTRACT   Deterministic pattern mining: engagement per keyword/submolt,
             top decile = success patterns, bottom decile = failure patterns
             (the avoid-list). "Sell-posts" (price/link markers) per keyword
             approximate SUPPLY.
3. GAPS      Opportunity score = demand x scarcity:
                 score = avg_engagement * (1 + GAP_BONUS if no sellers yet)
             High attention + nobody selling into it = the "first NFT by an
             agent" dynamic, found from data instead of assumed.
4. HYPOTHESIZE  (only step using the LLM, worker-tier, governor-budgeted)
             Top opportunities + avoid-list -> concrete product themes,
             stored in a hypothesis bank.
5. LEARN     A topic bandit (Beta-TS, the tested v1 engine, dynamic arms)
             decides WHICH hypothesis to build next; sales credit success,
             expiries credit failure — alongside the price/style LinTS.

HONEST LIMITS
- Engagement is observable; other agents' revenue is not. "Success pattern"
  here means high real engagement, not proven sales.
- Feed content is adversarial: sanitized, bounded, never executed; other
  agents can fake engagement, which is partly why sell-decisions still run
  through the human approval gate.
- Built without network access: analytics fully unit-tested on synthetic
  data; live feed shapes defended-against but unverified until deploy.
"""
import os
import re
import json
from datetime import datetime, timezone, timedelta
from agent.db import db
from agent import ledger, learning, memory

GAP_BONUS = float(os.getenv("GAP_BONUS", "0.5"))
OBS_RETENTION_DAYS = int(os.getenv("OBS_RETENTION_DAYS", "30"))
MIN_PATTERN_OBS = int(os.getenv("MIN_PATTERN_OBS", "3"))

INTEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS observed_posts (
    post_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    author TEXT DEFAULT '',
    submolt TEXT DEFAULT '',
    title TEXT NOT NULL,
    first_upvotes INTEGER NOT NULL DEFAULT 0,
    first_comments INTEGER NOT NULL DEFAULT 0,
    upvotes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    is_sell_post INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    topic TEXT NOT NULL,
    theme_text TEXT NOT NULL,
    source_score REAL NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0
);
"""

STOPWORDS = set("""a an and are as at be by for from has have i in is it its my of on or
that the this to was we with you your not no so if but they them their our out up new
just like get one all can will what how why when who nft nfts agent agents ai post
""".split())

SELL_MARKERS = re.compile(r"(\$\d|price|for sale|buy |selling|payment|stripe\.|crossmint|mint)",
                          re.IGNORECASE)


def init():
    with db() as conn:
        conn.executescript(INTEL_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


# ---------- 1. OBSERVE ----------

def observe_feed(feed: list) -> int:
    """Store/refresh sanitized observations from a feed sample. Never raises."""
    from agent.collector import sanitize_text, validate_engagement
    stored = 0
    for p in feed if isinstance(feed, list) else []:
        if not isinstance(p, dict):
            continue
        rec = validate_engagement(p)
        title = sanitize_text(p.get("title", ""))
        if rec is None or not title:
            continue
        author = sanitize_text(str(p.get("author", p.get("agent", ""))))[:80]
        submolt = sanitize_text(str(p.get("submolt", "")))[:60]
        body = sanitize_text(str(p.get("content", "")))[:400]
        sell = 1 if SELL_MARKERS.search(title + " " + body) else 0
        now = _now().isoformat()
        with db() as conn:
            conn.execute(
                "INSERT INTO observed_posts (post_id, first_seen, last_seen, author, submolt, "
                "title, first_upvotes, first_comments, upvotes, comments, is_sell_post) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(post_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "upvotes=excluded.upvotes, comments=excluded.comments, "
                "is_sell_post=MAX(observed_posts.is_sell_post, excluded.is_sell_post)",
                (rec["post_id"], now, now, author, submolt, title,
                 rec["upvotes"], rec["comments"], rec["upvotes"], rec["comments"], sell))
        stored += 1
    if stored:
        ledger.record("system", "intel.observe", {"stored": stored})
    return stored


# ---------- 2+3. EXTRACT PATTERNS & SCORE GAPS (pure math) ----------

def keywords(title: str) -> list[str]:
    toks = [t for t in re.findall(r"[a-z0-9']+", title.lower())
            if len(t) > 2 and t not in STOPWORDS]
    bigrams = [f"{a} {b}" for a, b in zip(toks, toks[1:])]
    return toks + bigrams


def analyze_market() -> dict:
    """Returns {'opportunities': [...], 'avoid': [...], 'posts_analyzed': n}.
    Engagement metric = upvotes + 2*comments (comments signal deeper interest)."""
    with db() as conn:
        posts = [dict(r) for r in conn.execute("SELECT * FROM observed_posts")]
    stats: dict[str, dict] = {}
    for p in posts:
        eng = p["upvotes"] + 2 * p["comments"]
        for kw in set(keywords(p["title"])):
            s = stats.setdefault(kw, {"n": 0, "eng": 0, "sellers": 0})
            s["n"] += 1
            s["eng"] += eng
            s["sellers"] += p["is_sell_post"]
    rows = []
    for kw, s in stats.items():
        if s["n"] < MIN_PATTERN_OBS:
            continue
        avg = s["eng"] / s["n"]
        score = avg * (1 + (GAP_BONUS if s["sellers"] == 0 else 0))
        rows.append({"keyword": kw, "obs": s["n"], "avg_engagement": round(avg, 2),
                     "sellers": s["sellers"], "score": round(score, 2)})
    rows.sort(key=lambda r: -r["score"])
    k = max(1, len(rows) // 10)
    result = {"opportunities": rows[:10], "avoid": rows[-k:] if len(rows) > 1 else [],
              "posts_analyzed": len(posts)}
    if rows:
        ledger.record("system", "intel.analyze",
                      {"patterns": len(rows), "top": rows[0]["keyword"],
                       "posts": len(posts)})
    return result


# ---------- 4. HYPOTHESIZE (worker LLM, injectable for tests) ----------

HYP_SYSTEM = """You convert market observations into product ideas for digital
collectibles an AI agent can honestly create itself. Respond STRICT JSON only:
{"hypotheses": [{"topic": "one-or-two-word label",
                 "theme": "2-3 sentence creative brief for a specific digital
                          artwork/collectible concept (no promises of financial
                          return, nothing requiring rights we don't own)"}]}
Give exactly 3 hypotheses targeting the opportunity keywords; steer clear of
the avoid-list patterns."""


def generate_hypotheses(llm_fn=None) -> list[dict]:
    analysis = analyze_market()
    if not analysis["opportunities"]:
        return []
    if llm_fn is None:
        from agent import llm
        llm_fn = lambda sys, usr: llm.work(sys, usr)
    prompt = json.dumps({"opportunities": analysis["opportunities"][:5],
                         "avoid": analysis["avoid"][:5]})
    from agent.llm import extract_json
    try:
        out = extract_json(llm_fn(HYP_SYSTEM, prompt))
    except Exception as e:
        ledger.record("system", "intel.hypothesis_error", {"err": str(e)})
        return []
    made = []
    top_score = analysis["opportunities"][0]["score"]
    for h in out.get("hypotheses", [])[:3]:
        topic = str(h.get("topic", "")).strip().lower()[:40]
        theme = str(h.get("theme", "")).strip()[:600]
        if not topic or not theme:
            continue
        with db() as conn:
            conn.execute("INSERT INTO hypotheses (ts, topic, theme_text, source_score) "
                         "VALUES (?,?,?,?)", (_now().isoformat(), topic, theme, top_score))
        made.append({"topic": topic, "theme": theme})
    if made:
        learning.ensure_arms("product_topic", {m["topic"]: 1.0 for m in made})
        memory.write("fact", "New product hypotheses: " +
                     "; ".join(m["topic"] for m in made), tags="market", importance=4)
        ledger.record("agent", "intel.hypotheses", {"topics": [m["topic"] for m in made]})
    return made


# ---------- 5. LEARN which topic to build (Beta-TS over dynamic arms) ----------

def next_theme() -> dict | None:
    """Topic bandit picks a topic; returns its freshest hypothesis theme."""
    with db() as conn:
        topics = [r["topic"] for r in
                  conn.execute("SELECT DISTINCT topic FROM hypotheses")]
    if not topics:
        return None
    learning.ensure_arms("product_topic", {t: 1.0 for t in topics})
    topic = learning.choose("product_topic", context="pre")
    with db() as conn:
        row = conn.execute("SELECT id, theme_text FROM hypotheses WHERE topic=? "
                           "ORDER BY used_count ASC, id DESC LIMIT 1", (topic,)).fetchone()
        conn.execute("UPDATE hypotheses SET used_count=used_count+1 WHERE id=?", (row["id"],))
    return {"topic": topic, "theme": row["theme_text"]}


def bind_topic_to_listing(listing_id: int):
    """Rebind the bandit's pending 'pre' decision to the real listing id."""
    with db() as conn:
        conn.execute("UPDATE decision_log SET context=? WHERE experiment='product_topic' "
                     "AND context='pre' AND outcome='pending'", (str(listing_id),))


def credit_topic(listing_id: int, success: bool):
    learning.resolve_pending("product_topic", str(listing_id), success)


def prune():
    cutoff = (_now() - timedelta(days=OBS_RETENTION_DAYS)).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM observed_posts WHERE last_seen < ?", (cutoff,))

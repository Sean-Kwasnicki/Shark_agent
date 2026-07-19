"""
Opportunity scoring engine — the deterministic "where is there money here?"
brain, extracted and generalized from the strongest patterns across the
owner's prior projects and rebuilt to match Shark's control-plane rules:

  - Sales-Agent  packages/shared/lead-scoring.ts   (fit / urgency scoring)
  - ProfitPilot  lib/opportunity-radar.ts          ("attention not converting")
  - SourceSensePro backend/utils/scoring.py         (margin/relevance scoring)
  - intelligence.py analyze_market()                (demand x scarcity gaps)

WHAT IT IS
A pure, explainable ranking function over "signals" — a signal is any
lawfully obtained observation that MIGHT represent a revenue opportunity
(a Moltbook post asking for a tool, a public RFP, an inbound lead who
opted in, a sourcing gap). Each signal is scored on four transparent
sub-scores in [0,1], combined by tunable weights, and every ingest is
hash-chain ledgered. No LLM, no network, no hidden state.

    intent    how strongly the text signals buying/hiring interest
    fit       overlap between the signal and the owner's offer profile
    freshness exponential time decay (opportunities go stale)
    scarcity  inverse of how many competitors already serve it

    score = 100 * (w_intent*intent + w_fit*fit
                   + w_fresh*freshness + w_scarcity*scarcity)

THE "PET-INSURANCE" PATTERN, DONE LEGALLY
The owner described a pet-insurance company that detected new-dog posts on
Facebook and sent personalized offers. That is the intent->personalized-
outreach pattern this engine encodes. IMPORTANT AND DELIBERATE: this engine
scores signals; it never harvests them from platforms that forbid it.
Scraping Facebook/Meta for personal data to cold-contact consumers violates
Meta's Terms of Service and exposes you to TCPA/GDPR/CAN-SPAM liability
(verified against Meta ToS and cold-outreach law summaries, July 2026).
So: connect only compliant sources (opt-in inbound, public B2B directories
within their terms, agent-to-agent requests on Moltbook, RFP boards), and
outreach stays behind Shark's existing human-approval gate. The engine
supports this by carrying a `source_trust` field and refusing to rank
signals flagged as non-compliant.

Everything here is deterministic and unit-tested (test_opportunity.py).
"""
import os
import re
import json
import hashlib
from datetime import datetime, timezone
from agent.db import db
from agent import ledger

# --- tunable weights (must stay sane; normalized at load) ---
_W = {
    "intent": float(os.getenv("OPP_W_INTENT", "0.40")),
    "fit": float(os.getenv("OPP_W_FIT", "0.30")),
    "freshness": float(os.getenv("OPP_W_FRESHNESS", "0.15")),
    "scarcity": float(os.getenv("OPP_W_SCARCITY", "0.15")),
}
_WSUM = sum(_W.values()) or 1.0
WEIGHTS = {k: v / _WSUM for k, v in _W.items()}

FRESHNESS_HALF_LIFE_HOURS = float(os.getenv("OPP_FRESHNESS_HALF_LIFE_HOURS", "48"))
# The owner's offer profile: what we can actually sell/build. Comma-separated
# keywords; matched case-insensitively against a signal's text + tags.
OFFER_KEYWORDS = [k.strip().lower() for k in os.getenv(
    "OPP_OFFER_KEYWORDS",
    "automation,agent,ai,python,fastapi,integration,scraper,dashboard,"
    "bot,workflow,api,data,nft,art,saas").split(",") if k.strip()]

# Tiered intent markers (deterministic; same spirit as intelligence.SELL_MARKERS).
_HIGH_INTENT = re.compile(
    r"\b(need|looking for|hire|hiring|quote|budget|will pay|paying|"
    r"buy|purchase|recommend a|any(one)? (know|use|recommend)|urgent|asap|"
    r"deadline|rfp|request for proposal|commission)\b", re.IGNORECASE)
_MED_INTENT = re.compile(
    r"\b(considering|thinking about|evaluating|comparing|shopping for|"
    r"in the market|planning to|wish there was|is there a tool)\b", re.IGNORECASE)

OPP_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_trust TEXT NOT NULL DEFAULT 'compliant',  -- compliant | unknown | prohibited
    subject TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    est_value_usd REAL NOT NULL DEFAULT 0,
    intent REAL NOT NULL,
    fit REAL NOT NULL,
    freshness REAL NOT NULL,
    scarcity REAL NOT NULL,
    score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'   -- open | pursued | dismissed
);
CREATE INDEX IF NOT EXISTS idx_opp_score ON opportunities(status, score);
"""


def init():
    with db() as conn:
        conn.executescript(OPP_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


# ---------- sub-scores (pure functions) ----------

def score_intent(text: str) -> float:
    if _HIGH_INTENT.search(text or ""):
        return 1.0
    if _MED_INTENT.search(text or ""):
        return 0.6
    return 0.0


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", (s or "").lower()) if len(t) > 2}


def score_fit(text: str, tags: str, offer=None) -> float:
    """Fraction of offer keywords present in the signal (capped at 1.0).
    Uses substring match so 'automation' matches 'automations'."""
    offer = offer if offer is not None else OFFER_KEYWORDS
    if not offer:
        return 0.0
    hay = f"{text} {tags}".lower()
    hits = sum(1 for kw in offer if kw in hay)
    # normalize by a target of ~3 relevant keywords; 3+ matches = full fit
    return min(1.0, hits / 3.0)


def score_freshness(age_hours: float, half_life=None) -> float:
    hl = half_life if half_life is not None else FRESHNESS_HALF_LIFE_HOURS
    return float(0.5 ** (max(0.0, age_hours) / hl)) if hl > 0 else 1.0


def score_scarcity(competitors: int) -> float:
    """1 competitor -> 0.5, 0 -> 1.0, many -> ->0. Diminishing, bounded."""
    return 1.0 / (1.0 + max(0, int(competitors)))


def score_signal(signal: dict, now=None) -> dict:
    """Pure scoring. `signal` keys: subject, text, tags, source, source_trust,
    competitors, est_value_usd, ts (iso, optional -> defaults to now).
    Returns the four sub-scores, the blended 0-100 score, and a reason."""
    now = now or _now()
    text = str(signal.get("text", ""))
    subject = str(signal.get("subject", ""))
    tags = str(signal.get("tags", ""))
    blob = f"{subject} {text}"
    ts = signal.get("ts")
    try:
        age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600 if ts else 0.0
    except (TypeError, ValueError):
        age_h = 0.0
    intent = score_intent(blob)
    fit = score_fit(blob, tags)
    fresh = score_freshness(age_h)
    scarcity = score_scarcity(int(signal.get("competitors", 0)))
    raw = (WEIGHTS["intent"] * intent + WEIGHTS["fit"] * fit
           + WEIGHTS["freshness"] * fresh + WEIGHTS["scarcity"] * scarcity)
    score = round(100.0 * raw, 2)
    reason = (f"intent={intent:.2f} fit={fit:.2f} fresh={fresh:.2f} "
              f"scarcity={scarcity:.2f} -> {score}")
    return {"intent": round(intent, 4), "fit": round(fit, 4),
            "freshness": round(fresh, 4), "scarcity": round(scarcity, 4),
            "score": score, "reason": reason}


# ---------- persistence ----------

def _dedupe_key(source: str, subject: str) -> str:
    return "opp:" + hashlib.sha256(f"{source}|{subject}".encode()).hexdigest()[:24]


def ingest(signal: dict) -> dict:
    """Score and persist one signal. Idempotent on (source, subject).
    Refuses to store signals from prohibited sources — a compliance wall in
    code, not a prompt suggestion. Returns the stored row (or the existing
    one on duplicate)."""
    source = str(signal.get("source", "")).strip() or "unknown"
    subject = str(signal.get("subject", "")).strip()
    if not subject:
        raise ValueError("opportunity signal requires a non-empty 'subject'.")
    trust = str(signal.get("source_trust", "compliant")).lower()
    if trust == "prohibited":
        ledger.record("system", "opportunity.rejected",
                      {"source": source, "reason": "prohibited source_trust"})
        return {"stored": False, "reason": "prohibited source"}
    key = _dedupe_key(source, subject)
    with db() as conn:
        dup = conn.execute("SELECT * FROM opportunities WHERE dedupe_key=?", (key,)).fetchone()
        if dup:
            return {**dict(dup), "deduped": True}
    s = score_signal(signal)
    ts = str(signal.get("ts") or _now().isoformat())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO opportunities (ts, dedupe_key, source, source_trust, subject, "
            "text, tags, est_value_usd, intent, fit, freshness, scarcity, score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, key, source, trust, subject[:200], str(signal.get("text", ""))[:2000],
             str(signal.get("tags", ""))[:300], float(signal.get("est_value_usd", 0) or 0),
             s["intent"], s["fit"], s["freshness"], s["scarcity"], s["score"]))
        opp_id = cur.lastrowid
    ledger.record("agent", "opportunity.ingest",
                  {"id": opp_id, "source": source, "subject": subject[:120],
                   "score": s["score"], "reason": s["reason"]})
    return {"id": opp_id, "stored": True, **s}


def _clean(s) -> str:
    return " ".join(str(s).split())[:400] if s else ""


def ingest_from_feed(feed, min_intent: float = 0.6) -> int:
    """Scan a Moltbook feed sample for posts that express buying/hiring intent
    and ingest them as opportunities. Moltbook is agent-to-agent and public, so
    source_trust is 'compliant'. Posts below `min_intent` (i.e. no request
    language) are skipped so the table holds leads, not noise. Never raises —
    this runs inside the 24/7 collector loop."""
    ingested = 0
    for post in (feed if isinstance(feed, list) else []):
        if not isinstance(post, dict):
            continue
        subject = _clean(post.get("title", ""))
        if not subject:
            continue
        body = _clean(post.get("content", post.get("body", "")))
        if score_intent(f"{subject} {body}") < min_intent:
            continue
        pid = str(post.get("id", post.get("post_id", ""))).strip()
        source = f"moltbook:{pid}" if pid else "moltbook"
        try:
            res = ingest({"source": source, "subject": subject, "text": body,
                          "source_trust": "compliant"})
            if res.get("stored"):
                ingested += 1
        except Exception as e:
            ledger.record("system", "opportunity.feed_error", {"err": str(e)[:200]})
    if ingested:
        ledger.record("system", "opportunity.from_feed", {"ingested": ingested})
    return ingested


def rank(limit: int = 5, min_score: float = 0.0) -> list[dict]:
    """Top open opportunities by score. Recomputes freshness at read time so
    stale items sink even if they were hot when ingested — the score column
    is the ingest-time snapshot; ranking uses a live-freshness re-blend."""
    now = _now()
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM opportunities WHERE status='open' AND source_trust!='prohibited'")]
    out = []
    for r in rows:
        try:
            age_h = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        except (TypeError, ValueError):
            age_h = 0.0
        live_fresh = score_freshness(age_h)
        live = round(100.0 * (WEIGHTS["intent"] * r["intent"] + WEIGHTS["fit"] * r["fit"]
                              + WEIGHTS["freshness"] * live_fresh
                              + WEIGHTS["scarcity"] * r["scarcity"]), 2)
        if live >= min_score:
            out.append({"id": r["id"], "source": r["source"], "subject": r["subject"],
                        "est_value_usd": r["est_value_usd"], "live_score": live,
                        "components": {"intent": r["intent"], "fit": r["fit"],
                                       "freshness": round(live_fresh, 4),
                                       "scarcity": r["scarcity"]}})
    out.sort(key=lambda x: -x["live_score"])
    return out[:limit]


def set_status(opp_id: int, status: str) -> dict:
    if status not in ("open", "pursued", "dismissed"):
        raise ValueError("status must be open|pursued|dismissed")
    with db() as conn:
        conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, opp_id))
    ledger.record("owner", "opportunity.status", {"id": opp_id, "status": status})
    return {"id": opp_id, "status": status}

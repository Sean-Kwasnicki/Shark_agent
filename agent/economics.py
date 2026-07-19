"""
Economics engine — makes "earn more than it costs" an enforced property,
not a hope. Three deterministic parts:

1. COST ACCOUNTING
   Every LLM call's real token usage x verified per-MTok price is ledgered
   as action 'cost.llm'. Infra cost (MONTHLY_INFRA_USD, e.g. Railway) is
   accrued daily as 'cost.infra'. Revenue already ledgers as
   'payments.received'. Net = revenue - api - infra - purchases, all
   computable from the hash-chained ledger at any window.

   Prices verified July 2026 (docs.claude.com/platform pricing pages):
   sonnet-4.6 $3/$15, haiku-4.5 $1/$5 per MTok. Override via
   PRICE_TABLE_JSON env when prices or models change.

2. GOVERNOR (state machine, evaluated every cycle)
   HARD STOP  today's cost >= DAILY_COST_BUDGET_USD -> no LLM until tomorrow
   HIBERNATE  7-day net <= -HIBERNATE_LOSS_USD      -> 1 planning cycle/day,
              cheap model, owner notified
   CONSERVE   7-day net < 0                          -> plan with the cheap
              model, skip every other cycle
   NORMAL     7-day net >= 0                         -> full planner
   GROW       7-day net >= GROW_NET_USD              -> full planner, may
              shorten interval (bounded by config caps)
   The model proposes nothing here — pure arithmetic on the ledger.

3. IDLE SKIP (the biggest cost lever)
   A fingerprint of actionable state (open tasks, pending approvals, live
   listings, unresolved decisions). If nothing changed since the last
   planned cycle, the LLM is not called at all — ledgered as a skip — with
   a forced full cycle every FORCE_CYCLE_EVERY_N skips so the agent never
   goes fully blind.
"""
import os
import json
import hashlib
from datetime import datetime, timezone, timedelta
from agent.db import db
from agent import ledger

# --- verified defaults; override via env as prices/models change ---
_DEFAULT_PRICES = {          # USD per 1M tokens: [input, output]
    "claude-sonnet-4-6": [3.00, 15.00],
    "claude-haiku-4-5-20251001": [1.00, 5.00],
}
PRICE_TABLE = json.loads(os.getenv("PRICE_TABLE_JSON", "null")) or _DEFAULT_PRICES
MONTHLY_INFRA_USD = float(os.getenv("MONTHLY_INFRA_USD", "5.00"))
DAILY_COST_BUDGET_USD = float(os.getenv("DAILY_COST_BUDGET_USD", "1.00"))
HIBERNATE_LOSS_USD = float(os.getenv("HIBERNATE_LOSS_USD", "10.00"))
GROW_NET_USD = float(os.getenv("GROW_NET_USD", "10.00"))
FORCE_CYCLE_EVERY_N = int(os.getenv("FORCE_CYCLE_EVERY_N", "6"))

ECON_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def init():
    with db() as conn:
        conn.executescript(ECON_SCHEMA)


def _kv_get(k: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def _kv_set(k: str, v: str):
    with db() as conn:
        conn.execute("INSERT INTO kv (k, v) VALUES (?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def _now():
    return datetime.now(timezone.utc)


# ---------- 1. cost accounting ----------

def llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICE_TABLE.get(model)
    if p is None:  # unknown model: charge at the most expensive known rate (fail-safe)
        p = max(PRICE_TABLE.values(), key=lambda x: x[1])
    return input_tokens * p[0] / 1e6 + output_tokens * p[1] / 1e6


def record_llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    c = llm_cost(model, input_tokens, output_tokens)
    ledger.record("system", "cost.llm",
                  {"model": model, "in": input_tokens, "out": output_tokens}, cost_usd=c)
    return c


def accrue_infra_daily():
    """Idempotent per UTC day: prorate monthly infra cost into the ledger."""
    today = _now().date().isoformat()
    if _kv_get("infra_last_day") == today:
        return 0.0
    daily = MONTHLY_INFRA_USD / 30.0
    ledger.record("system", "cost.infra", {"day": today, "monthly": MONTHLY_INFRA_USD},
                  cost_usd=daily)
    _kv_set("infra_last_day", today)
    return daily


def _sum(action: str, since_iso: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) AS s FROM ledger WHERE action=?"
    args: list = [action]
    if since_iso:
        q += " AND ts >= ?"
        args.append(since_iso)
    with db() as conn:
        return float(conn.execute(q, args).fetchone()["s"])


def pnl(days: int | None = None) -> dict:
    since = (_now() - timedelta(days=days)).isoformat() if days else None
    revenue = _sum("payments.received", since)
    api = _sum("cost.llm", since)
    infra = _sum("cost.infra", since)
    purchases = _sum("purchase.execute", since)
    return {"window_days": days, "revenue": round(revenue, 4),
            "cost_api": round(api, 4), "cost_infra": round(infra, 4),
            "cost_purchases": round(purchases, 4),
            "net": round(revenue - api - infra - purchases, 4)}


# ---------- 2. governor ----------

def govern() -> dict:
    """Pure arithmetic on the ledger -> operating decision for this cycle."""
    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    # Daily budget covers OPERATING burn (LLM + infra). Purchases are governed
    # separately by constraints.check_spend caps; counting them here too would
    # let one approved purchase mute the agent for the day.
    cost_today = _sum("cost.llm", today_start) + _sum("cost.infra", today_start)
    week = pnl(days=7)
    if cost_today >= DAILY_COST_BUDGET_USD:
        d = {"mode": "HARD_STOP", "allow_llm": False, "planner_model": None,
             "reason": f"daily cost ${cost_today:.2f} >= budget ${DAILY_COST_BUDGET_USD:.2f}"}
    elif week["net"] <= -HIBERNATE_LOSS_USD:
        last = _kv_get("hibernate_last_day")
        today = _now().date().isoformat()
        allow = last != today
        if allow:
            _kv_set("hibernate_last_day", today)
        d = {"mode": "HIBERNATE", "allow_llm": allow, "planner_model": "worker",
             "reason": f"7d net ${week['net']:.2f} <= -${HIBERNATE_LOSS_USD:.2f}; 1 cycle/day"}
    elif week["net"] < 0:
        n = int(_kv_get("conserve_counter", "0")) + 1
        _kv_set("conserve_counter", str(n))
        d = {"mode": "CONSERVE", "allow_llm": (n % 2 == 0), "planner_model": "worker",
             "reason": f"7d net ${week['net']:.2f} < 0; cheap model, every other cycle"}
    elif week["net"] >= GROW_NET_USD:
        d = {"mode": "GROW", "allow_llm": True, "planner_model": "planner",
             "reason": f"7d net ${week['net']:.2f} >= ${GROW_NET_USD:.2f}"}
    else:
        d = {"mode": "NORMAL", "allow_llm": True, "planner_model": "planner",
             "reason": f"7d net ${week['net']:.2f}"}
    d["cost_today"] = round(cost_today, 4)
    d["net_7d"] = week["net"]
    ledger.record("system", "governor.decision", d)
    return d


# ---------- 3. idle skip ----------

def state_fingerprint() -> str:
    with db() as conn:
        t = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        s = conn.execute("SELECT COUNT(*) AS n FROM spend_requests WHERE status='pending'").fetchone()["n"]
        l = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS m FROM listings "
                         "WHERE status IN ('draft','approved','live')").fetchone()
        o = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    return hashlib.sha256(f"{t}|{s}|{l['n']}|{l['m']}|{o}".encode()).hexdigest()[:16]


def should_skip_idle() -> bool:
    fp = state_fingerprint()
    if fp != _kv_get("last_fingerprint"):
        _kv_set("last_fingerprint", fp)
        _kv_set("skip_streak", "0")
        return False
    streak = int(_kv_get("skip_streak", "0")) + 1
    if streak >= FORCE_CYCLE_EVERY_N:
        _kv_set("skip_streak", "0")
        return False
    _kv_set("skip_streak", str(streak))
    return True


# ---------- daily owner digest ----------

def daily_digest_if_due() -> str | None:
    today = _now().date().isoformat()
    if _kv_get("digest_last_day") == today:
        return None
    _kv_set("digest_last_day", today)
    day, week, alltime = pnl(1), pnl(7), pnl(None)
    try:
        from agent import learning2
        top = learning2.report()[:3]
        learned = ", ".join(f"${t['price']:.0f}x{t['style']} EV ${t['expected_revenue']:.2f}"
                            for t in top)
    except Exception:
        learned = "n/a"
    gov = _kv_get("last_mode", "NORMAL")
    msg = (f"Daily P&L — today net ${day['net']:.2f} "
           f"(rev ${day['revenue']:.2f} / api ${day['cost_api']:.2f} / infra ${day['cost_infra']:.2f}) | "
           f"7d net ${week['net']:.2f} | all-time net ${alltime['net']:.2f} | mode {gov}. "
           f"Top learned actions: {learned}")
    return msg

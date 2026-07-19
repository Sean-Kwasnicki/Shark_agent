"""
Learning engine — deterministic math over outcome data. No LLM involved.

Algorithm: Thompson sampling with Beta-Bernoulli posteriors, extended to be
REVENUE-AWARE. For each decision ("experiment") the agent maintains, per
option ("arm"), a Beta(alpha, beta) posterior over conversion probability:

    alpha = 1 + decayed successes      beta = 1 + decayed failures

To choose, it draws p_i ~ Beta(alpha_i, beta_i) for every arm and picks
argmax over p_i * value_i (value = revenue of that arm, default 1.0).
This makes it maximize expected REVENUE, not just conversion rate — a $25
listing converting at 3% (EV $0.75) beats a $5 listing at 10% (EV $0.50),
and the bandit finds that on its own.

Why this algorithm for this agent:
- Learns online from tiny samples (early Moltbook sales will be sparse)
- Balances exploration vs exploitation automatically (no epsilon tuning)
- Pure math + SQLite: auditable, restart-safe, no model files, no drift
  from prompt changes — matches the deterministic-control-plane design.

Nonstationarity: markets shift, so decay() geometrically down-weights old
evidence (counts *= gamma), keeping the posterior responsive. Call it from
the cycle loop (planner does this once per cycle).

Every choice and outcome is ledgered, so the learning itself is auditable.
"""
import json
import random
from datetime import datetime, timezone
from agent.db import db
from agent import ledger

LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS bandit_arms (
    experiment TEXT NOT NULL,
    arm TEXT NOT NULL,
    successes REAL NOT NULL DEFAULT 0,
    failures REAL NOT NULL DEFAULT 0,
    value REAL NOT NULL DEFAULT 1.0,       -- reward per success (e.g. price)
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (experiment, arm)
);
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    experiment TEXT NOT NULL,
    arm TEXT NOT NULL,
    context TEXT DEFAULT '',               -- e.g. listing_id, for credit assignment
    outcome TEXT DEFAULT 'pending'         -- pending | success | failure
);
"""

_rng = random.Random()  # seedable in tests via seed()


def init():
    with db() as conn:
        conn.executescript(LEARNING_SCHEMA)


def seed(n: int):
    _rng.seed(n)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_arms(experiment: str, arms: dict[str, float]):
    """Register arms with their per-success value (e.g. {'price_5': 5.0})."""
    with db() as conn:
        for arm, value in arms.items():
            conn.execute(
                "INSERT INTO bandit_arms (experiment, arm, value, updated_ts) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(experiment, arm) DO UPDATE SET value=excluded.value",
                (experiment, arm, float(value), _now()),
            )


def choose(experiment: str, context: str = "") -> str:
    """Thompson sampling: draw from each arm's posterior, pick max expected revenue.
    Logs the decision (pending) for later credit assignment. Returns arm name."""
    with db() as conn:
        rows = conn.execute(
            "SELECT arm, successes, failures, value FROM bandit_arms WHERE experiment=?",
            (experiment,),
        ).fetchall()
    if not rows:
        raise ValueError(f"No arms registered for experiment '{experiment}'.")
    best_arm, best_score, samples = None, -1.0, {}
    for r in rows:
        p = _rng.betavariate(1.0 + r["successes"], 1.0 + r["failures"])
        score = p * r["value"]
        samples[r["arm"]] = round(score, 4)
        if score > best_score:
            best_arm, best_score = r["arm"], score
    with db() as conn:
        conn.execute(
            "INSERT INTO decision_log (ts, experiment, arm, context) VALUES (?,?,?,?)",
            (_now(), experiment, best_arm, context),
        )
    ledger.record("agent", "learning.choose",
                  {"experiment": experiment, "arm": best_arm, "context": context,
                   "sampled_scores": samples})
    return best_arm


def record_outcome(experiment: str, arm: str, success: bool, context: str = ""):
    """Update the arm's posterior with one observed outcome."""
    col = "successes" if success else "failures"
    with db() as conn:
        conn.execute(
            f"UPDATE bandit_arms SET {col} = {col} + 1, updated_ts=? "
            "WHERE experiment=? AND arm=?", (_now(), experiment, arm))
        conn.execute(
            "UPDATE decision_log SET outcome=? WHERE id = ("
            "  SELECT id FROM decision_log WHERE experiment=? AND arm=? AND context=? "
            "  AND outcome='pending' ORDER BY id LIMIT 1)",
            ("success" if success else "failure", experiment, arm, context))
    ledger.record("system", "learning.outcome",
                  {"experiment": experiment, "arm": arm, "success": success,
                   "context": context})


def resolve_pending(experiment: str, context: str, success: bool):
    """Credit assignment by context (e.g. listing_id): find which arm was chosen
    for this context and record the outcome against it. Idempotent per decision."""
    with db() as conn:
        row = conn.execute(
            "SELECT arm FROM decision_log WHERE experiment=? AND context=? "
            "AND outcome='pending' ORDER BY id LIMIT 1", (experiment, context)).fetchone()
    if row:
        record_outcome(experiment, row["arm"], success, context)
        return row["arm"]
    return None


def decay(gamma: float = 0.98):
    """Geometric down-weighting of old evidence; keeps the bandit adaptive."""
    with db() as conn:
        conn.execute("UPDATE bandit_arms SET successes = successes * ?, "
                     "failures = failures * ?, updated_ts=?", (gamma, gamma, _now()))


def report() -> list[dict]:
    """Posterior means + expected revenue per arm — the agent's learned beliefs."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM bandit_arms ORDER BY experiment, arm").fetchall()
    out = []
    for r in rows:
        a, b = 1.0 + r["successes"], 1.0 + r["failures"]
        mean = a / (a + b)
        out.append({
            "experiment": r["experiment"], "arm": r["arm"],
            "observations": round(r["successes"] + r["failures"], 2),
            "conversion_mean": round(mean, 4),
            "expected_revenue": round(mean * r["value"], 4),
            "value": r["value"],
        })
    return out

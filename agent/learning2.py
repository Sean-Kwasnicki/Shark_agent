"""
Contextual learning engine v2 — Linear Thompson Sampling (LinTS).

WHY THIS UPGRADE
v1 (learning.py) treats every option as independent: 4 prices x 3 styles =
12 arms that each learn alone, so evidence about "$15 + story" teaches
nothing about "$15 + utility". LinTS instead learns a Bayesian posterior
over FEATURE WEIGHTS (price, price^2, style, price x style), so every
outcome updates beliefs about all related actions. With sparse sales data
— your regime — information sharing is where the real gain is.

THE ALGORITHM (deterministic math, numpy only, no LLM)
Model conversion p(x) ≈ w·x with Gaussian posterior over w:
    A = λI + Σ γ^age · x xᵀ        (precision)
    b = Σ γ^age · y x
    μ = A⁻¹ b,  Σw = v² A⁻¹
Choose: sample w̃ ~ N(μ, Σw) via Cholesky, pick the action maximizing
    clip(w̃·x, 0, 1) × price       (expected REVENUE, not conversion)
Update on outcome y ∈ {0,1}: A += x xᵀ, b += y x.
Nonstationarity: discount() applies A ← γ(A−λI)+λI, b ← γb.

KNOWN TRADEOFFS (stated, not hidden)
- Linear-probability model for a binary outcome: standard practice in
  bandit systems and fine in the 0–50% conversion range we operate in;
  a Bayesian logistic model would be more correct at extremes but needs
  approximations that add failure modes for negligible gain here.
- Exploration scale v is a hyperparameter (default 0.3). Set v higher to
  explore more; the eval harness (test_learning2.py) is how you tune it.

Persistence: A, b stored as JSON in SQLite; decisions logged with their
feature vectors for exact credit assignment. Everything ledgered.
"""
import json
import numpy as np
from datetime import datetime, timezone
from agent.db import db
from agent import ledger

LINTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lints_models (
    name TEXT PRIMARY KEY,
    dim INTEGER NOT NULL,
    A_json TEXT NOT NULL,
    b_json TEXT NOT NULL,
    lam REAL NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lints_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    context TEXT NOT NULL,
    x_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending'  -- pending | success | failure
);
"""

_rng = np.random.default_rng()


def init():
    with db() as conn:
        conn.executescript(LINTS_SCHEMA)


def seed(n: int):
    global _rng
    _rng = np.random.default_rng(n)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LinTS:
    def __init__(self, name: str, dim: int, lam: float = 1.0, v: float = 0.3):
        self.name, self.dim, self.lam, self.v = name, dim, lam, v
        self.A = np.eye(dim) * lam
        self.b = np.zeros(dim)
        self._load()

    # ---------- persistence ----------
    def _load(self):
        with db() as conn:
            row = conn.execute("SELECT * FROM lints_models WHERE name=?", (self.name,)).fetchone()
        if row and row["dim"] == self.dim:
            self.A = np.array(json.loads(row["A_json"]))
            self.b = np.array(json.loads(row["b_json"]))
            self.lam = row["lam"]

    def _save(self):
        with db() as conn:
            conn.execute(
                "INSERT INTO lints_models (name, dim, A_json, b_json, lam, updated_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "A_json=excluded.A_json, b_json=excluded.b_json, updated_ts=excluded.updated_ts",
                (self.name, self.dim, json.dumps(self.A.tolist()),
                 json.dumps(self.b.tolist()), self.lam, _now()),
            )

    # ---------- core math ----------
    def _sample_weights(self) -> np.ndarray:
        A_inv = np.linalg.inv(self.A)
        mu = A_inv @ self.b
        cov = (self.v ** 2) * A_inv
        cov = (cov + cov.T) / 2  # symmetrize against float drift
        L = np.linalg.cholesky(cov + 1e-10 * np.eye(self.dim))
        return mu + L @ _rng.standard_normal(self.dim)

    def choose(self, candidates: list[dict], featurize, context: str) -> dict:
        """candidates: list of action dicts each containing 'value' (revenue).
        featurize(action) -> np.ndarray(dim). Picks argmax sampled p * value."""
        w = self._sample_weights()
        best, best_score, best_x = None, -np.inf, None
        for a in candidates:
            x = featurize(a)
            p = float(np.clip(w @ x, 0.0, 1.0))
            score = p * float(a["value"])
            if score > best_score:
                best, best_score, best_x = a, score, x
        with db() as conn:
            conn.execute(
                "INSERT INTO lints_decisions (ts, model, context, x_json, action_json) "
                "VALUES (?,?,?,?,?)",
                (_now(), self.name, context, json.dumps(best_x.tolist()),
                 json.dumps(best, default=str)),
            )
        ledger.record("agent", "learning2.choose",
                      {"model": self.name, "context": context, "action": best,
                       "sampled_ev": round(best_score, 4)})
        return best

    def update(self, x: np.ndarray, y: float):
        self.A += np.outer(x, x)
        self.b += y * x
        self._save()

    def resolve(self, context: str, success: bool):
        """Credit assignment: apply the outcome to the exact features of the
        pending decision made for this context. Idempotent per decision."""
        with db() as conn:
            row = conn.execute(
                "SELECT id, x_json FROM lints_decisions WHERE model=? AND context=? "
                "AND outcome='pending' ORDER BY id LIMIT 1", (self.name, context)).fetchone()
        if not row:
            return False
        self.update(np.array(json.loads(row["x_json"])), 1.0 if success else 0.0)
        with db() as conn:
            conn.execute("UPDATE lints_decisions SET outcome=? WHERE id=?",
                         ("success" if success else "failure", row["id"]))
        ledger.record("system", "learning2.outcome",
                      {"model": self.name, "context": context, "success": success})
        return True

    def discount(self, gamma: float = 0.995):
        self.A = gamma * (self.A - self.lam * np.eye(self.dim)) + self.lam * np.eye(self.dim)
        self.b = gamma * self.b
        self._save()

    def posterior_mean_p(self, x: np.ndarray) -> float:
        mu = np.linalg.solve(self.A, self.b)
        return float(np.clip(mu @ x, 0.0, 1.0))


# ---------- Listing model: price x style ----------
PRICES = [3.0, 8.0, 15.0, 25.0]
STYLES = ["story", "utility", "transparent_agent"]
DIM = 3 + 2 * len(STYLES)   # [1, p, p^2] + style one-hots + p*style interactions


def featurize_listing(action: dict) -> np.ndarray:
    p = float(action["price"]) / 25.0
    s = np.zeros(len(STYLES)); s[STYLES.index(action["style"])] = 1.0
    return np.concatenate(([1.0, p, p * p], s, p * s))


def listing_candidates() -> list[dict]:
    return [{"price": pr, "style": st, "value": pr} for pr in PRICES for st in STYLES]


def listing_model() -> LinTS:
    return LinTS("listing_v2", DIM)


def report() -> list[dict]:
    """Posterior-mean conversion and EV for every price x style action."""
    m = listing_model()
    out = []
    for a in listing_candidates():
        p = m.posterior_mean_p(featurize_listing(a))
        out.append({"price": a["price"], "style": a["style"],
                    "conversion_mean": round(p, 4),
                    "expected_revenue": round(p * a["price"], 4)})
    return sorted(out, key=lambda r: -r["expected_revenue"])

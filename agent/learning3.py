"""
Learning engine v3 — Bayesian LOGISTIC Thompson Sampling (Laplace-TS).

WHY THIS UPGRADE (and what it honestly fixes)
v2 (learning2.py, LinTS) models conversion probability as a LINEAR function
of features and clips it into [0,1]. Its own docstring states the tradeoff:
"fine in the 0-50% conversion range, less exact at extremes." v3 removes
that tradeoff by modeling the binary buy/no-buy outcome with the correct
likelihood:

    p(sale | x) = sigmoid(w . x)

and maintaining a Laplace (Gaussian) approximation to the posterior over w:

    w_MAP = argmax  sum_i  m_i * log Bernoulli(y_i | sigmoid(w.x_i))
                    - (lam/2)||w||^2          (found by Newton-Raphson)
    H     = lam*I + sum_i m_i * s_i(1-s_i) x_i x_i^T   (posterior precision)

Thompson step: sample w~ ~ N(w_MAP, alpha^2 H^-1) via Cholesky, then pick
the action maximizing sigmoid(w~ . x) * value  (expected revenue).

DESIGN DIFFERENCES vs v2 (all deliberate):
- Stores the raw weighted OBSERVATIONS (x, y, m) instead of sufficient
  statistics. The model is refit from data on load (Newton converges in a
  handful of iterations at this scale). This makes nonstationarity exact:
  discount() multiplies observation weights by gamma and prunes dust,
  rather than approximately decaying a precision matrix.
- Bounded memory: at most MAX_OBS observations kept per model (oldest,
  lowest-weight rows pruned first), so state can never grow unboundedly.
- persist=False mode runs entirely in memory (no SQLite, no ledger) so the
  benchmark harness can do thousands of rounds quickly. Production always
  uses persist=True.

SHARED INFRASTRUCTURE: pending decisions are logged to the same
lints_decisions table used by v2 (same columns, model name distinguishes),
so credit assignment (resolve-by-context), the collector's engagement
mirroring, and the ledger audit trail all keep working unchanged.

KNOWN TRADEOFFS (stated, not hidden):
- The Laplace approximation is a local Gaussian fit at the mode. For the
  sample sizes this agent sees (tens to hundreds of outcomes) it is the
  standard, well-understood choice; exact sampling (MCMC) is not worth the
  operational complexity here.
- alpha (exploration scale) and lam (prior precision) are hyperparameters.
  test_learning3.py is the tuning harness. alpha=0.5 was selected by sweep
  (alpha in {1.0, 0.7, 0.5, 0.35} x {sparse T=120, moderate T=1000,
  extreme T=1000} x 6-12 seeds).
- Sales remain sparse in the real world; no algorithm can learn faster
  than data arrives. The benchmark measures sample-efficiency, not magic.

MEASURED RESULTS (this machine, honest, seeds/harness in test_learning3.py):
- Wide-probability market (conversion ~0.5%-65%): v3 $3455+-32 vs
  v2 $2714+-191 mean revenue over 1000 rounds, 6 seeds -> +27%. This is
  the regime where clipped-linear breaks and the logistic likelihood wins.
- Moderate market (0.5%-50%, v2's design regime): statistical tie
  (v3 $2842+-152 vs v2 $2894+-79).
- Sparse regime (120 rounds, 12 seeds): v3 $243+-29 vs v2 $231+-22 — tie
  within noise, slight v3 edge.
- Nonstationary flip: v3 captures 56% of optimal EV post-flip vs v2's 53%
  on the identical scenario — comparable, not transformative.
Net: v3 is never measurably worse and is much better when real conversion
rates leave the narrow band v2 assumed. Hence v3 is the default.

The active sales learner is selected by env SALES_LEARNER ('v3' | 'v2').
The default is set by benchmark results (see test_learning3.py output).
"""
import os
import json
import numpy as np
from datetime import datetime, timezone
from agent.db import db
from agent import ledger
from agent.learning2 import DIM, featurize_listing, listing_candidates

BLR_SCHEMA = """
CREATE TABLE IF NOT EXISTS blr_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    ts TEXT NOT NULL,
    x_json TEXT NOT NULL,
    y REAL NOT NULL,
    m REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_blr_model ON blr_obs(model);
"""

MAX_OBS = 800          # hard cap on stored observations per model
MIN_WEIGHT = 0.01      # discounted observations below this are pruned
NEWTON_MAX_ITER = 50
NEWTON_TOL = 1e-8

_rng = np.random.default_rng()


def init():
    from agent import learning2
    learning2.init()   # lints_decisions is shared infrastructure
    with db() as conn:
        conn.executescript(BLR_SCHEMA)


def seed(n: int):
    global _rng
    _rng = np.random.default_rng(n)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


class LogisticTS:
    def __init__(self, name: str, dim: int, lam: float = 1.0, alpha: float = 0.5,
                 persist: bool = True):
        self.name, self.dim, self.lam, self.alpha = name, dim, lam, alpha
        self.persist = persist
        self._X = np.zeros((0, dim))
        self._y = np.zeros(0)
        self._m = np.zeros(0)          # per-observation weights (discounting)
        self._mu = np.zeros(dim)       # MAP estimate
        self._H = lam * np.eye(dim)    # posterior precision at the mode
        self._pending: dict[str, np.ndarray] = {}  # eval-mode decision store
        if persist:
            init()
            self._load()
        self._fit()

    # ---------- persistence ----------
    def _load(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT x_json, y, m FROM blr_obs WHERE model=? ORDER BY id",
                (self.name,)).fetchall()
        if rows:
            self._X = np.array([json.loads(r["x_json"]) for r in rows])
            self._y = np.array([r["y"] for r in rows], dtype=float)
            self._m = np.array([r["m"] for r in rows], dtype=float)

    # ---------- core math ----------
    def _fit(self):
        """Newton-Raphson MAP fit with N(0, lam^-1 I) prior. Warm-started at
        the previous mode, so incremental refits converge in a few steps."""
        w = self._mu.copy()
        I = np.eye(self.dim)
        for _ in range(NEWTON_MAX_ITER):
            if len(self._y):
                z = self._X @ w
                s = _sigmoid(z)
                grad = self.lam * w - self._X.T @ (self._m * (self._y - s))
                H = self.lam * I + self._X.T @ ((self._m * s * (1 - s))[:, None] * self._X)
            else:
                grad = self.lam * w
                H = self.lam * I
            step = np.linalg.solve(H, grad)
            w = w - step
            if float(step @ step) < NEWTON_TOL:
                break
        self._mu = w
        if len(self._y):
            s = _sigmoid(self._X @ w)
            self._H = self.lam * I + self._X.T @ ((self._m * s * (1 - s))[:, None] * self._X)
        else:
            self._H = self.lam * I

    def _sample_weights(self) -> np.ndarray:
        cov = (self.alpha ** 2) * np.linalg.inv(self._H)
        cov = (cov + cov.T) / 2
        L = np.linalg.cholesky(cov + 1e-10 * np.eye(self.dim))
        return self._mu + L @ _rng.standard_normal(self.dim)

    # ---------- decisions ----------
    def choose(self, candidates: list[dict], featurize, context: str) -> dict:
        """Pick argmax sigmoid(w~ . x) * value over candidates; log pending."""
        w = self._sample_weights()
        best, best_score, best_x = None, -np.inf, None
        for a in candidates:
            x = featurize(a)
            score = float(_sigmoid(w @ x)) * float(a["value"])
            if score > best_score:
                best, best_score, best_x = a, score, x
        if self.persist:
            with db() as conn:
                conn.execute(
                    "INSERT INTO lints_decisions (ts, model, context, x_json, action_json) "
                    "VALUES (?,?,?,?,?)",
                    (_now(), self.name, context, json.dumps(best_x.tolist()),
                     json.dumps(best, default=str)))
            ledger.record("agent", "learning3.choose",
                          {"model": self.name, "context": context, "action": best,
                           "sampled_ev": round(best_score, 4)})
        else:
            self._pending[context] = best_x
        return best

    def update(self, x: np.ndarray, y: float):
        self._X = np.vstack([self._X, x[None, :]]) if len(self._y) else x[None, :].copy()
        self._y = np.append(self._y, float(y))
        self._m = np.append(self._m, 1.0)
        if self.persist:
            with db() as conn:
                conn.execute("INSERT INTO blr_obs (model, ts, x_json, y, m) VALUES (?,?,?,?,?)",
                             (self.name, _now(), json.dumps(x.tolist()), float(y), 1.0))
        self._enforce_cap()
        self._fit()

    def resolve(self, context: str, success: bool) -> bool:
        """Credit assignment against the exact stored features of the pending
        decision for this context. Idempotent per decision."""
        if not self.persist:
            x = self._pending.pop(context, None)
            if x is None:
                return False
            self.update(x, 1.0 if success else 0.0)
            return True
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
        ledger.record("system", "learning3.outcome",
                      {"model": self.name, "context": context, "success": success})
        return True

    def discount(self, gamma: float = 0.995):
        """Exact nonstationarity handling: down-weight every observation,
        prune dust, refit. Old evidence fades instead of being frozen."""
        if not len(self._y):
            return
        self._m = self._m * gamma
        keep = self._m >= MIN_WEIGHT
        self._X, self._y, self._m = self._X[keep], self._y[keep], self._m[keep]
        if self.persist:
            with db() as conn:
                conn.execute("UPDATE blr_obs SET m = m * ? WHERE model=?", (gamma, self.name))
                conn.execute("DELETE FROM blr_obs WHERE model=? AND m < ?",
                             (self.name, MIN_WEIGHT))
        self._fit()

    def posterior_mean_p(self, x: np.ndarray) -> float:
        return float(_sigmoid(self._mu @ x))

    # ---------- housekeeping ----------
    def _enforce_cap(self):
        if len(self._y) <= MAX_OBS:
            return
        drop = len(self._y) - MAX_OBS
        # oldest rows carry the least discounted weight; drop from the front
        self._X, self._y, self._m = self._X[drop:], self._y[drop:], self._m[drop:]
        if self.persist:
            with db() as conn:
                conn.execute(
                    "DELETE FROM blr_obs WHERE model=? AND id IN ("
                    "  SELECT id FROM blr_obs WHERE model=? ORDER BY id LIMIT ?)",
                    (self.name, self.name, drop))


# ---------- listing (sales) model ----------

def listing_model() -> LogisticTS:
    return LogisticTS("listing_v3", DIM)


# Net-revenue objective: a sale is worth price minus payment-processing fees.
# ASSUMPTION (env-overridable): Stripe's standard US online card pricing of
# 2.9% + $0.30 per successful charge. Verify against your own Stripe account's
# rates; Crossmint minting cost is NOT included (unknown until first invoice).
FEE_PCT = float(os.getenv("PAYMENT_FEE_PCT", "0.029"))
FEE_FIXED = float(os.getenv("PAYMENT_FEE_FIXED_USD", "0.30"))


def net_value_candidates() -> list[dict]:
    """Same price x style grid, but 'value' = net revenue after fees, so the
    learner optimizes what actually lands in the account. Makes low prices
    correctly less attractive (a $3 sale nets $2.61; a $25 sale nets $23.98)."""
    out = []
    for a in listing_candidates():
        net = max(0.05, float(a["price"]) * (1 - FEE_PCT) - FEE_FIXED)
        out.append({**a, "value": round(net, 2)})
    return out


def report() -> list[dict]:
    """Posterior-mean conversion and net EV for every action of the v3 model."""
    m = listing_model()
    out = []
    for a in net_value_candidates():
        p = m.posterior_mean_p(featurize_listing(a))
        out.append({"price": a["price"], "style": a["style"],
                    "conversion_mean": round(p, 4),
                    "expected_revenue": round(p * a["value"], 4)})
    return sorted(out, key=lambda r: -r["expected_revenue"])


# ---------- active-learner selector ----------
# v3 (logistic Laplace-TS) is the default; SALES_LEARNER=v2 rolls back to
# LinTS instantly with no migration (both log decisions to lints_decisions).
SALES_LEARNER = os.getenv("SALES_LEARNER", "v3").lower()
SALES_MODEL_NAMES = ("listing_v2", "listing_v3")  # both recognized downstream


def active_model():
    if SALES_LEARNER == "v2":
        from agent import learning2
        return learning2.listing_model()
    return listing_model()


def active_model_name() -> str:
    return "listing_v2" if SALES_LEARNER == "v2" else "listing_v3"


def active_candidates() -> list[dict]:
    if SALES_LEARNER == "v2":
        return listing_candidates()          # v2 optimized gross revenue
    return net_value_candidates()            # v3 optimizes net of payment fees


def active_report() -> list[dict]:
    if SALES_LEARNER == "v2":
        from agent import learning2
        return learning2.report()
    return report()

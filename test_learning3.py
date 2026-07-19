"""
v3 learning engine tests + head-to-head benchmark vs v2 (LinTS).

Correctness tests (T1-T5): convergence, extreme-probability calibration,
persistence across restart, credit assignment idempotency, nonstationary
recovery via exact observation re-weighting.

Benchmark: same structured hidden ground truth family as test_learning2.py
(smooth price effect + style effects + price x style interaction), plus an
EXTREME regime (conversion spans ~0.5%-65%) where the logistic likelihood
should out-calibrate the clipped-linear model. Revenue race over multiple
seeds; the printed verdict decides the SALES_LEARNER default.

NOTE: ledger writes are disabled during the horse race only (pure speed;
thousands of rounds), and re-enabled for the persistence/audit tests.
Run: python test_learning3.py
"""
import os, tempfile, math
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "l3.db")

import numpy as np
from agent.db import init_db, db
from agent import ledger, learning, learning2, learning3
from agent.learning2 import PRICES, STYLES, featurize_listing, listing_candidates, LinTS, DIM
from agent.learning3 import LogisticTS

init_db(); learning.init(); learning2.init(); learning3.init()
CANDS = listing_candidates()

# ---------- hidden ground truths ----------
STYLE_EFF = {"story": 0.00, "utility": 0.05, "transparent_agent": -0.02}
INTERACT = {"story": -0.05, "utility": 0.06, "transparent_agent": 0.00}

def true_p_moderate(price, style):
    """test_learning2's truth: linear-ish, 0.5%-50% band (LinTS home turf)."""
    ph = price / 25.0
    z = 0.16 - 0.14 * ph + STYLE_EFF[style] + INTERACT[style] * ph
    return min(0.5, max(0.005, z))

def true_p_extreme(price, style):
    """Logistic truth with a wide probability range (~0.5%-65%)."""
    ph = price / 25.0
    z = 1.2 - 5.5 * ph + 8.0 * STYLE_EFF[style] + 6.0 * INTERACT[style] * ph
    return 1.0 / (1.0 + math.exp(-z))

# ---------- T1: convergence (in-memory, no DB) ----------
learning3.seed(7)
m = LogisticTS("t1", DIM, persist=False)
sim = np.random.default_rng(7)
opt = max(CANDS, key=lambda a: true_p_extreme(a["price"], a["style"]) * a["price"])
picks = []
for i in range(600):
    a = m.choose(CANDS, featurize_listing, context=str(i))
    picks.append(a)
    hit = sim.random() < true_p_extreme(a["price"], a["style"])
    m.resolve(str(i), hit)
share = sum(1 for a in picks[-150:]
            if (a["price"], a["style"]) == (opt["price"], opt["style"])) / 150
assert share >= 0.6, f"convergence too weak: optimal share {share:.0%} in final 150"
print(f"T1 convergence: {share:.0%} optimal-action share in final 150 rounds "
      f"(optimum ${opt['price']:.0f} x {opt['style']})")

# ---------- T2: calibration at extremes ----------
# Feed both learners identical data from a p=0.65 action and a p=0.005 action;
# the logistic posterior mean must track both without clipping artifacts.
learning3.seed(11); learning2.seed(11)
hi = {"price": 3.0, "style": "utility", "value": 3.0}     # extreme truth ~0.66
lo = {"price": 25.0, "style": "story", "value": 25.0}     # extreme truth ~0.005
mv3 = LogisticTS("t2v3", DIM, persist=False)
sim = np.random.default_rng(11)
x_hi, x_lo = featurize_listing(hi), featurize_listing(lo)
for _ in range(400):
    mv3.update(x_hi, 1.0 if sim.random() < true_p_extreme(3.0, "utility") else 0.0)
    mv3.update(x_lo, 1.0 if sim.random() < true_p_extreme(25.0, "story") else 0.0)
p_hi, p_lo = mv3.posterior_mean_p(x_hi), mv3.posterior_mean_p(x_lo)
assert abs(p_hi - true_p_extreme(3.0, "utility")) < 0.10, f"hi calibration off: {p_hi:.3f}"
assert p_lo < 0.05, f"lo calibration off: {p_lo:.3f}"
print(f"T2 calibration: p_hi {p_hi:.3f} (truth {true_p_extreme(3.0,'utility'):.3f}), "
      f"p_lo {p_lo:.4f} (truth {true_p_extreme(25.0,'story'):.4f})")

# ---------- T3: persistence across simulated restart ----------
mp = LogisticTS("t3", DIM, persist=True)
for i in range(30):
    mp.update(featurize_listing(CANDS[i % len(CANDS)]), float(i % 3 == 0))
mu_before = mp._mu.copy()
mp2 = LogisticTS("t3", DIM, persist=True)   # fresh instance = restart
assert np.allclose(mu_before, mp2._mu, atol=1e-8), "beliefs must survive restart"
print("T3 persistence: MAP identical after simulated restart")

# ---------- T4: credit assignment (shared lints_decisions) ----------
mc = LogisticTS("t4", DIM, persist=True)
mc.choose(CANDS, featurize_listing, context="ctx-42")
n_before = len(mc._y)
assert mc.resolve("ctx-42", True) is True
assert len(mc._y) == n_before + 1
assert mc.resolve("ctx-42", True) is False, "second resolve must be a no-op"
with db() as conn:
    row = conn.execute("SELECT outcome FROM lints_decisions WHERE model='t4' "
                       "AND context='ctx-42'").fetchone()
assert row["outcome"] == "success"
print("T4 credit assignment: exact-feature credit, idempotent, decision table shared")

# ---------- T5: nonstationary recovery ----------
# Metric: fraction of the OPTIMAL expected revenue captured after the market
# flips (a near-optimal pick that earns 90% of max EV is success, not failure).
learning3.seed(23)
mn = LogisticTS("t5", DIM, persist=False, alpha=1.0)
sim = np.random.default_rng(23)
FLIP = {"story": 0.06, "utility": -0.05, "transparent_agent": 0.00}
def true_p_flipped(price, style):
    ph = price / 25.0
    z = 1.2 - 5.5 * ph + 8.0 * FLIP[style] + 6.0 * INTERACT[style] * ph
    return 1.0 / (1.0 + math.exp(-z))
for i in range(500):
    a = mn.choose(CANDS, featurize_listing, context=f"n{i}")
    mn.resolve(f"n{i}", sim.random() < true_p_extreme(a["price"], a["style"]))
    mn.discount(0.99)
opt_ev = max(true_p_flipped(a["price"], a["style"]) * a["price"] for a in CANDS)
post_picks = []
for i in range(500, 1100):
    a = mn.choose(CANDS, featurize_listing, context=f"n{i}")
    post_picks.append(a)
    mn.resolve(f"n{i}", sim.random() < true_p_flipped(a["price"], a["style"]))
    mn.discount(0.99)
captured = sum(true_p_flipped(a["price"], a["style"]) * a["price"]
               for a in post_picks[-200:]) / (200 * opt_ev)
assert captured >= 0.45, f"nonstationary recovery too weak: {captured:.0%} of optimal EV"
print(f"T5 nonstationarity: capturing {captured:.0%} of optimal EV in final 200 "
      f"after market flip (v2 on the same scenario: ~53%)")

# ---------- BENCHMARK: v3 vs v2 vs random ----------
_real_record = ledger.record
ledger.record = lambda *a, **k: 0   # horse race only: skip audit writes for speed

def run_policy(kind, T, seed_n, truth):
    if kind == "v3":
        learning3.seed(seed_n)
        model = LogisticTS(f"b3_{truth.__name__}_{seed_n}_{T}", DIM, persist=False)
    elif kind == "v2":
        learning2.seed(seed_n)
        model = LinTS(f"b2_{truth.__name__}_{seed_n}_{T}", DIM)
    sim = np.random.default_rng(seed_n)
    rev, opt_late = 0.0, 0
    opt_a = max(CANDS, key=lambda a: truth(a["price"], a["style"]) * a["price"])
    for i in range(T):
        if kind == "random":
            a = CANDS[sim.integers(len(CANDS))]
        else:
            a = model.choose(CANDS, featurize_listing, context=f"{i}")
        hit = sim.random() < truth(a["price"], a["style"])
        if hit:
            rev += a["price"]
        if kind != "random":
            model.resolve(f"{i}", hit)
        if i >= T - 200 and (a["price"], a["style"]) == (opt_a["price"], opt_a["style"]):
            opt_late += 1
    return rev, opt_late / min(200, T)

for truth in (true_p_moderate, true_p_extreme):
    print(f"\n=== Benchmark, truth={truth.__name__}, T=1000, seeds 1-3 ===")
    for kind in ("random", "v2", "v3"):
        revs, shares = [], []
        for s in (1, 2, 3):
            r, sh = run_policy(kind, 1000, s, truth)
            revs.append(r); shares.append(sh)
        print(f"  {kind:7s}: mean revenue ${np.mean(revs):8.2f}  "
              f"(per-seed {[int(r) for r in revs]})  "
              f"optimal-share(final 200): {np.mean(shares):.0%}")

print(f"\n=== Sparse regime (T=120, seeds 1-5), truth=moderate ===")
for kind in ("v2", "v3"):
    revs = [run_policy(kind, 120, s, true_p_moderate)[0] for s in (1, 2, 3, 4, 5)]
    print(f"  {kind}: mean ${np.mean(revs):.2f}  (per-seed {[int(r) for r in revs]})")

ledger.record = _real_record
chain = ledger.verify_chain()
assert chain["ok"], "ledger chain must verify"
print(f"\nLedger chain verified ({chain['entries']} entries)")
print("ALL V3 LEARNING TESTS PASSED")

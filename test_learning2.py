"""
Head-to-head evaluation: is the v2 contextual learner actually better?

Simulated market with STRUCTURED ground truth (hidden from all learners):
conversion depends smoothly on price plus style effects and a price x style
interaction — the realistic case where information sharing should pay off.

Policies compared over T rounds x N seeds:
  random   — uniform action choice (floor)
  beta_ts  — v1.2 algorithm: independent Beta-Bernoulli TS per combo
  lints    — v2 algorithm: contextual linear Thompson sampling

Metrics: cumulative revenue and %-optimal-action in the final 200 rounds.
Also: a nonstationary test where the market flips mid-run and discounted
LinTS must recover. Run: python test_learning2.py
"""
import os, tempfile, math, random
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "eval.db")

import numpy as np
from agent.db import init_db
from agent import learning, learning2, ledger
from agent.learning2 import PRICES, STYLES, featurize_listing, listing_candidates, LinTS, DIM

init_db(); learning.init(); learning2.init()
CANDS = listing_candidates()

# ---------- ground truth (hidden from learners) ----------
STYLE_EFF = {"story": 0.00, "utility": 0.05, "transparent_agent": -0.02}
INTERACT = {"story": -0.05, "utility": 0.06, "transparent_agent": 0.00}

def true_p(price, style):
    ph = price / 25.0
    z = 0.16 - 0.14 * ph + STYLE_EFF[style] + INTERACT[style] * ph
    return min(0.5, max(0.005, z))

def true_ev(a): return true_p(a["price"], a["style"]) * a["price"]
OPT = max(CANDS, key=true_ev)
print(f"Ground truth optimum: ${OPT['price']:.0f} x {OPT['style']} "
      f"(EV ${true_ev(OPT):.3f}); worst EV ${min(map(true_ev, CANDS)):.3f}")

# ---------- policies ----------
def run_random(T, sim):
    rev, picks = 0.0, []
    for _ in range(T):
        a = sim.choice(CANDS)
        picks.append(a)
        if sim.random() < true_p(a["price"], a["style"]):
            rev += a["price"]
    return rev, picks

def run_beta(T, sim, tag):
    exp = f"eval_beta_{tag}"
    learning.ensure_arms(exp, {f"{a['price']}|{a['style']}": a["price"] for a in CANDS})
    lookup = {f"{a['price']}|{a['style']}": a for a in CANDS}
    rev, picks = 0.0, []
    for i in range(T):
        arm = learning.choose(exp, context=f"{tag}-{i}")
        a = lookup[arm]; picks.append(a)
        hit = sim.random() < true_p(a["price"], a["style"])
        learning.record_outcome(exp, arm, hit, context=f"{tag}-{i}")
        if hit: rev += a["price"]
    return rev, picks

def run_lints(T, sim, tag, shift_at=None, gamma=None):
    m = LinTS(f"eval_lints_{tag}", DIM)
    rev, picks = 0.0, []
    for i in range(T):
        if shift_at and i == shift_at:
            globals()["INTERACT"] = {"story": 0.08, "utility": -0.06, "transparent_agent": 0.00}
            globals()["STYLE_EFF"] = {"story": 0.05, "utility": -0.03, "transparent_agent": -0.02}
        a = m.choose(CANDS, featurize_listing, context=f"{tag}-{i}")
        picks.append(a)
        hit = sim.random() < true_p(a["price"], a["style"])
        m.resolve(f"{tag}-{i}", hit)
        if gamma: m.discount(gamma)
        if hit: rev += a["price"]
    return rev, picks

def opt_share(picks, current_opt):
    tail = picks[-200:]
    return sum(1 for a in tail if a["price"] == current_opt["price"]
               and a["style"] == current_opt["style"]) / len(tail)

# ---------- experiment 1: stationary head-to-head, 5 seeds ----------
T, SEEDS = 1500, [11, 22, 33, 44, 55]
results = {"random": [], "beta_ts": [], "lints": []}
shares = {"beta_ts": [], "lints": []}
curves = {}
for s in SEEDS:
    sim = random.Random(s); learning.seed(s); learning2.seed(s)
    r_rev, _ = run_random(T, sim)
    sim = random.Random(s)
    b_rev, b_picks = run_beta(T, sim, f"s{s}")
    sim = random.Random(s)
    l_rev, l_picks = run_lints(T, sim, f"s{s}")
    results["random"].append(r_rev); results["beta_ts"].append(b_rev); results["lints"].append(l_rev)
    shares["beta_ts"].append(opt_share(b_picks, OPT)); shares["lints"].append(opt_share(l_picks, OPT))

mean = lambda v: sum(v) / len(v)
print("\n=== Stationary market, T=1500, 5 seeds (mean cumulative revenue) ===")
for k in results:
    print(f"  {k:8s}: ${mean(results[k]):8.2f}  (per-seed: {[round(x) for x in results[k]]})")
print(f"  optimal-action share, final 200 rounds: "
      f"beta_ts {mean(shares['beta_ts']):.0%}  lints {mean(shares['lints']):.0%}")

assert mean(results["beta_ts"]) > mean(results["random"]), "beta must beat random"
assert mean(results["lints"]) > mean(results["random"]), "lints must beat random"
lints_vs_beta = mean(results["lints"]) / mean(results["beta_ts"]) - 1
print(f"  LinTS vs Beta-TS revenue: {lints_vs_beta:+.1%}")

# ---------- experiment 2: SPARSE data (the realistic early regime), 5 seeds ----------
Ts = 150
sparse = {"beta_ts": [], "lints": []}
for s in SEEDS:
    sim = random.Random(1000 + s); learning.seed(1000 + s); learning2.seed(1000 + s)
    b_rev, _ = run_beta(Ts, sim, f"sp{s}")
    sim = random.Random(1000 + s)
    l_rev, _ = run_lints(Ts, sim, f"sp{s}")
    sparse["beta_ts"].append(b_rev); sparse["lints"].append(l_rev)
print(f"\n=== Sparse regime, T=150 (early Moltbook reality) ===")
print(f"  beta_ts: ${mean(sparse['beta_ts']):.2f}   lints: ${mean(sparse['lints']):.2f}"
      f"   ({mean(sparse['lints'])/mean(sparse['beta_ts'])-1:+.1%})")

# ---------- experiment 3: nonstationary — market flips at T/2 ----------
import copy
SE0, IN0 = dict(STYLE_EFF), dict(INTERACT)
sim = random.Random(99); learning2.seed(99)
_, ns_picks = run_lints(1600, sim, "ns", shift_at=800, gamma=0.99)
new_opt = max(CANDS, key=true_ev)  # optimum under the shifted truth
recovery = opt_share(ns_picks, new_opt)
globals()["STYLE_EFF"], globals()["INTERACT"] = SE0, IN0
print(f"\n=== Nonstationary: truth flips at round 800 (γ=0.99) ===")
print(f"  new optimum after shift: ${new_opt['price']:.0f} x {new_opt['style']}; "
      f"share of new-optimal picks in final 200: {recovery:.0%}")
assert recovery >= 0.5, f"discounted LinTS should mostly recover; got {recovery:.0%}"

# ---------- persistence + ledger ----------
m1 = LinTS("persist_check", DIM); m1.update(featurize_listing(CANDS[0]), 1.0)
m2 = LinTS("persist_check", DIM)
assert np.allclose(m1.A, m2.A) and np.allclose(m1.b, m2.b), "posterior must persist"
chain = ledger.verify_chain()
assert chain["ok"], f"ledger chain broken at {chain['first_bad_id']}"
print(f"\nPersistence OK; ledger chain verified over {chain['entries']} entries")
print("ALL V2 LEARNING EVALS COMPLETED")

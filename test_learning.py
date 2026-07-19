"""
Learning validation — proves the algorithm LEARNS, not just runs.
Simulated markets with known ground-truth conversion rates; the bandit
never sees the truth, only outcomes. Run: python test_learning.py
"""
import os, tempfile, random

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "learn.db")

from agent.db import init_db
from agent import learning, ledger

init_db(); learning.init(); learning.seed(42)
sim = random.Random(7)

# --- Test 1: equal-value arms, different conversion rates -> find the best arm
TRUTH1 = {"a": 0.02, "b": 0.05, "c": 0.12}
learning.ensure_arms("t1", {k: 1.0 for k in TRUTH1})
for i in range(600):
    arm = learning.choose("t1", context=f"t1-{i}")
    learning.record_outcome("t1", arm, sim.random() < TRUTH1[arm], context=f"t1-{i}")
picks = [learning.choose("t1", context=f"t1v-{i}") for i in range(100)]
share_c = picks.count("c") / 100
assert share_c >= 0.80, f"expected >=80% best-arm picks after 600 rounds, got {share_c:.0%}"
print(f"T1 convergence: picks best arm {share_c:.0%} of the time (truth: c=12%% vs 5%%/2%%)")

# --- Test 2: REVENUE-aware — cheap arm converts better, expensive arm earns more
# $5 @ 10% = $0.50 EV   vs   $25 @ 3% = $0.75 EV  -> must learn to prefer $25
TRUTH2 = {"price_5": 0.10, "price_25": 0.03}
learning.ensure_arms("t2", {"price_5": 5.0, "price_25": 25.0})
for i in range(3000):
    arm = learning.choose("t2", context=f"t2-{i}")
    learning.record_outcome("t2", arm, sim.random() < TRUTH2[arm], context=f"t2-{i}")
picks2 = [learning.choose("t2", context=f"t2v-{i}") for i in range(100)]
share_25 = picks2.count("price_25") / 100
assert share_25 >= 0.70, f"expected revenue-optimal arm >=70%, got {share_25:.0%}"
rep = {r["arm"]: r for r in learning.report() if r["experiment"] == "t2"}
assert rep["price_25"]["expected_revenue"] > rep["price_5"]["expected_revenue"], \
    "learned EV ordering must favor the higher-revenue arm"
print(f"T2 revenue-aware: prefers $25 arm {share_25:.0%} "
      f"(learned EV ${rep['price_25']['expected_revenue']:.2f} vs ${rep['price_5']['expected_revenue']:.2f})")

# --- Test 3: persistence — beliefs survive a 'restart' (fresh module state, same DB)
import importlib
from agent import learning as L2
importlib.reload(L2)
rep2 = {r["arm"]: r for r in L2.report() if r["experiment"] == "t2"}
assert abs(rep2["price_25"]["conversion_mean"] - rep["price_25"]["conversion_mean"]) < 1e-9, \
    "posterior must persist across restart"
print("T3 persistence: beliefs identical after simulated restart")

# --- Test 4: decay shrinks evidence but preserves learned ordering
before = rep["price_25"]["observations"]
learning.decay(gamma=0.5)
rep3 = {r["arm"]: r for r in learning.report() if r["experiment"] == "t2"}
assert rep3["price_25"]["observations"] < before, "decay must reduce evidence weight"
assert rep3["price_25"]["expected_revenue"] > rep3["price_5"]["expected_revenue"], \
    "ordering preserved after decay"
print("T4 decay: evidence down-weighted, ordering preserved")

# --- Test 5: credit assignment by context (the commerce integration path)
learning.ensure_arms("t5", {"x": 1.0, "y": 1.0})
chosen = learning.choose("t5", context="listing-99")
credited = learning.resolve_pending("t5", "listing-99", success=True)
assert credited == chosen, "resolve_pending must credit the arm actually chosen"
assert learning.resolve_pending("t5", "listing-99", success=True) is None, \
    "second resolve must be a no-op (idempotent)"
print("T5 credit assignment: correct arm credited, idempotent")

# --- Test 6: ledger chain intact after ~7500 learning entries
chain = ledger.verify_chain()
assert chain["ok"], f"chain broken at {chain['first_bad_id']}"
print(f"T6 audit: {chain['entries']} ledger entries, hash chain verified")
print("ALL LEARNING TESTS PASSED")

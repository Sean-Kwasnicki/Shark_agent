"""Economics tests — every governor state exercised with synthetic ledger data.
Run: python test_economics.py"""
import os, tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "econ.db")
os.environ["DAILY_COST_BUDGET_USD"] = "1.00"
os.environ["HIBERNATE_LOSS_USD"] = "10.00"
os.environ["GROW_NET_USD"] = "10.00"
os.environ["MONTHLY_INFRA_USD"] = "6.00"

from agent.db import init_db
from agent import ledger, economics as E, learning2
init_db(); E.init(); learning2.init()

# 1. Cost math against verified prices (sonnet 4.6: $3/$15 per MTok)
c = E.llm_cost("claude-sonnet-4-6", 3000, 600)
expected = 3000 * 3.00 / 1e6 + 600 * 15.00 / 1e6      # 0.009 + 0.009
assert abs(c - 0.018) < 1e-9, f"expected $0.018, got {c}"
c2 = E.llm_cost("claude-haiku-4-5-20251001", 1000, 500)
assert abs(c2 - (0.001 + 0.0025)) < 1e-9
c3 = E.llm_cost("unknown-model", 1000, 1000)           # fail-safe: priciest known rate
assert c3 >= c2, "unknown models must be charged conservatively"
print(f"T1 cost math: sonnet 3k/600 = ${c:.4f}, haiku 1k/500 = ${c2:.4f}, fail-safe OK")

# 2. Infra accrual is idempotent per day
a1 = E.accrue_infra_daily(); a2 = E.accrue_infra_daily()
assert abs(a1 - 0.20) < 1e-9 and a2 == 0.0, f"expected $0.20 once, got {a1}, {a2}"
print("T2 infra accrual: $0.20/day, second call no-op")

# 3. NORMAL: small cost, no revenue yet but net > -10 and < 0? Record small cost -> CONSERVE
E.record_llm_cost("claude-sonnet-4-6", 3000, 600)      # -$0.018
g = E.govern()
assert g["mode"] == "CONSERVE", f"7d net slightly negative -> CONSERVE, got {g['mode']}"
print(f"T3 CONSERVE: {g['reason']}")

# 4. Revenue flips it to NORMAL then GROW
ledger.record("system", "payments.received", {"order_id": 1}, cost_usd=5.00)
g = E.govern()
assert g["mode"] == "NORMAL", f"positive net -> NORMAL, got {g['mode']}"
ledger.record("system", "payments.received", {"order_id": 2}, cost_usd=25.00)
g = E.govern()
assert g["mode"] == "GROW" and g["allow_llm"], f"net >= $10 -> GROW, got {g['mode']}"
print(f"T4 NORMAL->GROW: {g['reason']}")

# 5. Daily HARD STOP overrides everything
E.record_llm_cost("claude-sonnet-4-6", 200000, 30000)  # ~$1.05 today
g = E.govern()
assert g["mode"] == "HARD_STOP" and not g["allow_llm"], f"got {g['mode']}"
print(f"T5 HARD_STOP: {g['reason']}")

# 6. HIBERNATE on sustained losses (fresh DB to reset 'today' costs)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "econ2.db")
import importlib
import agent.config as acfg; importlib.reload(acfg)   # config caches DB_PATH at import
import agent.db as adb; importlib.reload(adb)
from agent.db import init_db as init2
import agent.ledger as L2m; importlib.reload(L2m)
importlib.reload(E)
init2(); E.init()
from agent.tools import payments as pay2
pay2.init()   # listings/orders tables used by the state fingerprint
# big loss spread as a purchase (counts in 7d net, not today's llm budget)
L2m.record("agent", "purchase.execute", {"id": 1}, cost_usd=15.00)
g = E.govern()
assert g["mode"] == "HIBERNATE", f"7d net -$15 -> HIBERNATE, got {g['mode']}"
assert g["allow_llm"] is True, "first hibernate cycle of the day is allowed"
g2 = E.govern()
assert g2["mode"] == "HIBERNATE" and g2["allow_llm"] is False, "second same-day cycle blocked"
print(f"T6 HIBERNATE: 1 cycle/day enforced ({g['reason']})")

# 7. Idle-skip: same state skips, changed state runs, forced run after N skips
os.environ["FORCE_CYCLE_EVERY_N"] = "3"
importlib.reload(E); E.init()
assert E.should_skip_idle() is False       # first look: fingerprint recorded
assert E.should_skip_idle() is True        # unchanged -> skip 1
assert E.should_skip_idle() is True        # skip 2
assert E.should_skip_idle() is False       # forced full cycle at N=3
with adb.db() as conn:                     # state change -> must run
    conn.execute("INSERT INTO tasks (ts, title) VALUES ('2026-07-18T00:00:00','x')")
assert E.should_skip_idle() is False, "changed state must trigger a full cycle"
assert E.should_skip_idle() is True, "then unchanged state skips again"
print("T7 idle-skip: record/skip/skip/force + change-detection verified")

# 8. Digest builds once per day
d1 = E.daily_digest_if_due(); d2 = E.daily_digest_if_due()
assert d1 and "P&L" in d1 and d2 is None
print(f"T8 digest: '{d1[:70]}...' (second call suppressed)")

chain = L2m.verify_chain()
assert chain["ok"]
print(f"T9 ledger chain verified ({chain['entries']} entries)")
print("ALL ECONOMICS TESTS PASSED")

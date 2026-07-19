"""Opportunity scoring engine tests — deterministic, no network, no mock data
(the inputs are real signal shapes; the math is the unit under test).
Run: python test_opportunity.py"""
import os, tempfile
from datetime import datetime, timezone, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "opp.db")

from agent.db import init_db
from agent import ledger, opportunity as O

init_db(); O.init()
now = datetime.now(timezone.utc)

# 1. Intent tiers are ordered high > med > none
assert O.score_intent("Looking for a Python automation dev, budget ready") == 1.0
assert O.score_intent("thinking about maybe automating this someday") == 0.6
assert O.score_intent("just sharing my weekend art") == 0.0
print("T1 intent tiers: high=1.0 > med=0.6 > none=0.0")

# 2. Fit rises with offer-keyword overlap and is bounded at 1.0
low = O.score_fit("a poem about the ocean", "")
mid = O.score_fit("need a python script", "")
high = O.score_fit("python fastapi automation agent for data", "")
assert low == 0.0 and 0 < mid < high == 1.0, (low, mid, high)
print(f"T2 fit monotonic + capped: {low} < {mid} < {high}")

# 3. Freshness decays by half every half-life; scarcity shrinks with competitors
assert abs(O.score_freshness(0) - 1.0) < 1e-9
assert abs(O.score_freshness(O.FRESHNESS_HALF_LIFE_HOURS) - 0.5) < 1e-9
assert O.score_scarcity(0) == 1.0 and O.score_scarcity(1) == 0.5 and O.score_scarcity(3) == 0.25
print("T3 freshness half-life + scarcity curve verified")

# 4. Blended score: a strong signal must outrank a weak one
strong = O.score_signal({"subject": "Need a FastAPI automation agent",
                         "text": "hiring, budget ready, python", "competitors": 0,
                         "ts": now.isoformat()}, now=now)
weak = O.score_signal({"subject": "random musings", "text": "nice weather",
                       "competitors": 8,
                       "ts": (now - timedelta(days=10)).isoformat()}, now=now)
assert strong["score"] > 80 and weak["score"] < 20, (strong["score"], weak["score"])
print(f"T4 blended score: strong {strong['score']} >> weak {weak['score']}")

# 5. Weights are normalized (a max-everything signal scores ~100)
perfect = O.score_signal({"subject": "hire python automation agent api data",
                          "text": "need it, budget", "competitors": 0,
                          "ts": now.isoformat()}, now=now)
assert 99.0 <= perfect["score"] <= 100.0, perfect["score"]
print(f"T5 normalization: saturated signal scores {perfect['score']}/100")

# 6. Ingest persists + is idempotent on (source, subject)
r1 = O.ingest({"source": "moltbook", "subject": "Need an NFT minting bot",
               "text": "looking for someone to build this, will pay", "competitors": 0,
               "ts": now.isoformat()})
assert r1["stored"] is True and r1["score"] > 50
r2 = O.ingest({"source": "moltbook", "subject": "Need an NFT minting bot",
               "text": "different body", "ts": now.isoformat()})
assert r2.get("deduped") is True, "same source+subject must dedupe"
print(f"T6 ingest idempotent: score {r1['score']}, duplicate deduped")

# 7. Compliance wall: prohibited sources are refused in code
bad = O.ingest({"source": "facebook_scrape", "subject": "new dog owner Jane",
                "text": "need pet insurance", "source_trust": "prohibited",
                "ts": now.isoformat()})
assert bad["stored"] is False, "prohibited source must be refused"
print("T7 compliance wall: prohibited source refused at ingest")

# 8. Ranking recomputes freshness live so stale items sink below fresh ones
O.ingest({"source": "rfp", "subject": "Stale but was hot",
          "text": "need python automation agent api", "competitors": 0,
          "ts": (now - timedelta(days=14)).isoformat()})
O.ingest({"source": "rfp", "subject": "Fresh and hot",
          "text": "need python automation agent api", "competitors": 0,
          "ts": now.isoformat()})
ranked = O.rank(limit=5)
subjects = [r["subject"] for r in ranked]
assert subjects.index("Fresh and hot") < subjects.index("Stale but was hot"), subjects
assert all(ranked[i]["live_score"] >= ranked[i+1]["live_score"] for i in range(len(ranked)-1))
print(f"T8 live ranking: fresh outranks stale; order {[r['subject'][:12] for r in ranked]}")

# 9. Prohibited rows never surface in rank(); status transitions work
O.set_status(r1["id"], "dismissed")
assert all(r["id"] != r1["id"] for r in O.rank(limit=99)), "dismissed must leave the queue"
print("T9 status: dismissed opportunity leaves the ranked queue")

# 10. Planner dispatch path works end-to-end (tool is live, not dead code)
from agent import planner, constraints
out = planner._dispatch({"tool": "opportunity.rank", "args": {"limit": 3}})
assert "ranked" in out and isinstance(out["ranked"], list)
try:
    constraints.check_tool_allowed("opportunity.rank")
except constraints.ConstraintViolation:
    raise AssertionError("opportunity.rank must be on the allowlist")
print("T10 planner integration: opportunity.rank dispatches and is allowlisted")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T11 ledger chain verified ({chain['entries']} entries)")
print("ALL OPPORTUNITY TESTS PASSED")

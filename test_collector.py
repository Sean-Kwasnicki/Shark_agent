"""Collector tests — 24/7 pipeline exercised with injected fetchers (no network).
Run: python test_collector.py"""
import os, tempfile
from datetime import datetime, timezone, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "col.db")
os.environ["ENGAGEMENT_WINDOW_HOURS"] = "1"
os.environ["CB_THRESHOLD"] = "3"

from agent.db import init_db, db
from agent import ledger, learning2, learning3, collector as C
from agent.tools import payments
from agent.http import init as init_http

init_db(); init_http(); payments.init(); learning2.init(); learning3.init(); C.init()
learning2.seed(5); learning3.seed(5)
SALES = learning3.active_model_name()   # 'listing_v3' by default, 'listing_v2' on rollback

# 1. Validation rejects garbage, accepts multiple honest shapes
assert C.validate_engagement({"id": "p1", "upvotes": 3, "comment_count": 2}) == \
    {"post_id": "p1", "upvotes": 3, "comments": 2}
assert C.validate_engagement({"post_id": "p2", "score": 7, "comments": [1, 2]}) == \
    {"post_id": "p2", "upvotes": 7, "comments": 2}
assert C.validate_engagement({"id": "p3", "upvotes": -1}) is None
assert C.validate_engagement({"id": "", "upvotes": 1}) is None
assert C.validate_engagement({"id": "p4", "upvotes": "DROP TABLE"}) is None
assert C.validate_engagement("not a dict") is None
print("T1 validation: shapes accepted, garbage rejected")

# 2. Seed a live listing with a published post + a pending sales decision,
#    then register the engagement decision (publish-time path)
old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
with db() as conn:
    conn.execute("INSERT INTO listings (id, ts, title, description, image_url, price_usd, "
                 "status, moltbook_post_id) VALUES (1,?,?,?,?,?,?,?)",
                 (old, "Test piece", "d", "", 8.0, "live", "post-abc"))
m = learning3.active_model()
m.choose(learning3.active_candidates(), learning2.featurize_listing, context="1")
C.record_engagement_decision(1)
with db() as conn:
    n = conn.execute("SELECT COUNT(*) AS n FROM lints_decisions "
                     "WHERE model='listing_engagement_v1' AND context='1'").fetchone()["n"]
assert n == 1, "engagement pending decision must mirror the published action"
print("T2 publish-time engagement decision registered")

# 3. Two collection passes with growing engagement -> snapshots stored, then
#    outcome resolves as SUCCESS into the engagement model ONLY
counter = {"n": 0}
def fake_post(post_id):
    counter["n"] += 1
    return {"id": post_id, "upvotes": 1 if counter["n"] == 1 else 4, "comment_count": 0}
def fake_feed():
    return [{"title": "Agent art thread"}, {"title": "x" * 999}, {"bad": True}]

r1 = C.collect_once(fetch_post=fake_post, fetch_feed=fake_feed)
r2 = C.collect_once(fetch_post=fake_post, fetch_feed=fake_feed)
assert r1["stored"] == 1 and r2["stored"] == 1 and r2["resolved"] == 1, (r1, r2)
with db() as conn:
    eng = conn.execute("SELECT outcome FROM lints_decisions WHERE model='listing_engagement_v1' "
                       "AND context='1'").fetchone()["outcome"]
    sales = conn.execute("SELECT outcome FROM lints_decisions WHERE model=? "
                         "AND context='1'", (SALES,)).fetchone()["outcome"]
assert eng == "success", "positive delta must resolve engagement success"
assert sales == "pending", "sales model must remain UNTOUCHED by engagement data"
print("T3 engagement resolved into engagement model; sales model untouched")

# 4. Third pass: nothing new -> no duplicate resolution
r3 = C.collect_once(fetch_post=fake_post, fetch_feed=fake_feed)
assert r3["resolved"] == 0, "resolution must be one-time per listing"
print("T4 no double resolution")

# 5. Downward counters get flagged (suspicious data kept but marked)
def shrinking(post_id):
    return {"id": post_id, "upvotes": 0, "comment_count": 0}
r4 = C.collect_once(fetch_post=shrinking, fetch_feed=lambda: [])
with db() as conn:
    f = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE flagged=1").fetchone()["n"]
assert r4["flagged"] == 1 and f == 1
print("T5 monotonicity guard: shrinking counters flagged")

# 6. Circuit breaker opens after CB_THRESHOLD consecutive failing runs
def failing(post_id):
    return None
for _ in range(3):
    C.collect_once(fetch_post=failing, fetch_feed=lambda: [])
assert C.breaker_open() is True, "breaker must open after 3 failing runs"
r5 = C.collect_once(fetch_post=failing, fetch_feed=lambda: [])
assert r5.get("skipped") == "breaker_open", "open breaker must skip work"
print("T6 circuit breaker: opens and skips")

# 7. Heartbeat freshness
hb = C.heartbeat_status()
assert hb["alive"] and hb["stale"] is False and hb["age_minutes"] < 1
print(f"T7 heartbeat: alive, {hb['age_minutes']} min old")

# 8. Pruning removes old rows only
ancient = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
with db() as conn:
    conn.execute("INSERT INTO signals (ts, post_id, listing_id, upvotes, comments) "
                 "VALUES (?,?,?,?,?)", (ancient, "old", 1, 1, 1))
removed = C.prune()
assert removed == 1, f"exactly the ancient row should go, removed {removed}"
print("T8 retention pruning: old rows removed, fresh kept")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T9 ledger chain verified ({chain['entries']} entries)")
print("ALL COLLECTOR TESTS PASSED")

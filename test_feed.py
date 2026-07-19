"""Data-feed upgrade tests: adaptive polling schedule + trend detection.
Run: python test_feed.py"""
import os, tempfile
from datetime import datetime, timezone, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "feed.db")

from agent.db import init_db, db
from agent import ledger, learning, learning2, learning3, collector as C, intelligence as I
from agent.tools import payments
from agent.http import init as init_http

init_db(); init_http(); payments.init(); learning.init(); learning2.init(); learning3.init()
C.init(); I.init()

now = datetime.now(timezone.utc)

# 1. Adaptive polling schedule: fresh always, mid every 2nd, old every 4th run
fresh = (now - timedelta(hours=1)).isoformat()
mid = (now - timedelta(hours=24)).isoformat()
old = (now - timedelta(days=5)).isoformat()
assert all(C.poll_due(fresh, rc, now=now) for rc in range(1, 9)), "fresh: every run"
mids = [C.poll_due(mid, rc, now=now) for rc in range(1, 9)]
assert mids == [False, True] * 4, f"mid: every 2nd run, got {mids}"
olds = [C.poll_due(old, rc, now=now) for rc in range(1, 9)]
assert olds == [False, False, False, True] * 2, f"old: every 4th run, got {olds}"
assert C.poll_due("garbage-ts", 1, now=now) is True, "unparseable ts must not blind us"
print("T1 adaptive polling: fresh=every run, 24h=every 2nd, 5d=every 4th")

# 2. collect_once respects the schedule (old listing skipped on odd runs)
with db() as conn:
    conn.execute("INSERT INTO listings (id, ts, title, description, image_url, price_usd, "
                 "status, moltbook_post_id) VALUES (1,?,?,?,?,?,?,?)",
                 (old, "Old piece", "d", "", 8.0, "live", "post-old"))
calls = {"n": 0}
def fake_post(post_id):
    calls["n"] += 1
    return {"id": post_id, "upvotes": calls["n"], "comment_count": 0}
polled = []
for run in range(1, 9):   # collector run_counter goes 1..8
    C.collect_once(fetch_post=fake_post, fetch_feed=lambda: [])
    polled.append(calls["n"])
assert calls["n"] == 2, f"5-day-old post must be fetched on runs 4 and 8 only, got {calls['n']}"
print(f"T2 collect_once schedule: 8 runs -> {calls['n']} fetches for a 5-day-old post")

# 3. Trend detection: growing keyword must outrank a static keyword with a
#    bigger total, and stale growth must decay away.
feed_t0 = [
    {"id": f"r{i}", "title": f"Rising kelp garden {i}", "upvotes": 1, "comment_count": 0}
    for i in range(3)
] + [
    {"id": f"s{i}", "title": f"Static moon lore {i}", "upvotes": 500, "comment_count": 9}
    for i in range(3)
]
I.observe_feed(feed_t0)
# second sighting: kelp posts grew, moon posts did not move
feed_t1 = [
    {"id": f"r{i}", "title": f"Rising kelp garden {i}", "upvotes": 30, "comment_count": 6}
    for i in range(3)
] + [
    {"id": f"s{i}", "title": f"Static moon lore {i}", "upvotes": 500, "comment_count": 9}
    for i in range(3)
]
I.observe_feed(feed_t1)
tr = I.trending(now=now)
kws = {r["keyword"]: r for r in tr}
assert "kelp" in kws, f"growing keyword must be detected, got {list(kws)[:6]}"
assert "moon" not in kws, "zero-growth keyword must NOT trend despite huge totals"
print(f"T3 trending: 'kelp' momentum {kws['kelp']['momentum']} detected; "
      f"static 'moon' (500 upvotes, no growth) correctly absent")

# 4. Momentum decays with age of last sighting (half-life)
later = now + timedelta(days=6)   # 2 half-lives at default 3d
tr_later = I.trending(now=later)
m_later = next((r["momentum"] for r in tr_later if r["keyword"] == "kelp"), 0.0)
assert 0 < m_later < kws["kelp"]["momentum"] * 0.35, \
    f"6d-old momentum should decay to ~25%, got {m_later} vs {kws['kelp']['momentum']}"
print(f"T4 decay: kelp momentum {kws['kelp']['momentum']} -> {m_later} after 6 idle days")

# 5. analyze_market carries the trending block; hypothesize prompt gets it
a = I.analyze_market()
assert "trending" in a and isinstance(a["trending"], list)
seen_prompt = {}
def spy_llm(system, user):
    import json as J
    seen_prompt.update(J.loads(user))
    return J.dumps({"hypotheses": [{"topic": "kelp", "theme": "A kelp garden series."}]})
I.generate_hypotheses(llm_fn=spy_llm)
assert "rising_now" in seen_prompt, "hypothesis prompt must include momentum data"
print("T5 integration: analyze_market exposes 'trending'; hypothesizer receives 'rising_now'")

# 6. Feed -> opportunity pipeline: intent posts become ranked opportunities,
#    chatter does not, and re-seeing the same post does not duplicate.
from agent import opportunity as O
intent_feed = [
    {"id": "req-1", "title": "Looking to buy custom pixel art for my agent",
     "content": "Budget $15, need it this week. DM me.", "upvotes": 2, "comment_count": 1},
    {"id": "chat-1", "title": "Nice sunset today on the reef",
     "content": "Just vibes.", "upvotes": 50, "comment_count": 8},
]
C.collect_once(fetch_post=fake_post, fetch_feed=lambda: intent_feed)
opps = O.rank(limit=10)
subjects = [o["subject"] for o in opps]
assert any("pixel art" in s for s in subjects), f"intent post must be ingested, got {subjects}"
assert not any("sunset" in s.lower() for s in subjects), "chatter must NOT become an opportunity"
n_before = len(O.rank(limit=50, min_score=-1))
C.collect_once(fetch_post=fake_post, fetch_feed=lambda: intent_feed)  # same feed again
assert len(O.rank(limit=50, min_score=-1)) == n_before, "re-seen post must dedupe"
print(f"T6 feed->opportunity: request post ingested (score {opps[0]['live_score']}), "
      "chatter filtered, dedupe holds")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T7 ledger chain verified ({chain['entries']} entries)")
print("ALL FEED TESTS PASSED")

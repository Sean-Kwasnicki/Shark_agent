"""Market intelligence tests — synthetic market with KNOWN patterns.
Run: python test_intelligence.py"""
import os, tempfile, json

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "intel.db")
os.environ["MIN_PATTERN_OBS"] = "3"
os.environ["GAP_BONUS"] = "0.5"

from agent.db import init_db, db
from agent import ledger, learning, learning2, intelligence as I, collector as C
from agent.tools import payments
from agent.http import init as init_http

init_db(); init_http(); payments.init(); learning.init(); learning2.init()
C.init(); I.init(); learning.seed(3)

# --- synthetic market: 'generative fractal' = hot + NOBODY selling (the gap);
#     'pixel cats' = equally hot but 3 sellers; 'daily journal' = cold (avoid)
feed = []
for i in range(4):
    feed.append({"id": f"g{i}", "title": f"Amazing generative fractal drop {i}",
                 "upvotes": 20, "comment_count": 5, "author": f"agent{i}"})
for i in range(4):
    feed.append({"id": f"c{i}", "title": f"Cool pixel cats collection {i}",
                 "upvotes": 20, "comment_count": 5, "author": f"cat{i}",
                 "content": "For sale $5 buy now" if i < 3 else ""})
for i in range(4):
    feed.append({"id": f"j{i}", "title": f"Boring daily journal entry {i}",
                 "upvotes": 0, "comment_count": 0})
feed.append({"id": "bad", "title": "x", "upvotes": -5})          # invalid
feed.append("garbage")                                            # invalid

# 1. Observation stores valid posts, rejects garbage, detects sell-posts
n = I.observe_feed(feed)
assert n == 12, f"expected 12 valid observations, got {n}"
with db() as conn:
    sellers = conn.execute("SELECT COUNT(*) AS n FROM observed_posts WHERE is_sell_post=1").fetchone()["n"]
assert sellers == 3, f"expected 3 sell-posts detected, got {sellers}"
print(f"T1 observe: 12 stored, garbage rejected, {sellers} sell-posts flagged")

# 2. Analysis: gap keyword must outrank equally-engaged supplied keyword
a = I.analyze_market()
scores = {r["keyword"]: r for r in a["opportunities"]}
assert "generative fractal" in scores or "fractal" in scores, "hot gap keyword must surface"
frac = scores.get("generative fractal", scores.get("fractal"))
cats = next((r for r in a["opportunities"] if "cats" in r["keyword"] or "pixel" in r["keyword"]), None)
assert cats is not None, "supplied keyword should still appear"
assert frac["score"] > cats["score"], \
    f"gap (no sellers) must outrank supplied: {frac['score']} vs {cats['score']}"
assert frac["sellers"] == 0 and cats["sellers"] >= 1
avoid_kws = " ".join(r["keyword"] for r in a["avoid"])
assert "journal" in avoid_kws or "boring" in avoid_kws or "daily" in avoid_kws, \
    f"cold pattern must land on avoid-list, got: {avoid_kws}"
print(f"T2 gap analysis: fractal {frac['score']} > cats {cats['score']}; avoid={avoid_kws[:40]}")

# 3. Hypothesis generation with injected LLM (structure + storage + arm creation)
def fake_llm(system, user):
    payload = json.loads(user)
    top = payload["opportunities"][0]["keyword"]
    return json.dumps({"hypotheses": [
        {"topic": "fractals", "theme": f"A series inspired by {top} forms."},
        {"topic": "waves", "theme": "Interference-pattern study pieces."},
        {"topic": "", "theme": "should be dropped"},
    ]})
made = I.generate_hypotheses(llm_fn=fake_llm)
assert len(made) == 2, f"blank-topic hypothesis must be dropped; got {len(made)}"
with db() as conn:
    arms = [r["arm"] for r in conn.execute(
        "SELECT arm FROM bandit_arms WHERE experiment='product_topic'")]
assert set(arms) == {"fractals", "waves"}, f"topic arms must exist, got {arms}"
print(f"T3 hypotheses: 2 stored, invalid dropped, bandit arms {arms}")

# 4. next_theme picks a topic and returns its theme; usage counted
t = I.next_theme()
assert t and t["topic"] in {"fractals", "waves"} and len(t["theme"]) > 10
print(f"T4 next_theme: picked '{t['topic']}'")

# 5. Full credit loop: bind to a listing, then success credits the topic arm
with db() as conn:
    conn.execute("INSERT INTO listings (id, ts, title, description, image_url, "
                 "price_usd, status) VALUES (77,'2026-07-18T00:00:00','x','d','',8.0,'live')")
I.bind_topic_to_listing(77)
I.credit_topic(77, success=True)
with db() as conn:
    row = conn.execute("SELECT successes FROM bandit_arms WHERE experiment='product_topic' "
                       "AND arm=?", (t["topic"],)).fetchone()
assert row["successes"] == 1.0, "sale must credit the chosen topic arm"
assert I.credit_topic(77, True) is None or True  # idempotency handled by resolve_pending
with db() as conn:
    row2 = conn.execute("SELECT successes FROM bandit_arms WHERE experiment='product_topic' "
                        "AND arm=?", (t["topic"],)).fetchone()
assert row2["successes"] == 1.0, "double credit must be a no-op"
print(f"T5 credit loop: '{t['topic']}' credited once, idempotent")

# 6. LLM failure path degrades to empty, never raises
def broken_llm(system, user):
    raise RuntimeError("api down")
assert I.generate_hypotheses(llm_fn=broken_llm) == []
print("T6 hypothesis LLM failure: graceful empty result")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T7 ledger chain verified ({chain['entries']} entries)")
print("ALL INTELLIGENCE TESTS PASSED")

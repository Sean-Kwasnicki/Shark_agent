"""Hardening tests: ledger chain under concurrent writers + stuck-order recovery.
Run: python test_hardening.py"""
import os, tempfile, threading

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "hard.db")
from agent.db import init_db, db
from agent import ledger
from agent.http import init as init_http
from agent.tools import payments
init_db(); init_http(); payments.init()

# 1. 8 threads x 40 appends each — the chain must survive concurrency intact
def writer(tid):
    for i in range(40):
        ledger.record("system", "stress.write", {"t": tid, "i": i})
threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
chain = ledger.verify_chain()
assert chain["ok"] and chain["entries"] == 320, f"chain broke: {chain}"
print(f"T1 concurrency: 320 concurrent appends, chain verified intact")

# 2. Stuck-order recovery: 'paid' order with no mint -> recovered via idempotent mint
import agent.tools.nft as nftmod
calls = {"n": 0}
def fake_mint(recipient, name, description, image_url, attributes=None):
    calls["n"] += 1
    return {"id": f"nft_{calls['n']}"}
nftmod.mint_to = fake_mint  # inject; real mint is network-only
from agent import commerce
with db() as conn:
    conn.execute("INSERT INTO listings (id, ts, title, description, image_url, price_usd, status) "
                 "VALUES (5,'2026-07-18T00:00:00','Piece','d','',8.0,'live')")
    conn.execute("INSERT INTO orders (ts, listing_id, stripe_ref, amount_usd, recipient, status) "
                 "VALUES ('2026-07-18T00:00:00',5,'cs_stuck_1',8.0,'email:b@x.com:polygon','paid')")
    conn.execute("INSERT INTO orders (ts, listing_id, stripe_ref, amount_usd, recipient, status) "
                 "VALUES ('2026-07-18T00:00:00',5,'cs_stuck_2',8.0,'email:c@x.com:polygon','failed')")
n = commerce.recover_stuck_orders()
assert n == 2, f"both stuck orders must recover, got {n}"
with db() as conn:
    left = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status!='minted'").fetchone()["n"]
assert left == 0
n2 = commerce.recover_stuck_orders()
assert n2 == 0, "second pass must find nothing"
print("T2 recovery: 2 stuck orders minted, second pass no-op")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T3 chain still verified ({chain['entries']} entries)")
print("ALL HARDENING TESTS PASSED")

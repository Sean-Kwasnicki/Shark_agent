"""Smoke test — deterministic core only (no network, no LLM). Run: python test_core.py"""
import os, tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["MAX_DAILY_SPEND_USD"] = "50"
os.environ["MAX_TOTAL_SPEND_USD"] = "60"

from agent.db import init_db
from agent import ledger, memory, spending, constraints

init_db()

# 1. Ledger hash chain
ledger.record("system", "boot", {"test": True})
ledger.record("agent", "memory.write", {"id": 1})
assert ledger.verify_chain()["ok"], "chain should verify"

# 2. Memory write + retrieval ranking
memory.write("fact", "Property tax appeal deadlines vary by county", tags="tax", importance=5)
memory.write("fact", "Owner prefers deterministic systems", importance=4)
top = memory.retrieve("property tax county")[0]
assert "Property tax" in top["content"], "keyword retrieval should rank tax fact first"

# 3. Spend lifecycle: request -> approve -> execute
req = spending.request_spend("Test purchase", 40.0, "TestVendor")
assert spending.pending()[0]["id"] == req["id"]
spending.decide(req["id"], approve=True)
out = spending.execute_spend(req["id"])
assert out["status"] == "executed" and out["simulated"] is True
assert abs(ledger.total_spend() - 40.0) < 1e-6

# 4. Caps: next 40 would exceed daily(50) and lifetime(60) — must be blocked
try:
    spending.request_spend("Second purchase", 40.0, "TestVendor")
    raise AssertionError("cap should have blocked this")
except constraints.ConstraintViolation:
    pass

# 5. Execute on non-approved request must fail
req2 = spending.request_spend("Small item", 5.0, "TestVendor")
try:
    spending.execute_spend(req2["id"])
    raise AssertionError("unapproved execute should fail")
except constraints.ConstraintViolation:
    pass

# 6. Tool allowlist + forbidden domains
try:
    constraints.check_tool_allowed("wallet.transfer")
    raise AssertionError("unlisted tool should be blocked")
except constraints.ConstraintViolation:
    pass
try:
    constraints.check_url_allowed("https://mybank.example.com/login")
    raise AssertionError("forbidden domain should be blocked")
except constraints.ConstraintViolation:
    pass

# 7. Chain still valid after all activity
chain = ledger.verify_chain()
assert chain["ok"], f"chain broken at {chain['first_bad_id']}"

print(f"ALL TESTS PASSED — {chain['entries']} ledger entries, chain verified")

# ---- v1.1 additions: offline-testable commerce/reliability layers ----
import time, hmac as _hmac, hashlib as _hashlib
from agent.http import init as init_http, seen, remember
from agent.tools import payments as pay, moltbook as mb

init_http(); pay.init()

# 8. Idempotency store
remember("op:abc", {"id": 1})
assert seen("op:abc")["id"] == 1 and seen("op:missing") is None

# 9. Stripe webhook signature (documented t/v1 HMAC scheme), pure-function test
secret = "whsec_testsecret"
body = b'{"type":"checkout.session.completed"}'
t = int(time.time())
v1 = _hmac.new(secret.encode(), f"{t}.".encode() + body, _hashlib.sha256).hexdigest()
good = f"t={t},v1={v1}"
assert pay.verify_webhook_signature(body, good, secret) is True
assert pay.verify_webhook_signature(body, f"t={t},v1={'0'*64}", secret) is False   # bad hmac
assert pay.verify_webhook_signature(body, good, secret, now=t + 999) is False      # stale
assert pay.verify_webhook_signature(body, "garbage", secret) is False              # malformed

# 10. Order recording is idempotent on stripe_ref, revenue ledgered
oid = pay.record_paid_order(1, "cs_test_123", 9.99, "email:buyer@x.com:polygon")
assert oid is not None
assert pay.record_paid_order(1, "cs_test_123", 9.99, "email:buyer@x.com:polygon") is None
assert abs(pay.total_revenue() - 9.99) < 1e-6
assert abs(ledger.total_spend() - 40.0) < 1e-6  # revenue must NOT count as spend (only the $40 executed)

# 11. Moltbook client-side rate limiter reads the ledger
assert mb.can_post() is True
ledger.record("agent", "moltbook.post", {"submolt": "general", "title": "x"})
assert mb.can_post() is False   # within 35-min window now

# 12. Chain still valid after everything
chain2 = ledger.verify_chain()
assert chain2["ok"], f"chain broken at {chain2['first_bad_id']}"
print(f"V1.1 TESTS PASSED — {chain2['entries']} ledger entries, chain verified")

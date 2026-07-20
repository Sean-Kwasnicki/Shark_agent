"""Compliant outreach lane tests (Pounce mailer + rainmaker-style guardrails).
Run: python test_outreach.py

Network is never touched — SMTP is injected with a fake mailer.
"""
import os
import tempfile
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "outreach.db")
os.environ["OUTREACH_UNSUBSCRIBE_URL"] = "mailto:unsub@example.com"
os.environ["OUTREACH_MAILING_ADDRESS"] = "123 Test St, Portland OR 97201"
os.environ["OUTREACH_SENDER_NAME"] = "Shark Test"
os.environ["OUTREACH_UTC_OFFSET_HOURS"] = "0"
os.environ["OUTREACH_SEND_START_HOUR"] = "0"
os.environ["OUTREACH_SEND_END_HOUR"] = "23"
os.environ["OUTREACH_SEND_ON_WEEKENDS"] = "true"
os.environ["OUTREACH_WARMUP_RAMP"] = "2,5"
os.environ["OUTREACH_STEADY_DAILY_CAP"] = "5"
os.environ["OUTREACH_PER_DOMAIN_DAILY_CAP"] = "2"

from agent.db import init_db
from agent import ledger, outreach as O
from agent.config import HARD_RULES
from agent import planner

init_db()
O.init()


@dataclass
class FakeResult:
    ok: bool
    attempts: int = 1
    error: str = ""


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send_now(self, to, subject, text, html=None):
        self.sent.append({"to": to, "subject": subject, "text": text})
        return FakeResult(True)


# 1. Subject compliance
ok, _ = O.subject_is_compliant("Custom automation for your agent")
assert ok
bad, why = O.subject_is_compliant("Re: you won free money!!!")
assert not bad and "deceptive" in why
print("T1 subject compliance: truthful ok, deceptive rejected")

# 2. Footer always appended at draft time
from agent.db import db
d = O.draft("buyer@example.com", "Agent automation offer",
            "We can build the workflow you asked about.")
assert d["status"] == "draft" and d.get("deduped") is not True
with db() as conn:
    body = conn.execute("SELECT body FROM outreach WHERE id=?", (d["id"],)).fetchone()["body"]
assert "mailto:unsub@example.com" in body
assert "123 Test St" in body
assert "commercial outreach from Shark Test" in body
print("T2 draft appends CAN-SPAM footer before owner review")

# 3. Dedup
d2 = O.draft("buyer@example.com", "Again", "Again body", opportunity_id=0)
assert d2.get("deduped") is True
print("T3 draft dedupe on (recipient, opportunity_id)")

# 4. Suppression blocks draft and send
O.suppress("blocked@example.com", "unsubscribe")
try:
    O.draft("blocked@example.com", "Hi", "Body")
    raise AssertionError("suppressed recipient must refuse draft")
except ValueError as e:
    assert "suppression" in str(e).lower()
print("T4 suppression list blocks drafting")

# 5. Approval gate — cannot send from draft
try:
    O.send(d["id"], mailer=FakeMailer())
    raise AssertionError("must not send unapproved draft")
except ValueError as e:
    assert "approved" in str(e).lower() or "draft" in str(e).lower()
O.decide(d["id"], True)
fm = FakeMailer()
# force a weekday noon UTC so window is open under our env
now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)  # Monday
res = O.send(d["id"], mailer=fm, now=now)
assert res["sent"] is True and len(fm.sent) == 1
print("T5 owner approve then send succeeds through fake SMTP")

# 6. Daily warmup cap (day-0 ramp=2): buyer already sent in T5; one more ok, then stop
d3 = O.draft("other@example.com", "Second offer", "Body", opportunity_id=1)
O.decide(d3["id"], True)
r3 = O.send(d3["id"], mailer=FakeMailer(), now=now)
assert r3["sent"] is True
d4 = O.draft("a@elsewhere.com", "Third offer", "Body", opportunity_id=2)
O.decide(d4["id"], True)
r4 = O.send(d4["id"], mailer=FakeMailer(), now=now)
assert r4["sent"] is False and "cap" in r4["reason"]
print(f"T6 daily warmup cap blocks 3rd send: {r4['reason']}")

# 7. Per-domain cap with room under the daily cap (raise warmup for this check)
O.WARMUP_RAMP = [50]
O.STEADY_DAILY_CAP = 50
# two already sent today to example.com; domain cap=2 so another example.com fails
d5 = O.draft("third@example.com", "Domain check", "Body", opportunity_id=3)
O.decide(d5["id"], True)
r5 = O.send(d5["id"], mailer=FakeMailer(), now=now)
assert r5["sent"] is False and "domain" in r5["reason"]
print(f"T7 per-domain daily cap enforced: {r5['reason']}")

# 8. Weekend block when configured
os.environ["OUTREACH_SEND_ON_WEEKENDS"] = "false"
# re-read module settings — outreach reads env at import time, so call helper directly
# within_send_window uses module-level SEND_ON_WEEKENDS; patch it
O.SEND_ON_WEEKENDS = False
sat = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)  # Saturday
ok, why = O.within_send_window(sat)
assert not ok and why == "weekend"
O.SEND_ON_WEEKENDS = True
print("T8 weekend send window respected")

# 9. Planner allowlist + dispatch
assert "outreach.draft" in HARD_RULES["allowed_tools"]
# Need CAN-SPAM env still set; use a unique recipient
out = planner._dispatch({
    "tool": "outreach.draft",
    "args": {"recipient": "planner@unique.test", "subject": "Planner draft",
             "body": "Hello from planner", "opportunity_id": 99},
})
assert out["status"] == "draft" and out["recipient"] == "planner@unique.test"
print("T9 planner dispatches outreach.draft and tool is allowlisted")

chain = ledger.verify_chain()
assert chain["ok"]
print(f"T10 ledger chain verified ({chain['entries']} entries)")
print("ALL OUTREACH TESTS PASSED")

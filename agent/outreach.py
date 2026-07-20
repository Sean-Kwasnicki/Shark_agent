"""
Compliant email outreach lane — guardrail logic adapted from the rainmaker
project (src/deliver/guardrails.py), transport from the Pounce mailer port
(agent/tools/mailer.py).

Pipeline (every step ledgered):
  1. draft()   — agent/planner composes a message for a scored opportunity.
                 The CAN-SPAM footer (unsubscribe + physical mailing address +
                 ad disclosure) is appended AT DRAFT TIME so the owner reviews
                 exactly what would be sent. Deduped per (recipient, opportunity).
  2. approve() — HUMAN gate. No email leaves without an explicit owner approval;
                 there is no auto-send flag, by design.
  3. send()    — passes through evaluate_send(), the single choke point:
                 suppression list, truthful-subject check, footer presence,
                 warmup/daily caps, per-recipient-domain cap, business-hours
                 send window. Only then does SMTP run.

Compliance stance (US CAN-SPAM): commercial email must have a truthful subject,
identify itself as an ad, include the sender's physical postal address, and a
working unsubscribe honored promptly. Suppression is enforced in code before
every send. EU/GDPR recipients require consent — do not target EU addresses
with cold email; the human approval step is where that judgment is applied.

The suppression list is append-mostly: unsubscribes/bounces/complaints go in
via suppress() (owner API) and are never auto-removed.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from agent.db import db
from agent import ledger
from agent.tools.mailer import Mailer, MailerConfig

# --- configuration -----------------------------------------------------------
UNSUBSCRIBE_URL = os.getenv("OUTREACH_UNSUBSCRIBE_URL", "")   # mailto: works
MAILING_ADDRESS = os.getenv("OUTREACH_MAILING_ADDRESS", "")   # CAN-SPAM: required
SENDER_NAME = os.getenv("OUTREACH_SENDER_NAME", "")           # ad disclosure name
# Warm-up ramp: max sends/day for day 0,1,2,... since first send, then steady.
WARMUP_RAMP = [int(x) for x in os.getenv("OUTREACH_WARMUP_RAMP", "5,10,20,40,80").split(",") if x.strip()]
STEADY_DAILY_CAP = int(os.getenv("OUTREACH_STEADY_DAILY_CAP", "40"))
PER_DOMAIN_DAILY_CAP = int(os.getenv("OUTREACH_PER_DOMAIN_DAILY_CAP", "5"))
# Send window in the owner's local time (UTC offset in hours, e.g. -7 for PDT).
UTC_OFFSET_HOURS = float(os.getenv("OUTREACH_UTC_OFFSET_HOURS", "0"))
SEND_START_HOUR = int(os.getenv("OUTREACH_SEND_START_HOUR", "8"))
SEND_END_HOUR = int(os.getenv("OUTREACH_SEND_END_HOUR", "18"))
SEND_ON_WEEKENDS = os.getenv("OUTREACH_SEND_ON_WEEKENDS", "false").lower() == "true"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Deceptive-subject patterns (CAN-SPAM: subject must not be misleading).
_MISLEADING_SUBJECT = [
    r"^\s*re:", r"^\s*fwd:",                    # fake reply/forward
    r"\byou won\b", r"\bfree money\b", r"\b100% free\b", r"\bguarantee(d)?\b",
    r"\burgent\b", r"!!+",
]

OUTREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    opportunity_id INTEGER NOT NULL DEFAULT 0,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | approved | sent | denied | failed
    error TEXT NOT NULL DEFAULT '',
    sent_ts TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_dedupe
    ON outreach(recipient, opportunity_id);
CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


def init():
    with db() as conn:
        conn.executescript(OUTREACH_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _local(now_utc: datetime) -> datetime:
    return now_utc + timedelta(hours=UTC_OFFSET_HOURS)


# --- compliance primitives (pure, unit-tested) -------------------------------

def subject_is_compliant(subject: str) -> tuple[bool, str]:
    s = (subject or "").lower()
    if not s.strip():
        return False, "empty subject"
    for p in _MISLEADING_SUBJECT:
        if re.search(p, s):
            return False, f"subject matches deceptive pattern: {p}"
    return True, ""


def compliance_footer() -> str:
    """The CAN-SPAM block appended to every draft. Empty required fields are a
    config error surfaced at draft time, not silently omitted at send time."""
    parts = ["\n\n---"]
    if SENDER_NAME:
        parts.append(f"Advertisement: commercial outreach from {SENDER_NAME}.")
    if MAILING_ADDRESS:
        parts.append(MAILING_ADDRESS)
    if UNSUBSCRIBE_URL:
        parts.append(f"Unsubscribe: {UNSUBSCRIBE_URL}")
    return "\n".join(parts)


def body_is_compliant(body: str) -> tuple[bool, str]:
    if UNSUBSCRIBE_URL and UNSUBSCRIBE_URL not in body:
        return False, "missing unsubscribe link"
    if MAILING_ADDRESS and MAILING_ADDRESS not in body:
        return False, "missing physical mailing address"
    if SENDER_NAME and f"commercial outreach from {SENDER_NAME}" not in body:
        return False, "missing commercial-email disclosure"
    return True, ""


def is_suppressed(email: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM suppression WHERE email=?",
                           ((email or "").lower(),)).fetchone()
    return row is not None


def warmup_daily_cap(days_since_first_send: int) -> int:
    d = max(0, days_since_first_send)
    if d >= len(WARMUP_RAMP):
        return STEADY_DAILY_CAP
    return WARMUP_RAMP[d]


def within_send_window(now_local: datetime) -> tuple[bool, str]:
    if not SEND_ON_WEEKENDS and now_local.weekday() >= 5:
        return False, "weekend"
    if not (SEND_START_HOUR <= now_local.hour < SEND_END_HOUR):
        return False, f"outside send window ({SEND_START_HOUR}:00-{SEND_END_HOUR}:00 local)"
    return True, ""


# --- send accounting ----------------------------------------------------------

def _sent_stats(now_local: datetime, domain: str) -> tuple[int, int, int]:
    """(sent today total, sent today to this domain, days since first send).
    'Today' is the owner's local calendar day, matching the send window."""
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = (day_start_local - timedelta(hours=UTC_OFFSET_HOURS)).isoformat()
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM outreach WHERE status='sent' AND sent_ts>=?",
            (day_start_utc,)).fetchone()["n"]
        dom = conn.execute(
            "SELECT COUNT(*) AS n FROM outreach WHERE status='sent' AND sent_ts>=? "
            "AND recipient LIKE ?", (day_start_utc, f"%@{domain}")).fetchone()["n"]
        first = conn.execute(
            "SELECT MIN(sent_ts) AS t FROM outreach WHERE status='sent' AND sent_ts!=''"
        ).fetchone()["t"]
    days = 0
    if first:
        try:
            first_local = _local(datetime.fromisoformat(first))
            days = (now_local.date() - first_local.date()).days
        except (TypeError, ValueError):
            days = 0
    return int(total), int(dom), days


def evaluate_send(recipient: str, subject: str, body: str,
                  now: datetime | None = None) -> tuple[bool, str]:
    """The single choke point every outreach send passes through.
    Order: suppression & compliance first, then volume, then timing."""
    if is_suppressed(recipient):
        return False, "recipient suppressed (unsubscribe/bounce/complaint)"
    ok, why = subject_is_compliant(subject)
    if not ok:
        return False, f"subject non-compliant: {why}"
    ok, why = body_is_compliant(body)
    if not ok:
        return False, f"body non-compliant: {why}"
    now_local = _local(now or _now())
    domain = recipient.rsplit("@", 1)[-1].lower()
    total, dom, days = _sent_stats(now_local, domain)
    cap = warmup_daily_cap(days)
    if total >= cap:
        return False, f"daily warmup/steady cap reached ({cap})"
    if dom >= PER_DOMAIN_DAILY_CAP:
        return False, f"per-recipient-domain cap reached ({PER_DOMAIN_DAILY_CAP})"
    ok, why = within_send_window(now_local)
    if not ok:
        return False, why
    return True, "ok"


# --- lifecycle ----------------------------------------------------------------

def draft(recipient: str, subject: str, body: str,
          opportunity_id: int = 0) -> dict:
    """Store a reviewed-ready draft. Footer appended here so the owner approves
    the exact final content. Idempotent per (recipient, opportunity_id)."""
    recipient = (recipient or "").strip().lower()
    if not _EMAIL_RE.match(recipient):
        raise ValueError(f"invalid recipient email: {recipient!r}")
    if is_suppressed(recipient):
        raise ValueError("recipient is on the suppression list; refusing to draft.")
    if not UNSUBSCRIBE_URL or not MAILING_ADDRESS:
        raise ValueError(
            "OUTREACH_UNSUBSCRIBE_URL and OUTREACH_MAILING_ADDRESS must be set "
            "before drafting outreach (CAN-SPAM requires both).")
    ok, why = subject_is_compliant(subject)
    if not ok:
        raise ValueError(f"subject non-compliant: {why}")
    full_body = body.rstrip() + compliance_footer()
    with db() as conn:
        dup = conn.execute(
            "SELECT id, status FROM outreach WHERE recipient=? AND opportunity_id=?",
            (recipient, opportunity_id)).fetchone()
        if dup:
            return {"id": dup["id"], "status": dup["status"], "deduped": True}
        cur = conn.execute(
            "INSERT INTO outreach (ts, opportunity_id, recipient, subject, body) "
            "VALUES (?,?,?,?,?)",
            (_now().isoformat(), opportunity_id, recipient, subject[:200], full_body[:5000]))
        oid = cur.lastrowid
    ledger.record("agent", "outreach.draft",
                  {"id": oid, "recipient": recipient, "subject": subject[:120],
                   "opportunity_id": opportunity_id})
    try:
        from agent.tools import notify
        notify.owner(
            f"Outreach draft #{oid} to {recipient}: '{subject[:80]}'. "
            f"Review then POST /outreach/{oid}/decide and /outreach/{oid}/send.")
    except Exception:
        pass
    return {"id": oid, "status": "draft", "recipient": recipient}


def decide(outreach_id: int, approve: bool) -> dict:
    """Owner approval gate. Approving does NOT send; call send() after."""
    status = "approved" if approve else "denied"
    with db() as conn:
        row = conn.execute("SELECT status FROM outreach WHERE id=?", (outreach_id,)).fetchone()
        if not row:
            raise ValueError(f"outreach #{outreach_id} not found")
        if row["status"] != "draft":
            raise ValueError(f"outreach #{outreach_id} is '{row['status']}', not 'draft'")
        conn.execute("UPDATE outreach SET status=? WHERE id=?", (status, outreach_id))
    ledger.record("owner", "outreach.decide", {"id": outreach_id, "status": status})
    return {"id": outreach_id, "status": status}


_default_mailer: Mailer | None = None


def _get_mailer() -> Mailer:
    global _default_mailer
    if _default_mailer is None:
        _default_mailer = Mailer(MailerConfig.from_env("SMTP_"))
    return _default_mailer


def send(outreach_id: int, mailer: Mailer | None = None,
         now: datetime | None = None) -> dict:
    """Send one APPROVED outreach through the guardrail choke point. A guardrail
    denial leaves the row 'approved' (retryable later, e.g. window/caps); an
    SMTP failure marks it 'failed' with the honest error."""
    with db() as conn:
        row = conn.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone()
    if not row:
        raise ValueError(f"outreach #{outreach_id} not found")
    if row["status"] != "approved":
        raise ValueError(f"outreach #{outreach_id} is '{row['status']}'; only "
                         "owner-approved drafts can be sent.")
    allowed, reason = evaluate_send(row["recipient"], row["subject"], row["body"], now=now)
    if not allowed:
        ledger.record("system", "outreach.blocked", {"id": outreach_id, "reason": reason})
        return {"id": outreach_id, "sent": False, "reason": reason, "status": "approved"}
    m = mailer or _get_mailer()
    res = m.send_now(row["recipient"], row["subject"], row["body"])
    if res.ok:
        with db() as conn:
            conn.execute("UPDATE outreach SET status='sent', sent_ts=? WHERE id=?",
                         (_now().isoformat(), outreach_id))
        ledger.record("agent", "outreach.sent",
                      {"id": outreach_id, "recipient": row["recipient"],
                       "attempts": res.attempts})
        return {"id": outreach_id, "sent": True, "status": "sent"}
    with db() as conn:
        conn.execute("UPDATE outreach SET status='failed', error=? WHERE id=?",
                     (res.error[:300], outreach_id))
    ledger.record("system", "outreach.failed",
                  {"id": outreach_id, "recipient": row["recipient"], "err": res.error[:200]})
    return {"id": outreach_id, "sent": False, "status": "failed", "error": res.error}


def suppress(email: str, reason: str = "unsubscribe") -> dict:
    """Add to the suppression list (unsub/bounce/complaint). Never auto-removed."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f"invalid email: {email!r}")
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO suppression (email, reason, ts) VALUES (?,?,?)",
                     (email, reason[:100], _now().isoformat()))
    ledger.record("owner", "outreach.suppress", {"email": email, "reason": reason[:100]})
    return {"email": email, "suppressed": True}


def queue(limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, ts, opportunity_id, recipient, subject, status, error, sent_ts "
            "FROM outreach ORDER BY id DESC LIMIT ?", (limit,))]
    return rows

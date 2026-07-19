"""
Spending lifecycle. The agent can only REQUEST a spend. Money moves only
after an owner approves via the API, and even approved spends re-check
caps at execution time.

execute_spend() ships as a SIMULATED payment (records the transaction but
moves no money). Wire a real payment method deliberately — e.g. a Stripe
Issuing virtual card with its own hard limit, or Privacy.com — and keep
the platform-level limit BELOW your config caps as a second wall.
"""
from datetime import datetime, timezone
from agent.db import db
from agent import ledger, constraints
from agent.tools import notify


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_spend(description: str, amount_usd: float, vendor: str) -> dict:
    constraints.check_spend(amount_usd, auto=False)  # fails early if caps already blown
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO spend_requests (ts, description, amount_usd, vendor) VALUES (?,?,?,?)",
            (_now(), description, amount_usd, vendor),
        )
        req_id = cur.lastrowid
    ledger.record("agent", "purchase.request",
                  {"id": req_id, "desc": description, "amount": amount_usd, "vendor": vendor})
    notify.owner(f"Spend request #{req_id}: ${amount_usd:.2f} at {vendor} — {description}. "
                 f"Approve or deny in the review dashboard.")
    return {"id": req_id, "status": "pending"}


def decide(req_id: int, approve: bool) -> dict:
    status = "approved" if approve else "denied"
    with db() as conn:
        conn.execute("UPDATE spend_requests SET status=?, decided_ts=? WHERE id=? AND status='pending'",
                     (status, _now(), req_id))
    ledger.record("owner", "purchase.decision", {"id": req_id, "status": status})
    return {"id": req_id, "status": status}


def execute_spend(req_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM spend_requests WHERE id=?", (req_id,)).fetchone()
    if not row or row["status"] != "approved":
        raise constraints.ConstraintViolation(f"Spend #{req_id} is not in approved state.")
    constraints.check_spend(row["amount_usd"], auto=False)  # re-check caps at execution time
    # --- SIMULATED PAYMENT: replace with real payment integration deliberately ---
    with db() as conn:
        conn.execute("UPDATE spend_requests SET status='executed', executed_ts=? WHERE id=?",
                     (_now(), req_id))
    ledger.record("agent", "purchase.execute",
                  {"id": req_id, "vendor": row["vendor"], "desc": row["description"],
                   "simulated": True},
                  cost_usd=row["amount_usd"])
    return {"id": req_id, "status": "executed", "simulated": True}


def pending() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM spend_requests WHERE status='pending' ORDER BY id").fetchall()
    return [dict(r) for r in rows]

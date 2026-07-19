"""
FastAPI entrypoint. Runs the cycle on a schedule and exposes the owner's
control surface: review queue, ledger, memory, manual cycle trigger.

Deploy: Railway → start command `uvicorn main:app --host 0.0.0.0 --port $PORT`
Protect every route with OWNER_TOKEN (set it in env; requests must send
Authorization: Bearer <token>).
"""
import os
import hmac
import json
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel

from agent.db import init_db
from agent import config, ledger, memory, spending, planner, commerce, learning, learning2, economics, collector, intelligence
from agent.http import init as init_http
from agent.tools import payments

OWNER_TOKEN = os.getenv("OWNER_TOKEN", "")


def _auth(authorization: str | None):
    if not OWNER_TOKEN:
        raise HTTPException(500, "OWNER_TOKEN not configured; refusing to run open.")
    if not hmac.compare_digest(authorization or "", f"Bearer {OWNER_TOKEN}"):
        raise HTTPException(401, "Unauthorized")


async def _scheduler():
    interval = config.HARD_RULES["cycle_interval_minutes"] * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(planner.run_cycle)
        except Exception as e:
            ledger.record("system", "scheduler.error", {"err": str(e)})


async def _collector_loop():
    """Independent 24/7 data loop — pure HTTP + SQLite, ~zero cost, no LLM."""
    interval = collector.COLLECTOR_INTERVAL_MINUTES * 60
    while True:
        try:
            await asyncio.to_thread(collector.collect_once)
        except Exception as e:
            ledger.record("system", "collector.loop_error", {"err": str(e)})
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_http()
    payments.init()
    learning.init()
    learning2.init()
    economics.init()
    collector.init()
    intelligence.init()
    ledger.record("system", "boot", {"agent": config.AGENT_NAME})
    task = asyncio.create_task(_scheduler())
    ctask = asyncio.create_task(_collector_loop())
    yield
    task.cancel()
    ctask.cancel()


app = FastAPI(title=f"{config.AGENT_NAME} agent", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "agent": config.AGENT_NAME,
            "collector": collector.heartbeat_status()}


@app.post("/cycle")
def trigger_cycle(authorization: str | None = Header(None)):
    _auth(authorization)
    return planner.run_cycle()


@app.get("/ledger")
def get_ledger(limit: int = 50, authorization: str | None = Header(None)):
    _auth(authorization)
    return {"entries": ledger.recent(limit), "chain": ledger.verify_chain()}


@app.get("/ledger/export")
def ledger_export(authorization: str | None = Header(None)):
    """Full chain export — off-box backup, or publish it to make the agent's
    history independently verifiable (the thing the Bartok story never had)."""
    _auth(authorization)
    from agent.db import db as _db
    with _db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ledger ORDER BY id")]
    return {"entries": rows, "chain": ledger.verify_chain()}


@app.get("/ledger/verify")
def verify(authorization: str | None = Header(None)):
    _auth(authorization)
    return ledger.verify_chain()


@app.get("/spend/pending")
def spend_pending(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"pending": spending.pending()}


class Decision(BaseModel):
    approve: bool


@app.post("/spend/{req_id}/decide")
def spend_decide(req_id: int, d: Decision, authorization: str | None = Header(None)):
    _auth(authorization)
    return spending.decide(req_id, d.approve)


@app.post("/spend/{req_id}/execute")
def spend_execute(req_id: int, authorization: str | None = Header(None)):
    _auth(authorization)
    return spending.execute_spend(req_id)


class MemoryIn(BaseModel):
    kind: str = "fact"
    content: str
    importance: int = 3


@app.post("/memory")
def add_memory(m: MemoryIn, authorization: str | None = Header(None)):
    _auth(authorization)
    return {"id": memory.write(m.kind, m.content, importance=m.importance)}


@app.get("/memory")
def get_memory(q: str = "", authorization: str | None = Header(None)):
    _auth(authorization)
    return {"memories": memory.retrieve(q)}


# ---------- Commerce ----------

@app.post("/listings/{listing_id}/approve")
def listing_approve(listing_id: int, authorization: str | None = Header(None)):
    _auth(authorization)
    return commerce.approve_listing(listing_id)


@app.post("/listings/{listing_id}/publish")
def listing_publish(listing_id: int, authorization: str | None = Header(None)):
    _auth(authorization)
    return commerce.publish_listing(listing_id)


@app.get("/market")
def market(authorization: str | None = Header(None)):
    _auth(authorization)
    return intelligence.analyze_market()


@app.get("/learning")
def learning_report(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"beliefs_v2_listing": learning2.report(), "beliefs_v1_arms": learning.report()}


@app.get("/economics")
def econ(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"today": economics.pnl(1), "week": economics.pnl(7),
            "all_time": economics.pnl(None),
            "governor": {"mode": economics._kv_get("last_mode", "NORMAL"),
                         "daily_budget_usd": economics.DAILY_COST_BUDGET_USD}}


@app.get("/revenue")
def revenue(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"total_revenue_usd": payments.total_revenue(),
            "total_spend_usd": ledger.total_spend(),
            "net_usd": payments.total_revenue() - ledger.total_spend()}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """No bearer auth here — Stripe calls this. Security = signature verification."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not payments.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET not configured.")
    if not payments.verify_webhook_signature(payload, sig, payments.STRIPE_WEBHOOK_SECRET):
        ledger.record("system", "webhook.rejected", {"reason": "bad signature"})
        raise HTTPException(400, "Invalid signature")
    event = json.loads(payload)
    if event.get("type") != "checkout.session.completed":
        return {"received": True, "ignored": event.get("type")}
    session = event["data"]["object"]
    listing_id = int(session.get("metadata", {}).get("listing_id", 0) or 0)
    amount_usd = float(session.get("amount_total", 0)) / 100.0
    recipient = ""
    for f in session.get("custom_fields", []):
        if f.get("key") == "nft_recipient":
            recipient = str(f.get("text", {}).get("value", "")).strip()
    if not recipient:
        email = session.get("customer_details", {}).get("email", "")
        recipient = f"email:{email}:polygon" if email else ""
    if not listing_id or not recipient:
        ledger.record("system", "webhook.unfulfillable",
                      {"listing_id": listing_id, "has_recipient": bool(recipient)})
        return {"received": True, "fulfilled": False}
    return commerce.fulfill_order(listing_id, str(session.get("id", "")), amount_usd, recipient)

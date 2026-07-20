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
from agent import config, ledger, memory, spending, planner, commerce, learning, learning2, learning3, economics, collector, intelligence, opportunity, outreach
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
    learning3.init()
    economics.init()
    collector.init()
    intelligence.init()
    opportunity.init()
    outreach.init()
    ledger.record("system", "boot", {"agent": config.AGENT_NAME})
    task = asyncio.create_task(_scheduler())
    ctask = asyncio.create_task(_collector_loop())
    yield
    task.cancel()
    ctask.cancel()


app = FastAPI(title=f"{config.AGENT_NAME} agent", lifespan=lifespan)


@app.get("/")
def root():
    """Public landing route so the deployed URL doesn't 404 in a browser."""
    return {"service": f"{config.AGENT_NAME} agent", "status": "running",
            "health": "/health", "docs": "/docs",
            "note": "All control endpoints require Authorization: Bearer <OWNER_TOKEN>."}


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


class SignalIn(BaseModel):
    source: str
    subject: str
    text: str = ""
    tags: str = ""
    source_trust: str = "compliant"
    competitors: int = 0
    est_value_usd: float = 0.0
    ts: str | None = None


@app.post("/opportunities")
def add_opportunity(s: SignalIn, authorization: str | None = Header(None)):
    _auth(authorization)
    return opportunity.ingest(s.model_dump(exclude_none=True))


@app.get("/opportunities")
def list_opportunities(limit: int = 10, min_score: float = 0.0,
                       authorization: str | None = Header(None)):
    _auth(authorization)
    return {"opportunities": opportunity.rank(limit, min_score)}


# ---------- Compliant outreach (draft → owner approve → send) ----------

class OutreachDraftIn(BaseModel):
    recipient: str
    subject: str
    body: str
    opportunity_id: int = 0


@app.get("/outreach")
def outreach_queue(limit: int = 20, authorization: str | None = Header(None)):
    _auth(authorization)
    return {"outreach": outreach.queue(limit)}


@app.post("/outreach/draft")
def outreach_draft(d: OutreachDraftIn, authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        return outreach.draft(d.recipient, d.subject, d.body, d.opportunity_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/outreach/{oid}/decide")
def outreach_decide(oid: int, d: Decision, authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        return outreach.decide(oid, d.approve)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/outreach/{oid}/send")
def outreach_send(oid: int, authorization: str | None = Header(None)):
    """Owner-triggered send. Still passes evaluate_send() guardrails."""
    _auth(authorization)
    try:
        return outreach.send(oid)
    except ValueError as e:
        raise HTTPException(400, str(e))


class SuppressIn(BaseModel):
    email: str
    reason: str = "unsubscribe"


@app.post("/outreach/suppress")
def outreach_suppress(s: SuppressIn, authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        return outreach.suppress(s.email, s.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/learning")
def learning_report(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"active_sales_learner": learning3.SALES_LEARNER,
            "beliefs_active_listing": learning3.active_report(),
            "beliefs_v1_arms": learning.report()}


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


class RegisterIn(BaseModel):
    name: str
    description: str


@app.post("/moltbook/register")
def moltbook_register(r: RegisterIn, authorization: str | None = Header(None)):
    """One-time Moltbook registration. Returns api_key + claim_url; a HUMAN
    must then complete the claim_url (X/Twitter verification) before the key
    works. Save the returned api_key into MOLTBOOK_API_KEY and redeploy.
    NOTE: this calls the live Moltbook API and is not exercised by the offline
    test suite — verify the response shape against moltbook.com/skill.md."""
    _auth(authorization)
    from agent.tools import moltbook
    try:
        return moltbook.register(r.name, r.description)
    except moltbook.MoltbookError as e:
        raise HTTPException(502, f"Moltbook registration failed: {e}")


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

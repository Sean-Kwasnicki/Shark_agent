"""
Payment rail: Stripe Payment Links. The agent sells in dollars, not crypto —
buyers (human or agent-operated) pay a normal Stripe link; on webhook
confirmation we mint the NFT to the recipient they entered at checkout.

Endpoints used (Stripe API v1, form-encoded, Bearer secret key):
  POST /v1/products        {name}
  POST /v1/prices          {product, unit_amount (cents), currency}
  POST /v1/payment_links   {line_items[0][price], line_items[0][quantity],
                            metadata[listing_id],
                            custom_fields for buyer's mint recipient}

Webhook verification implements Stripe's documented scheme without the SDK:
  Stripe-Signature: t=<ts>,v1=<hmac>,...
  v1 = HMAC_SHA256(webhook_secret, f"{t}.{raw_body}")
Constant-time compare + 5-minute tolerance window. This function is
pure/offline and covered by test_core.py.
"""
import os
import hmac
import time
import hashlib
from datetime import datetime, timezone
from agent import ledger
from agent.http import request_with_retry
from agent.db import db

STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BASE = "https://api.stripe.com/v1"

ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    image_url TEXT NOT NULL,
    price_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | approved | live | closed
    payment_link TEXT DEFAULT '',
    moltbook_post_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    listing_id INTEGER NOT NULL,
    stripe_ref TEXT NOT NULL UNIQUE,        -- checkout session id (idempotency)
    amount_usd REAL NOT NULL,
    recipient TEXT NOT NULL,                -- email:.. or chain:address
    status TEXT NOT NULL DEFAULT 'paid',    -- paid | minted | failed
    nft_id TEXT DEFAULT ''
);
"""


def init():
    with db() as conn:
        conn.executescript(ORDERS_SCHEMA)


class PaymentError(Exception):
    pass


def _stripe(path: str, data: dict) -> dict:
    if not STRIPE_KEY:
        raise PaymentError("STRIPE_SECRET_KEY not set.")
    r = request_with_retry("POST", f"{STRIPE_BASE}{path}", data=data,
                           headers={"Authorization": f"Bearer {STRIPE_KEY}"})
    if r.status_code >= 400:
        raise PaymentError(f"stripe {path} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def create_payment_link(listing_id: int, title: str, price_usd: float) -> str:
    product = _stripe("/products", {"name": title})
    price = _stripe("/prices", {"product": product["id"], "currency": "usd",
                                "unit_amount": int(round(price_usd * 100))})
    link = _stripe("/payment_links", {
        "line_items[0][price]": price["id"],
        "line_items[0][quantity]": 1,
        "metadata[listing_id]": str(listing_id),
        "custom_fields[0][key]": "nft_recipient",
        "custom_fields[0][label][type]": "custom",
        "custom_fields[0][label][custom]": "NFT delivery (email or wallet address)",
        "custom_fields[0][type]": "text",
    })
    ledger.record("agent", "payments.link_created",
                  {"listing_id": listing_id, "price_usd": price_usd, "url": link["url"]})
    return link["url"]


def verify_webhook_signature(payload: bytes, sig_header: str, secret: str,
                             tolerance_s: int = 300, now: int | None = None) -> bool:
    """Pure function implementing Stripe's t/v1 HMAC scheme. Offline-testable."""
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        t = int(parts["t"])
        v1 = parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else int(time.time())) - t) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def record_paid_order(listing_id: int, stripe_ref: str, amount_usd: float,
                      recipient: str) -> int | None:
    """Idempotent on stripe_ref. Returns order id, or None if already recorded."""
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        dup = conn.execute("SELECT id FROM orders WHERE stripe_ref=?", (stripe_ref,)).fetchone()
        if dup:
            return None
        cur = conn.execute(
            "INSERT INTO orders (ts, listing_id, stripe_ref, amount_usd, recipient) "
            "VALUES (?,?,?,?,?)", (ts, listing_id, stripe_ref, amount_usd, recipient))
        order_id = cur.lastrowid
    ledger.record("system", "payments.received",
                  {"order_id": order_id, "listing_id": listing_id,
                   "recipient": recipient, "amount_usd": amount_usd},
                  cost_usd=amount_usd)  # revenue; separated from spend by action name
    return order_id


def total_revenue() -> float:
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM ledger WHERE action='payments.received'"
        ).fetchone()
    return float(row["s"])

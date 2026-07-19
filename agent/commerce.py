"""
Commerce orchestration — the end-to-end money loop:

  1. propose_listing()  agent (worker model) drafts an NFT concept -> 'draft'
  2. owner approves via API (or AUTO_PUBLISH=true skips this)
  3. publish_listing()  creates Stripe payment link + posts to Moltbook -> 'live'
  4. Stripe webhook fires on purchase -> fulfill_order() mints via Crossmint
     directly to the buyer's recipient -> order 'minted'

Every step is ledgered. Fulfillment is idempotent (unique stripe_ref +
mint idempotency key), so webhook replays cannot double-mint.
"""
import os
import json
from datetime import datetime, timezone, timedelta
from agent.db import db
from agent import ledger, llm, learning2, intelligence
from agent.tools import payments, moltbook, nft, notify

AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "false").lower() == "true"
LISTING_SUBMOLT = os.getenv("LISTING_SUBMOLT", "general")
MAX_LIVE_LISTINGS = int(os.getenv("MAX_LIVE_LISTINGS", "5"))
DEFAULT_IMAGE_URL = os.getenv("LISTING_IMAGE_URL", "")  # host art you actually own rights to
LISTING_TTL_DAYS = int(os.getenv("LISTING_TTL_DAYS", "7"))

# Post style hints for the copywriting worker (actions live in learning2).
STYLE_HINTS = {
    "story": "Frame the post as a short narrative about why this piece exists.",
    "utility": "Frame the post around what the buyer concretely gets and can do with it.",
    "transparent_agent": "Lead with the fact an autonomous AI agent made and listed this, plainly.",
}

WORKER_SYSTEM = """You draft one NFT listing concept as STRICT JSON, no prose:
{"title": str, "description": str (2-3 sentences, honest, no hype about
future value or investment returns), 
"attributes": [{"trait_type": str, "value": str}],
"moltbook_post": str (the sales post: what it is, why it exists; transparent
that an AI agent made it; NO financial-return claims; do NOT state a price —
it is appended automatically)}
Style directive for the post: {style_hint}"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def propose_listing(theme: str = "") -> dict:
    """Contextual bandit (LinTS v2) picks the price x style action — learned
    jointly across all combinations from sales outcomes. The worker model
    only writes copy for the chosen style. Stored as 'draft' (or auto-approved)."""
    topic = ""
    if not theme:
        picked = intelligence.next_theme()
        if picked:
            theme, topic = picked["theme"], picked["topic"]
        else:
            theme = "an original abstract digital artwork exploring emergence"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO listings (ts, title, description, image_url, price_usd, status) "
            "VALUES (?,?,?,?,?,?)",
            (_now(), "(drafting)", "", DEFAULT_IMAGE_URL, 0.0,
             "approved" if AUTO_PUBLISH else "draft"),
        )
        listing_id = cur.lastrowid
    if topic:
        intelligence.bind_topic_to_listing(listing_id)
    action = learning2.listing_model().choose(
        learning2.listing_candidates(), learning2.featurize_listing, context=str(listing_id))
    price, style = float(action["price"]), str(action["style"])
    raw = llm.work(WORKER_SYSTEM.replace("{style_hint}", STYLE_HINTS[style]),
                   f"Theme/context: {theme}")
    d = llm.extract_json(raw)
    with db() as conn:
        conn.execute("UPDATE listings SET title=?, description=?, price_usd=? WHERE id=?",
                     (str(d["title"])[:120], str(d["description"])[:1000], price, listing_id))
        conn.execute("INSERT INTO tasks (ts, title, notes) VALUES (?,?,?)",
                     (_now(), f"Publish listing #{listing_id}",
                      json.dumps({"moltbook_post": d.get("moltbook_post", "")})))
    ledger.record("agent", "commerce.propose",
                  {"listing_id": listing_id, "title": d["title"], "price_usd": price,
                   "style": style})
    if not AUTO_PUBLISH:
        notify.owner(f"Listing draft #{listing_id}: '{d['title']}' at ${price:.2f} "
                     f"(learned action: ${price:.0f} x {style}). "
                     f"Approve via POST /listings/{listing_id}/approve")
    return {"listing_id": listing_id, "status": "approved" if AUTO_PUBLISH else "draft",
            "price_usd": price, "style": style}


def expire_stale_listings() -> int:
    """Close listings live past TTL with no sale; record FAILURE outcomes so the
    bandit learns what doesn't sell. Called once per cycle. Returns count closed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LISTING_TTL_DAYS)).isoformat()
    with db() as conn:
        stale = [dict(r) for r in conn.execute(
            "SELECT l.id FROM listings l WHERE l.status='live' AND l.ts < ? AND NOT EXISTS "
            "(SELECT 1 FROM orders o WHERE o.listing_id=l.id)", (cutoff,))]
    for row in stale:
        lid = str(row["id"])
        learning2.listing_model().resolve(lid, success=False)
        intelligence.credit_topic(row["id"], success=False)
        with db() as conn:
            conn.execute("UPDATE listings SET status='closed' WHERE id=?", (row["id"],))
        ledger.record("system", "commerce.expire", {"listing_id": row["id"]})
    return len(stale)


def approve_listing(listing_id: int) -> dict:
    with db() as conn:
        conn.execute("UPDATE listings SET status='approved' WHERE id=? AND status='draft'",
                     (listing_id,))
    ledger.record("owner", "commerce.approve", {"listing_id": listing_id})
    return {"listing_id": listing_id, "status": "approved"}


def publish_listing(listing_id: int, moltbook_post_text: str = "") -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        live = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE status='live'").fetchone()["n"]
    if not row or row["status"] != "approved":
        raise ValueError(f"Listing #{listing_id} is not approved.")
    if live >= MAX_LIVE_LISTINGS:
        raise ValueError(f"Max live listings ({MAX_LIVE_LISTINGS}) reached.")
    link = payments.create_payment_link(listing_id, row["title"], row["price_usd"])
    body = moltbook_post_text or (
        f"{row['description']}\n\nPrice: ${row['price_usd']:.2f} — {link}\n"
        f"(Listed autonomously by an AI agent; owner-audited hash-chained ledger.)"
    )
    post_id = ""
    try:
        result = moltbook.post(LISTING_SUBMOLT, f"[For sale] {row['title']}", body)
        post_id = str(result.get("id", result.get("post", {}).get("id", "")))
    except moltbook.MoltbookError as e:
        ledger.record("system", "commerce.post_deferred", {"listing_id": listing_id, "err": str(e)})
    with db() as conn:
        conn.execute("UPDATE listings SET status='live', payment_link=?, moltbook_post_id=? "
                     "WHERE id=?", (link, post_id, listing_id))
    if post_id:
        from agent import collector
        collector.record_engagement_decision(listing_id)
    ledger.record("agent", "commerce.publish",
                  {"listing_id": listing_id, "link": link, "moltbook_post_id": post_id})
    return {"listing_id": listing_id, "status": "live", "payment_link": link}


def fulfill_order(listing_id: int, stripe_ref: str, amount_usd: float, recipient: str) -> dict:
    """Called by the Stripe webhook handler AFTER signature verification."""
    order_id = payments.record_paid_order(listing_id, stripe_ref, amount_usd, recipient)
    if order_id is None:
        return {"status": "duplicate_ignored", "stripe_ref": stripe_ref}
    with db() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if not row:
        _mark_order(order_id, "failed", "")
        return {"status": "failed", "reason": f"listing {listing_id} not found"}
    try:
        minted = nft.mint_to(recipient, row["title"], row["description"], row["image_url"])
        nft_id = str(minted.get("id", minted.get("actionId", "")))
        _mark_order(order_id, "minted", nft_id)
        learning2.listing_model().resolve(str(listing_id), success=True)
        intelligence.credit_topic(listing_id, success=True)
        notify.owner(f"SALE: '{row['title']}' ${amount_usd:.2f} — minted {nft_id} to {recipient}. "
                     f"Total revenue: ${payments.total_revenue():.2f}")
        return {"status": "minted", "order_id": order_id, "nft_id": nft_id}
    except nft.NFTError as e:
        _mark_order(order_id, "failed", "")
        ledger.record("system", "commerce.fulfill_error", {"order_id": order_id, "err": str(e)})
        notify.owner(f"FULFILLMENT FAILED for order {order_id}: {e}. Manual action needed.")
        return {"status": "failed", "order_id": order_id, "error": str(e)}


def _mark_order(order_id: int, status: str, nft_id: str):
    with db() as conn:
        conn.execute("UPDATE orders SET status=?, nft_id=? WHERE id=?",
                     (status, nft_id, order_id))


def recover_stuck_orders() -> int:
    """Crash-consistency: an order can be stuck 'paid' (process died between
    payment recording and mint) or 'failed' (Crossmint hiccup). Retry both —
    safe because mint_to is idempotent on (recipient, name). Runs every cycle."""
    with db() as conn:
        stuck = [dict(r) for r in conn.execute(
            "SELECT o.id, o.listing_id, o.recipient, l.title, l.description, l.image_url "
            "FROM orders o JOIN listings l ON l.id = o.listing_id "
            "WHERE o.status IN ('paid','failed')")]
    n = 0
    for o in stuck:
        try:
            minted = nft.mint_to(o["recipient"], o["title"], o["description"], o["image_url"])
            nft_id = str(minted.get("id", minted.get("actionId", "")))
            _mark_order(o["id"], "minted", nft_id)
            ledger.record("system", "commerce.recovered", {"order_id": o["id"], "nft_id": nft_id})
            n += 1
        except Exception as e:
            ledger.record("system", "commerce.recover_error", {"order_id": o["id"], "err": str(e)})
    return n

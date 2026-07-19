"""
Go-live smoke harness — the ONLY test in this repo that touches real networks.

Purpose: prove the revenue pipeline's external integrations work against
Stripe TEST mode and Crossmint STAGING before flipping to production. Run it
manually once keys exist; it is deliberately NOT part of run_tests.sh, which
must stay offline and deterministic.

    STRIPE_SECRET_KEY=sk_test_...  CROSSMINT_API_KEY=...  python test_live_smoke.py

Behavior per credential:
  - missing            -> that check is SKIPPED (clearly reported), exit 0
  - present            -> the real API is called and the result asserted
Safety rails (non-negotiable):
  - refuses to run with a LIVE Stripe key (sk_live_) — test mode only
  - refuses to run with CROSSMINT_ENV=www — staging only
  - uses a throwaway temp DB so it never pollutes the production ledger
"""
import os
import sys
import json
import time
import hmac
import hashlib
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

from agent.db import init_db
from agent.http import init as init_http
from agent.tools import payments, nft

init_db()
init_http()
payments.init()

STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CROSSMINT_KEY = os.getenv("CROSSMINT_API_KEY", "")
MOLTBOOK_KEY = os.getenv("MOLTBOOK_API_KEY", "")

ran, skipped, failures = [], [], []


def check(name, present, fn):
    if not present:
        skipped.append(name)
        print(f"SKIP  {name}: credential not set")
        return
    try:
        detail = fn()
        ran.append(name)
        print(f"PASS  {name}: {detail}")
    except Exception as e:
        failures.append((name, str(e)))
        print(f"FAIL  {name}: {e}")


# ---- safety rails ----
if STRIPE_KEY.startswith("sk_live_"):
    print("ABORT: STRIPE_SECRET_KEY is a LIVE key. This harness only runs "
          "against Stripe TEST mode (sk_test_...). Nothing was executed.")
    sys.exit(2)
if CROSSMINT_KEY and os.getenv("CROSSMINT_ENV", "staging") == "www":
    print("ABORT: CROSSMINT_ENV=www (production). This harness only runs "
          "against staging. Nothing was executed.")
    sys.exit(2)


# ---- 1. Stripe: product -> price -> payment link (test mode) ----
def stripe_link():
    url = payments.create_payment_link(
        listing_id=0, title="SMOKE TEST — do not buy", price_usd=1.00)
    assert url.startswith("https://"), f"unexpected link: {url!r}"
    return f"payment link created: {url}"


check("stripe.payment_link", bool(STRIPE_KEY), stripe_link)


# ---- 2. Stripe webhook secret: sign like Stripe does, verify our verifier ----
def webhook_roundtrip():
    body = json.dumps({"type": "checkout.session.completed", "smoke": True}).encode()
    t = int(time.time())
    v1 = hmac.new(STRIPE_WH.encode(), f"{t}.".encode() + body,
                  hashlib.sha256).hexdigest()
    header = f"t={t},v1={v1}"
    assert payments.verify_webhook_signature(body, header, STRIPE_WH), \
        "valid signature rejected"
    assert not payments.verify_webhook_signature(body + b"x", header, STRIPE_WH), \
        "tampered payload accepted"
    return "signature scheme verified against configured secret"


check("stripe.webhook_secret", bool(STRIPE_WH), webhook_roundtrip)


# ---- 3. Crossmint staging: mint one NFT to a test email recipient ----
def crossmint_mint():
    out = nft.mint_to(recipient="email:smoke-test@example.com:polygon",
                      name=f"Smoke Test {int(time.time())}",
                      description="Staging smoke test — worthless by design.",
                      image_url="https://picsum.photos/seed/smoke/400")
    ref = out.get("id") or out.get("actionId")
    assert ref, f"no id/actionId in response: {out}"
    return f"staging mint accepted, ref={ref}"


check("crossmint.staging_mint", bool(CROSSMINT_KEY), crossmint_mint)


# ---- 4. Moltbook: authenticated feed fetch through the production code path ----
def moltbook_feed():
    from agent.tools import moltbook
    posts = moltbook.get_feed(limit=3)
    assert isinstance(posts, list), f"feed did not parse to a list: {type(posts)}"
    return f"feed reachable via get_feed(), {len(posts)} posts returned"


check("moltbook.feed", bool(MOLTBOOK_KEY), moltbook_feed)


# ---- verdict ----
print("-" * 60)
print(f"ran={len(ran)} skipped={len(skipped)} failed={len(failures)}")
if failures:
    for name, err in failures:
        print(f"  FAILED {name}: {err}")
    sys.exit(1)
if not ran:
    print("SMOKE SKIPPED ENTIRELY — no credentials set. Provide sk_test "
          "Stripe + staging Crossmint keys and rerun before go-live.")
print("LIVE SMOKE COMPLETE")

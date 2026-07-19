"""
NFT minting via Crossmint's Minting API — no private-key management.

Verified against Crossmint docs (July 2026):
  POST https://{env}.crossmint.com/api/2022-06-09/collections/{collectionId}/nfts
  Headers: X-API-KEY (scope nfts.create), Content-Type: application/json
  Body: {"metadata": {"name","image","description","attributes"}, "recipient": ...}
  Recipient formats: "email:buyer@x.com:polygon" or "<chain>:<walletAddress>"
  Response includes id/actionId and onChain.status "pending"; poll the
  action resource or GET the NFT for completion.

Defaults to STAGING (testnet, free). Set CROSSMINT_ENV=www for production
only after end-to-end staging tests pass.

Design choice: MINT-ON-SALE. We never pre-mint inventory; an NFT is minted
directly to the buyer only after their payment is confirmed. Zero upfront
mint cost, no custody, no transfer step.
"""
import os
import hashlib
from agent import ledger
from agent.http import request_with_retry, seen, remember

CROSSMINT_ENV = os.getenv("CROSSMINT_ENV", "staging")          # staging | www
CROSSMINT_API_KEY = os.getenv("CROSSMINT_API_KEY", "")
COLLECTION_ID = os.getenv("CROSSMINT_COLLECTION_ID", "default")


class NFTError(Exception):
    pass


def _base() -> str:
    return f"https://{CROSSMINT_ENV}.crossmint.com/api/2022-06-09"


def mint_to(recipient: str, name: str, description: str, image_url: str,
            attributes: list[dict] | None = None) -> dict:
    """Mint one NFT to recipient. Idempotent on (recipient, name)."""
    if not CROSSMINT_API_KEY:
        raise NFTError("CROSSMINT_API_KEY not set.")
    idem = "mint:" + hashlib.sha256(f"{recipient}|{name}".encode()).hexdigest()[:24]
    cached = seen(idem)
    if cached:
        return {**cached, "deduped": True}
    r = request_with_retry(
        "POST", f"{_base()}/collections/{COLLECTION_ID}/nfts",
        headers={"X-API-KEY": CROSSMINT_API_KEY, "Content-Type": "application/json"},
        json={
            "metadata": {"name": name, "description": description,
                         "image": image_url, "attributes": attributes or []},
            "recipient": recipient,
        },
    )
    if r.status_code >= 400:
        raise NFTError(f"mint failed {r.status_code}: {r.text[:300]}")
    out = r.json()
    remember(idem, out)
    ledger.record("agent", "nft.mint",
                  {"name": name, "recipient": recipient, "env": CROSSMINT_ENV,
                   "id": out.get("id", out.get("actionId", ""))})
    return out


def mint_status(nft_id: str) -> dict:
    r = request_with_retry(
        "GET", f"{_base()}/collections/{COLLECTION_ID}/nfts/{nft_id}",
        headers={"X-API-KEY": CROSSMINT_API_KEY},
    )
    if r.status_code >= 400:
        raise NFTError(f"status failed {r.status_code}: {r.text[:300]}")
    return r.json()

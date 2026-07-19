"""
Owner notifications. Discord webhook first (instant, free), Resend email
as a second channel if RESEND_API_KEY is set. Never raises — a failed
notification must not crash a cycle; it is ledgered instead.
"""
import os
import requests
from agent import config, ledger

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
# Must be an address on a domain you've verified in Resend, or the API rejects
# the send. No safe default exists (a placeholder domain silently fails), so
# email is skipped unless this is set explicitly.
RESEND_FROM = os.getenv("RESEND_FROM", "")


def owner(message: str):
    delivered = []
    if config.OWNER_DISCORD_WEBHOOK:
        try:
            requests.post(config.OWNER_DISCORD_WEBHOOK,
                          json={"content": message[:1900]}, timeout=10)
            delivered.append("discord")
        except Exception as e:
            ledger.record("system", "notify.error", {"channel": "discord", "err": str(e)})
    if RESEND_API_KEY and config.OWNER_EMAIL and RESEND_FROM:
        try:
            requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": RESEND_FROM, "to": [config.OWNER_EMAIL],
                      "subject": f"[{config.AGENT_NAME}] update", "text": message},
                timeout=15,
            )
            delivered.append("email")
        except Exception as e:
            ledger.record("system", "notify.error", {"channel": "email", "err": str(e)})
    elif RESEND_API_KEY and config.OWNER_EMAIL and not RESEND_FROM:
        ledger.record("system", "notify.skipped",
                      {"channel": "email", "reason": "RESEND_FROM not set"})
    ledger.record("agent", "notify.owner", {"channels": delivered, "preview": message[:120]})

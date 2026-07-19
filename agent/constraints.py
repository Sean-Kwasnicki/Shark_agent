"""
Deterministic constraint engine. Runs BEFORE any tool executes.
The LLM proposes; this code disposes. No prompt injection can bypass it
because it never reads model output as instructions — only as structured
proposals to validate.
"""
from datetime import datetime, timezone, timedelta
from agent.config import HARD_RULES
from agent import ledger


class ConstraintViolation(Exception):
    pass


def check_tool_allowed(tool_name: str):
    if tool_name not in HARD_RULES["allowed_tools"]:
        raise ConstraintViolation(f"Tool '{tool_name}' is not in the allowlist.")


def check_url_allowed(url: str):
    low = url.lower()
    for bad in HARD_RULES["forbidden_domains"]:
        if bad in low:
            raise ConstraintViolation(f"URL blocked by forbidden_domains rule: {url}")


def check_spend(amount_usd: float, auto: bool):
    """Validate a spend against per-transaction, daily, and lifetime caps."""
    if amount_usd <= 0:
        raise ConstraintViolation("Spend amount must be positive.")
    if auto and amount_usd > HARD_RULES["max_single_spend_auto_usd"]:
        raise ConstraintViolation(
            f"Auto-spend ${amount_usd:.2f} exceeds auto-approve limit "
            f"${HARD_RULES['max_single_spend_auto_usd']:.2f}; requires owner approval."
        )
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    daily = ledger.total_spend(since_iso=day_ago)
    if daily + amount_usd > HARD_RULES["max_daily_spend_usd"]:
        raise ConstraintViolation(
            f"Daily cap: spent ${daily:.2f} in last 24h; "
            f"${amount_usd:.2f} more would exceed ${HARD_RULES['max_daily_spend_usd']:.2f}."
        )
    lifetime = ledger.total_spend()
    if lifetime + amount_usd > HARD_RULES["max_total_spend_usd"]:
        raise ConstraintViolation(
            f"Lifetime cap: spent ${lifetime:.2f} total; "
            f"${amount_usd:.2f} more would exceed ${HARD_RULES['max_total_spend_usd']:.2f}."
        )


def check_cycle_budget(actions_taken: int, llm_calls: int):
    if actions_taken >= HARD_RULES["max_actions_per_cycle"]:
        raise ConstraintViolation("Max actions per cycle reached.")
    if llm_calls >= HARD_RULES["max_llm_calls_per_cycle"]:
        raise ConstraintViolation("Max LLM calls per cycle reached.")

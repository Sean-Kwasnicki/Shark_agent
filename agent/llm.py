"""
Thin Claude API wrapper. Tiered: planner model for the cycle plan,
worker model for cheap task execution. All calls are ledgered.
Requires: pip install anthropic
"""
import json
import re
from agent.config import ANTHROPIC_API_KEY, MODEL_PLANNER, MODEL_WORKER
from agent import ledger

try:
    import anthropic
    _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:  # allows deterministic core to run/test without the SDK
    _client = None


class LLMUnavailable(Exception):
    pass


def _call(model: str, system: str, user: str, max_tokens: int = 2000) -> str:
    if _client is None:
        raise LLMUnavailable("Anthropic SDK not installed or ANTHROPIC_API_KEY not set.")
    resp = _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    usage = getattr(resp, "usage", None)
    cost = 0.0
    if usage is not None:
        from agent import economics
        cost = economics.record_llm_cost(model, int(getattr(usage, "input_tokens", 0)),
                                         int(getattr(usage, "output_tokens", 0)))
    ledger.record("agent", "llm.call",
                  {"model": model, "chars_out": len(text), "cost_usd": round(cost, 6)})
    return text


def plan(system: str, user: str, tier: str = "planner") -> str:
    """tier='planner' uses MODEL_PLANNER; tier='worker' downgrades to the
    cheap model — the economic governor sets this in CONSERVE/HIBERNATE."""
    return _call(MODEL_WORKER if tier == "worker" else MODEL_PLANNER, system, user)


def work(system: str, user: str) -> str:
    return _call(MODEL_WORKER, system, user)


def extract_json(text: str):
    """Parse JSON from model output, tolerating code fences. Raises ValueError on failure."""
    cleaned = re.sub(r"```(json)?", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output.")
    return json.loads(cleaned[start:end + 1])

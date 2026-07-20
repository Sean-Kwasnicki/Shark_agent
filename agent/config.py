"""
Central configuration. Everything the agent is ALLOWED to do lives here,
in code, versioned — not in a prompt. Prompts can be jailbroken; this can't.
"""
import os

# --- Identity ---
AGENT_NAME = os.getenv("AGENT_NAME", "Loop")
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "")
OWNER_DISCORD_WEBHOOK = os.getenv("OWNER_DISCORD_WEBHOOK", "")

# --- Mission (the ONLY free-text instruction the planner receives) ---
MISSION = os.getenv(
    "AGENT_MISSION",
    "Research and identify revenue opportunities for the owner's software "
    "business. Draft outreach and product experiments for human approval. "
    "Never spend money without explicit approval unless within auto-approve limits.",
)

# --- Hard constraints (deterministic; enforced in constraints.py, not by the LLM) ---
HARD_RULES = {
    # Money
    "max_single_spend_auto_usd": float(os.getenv("MAX_SINGLE_SPEND_AUTO_USD", "0")),   # 0 = every spend needs approval
    "max_daily_spend_usd": float(os.getenv("MAX_DAILY_SPEND_USD", "25")),
    "max_total_spend_usd": float(os.getenv("MAX_TOTAL_SPEND_USD", "100")),
    # Actions
    "allowed_tools": [
        "research.web_search",
        "research.fetch_page",
        "notify.owner",
        "memory.write",
        "purchase.request",       # creates an approval request; does NOT spend
        "purchase.execute",       # only runs against APPROVED requests
        "moltbook.post",
        "moltbook.comment",
        "moltbook.feed",
        "commerce.propose_listing",   # draft only; publishing needs approval unless AUTO_PUBLISH
        "commerce.publish_listing",   # only succeeds on owner-approved listings
        "intel.analyze",              # deterministic, free
        "intel.hypothesize",          # worker LLM, governed by cycle budget
        "opportunity.rank",           # deterministic revenue-opportunity ranking; free
        "outreach.draft",             # draft ONLY; sending requires explicit owner approval + guardrails
    ],
    "forbidden_domains": ["bank", "coinbase", "binance", "robinhood"],  # substring match on URLs
    # Cadence
    "max_llm_calls_per_cycle": int(os.getenv("MAX_LLM_CALLS_PER_CYCLE", "6")),
    "max_actions_per_cycle": int(os.getenv("MAX_ACTIONS_PER_CYCLE", "10")),
    "cycle_interval_minutes": int(os.getenv("CYCLE_INTERVAL_MINUTES", "60")),
}

# --- Model tiers (set via env so you can upgrade without a deploy) ---
MODEL_PLANNER = os.getenv("MODEL_PLANNER", "claude-sonnet-4-6")   # plans each cycle
MODEL_WORKER = os.getenv("MODEL_WORKER", "claude-haiku-4-5-20251001")  # cheap task execution
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Storage ---
DB_PATH = os.getenv("DB_PATH", "agent.db")

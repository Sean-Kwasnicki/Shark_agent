# Loop v1.7 — a Bartók-class autonomous agent (with receipts)

An autonomous agent framework with the capability profile attributed to
"Bartok" (persistent memory, autonomous planning loop, tools, economic
actions) — plus the thing the Bartok story famously lacks: **a
hash-chained audit ledger that proves what actually happened.**

Architecture matches your standing preference: **deterministic control
plane, LLM only for planning/reasoning.** The model proposes actions as
strict JSON; Python validates and executes them. No prompt injection can
bypass spending caps or the tool allowlist because they're enforced in
code that never treats model output as instructions.

## What's in the box

| Layer | File | Status |
|---|---|---|
| Hash-chained audit ledger | `agent/ledger.py` | ✅ tested (`test_core.py`) |
| Constraint engine (caps, allowlists) | `agent/constraints.py` | ✅ tested |
| Spend lifecycle (request→approve→execute) | `agent/spending.py` | ✅ tested (payments **simulated**) |
| Persistent memory + retrieval | `agent/memory.py` | ✅ tested |
| Planning loop (observe→plan→validate→act→reflect) | `agent/planner.py` | syntax-checked; needs API key to run |
| Claude API wrapper (tiered models) | `agent/llm.py` | syntax-checked |
| Research / notify tools | `agent/tools/` | syntax-checked; need BRAVE/RESEND keys |
| FastAPI control surface + scheduler | `main.py` | syntax-checked |
| Retry/backoff + idempotency store | `agent/http.py` | ✅ tested |
| Moltbook connector (post/comment/feed, rate-limited) | `agent/tools/moltbook.py` | rate limiter ✅ tested; network calls untested here |
| Stripe payment links + webhook HMAC verification | `agent/tools/payments.py` | signature verify + order idempotency ✅ tested; API calls untested here |
| Crossmint NFT mint-on-sale | `agent/tools/nft.py` | syntax-checked; test on Crossmint STAGING first |
| Listing lifecycle (draft→approve→publish→fulfill) | `agent/commerce.py` | syntax-checked; approval gates enforced |
| Learning engine v1 (independent Beta-TS arms) | `agent/learning.py` | ✅ tested (`test_learning.py`) |
| Learning engine v2 (contextual Linear Thompson Sampling) | `agent/learning2.py` | ✅ tested + benchmarked (`test_learning2.py`) |
| Economics: cost accounting, P&L, governor, idle-skip, daily digest | `agent/economics.py` | ✅ tested (`test_economics.py`, all 5 governor modes) |
| 24/7 data collector: engagement signal, watchdog, circuit breaker | `agent/collector.py` | ✅ tested (`test_collector.py`, injected fetchers) |
| Market intelligence: observe agents, gap scoring, hypotheses, topic bandit | `agent/intelligence.py` | ✅ tested (`test_intelligence.py`, synthetic market) |
| Hardening: chain concurrency lock, stuck-order recovery, timing-safe auth | `agent/ledger.py`, `agent/commerce.py`, `main.py` | ✅ tested (`test_hardening.py`, defect demonstrated pre-fix) |
| Ops: railway.json, run_tests.sh (7 suites), GitHub Actions CI | repo root | ✅ config present; CI runs on push |

**Honest test status:** the deterministic core was executed and passed 7
assertions (chain integrity, retrieval ranking, full spend lifecycle,
daily/lifetime cap enforcement, unapproved-execute rejection, tool
allowlist, forbidden domains). The FastAPI/LLM/network layers compile but
were not runtime-tested in the build environment (no network access) —
smoke-test them on first deploy with `POST /cycle`.

## Deploy (Railway, your usual flow)

1. Push this repo to GitHub, connect to Railway.
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Set env vars from `.env.example`. **OWNER_TOKEN is mandatory** — every
   route requires `Authorization: Bearer <token>`.
4. Add a Railway volume mounted where `DB_PATH` points, or SQLite resets
   on redeploy.
5. Trigger a manual cycle: `curl -X POST -H "Authorization: Bearer $TOKEN" https://<app>/cycle`
6. Check the receipts: `GET /ledger` (includes live chain verification).

## Safety model (read before wiring real money)

- Ships with **simulated payments**. `execute_spend()` records the
  transaction but moves nothing.
- When you go live, use a **Stripe Issuing virtual card or Privacy.com
  card with its own hard limit set BELOW the config caps** — two
  independent walls. Never connect a real bank account.
- Defaults: $0 auto-approve (every spend needs your click), $25/day,
  $100 lifetime. Raise deliberately.
- Approval flow: agent calls `purchase.request` → you get a Discord
  ping → `POST /spend/{id}/decide` → `POST /spend/{id}/execute`.
  Caps re-checked at execution time.

## The money loop (v1.1)

1. Agent drafts an NFT listing (worker model) → `draft`, Discord ping
2. You approve: `POST /listings/{id}/approve` (or set `AUTO_PUBLISH=true`)
3. Publish: Stripe payment link created, sales post goes to Moltbook
4. Buyer pays the link → Stripe webhook (signature-verified) → NFT minted
   via Crossmint directly to the buyer's email or wallet → you get a
   "SALE" ping with running revenue total
5. `GET /revenue` shows revenue vs. spend vs. net, all backed by the ledger

Go-live order: Stripe TEST keys + Crossmint STAGING end-to-end first,
then production keys. One manual step is unavoidable: Moltbook
registration returns a claim URL your human account must verify via an
X post before the API key works.

## How the agent learns (v2 — contextual LinTS)

The live learner is contextual Linear Thompson Sampling (agent/learning2.py):
a Bayesian posterior over FEATURE WEIGHTS (price, price², style, price×style)
rather than independent arms, so every sale or expiry updates beliefs about
all 12 price×style actions at once. It samples weights from the posterior and
picks the action maximizing sampled conversion × price (expected revenue).
Sales credit success; listings expiring unsold (7-day TTL) credit failure;
per-cycle discounting keeps it adaptive. Deterministic numpy math, fully
persisted in SQLite, every choice/outcome hash-chain ledgered.
`GET /learning` shows the ranked EV table for all actions.

Benchmark vs the v1 algorithm (structured hidden ground truth, 5 seeds):
- Stationary, 1500 rounds: LinTS +14.4% revenue over Beta-TS
  (optimal-action share in final 200 rounds: 93% vs 74%; random floor far below both)
- Sparse regime, 150 rounds (early-Moltbook reality): LinTS +18.8%
- Nonstationary (market flips mid-run, γ=0.99): recovers to 70% new-optimal picks
- Plus persistence-across-restart and exact credit-assignment tests

Stated tradeoffs: linear-probability model for a binary outcome (standard
bandit practice; fine in the 0–50% conversion range, less exact at extremes),
and exploration scale v=0.3 is a tunable — test_learning2.py is the harness
for tuning it. Real-world learning speed still depends on real sales volume.

## Self-sustaining economics (v1.4)

The agent accounts for its own costs and throttles itself to stay profitable:

- Every LLM call's REAL token usage is priced (verified July 2026 rates:
  Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok — override PRICE_TABLE_JSON
  when prices change; note Sonnet 5 intro pricing $2/$10 through Aug 31 2026
  is a cheaper planner option) and hash-chain ledgered. Infra cost accrues
  daily. `GET /economics` = live P&L (today / 7-day / all-time).
- Governor (pure arithmetic, runs before any LLM call):
  HARD_STOP when today's operating burn hits DAILY_COST_BUDGET_USD ($1 default);
  HIBERNATE at 7-day net <= -$10 (one cheap cycle/day + owner alert);
  CONSERVE when 7-day net < 0 (cheap model, every other cycle);
  NORMAL / GROW when net-positive. Purchases are governed separately by
  the spending caps, so an approved purchase can't mute the agent.
- Idle-skip: if actionable state hasn't changed, the cycle costs $0 —
  no LLM call — with a forced full cycle every 6 skips.
- Daily digest to Discord/email: P&L, mode, top learned actions —
  the "money while you sleep" report you wake up to.

Cost model (assumptions labeled): a full planner cycle at ~3k input /
600 output tokens on Sonnet 4.6 ≈ $0.018. Hourly cycles with no idle
periods = ~$0.43/day ≈ $13/mo + ~$5 infra ≈ $18/mo worst case; idle-skip
and CONSERVE mode push realistic burn well below that, and the $1/day
hard budget caps it at ~$35/mo absolute ceiling. Break-even is therefore
roughly 2–4 NFT sales per month at the $8–$25 bands — an assumption to
be validated by real sales, not a forecast.

## Observational learning & product discovery (v1.6)

The agent now watches the Moltbook market 24/7 and decides WHAT to make:

1. OBSERVE (free, every 15 min in the collector loop): snapshots other
   agents' posts — author, submolt, title, real engagement — sanitized as
   adversarial input, never executed.
2. EXTRACT (pure math): engagement per keyword; top patterns = what works,
   bottom decile = the avoid-list (learning from others' failures).
   Sell-marker detection approximates SUPPLY per keyword.
3. FIND GAPS: opportunity score = demand x scarcity — high engagement with
   zero sellers gets a gap bonus. This is the data-driven version of the
   "first NFT by an agent" insight: find where attention exists but nobody
   is selling yet, instead of assuming it.
4. HYPOTHESIZE (worker LLM, budget-governed): top gaps + avoid-list ->
   3 concrete product briefs stored in a hypothesis bank.
5. LEARN WHAT SELLS: a topic bandit picks which hypothesis to build next;
   real sales credit the topic, expiries debit it — layered on top of the
   price/style LinTS. Own successes AND failures feed back automatically.

Two engagement/sales signals stay strictly separate: the collector resolves
real upvote/comment deltas into an engagement model, while the sales model
is only ever updated by signature-verified Stripe webhooks. Tests assert
the sales model is untouched by engagement data.

HONEST LIMITS: other agents' engagement is observable; their revenue is
not — "success pattern" means proven attention, not proven sales. Feed
data can be gamed by other agents, which is one more reason sell-decisions
keep a human approval gate by default. Live Moltbook response shapes
remain unverified until first deploy (validation defends against several
shapes; confirm against moltbook.com/skill.md).

## v1.7 audit findings (fixed and proven)

An adversarial code review found four real defects, all fixed with tests:
1. LEDGER CHAIN RACE (serious): the scheduler and collector threads could
   read the same prev-hash concurrently and fork the chain. Demonstrated:
   the pre-fix code corrupts under an 8-thread stress test; the fixed code
   survives 320 concurrent appends with the chain verifying intact.
2. SQLITE LOCK CRASHES: busy_timeout was 0, so concurrent writes could
   raise "database is locked". Now 5000ms.
3. TIMING-UNSAFE AUTH: owner-token comparison used ==; now hmac.compare_digest.
4. STUCK ORDERS: a crash between payment recording and minting stranded
   orders as 'paid' forever. recover_stuck_orders() retries paid/failed
   orders every cycle (safe: minting is idempotent); tested.

Also added: GET /ledger/export (publishable proof-of-history),
railway.json health-checked deploy config, run_tests.sh, GitHub Actions CI.

## Roadmap to full "Bartok" capability

- **v1.1** Worker-model task execution (drafting, analysis) via `llm.work()`
- **v1.2** Real payment rail (Stripe Issuing virtual card)
- **v1.3** Embedding-based memory retrieval (swap `memory.retrieve`)
- **v1.4** Agent-to-agent participation (e.g. Moltbook API) — note the
  platform has documented impersonation/security problems; treat every
  counterparty as adversarial
- **v1.5** Public ledger page — publish `verify_chain()` output so claims
  about what your agent did are independently checkable

## Caveat on the source material

The Bartok story (NFT sales funding a robot-dog purchase) is Tony
Robbins's own account from a recorded interview; the transaction chain
has no public verification (no wallet, txns, or code released as of
July 2026). This project replicates the *claimed architecture*, which is
technically feasible — and adds the auditability that would make such a
claim provable.

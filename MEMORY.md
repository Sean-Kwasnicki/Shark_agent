# Shark Agent — Memory / Decision Log

Running log of decisions, findings, and state. Newest first. Pair with
`BLUEPRINT.md` (phase/status tracker). Keep entries factual; label anything
unverified.

---

## 2026-07-19 — Session: review, learning/feed upgrades, project survey, deploy prep

### Where we are
- **Phase 7 → 8** (deployment hardening into go-live). See `BLUEPRINT.md` §2.
- Deployed service (`shark-agent-u08m.onrender.com`) is UP but **non-functional
  for control**: `OWNER_TOKEN` and `ANTHROPIC_API_KEY` are not set, so every
  authed route 500s. Confirmed live via `/health` (200) vs `/ledger` (500).
- Render logs confirm **free-tier spin-down** (idle shutdown at 15 min, cold
  boot on next request) → ephemeral SQLite wipe. Strong inference from log
  timing, not dashboard-confirmed.

### Decisions made
- **Sales learner default = v3** (logistic Laplace-TS, `agent/learning3.py`).
  Rationale: measured +27% revenue vs v2 in wide-conversion regime, tie in v2's
  narrow regime and sparse regime — never worse. `SALES_LEARNER=v2` = instant
  rollback. Both log to shared `lints_decisions` table. alpha=0.5 chosen by sweep.
- **Do NOT merge all Desktop projects into one agent.** They span
  TS/Next/Flask/FastAPI and unrelated domains. Decision: keep Shark as the single
  control plane; extract proven components as clean Python modules (template =
  `agent/opportunity.py`). Standalone products stay standalone services.
- **Pet-insurance-style Facebook lead scraping = rejected.** ToS violation +
  TCPA/GDPR/CAN-SPAM liability (verified 2026-07). Compliant version: score
  lawfully-obtained signals via the opportunity engine, outreach stays gated.

### Built this session
- `agent/learning3.py` + `test_learning3.py` (logistic TS, benchmarked).
- `agent/opportunity.py` + `test_opportunity.py` (intent/fit/freshness/scarcity
  scoring, compliance wall, planner tool `opportunity.rank`, `/opportunities`).
- Feed upgrades: adaptive polling + trend momentum (`collector.py`,
  `intelligence.py`, `test_feed.py`).
- **Moltbook → opportunity connector** (`opportunity.ingest_from_feed`, wired
  into `collector.collect_once`): the 24/7 feed pull now routes posts with
  request language into the opportunity table (compliant source, intent-gated,
  deduped on post id, never raises in the loop). Tested in `test_feed` T6.
- **Go-live smoke harness** `test_live_smoke.py`: env-gated real-network checks
  (Stripe TEST payment link, webhook-secret signature round-trip, Crossmint
  staging mint, Moltbook `get_feed()`), refuses live Stripe keys and prod
  Crossmint, temp DB. Manual — deliberately not in `run_tests.sh`.
- `render.yaml` (paid disk blueprint), `.gitignore`, README deploy section.
- Defect fixes: see `BLUEPRINT.md` §4.

### Verification status
- **ALL 10/10 offline suites confirmed passing.** `test_learning2` reconfirmed
  independently (background run finished): "ALL V2 LEARNING EVALS COMPLETED",
  exit 0, chain verified over 36,200 entries (~29 min on this machine). After
  the connector work, the touched suites were rerun and pass: `test_feed`
  (7 tests incl. new feed→opportunity T6), `test_opportunity`, `test_collector`,
  plus `test_core`, `test_learning`, `test_economics`, `test_intelligence`,
  `test_hardening`.
- `test_live_smoke.py`: **only the skip path is verified here** (exit 0,
  per-credential SKIP). Its live request paths run once the owner sets keys.
- No live-network path has been exercised (no keys in this environment).

### Research done (sources cited to user)
- Model IDs `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` valid (Anthropic docs).
- Render free tier: ephemeral FS + 15-min spin-down (Render docs).
- Moltbook: few verifiably profitable agents; cost-offset via micro-tasks /
  prediction markets; reputation-building is the winning pattern.
- Upwork/marketplaces: fully-autonomous job-taking violates ToS; MCP beta allows
  read+draft with human confirm only.
- Stripe: first-party NFT sales = restricted business needing approval.

### Owner's side (blocking go-live) — as of this session
1. Deploy on **paid** Render (apply `render.yaml`) so data persists / runs 24/7.
2. Provide secrets in Dashboard: `ANTHROPIC_API_KEY` (required), Discord webhook,
   then Stripe TEST, Crossmint staging, Moltbook, Brave/Resend as available.
3. Moltbook registration completes with a human X-verification step.
4. Provide artwork you own the rights to (`LISTING_IMAGE_URL`).
5. Apply for Stripe NFT/business approval before switching to prod keys.

### Uncommitted
- All session changes are in the working tree, **not yet committed**
  (incl. `render.yaml`, `.gitignore`, `BLUEPRINT.md`, `MEMORY.md`,
  `test_live_smoke.py`). Commit pending explicit go-ahead.

### Next actions (agent side, when unblocked)
- ~~Write `test_live_smoke.py`~~ ✅ done (skip path verified; live run needs keys).
- ~~Build first compliant opportunity connector (Moltbook requests)~~ ✅ done.
- Run `test_live_smoke.py` with Stripe TEST + Crossmint staging keys (owner-gated).
- Additional connectors: ToS-compliant RFP/job boards, opt-in inbound.
- Begin module extraction: Pounce mailer first.

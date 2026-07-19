# Shark Agent — Blueprint & Phase Tracker

Canonical status document. Updated 2026-07-19.

This file is the single source of truth for **what phase the project is in,
what is complete and verified, and what still needs to be coded**. Pair it
with `MEMORY.md` (the running decision log). The product vision lives in
`README.md`; this file tracks execution against it.

---

## 1. What this project is (one paragraph)

An autonomous agent whose job is to find and act on revenue opportunities for
the owner, under a **deterministic control plane**: Python enforces every
spending cap, tool allowlist, and approval gate in code; the LLM only proposes
plans (as strict JSON) and writes copy. Every action is written to a
hash-chained audit ledger so claims about what the agent did are independently
verifiable. Money movement is simulated by default and gated behind human
approval; going live is a deliberate, key-by-key process.

---

## 2. Phase model

| Phase | Name | Definition of done | State |
|---|---|---|---|
| 0 | Deterministic core | Ledger, constraints, spend lifecycle, memory, DB, HTTP retry — all unit-tested | ✅ Complete, tested |
| 1 | Reasoning loop | Planner (observe→plan→validate→act→reflect), LLM wrapper, tiered models | ✅ Code complete; needs live API key to run |
| 2 | Learning | Bandit learners + benchmarks; active sales learner selectable | ✅ Complete (v1/v2/v3), benchmarked, all suites pass |
| 3 | Economics | Cost accounting, P&L, governor, idle-skip, daily digest | ✅ Complete, tested |
| 4 | Data feed | 24/7 collector, engagement model, market intelligence, trend momentum | ✅ Complete, tested (offline; live shapes unverified) |
| 5 | Commerce loop | draft→approve→publish→Stripe→Crossmint mint→revenue | 🟡 Code complete; **never run end-to-end against live APIs** |
| 6 | Opportunity engine | Deterministic intent/fit/freshness/scarcity scoring + ranking tool | ✅ Complete, tested — now fed live by the collector (Moltbook feed → `ingest_from_feed`) |
| 7 | Deployment hardening | Persistent disk, env wiring, one-click blueprint | 🟡 Config shipped (`render.yaml`); **not yet deployed on paid tier** |
| 8 | Go-live | Real keys, Stripe TEST→prod, Moltbook registration, first sale | 🟡 Smoke harness (`test_live_smoke.py`) written & skip-path verified; **execution blocked on owner keys + Stripe NFT approval** |
| 9 | Revenue expansion | Compliant lead/outreach lane; extract proven modules from sibling projects | 🟡 First connector shipped (Moltbook requests → opportunities, tested); other lanes planned (see §5) |

Legend: ✅ done & verified · 🟡 code done, verification/deploy pending · ⛔ not started / blocked.

**Current overall phase: transitioning from Phase 7 (deployment hardening) into
Phase 8 (go-live), with Phase 9 scoped but not begun.**

---

## 3. Component status (verified this session unless noted)

| Component | File | Status |
|---|---|---|
| Hash-chained ledger (+ concurrency lock) | `agent/ledger.py` | ✅ tested (`test_core`, `test_hardening`) |
| Constraint engine (caps, allowlist, domains) | `agent/constraints.py` | ✅ tested |
| Spend lifecycle (request→approve→execute, simulated) | `agent/spending.py` | ✅ tested |
| Persistent memory + retrieval | `agent/memory.py` | ✅ tested |
| SQLite layer (WAL, busy_timeout) | `agent/db.py` | ✅ tested |
| HTTP retry/backoff + idempotency | `agent/http.py` | ✅ tested |
| Planning loop | `agent/planner.py` | ✅ syntax/logic; LLM path needs key |
| Claude wrapper (tiered) | `agent/llm.py` | ✅ code; needs key |
| Learning v1 Beta-TS | `agent/learning.py` | ✅ tested (`test_learning`) |
| Learning v2 LinTS | `agent/learning2.py` | ✅ tested/benchmarked (`test_learning2`) |
| Learning v3 logistic Laplace-TS (**default**) | `agent/learning3.py` | ✅ tested/benchmarked (`test_learning3`) |
| Economics governor | `agent/economics.py` | ✅ tested (`test_economics`) |
| Collector + adaptive polling | `agent/collector.py` | ✅ tested (`test_collector`, `test_feed`) |
| Market intelligence + trend momentum | `agent/intelligence.py` | ✅ tested (`test_intelligence`, `test_feed`) |
| Opportunity scoring engine + feed connector | `agent/opportunity.py` | ✅ tested (`test_opportunity`, `test_feed` T6) |
| Go-live smoke harness (env-gated, manual) | `test_live_smoke.py` | 🟡 written; skip path verified, live paths need keys |
| Commerce loop | `agent/commerce.py` | 🟡 logic done; network untested offline |
| Stripe payment links + webhook HMAC | `agent/tools/payments.py` | 🟡 signature verify tested; API calls untested |
| Crossmint mint-on-sale | `agent/tools/nft.py` | 🟡 code; untested (needs staging key) |
| Moltbook connector (+ register endpoint) | `agent/tools/moltbook.py`, `main.py` | 🟡 rate limiter tested; network untested |
| Research (Brave) / notify (Discord/Resend) | `agent/tools/research.py`, `notify.py` | 🟡 code; needs keys |
| FastAPI surface + scheduler + collector loop | `main.py` | ✅ imports clean; routes need token |
| Deploy blueprint (paid disk) | `render.yaml` | 🟡 shipped; not yet applied |

**Test suites: ALL 10 of 10 confirmed passing** (`test_core`, `test_learning`,
`test_economics`, `test_collector`, `test_intelligence`, `test_hardening`,
`test_feed`, `test_opportunity`, `test_learning2`, `test_learning3`).
`test_learning2` confirmed independently: "ALL V2 LEARNING EVALS COMPLETED",
exit 0, ledger chain verified over 36,200 entries (~29 min run on this machine).
An 11th, **manual** harness (`test_live_smoke.py`) exists for go-live network
verification; it is intentionally excluded from `run_tests.sh` (offline-only)
and its skip path is verified (exit 0, per-credential SKIP reporting).

---

## 4. Fixed this session (defects, with cause)

1. Deployed URL returned 404 at `/` → added public status route (`main.py`).
2. Worker's style-directed sales copy was drafted then discarded at publish →
   now reused; publish tasks marked done (`agent/commerce.py`).
3. Per-cycle LLM call cap never enforced (hardcoded `llm_calls=1`) → real
   counting (`agent/planner.py`).
4. Failed listing drafts left orphan `(drafting)` rows (could auto-publish empty
   under `AUTO_PUBLISH`) → rolled back on failure (`agent/commerce.py`).
5. Email sender used a fake `agent@yourdomain.com` (silent Resend failure) →
   `RESEND_FROM` env, email skipped if unset (`agent/tools/notify.py`).
6. No in-app Moltbook registration → owner-authed `POST /moltbook/register`.
7. No persistence/spin-down protection on Render → `render.yaml` paid-disk
   blueprint; `.gitignore` added so runtime DB/secrets never get committed.

---

## 5. What still needs to be coded — at senior level

Ordered by leverage.

### A. Go-live verification harness (Phase 8) — ✅ WRITTEN, ⏳ execution needs keys
- ✅ `test_live_smoke.py` exists: env-gated checks for Stripe payment-link
  creation (TEST mode only — refuses `sk_live_` keys), webhook-secret signature
  round-trip, Crossmint STAGING mint (refuses `CROSSMINT_ENV=www`), and a
  Moltbook feed fetch through the production `get_feed()` path. Uses a
  throwaway temp DB. Verified here: it exits 0 and reports SKIP per missing
  credential; **the live request paths themselves are unexecuted** until the
  owner supplies keys, then run `python test_live_smoke.py` with them set.
- ⏳ **Moltbook response-shape validation** against the real API once a key
  exists (the smoke harness covers the feed; post/comment shapes still assumed).

### B. Compliant data-source connectors feeding the opportunity engine (Phase 9)
- ✅ **Moltbook connector shipped**: `opportunity.ingest_from_feed()` scans the
  feed the collector already pulls 24/7, keeps only posts with request language
  (intent ≥ 0.6), ingests them as compliant-source opportunities, dedupes on
  post id, and never raises inside the loop. Wired into `collector.collect_once`
  and tested end-to-end in `test_feed` T6 (request post ingested at score 90,
  chatter filtered, dedupe holds).
- ⏳ Remaining connectors: public RFP/job boards within their ToS, opt-in
  inbound. Senior bar: robots/ToS-aware, rate-limited, dedup'd, each with
  injected-fetcher tests like the collector.
- **Explicitly excluded**: Facebook/Meta scraping for consumer outreach — ToS
  violation + TCPA/GDPR liability (verified 2026-07). Do not build.

### C. Module extraction from sibling projects (Phase 9)
Port as clean Python modules obeying the control-plane rules, each with its own
passing test suite (the `opportunity.py` extraction is the template):
- **Pounce** `src/notify/mailer.py` → hardened SMTP outreach channel.
- **rainmaker** `classify/` + `enrich/` + `closer/` → compliant lead loop.
- **SourceSensePro** `utils/scoring.py` + `profit_calculator.py` → arbitrage lane.

### D. Roadmap items from README (later phases)
- Embedding-based memory retrieval (swap `memory.retrieve`; currently keyword).
- Real payment rail beyond simulated `execute_spend` (Stripe Issuing virtual
  card with its own hard limit below config caps).
- Public ledger page (publish `verify_chain()` output).

---

## 6. Known limitations (honest)

- No component that makes outbound network calls (Stripe, Crossmint, Moltbook,
  Brave, Resend, Discord) has been exercised against a live endpoint in this
  environment — all such verification is deferred to first deploy with keys.
- The learning benchmarks measure **sample-efficiency on synthetic ground
  truth**, not real-world profit. Real learning speed is bounded by real sales
  volume, which is unknown until live.
- Stripe classifies first-party NFT sales as a **restricted business needing
  approval** (verified 2026-07); go-live is not guaranteed to be approved.

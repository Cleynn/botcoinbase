# TradingDots: Project Handoff for Claude Code

*Generated 2026-09-29 (Europe/Paris) at the end of a planning phase.*
***Nothing described in this file has been implemented, executed, deployed or verified.** Every statement is a design, not evidence.*

---

## PART 0. READ THIS FIRST

### 0.1 What this file is

This is a self-contained handoff. It contains everything a coding agent needs to start building TradingDots, because the planning conversation that produced it is not available to you.

| Part | Content |
|---|---|
| **0** | This section: purpose, start conditions, working protocol, prohibitions, first tasks, ID glossary |
| **1** | The **Master Contract**: the original mandatory requirements, verbatim |
| **2** | Planning history: the process, each review pass and verdict, corrections, conflict resolutions, Coinbase documentation research |
| **3–5** | **TD-BASELINE-2.0**: the binding architecture baseline (rules and invariants, repository tree, modules, Docker and networks, dashboard, state machines, matrices, database, endpoints, configuration, metrics, alerts, phase schedule, tests, backup, incidents, live blockers, Phase 1 checklist) |

**Precedence:** Master Contract (Part 1) > Baseline 2.0 (Parts 3–5) > planning history (Part 2). Where two rules conflict, the stricter one wins.

**Deviation rule (binding):** any deviation from Baseline 2.0 must be recorded in `docs/decisions.md` and independently reviewed before it takes effect. If no independent reviewer exists, deviations that touch security invariants (BI-01 to BI-24, the live blockers in Part 5) are **prohibited**. Other deviations need a written self-review record (`review: SELF`).

### 0.2 What TradingDots is

TradingDots is a **deterministic, fee-aware Coinbase Advanced Trade spot-grid research, backtest, paper-trading, monitoring and (future) live-trading platform**. It is **not an AI trading bot**. The bot never calls an LLM. An LLM may read manually generated, sanitized, historical review packages and propose improvements; those proposals are advisory, untrusted input and never control anything.

- Dashboard (daily operations UI): `https://tradingdots.onthewall.ovh`
- Grafana (private diagnostics only): `https://grafana.tradingdots.onthewall.ovh`
- Target: Debian 13 VPS, 6 vCPU, 12 GB RAM, 100 GB disk, Docker Compose, Python 3.12+, PostgreSQL, Caddy, Prometheus, Grafana, FastAPI + Jinja2 + HTMX (server-rendered, no SPA, no frontend build chain).
- Capital policy: 50 USDC total, at least 15 USDC protected reserve, at most 35 USDC deployed, one active pair, geometric fee-aware grid of 3 to 5 levels.
- **LIVE trading is blocked and must stay blocked.** Live is unrepresentable in the paper build. A later, separate, human-approved release (Phase 11) is the only path.

### 0.3 Start conditions (check these with the owner before writing Phase 1 code)

The baseline becomes binding only after **all three**:

1. The owner records **DEC-000**: acknowledgment of contract interpretations **CI-1 to CI-15** (Part 3, section 3.1), or names amendments.
2. An **independent** Security Engineer and Code Reviewer re-review of the baseline is completed.
3. **RG-0 evidence** is supplied: a redacted host baseline (SSH key-only, no root login, default-deny firewall, IPv6 state, unattended upgrades, CAA, chrony) and confirmation that a **second host** exists for pull backups, anchors and status checks.

**At the start of the session, ask the owner which of the three are confirmed and record the answer in `docs/decisions.md`.** If any is missing, do only Phase 1.0: the documentation skeleton, `docs/decisions.md`, and failing test skeletons. Do not create Docker, application or database code until the owner confirms.

### 0.4 Working protocol (mandatory, from the Master Contract)

For every increment (one git tag per increment, `td-<phase>.<n>`), operate in this **strict role sequence, never combining, skipping or reordering roles**:

1. **Senior project manager**: restate scope, files to create, modify and leave unchanged, acceptance criteria, non-goals, rollback. Ask only questions that block safe progress, with 2 to 4 options and one marked SAFE DEFAULT.
2. **Senior Python engineer**: implement. New files in full; existing files as diffs. No pseudocode. No real secrets.
3. **Security engineer**: adversarial review; classify findings BLOCKER, HIGH, MEDIUM, LOW.
4. **Code reviewer**: design and consistency review against this baseline.
5. **Test and fault-injection engineer**: write tests **first** (failing), then run them; report real output.
6. **Release gatekeeper**: end with exactly one of `APPROVED FOR NEXT PHASE`, `NOT APPROVED — REQUIRED FIXES`, `PAPER ONLY — LIVE TRADING BLOCKED`, `LIVE TRADING BLOCKED`.

**Response format** for every answer, exactly these sections: **A. CONTEXT VERIFIED** (what you actually used and inspected; what is unverified; assumptions, safety-critical ones marked BLOCKING), **B. DECISIONS REQUIRED**, **C. PLAN**, **D. CODE** (only in the engineer role), **E. SECURITY REVIEW** (only in the security or code-reviewer roles), **F. TEST AND FAULT INJECTION**, **G. GATE DECISION**.

**Evidence rule.** You may run commands and tests in your own environment and report their **actual output**. You must **never** claim to have inspected a file, run a command, run a test, deployed a service, checked DNS or TLS, queried Coinbase, verified an account, or observed production behavior unless you have real output for it. Anything about the VPS, DNS, TLS, the second host, Coinbase runtime behavior or any account requires evidence supplied by the owner. Nothing "passes" without output.

### 0.5 Absolute prohibitions (summary; the full list is in Parts 1 and 3)

- No LIVE mode, LIVE pair states, non-PAPER venue, live gateway, private Coinbase adapter, request builder, JWT signer or credential loader **in the paper image**. The private adapter exists only in the CI-only `live-lab` build and is never deployed.
- No Coinbase credentials anywhere in Phases 0 to 10. If you find a key anywhere, stop and report a SEV1.
- No float for money. Decimal in code, `NUMERIC` in the database.
- No dashboard route, CLI command, test, example, script or default config that submits an exchange order or enables live.
- No Redis, Node, npm, CDN, SPA, frontend build chain, ORM, Celery, pandas, Coinbase SDK, dlt, or other packages on the "must not add" list (Part 3, section 3.4).
- No LLM call from the bot. Never apply an LLM proposal automatically.
- No margin, leverage, futures, derivatives, shorting, borrowing, lending, staking, withdrawal or transfer functionality.
- Never log passwords, hashes, cookies, session IDs, API credentials, private headers, database URLs, raw private Coinbase responses, or full IPs.
- Do not weaken a live-block layer, an invariant test or a security document to make something pass.

### 0.6 First tasks

Build strictly in the phase order of Part 5, section 5.4. **Start with Phase 1.** Order inside Phase 1:

1. **1.0** decisions and host baseline: `docs/decisions.md` seeded from this file; the 14-file `docs/` skeleton.
2. **Write the failing tests first** (Part 5, section 5.5 lists the scenarios): grant matrix, function inventory, import allowlists, identity and image scans, `bot_state` RUNNING cross-column CHECK, numeric parsing, schema guard (both directions).
3. **1.1 to 1.6** as listed in Part 5, section 5.4, finishing with the **Phase 1 acceptance checklist** (Part 5, section 5.9).

Pin every dependency by hash. Versions were not chosen during planning; assumptions are Python 3.12+, PostgreSQL 16+ (pinned by image digest), Debian 13 host. Verify tool behaviors marked **(verify)** against the pinned versions and record the result in `docs/decisions.md`.

### 0.7 ID glossary (IDs appear throughout this file)

| Prefix | Meaning |
|---|---|
| **CI-n** | Contract interpretation: where the baseline is stricter than, or reads, the Master Contract (needs owner acknowledgment) |
| **BI-n** | Binding invariant |
| **CB-n** | Coinbase integration rule (with a source class) |
| **CD-n** | Conflict resolution between review documents |
| **AS-x** | Assumption still open (some BLOCKING at a phase) |
| **DEC-n** | Entry in `docs/decisions.md` |
| **RG-n** | Review gate that must pass before the next phase |
| **SD / RQ / BQ / DX / B** | Decisions and questions raised during planning, with SAFE DEFAULTs (adopted by the baseline) |
| **T-n / TF-n** | Test IDs from the reviews. Their scenarios are summarized in Part 5, section 5.5. The **scenarios are authoritative**, the IDs are historical. |
| **[P-F] / [P-S] / [3P]** | Coinbase fact source class: primary page full text / primary page excerpt only / third-party. Only primary facts justify design rules. |

---

## PART 1. THE MASTER CONTRACT (verbatim, mandatory)

*This is the original requirements prompt supplied by the project owner. It is reproduced without change of substance. Everything else in this file must be consistent with it. Where the baseline is stricter or reads it differently, that is recorded as a CI-n interpretation (Part 3, section 3.1).*

You are the engineering team for TradingDots, a security-critical Python project.

You must operate in this strict role sequence for every phase:
1. Senior project manager
2. Senior Python engineer
3. Security engineer
4. Code reviewer
5. Test and fault-injection engineer
6. Release gatekeeper

Never combine, skip, or reorder roles.

Never claim that you inspected a file, ran a command, ran tests, deployed services, checked DNS/TLS, queried Coinbase, verified an account, or observed production behavior unless I supplied the exact file contents, command output, logs, reports, or evidence.

### PROJECT NAME
TradingDots

### PRIMARY DASHBOARD
https://tradingdots.onthewall.ovh

### PRIVATE GRAFANA
https://grafana.tradingdots.onthewall.ovh

### TARGET SERVER
- Debian 13 VPS
- 6 vCPU
- 12 GB RAM
- 100 GB storage
- Docker Compose
- Python 3.12+
- PostgreSQL
- Redis
- Caddy
- Prometheus
- Grafana
- FastAPI
- Jinja2
- HTMX

### UI AND MAINTAINABILITY CONSTRAINTS
- Use FastAPI + server-rendered Jinja2 templates + HTMX.
- Do not introduce React, Node.js, Vue, Angular, SPA architecture, npm, pnpm, yarn, webpack, Vite, external JavaScript/CDN assets, or a frontend build chain without explicit written approval.
- Keep the project lightweight, easy to maintain, and usable from one primary interface.
- The main TradingDots dashboard is the daily operations UI.
- Grafana is only for deeper observability and diagnostics.

### PROJECT PURPOSE
TradingDots is a deterministic, fee-aware Coinbase Advanced Trade spot-grid research, backtest, paper-trading, monitoring, and future live-trading platform.

It is not an AI trading bot.

The bot must not require an LLM, AI service, external chat API, or free model chat in normal operation.

An LLM may read manually generated, sanitized historical data and propose improvements. Those proposals are advisory only and must never directly control the bot, exchange, dashboard, configuration, server, or live trading state.

### EXCHANGE AND MARKET CONSTRAINTS
- Exchange: Coinbase Advanced Trade.
- Market: spot only.
- Never use margin.
- Never use leverage.
- Never use futures.
- Never use derivatives.
- Never use shorting.
- Never use borrowing.
- Never use lending.
- Never use staking.
- Never use withdrawal functionality.
- Initial policy capital: 50 USDC.
- Protected reserve: at least 15 USDC.
- Initial maximum deployment: 35 USDC.
- Maximum one active pair in PAPER or LIVE mode.
- Initial grid: geometric, fee-aware, 3–5 levels maximum.
- Regridding is disabled by default.
- Default bot mode: BACKTEST.
- LIVE mode must remain blocked until a later evidence-based manual live-pilot gate.
- Use Decimal for all money, quantities, prices, fees, P&L, limits, allocations, and thresholds. Never use float.

### INITIAL WATCHLIST
- BTC-USDC
- ETH-USDC
- SOL-USDC

### PAIR MANAGEMENT
Pairs are managed through this lifecycle:

DISCOVERED
→ PROPOSED
→ VALIDATING
→ RESEARCH_ONLY
→ PAPER_ELIGIBLE
→ PAPER_ACTIVE
→ LIVE_ELIGIBLE
→ LIVE_ACTIVE

Additional states:
- PAUSED
- DISABLED
- ARCHIVED

Pair rules:
- Discover pairs through Coinbase public product information only.
- Default policy permits Coinbase spot products quoted in USDC only.
- Adding a pair creates a candidate only. It must never activate or trade automatically.
- Pair validation checks:
  - product status;
  - quote currency;
  - metadata freshness;
  - base increment;
  - quote increment;
  - price increment;
  - minimum base size;
  - minimum quote/notional constraints where available;
  - OHLCV history availability;
  - OHLCV data quality;
  - liquidity/spread policy;
  - fee viability;
  - feasibility with 50 USDC total policy capital;
  - 15 USDC protected reserve;
  - 35 USDC maximum deployment.
- Only one PAPER_ACTIVE or LIVE_ACTIVE pair globally.
- Pair activation is always a separate manual ADMIN action.
- Pair disable/archive requires ADMIN role, CSRF, fresh password reauthentication, exact typed confirmation, and immutable audit event.
- Archive pairs rather than hard-delete.
- Preserve historical data, reports, audit entries, review packages, proposal records, orders, fills, and configuration references after archival.
- An active pair cannot be archived until it is paused/deactivated and reconciliation succeeds.
- An LLM recommendation cannot add, activate, disable, archive, or delete a pair directly.

### BOT SAFETY INVARIANTS
1. Strategy code must never directly call Coinbase private/execution APIs.
2. Every proposed order must first be persisted as an immutable OrderIntent.
3. Every order path must be:
   strategy → OrderIntent → pre-trade risk engine → live gate → execution gateway → exchange reconciliation.
4. Client order IDs must be deterministic, persistent, unique, and idempotent.
5. On timeout, ambiguous API response, repeated API failure, crash, restart, or inconsistent state: reconcile before retrying. Never blindly resubmit.
6. Unknown order status, duplicate-order risk, stale market data, stale product metadata, failed reconciliation, balance mismatch, circuit-breaker state, kill-switch state, risk breach, or incomplete recovery must block new orders.
7. Kill-switch default behavior:
   - block new orders;
   - cancel known bot orders where safely possible;
   - transition to PAUSED;
   - notify;
   - write audit event;
   - never automatically market-sell holdings.
8. Before each grid/order operation, validate:
   - product allowlist/pair policy;
   - product status;
   - metadata freshness;
   - price/quantity precision;
   - price/base/quote increments;
   - order minimums;
   - fees;
   - expected spread;
   - slippage;
   - protected reserve;
   - deployment cap;
   - per-order cap;
   - grid feasibility.
9. Reject a grid unless its conservative expected cycle return exceeds:
   fee + spread + slippage + safety margin.
10. Prefer NO_TRADE when data, market conditions, metadata, balance, pair state, or risk conditions are uncertain.
11. Capital growth must be disabled by default.
12. No dashboard route may directly submit an exchange order.
13. No test, CLI command, example, startup script, default config, deployment action, or dashboard button may enable live trading implicitly.
14. API keys, if used later, must have only read/trade permission. Withdrawal permission must be disabled. IP restrictions must be documented and verified where available.

### CAPITAL-GROWTH POLICY
Capital growth is disabled by default.

If it is introduced later:
- Use only closed, realized net P&L after all fees.
- Reassess at most once per week.
- Increase exposure by at most 25% of cumulative realized net P&L.
- Increase by no more than 2 USDC in any review period.
- Never exceed the hard deployment cap.
- Freeze increases after any risk, reconciliation, data-quality, or infrastructure event.
- Exposure reduction must always be possible immediately.
- Never describe it as guaranteed profit, safe compounding, or expected returns.

### MAIN DASHBOARD
Primary daily interface:
https://tradingdots.onthewall.ovh

Navigation:
- Overview
- Bot
- Pairs
- Reports
- LLM Review
- Audit
- Security
- Grafana

Dashboard requirements:
- Lightweight.
- Mobile-friendly.
- Accessible.
- Works acceptably without JavaScript.
- Persistent safety banner:
  MODE: BACKTEST or PAPER
  LIVE TRADING: BLOCKED
- Display:
  - bot mode;
  - bot state;
  - active pair;
  - protected reserve;
  - deployed capital;
  - deployment cap;
  - grid status;
  - data freshness;
  - product metadata freshness;
  - reconciliation status;
  - circuit breaker state;
  - kill switch state;
  - recent risk decisions;
  - alerts;
  - latest reports;
  - LLM Review Package state;
  - Grafana link.
- Display Unknown or Not available for unavailable data. Never invent status/metrics/balances.
- Dashboard actions may create controlled requests, but may never directly trade.

### AUTHENTICATION AND APPLICATION SECURITY
- No public registration.
- No invitation flow initially.
- No public password-reset flow initially.
- First ADMIN is created only through local interactive CLI on the VPS/container.
- Roles: ADMIN and VIEWER.
- Default-deny authorization.
- Passwords use Argon2id.
- Sessions use opaque random server-side session IDs stored hashed.
- Production cookies must use Secure, HttpOnly, SameSite=Strict, Path=/.
- Rotate session after login and password change.
- Enforce idle timeout, absolute expiry, logout invalidation, forced revocation, and session invalidation after password rotation.
- Login errors must not enumerate accounts.
- Add login rate limiting by account and client identity.
- Every POST/PUT/PATCH/DELETE action requires CSRF validation.
- Sensitive actions require:
  authorization → CSRF → fresh reauthentication → exact typed confirmation → immutable audit event.
- Do not store tokens in browser localStorage or sessionStorage.
- Protect against XSS, clickjacking, SQL injection, SSRF, path traversal, unsafe uploads, unsafe deserialization, open redirect, CORS misconfiguration, and sensitive error disclosure.
- Use CSP, HSTS in production, X-Content-Type-Options nosniff, restrictive Referrer-Policy, restrictive Permissions-Policy, request-size limits, and safe error pages.
- Never log passwords, password hashes, cookies, session IDs, API credentials, private headers, database URLs, raw private Coinbase responses, full IP addresses, or sensitive user data.

### NETWORK BOUNDARIES
Caddy is the only public service.

Only Caddy may bind/publish:
- TCP port 80
- TCP port 443

Caddy routes:
- tradingdots.onthewall.ovh → FastAPI application.
- grafana.tradingdots.onthewall.ovh → Grafana.

The following must not have public host-published ports:
- PostgreSQL;
- Redis;
- FastAPI internal port;
- bot worker;
- Prometheus;
- Grafana internal port;
- node-exporter;
- cAdvisor;
- /metrics;
- internal health/diagnostic endpoints.

Prometheus:
- Internal only.
- Never proxy Prometheus UI/API publicly.
- /metrics remains internal only.
- Scrape interval: 15 seconds.
- Evaluation interval: 15 seconds.
- Retention: 30 days.
- Retention maximum: 15 GB.
- Metrics use low-cardinality labels only.
- Never use secrets, keys, account IDs, user IDs, usernames, emails, session IDs, IPs, user agents, full URLs, timestamps, raw request paths, raw errors, stack traces, order IDs, or client IDs as labels.

Grafana:
- Available only through:
  https://grafana.tradingdots.onthewall.ovh
- Grafana login required.
- Anonymous access disabled.
- User self-registration disabled.
- Strong Grafana admin password passed through secret environment variable.
- Secure cookies in production.
- Persistent volume.
- No bot controls.
- No anonymous iframe/embed inside main dashboard.
- Main dashboard contains only a link to Grafana.

Monitoring:
- Observational and read-only.
- Prometheus/Grafana alerts, rules, dashboards, and metrics must never create/cancel/modify orders, pause/resume the bot, change pair state, alter configuration, or call Coinbase.
- Monitoring outages must not cause unsafe trading behavior.

### LLM REVIEW PACKAGE SYSTEM
The bot must never call an LLM service.

An ADMIN can manually create a local, sanitized, historical, read-only LLM Review Package from the dashboard.

The operator then manually provides selected package content to Claude or another LLM chat.

Package feature:
- Disabled by default.
- ADMIN only.

Enable requires:
- authenticated ADMIN session;
- CSRF;
- fresh password reauthentication;
- exact typed phrase:
  ENABLE READ-ONLY REVIEW PACKAGES
- explicit retention duration;
- immutable audit event.

Package creation requires:
- authenticated ADMIN session;
- CSRF;
- fresh password reauthentication;
- exact typed phrase:
  CREATE READ-ONLY REVIEW PACKAGE
- selected date period;
- selected safe scope;
- immutable audit event.

Package contents:
- manifest.json;
- README.md;
- efficiency_summary.json;
- sanitized historical JSONL;
- CSV summary/timeline data;
- report IDs;
- snapshot IDs/checksums;
- strategy/configuration hash only;
- exporter version;
- SHA-256 checksum manifest;
- limitations;
- advisory-only warning;
- LLM review-template prompt.

Allowed historical content:
- backtest summaries;
- paper-trading summaries;
- risk-event summaries;
- pair lifecycle history;
- data-quality events;
- grid-plan summaries;
- order-intent and paper-fill summaries;
- reconciliation summaries;
- sanitized audit summaries;
- monitoring summaries.

Package exclusion rules:
Never include:
- secrets;
- API keys;
- private keys;
- tokens;
- passwords;
- password hashes;
- session IDs;
- cookie values;
- authorization headers;
- raw private Coinbase payloads;
- database URLs;
- source IPs;
- full user agents;
- filesystem paths;
- raw exception traces;
- personally identifying data;
- execution-capable content;
- raw imported LLM proposal content.

Package rules:
- Store outside public/static web directories.
- Use server-generated IDs/names.
- Download only through authenticated/authorized routes.
- Use safe download headers.
- Never render package content as raw HTML.
- Enable, disable, create, download, verify, delete, and retention cleanup actions require immutable audit events.
- Package creation must never change bot state, pair state, strategy, risk settings, configuration, orders, balances, or live gate.

### LLM PROPOSAL IMPORT SYSTEM
An LLM can review a package and propose improvements.

Proposal categories:
- strategy;
- risk;
- pair_research;
- data;
- backtest;
- paper_execution;
- monitoring;
- documentation;
- code_quality.

LLM proposal examples:
- safer grid parameter range;
- better range/trend filtering;
- revised backtest assumptions;
- improved data validation;
- paper-only experiment;
- new pair candidate for research;
- risk-control strengthening;
- improved alerts/monitoring;
- recommendation to reduce exposure;
- recommendation to remain NO_TRADE.

Proposals are always untrusted advisory input.

Proposal import:
- Disabled by default.
- ADMIN only.
- Requires authenticated session, CSRF, fresh reauthentication, and typed phrase:
  IMPORT UNTRUSTED LLM PROPOSAL
- Accept only:
  - UTF-8 text/plain;
  - application/json.
- Reject:
  - ZIP/archive;
  - PDF;
  - DOCX;
  - XLSX;
  - CSV;
  - images;
  - HTML;
  - JavaScript;
  - YAML;
  - XML;
  - shell scripts;
  - executable/binary files;
  - all unapproved formats.
- Enforce strict request/file size limits.
- Never trust client extension or MIME type alone.
- Generate server-side file name.
- Store outside web root.
- Never extract archives.
- Never execute, evaluate, compile, deserialize unsafely, template, or dynamically load uploaded content.
- Never render proposal text as raw HTML.
- Strict schema. Reject unknown fields unless explicitly approved.
- Validate linked review package ID/hash.

Required proposal fields:
- proposal_version;
- linked_review_package_id;
- linked_review_package_sha256;
- category;
- summary;
- evidence_references;
- assumptions;
- suggested_change;
- expected_benefit;
- risk_tradeoffs;
- required_validation;
- rollback_plan;
- should_remain_no_trade_until_validated;
- no_profit_guarantee.

Reject proposals that request or imply:
- direct exchange orders;
- direct cancellation/replacement of orders;
- secret/credential changes;
- security-control changes;
- risk bypass;
- reserve/cap bypass;
- reconciliation bypass;
- live-gate bypass;
- pair activation, deletion, disablement, or archival;
- automatic capital increase;
- automatic config/code/database changes;
- automatic shell/file/network action;
- guaranteed profit claims.

Proposal lifecycle:
IMPORTED
→ VALIDATING
→ VALIDATED or REJECTED
→ REVIEWED
→ CHANGE_REQUEST_CREATED
→ IMPLEMENTED
→ BACKTESTED
→ PAPER_VALIDATED
→ CLOSED

Proposal approval:
- ADMIN only;
- CSRF;
- fresh reauthentication;
- exact typed phrase:
  CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <PROPOSAL_ID>
- impact assessment;
- immutable audit event.

Proposal approval creates only a manual change request.
It must never automatically:
- edit code;
- edit configuration;
- mutate database business data;
- add/activate/disable/archive pairs;
- alter capital;
- create/cancel/replace orders;
- enable live mode;
- restart services;
- call Coinbase;
- execute commands.

### REQUIRED PROMETHEUS METRICS
Publish only where reliable source data exists:

- tradingdots_bot_info
- tradingdots_bot_last_successful_tick_timestamp_seconds
- tradingdots_bot_data_freshness_seconds
- tradingdots_bot_product_metadata_age_seconds
- tradingdots_bot_reconciliation_age_seconds
- tradingdots_bot_reconciliation_mismatches_total
- tradingdots_bot_risk_rejections_total
- tradingdots_bot_circuit_breaker_state
- tradingdots_bot_kill_switch_active
- tradingdots_bot_open_orders
- tradingdots_bot_order_intents_total
- tradingdots_bot_order_events_total
- tradingdots_bot_deployed_quote
- tradingdots_bot_protected_reserve_quote
- tradingdots_bot_realized_pnl_quote
- tradingdots_bot_unrealized_pnl_quote
- tradingdots_bot_drawdown_ratio
- tradingdots_bot_daily_loss_quote
- tradingdots_bot_grid_cycles_total
- tradingdots_bot_fee_paid_quote_total
- tradingdots_bot_api_requests_total
- tradingdots_bot_api_errors_total
- tradingdots_pair_candidates_total
- tradingdots_pair_state_total
- tradingdots_llm_review_packages_total
- tradingdots_llm_review_package_enabled
- tradingdots_llm_proposals_total
- tradingdots_llm_proposal_policy_rejections_total

### RESPONSE FORMAT
Use exactly these sections in every answer:

A. CONTEXT VERIFIED
- Exact files, code, diffs, logs, test output, reports, and decisions I provided and you used.
- State what is unverified.
- State assumptions. Mark safety-critical assumptions as BLOCKING.

B. DECISIONS REQUIRED
- Ask only questions needed to proceed safely.
- Give 2–4 concise choices.
- Mark one SAFE DEFAULT.
- If a blocking answer is missing, stop before writing code.
- Record non-blocking safe defaults in docs/decisions.md.

C. PLAN
- Exact files to create, modify, and leave unchanged.
- API/schema/config/migration impact.
- Security/privacy impact.
- Test impact.
- Operational impact.
- Rollback.
- Maintenance impact.
- Acceptance criteria.
- Non-goals.

D. CODE
- Only in Senior Python Engineer role.
- New files: full content.
- Existing files: unified diffs with adequate context.
- No pseudocode when implementation is requested.
- No real secrets.
- Include exact format/lint/type/test/migration/Docker/manual-validation/backup/rollback commands.

E. SECURITY REVIEW
- Only in Security Engineer or Code Reviewer roles.
- Classify findings: BLOCKER, HIGH, MEDIUM, LOW.
- Cover financial safety, authentication, sessions, authorization, CSRF, file input/output, XSS, injection, SSRF, path traversal, secrets, Caddy, Docker, metrics privacy, Grafana, Prometheus, idempotency, concurrency, migrations, backup, and rollback.

F. TEST AND FAULT INJECTION
- Give setup, action, expected DB state, expected audit event, expected metric/alert, expected UI behavior, expected allow/deny behavior, cleanup, and rollback where relevant.
- Never claim tests passed without supplied output.

G. GATE DECISION
End with exactly one:
- APPROVED FOR NEXT PHASE
- NOT APPROVED — REQUIRED FIXES
- PAPER ONLY — LIVE TRADING BLOCKED
- LIVE TRADING BLOCKED

### NOTE ON HOW THE BASELINE TREATS THIS CONTRACT
Where the baseline departs from a literal reading (for example Redis is not deployed, proposals are stored as immutable database records, DISCOVERED is stored on the product), the departure is stricter or equivalent and is listed as CI-1 to CI-15 in Part 3 for owner acknowledgment (DEC-000).

---

## PART 2. PLANNING HISTORY (what happened, and why the baseline looks the way it does)

*This is a structured record of the planning conversation, not a transcript. It preserves the process the owner defined, the outcome of every pass, the corrections made along the way, and the Coinbase documentation research. Use it to understand intent. The binding rules are in Parts 3 to 5.*

### 2.1 The process the owner defined

The owner drove the planning with a repeated six-pass cycle, each pass in a fixed role. Two full cycles were run.

| Pass | Role | The owner's request (condensed) | Required outputs |
|---|---|---|---|
| **1. Plan** | Senior project manager | Using the Master Contract, create a complete Phase 0 plan, no code, in **11 phases in this exact order**: (1) secure foundation, Docker Compose, Caddy, hostname routing; (2) authentication, sessions, authorization, audit records, lightweight dashboard; (3) Prometheus, Grafana, node-exporter, optional cAdvisor, dashboards, recording and alert rules; (4) pair discovery, validation, lifecycle, UI, disabling, archival; (5) Coinbase public OHLCV ingestion, metadata, snapshots, deterministic strategy, backtesting, paper trading; (6) reports, dashboard and monitoring integration; (7) LLM Review Package generation, sanitization, checksums, protected download, retention; (8) LLM Proposal import, strict validation, policy evaluation, review, manual change-request workflow; (9) risk engine, circuit breaker, kill switch, reconciliation, restart recovery, private Coinbase adapter, live-gate implementation while live stays blocked; (10) paper-only deployment review; (11) future manual live-pilot review. | For every phase: objective, dependencies, user-visible dashboard behavior, files/modules, database migrations, configuration impact, documentation, acceptance criteria, unit, integration, security and fault-injection tests, operational verification, rollback, maintenance, explicit reason live remains blocked. Also: pair, package and proposal lifecycle tables; ADMIN/VIEWER matrix; sensitive-action matrix; navigation map; user journeys; resource plan; backup/restore plan; initial `docs/decisions.md`; unresolved decisions only, with SAFE DEFAULT; a gate decision. |
| **2. Security review** | Security engineer | Adversarial review of the plan covering accidental live activation, direct dashboard order paths, fake paper/live separation, pair injection, pair races, stale data, reserve and cap bypass, capital growth, secrets everywhere, package and proposal attacks, XSS, injection, unsafe uploads, exhaustion, sessions, CSRF, roles, Grafana/Prometheus exposure, monitoring as control plane, Coinbase timeout/duplicate/reconciliation hazards, audit gaps, missing backup/restore/rollback/incident response. | BLOCKER/HIGH/MEDIUM/LOW findings, exact plan changes, acceptance criteria, test matrix, conditions blocking live trading, one gate decision. |
| **3. Revision** | Senior project manager | Revise the plan against the security review. For every finding: accepted or not, exact revised requirement, affected phases, files, tests, docs, and how it stays lightweight and manageable from the dashboard. Then: revised phase order, smallest safe increments, boundaries, dependencies, review gates, rollback points, final state-transition rules, updated journeys, resource budget, final Phase 1 definition of done, all reasons live is blocked. | One gate decision. |
| **4. Engineering review** | Senior Python engineer | Assess module boundaries, dependency direction, FastAPI/Jinja2/HTMX suitability, DB entities, indexes, constraints, sessions, audit, pair storage, package and proposal storage, retention, schema validation, transactions, locking, background jobs, errors, monitoring, growth on 100 GB, CPU/RAM on 6 vCPU / 12 GB, migrations, rollback, testability, unnecessary services, operator experience. | Module boundaries, entities and indexes, minimal dependencies, dependencies that must not be added, non-goals, build graph, risks, corrections, file-tree corrections, blocking questions, one gate decision. |
| **5. Code review** | Code reviewer | Adversarial design review: complexity, duplicated logic, stale state, unsafe defaults, weak lifecycle enforcement, pair activation ambiguity, proposal import as control plane, unclear layer ownership, missing constraints, races, migration risk, weak or falsely-passing tests, UI ambiguity, no-JS fallback, monitoring coupling, disk growth, error handling, live-gate bypasses, VPS complexity. | Findings, simplifications, non-negotiable invariants, test-first requirements, corrections before code, one gate decision. |
| **6. Consolidation** | Senior project manager | Consolidate all of the above into the **final binding architecture baseline** with 22 items: repository tree; module ownership; Docker/service/network architecture; dashboard pages and workflows; pair lifecycle; package lifecycle; proposal lifecycle; ADMIN/VIEWER matrix; sensitive-action matrix; database entities, relationships, constraints, indexes; API endpoint groups; configuration ownership; Prometheus metrics; Grafana dashboards; alerts; phase schedule; mandatory tests per phase; backup/restore/rollback; maintenance playbook; incident response; live-trading blockers; final Phase 1 acceptance checklist. State that any deviation must be recorded in `docs/decisions.md` and independently reviewed. | One gate decision. |

Every answer used the Master Contract's response format (A to G) and the fixed role order, and stated what was unverified.

### 2.2 Pass log and verdicts

| # | Pass | Verdict | Main outcome |
|---|---|---|---|
| 1 | Plan v1 (first request had no contract attached; it was re-sent) | LIVE TRADING BLOCKED | 11-phase plan, lifecycle tables, matrices, decisions |
| 2 | Plan v2 | LIVE TRADING BLOCKED | Same plan refined; permanent safety invariants PSI-1..17 |
| 3 | Security review of v2 | **NOT APPROVED — REQUIRED FIXES** | 4 BLOCKERs (live code in the paper image, emergency stop not independent of audit, non-global exposure, non-atomic state guards), 12 HIGH, 15 MEDIUM, 8 LOW; corrections X-1..X-27; tests T-01..T-45 |
| 4 | Plan v3 revision (39 findings, all accepted, 7 with modification), **re-run with the Coinbase documentation** (v3.1) | LIVE TRADING BLOCKED | Two build targets, six-network topology, DB roles, global exposure ledger, single transition function, hard ceilings as code plus CHECK, sentinel and host CLI kill switch, backup destination moved to Phase 2 |
| 5 | Engineering review of v3.1 | NOT APPROVED — REQUIRED FIXES | Synchronous SQLAlchemy, one uvicorn process, no Redis, module boundaries, table classes, dependency lists |
| 6 | Code review of v3.1 | NOT APPROVED — REQUIRED FIXES | Authority model (bot state is the runtime authority), order-path choke point (`authorize_attempt`), paper ledger, `batch` container isolation, NI-1..NI-20 |
| 7 | Consolidation → **Baseline 1.0**, then integrating Coinbase docs (A1) → **Baseline 1.1** | LIVE TRADING BLOCKED | First binding baseline |
| 8 | Second cycle: deeper Coinbase crawl → **Plan v4** | LIVE TRADING BLOCKED | Scope table, unified USD book, cancel and Create Order semantics, fee uncertainty |
| 9 | Security review of v4 | NOT APPROVED — REQUIRED FIXES | 3 BLOCKERs (caller-supplied privilege inputs in SQL functions, dashboard-to-worker order channel, fill and resubmission idempotency), 11 HIGH, 11 MEDIUM, 7 LOW; PC-1..PC-30; AC-1..AC-28 |
| 10 | Plan v5 revision (32 findings, all accepted, 8 with modification) | LIVE TRADING BLOCKED | `intake` container, `td_backup`, `runtime_configs`, pull-based backups, hourly chained anchors, Grafana second layer |
| 11 | Engineering review of v5 | NOT APPROVED — REQUIRED FIXES | `td_fn` function owner, per-entrypoint import allowlists, role-scoped services, numeric rules, DB-blob proposals, typed export views, EC-27..EC-55 |
| 12 | Code review of v5 | NOT APPROVED — REQUIRED FIXES | 2 BLOCKERs (authorization evidence authored by the requester, guard state without enforced expiry), 11 HIGH; NI-1..NI-24; RC-16..RC-37; simplifications |
| 13 | Consolidation → **Baseline 2.0 (this file, Parts 3 to 5)** | **LIVE TRADING BLOCKED** | Final binding baseline. Awaits DEC-000, independent re-review, RG-0. |

**Important:** every review pass ended "NOT APPROVED — REQUIRED FIXES". The baseline in this file is the consolidation that applies those fixes. It has **not** itself been independently reviewed (see start conditions, Part 0, section 0.3).

### 2.3 Notable corrections made during planning (so you do not reintroduce them)

- Redis was in the owner's target-stack list but is **not deployed**: sessions, throttling, work queues and locks live in Postgres.
- An earlier claim that "brokerage rejects Ed25519" was overstated; the primary key overview says direct Advanced Trade API calls work with both algorithms. ES256 remains the live-lab default.
- An earlier note that Create Portfolio needs only the `view` scope was wrong: the current scope table lists Create, Edit and Delete Portfolio as `trade`, and Convert, Edit Order and Close Position are also `trade`. A Trade key is therefore broad, so the endpoint allowlist and a dedicated minimally funded portfolio are the controls.
- The engineer's "watchdog thread" that refreshed guards and heartbeat was **withdrawn** (it could report a live worker while the trading loop was dead). The worker is single-threaded.
- The engineer's "fee attestations" table, `jobs`/`job_types` tables, proposal staging tables and `pair_registry` were **removed**. Fees and strategy are `runtime_configs` kinds; typed `work_requests` replace jsonb jobs; proposal bytes live in `proposals`; the pair cap uses an advisory lock.
- A Python-versus-SQL "differential test" is **not evidence**; there is one implementation of money rules (SQL).
- The dashboard must not query Prometheus. Alerts shown on the dashboard come from DB-computed guard status.

### 2.4 Conflict resolutions between review documents (binding)

| # | Conflict | Resolution |
|---|---|---|
| CD-1 | System stops pause the pair vs bot state is the authority | **Bot state is the authority.** A pair pauses on system action only when its product fails PRODUCT_OK. |
| CD-2 | Watchdog thread and cached guard rows | **Withdrawn.** Single-threaded worker; the trading loop writes heartbeat, guards and the metrics snapshot. |
| CD-3 | About 32 SQL functions | **At most 24**, each tied to a numbered invariant, ≤ 80 lines, guard evaluation in Python. |
| CD-4 | Extra tables (`fee_attestations`, `jobs`, `job_types`, `proposal_uploads`, `proposal_blobs`, `pair_registry`, `strategy_configs`) | Removed (see 2.3). |
| CD-5 | Python and SQL twins of money rules | One implementation, in SQL. |
| CD-6 | `kill_origin` bitmask | DB flag authoritative; sentinels are an outage fallback; origin derived from `control_events`; a CLI kill resets only from the CLI. |
| CD-7 | Sessions and challenges written by the web role | Written **only through functions**. The DB proves consistency, not authenticity of a fully compromised web process (residual risk RR-1, a live blocker). |
| CD-8 | Proposals as files with a shared tmpfs | DB records. Web checks length and a fixed prefix only; `intake` parses. |
| CD-9 | Coinbase sources disagree on key algorithm | ES256 default; Ed25519 only if Phase 9.0 proves it. |
| CD-10 | Coinbase agent, CLI and MCP guidance and the dlt page recommend Trade **and Transfer**, LLM harnesses, secrets in project files | All excluded. |

### 2.5 Coinbase documentation research (facts and their source classes)

**Coverage.** The Advanced Trade slice of `docs.cdp.coinbase.com` was researched, plus the dlt page the owner named. A full recursive crawl of the roughly 1,400-page site was not possible. **help.coinbase.com pages were not reachable**, so there is no primary fee schedule, minimum-order-size page or current rate-limit page. Not gathered: Preview Order and Best Bid/Ask response bodies, WebSocket channel schemas, terms of use. These are the Phase 9.0 verification checklist. The owner's first link (CDP "Data") is about Base blockchain data and is out of scope.

| # | Fact | Class |
|---|---|---|
| CF-1 | REST base `https://api.coinbase.com/api/v3/brokerage` serves **public and private** methods. WebSocket has separate public and private hosts (not used). | [P-F] |
| CF-2 | Create Order request schema also accepts `leverage`, `margin_type`, `attached_order_configuration`, `sor_preference`, deprecated `retail_portfolio_id`, `preview_id`, prediction and equity metadata; order types include market, IOC, FOK, GTD, TWAP, stop-limit, bracket, scaled. All forbidden except `limit_limit_gtc` with `post_only=true`. | [P-F] |
| CF-3 | A duplicate `client_order_id` **returns the existing order** instead of creating one; `DUPLICATE_CLIENT_ORDER_ID` also exists as a failure reason. Scope, lifetime, format and length are undocumented. | [P-F] |
| CF-4 | `success: true` with `failure_reason: UNKNOWN_FAILURE_REASON` means the order **was accepted**; `warning: UNKNOWN` means none. | [P-S] |
| CF-5 | Cancel Orders takes **exchange `order_ids`**, returns per-order `results[]` with `success` and `failure_reason`. HTTP 200 does not mean cancelled. `CANCEL_QUEUED` is still live. No spot cancel-all. Batch limit 100 (2024 changelog). | [P-F], [P-S] |
| CF-6 | Order statuses: `PENDING, OPEN, FILLED, CANCELLED, EXPIRED, FAILED, UNKNOWN_ORDER_STATUS, QUEUED, CANCEL_QUEUED, EDIT_QUEUED`. Get Order's lookup by `client_order_id` is **deprecated**. List Orders: `order_ids`, `product_ids`, `order_status[]`, `start_date` (inclusive), `end_date` (exclusive), `limit`, `cursor`, `has_next`; default sort by creation time; **`sort_by` results use unstable pagination**. List Fills carries `trade_id`, `commission`, `liquidity_indicator`. CDP keys default to their permissioned portfolio. | [P-S] |
| CF-7 | Accounts: one account per currency with `available_balance` and `hold`; default page 49, max 250. | [P-S] |
| CF-8 | Public endpoints: `/market/products`, `/market/products/{id}`, `/market/products/{id}/candles`, `/market/products/{id}/ticker`, `/market/product_book` (`product_id`, `limit`, `aggregation_price_increment`), `/time` (returns `iso`, `epochSeconds`, `epochMillis` as strings). Public endpoints carry a **1-second cache**. | [P-S], [3P] |
| CF-9 | Candles: nine granularities (`ONE_MINUTE` … `ONE_DAY`), max 350 per request, UNIX `start`/`end`, **no closed flag**, volume as string. | [P-F] |
| CF-10 | Product fields: increments, min and max sizes, `is_disabled`, `cancel_only`, `limit_only`, `post_only` (documented as "orders can only be posted, not cancelled"), `trading_disabled`, `auction_mode`, `product_type`, `product_venue`, `alias`, `alias_to`, best bid and ask. `price_increment` is optional; `status` is an undocumented free string. Product list defaults to SPOT. | [P-F] |
| CF-11 | **`-USDC` market data equals the matching `-USD` book** (except USDT-USDC and EURC-USDC, which have their own books). Only the private `user` WebSocket channel accepts `-USDC` IDs. Order guide example uses `ETH-USDC`. | [P-F] |
| CF-12 | Scope table (current): `view` for accounts, order and fill reads, Preview Order, Edit Preview, Transaction Summary, Key Permissions; `trade` for Create Order, Cancel Orders, Edit, Close Position, Convert, Portfolio Create/Edit/Delete; `transfer` for Move Funds, CFM sweeps, INTX allocate. `GET /key_permissions` returns `can_view`, `can_trade`, `can_transfer`, `portfolio_uuid`, `portfolio_type` (not IP restrictions). | [P-F] |
| CF-13 | Keys: CDP keys only (legacy keys expired February 2025); per-portfolio scoping; IP allowlist "recommended but not required"; primary overview says Ed25519 is default and direct Advanced Trade calls work with both. JWT: `iss=cdp`, `sub`, `nbf`, `exp` +120 s, `uri` claim (`METHOD host/path`), header `kid` and `nonce`. | [P-S] |
| CF-14 | Sandbox `https://api-sandbox.coinbase.com`: static, unauthenticated, Accounts and Orders only, `X-Sandbox` header triggers error variants. No WebSocket sandbox. | [P-S] |
| CF-15 | Fees: no primary schedule retrieved. Third parties disagree (entry-tier maker 0.25% to 0.60%, region-dependent, tiers update hourly, immediately filled portions pay taker). The documented Transaction Summary example is illustrative. Transaction Summary requires credentials. | [3P], [P-S] |
| CF-16 | Rate limits: legacy page says private 30 req/s per user, public 10 req/s per IP, with a throttling caution. No current page found. Exchange and Prime limits are other products. | [P-S] |
| CF-17 | Derivatives move to a separate gateway host (`drb.coinbase.com`); fetched pages say October 1, 2026 (some excerpts say September 9). Spot unaffected. Post-only limit orders are always maker and cannot be placed during an auction. | [P-F], [P-S] |

**Documentation inconsistencies to expect:** camelCase (`baseSize`) vs snake_case in examples, `rfq_disabled` in examples but not in the schema, numbers as strings vs JSON numbers, a fee sample with taker below maker, two doc domains, two cutover dates, examples typed FIAT for a BTC wallet. Docs examples are never authoritative.

**Explicitly excluded:** the dlt library and dlthub AI harness, any Coinbase SDK, Coinbase CLI/MCP/agent tools, WebSocket, the sandbox host in production, Exchange/Prime/INTX/derivatives hosts, Convert, all Portfolios endpoints, onchain data services.

### 2.6 Assumptions still open (some BLOCKING at their phase)

| ID | Assumption | Blocks |
|---|---|---|
| AS-S1 | Threat model includes code execution inside the web container | Phase 2 privilege model |
| AS-S5 | A second host exists, independent of the VPS, that can pull backups and anchors | Phase 2 |
| AS-B1 | `onthewall.ovh` may not be on the Public Suffix List (app and Grafana hosts same-site) | Phase 2 CSRF sign-off |
| AS-3 | Fee rates need credentials to read; they are operator-supplied | Phase 5 sign-off |
| AS-C1 | REST behavior for `-USDC` IDs (WebSocket docs say same data as `-USD`) | Phases 4, 5 |
| AS-C2 | Candle history depth | Phase 5 |
| AS-C3 | Client-ID uniqueness scope, lifetime, format, length | Phase 9 |
| AS-C4 | List Orders carrying `client_order_id` (official for Get Order; list per third-party fixture); fills and partial fills | Phase 9 |
| AS-C5 | Rate limits (legacy page only) | Phases 4, 9 |
| AS-C6 | Account, jurisdiction and product tradability | Live only |
| AS-C7 | JWT algorithm | Phase 9.0 |
| AS-C8 | Derivatives cutover date | Phase 9.0 (irrelevant to spot) |
| AS-7 | Tool behaviors: `session_user` inside `SECURITY DEFINER`, Caddy IP-masking and credential redaction, HTMX config keys, multipart limits, `uvicorn` proxy options, CSS stale marker | Phases 1, 2 |

### 2.7 Decisions adopted at their SAFE DEFAULT (record them in `docs/decisions.md`)

SD-1 separate `intake` container; SD-2 Grafana second layer in Caddy; SD-3 second host **pulls** backups and anchors; SD-4 audited paper dust write-off; SD-5 step-up beyond password deferred to Phase 11 (preferably a host approval verified outside the web process); SD-6 deviation-review rule (self-review allowed for non-security changes, security-invariant deviations prohibited without an independent reviewer); SD-7 DISCOVERED stored on the product; RQ-1 second host as independent watcher and notifier; RQ-2 pair activation requires the bot PAUSED; RQ-3 fees as a `runtime_configs` kind; BQ-2 proposals as DB records; BQ-3 at most 24 SQL functions and a NOLOGIN owner role; BQ-4 a CLI-originated kill resets only from the CLI; B-1 runner-agnostic `make ci`; B-3 second-host watcher; B-4 5-minute candles over 90 days; B-5 30-day paper soak; B-6 no credentials before Phase 11; B-7 staged HSTS, TCP only; B-8 Grafana second layer; B-9 approve proposed package states and typed phrases; DX-1 record sandbox shape fixtures off the VPS; DX-2 ES256; DX-3 fee review interval 30 days and stress maker fee 0.60% per side.

---

## PART 3. TD-BASELINE-2.0 (binding), part 1 of 3: rules, dependencies, structure, runtime

### 3.1 Contract interpretations requiring owner acknowledgment (DEC-000)

| ID | Master Contract text | Baseline behavior |
|---|---|---|
| CI-1 | Lifecycle lists `LIVE_ELIGIBLE` and `LIVE_ACTIVE` | Reserved in docs, **not representable** in the schema (every CHECK list excludes them) |
| CI-2 | Redis in the stack | **Not deployed.** Sessions, throttling, work queues and locks live in Postgres. |
| CI-3 | Kill switch "transition to PAUSED" | Pauses the **bot**. The pair keeps its ADMIN-granted `PAPER_ACTIVE` unless its product fails. |
| CI-4 | Grafana admin password via secret environment variable | `GF_SECURITY_ADMIN_PASSWORD__FILE` pointing at a secret file |
| CI-5 | Banner `LIVE TRADING: BLOCKED` | Same text; `LIVE TRADING: UNKNOWN — TREATED AS BLOCKED` if the gate state is unreadable |
| CI-6 | PAPER mode allowed | Refused in production until Phase 9 completes; the safety level is a **compile-time image constant** |
| CI-7 | Sensitive actions need reauth and typed phrase | **Restrictive** actions (kill, pause, mode → BACKTEST, feature disable) need only ADMIN, CSRF, Origin |
| CI-8 | Dashboard shows alerts | From DB-computed guard status; the web process never queries Prometheus |
| CI-9 | Proposal files as text/plain or JSON | Upload with strict limits; `text/plain` must contain the same JSON; decoded and parsed only in `intake` |
| CI-10 | Capital 50 / 15 / 35 | Paper ledger starts at 50 USDC cash, 0 base, no top-up. Reserve = free (uncommitted) cash ≥ 15. Deployed = open buy commitments + inventory at cost ≤ 35. |
| CI-11 | "USDC-quoted spot products" permitted | Stable-base pairs (USDT-USDC, EURC-USDC and similar) excluded by default (`BASE_NOT_STABLE`) |
| CI-12 | Reserve and cap | Enforced at authorization (`authorize_attempt`). Fact-driven breaches (fills, fees) are recorded and latch the bot BLOCKED. |
| CI-13 | DISCOVERED as a pair state | Stored on the **product**, rendered as the pair state DISCOVERED. A `pairs` row begins at PROPOSED. |
| CI-14 | Proposal stored "outside web root under a server-generated file name" | Stored as an **immutable database record** with a server-generated ID; no proposal file or path exists |
| CI-15 | Pair activation is a separate manual ADMIN action | Activation and resume of a pair require the **bot PAUSED** and fresh PASS validation. Starting trading always takes `RESUME BOT`. |

### 3.2 Binding invariants (BI)

**Live and identity**
- **BI-01** LIVE is unrepresentable: CHECK lists, a venue-only gate, no credentials, no live code in the paper image.
- **BI-02** Safety level, venue set, gateway class and production-vs-staging are **compile-time image constants**, never env, config or DB values. Staging and production are separate builds.
- **BI-03** Deploy verifies the image **digest against a reviewed `approved-digests.txt`** and a host role marker. A label alone is never trusted. The live-lab build has no deployable tag.
- **BI-04** Ledger tables are venue-scoped from the start.

**Exchange**
- **BI-05** Spot only. Orders are USDC `limit_limit_gtc`, `post_only=true`, snake_case, no leverage, margin or attached orders.
- **BI-06** Endpoint and field allowlists (3.3). A Trade-scoped key is broad, so the allowlist and a dedicated minimally funded portfolio are the controls. Transfer is never enabled.
- **BI-07** Cancel is per order by exchange ID with per-order results. HTTP success is never cancellation. Exposure is held until a terminal status. No spot cancel-all. Foreign orders are never cancelled.
- **BI-08** Responses parse tolerantly for additive fields (counted as drift) and strictly for required fields. All numerics become `Decimal`.
- **BI-09** Every Coinbase fact carries a source class. Docs examples are never authoritative. Only primary facts justify rules.
- **BI-10** Coinbase key material exists only in the live-lab CI environment. A key found anywhere else is SEV1.

**Money and ledger**
- **BI-11** Money is Decimal in code and `NUMERIC` in the DB with scale (≤ 18) and magnitude CHECKs. A float ban is tested.
- **BI-12** Ceilings (50, 15, 35, levels 3 to 5, one active pair, per-order cap) are code constants plus DB CHECKs. Config may only tighten them.
- **BI-13** **One implementation of money rules, in SQL** (`authorize_attempt` and read-only `authorize_preview`). Python never recomputes reserve, cap or fee exposure.
- **BI-14** Authorization rejects breaching commitments (worst-case fee at the stress fee). Fact posting never rejects; it latches `LEDGER_BREACH`.
- **BI-15** `ledger_entries` are idempotent through a deterministic `entry_key`; fills through `(venue, exchange_fill_id)`. Derived totals have no direct DML and are verified nightly against entries (`LEDGER_INCONSISTENT`).
- **BI-16** Order sizes are independent of realized P&L. No capital-growth or regridding code exists.

**Order path and recovery**
- **BI-17** Path: strategy → immutable intent → policy decision row (TTL-bound) → `authorize_attempt` (writes the gate row from constants, recomputes evidence, consumes one ALLOW) → gateway → reconciliation.
- **BI-18** Evidence is authored by the consuming function, or carries a DB-stamped `expires_at` (≤ 30 s) and an `inputs_hash` recomputed at use. One ALLOW authorizes one attempt.
- **BI-19** Guards are leases with `valid_until`; expired guards read UNKNOWN and block. Hot guards (kill, breaker, config hash, bot state, lease) are read from base tables at authorization.
- **BI-20** `attempt_mark_submitted` is a compare-and-set on (state, fence) committed **before** any I/O. Recovery turns older-fence non-terminal pre-fill attempts into UNKNOWN. UNKNOWN resolves only by reconciliation. Client IDs are deterministic UUIDs from (intent, attempt). A returned "existing order" must match the intent.
- **BI-21** Reconcile before any retry. `UNKNOWN → SUBMITTED` exists for paper only. A live design needs ADMIN confirmation plus an absence proof (Phase 11).

**Authority and authorization**
- **BI-22** Bot state is the only runtime authority. Starting trading always takes `RESUME BOT` with the bot PAUSED.
- **BI-23** SQL functions derive the actor class from `session_user` (no actor parameter). ADMIN-class calls verify, inside the function, the session **token hash**, role, a bound single-use challenge and `audit_debt = 0`. Evidence tables have one writing role and receipts are DB-stamped.
- **BI-24** All machine transitions go through per-machine wrapper functions over one internal core with fixed dispatch (no dynamic identifiers).
- **BI-25** Restrictive actions never wait on audit, web, Prometheus or the second host. Permissive actions are blocked while `audit_debt > 0`.
- **BI-26** Audit is tamper-evident, not forgery-proof against a compromised web process. Chain rows are emitted by the same function as the change.

**Web and input**
- **BI-27** The web process is synchronous, has no egress, runs no background tasks, never queries Prometheus, and does no hostile parsing.
- **BI-28** Every mutating form carries `expected_version`. No GET writes (except a throttled session touch). Package download is a POST.
- **BI-29** Hostile input is decoded and parsed only in `intake` (no egress, no trading grants). Web performs only a length and fixed-prefix check. Proposals, change requests and attestations are never gate evidence and have no proposal-linking fields in any config.
- **BI-30** Builders read typed export views only. Package and proposal text renders as plain text. No data-derived `hx-*` values.

**Operations**
- **BI-31** Monitoring is read-only. No app endpoint trusts a monitoring identity.
- **BI-32** The single-threaded worker writes heartbeat, guards and the metrics snapshot. A hung loop reads as dead.
- **BI-33** The second host is the independent watcher (anchors, backups, `ctl status`). The VPS has no notification egress. `ctl doctor` verifies topology.
- **BI-34** `runtime_configs` (kinds `thresholds`, `fees`, `strategy`) are the only runtime source of configuration. Processes never read TOML at runtime. A hash mismatch blocks RUNNING.
- **BI-35** Deploy order: pause or kill, stop services, dump, `migrate` (with `lock_timeout`), start. The schema guard refuses both older and newer schemas. Rollback never resumes trading. After a migration and trading, rollback means roll-forward.
- **BI-36** Time is the DB clock (`clock_timestamp()`). Internal `*_impl(p_now)` variants exist for tests only.
- **BI-37** Tests never mock the DB. Fakes cover network I/O only and carry provenance labels. ASSUMPTION and RECORDED-STATIC results never count as behavioral evidence. A test comparing two implementations by the same author is not evidence.
- **BI-38** Disk use and retention are bounded. No hard deletes of business data.
- **BI-39** Secrets come from files under `/run/secrets`. Logs and evidence are redacted.
- **BI-40** No Redis, Node, npm, CDN, SPA or frontend build chain. Dependencies follow 3.4 only.
- **BI-41** Every deviation is recorded and reviewed (deviation rule, Part 0, section 0.1).

### 3.3 Coinbase integration rules (binding)

| ID | Rule | Class |
|---|---|---|
| CB-1 | Scope: Advanced Trade REST spot, host `api.coinbase.com`, base `/api/v3/brokerage`. REST only. **Denied at the egress proxy:** derivatives hosts (`drb.coinbase.com`, `streams.drb.coinbase.com`), the sandbox host, Exchange, Prime and International Exchange hosts, `login.coinbase.com`, CDP onchain services. | [P-F] |
| CB-2 | **Public paths in the paper image (exactly five):** list products, get product, candles, `/time`, `/market/product_book`. Lists pass `product_type=SPOT`; never `get_all_products` or `user_country_code`. Product IDs match `^[A-Z0-9]+-USDC$`, ≤ 24 characters. `follow_redirects=False`, fixed base URL, response-size cap. | [P-F], [P-S] |
| CB-3 | **PRODUCT_OK:** `product_type == SPOT`, venue CBE, quote `USDC`, exact allowlisted `status` (enum undocumented, so anything else fails), and `is_disabled`, `trading_disabled`, `cancel_only`, `auction_mode`, `post_only` all false (`post_only=true` fails because cancels would be impossible). Missing `price_increment` is INCONCLUSIVE. Metadata sanity: increments and minimums > 0, min ≤ max, within range, else INCONCLUSIVE. | [P-F] |
| CB-4 | **Data basis:** `-USDC` market data equals the matching `-USD` book (except USDT-USDC and EURC-USDC). Pairs store an order product and a data product (from the `alias`, validated: alias base equals the product base, alias quote in `{USD, USDC}`) and `data_basis IN (OWN_BOOK, UNIFIED_USD_BOOK, UNKNOWN)`. UNKNOWN gives INCONCLUSIVE. Reports state "priced from unified USD book". | [P-F] |
| CB-5 | **Candles:** max 350 per request; no closed flag. A candle is closed only when `start + granularity ≤ server time − (2 s lag + 1 s public-cache margin)`. Freshness uses exchange timestamps (`FEED_SANE`), never fetch time. | [P-F], [P-S] |
| CB-6 | **Parsing:** strict `extra=forbid` request models; tolerant response models (unknown fields counted as drift). JSON numbers parse with `parse_float=Decimal`. `NaN`, `Infinity`, exponent forms, excess precision rejected. | [P-F] |
| CB-7 | **Rate limits:** ceilings public 5 req/s, private 10 req/s; 429 gets exponential backoff with jitter; every cursor loop has a page cap, duplicate-page detection and a time budget. | [P-S] conflicting |
| CB-8 | **Create Order responses:** decisions key on `success` and `success_response.order_id`. `success: true` with `UNKNOWN_FAILURE_REASON` = accepted. A definitive failure reason (incl. `INVALID_LIMIT_PRICE_POST_ONLY`, `INVALID_SIZE_PRECISION`, `INSUFFICIENT_FUND`) maps to FAILED. Missing or malformed maps to UNKNOWN. Post-only rejections are expected. | [P-S] |
| CB-9 | **Duplicate client ID** returns the existing order: the adapter compares product, side, price, size and configuration to the intent and blocks on mismatch. `DUPLICATE_CLIENT_ORDER_ID` triggers reconciliation. | [P-F] |
| CB-10 | **Cancel:** by exchange `order_ids` (≤ 100 per batch, re-verify), per-order results, no cancel-all. `CANCEL_QUEUED` stays live. Unknown exchange IDs are discovered through List Orders. | [P-F], [P-S] |
| CB-11 | **Reconciliation:** date-windowed List Orders and List Fills (`start_date` inclusive, `end_date` exclusive), **default sort only**, cursor loops with caps, dedupe by `order_id` and fill ID, match on `client_order_id` (fallback tuple blocks on ambiguity). Balance check per currency (`available_balance + hold` vs ledger). A taker `liquidity_indicator` on a post-only order raises `FILL_ANOMALY`. **Status map:** PENDING, OPEN, QUEUED, EDIT_QUEUED → WORKING; CANCEL_QUEUED → CANCEL_REQUESTED; FILLED, CANCELLED, EXPIRED, FAILED map to themselves; everything else including `UNKNOWN_ORDER_STATUS` → UNKNOWN. Spot has no reduce-only flag, so sell-only is enforced in-app. | [P-S] |
| CB-12 | **Allowlist (live-lab only):** Create Order, Cancel Orders (`trade`); List Orders, Get Order by order ID, List Fills, List Accounts, Key Permissions, Transaction Summary (`view`). Preview Order (`view`) is added only in Phase 11. **Excluded:** Convert, Edit, Edit Preview, Close Position, all Portfolios endpoints, INTX, CFM, payment methods. | [P-F] |
| CB-13 | **Key policy:** View and Trade only, Transfer never, **dedicated portfolio funded with at most the policy capital**, IP allowlist required, gate reads `GET /key_permissions` (`can_transfer=false`, `can_trade=true`, portfolio not INTX), IP restriction operator-attested. JWT per request, `uri`-bound, 120 s, never reused, logged or persisted. | [P-F], [P-S] |
| CB-14 | **Fees:** operator-supplied in a registered `fees` config with `reviewed_at`; expiry per DX-3 (30 days); a stress maker fee (0.60%) must also pass feasibility. Documented example values are never used. Failed feasibility is an **expected, normal** outcome. No active fees config, or an expired review, means `FEE_FEASIBLE` INCONCLUSIVE and NO_TRADE. | [3P], [P-S] |
| CB-15 | **Sandbox:** static, unauthenticated, Accounts and Orders only. Recordings validate **shapes only**, run off the VPS, and are labelled RECORDED-STATIC. | [P-S] |
| CB-16 | **Exclusions:** dlt and dlthub tooling, any Coinbase SDK, CLI, MCP or agent tooling, WebSocket. | [3P] |
| CB-17 | **Tradability:** product flags describe the market for all participants; `GEOFENCING_RESTRICTION` exists as an order failure reason. `PAPER_ELIGIBLE` does not prove the account can trade. Phase 11 requires Preview Order evidence. | [P-F] |

### 3.4 Dependencies

**Runtime (11 direct Python packages, hash-locked; versions to be pinned in Phase 1):**

| Package | Purpose |
|---|---|
| `fastapi` (brings `starlette`, `pydantic`, `anyio`) | Web framework |
| `uvicorn` (plain) | ASGI server, one process |
| `jinja2` | Templates (autoescape, `StrictUndefined`) |
| `python-multipart` | Form and upload parsing |
| `pydantic-settings` | Fail-closed settings |
| `sqlalchemy` 2.x, **Core only** | Pool and parameterized SQL; needed by Alembic |
| `alembic` | Migrations (raw-SQL revisions) |
| `psycopg[binary]` 3.x | Driver |
| `argon2-cffi` | Argon2id |
| `httpx` | Coinbase public client and test transport |
| `prometheus-client` | Metrics collector |

Also vendored: `htmx.min.js` (pinned checksum) and one hand-written CSS file. Standard library covers `hashlib`, `hmac`, `secrets`, `decimal`, `json`, `tomllib`, `zipfile`, `csv`, `uuid`, `logging`, `pathlib`, `os`, `subprocess`.

**Host tools (pinned by checksum, not Python deps):** `age` (backup encryption), `systemd` timers, `openssh` with a forced-command wrapper.

**Dev and CI only:** `pytest`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pip-audit`, `uv` or `pip-tools`, `coverage`, `pytest-timeout`. **Live-lab build only (CI):** one JWT library chosen at Phase 9.0.

**Must NOT be added:**
Redis, Celery, RQ, arq, Dramatiq, APScheduler · the SQLAlchemy **ORM** · `psycopg-pool`, PgBouncer, async drivers · Postgres extensions (`pgcrypto`, `pg_cron`, `citext`, `uuid-ossp`, `pg_partman`, `timescaledb`) · pandas, numpy, scipy, TA-Lib · dlt and dlthub tooling · any Coinbase SDK, `ccxt`, Coinbase CLI, MCP or agent tools · WebSocket libraries · HTMX extensions, any JS or CSS framework, any CDN · `passlib`, `authlib`, `fastapi-users`, `itsdangerous`, Starlette `SessionMiddleware`, PyJWT or `cryptography` in the paper image · Alertmanager, Loki, ELK, OpenTelemetry, Sentry · Adminer, pgAdmin, Portainer, Watchtower, cron containers, `docker-socket-proxy` · `python-dotenv`, `structlog`, `orjson`, `tenacity`, `psutil`, `uvloop`, `httptools`, `requests` · third-party Caddy modules.

### 3.5 Final repository tree

```
tradingdots/
├─ README.md  pyproject.toml  alembic.ini  Makefile
├─ requirements/{runtime.txt,dev.txt,live-lab.txt}          # hash-locked, generated
├─ config/{thresholds.toml,fees.example.toml,strategy/grid-v1.toml}   # inputs to `ctl register-config`; NOT read at runtime
├─ ci/{image_scan,secret_scan,protect_invariants,gen_transitions,gen_rules,function_inventory}.py
├─ tools/record_sandbox.py                                   # never in an image, never on the VPS
├─ deploy/
│  ├─ Dockerfile                        # targets: paper-prod, paper-staging, live-lab (CI only)
│  ├─ docker-compose.yml  env.example  approved-digests.txt   # reviewed file; changes need a deviation record
│  ├─ caddy/Caddyfile
│  ├─ postgres/{init/00-roles.sh,postgresql.conf,pg_hba.conf}
│  ├─ egress-proxy/                     # Phase 4.1
│  ├─ prometheus/{prometheus.yml,rules/}   grafana/{grafana.ini,provisioning/,dashboards/}
│  └─ host/{firewall.md,systemd/,backup/{wrapper,age-recipient.pub},second-host/{pull.sh,verify.py,watch.py}}
├─ docs/                                # exactly 14 files (below)
├─ src/tradingdots/
│  ├─ core/       config.py invariants.py build_identity.py(generated at image build) clock.py errors.py logging.py faults.py canonical.py numeric.py metrics.py
│  ├─ domain/     money.py transitions.py guards.py grid.py costs.py backtest.py policy_checks.py validation_rules.py proposal_policy.py clientid.py statusmap.py phrases.py
│  ├─ db/         engine.py uow.py rows.py queries/ commands/ sql/{functions/,triggers/,views/,grants/} migrations/
│  ├─ services/{shared,web_ops,worker_ops,batch_ops,intake_ops,ctl_ops}/
│  ├─ adapters/   coinbase_public.py coinbase_parse.py paper_gateway.py filestore.py
│  └─ web/  worker/  batch/  intake/  cli/            # composition roots; cli/ctl.py is the host CLI
├─ src/tradingdots_live_lab/            # CI-only from Phase 9: private adapter, strict request builders, JWT signer, live gateway
└─ tests/  (excluded from every image)
   ├─ unit/ integration/ security/ faults/ fakes/ mutants/
   ├─ fixtures/coinbase/{public_recorded,sandbox_static}
   └─ invariants/                       # protected directory
```

**The 14 documents (`docs/`):** `decisions.md`, `architecture.md`, `security-model.md`, `invariants.md`, `state-machines.md`, `db-roles.md`, `config-reference.md`, `runbook-operations.md`, `runbook-backup-restore.md`, `incident-response.md`, `exchange-contract.md` (source-class tagged, snapshot hashes), `package-and-proposal-spec.md`, `monitoring.md`, `host-supply-chain-evidence.md` (includes the Coinbase key policy).

### 3.6 Module ownership and dependency direction

**Direction.** `web | worker | batch | intake | cli` (entrypoints and composition roots, one DB role each) → their own `services/<role>_ops` plus `services/shared` → `db.queries` (read) and `db.commands` (write) and ports → `domain` → `core`. `adapters` depend only on `domain`, `core` and ports.

**Ownership matrix (binding)**

| Layer | Owns | Must not |
|---|---|---|
| API (`web/routes`) | Parse input, declare the access level, render, carry `expected_version` | Call SQL directly; hold business rules; decode uploads |
| Service (`*_ops`) | One use case = one transaction; orchestration; error translation | Compute money or guard rules; call the exchange inside a transaction |
| Repository (`db.queries` read, `db.commands` write) | Typed access; **one wrapper per SQL function; wrappers raise on denial** | Make decisions |
| Domain (pure) | Strategy, grid, costs, guard evaluation, validation, proposal triage, status map, client-ID derivation, phrases | I/O, SQL, time, money and exposure rules |
| Risk | Policy checks (spread, feed, fee feasibility) in Python producing a TTL-bound decision row; **money and exposure rules only in SQL** | Recompute reserve, cap or fee exposure in Python |
| Execution | A `Gateway` port; the paper gateway is deterministic and in-process; called only after `authorize_attempt` and `attempt_mark_submitted` | Decide anything |
| Exchange (`adapters/coinbase_*`) | HTTP, parsing, rate limits, DTOs; public paths only in the paper image | Business logic; DB access |

**Import allowlists (CI contracts; add a scratch forbidden import to prove they fail):**
1. `web` may import only `core`, `domain`, `db.queries`, `db.commands`, `services.shared`, `services.web_ops`, `adapters.filestore` (read).
2. `worker`: `core`, `domain`, `db`, `services.shared`, `services.worker_ops`, `adapters.coinbase_*`, `adapters.paper_gateway`.
3. `batch`: `core`, `domain`, `db`, `services.shared`, `services.batch_ops`, `adapters.filestore` (write).
4. `intake`: `core`, `domain.proposal_policy`, `db`, `services.shared`, `services.intake_ops`.
5. `cli`: `core`, `domain`, `db`, `services.shared`, `services.ctl_ops`.
6. The `services/*_ops` packages are mutually independent.
7. `domain` and `core` import no `sqlalchemy`, `fastapi`, `httpx`, `psycopg`, `pydantic` (except `core.config`).
8. Proposal, change-request and attestation queries are importable only by `web_ops.proposals` and `intake_ops`.
9. `tradingdots_live_lab` is never importable from `tradingdots`; the paper image holds no private endpoint constants, request builders, key loader or JWT signer.

**Transactions.** One transaction per use case, opened only in `services/*_ops` through one unit-of-work helper. The worker order flow uses **two transactions**: authorize and mark submitted (commit) → exchange I/O → record result (commit). A crash between them is resolved by RECOVERING and reconciliation. Denial events use an independent transaction.

**Lock order (documented and tested):** advisory pair lock → `bot_state` → `ledger_state` (by venue) → `pairs` (ascending id) → `worker_lease` → attempts and intents → `guard_status` → `runtime_config_active` → `audit_head` (last). `session_create` locks the user row first.

**Error model.** `db.commands` wrappers raise. Exception family: `Conflict`, `GuardFailed(reasons)`, `NotFound`, `Invalid`, `Forbidden`, `Unavailable` (transient: DB down, 429, timeout → NO_TRADE and backoff, **no latch**), `Invariant` (unexpected type or violated invariant → latching BLOCKED). A SQLSTATE mapping table translates 40001, 40P01, 55P03, 57014, 23505, 23514 and custom codes. The UI shows codes and plain text only, never internals.

**Worker.** One process, **one thread**: per-call HTTP timeout ≤ 3 s, an iteration budget, sentinel checks between calls. Lease TTL exceeds the maximum call budget plus margin. `stop_grace_period` set; SIGTERM stops after the current call. Heartbeat, guard writes and the metrics snapshot come from the same loop.

**FastAPI rules.** Routers are built by a factory that requires an access level (`PUBLIC`, `VIEWER`, `ADMIN`). Synchronous `def` handlers. Pure-ASGI middleware in a fixed order: request ID → size limits → Host check → Origin and Fetch-Metadata → session load → access. Authorization runs before any body parsing. `BackgroundTasks`, `/docs`, `/redoc`, `/openapi.json` disabled. Jinja2 autoescape and `StrictUndefined`. `|safe`, `Markup`, `hx-on`, data-derived `hx-*` values and `hx-swap-oob` are banned by lint. Thread limiter about 8; DB pool with `max_overflow=0`.

### 3.7 Docker, service and network architecture

**Services (10 long-running, 2 one-shot)**

| Service | Networks | DB role | Introduced | Memory limit | Notes |
|---|---|---|---|---|---|
| `caddy` | `edge_public`, `edge_app`, `edge_grafana` | none | 1 | 128 MB | **Only publisher (TCP 80, 443).** Grafana host has the second layer (IP allowlist if a stable source IP exists, else basic-auth from a secret file). |
| `app` (one uvicorn process) | `edge_app`, `backend`, `mon_scrape` | `td_web` | 1 | 768 MB | No egress. Metrics listener on a separate port bound to a static address. |
| `postgres` | `backend` | owners only | 1 | 2.5 GB | `max_connections` 40; `pg_hba` scram-only per role and network; `log_statement=none`. |
| `migrate` (one-shot) | `backend` | `td_migrator` | 1 | transient | Runs with `lock_timeout` while other services are stopped. |
| `worker` | `backend`, `mon_scrape`, `egress_int` | `td_worker` | 4.1 | 1 GB | Single-threaded. |
| `batch` | `backend`, `mon_scrape` | `td_batch` | 5 | 768 MB | Backtest, reports, packages, retention. No egress. |
| `intake` | `backend` | `td_intake` | 8 | 256 MB | Hostile proposal parsing. No egress. |
| `egress-proxy` | `egress_int`, `egress_ext` | none | 4.1 | 64 MB | Allows `api.coinbase.com` only; denies the CB-1 list. |
| `prometheus` | `mon_scrape`, `mon_query` | none | 3 | 1.5 GB | 15 s scrape and evaluation; 30 d and 15 GB retention; admin, lifecycle, remote-write off. |
| `grafana` | `edge_grafana`, `mon_query` | none | 3 | 512 MB | Own login. Anonymous, sign-up, public dashboards, snapshots, plugin install, embedding off; secret key from a file; dashboards non-editable; one read-only datasource; contact points empty; login limits on. |
| `node-exporter` | `mon_scrape` | none | 3 | 64 MB | Read-only mounts, no host network. |
| `cadvisor` (profile, off) | `mon_scrape` | none | 3 | 256 MB | Docker-only, no environment metrics. |
| `ctl` (profile `ops`, one-shot) | `backend` | `td_ctl` | 2 | transient | Host CLI, own secret. |

**Networks:** `edge_public` (caddy) · `edge_app` (caddy, app) · `edge_grafana` (caddy, grafana) · `backend` (app, worker, batch, intake, postgres, migrate, ctl) · `mon_scrape` (prometheus, app, worker, batch, node-exporter, cadvisor) · `mon_query` (prometheus, grafana) · `egress_int` (worker, egress-proxy) · `egress_ext` (egress-proxy). The app and Grafana share none. Static subnets for `mon_scrape` and the Caddy address (for `--forwarded-allow-ips`).

**DB roles (8):** `td_migrator`, **`td_fn` (NOLOGIN, sole owner of SQL functions)**, `td_web`, `td_worker`, `td_batch`, `td_intake`, `td_ctl`, `td_backup` (read-only). Created by the Postgres init script (secrets from files; the superuser secret is a file, unreachable from app networks). Connection limits: web 10, worker 4, batch 3, intake 3, ctl 3, backup 2, migrator 2, reserve 3. **All grants live in migrations.**

**Volumes and secrets**

| Item | Owner and access |
|---|---|
| `pgdata` | postgres |
| `packages` | batch writes, app reads (read-only mount) |
| `sentinel` (per-origin subdirectories, distinct uids, `O_NOFOLLOW`) | web writes `web.kill`, ctl writes `ctl.kill`; worker mounts read-only |
| `spool` | ctl and worker (audit fallback) |
| `tmpfs /tmp` (size-limited) | every service |
| `grafana-data`, `prometheus-data`, `caddy-data` | own service |
| Secrets (`/run/secrets`, files only, mounted **per service**) | role passwords, identity HMAC key, Grafana password and secret key, second-layer credential. **No Coinbase credentials.** |

**Container rules.** Non-root with distinct uids, `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem where feasible, `init: true`, memory, CPU and pids limits, healthchecks, log rotation, digest-pinned images, no `docker.sock`, no privileged containers. **The VPS holds images and configs only:** no repository checkout, no sandbox recorder.

**Caddy.** Admin API off; HTTP to HTTPS; staged HSTS (no preload); streaming body caps and write and idle timeouts; unknown Host rejected; `/metrics*`, `/internal/*`, `/docs`, `/openapi.json` return 404; masked-IP logs with credentials redacted **(verify)**; safe error pages; a larger body cap on the proposal-import route only. Security headers: CSP (with `form-action 'self'` and `frame-ancestors 'none'`), HSTS, `nosniff`, restrictive Referrer-Policy and Permissions-Policy, no CORS, no `Server` header.

**Second host.** Pulls encrypted backups and exported audit rows over a fixed forced-command SSH wrapper into append-only, versioned, retention-locked storage; replays the audit chain hourly and verifies anchor continuity; runs `ctl status` and notifies through the operator's own channel. It holds no VPS credentials beyond the restricted pull key and is not reachable from the VPS.

**Resource budget (6 vCPU, 12 GB RAM, 100 GB disk).** *Estimates only; Phase 10 measures them.*

| Memory limits | |
|---|---|
| Postgres | 2.5 GB |
| Prometheus | 1.5 GB |
| Worker | 1 GB |
| App, Batch | 768 MB each |
| Grafana | 512 MB |
| Intake | 256 MB |
| Caddy | 128 MB |
| Egress proxy, node-exporter | 64 MB each |
| **Total** | 7.5 GB (7.75 GB with cAdvisor); about 4.5 GB left for OS and page cache |

CPU limits are caps, not reservations; `batch` runs at lower priority than `worker`. Argon2id about 46 MiB, t = 2, p = 1, tuned on the VPS to about 150 ms; concurrency: login 2, reauth 1. At most 2 concurrent package downloads; kill and pause routes keep a reserved thread slice.

| Disk | Budget |
|---|---|
| OS and images | 15 GB |
| Prometheus (cap plus WAL; expected use far lower, about 1 GB) | 17 GB |
| PostgreSQL (includes proposal bytes) | 20 GB |
| Packages | 3 GB |
| Local backup staging (pull window) | 10 GB |
| Logs, spool, anchors | 6 GB |
| Grafana and Caddy data | 1 GB |
| **Planned** | about 72 GB, headroom about 28 GB |

**Growth.** Candles about 60 MB per year for 3 pairs (about 400 MB at 20 pairs; 25,920 rows per pair for the initial 90 days at 5 minutes). Metadata snapshots hold static rules only. Grid plans persist per activation or configuration, not per candle; policy decisions are written on change. Proposals ≤ 10 per hour, ≤ 20 per day, ≤ 200 MB of raw bytes total, purged past retention. Denial events are aggregated. Disk alerts at 70, 80, 90%; the risk engine returns NO_TRADE below a local free-disk threshold.

---

## PART 4. TD-BASELINE-2.0 (binding), part 2 of 3: behavior, data and interfaces

### 4.1 Dashboard pages and primary operator workflows

**Persistent banner on every page:** `MODE: BACKTEST|PAPER` and `LIVE TRADING: BLOCKED` (or `LIVE TRADING: UNKNOWN — TREATED AS BLOCKED`). It is informational, not a control. All times are labelled UTC. State labels are always prefixed ("Bot: PAUSED", "Pair: PAUSED").

**Navigation:** Overview · Bot · Pairs · Reports · LLM Review (ADMIN) · Audit (ADMIN) · Security · Grafana (external link only, no embeds).

| Page | Content | Access |
|---|---|---|
| Overview | One **status line** (`BLOCKED > UNKNOWN > ATTENTION > OK`; OK reads "No issues detected (paper)") with a CSS STALE overlay when not refreshed; an attention list deduplicated by cause, capped, severity-ordered, each item linking to its page. Tiles (each with a server timestamp): mode, bot state, active pair, protected reserve, deployed vs cap, grid status, data and metadata freshness, feed sanity, clock offset, reconciliation, breaker, kill switch, worker heartbeat, audit debt, ledger breach, active config hashes and fee review date (**operator-attested**), recent policy decisions, guard alerts, latest reports, package state, backup, anchor and second-host pull ages, Grafana link. Every value is real, `Unknown` or `Not available`; never invented. | VIEWER, ADMIN |
| Bot | State and mode, config hashes, grid plan summary, decisions, kill and breaker controls, pause, resume, mode, reconciliation and recovery; **static live-gate panel** ("NOT SATISFIED — requires the Phase 11 process") | View: both. Controls: ADMIN |
| Pairs | Pair list (state badges, exposure line); detail (each product flag, `data_basis`, alias, stable-base result, fee feasibility, evidence timestamps, history, allowed actions). A **separate paginated "Discovered products" section** lists products (never pairs). Candidate creation is by selecting a discovered product. | View: both. Actions: ADMIN |
| Reports | Backtest, paper daily and weekly, risk-event, data-quality, pair-lifecycle reports with ID, checksum, config hashes, snapshot IDs, limitations, fee scenario, "priced from unified USD book" | Both; generate: ADMIN |
| LLM Review | Feature state; packages (create, verify, download (POST), delete); proposals (import, list, plain-text detail with an "untrusted" banner, review, change request). VALIDATED reads "passed triage (heuristic, not an endorsement)". | ADMIN (VIEWER sees a state tile) |
| Audit | Filterable event list, chain and anchor status | ADMIN |
| Security | Own sessions, password, readiness checklist (read-only), backup, anchor, drill and contract-verification dates | Both; ADMIN: session revocation |
| Login, error pages | Uniform login errors; safe error pages with no internals | Public |

**No-JavaScript.** Every page is a full server-rendered route. HTMX only replaces read-only tiles from `/partials/*`. Partials never extend the idle timeout and return an HTMX redirect to login on expiry. Every mutating form carries `expected_version` (restrictive actions ignore it); a stale submission is denied and shows current state. No `<meta refresh>`. Post/Redirect/Get everywhere. HTMX config: `historyCacheSize=0`, `allowEval=false`, `selfRequestsOnly=true`, `hx-history="false"` on authenticated pages; `Cache-Control: no-store` on authenticated responses; `Clear-Site-Data` on logout.

**Workflows**

| ID | Workflow |
|---|---|
| W1 Daily | Open Overview; read the status line and attention list; follow links. |
| W2 Candidate to paper | `ctl seed-watchlist` (or select a discovered product); validation runs; if PAPER_ELIGIBLE, with the **bot PAUSED** the ADMIN activates the pair (two-step); then `RESUME BOT` (two-step). Frequent NO_TRADE from fee feasibility is normal. |
| W3 Emergency stop | Bot page kill (one click) or `tradingdots-ctl killswitch activate` on the host. Reset needs the full chain (a CLI kill resets only from the CLI). |
| W4 Trouble | Transient guards give NO_TRADE and clear; latching guards set the bot BLOCKED; ADMIN reads reason codes, clears the cause, retries recovery, then resumes. |
| W5 Stuck pair | Flatten (worker derives the order), write off dust, or abandon paper orders (paper-only typed actions); then disable and archive. |
| W6 LLM review loop | Enable packages, create, download, paste by hand into an LLM chat, save the JSON reply, import, triage, review, manual change request, human implements through the normal release, ADMIN records attested states. Nothing is automated. |
| W7 Users | Password change on Security; role change, disable and recovery only through the host CLI. |
| W8 Configuration | A release ships new config inputs; `ctl register-config`; with the bot PAUSED, `ctl config-activate`; the worker reloads at RECOVERING; `RESUME BOT`. Fees are re-registered before expiry. |
| W9 Backups | The second host pulls and verifies; a quarterly drill restores with anchor continuity and a wrong-key failure. |
| W10 Investigation | Pass the Grafana second layer and login; return to the main dashboard for any action. |
| W11 Documentation | Quarterly, re-verify `exchange-contract.md`; the date shows on Security. |

### 4.2 Pair lifecycle and complete transition rules

**Definitions**
- **Full chain:** authorization → CSRF and Origin → single-use reauth (challenge ≤ 120 s, bound to session, action, target, version) → server-derived exact ASCII phrase → audit, all verified **inside the DB function**. Two-step flow, no writes on GET.
- **Restrictive:** state first, audit after through the outbox.
- **CLEAN(pair):** no non-terminal attempts or intents; a fresh successful reconciliation; zero inventory, or DUST that has been written off.
- **GLOBAL_CLEAN:** CLEAN for every other pair.
- **PRODUCT_OK:** CB-3 on fresh, sane metadata.
- **VALID(pair):** a PASS validation run within its TTL: all checks PASS including PRODUCT_OK, `BASE_NOT_STABLE`, known `data_basis`, fee feasibility under the operator **and** stress fees with an unexpired `fees` config.
- **Common guards for activation and resume:** active config hashes match, `FEED_SANE`, `CLOCK_OK`, no `LEDGER_BREACH`, `audit_debt = 0`.

**Pair machine** (8 states in `pairs`; DISCOVERED lives on `products`; LIVE states not representable)

| # | Transition | Actor class | Guards | Chain | Audit |
|---|---|---|---|---|---|
| 0 | product ∅ → DISCOVERED | WORKER | Public, SPOT, venue CBE, pattern, ≤ 24 chars; discovery cap 500 | None | `product.discovered` (batched) |
| 1 | DISCOVERED → PROPOSED (pair row created) | WEB (ADMIN) or HOST (`seed-watchlist`) | Selected by product UUID; alias validation; ≤ 20 non-archived pairs (advisory lock); no existing non-archived pair for the product | CSRF | `pair.proposed` |
| 2 | PROPOSED → VALIDATING | WEB (ADMIN) or WORKER | Fresh metadata; validation rate limit | CSRF (ADMIN) | `pair.validation_started` |
| 3 | VALIDATING → RESEARCH_ONLY | WORKER | CAS on (state, version); evidence rows for this pair and version; a gating check failed or INCONCLUSIVE | None | `pair.research_only` |
| 4 | VALIDATING → PAPER_ELIGIBLE | WORKER | CAS; VALID(pair) evidence; DB-stamped receipts | None | `pair.paper_eligible` |
| 5 | RESEARCH_ONLY → VALIDATING | WEB (ADMIN) or WORKER | Rate limit | CSRF | `pair.validation_started` |
| 6 | PAPER_ELIGIBLE → VALIDATING | WORKER | TTL expiry, metadata change, or PRODUCT_OK lost | None | `pair.eligibility_expired` |
| 7 | PAPER_ELIGIBLE → PAPER_ACTIVE | WEB (ADMIN) | **Bot PAUSED**; mode PAPER; compile-time safety level ≥ 9 in production; VALID(pair); PRODUCT_OK; common guards; GLOBAL_CLEAN; kill off; breaker closed; unique-active index | Full: `ACTIVATE PAPER PAIR <PRODUCT_ID>` | `pair.activated_paper` |
| 8 | PAPER_ACTIVE → PAUSED | WEB (ADMIN, restrictive) or WORKER only when PRODUCT_OK is lost | None | Restrictive | `pair.paused` |
| 9 | PAUSED → PAPER_ACTIVE | WEB (ADMIN) | As row 7 plus a fresh PASS validation run, clean fresh reconciliation, no unknown orders | Full: `RESUME PAPER PAIR <PRODUCT_ID>` | `pair.resumed_paper` |
| 10 | PAUSED → PAPER_ELIGIBLE | WEB (ADMIN) | CLEAN(pair) | CSRF | `pair.deactivated` |
| 11 | PAUSED → VALIDATING | WEB (ADMIN) or WORKER | CLEAN(pair) | CSRF | `pair.validation_started` |
| 12 | Non-active → DISABLED | WEB (ADMIN) | Not PAPER_ACTIVE or ARCHIVED; CLEAN(pair) if ever traded | Full: `DISABLE PAIR <ID>` | `pair.disabled` |
| 13 | DISABLED → VALIDATING | WEB (ADMIN) | CLEAN(pair) | Full: `REENABLE PAIR <ID>` | `pair.reenabled` |
| 14 | Non-active → ARCHIVED | WEB (ADMIN) | CLEAN(pair); if ever active: paused and last reconciliation succeeded; records retained | Full: `ARCHIVE PAIR <ID>` | `pair.archived` |
| 15 | PAPER_ACTIVE → DISABLED or ARCHIVED | Denied | Must pause first | n/a | `pair.transition_denied` |
| 16 | ARCHIVED → any | Denied (terminal) | Re-adding creates a new linked candidate | n/a | `pair.transition_denied` |
| 17 | Any → LIVE_ELIGIBLE or LIVE_ACTIVE | Not representable | CHECK excludes | n/a | `pair.transition_denied` |

Only one pair may be `PAPER_ACTIVE` (unique index on the constant expression where state is `PAPER_ACTIVE`). Adding a pair creates a candidate only. Pair switching requires the bot PAUSED. No LLM content triggers any transition.

**Paper-only side actions** (full chain; none exist for any live venue):
- `FLATTEN PAPER LEDGER FOR <ID>`: creates a `PAPER_FLATTEN` work request with typed columns (`pair_id`, `request_id` only). Pair PAUSED, fresh data, no unknown orders. The worker alone derives a SELL-only intent bounded by inventory, priced from the current book.
- `WRITE OFF PAPER DUST FOR <ID>`: only for DUST (below `base_min_size` or `quote_min_size` value); posts a `WRITEOFF` ledger entry.
- `ABANDON PAPER ORDERS FOR <ID>`: only when PRODUCT_OK is lost or cancels fail; attempts become `VOIDED_ADMIN`, ledger released.

**Product-unfit protocol.** When PRODUCT_OK is lost the worker blocks new intents for the pair, pauses the pair (row 8), initiates cancels where the product state permits, and leaves failures CANCEL_REQUESTED or UNKNOWN.

**Bot machine and mode** (cross-column CHECK: `RUNNING` implies kill off, breaker CLOSED, mode PAPER)

| # | Transition | Actor class | Guards | Chain |
|---|---|---|---|---|
| B1 | process start → RECOVERING | WORKER | Always | None |
| B2 | RECOVERING → PAUSED | WORKER | Paper reconciliation clean; flag and sentinels consistent; active config hashes loaded | None |
| B3 | RECOVERING → BLOCKED | WORKER | Any failure or ambiguity | None |
| B4 | PAUSED → RUNNING | WEB (ADMIN) | Mode PAPER; a PAPER_ACTIVE pair exists; kill off, no sentinel; breaker closed; `audit_debt` 0; clean fresh reconciliation; no `LEDGER_BREACH` or `LEDGER_INCONSISTENT`; loaded config hashes equal the active ones | Full: `RESUME BOT` |
| B5 | RUNNING → PAUSED | WEB (ADMIN), HOST, or WORKER (kill or config change) | None | Restrictive |
| B6 | RUNNING or PAUSED → BLOCKED | WORKER | Latching guard, or an `Invariant` error | Restrictive |
| B7 | BLOCKED → RECOVERING | WEB (ADMIN) | Cause noted as cleared | CSRF |
| M1 | BACKTEST → PAPER | WEB (ADMIN) | Bot PAUSED; no open attempts; clean reconciliation; compile-time safety level ≥ 9 in production | Full: `SET BOT MODE PAPER` |
| M2 | PAPER → BACKTEST | WEB (ADMIN), completed by WORKER | Pauses immediately; flips when clean | CSRF |

The bot **never** enters RUNNING automatically, including after restart, rollback or restore. **Config activation** (`ctl config-activate`) requires the bot PAUSED; a mismatch mid-run pauses the bot.

**Kill switch.** The DB flag is authoritative. The origin (WEB or CTL) comes from the latest `KILL_ON` row in `control_events`. A per-origin sentinel file is written first as an outage fallback. The worker treats any sentinel, the flag, or an unreadable DB as blocked. Known orders are cancelled where safe (per-order results); the bot goes PAUSED; holdings are **never** sold. Reset needs full chain `RESET KILL SWITCH` (web) or the audited host CLI; a CLI-originated kill resets only from the CLI. **Breaker:** opens automatically (loss, drawdown, repeated API failure) or by ADMIN (restrictive); closing needs full chain `RESET CIRCUIT BREAKER`; no auto-resume.

**Guards.** TRANSIENT (NO_TRADE, lease expires by itself): DATA_FRESH, META_FRESH, `FEED_SANE`, PRODUCT_OK, DISK_OK, SPREAD_OK, CLOCK_OK, FEE_FEASIBLE. LATCHING (BLOCKED or a latch, ADMIN clears): KILL, BREAKER, RECON_CLEAN, ORDER_STATUS_KNOWN, LEDGER_OK, `LEDGER_BREACH`, `LEDGER_INCONSISTENT`, RECOVERY_COMPLETE, UNEXPECTED_ERROR (`Invariant` only), FILL_ANOMALY, BALANCE_MISMATCH. Operational (no trading effect): AUDIT_DEBT (blocks permissive actions), WORKER_HEARTBEAT, BACKUP_FRESH, ANCHOR_FRESH, SECOND_HOST_FRESH, CONTRACT_FRESH, FEES_FRESH.

**Order attempt machine** (created only by `authorize_attempt`; a trigger over `allowed_transitions` enforces it and freezes terminal states)

| # | Transition | Actor class | Rule |
|---|---|---|---|
| A1 | ∅ → AUTHORIZED | `authorize_attempt` | Policy decision row unexpired with matching `inputs_hash`, consumed once; gate row written by the function; bot RUNNING, kill off, breaker closed, evidence fresh, fence current; commitment posted with worst-case fee; per-order cap and reserve pass |
| A2 | AUTHORIZED → SUBMITTED | WORKER (`attempt_mark_submitted`) | Compare-and-set on (state, fence), committed before any I/O |
| A3 | AUTHORIZED → VOIDED | WORKER | Same fence; stop or guard change before submit; commitment released |
| A4 | SUBMITTED → WORKING | WORKER | PENDING, OPEN, QUEUED, EDIT_QUEUED |
| A5 | SUBMITTED → FAILED | WORKER | Definitive documented rejection (incl. post-only reject) |
| A6 | SUBMITTED → UNKNOWN | WORKER | Timeout, ambiguity, mismatched returned order |
| A7 | WORKING → CANCEL_REQUESTED | WORKER | Cancel initiated, or CANCEL_QUEUED |
| A8 | CANCEL_REQUESTED → CANCELLED | WORKER | Exchange-terminal; exposure released only now |
| A9 | WORKING → FILLED, CANCELLED, EXPIRED | WORKER | Exchange-terminal |
| A10 | CANCEL_REQUESTED → FILLED | WORKER | Race |
| A11 | Any non-terminal → UNKNOWN | WORKER or recovery | Unrecognized status, `UNKNOWN_ORDER_STATUS`, ambiguity, **or an older fence at recovery (never VOIDED)** |
| A12 | UNKNOWN → WORKING, FILLED, CANCELLED, EXPIRED, FAILED | Reconciliation service | On evidence only |
| A13 | UNKNOWN → SUBMITTED | Reconciliation service | **Paper venue only**, bounded count |
| A14 | UNKNOWN, WORKING, CANCEL_REQUESTED → VOIDED_ADMIN | WEB (ADMIN) | **Paper only**, via `ABANDON PAPER ORDERS FOR <ID>` |

Terminal: FILLED, CANCELLED, EXPIRED, FAILED, VOIDED, VOIDED_ADMIN. Paper "reconciliation" compares ledger integrity, attempts and fills.

### 4.3 LLM Review Package lifecycle and transition rules
*(States proposed because the Master Contract defines none.)* Package building runs only in `batch`, from typed export views.

| # | Transition | Actor class | Guards | Chain | Audit |
|---|---|---|---|---|---|
| F1 | Feature DISABLED → ENABLED | WEB (ADMIN) | Retention days stated; `audit_debt` 0 | Full: `ENABLE READ-ONLY REVIEW PACKAGES` | `review.enabled` |
| F2 | Feature ENABLED → DISABLED | WEB (ADMIN) | None | CSRF (restrictive) | `review.disabled` |
| P1 | ∅ → REQUESTED | WEB (ADMIN) | Enabled; period and safe scope; ≤ 10 retained; none building; rate limit | Full: `CREATE READ-ONLY REVIEW PACKAGE` | `review.requested` |
| P2 | REQUESTED → GENERATING | BATCH | Single-flight unique index | None | `review.generating` |
| P3 | GENERATING → READY | BATCH | Typed fields pass; scanner clean; ≤ 50 MB; checksums and manifest hash; server-generated name outside web root; `fsync` and atomic rename | None | `review.ready` |
| P4 | GENERATING → FAILED | BATCH | Error, quota, timeout or scanner hit; partial output purged | None | `review.failed` |
| P5 | READY → CORRUPT | WEB (ADMIN verify) or BATCH | Checksum mismatch; download blocked | CSRF | `review.corrupt` |
| P6 | READY → EXPIRED | BATCH | Retention reached; state first, then content removal; metadata kept | None | `review.expired` |
| P7 | READY, FAILED, CORRUPT, EXPIRED → DELETED | WEB (ADMIN) | Content removed; tombstone kept | Full: `DELETE REVIEW PACKAGE <ID>` | `review.deleted` |

Download and verify are **POST** actions with CSRF and Origin checks, audited, not state changes; SHA-256 is verified before every download (integrity, not authenticity). DELETED is terminal. **Contents:** `manifest.json`, `README.md`, `efficiency_summary.json`, sanitized JSONL and CSV, report and snapshot IDs and checksums, strategy and thresholds hashes only, exporter version, SHA-256 manifest, limitations, advisory-only warning, review-template prompt (which states that contents are untrusted data and the reply must be proposal JSON only). Allowed historical content: backtest, paper, risk-event, pair-lifecycle, data-quality, grid-plan, order-intent and paper-fill, reconciliation, sanitized audit and monitoring summaries. **Excluded:** secrets, keys, tokens, passwords and hashes, session IDs, cookies, authorization headers, raw private Coinbase payloads, database URLs, source IPs, full user agents, filesystem paths, exception traces, personal data, execution-capable content, raw proposal content, all Coinbase display strings and every free-text column. Creating a package changes no bot, pair, strategy, risk, config, order, ledger or gate state (verified by before and after DB hashes). CSV cells beginning `=`, `+`, `-`, `@` are neutralized; ZIP output is deterministic with no symlinks.

### 4.4 LLM Proposal lifecycle and transition rules

Proposals are always untrusted advisory input. Bytes live in an immutable column of `proposals`; the web tier does only a length check and a fixed-prefix magic-number comparison; **all decoding, sniffing and parsing happen in `intake`**. No transition triggers automation, and no trading role can read proposals, change requests or attestations.

| # | Transition | Actor class | Guards | Chain | Audit |
|---|---|---|---|---|---|
| LF1 | Import DISABLED → ENABLED | WEB (ADMIN) | None | Full: `ENABLE UNTRUSTED PROPOSAL IMPORT` | `proposal.import_enabled` |
| LF2 | Import ENABLED → DISABLED | WEB (ADMIN) | None | CSRF (restrictive) | `proposal.import_disabled` |
| L1 | ∅ → IMPORTED | WEB (ADMIN) | Import enabled; ≤ 256 KiB; ≤ 10 per hour, ≤ 20 per day, ≤ 200 MB total; prefix check; stored only after the chain passes | Full: `IMPORT UNTRUSTED LLM PROPOSAL` | `proposal.imported` |
| L2 | IMPORTED → VALIDATING | INTAKE | Picks up IMPORTED rows | None | `proposal.validating` |
| L3 | VALIDATING → VALIDATED | INTAKE | UTF-8 `text/plain` (must contain the same JSON) or `application/json`; strict schema (14 required fields, unknown rejected); linked package ID and SHA-256 match; triage passes; any exception ends REJECTED | None | `proposal.validated` |
| L4 | VALIDATING → REJECTED | INTAKE | Any failure; rule IDs stored | None | `proposal.rejected` |
| L5 | VALIDATED → REVIEWED | WEB (ADMIN) | Notes recorded | CSRF | `proposal.reviewed` |
| L6 | REVIEWED → CHANGE_REQUEST_CREATED | WEB (ADMIN) | Impact assessment; `PARAMETER_CHANGE` ceilings-unaffected attestation | Full: `CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <PROPOSAL_ID>` | `proposal.change_request_created` |
| L7 | CHANGE_REQUEST_CREATED → IMPLEMENTED | WEB (ADMIN, attested) | Release reference; nothing verified | CSRF + reauth | `proposal.implemented` |
| L8 | IMPLEMENTED → BACKTESTED | WEB (ADMIN, attested) | Backtest report IDs exist | CSRF + reauth | `proposal.backtested` |
| L9 | BACKTESTED → PAPER_VALIDATED | WEB (ADMIN, attested) | Paper report IDs meet minimum duration | CSRF + reauth | `proposal.paper_validated` |
| L10 | PAPER_VALIDATED → CLOSED | WEB (ADMIN) | None | CSRF | `proposal.closed` |
| L11 | REJECTED, REVIEWED, CHANGE_REQUEST_CREATED → CLOSED | WEB (ADMIN) | Reason recorded | CSRF | `proposal.closed` |

No backward transitions. CLOSED is terminal; a resubmission is a new import. Raw bytes of CLOSED or REJECTED records are purged past retention through a purge function the immutability trigger permits (hash and metadata kept). **Rejected content:** archives, PDF, DOCX, XLSX, CSV, images, HTML, JavaScript, YAML, XML, scripts, executables and everything unapproved (server-side sniffing; extension and MIME never trusted alone; archives never extracted; nothing executed, evaluated, compiled, templated or unsafely deserialized; never rendered as HTML). **Required proposal fields (14):** `proposal_version`, `linked_review_package_id`, `linked_review_package_sha256`, `category` (strategy, risk, pair_research, data, backtest, paper_execution, monitoring, documentation, code_quality), `summary`, `evidence_references`, `assumptions`, `suggested_change`, `expected_benefit`, `risk_tradeoffs`, `required_validation`, `rollback_plan`, `should_remain_no_trade_until_validated`, `no_profit_guarantee`. **Triage** rejects any proposal that requests or implies: direct exchange orders, cancellation or replacement, secret or credential changes, security-control changes, bypass of risk, reserve, cap, reconciliation or live gate, pair activation, deletion, disablement or archival, automatic capital increases, automatic config, code, database, shell, file or network actions, or profit guarantees. Triage is heuristic, not a judge of intent. `register-config` and `runtime_configs` have no proposal-linking fields.

### 4.5 ADMIN / VIEWER authorization matrix
Default deny. Unauthenticated users reach only login, logout and static assets. Users are created only through the host CLI.

| Capability | ADMIN | VIEWER |
|---|---|---|
| View Overview, Bot, Pairs (incl. discovered products, validation detail), Reports | ✔ | ✔ |
| View kill switch, breaker, reconciliation state | ✔ | ✔ |
| Select a discovered product as candidate, run validation, pause pair | ✔ | ✖ |
| Activate, resume, disable, re-enable, archive pair; paper flatten, dust write-off, abandon orders | ✔ | ✖ |
| Pause bot, kill switch on, switch to BACKTEST | ✔ | ✖ |
| Resume bot, reset kill switch, reset breaker, switch to PAPER, retry recovery | ✔ | ✖ |
| Generate report | ✔ | ✖ |
| LLM Review pages, package and proposal actions (incl. package download) | ✔ | ✖ (state tile only) |
| Audit page | ✔ | ✖ |
| Own sessions and own password change | ✔ | ✔ |
| Revoke another user's sessions | ✔ | ✖ |
| Readiness checklist (read-only) | ✔ | ✖ |
| Grafana link | ✔ | ✔ |
| Create or disable users, change roles, recover passwords, `register-config`, `config-activate`, `seed-watchlist` | Host CLI only | Host CLI only |
| Edit strategy, risk, capital, threshold or fee config | Not available (release plus registration) | Not available |
| `/metrics`, Prometheus, internal health | Not exposed | Not exposed |
| Any live-trading action | Not available | Not available |

### 4.6 Sensitive-action confirmation matrix
Full chain (authorization, CSRF and Origin, single-use target-bound reauth, server-derived exact ASCII phrase with no normalization, audit) is verified **inside the DB function**. Two-step flow. Failed reauth is throttled and audited.

| Action | Role | Reauth | Typed phrase |
|---|---|---|---|
| Enable review packages | ADMIN | ✔ | `ENABLE READ-ONLY REVIEW PACKAGES` + retention days |
| Create package | ADMIN | ✔ | `CREATE READ-ONLY REVIEW PACKAGE` + period and scope |
| Delete package | ADMIN | ✔ | `DELETE REVIEW PACKAGE <ID>` |
| Disable packages, disable proposal import | ADMIN | none (restrictive) | none |
| Download or verify package (POST) | ADMIN | none (CSRF and Origin only) | none |
| Enable proposal import | ADMIN | ✔ | `ENABLE UNTRUSTED PROPOSAL IMPORT` |
| Import proposal | ADMIN | ✔ | `IMPORT UNTRUSTED LLM PROPOSAL` |
| Create manual change request | ADMIN | ✔ | `CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <PROPOSAL_ID>` + impact assessment |
| Record IMPLEMENTED, BACKTESTED, PAPER_VALIDATED | ADMIN | ✔ | none |
| Activate / resume pair | ADMIN | ✔ | `ACTIVATE PAPER PAIR <PRODUCT_ID>` / `RESUME PAPER PAIR <PRODUCT_ID>` |
| Disable / re-enable / archive pair | ADMIN | ✔ | `DISABLE PAIR <ID>` / `REENABLE PAIR <ID>` / `ARCHIVE PAIR <ID>` |
| Paper flatten / dust write-off / abandon orders | ADMIN | ✔ | `FLATTEN PAPER LEDGER FOR <ID>` / `WRITE OFF PAPER DUST FOR <ID>` / `ABANDON PAPER ORDERS FOR <ID>` |
| Pause pair or bot, kill switch on, mode → BACKTEST | ADMIN | none | none (restrictive) |
| Resume bot | ADMIN | ✔ | `RESUME BOT` |
| Reset kill switch / reset breaker | ADMIN | ✔ | `RESET KILL SWITCH` / `RESET CIRCUIT BREAKER` |
| Switch mode to PAPER | ADMIN | ✔ | `SET BOT MODE PAPER` |
| Retry recovery | ADMIN | none | none |
| Revoke another user's sessions | ADMIN | ✔ | `REVOKE SESSIONS FOR <USERNAME>` |
| Change own password | Any | ✔ (current password) | none |
| Kill switch, user admin, `register-config`, `config-activate`, `seed-watchlist` from the host | Host admin | n/a | none (audited through the spool) |
| Strategy, threshold or fee edits from the dashboard; any live-mode action | Not available | | |

**Phase 11 prerequisite (SD-5):** step-up, preferably a host approval (`ctl approve`) verified outside the web process, for ACTIVATE, RESUME BOT, SET MODE, RESET KILL SWITCH and RESET CIRCUIT BREAKER.

### 4.7 Database: entities, relationships, constraints, indexes

**Conventions.** A = append-only (trigger), I = immutable, M = mutable, E = ephemeral (purged), S = singleton. Timestamps `timestamptz`, receipts DB-stamped. Money is `NUMERIC` with CHECKs on scale (≤ 18) and magnitude. Every FK is `ON DELETE RESTRICT`. State columns use CHECK lists (never lookup tables), so LIVE values cannot exist. `td_web` and `td_batch` read **views only** (owned by `td_fn`). Nobody has DELETE on business tables.

**SQL functions (≤ 24, ≤ 80 lines each, one file each, each header names the invariant it protects; all owned by `td_fn` with pinned `search_path` and `REVOKE ALL FROM PUBLIC`; each has a grant test):** `session_create`, `session_end` (revoke/touch), `challenge_create`, `pair_transition`, `bot_transition` (includes kill, pause, mode, breaker), `package_transition`, `proposal_transition`, `attempt_transition`, `transition_core` (internal), `authorize_attempt`, `authorize_preview`, `attempt_mark_submitted`, `ledger_post`, `guard_set`, `work_request_create`, `work_request_lease`, `audit_emit` (internal), `audit_record_denial`, `audit_drain`, `user_admin` (`td_ctl`), `config_apply` (register and activate), `retention_purge`. Two slots spare.

| Domain | Entity (class) | Key columns, relations, constraints | Required indexes |
|---|---|---|---|
| Foundation | `schema_meta` (S) | version, head, minimum code version | none |
| | `bot_state` (S/M) | mode CHECK `BACKTEST\|PAPER`; state CHECK `RECOVERING\|RUNNING\|PAUSED\|BLOCKED`; `kill_flag`; breaker; version; **CHECK: RUNNING implies kill off, breaker CLOSED, mode PAPER** | none |
| | `allowed_transitions` (I) | (machine, from, to, actor_class), chain level, guard codes; seeded by a generator; immutable after seeding | pk |
| | `runtime_configs` (I) | kind CHECK `thresholds\|fees\|strategy`, `sha256`, `body` jsonb (typed, parsed), `registered_at` (fees body holds `reviewed_at`); `runtime_config_active` (one row per kind) | unique `sha256` |
| | `ops_status` (S) | backup, backup verify, anchor, audit verify, restore drill, contract verification, last status pull (written by `td_ctl`) | none |
| Auth | `users` (M) | role CHECK `ADMIN\|VIEWER`, `password_hash`, `disabled_at`; never deleted | unique `lower(username)` |
| | `sessions` (E) | `token_hash` (SHA-256 of a 256-bit token), csrf secret, `identity_hmac`, `last_seen_at` (throttled), `absolute_expires_at`, `revoked_at`; **function-only writes**; cap 5 per user | unique `token_hash`; (`user_id`) where live |
| | `confirmation_challenges` (E) | bound to (session, action, target, version); `expires_at`, `consumed_at`; **function-only**; ≤ 5 per minute per session | unique (`session_id`,`action`) where unconsumed |
| | `idempotency_keys` (E), `login_throttle` (E) | key, session, action; (scope, key_hmac, window) | (`created_at`); (`window_start`) |
| Audit | `audit_event_types` (I) | code, class, allowed actor classes | pk |
| | `audit_events` (A) | `seq`, `event_id`, code, actor, target, result, `request_id`, hashes, `payload_text` ≤ 4 KiB (canonical JSON), `prev_hash`, `event_hash` | unique `event_id`; (`event_code`,`seq`); (`target_type`,`target_id`,`seq`) |
| | `audit_head` (S), `audit_outbox` (E), `audit_export_marks` (A) | head written only by `audit_emit`; outbox has `occurred_at`, `outbox_id`, row cap; hourly marks embed the previous anchor hash | (`drained_at`) where null |
| Pairs | `products` (M) | pattern CHECK, length ≤ 24, base, quote, type, discovery rank (top 500 by the documented volume order) | pk; (`discovered_rank`) |
| | `product_metadata_snapshots` (A) | **static rules only** (increments, min and max sizes, flags, status, type, venue, `alias`, `alias_to`; no price, volume or spread); new row only on hash change; sanity CHECKs | unique (`product_id`,`sha256`) |
| | `product_metadata_current` (M) | snapshot, DB-stamped `last_verified_at`, `server_time_offset_ms` | pk |
| | `pairs` (M) | 8-state CHECK, version, `data_product_id`, `data_basis` | unique `product_id` where not ARCHIVED; unique `(true)` where PAPER_ACTIVE |
| | `pair_state_history` (A), `pair_validation_runs` (A) | history keyed (`pair_id`,`version_after`); run holds `checks` jsonb, `thresholds_sha256`, TTL; **written only by `td_worker`** | (`pair_id`,`started_at` desc) |
| Runtime | `worker_lease` (S) | owner, `fence_token`, heartbeat, crash count, reported config hashes | none |
| | `guard_status` (M) | guard code, status, class, detail, `since`, **`valid_until`** (expired reads UNKNOWN) | pk |
| | `work_requests` (E) | **typed columns, no jsonb**: kind CHECK `BACKTEST\|REPORT\|PAPER_FLATTEN\|RECONCILE_NOW`; per-kind typed key columns (`config_id`,`snapshot_id`; `report_type`,`period`; `pair_id`,`request_id`); state, lease, attempts. **No column can carry price, size, side or product.** | unique active per (kind, key); (`state`,`run_after`) |
| Ledger | `ledger_state` (per venue) | cash, committed, inventory; CHECKs on **commitment** columns only (`cash_committed ≤ 35`, per-order cap); derived, **no direct DML**; nightly rebuild | pk `venue` |
| | `ledger_positions` (M, derived) | pk (`venue`,`pair_id`), base quantity, committed; CHECK committed ≤ quantity | pk |
| | `ledger_entries` (A) | kind `DEPOSIT\|COMMIT_BUY\|RELEASE_BUY\|COMMIT_SELL\|RELEASE_SELL\|FILL_BUY\|FILL_SELL\|FEE\|WRITEOFF`, amounts, `attempt_id`, **`entry_key` NOT NULL UNIQUE**; single 50 USDC DEPOSIT per venue | unique `entry_key`; (`pair_id`,`id`); unique DEPOSIT per venue |
| Orders | `order_intents` (I) | increment and minimum CHECKs (values **copied by the function from the referenced snapshot**, never caller-supplied); `venue CHECK ('PAPER')`; `purpose GRID\|FLATTEN` | unique (`grid_plan_id`,`level_index`,`cycle_no`,`side`) |
| | `risk_decisions` (A) | `intent_id`, decision ALLOW/REJECT/NO_TRADE, reason codes, **`expires_at` (DB-stamped ≤ 30 s), `inputs_hash`, `consumed_by_attempt_id`**; written on change of reason set | (`intent_id`); (`at` desc) |
| | `gate_decisions` (A) | **written only inside `authorize_attempt`** from constants | (`intent_id`) |
| | `order_attempts` (M) | state CHECK (UNKNOWN, VOIDED_ADMIN included); UUID `client_order_id`; fence; **no role has INSERT**; transitions by trigger over `allowed_transitions`; terminal states immutable | unique `client_order_id`; unique `intent_id` where non-terminal; (`state`) where non-terminal |
| | `order_events` (A), `fills` (A) | `exchange_fill_id NOT NULL`, `liquidity_indicator` | unique (`venue`,`exchange_fill_id`) |
| | `reconciliation_runs`, `control_events`, `recovery_runs` (A) | typed detail only; `control_events` kinds KILL_ON/OFF, BREAKER, PAUSE, RESUME, MODE_CHANGE, CONFIG_REGISTERED/ACTIVATED with source WEB/CLI/SYSTEM | (`finished_at` desc) |
| Market data | `candles` (A) | pk (`product_id`,`granularity`,`start_ts`); `CHECK high >= low`; ingested only for PROPOSED-or-later pairs | pk |
| | `candle_conflicts`, `ingest_runs` (A), `dataset_snapshots` (I) | snapshot = range + `max_ingest_run_id` + hash | unique (product, granularity, range, bound, hash) |
| Strategy | `grid_plans` (I) | pair, strategy config id, snapshot, thresholds hash, `levels CHECK 3..5`, feasibility under operator and stress fees; **per activation or configuration, not per candle** | (`pair_id`,`created_at` desc) |
| | `backtest_runs` (I), `reports` (I) | identity includes engine version and thresholds hash; typed bodies | unique (`config_id`,`snapshot_id`,`thresholds_sha256`,`engine_version`) |
| Packages | `feature_flags` (M) | key CHECK, retention days; absent row means disabled | pk |
| | `review_packages` (M) | 7-state CHECK; server-generated `storage_key` pattern CHECK; size ≤ 50 MB; manifest jsonb | unique `storage_key`; unique `(true)` where REQUESTED or GENERATING |
| Proposals | `proposals` (M) | 11-state CHECK; 9-category CHECK; **immutable `raw_bytes` ≤ 256 KiB** (purge permitted after retention), `sha256`, sniffed type, linked package id and SHA-256, `policy_results` jsonb, `parameter_change` | (`state`,`imported_at` desc); (`linked_package_id`); unique `sha256` where active |
| | `proposal_state_history` (A), `change_requests` (I), `proposal_attestations` (A) | one change request per proposal; attested release references pattern-limited; **unreadable by trading roles** | unique `proposal_id` on change requests |
| Views | `overview_v`, `pairs_v` (renders DISCOVERED for products without a pair), `bot_v`, `reports_v`, `packages_v`, `audit_v`, `work_v`, `export_*_v` (typed, no free text) | read models for `td_web` and `td_batch` | n/a |

**Relationship spine:** `products → pairs → grid_plans → order_intents → order_attempts → order_events and fills → ledger_entries → ledger_state and ledger_positions`. `runtime_configs` feed `grid_plans` and `backtest_runs`; `dataset_snapshots` feed both. `review_packages ← proposals → change_requests, proposal_attestations, proposal_state_history`.

**Triggers and constraints summary:** append-only triggers (audit, history, events, fills, ledger entries, candles, snapshots); immutability triggers (intents, configs, reports, allowed transitions, terminal attempts); attempt-machine trigger; ledger derived-state trigger; cross-column CHECKs (bot state, intent increments, candle high/low, metadata sanity); LIVE values excluded by every CHECK list. Edits to `allowed_transitions` or the `bot_state` CHECKs require a deviation record.

**Purge and retention.** `login_throttle` and `idempotency_keys` 2 d; `confirmation_challenges` 1 d; expired `sessions` 7 d after expiry; drained `audit_outbox` and finished `work_requests` 7 d; packages ≤ 10 retained, 1 GB total, ≤ 50 MB each, state-first retention with `fsync` and atomic rename and an orphan sweeper; proposal bytes purged past retention. Audit events are never deleted in Phase 0 (archive by export).

### 4.8 High-level API endpoint groups

There is **no public or third-party JSON API**. Every route is server-rendered HTML or an HTML fragment. Sensitive actions use a `/request` then `/confirm` POST pair. Every mutating route is POST, CSRF and Origin checked, idempotency-keyed, `expected_version` bearing (except restrictive actions), and Post/Redirect/Get.

| Group | Routes (prefix) | Methods | Access |
|---|---|---|---|
| Auth | `/login`, `/logout` | GET, POST | Public / authenticated |
| Overview and partials | `/`, `/partials/status`, `/partials/tiles` | GET | VIEWER, ADMIN. Read-only; no session touch. |
| Bot | `/bot`; `/bot/pause`, `/bot/kill`, `/bot/mode/backtest`, `/bot/recovery/retry`; `/bot/resume/{request,confirm}`, `/bot/mode/paper/{request,confirm}`, `/bot/kill/reset/{request,confirm}`, `/bot/breaker/reset/{request,confirm}` | GET, POST | View: both. Actions: ADMIN |
| Pairs | `/pairs`, `/pairs/products` (paginated), `/pairs/{id}`; `/pairs/candidates` (POST product UUID), `/pairs/{id}/{validate,pause}`; `/pairs/{id}/{activate,resume,disable,reenable,archive,flatten,writeoff,abandon}/{request,confirm}` | GET, POST | View: both. Actions: ADMIN |
| Reports | `/reports`, `/reports/{id}`, `/reports/generate` | GET, POST | Both; generate: ADMIN |
| Review packages | `/review`; `/review/enable/{request,confirm}`, `/review/disable`; `/review/packages/create/{request,confirm}`, `/review/packages/{id}`, `/review/packages/{id}/verify` (POST), `/review/packages/{id}/download` (**POST**), `/review/packages/{id}/delete/{request,confirm}` | GET, POST | ADMIN |
| Proposals | `/review/proposals`, `/review/proposals/{id}`; `/review/proposals/import-enable/{request,confirm}`, `/review/proposals/import-disable`; `/review/proposals/import/{request,confirm}` (multipart at confirm); `/review/proposals/{id}/{review,change-request/{request,confirm},advance,close}` | GET, POST | ADMIN |
| Audit | `/audit`, `/audit/status` | GET | ADMIN |
| Security | `/security`, `/security/password`, `/security/sessions/revoke-own`, `/security/sessions/revoke-user/{request,confirm}` | GET, POST | Both; revoke-user ADMIN |
| Static | `/static/*` | GET | Public |
| Not routed | `/metrics` (separate port on `mon_scrape` only), `/docs`, `/redoc`, `/openapi.json`, health | n/a | Never public |

There is no route that calls Coinbase and no route that reaches the execution gateway. The web process performs only a length and fixed-prefix check on uploads. Cookies: `__Host-` prefix, Secure, HttpOnly, SameSite=Strict, Path=/, no `Domain`; a pre-session signed CSRF token protects the login form.

### 4.9 Configuration ownership

| Class | Owner | Where | Dashboard-editable? |
|---|---|---|---|
| Hard ceilings (50, 15, 35, levels 3 to 5, one active pair, per-order cap bounds, package and upload maxima, fee sanity bounds) | Maintainer, release only | `core/invariants.py` plus DB CHECKs | No |
| Build identity (safety level, venue set, gateway class, prod vs staging) | Maintainer | `core/build_identity.py`, generated at image build | No |
| Thresholds (freshness, staleness, clock, spread, loss, drawdown, per-order cap, reconciliation age, disk, costs incl. **stress fee**) | Operator, via release | Input `config/thresholds.toml` → `ctl register-config` → `runtime_configs` kind `thresholds` (immutable, hashed) → `ctl config-activate` | No |
| Fees (maker, taker, optional tax, `reviewed_at`) | Operator | `runtime_configs` kind `fees`; expiry per DX-3; shown as operator-attested | No |
| Strategy presets | Maintainer, via release | `runtime_configs` kind `strategy` | No |
| Runtime settings (hostnames, environment name, pool sizes, log level) | Operator | `env.example` (non-secret), `pydantic-settings`, `extra="forbid"` | No |
| Secrets | Operator | `/run/secrets/*` files, mounted per service | No |
| Feature flags (`review_packages`, `proposal_import`) | ADMIN | DB | Yes (chains per 4.6) |
| Bot mode, kill, breaker | ADMIN, host admin | DB | Yes (chains per 4.6) |
| Coinbase endpoint and field allowlists, path constants | Maintainer, via release | Code (`adapters/`, live-lab) | No |
| Approved image digests | Maintainer, via deviation-recorded review | `deploy/approved-digests.txt` | No |
| Caddy, Compose, Prometheus, Grafana provisioning | Maintainer | `deploy/` | No |
| Prometheus rules | Generated from thresholds | `deploy/prometheus/rules/` (drift-tested) | No |
| Exchange-contract facts and snapshot hashes | Maintainer | `docs/exchange-contract.md` | No |
| Coinbase credentials | **None exist** in Phases 0 to 10 | n/a | n/a |

**Fixed defaults:** session idle 30 min, absolute 12 h, at most 5 sessions per user, at most 5 challenges per minute per session, reauth challenge ≤ 120 s, password ≥ 14 characters with a trivial-value denylist (no composition rules), DB statement timeouts (web 10 s, worker 60 s), lock timeout 3 s, idle-in-transaction 10 s, Coinbase public ceiling 5 req/s, closed-candle lag 2 s, candidate cap 20, discovery cap 500, policy decision TTL ≤ 30 s, exact-phrase comparison is an ASCII byte match with no trimming, case folding or normalization.

---

## PART 5. TD-BASELINE-2.0 (binding), part 3 of 3: observability, schedule, tests, operations, blockers

### 5.1 Prometheus metric catalogue

Publish **only where reliable source data exists**; an absent source means an absent series, never zero. The worker's loop writes the DB snapshot; the collector emits **monotonic DB-derived counters** (no reset on restart) and omits a series when the snapshot is stale. The metrics listener binds to the static `mon_scrape` address. Labels come from an enumerated allowlist clamped to `other`, and never contain secrets, keys, account, user or session IDs, usernames, emails, IPs, user agents, URLs, paths, timestamps, raw errors, stack traces, order or client IDs.

| Metric (required by the Master Contract) | Type | Labels | Phase |
|---|---|---|---|
| `tradingdots_bot_info` | gauge (=1) | `version`, `mode`, `safety_kernel_level` | 3 |
| `tradingdots_bot_last_successful_tick_timestamp_seconds` | gauge | none | 5 |
| `tradingdots_bot_data_freshness_seconds` | gauge | none | 5 |
| `tradingdots_bot_product_metadata_age_seconds` | gauge | none | 5 |
| `tradingdots_bot_reconciliation_age_seconds` | gauge | none | 5 |
| `tradingdots_bot_reconciliation_mismatches_total` | counter | none | 5 |
| `tradingdots_bot_risk_rejections_total` | counter | `reason_class` | 5 |
| `tradingdots_bot_circuit_breaker_state` | gauge | none | 5 |
| `tradingdots_bot_kill_switch_active` | gauge | none | 5 |
| `tradingdots_bot_open_orders` | gauge | none | 5 |
| `tradingdots_bot_order_intents_total` | counter | `result` | 5 |
| `tradingdots_bot_order_events_total` | counter | `event_type` | 5 |
| `tradingdots_bot_deployed_quote` | gauge | none | 5 |
| `tradingdots_bot_protected_reserve_quote` | gauge (free cash) | none | 5 |
| `tradingdots_bot_realized_pnl_quote` | gauge | none | 5 |
| `tradingdots_bot_unrealized_pnl_quote` | gauge (only with a fresh mark) | none | 5 |
| `tradingdots_bot_drawdown_ratio` | gauge | none | 5 |
| `tradingdots_bot_daily_loss_quote` | gauge | none | 5 |
| `tradingdots_bot_grid_cycles_total` | counter | none | 5 |
| `tradingdots_bot_fee_paid_quote_total` | counter | none | 5 |
| `tradingdots_bot_api_requests_total` | counter | `endpoint_class`, `status_class` | 4 |
| `tradingdots_bot_api_errors_total` | counter | `endpoint_class`, `error_class` | 4 |
| `tradingdots_pair_candidates_total` | counter | `source` | 4 |
| `tradingdots_pair_state_total` | gauge (contract name kept; type recorded in decisions) | `state` | 4 |
| `tradingdots_llm_review_packages_total` | gauge | `state` | 7 |
| `tradingdots_llm_review_package_enabled` | gauge | none | 7 |
| `tradingdots_llm_proposals_total` | gauge | `state`, `category` | 8 |
| `tradingdots_llm_proposal_policy_rejections_total` | counter | `category` (no rule IDs) | 8 |

**Allowed operational additions:** `tradingdots_api_schema_drift_total{endpoint_class}`, `tradingdots_bot_clock_offset_seconds`, `tradingdots_bot_fill_anomalies_total`, `tradingdots_worker_heartbeat_timestamp_seconds`, `tradingdots_guard_ok{guard}`, `tradingdots_ledger_breach`, `tradingdots_ledger_inconsistent`, `tradingdots_audit_debt_rows`, `tradingdots_backup_last_success_timestamp_seconds`, `tradingdots_audit_last_verified_timestamp_seconds`, `tradingdots_anchor_last_export_timestamp_seconds`, `tradingdots_exchange_contract_verified_timestamp_seconds`, `tradingdots_second_host_last_pull_timestamp_seconds`, `tradingdots_fees_reviewed_timestamp_seconds`, `tradingdots_work_request_queue_depth{kind}`, `tradingdots_http_requests_total{route_template,status_class}`, `tradingdots_auth_events_total{event}`, `tradingdots_db_pool_in_use`. Infrastructure metrics come from `node-exporter` (and cAdvisor if enabled).

### 5.2 Grafana dashboard catalogue

All dashboards are provisioned as code, **non-editable** (`editable: false`, `allowUiUpdates: false`, `disableDeletion: true`), on one read-only Prometheus datasource, with no bot-control panels, no embedding, no action links.

| # | Dashboard | Panels |
|---|---|---|
| G1 | Operations overview | Guard status grid, bot info and mode, kill switch, breaker, worker heartbeat, audit debt, ledger flags, backup, anchor, second-host pull and contract-verification age |
| G2 | Paper trading diagnostics | Open orders, intents and events, fills, fill anomalies, grid cycles, deployed vs reserve, P&L, drawdown, daily loss, fees |
| G3 | Data and exchange | Data freshness, metadata age, clock offset, reconciliation age and mismatches, Coinbase public API request and error rates, schema drift, ingest results, work-request depth |
| G4 | Security and access | Auth events, login failures, throttle activity, HTTP status classes, session counts, audit chain verification age |
| G5 | LLM review and proposals | Package states, feature flags, proposal states by category, policy rejections |
| G6 | Host and containers | CPU, memory, disk per volume, Prometheus TSDB size vs cap, target status, container metrics (if cAdvisor is on) |

### 5.3 Alert catalogue

Rules are generated from the registered `thresholds` config (`ci/gen_rules.py`, drift-tested), are **observational only**, and never control the bot, pause it, change pair state or call Coinbase. The dashboard attention list uses the same guard vocabulary from the DB. Rules on subsystem metrics use `absent_over_time`. Severity set: critical, high, warn, info; the second-host watcher notifies for critical and high.

| Group | Alerts |
|---|---|
| **Infra** | AL-01 TargetDown; AL-02 DiskUsage (70/80/90%); AL-03 MemoryPressure; AL-04 CpuSaturation; AL-05 TsdbNearCap (≥ 80% of 15 GB); AL-11 WorkerHeartbeatAbsent; AL-27 WorkRequestBacklog; AL-28 WorkRequestFailures |
| **Data and exchange** | AL-13 DataStale; AL-14 MetadataStale; AL-25 IngestFailures; AL-26 ApiErrorRate; AL-33 SchemaDrift; AL-34 ClockOffset; AL-36 ExchangeContractStale (> 100 d); AL-44 FeesReviewExpiring (< 5 d) |
| **Trading safety** | AL-12 TickStale; AL-15 ReconciliationStale; AL-16 ReconciliationMismatch; AL-17 KillSwitchActive (informational); AL-18 BreakerOpen; AL-19 BotBlocked; AL-20 UnknownOrderStatus; AL-21 RiskRejectionSpike; AL-22 DrawdownLimit; AL-23 DailyLossLimit; AL-24 DiskLowNoTrade; AL-35 FillAnomaly; AL-40 LedgerBreachOrInconsistent; AL-41 PairStuckUnclean; AL-42 GuardLeaseExpired; AL-39 ConfigMismatch |
| **Security** | AL-29 AuthFailureSpike; AL-30 ThrottleGlobalActive; AL-37 UnusualPermissiveActions; AL-43 NewClientIdentity |
| **Integrity and backup** | AL-06 BackupStale (> 26 h); AL-07 AnchorExportStale; AL-08 AuditVerifyFailedOrStale; AL-09 RestoreDrillOverdue (> 100 d); AL-10 AuditDebt; AL-38 SecondHostPullStale |
| **LLM** | AL-31 PackageFailed; AL-32 ProposalRejectionSpike |

### 5.4 Phase-by-phase implementation schedule

**Order is fixed.** No phase starts before the previous review gate (RG) passes. Each increment is one git tag `td-<phase>.<n>`, independently testable and revertible. Migrations are expand-only. The failing test files for the criteria an increment covers exist **before** the code. Effort is not estimated.

| Phase | Increments | New services | Depends on | Gate | Rollback point |
|---|---|---|---|---|---|
| **1** Foundation, Docker, Caddy | 1.0 decisions, host baseline · 1.1 skeleton, ceilings, numeric rules, lints, `protect_invariants`, import allowlists, function-inventory skeleton, build identity · 1.2 Compose (caddy, app, postgres, migrate), per-service secrets · 1.3 eight roles incl. `td_fn`, grants, schema guard (both directions), `pg_hba`, DB timeouts, `bot_state` cross-column CHECK · 1.4 Caddy routing, headers, 404s, body caps, log-redaction samples · 1.5 landing page, gate-derived banner · 1.6 image targets, approved-digest deploy check, image and secret scans, evidence tooling, RG-1 | caddy, app, postgres, migrate | DEC-000, RG-0, B-1, B-7 | RG-1 | `td-1.6`; teardown safe |
| **2** Auth, sessions, audit, dashboard | 2.1 users, `ctl` · 2.2 sessions and challenges via functions, password policy, CSRF and Origin, throttling · 2.3 default-deny routes and inventory · 2.4 privilege model (`session_user` actor map, token-hash sessions, per-machine wrappers over `transition_core`, evidence-ownership grants) · 2.5 audit (catalogue, denial events, outbox, completeness test) · 2.6 `runtime_configs`, `config_apply`, `ctl register-config`, `ctl config-activate` · 2.7 pull-based encrypted backups, exported audit rows, hourly chained anchors, second-host verifier and watcher, `ctl doctor`, restore drill 1 | ctl | Phase 1, B-2, B-3, AS-S5, AS-B1 | RG-2 | `td-2.7`; restore from dump |
| **3** Monitoring | 3.1 Prometheus, node-exporter · 3.2 Grafana hardening and second layer · 3.3 metrics collector, generated rules · 3.4 cAdvisor profile (off) · 3.5 Overview integration | prometheus, grafana, node-exporter | 1, 2, B-8 | RG-3 | `td-3.5`; stop monitoring |
| **4** Pairs | 4.1 worker (single-threaded), egress proxy, public client (five paths, parsing policy, token bucket, server-time offset) · 4.2 products, pairs, venue-scoped ledger and intent and attempt tables (empty), `work_requests`, grants · 4.3 discovery (cap 500), candidate by product UUID, alias validation, metadata sanity, `seed-watchlist` · 4.4 Pairs UI, actions, activation ordering, race tests | worker, egress-proxy | 2, 3, AS-C1, AS-C5 | RG-4 | `td-4.4` |
| **5** Data, strategy, backtest, paper | 5.1 ingestion, `FEED_SANE`, closed-candle rule · 5.2 snapshots, strategy, backtest (in `batch`), fee stress · 5.3 safety kernel (kill state, sentinels, host kill, outbox, config-mismatch pause) · 5.4 `authorize_attempt`, policy decision rows with TTL, idempotent fills, facts-versus-checks ledger, `LEDGER_BREACH`, nightly rebuild · 5.5 paper broker, paper reconciliation, worker-derived flatten, dust write-off, abandon flow · 5.6 Bot page, RG-5B | batch | 4, AS-3, AS-C2, B-4, DX-3 | RG-5A, RG-5B | `td-5.6`; none below 5.3 while orders or inventory exist |
| **6** Reports, integration | 6.1 report builders · 6.2 Overview and Bot integration (UTC, STALE overlay, `expected_version`) · 6.3 alert rules · 6.4 accessibility and no-JS | none | 3, 5 | RG-6 | `td-6.4` |
| **7** Review packages | 7.1 flag, typed export views · 7.2 sanitizer, scanner · 7.3 builder (`batch`) · 7.4 storage, verify, POST download, retention · 7.5 UI, metrics | none | 2, 5, 6, B-9 | RG-7 | `td-7.5`; disable feature |
| **8** Proposal import | 8.1 `intake` container and role · 8.2 streaming upload with length and prefix check · 8.3 schema, link check, triage · 8.4 review, change requests, plain-text rendering, hostile-corpus run | intake | 2, 5, 6, 7 | RG-8 | `td-8.4`; disable import |
| **9** Safety machinery | 9.0 exchange-contract verification (AS-C3, C4, C5, C7, C8; every ungathered doc item; sandbox recording per DX-1) · 9.1 full risk engine · 9.2 breaker, kill switch, notification · 9.3 reconciliation and recovery (CB-8 to CB-11, absence-proof design) · 9.4 fake exchange with provenance labels · 9.5 live-lab build (CI only): allowlisted adapter, strict request builders, JWT signer, key-permission checks · 9.6 drills, incident tabletop, RG-9 | none | 5 to 8, B-6, DX-1, DX-2 | RG-9 | `td-9.6`; kill ON, stop worker |
| **10** Paper deployment review | 10.1 30-day soak after RG-9 · 10.2 drills, evidence · 10.3 review record | none | 1 to 9, B-5 | RG-10 | Return to BACKTEST |
| **11** Live-pilot review | 11.1 written review incl. fee tier and account-level Preview Order evidence (view-only key only with written approval), step-up, stronger anchors, absence proof · 11.2 blocked-or-separate-release decision | none | 10, contract amendment, owner approval, SD-5 | RG-11 | n/a |

**Review gates** (each needs operator-supplied evidence and a Security Engineer re-review of the phase diff): RG-0 decisions, redacted host baseline, second host confirmed · RG-1 Phase 1 checklist (5.9) · RG-2 auth, privilege, audit, backup, anchors, `ctl doctor`, restore drill · RG-3 monitoring isolation, Grafana second layer · RG-4 pairs, activation ordering, races, AS-C1 record · RG-5A backtest reproducibility, feed sanity, AS-C2 record · RG-5B safety kernel, authorization, ledger, recovery · RG-6 dashboard vs DB vs metric · RG-7 package canaries · RG-8 hostile corpus · RG-9 safety machinery, contract-verification record, tabletop · RG-10 soak, drills, scan · RG-11 signed review and a separate approved unblock change.

**Phase-level acceptance and per-phase test, documentation, migration, rollback and maintenance detail** follows the increments above and the rules in Parts 3 and 4. For each phase, at minimum: unit, integration, security and fault-injection tests for its scope (5.5), the documentation it touches, expand-only migrations, a tagged rollback point, and an explicit statement of why live trading remains blocked (5.10).

### 5.5 Mandatory tests

**Rules for all tests (BI-37).** Real Postgres with real login roles. Time-based logic tested through `*_impl(p_now)` **and** the production wrappers with a real clock. Crash safety tested by SIGKILL of processes, DB-side kills and lost commit acknowledgments (not exceptions). SQL invariants covered by scripted mutants (`tests/mutants/`). Fakes cover network I/O only and cite a primary source or carry an ASSUMPTION or RECORDED-STATIC label, which never counts as evidence. A test comparing two implementations by the same author is not evidence. Grant tests compare real privileges against an allowlist file that is independent of the migrations. The paper image contains no test code. **Write failing tests first.** Nothing counts as passed without real output.

**Per-phase minimum (topic areas from the scenario catalogue below):**

| Phase | Mandatory test areas |
|---|---|
| 1 | Identity and live-block; grant matrix and function inventory; import allowlists; numeric parsing; schema guard both directions; settings fail-closed; headers, 404s, external port scan, container inspection, log-redaction samples, secret scan, image scan |
| 2 | Privilege model; sessions and challenges; CSRF and Origin (incl. from the Grafana origin); default-deny inventory; audit catalogue completeness, denial events, outbox, tamper detection; runtime configs; backup, anchors, second-host replay, `ctl doctor`; restore drill with wrong-key and broken-chain cases |
| 3 | Monitoring isolation and hardening; metrics label clamp and staleness; rule drift; Grafana second layer |
| 4 | Pair lifecycle (exhaustive transition matrix against the DB); product injection; races; activation ordering; public client (five paths, SSRF, parsing, token bucket); metadata sanity; stable-base and data-basis; egress allow and deny |
| 5 | Ingestion and closed-candle rule; feed sanity; snapshots and reproducibility; authorization, ledger and idempotency; kill state; recovery and fencing; paper broker; dust, flatten and abandon flows; config-hash enforcement; growth simulation |
| 6 | Dashboard vs DB vs metric; no-JS and stale marker; `expected_version`; XSS and `hx-*` lint |
| 7 | Package canaries, typed views, retention, tamper, kill during build, POST download, IDOR |
| 8 | Hostile corpus, `intake` crash isolation, caps and purge, plain-text rendering, proposal containment |
| 9 | Create Order response table, duplicate-ID mismatch, cancel semantics, reconciliation windows and balances, allowlist unrepresentability, JWT claims, fake-exchange provenance, SIGKILL matrix, incident tabletop |
| 10 | Full repeat of every suite, restore drill, Postgres and disk drills, external scan, secret-canary scan, anchor verification, resource measurement |
| 11 | No code. A future unblock change needs its own full suite. |

**Scenario catalogue (authoritative; historical T-/TF- IDs map onto these):**

*Identity and live-block:* try to set LIVE via env, config, SQL, insert of a LIVE pair, migration replay, or importing a live gateway; hostile env vars, config and DB rows naming other safety levels or venues have no effect; deploy refuses an unapproved digest and a mislabelled image; image scan finds no private paths, builders or signer; a second venue cannot post to a shared ledger.
*Privilege model:* `td_web` cannot write evidence, sessions, challenges, ledger, order or fill tables; forged actor values do not exist as parameters; UUID-only sessions rejected; fuzz function string arguments and `search_path`; function inventory (count, size, owner, no dynamic identifiers); grant matrix on real login roles.
*Order path:* stale or replayed ALLOW denied; ALLOW with mismatched `inputs_hash` denied; worker-inserted gate row impossible; one ALLOW cannot authorize two attempts; guard TTL expiry blocks; hung loop reads stale; two workers race on fencing (old fence cannot mark SUBMITTED); older-fence pre-fill attempts become UNKNOWN; SIGKILL at every crash point and DB-side kill after commit before acknowledgment yield exactly-once outcomes; scripted mutants of `authorize_attempt` (drop reserve, drop worst-case fee, drop per-order cap, allow non-positive size, skip fence) each break a test.
*Ledger:* reserve, cap and per-order cap at authorization with worst-case fee; facts never reject and latch `LEDGER_BREACH`; fills idempotent on exchange fill ID; `entry_key` replays are no-ops for every entry kind; nightly rebuild flags seeded drift; hypothesis stateful tests; order sizes independent of realized P&L; no capital-growth path.
*Pairs:* candidate creation only from a discovered product UUID; oversize and homoglyph IDs rejected; cross-asset alias gives UNKNOWN data basis; stable base fails; discovery cap and pair cap under concurrency; races (activate vs disable, archive vs validation, two activations, activation vs product flag flip); activation and resume require the bot PAUSED and a fresh PASS run; pair switching requires PAUSED; dust write-off and abandon-orders flows are paper-only and audited; product-unfit protocol; archive denied while unclean.
*Data and metadata:* frozen feed, future or non-monotonic candles, clock skew, worker-supplied timestamps ignored, metadata sanity bounds, closed-candle rule with lag, 350-candle pagination, candle conflicts recorded, snapshot stable after a late-filled gap, schema drift counted, numeric parsing (strings, JSON numbers, NaN, exponent, precision).
*Exchange (fake plus recorded shapes):* Create Order response table (success with `UNKNOWN_FAILURE_REASON` is accepted; definitive rejections FAILED; ambiguous UNKNOWN); returned "existing order" mismatching the intent blocks; cancel with mixed per-order results, `CANCEL_QUEUED`, unknown exchange IDs, foreign orders never cancelled, batches split at 100; reconciliation with date windows, default sort only, duplicate pages, missing `client_order_id` fallback blocks; balance check per currency; taker fill on a post-only order raises `FILL_ANOMALY`; allowlist: Convert, Portfolios, Edit, Close Position, INTX, CFM, payment methods unrepresentable; key-permission fixtures with `can_transfer=true`, missing `can_trade`, INTX portfolio fail the gate check; JWT claims per request; 429 storms respect ceilings; sandbox fixtures carry RECORDED-STATIC and never count as behavioral evidence.
*Web:* default-deny route inventory; CSRF and Origin for every mutating route incl. the Grafana origin; fixation and cookie replay; session cap and challenge limits; login flood while an ADMIN reauths; `expected_version` stale-form denial; no GET writes; POST download with CSRF; no-JS rendering and STALE overlay; partials never extend idle timeout; XSS and attribute injection lint; headers and `no-store`; log samples free of cookies, authorization headers, full IPs, statements.
*Uploads and proposals:* hostile corpus (archives, polyglots, HTML/JS, template strings, bidi, duplicate keys, NaN, deep nesting, huge fields, chunked and slow bodies, multipart abuse); length and prefix check only in web; `intake` crash leaves other tables unchanged; caps and purge under the immutability trigger; import allowlists per query module; `register-config` rejects proposal references.
*Packages:* canary secrets never reach output; typed export views; tamper leads to CORRUPT and blocks download; kill during build leaves no partial file; retention state-first; IDOR and unauthenticated download denied.
*Audit, backup, migration:* every side-effecting route, CLI command, migration, restore, retention and housekeeping action maps to a cataloged event; denial events aggregated; tamper detection; second-host replay detects a rewritten prefix and a forged anchor; wrong key, tampered dump and broken anchor chain fail restore; `migrate` respects `lock_timeout`; old code refuses a newer head and vice versa; dump and restore preserve function ownership and grants; config pointer rollback; rollback never resumes trading.
*Monitoring and ops:* stop Prometheus, Grafana and the second host in turn: trading and dashboard unaffected and the watcher alerts; metrics stale series omitted; Grafana anonymous, sign-up, public dashboards, snapshots, plugins off and provisioned dashboards non-editable; `/metrics` variants 404 on both hosts; network isolation (app and Grafana share none; app has no egress); rule and transition-seed drift tests; `ctl doctor` detects seeded misconfigurations.
*Growth:* a 30-day simulated run at 5-minute cadence keeps every table within budget.

### 5.6 Backup, restore and rollback strategy

**Backed up.** PostgreSQL (a daily custom-format `pg_dump`, plus one before every migration; proposals are in the dump); package files with checksum manifests (retention re-applies on restore); the Grafana volume (dashboards are also code); the Caddy data volume; secrets (stored separately). **Not** backed up: Prometheus.

**Rules.** Backups are encrypted to an **offline recipient key** (`age`; public key on the VPS, private key offline). The `td_backup` role is read-only. The second host **pulls** through a fixed forced-command wrapper (takes no arguments; root-equivalent on the VPS if mis-written, so reviewed and tested) into append-only, versioned, retention-locked storage: 7 daily and 4 weekly. **Anchors are hourly**, chained (each embeds the previous anchor's hash), with exported audit rows so the second host **replays the chain** and checks that the prefix is unchanged. `ctl daily` records timestamps in `ops_status`. Targets (unmeasured): RPO 24 h, RTO 4 h.

**Restore.**
1. Keep every service stopped; use a scratch host with no egress and no credentials.
2. Recreate cluster roles first, then restore the DB **preserving function ownership and grants** (no `--no-owner`).
3. Restore packages and re-apply retention.
4. Check the schema version against the code head (both directions).
5. Verify the audit chain and package checksums against the second host's anchors.
6. Revoke all sessions.
7. Set kill ON and the bot PAUSED.
8. Start the app, then the worker.
9. The bot passes RECOVERING; only an ADMIN can resume.

A wrong key, a tampered dump or a broken anchor chain **must fail** the restore. **Drills:** after Phase 2, after each schema-changing phase, before Phase 10, quarterly.

**Deploy and rollback (BI-35).** Pause or kill → stop app, worker, batch, intake → pre-migration dump → `migrate` with `lock_timeout` → start; the bot comes up PAUSED. The schema guard refuses both older and newer schemas. Migrations are raw-SQL Alembic revisions tagged `backward_compatible`; function versions change only by expand/contract; edits to `allowed_transitions` or the `bot_state` CHECKs need a deviation record. **Before any migration applies:** redeploy the previous tag. **After a migration and trading:** roll forward; restoring a pre-migration dump is allowed only within the same maintenance window with its data-loss window stated in advance. **Configuration rollback** is a pointer switch to a previously registered immutable `runtime_configs` row with the bot PAUSED (audited). Every rollback boots with kill ON, bot PAUSED, mode BACKTEST.

### 5.7 Maintenance playbook

| Cadence | Task |
|---|---|
| Daily | Overview: status line and attention list. The second host verifies anchors and backups, runs `ctl status`, and notifies. `ctl daily` runs on its systemd timer (housekeeping, export marks, audit verify, backup verify, `ctl doctor`). |
| Weekly | Review reports and limitations notes; check disk and TSDB size; review failed work requests and pending packages or proposals; run the Coinbase public smoke test and read the schema-drift counter; confirm the fee review date is not near expiry. |
| Monthly | Image and dependency review (`pip-audit`; digest bumps recorded in `approved-digests.txt` through the review process); Caddy, Grafana (CVE watch) and Prometheus patching; certificate expiry; user access review; threshold and fee-assumption review; HTMX checksum re-verification; host and second-host patches. |
| Quarterly | Restore drill (anchor continuity and wrong-key cases); kill-switch and restart drills; external port scan; invariant-test and function-inventory review; **re-verify `exchange-contract.md` against the live Coinbase documentation** (source class, URL, date, snapshot hash; rate limits, key algorithm, scopes, sandbox scope, cutover date); review the deviations log and `decisions.md`. |
| Annually and before Phase 10 | Incident tabletop (compromise, credential found, audit mismatch, duplicate orders); password rotation review. |
| Per release | `make ci` evidence; migration dump; tag; expand/contract check; independent review of the diff (or a self-review record under the deviation rule for non-security changes); `decisions.md` updated for any deviation. |

### 5.8 Incident response outline

Paper phases risk no client funds, but data, secrets and host integrity still matter. **Severity:** SEV1 suspected compromise, audit or anchor mismatch, or a credential found anywhere; SEV2 duplicate, unknown or mismatched orders, reconciliation failure, ledger breach, data-loss risk; SEV3 degraded monitoring, stale data, disk warnings. **Common steps:** detect, contain (kill switch first), preserve redacted evidence, eradicate, recover through the normal recovery path, review, record in `decisions.md` if a baseline change follows.

| Trigger | First actions |
|---|---|
| IR-1 Suspected compromise | Kill switch (web or host); revoke all sessions (`ctl`); rotate DB role passwords, HMAC and signing keys, Grafana password and secret key, second-layer credential; compare the audit head to the second host's anchors; snapshot; restore clean if integrity is uncertain. |
| IR-2 Audit or anchor mismatch | Treat as IR-1; do not delete rows; use the second host's history. |
| IR-3 Credential found anywhere | SEV1; revoke at Coinbase; remove; record as a baseline violation. |
| IR-4 Duplicate, unknown, mismatched or fill-anomalous orders | The bot is BLOCKED by design; no resubmission; reconcile through the service; abandon only through the paper-only flow. |
| IR-5 `LEDGER_BREACH` or `LEDGER_INCONSISTENT` | Bot BLOCKED; do not edit the ledger; resolve through the documented write-off, flatten or rebuild-and-compare flows. |
| IR-6 Config hash mismatch | Bot BLOCKED; activate the intended registered config (bot PAUSED) or redeploy the registered image. |
| IR-7 Data or metadata anomaly | NO_TRADE stays; inspect candle conflicts, drift and the clock offset; re-run the public smoke test; re-verify the relevant Coinbase pages. |
| IR-8 Disk full | NO_TRADE; free logs and old staging backups; never delete business rows. |
| IR-9 Monitoring or second-host outage | The status line shows UNKNOWN or the watcher alerts; trading is unaffected; restart and re-verify the anchor history. |
| IR-10 Malicious proposal or package content | Disable import; confirm trading tables unchanged; keep the content inert. |
| IR-11 Upgrade failure | Stop; follow the roll-forward rules. |
| IR-12 Host loss | Restore on a new host per 5.6. |

### 5.9 Final Phase 1 acceptance checklist

Every item needs operator-supplied, redacted evidence (or your own real command output for anything you run locally). Nothing counts until supplied. **Phase 1 contains no auth, worker, batch, intake, proxy, monitoring, second-host tooling or Coinbase call.**

1. DEC-000 (CI-1 to CI-15) recorded; B-1 and B-7 decided; the deviation-review mechanism (`review: <name|SELF>`; security-invariant deviations prohibited without an independent reviewer) recorded; host baseline reviewed (SSH key-only, no root login, default-deny firewall, IPv6, unattended upgrades, CAA, chrony); second host confirmed to exist (RG-0).
2. Repository matches 3.5 for Phase 1 files; hash-locked dependencies; `make ci` runs ruff, mypy, pytest, import-linter, pip-audit, secret scan, function inventory.
3. `core/invariants.py` holds the ceilings and fee sanity bounds; the protected `tests/invariants/` exists; `ci/protect_invariants.py` blocks joint edits and requires a deviation record for changes to invariants, security docs or `approved-digests.txt`.
4. Lints ban float in money code, pickle, eval, unsafe YAML, `|safe`, `Markup`, `hx-on`, data-derived `hx-*`, `hx-swap-oob`, `BackgroundTasks`, direct clock calls.
5. All import-allowlist contracts of 3.6 exist and pass; a scratch forbidden import fails; role packages are mutually independent.
6. Settings fail closed (missing secret, unknown setting, mode LIVE); no `LIVE_*` setting; `SecretStr` loaded from files. `core/numeric` fixes one Decimal context and rejects `NaN`, `Infinity`, exponent forms and excess scale.
7. **Build identity is a compile-time constant**; separate `paper-prod`, `paper-staging` and `live-lab` targets; the deploy script verifies the image **digest against `approved-digests.txt`** and a host role marker and refuses everything else; the image scan finds no live-lab code, key loader, private endpoint constants, request builders or JWT signer.
8. Eight DB roles exist (`td_fn` NOLOGIN owns functions; seven login roles with connection limits); the grant-matrix test passes with real login roles; the function inventory test passes (≤ 24 functions, ≤ 80 lines each, invariant header present, no dynamic identifiers, no actor parameter). The Postgres superuser secret is a file, unreachable from app networks.
9. Migration `0001` creates `schema_meta` and `bot_state` (CHECKs excluding LIVE, mode restriction, **RUNNING cross-column CHECK**); the schema guard refuses both older and newer schemas.
10. All images pinned by digest; no Coinbase SDK, dlt or agent tooling; runtime dependencies match 3.4.
11. Compose defines caddy, app, postgres, migrate only; only Caddy has `ports:` (TCP 80 and 443); secrets mounted per service; distinct uids; static subnets; `init: true`; size-limited tmpfs; no `docker.sock`, no privileged containers; the VPS holds images and configs only.
12. The app has no outbound route (evidence: an attempt fails); external scan shows only 80 and 443 (IPv4 and IPv6); `DOCKER-USER` rules documented.
13. Containers run non-root with `cap_drop: ALL`, `no-new-privileges`, resource limits, healthchecks, log rotation, read-only root filesystem where feasible.
14. Postgres: `pg_hba` allows only `scram-sha-256` per role and per source network (no `trust`, no superuser from app networks); `log_statement` and `log_connections` off; role DB timeouts set.
15. Caddy routes both hostnames, rejects unknown Host, streams body caps with write and idle timeouts, returns 404 for `/metrics*`, `/internal/*`, `/docs`, `/openapi.json`; the Grafana host returns a safe 503; **log samples from Caddy and Postgres show no cookies, authorization headers, full IPs or statements**.
16. Header set present on both hosts (CSP with `form-action` and `frame-ancestors`, staged HSTS, `nosniff`, Referrer-Policy, Permissions-Policy, no CORS, no `Server` header).
17. The landing page banner derives from the gate state, with `UNKNOWN — TREATED AS BLOCKED` if unreadable; times labelled UTC.
18. Evidence redaction script and secret-canary scan exist and are clean; the scan rejects `secrets.toml` and key-material patterns.
19. `docs/` contains the 14 files: `exchange-contract.md` seeded with CF-1 to CF-17, source classes and open items AS-C1 to AS-C8; `incident-response.md` seeded from 5.8; `decisions.md` seeded from this file.
20. Failing test files exist for the grant matrix, function inventory, session-write path, build identity, evidence binding and guard leases, and the Phase 2 items.
21. Unit, integration, security and fault tests for Phase 1 (5.5) have supplied passing output.
22. Rollback to `td-1.6` demonstrated; `alembic downgrade base` tested.
23. An independent Security Engineer re-review of the Phase 1 diff is completed (RG-1).

**Out of scope for Phase 1:** authentication, dashboard data, monitoring, worker, batch, intake, egress proxy, second-host tooling, Coinbase calls, production data backups.

### 5.10 Full list of live-trading blockers

Live trading stays blocked while any of these is true. **In Phase 0 all are true by design. Nothing you build may relax them.**

1. No contract amendment and no signed Phase 11 review exist.
2. The paper image contains no adapter, live gateway, key loader, private endpoint constants, request builders or JWT signer; the live-lab build exists only in CI and has no deployable tag; deploy verifies the digest against the approved list.
3. The schema cannot represent LIVE mode, LIVE pair states or any non-PAPER venue; ledger tables are venue-scoped; identity (safety level, venues, gateway) is compile-time.
4. The gate is a venue-only function whose rows are written inside `authorize_attempt`; the UI panel is static.
5. No setting, CLI command, test, script, config, deployment action or dashboard button can enable live.
6. Hard ceilings are code and DB enforced; thresholds and fees are registered, hashed and hash-checked before RUNNING.
7. **No credentials exist**, so no reconciliation against the real exchange has ever run; sandbox recordings prove shapes only.
8. AS-C3 and AS-C4 (client-ID scope, list fields, fills and partial fills) are unverified, and an absence proof for `UNKNOWN → SUBMITTED` is only designed.
9. Fee tiers, minimum sizes and rate limits lack primary-source verification; the documentation crawl is incomplete (help-center pages unreached); a fee review is operator-attested.
10. Account, jurisdiction and product tradability is unproven (no Preview Order evidence).
11. REST behavior for `-USDC` IDs versus the documented unified book is unverified (AS-C1).
12. The JWT algorithm and key controls are unverified: View and Trade only, Transfer never, dedicated minimally funded portfolio, IP allowlist, key-permission check.
13. **A web-tier compromise can still perform ADMIN-available actions within guards** (the DB proves consistency, not authenticity). Step-up outside the web process (SD-5) must exist first.
14. Paper anchors are hourly; a live design needs a stronger cadence and an anchor per control event.
15. Any BLOCKER or HIGH finding from any review is open.
16. Production paper activation is refused until Phase 9 passes, so no paper evidence exists yet.
17. Backup, restore, rollback, anchor and second-host watcher drills, and the incident tabletop, have no evidence yet.
18. Paper fills are pessimistic and never live evidence; the 30-day soak after Phase 9 is incomplete.
19. LLM proposals, change requests and ADMIN attestations are never gate evidence; hostile input is parsed only in `intake`.
20. Any unknown order status, duplicate-order risk, stale data or metadata, failed reconciliation, balance mismatch, ledger breach or inconsistency, open breaker, active kill switch, risk breach, config-hash mismatch, or incomplete recovery blocks new orders.
21. The audit chain is unanchored, or the second host's verification has failed.
22. No independent-review mechanism exists for security-invariant changes (deviations are prohibited in that case).
23. Capital growth, regridding and any code path that raises a ceiling do not exist.
24. An unreadable gate state is treated as blocked.
25. Phase 11 remains "blocked" unless a separately approved unblock change with its own full test suite exists.

### 5.11 Residual risks accepted by the baseline (do not "fix" these by weakening other rules)

| ID | Residual risk | Handling |
|---|---|---|
| RR-1 | A fully compromised web process can capture passwords and cookies and perform ADMIN-available actions within guards; the DB chain proves consistency, not authenticity | Bounded by ceilings, guards, ledger CHECKs, paper-only scope; audited; **live blocker 13** |
| RR-2 | An owner-level compromise can rewrite the same-DB audit chain | Hourly chained anchors verified by the second host; not forgery-proof |
| RR-3 | The second host is a trust anchor and the notifier | Keep patched, separate and unreachable from the VPS |
| RR-4 | Shared Coinbase REST host: the egress proxy cannot separate public from private calls | Absent credentials plus the image scan are the barrier in paper |
| RR-5 | The backup wrapper is root-equivalent on the VPS if mis-written | Fixed command, no arguments, reviewed and tested |
| RR-6 | Documentation heterogeneity could bake a wrong fact into a rule | Source classes, Phase 9.0 verification record, quarterly re-verification |
| RR-7 | Writable sentinel volume lets a compromised web process halt the bot | Fail-safe direction; accepted |
| RR-8 | Hourly anchors leave a one-hour rewrite window | Stronger cadence is a Phase 11 prerequisite |
| RR-9 | A single maintainer protects invariants by process | Deviation rule and CI protection |
| RR-10 | Upload bytes still transit the web process | Length and prefix checks only, size and rate limits, tested |

---

*End of handoff. Begin with Part 0, section 0.3 (start conditions) and section 0.6 (first tasks). When in doubt, choose the stricter rule, prefer NO_TRADE, and record the decision in `docs/decisions.md`.*

# CLAUDE.md — TradingDots

Security-critical **spot-grid research platform** (repo `cleynn/botcoinbase`, branch
`claude/epic-carson-18byfr`). Python 3.12, FastAPI + Jinja2, PostgreSQL 16, Prometheus/Grafana, Caddy.
**Mode is PAPER / BACKTEST only. LIVE TRADING IS BLOCKED and unrepresentable in this build.**
Read `docs/current-handoff.md` and `docs/decisions.md` (DEC-001..DEC-024) for detail; this file is the
fast path. Anything not listed under "Verified" below must be treated as unverified.

## 1. Current status
Phases 1–9 and the Phase 9 security-review fixes (DEC-025) are implemented and pushed. Nothing has been deployed or run in Docker.
- 1–3 auth/sessions/audit chain, pair research; 4 pairs lifecycle; 5 OHLCV, backtest, paper trader;
  6 read-only LLM review packages; 7 LLM proposal import/review (no auto-apply);
  8 safety machinery (risk engine, kill switch, breaker, reconciliation, recovery, control workflow,
  live gate that only returns BLOCKED); 9 capital profiles + exchange-funds check + funds-based sizing.
- Deployment is not evidenced: DNS/TLS, `caddy validate`, Docker start, backup/restore, and an
  independent security review are all missing (see section 7). Release-gate verdict so far:
  **NOT APPROVED — REQUIRED FIXES** (paper-only scope).

## 2. Most recent work (newest first)
0. Phase 9 review fixes (DEC-025, migration 0008): paper profile changes need an idle paper side
   (session PAUSED, no open orders, state inside the new limits; the paper capital check never blocks
   a cancel); order authorization serialized (`bot_control` FOR SHARE + advisory lock); migration
   checksums (`schema_migrations`, owner-only); failed funds reads recorded as API events; production
   accepts only the `pilot` profile; audit records the real live-gate status.
1. Phase 9 (DEC-024, `docs/capital-profiles.md`): immutable `capital_profiles` table (migration 0007),
   profiles `pilot` (default 50/15/35/12 = cap/reserve/deployment/per-order), `expanded` (100/25/75/25),
   `medium` (250/50/150/50), `research` (0). PAPER/LIVE profile selection on the Bot page
   (`UPDATE CAPITAL LIMITS FOR PAPER MODE` / `... LIVE MODE`; ADMIN, CSRF, reauth, typed phrase, audit,
   bot PAUSED, DB-enforced). BUY orders must fit USDC reported through the read adapter
   (`FUNDS_UNAVAILABLE` / `INSUFFICIENT_FUNDS`). Grids sized from available funds; fewer lines tried
   before NO_TRADE. LIVE profile is recorded only.
2. Phase 8 security-review fixes (DEC-023): DB clock `td_now()` is the only trusted clock (60 s bound),
   boot-id bound to recovery, money rules recomputed in SQL (`td_authorize_order`), per-venue
   freshness, absence proof needs 2 OK runs >= 60 s apart.
3. Phase 8 (DEC-021/022): safety machinery, see `docs/safety-machinery.md` (state + fault matrices).

## 3. Verified vs not verified
**Verified (real PostgreSQL 16 via pgserver, real promtool/prometheus/node_exporter, real Chromium):**
- Full suite on the review-fix commit: 3109 passed / 1 failed; the failure was a wrong assumption in
  one new test (fixed; `test_capital_profiles.py` then 41 passed). The fixed file was re-run alone,
  the whole suite was not re-run once more after that one-test fix.
- Concurrency tests (`test_safety_concurrency.py`) fail when the two locks are removed and pass with
  them (negative control run). The H1 regression tests include the exact reproduced wedge state.
- Migration checksum guard tested (edit of an applied migration is refused).
- Real-browser check of the Bot page: 28/28 (incl. no horizontal scroll at 375 px).
- Migrations 0006, 0007 and 0008 roll back and re-apply (`test_rollback_and_remigrate_round_trip`).
- ruff format/check and `mypy app tests scripts` clean.

**NOT verified (do not claim otherwise):**
- Any real Coinbase response. There is no signer and no credentials (`NullSigner`;
  `app/exchange/factory.py` returns None). The exchange-funds read has only run against `FakeExchange`.
- Docker/compose start, Caddy config, TLS, public-port exposure, Grafana live login, backup/restore;
  DNS only for the app host (see section 7, item 5).
- `expanded` / `medium` profiles beyond tests (no paper soak). Loss ($10) and drawdown (20%) SQL
  ceilings are not scaled per profile and have no dedicated SQL regression test with fills.
- Kill-switch-versus-authorization races beyond the two tested cases. Independent security review:
  none (all reviews in `docs/decisions.md` say `review: SELF`).
- The checksum guard trusts a database that predates it once, so an earlier in-place edit of 0006
  stays undetectable there.

## 4. Files for the next task
| Area | Files |
|---|---|
| Capital/profiles | `app/capital/{profiles,funds}.py`, `app/strategy/{decision,grid}.py`, `app/config.py` (`PairPolicy.capital_profile`, `for_profile`), `app/backtest/trader.py`, `app/paper/exchange.py` (`_policy_for`) |
| Order path / risk | `app/safety/{risk_engine,context,pipeline,types}.py`, `app/storage/safety_repositories.py` |
| Control + Bot page | `app/safety/control.py`, `app/api/bot.py`, `app/web/bot_views.py`, `app/web/templates/bot*.html`, `app/api/schemas.py` |
| Reconcile/recover | `app/safety/{reconciler,recovery,commands,monitor}.py`, `app/exchange/*` |
| Database | `app/storage/migrations/0001..0007_*.sql` (+ `.down.sql`), `app/storage/database.py` |
| Metrics/alerts | `app/monitoring/{collectors,metrics}.py`, `infra/monitoring/alert_rules.yml` (no `tradingdots_bot*` alerts yet) |
| Tests | `tests/integration/safety_env.py` (harness), `test_safety_*.py`, `test_capital_profiles.py`, `tests/security/*` (reviewed route inventories) |
| Docs | `docs/{current-handoff,decisions,safety-machinery,capital-profiles,security-model,threat-model}.md` |

Commands: `uv run pytest` (or `.venv/bin/python -m pytest -q -p no:cacheprovider`); Prometheus tests
need `TD_PROMETHEUS_DIR` and `TD_NODE_EXPORTER_DIR`. Also `make format lint typecheck`. The full suite
takes ~15 min; safety test files are slow (about 2 min each). `tests/pending/` is excluded from collection.

## 5. Invariants that must not be broken
Security / process:
- LIVE is unrepresentable: modes `BACKTEST|PAPER`, venues `PAPER|FAKE` (DB CHECKs). The live gate only
  ever returns `LIVE TRADING BLOCKED`; selecting a LIVE profile must never open it.
- **The dashboard can never create, change or sell an order.** The web role (`td_app`) cannot insert
  intents/decisions/attempts/fills/reconciliations. Host modules (`pipeline, reconciler, recovery,
  commands, monitor, host_control, context`) are never imported by the web tier. `ControlService` is
  DB-only.
- Private Coinbase reads only through the GET-only adapter; no credentials, JWT or signing code before
  Phase 11 and its own review. Sandbox is for request shape only, never market/fill simulation.
- Control flow: request -> authz -> CSRF -> fresh reauth -> typed phrase -> audit -> command -> outcome
  audit. Phrases (exact): `PAUSE BOT`, `RESUME BOT AFTER RECONCILIATION`, `CANCEL KNOWN BOT ORDERS`,
  `ACTIVATE KILL SWITCH`, `UPDATE CAPITAL LIMITS FOR PAPER MODE`, `UPDATE CAPITAL LIMITS FOR LIVE MODE`.
  A wrong phrase or invalid input never spends the reauth. Kill switch never market-sells; only the
  host releases it and releasing never resumes.
- Resume only after a current successful reconciliation; startup recovery before any action; REST
  reconciliation is authoritative, WebSocket supplemental. Persist the intent before execution; client
  id = UUIDv5(intent id, attempt no); an UNKNOWN attempt resolves only by reconciliation; retry only
  after proven absence (2 OK runs >= 60 s apart) or rejection.
- `td_now()` (database clock) is the only trusted clock; supplied times must be within 60 s. An
  attempt must carry the boot id recorded by recovery.
- Audit events are append-only and hash-chained; never log passwords, phrases, secrets.
- Migrations: **never edit an applied migration** (there is no checksum guard; 0006 was edited in
  place once, see section 7). Add a new numbered migration with a `.down.sql`.
- Reviewed route inventories in `tests/security/` must be updated deliberately when routes change.

Financial:
- Money is `Decimal` only (no floats in money paths; config rejects floats). Post-only
  `limit_limit_gtc` orders only.
- Limits come from the selected capital profile and are enforced again in SQL
  (`td_authorize_order`, `paper_capital_check`, `paper_deposit_guard`) on top of absolute table CHECKs
  (<= 50 per intent, <= 150 per grid cell). Config and Python may only tighten them. The protected
  reserve is never deployed; `usable = max(0, min(min(available, cap) - reserve, max_deployment -
  committed))`. One active pair; capital growth and regridding stay disabled.
- Funds that cannot be read or are ambiguous block BUY orders; they never read as zero or as the
  ledger's number. Stale data/metadata, bad precision/minimums, fee failure, spread, loss/drawdown,
  breaker, kill switch, unknown order, failed reconciliation all block.
- `expanded`/`medium` raise the maximum capital the code can use: do not use them outside tests
  before an independent review (DEC-024).

## 6. Smallest next safe task
Add `tradingdots_bot*` alert rules (kill switch active, breaker open, recovery incomplete,
reconciliation age/mismatch, UNKNOWN attempts) in `infra/monitoring/alert_rules.yml` with
`promtool test rules` — no money-path change. Then: a database-outage fault test, then backup/restore.

## 7. Blockers and open issues
1. Resolved in DEC-025: order authorization serialization (H2), migration checksum guard for new
   databases (H3), paper-profile change while the paper side is active (H1).
2. Re-check after any new money-path change: concurrent authorization, kill switch / pause / profile
   change racing an insert, and that `cancel_for_safety` can never be blocked.
3. No independent security review (all `review: SELF`). Security review items still open: M3 caller-
   supplied order book unverified, M4 no scheduler/heartbeat, M5 no table retention, M6 restrictive
   actions depend on audit availability, M7 limited reconciliation lookback; lows: VIEWER sees client
   order ids, FAKE venue legal in the production schema, no per-session phrase throttle, backup docs,
   0006 down-migration destroys safety history.
4. M1: the exchange-funds check (`INSUFFICIENT_FUNDS`) is host-process only; the database cannot read
   the exchange, so a compromised host could write an ALLOW that skips it (reconciliation's balance
   check is the independent control). M3: the paper deposit is fixed at the first start.
   No alerts on bot safety metrics; no database-outage fault test (`tests/faults/` is empty); no
   retention/pruning of DB tables; no backup/restore test.
5. Release gate (NOT EVIDENCED): TLS for both hosts, `caddy validate`, Docker start, public-port scan,
   Grafana live login, backup/restore. DNS: the app host `tradingdots.onthewall.ovh` is IPv4
   `92.222.190.142` (operator-supplied; also resolved from the sandbox on 2026-09-30). The Grafana host
   `grafana.tradingdots.onthewall.ovh` did **not** resolve from the sandbox: create its record (same VPS)
   before first start or Caddy cannot issue its certificate. IPv6 unknown. See `docs/operational-runbook.md`.
6. Design debt: limits duplicated in SQL (12/15/35 style literals now profile-driven, but loss $10 and
   drawdown 20% are hardcoded) and Python; `safety_repositories.py` is large; Python and SQL money
   rules can drift (a constants/parity test exists only for the profile table).
7. Environment quirk: shell calls can fail transiently when the auto-mode safety classifier has no
   verdict; retry, and never run `pkill -f` with a pattern that matches your own command line.

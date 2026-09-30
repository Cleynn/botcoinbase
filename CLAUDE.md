# CLAUDE.md — TradingDots

Security-critical **spot-grid research platform** (repo `cleynn/botcoinbase`, branch
`claude/epic-carson-18byfr`). Python 3.12, FastAPI + Jinja2, PostgreSQL 16, Prometheus/Grafana, Caddy.
**Mode is PAPER / BACKTEST only. LIVE TRADING IS BLOCKED and unrepresentable in this build.**
Read `docs/current-handoff.md` and `docs/decisions.md` (DEC-001..DEC-024) for detail; this file is the
fast path. Anything not listed under "Verified" below must be treated as unverified.

## 1. Current status
Phases 1–9 are implemented and pushed (HEAD `2ce7565`). Nothing has been deployed or run in Docker.
- 1–3 auth/sessions/audit chain, pair research; 4 pairs lifecycle; 5 OHLCV, backtest, paper trader;
  6 read-only LLM review packages; 7 LLM proposal import/review (no auto-apply);
  8 safety machinery (risk engine, kill switch, breaker, reconciliation, recovery, control workflow,
  live gate that only returns BLOCKED); 9 capital profiles + exchange-funds check + funds-based sizing.
- Deployment is not evidenced: DNS/TLS, `caddy validate`, Docker start, backup/restore, and an
  independent security review are all missing (see section 7). Release-gate verdict so far:
  **NOT APPROVED — REQUIRED FIXES** (paper-only scope).

## 2. Most recent work (newest first)
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
- Full suite last completed run: 3095 passed / 3 failed (3 reviewed route inventories that did not
  know the new `/bot/capital/*` routes). They were fixed; `tests/security` then passed (418).
  **The full suite was not re-run after that fix** — re-run it first.
- `tests/integration/test_capital_profiles.py` 38 passed; `tests/unit/test_capital*.py` passed.
- Real-browser check of the Bot page: 28/28 (incl. no horizontal scroll at 375 px).
- Migration 0006 and 0007 roll back and re-apply (`test_rollback_and_remigrate_round_trip`).
- ruff format/check and `mypy app tests scripts` clean.

**NOT verified (do not claim otherwise):**
- Any real Coinbase response. There is no signer and no credentials (`NullSigner`;
  `app/exchange/factory.py` returns None). The exchange-funds read has only run against `FakeExchange`.
- Docker/compose start, Caddy config, DNS/TLS, public-port exposure, Grafana live login, backup/restore.
- `expanded` / `medium` profiles beyond tests (no paper soak). Loss ($10) and drawdown (20%) SQL
  ceilings are not scaled per profile and have no dedicated SQL regression test with fills.
- Concurrency of order authorization (see section 7). Independent security review: none (all
  reviews in `docs/decisions.md` say `review: SELF`).

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
Re-run the full suite on HEAD and record the result in `docs/current-handoff.md`
(`make`-free: `uv run pytest`, with the two monitoring env vars set). Then, in order of size:
1. Add `tradingdots_bot*` alert rules (kill switch active, breaker open, recovery incomplete,
   reconciliation age/mismatch, UNKNOWN attempts) with `promtool test rules` — no money-path change.
2. Serialize order authorization (see section 7, item 1) with a concurrency test.

## 7. Blockers and open issues
1. **HIGH (before any execution phase):** `order_attempt_guard` locks only the decision row. Concurrent
   attempt inserts can both pass the cap/reserve checks, and a concurrent kill/pause can be missed.
   Fix: `SELECT ... FROM bot_control FOR SHARE` in the guard plus a `pg_advisory_xact_lock` around
   `td_authorize_order`; add concurrency tests.
2. **HIGH:** migration 0006 was edited in place after being written (no checksum guard). Any database
   already at schema 6 before that edit lacks the fixes; roll back to 5 and re-apply, or add a
   checksum check.
3. No independent security review (all `review: SELF`). Security review items still open: M3 caller-
   supplied order book unverified, M4 no scheduler/heartbeat, M5 no table retention, M6 restrictive
   actions depend on audit availability, M7 limited reconciliation lookback; lows: VIEWER sees client
   order ids, FAKE venue legal in the production schema, no per-session phrase throttle, backup docs,
   0006 down-migration destroys safety history.
4. No alerts on bot safety metrics; no database-outage fault test (`tests/faults/` is empty); no
   retention/pruning of DB tables; no backup/restore test.
5. Release gate (NOT EVIDENCED): DNS/TLS for `tradingdots.onthewall.ovh` and
   `grafana.tradingdots.onthewall.ovh`, `caddy validate`, Docker start, public-port scan, Grafana live
   login, backup/restore.
6. Design debt: limits duplicated in SQL (12/15/35 style literals now profile-driven, but loss $10 and
   drawdown 20% are hardcoded) and Python; `safety_repositories.py` is large; Python and SQL money
   rules can drift (a constants/parity test exists only for the profile table).
7. Environment quirk: shell calls can fail transiently when the auto-mode safety classifier has no
   verdict; retry, and never run `pkill -f` with a pattern that matches your own command line.

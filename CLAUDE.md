# CLAUDE.md — TradingDots

Security-critical **spot-grid research platform** (repo `cleynn/botcoinbase`, branch
`claude/epic-carson-18byfr`). Python 3.12, FastAPI + Jinja2, PostgreSQL 16, Prometheus/Grafana, Caddy.
**Modes BACKTEST / PAPER / LIVE (owner-directed, DEC-026). Live orders need the host arming and have never been run against the real Coinbase API.**
Read `docs/current-handoff.md` and `docs/decisions.md` (DEC-001..DEC-024) for detail; this file is the
fast path. Anything not listed under "Verified" below must be treated as unverified.

## 1. Current status
Phases 1-9, the Phase 9 review fixes (DEC-025) and the live-trading phase (DEC-026) are implemented and pushed. Nothing has been deployed or run in Docker, and nothing has talked to the real Coinbase API.
- 1-3 auth/sessions/audit chain, pair research; 4 pairs lifecycle; 5 OHLCV, backtest, paper trader; 6-7 LLM review/proposals (no auto-apply); 8 safety machinery; 9 capital profiles + funds check; **10 live trading**: venue COINBASE, CDP credentials + ES256 signer, create/cancel-only gateway, per-mode editable `trading_config`, mode BACKTEST/PAPER/LIVE, host arming, grid runner on N parallel pairs, `live` CLI, Bot page mode buttons and configuration form, VPS scripts (`scripts/vps/`, `docs/vps-deployment.md`).
- Deployment is not evidenced: DNS/TLS, `caddy validate`, Docker start, backup/restore, the first real exchange call and an independent security review are all missing (section 7). The owner will open a Claude CLI on the VPS to deploy and run the network tests.

## 2. Most recent work (newest first)
0. DEC-026 live phase: migrations 0009 (COINBASE venue, `live_arming`, `live_attestations`), 0010 (`trading_config` per mode: pairs, lines, USDC per grid, invested cap, reserve, per-order cap; `trading_state`), 0011 (mode BACKTEST/PAPER/LIVE), 0012 (`live_grids`; arming also ends on a LIVE config or mode change). `app/live/runner.py` (tick: gates, reconcile, per pair refresh data, start grid from `decide`, keep cells working through `OrderPipeline`), `app/live/cli.py`, `ControlService.switch_mode` / `set_trading_config`, routes `/bot/mode/{mode}/...` and `/bot/trading/{mode}/...`, compose service `live` (profile `live`), `scripts/vps/06-install-key.sh`.
1. Phase 9 review fixes (DEC-025, migration 0008), Phase 9 (DEC-024, `docs/capital-profiles.md`), Phase 8 (DEC-021..023, `docs/safety-machinery.md`).

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
  DNS A records for both hosts resolved once from the sandbox (see section 7, item 5).
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
| Live | `app/live/{runner,cli}.py`, `app/exchange/{credentials,cdp_signer,coinbase_live,factory}.py`, `app/capital/trading.py`, `app/safety/live_gate.py`, compose service `live`, `scripts/vps/*`, `docs/vps-deployment.md` |
| Database | `app/storage/migrations/0001..0012_*.sql` (+ `.down.sql`), `app/storage/database.py` |
| Metrics/alerts | `app/monitoring/{collectors,metrics}.py`, `infra/monitoring/alert_rules.yml` (no `tradingdots_bot*` alerts yet) |
| Tests | `tests/integration/safety_env.py` (harness), `test_safety_*.py`, `test_capital_profiles.py`, `test_trading_mode.py`, `test_live_runner.py`, `test_live_cli.py`, `tests/security/*` (reviewed route inventories) |
| Docs | `docs/{current-handoff,decisions,safety-machinery,capital-profiles,security-model,threat-model}.md` |

Commands: `uv run pytest` (or `.venv/bin/python -m pytest -q -p no:cacheprovider`). **Never run two pytest
processes at once: the second deletes the first one's temporary database directory (hundreds of bogus
connection errors).** Prometheus tests
need `TD_PROMETHEUS_DIR` and `TD_NODE_EXPORTER_DIR`. Also `make format lint typecheck`. The full suite
takes ~15 min; safety test files are slow (about 2 min each). `tests/pending/` is excluded from collection.

## 5. Invariants that must not be broken
Security / process:
- Modes `BACKTEST|PAPER|LIVE`, venues `PAPER|FAKE|COINBASE` (DB CHECKs). A COINBASE order needs the host
  arming (`td_live_armed`: unrevoked, <= 24 h, ended by a kill switch, breaker trip, recovery reset, LIVE
  profile or configuration change, or a mode change made after it). Switching the mode or choosing a
  LIVE profile or editing the LIVE configuration never places an order and never arms. Arming refuses a
  key that can transfer funds.
- **The dashboard can never create, change or sell an order.** The web role (`td_app`) cannot insert
  intents/decisions/attempts/fills/reconciliations. Host modules (`pipeline, reconciler, recovery,
  commands, monitor, host_control, context`) are never imported by the web tier. `ControlService` is
  DB-only.
- Private Coinbase reads only through the GET-only adapter; orders only through `CoinbaseLiveGateway`
  (create and cancel, post-only limit GTC, no market order). The key lives in one 0600 file outside the
  repository (`TD_COINBASE_KEY_FILE`), mounted read-only into `batch`/`live` only; never in the repo, `.env`,
  a log, the web tier or chat. Sandbox is for request shape only.
- Control flow: request -> authz -> CSRF -> fresh reauth -> typed phrase -> audit -> command -> outcome
  audit. Phrases (exact): `PAUSE BOT`, `RESUME BOT AFTER RECONCILIATION`, `CANCEL KNOWN BOT ORDERS`,
  `ACTIVATE KILL SWITCH`, `UPDATE CAPITAL LIMITS FOR PAPER|LIVE MODE`, `SWITCH TO BACKTEST|PAPER|LIVE MODE`,
  `UPDATE TRADING CONFIGURATION FOR PAPER|LIVE MODE`.
  A wrong phrase or invalid input never spends the reauth. Kill switch never market-sells; only the
  host releases it and releasing never resumes.
- Resume only after a current successful reconciliation; startup recovery before any action; REST
  reconciliation is authoritative, WebSocket supplemental. Persist the intent before execution; client
  id = UUIDv5(intent id, attempt no); an UNKNOWN attempt resolves only by reconciliation; retry only
  after proven absence (2 OK runs >= 60 s apart) or rejection.
- `td_now()` (database clock) is the only trusted clock; supplied times must be within 60 s. An
  attempt must carry the boot id recorded by recovery.
- Audit events are append-only and hash-chained; never log passwords, phrases, secrets.
- Migrations: **never edit an applied migration** (a checksum guard refuses it for new databases; 0006 was
  edited in place once, see section 7). Add a new numbered migration with a `.down.sql`.
- Never run two pytest processes at once (see section 4).
- Reviewed route inventories in `tests/security/` must be updated deliberately when routes change.

Financial:
- Money is `Decimal` only (no floats in money paths; config rejects floats). Post-only
  `limit_limit_gtc` orders only.
- Limits come from the mode's `trading_config` row (pairs 1..10, lines 3..20, USDC per grid, invested cap,
  reserve >= 20% of invested+reserve, per-order cap for BUYs and SELLs) and are enforced again in SQL
  (`td_authorize_order`, `paper_capital_check`, `paper_deposit_guard`). Defaults equal the pilot profile
  (1 pair, 3 lines, 35 / 15 / 12). The protected reserve is never deployed; `usable = max(0, min(min(available,
  cap) - reserve, max_deployment - committed))`. Capital growth and regridding stay disabled. The paper
  side trades one pair; the live runner trades up to `max_pairs`.
- Funds that cannot be read or are ambiguous block BUY orders; they never read as zero or as the
  ledger's number. Stale data/metadata, bad precision/minimums, fee failure, spread, loss/drawdown,
  breaker, kill switch, unknown order, failed reconciliation all block.
- DEC-026 relaxed the pilot-only limits on the owner's instruction (see its list). **Restore them, add an
  independent review and a paper soak before real money beyond a small test.**

## 6. Smallest next safe task
Deploy to the VPS and run the network tests (owner opens a Claude CLI there): follow `docs/vps-deployment.md`
(`scripts/vps/check.sh`, then steps 02, 01, firewall, `.env`, `06-install-key.sh`, first start), then
`live check` (read-only), `live baseline`, `live run` (recovery, then resume on the Bot page, `live arm`),
starting with a tiny LIVE configuration (for example 1 pair, 3 lines, 12 per grid). The first real order and
the real response shapes (AS-C3, AS-C4) are the first unverified facts to confirm; expect to fix parsing.
Then: restore the DEC-026 relaxations, `tradingdots_bot*` alerts, a database-outage fault test, backup/restore.

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
   `92.222.190.142` (operator-supplied). Both `tradingdots.onthewall.ovh` and
   `grafana.tradingdots.onthewall.ovh` resolved to it from the sandbox on 2026-09-30 (no AAAA records).
   See `docs/operational-runbook.md`.
6. Design debt: limits duplicated in SQL (12/15/35 style literals now profile-driven, but loss $10 and
   drawdown 20% are hardcoded) and Python; `safety_repositories.py` is large; Python and SQL money
   rules can drift (a constants/parity test exists only for the profile table).
7. DEC-026 leftovers: no WebSocket feed; grid state is derived from intents/attempts; `live run` recovers (and
   pauses the bot) on every start; no Bot-page view of live grids yet (use `live status`); loss/drawdown SQL
   ceilings are not scaled to larger configurations; the five attestations are not required to arm.
8. Environment quirk: shell calls can fail transiently when the auto-mode safety classifier has no
   verdict; retry, and never run `pkill -f` with a pattern that matches your own command line.

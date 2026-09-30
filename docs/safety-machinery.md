# Safety machinery (Phase 8)

Status: implemented. **Live trading stays BLOCKED**: there is no live gateway class, no signer, no
credential, no LIVE venue or mode, and the gate cannot return anything but BLOCKED. DEC-000 is not
acknowledged and no independent security review has been done. Decisions: DEC-021, DEC-022.

Numbering: this is the request's "Phase 8"; the baseline calls the safety machinery Phase 9 (DEC-021).

## What exists
| Part | Where | Notes |
|---|---|---|
| Risk engine | `app/safety/risk_engine.py` | pure; every reason returned; unknown = block |
| Circuit breaker | `breaker.py`, `monitor.py`, `host_control.py` | pure policy + host trip; pauses the bot |
| Kill switch | `control.py` (web), `host_control.py` (host) | pauses, blocks, queues a cancel of known bot orders; never sells |
| Stale data/metadata | `staleness.py`, `context.py` | age from candles / verification time; missing = STALE |
| Anomaly checks | `anomaly.py` | candle jump, spread spike, taker fill on post-only, fill worse than limit, balance, order rate, reject storm |
| Private read adapter | `app/exchange/coinbase_private.py` | five GET paths; `NullSigner` only; no write method |
| WebSocket user updates | `app/exchange/websocket.py` | parser + tracker, **supplemental hints only**; no socket code |
| REST reconciliation | `app/safety/reconciler.py` | authoritative; immutable run + findings |
| Startup recovery | `recovery.py` | reconcile before any action; never resumes |
| Retry policy | `retry.py`, `reader.py` | reads only; orders are never retried blindly |
| Control workflow | `control.py`, `app/api/bot.py` | four phrases, full chain, audit before and after |
| Live gate | `live_gate.py` | panel always BLOCKED; order gate passes only PAPER/FAKE |
| Sandbox | `app/exchange/sandbox.py` | request/response **shape** checks only |
| Fake exchange | `app/exchange/fake.py` | scripted TEST-DOUBLE: no market, no matching, no fills unless a test injects them |
| Order path | `pipeline.py` | intent -> decision -> authorization -> mark -> gateway -> reconcile |

Nothing in the default deployment can place an order: `build_reader` and `build_gateway` return `None`,
no CLI command or route submits, and the only gateway implementation is the test double.

## Order path (safety invariants 2-6, BI-17..21)
1. `create_intent`: an immutable `order_intents` row (deterministic key; same proposal in the same slot is the same intent). The schema caps the notional at 12 USDC and allows only post-only `limit_limit_gtc`.
2. Risk decision computed from fresh facts and stored (ALLOW and BLOCK), 30 s TTL, consumed by exactly one attempt.
3. Authorization = inserting the `order_attempts` row. The database re-reads kill switch, breaker, bot state, recovery, reconciliation freshness (<= 300 s) and UNKNOWN attempts at that instant, consumes the ALLOW, and refuses a reused client id.
4. Client order id = UUIDv5(intent id, attempt number): stable, unique, idempotent.
5. `SUBMITTING` (with `submitting_at`) is committed **before** the exchange call.
6. Any result other than an explicit definite rejection or an accepted order **with an exchange id** leaves the attempt `UNKNOWN`; reconciliation runs before anything else.
7. A retry is a *new* attempt, allowed only after the earlier one is `ABSENT` (absence proof) or definitively `REJECTED`.

## State matrix
Bot control (`bot_control`, one row; every change advances `version` and is recorded in `bot_control_history`):

| Change | Who | Requires |
|---|---|---|
| PAUSE (RUNNING -> PAUSED) | web ADMIN or host | phrase `PAUSE BOT` (web) |
| ACTIVATE KILL SWITCH | web ADMIN or host | phrase `ACTIVATE KILL SWITCH`; forces PAUSED; queues CANCEL_KNOWN |
| CANCEL KNOWN BOT ORDERS | web ADMIN | phrase; bot must be PAUSED; single flight; host executes |
| OPEN BREAKER | host only | a trip reason; forces PAUSED; cooldown set |
| RESUME (PAUSED -> RUNNING) | web ADMIN only | phrase `RESUME BOT AFTER RECONCILIATION`; kill INACTIVE; recovery COMPLETE; breaker CLOSED or cooled down (closed by the same change); newest reconciliation OK, <= 300 s old, finished after any breaker trip; no UNKNOWN attempt |
| MARK RECOVERY | host only | COMPLETE needs a current OK reconciliation |
| RELEASE KILL SWITCH | host only | phrase `RELEASE KILL SWITCH`; no cancel command open; leaves PAUSED and recovery INCOMPLETE |

`RUNNING` with kill ACTIVE, breaker OPEN or recovery INCOMPLETE is unrepresentable (CHECK).

Order attempt: `AUTHORIZED -> SUBMITTING | REJECTED(NOT_SENT)`; `SUBMITTING -> WORKING | REJECTED | UNKNOWN`; `WORKING -> CANCEL_REQUESTED | FILLED | CANCELLED | EXPIRED | UNKNOWN`; `CANCEL_REQUESTED -> CANCELLED | FILLED | WORKING | EXPIRED | UNKNOWN`; `UNKNOWN -> WORKING | FILLED | CANCELLED | EXPIRED | REJECTED | ABSENT`. FILLED, CANCELLED, EXPIRED, REJECTED, ABSENT are settled. Only the host writes attempts. `ABSENT` needs two OK reconciliations that *started* at least 120 s after the submit mark, none of which ever named the client id (enforced in the database).

## Block reasons (requirement 8)
Stale market data, stale metadata, invalid precision, below minimum size/notional, fee unattested or expired, edge below fee+spread+slippage+margin, reserve/deployment/per-order cap, sell beyond inventory, daily loss, drawdown, abnormal or unknown spread, price far from market, API failures, breaker open, kill switch, bot not RUNNING, unexpected balance, failed/missing/stale reconciliation, unknown order, unknown attempt, duplicate client id, duplicate intent, recovery incomplete, failed live gate, pair not active, product not tradable. All in `BLOCK_REASONS`; each has an operator sentence and a metric class.

## Fault matrix (all exercised with the scripted FAKE exchange)
| Fault | Result | Test |
|---|---|---|
| Submit timeout / network / 5xx / 429 before processing | attempt UNKNOWN, reconcile, **no resubmit**; ABSENT after proof, then a new attempt | `test_safety_pipeline` |
| Same faults after the exchange took the order | reconciliation adopts it: one order, no duplicate | `test_safety_pipeline` |
| Definite reject | REJECTED with a fixed code; retry is a fresh attempt | `test_safety_pipeline` |
| Duplicate client id returns the existing order / a different order | adopted + reconciled / REJECTED and flagged | `test_safety_pipeline` |
| Unexpected error, success without an exchange id | UNKNOWN | `test_safety_pipeline` |
| Crash after the submit mark / before it | recovery: adopted or UNKNOWN / closed NOT_SENT | `test_safety_pipeline` |
| Race: kill switch flips between decision and authorization | database refuses; nothing sent | `test_safety_pipeline` |
| Every hot guard (kill, pause, breaker, stale reconciliation, unknown attempt, foreign order, precision, notional, deviation, edge, spread, book, fees, production FAKE, API failures) | BLOCK before any exchange call | `test_safety_pipeline` |
| Foreign order, missing order, lagging/empty listing, duplicate client id, changed order, unmapped status, failed-after-accept, revived terminal | MISMATCH, attempt UNKNOWN where relevant | `test_safety_reconciler` |
| Taker fill, fill worse than limit, unknown fill, fills not adding up, duplicate fill | FILL_ANOMALY / UNKNOWN_FILL / FILL_MISMATCH / deduplicated | `test_safety_reconciler` |
| Wrong balance, unexpected currency | BALANCE_MISMATCH; breaker | `test_safety_reconciler` |
| List/fills/accounts failing, malformed, transient | FAILED run (nothing changed) / retried with backoff | `test_safety_reconciler` |
| Feed hint contradicting REST, hint for unknown order, sequence gap, malformed message | REST wins; UNKNOWN_ORDER; reconcile requested | `test_safety_reconciler`, `test_exchange_boundary` |
| Order appears after ABSENT | ORDER_APPEARED_AFTER_ABSENCE; breaker | `test_safety_reconciler` |
| Cancel: queued, rejected, call failing, batches, no gateway | CANCEL_REQUESTED only; foreign orders untouched; never sells | `test_safety_reconciler` |
| Restart: no reader, failed/mismatching reconciliation, running bot | recovery INCOMPLETE, bot PAUSED | `test_safety_recovery_monitor` |
| API failures, loss, drawdown, order rate, reject storm | breaker opens once, bot paused | `test_safety_recovery_monitor` |
| Kill or breaker with a running paper session | paper orders cancelled, session paused, nothing sold | `test_safety_recovery_monitor` |

## Clock, boot id and SQL authorization (DEC-023)
- `td_now()` is the only trusted clock. Any supplied timestamp must be within 60 s of it, for both roles. Tests move it via the owner-only `td_test_clock` table.
- Recovery writes a `boot_id`; an attempt carrying any other boot id is refused, so a restarted process cannot authorize orders before it recovers.
- `td_authorize_order` re-checks order cap, reserve, deployment cap, sell-vs-inventory, loss and drawdown from recorded facts when an attempt is inserted.
- Reconciliation freshness and absence proof are per venue; absence needs two OK runs at least 60 s apart.

| Fault | Result |
|---|---|
| web writes future/back-dated `updated_at` | refused (60 s bound); host actions unaffected |
| back-dated resume after a stale reconciliation | refused; resume works after a real reconciliation |
| attempt with foreign or stale boot id | refused |
| forged ALLOW breaching reserve / deployment cap / inventory | refused by SQL |
| two back-to-back OK runs | do not prove absence |

## Commands
```
make safety-status | safety-recover | safety-reconcile | safety-monitor | safety-commands
uv run python -m app.cli safety kill --confirm "ACTIVATE KILL SWITCH"
uv run python -m app.cli safety kill-release --confirm "RELEASE KILL SWITCH"
```
There is no `resume`, `submit`, `order` or `sell` command; RESUME is the ADMIN dashboard action.
Without an exchange reader (every deployment of this build) `recover` and `reconcile` report
`NO_EXCHANGE_READER` and change nothing, so the bot stays PAUSED and RESUME is refused with reasons.

## Configuration
`SafetySettings` (`app/config.py`, YAML `safety:`): freshness limits, spread and price-deviation limits, reconciliation window (<= 300 s), API-failure window/threshold, breaker cooldown, daily loss (<= 10 USDC), drawdown ratio (<= 20%), per-order cap (<= the 12 USDC ceiling), intent rate, reject streak, retry bounds, absence window/count. Every bound may only tighten a ceiling.

## Rollback
Trading is never resumed by a rollback. Stop the host worker and, if in doubt, ACTIVATE KILL SWITCH. Development/test schema rollback: `rollback(target, to_version=5)` applies `0006_safety.down.sql` (drops the safety tables and functions; audit events stay). Verified on a real PostgreSQL.

## Not verified
Any real Coinbase response (AS-C1, AS-C3, AS-C4: order, fill and account shapes are the documented ones and untested against the live API); client-id scope; the absence proof against a real exchange (it is a conservative design, not evidence); real containers (no Docker daemon); WebSocket connectivity (no client exists). No key, signer or live gateway exists to test with.

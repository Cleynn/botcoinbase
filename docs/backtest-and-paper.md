# Market data, backtests and PAPER trading (Phase 5)

Nothing in this phase trades anywhere. There is no private Coinbase client, no order call, no
credential, no live mode and no LLM feature. The only "exchange" is the local paper database. Live
trading stays BLOCKED (DEC-000 is not acknowledged; no independent security review has happened).

## Pipeline

```
Coinbase public REST (GET only, egress proxy)
   -> importer (dry run by default) -> candles + data-quality events + cursor   [td_ctl writes]
   -> snapshot builder -> Parquet file + manifest + dataset_snapshots row       (frozen, checksummed)
   -> backtest / walk-forward (frozen snapshot only) -> reports (JSON + Markdown, immutable)
   -> paper exchange (local, persisted) reads candles, writes paper_* tables
   -> web tier reads reports and status only                                    [td_app: SELECT]
```

## Commands (run as `td_ctl` in the `batch` container; profile `discovery`)

| Command | Effect |
|---|---|
| `market import --product P [--days N]` | DRY RUN: fetch, validate, report. Writes nothing. |
| `market import --product P --commit` | Writes candles, run, events, cursor, audit event. |
| `market snapshot --product P [--days N]` | Freezes the ingested range into a Parquet snapshot. |
| `market snapshots` / `market quality --product P` | List snapshots / store a data-quality report. |
| `backtest run --snapshot ID [--walk-forward] [--levels 3..5]` | Operator and stress fee scenarios; stores a report. |
| `paper status | start [--acknowledge-halt] | stop | step | report` | Local paper exchange. |

`make market-import PRODUCT=BTC-USDC` (add `COMMIT=1` to write), `make market-snapshot`,
`make backtest SNAPSHOT=<id> [WALK=1]`, `make paper-status|paper-step|paper-report`.

## Data rules

- Decimal everywhere in financial code (`app/domain/money.py`): floats, bools and NaN are refused;
  fixed context (34 digits, half-even); a test fails on any float literal, `float()` or `math` in
  the financial modules (one exemption: the numeric argument of `sleep`).
- Candles are validated (`app/market/candles.py`): gaps, duplicates, conflicts, ordering,
  malformed values, invalid OHLC, misalignment, future and still-open candles. Bad candles are
  excluded and recorded; **candles are never invented, filled or altered**. A changed repeat is
  recorded as a conflict and the stored candle stands.
- Bounded safe-read retry: at most `data.max_attempts` (default 3) attempts, fixed exponential
  backoff, only for timeouts, network errors, HTTP 429 and 5xx. Client errors are never retried.
- The cursor only moves forward, over a contiguous successfully processed span (database trigger).
- Snapshots: deterministic Parquet bytes, `file_sha256` and `content_sha256`, provenance (range,
  ingest run high-water mark, product metadata snapshot, engine version), written read-only. Loading
  re-verifies size, both hashes, ordering and range. A late-filled gap creates a *new* snapshot;
  the old one is unchanged. Snapshots need fresh product metadata and a bounded gap ratio.

## Strategy (deterministic; NO_TRADE is normal)

EMA trend filter, Kaufman efficiency ratio, ATR band and Donchian range; a **geometric** grid with
3-5 levels; a pair score 0-100. The grid is refused (NO_TRADE with reasons) when fees make a cell
unprofitable at the attested rate *or* the stress rate, when exchange minimums or increments cannot
be met, when the band is too narrow or wide, when price is at a band edge, or the market trends.
Sizing keeps one quote increment of headroom. No regridding, no growth (both refused in config
validation and again in the trader).

## Capital rules (enforced in the trader and again by the database)

50 USDC total paper deposit (exactly one), 15 USDC protected reserve (free cash never below it),
35 USDC deployment cap (open buy reserves + inventory cost), 3-5 levels, one active paper pair. The
database trigger constants (50/15/35) are hard-coded and cannot be loosened by configuration.

## Simulation assumptions (all pessimistic; `backtest:` in `config/pair-policy.yaml`)

Orders are post-only limits placed on the candle after the decision; a fill needs price to trade
*through* the limit by `adverse_bps`; at most 10% of candle volume is shared among orders (partial
fills, no fill on zero volume); every fill pays a maker fee rounded up; orders older than the age
limit and all orders across a data gap are cancelled; a close beyond the band stops the grid and
keeps inventory (never a market sell); a drawdown past the stop ratio HALTS until an operator
acknowledges (`paper start --acknowledge-halt`). Fees are operator-attested
(`fees.operator_maker_rate`/`attested_on`, review interval 30 days); without a valid attestation
nothing runs. The stress scenario uses `fees.stress_maker_rate` (0.006).

## Walk-forward

Rolling folds (train 4032, test 2016 candles by default). On each train window a small grid of
levels {3,4,5} x breakout buffer {0.01, 0.02} is scored (net P&L minus drawdown x capital); the
winner is run on the following out-of-sample window. Reports give per-fold and out-of-sample totals.

## Paper exchange

The state lives in PostgreSQL (`paper_*`), not in memory. `step` rebuilds the trader state from the
tables, feeds it the closed candles that arrived since the last step, and writes all effects in one
transaction, so a crash loses nothing and a repeated step is a no-op (deterministic UUIDv5 client
order ids, unique ledger keys, one fill per order per candle). The trader is the *same code* the
backtest uses; an integration test asserts they agree exactly on identical candles. A session only
starts for the single PAPER_ACTIVE pair, in PAPER mode, outside production, with attested fees and
fresh data. `stop` cancels open orders and never sells inventory. Pausing the pair stops the session.
The default runtime gate (`PaperRuntimeGate`) lets a pair activate only in PAPER mode while the paper
session is PAUSED, and treats open paper orders or inventory as "not clean".

## Dashboard and reports

`/reports`, `/reports/{id}` and JSON/Markdown downloads (`text/plain`, `attachment`, `nosniff`) are
read-only and VIEWER-visible; report text is escaped. Every result is labelled BACKTEST or PAPER
(the database also CHECKs the label against the kind). Report bodies carry no generation time, so
identical inputs give identical bytes and hash; repeating a run stores nothing new.

An example (FICTIONAL, synthetic series): `docs/examples/backtest-report-FICTIONAL.md` / `.json`.

## Metrics (read-only)

`tradingdots_ingest_last_success_timestamp_seconds`, `tradingdots_data_quality_events_total{code}`,
`tradingdots_backtest_runs_total`, `tradingdots_backtest_last_run_timestamp_seconds`,
`tradingdots_reports_total`, `tradingdots_paper_running`, `tradingdots_paper_orders{state}`,
`tradingdots_paper_deployed_quote`, `tradingdots_paper_free_cash_quote`.

## Rollback

`app.storage.database.rollback(target, to_version=2)` applies `0003_market.down.sql` (development
and test only; it deletes all Phase 5 data). Verified against a real PostgreSQL: after rollback the
market and paper tables are gone and the Phase 4 `pairs` tables remain. In an environment with
data, restore from backup instead; do not run the down migration.

## Known limits (not solved here)

Flatten/dust write-off and a kill switch are not built (no live orders exist to flatten). Fill
simulation ignores queue position and the order book. Real Coinbase candle shapes are unverified
(AS-C1): all fixtures are synthetic. The strategy and fee model are engineering assumptions and no
result here predicts real performance.

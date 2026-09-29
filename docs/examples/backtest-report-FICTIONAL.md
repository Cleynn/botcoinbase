> **FICTIONAL EXAMPLE.** Generated from a constructed, deterministic synthetic price series for product `FICT-USDC` (not Coinbase data; snapshot id and file hashes are placeholders). It shows the report format only. Its numbers say nothing about any real market.

# Backtest report: FICT-USDC

**MODE: BACKTEST | LIVE TRADING: BLOCKED**

This is a simulation on a frozen dataset. It is not real trading and not a promise of profit.

## Provenance
- Report SHA-256 (of the JSON body): `13f9425850a36565fc3cd89b3aaf6dcd047877a206cc9820d015d6cfba9a61ec`
- Dataset snapshot: `00000000-0000-0000-0000-00000000f1c7` (8640 candles, 0 gaps, 0 missing intervals)
- Range: 2026-05-28T20:30:00Z to 2026-06-27T20:30:00Z (UTC)
- Dataset file SHA-256: `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff`
- Dataset content SHA-256: `eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`
- Engine version: 5.0.0; priced from: unified USD book

- Config SHA-256 (OPERATOR): `8cfd11c2bdecce00c1b8e71328ce181d813f7500ef9b24177c1ca7fa714bf8d9`
- Config SHA-256 (STRESS): `2a3906313e00d3eaa41b78004239d18c7d6723bdf424ba9521ab39cb4c8e45ed`

## Results

| Metric | OPERATOR | STRESS |
|---|---|---|
| Fee rate | 0.002 | 0.006 |
| Net P&L (USDC) | -0.771649 | -0.981649 |
| Return | -1.5433% | -1.9633% |
| Realised P&L | 0.097255 | -0.042745 |
| Unrealised P&L | -0.868904 | -0.938904 |
| Fees paid | 0.12 | 0.33 |
| Max drawdown | 4.2221% | 4.3621% |
| Buy and hold (context) | -0.07326 | -0.08069 |
| Fills | 3 | 3 |
| Partial fills | 0 | 0 |
| Touched, not filled | 8 | 8 |
| Cycles completed | 1 | 1 |
| Stale cancels (age / gap) | 1 / 0 | 1 / 0 |
| Post-only rejects | 0 | 0 |
| Capital-limit blocks | 0 | 0 |
| Breakout stops / halts | 1 / 0 | 1 / 0 |
| Decisions GRID / NO_TRADE | 1 / 100 | 1 / 100 |
| End phase | STOPPED | STOPPED |
| Open inventory / cost | 0.1698 / 17.239322 | 0.1698 / 17.309322 |

## Why NO_TRADE

- INSUFFICIENT_HISTORY: 72 evaluations
- TREND_UP: 28 evaluations

## Limitations

- Simulated with closed five-minute candles only: no order book, queue position or intra-candle order is modelled.
- Fills are pessimistic by construction (price must trade through the limit, volume-capped, delayed one candle) but real fills can differ in either direction.
- Fees are operator-attested, not read from Coinbase; the stress scenario uses the configured stress maker fee.
- Synthetic or historical results do not predict future results and are not a profit guarantee.
- The strategy never sells holdings at market; an unfinished position is marked to the last close, not realised.
- Capital growth and regridding are disabled; the deployment cap is a fixed 35 USDC and the protected reserve at least 15 USDC.

Growth policy: disabled.

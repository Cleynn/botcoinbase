# Live trading: how it works and how to operate it

Status: implemented, tested against the scripted FAKE exchange only. **Never run against the real
Coinbase API yet** (DEC-026). Start with a tiny configuration.

## Moving parts
| What | Where |
|---|---|
| Mode BACKTEST / PAPER / LIVE | Bot page, "Trading mode" (phrase `SWITCH TO <MODE> MODE`) |
| Pairs in parallel, grid lines, USDC per grid, invested cap, reserve, largest order | Bot page, "Trading configuration" (phrase `UPDATE TRADING CONFIGURATION FOR <MODE> MODE`) or `live config` |
| Which pairs trade | Bot page, trading configuration: tick the pairs to trade in LIVE mode (validated pairs only). A chosen pair is traded only while it is active: activate it on the Pairs page |
| The runner | `docker compose --profile live up -d live` (runs `live run`) |
| Arming | `live arm --hours N --confirm "ARM LIVE TRADING"` (1 to 24 h) |

Edits need the bot PAUSED. Editing the LIVE numbers or changing the mode ends any arming.

## Order of operations
1. `scripts/vps/check.sh` all OK; the key installed with `scripts/vps/06-install-key.sh` (View + Trade, never Transfer).
2. `docker compose --profile discovery run --rm batch live check` (read-only: key permissions, USDC).
3. `... batch live baseline --confirm "RECORD LIVE BASELINE"` (once: today's balances are the starting point).
4. On the Bot page set the LIVE configuration (start small, for example 1 pair, 3 lines, 12 per grid, 12 per order, reserve 15, invested 35), activate the pair(s), switch the mode to LIVE.
5. Start the runner. It recovers and pauses the bot; it reports `idle (BOT_NOT_RUNNING)`.
6. Bot page: RESUME BOT AFTER RECONCILIATION (needs a current successful reconciliation).
7. `... batch live arm --hours 4 --confirm "ARM LIVE TRADING"`. Only now can orders flow.

To stop: PAUSE BOT (no new orders, existing ones stay), CANCEL KNOWN BOT ORDERS, or the kill switch (blocks everything, cancels the bot's known orders, never sells). `live disarm` revokes the arming.

## What the runner does each tick
Gates (mode LIVE, bot RUNNING, kill switch off, armed) -> reconcile -> for each pair that is chosen for LIVE and active: refresh candles, start a grid from the strategy when it says GRID (sized from the venue's USDC above the reserve) -> for each cell: BUY at its line while the price is above it and inside the band; after a fill, SELL one line up; after the sell, repeat. When the price leaves the band the open BUYs are cancelled (never a market sell) and the grid ends once nothing is open or held. Every order passes the risk engine and the database authorization, so the reserve, caps, fund check and kill switch apply to each one.

## What is not covered yet
Real API response shapes, WebSocket feed, a view of live grids on the Bot page (`live status`), restoring the relaxed protections listed in DEC-026.

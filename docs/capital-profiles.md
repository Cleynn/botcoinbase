# Capital profiles and funds-based sizing (Phase 9)

The bot sizes every grid and every order from what is actually available, inside a named policy.
There is no "starting capital" constant.

## The four numbers (a profile)
| Value | Meaning |
|---|---|
| allocation cap | the most of the account the bot may consider |
| protected reserve | USDC that is never deployed (at least 20% of the cap) |
| maximum deployment | the most committed across the active grid (open buy reserves plus inventory cost) |
| maximum per order | the most in one order |

| Profile | Cap | Reserve | Deployment | Per order |
|---|---|---|---|---|
| `pilot` (default) | 50 | 15 | 35 | 12 |
| `expanded` | 100 | 25 | 75 | 25 |
| `medium` | 250 | 50 | 150 | 50 |
| `research` | 0 | 0 | 0 | 0 (never deploys) |

`pilot` equals the former hard constants exactly. Profiles are code (`app/capital/profiles.py`) and an
identical, immutable database table (`capital_profiles`, migration 0007; a test keeps them equal).
No runtime role, and not even the owner, can change a row. There is no `use_full_available_balance`.

## Usable capital
`usable = max(0, min(min(available, cap) - reserve, max_deployment - committed))`, where `committed`
is quote held by open orders plus the cost of inventory the bot holds. `available` is the USDC the
source reports, already net of holds. `usable <= 0` means NO_TRADE (`NO_DEPLOYABLE_CAPITAL`). Extra
deposits never widen exposure; a short balance shrinks the grid; nothing exceeds the profile.

Sources of `available`:
- **Exchange**: `exchange_funds(reader.list_accounts)` reads the single USDC account through the
  read-only adapter. No account, two accounts, a negative figure or a failed read is
  `FundsUnavailable`, which blocks BUY orders (`FUNDS_UNAVAILABLE`); it never reads as zero or as the
  ledger's number. A BUY that does not fit `usable` is blocked (`INSUFFICIENT_FUNDS`).
- **Paper**: cash less the quote reserved by open paper buys.

**Not verified:** the exchange read has only run against the FAKE test double. This build has no
signer and no credentials (`NullSigner`, factory returns None), so nothing reads a real Coinbase
account. The pipeline is ready for it; a real read needs Phase 11 credentials and its own review.

## Grid sizing
`decide(..., funds=...)` passes `capital_policy(policy, funds)` to the grid builder: the deployment cap
becomes the usable quote. Each cell is sized from its share, the stress-fee allowance and one
increment, rounded down to the product's `base_increment`, then checked against `base_min_size`,
`quote_min_size` and `base_max_size`. If a grid falls below a minimum or cannot earn its fees, fewer
lines are tried (3 to 5) before returning NO_TRADE. With full pilot funds the plans are identical to
the former static sizing (the backtest and paper suites are unchanged). Backtest and paper use the
same `Trader`, so both size from their ledger's available cash.

## Selecting a profile (PAPER and LIVE)
Bot page, ADMIN only, bot PAUSED: authorization, CSRF, fresh password, typed phrase, audit, command,
outcome audit. Phrases: `UPDATE CAPITAL LIMITS FOR PAPER MODE` and `UPDATE CAPITAL LIMITS FOR LIVE
MODE`. An unknown profile is refused before the reauthentication is spent. The database enforces it
too: only the web actor, only PAUSED to PAUSED, never combined with another control change, one profile
at a time, and it is recorded in `bot_control_history` (`PAPER_PROFILE` / `LIVE_PROFILE`).

- The PAPER profile limits the paper ledger (deposit = cap), `td_authorize_order`, and the paper
  capital triggers. The deposit is fixed at the first paper start; later profile changes change the
  limits, not the ledger.
- The LIVE profile is **recorded only**. LIVE is unrepresentable in this build; selecting a profile
  cannot open the live gate, which stays `LIVE TRADING BLOCKED`.
- A profile change creates no order and queues no command.

## Limits and follow-ups
- `safety.per_order_cap` is now optional (None = the profile's cap) and may only tighten it.
- The YAML `pair_policy.capital_profile` (default `pilot`) bounds the static policy used by backtests
  and pair validation; the runtime PAPER limits come from the database selection.
- The daily loss ($10) and drawdown (20%) ceilings in SQL are not profile-scaled; the configurable
  limits can only tighten them.
- Capital growth and regridding stay disabled.

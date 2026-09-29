# Pair management (Phase 4)

Discover Coinbase spot products through **public** market data, propose candidates, validate them, and
manage their lifecycle. Nothing here reads an account, places or cancels an order, or enables live
trading. Live states are not representable.

## Who does what
| Actor | Role in database | Can |
|---|---|---|
| Host runner (`make pairs-*`, profile `discovery`) | `td_ctl` | refresh products and metadata, propose the watchlist, run validation, expire eligibility |
| ADMIN (web) | `td_app` | add a candidate, queue validation, pause/deactivate, and (full chain) activate, resume, disable, re-enable, archive |
| VIEWER (web) | `td_app` | read only |

The web tier never contacts Coinbase (it has no route and does not import the client). The runner reaches
Coinbase only through `egress-proxy`, which allows `CONNECT api.coinbase.com:443` and nothing else.

## Discovery and policy
`pairs discover` reads `/time` and `/market/products?product_type=SPOT`, keeps products that are SPOT, venue `CBE`,
quoted in `USDC`, with an id matching `^[A-Z0-9]+-USDC$` (max 24 chars), ranks them by 24 h quote volume, caps at 500,
and stores **static rules only** (status text, six flags, increments, min/max sizes, alias fields). Display names,
prices and volumes are never stored. A new metadata snapshot exists only when content changes.

Default candidates = active USDC spot products (`status` exactly `online`, no blocking flag) whose base is not a stable
or pegged asset. Blocking flags: `is_disabled`, `trading_disabled`, `cancel_only`, `auction_mode`, `post_only`
(`limit_only` is allowed). Initial watchlist: BTC-USDC, ETH-USDC, SOL-USDC (`pairs seed`).

Adding a pair (`POST /pairs/candidates`, or `pairs seed`) creates **PROPOSED** only, at most 20 open pairs, one open
pair per product; adding an archived product again creates a new candidate linked to the old one.

## Validation (14 checks, each PASS / FAIL / INCONCLUSIVE with a stored reason)
PRODUCT_STATUS, QUOTE_CURRENCY, BASE_NOT_STABLE, DATA_BASIS, METADATA_FRESHNESS, CLOCK_SYNC, INCREMENTS, PRECISION,
MINIMUMS, HISTORY_AVAILABILITY (daily candles), OHLCV_QUALITY (five-minute candles), LIQUIDITY_SPREAD, FEE_VIABILITY,
CAPITAL_FEASIBILITY (50 / 15 / 35 USDC). Any FAIL gives FAIL, otherwise any INCONCLUSIVE gives INCONCLUSIVE, otherwise
PASS. PASS gives PAPER_ELIGIBLE; the others give RESEARCH_ONLY. A run stores all checks, the thresholds fingerprint,
the metadata snapshot used and an expiry (24 h). Thresholds live in `config/pair-policy.yaml` with hard bounds.

Details worth knowing:
- Candles are *closed* only if `start + granularity <= server time - 3 s` (no closed flag exists, CB-5).
- `-USDC` products priced from the unified `-USD` book (alias) say so on the page and in the evidence (CF-11).
- **Fees are operator-attested.** No primary fee schedule has been retrieved (CF-15). Until `fees.operator_maker_rate`
  and `fees.attested_on` are set in `config/pair-policy.yaml` (and again every 30 days), FEE_VIABILITY is
  INCONCLUSIVE and **no pair can become PAPER_ELIGIBLE**. This is deliberate.
- FEE_MODEL_V1 is an engineering assumption, not an exchange fact: grid spacing = 25th-percentile daily range / levels must
  exceed 2 x maker fee + minimum edge, for both the attested and the stress (0.60 %) rate. It will be replaced by the real
  strategy's numbers in Phase 5. Expect BTC-class pairs to fail the stress rate; that is normal.
- Depth and spread need a valid book; without one the price-dependent checks are INCONCLUSIVE, never PASS.

## Lifecycle
DISCOVERED (a product with no pair row) -> PROPOSED -> VALIDATING -> RESEARCH_ONLY | PAPER_ELIGIBLE -> PAPER_ACTIVE;
also PAUSED, DISABLED, ARCHIVED. LIVE_ELIGIBLE and LIVE_ACTIVE are **not representable** (no enum member, database CHECK).
The table below is the code table; migration 0002 seeds the same rows into `allowed_transitions` and a trigger refuses any
other change (a test compares them). "Chain": NONE = runner, CSRF = ADMIN + CSRF, RESTRICTIVE = ADMIN + CSRF (makes things
safer), FULL = ADMIN + CSRF + fresh single-use password confirmation + exact typed phrase + audit.

| # | From | To | Who | Chain | Audit event |
|---|---|---|---|---|---|
| 2 | PAUSED | VALIDATING | HOST | CSRF | `pair.validation_started` |
| 2 | PAUSED | VALIDATING | WEB | CSRF | `pair.validation_started` |
| 2 | PROPOSED | VALIDATING | HOST | CSRF | `pair.validation_started` |
| 2 | PROPOSED | VALIDATING | WEB | CSRF | `pair.validation_started` |
| 2 | RESEARCH_ONLY | VALIDATING | HOST | CSRF | `pair.validation_started` |
| 2 | RESEARCH_ONLY | VALIDATING | WEB | CSRF | `pair.validation_started` |
| 3 | VALIDATING | RESEARCH_ONLY | HOST | NONE | `pair.research_only` |
| 4 | VALIDATING | PAPER_ELIGIBLE | HOST | NONE | `pair.paper_eligible` |
| 6 | PAPER_ELIGIBLE | VALIDATING | HOST | NONE | `pair.eligibility_expired` |
| 7 | PAPER_ELIGIBLE | PAPER_ACTIVE | WEB | FULL | `pair.activated_paper` |
| 8 | PAPER_ACTIVE | PAUSED | HOST | RESTRICTIVE | `pair.paused` |
| 8 | PAPER_ACTIVE | PAUSED | WEB | RESTRICTIVE | `pair.paused` |
| 9 | PAUSED | PAPER_ACTIVE | WEB | FULL | `pair.resumed_paper` |
| 10 | PAUSED | PAPER_ELIGIBLE | WEB | CSRF | `pair.deactivated` |
| 12 | PAPER_ELIGIBLE | DISABLED | WEB | FULL | `pair.disabled` |
| 12 | PAUSED | DISABLED | WEB | FULL | `pair.disabled` |
| 12 | PROPOSED | DISABLED | WEB | FULL | `pair.disabled` |
| 12 | RESEARCH_ONLY | DISABLED | WEB | FULL | `pair.disabled` |
| 12 | VALIDATING | DISABLED | WEB | FULL | `pair.disabled` |
| 13 | DISABLED | VALIDATING | WEB | FULL | `pair.reenabled` |
| 14 | DISABLED | ARCHIVED | WEB | FULL | `pair.archived` |
| 14 | PAPER_ELIGIBLE | ARCHIVED | WEB | FULL | `pair.archived` |
| 14 | PAUSED | ARCHIVED | WEB | FULL | `pair.archived` |
| 14 | PROPOSED | ARCHIVED | WEB | FULL | `pair.archived` |
| 14 | RESEARCH_ONLY | ARCHIVED | WEB | FULL | `pair.archived` |
| 14 | VALIDATING | ARCHIVED | WEB | FULL | `pair.archived` |

Not allowed (denied and audited as `pair.transition_denied`): anything from ARCHIVED; PAPER_ACTIVE to DISABLED or
ARCHIVED (pause first, and a previously active pair also needs reconciliation to show it clean, which cannot be shown yet,
so it stays refused); any path to a LIVE state.

Typed phrases (ASCII, exact, case-sensitive): `DISABLE PAIR <PRODUCT_ID>`, `ARCHIVE PAIR <PRODUCT_ID>`,
`REENABLE PAIR <PRODUCT_ID>`, `ACTIVATE PAPER PAIR <PRODUCT_ID>`, `RESUME PAPER PAIR <PRODUCT_ID>`.

## Activation
Separate from creation and validation. Guards (all re-checked on submit): a PASS run that is the current eligibility
evidence, unexpired, on the metadata that is current now; metadata younger than the freshness limit; product still OK;
no other PAPER_ACTIVE or PAUSED pair; and the runtime gate. **The runtime gate refuses in this build** (`BOT_STATE_UNAVAILABLE`,
`MODE_NOT_PAPER`): activation needs a PAUSED bot in PAPER mode and neither exists until later phases. The chain, the
single-active database index and the race behaviour are implemented and tested with an injected open gate.

## Operating it
```
make pairs-discover           # refresh the catalogue (public data)
make pairs-seed               # BTC/ETH/SOL as PROPOSED and queue validation
make pairs-validate           # expire stale eligibility, then validate queued pairs
make pairs-list
```
Web: `/pairs`, `/pairs/products`, `/pairs/<id>`. Set the fee attestation first or every result is INCONCLUSIVE.

## Metrics
`tradingdots_pair_candidates_total`, `tradingdots_pair_state_total{state}` (contract names, real database counts) and
`tradingdots_pair_metadata_age_seconds` (extension). No product identity appears in any label.

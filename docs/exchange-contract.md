# Exchange contract (Coinbase Advanced Trade), source-class tagged

Status: **SEED (Phase 1.0)**. Facts are copied from `baseline/TRADINGDOTS_HANDOFF.md` section 2.5 and
have **not** been re-verified in this repository. Snapshot hashes: none recorded yet.

Source classes: **[P-F]** primary page, full text. **[P-S]** primary page, excerpt only. **[3P]** third party.
Only primary facts justify design rules (BI-09). Docs examples are never authoritative.

## Facts (CF-n)
| # | Fact | Class |
|---|---|---|
| CF-1 | REST base `https://api.coinbase.com/api/v3/brokerage` serves public and private methods. WebSocket not used. | [P-F] |
| CF-2 | Create Order accepts leverage, margin_type, attached orders, many order types. Only `limit_limit_gtc` with `post_only=true` is allowed here. | [P-F] |
| CF-3 | A duplicate `client_order_id` returns the existing order. Scope, lifetime, format, length undocumented in baseline. | [P-F] |
| CF-4 | `success:true` with `UNKNOWN_FAILURE_REASON` means the order was accepted. | [P-S] |
| CF-5 | Cancel Orders takes exchange `order_ids`, per-order results. HTTP 200 is not cancellation. `CANCEL_QUEUED` is live. No spot cancel-all. Batch limit 100. | [P-F],[P-S] |
| CF-6 | Order statuses: PENDING, OPEN, FILLED, CANCELLED, EXPIRED, FAILED, UNKNOWN_ORDER_STATUS, QUEUED, CANCEL_QUEUED, EDIT_QUEUED. List Orders `sort_by` pagination is unstable. | [P-S] |
| CF-7 | Accounts: one per currency, `available_balance` and `hold`. | [P-S] |
| CF-8 | Public endpoints: products, product, candles, ticker, product_book, `/time`. 1-second cache. | [P-S],[3P] |
| CF-9 | Candles: nine granularities, max 350 per request, UNIX start/end, no closed flag. | [P-F] |
| CF-10 | Product flags: is_disabled, cancel_only, limit_only, post_only, trading_disabled, auction_mode; `status` undocumented free string. | [P-F] |
| CF-11 | `-USDC` market data equals the matching `-USD` book (except USDT-USDC, EURC-USDC). | [P-F] |
| CF-12 | Scopes: view / trade / transfer per endpoint. `GET /key_permissions` returns can_view, can_trade, can_transfer, portfolio_uuid, portfolio_type. | [P-F] |
| CF-13 | CDP keys only; JWT `iss=cdp`, `sub`, `nbf`, `exp` +120 s, `uri` claim, header `kid` + `nonce`. | [P-S] |
| CF-14 | Sandbox static, unauthenticated, Accounts and Orders only. | [P-S] |
| CF-15 | No primary fee schedule retrieved. | [3P],[P-S] |
| CF-16 | Rate limits: legacy page only (private 30/s, public 10/s). | [P-S] |
| CF-17 | Derivatives gateway move (Oct 1 2026 or Sep 9). Spot unaffected. | [P-F],[P-S] |

## Open items (AS-C1..AS-C8), all unverified
AS-C1 `-USDC` REST behavior; AS-C2 candle depth; AS-C3 client-ID scope/lifetime/format; AS-C4 List Orders
`client_order_id` and partial fills; AS-C5 rate limits; AS-C6 account tradability; AS-C7 JWT algorithm;
AS-C8 derivatives cutover date.

## Observations from the 2026-09-29 session (search snippets only, NOT verified; see DEC-004)
Direct fetch of docs.cdp.coinbase.com was denied by the sandbox egress proxy. Search excerpts suggested:
- Create Order doc describes `client_order_id` as a client-generated UUID that must be unique (informs AS-C3; scope/lifetime still open).
- `limit_limit_gtc` lists `rfq_disabled` among its parameters (CF-2 note said examples-only). Keep strict request models.
- Candles: 350 default and max confirmed in excerpt (CF-9).
- JWT claims and 120 s expiry match CF-13. Docs recommend Ed25519, but the Advanced Trade / Coinbase App SDKs
  reportedly support only ES256; ES256 stays the live-lab default (DX-2).
- A "Coinbase App Rate Limiting" page exists (`/coinbase-app/api-architecture/rate-limiting`); check in Phase 9.0 (AS-C5).
- Current doc paths: `/coinbase-app/advanced-trade-apis/...`, `/api-reference/advanced-trade-api/rest-api/{orders,products,public}/...`,
  `/get-started/authentication/jwt-authentication`.

## Phase 4 usage (public client)
The runner uses exactly the five CB-2 paths through `app/adapters/coinbase_public.py`. **Response shapes are those documented above
and remembered from the public docs; none was verified against the live API from this repository** (the sandbox cannot reach
api.coinbase.com), so AS-C1 stays open and the fixtures in `tests/coinbase_fakes.py` are **synthetic**. Unverified assumptions the
code makes and that must be checked against a real response before relying on validation results: `product_venue == "CBE"` for
spot products; `status == "online"` is the only tradable status; `alias` holds the `-USD` product for a `-USDC` product;
`approximate_quote_24h_volume` exists for ranking; candles arrive newest first with string numbers; the book carries `time`.
If any of these is wrong, discovery stores nothing or validation reports INCONCLUSIVE; it never passes by default.

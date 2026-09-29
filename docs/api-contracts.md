# HTTP contracts (Phase 2)

There is no JSON or third-party API. Every route returns HTML (or an HTML fragment). `/docs`, `/redoc`, `/openapi.json` are disabled.

## Global rules (enforced app-wide, not per route)
1. **Default deny**: every route must declare `public` or a permission; an undeclared route answers 403 and never runs.
2. **CSRF on POST/PUT/PATCH/DELETE**: same-origin signal (`Origin` must equal the host; else `Sec-Fetch-Site: same-origin|none`; no signal = refuse) plus a token (`csrf_token` form field or `X-CSRF-Token`). Session-bound HMAC token; `/login` uses a pre-session token bound to the `__Host-td_login` cookie. Failure: 403, audited (throttled).
3. **Bodies** are capped at 16 KiB (413). Forms reject unknown fields (400).
4. Unauthenticated access to a protected page: `303 /login` (`HX-Redirect` for HTMX). Insufficient role: 403 + `authz.denied` audit event.
5. Cookies: `__Host-td_session` (Secure, HttpOnly, SameSite=Strict, Path=/, no Domain, no expiry) and `__Host-td_login` (same flags, 1 h, only while the login form is open).
6. Responses: `Cache-Control: no-store`, CSP (`default-src 'none'`, `script-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`), `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. **Never `no-referrer`**: browsers then send `Origin: null` on our own form posts and every login would fail the CSRF check (found in browser validation).

## Routes
| Method | Path | Access | Form fields | Success | Failures |
|---|---|---|---|---|---|
| GET | `/healthz` | public | | 200 `{"status":"ok"}` | |
| GET | `/login` | public | | 200 form (303 `/` if signed in) | |
| POST | `/login` | public | `csrf_token, username, password` | 303 `/` + session cookie | 401 generic; 429 + `Retry-After`; 403 CSRF; 400 malformed |
| POST | `/logout` | logout | `csrf_token` | 303 `/login`, cookie cleared, `Clear-Site-Data` | 403 |
| GET | `/` | view_dashboard | | 200 | 303 |
| GET | `/partials/status` | view_dashboard (no idle extension) | | 200 fragment | 200 `HX-Redirect` |
| GET | `/security` | view_own_security | `?msg=` (fixed codes) | 200 | 303 |
| POST | `/security/password` | change_own_password | `csrf_token, current_password, new_password, confirm_password` | 303 `/security?msg=password_changed`, **new session cookie**, all other sessions ended | 400 (`bad_current`, `mismatch`, `same`, `policy`), 429 |
| POST | `/security/reauth` | reauthenticate | `csrf_token, password` | 303 `?msg=reauth_ok` (120 s, single use) | 400, 429 |
| POST | `/security/sessions/revoke` | revoke_own_sessions | `csrf_token, session_id` | 303 | 400 (not yours / current / unknown) |
| POST | `/security/sessions/revoke-others` | revoke_own_sessions | `csrf_token` | 303 | |
| POST | `/security/users/revoke-sessions` | revoke_user_sessions (ADMIN) | `csrf_token, user_id, confirmation` | 303 | 400 `reauth_required` / `phrase_mismatch`, 404 unknown user, 403 |
| GET | `/audit` | view_audit (ADMIN) | `?code=` (catalogue value), `?before=` (int) | 200 | 400 invalid filter, 403 |

Absent by design: signup, invitation, password reset, user creation, any bot/pair/exchange/config/package/proposal route.

## Sensitive action pattern (ADMIN revoke sessions)
authorization → CSRF/Origin → fresh, single-use reauthentication (`/security/reauth`, 120 s) → exact typed phrase `REVOKE SESSIONS FOR <USERNAME>` (ASCII byte match) → state change + audit event in one transaction. A wrong phrase does not consume the reauthentication.

## Audit catalogue
`auth.login.{success,failure,throttled}`, `auth.logout`, `session.{expired,revoked,rejected}`, `auth.password.{changed,change_failed}`, `auth.reauth.{success,failure,throttled}`, `authz.denied`, `csrf.rejected`, `admin.{sessions_revoked,sessions_revoke_denied,created,password_rotated}`. Records hold actor, result, reason code, a keyed hash of the client (never the address), request id and a scalar-only detail (secret-looking keys are refused). The log is append-only (database triggers) and hash-chained; `/audit` recomputes the chain on every view.

## Pairs (Phase 4)
Reads need `view_pairs` (any signed-in user); every change needs `manage_pairs` (ADMIN). All writes are CSRF-protected POSTs
with strict forms (unknown fields rejected). A `version` field carries the pair version the page showed; a mismatch is refused
(409, audited `STALE_VERSION`). GET never writes.

| Method | Path | Access | Form fields | Success | Failures |
|---|---|---|---|---|---|
| GET | `/pairs` | view_pairs | `?msg=` | 200 list, lifecycle, counts | 303 |
| GET | `/pairs/products` | view_pairs | `?show=default\|all`, `?page=1..1000` | 200 discovered products | 400 |
| GET | `/pairs/{id}` | view_pairs | `?msg=` | 200 detail (ADMIN also sees actions and the audit timeline) | 404 |
| POST | `/pairs/candidates` | manage_pairs | `csrf_token, product_id` (product UUID) | 303 `/pairs/{id}?msg=pair_proposed` | 400, 404, 409 (duplicate, cap) |
| POST | `/pairs/{id}/validate` | manage_pairs | `csrf_token, version` | 303, state VALIDATING | 404, 409 |
| POST | `/pairs/{id}/pause` | manage_pairs | `csrf_token, version` | 303 | 404, 409 |
| POST | `/pairs/{id}/deactivate` | manage_pairs | `csrf_token, version` | 303 | 404, 409 |
| GET | `/pairs/{id}/{action}/request` | manage_pairs | | 200 confirmation page (no write) | 404 |
| POST | `/pairs/{id}/{action}/reauth` | manage_pairs | `csrf_token, version, password` | 303 to the request page | 400, 429 |
| POST | `/pairs/{id}/{action}/confirm` | manage_pairs | `csrf_token, version, confirmation` | 303 `/pairs/{id}?msg=pair_<done>` | 400 (phrase, reauth), 404, 409 (stale, illegal, guard) |

`action` is one of `activate, resume, disable, reenable, archive`. There is no DELETE route: pairs are archived, never deleted.
New audit events: `product.discovered`, `pair.proposed`, `pair.validation_started`, `pair.research_only`, `pair.paper_eligible`,
`pair.eligibility_expired`, `pair.activated_paper`, `pair.paused`, `pair.resumed_paper`, `pair.deactivated`, `pair.disabled`,
`pair.reenabled`, `pair.archived`, `pair.transition_denied`.

## Reports (Phase 5)
All routes are GET, read-only, permission `view_reports` (ADMIN and VIEWER). No write route exists.

| Method | Path | Result | Errors |
|---|---|---|---|
| GET | `/reports?page=N` | 200 list, newest first, each row labelled BACKTEST or PAPER | 400/422 bad page |
| GET | `/reports/{id}` | 200 detail page (all text escaped) | 400/404 |
| GET | `/reports/{id}/json` | 200 `text/plain`, `Content-Disposition: attachment`, canonical JSON | 404 |
| GET | `/reports/{id}/md` | 200 `text/plain`, attachment, Markdown | 404 |

The overview gains read-only market rows (latest report, data freshness) marked BACKTEST/PAPER.
Audit events added: `market.ingested`, `market.snapshot_created`, `backtest.completed`, `report.created`,
`paper.started`, `paper.stopped`, `paper.stepped`. Details: `docs/backtest-and-paper.md`.

## Review packages (Phase 6)
All routes need permission `manage_review_packages` (ADMIN only; VIEWER gets 403, audited as `authz.denied`). The feature is DISABLED by default. Details: `docs/review-packages.md`.

| Method | Path | Effect | Errors |
|---|---|---|---|
| GET | `/review` | 200 status, request form (when enabled), package list | |
| GET | `/review/enable/request?retention_days=N` | 200 confirmation page (no write) | 422 |
| POST | `/review/enable/reauth` | `csrf_token, retention_days, password`; 303 back to the request page | 400, 429 |
| POST | `/review/enable/confirm` | `csrf_token, retention_days, confirmation` (`ENABLE READ-ONLY REVIEW PACKAGES`); 303 `/review?msg=review_enabled` | 400 (phrase, reauth), 409 |
| GET/POST | `/review/disable/request`, `/reauth`, `/confirm` | same chain, phrase `DISABLE READ-ONLY REVIEW PACKAGES` | 400, 409 |
| GET | `/review/packages/create/request?period_start&period_end&scope=..` | 200 confirmation page (no write) | 400, 422 |
| POST | `/review/packages/create/reauth` | fields + `password` | 400, 429 |
| POST | `/review/packages/create/confirm` | fields + `confirmation` (`CREATE READ-ONLY REVIEW PACKAGE`); 303 `/review/packages/{id}?msg=review_requested` | 400, 409, 429 (limits) |
| GET | `/review/packages/{id}` | 200 metadata only (never package content) | 404 |
| POST | `/review/packages/{id}/verify` | `csrf_token`; 303 with result | 404, 409 |
| POST | `/review/packages/{id}/download` | `csrf_token`; 200 `application/octet-stream` attachment, `nosniff`, `no-store`, sandbox CSP; verified first | 404, 409 (not READY, disabled, corrupt) |

There is no GET download, no static route and no DELETE. Audit events added: `review.enabled`, `review.disabled`, `review.requested`, `review.generating`, `review.ready`, `review.failed`, `review.corrupt`, `review.expired`, `review.verified`, `review.downloaded`, `review.cleanup`, `review.denied`.

## Proposals (Phase 7)
All routes need permission `manage_proposals` (ADMIN only; VIEWER 403, audited as `authz.denied`). Every POST needs CSRF and a same-origin request. Nothing is written on GET.
| Method | Path | Notes |
|---|---|---|
| GET | `/review/proposals` | status, list |
| GET/POST | `/review/proposals/import-enable/request`, `/reauth`, `/confirm` | phrase `ENABLE UNTRUSTED PROPOSAL IMPORT` |
| POST | `/review/proposals/import-disable` | CSRF only |
| GET | `/review/proposals/import/request` | two-step page |
| POST | `/review/proposals/import/reauth` | password |
| POST | `/review/proposals/import/confirm` | multipart, exactly one `file` part **or** `text`, plus `csrf_token`, `confirmation` (`IMPORT UNTRUSTED LLM PROPOSAL`); 303 to the proposal, 400/413/429 |
| GET | `/review/proposals/{id}` | escaped detail; 404 |
| POST | `/review/proposals/{id}/review` | `notes`; CSRF only |
| POST | `/review/proposals/{id}/close` | `reason`; CSRF only |
| GET/POST | `/review/proposals/{id}/change-request/request`, `/reauth`, `/confirm` | phrase `CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <id>`, `change_type`, `impact_assessment`, `ceilings_unaffected` |
| GET/POST | `/review/proposals/{id}/attest/{kind}` (implemented, backtested or paper-validated)`/request`, `/reauth`, `/confirm` | reauth, no phrase; `reference` or `report_ids` |
There is no download, raw-view, apply, approve-and-run or DELETE route. Audit events: `proposal.import_enabled|import_disabled|imported|validating|validated|rejected|reviewed|change_request_created|implemented|backtested|paper_validated|closed|cleanup|denied`.

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

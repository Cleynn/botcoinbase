# Web UI specification (Phase 2)

Server-rendered Jinja2 + one vendored HTMX file. No SPA, no Node, no CDN, no inline script or style (CSP forbids them). Every page works without JavaScript.

## Pages
| Path | Access | Content |
|---|---|---|
| `/login` | public | Username/password form, hidden CSRF token, generic errors. No signup, invitation or reset. |
| `/` Overview | any signed-in user | "Known values" (bot mode, live trading BLOCKED, signed-in user, session end, idle timeout) and "Not available yet" cards (every unmeasured value reads `Unknown` or `Not available`), Grafana link (never embedded), server-status tile refreshed by HTMX every 30 s. |
| `/security` | any signed-in user | Own account, own active sessions (revoke one / all others), change password, confirm password (fresh reauthentication). ADMIN also sees users and the revoke-sessions form. |
| `/audit` | ADMIN | Newest-first audit events, event-type filter, cursor pagination (50 per page), audit-chain verification status. |
| `/partials/status` | any signed-in user | HTML fragment for the status tile. Does not extend the idle timeout. |
| `/healthz` | public (Caddy answers 404 externally) | `{"status":"ok"}` |

## Layout rules
- Persistent banner on every page: `MODE: BACKTEST|PAPER` and `LIVE TRADING: BLOCKED`.
- Navigation: Overview, Bot, Pairs, Reports, LLM Review (the last four are visibly disabled: "not available yet"), Audit (ADMIN), Security, Grafana (external link, `rel="noopener noreferrer"`), Sign out (POST form).
- No control on any page changes bot, exchange, pair or configuration state. The only forms are the auth/session forms listed in `api-contracts.md`.
- Times are UTC. Dashboard values are either real or `Unknown`/`Not available`; nothing is estimated.

## Accessibility and responsiveness
Skip link, one `<h1>` per page, `<main>`, labelled inputs, `aria-current`, `role="alert"` on errors, visible focus, 2.75 rem targets, prefers-color-scheme and reduced-motion support, tables scroll inside their wrapper (no page-level horizontal scroll at 375 px: verified in Chromium).

## Flash messages
Chosen by fixed codes (`?msg=password_changed`); unknown codes are ignored. No user input is ever reflected.

## Session behaviour visible to users
Idle timeout 30 min, absolute 12 h, at most 5 sessions (oldest is ended). Logout, password change, revocation and expiry send the browser to `/login`; HTMX polling receives `HX-Redirect`.

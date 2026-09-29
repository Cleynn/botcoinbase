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

## Pairs (Phase 4)
Navigation gains **Pairs** (real page). `/pairs`: lifecycle (LIVE_ELIGIBLE and LIVE_ACTIVE shown as blocked), active pair,
open-candidate count, state counts, pair table (state badge, data basis, latest validation, validated time, metadata age with a
"stale" marker). `/pairs/products`: paginated discovered products (default candidates first view, "show all" toggle, reason a
product is not a default candidate, add button for ADMIN only). `/pairs/{id}`: lifecycle position, validity, actions with the reasons
they are currently refused, every metadata field and flag, metadata age, history state (daily history, five-minute candle
quality), the 14 validation checks with result, reason code, fixed explanation and observed values, earlier runs, state transitions,
and (ADMIN) the audit timeline; a VIEWER sees transitions without actor names and no audit timeline. Confirmation pages show the
exact phrase, a password step and a phrase step, and no form at all while a guard refuses. All exchange-derived text is escaped
by the template engine (nothing is marked safe) and reduced to a safe vocabulary before storage.

## Reports (Phase 5)
Navigation item "Reports" (ADMIN, VIEWER). List: kind, label badge (BACKTEST or PAPER), created, hash prefix. Detail: banner, provenance, tables, limitations, JSON/Markdown download links. No form, no action button. Every result is labelled BACKTEST or PAPER; nothing implies real trading.

## Review packages (Phase 6)
Navigation item "Review packages" (ADMIN only). Every page on this surface opens with the notice that a package cannot trade and cannot change the bot, any pair, risk setting, order, configuration or live setting. Overview: feature status (DISABLED by default), enable link or request form (period + scope checkboxes), package table (id prefix, state badge, period, requested, expires, size). Confirmation pages follow the two-step pattern: step 1 password (single use), step 2 exact phrase; nothing is written on GET. Package page: metadata, Verify and Download (ZIP) buttons (POST, CSRF), file list with sizes and digest prefixes. Package content is never rendered.

## Proposals (Phase 7)
Navigation item "Proposals" (ADMIN only). Every page carries the badge and notice **UNTRUSTED ADVISORY INPUT** and says a proposal cannot trade or change anything. Overview: import status (DISABLED by default), enable/disable, list. Import: two-step page (password, then one file or pasted text plus the exact phrase). Detail: state, linked package, findings in plain language with rule ids, risk assessment, evidence links, manual next steps, history, and only the forms the current state allows. All proposal text is escaped; the raw file is never shown or downloadable.

## Bot (Phase 8)
Navigation item "Bot" for every signed-in user. The page opens with the notice that it cannot create, submit, change or sell any order and shows: bot state, kill switch, breaker (reason and cooldown), startup recovery, last reconciliation and its blocking findings, the live gate panel (**LIVE TRADING BLOCKED** and every unmet condition), attempts by state, recent risk decisions in plain language, history and cancel commands. ADMIN also sees four links (pause, resume, cancel known orders, kill switch) and what currently blocks resume. Each control is the two-step page: password (single use), then the exact phrase. The overview shows real bot, kill switch, breaker, recovery and reconciliation rows.

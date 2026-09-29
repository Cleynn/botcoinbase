# Threat model (Phase 1)

Scope: the Phase 1 shell only. No trading, credentials, or business data exist yet, which bounds the impact.

| # | Threat | Mitigation in Phase 1 | Status |
|---|---|---|---|
| T1 | Accidental or malicious enabling of LIVE | `LIVE` not representable (mode allowlist, `TD_PROFILE` allowlist, unknown `TD_*` rejected, `live.example.yaml` never loaded); banner fixed to BLOCKED; no exchange code exists | Tested (`test_config.py`) |
| T2 | Internal service exposed publicly | Only Caddy publishes 80/443; internal networks; validator + tests fail on any other port, host network, docker.sock, privileged | Tested statically; **not** verified on a running host |
| T3 | Weak production config (debug, placeholder secrets, insecure cookies, bad hostnames) | `load_settings` refuses production start; `make verify-security-config` checks `.env` (incl. mode 600) | Tested |
| T4 | Secret leakage via logs/errors/health | Redacting formatter; generic error pages; `/healthz` fixed body; `.env` git-ignored; scans for key material | Tested |
| T5 | XSS / clickjacking / content sniffing | Autoescape, no inline scripts, CSP `default-src 'none'` + self, `frame-ancestors 'none'`, nosniff, htmx `allowEval=false`, vendored checksum-pinned HTMX | Tested |
| T6 | Host header abuse / unknown vhost | TrustedHost middleware; Caddy serves configured hosts only | Tested (app); Caddy unverified |
| T7 | Path traversal via static | Starlette StaticFiles; tests for encoded traversal | Tested |
| T8 | Introspection (`/docs`, `/openapi.json`, `/metrics`) | Disabled in app; 404 at Caddy | Tested (app) |
| T9 | Unauthenticated access to dashboard | Resolved in Phase 2: every page except `/login` and `/healthz` requires a session (default-deny guard, route-inventory test) | Tested |
| T10 | Compromised container | Non-root app user, `cap_drop: ALL`, `no-new-privileges`, read-only root fs, tmpfs, memory/pids limits, no egress | Config only; not runtime-verified (no Docker daemon available) |
| T11 | Supply chain (images, packages) | `uv.lock` pins Python packages; HTMX integrity-checked against the npm registry hash | **Open**: base image digests not pinned; lock has no hash-verified install in the image beyond uv defaults |
| T12 | Secret exposure through Compose environment | Secrets come from `.env` (0600) into container env (visible via `docker inspect` to Docker admins) | **Accepted for Phase 1**; file-based secrets are a later hardening |
| T13 | Single host, no independent watcher or off-host backup | Owner decision DEC-006: not implemented or planned. Risk accepted by the owner | Accepted; keeps live blocked |

Not modelled yet: authentication, CSRF, sessions, audit tampering, exchange failure modes, LLM content, uploads.

## Phase 2 additions
| # | Threat | Mitigation | Status |
|---|---|---|---|
| T14 | Credential guessing / stuffing | Argon2id, generic failure, account+client, client-wide and account-wide throttles, throttled reauth/password-change | Tested |
| T15 | Account enumeration | Identical response and equivalent work for unknown/disabled/wrong; throttle keys use the submitted name whether or not it exists | Tested |
| T16 | Session theft/fixation/replay | Opaque hashed tokens, `__Host-` HttpOnly Secure Strict cookies, rotation on login/password change, idle+absolute expiry, revocation, `Clear-Site-Data` | Tested (client and Chromium) |
| T17 | CSRF / login CSRF | Session-bound HMAC tokens, separate pre-session login token, Origin/Fetch-Metadata, SameSite=Strict, app-wide guard | Tested (incl. real cross-site form) |
| T18 | Privilege escalation / IDOR | Permission matrix, default deny, object checks on session ids, extra form fields rejected, service-level permission check | Tested |
| T19 | Audit tampering | Append-only triggers (also for the owner), hash chain verified on `/audit`, web role cannot modify history | Tested; **no off-host anchor (DEC-006)** |
| T20 | Web-process compromise creating users | Only `td_ctl` can INSERT users; but a compromised web process can still act as signed-in users and change passwords (RR-1) | Partly mitigated; live blocker |
| T21 | Lock-out of the ADMIN by attackers | Account-wide throttle can be triggered by distributed guessing; the window is 15 min and the host CLI can always rotate the password | Accepted |
| T22 | Wrong client identity behind the proxy | `X-Forwarded-For` honoured only from `trusted_proxies`; mismatch with the Compose subnet would make throttling global | Runbook warning; unverified on a host |

## Phase 3 additions
| # | Threat | Mitigation | Status |
|---|---|---|---|
| T23 | Metrics exposed publicly | Separate internal listener bound to a private address, allowed scrapers only, never proxied; Caddy 404 for `/metrics*`; no host ports for any monitoring service | Tested; Caddy/host unverified |
| T24 | Sensitive data or identities in metrics/dashboards | Fixed label sets, forbidden label names, sanitiser, no free text; dashboards checked for the same | Tested (incl. hostile input) |
| T25 | Cardinality explosion by an attacker | Route templates only, fixed value sets, 100-series cap per family | Tested |
| T26 | Monitoring used as a control plane | No Alertmanager/webhook/transport, Grafana alerting off, Prometheus lifecycle/admin APIs disabled, rules restricted to labels/annotations without actions or URLs, read-only provisioned dashboards, web tier never queries Prometheus | Tested (real Prometheus) |
| T27 | Grafana takeover | Unique admin user and required strong secrets (no defaults), login protection, no sign-up/anonymous/embedding/plugins, secure strict cookies, isolated from the app network | Config tested; Grafana not run here |
| T28 | Compromised monitoring container reaching the host | Non-root, read-only rootfs, `cap_drop: ALL`, read-only host mounts, no Docker socket or Docker data (cAdvisor included), internal networks only | Config tested; runtime unverified |
| T29 | Monitoring outage affecting the application | Listener failure is non-fatal; snapshot and summary never raise; database outage becomes `db_up 0` | Tested |
| T30 | Misleading "all clear" from missing data | Absent series instead of zeros; TargetDown/absent alerts; dashboards state what is not measured | Tested |
| T31 | No alert delivery (by design) | Alerts are only visible if someone looks: daily check in the runbook; residual risk accepted | Accepted |

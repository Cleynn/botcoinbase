# Security checklist

`[x]` = verified by an automated test or a command run in the authoring environment (evidence named). `[ ]` = needs operator evidence or a later phase. Nothing below claims DNS, TLS, firewall, container runtime or production behaviour.

## Phase 1 (still true)
- [x] Default mode BACKTEST; LIVE not representable; banner `LIVE TRADING: BLOCKED` (`tests/unit/test_config.py`, `tests/integration/test_app_starts.py`)
- [x] Only Caddy publishes exactly TCP 80/443; internal networks; no host network, privileged, docker.sock, `:latest` (`tests/security/test_no_public_internal_ports.py`)
- [x] Production validator: debug, placeholder/short/missing secrets, cookie settings, hostnames, DB credentials, Argon2 minimums
- [x] No external assets, no inline script/style, no Node/npm, HTMX checksum pinned, no browser storage in first-party JS

## Phase 2: authentication and sessions
- [x] No signup, invitation or reset route (`test_web_auth.py`)
- [x] First ADMIN only via interactive-TTY CLI; no argv/env/pipe passwords; exactly one under a race (`test_bootstrap_cli.py`)
- [x] Argon2id, salted, parameters from settings, production minimum m=19456 KiB t=2 (`test_auth_password.py`, `test_config.py`)
- [x] Opaque 256-bit tokens; only SHA-256 stored; token absent from DB dump, pages, URLs, logs (`test_session_security.py`, `test_no_secret_leakage.py`)
- [x] Cookies `__Host-`, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain, no expiry (test client **and Chromium**)
- [x] Session rotated on login and password change; fixation defence; idle 30 min; absolute 12 h; logout, revocation, password-change and disabled-user invalidation; cap of 5
- [x] Generic login failure (unknown user, wrong password, disabled user, malformed name: byte-identical page)
- [x] Login throttling: account+client, client-wide and account-wide counters; keyed hashes only; blocked attempts not recorded; XFF trusted only from configured proxies, rightmost entry
- [x] CSRF on every state-changing route (route-enumerating test), Origin/Fetch-Metadata, token bound to session, login token separate; **real cross-site auto-submit refused in Chromium**
- [x] Roles ADMIN/VIEWER, default deny, route inventory equals reviewed map, undeclared/conflicting routes fail closed, new unsafe routes are CSRF-protected automatically
- [x] Fresh, single-use, session-bound reauthentication helper; used by ADMIN session revocation with exact typed phrase
- [x] Audit: success, failure, denial, expiry, revocation, password events; append-only triggers (owner too); hash chain detects edit/delete/truncate; state change and audit commit atomically
- [x] Least-privilege DB roles: web role cannot create users, change roles or touch audit history (real PostgreSQL grant tests)
- [x] XSS: autoescape + StrictUndefined, hostile values escaped, no reflected parameters, CSP without unsafe-inline
- [x] Usable without JavaScript; no horizontal scroll at 375 px (Chromium)

## Operator evidence still required
- [ ] Host baseline (SSH key-only, no root login, default-deny firewall incl. `DOCKER-USER`, IPv6, upgrades, CAA, chrony)
- [ ] DNS, TLS issuance, HSTS review; external port scan shows only 80/443
- [ ] `docker compose up` actually starts: images build, `migrate` completes, `app` healthy, roles created (no Docker daemon was available)
- [ ] `caddy validate`; real Caddy log lines contain no cookies/authorization/full IPs; Caddy forwards a single client address in `X-Forwarded-For` from the `172.29.10.0/24` network
- [ ] Base image digests pinned; dependency audit
- [ ] Independent security review of the Phase 2 diff (not done)

## Known limitations (accepted, recorded in `decisions.md`)
- Baseline SQL-function privilege model (actor derived from `session_user`, DB-verified sessions) is not implemented; enforcement is in the application plus grants and triggers.
- Time comes from the application clock, not the database clock.
- No second host: audit anchoring and off-host backups do not exist (DEC-006). A fully privileged database owner can still rewrite the chain undetectably.
- VIEWER accounts cannot be created yet (no allowed provisioning path); the role is implemented and tested.

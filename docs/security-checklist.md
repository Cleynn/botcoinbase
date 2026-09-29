# Security checklist (Phase 1)

`[x]` = verified by an automated test or command run in the authoring environment. `[ ]` = needs operator evidence or a later phase.
Nothing below claims DNS, TLS, firewall or production behavior.

## Verified in the authoring environment
- [x] Default mode BACKTEST; LIVE rejected (`tests/unit/test_config.py`)
- [x] Banner shows `MODE: BACKTEST` and `LIVE TRADING: BLOCKED` (`tests/integration/test_app_starts.py`)
- [x] Production validator fails on debug, placeholder/short/missing secret, cookie settings absent or weakened, invalid/missing hostnames
- [x] Compose: only Caddy publishes exactly TCP 80/443; internal networks; no host network, privileged, docker.sock, `:latest`
- [x] Health checks and restart policies defined for every non-profile service (static check)
- [x] `/healthz` generic and non-sensitive; docs/openapi disabled
- [x] No external assets/CDN/inline scripts; no Node/npm files; HTMX checksum pinned
- [x] Logs and error pages redact secrets/IPs; `.env.example` placeholders only; no key material in the repo
- [x] `docker compose config` validates with `.env.example` and with a bootstrap-generated `.env`
- [x] App started in production mode from a generated `.env`; loopback health probe returned healthy

## Operator evidence required (not supplied)
- [ ] Host baseline: SSH key-only, no root login, default-deny firewall, IPv6 state, unattended upgrades, CAA, chrony
- [ ] DNS A/AAAA for both hostnames; TLS issued by Caddy; HSTS reviewed before raising `max-age`
- [ ] External port scan shows only 80/443 (IPv4 and IPv6); `DOCKER-USER` rules documented
- [ ] Containers actually start, run non-root, and health checks turn healthy (`docker compose up`)
- [ ] `caddy validate` passes; real Caddy log lines contain no cookies, authorization headers or full IPs
- [ ] Base images pinned by digest; `pip-audit` (or equivalent) run

## Later phases
Authentication/sessions/CSRF (Phase 2), audit, monitoring, everything exchange-related.

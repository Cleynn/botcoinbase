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
| T9 | Unauthenticated access to dashboard | **Accepted for Phase 1**: the shell contains no data and no controls. Authentication is Phase 2 and **must** land before any real data is shown | Accepted risk |
| T10 | Compromised container | Non-root app user, `cap_drop: ALL`, `no-new-privileges`, read-only root fs, tmpfs, memory/pids limits, no egress | Config only; not runtime-verified (no Docker daemon available) |
| T11 | Supply chain (images, packages) | `uv.lock` pins Python packages; HTMX integrity-checked against the npm registry hash | **Open**: base image digests not pinned; lock has no hash-verified install in the image beyond uv defaults |
| T12 | Secret exposure through Compose environment | Secrets come from `.env` (0600) into container env (visible via `docker inspect` to Docker admins) | **Accepted for Phase 1**; file-based secrets are a later hardening |
| T13 | Single host, no independent watcher or off-host backup | Owner decision DEC-006: not implemented or planned. Risk accepted by the owner | Accepted; keeps live blocked |

Not modelled yet: authentication, CSRF, sessions, audit tampering, exchange failure modes, LLM content, uploads.

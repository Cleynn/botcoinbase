# Caddy

Caddy is the only service that publishes host ports (TCP 80 and 443). `admin off` disables the admin API.

- `TD_APP_HOSTNAME` routes to `app:8000`; `TD_GRAFANA_HOSTNAME` is a 503 placeholder until Phase 3.
- `/healthz`, `/metrics`, `/internal/*`, `/docs`, `/redoc`, `/openapi.json` return 404 publicly. The container health check reaches `/healthz` on the internal network only.
- HSTS is staged at `max-age=300`. Raise it only after HTTPS is confirmed working, and never add `preload` without an explicit decision.
- Access logs delete `Authorization`/`Cookie`/`Set-Cookie` and mask client IPs. **Unverified**: the filter syntax has not been run against a real Caddy; run `caddy validate` and inspect real log lines before relying on it.
- HTTP/3 (UDP 443) is disabled and not published.
- Prerequisites (not done by this repo): DNS A/AAAA records for both hostnames, ports 80/443 reachable, CAA record, host firewall default-deny. See `docs/operational-runbook.md`.

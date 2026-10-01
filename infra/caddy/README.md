# Caddy

Caddy is the only service that publishes host ports: TCP 8080 and 444 on 127.0.0.1. The host's Apache owns the public 80/443 and proxies both hostnames to them (`infra/apache/tradingdots.conf`, `docs/vps-deployment.md`). `admin off` disables the admin API.

- `TD_APP_HOSTNAME` routes to `app:8000`; `TD_GRAFANA_HOSTNAME` is a 503 placeholder until Phase 3.
- `/healthz`, `/metrics`, `/internal/*`, `/docs`, `/redoc`, `/openapi.json` return 404 publicly. The container health check reaches `/healthz` on the internal network only.
- HSTS is staged at `max-age=300`. Raise it only after HTTPS is confirmed working, and never add `preload` without an explicit decision.
- Access logs delete `Authorization`/`Cookie`/`Set-Cookie` and mask client IPs. `caddy validate` passes on Caddy 2.8 and real log lines show masked addresses; the header deletion has not been inspected in real log lines.
- Client address: `trusted_proxies static 172.29.30.1/32` with `trusted_proxies_strict` (Apache, seen as the `edge_public` gateway, is the only trusted proxy) and `header_up X-Forwarded-For {client_ip}` so the app receives exactly one address.
- Certificates: issued over HTTP-01 through Apache's port 80; the TLS-ALPN challenge is disabled because public 443 is Apache's.
- HTTP/3 (UDP 443) is disabled and not published.
- Prerequisites (not done by this repo): DNS A/AAAA records for both hostnames, ports 80/443 reachable through Apache, CAA record, host firewall default-deny. See `docs/operational-runbook.md`.

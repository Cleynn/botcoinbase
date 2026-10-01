# Architecture (Phase 1)

Status: implemented as far as Phase 1 goes; baseline items beyond it are not built.

## Runtime topology

```
Internet -> host Apache (TCP 80/443) -> caddy (loopback 8080/444, only published ports)
              |-- edge_app (internal) -----> app:8000  (FastAPI + Jinja2 + HTMX)
              '-- edge_grafana (internal) -> 503 placeholder (Grafana profile "monitoring", Phase 3)
backend (internal): postgres, redis, app (PostgreSQL only), migrate (one-shot), ctl (profile ops)
mon_scrape (internal, 172.29.20.0/24): prometheus, app (metrics listener 172.29.20.10:9464), node-exporter, cadvisor (optional)
mon_query (internal): prometheus, grafana
edge_grafana (internal): caddy, grafana
```

- Only `edge_public` has outbound access, and only Caddy is attached to it. The app has **no egress**.
- No host ports other than Caddy's 8080 and 444, bound to 127.0.0.1; the host's Apache owns 80/443 and proxies both hostnames to them (`infra/apache/tradingdots.conf`, DEC-027). Enforced by `scripts/verify_security_config.py` and tests.
- The app stores users, sessions, login attempts and the audit log in PostgreSQL. Redis is deployed but unused.
- Roles: `tradingdots` (owner, used only by `migrate`), `td_app` (web), `td_ctl` (host CLI). See `app/storage/migrations/0001_auth.sql` for the exact grants.
- Layers: `app/api` (routes, dependencies, errors) -> `app/auth` (password, session service, CSRF, throttling, audit, authorization) -> `app/storage` (repositories, migrations) ; `app/domain` holds enums, permissions and models; `app/web` holds templates and view models.
- Guards registered app-wide: `access_guard` (default deny) and `csrf_guard` (all unsafe methods); request-size limit and trusted-host middleware wrap everything.

## Application

- `app/config.py` fail-closed loader: YAML profile (`backtest` default, `paper`) + whitelisted `TD_*` variables.
  Unknown `TD_*` variables, `LIVE`, float money and loosened ceilings are rejected. `production_problems()` is the single
  production rule set, reused by `scripts/verify_security_config.py`.
- `app/constants.py` hard ceilings (50/15/35 USDC, 3-5 levels, one active pair). Config may only tighten them.
- `app/api/app.py` factory: docs/redoc/openapi disabled, TrustedHost, security headers, generic error pages,
  StrictUndefined + autoescape templates, static mount.
- `GET /healthz` returns exactly `{"status":"ok"}`; Caddy answers 404 for it publicly. The container health check uses loopback.
- Routes: `/` (dashboard shell), `/login` (notice only), `/partials/status` (HTMX fragment), `/static/*`, `/healthz`.
- Logging: `config/logging.yaml` + `RedactingFormatter` (credentials, cookies, URLs with passwords, IPs, exception text).

## Out of scope in Phase 1
Exchange adapter, orders,
pairs, data import, LLM packages/proposals, bot controls, backups.

## Monitoring (Phase 3)
Prometheus scrapes the app's internal metrics listener, node-exporter and (optionally) cAdvisor every 15 s over `mon_scrape`; Grafana queries Prometheus over `mon_query` and is published only through Caddy on its own hostname. The app and Grafana share no network; Prometheus and Grafana have no outbound route. `app/monitoring/`: `metrics.py` (catalogue, label policy, sanitiser, listener), `health.py` (cached snapshot and dashboard summary), `collectors.py` (snapshot to metrics), `alerts.py` (in-process attention items, rule catalogue). See `docs/monitoring.md`.

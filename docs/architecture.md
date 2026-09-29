# Architecture (Phase 1)

Status: implemented as far as Phase 1 goes; baseline items beyond it are not built.

## Runtime topology

```
Internet -> caddy (TCP 80/443, only published ports)
              |-- edge_app (internal) -----> app:8000  (FastAPI + Jinja2 + HTMX)
              '-- edge_grafana (internal) -> 503 placeholder (Grafana profile "monitoring", Phase 3)
backend (internal): postgres, redis   (no service attached; app does not use them in Phase 1)
monitoring (internal): prometheus, grafana   (profile "monitoring", off by default, placeholders)
```

- Only `edge_public` has outbound access, and only Caddy is attached to it. The app has **no egress**.
- No host ports other than Caddy's 80 and 443. Enforced by `scripts/verify_security_config.py` and tests.
- The app is stateless in Phase 1: no database or Redis connection code exists.

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
Authentication and sessions, audit records, database schema/migrations, monitoring, exchange adapter, orders,
pairs, data import, LLM packages/proposals, bot controls, backups.

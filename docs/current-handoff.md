# Current Handoff

Update this file at the end of every phase or session. Master Contract and `docs/decisions.md` take precedence over it.

## Project state
- Repository: `cleynn/botcoinbase`
- Branch: `claude/epic-carson-18byfr`
- Phase 4 (pairs) committed on top of `eef7311` (Phase 3), `7091205` (Phase 2), `09c9d9c` (Phase 1); see `git log`
- Tag `td-3.1`: local only; the tag push fails ("remote hung up"), do not retry without a policy change
- Package version: 0.4.0; schema version 2 (migration `0002_pairs.sql`)
- Mode: BACKTEST only
- Live trading: BLOCKED
- Gate: PAPER ONLY. Not approved for the next phase.
- DEC-000 (contract interpretations CI-1..CI-15): NOT acknowledged
- Independent security review: NOT done
- Second host: does not exist and must not be implemented or planned (DEC-006)

## Completed
- Phase 1: Python foundation, Compose, Caddy, FastAPI/Jinja2/HTMX shell, security config validator.
- Phase 2: local ADMIN bootstrap, Argon2id, opaque hashed sessions, `__Host-` cookies, app-wide CSRF, login throttling, hash-chained audit log, security and audit pages. Roles `td_app`, `td_ctl` and owner (migrate only).
- Phase 3: monitoring.
  - Separate internal `/metrics` listener (`TD_METRICS_BIND`, allowed scrapers only). The web app never serves `/metrics`.
  - 19-family metric catalogue with a sanitizer and label policy.
  - Prometheus (15 s, 30 d / 15 GB), 8 recording rules, 20 alerts, no Alertmanager/remote_write.
  - Grafana with provisioned, non-editable dashboards (Overview, Risk and Failsafes, Data Health, Execution and Reconciliation, VPS and Container Health), anonymous access and signup off, alerting off.
  - node-exporter; optional cAdvisor (profile `cadvisor`).
  - Dashboard monitoring summary (ADMIN sees audit event counts, VIEWER does not) plus a Grafana link.
  - Validators `make verify-monitoring-config` and `make monitoring-status`.
  - Docs: `docs/monitoring.md`, `alert-policy.md`, `grafana-access.md`, DEC-011/012.

- Phase 4: public product discovery, pair policy, 14-check validation with stored reasons, DB-enforced lifecycle, Pairs UI/API, full-chain disable/archive/activate, host CLI (`make pairs-*`), egress allowlist proxy. Activation is wired but REFUSED (no bot state). Fees must be operator-attested in `config/pair-policy.yaml` or every validation is INCONCLUSIVE. See `docs/pair-management.md`, DEC-013/014.

## Verified
- Phase 4: 1200 tests passed (0 skipped), real-browser check 12/12 against SYNTHETIC Coinbase data.
- `ruff format`, `ruff check`, `mypy app scripts`: clean.
- Full `pytest`: 782 passed, including real `promtool` rule tests and a real Prometheus end-to-end scrape (needs `TD_PROMETHEUS_DIR` and `TD_NODE_EXPORTER_DIR`; without them those tests skip).
- Full-stack check (real app process, Chromium, real Prometheus and node_exporter): 20/20. 28 rules healthy, 51 dashboard queries execute.
- `make verify-security-config` and `make verify-monitoring-config` pass with a generated `.env` (deleted afterwards).

## Not yet verified
- Phase 4: real Coinbase response shapes (AS-C1; fixtures are synthetic), Docker start of `egress-proxy`/`pairs`, proxy vs the live host.
- No Docker daemon was available: container start and health, Caddy proxying Grafana, node-exporter host mounts, cAdvisor and resource sizing are untested.
- Grafana was never run (download host blocked). Dashboards were validated structurally and by running every query against real Prometheus. Provisioning, rendering and the admin-reset command are unverified.
- `docker compose config` (including `--profile cadvisor`) has not been run.

## Current blocker
- None in code. External gates: a real container and Grafana run on the target host, and an independent security review.
- The `td-3.1` tag push is blocked by the remote.

## Next task
- Await the user's next phase prompt. Do not start bot components without one.
- Before any phase that builds bot components, decide the DEC-000 acknowledgement.
- When a bot component is built, publish its metrics from the Master Contract list only. `tradingdots_bot_*` (except `bot_info`), `pair_*` and `llm_*` are reserved and currently not published; `tests/security/test_metrics_secret_redaction.py` will need a deliberate update.

## Files most relevant
- `app/config.py` (`MonitoringSettings`, `production_problems`)
- `app/monitoring/{metrics,health,alerts,collectors}.py`
- `app/main.py`, `app/api/app.py`, `app/api/dashboard.py`
- `app/storage/repositories.py` (`MonitoringRepository`)
- `infra/monitoring/` (prometheus.yml, recording_rules.yml, alert_rules.yml, grafana/)
- `docker-compose.yml`, `infra/caddy/Caddyfile`, `.env.example`
- `scripts/verify_monitoring_config.py`, `scripts/verify_security_config.py`, `scripts/monitoring_status.py`
- `tests/integration/test_prometheus_scrape.py`, `tests/integration/test_grafana_provisioning.py`, `tests/security/test_metrics_secret_redaction.py`, `tests/security/test_monitoring_not_public.py`
- `docs/decisions.md`, `docs/monitoring.md`, `docs/operational-runbook.md`

## Security invariants to preserve
- Live trading stays blocked; no code path enables it.
- No secrets in the repo. Grafana admin password and secret key come from `.env` only, and production validation fails on missing, default or placeholder values.
- Prometheus, `/metrics`, Grafana, PostgreSQL, Redis and the app internal port publish no host ports. Only Caddy is public.
- The app and Grafana share no network. Grafana and Prometheus have no outbound route.
- Monitoring is read-only and observational: no external alert transport, no monitoring-to-bot actions, and rules and alerts cannot reach bot controls, Coinbase or configuration.
- Low-cardinality metrics only. No sensitive labels (usernames, IPs, tokens, ids). Unknown families are dropped. Absent series, never fake zeros, for unbuilt components.
- No metrics invented for unfinished bot components.
- Audit log stays hash-chained and append-only. The web role has least privilege.
- Every state-changing request needs CSRF. `Referrer-Policy: same-origin`.
- Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: <session url>`. Push only to the designated branch. No PRs unless asked. No model identifiers in repo artifacts.

## Validation commands
```
make lint typecheck test
TD_PROMETHEUS_DIR=<dir> TD_NODE_EXPORTER_DIR=<dir> uv run pytest -q
make verify-security-config
make verify-monitoring-config
docker compose config   # and --profile cadvisor
```

# Current Handoff

Update this file at the end of every phase or session. Master Contract and `docs/decisions.md` take precedence over it.

## Project state
- Repository: `cleynn/botcoinbase`
- Branch: `claude/epic-carson-18byfr`
- Phase 8 (safety machinery) committed on top of Phase 7 (imported proposals), Phase 6 (read-only review packages), Phase 5 (market data, backtest, paper), Phase 4 (pairs), on top of `eef7311` (Phase 3), `7091205` (Phase 2), `09c9d9c` (Phase 1); see `git log`
- Tag `td-3.1`: local only; the tag push fails ("remote hung up"), do not retry without a policy change
- Package version: 0.4.0 (not bumped in Phases 5 and 6); schema version 6 (migration `0006_safety.sql`)
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

- Phase 5: candles/importer/validation, checksummed Parquet snapshots, deterministic grid strategy, fee-aware backtest + walk-forward, shared trader, local persisted paper exchange, immutable JSON/Markdown reports, Reports pages, read-only metrics, `batch` service and `make market-*`/`backtest`/`paper-*`. See `docs/backtest-and-paper.md`, DEC-015/016. Example report (FICTIONAL): `docs/examples/`.

- Phase 6: disabled-by-default review packages: ADMIN-only enable/disable/create chain (CSRF + fresh reauth + exact phrases), typed export views + scanner, deterministic ZIP builder/verifier, protected POST download, retention cleanup, dashboard, audit events, aggregate metrics, `review build|verify|cleanup|list` and `make review-*`. No LLM call, no proposal import, no bot mutation. See `docs/review-packages.md`, DEC-017/018; fictional README: `docs/examples/review-package-README-FICTIONAL.md`.

- Phase 7: disabled-by-default proposal import: ADMIN chain (CSRF + fresh reauth + phrases), text/plain and JSON only, opaque storage outside the web root, host validation (strict schema, policy, risk), trigger-enforced lifecycle, manual change request and attestations, escaped UNTRUSTED ADVISORY INPUT UI, `proposal validate|cleanup|list`, `make proposal-*`, aggregate metrics. Nothing is ever applied. See `docs/proposals.md`, DEC-019/020; fictional examples in `docs/examples/`.

- Phase 8: risk engine, breaker, kill switch, staleness/anomaly checks, GET-only private read adapter (NullSigner, no credentials), WebSocket hint parser, REST reconciliation, startup recovery, retry policy, ADMIN Bot page (four phrases), live gate that only returns BLOCKED, order path with immutable intents, scripted fake exchange, request-shape sandbox, `safety` host CLI and `make safety-*`, thirteen `tradingdots_bot_*` metrics. No gateway, reader or signer exists in any deployment. Security-review fixes B1/H1/H2/H3/M1/M2 applied (DB clock, boot-id binding, SQL money rules). See `docs/safety-machinery.md`, DEC-021/022/023.

## Verified
- Phase 8: 2973 passed plus the corrected scrape test (48 passed); real-process + browser check 28/28 on SYNTHETIC data; migration 0006 rollback verified.
- Phase 7: 2374 tests passed (0 skipped); real-process + browser check 28/28 on SYNTHETIC data; migration 0005 rollback verified.
- Phase 6: 1603 tests passed (0 skipped); real-process + browser check 25/25 on SYNTHETIC data; byte fuzzing of packages; migration 0004 rollback verified.
- Phase 5: 1400 tests passed (0 skipped); real-process + browser check 16/16 on SYNTHETIC data; migration 0003 rollback verified.
- Phase 4: 1200 tests passed (0 skipped), real-browser check 12/12 against SYNTHETIC Coinbase data.
- `ruff format`, `ruff check`, `mypy app scripts`: clean.
- Full `pytest`: 782 passed, including real `promtool` rule tests and a real Prometheus end-to-end scrape (needs `TD_PROMETHEUS_DIR` and `TD_NODE_EXPORTER_DIR`; without them those tests skip).
- Full-stack check (real app process, Chromium, real Prometheus and node_exporter): 20/20. 28 rules healthy, 51 dashboard queries execute.
- `make verify-security-config` and `make verify-monitoring-config` pass with a generated `.env` (deleted afterwards).

## Not yet verified
- Phase 8: any real Coinbase behaviour (AS-C1, AS-C3, AS-C4), the absence proof against a real exchange, WebSocket connectivity, containers, a paper reconciler; the paper trader honours only kill and breaker.
- Phase 7: `app`/`batch` sharing the `proposals` volume in real containers (no Docker daemon). Policy is heuristic and over-inclusive; no independent review.
- Phase 6: `batch`/`app` sharing the `review_packages` volume in real containers (no Docker daemon). No package delete action, no proposal import (baseline Phase 8), no signature or per-package encryption.
- Phase 5: real Coinbase candle shapes (AS-C1), Docker start of `batch`, `/data` volume permissions, `docker compose config`. Strategy and fee model are assumptions; no result predicts real performance.
- Phase 4: real Coinbase response shapes (AS-C1; fixtures are synthetic), Docker start of `egress-proxy`/`pairs`, proxy vs the live host.
- No Docker daemon was available: container start and health, Caddy proxying Grafana, node-exporter host mounts, cAdvisor and resource sizing are untested.
- Grafana was never run (download host blocked). Dashboards were validated structurally and by running every query against real Prometheus. Provisioning, rendering and the admin-reset command are unverified.
- `docker compose config` (including `--profile cadvisor`) has not been run.

## Current blocker
- None in code. External gates: a real container and Grafana run on the target host, and an independent security review.
- The `td-3.1` tag push is blocked by the remote.

## Next task
- Await the user's next phase prompt (Phase 6 gate: PAPER ONLY, LIVE TRADING BLOCKED). Do not start bot components without one.
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

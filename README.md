# TradingDots

Deterministic, fee-aware Coinbase Advanced Trade **spot-grid research, backtest and paper-trading platform**.
It is **not an AI trading bot**. **LIVE TRADING: BLOCKED** in every build produced from this repository.

**Status: Phase 3 (monitoring).** Prometheus, Grafana and node-exporter run alongside the app (read-only, internal, no alert delivery); see `docs/monitoring.md`. Phase 2 provides authentication and the security pages. Local-CLI ADMIN bootstrap, Argon2id, server-side sessions, CSRF, login throttling, audit log, Security and Audit pages. There is still no exchange adapter, no orders, no pair management, no data import, no LLM packages/proposals and no bot controls. Every unmeasured value reads "Unknown" or "Not available".

Stack: Python 3.12, FastAPI, server-rendered Jinja2, vendored HTMX, PostgreSQL, Redis, Caddy.
No React/Node/npm/CDN/frontend build chain. Governing documents: `baseline/TRADINGDOTS_HANDOFF.md`
(not yet binding: see `docs/decisions.md`).

## Quick start (development)

```bash
uv sync                       # Python 3.12 + locked dependencies (uv.lock)
make lint typecheck test      # all must pass
make verify-security-config-example
```

Tests need PostgreSQL: they start a private one via `pgserver` (dev dependency), or set `TD_TEST_PG_URI` to a superuser URI.

## Deploy prerequisites (NOT done or verified by this repository)
DNS records, TLS issuance, host firewall, SSH hardening. See `docs/operational-runbook.md`.

```bash
./scripts/bootstrap.sh        # creates .env (mode 600) with random secrets; prints none
make verify-security-config   # strict: fails on placeholders, debug, insecure cookies, published ports
make up && make health
docker compose run --rm -it ctl python scripts/create_admin.py   # first ADMIN, interactive only
```

## Make targets
`format lint typecheck test up down logs health verify-security-config verify-monitoring-config monitoring-status` (plus `verify-security-config-example`).

## Pairs (Phase 4)
Discovery, validation and lifecycle of USDC spot pairs from public Coinbase data: see `docs/pair-management.md`. Nothing here trades, reads an account or enables live trading.

## Layout
`app/` application, `config/` YAML profiles, `infra/` Caddy and Postgres init, `scripts/` health,
bootstrap and security validation, `tests/` (`tests/pending/` holds not-yet-runnable skeletons for later
phases), `docs/`, `baseline/`.

## Market data, backtests, paper trading (Phase 5)
Public candle import (dry run unless `--commit`), checksummed Parquet snapshots, a deterministic grid
strategy, a fee-aware backtest with walk-forward, and a local persisted paper exchange, driven by the
`batch` service (`make market-import`, `market-snapshot`, `backtest`, `paper-*`). No private
execution, no live mode. See `docs/backtest-and-paper.md`; example report (FICTIONAL, synthetic):
`docs/examples/backtest-report-FICTIONAL.md`.

## Review packages (Phase 6)
An ADMIN can enable (disabled by default), request, verify and download a sanitized, historical,
checksummed ZIP to give to an AI assistant by hand. It cannot trade or change any setting, no LLM is
called, and no proposal is imported. Build/verify/cleanup run on the host (`make review-*`). See
`docs/review-packages.md`; fictional package README: `docs/examples/review-package-README-FICTIONAL.md`.

## Imported proposals (Phase 7)
An ADMIN can enable (disabled by default) and import a plain-text/JSON proposal written by an AI assistant. It is stored opaquely, validated on the host against a strict schema and policy, shown escaped and labelled **UNTRUSTED ADVISORY INPUT**, and can lead to a *manual* change request. Nothing is ever applied and no LLM is called. See `docs/proposals.md`; fictional example: `docs/examples/proposal-FICTIONAL.json`.

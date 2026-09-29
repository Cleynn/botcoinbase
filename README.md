# TradingDots

Deterministic, fee-aware Coinbase Advanced Trade **spot-grid research, backtest and paper-trading platform**.
It is **not an AI trading bot**. **LIVE TRADING: BLOCKED** in every build produced from this repository.

**Status: Phase 1 foundation only.** There is no exchange adapter, no orders, no pair management,
no data import, no LLM packages/proposals and no bot controls. The dashboard is a static shell in which
every measurement reads "Unknown" or "Not available".

Stack: Python 3.12, FastAPI, server-rendered Jinja2, vendored HTMX, PostgreSQL, Redis, Caddy.
No React/Node/npm/CDN/frontend build chain. Governing documents: `baseline/TRADINGDOTS_HANDOFF.md`
(not yet binding: see `docs/decisions.md`).

## Quick start (development)

```bash
uv sync                       # Python 3.12 + locked dependencies (uv.lock)
make lint typecheck test      # all must pass
make verify-security-config-example
```

## Deploy prerequisites (NOT done or verified by this repository)
DNS records, TLS issuance, host firewall, SSH hardening. See `docs/operational-runbook.md`.

```bash
./scripts/bootstrap.sh        # creates .env (mode 600) with random secrets; prints none
make verify-security-config   # strict: fails on placeholders, debug, insecure cookies, published ports
make up && make health
```

## Make targets
`format lint typecheck test up down logs health verify-security-config` (plus `verify-security-config-example`).

## Layout
`app/` application, `config/` YAML profiles, `infra/` Caddy and Postgres init, `scripts/` health,
bootstrap and security validation, `tests/` (`tests/pending/` holds not-yet-runnable skeletons for later
phases), `docs/`, `baseline/`.

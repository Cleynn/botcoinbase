PY ?= uv run python
COMPOSE ?= docker compose

.PHONY: vps-check pairs-discover pairs-seed pairs-validate pairs-list format lint typecheck test up down logs health verify-security-config verify-security-config-example verify-monitoring-config monitoring-status market-import market-snapshot market-quality backtest paper-status paper-step paper-report review-build review-verify review-cleanup review-list proposal-validate proposal-cleanup proposal-list safety-status safety-recover safety-reconcile safety-monitor safety-commands

format:
	uv run ruff check --fix .
	uv run ruff format .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

health:
	$(COMPOSE) exec -T app python scripts/healthcheck.py

# Strict production check against the real .env (fails on placeholders, weak file mode, etc.).
verify-security-config:
	$(PY) scripts/verify_security_config.py

# Structure-only check against .env.example (placeholders allowed). Not a production check.
verify-security-config-example:
	$(PY) scripts/verify_security_config.py --env-file .env.example --example

# Static checks for Prometheus, rules, Grafana provisioning/dashboards, Compose and Caddy (see docs/monitoring.md).
verify-monitoring-config:
	$(PY) scripts/verify_monitoring_config.py --env-file .env

# Read-only: asks Prometheus (inside its container) for target health and firing alerts.
monitoring-status:
	$(PY) scripts/monitoring_status.py

# Phase 4: pair discovery and validation (public Coinbase market data only, via the allowlist proxy).
pairs-discover:
	docker compose --profile discovery run --rm pairs discover

pairs-seed:
	docker compose --profile discovery run --rm pairs seed --queue-validation

pairs-validate:
	docker compose --profile discovery run --rm pairs validate

pairs-list:
	docker compose --profile discovery run --rm pairs list

# Phase 5: market data, backtests and PAPER trading. Public GETs only; import is a dry run unless
# COMMIT=1. Nothing here trades on any exchange. Usage: make market-import PRODUCT=BTC-USDC
PRODUCT ?= BTC-USDC
market-import:
	docker compose --profile discovery run --rm batch market import --product $(PRODUCT) $(if $(COMMIT),--commit,)

market-snapshot:
	docker compose --profile discovery run --rm batch market snapshot --product $(PRODUCT)

market-quality:
	docker compose --profile discovery run --rm batch market quality --product $(PRODUCT)

backtest:
	docker compose --profile discovery run --rm batch backtest run --snapshot $(SNAPSHOT) $(if $(WALK),--walk-forward,)

paper-status:
	docker compose --profile discovery run --rm batch paper status

paper-step:
	docker compose --profile discovery run --rm batch paper step

paper-report:
	docker compose --profile discovery run --rm batch paper report

# Phase 6: read-only review packages (built and verified on the host; enabling, requesting and
# downloading are ADMIN web actions). Nothing here calls an LLM or changes bot state.
review-build:
	docker compose --profile discovery run --rm batch review build

review-verify:
	docker compose --profile discovery run --rm batch review verify --all

review-cleanup:
	docker compose --profile discovery run --rm batch review cleanup

review-list:
	docker compose --profile discovery run --rm batch review list

# Phase 7: imported LLM proposals are UNTRUSTED ADVISORY INPUT. Importing, reviewing and the manual
# change-request steps are ADMIN web actions; validation and cleanup run on the host. Nothing here
# applies a proposal or changes any system state.
proposal-validate:
	docker compose --profile discovery run --rm batch proposal validate

proposal-cleanup:
	docker compose --profile discovery run --rm batch proposal cleanup

proposal-list:
	docker compose --profile discovery run --rm batch proposal list

# Phase 8: safety machinery. Host commands only. Nothing here places, submits or sells an order, and
# resuming the bot is an ADMIN dashboard action, not a command. Without an exchange reader (the state
# of every deployment of this build) recover and reconcile report that and do nothing.
safety-status:
	docker compose --profile discovery run --rm batch safety status

safety-recover:
	docker compose --profile discovery run --rm batch safety recover

safety-reconcile:
	docker compose --profile discovery run --rm batch safety reconcile

safety-monitor:
	docker compose --profile discovery run --rm batch safety monitor

safety-commands:
	docker compose --profile discovery run --rm batch safety commands

vps-check:
	scripts/vps/check.sh

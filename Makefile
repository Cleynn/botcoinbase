PY ?= uv run python
COMPOSE ?= docker compose

.PHONY: pairs-discover pairs-seed pairs-validate pairs-list format lint typecheck test up down logs health verify-security-config verify-security-config-example verify-monitoring-config monitoring-status

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

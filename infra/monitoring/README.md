# infra/monitoring

Observational, read-only monitoring for TradingDots. See `docs/monitoring.md` (design and metric catalogue), `docs/alert-policy.md` (rules) and `docs/grafana-access.md`.

| Path | Purpose |
|---|---|
| `prometheus.yml` | 15 s scrape and evaluation; four internal jobs; no Alertmanager, remote read or write. Retention (30 d / 15 GB) is set by flags in `docker-compose.yml`. |
| `recording_rules.yml` | Aggregations of existing metrics only. |
| `alert_rules.yml` | 20 alerts, displayed only (no transport). |
| `grafana/provisioning/` | One read-only Prometheus datasource; a read-only dashboard provider. |
| `grafana/dashboards/` | Overview, Risk and Failsafes, Data Health, Execution and Reconciliation, VPS and Container Health. |

Validate after any change:
```bash
uv run python scripts/verify_monitoring_config.py            # structure, metric honesty, no delivery channels
promtool check rules infra/monitoring/recording_rules.yml infra/monitoring/alert_rules.yml
```
Rules and dashboards may only use metrics in `app/monitoring/metrics.py` (`CATALOGUE`) or the standard `node_*`, `prometheus_*`, `process_*`, `container_*`, `up`, `scrape_*` and `ALERTS` families.

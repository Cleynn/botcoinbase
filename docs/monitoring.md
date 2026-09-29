# Monitoring (Phase 3)

Monitoring here is **observational and read-only**. It cannot start, stop or change the bot, an exchange, a pair or any configuration, it delivers no notifications, and a monitoring failure must never affect the application.

## Components

```
app (FastAPI, :8000)  --edge_app-->  Caddy  --> https://tradingdots.onthewall.ovh
app metrics listener (172.29.20.10:9464, mon_scrape only, never routed)
   ^ scrape every 15 s
Prometheus  (mon_scrape + mon_query, internal)  <-- node-exporter (:9100)  <-- cAdvisor (optional, :8080)
   ^ queries (mon_query)
Grafana (mon_query + edge_grafana, internal)  <-- Caddy  --> https://grafana.tradingdots.onthewall.ovh
```

| Service | Default | Published | Networks | Notes |
|---|---|---|---|---|
| `prometheus` v2.53.0 | on | no | `mon_scrape`, `mon_query` | 15 s scrape and evaluation; retention **30 d / 15 GB** (command flags); lifecycle and admin APIs disabled; no Alertmanager, remote read or write |
| `grafana` 11.1.0 | on | no (Caddy only) | `edge_grafana`, `mon_query` | login required; no anonymous access, sign-up, embedding, public dashboards, plugins, updates checks or alerting; dashboards read-only |
| `node-exporter` v1.8.1 | on | no | `mon_scrape` | non-root, read-only host mounts, no host network or PID namespace |
| `cadvisor` v0.49.1 | **off** | no | `mon_scrape` | optional profile, see below |

The application and Grafana share **no** network. Prometheus has no route to the internet, and neither has Grafana.

## The application's metrics

Served by a separate listener (`app/monitoring/metrics.py`), not by FastAPI, so `/metrics` cannot be reached through Caddy or the web port: the listener binds a specific private address (`TD_METRICS_BIND`, the app's static address on `mon_scrape`), answers only `GET /metrics`, only to `monitoring.allowed_scrapers`, and logs nothing about peers. Production refuses a wildcard/public bind and broad scraper networks. Caddy additionally returns 404 for `/metrics*` on both hostnames.

Metrics are read from a cached snapshot (`app/monitoring/health.py`, refreshed at most every 10 s) that the dashboard summary shares, so scraping cannot hammer the database. If the database is down the snapshot says so (`tradingdots_db_up 0`) and database-derived series are **absent**, never zero. The audit chain is re-verified every 5 minutes, not on every scrape.

### Catalogue (every metric that exists)

| Metric | Type | Labels | Source | Meaning |
|---|---|---|---|---|
| `tradingdots_bot_info` | gauge | `version`, `mode` | config | Build and configured mode (value is always 1). |
| `tradingdots_live_trading_blocked` | gauge | - | code | 1 while live trading is blocked (compile-time constant). |
| `tradingdots_http_requests_total` | counter | `route_template`, `status_class` | http | HTTP requests handled by the web application. |
| `tradingdots_http_request_duration_seconds` | histogram | `route_template` | http | HTTP request duration in seconds. |
| `tradingdots_db_up` | gauge | - | database | 1 if the database answered the last probe. |
| `tradingdots_db_probe_duration_seconds` | gauge | - | database | Duration of the last database probe. |
| `tradingdots_db_schema_version` | gauge | - | database | Schema version found in the database. |
| `tradingdots_db_expected_schema_version` | gauge | - | code | Schema version this code requires. |
| `tradingdots_sessions_active` | gauge | - | database | Sessions that are neither revoked nor expired. |
| `tradingdots_users` | gauge | `role` | database | Enabled user accounts by role. |
| `tradingdots_auth_events_total` | counter | `event` | database | Security audit events recorded, by event type. |
| `tradingdots_audit_events_total` | counter | - | database | All audit events recorded. |
| `tradingdots_audit_last_event_timestamp_seconds` | gauge | - | database | Time of the newest audit event. |
| `tradingdots_audit_chain_ok` | gauge | - | database | 1 if the audit hash chain verified, 0 if it is broken. |
| `tradingdots_audit_chain_events_verified` | gauge | - | database | Audit events covered by the last chain verification. |
| `tradingdots_audit_last_verified_timestamp_seconds` | gauge | - | database | When the audit chain was last verified. |
| `tradingdots_pair_candidates_total` | gauge | - | database | Pairs that are not archived (candidates and the active pair). |
| `tradingdots_pair_state_total` | gauge | `state` | database | Pairs currently in each lifecycle state (a count of rows, kept as a gauge under the contract name). One series per state, zeros included because they are real counts. |
| `tradingdots_pair_metadata_age_seconds` | gauge | - | database | Age of the oldest verified product metadata among non-archived pairs. Absent when there are no pairs. An extension to the contract list; its source (the time metadata was last verified against Coinbase) is real. |
| `tradingdots_metrics_scrapes_total` | counter | - | listener | Scrapes served by the metrics listener. |
| `tradingdots_metrics_last_scrape_timestamp_seconds` | gauge | - | listener | When the metrics listener last served a scrape. |
| `tradingdots_collector_errors_total` | counter | `collector` | collector | Errors while collecting metrics, by collector. |

Plus the standard `process_*` and `python_*` families from the client library. Nothing else is published. The exposition is passed through a sanitiser that drops unknown families, unexpected or sensitive label names, non-plain label values and anything beyond 100 series per family (`tradingdots_collector_errors_total` counts render failures).

### Label policy
Only these label names exist: `route_template`, `status_class`, `event`, `role`, `mode`, `version`, `collector`, `state` (plus `le`). Route labels are **route templates**, never raw paths; unknown paths collapse to `unmatched`. Names matching user, email, ip, client, session, token, cookie, path, url, agent, order, account, password, secret, hash, key, address, host, error, message or timestamp are refused everywhere, in metrics and in dashboard queries. Values come from fixed sets, so an attacker cannot create series (tested with hundreds of hostile logins, paths, hosts and cookies).

### Reserved, not implemented
The Master Contract lists `tradingdots_bot_*` (ticks, freshness, reconciliation, risk rejections, circuit breaker, kill switch, open orders, order intents/events, deployed/reserve, P&L, drawdown, grid cycles, fees, API requests/errors), and `tradingdots_llm_*`. **None of these has a source yet, so none is published** (only `tradingdots_bot_info`, which is a configuration fact). Each will be added in the same change as the component that produces it. A test fails if any of them appears early. (`tradingdots_pair_candidates_total` and `tradingdots_pair_state_total` from the contract list are published since Phase 4, because the pair tables are their real source.)

## Dashboards (Grafana, provisioned as code, read-only)
Overview, Risk and Failsafes, Data Health, Execution and Reconciliation, VPS and Container Health (`infra/monitoring/grafana/dashboards/`). Risk and Failsafes shows the *security* failsafes that exist today; Data Health shows the database and the monitoring pipeline; Execution and Reconciliation is deliberately just context plus an explanation, because there is nothing to measure. Every panel names its source; a test checks that every query parses and uses only metrics that exist, and the queries are also executed against a real Prometheus.

## Main dashboard summary
The web application shows a simple summary (monitoring status, database, schema, audit chain, metrics endpoint, last scrape, and up to five attention items) computed from the same snapshot. **It never queries Prometheus.** VIEWERs see chain state without event counts; ADMINs see counts and positions. The "Alerts" tile stays `Not available` because alert state lives in Prometheus; it is visible in Grafana ("Firing alerts") and via `scripts/monitoring_status.py`. The Grafana link is a plain link, never an embed.

## Alerts
See `docs/alert-policy.md`. Rules are evaluated and displayed only: no Alertmanager, webhook, e-mail or chat transport exists, and no rule or dashboard can act on anything.

## Optional cAdvisor
```
docker compose --profile cadvisor up -d cadvisor
```
Adds per-container CPU and memory panels. It is **off by default** and **untested here** (no Docker daemon). It deliberately does **not** mount the Docker socket or `/var/lib/docker`: a read-only bind of a directory containing the socket still allows API calls, which would give a compromised monitoring container control of every container. Consequently containers are identified by cgroup id (first 12 characters, as `docker ps` shows) instead of name, and Prometheus keeps only a small allowlist of families for Docker cgroups and drops descriptive labels. Depending on the host (cgroup v1/v2) cAdvisor may need more access than this to report everything; review before granting it, and disable the profile if in doubt. Alert rules ignore the `cadvisor` job so a disabled cAdvisor never alerts.

## Resources (6 vCPU / 12 GB / 100 GB)
Memory limits: postgres 2 GB, prometheus 1.5 GB, app 768 MB, grafana 512 MB, redis 256 MB, caddy 128 MB, node-exporter 64 MB (about 5.3 GB; +256 MB with cAdvisor). Prometheus is capped at 15 GB on disk; at roughly 550 series and a 15 s interval it is expected to use far less than 1 GB (estimate, measure before relying on it). Series count is watched by the "Samples per scrape" and "Prometheus head series" panels.

## Verifying
```bash
uv run python scripts/verify_monitoring_config.py --env-file .env          # static rules; add --example for .env.example
uv run pytest tests/unit/test_metrics.py tests/unit/test_collectors.py tests/integration/test_grafana_provisioning.py \
              tests/integration/test_prometheus_scrape.py tests/security/test_metrics_secret_redaction.py \
              tests/security/test_monitoring_not_public.py
# with real binaries (promtool rule tests + a real Prometheus scraping the real app):
TD_PROMETHEUS_DIR=/path/to/prometheus TD_NODE_EXPORTER_DIR=/path/to/node_exporter uv run pytest tests/integration/test_prometheus_scrape.py
make monitoring-status        # on the host, once the stack is up (read-only)
```

## Not verified
Container start of Prometheus/Grafana/node-exporter/cAdvisor and Compose health checks (no Docker daemon), Grafana loading the provisioned files and rendering the dashboards (its download host is blocked; structure and queries are validated instead), Caddy proxying Grafana, node-exporter's host mounts on the VPS, and the memory/disk estimates above.

# Alert policy

## Principles
- Alerts are **evaluated by Prometheus and displayed**, nothing more. There is no Alertmanager, webhook, e-mail, chat or push transport, and none may be added without an explicit decision. Grafana alerting is switched off.
- A rule can only read metrics. It cannot call the bot, an exchange or the configuration, and no dashboard button or link can either (`scripts/verify_monitoring_config.py` and tests enforce this).
- Only alerts for things that exist are defined. Alerts for unbuilt components are *reserved* (bottom of this page).
- Nothing here changes trading behaviour: monitoring outages and alerts never pause, resume or stop anything. The application's own guards decide, not Prometheus.

## Severities
| Severity | Meaning | Expected reaction |
|---|---|---|
| `critical` | Integrity, availability or data-loss risk | Look today, now if possible |
| `high` | Real user-visible failure | Look the same day |
| `warn` | Degradation or a trend | Look at the next check |
| `info` | Worth knowing | No action needed |

## Where to see alerts
Grafana → Overview → *Firing alerts*; `make monitoring-status` on the host; the dashboard summary in the web application shows the health-related subset (database, schema, audit chain, scraping) without querying Prometheus. Since nothing is pushed, **check them**: daily is enough (`docs/operational-runbook.md`).

## Rules
Each section is the `runbook` target of one rule.

### TargetDown
`critical`, 1 min. Prometheus cannot scrape `prometheus`, `tradingdots-app` or `node`. Check `docker compose ps`, then the service's logs. For `tradingdots-app`: the internal listener may have failed to bind (the dashboard says "failed to start") or `TD_METRICS_BIND` differs from the Compose address. The optional `cadvisor` job is excluded.

### DiskUsageWarn
`warn`, root filesystem above 70 % for 10 min. Review logs, old images and Prometheus data.

### DiskUsageHigh
`high`, above 80 % for 10 min. Free space now (prune unused images, rotate logs). Never delete database volumes.

### DiskUsageCritical
`critical`, above 90 % for 5 min. Services may fail imminently. Free space immediately.

### MemoryPressure
`warn`, host memory above 90 % for 10 min. Compare with the container memory limits; look for a runaway container.

### CpuSaturation
`warn`, host CPU above 90 % for 15 min.

### TsdbNearCap
`warn`, Prometheus storage above 80 % of its 15 GB cap for 15 min. Prometheus will start dropping the oldest data at the cap; check series growth on Data Health.

### PrometheusRuleFailures
`warn`. A rule failed to evaluate in the last 10 minutes; inspect Prometheus' rules page (internal) or logs.

### AppMetricsMissing
`warn`, 5 min. The `tradingdots-app` job has no `up` series: the scrape configuration was changed or not loaded.

### DatabaseUnavailable
`critical`, 1 min. The application's database probe fails. Check PostgreSQL, its health check and the `td_app` credentials.

### SchemaMismatch
`critical`, 1 min. The database schema version differs from the one this code requires. Run `docker compose run --rm migrate` (roll forward) or redeploy the matching version.

### Http5xxRatioHigh
`high`, more than 5 % of requests fail with 5xx for 5 min. Check the application log (redacted) and database.

### HttpLatencyHigh
`warn`, p95 latency above 2 s for 10 min. Sign-in costs about 0.15 s by design (Argon2id); look for database slowness.

### CollectorErrors
`warn`. A metrics collector or the renderer reported errors in the last 15 minutes. Metrics are incomplete, not wrong.

### AuditChainBroken
`critical`, immediately. Audit rows were edited, removed or truncated (or the head disagrees). Treat as an incident (`docs/incident-response.md`, IR-2): do not delete anything, preserve the database, and remember that without a second host (DEC-006) integrity cannot be independently proven.

### AuditChainVerifyStale
`warn`. The chain has not been verified for 30 minutes although it normally is every 5. The application may be unhealthy or the database unreachable.

### AuthFailureSpike
`warn`. More than 20 failed sign-ins in 15 minutes. Check the Audit page (client tags, not addresses) and the throttle counters.

### LoginThrottleActive
`info`. A client was throttled in the last 15 minutes.

### DenialSpike
`warn`. More than 20 authorization denials, CSRF rejections or rejected sessions in 15 minutes: possible probing or a broken client.

### AdminAccountChange
`info`. An administrator account was created, a password rotated or sessions revoked. Confirm it was intended (Audit page).

## Reserved (not implemented: the metrics do not exist)
Each of these is added together with the component that produces its metric, with a test: tick stale, data stale, product metadata stale, reconciliation stale or mismatching, kill switch active, circuit breaker open, bot blocked, unknown order status, risk-rejection spike, drawdown limit, daily-loss limit, fill anomaly, ledger breach or inconsistency, pair stuck unclean, configuration mismatch, exchange API error rate, schema drift, clock offset, ingest failures, work-request backlog, backup/anchor/second-host staleness (no second host exists, DEC-006), package failed, proposal rejection spike.

## Changing the policy
Edit `infra/monitoring/alert_rules.yml`, keep labels within `severity`/`component`, keep annotations to `summary`/`description`/`runbook`, add a section above, and run `scripts/verify_monitoring_config.py` and `promtool check rules`.

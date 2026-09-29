# Operational runbook (Phase 1)

**Prerequisites are not done by this repository and have not been verified.**

## Before first start (operator)
1. DNS: A (and AAAA if used) records for `tradingdots.onthewall.ovh` and `grafana.tradingdots.onthewall.ovh` to the VPS. Add a CAA record.
2. Firewall: default-deny inbound, allow SSH (key-only), TCP 80 and 443. Docker-published ports bypass `ufw`; restrict with `DOCKER-USER` rules and verify with an external scan.
3. SSH key-only, no root login, unattended security upgrades, time sync (chrony).
4. Install Docker Engine + Compose plugin.

## Bring-up
```bash
./scripts/bootstrap.sh              # creates .env (0600), random secrets, prints none
make verify-security-config         # strict; must PASS
make up                             # builds the app image, starts caddy, app, postgres, redis
make health                         # app health via the internal probe
make logs
```
Prometheus/Grafana placeholders: `docker compose --profile monitoring up -d` (not routed by Caddy until Phase 3).

## Verify from outside
`https://tradingdots.onthewall.ovh/` shows the banner; `/healthz`, `/docs`, `/openapi.json`, `/metrics` return 404; the Grafana host returns 503; an external scan shows only 80/443.

## Resource guidance (6 vCPU, 12 GB RAM, 100 GB disk)
| Service | Memory limit |
|---|---|
| postgres | 2 GB |
| prometheus (profile) | 1.5 GB |
| app | 768 MB (1.5 CPU) |
| grafana (profile) | 512 MB |
| redis | 256 MB |
| caddy | 128 MB |
| Total with monitoring | about 5.2 GB, leaving about 6.8 GB for OS and page cache |

Prometheus is capped at 30 days and 15 GB. Phase 1 data volume is negligible. Estimates only; measure before relying on them.

## Rollback
- `make down` stops everything and keeps volumes. `docker compose down -v` deletes volumes: use only deliberately.
- Code: `git revert` the change (or check out tag `td-1.0`); `make up` rebuilds. Phase 1 has no migrations or business data.
- Secrets exposure suspected: rotate all values in `.env` (re-run bootstrap into a new file), recreate containers, and record it in `docs/decisions.md`.

## No second host, no off-host backups
By owner decision (DEC-006) neither is implemented or planned. Host loss means loss of local data.

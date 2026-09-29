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
make up                             # builds the image; postgres -> migrate (one-shot) -> app -> caddy
make health                         # app health via the internal probe
make logs
```
`migrate` connects as the database owner, creates/refreshes the `td_app` (web) and `td_ctl` (host CLI) roles from `TD_DB_APP_PASSWORD` / `TD_DB_CTL_PASSWORD`, applies pending migrations and exits. The app refuses to start if the schema is older or newer than the code.

## Accounts (host only)
There is no signup, invitation or reset. Run these from an interactive terminal (they refuse pipes and take no arguments):
```bash
docker compose run --rm -it ctl python scripts/create_admin.py            # first ADMIN only; refuses if one exists
docker compose run --rm -it ctl python scripts/rotate_admin_password.py   # ends all that user's sessions
```
Passwords: at least 14 characters, not common, not containing the username or product names. A locked-out or forgotten ADMIN password is recovered with `rotate_admin_password.py`. VIEWER provisioning is not available yet.

## Sessions, throttling and audit
- Idle timeout 30 min, absolute 12 h, 5 sessions per user. ADMINs can end a user's sessions from **Security** (confirm password, then type the phrase).
- Login is throttled per account+client (5 failures / 15 min), per client (20) and per account (30). Blocked attempts do not extend the lock; wait for the window (the response carries `Retry-After`).
- **Audit** shows the newest events and whether the hash chain verifies. `BROKEN` means rows were edited, removed or truncated: treat it as an incident (`docs/incident-response.md` IR-2), do not delete anything.
- Database schema: `docker compose run --rm migrate` re-applies pending migrations. Downgrades exist only for development (`python -m app.storage.database rollback --to N --i-understand-data-loss`) and destroy data; production rollback is roll-forward.

## Behind Caddy
The app trusts `X-Forwarded-For` only from `172.29.10.0/24` (the static `edge_app` subnet). If that subnet conflicts with your host network, change it in `docker-compose.yml` **and** `trusted_proxies` in `config/base.yaml` together. If they disagree every client looks like the proxy and login throttling becomes global.
Prometheus/Grafana placeholders: `docker compose --profile monitoring up -d` (not routed by Caddy until Phase 3).

## Verify from outside
`https://tradingdots.onthewall.ovh/` redirects to the login page, which shows the banner; after sign-in the overview loads; `/healthz`, `/docs`, `/openapi.json`, `/metrics` return 404; the Grafana host returns 503; an external scan shows only 80/443.

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

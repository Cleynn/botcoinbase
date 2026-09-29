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

## Monitoring (read-only)
Prometheus, Grafana and node-exporter start with `make up` (cAdvisor is optional: `docs/monitoring.md`). Nothing is published; Grafana is reached through Caddy at its own hostname with its own login (`docs/grafana-access.md`).
```bash
make verify-monitoring-config     # static rules for config, rules, dashboards, compose, caddy, Grafana secrets
make monitoring-status            # targets up/down and firing alerts (asks Prometheus inside its container)
```
- **Nothing is pushed to you.** There is no e-mail, chat or webhook. Check daily: the main dashboard's monitoring summary, Grafana's *Overview* (firing alerts) or `make monitoring-status`. Exit code 0 = healthy, 1 = attention (a required target is down or a critical/high alert is firing), 2 = could not query.
- The web application's summary (database, schema, audit chain, scraping) works even if Prometheus and Grafana are down; if it says *Prometheus has stopped scraping*, check the `prometheus` container and the `TD_METRICS_BIND` / `172.29.20.0/24` settings.
- Monitoring outages never change trading behaviour and never require action to keep the application safe. Restarting Prometheus/Grafana is always safe; their data is not backed up (Prometheus is disposable, dashboards are code).
- Prometheus keeps 30 days or 15 GB, whichever comes first. Disk alerts fire at 70/80/90 %.

## Behind Caddy
The app trusts `X-Forwarded-For` only from `172.29.10.0/24` (the static `edge_app` subnet). If that subnet conflicts with your host network, change it in `docker-compose.yml` **and** `trusted_proxies` in `config/base.yaml` together. If they disagree every client looks like the proxy and login throttling becomes global.
Prometheus, Grafana and node-exporter start with the rest of the stack (Phase 3). Optional cAdvisor: `docker compose --profile cadvisor up -d cadvisor` (`docs/monitoring.md`).

## Verify from outside
`https://tradingdots.onthewall.ovh/` redirects to the login page, which shows the banner; after sign-in the overview loads with the monitoring summary; `/healthz`, `/docs`, `/openapi.json`, `/metrics` return 404; the Grafana host shows Grafana's own login page; `/metrics` and Grafana's `/api/health` return 404 on both hosts; an external scan shows only 80/443.

## Resource guidance (6 vCPU, 12 GB RAM, 100 GB disk)
| Service | Memory limit |
|---|---|
| postgres | 2 GB |
| prometheus | 1.5 GB |
| app | 768 MB (1.5 CPU) |
| grafana | 512 MB |
| redis | 256 MB |
| caddy | 128 MB |
| node-exporter | 64 MB |
| Total | about 5.3 GB (5.6 GB with the optional cAdvisor), leaving about 6.7 GB for OS and page cache |

Prometheus is capped at 30 days and 15 GB (expected use is far lower; estimate). Estimates only; measure before relying on them.

## Rollback
- `make down` stops everything and keeps volumes. `docker compose down -v` deletes volumes: use only deliberately.
- Code: `git revert` the change (or check out tag `td-1.0`); `make up` rebuilds. Phase 1 has no migrations or business data.
- Secrets exposure suspected: rotate all values in `.env` (re-run bootstrap into a new file), recreate containers, and record it in `docs/decisions.md`.

## No second host, no off-host backups
By owner decision (DEC-006) neither is implemented or planned. Host loss means loss of local data.

## Pairs (Phase 4)
1. Set the fee attestation in `config/pair-policy.yaml` (`fees.operator_maker_rate`, `fees.attested_on`) from your own Coinbase fee tier; renew every 30 days. Without it every validation is INCONCLUSIVE.
2. `make pairs-discover`, then `make pairs-seed` (queues validation), then `make pairs-validate`. Check `/pairs`.
3. A pair is PAPER_ELIGIBLE only after a full PASS. It is never activated automatically; activation is unavailable until the bot phases exist.
4. Evidence expires after 24 h; `make pairs-validate` re-queues expired pairs' validation (they move to VALIDATING, then run).
5. Disable/archive: open the pair, choose the action, confirm your password, type the phrase. An active or previously active pair cannot be archived.
6. Rollback of this phase: `git revert`; `python -m app.storage.database rollback --to 1 --i-understand-data-loss` (development only) drops pair tables; audit events already written stay.

## Phase 5: market data, backtest, paper (operator steps)
1. `make pairs-discover`, seed, validate (Phase 4); attest fees in `config/pair-policy.yaml`.
2. `make market-import PRODUCT=P` (dry run: read the counts), then `make market-import PRODUCT=P COMMIT=1`.
3. `make market-quality PRODUCT=P` to store a data-quality report; investigate gaps and conflicts, do not fill them.
4. `make market-snapshot PRODUCT=P`, then `make backtest SNAPSHOT=<id>` (and `WALK=1`). Read the stress column.
5. Paper: activate the pair in the UI (needs `TD_MODE=PAPER`, paper session PAUSED), `paper start`, run `paper step` after each import, `paper stop` to pause (orders are cancelled, inventory is kept).
6. A HALTED session needs a person: review, then `paper start --acknowledge-halt`.
Snapshot files are read-only; back up the `datasets` volume with the database. Never edit a snapshot: a changed file is refused.

## Phase 6: review packages (operator steps)
1. As ADMIN open Review packages, choose retention days, confirm the password and type `ENABLE READ-ONLY REVIEW PACKAGES`.
2. Request a package: pick the period (UTC dates, at most 90 days, not in the future) and scope, confirm the password, type `CREATE READ-ONLY REVIEW PACKAGE`. The request is recorded only.
3. On the host: `make review-build`, then `make review-verify`. A FAILED package shows a fixed failure code (for example `SCANNER_HIT`: treat that as a sanitizer bug and report it, do not work around it).
4. In the UI verify and download the ZIP (POST). Read `README.md`; give the AI assistant only the files you choose; its reply is advisory and is never imported.
5. `make review-cleanup` (schedule it): expires packages past retention, removes their files, removes orphans. Everything is audited.
6. To stop: disable the feature (ADMIN, `DISABLE READ-ONLY REVIEW PACKAGES`). Downloads and requests stop at once.
If a package turns CORRUPT, do not use it; request a new one and investigate the volume.

## Phase 7: imported proposals (operator steps)
1. As ADMIN open Proposals, confirm the password and type `ENABLE UNTRUSTED PROPOSAL IMPORT`.
2. Import one JSON file or pasted text (confirm the password, type `IMPORT UNTRUSTED LLM PROPOSAL`). It is stored, not trusted.
3. On the host: `make proposal-validate`. A REJECTED proposal shows the rule ids; fix the source document and import a new one (the same bytes may be re-imported only after REJECTED or CLOSED).
4. Read a VALIDATED proposal as advice. Record a review, then optionally create a manual change request (`CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <id>`). That records intent only.
5. Make the actual change as a normal reviewed change; then attest IMPLEMENTED, BACKTESTED (report ids) and PAPER_VALIDATED (paper reports), and close.
6. `make proposal-cleanup` (schedule it): removes bytes of old CLOSED/REJECTED proposals and orphan files.
7. To stop: Disable import (one click). Existing proposals stay readable.

## Phase 8: safety machinery (operator steps)
1. `make safety-status` shows the control state, the last reconciliation and what blocks RESUME. Live trading is BLOCKED and this cannot change.
2. After every host start: `make safety-recover`. Without an exchange reader (every deployment of this build) it reports `NO_EXCHANGE_READER` and the bot stays PAUSED.
3. `make safety-reconcile` (schedule it) records a reconciliation; `make safety-monitor` evaluates breaker signals; `make safety-commands` runs a queued cancel of known bot orders.
4. ADMIN dashboard **Bot**: Pause, Resume (only after reconciliation), Cancel known orders, Kill switch; each needs your password and an exact phrase.
5. After a kill switch: run `safety-commands` (the cancel), then `safety kill-release --confirm "RELEASE KILL SWITCH"` on the host, `safety-recover`, then Resume in the dashboard. A kill or breaker never sells anything.
6. An UNKNOWN attempt blocks orders and resume until reconciliation adopts it or proves it absent. Foreign orders are flagged, never touched: deal with them on the exchange, then reconcile again.

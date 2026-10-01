# VPS deployment guide

Target: a Debian 12 or Ubuntu 22.04/24.04 VPS (2+ CPUs, 4 GB RAM minimum and 12 GB recommended,
20 GB free disk). The scripts in `scripts/vps/` do each step; `check.sh` tells you what is still missing.
Mode after deployment is whatever the Bot page says (default PAPER); LIVE orders need the host arming
described in `docs/capital-profiles.md` and are never placed by switching mode.

**Nothing here has been run on a real VPS yet.** The first run is the network test; report failures
with the output of `scripts/vps/check.sh` (it never prints secrets).

## Order of steps

| # | Command (run in the repository root) | What it does | You must see |
|---|---|---|---|
| 0 | `git clone <repo> && cd botcoinbase && git checkout claude/epic-carson-18byfr` | gets the code | the folder |
| 1 | `scripts/vps/check.sh` | lists what is missing | `Missing (do these in order ...)` |
| 2 | `sudo scripts/vps/02-install-prereqs.sh` | git, make, curl, python3 (with yaml and pydantic), dig, ss, ufw | `Prerequisites installed` |
| 3 | `sudo scripts/vps/01-install-docker.sh --add-user $USER` | Docker Engine + Compose v2 from Docker's apt repo | `Docker works.` (then log out and in) |
| 4 | DNS: A records for `tradingdots.onthewall.ovh` and `grafana.tradingdots.onthewall.ovh` -> server IPv4 (no AAAA) | certificates need it | `check.sh` DNS lines `[ OK ]` |
| 5 | `sudo scripts/vps/03-firewall.sh --ssh-port 22` (dry run), then add `--yes` | deny all incoming except SSH, 80, 443 | `Status: active` |
| 6 | `scripts/vps/04-setup-env.sh` | creates `.env` with random secrets (mode 600) | `.env created` |
| 7 | LIVE only: `sudo scripts/vps/06-install-key.sh` (asks the key name, then the PRIVATE key with echo off; or `--from-json FILE`) | writes `/opt/tradingdots/secrets/coinbase.json` (uid 10001, mode 600), sets `TD_COINBASE_KEY_HOST_PATH` and `TD_COINBASE_KEY_FILE` in `.env`; compose mounts it read-only into `batch` only | `Key installed`, then `docker compose --profile discovery run --rm batch live check` |
| 8 | `scripts/vps/05-first-start.sh` | verifies config, builds, starts, checks | `docker compose ps` shows services running |
| 9 | `docker compose run --rm -it ctl python scripts/create_admin.py` | first ADMIN (password typed at the prompt) | admin created |
| 10 | open `https://tradingdots.onthewall.ovh/` | log in | the dashboard over valid HTTPS |
| 11 | `scripts/vps/check.sh` | final check | `0 failure(s)` |

Optional, after step 7: `docker compose --profile discovery up -d feed` starts the exchange feed. It reads the Coinbase account every minute with GET requests only and the web interface shows the result on the Coinbase page (`/coinbase`). Check it with `docker compose --profile discovery logs --tail=5 feed`.

Steps 2 and 3 can be run in either order; step 4 can be done while they run (propagation takes time).

## Reading the check output

* `[ OK ]` fine. `[WARN]` not blocking, read the hint. `[FAIL]` something required is missing:
  the line below it starts with `-> fix:` and names the command. The final `Missing` list is in the
  order to fix them. Exit code 1 if any FAIL.
* `--offline` skips the checks that need the Internet.
* Run it with `sudo` if the firewall section says it cannot read ufw.

## Secrets

* The Coinbase key: the PRIVATE key and the key name are needed, never the public key. Use
  `scripts/vps/06-install-key.sh`. Never paste its contents (or the `privateKey`) in chat, an issue, a commit or `.env`.
* Create the key with View and Trade only, never Transfer, and allowlist the server IP on the key.
* `.env` holds the generated passwords: do not commit it or share it.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `docker daemon` FAIL | service stopped or user not in the docker group | `sudo systemctl enable --now docker`; `sudo usermod -aG docker $USER`; log in again |
| port 8080/444 WARN | another program holds Caddy's loopback port | `sudo ss -ltnp 'sport = :8080'`, then stop or move that service |
| port 80/443 WARN | Apache is not running | `sudo systemctl enable --now apache2`; see "Behind Apache" below |
| DNS FAIL | missing or wrong A record, or IPv6 AAAA present | fix at the DNS provider, wait, re-run |
| HTTPS FAIL | certificate not issued, or Apache cannot reach Caddy | `docker compose logs --tail=50 caddy`; `sudo tail /var/log/apache2/tradingdots-error.log`; needs DNS and open 80/443 |
| `.env` placeholder FAIL | edited by hand | `rm .env && scripts/vps/04-setup-env.sh` (regenerates all secrets: only before first start, the database password is fixed at first start) |
| Coinbase API FAIL | outbound 443 blocked | open outbound TCP 443 |

## Ufw and Docker

Docker writes its own firewall rules and can bypass ufw for published ports. This stack publishes only
Caddy's 8080 and 444, bound to 127.0.0.1 (see `docker-compose.yml`); confirm with `docker compose ps`
and `ss -ltn` that nothing of this stack listens publicly.

## Behind Apache

This server already runs Apache on 80/443 for another site, so Apache stays in front:

```
Internet -> Apache :80  -> 127.0.0.1:8080 -> Caddy :80  (ACME HTTP-01, redirect to HTTPS)
Internet -> Apache :443 -> 127.0.0.1:444  -> Caddy :443 -> app / Grafana
```

* Site file: `infra/apache/tradingdots.conf`, installed as `/etc/apache2/sites-available/tradingdots.conf`
  (`sudo a2ensite tradingdots`). Modules: `proxy proxy_http ssl headers rewrite`.
* Two certificates exist for the two hostnames. Apache's comes from certbot (`sudo certbot
  certificates`, renewed by the certbot timer). Caddy's own is issued over HTTP-01 through Apache's
  port 80; Apache verifies it on every proxied request.
* Client address: Apache drops any `X-Forwarded-*` header a visitor sends and adds the real address;
  Caddy trusts that header only from `172.29.30.1` (the `edge_public` gateway) and hands the app one
  address. If the `edge_public` subnet changes, change `trusted_proxies` in the Caddyfile with it,
  otherwise every visitor shares one login-throttle bucket.
* Apache's own access log holds full client addresses (Caddy's log masks them).

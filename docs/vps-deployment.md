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
| 2 | `sudo scripts/vps/02-install-prereqs.sh` | git, make, curl, python3, dig, ss, ufw | `Prerequisites installed` |
| 3 | `sudo scripts/vps/01-install-docker.sh --add-user $USER` | Docker Engine + Compose v2 from Docker's apt repo | `Docker works.` (then log out and in) |
| 4 | DNS: A records for `tradingdots.onthewall.ovh` and `grafana.tradingdots.onthewall.ovh` -> server IPv4 (no AAAA) | certificates need it | `check.sh` DNS lines `[ OK ]` |
| 5 | `sudo scripts/vps/03-firewall.sh --ssh-port 22` (dry run), then add `--yes` | deny all incoming except SSH, 80, 443 | `Status: active` |
| 6 | `scripts/vps/04-setup-env.sh` | creates `.env` with random secrets (mode 600) | `.env created` |
| 7 | LIVE only: `sudo scripts/vps/04-setup-env.sh --key-dir`, follow the printed 6 steps | key file outside the repo, mode 600 | credential section `[ OK ]` |
| 8 | `scripts/vps/05-first-start.sh` | verifies config, builds, starts, checks | `docker compose ps` shows services running |
| 9 | `docker compose run --rm -it ctl python scripts/create_admin.py` | first ADMIN (password typed at the prompt) | admin created |
| 10 | open `https://tradingdots.onthewall.ovh/` | log in | the dashboard over valid HTTPS |
| 11 | `scripts/vps/check.sh` | final check | `0 failure(s)` |

Steps 2 and 3 can be run in either order; step 4 can be done while they run (propagation takes time).

## Reading the check output

* `[ OK ]` fine. `[WARN]` not blocking, read the hint. `[FAIL]` something required is missing:
  the line below it starts with `-> fix:` and names the command. The final `Missing` list is in the
  order to fix them. Exit code 1 if any FAIL.
* `--offline` skips the checks that need the Internet.
* Run it with `sudo` if the firewall section says it cannot read ufw.

## Secrets

* The Coinbase key file is copied with `scp` straight to `/opt/tradingdots/secrets/coinbase.json`
  (mode 600). Never paste its contents (or the `privateKey`) in chat, an issue, a commit or `.env`.
* Create the key with View and Trade only, never Transfer, and allowlist the server IP on the key.
* `.env` holds the generated passwords: do not commit it or share it.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `docker daemon` FAIL | service stopped or user not in the docker group | `sudo systemctl enable --now docker`; `sudo usermod -aG docker $USER`; log in again |
| port 80/443 WARN | another web server | `sudo ss -ltnp 'sport = :80'`, then `sudo systemctl disable --now nginx apache2` |
| DNS FAIL | missing or wrong A record, or IPv6 AAAA present | fix at the DNS provider, wait, re-run |
| HTTPS FAIL | certificate not issued | `docker compose logs --tail=50 caddy`; needs DNS and open 80/443 |
| `.env` placeholder FAIL | edited by hand | `rm .env && scripts/vps/04-setup-env.sh` (regenerates all secrets: only before first start, the database password is fixed at first start) |
| Coinbase API FAIL | outbound 443 blocked | open outbound TCP 443 |

## Ufw and Docker

Docker writes its own firewall rules and can bypass ufw for published ports. This stack publishes only
Caddy's 80 and 443 (see `docker-compose.yml`); confirm with `docker compose ps` and
`ss -ltn` that nothing else listens publicly.

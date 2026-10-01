#!/usr/bin/env bash
# TradingDots VPS readiness check.
#
# Prints one line per requirement: [ OK ], [WARN] or [FAIL]. Every FAIL and WARN names the exact
# command or script that fixes it. Exit code: 0 = nothing required is missing, 1 = something is.
# It changes nothing on the machine and never prints a secret.
#
#   scripts/vps/check.sh              full check (needs outbound network for the network checks)
#   scripts/vps/check.sh --offline    skip every check that needs the Internet
#   scripts/vps/check.sh --help
set -u
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

OFFLINE=0
for arg in "$@"; do
  case "$arg" in
    --offline) OFFLINE=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'; else G=""; R=""; Y=""; N=""; fi
PASS=0; FAIL=0; WARN=0
FIXES=()

section() { printf '\n== %s ==\n' "$1"; }
ok()   { printf '%s[ OK ]%s %-30s %s\n' "$G" "$N" "$1" "$2"; PASS=$((PASS + 1)); }
bad()  { printf '%s[FAIL]%s %-30s %s\n       -> fix: %s\n' "$R" "$N" "$1" "$2" "$3"; FAIL=$((FAIL + 1)); FIXES+=("$1  ->  $3"); }
warn() { printf '%s[WARN]%s %-30s %s\n       -> fix: %s\n' "$Y" "$N" "$1" "$2" "$3"; WARN=$((WARN + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }

env_value() { [ -f .env ] && grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }

# ------------------------------------------------------------------ system
section "System"
if [ "$(uname -s)" = "Linux" ]; then ok "operating system" "Linux $(uname -r)"; else bad "operating system" "$(uname -s) is not Linux" "use a Linux VPS (Debian 12 or Ubuntu 22.04/24.04)"; fi
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ok "distribution" "${PRETTY_NAME:-$ID}" ;;
    *) warn "distribution" "${PRETTY_NAME:-unknown}; the install scripts support Debian and Ubuntu only" "install Docker by hand: https://docs.docker.com/engine/install/" ;;
  esac
fi
CPUS="$(nproc 2>/dev/null || echo 0)"
if [ "$CPUS" -ge 2 ]; then ok "CPU cores" "$CPUS"; else bad "CPU cores" "$CPUS (need at least 2; 6 recommended)" "choose a larger VPS plan"; fi
MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$MEM_MB" -ge 7000 ]; then ok "memory" "${MEM_MB} MB"; elif [ "$MEM_MB" -ge 3500 ]; then warn "memory" "${MEM_MB} MB (12 GB recommended; the container limits add up to about 7 GB)" "choose a larger VPS plan"; else bad "memory" "${MEM_MB} MB (need at least 4 GB)" "choose a larger VPS plan"; fi
DISK_MB="$(df -Pm "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
DISK_MB="${DISK_MB:-0}"
if [ "$DISK_MB" -ge 20000 ]; then ok "free disk" "$((DISK_MB / 1024)) GB"; else bad "free disk" "$((DISK_MB / 1024)) GB free (need at least 20 GB)" "free space or enlarge the disk (images, database, market data, backups)"; fi
if [ "$(id -u)" -eq 0 ]; then ok "privileges" "running as root"; elif have sudo && sudo -n true 2>/dev/null; then ok "privileges" "sudo without password"; else warn "privileges" "not root and no passwordless sudo" "run the install scripts with: sudo scripts/vps/01-install-docker.sh (it will ask for your password)"; fi

# ------------------------------------------------------------------ tools
section "Tools"
for tool in git curl make; do
  if have "$tool"; then ok "$tool" "$("$tool" --version 2>&1 | head -1)"; else bad "$tool" "not installed" "sudo scripts/vps/02-install-prereqs.sh"; fi
done
if have python3 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then ok "python3" "$(python3 --version 2>&1)"; else bad "python3" "missing or older than 3.9 (used by bootstrap and this check)" "sudo scripts/vps/02-install-prereqs.sh"; fi
if have dig || have getent; then ok "DNS lookup tool" "$(have dig && echo dig || echo getent)"; else warn "DNS lookup tool" "neither dig nor getent" "sudo scripts/vps/02-install-prereqs.sh"; fi

# ------------------------------------------------------------------ docker
section "Docker"
DOCKER_OK=0
if have docker; then
  if DV="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" && [ -n "$DV" ]; then
    ok "docker engine" "server $DV"
    DOCKER_OK=1
  elif docker version >/dev/null 2>&1 || docker --version >/dev/null 2>&1; then
    bad "docker daemon" "the client exists but the daemon is not reachable (stopped, or your user may not use it)" "sudo systemctl enable --now docker   and, if you are not root: sudo usermod -aG docker \$USER && log out and in"
  fi
else
  bad "docker engine" "not installed" "sudo scripts/vps/01-install-docker.sh"
fi
if [ "$DOCKER_OK" -eq 1 ]; then
  if CV="$(docker compose version --short 2>/dev/null)" && [ -n "$CV" ]; then ok "docker compose" "v$CV"; else bad "docker compose" "the Compose v2 plugin is missing" "sudo scripts/vps/01-install-docker.sh"; fi
fi

# ------------------------------------------------------------------ project files
section "Project"
if [ -f docker-compose.yml ] && [ -f Dockerfile ] && [ -f pyproject.toml ]; then ok "repository" "$ROOT"; else bad "repository" "docker-compose.yml, Dockerfile or pyproject.toml missing" "run this from a full checkout of the repository"; fi
if [ -f .env ]; then
  mode="$(stat -c '%a' .env 2>/dev/null || echo '?')"
  if [ "$mode" = "600" ]; then ok ".env file" "present, mode 600"; else bad ".env file" "mode is $mode (must be 600)" "chmod 600 .env"; fi
  if grep -Eiq 'CHANGE_ME|REPLACE_ME|changeme|placeholder' .env; then bad ".env secrets" "placeholder values remain" "rm .env && scripts/vps/04-setup-env.sh   (regenerates every secret)"; else ok ".env secrets" "no placeholder left"; fi
  APP_HOST="$(env_value TD_APP_HOSTNAME)"; GRAFANA_HOST="$(env_value TD_GRAFANA_HOSTNAME)"
  if [ -n "$APP_HOST" ] && [ -n "$GRAFANA_HOST" ]; then ok "hostnames" "$APP_HOST and $GRAFANA_HOST"; else bad "hostnames" "TD_APP_HOSTNAME or TD_GRAFANA_HOSTNAME is empty" "edit .env and set both"; fi
else
  APP_HOST=""; GRAFANA_HOST=""
  bad ".env file" "does not exist" "scripts/vps/04-setup-env.sh"
fi
if [ "$DOCKER_OK" -eq 1 ] && [ -f .env ]; then
  if docker compose config -q 2>/dev/null; then ok "compose configuration" "valid"; else bad "compose configuration" "docker compose config reports an error" "docker compose config   (read the first error line)"; fi
fi
if have python3 && [ -f scripts/verify_security_config.py ] && [ -f .env ]; then
  if python3 scripts/verify_security_config.py >/dev/null 2>&1; then ok "security config check" "passes"; else warn "security config check" "scripts/verify_security_config.py reports a problem" "python3 scripts/verify_security_config.py   (it names each problem)"; fi
fi

# ------------------------------------------------------------------ network
section "Network"
ports_in_use() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
if have ss; then
  # Caddy listens on loopback 8080/444; the host's Apache owns 80/443 and proxies both hostnames to it.
  for p in 8080 444; do
    if ports_in_use "$p"; then
      if [ "$DOCKER_OK" -eq 1 ] && docker compose ps --status running --services 2>/dev/null | grep -qx caddy; then ok "port $p" "in use by this stack's Caddy"; else warn "port $p" "already in use by another program (Caddy cannot bind it)" "sudo ss -ltnp 'sport = :$p'   then stop or move that service"; fi
    else
      ok "port $p" "free"
    fi
  done
  for p in 80 443; do
    if ports_in_use "$p"; then ok "port $p" "in use (the host's Apache, which proxies to Caddy)"; else warn "port $p" "nothing listens on it, so the site is unreachable" "sudo systemctl enable --now apache2   (site file: infra/apache/tradingdots.conf)"; fi
  done
else
  warn "ports 80/443/444/8080" "ss is not installed, cannot check" "sudo scripts/vps/02-install-prereqs.sh"
fi
if have ufw; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    rules="$(ufw status 2>/dev/null)"
    if echo "$rules" | grep -Eq '(^|[[:space:]])80(/tcp)?[[:space:]]+ALLOW' && echo "$rules" | grep -Eq '(^|[[:space:]])443(/tcp)?[[:space:]]+ALLOW'; then ok "firewall (ufw)" "active, 80 and 443 allowed"; else bad "firewall (ufw)" "active but 80 and/or 443 are not allowed" "sudo scripts/vps/03-firewall.sh"; fi
  else
    bad "firewall (ufw)" "installed but not active, or not readable without root (re-run with sudo)" "sudo scripts/vps/03-firewall.sh"
  fi
else
  bad "firewall (ufw)" "ufw is not installed" "sudo scripts/vps/02-install-prereqs.sh && sudo scripts/vps/03-firewall.sh"
fi
if [ "$OFFLINE" -eq 1 ]; then
  warn "network checks" "skipped (--offline)" "run scripts/vps/check.sh without --offline"
else
  PUBLIC_IP="$(curl -4fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
  if [ -n "$PUBLIC_IP" ]; then ok "this server's public IP" "$PUBLIC_IP"; else warn "this server's public IP" "could not be determined (no outbound HTTPS?)" "check outbound connectivity: curl -4 https://api.ipify.org"; fi
  for h in "$APP_HOST" "$GRAFANA_HOST"; do
    [ -n "$h" ] || continue
    if have dig; then ip="$(dig +short A "$h" 2>/dev/null | tail -1)"; else ip="$(getent ahostsv4 "$h" 2>/dev/null | awk 'NR==1 {print $1}')"; fi
    if [ -z "$ip" ]; then bad "DNS $h" "does not resolve" "create an A record: $h -> ${PUBLIC_IP:-<server-ip>}"
    elif [ -n "$PUBLIC_IP" ] && [ "$ip" != "$PUBLIC_IP" ]; then bad "DNS $h" "resolves to $ip but this server is $PUBLIC_IP" "fix the A record of $h to $PUBLIC_IP, then wait for propagation"
    else ok "DNS $h" "-> $ip"; fi
  done
  if curl -fsS --max-time 8 -o /dev/null https://api.coinbase.com/api/v3/brokerage/time 2>/dev/null; then ok "Coinbase public API" "reachable"; else bad "Coinbase public API" "https://api.coinbase.com is not reachable" "allow outbound TCP 443 to api.coinbase.com (provider firewall or ufw default-deny outgoing)"; fi
  if curl -sS --max-time 8 -o /dev/null https://registry-1.docker.io/v2/ 2>/dev/null; then ok "Docker registry" "reachable"; else warn "Docker registry" "registry-1.docker.io is not reachable (images cannot be pulled)" "allow outbound TCP 443"; fi
  if [ -n "$APP_HOST" ] && [ "$DOCKER_OK" -eq 1 ] && docker compose ps --status running --services 2>/dev/null | grep -qx caddy; then
    code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "https://$APP_HOST/" 2>/dev/null || true)"
    case "$code" in
      200|303|302) ok "HTTPS $APP_HOST" "valid certificate, HTTP $code" ;;
      *) bad "HTTPS $APP_HOST" "no valid HTTPS answer (got '${code:-none}')" "docker compose logs --tail=50 caddy; sudo tail /var/log/apache2/tradingdots-error.log   (certificate issuance needs DNS, ports 80/443 open and Apache proxying to Caddy)" ;;
    esac
  fi
fi

# ------------------------------------------------------------------ Coinbase credential
section "Coinbase credential (needed only for LIVE data and orders)"
KEY_FILE="${TD_COINBASE_KEY_FILE:-$(env_value TD_COINBASE_KEY_FILE)}"
if [ -z "$KEY_FILE" ]; then
  warn "credential file" "TD_COINBASE_KEY_FILE is not set (paper and backtest do not need it)" "scripts/vps/04-setup-env.sh --key-dir shows the exact steps"
else
  if [ ! -f "$KEY_FILE" ] || [ -L "$KEY_FILE" ]; then bad "credential file" "$KEY_FILE is missing or a symlink" "place the JSON key file there (see docs/vps-deployment.md, step 6)"
  else
    kmode="$(stat -c '%a' "$KEY_FILE")"; kown="$(stat -c '%U' "$KEY_FILE")"
    if [ "$kmode" = "600" ] || [ "$kmode" = "400" ]; then ok "credential mode" "$kmode (owner $kown)"; else bad "credential mode" "$kmode (must be 600 or 400)" "chmod 600 '$KEY_FILE'"; fi
    case "$KEY_FILE" in "$ROOT"/*) bad "credential location" "inside the repository" "move it outside the repository, e.g. /opt/tradingdots/secrets/" ;; *) ok "credential location" "outside the repository" ;; esac
    if python3 - "$KEY_FILE" <<'PY' 2>/dev/null
import json, sys
data = json.load(open(sys.argv[1]))
assert isinstance(data.get("name"), str) and "PRIVATE KEY" in data.get("privateKey", "")
PY
    then ok "credential format" "has name and privateKey (contents not shown)"; else bad "credential format" "not the JSON Coinbase provides (name + privateKey)" "download the CDP API key JSON again and replace the file"; fi
  fi
fi

# ------------------------------------------------------------------ stack
section "Stack"
if [ "$DOCKER_OK" -eq 1 ] && [ -f .env ]; then
  expected="$(docker compose config --services 2>/dev/null | sort | tr '\n' ' ')"
  running="$(docker compose ps --status running --services 2>/dev/null | sort | tr '\n' ' ')"
  if [ -z "$running" ]; then
    bad "services" "no service is running" "scripts/vps/05-first-start.sh"
  else
    missing=""
    for s in app caddy postgres redis prometheus grafana; do case " $running " in *" $s "*) ;; *) missing="$missing $s" ;; esac; done
    if [ -z "$missing" ]; then ok "core services" "app, caddy, postgres, redis, prometheus, grafana running"; else bad "core services" "not running:$missing" "docker compose up -d --build && docker compose logs --tail=40$missing"; fi
  fi
  : "$expected"
else
  warn "services" "cannot be checked yet (Docker or .env missing)" "fix the items above first"
fi

# ------------------------------------------------------------------ summary
printf '\n== Summary ==\n%s%d ok%s, %s%d warning(s)%s, %s%d failure(s)%s\n' "$G" "$PASS" "$N" "$Y" "$WARN" "$N" "$R" "$FAIL" "$N"
if [ "$FAIL" -gt 0 ]; then
  printf '\nMissing (do these in order, then run this check again):\n'
  i=1; for f in "${FIXES[@]}"; do printf '  %d. %s\n' "$i" "$f"; i=$((i + 1)); done
  printf '\nFull guide: docs/vps-deployment.md\n'
  exit 1
fi
printf 'Nothing required is missing.\n'
exit 0

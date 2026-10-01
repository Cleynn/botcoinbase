#!/usr/bin/env bash
# Configures ufw: deny all incoming except SSH, 80 and 443. Run as root.
#   sudo scripts/vps/03-firewall.sh [--ssh-port N] [--yes]
# Without --yes it only prints what it would do. It always allows the SSH port first so that
# enabling the firewall cannot lock you out of the machine.
set -euo pipefail
SSH_PORT=22; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-port) SSH_PORT="${2:?--ssh-port needs a number}"; shift 2 ;;
    --yes) YES=1; shift ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
command -v ufw >/dev/null || { echo "ufw missing: run scripts/vps/02-install-prereqs.sh" >&2; exit 1; }
case "$SSH_PORT" in ''|*[!0-9]*) echo "bad ssh port" >&2; exit 2 ;; esac

cat <<PLAN
Plan: default deny incoming, allow outgoing; allow ${SSH_PORT}/tcp (SSH), 80/tcp, 443/tcp; enable ufw.
Check that ${SSH_PORT} is really the port you are connected on: ss -tnp | grep ssh
Note: Docker publishes ports itself; only Caddy (80/443) is published by this stack's compose file.
PLAN
if [ "$YES" -ne 1 ]; then echo "Dry run. Re-run with --yes to apply."; exit 0; fi
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
echo "Next: scripts/vps/04-setup-env.sh"

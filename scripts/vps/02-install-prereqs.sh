#!/usr/bin/env bash
# Installs the small tools the other scripts need (Debian or Ubuntu). Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
# python3-yaml and python3-pydantic: scripts/verify_security_config.py runs on the host (05-first-start.sh).
apt-get install -y git make curl python3 python3-yaml python3-pydantic openssl iproute2 dnsutils ufw ca-certificates
echo "Prerequisites installed. Next: sudo scripts/vps/03-firewall.sh"

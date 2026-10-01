#!/usr/bin/env bash
# Installs the small tools the other scripts need (Debian or Ubuntu). Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git make curl python3 openssl iproute2 dnsutils ufw ca-certificates
echo "Prerequisites installed. Next: sudo scripts/vps/03-firewall.sh"

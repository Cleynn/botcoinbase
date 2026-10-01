#!/usr/bin/env bash
# Installs Docker Engine and the Compose v2 plugin from Docker's official apt repository
# (Debian or Ubuntu). Run as root: sudo scripts/vps/01-install-docker.sh [--add-user NAME]
# Safe to run twice. It does not start the TradingDots stack.
set -euo pipefail

ADD_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --add-user) ADD_USER="${2:?--add-user needs a user name}"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) echo "only Debian and Ubuntu are supported (found: ${ID:-unknown}); see https://docs.docker.com/engine/install/" >&2; exit 1 ;; esac

if docker version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker and Compose are already installed: $(docker --version)"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker
if [ -n "$ADD_USER" ]; then
  usermod -aG docker "$ADD_USER"
  echo "User $ADD_USER added to the docker group: log out and back in for it to apply."
fi
docker run --rm hello-world >/dev/null && echo "Docker works." || echo "WARNING: 'docker run hello-world' failed (check outbound access to registry-1.docker.io)." >&2
echo "Next: sudo scripts/vps/02-install-prereqs.sh"

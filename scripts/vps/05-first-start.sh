#!/usr/bin/env bash
# First start: validates the configuration, builds the images, starts the stack and checks it.
#   scripts/vps/05-first-start.sh
# Needs Docker, a valid .env and DNS pointing at this server (for certificates).
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo ".env missing: run scripts/vps/04-setup-env.sh" >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose missing: run scripts/vps/01-install-docker.sh" >&2; exit 1; }

echo "== 1/4 configuration =="
python3 scripts/verify_security_config.py
docker compose config -q
echo "== 2/4 build and start (the first build downloads images and Python packages: several minutes) =="
docker compose up -d --build
echo "== 3/4 waiting for the services =="
for i in $(seq 1 30); do
  if docker compose ps --status running --services | grep -qx app; then break; fi
  sleep 5
done
docker compose ps
echo "== 4/4 readiness check =="
scripts/vps/check.sh || true
cat <<NEXT

Create the first administrator (you choose the password at the prompt):
  docker compose run --rm -it ctl python scripts/create_admin.py
Then open https://$(grep -E '^TD_APP_HOSTNAME=' .env | cut -d= -f2-)/ and log in.
Logs if something is wrong: docker compose logs --tail=100 SERVICE
NEXT

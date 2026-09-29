#!/usr/bin/env bash
# Creates .env from .env.example with freshly generated random secrets (mode 0600).
# Never prints secrets. Does not start containers, touch DNS/TLS/firewall, or contact any service.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -e .env ]; then
  echo "refusing to overwrite existing .env" >&2
  exit 1
fi

for tool in docker python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing prerequisite: $tool" >&2; exit 1; }
done

umask 077

python3 - <<'PY'
import pathlib, secrets

text = pathlib.Path(".env.example").read_text(encoding="utf-8")
out = []
for line in text.splitlines():
    if "=CHANGE_ME" in line:
        key = line.split("=", 1)[0]
        line = f"{key}={secrets.token_urlsafe(48)}"
    out.append(line)
pathlib.Path(".env").write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chmod 600 .env
echo ".env created (mode 600). Review TD_APP_HOSTNAME / TD_GRAFANA_HOSTNAME, then run: make verify-security-config"

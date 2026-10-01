#!/usr/bin/env bash
# Creates .env with random secrets (via scripts/bootstrap.sh) and, for LIVE data/orders, prepares the
# directory that holds the Coinbase key file. Never prints a secret and never asks for one in chat.
#   scripts/vps/04-setup-env.sh            create .env (refuses to overwrite)
#   scripts/vps/04-setup-env.sh --key-dir  also create /opt/tradingdots/secrets (mode 700) and show how
#                                          to put the key file there (needs root)
set -euo pipefail
cd "$(dirname "$0")/../.."
KEY_DIR_FLAG=0
for a in "$@"; do
  case "$a" in
    --key-dir) KEY_DIR_FLAG=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if [ ! -e .env ]; then ./scripts/bootstrap.sh; else echo ".env already exists: left unchanged."; fi
chmod 600 .env
echo "Hostnames in .env: $(grep -E '^TD_(APP|GRAFANA)_HOSTNAME=' .env | tr '\n' ' ')"
echo "If these are not your domains, edit .env now (nano .env), then re-run scripts/vps/check.sh."

if [ "$KEY_DIR_FLAG" -eq 1 ]; then
  DIR=/opt/tradingdots/secrets
  if [ "$(id -u)" -ne 0 ]; then echo "--key-dir needs root: sudo $0 --key-dir" >&2; exit 1; fi
  install -d -m 700 "$DIR"
  cat <<STEPS

Coinbase key file (needed only for LIVE data and orders; PAPER and BACKTEST do not need it):
  1. On the Coinbase Developer Platform create a Secret API key (ECDSA) with View and Trade
     permissions only. NEVER enable Transfer or withdrawal. Add this server's IP to the key's allowlist.
  2. Download the JSON file. It holds "name" and "privateKey".
  3. Copy it from your computer WITHOUT pasting it anywhere else:
       scp cdp_api_key.json USER@SERVER:$DIR/coinbase.json
  4. On the server:  sudo chmod 600 $DIR/coinbase.json
  5. Add this line to .env:  TD_COINBASE_KEY_FILE=$DIR/coinbase.json
  6. Check:  scripts/vps/check.sh   (the credential section must be all [ OK ])
STEPS
fi
echo "Next: scripts/vps/05-first-start.sh"

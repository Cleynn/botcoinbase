#!/usr/bin/env bash
# Stores the Coinbase (CDP) API key on this server so the host/batch container can read it.
#   sudo scripts/vps/06-install-key.sh                 asks for the key name and the PRIVATE key
#   sudo scripts/vps/06-install-key.sh --from-json F   copies the JSON file Coinbase gave you (then
#                                                      delete F yourself)
# Which key: the PRIVATE key (the "privateKey" PEM text from the JSON file) and the key
# NAME ("organizations", then the org and key ids). The public key is not needed: Coinbase keeps it.
# The secret is typed with echo off, never an argument, never printed, never written to the repository
# or to .env (only the path goes in .env). The file is owned by uid 10001 (the container user), 0600.
set -euo pipefail
cd "$(dirname "$0")/../.."
DIR=/opt/tradingdots/secrets
FILE="$DIR/coinbase.json"
FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from-json) FROM="${2:?--from-json needs a file}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
[ -t 0 ] || [ -n "$FROM" ] || { echo "run this in an interactive terminal (the secret is never read from a pipe)" >&2; exit 1; }
[ -f .env ] || { echo ".env missing: run scripts/vps/04-setup-env.sh first" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 missing: run scripts/vps/02-install-prereqs.sh" >&2; exit 1; }

install -d -m 700 -o 10001 -g 10001 "$DIR"
umask 077
TMP="$(mktemp "$DIR/.incoming.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

if [ -n "$FROM" ]; then
  [ -f "$FROM" ] && [ ! -L "$FROM" ] || { echo "not a regular file: $FROM" >&2; exit 1; }
  cat "$FROM" > "$TMP"
else
  read -r -p "Key name (the "name" field of the JSON): " KEY_NAME
  echo "Paste the PRIVATE key (nothing is shown). Finish with a line containing only END, or just press"
  echo "Enter after the final -----END line. A single line with literal \\n is accepted."
  PEM=""
  while IFS= read -r -s line; do
    [ "$line" = "END" ] && break
    PEM="${PEM}${line}"$'\n'
    case "$line" in *"-----END"*"PRIVATE KEY-----"*) break ;; esac
    case "$line" in *'\n'*"-----END"*) break ;; esac
  done
  echo
  KEY_NAME="$KEY_NAME" PEM="$PEM" python3 - > "$TMP" <<'PY'
import json, os
pem = os.environ["PEM"].replace("\\n", "\n").strip() + "\n"
print(json.dumps({"name": os.environ["KEY_NAME"].strip(), "privateKey": pem}))
PY
fi

python3 - "$TMP" <<'PY' || { echo "REFUSED: the key is not in the expected format (nothing was installed)." >&2; exit 1; }
import json, re, sys
d = json.load(open(sys.argv[1]))
name, key = d.get("name", ""), d.get("privateKey", "")
assert re.fullmatch(r"[A-Za-z0-9/_.:-]{8,200}", name), "name"
import re as _re
assert _re.search(r"-----BEGIN (EC )?PRIVATE KEY-----", key), "private key"
assert "-----END" in key, "end line"
assert "PUBLIC KEY" not in key, "that is a public key"
PY

chown 10001:10001 "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$FILE"
trap - EXIT
if grep -q '^TD_COINBASE_KEY_HOST_PATH=' .env; then
  sed -i "s#^TD_COINBASE_KEY_HOST_PATH=.*#TD_COINBASE_KEY_HOST_PATH=$FILE#" .env
else
  printf 'TD_COINBASE_KEY_HOST_PATH=%s\n' "$FILE" >> .env
fi
if grep -q '^TD_COINBASE_KEY_FILE=' .env; then
  sed -i "s#^TD_COINBASE_KEY_FILE=.*#TD_COINBASE_KEY_FILE=$FILE#" .env
else
  printf 'TD_COINBASE_KEY_FILE=%s\n' "$FILE" >> .env
fi
chmod 600 .env
ls -l "$FILE"
cat <<NEXT
Key installed (contents not shown). Mounted read-only into the 'batch' service only, never the web app.
Check it without placing any order:
  docker compose --profile discovery run --rm batch live check
Then: scripts/vps/check.sh
NEXT

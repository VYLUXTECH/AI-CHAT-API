#!/bin/bash
# Refresh the container's qwen session: harvest the live JWT (+cookies) from
# local Chrome via CDP, then push the updated state.json over SFTP.
#   env: CONTAINER_HOST / CONTAINER_PORT / CONTAINER_USER / CONTAINER_PASS
#       AICHAT_HOME  (defaults /root/aichat)
set -e
set -e
AICHAT_HOME="${AICHAT_HOME:-/root/aichat}"
node "$AICHAT_HOME/harvest.js" > "$AICHAT_HOME/state.json.tmp"
python3 - "$AICHAT_HOME/state.json.tmp" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get('token'), 'no token harvested'
PY
mv "$AICHAT_HOME/state.json.tmp" "$AICHAT_HOME/state.json"; chmod 600 "$AICHAT_HOME/state.json"
sshpass -p "$CONTAINER_PASS" sftp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -P "${CONTAINER_PORT:-2022}" "$CONTAINER_USER@$CONTAINER_HOST" \
  <<< "put $AICHAT_HOME/state.json /AICHAT/state.json" >/dev/null
echo "$(date -u '+%F %T') refreshed + pushed"

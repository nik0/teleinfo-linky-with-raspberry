#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_ARGS=()
[ -f "$DIR/.env" ] && ENV_ARGS+=(--env-file "$DIR/.env")
[ -f /etc/default/teleinfo ] && ENV_ARGS+=(--env-file /etc/default/teleinfo)

exec /usr/bin/docker run --rm --name teleinfo --network=host \
  "${ENV_ARGS[@]}" \
  -v "$DIR":/app \
  -v /var/log/teleinfo:/logs \
  --device=/dev/ttyAMA0:/dev/ttyAMA0 teleinfo python3 /app/teleinfo.py

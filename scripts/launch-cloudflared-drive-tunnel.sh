#!/usr/bin/env bash
set -euo pipefail

# Runs the durable Drive Cloudflare Tunnel. Quick Tunnels are intentionally not
# the default because their trycloudflare.com hostnames are process-scoped.

ENV_FILE="${ENV_FILE:-/etc/v2x-drive-tunnel.env}"
CONFIG_FILE="${CONFIG_FILE:-/etc/cloudflared/v2x-drive.yml}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-drive.path2v2x.net}"
ORIGIN_SERVICE="${ORIGIN_SERVICE:-http://localhost:8765}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
  echo "Starting named Cloudflare Tunnel for ${PUBLIC_HOSTNAME} with token-managed config."
  exec /usr/local/bin/cloudflared tunnel --no-autoupdate --logfile /tmp/v2x-cloudflared.log --loglevel info run --token "${TUNNEL_TOKEN}"
fi

if [[ -n "${TUNNEL_NAME:-}" || -n "${TUNNEL_ID:-}" ]]; then
  tunnel_ref="${TUNNEL_NAME:-${TUNNEL_ID}}"
  echo "Starting named Cloudflare Tunnel ${tunnel_ref} for ${PUBLIC_HOSTNAME} using ${CONFIG_FILE}."
  exec /usr/local/bin/cloudflared tunnel --no-autoupdate --config "${CONFIG_FILE}" --logfile /tmp/v2x-cloudflared.log --loglevel info run "${tunnel_ref}"
fi

if [[ "${ALLOW_QUICK_TUNNEL:-0}" == "1" ]]; then
  echo "WARNING: starting break-glass Quick Tunnel for ${ORIGIN_SERVICE}; hostname will not be stable."
  exec /usr/local/bin/cloudflared tunnel --url "${ORIGIN_SERVICE}" --logfile /tmp/v2x-cloudflared.log --loglevel info
fi

cat >&2 <<EOF
No named Cloudflare Tunnel credentials found.

Configure one of:
  - TUNNEL_TOKEN in ${ENV_FILE} for a remotely managed Cloudflare Tunnel, or
  - TUNNEL_NAME/TUNNEL_ID plus ${CONFIG_FILE} for a locally managed tunnel.

Expected public hostname: ${PUBLIC_HOSTNAME}
Expected origin service: ${ORIGIN_SERVICE}

Set ALLOW_QUICK_TUNNEL=1 only for temporary break-glass debugging.
EOF
exit 78

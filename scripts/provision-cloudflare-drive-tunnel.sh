#!/usr/bin/env bash
set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-v2x-drive}"
DRIVE_HOSTNAME="${DRIVE_HOSTNAME:-drive.path2v2x.net}"
ORIGIN_SERVICE="${ORIGIN_SERVICE:-http://localhost:8765}"
CONFIG_OUTPUT="${CONFIG_OUTPUT:-/etc/cloudflared/v2x-drive.yml}"
ENV_OUTPUT="${ENV_OUTPUT:-/etc/v2x-drive-tunnel.env}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need cloudflared
need jq

if ! cloudflared tunnel list --output json >/tmp/v2x-cloudflared-tunnels.json 2>/tmp/v2x-cloudflared-list.err; then
  cat >&2 <<EOF
cloudflared is not authenticated for named tunnel management.

Run this once on the Path PC, then rerun this script:
  cloudflared tunnel login

Original error:
$(cat /tmp/v2x-cloudflared-list.err)
EOF
  exit 2
fi

tunnel_id="$(jq -r --arg name "${TUNNEL_NAME}" '.[] | select(.name == $name) | .id' /tmp/v2x-cloudflared-tunnels.json | head -n 1)"

if [[ -z "${tunnel_id}" || "${tunnel_id}" == "null" ]]; then
  echo "Creating Cloudflare Tunnel: ${TUNNEL_NAME}"
  cloudflared tunnel create "${TUNNEL_NAME}"
  cloudflared tunnel list --output json >/tmp/v2x-cloudflared-tunnels.json
  tunnel_id="$(jq -r --arg name "${TUNNEL_NAME}" '.[] | select(.name == $name) | .id' /tmp/v2x-cloudflared-tunnels.json | head -n 1)"
fi

if [[ -z "${tunnel_id}" || "${tunnel_id}" == "null" ]]; then
  echo "Could not find or create tunnel ${TUNNEL_NAME}" >&2
  exit 3
fi

credentials_file="${HOME}/.cloudflared/${tunnel_id}.json"
if [[ ! -f "${credentials_file}" ]]; then
  echo "Missing tunnel credentials file: ${credentials_file}" >&2
  exit 4
fi

echo "Routing ${DRIVE_HOSTNAME} to tunnel ${TUNNEL_NAME} (${tunnel_id})"
cloudflared tunnel route dns "${TUNNEL_NAME}" "${DRIVE_HOSTNAME}"

sudo install -d -m 0755 /etc/cloudflared
tmp_config="$(mktemp)"
cat >"${tmp_config}" <<EOF
tunnel: ${tunnel_id}
credentials-file: ${credentials_file}

ingress:
  - hostname: ${DRIVE_HOSTNAME}
    service: ${ORIGIN_SERVICE}
  - service: http_status:404
EOF
sudo install -m 0644 "${tmp_config}" "${CONFIG_OUTPUT}"
rm -f "${tmp_config}"

tmp_env="$(mktemp)"
cat >"${tmp_env}" <<EOF
PUBLIC_HOSTNAME=${DRIVE_HOSTNAME}
ORIGIN_SERVICE=${ORIGIN_SERVICE}
TUNNEL_NAME=${TUNNEL_NAME}
CONFIG_FILE=${CONFIG_OUTPUT}
EOF
sudo install -m 0600 "${tmp_env}" "${ENV_OUTPUT}"
rm -f "${tmp_env}"

echo "Named Drive tunnel provisioned."
echo "Tunnel: ${TUNNEL_NAME} (${tunnel_id})"
echo "Hostname: ${DRIVE_HOSTNAME}"
echo "Config: ${CONFIG_OUTPUT}"
echo "Env: ${ENV_OUTPUT}"

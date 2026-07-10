#!/usr/bin/env bash
set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-v2x-drive}"
DRIVE_HOSTNAME="${DRIVE_HOSTNAME:-drive.path2v2x.net}"
ORIGIN_SERVICE="${ORIGIN_SERVICE:-http://localhost:8765}"
CONFIG_OUTPUT="${CONFIG_OUTPUT:-/etc/cloudflared/v2x-drive.yml}"
ENV_OUTPUT="${ENV_OUTPUT:-/etc/v2x-drive-tunnel.env}"
PLAN_ONLY="${PLAN_ONLY:-false}"
OVERWRITE_DNS="${OVERWRITE_DNS:-false}"

for boolean_name in PLAN_ONLY OVERWRITE_DNS; do
  boolean_value="${!boolean_name}"
  if [[ "${boolean_value}" != "true" && "${boolean_value}" != "false" ]]; then
    echo "${boolean_name} must be true or false (got: ${boolean_value})" >&2
    exit 2
  fi
done

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need cloudflared
need jq

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
tunnels_json="${WORKDIR}/tunnels.json"
list_error="${WORKDIR}/list.err"

if ! cloudflared tunnel list --output json >"${tunnels_json}" 2>"${list_error}"; then
  cat >&2 <<EOF
cloudflared is not authenticated for named tunnel management.

The deployment credential prerequisite is an authenticated cloudflared named-tunnel session.

Original error:
$(cat "${list_error}")
EOF
  exit 2
fi

tunnel_id="$(jq -r --arg name "${TUNNEL_NAME}" '.[] | select(.name == $name) | .id' "${tunnels_json}" | head -n 1)"

if [[ -z "${tunnel_id}" || "${tunnel_id}" == "null" ]]; then
  if [[ "${PLAN_ONLY}" == "true" ]]; then
    echo "Named Drive tunnel reconciliation plan (read-only):"
    echo "  CREATE tunnel ${TUNNEL_NAME}"
    echo "  ENSURE DNS ${DRIVE_HOSTNAME} -> <new-tunnel-id>.cfargotunnel.com"
    echo "  VALIDATE and install ${CONFIG_OUTPUT}"
    echo "  INSTALL ${ENV_OUTPUT} with DRIVE_TUNNEL_MODE=named-config"
    exit 0
  fi
  echo "Creating Cloudflare Tunnel: ${TUNNEL_NAME}"
  cloudflared tunnel create "${TUNNEL_NAME}"
  cloudflared tunnel list --output json >"${tunnels_json}"
  tunnel_id="$(jq -r --arg name "${TUNNEL_NAME}" '.[] | select(.name == $name) | .id' "${tunnels_json}" | head -n 1)"
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

tmp_config="${WORKDIR}/v2x-drive.yml"
cat >"${tmp_config}" <<EOF
tunnel: ${tunnel_id}
credentials-file: ${credentials_file}

ingress:
  - hostname: ${DRIVE_HOSTNAME}
    service: ${ORIGIN_SERVICE}
  - service: http_status:404
EOF
cloudflared tunnel --config "${tmp_config}" ingress validate >/dev/null

tmp_env="${WORKDIR}/v2x-drive-tunnel.env"
cat >"${tmp_env}" <<EOF
DRIVE_TUNNEL_MODE=named-config
PUBLIC_HOSTNAME=${DRIVE_HOSTNAME}
ORIGIN_SERVICE=${ORIGIN_SERVICE}
TUNNEL_NAME=${TUNNEL_NAME}
CONFIG_FILE=${CONFIG_OUTPUT}
EOF

desired_cname="${tunnel_id}.cfargotunnel.com."
current_cname=""
if command -v dig >/dev/null 2>&1; then
  current_cname="$(dig +short CNAME "${DRIVE_HOSTNAME}" | head -n 1)"
fi

echo "Named Drive tunnel reconciliation plan:"
echo "  tunnel=${TUNNEL_NAME} (${tunnel_id})"
echo "  dns=${DRIVE_HOSTNAME} -> ${desired_cname}"
echo "  config=${CONFIG_OUTPUT}"
echo "  environment=${ENV_OUTPUT}"
if [[ "${PLAN_ONLY}" == "true" ]]; then
  echo "  planOnly=true (no DNS or filesystem writes)"
  exit 0
fi

if [[ "${current_cname}" == "${desired_cname}" ]]; then
  echo "DNS route already matches ${desired_cname}; keeping it unchanged."
elif [[ -n "${current_cname}" && "${OVERWRITE_DNS}" != "true" ]]; then
  echo "${DRIVE_HOSTNAME} currently resolves as CNAME ${current_cname}; refusing to overwrite it." >&2
  echo "Set OVERWRITE_DNS=true only after confirming the production DNS cutover." >&2
  exit 5
else
  echo "Routing ${DRIVE_HOSTNAME} to tunnel ${TUNNEL_NAME} (${tunnel_id})"
  route_args=(tunnel route dns)
  if [[ "${OVERWRITE_DNS}" == "true" ]]; then
    route_args+=(--overwrite-dns)
  fi
  cloudflared "${route_args[@]}" "${TUNNEL_NAME}" "${DRIVE_HOSTNAME}"
fi

sudo install -d -m 0755 "$(dirname "${CONFIG_OUTPUT}")"
sudo install -d -m 0755 "$(dirname "${ENV_OUTPUT}")"
sudo install -m 0644 "${tmp_config}" "${CONFIG_OUTPUT}"
sudo install -m 0600 "${tmp_env}" "${ENV_OUTPUT}"

echo "Named Drive tunnel provisioned."
echo "Tunnel: ${TUNNEL_NAME} (${tunnel_id})"
echo "Hostname: ${DRIVE_HOSTNAME}"
echo "Config: ${CONFIG_OUTPUT}"
echo "Env: ${ENV_OUTPUT}"

#!/usr/bin/env bash
set -euo pipefail

# Runs one V2X Cloudflare Tunnel in an explicit mode. Drive and perception
# systemd units provide distinct origin, log, hostname, config, and environment
# paths while sharing this credential-safe launcher.

CONFIG_FILE="${CONFIG_FILE:-/etc/cloudflared/v2x-drive.yml}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-drive.path2v2x.net}"
ORIGIN_SERVICE="${ORIGIN_SERVICE:-http://localhost:8765}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-/usr/local/bin/cloudflared}"
DRIVE_TUNNEL_MODE="${DRIVE_TUNNEL_MODE:-quick}"
TUNNEL_LABEL="${TUNNEL_LABEL:-Drive}"
LOG_FILE="${LOG_FILE:-/tmp/v2x-cloudflared.log}"

# Environment files are loaded by systemd before it drops privileges to the
# `path` service user.  Do not source them again here: named-tunnel environment
# files are deliberately root-owned, and treating them as shell programs both
# breaks that permission boundary and expands their parsing semantics.  Manual
# invocations must export the same variables explicitly.

if [[ ! -x "${CLOUDFLARED_BIN}" ]]; then
  echo "cloudflared is not executable at ${CLOUDFLARED_BIN}" >&2
  exit 127
fi

case "${DRIVE_TUNNEL_MODE}" in
  quick)
    echo "WARNING: starting ${TUNNEL_LABEL} Quick Tunnel for ${ORIGIN_SERVICE}; its public hostname is process-scoped."
    exec "${CLOUDFLARED_BIN}" tunnel \
      --url "${ORIGIN_SERVICE}" \
      --logfile "${LOG_FILE}" \
      --loglevel info
    ;;
  named-token)
    if [[ -z "${TUNNEL_TOKEN_FILE:-}" ]]; then
      echo "DRIVE_TUNNEL_MODE=named-token requires TUNNEL_TOKEN_FILE in the service environment." >&2
      exit 78
    fi
    if [[ ! -r "${TUNNEL_TOKEN_FILE}" ]]; then
      echo "Named tunnel token file is not readable: ${TUNNEL_TOKEN_FILE}" >&2
      exit 78
    fi
    echo "Starting token-managed ${TUNNEL_LABEL} tunnel for ${PUBLIC_HOSTNAME}."
    exec "${CLOUDFLARED_BIN}" tunnel \
      --no-autoupdate \
      --logfile "${LOG_FILE}" \
      --loglevel info \
      run --token-file "${TUNNEL_TOKEN_FILE}"
    ;;
  named-config)
    tunnel_ref="${TUNNEL_NAME:-${TUNNEL_ID:-}}"
    if [[ -z "${tunnel_ref}" ]]; then
      echo "DRIVE_TUNNEL_MODE=named-config requires TUNNEL_NAME or TUNNEL_ID in the service environment." >&2
      exit 78
    fi
    if [[ ! -r "${CONFIG_FILE}" ]]; then
      echo "Named tunnel config is not readable: ${CONFIG_FILE}" >&2
      exit 78
    fi
    echo "Starting named ${TUNNEL_LABEL} tunnel ${tunnel_ref} for ${PUBLIC_HOSTNAME} using ${CONFIG_FILE}."
    exec "${CLOUDFLARED_BIN}" tunnel \
      --no-autoupdate \
      --config "${CONFIG_FILE}" \
      --logfile "${LOG_FILE}" \
      --loglevel info \
      run "${tunnel_ref}"
    ;;
  *)
    echo "Unsupported DRIVE_TUNNEL_MODE=${DRIVE_TUNNEL_MODE}; use quick, named-token, or named-config." >&2
    exit 64
    ;;
esac

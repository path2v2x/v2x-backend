#!/usr/bin/env bash
set -euo pipefail

AMPLIFY_REGION="${AMPLIFY_REGION:-us-west-2}"
AMPLIFY_APP_ID="${AMPLIFY_APP_ID:-d1ugco1rmb7yjj}"
AMPLIFY_BRANCH="${AMPLIFY_BRANCH:-main}"
LOG_FILE="${LOG_FILE:-/tmp/v2x-cloudflared.log}"
TAILSCALE_DRIVE_WS_URL="${TAILSCALE_DRIVE_WS_URL:-wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net}"
WAIT_FOR_DEPLOY="${WAIT_FOR_DEPLOY:-true}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need aws
need jq

extract_latest_quick_tunnel_url() {
  if [[ ! -f "${LOG_FILE}" ]]; then
    return 1
  fi

  grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "${LOG_FILE}" | tail -n 1
}

normalize_ws_url() {
  local url="$1"
  url="${url/#https:/wss:}"
  url="${url/#http:/ws:}"
  printf '%s\n' "${url%/}"
}

DRIVE_WS_URL="${DRIVE_WS_URL:-}"
if [[ -z "${DRIVE_WS_URL}" ]]; then
  tunnel_url="$(extract_latest_quick_tunnel_url || true)"
  if [[ -z "${tunnel_url}" ]]; then
    echo "Could not find a trycloudflare.com URL in ${LOG_FILE}; set DRIVE_WS_URL explicitly." >&2
    exit 2
  fi
  DRIVE_WS_URL="$(normalize_ws_url "${tunnel_url}")"
else
  DRIVE_WS_URL="$(normalize_ws_url "${DRIVE_WS_URL}")"
fi

echo "Updating Amplify branch env for ${AMPLIFY_APP_ID}/${AMPLIFY_BRANCH}:"
echo "  VITE_CLOUDFLARE_DRIVE_WS_URL=${DRIVE_WS_URL}"

env_file="$(mktemp)"
trap 'rm -f "${env_file}"' EXIT

AWS_REGION="${AMPLIFY_REGION}" aws amplify get-branch \
  --app-id "${AMPLIFY_APP_ID}" \
  --branch-name "${AMPLIFY_BRANCH}" \
  --query 'branch.environmentVariables' \
  --output json \
  | jq \
      --arg drive_ws_url "${DRIVE_WS_URL}" \
      --arg tailscale_drive_ws_url "${TAILSCALE_DRIVE_WS_URL}" \
      '. + {
        VITE_CLOUDFLARE_DRIVE_WS_URL: $drive_ws_url,
        VITE_TAILSCALE_DRIVE_WS_URL: $tailscale_drive_ws_url
      }' > "${env_file}"

AWS_REGION="${AMPLIFY_REGION}" aws amplify update-branch \
  --app-id "${AMPLIFY_APP_ID}" \
  --branch-name "${AMPLIFY_BRANCH}" \
  --environment-variables "file://${env_file}" >/dev/null

job_id="$(AWS_REGION="${AMPLIFY_REGION}" aws amplify start-job \
  --app-id "${AMPLIFY_APP_ID}" \
  --branch-name "${AMPLIFY_BRANCH}" \
  --job-type RELEASE \
  --query 'jobSummary.jobId' \
  --output text)"

echo "Started Amplify release job: ${job_id}"

if [[ "${WAIT_FOR_DEPLOY}" != "true" ]]; then
  exit 0
fi

for _ in $(seq 1 90); do
  status="$(AWS_REGION="${AMPLIFY_REGION}" aws amplify get-job \
    --app-id "${AMPLIFY_APP_ID}" \
    --branch-name "${AMPLIFY_BRANCH}" \
    --job-id "${job_id}" \
    --query 'job.summary.status' \
    --output text 2>/dev/null || true)"
  echo "Amplify job ${job_id}: ${status}"
  case "${status}" in
    SUCCEED)
      echo "Published Drive frontend config: ${DRIVE_WS_URL}"
      exit 0
      ;;
    FAILED|CANCELLED)
      AWS_REGION="${AMPLIFY_REGION}" aws amplify get-job \
        --app-id "${AMPLIFY_APP_ID}" \
        --branch-name "${AMPLIFY_BRANCH}" \
        --job-id "${job_id}" \
        --output json >&2 || true
      exit 3
      ;;
  esac
  sleep 10
done

echo "Timed out waiting for Amplify release job ${job_id}" >&2
exit 124

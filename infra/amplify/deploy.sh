#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need aws
need jq
need curl
need zip
need npm

AWS_REGION="${AWS_REGION:-us-west-2}"
APP_NAME="${APP_NAME:-v2x-backend}"
BRANCH_NAME="${BRANCH_NAME:-main}"
API_BASE_URL="${API_BASE_URL:-}"
STATE_BASE_URL="${STATE_BASE_URL:-}"
STATE_BUCKET="${STATE_BUCKET:-}"
STATE_PATH="${STATE_PATH:-/state}"
MAP_DATA_PATH="${MAP_DATA_PATH:-/map-data}"
VIDEO_CAMERA_IDS="${VIDEO_CAMERA_IDS:-[\"ch1\",\"ch2\",\"ch3\",\"ch4\"]}"
DEMO_VIDEOS_PATH="${DEMO_VIDEOS_PATH:-/demo-videos}"
CLOUDFLARE_DRIVE_WS_URL="${CLOUDFLARE_DRIVE_WS_URL:-${VITE_CLOUDFLARE_DRIVE_WS_URL:-${VITE_DRIVE_WS_URL:-}}}"
TAILSCALE_DRIVE_WS_URL="${TAILSCALE_DRIVE_WS_URL:-${VITE_TAILSCALE_DRIVE_WS_URL:-wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net}}"

if [[ -z "${API_BASE_URL}" ]]; then
  echo "API_BASE_URL is required (from provision-read-api.sh output)." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_DIR="${ROOT}/apps/web"

if [[ -z "${STATE_BASE_URL}" ]]; then
  if [[ -n "${STATE_BUCKET}" ]]; then
    STATE_BASE_URL="https://${STATE_BUCKET}.s3.us-west-1.amazonaws.com"
  else
    STATE_BASE_URL="${API_BASE_URL}"
  fi
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

echo "Building dashboard..."
(cd "${SITE_DIR}" && npm ci && npm run build >/dev/null)

BUILD_DIR="${WORKDIR}/site"
cp -R "${SITE_DIR}/build" "${BUILD_DIR}"

cat > "${BUILD_DIR}/config.json" <<JSON
{
  "apiBaseUrl": "${API_BASE_URL}",
  "stateBaseUrl": "${STATE_BASE_URL}",
  "statePath": "${STATE_PATH}",
  "mapDataPath": "${MAP_DATA_PATH}",
  "demoVideosPath": "${DEMO_VIDEOS_PATH}",
  "videoCameraIds": ${VIDEO_CAMERA_IDS},
  "cloudflareDriveWsUrl": "${CLOUDFLARE_DRIVE_WS_URL}",
  "tailscaleDriveWsUrl": "${TAILSCALE_DRIVE_WS_URL}"
}
JSON

(cd "${BUILD_DIR}" && zip -qr "${WORKDIR}/site.zip" .)

echo "Ensuring Amplify app exists: ${APP_NAME} (${AWS_REGION})"
APP_ID="$(aws amplify list-apps --max-results 100 --query "apps[?name==\`${APP_NAME}\`].appId | [0]" --output text 2>/dev/null || true)"
if [[ -z "${APP_ID}" || "${APP_ID}" == "None" ]]; then
  APP_ID="$(aws amplify create-app --name "${APP_NAME}" --platform WEB --query 'app.appId' --output text)"
fi

echo "Ensuring branch exists: ${BRANCH_NAME}"
if ! aws amplify get-branch --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" >/dev/null 2>&1; then
  aws amplify create-branch --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --stage PRODUCTION >/dev/null
fi

echo "Stopping any in-progress jobs..."
aws amplify list-jobs --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --max-results 10 --output json 2>/dev/null \
  | jq -r '.jobSummaries[]? | select(.status=="PENDING" or .status=="RUNNING") | .jobId' \
  | while read -r job_id; do
      [[ -z "${job_id}" ]] && continue
      aws amplify stop-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${job_id}" >/dev/null 2>&1 || true
    done

echo "Creating deployment..."
DEPLOY_JSON="$(aws amplify create-deployment --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}")"
JOB_ID="$(echo "${DEPLOY_JSON}" | jq -r '.jobId')"
UPLOAD_URL="$(echo "${DEPLOY_JSON}" | jq -r '.zipUploadUrl')"

if [[ -z "${JOB_ID}" || "${JOB_ID}" == "null" || -z "${UPLOAD_URL}" || "${UPLOAD_URL}" == "null" ]]; then
  echo "Unexpected create-deployment response:" >&2
  echo "${DEPLOY_JSON}" | jq . >&2 || true
  exit 1
fi

echo "Uploading artifact..."
curl -fsS -T "${WORKDIR}/site.zip" "${UPLOAD_URL}" >/dev/null

echo "Starting deployment (jobId=${JOB_ID})..."
aws amplify start-deployment --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" >/dev/null

echo "Waiting for deployment to finish..."
for _ in $(seq 1 60); do
  STATUS="$(aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --query 'job.summary.status' --output text 2>/dev/null || true)"
  if [[ "${STATUS}" == "SUCCEED" ]]; then
    break
  fi
  if [[ "${STATUS}" == "FAILED" || "${STATUS}" == "CANCELLED" ]]; then
    aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --output json | jq . >&2 || true
    exit 1
  fi
  sleep 5
done

DEFAULT_DOMAIN="$(aws amplify get-app --app-id "${APP_ID}" --query 'app.defaultDomain' --output text)"
echo "Done."
echo "AppId: ${APP_ID}"
echo "URL: https://${BRANCH_NAME}.${DEFAULT_DOMAIN}/"

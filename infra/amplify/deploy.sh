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
DETECTIONS_API_BASE_URL="${DETECTIONS_API_BASE_URL:-${API_BASE_URL}}"
STATE_BASE_URL="${STATE_BASE_URL:-}"
STATE_BUCKET="${STATE_BUCKET:-}"
STATE_PATH="${STATE_PATH:-/state}"
MAP_DATA_PATH="${MAP_DATA_PATH:-/map-data}"
DRIVE_CONFIG_PATH="${DRIVE_CONFIG_PATH:-/drive-config}"
VIDEO_CAMERA_IDS="${VIDEO_CAMERA_IDS:-[\"ch1\",\"ch2\",\"ch3\",\"ch4\"]}"
PERCEPTION_STREAM_URLS="${PERCEPTION_STREAM_URLS:-{}}"
PERCEPTION_STREAM_BASE_URL="${PERCEPTION_STREAM_BASE_URL:-}"
PERCEPTION_STREAM_PATH_TEMPLATE="${PERCEPTION_STREAM_PATH_TEMPLATE:-}"
if [[ -z "${PERCEPTION_STREAM_PATH_TEMPLATE}" ]]; then
  PERCEPTION_STREAM_PATH_TEMPLATE='/streams/{camera_id}.mjpg'
fi
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
  "detectionsApiBaseUrl": "${DETECTIONS_API_BASE_URL}",
  "detectionRoutes": {
    "recent": "/detections/recent",
    "byObject": "/detections/object/{object_id}",
    "byGeohash": "/detections/geohash/{geohash}"
  },
  "stateBaseUrl": "${STATE_BASE_URL}",
  "statePath": "${STATE_PATH}",
  "mapDataPath": "${MAP_DATA_PATH}",
  "driveConfigPath": "${DRIVE_CONFIG_PATH}",
  "demoVideosPath": "${DEMO_VIDEOS_PATH}",
  "videoCameraIds": ${VIDEO_CAMERA_IDS},
  "perceptionStreamUrls": ${PERCEPTION_STREAM_URLS},
  "perceptionStreamBaseUrl": "${PERCEPTION_STREAM_BASE_URL}",
  "perceptionStreamPathTemplate": "${PERCEPTION_STREAM_PATH_TEMPLATE}",
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

ENV_JSON="${WORKDIR}/environment.json"
jq -n \
  --arg API_BASE_URL "${API_BASE_URL}" \
  --arg DETECTIONS_API_BASE_URL "${DETECTIONS_API_BASE_URL}" \
  --arg STATE_BASE_URL "${STATE_BASE_URL}" \
  --arg STATE_PATH "${STATE_PATH}" \
  --arg MAP_DATA_PATH "${MAP_DATA_PATH}" \
  --arg DRIVE_CONFIG_PATH "${DRIVE_CONFIG_PATH}" \
  --arg DEMO_VIDEOS_PATH "${DEMO_VIDEOS_PATH}" \
  --arg VIDEO_CAMERA_IDS "${VIDEO_CAMERA_IDS}" \
  --arg PERCEPTION_STREAM_URLS "${PERCEPTION_STREAM_URLS}" \
  --arg PERCEPTION_STREAM_BASE_URL "${PERCEPTION_STREAM_BASE_URL}" \
  --arg PERCEPTION_STREAM_PATH_TEMPLATE "${PERCEPTION_STREAM_PATH_TEMPLATE}" \
  --arg VITE_CLOUDFLARE_DRIVE_WS_URL "${CLOUDFLARE_DRIVE_WS_URL}" \
  --arg VITE_TAILSCALE_DRIVE_WS_URL "${TAILSCALE_DRIVE_WS_URL}" \
  '{
    API_BASE_URL: $API_BASE_URL,
    DETECTIONS_API_BASE_URL: $DETECTIONS_API_BASE_URL,
    STATE_BASE_URL: $STATE_BASE_URL,
    STATE_PATH: $STATE_PATH,
    MAP_DATA_PATH: $MAP_DATA_PATH,
    DRIVE_CONFIG_PATH: $DRIVE_CONFIG_PATH,
    DEMO_VIDEOS_PATH: $DEMO_VIDEOS_PATH,
    VIDEO_CAMERA_IDS: $VIDEO_CAMERA_IDS,
    PERCEPTION_STREAM_URLS: $PERCEPTION_STREAM_URLS,
    PERCEPTION_STREAM_BASE_URL: $PERCEPTION_STREAM_BASE_URL,
    PERCEPTION_STREAM_PATH_TEMPLATE: $PERCEPTION_STREAM_PATH_TEMPLATE,
    VITE_CLOUDFLARE_DRIVE_WS_URL: $VITE_CLOUDFLARE_DRIVE_WS_URL,
    VITE_TAILSCALE_DRIVE_WS_URL: $VITE_TAILSCALE_DRIVE_WS_URL
  }' > "${ENV_JSON}"

APP_REPOSITORY="$(aws amplify get-app --app-id "${APP_ID}" --query 'app.repository' --output text 2>/dev/null || true)"
if [[ -n "${APP_REPOSITORY}" && "${APP_REPOSITORY}" != "None" ]]; then
  echo "Connected repository detected; updating branch environment and app build spec."
  aws amplify update-branch \
    --app-id "${APP_ID}" \
    --branch-name "${BRANCH_NAME}" \
    --environment-variables "file://${ENV_JSON}" >/dev/null

  BUILD_SPEC_FILE="${ROOT}/infra/amplify/buildspec.yml"
  if [[ -f "${BUILD_SPEC_FILE}" ]]; then
    aws amplify update-app \
      --app-id "${APP_ID}" \
      --build-spec "file://${BUILD_SPEC_FILE}" >/dev/null
  fi

  echo "Starting connected-repo release..."
  JOB_ID="$(aws amplify start-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-type RELEASE --query 'jobSummary.jobId' --output text)"

  echo "Waiting for deployment to finish..."
  for _ in $(seq 1 90); do
    STATUS="$(aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --query 'job.summary.status' --output text 2>/dev/null || true)"
    if [[ "${STATUS}" == "SUCCEED" ]]; then
      break
    fi
    if [[ "${STATUS}" == "FAILED" || "${STATUS}" == "CANCELLED" ]]; then
      aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --output json | jq . >&2 || true
      exit 1
    fi
    sleep 10
  done

  DEFAULT_DOMAIN="$(aws amplify get-app --app-id "${APP_ID}" --query 'app.defaultDomain' --output text)"
  echo "Done."
  echo "AppId: ${APP_ID}"
  echo "JobId: ${JOB_ID}"
  echo "URL: https://${BRANCH_NAME}.${DEFAULT_DOMAIN}/"
  exit 0
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

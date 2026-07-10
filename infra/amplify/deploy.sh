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
need sha256sum

AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_REGION
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
PERCEPTION_STREAM_URLS_EXPLICIT="${PERCEPTION_STREAM_URLS+x}"
PERCEPTION_STREAM_BASE_URL_EXPLICIT="${PERCEPTION_STREAM_BASE_URL+x}"
CLOUDFLARE_DRIVE_WS_URL_EXPLICIT="${CLOUDFLARE_DRIVE_WS_URL+x}${VITE_CLOUDFLARE_DRIVE_WS_URL+x}${VITE_DRIVE_WS_URL+x}"
PERCEPTION_STREAM_URLS="${PERCEPTION_STREAM_URLS:-{}}"
PERCEPTION_STREAM_BASE_URL="${PERCEPTION_STREAM_BASE_URL:-}"
PERCEPTION_STREAM_PATH_TEMPLATE="${PERCEPTION_STREAM_PATH_TEMPLATE:-}"
if [[ -z "${PERCEPTION_STREAM_PATH_TEMPLATE}" ]]; then
  PERCEPTION_STREAM_PATH_TEMPLATE='/streams/{camera_id}.mjpg'
fi
DEMO_VIDEOS_PATH="${DEMO_VIDEOS_PATH:-/demo-videos}"
CLOUDFLARE_DRIVE_WS_URL="${CLOUDFLARE_DRIVE_WS_URL:-${VITE_CLOUDFLARE_DRIVE_WS_URL:-${VITE_DRIVE_WS_URL:-}}}"
TAILSCALE_DRIVE_WS_URL="${TAILSCALE_DRIVE_WS_URL:-${VITE_TAILSCALE_DRIVE_WS_URL:-wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net}}"
STOP_IN_PROGRESS_JOBS="${STOP_IN_PROGRESS_JOBS:-false}"
RECOVERY_CONNECTED_DEPLOY_GATE="${RECOVERY_CONNECTED_DEPLOY_GATE:-}"
EXPECTED_CANONICAL_REPOSITORY="${EXPECTED_CANONICAL_REPOSITORY:-https://github.com/path2v2x/v2x-backend}"
EXPECTED_APP_METADATA_HASH="${EXPECTED_APP_METADATA_HASH:-}"
EXPECTED_BRANCH_ENV_HASH="${EXPECTED_BRANCH_ENV_HASH:-}"
RECOVERY_BACKUP_DIR="${RECOVERY_BACKUP_DIR:-/home/path/V2XCarla/v2x-backend-backups/amplify-deploy}"

if [[ "${STOP_IN_PROGRESS_JOBS}" != "true" && "${STOP_IN_PROGRESS_JOBS}" != "false" ]]; then
  echo "STOP_IN_PROGRESS_JOBS must be true or false" >&2
  exit 2
fi

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

if ! jq -e 'type == "array" and all(.[]; type == "string")' <<<"${VIDEO_CAMERA_IDS}" >/dev/null; then
  echo "VIDEO_CAMERA_IDS must be a JSON array of strings" >&2
  exit 2
fi
if ! jq -e 'type == "object" and all(to_entries[]; .value | type == "string")' \
  <<<"${PERCEPTION_STREAM_URLS}" >/dev/null; then
  echo "PERCEPTION_STREAM_URLS must be a JSON object with string values" >&2
  exit 2
fi

jq -n \
  --arg apiBaseUrl "${API_BASE_URL}" \
  --arg detectionsApiBaseUrl "${DETECTIONS_API_BASE_URL}" \
  --arg stateBaseUrl "${STATE_BASE_URL}" \
  --arg statePath "${STATE_PATH}" \
  --arg mapDataPath "${MAP_DATA_PATH}" \
  --arg driveConfigPath "${DRIVE_CONFIG_PATH}" \
  --arg demoVideosPath "${DEMO_VIDEOS_PATH}" \
  --argjson videoCameraIds "${VIDEO_CAMERA_IDS}" \
  --argjson perceptionStreamUrls "${PERCEPTION_STREAM_URLS}" \
  --arg perceptionStreamBaseUrl "${PERCEPTION_STREAM_BASE_URL}" \
  --arg perceptionStreamPathTemplate "${PERCEPTION_STREAM_PATH_TEMPLATE}" \
  --arg cloudflareDriveWsUrl "${CLOUDFLARE_DRIVE_WS_URL}" \
  --arg tailscaleDriveWsUrl "${TAILSCALE_DRIVE_WS_URL}" \
  '{
    $apiBaseUrl,
    $detectionsApiBaseUrl,
    detectionRoutes: {
      recent: "/detections/recent",
      byObject: "/detections/object/{object_id}",
      byGeohash: "/detections/geohash/{geohash}"
    },
    $stateBaseUrl,
    $statePath,
    $mapDataPath,
    $driveConfigPath,
    $demoVideosPath,
    $videoCameraIds,
    $perceptionStreamUrls,
    $perceptionStreamBaseUrl,
    $perceptionStreamPathTemplate,
    $cloudflareDriveWsUrl,
    $tailscaleDriveWsUrl
  }' >"${BUILD_DIR}/config.json"

(cd "${BUILD_DIR}" && zip -qr "${WORKDIR}/site.zip" .)

echo "Ensuring Amplify app exists: ${APP_NAME} (${AWS_REGION})"
APP_ID="$(aws amplify list-apps --max-results 100 --query "apps[?name==\`${APP_NAME}\`].appId | [0]" --output text)"
if [[ -z "${APP_ID}" || "${APP_ID}" == "None" ]]; then
  APP_ID="$(aws amplify create-app --name "${APP_NAME}" --platform WEB --query 'app.appId' --output text)"
fi

echo "Ensuring branch exists: ${BRANCH_NAME}"
BRANCH_ERROR="${WORKDIR}/get-branch.err"
if ! aws amplify get-branch --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" >/dev/null 2>"${BRANCH_ERROR}"; then
  if grep -q 'NotFoundException' "${BRANCH_ERROR}"; then
    aws amplify create-branch --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --stage PRODUCTION >/dev/null
  else
    cat "${BRANCH_ERROR}" >&2
    exit 1
  fi
fi

ENV_PATCH_JSON="${WORKDIR}/environment-patch.json"
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
  --arg PERCEPTION_STREAM_URLS_EXPLICIT "${PERCEPTION_STREAM_URLS_EXPLICIT}" \
  --arg PERCEPTION_STREAM_BASE_URL_EXPLICIT "${PERCEPTION_STREAM_BASE_URL_EXPLICIT}" \
  --arg CLOUDFLARE_DRIVE_WS_URL_EXPLICIT "${CLOUDFLARE_DRIVE_WS_URL_EXPLICIT}" \
  '{
    API_BASE_URL: $API_BASE_URL,
    DETECTIONS_API_BASE_URL: $DETECTIONS_API_BASE_URL,
    STATE_BASE_URL: $STATE_BASE_URL,
    STATE_PATH: $STATE_PATH,
    MAP_DATA_PATH: $MAP_DATA_PATH,
    DRIVE_CONFIG_PATH: $DRIVE_CONFIG_PATH,
    DEMO_VIDEOS_PATH: $DEMO_VIDEOS_PATH,
    VIDEO_CAMERA_IDS: $VIDEO_CAMERA_IDS,
    PERCEPTION_STREAM_PATH_TEMPLATE: $PERCEPTION_STREAM_PATH_TEMPLATE,
    VITE_TAILSCALE_DRIVE_WS_URL: $VITE_TAILSCALE_DRIVE_WS_URL
  }
  + (if $PERCEPTION_STREAM_URLS_EXPLICIT != "" then
      {PERCEPTION_STREAM_URLS: $PERCEPTION_STREAM_URLS}
    else {} end)
  + (if $PERCEPTION_STREAM_BASE_URL_EXPLICIT != "" then
      {PERCEPTION_STREAM_BASE_URL: $PERCEPTION_STREAM_BASE_URL}
    else {} end)
  + (if $CLOUDFLARE_DRIVE_WS_URL_EXPLICIT != "" then
      {VITE_CLOUDFLARE_DRIVE_WS_URL: $VITE_CLOUDFLARE_DRIVE_WS_URL}
    else {} end)' > "${ENV_PATCH_JSON}"

CURRENT_ENV_JSON="${WORKDIR}/environment-current.json"
ENV_JSON="${WORKDIR}/environment.json"
CURRENT_BRANCH_JSON="${WORKDIR}/branch-current.json"
aws amplify get-branch \
  --app-id "${APP_ID}" \
  --branch-name "${BRANCH_NAME}" \
  --output json >"${CURRENT_BRANCH_JSON}"
jq '.branch.environmentVariables // {}' "${CURRENT_BRANCH_JSON}" >"${CURRENT_ENV_JSON}"
jq -s '.[0] + .[1]' "${CURRENT_ENV_JSON}" "${ENV_PATCH_JSON}" >"${ENV_JSON}"

CURRENT_APP_JSON="${WORKDIR}/app-current.json"
aws amplify get-app --app-id "${APP_ID}" --output json >"${CURRENT_APP_JSON}"
APP_REPOSITORY="$(jq -r '.app.repository // ""' "${CURRENT_APP_JSON}")"
if [[ -n "${APP_REPOSITORY}" && "${APP_REPOSITORY}" != "None" ]]; then
  app_metadata_hash="$(jq -Sc '.app' "${CURRENT_APP_JSON}" | sha256sum | awk '{print $1}')"
  branch_env_hash="$(jq -Sc . "${CURRENT_ENV_JSON}" | sha256sum | awk '{print $1}')"
  if [[ "${RECOVERY_CONNECTED_DEPLOY_GATE}" != "canonical-reviewed-release" ]]; then
    echo "Connected-repository recovery deploy is disabled by default." >&2
    echo "Use reconcile-repository.sh and publish-amplify-runtime-config.sh instead." >&2
    echo "A reviewed exception requires RECOVERY_CONNECTED_DEPLOY_GATE=canonical-reviewed-release." >&2
    exit 8
  fi
  if [[ "${APP_REPOSITORY}" != "${EXPECTED_CANONICAL_REPOSITORY}" ]]; then
    echo "Connected repository is ${APP_REPOSITORY}; expected canonical ${EXPECTED_CANONICAL_REPOSITORY}." >&2
    exit 8
  fi
  if [[ -z "${EXPECTED_APP_METADATA_HASH}" || -z "${EXPECTED_BRANCH_ENV_HASH}" ]]; then
    echo "Connected recovery deploy requires EXPECTED_APP_METADATA_HASH and EXPECTED_BRANCH_ENV_HASH." >&2
    echo "Observed appMetadataHash=${app_metadata_hash}" >&2
    echo "Observed branchEnvironmentHash=${branch_env_hash}" >&2
    exit 8
  fi
  if [[ "${EXPECTED_APP_METADATA_HASH}" != "${app_metadata_hash}" || \
        "${EXPECTED_BRANCH_ENV_HASH}" != "${branch_env_hash}" ]]; then
    echo "Amplify state changed; refusing the connected recovery deploy." >&2
    echo "Observed appMetadataHash=${app_metadata_hash}" >&2
    echo "Observed branchEnvironmentHash=${branch_env_hash}" >&2
    exit 8
  fi

  install -d -m 0700 "${RECOVERY_BACKUP_DIR}"
  recovery_backup="${RECOVERY_BACKUP_DIR%/}/${APP_ID}-${BRANCH_NAME}-$(date -u +%Y%m%dT%H%M%SZ)-${app_metadata_hash:0:12}"
  install -d -m 0700 "${recovery_backup}"
  install -m 0600 "${CURRENT_APP_JSON}" "${recovery_backup}/app.json"
  install -m 0600 "${CURRENT_BRANCH_JSON}" "${recovery_backup}/branch.json"
  install -m 0600 "${ROOT}/infra/amplify/buildspec.yml" "${recovery_backup}/replacement-buildspec.yml"
  printf '%s\n' \
    "appMetadataHash=${app_metadata_hash}" \
    "branchEnvironmentHash=${branch_env_hash}" \
    >"${recovery_backup}/expected-state.txt"
  chmod 0600 "${recovery_backup}/expected-state.txt"
  echo "Saved connected-deploy rollback evidence: ${recovery_backup}"

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
  DEPLOY_SUCCEEDED=false
  for _ in $(seq 1 90); do
    STATUS="$(aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --query 'job.summary.status' --output text 2>/dev/null || true)"
    if [[ "${STATUS}" == "SUCCEED" ]]; then
      DEPLOY_SUCCEEDED=true
      break
    fi
    if [[ "${STATUS}" == "FAILED" || "${STATUS}" == "CANCELLED" ]]; then
      aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --output json | jq . >&2 || true
      exit 1
    fi
    sleep 10
  done
  if [[ "${DEPLOY_SUCCEEDED}" != "true" ]]; then
    echo "Timed out waiting for connected-repository Amplify job ${JOB_ID}; last status: ${STATUS:-unknown}" >&2
    exit 124
  fi

  DEFAULT_DOMAIN="$(aws amplify get-app --app-id "${APP_ID}" --query 'app.defaultDomain' --output text)"
  echo "Done."
  echo "AppId: ${APP_ID}"
  echo "JobId: ${JOB_ID}"
  echo "URL: https://${BRANCH_NAME}.${DEFAULT_DOMAIN}/"
  exit 0
fi

ACTIVE_JOBS_FILE="${WORKDIR}/active-jobs.txt"
aws amplify list-jobs --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --max-results 10 --output json \
  | jq -r '.jobSummaries[]? | select(.status=="PENDING" or .status=="RUNNING") | .jobId' \
  >"${ACTIVE_JOBS_FILE}"
if [[ -s "${ACTIVE_JOBS_FILE}" && "${STOP_IN_PROGRESS_JOBS}" != "true" ]]; then
  echo "An Amplify job is already pending/running; refusing to cancel it implicitly." >&2
  echo "Set STOP_IN_PROGRESS_JOBS=true only after confirming that cancellation is safe." >&2
  exit 6
fi
if [[ -s "${ACTIVE_JOBS_FILE}" ]]; then
  echo "Stopping explicitly approved in-progress jobs..."
  while read -r job_id; do
    [[ -z "${job_id}" ]] && continue
    aws amplify stop-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${job_id}" >/dev/null
  done <"${ACTIVE_JOBS_FILE}"
fi

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
DEPLOY_SUCCEEDED=false
for _ in $(seq 1 60); do
  STATUS="$(aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --query 'job.summary.status' --output text 2>/dev/null || true)"
  if [[ "${STATUS}" == "SUCCEED" ]]; then
    DEPLOY_SUCCEEDED=true
    break
  fi
  if [[ "${STATUS}" == "FAILED" || "${STATUS}" == "CANCELLED" ]]; then
    aws amplify get-job --app-id "${APP_ID}" --branch-name "${BRANCH_NAME}" --job-id "${JOB_ID}" --output json | jq . >&2 || true
    exit 1
  fi
  sleep 5
done
if [[ "${DEPLOY_SUCCEEDED}" != "true" ]]; then
  echo "Timed out waiting for manual Amplify deployment ${JOB_ID}; last status: ${STATUS:-unknown}" >&2
  exit 124
fi

DEFAULT_DOMAIN="$(aws amplify get-app --app-id "${APP_ID}" --query 'app.defaultDomain' --output text)"
echo "Done."
echo "AppId: ${APP_ID}"
echo "URL: https://${BRANCH_NAME}.${DEFAULT_DOMAIN}/"

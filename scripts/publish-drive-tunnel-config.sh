#!/usr/bin/env bash
set -euo pipefail

# Publishes the short-lived Drive WebSocket overlay consumed through
# GET /drive-config. Every replacement first preserves the current object under
# BACKUP_PREFIX. Rollbacks create a new, monotonically increasing config
# version instead of moving the public version backwards. The default action is
# a read-only plan; every mutating caller must opt into publish or rollback.

AWS_REGION="${AWS_REGION:-us-west-1}"
STATE_BUCKET="${STATE_BUCKET:-}"
CONFIG_KEY="${CONFIG_KEY:-api/drive-config.json}"
BACKUP_PREFIX="${BACKUP_PREFIX:-api/drive-config-backups}"
LOG_FILE="${LOG_FILE:-/tmp/v2x-cloudflared.log}"
TAILSCALE_DRIVE_WS_URL="${TAILSCALE_DRIVE_WS_URL:-wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net}"
TTL_SECONDS="${TTL_SECONDS:-7200}"
ACTION="${ACTION:-plan}"
DRY_RUN="${DRY_RUN:-false}"
EXPECTED_CURRENT_VERSION="${EXPECTED_CURRENT_VERSION:-}"
CONFIG_VERSION="${CONFIG_VERSION:-}"
ALLOW_UNVERSIONED_CURRENT="${ALLOW_UNVERSIONED_CURRENT:-false}"
ROLLBACK_BACKUP_KEY="${ROLLBACK_BACKUP_KEY:-}"
ROLLBACK_VERSION_ID="${ROLLBACK_VERSION_ID:-}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need aws
need jq

case "${ACTION}" in
  publish) ;;
  plan)
    DRY_RUN=true
    ;;
  rollback) ;;
  *)
    echo "ACTION must be publish, plan, or rollback (got: ${ACTION})" >&2
    exit 2
    ;;
esac

for boolean_name in DRY_RUN ALLOW_UNVERSIONED_CURRENT; do
  boolean_value="${!boolean_name}"
  if [[ "${boolean_value}" != "true" && "${boolean_value}" != "false" ]]; then
    echo "${boolean_name} must be true or false (got: ${boolean_value})" >&2
    exit 2
  fi
done

if [[ "${ACTION}" == "rollback" && -z "${EXPECTED_CURRENT_VERSION}" ]]; then
  echo "ACTION=rollback requires EXPECTED_CURRENT_VERSION for a version-gated rollback." >&2
  exit 2
fi

if ! [[ "${TTL_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "TTL_SECONDS must be a positive integer" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

aws_region() {
  aws --region "${AWS_REGION}" "$@"
}

if [[ -z "${STATE_BUCKET}" ]]; then
  account_id="$(aws_region sts get-caller-identity --query Account --output text)"
  STATE_BUCKET="v2x-backend-state-${account_id}-${AWS_REGION}"
fi

is_not_found_error() {
  grep -Eq '(^|[^0-9])(404|NoSuchKey|Not Found)([^0-9]|$)' "$1"
}

head_object() {
  local output_file="$1"
  local error_file="$2"
  if aws_region s3api head-object \
    --bucket "${STATE_BUCKET}" \
    --key "${CONFIG_KEY}" >"${output_file}" 2>"${error_file}"; then
    return 0
  fi
  if is_not_found_error "${error_file}"; then
    return 1
  fi
  cat "${error_file}" >&2
  return 2
}

current_exists=false
current_version=0
current_s3_version=""
current_etag=""
current_file="${WORKDIR}/current.json"
initial_head="${WORKDIR}/head.json"
head_error="${WORKDIR}/head.err"

if head_object "${initial_head}" "${head_error}"; then
  current_exists=true
  current_s3_version="$(jq -r '.VersionId // ""' "${initial_head}")"
  current_etag="$(jq -r '.ETag // ""' "${initial_head}")"
  aws_region s3api get-object \
    --bucket "${STATE_BUCKET}" \
    --key "${CONFIG_KEY}" \
    "${current_file}" >/dev/null

  if current_version="$(jq -er '.version | select(type == "number" and . >= 0 and floor == .)' "${current_file}")"; then
    :
  elif [[ "${ALLOW_UNVERSIONED_CURRENT}" == "true" ]]; then
    current_version=0
  else
    echo "Existing s3://${STATE_BUCKET}/${CONFIG_KEY} has no non-negative integer version." >&2
    echo "Set ALLOW_UNVERSIONED_CURRENT=true only for the one-time migration." >&2
    exit 3
  fi
else
  status=$?
  if [[ "${status}" -ne 1 ]]; then
    exit "${status}"
  fi
fi

if [[ -n "${EXPECTED_CURRENT_VERSION}" && "${EXPECTED_CURRENT_VERSION}" != "${current_version}" ]]; then
  echo "Current config version is ${current_version}; expected ${EXPECTED_CURRENT_VERSION}. Refusing to publish." >&2
  exit 4
fi

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

validate_ws_url() {
  local label="$1"
  local url="$2"
  if [[ ! "${url}" =~ ^wss?://[^[:space:]]+$ ]]; then
    echo "${label} must be a ws:// or wss:// URL (got: ${url})" >&2
    exit 5
  fi
}

rollback_of_version="null"
semantic_tombstone=false
if [[ "${ACTION}" == "rollback" ]]; then
  if [[ -n "${ROLLBACK_BACKUP_KEY}" && -n "${ROLLBACK_VERSION_ID}" ]]; then
    echo "Set only one of ROLLBACK_BACKUP_KEY or ROLLBACK_VERSION_ID." >&2
    exit 6
  fi
  if [[ -z "${ROLLBACK_BACKUP_KEY}" && -z "${ROLLBACK_VERSION_ID}" ]]; then
    echo "Rollback requires ROLLBACK_BACKUP_KEY or ROLLBACK_VERSION_ID." >&2
    exit 6
  fi

  rollback_file="${WORKDIR}/rollback.json"
  if [[ -n "${ROLLBACK_BACKUP_KEY}" ]]; then
    aws_region s3api get-object \
      --bucket "${STATE_BUCKET}" \
      --key "${ROLLBACK_BACKUP_KEY}" \
      "${rollback_file}" >/dev/null
  else
    aws_region s3api get-object \
      --bucket "${STATE_BUCKET}" \
      --key "${CONFIG_KEY}" \
      --version-id "${ROLLBACK_VERSION_ID}" \
      "${rollback_file}" >/dev/null
  fi

  if jq -e --arg key "${CONFIG_KEY}" '
      .kind == "drive-config-prior-absence"
      and .absent == true
      and .configKey == $key
      and .observedVersion == 0' \
      "${rollback_file}" >/dev/null; then
    if [[ "${current_exists}" != "true" ]]; then
      echo "Drive config is already physically absent; semantic absence rollback is unnecessary." >&2
      exit 6
    fi
    semantic_tombstone=true
    DRIVE_WS_URL="$(jq -er '.cloudflareDriveWsUrl | select(type == "string" and length > 0)' "${current_file}")"
    TAILSCALE_DRIVE_WS_URL="$(jq -er '.tailscaleDriveWsUrl | select(type == "string" and length > 0)' "${current_file}")"
    rollback_of_version=0
    SOURCE="${SOURCE:-rollback_prior_absence}"
  else
    DRIVE_WS_URL="$(jq -er '.cloudflareDriveWsUrl | select(type == "string" and length > 0)' "${rollback_file}")"
    TAILSCALE_DRIVE_WS_URL="$(jq -er '.tailscaleDriveWsUrl | select(type == "string" and length > 0)' "${rollback_file}")"
    rollback_of_version="$(jq -er '.version | select(type == "number" and . >= 0 and floor == .)' "${rollback_file}")"
    SOURCE="${SOURCE:-rollback}"
  fi
else
  DRIVE_WS_URL="${DRIVE_WS_URL:-}"
  if [[ -z "${DRIVE_WS_URL}" ]]; then
    tunnel_url="$(extract_latest_quick_tunnel_url || true)"
    if [[ -z "${tunnel_url}" ]]; then
      echo "Could not find a trycloudflare.com URL in ${LOG_FILE}; set DRIVE_WS_URL explicitly." >&2
      exit 7
    fi
    DRIVE_WS_URL="${tunnel_url}"
    SOURCE="${SOURCE:-quick_tunnel}"
  else
    SOURCE="${SOURCE:-configured}"
  fi
fi

DRIVE_WS_URL="$(normalize_ws_url "${DRIVE_WS_URL}")"
TAILSCALE_DRIVE_WS_URL="$(normalize_ws_url "${TAILSCALE_DRIVE_WS_URL}")"
validate_ws_url DRIVE_WS_URL "${DRIVE_WS_URL}"
validate_ws_url TAILSCALE_DRIVE_WS_URL "${TAILSCALE_DRIVE_WS_URL}"

next_version=$((current_version + 1))
if [[ -n "${CONFIG_VERSION}" ]]; then
  if ! [[ "${CONFIG_VERSION}" =~ ^[1-9][0-9]*$ ]] || [[ "${CONFIG_VERSION}" -ne "${next_version}" ]]; then
    echo "CONFIG_VERSION must equal the next monotonic version (${next_version})." >&2
    exit 8
  fi
  next_version="${CONFIG_VERSION}"
fi

updated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "${semantic_tombstone}" == "true" ]]; then
  expires_at="$(date -u -d '-1 second' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)"
else
  expires_at="$(date -u -d "+${TTL_SECONDS} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || python3 - "${TTL_SECONDS}" <<'PY'
from datetime import datetime, timedelta, timezone
import sys
print((datetime.now(timezone.utc) + timedelta(seconds=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)"
fi

candidate_file="${WORKDIR}/candidate.json"
jq -n \
  --argjson version "${next_version}" \
  --arg updatedAt "${updated_at}" \
  --arg expiresAt "${expires_at}" \
  --arg source "${SOURCE}" \
  --arg cloudflareDriveWsUrl "${DRIVE_WS_URL}" \
  --arg tailscaleDriveWsUrl "${TAILSCALE_DRIVE_WS_URL}" \
  --argjson rollbackOfVersion "${rollback_of_version}" \
  --argjson tombstone "${semantic_tombstone}" \
  '{
    version: $version,
    updatedAt: $updatedAt,
    expiresAt: $expiresAt,
    source: $source,
    cloudflareDriveWsUrl: $cloudflareDriveWsUrl,
    tailscaleDriveWsUrl: $tailscaleDriveWsUrl
  }
  + (if $rollbackOfVersion == null then {} else {rollbackOfVersion: $rollbackOfVersion} end)
  + (if $tombstone then {tombstone: true, restoresPriorAbsence: true} else {} end)' \
  >"${candidate_file}"

backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ "${current_exists}" == "true" ]]; then
  backup_key="${BACKUP_PREFIX%/}/drive-config-v${current_version}-${backup_stamp}.json"
else
  backup_key="${BACKUP_PREFIX%/}/drive-config-prior-absence-${backup_stamp}.json"
fi

echo "Drive config publication plan:"
echo "  action=${ACTION}"
echo "  target=s3://${STATE_BUCKET}/${CONFIG_KEY}"
echo "  currentVersion=${current_version}"
echo "  nextVersion=${next_version}"
echo "  backup=s3://${STATE_BUCKET}/${backup_key}$([[ "${current_exists}" == "true" ]] || printf ' (prior-absence marker)')"
echo "  cloudflareDriveWsUrl=${DRIVE_WS_URL}"
echo "  expiresAt=${expires_at}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "  dryRun=true (no S3 writes)"
  jq . "${candidate_file}"
  exit 0
fi

# Detect a change between the initial read and the write. S3 object versioning,
# when enabled, remains an additional rollback layer; the explicit backup works
# for both versioned and unversioned buckets.
final_head="${WORKDIR}/head-final.json"
final_error="${WORKDIR}/head-final.err"
if [[ "${current_exists}" == "true" ]]; then
  if ! head_object "${final_head}" "${final_error}"; then
    echo "Drive config changed or disappeared during publication; refusing to overwrite it." >&2
    exit 9
  fi
  final_etag="$(jq -r '.ETag // ""' "${final_head}")"
  final_s3_version="$(jq -r '.VersionId // ""' "${final_head}")"
  if [[ "${final_etag}" != "${current_etag}" || "${final_s3_version}" != "${current_s3_version}" ]]; then
    echo "Drive config changed during publication; rerun against the new current version." >&2
    exit 9
  fi
else
  if head_object "${final_head}" "${final_error}"; then
    echo "Drive config was created by another publisher; refusing to overwrite it." >&2
    exit 9
  else
    status=$?
    if [[ "${status}" -ne 1 ]]; then
      exit "${status}"
    fi
  fi
fi

if [[ "${current_exists}" == "true" ]]; then
  aws_region s3api copy-object \
    --bucket "${STATE_BUCKET}" \
    --copy-source "${STATE_BUCKET}/${CONFIG_KEY}" \
    --copy-source-if-match "${current_etag}" \
    --key "${backup_key}" >/dev/null
  echo "Backed up current config to s3://${STATE_BUCKET}/${backup_key}"
else
  absence_marker="${WORKDIR}/prior-absence.json"
  jq -n \
    --arg kind drive-config-prior-absence \
    --arg configKey "${CONFIG_KEY}" \
    --arg observedAt "${updated_at}" \
    '{
      kind: $kind,
      absent: true,
      configKey: $configKey,
      observedVersion: 0,
      observedAt: $observedAt
    }' >"${absence_marker}"
  aws_region s3api put-object \
    --bucket "${STATE_BUCKET}" \
    --key "${backup_key}" \
    --body "${absence_marker}" \
    --content-type application/json \
    --cache-control 'no-store, max-age=0' \
    --if-none-match '*' >/dev/null
  echo "Recorded prior absence at s3://${STATE_BUCKET}/${backup_key}"
fi

put_args=(
  s3api put-object
  --bucket "${STATE_BUCKET}"
  --key "${CONFIG_KEY}"
  --body "${candidate_file}"
  --content-type application/json
  --cache-control 'no-store, max-age=0'
  --output json
)
if [[ "${current_exists}" == "true" ]]; then
  put_args+=(--if-match "${current_etag}")
else
  put_args+=(--if-none-match '*')
fi
put_result="$(aws_region "${put_args[@]}")"

echo "Published drive config version ${next_version}."
published_s3_version="$(jq -r '.VersionId // empty' <<<"${put_result}")"
if [[ -n "${published_s3_version}" ]]; then
  echo "S3 VersionId: ${published_s3_version}"
fi

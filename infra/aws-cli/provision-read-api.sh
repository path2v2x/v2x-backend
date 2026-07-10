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
need sha256sum

AWS_REGION="${AWS_REGION:-us-west-1}"
TABLE_NAME="${TABLE_NAME:-v2x-backend-detections}"
READ_LAMBDA_NAME="${READ_LAMBDA_NAME:-v2x-backend-read}"
READ_LAMBDA_ROLE_ARN="${READ_LAMBDA_ROLE_ARN:-}"
API_NAME="${API_NAME:-v2x-backend-api}"
API_ID="${API_ID:-}"
STAGE_NAME="${STAGE_NAME:-\$default}"
# IAM mutation is an explicit deployment decision. Existing read functions keep
# their current execution role; creating a function requires the caller to name
# the pre-provisioned role with READ_LAMBDA_ROLE_ARN.
ATTACH_DDB_READ_POLICY="${ATTACH_DDB_READ_POLICY:-false}"
READ_POLICY_NAME="${READ_POLICY_NAME:-v2x-backend-detections-ddb-read}"
VIDEO_AWS_REGION="${VIDEO_AWS_REGION:-us-west-2}"
VIDEO_STREAM_PREFIX="${VIDEO_STREAM_PREFIX:-v2x-backend-cam-}"
VIDEO_HLS_EXPIRES_SECONDS="${VIDEO_HLS_EXPIRES_SECONDS:-300}"
VIDEO_ONDEMAND_EXPIRES_SECONDS="${VIDEO_ONDEMAND_EXPIRES_SECONDS:-3600}"
SITE_GEOHASH="${SITE_GEOHASH:-9q9p8}"
SNAPSHOT_URL_EXPIRES_SECONDS="${SNAPSHOT_URL_EXPIRES_SECONDS:-300}"
DEMO_VIDEOS_PREFIX="${DEMO_VIDEOS_PREFIX:-demo-videos/}"
DEMO_VIDEO_URL_EXPIRES_SECONDS="${DEMO_VIDEO_URL_EXPIRES_SECONDS:-3600}"
PLAN_ONLY="${PLAN_ONLY:-true}"
RECONCILE_LAMBDA="${RECONCILE_LAMBDA:-false}"
EXPECTED_CURRENT_STATE_HASH="${EXPECTED_CURRENT_STATE_HASH:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation}"
MANAGED_INTEGRATION_DESCRIPTION="${MANAGED_INTEGRATION_DESCRIPTION:-managed-by=v2x-backend/provision-read-api}"

ROUTE_KEYS=(
  "GET /detections/recent"
  "GET /detections/range"
  "GET /detections/object/{object_id}"
  "GET /detections/geohash/{geohash}"
  "GET /demo-videos"
  "GET /state"
  "GET /map-data"
  "GET /drive-config"
  "GET /snapshots/{object_id}/latest"
  "GET /video/session/{camera_id}"
  "GET /video/coverage/{camera_id}"
  "GET /detections/timeline"
)

case "${PLAN_ONLY}" in
  true|false) ;;
  *)
    echo "PLAN_ONLY must be true or false" >&2
    exit 2
    ;;
esac

case "${ATTACH_DDB_READ_POLICY}" in
  true|false) ;;
  *)
    echo "ATTACH_DDB_READ_POLICY must be true or false" >&2
    exit 2
    ;;
esac

case "${RECONCILE_LAMBDA}" in
  true|false) ;;
  *)
    echo "RECONCILE_LAMBDA must be true or false" >&2
    exit 2
    ;;
esac

if [[ "${RECONCILE_LAMBDA}" == "false" && "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  echo "RECONCILE_LAMBDA=false is a route-only recovery mode and cannot mutate Lambda IAM." >&2
  exit 2
fi

export AWS_REGION

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

is_not_found_error() {
  grep -Eq 'ResourceNotFoundException|NotFoundException|not found' "$1"
}

aws_read_allow_not_found() {
  local output_file="$1"
  local error_file="$2"
  shift 2
  if "$@" >"${output_file}" 2>"${error_file}"; then
    return 0
  fi
  if is_not_found_error "${error_file}"; then
    return 1
  fi
  cat "${error_file}" >&2
  return 2
}

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${STATE_BUCKET:-v2x-backend-state-${ACCOUNT_ID}-${AWS_REGION}}"

echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${HERE}/.secrets"

READ_FUNCTION_FILE="${WORKDIR}/read-function.json"
READ_FUNCTION_ERROR="${WORKDIR}/read-function.err"
READ_LAMBDA_EXISTS=false
if aws_read_allow_not_found "${READ_FUNCTION_FILE}" "${READ_FUNCTION_ERROR}" \
  aws lambda get-function --function-name "${READ_LAMBDA_NAME}" --output json; then
  READ_LAMBDA_EXISTS=true
  READ_FUNCTION_JSON="$(<"${READ_FUNCTION_FILE}")"
  READ_LAMBDA_ARN="$(jq -r '.Configuration.FunctionArn' <<<"${READ_FUNCTION_JSON}")"
else
  status=$?
  if [[ "${status}" -ne 1 ]]; then
    exit "${status}"
  fi
  READ_FUNCTION_JSON='{}'
  READ_LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${READ_LAMBDA_NAME}"
fi

if [[ "${RECONCILE_LAMBDA}" == "false" && "${READ_LAMBDA_EXISTS}" != "true" ]]; then
  echo "RECONCILE_LAMBDA=false requires the existing ${READ_LAMBDA_NAME} Lambda." >&2
  exit 4
fi

READ_POLICY_EXISTS=false
READ_POLICY_JSON='{}'
if [[ "${READ_LAMBDA_EXISTS}" == "true" ]]; then
  initial_policy_file="${WORKDIR}/read-policy.json"
  initial_policy_error="${WORKDIR}/read-policy.err"
  if aws_read_allow_not_found "${initial_policy_file}" "${initial_policy_error}" \
      aws lambda get-policy --function-name "${READ_LAMBDA_NAME}" --output json; then
    READ_POLICY_EXISTS=true
    READ_POLICY_JSON="$(<"${initial_policy_file}")"
  else
    status=$?
    if [[ "${status}" -ne 1 ]]; then
      exit "${status}"
    fi
  fi
fi

if [[ "${READ_LAMBDA_EXISTS}" == "true" ]]; then
  ROLE_ARN="$(jq -er '.Configuration.Role | select(type == "string" and length > 0)' <<<"${READ_FUNCTION_JSON}")"
else
  ROLE_ARN="${READ_LAMBDA_ROLE_ARN}"
fi
ROLE_NAME="${ROLE_ARN##*/}"

MATCHING_API_IDS=()
if [[ -n "${API_ID}" ]]; then
  API_JSON="$(aws apigatewayv2 get-api --api-id "${API_ID}" --output json)"
  observed_api_name="$(jq -r '.Name // ""' <<<"${API_JSON}")"
  if [[ "${observed_api_name}" != "${API_NAME}" ]]; then
    echo "API_ID ${API_ID} is named '${observed_api_name}', expected '${API_NAME}'. Refusing cross-API reconciliation." >&2
    exit 3
  fi
  MATCHING_API_IDS=("${API_ID}")
else
  APIS_JSON="$(aws apigatewayv2 get-apis --output json)"
  mapfile -t MATCHING_API_IDS < <(
    jq -r --arg name "${API_NAME}" '.Items[]? | select(.Name == $name) | .ApiId' <<<"${APIS_JSON}"
  )
fi
if (( ${#MATCHING_API_IDS[@]} > 1 )); then
  echo "Multiple HTTP APIs are named ${API_NAME}: ${MATCHING_API_IDS[*]}" >&2
  echo "Refusing to guess which API owns the production routes." >&2
  exit 3
fi

API_EXISTS=false
API_JSON='{}'
INTEGRATIONS_JSON='{"Items":[]}'
ROUTES_JSON='{"Items":[]}'
STAGES_JSON='{"Items":[]}'
if (( ${#MATCHING_API_IDS[@]} == 1 )); then
  API_EXISTS=true
  API_ID="${MATCHING_API_IDS[0]}"
  API_JSON="$(aws apigatewayv2 get-api --api-id "${API_ID}" --output json)"
  INTEGRATIONS_JSON="$(aws apigatewayv2 get-integrations --api-id "${API_ID}" --output json)"
  ROUTES_JSON="$(aws apigatewayv2 get-routes --api-id "${API_ID}" --output json)"
  STAGES_JSON="$(aws apigatewayv2 get-stages --api-id "${API_ID}" --output json)"
fi

CURRENT_STATE_FILE="${WORKDIR}/current-state.json"
jq -nS \
  --argjson lambda_exists "${READ_LAMBDA_EXISTS}" \
  --argjson lambda "$(jq '.Configuration // {}' <<<"${READ_FUNCTION_JSON}")" \
  --argjson lambda_policy_exists "${READ_POLICY_EXISTS}" \
  --argjson lambda_policy "${READ_POLICY_JSON}" \
  --argjson api_exists "${API_EXISTS}" \
  --argjson api "${API_JSON}" \
  --argjson integrations "${INTEGRATIONS_JSON}" \
  --argjson routes "${ROUTES_JSON}" \
  --argjson stages "${STAGES_JSON}" \
  '{
    lambdaExists: $lambda_exists,
    lambdaConfiguration: $lambda,
    lambdaPolicyExists: $lambda_policy_exists,
    lambdaPolicy: $lambda_policy,
    apiExists: $api_exists,
    api: $api,
    integrations: $integrations,
    routes: $routes,
    stages: $stages
  }' >"${CURRENT_STATE_FILE}"
CURRENT_STATE_HASH="$(sha256sum "${CURRENT_STATE_FILE}" | awk '{print $1}')"
if [[ -n "${EXPECTED_CURRENT_STATE_HASH}" && \
      "${EXPECTED_CURRENT_STATE_HASH}" != "${CURRENT_STATE_HASH}" ]]; then
  echo "Read API state hash is ${CURRENT_STATE_HASH}; expected ${EXPECTED_CURRENT_STATE_HASH}. Refusing to continue." >&2
  exit 3
fi
if [[ "${PLAN_ONLY}" == "false" && -z "${EXPECTED_CURRENT_STATE_HASH}" ]]; then
  echo "PLAN_ONLY=false requires EXPECTED_CURRENT_STATE_HASH from the reviewed plan." >&2
  exit 3
fi

SELECTED_INTEGRATION_ID=""
INTEGRATION_NEEDS_UPDATE=false

is_managed_route_key() {
  local candidate="$1"
  local managed
  for managed in "${ROUTE_KEYS[@]}"; do
    if [[ "${candidate}" == "${managed}" ]]; then
      return 0
    fi
  done
  return 1
}

integration_is_exclusive_to_managed_routes() {
  local integration_id="$1"
  local route_key
  local -a consumers=()

  mapfile -t consumers < <(
    jq -r --arg target "integrations/${integration_id}" \
      '.Items[]? | select((.Target // "") == $target) | .RouteKey' \
      <<<"${ROUTES_JSON}"
  )
  (( ${#consumers[@]} > 0 )) || return 1
  for route_key in "${consumers[@]}"; do
    is_managed_route_key "${route_key}" || return 1
  done
}

select_managed_integration() {
  local route_key target candidate candidate_uri integration_json
  local -a matches=()

  mapfile -t matches < <(
    jq -r \
      --arg arn "${READ_LAMBDA_ARN}" \
      '.Items[]?
       | select(.IntegrationType == "AWS_PROXY")
       | select((.IntegrationUri // "") | contains($arn))
       | select((.PayloadFormatVersion // "") == "2.0")
       | .IntegrationId' <<<"${INTEGRATIONS_JSON}"
  )
  if (( ${#matches[@]} > 0 )); then
    SELECTED_INTEGRATION_ID="${matches[0]}"
    if (( ${#matches[@]} > 1 )); then
      echo "WARN: multiple integrations already target ${READ_LAMBDA_NAME}: ${matches[*]}; reusing ${SELECTED_INTEGRATION_ID}." >&2
    fi
  fi

  if [[ -z "${SELECTED_INTEGRATION_ID}" ]]; then
    candidate="$(
      jq -r --arg description "${MANAGED_INTEGRATION_DESCRIPTION}" \
        '.Items[]? | select(.Description == $description) | .IntegrationId' \
        <<<"${INTEGRATIONS_JSON}" | head -n 1
    )"
    if [[ -n "${candidate}" ]]; then
      candidate_uri="$(jq -r --arg id "${candidate}" \
        '.Items[]? | select(.IntegrationId == $id) | .IntegrationUri // ""' \
        <<<"${INTEGRATIONS_JSON}")"
      if [[ "${candidate_uri}" == *"${READ_LAMBDA_ARN}"* ]] || \
         integration_is_exclusive_to_managed_routes "${candidate}"; then
        SELECTED_INTEGRATION_ID="${candidate}"
      else
        echo "WARN: described integration ${candidate} is shared by an unmanaged route; refusing to retarget it." >&2
      fi
    fi
  fi

  # Reuse an integration already owned by one of this script's routes before
  # creating another integration. It will be retargeted below if it drifted.
  if [[ -z "${SELECTED_INTEGRATION_ID}" ]]; then
    for route_key in "${ROUTE_KEYS[@]}"; do
      target="$(
        jq -r --arg route_key "${route_key}" \
          '.Items[]? | select(.RouteKey == $route_key) | .Target // empty' \
          <<<"${ROUTES_JSON}" | head -n 1
      )"
      candidate="${target#integrations/}"
      if [[ -n "${candidate}" && "${candidate}" != "${target}" ]] && \
        jq -e --arg id "${candidate}" '.Items[]? | select(.IntegrationId == $id)' \
          <<<"${INTEGRATIONS_JSON}" >/dev/null && \
        integration_is_exclusive_to_managed_routes "${candidate}"; then
        SELECTED_INTEGRATION_ID="${candidate}"
        break
      fi
    done
  fi

  if [[ -z "${SELECTED_INTEGRATION_ID}" ]]; then
    INTEGRATION_NEEDS_UPDATE=false
    return
  fi

  integration_json="$(
    jq -c --arg id "${SELECTED_INTEGRATION_ID}" \
      '.Items[] | select(.IntegrationId == $id)' <<<"${INTEGRATIONS_JSON}"
  )"
  if ! jq -e \
    --arg arn "${READ_LAMBDA_ARN}" \
    --arg description "${MANAGED_INTEGRATION_DESCRIPTION}" \
    '.IntegrationType == "AWS_PROXY"
     and ((.IntegrationUri // "") | contains($arn))
     and ((.PayloadFormatVersion // "") == "2.0")
     and ((.Description // "") == $description)' \
    <<<"${integration_json}" >/dev/null; then
    INTEGRATION_NEEDS_UPDATE=true
  fi
}

select_managed_integration

print_reconciliation_plan() {
  local route_key route_id target desired_target stage_auto

  echo
  echo "Read API reconciliation plan (read-only):"
  echo "  currentStateHash=${CURRENT_STATE_HASH}"
  echo "  reconcileLambda=${RECONCILE_LAMBDA}"
  if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
    if [[ -n "${ROLE_NAME}" ]]; then
      echo "  RECONCILE explicitly requested IAM inline policy ${READ_POLICY_NAME} on ${ROLE_NAME}"
    else
      echo "  BLOCKED: ATTACH_DDB_READ_POLICY=true requires READ_LAMBDA_ROLE_ARN for a new Lambda"
    fi
  else
    echo "  KEEP IAM unchanged (ATTACH_DDB_READ_POLICY=false)"
  fi

  if [[ "${RECONCILE_LAMBDA}" == "false" ]]; then
    echo "  KEEP existing Lambda code, configuration, role, and policy: ${READ_LAMBDA_NAME}"
  elif [[ "${READ_LAMBDA_EXISTS}" == "true" ]]; then
    echo "  UPDATE Lambda code and reconcile configuration: ${READ_LAMBDA_NAME}"
  else
    echo "  CREATE Lambda: ${READ_LAMBDA_NAME} (requires explicit READ_LAMBDA_ROLE_ARN when applied)"
  fi

  if [[ "${API_EXISTS}" == "true" ]]; then
    echo "  REUSE HTTP API: ${API_NAME} (${API_ID})"
  else
    echo "  CREATE HTTP API: ${API_NAME}"
  fi

  if [[ -z "${SELECTED_INTEGRATION_ID}" ]]; then
    echo "  CREATE Lambda proxy integration"
  elif [[ "${INTEGRATION_NEEDS_UPDATE}" == "true" ]]; then
    echo "  RETARGET integration ${SELECTED_INTEGRATION_ID} -> ${READ_LAMBDA_ARN}"
  else
    echo "  REUSE integration ${SELECTED_INTEGRATION_ID}"
  fi

  desired_target="integrations/${SELECTED_INTEGRATION_ID:-<new>}"
  for route_key in "${ROUTE_KEYS[@]}"; do
    route_id="$(
      jq -r --arg route_key "${route_key}" \
        '.Items[]? | select(.RouteKey == $route_key) | .RouteId' \
        <<<"${ROUTES_JSON}" | head -n 1
    )"
    target="$(
      jq -r --arg route_key "${route_key}" \
        '.Items[]? | select(.RouteKey == $route_key) | .Target // empty' \
        <<<"${ROUTES_JSON}" | head -n 1
    )"
    if [[ -z "${route_id}" ]]; then
      echo "  CREATE route ${route_key} -> ${desired_target}"
    elif [[ "${target}" != "${desired_target}" ]]; then
      echo "  RETARGET route ${route_key}: ${target:-<none>} -> ${desired_target}"
    else
      echo "  KEEP route ${route_key} -> ${target}"
    fi
  done

  stage_auto="$(
    jq -r --arg stage "${STAGE_NAME}" \
      '.Items[]? | select(.StageName == $stage) | .AutoDeploy' \
      <<<"${STAGES_JSON}" | head -n 1
  )"
  if [[ -z "${stage_auto}" ]]; then
    echo "  CREATE auto-deploy stage ${STAGE_NAME}"
  elif [[ "${stage_auto}" != "true" ]]; then
    echo "  ENABLE auto-deploy on stage ${STAGE_NAME}"
  else
    echo "  KEEP auto-deploy stage ${STAGE_NAME}"
  fi
  if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
    echo "  RECONCILE Lambda invoke permission for API Gateway"
  else
    echo "  KEEP existing Lambda invoke permission unchanged (route-only recovery)"
  fi
}

if [[ "${PLAN_ONLY}" == "true" ]]; then
  print_reconciliation_plan
  echo
  echo "PLAN_ONLY=true: no IAM, Lambda, API Gateway, or filesystem state was changed."
  exit 0
fi

# Capture deployable rollback evidence before the first persistent AWS mutation.
# The get-function response contains a short-lived signed Code.Location, so keep
# only a redacted copy and, when code may change, download the artifact now.
backup_dir="${BACKUP_ROOT%/}/${READ_LAMBDA_NAME}-$(date -u +%Y%m%dT%H%M%SZ)-${CURRENT_STATE_HASH:0:12}"
install -d -m 0700 "${BACKUP_ROOT}" "${backup_dir}"
install -m 0600 "${CURRENT_STATE_FILE}" "${backup_dir}/current-state.json"
printf '%s\n' "${CURRENT_STATE_HASH}" >"${backup_dir}/current-state.sha256"
chmod 0600 "${backup_dir}/current-state.sha256"

if [[ "${READ_LAMBDA_EXISTS}" == "true" ]]; then
  jq 'del(.Code.Location)' "${READ_FUNCTION_FILE}" >"${backup_dir}/lambda-get-function-redacted.json"
  jq -S '.Configuration' "${READ_FUNCTION_FILE}" >"${backup_dir}/lambda-configuration.json"
  if [[ "${READ_POLICY_EXISTS}" == "true" ]]; then
    jq -S . <<<"${READ_POLICY_JSON}" >"${backup_dir}/lambda-policy.json"
    printf 'true\n' >"${backup_dir}/lambda-policy-existed.txt"
  else
    printf 'false\n' >"${backup_dir}/lambda-policy-existed.txt"
  fi

  if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
    need curl
    code_location="$(jq -er '.Code.Location | select(type == "string" and length > 0)' "${READ_FUNCTION_FILE}")"
    curl -fsSL --connect-timeout 15 --max-time 180 \
      "${code_location}" -o "${backup_dir}/lambda-before.zip"
    code_location=""
    if [[ ! -s "${backup_dir}/lambda-before.zip" ]]; then
      echo "Downloaded Lambda rollback artifact is empty." >&2
      exit 5
    fi
    chmod 0600 "${backup_dir}/lambda-before.zip"
  fi
else
  printf 'false\n' >"${backup_dir}/lambda-existed.txt"
fi

if [[ "${API_EXISTS}" == "true" ]]; then
  jq -S . <<<"${API_JSON}" >"${backup_dir}/api.json"
  jq -S . <<<"${INTEGRATIONS_JSON}" >"${backup_dir}/integrations.json"
  jq -S . <<<"${ROUTES_JSON}" >"${backup_dir}/routes.json"
  jq -S . <<<"${STAGES_JSON}" >"${backup_dir}/stages.json"
  printf 'true\n' >"${backup_dir}/api-existed.txt"
else
  printf 'false\n' >"${backup_dir}/api-existed.txt"
fi

printf '%s\n' \
  "apiId=${API_ID:-absent}" \
  "readLambda=${READ_LAMBDA_NAME}" \
  "reconcileLambda=${RECONCILE_LAMBDA}" \
  "attachDdbReadPolicy=${ATTACH_DDB_READ_POLICY}" \
  >"${backup_dir}/reconciliation-inputs.txt"
chmod 0600 "${backup_dir}"/*
(cd "${backup_dir}" && sha256sum -- * >evidence-sha256.txt)
chmod 0600 "${backup_dir}/evidence-sha256.txt"
echo "Rollback evidence captured before apply: ${backup_dir}"

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
  need zip
  need openssl
  need base64
fi

if [[ "${READ_LAMBDA_EXISTS}" != "true" && -z "${ROLE_ARN}" ]]; then
  echo "READ_LAMBDA_ROLE_ARN is required to create ${READ_LAMBDA_NAME}; refusing to infer or create an IAM role." >&2
  exit 4
fi

if [[ "${ATTACH_DDB_READ_POLICY}" == "true" && -z "${ROLE_NAME}" ]]; then
  echo "Cannot attach ${READ_POLICY_NAME} without an explicit or existing read Lambda role." >&2
  exit 4
fi

if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  mkdir -p "${SECRETS_DIR}"
  cat > "${SECRETS_DIR}/lambda-ddb-read-inline.json" <<JSON
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":[ "dynamodb:GetItem", "dynamodb:Query" ],
      "Resource":[
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}/index/*"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:ListFragments"
      ],
      "Resource":[
        "arn:aws:kinesisvideo:${VIDEO_AWS_REGION}:${ACCOUNT_ID}:stream/${VIDEO_STREAM_PREFIX}*"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "kinesisvideo:GetHLSStreamingSessionURL"
      ],
      "Resource":"*"
    },
    {
      "Effect":"Allow",
      "Action":[ "s3:ListBucket" ],
      "Resource":[ "arn:aws:s3:::${STATE_BUCKET}" ]
    },
    {
      "Effect":"Allow",
      "Action":[ "s3:GetObject" ],
      "Resource":[
        "arn:aws:s3:::${STATE_BUCKET}/api/*",
        "arn:aws:s3:::${STATE_BUCKET}/snapshots/*",
        "arn:aws:s3:::${STATE_BUCKET}/${DEMO_VIDEOS_PREFIX}*"
      ]
    }
  ]
}
JSON
  aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${READ_POLICY_NAME}" \
    --policy-document "file://${SECRETS_DIR}/lambda-ddb-read-inline.json" >/dev/null
fi

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
cat > "${WORKDIR}/index.py" <<PY
import base64
import json
import math
import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from botocore.config import Config as BotoConfig

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME", "${TABLE_NAME}")
GSI_NAME = os.environ.get("GSI_NAME", "gsi_geohash_time")
MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "200"))
VIDEO_AWS_REGION = os.environ.get("VIDEO_AWS_REGION", "${VIDEO_AWS_REGION}")
VIDEO_STREAM_PREFIX = os.environ.get("VIDEO_STREAM_PREFIX", "${VIDEO_STREAM_PREFIX}")
VIDEO_HLS_EXPIRES_SECONDS = int(os.environ.get("VIDEO_HLS_EXPIRES_SECONDS", "${VIDEO_HLS_EXPIRES_SECONDS}"))
VIDEO_ONDEMAND_EXPIRES_SECONDS = int(os.environ.get("VIDEO_ONDEMAND_EXPIRES_SECONDS", "${VIDEO_ONDEMAND_EXPIRES_SECONDS}"))
SITE_GEOHASH = os.environ.get("SITE_GEOHASH", "${SITE_GEOHASH}")
STATE_BUCKET = os.environ.get("STATE_BUCKET", "${STATE_BUCKET}")
SNAPSHOT_URL_EXPIRES_SECONDS = int(os.environ.get("SNAPSHOT_URL_EXPIRES_SECONDS", "${SNAPSHOT_URL_EXPIRES_SECONDS}"))
DEMO_VIDEOS_PREFIX = os.environ.get("DEMO_VIDEOS_PREFIX", "${DEMO_VIDEOS_PREFIX}")
DEMO_VIDEO_URL_EXPIRES_SECONDS = int(os.environ.get("DEMO_VIDEO_URL_EXPIRES_SECONDS", "${DEMO_VIDEO_URL_EXPIRES_SECONDS}"))

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE_NAME)
video_client = boto3.client("kinesisvideo", region_name=VIDEO_AWS_REGION, config=BotoConfig(retries={"max_attempts": 3}))
s3_client = boto3.client("s3")

ALLOWED_CAMERA_IDS = {"ch1", "ch2", "ch3", "ch4"}
ALLOWED_DEMO_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}

def _jsonable(value):
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value

def _strip_api_fields(item):
    # Keep storage as-is, but remove fleet identifiers from the public read API.
    if not isinstance(item, dict):
        return item
    item = dict(item)
    item.pop("fleet_id", None)
    return item

def _b64(obj):
    if obj is None:
        return None
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")

def _unb64(s):
    if not s:
        return None
    return json.loads(base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8"))

def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
        },
        "body": json.dumps(body),
    }

def _api_base_url(event):
    headers = event.get("headers") or {}
    request_context = event.get("requestContext") or {}
    proto = headers.get("x-forwarded-proto", "https")
    domain_name = request_context.get("domainName") or headers.get("host", "")
    stage = request_context.get("stage") or ""

    if stage and stage != ("$" + "default"):
        return f"{proto}://{domain_name}/{stage}"
    return f"{proto}://{domain_name}"

def _get_s3_json(key):
    try:
        response = s3_client.get_object(Bucket=STATE_BUCKET, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"} else 502
        return None, _resp(status, {"error": "state_asset_unavailable", "detail": error_code, "key": key})

    body = response["Body"].read().decode("utf-8")
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, _resp(502, {"error": "state_asset_invalid_json", "key": key})

def _snapshot_api_url(event, object_id, snapshot_timestamp):
    base_url = _api_base_url(event)
    encoded_object_id = quote(str(object_id), safe="")
    if snapshot_timestamp:
        encoded_version = quote(str(snapshot_timestamp), safe="")
        return f"{base_url}/snapshots/{encoded_object_id}/latest?v={encoded_version}"
    return f"{base_url}/snapshots/{encoded_object_id}/latest"

def _get_state(event):
    payload, error = _get_s3_json("api/state.json")
    if error:
        return error

    objects = []
    for item in payload.get("objects", []) or []:
        obj = dict(item)
        if obj.get("snapshot_url") and obj.get("object_id"):
            obj["snapshot_url"] = _snapshot_api_url(
                event,
                obj["object_id"],
                obj.get("snapshot_timestamp"),
            )
        objects.append(obj)
    payload["objects"] = objects
    return _resp(200, payload)

def _get_map_data():
    payload, error = _get_s3_json("api/map-data.json")
    if error:
        return error
    return _resp(200, payload)

def _get_drive_config():
    payload, error = _get_s3_json("api/drive-config.json")
    if error:
        return error
    return _resp(200, payload)

def _get_snapshot(object_id):
    key = f"snapshots/{object_id}/latest.jpg"
    try:
        s3_client.head_object(Bucket=STATE_BUCKET, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"NoSuchKey", "404", "NotFound"} else 502
        return _resp(
            status,
            {
                "error": "snapshot_unavailable",
                "objectId": object_id,
                "detail": error_code,
            },
        )

    signed_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": STATE_BUCKET, "Key": key},
        ExpiresIn=SNAPSHOT_URL_EXPIRES_SECONDS,
    )
    return {
        "statusCode": 307,
        "headers": {
            "location": signed_url,
            "cache-control": "no-store",
            "access-control-allow-origin": "*",
        },
        "body": "",
    }

def _demo_video_title(filename):
    stem, _sep, _ext = filename.rpartition(".")
    source = stem or filename
    parts = source.replace("_", " ").replace("-", " ").split()
    return " ".join(parts) if parts else filename

def _get_demo_videos():
    paginator = s3_client.get_paginator("list_objects_v2")
    items = []

    for page in paginator.paginate(Bucket=STATE_BUCKET, Prefix=DEMO_VIDEOS_PREFIX):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            if not key or key.endswith("/"):
                continue

            filename = key.rsplit("/", 1)[-1]
            lower_name = filename.lower()
            if not any(lower_name.endswith(ext) for ext in ALLOWED_DEMO_VIDEO_EXTENSIONS):
                continue

            signed_url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": STATE_BUCKET, "Key": key},
                ExpiresIn=DEMO_VIDEO_URL_EXPIRES_SECONDS,
            )
            content_type = mimetypes.guess_type(filename)[0] or "video/mp4"
            last_modified = obj.get("LastModified")
            items.append(
                {
                    "key": key,
                    "fileName": filename,
                    "title": _demo_video_title(filename),
                    "url": signed_url,
                    "sizeBytes": obj.get("Size", 0),
                    "lastModified": last_modified.isoformat() if last_modified else None,
                    "contentType": content_type,
                }
            )

    items.sort(key=lambda item: item.get("lastModified") or "", reverse=True)
    return _resp(200, {"items": items})

def _camera_stream_name(camera_id):
    return f"{VIDEO_STREAM_PREFIX}{camera_id}"

def _parse_ts(value):
    """Parse an ISO-8601 timestamp (with optional trailing Z) to aware UTC."""
    if not value:
        return None
    v = str(value).strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _parse_trusted_ts(value):
    """Parse only explicit timezone-bearing timestamps for trust decisions."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)

def _exact_schema_version(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)

def _trusted_media_time(item):
    """Apply the persisted schema-v2 HLS media-time acceptance contract."""
    if item.get("media_time_trusted") is not True:
        return False
    if _exact_schema_version(item.get("timestamp_schema_version")) != 2:
        return False

    timestamp_raw = item.get("timestamp_utc")
    media_timestamp_raw = item.get("media_timestamp_utc")
    if (
        not isinstance(timestamp_raw, str)
        or not timestamp_raw.strip()
        or not isinstance(media_timestamp_raw, str)
        or not media_timestamp_raw.strip()
        or timestamp_raw.strip() != media_timestamp_raw.strip()
    ):
        return False
    media_timestamp = _parse_trusted_ts(media_timestamp_raw)
    if media_timestamp is None:
        return False

    media_clock = item.get("media_clock")
    if not isinstance(media_clock, dict):
        return False
    if media_clock.get("source") != "hls_ext_x_program_date_time":
        return False
    if _exact_schema_version(media_clock.get("schema_version")) != 1:
        return False
    anchor = _parse_trusted_ts(media_clock.get("anchor_program_date_time_utc"))
    position = media_clock.get("position_milliseconds")
    if (
        anchor is None
        or isinstance(position, bool)
        or not isinstance(position, (int, float, Decimal))
    ):
        return False
    try:
        position_ms = float(position)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(position_ms) or position_ms < 0:
        return False
    reconstructed = anchor + timedelta(milliseconds=position_ms)
    return abs((reconstructed - media_timestamp).total_seconds()) * 1000.0 <= 5.0

def _iso_millis(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def _ts_event_bounds(start_dt, end_dt):
    # ts_event is "{timestamp_utc}#{event_id}" with millisecond timestamps.
    # Normalising both bounds to millisecond precision keeps the lexicographic
    # BETWEEN correct; "~" sorts after both "Z" and "#".
    return _iso_millis(start_dt), _iso_millis(end_dt) + "~"

def _resolve_window(qs, default_hours=24, max_hours=48):
    start_dt = _parse_ts(qs.get("start"))
    end_dt = _parse_ts(qs.get("end"))
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(hours=default_hours)
    if start_dt >= end_dt:
        return None, None, _resp(400, {"error": "invalid_range", "detail": "start must be before end"})
    if end_dt - start_dt > timedelta(hours=max_hours):
        start_dt = end_dt - timedelta(hours=max_hours)
    return start_dt, end_dt, None

def _archived_media_client(stream_name, api_name):
    endpoint = video_client.get_data_endpoint(
        StreamName=stream_name,
        APIName=api_name,
    )["DataEndpoint"]
    return boto3.client(
        "kinesis-video-archived-media",
        region_name=VIDEO_AWS_REGION,
        endpoint_url=endpoint,
        config=BotoConfig(retries={"max_attempts": 3}),
    )

def _get_hls_session(camera_id, qs):
    if camera_id not in ALLOWED_CAMERA_IDS:
        return _resp(404, {"error": "camera_not_found", "cameraId": camera_id})

    stream_name = _camera_stream_name(camera_id)
    start_dt = _parse_ts(qs.get("start"))
    end_dt = _parse_ts(qs.get("end"))
    on_demand = start_dt is not None or end_dt is not None

    if on_demand:
        if start_dt is None or end_dt is None:
            return _resp(400, {"error": "invalid_range", "detail": "archive playback requires both start and end"})
        if start_dt >= end_dt:
            return _resp(400, {"error": "invalid_range", "detail": "start must be before end"})
        if end_dt - start_dt > timedelta(hours=24):
            return _resp(400, {"error": "invalid_range", "detail": "window must be 24 hours or less"})

    try:
        archived_media = _archived_media_client(stream_name, "GET_HLS_STREAMING_SESSION_URL")
        if on_demand:
            hls_url = archived_media.get_hls_streaming_session_url(
                StreamName=stream_name,
                PlaybackMode="ON_DEMAND",
                HLSFragmentSelector={
                    "FragmentSelectorType": "SERVER_TIMESTAMP",
                    "TimestampRange": {
                        "StartTimestamp": start_dt,
                        "EndTimestamp": end_dt,
                    },
                },
                Expires=VIDEO_ONDEMAND_EXPIRES_SECONDS,
                ContainerFormat="FRAGMENTED_MP4",
                DiscontinuityMode="ON_DISCONTINUITY",
                DisplayFragmentTimestamp="ALWAYS",
                MaxMediaPlaylistFragmentResults=5000,
            )["HLSStreamingSessionURL"]
            return _resp(
                200,
                {
                    "cameraId": camera_id,
                    "streamName": stream_name,
                    "playbackMode": "ON_DEMAND",
                    "hlsUrl": hls_url,
                    "expiresIn": VIDEO_ONDEMAND_EXPIRES_SECONDS,
                    "start": _iso_millis(start_dt),
                    "end": _iso_millis(end_dt),
                    "region": VIDEO_AWS_REGION,
                },
            )
        hls_url = archived_media.get_hls_streaming_session_url(
            StreamName=stream_name,
            PlaybackMode="LIVE",
            Expires=VIDEO_HLS_EXPIRES_SECONDS,
            ContainerFormat="FRAGMENTED_MP4",
            DiscontinuityMode="ALWAYS",
            DisplayFragmentTimestamp="ALWAYS",
            MaxMediaPlaylistFragmentResults=5,
        )["HLSStreamingSessionURL"]
        return _resp(
            200,
            {
                "cameraId": camera_id,
                "streamName": stream_name,
                "playbackMode": "LIVE",
                "hlsUrl": hls_url,
                "expiresIn": VIDEO_HLS_EXPIRES_SECONDS,
                "region": VIDEO_AWS_REGION,
            },
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"ResourceNotFoundException", "NoDataRetentionException"} else 502
        return _resp(
            status,
            {
                "error": "video_session_unavailable",
                "cameraId": camera_id,
                "streamName": stream_name,
                "detail": error_code,
            },
        )

def _get_video_coverage(camera_id, qs):
    """Merged fragment intervals so the timeline UI can grey out gaps."""
    if camera_id not in ALLOWED_CAMERA_IDS:
        return _resp(404, {"error": "camera_not_found", "cameraId": camera_id})

    stream_name = _camera_stream_name(camera_id)
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    try:
        archived_media = _archived_media_client(stream_name, "LIST_FRAGMENTS")
        fragments = []
        next_token = None
        pages = 0
        # ~2s fragments -> a 24h window is ~45 pages of 1000, which cannot
        # finish inside API Gateway's 30s integration limit. Stop on a time
        # budget and report truncation; the web client requests coverage in
        # ~4h chunks so real queries never hit this.
        deadline = time.monotonic() + 20.0
        while pages < 60 and time.monotonic() < deadline:
            kwargs = {
                "StreamName": stream_name,
                "MaxResults": 1000,
                "FragmentSelector": {
                    "FragmentSelectorType": "SERVER_TIMESTAMP",
                    "TimestampRange": {
                        "StartTimestamp": start_dt,
                        "EndTimestamp": end_dt,
                    },
                },
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = archived_media.list_fragments(**kwargs)
            fragments.extend(resp.get("Fragments", []) or [])
            next_token = resp.get("NextToken")
            pages += 1
            if not next_token:
                break
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"ResourceNotFoundException", "NoDataRetentionException"} else 502
        return _resp(status, {"error": "video_coverage_unavailable", "cameraId": camera_id, "detail": error_code})

    spans = sorted(
        (
            (
                f["ServerTimestamp"],
                f["ServerTimestamp"] + timedelta(milliseconds=int(f.get("FragmentLengthInMilliseconds") or 0)),
            )
            for f in fragments
            if f.get("ServerTimestamp") is not None
        ),
        key=lambda pair: pair[0],
    )

    gap_tolerance = timedelta(seconds=15)
    intervals = []
    for span_start, span_end in spans:
        if intervals and span_start - intervals[-1][1] <= gap_tolerance:
            if span_end > intervals[-1][1]:
                intervals[-1][1] = span_end
        else:
            intervals.append([span_start, span_end])

    return _resp(
        200,
        {
            "cameraId": camera_id,
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
            "intervals": [
                {"start": _iso_millis(s), "end": _iso_millis(e)} for s, e in intervals
            ],
            "fragmentCount": len(fragments),
            "truncated": next_token is not None,
        },
    )

def _range_filter_expression(qs):
    filters = []
    device_id = (qs.get("device_id") or "").strip()
    object_type = (qs.get("object_type") or "").strip()
    if device_id:
        filters.append(Attr("device_id").eq(device_id))
    if object_type:
        filters.append(Attr("object_type").eq(object_type))
    if not filters:
        return None
    condition = filters[0]
    for extra in filters[1:]:
        condition = condition & extra
    return condition

def _get_detections_range(qs, limit, exclusive_start_key):
    # All detections at the site share one precision-5 geohash, so the
    # geohash+ts_event GSI doubles as a time index.
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    start_key, end_key = _ts_event_bounds(start_dt, end_dt)
    kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH)
        & Key("ts_event").between(start_key, end_key),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    condition = _range_filter_expression(qs)
    if condition is not None:
        kwargs["FilterExpression"] = condition
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key
    resp = table.query(**kwargs)
    items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
    return _resp(
        200,
        {
            "items": _jsonable(items),
            "next": _b64(resp.get("LastEvaluatedKey")),
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
        },
    )

def _get_detections_recent(limit, exclusive_start_key):
    """Return the site's newest detections from the geohash/time index.

    DynamoDB Scan order isn't chronological, and its Limit is applied before
    any client-side sort. Querying the site's shared geohash partition keeps
    pagination stable and guarantees newest-first results without reading old
    table pages first.
    """
    kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key
    resp = table.query(**kwargs)
    items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
    return _resp(
        200,
        {
            "items": _jsonable(items),
            "next": _b64(resp.get("LastEvaluatedKey")),
        },
    )

TIMELINE_MAX_PAGES = int(os.environ.get("TIMELINE_MAX_PAGES", "40"))

def _get_detections_timeline(qs):
    """Aggregate a time window into track events + a per-bucket histogram.

    Grouping happens here so the browser never has to page through tens of
    thousands of raw detection rows to draw timeline markers.
    """
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    try:
        bucket_seconds = int(qs.get("bucket") or "60")
    except ValueError:
        bucket_seconds = 60
    bucket_seconds = max(10, min(3600, bucket_seconds))

    start_key, end_key = _ts_event_bounds(start_dt, end_dt)
    base_kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH)
        & Key("ts_event").between(start_key, end_key),
        "ScanIndexForward": True,
        "ProjectionExpression": (
            "event_id, object_id, object_type, timestamp_utc, "
            "media_timestamp_utc, timestamp_schema_version, media_time_trusted, "
            "media_clock, device_id, confidence_score"
        ),
    }
    condition = _range_filter_expression(qs)
    if condition is not None:
        base_kwargs["FilterExpression"] = condition

    tracks = {}
    buckets = {}
    total = 0
    truncated = False
    exclusive_start_key = None
    for _ in range(TIMELINE_MAX_PAGES):
        kwargs = dict(base_kwargs)
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        for item in resp.get("Items", []) or []:
            ts = _parse_ts(item.get("timestamp_utc"))
            if ts is None:
                continue
            total += 1
            object_id = str(item.get("object_id") or "unknown")
            object_type = str(item.get("object_type") or "unknown")
            confidence = item.get("confidence_score")
            confidence = float(confidence) if isinstance(confidence, (int, float, Decimal)) else 0.0
            schema_raw = item.get("timestamp_schema_version")
            timestamp_schema_version = _exact_schema_version(schema_raw)
            media_time_trusted = _trusted_media_time(item)
            event_id = str(item.get("event_id") or "")
            media_timestamp = str(item.get("media_timestamp_utc") or "")

            track = tracks.get(object_id)
            if track is None:
                tracks[object_id] = {
                    "object_id": object_id,
                    "object_type": object_type,
                    "device_id": str(item.get("device_id") or ""),
                    "first_seen": ts,
                    "last_seen": ts,
                    "count": 1,
                    "max_confidence": confidence,
                    "media_time_trusted": media_time_trusted,
                    "timestamp_schema_version": timestamp_schema_version,
                    "first_event_id": event_id,
                    "last_event_id": event_id,
                    "first_media_timestamp_utc": media_timestamp,
                    "last_media_timestamp_utc": media_timestamp,
                }
            else:
                track["count"] += 1
                track["media_time_trusted"] = (
                    track["media_time_trusted"] and media_time_trusted
                )
                if ts < track["first_seen"]:
                    track["first_seen"] = ts
                    track["first_event_id"] = event_id
                    track["first_media_timestamp_utc"] = media_timestamp
                if ts > track["last_seen"]:
                    track["last_seen"] = ts
                    track["last_event_id"] = event_id
                    track["last_media_timestamp_utc"] = media_timestamp
                if confidence > track["max_confidence"]:
                    track["max_confidence"] = confidence

            bucket_idx = int((ts - start_dt).total_seconds() // bucket_seconds)
            counts = buckets.setdefault(bucket_idx, {})
            counts[object_type] = counts.get(object_type, 0) + 1

        exclusive_start_key = resp.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break
    else:
        truncated = True

    events = sorted(tracks.values(), key=lambda t: t["first_seen"])
    return _resp(
        200,
        {
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
            "bucketSeconds": bucket_seconds,
            "totalDetections": total,
            "truncated": truncated,
            "events": [
                {
                    "object_id": t["object_id"],
                    "object_type": t["object_type"],
                    "device_id": t["device_id"],
                    "first_seen": _iso_millis(t["first_seen"]),
                    "last_seen": _iso_millis(t["last_seen"]),
                    "count": t["count"],
                    "max_confidence": round(t["max_confidence"], 4),
                    "media_time_trusted": t["media_time_trusted"],
                    "timestamp_schema_version": t["timestamp_schema_version"],
                    "first_event_id": t["first_event_id"],
                    "last_event_id": t["last_event_id"],
                    "first_media_timestamp_utc": t["first_media_timestamp_utc"],
                    "last_media_timestamp_utc": t["last_media_timestamp_utc"],
                }
                for t in events
            ],
            "histogram": [
                {
                    "bucket_start": _iso_millis(start_dt + timedelta(seconds=idx * bucket_seconds)),
                    "counts": buckets[idx],
                }
                for idx in sorted(buckets)
            ],
        },
    )

def handler(event, context):
    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")
    qs = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    try:
        limit = int(qs.get("limit") or "50")
    except ValueError:
        limit = 50
    limit = max(1, min(MAX_LIMIT, limit))

    next_token = qs.get("next")
    exclusive_start_key = _unb64(next_token)

    if path.startswith("/video/session/"):
        camera_id = path_params.get("camera_id") or path.split("/video/session/", 1)[1]
        return _get_hls_session(camera_id, qs)

    if path.startswith("/video/coverage/"):
        camera_id = path_params.get("camera_id") or path.split("/video/coverage/", 1)[1]
        return _get_video_coverage(camera_id, qs)

    if path == "/detections/timeline":
        return _get_detections_timeline(qs)

    if path == "/demo-videos":
        return _get_demo_videos()

    if path == "/state":
        return _get_state(event)

    if path == "/map-data":
        return _get_map_data()

    if path == "/drive-config":
        return _get_drive_config()

    if path.startswith("/snapshots/") and path.endswith("/latest"):
        object_id = path_params.get("object_id") or path.split("/snapshots/", 1)[1].rsplit("/latest", 1)[0]
        return _get_snapshot(object_id)

    if path.startswith("/detections/object/"):
        object_id = path_params.get("object_id") or path.split("/detections/object/", 1)[1]
        kwargs = {
            "KeyConditionExpression": Key("object_id").eq(object_id),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
        return _resp(
            200,
            {
                "items": _jsonable(items),
                "next": _b64(resp.get("LastEvaluatedKey")),
            },
        )

    if path.startswith("/detections/geohash/"):
        geohash = path_params.get("geohash") or path.split("/detections/geohash/", 1)[1]
        kwargs = {
            "IndexName": GSI_NAME,
            "KeyConditionExpression": Key("geohash").eq(geohash),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
        return _resp(
            200,
            {
                "items": _jsonable(items),
                "next": _b64(resp.get("LastEvaluatedKey")),
            },
        )

    if path == "/detections/range":
        return _get_detections_range(qs, limit, exclusive_start_key)

    if path == "/detections/recent":
        return _get_detections_recent(limit, exclusive_start_key)

    if path in ("", "/"):
        return _resp(
            200,
            {
                "ok": True,
                "routes": [
                    "/demo-videos",
                    "/state",
                    "/map-data",
                    "/drive-config",
                    "/snapshots/{object_id}/latest",
                    "/detections/range",
                    "/detections/recent",
                    "/detections/timeline",
                    "/detections/object/{object_id}",
                    "/detections/geohash/{geohash}",
                    "/video/session/{camera_id}",
                    "/video/coverage/{camera_id}",
                ],
            },
        )

    return _resp(404, {"error": "not_found", "path": path})
PY

(cd "${WORKDIR}" && touch -t 198001010000 index.py && zip -Xq function.zip index.py)

desired_vars_file="${WORKDIR}/desired-vars.json"
jq -n \
  --arg TABLE_NAME "${TABLE_NAME}" \
  --arg GSI_NAME gsi_geohash_time \
  --arg MAX_LIMIT 200 \
  --arg VIDEO_AWS_REGION "${VIDEO_AWS_REGION}" \
  --arg VIDEO_STREAM_PREFIX "${VIDEO_STREAM_PREFIX}" \
  --arg VIDEO_HLS_EXPIRES_SECONDS "${VIDEO_HLS_EXPIRES_SECONDS}" \
  --arg STATE_BUCKET "${STATE_BUCKET}" \
  --arg SNAPSHOT_URL_EXPIRES_SECONDS "${SNAPSHOT_URL_EXPIRES_SECONDS}" \
  --arg DEMO_VIDEOS_PREFIX "${DEMO_VIDEOS_PREFIX}" \
  --arg DEMO_VIDEO_URL_EXPIRES_SECONDS "${DEMO_VIDEO_URL_EXPIRES_SECONDS}" \
  --arg VIDEO_ONDEMAND_EXPIRES_SECONDS "${VIDEO_ONDEMAND_EXPIRES_SECONDS}" \
  --arg SITE_GEOHASH "${SITE_GEOHASH}" \
  '{$TABLE_NAME, $GSI_NAME, $MAX_LIMIT, $VIDEO_AWS_REGION, $VIDEO_STREAM_PREFIX,
    $VIDEO_HLS_EXPIRES_SECONDS, $STATE_BUCKET, $SNAPSHOT_URL_EXPIRES_SECONDS,
    $DEMO_VIDEOS_PREFIX, $DEMO_VIDEO_URL_EXPIRES_SECONDS,
    $VIDEO_ONDEMAND_EXPIRES_SECONDS, $SITE_GEOHASH}' >"${desired_vars_file}"

environment_file="${WORKDIR}/environment.json"
if [[ "${READ_LAMBDA_EXISTS}" == "true" ]]; then
  jq --slurpfile desired "${desired_vars_file}" \
    '{Variables: ((.Configuration.Environment.Variables // {}) + $desired[0])}' \
    <<<"${READ_FUNCTION_JSON}" >"${environment_file}"

  candidate_sha256="$(openssl dgst -sha256 -binary "${WORKDIR}/function.zip" | base64 -w 0)"
  current_sha256="$(jq -r '.Configuration.CodeSha256 // ""' <<<"${READ_FUNCTION_JSON}")"
  if [[ "${candidate_sha256}" != "${current_sha256}" ]]; then
    aws lambda update-function-code \
      --function-name "${READ_LAMBDA_NAME}" \
      --zip-file "fileb://${WORKDIR}/function.zip" >/dev/null
    aws lambda wait function-updated --function-name "${READ_LAMBDA_NAME}"
  else
    echo "Lambda code already matches the deterministic package; keeping it unchanged."
  fi

  current_timeout="$(jq -r '.Configuration.Timeout // 0' <<<"${READ_FUNCTION_JSON}")"
  current_environment="$(jq -Sc '.Configuration.Environment.Variables // {}' <<<"${READ_FUNCTION_JSON}")"
  desired_environment="$(jq -Sc '.Variables' "${environment_file}")"
  if [[ "${current_timeout}" != "30" || "${current_environment}" != "${desired_environment}" ]]; then
    aws lambda update-function-configuration \
      --function-name "${READ_LAMBDA_NAME}" \
      --timeout 30 \
      --environment "file://${environment_file}" >/dev/null
    aws lambda wait function-updated --function-name "${READ_LAMBDA_NAME}"
  else
    echo "Lambda configuration already matches; keeping it unchanged."
  fi
else
  jq -n --slurpfile desired "${desired_vars_file}" '{Variables: $desired[0]}' >"${environment_file}"
  aws lambda create-function \
    --function-name "${READ_LAMBDA_NAME}" \
    --runtime python3.12 \
    --handler index.handler \
    --role "${ROLE_ARN}" \
    --timeout 30 \
    --environment "file://${environment_file}" \
    --zip-file "fileb://${WORKDIR}/function.zip" >/dev/null
  aws lambda wait function-active-v2 --function-name "${READ_LAMBDA_NAME}"
fi
else
  echo "Route-only recovery: keeping existing Lambda code, configuration, execution role, and resource policy unchanged."
fi

READ_LAMBDA_ARN="$(aws lambda get-function --function-name "${READ_LAMBDA_NAME}" --query Configuration.FunctionArn --output text)"

if [[ "${API_EXISTS}" != "true" ]]; then
  API_ID="$(aws apigatewayv2 create-api \
    --name "${API_NAME}" \
    --protocol-type HTTP \
    --cors-configuration AllowOrigins='*',AllowMethods='GET,OPTIONS',AllowHeaders='content-type' \
    --query ApiId --output text)"
  API_EXISTS=true
  INTEGRATIONS_JSON='{"Items":[]}'
  ROUTES_JSON='{"Items":[]}'
  STAGES_JSON='{"Items":[]}'
fi

if [[ -z "${SELECTED_INTEGRATION_ID}" ]]; then
  SELECTED_INTEGRATION_ID="$(aws apigatewayv2 create-integration \
    --api-id "${API_ID}" \
    --integration-type AWS_PROXY \
    --integration-uri "${READ_LAMBDA_ARN}" \
    --payload-format-version 2.0 \
    --description "${MANAGED_INTEGRATION_DESCRIPTION}" \
    --query IntegrationId --output text)"
elif [[ "${INTEGRATION_NEEDS_UPDATE}" == "true" ]]; then
  aws apigatewayv2 update-integration \
    --api-id "${API_ID}" \
    --integration-id "${SELECTED_INTEGRATION_ID}" \
    --integration-uri "${READ_LAMBDA_ARN}" \
    --payload-format-version 2.0 \
    --description "${MANAGED_INTEGRATION_DESCRIPTION}" >/dev/null
fi

desired_target="integrations/${SELECTED_INTEGRATION_ID}"
for route_key in "${ROUTE_KEYS[@]}"; do
  route_id="$(
    jq -r --arg route_key "${route_key}" \
      '.Items[]? | select(.RouteKey == $route_key) | .RouteId' \
      <<<"${ROUTES_JSON}" | head -n 1
  )"
  route_target="$(
    jq -r --arg route_key "${route_key}" \
      '.Items[]? | select(.RouteKey == $route_key) | .Target // empty' \
      <<<"${ROUTES_JSON}" | head -n 1
  )"
  if [[ -z "${route_id}" ]]; then
    aws apigatewayv2 create-route \
      --api-id "${API_ID}" \
      --route-key "${route_key}" \
      --target "${desired_target}" >/dev/null
  elif [[ "${route_target}" != "${desired_target}" ]]; then
    aws apigatewayv2 update-route \
      --api-id "${API_ID}" \
      --route-id "${route_id}" \
      --target "${desired_target}" >/dev/null
  fi
done

stage_auto="$(
  jq -r --arg stage "${STAGE_NAME}" \
    '.Items[]? | select(.StageName == $stage) | .AutoDeploy' \
    <<<"${STAGES_JSON}" | head -n 1
)"
if [[ -z "${stage_auto}" ]]; then
  aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name "${STAGE_NAME}" \
    --auto-deploy >/dev/null
elif [[ "${stage_auto}" != "true" ]]; then
  aws apigatewayv2 update-stage \
    --api-id "${API_ID}" \
    --stage-name "${STAGE_NAME}" \
    --auto-deploy >/dev/null
fi

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
STATEMENT_ID="apigw-${API_ID}"
SOURCE_ARN="arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*/*"
POLICY_JSON="${WORKDIR}/lambda-policy.json"
POLICY_ERROR="${WORKDIR}/lambda-policy.err"
permission_matches=false
permission_exists=false
if aws_read_allow_not_found "${POLICY_JSON}" "${POLICY_ERROR}" \
  aws lambda get-policy --function-name "${READ_LAMBDA_NAME}" --output json; then
  if jq -e --arg sid "${STATEMENT_ID}" '.Policy | fromjson | .Statement[]? | select(.Sid == $sid)' \
    "${POLICY_JSON}" >/dev/null; then
    permission_exists=true
  fi
  if jq -e \
    --arg sid "${STATEMENT_ID}" \
    --arg source "${SOURCE_ARN}" \
    '.Policy | fromjson | .Statement[]?
     | select(.Sid == $sid)
     | select(.Effect == "Allow")
     | select(.Action == "lambda:InvokeFunction")
     | select(.Principal.Service == "apigateway.amazonaws.com")
     | select(.Condition.ArnLike["AWS:SourceArn"] == $source)' \
    "${POLICY_JSON}" >/dev/null; then
    permission_matches=true
  fi
else
  status=$?
  if [[ "${status}" -ne 1 ]]; then
    exit "${status}"
  fi
fi

if [[ "${permission_matches}" != "true" ]]; then
  if [[ "${permission_exists}" == "true" ]]; then
    aws lambda remove-permission \
      --function-name "${READ_LAMBDA_NAME}" \
      --statement-id "${STATEMENT_ID}" >/dev/null
  fi
  aws lambda add-permission \
    --function-name "${READ_LAMBDA_NAME}" \
    --statement-id "${STATEMENT_ID}" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "${SOURCE_ARN}" >/dev/null
fi
fi

API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "${API_ID}" --query ApiEndpoint --output text)"

echo "Done."
echo "Read Lambda: ${READ_LAMBDA_NAME}"
echo "HTTP API: ${API_ENDPOINT}"
echo "Example:"
echo "  ${API_ENDPOINT}/detections/object/traffic_cone_001?limit=10"

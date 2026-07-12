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
HLS_PROXY_PREFIX="${HLS_PROXY_PREFIX:-hls-proxy/v1/}"
HLS_PROXY_FETCH_TIMEOUT_SECONDS="${HLS_PROXY_FETCH_TIMEOUT_SECONDS:-8}"
HLS_PROXY_PLAYLIST_MAX_BYTES="${HLS_PROXY_PLAYLIST_MAX_BYTES:-1048576}"
HLS_PROXY_SEGMENT_MAX_BYTES="${HLS_PROXY_SEGMENT_MAX_BYTES:-4194304}"
HLS_SESSION_THROTTLE_BURST="${HLS_SESSION_THROTTLE_BURST:-8}"
HLS_SESSION_THROTTLE_RATE="${HLS_SESSION_THROTTLE_RATE:-2.0}"
HLS_PROXY_LIFECYCLE_DAYS="${HLS_PROXY_LIFECYCLE_DAYS:-1}"
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
  "GET /video/proxy/{token}/{resource_id}"
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

if [[ ! "${HLS_SESSION_THROTTLE_BURST}" =~ ^[1-9][0-9]*$ ]] ||
   (( HLS_SESSION_THROTTLE_BURST > 100 )); then
  echo "HLS_SESSION_THROTTLE_BURST must be an integer from 1 through 100" >&2
  exit 2
fi
if ! jq -en --arg value "${HLS_SESSION_THROTTLE_RATE}" \
  '($value | tonumber?) as $rate | $rate != null and $rate > 0 and $rate <= 100' >/dev/null; then
  echo "HLS_SESSION_THROTTLE_RATE must be greater than 0 and at most 100" >&2
  exit 2
fi
if [[ ! "${HLS_PROXY_LIFECYCLE_DAYS}" =~ ^[1-7]$ ]]; then
  echo "HLS_PROXY_LIFECYCLE_DAYS must be an integer from 1 through 7" >&2
  exit 2
fi

if [[ "${RECONCILE_LAMBDA}" == "false" && "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  echo "RECONCILE_LAMBDA=false is a route-only recovery mode and cannot mutate Lambda IAM." >&2
  exit 2
fi
if [[ "${PLAN_ONLY}" == "false" && "${RECONCILE_LAMBDA}" == "true" && \
      "${ATTACH_DDB_READ_POLICY}" != "true" ]]; then
  echo "The HLS proxy requires its reviewed, prefix-scoped state policy; Lambda reconciliation requires ATTACH_DDB_READ_POLICY=true." >&2
  exit 2
fi

export AWS_REGION

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

is_not_found_error() {
  grep -Eq 'ResourceNotFoundException|NotFoundException|NoSuchEntity|NoSuchLifecycleConfiguration|not found' "$1"
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

DESIRED_POLICY_FILE="${WORKDIR}/desired-read-role-inline-policy.json"
if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  cat >"${DESIRED_POLICY_FILE}" <<JSON
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
      "Action":[ "kinesisvideo:GetHLSStreamingSessionURL" ],
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
    },
    {
      "Effect":"Allow",
      "Action":[ "s3:GetObject", "s3:PutObject", "s3:DeleteObject" ],
      "Resource":[ "arn:aws:s3:::${STATE_BUCKET}/${HLS_PROXY_PREFIX}*" ]
    }
  ]
}
JSON
  jq -e . "${DESIRED_POLICY_FILE}" >/dev/null
  DESIRED_POLICY_HASH="$(sha256sum "${DESIRED_POLICY_FILE}" | awk '{print $1}')"
else
  printf '{}\n' >"${DESIRED_POLICY_FILE}"
  DESIRED_POLICY_HASH="absent"
fi

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

READ_ROLE_INLINE_POLICY_INSPECTED=false
READ_ROLE_INLINE_POLICY_EXISTS=false
READ_ROLE_INLINE_POLICY_JSON='{}'
if [[ "${ATTACH_DDB_READ_POLICY}" == "true" && -n "${ROLE_NAME}" ]]; then
  READ_ROLE_INLINE_POLICY_INSPECTED=true
  role_policy_file="${WORKDIR}/read-role-inline-policy.json"
  role_policy_error="${WORKDIR}/read-role-inline-policy.err"
  if aws_read_allow_not_found "${role_policy_file}" "${role_policy_error}" \
      aws iam get-role-policy \
        --role-name "${ROLE_NAME}" \
        --policy-name "${READ_POLICY_NAME}" \
        --output json; then
    READ_ROLE_INLINE_POLICY_EXISTS=true
    READ_ROLE_INLINE_POLICY_JSON="$(<"${role_policy_file}")"
  else
    status=$?
    if [[ "${status}" -ne 1 ]]; then
      exit "${status}"
    fi
  fi
fi

HLS_PROXY_LIFECYCLE_RULE_ID="v2x-hls-proxy-expiry-v1"
BUCKET_LIFECYCLE_EXISTS=false
BUCKET_LIFECYCLE_JSON='{"Rules":[]}'
if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
  lifecycle_file="${WORKDIR}/bucket-lifecycle.json"
  lifecycle_error="${WORKDIR}/bucket-lifecycle.err"
  if aws_read_allow_not_found "${lifecycle_file}" "${lifecycle_error}" \
      aws s3api get-bucket-lifecycle-configuration \
        --bucket "${STATE_BUCKET}" \
        --expected-bucket-owner "${ACCOUNT_ID}" \
        --output json; then
    BUCKET_LIFECYCLE_EXISTS=true
    BUCKET_LIFECYCLE_JSON="$(<"${lifecycle_file}")"
  else
    status=$?
    if [[ "${status}" -ne 1 ]]; then
      exit "${status}"
    fi
  fi
fi
DESIRED_LIFECYCLE_FILE="${WORKDIR}/desired-bucket-lifecycle.json"
jq -n \
  --argjson current "${BUCKET_LIFECYCLE_JSON}" \
  --arg rule_id "${HLS_PROXY_LIFECYCLE_RULE_ID}" \
  --arg prefix "${HLS_PROXY_PREFIX}" \
  --argjson days "${HLS_PROXY_LIFECYCLE_DAYS}" \
  '{Rules: (
    (($current.Rules // []) | map(select(.ID != $rule_id)))
    + [{
      ID: $rule_id,
      Status: "Enabled",
      Filter: {Prefix: $prefix},
      Expiration: {Days: $days},
      AbortIncompleteMultipartUpload: {DaysAfterInitiation: 1}
    }]
  )}' >"${DESIRED_LIFECYCLE_FILE}"
DESIRED_LIFECYCLE_HASH="$(sha256sum "${DESIRED_LIFECYCLE_FILE}" | awk '{print $1}')"

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
  --argjson role_inline_policy_inspected "${READ_ROLE_INLINE_POLICY_INSPECTED}" \
  --argjson role_inline_policy_exists "${READ_ROLE_INLINE_POLICY_EXISTS}" \
  --argjson role_inline_policy "${READ_ROLE_INLINE_POLICY_JSON}" \
  --arg desired_role_inline_policy_sha256 "${DESIRED_POLICY_HASH}" \
  --argjson bucket_lifecycle_exists "${BUCKET_LIFECYCLE_EXISTS}" \
  --argjson bucket_lifecycle "${BUCKET_LIFECYCLE_JSON}" \
  --arg desired_bucket_lifecycle_sha256 "${DESIRED_LIFECYCLE_HASH}" \
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
    readRoleInlinePolicyInspected: $role_inline_policy_inspected,
    readRoleInlinePolicyExists: $role_inline_policy_exists,
    readRoleInlinePolicy: $role_inline_policy,
    desiredReadRoleInlinePolicySha256: $desired_role_inline_policy_sha256,
    bucketLifecycleExists: $bucket_lifecycle_exists,
    bucketLifecycle: $bucket_lifecycle,
    desiredBucketLifecycleSha256: $desired_bucket_lifecycle_sha256,
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
  echo "  THROTTLE GET /video/session/{camera_id}: burst=${HLS_SESSION_THROTTLE_BURST}, rate=${HLS_SESSION_THROTTLE_RATE}/second"
  if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
    echo "  RECONCILE S3 lifecycle ${HLS_PROXY_LIFECYCLE_RULE_ID}: expire ${HLS_PROXY_PREFIX} after ${HLS_PROXY_LIFECYCLE_DAYS} day(s)"
  fi
  if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
    if [[ -n "${ROLE_NAME}" ]]; then
      echo "  RECONCILE explicitly requested IAM inline policy ${READ_POLICY_NAME} on ${ROLE_NAME}"
      echo "  observedInlinePolicyExists=${READ_ROLE_INLINE_POLICY_EXISTS} (included in currentStateHash)"
    else
      echo "  BLOCKED: ATTACH_DDB_READ_POLICY=true requires READ_LAMBDA_ROLE_ARN for a new Lambda"
    fi
  else
    echo "  KEEP IAM unchanged (ATTACH_DDB_READ_POLICY=false)"
    if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
      echo "  BLOCKED FOR APPLY: HLS proxy state access has not been reconciled"
    fi
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

if [[ "${READ_ROLE_INLINE_POLICY_INSPECTED}" == "true" ]]; then
  printf '%s\n' "${READ_ROLE_INLINE_POLICY_EXISTS}" \
    >"${backup_dir}/read-role-inline-policy-existed.txt"
  if [[ "${READ_ROLE_INLINE_POLICY_EXISTS}" == "true" ]]; then
    jq -S . <<<"${READ_ROLE_INLINE_POLICY_JSON}" \
      >"${backup_dir}/read-role-inline-policy.json"
  fi
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

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
  printf '%s\n' "${BUCKET_LIFECYCLE_EXISTS}" \
    >"${backup_dir}/bucket-lifecycle-existed.txt"
  jq -S . <<<"${BUCKET_LIFECYCLE_JSON}" \
    >"${backup_dir}/bucket-lifecycle-before.json"
  install -m 0600 "${DESIRED_LIFECYCLE_FILE}" \
    "${backup_dir}/desired-bucket-lifecycle.json"
fi

printf '%s\n' \
  "apiId=${API_ID:-absent}" \
  "readLambda=${READ_LAMBDA_NAME}" \
  "readRoleArn=${ROLE_ARN:-absent}" \
  "readRoleName=${ROLE_NAME:-absent}" \
  "readPolicyName=${READ_POLICY_NAME}" \
  "desiredReadPolicySha256=${DESIRED_POLICY_HASH}" \
  "reconcileLambda=${RECONCILE_LAMBDA}" \
  "attachDdbReadPolicy=${ATTACH_DDB_READ_POLICY}" \
  >"${backup_dir}/reconciliation-inputs.txt"
if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  install -m 0600 "${DESIRED_POLICY_FILE}" \
    "${backup_dir}/desired-read-role-inline-policy.json"
fi
chmod 0600 "${backup_dir}"/*
(cd "${backup_dir}" && sha256sum -- * >evidence-sha256.txt)
chmod 0600 "${backup_dir}/evidence-sha256.txt"
echo "Rollback evidence captured before apply: ${backup_dir}"

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
  need zip
  need openssl
  need base64
  need python3
fi

if [[ "${READ_LAMBDA_EXISTS}" != "true" && -z "${ROLE_ARN}" ]]; then
  echo "READ_LAMBDA_ROLE_ARN is required to create ${READ_LAMBDA_NAME}; refusing to infer or create an IAM role." >&2
  exit 4
fi

if [[ "${ATTACH_DDB_READ_POLICY}" == "true" && -z "${ROLE_NAME}" ]]; then
  echo "Cannot attach ${READ_POLICY_NAME} without an explicit or existing read Lambda role." >&2
  exit 4
fi

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
install -m 0600 "${HERE}/read-api-lambda.py" "${WORKDIR}/index.py"

# Compile the exact file that will be zipped. This is intentionally in the
# deployment path so source-generation regressions cannot be hidden by tests
# that compile a different representation.
python3 -m py_compile "${WORKDIR}/index.py"

(cd "${WORKDIR}" && touch -t 198001010000 index.py && zip -Xq function.zip index.py)

desired_vars_file="${WORKDIR}/desired-vars.json"
jq -n \
  --arg TABLE_NAME "${TABLE_NAME}" \
  --arg GSI_NAME gsi_geohash_time \
  --arg MAX_LIMIT 200 \
  --arg VIDEO_AWS_REGION "${VIDEO_AWS_REGION}" \
  --arg VIDEO_STREAM_PREFIX "${VIDEO_STREAM_PREFIX}" \
  --arg VIDEO_HLS_EXPIRES_SECONDS "${VIDEO_HLS_EXPIRES_SECONDS}" \
  --arg HLS_PROXY_PREFIX "${HLS_PROXY_PREFIX}" \
  --arg HLS_PROXY_FETCH_TIMEOUT_SECONDS "${HLS_PROXY_FETCH_TIMEOUT_SECONDS}" \
  --arg HLS_PROXY_PLAYLIST_MAX_BYTES "${HLS_PROXY_PLAYLIST_MAX_BYTES}" \
  --arg HLS_PROXY_SEGMENT_MAX_BYTES "${HLS_PROXY_SEGMENT_MAX_BYTES}" \
  --arg STATE_BUCKET "${STATE_BUCKET}" \
  --arg SNAPSHOT_URL_EXPIRES_SECONDS "${SNAPSHOT_URL_EXPIRES_SECONDS}" \
  --arg DEMO_VIDEOS_PREFIX "${DEMO_VIDEOS_PREFIX}" \
  --arg DEMO_VIDEO_URL_EXPIRES_SECONDS "${DEMO_VIDEO_URL_EXPIRES_SECONDS}" \
  --arg VIDEO_ONDEMAND_EXPIRES_SECONDS "${VIDEO_ONDEMAND_EXPIRES_SECONDS}" \
  --arg SITE_GEOHASH "${SITE_GEOHASH}" \
  '{$TABLE_NAME, $GSI_NAME, $MAX_LIMIT, $VIDEO_AWS_REGION, $VIDEO_STREAM_PREFIX,
    $VIDEO_HLS_EXPIRES_SECONDS, $HLS_PROXY_PREFIX,
    $HLS_PROXY_FETCH_TIMEOUT_SECONDS, $HLS_PROXY_PLAYLIST_MAX_BYTES,
    $HLS_PROXY_SEGMENT_MAX_BYTES, $STATE_BUCKET, $SNAPSHOT_URL_EXPIRES_SECONDS,
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

# Apply execution-role privileges only after the exact Lambda artifact has been
# built, compiled by Python above, and successfully created or updated. A
# package failure therefore cannot leave newly granted permissions behind.
if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${READ_POLICY_NAME}" \
    --policy-document "file://${DESIRED_POLICY_FILE}" >/dev/null
fi

if [[ "${RECONCILE_LAMBDA}" == "true" ]]; then
  aws s3api put-bucket-lifecycle-configuration \
    --bucket "${STATE_BUCKET}" \
    --expected-bucket-owner "${ACCOUNT_ID}" \
    --lifecycle-configuration "file://${DESIRED_LIFECYCLE_FILE}"
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
stage_route_settings_file="${WORKDIR}/stage-route-settings.json"
current_stage_route_settings="$(
  jq -c --arg stage "${STAGE_NAME}" \
    '.Items[]? | select(.StageName == $stage) | .RouteSettings // {}' \
    <<<"${STAGES_JSON}" | head -n 1
)"
if [[ -z "${current_stage_route_settings}" ]]; then
  current_stage_route_settings='{}'
fi
jq -n \
  --argjson current "${current_stage_route_settings}" \
  --arg route_key "GET /video/session/{camera_id}" \
  --argjson burst "${HLS_SESSION_THROTTLE_BURST}" \
  --argjson rate "${HLS_SESSION_THROTTLE_RATE}" \
  '$current + {($route_key): (($current[$route_key] // {}) + {
    ThrottlingBurstLimit: $burst,
    ThrottlingRateLimit: $rate
  })}' >"${stage_route_settings_file}"
stage_throttle_matches="$(
  jq -r \
    --arg stage "${STAGE_NAME}" \
    --arg route_key "GET /video/session/{camera_id}" \
    --argjson burst "${HLS_SESSION_THROTTLE_BURST}" \
    --argjson rate "${HLS_SESSION_THROTTLE_RATE}" \
    'any(.Items[]?;
      .StageName == $stage
      and .RouteSettings[$route_key].ThrottlingBurstLimit == $burst
      and .RouteSettings[$route_key].ThrottlingRateLimit == $rate
    )' <<<"${STAGES_JSON}"
)"
if [[ -z "${stage_auto}" ]]; then
  aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name "${STAGE_NAME}" \
    --route-settings "file://${stage_route_settings_file}" \
    --auto-deploy >/dev/null
elif [[ "${stage_auto}" != "true" || "${stage_throttle_matches}" != "true" ]]; then
  aws apigatewayv2 update-stage \
    --api-id "${API_ID}" \
    --stage-name "${STAGE_NAME}" \
    --route-settings "file://${stage_route_settings_file}" \
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

#!/usr/bin/env bash
set -euo pipefail

# Bootstrap is intentionally separate from provision-read-api.sh. It must be
# run by a separately authorized IAM administrator; the rfs-v2x-service user and
# Amplify service roles must not be granted IAM self-escalation.

ACTION="${ACTION:-plan}"
AWS_REGION="${AWS_REGION:-us-west-1}"
ACCOUNT_ID="${ACCOUNT_ID:-147229569658}"
ROLE_NAME="${ROLE_NAME:-V2XBackendDeployRole}"
POLICY_NAME="${POLICY_NAME:-V2XBackendDeployPolicy}"
TRUSTED_USER_NAME="${TRUSTED_USER_NAME:-rfs-v2x-service}"
ASSUME_USER_POLICY_NAME="${ASSUME_USER_POLICY_NAME:-AssumeV2XBackendDeployRole}"
API_ID="${API_ID:-w0j9m7dgpg}"
READ_LAMBDA_NAME="${READ_LAMBDA_NAME:-v2x-backend-read}"
CONFIRM_DELETE="${CONFIRM_DELETE:-}"
EXPECTED_CURRENT_STATE_HASH="${EXPECTED_CURRENT_STATE_HASH:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups/iam-bootstrap}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUST_POLICY_FILE="${TRUST_POLICY_FILE:-${HERE}/iam/v2x-deploy-role-trust.json}"
PERMISSIONS_POLICY_FILE="${PERMISSIONS_POLICY_FILE:-${HERE}/iam/v2x-deploy-role-policy.json}"
ASSUME_POLICY_FILE="${ASSUME_POLICY_FILE:-${HERE}/iam/rfs-user-assume-v2x-deploy-role-policy.json}"

case "$ACTION" in
  plan|review|apply|delete) ;;
  *)
    echo "ACTION must be plan, review, apply, or delete" >&2
    exit 2
    ;;
esac

command -v jq >/dev/null 2>&1 || {
  echo "Missing dependency: jq" >&2
  exit 1
}

for file in "$TRUST_POLICY_FILE" "$PERMISSIONS_POLICY_FILE" "$ASSUME_POLICY_FILE"; do
  if [[ ! -r "$file" ]] || ! jq -e . "$file" >/dev/null; then
    echo "Policy document is missing or invalid JSON: $file" >&2
    exit 2
  fi
done

expected_trusted_arn="arn:aws:iam::${ACCOUNT_ID}:user/${TRUSTED_USER_NAME}"
expected_deploy_arn="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
expected_lambda_arn="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${READ_LAMBDA_NAME}"
expected_api_root="arn:aws:apigateway:${AWS_REGION}::/apis/${API_ID}"
expected_log_group="arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${READ_LAMBDA_NAME}"

if ! jq -e --arg arn "$expected_trusted_arn" '
  .Statement | length == 1
  and .[0].Effect == "Allow"
  and .[0].Principal.AWS == $arn
  and .[0].Action == "sts:AssumeRole"' "$TRUST_POLICY_FILE" >/dev/null; then
  echo "Trust policy must trust only $expected_trusted_arn" >&2
  exit 3
fi

if ! jq -e --arg arn "$expected_deploy_arn" '
  .Statement | length == 1
  and .[0].Effect == "Allow"
  and .[0].Action == "sts:AssumeRole"
  and .[0].Resource == $arn' "$ASSUME_POLICY_FILE" >/dev/null; then
  echo "Source-role policy must allow only sts:AssumeRole on $expected_deploy_arn" >&2
  exit 3
fi

if ! jq -e \
  --arg lambda "$expected_lambda_arn" \
  --arg api "$expected_api_root" \
  --arg logGroup "$expected_log_group" '
    any(.Statement[]; .Resource == $lambda)
    and any(.Statement[]; ((.Resource | arrays) // []) | any(. == $api))
    and any(.Statement[];
      ((.Action | arrays) // []) as $actions
      | (($actions | index("cloudwatch:GetMetricStatistics")) != null)
        and .Resource == "*")
    and any(.Statement[];
      ((.Action | arrays) // []) as $actions
      | ((.Resource | arrays) // []) as $resources
      | (($actions | index("logs:FilterLogEvents")) != null)
        and (($resources | index($logGroup)) != null))
    and ([.Statement[].Action] | flatten | index("iam:PassRole") == null)
    and ([.Statement[].Action] | flatten | index("iam:*") == null)
    and ([.Statement[].Action] | flatten | index("lambda:CreateFunction") == null)
    and ([.Statement[].Action] | flatten | index("apigateway:DELETE") == null)
    and ([.Statement[].Action] | flatten | map(select(startswith("cloudwatch:")))
      | all(. == "cloudwatch:GetMetricData"
        or . == "cloudwatch:GetMetricStatistics"
        or . == "cloudwatch:ListMetrics"))
    and ([.Statement[].Action] | flatten | map(select(startswith("logs:")))
      | all(. == "logs:DescribeLogStreams"
        or . == "logs:FilterLogEvents"
        or . == "logs:GetLogEvents"))' \
  "$PERMISSIONS_POLICY_FILE" >/dev/null; then
  echo "Permissions policy is not the reviewed existing-resource-only V2X policy." >&2
  exit 3
fi

echo "Dedicated V2X deploy-role reconciliation:"
echo "  action=$ACTION"
echo "  role=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo "  trust=$expected_trusted_arn (only)"
echo "  sourceUserPolicy=${TRUSTED_USER_NAME}/${ASSUME_USER_POLICY_NAME} (assume only this deploy role)"
echo "  api=$API_ID region=$AWS_REGION"
echo "  lambda=$READ_LAMBDA_NAME"
echo "  observability=read-only CloudWatch metrics and /aws/lambda/$READ_LAMBDA_NAME logs"
echo "  iamPassRole=omitted (existing Lambda keeps its role)"
echo "  createFunction=denied"
echo "  amplifyServiceRole=unchanged"

if [[ "$ACTION" == "plan" ]]; then
  echo "  planOnly=true (no AWS or persistent filesystem writes)"
  jq . "$TRUST_POLICY_FILE"
  jq . "$PERMISSIONS_POLICY_FILE"
  jq . "$ASSUME_POLICY_FILE"
  exit 0
fi

command -v aws >/dev/null 2>&1 || {
  echo "Missing dependency: aws" >&2
  exit 1
}
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
state_dir="${WORKDIR}/current-state"
install -d -m 0700 "$state_dir"

caller_account="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$caller_account" != "$ACCOUNT_ID" ]]; then
  echo "Authenticated account is $caller_account, expected $ACCOUNT_ID" >&2
  exit 4
fi
if ! aws iam get-user --user-name "$TRUSTED_USER_NAME" --output json \
    >"${state_dir}/trusted-user.json"; then
  echo "Trusted source user $TRUSTED_USER_NAME is unavailable; refusing any IAM mutation." >&2
  exit 4
fi

source_policy_exists=false
source_policy_error="${WORKDIR}/source-policy.err"
if aws iam get-user-policy \
    --user-name "$TRUSTED_USER_NAME" \
    --policy-name "$ASSUME_USER_POLICY_NAME" \
    --output json >"${state_dir}/source-assume-policy.json" 2>"$source_policy_error"; then
  source_policy_exists=true
elif ! grep -q 'NoSuchEntity' "$source_policy_error"; then
  cat "$source_policy_error" >&2
  exit 7
fi
printf '%s\n' "$source_policy_exists" >"${state_dir}/source-assume-policy-existed.txt"

role_exists=false
role_error="${WORKDIR}/role.err"
if aws iam get-role --role-name "$ROLE_NAME" --output json \
    >"${state_dir}/role.json" 2>"$role_error"; then
  role_exists=true
elif ! grep -q 'NoSuchEntity' "$role_error"; then
  cat "$role_error" >&2
  exit 7
fi
printf '%s\n' "$role_exists" >"${state_dir}/role-existed.txt"

policy_exists=false
if [[ "$role_exists" == "true" ]]; then
  aws iam list-role-tags --role-name "$ROLE_NAME" --output json >"${state_dir}/tags.json"
  aws iam list-role-policies --role-name "$ROLE_NAME" --output json >"${state_dir}/role-policies.json"
  aws iam list-attached-role-policies --role-name "$ROLE_NAME" --output json >"${state_dir}/attached-policies.json"
  aws iam list-instance-profiles-for-role --role-name "$ROLE_NAME" --output json >"${state_dir}/instance-profiles.json"
  policy_error="${WORKDIR}/role-policy.err"
  if aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" \
      --output json >"${state_dir}/inline-policy.json" 2>"$policy_error"; then
    policy_exists=true
  elif ! grep -q 'NoSuchEntity' "$policy_error"; then
    cat "$policy_error" >&2
    exit 7
  fi
else
  printf '{"Tags":[]}\n' >"${state_dir}/tags.json"
  printf '{"PolicyNames":[]}\n' >"${state_dir}/role-policies.json"
  printf '{"AttachedPolicies":[]}\n' >"${state_dir}/attached-policies.json"
  printf '{"InstanceProfiles":[]}\n' >"${state_dir}/instance-profiles.json"
fi
printf '%s\n' "$policy_exists" >"${state_dir}/policy-existed.txt"

current_state_file="${state_dir}/iam-current-state.json"
jq -nS \
  --slurpfile user "${state_dir}/trusted-user.json" \
  --argjson sourcePolicyExists "$source_policy_exists" \
  --argjson sourcePolicy "$([[ "$source_policy_exists" == "true" ]] && cat "${state_dir}/source-assume-policy.json" || printf '{}')" \
  --argjson roleExists "$role_exists" \
  --argjson role "$([[ "$role_exists" == "true" ]] && cat "${state_dir}/role.json" || printf '{}')" \
  --argjson policyExists "$policy_exists" \
  --argjson inlinePolicy "$([[ "$policy_exists" == "true" ]] && cat "${state_dir}/inline-policy.json" || printf '{}')" \
  --slurpfile tags "${state_dir}/tags.json" \
  --slurpfile rolePolicies "${state_dir}/role-policies.json" \
  --slurpfile attached "${state_dir}/attached-policies.json" \
  --slurpfile profiles "${state_dir}/instance-profiles.json" \
  '{
    trustedUser: $user[0],
    sourcePolicyExists: $sourcePolicyExists,
    sourcePolicy: $sourcePolicy,
    roleExists: $roleExists,
    role: $role,
    managedInlinePolicyExists: $policyExists,
    managedInlinePolicy: $inlinePolicy,
    roleTags: $tags[0],
    rolePolicies: $rolePolicies[0],
    attachedPolicies: $attached[0],
    instanceProfiles: $profiles[0]
  }' >"$current_state_file"
current_state_hash="$(sha256sum "$current_state_file" | awk '{print $1}')"

echo "  observedRoleExists=$role_exists"
echo "  observedSourcePolicyExists=$source_policy_exists"
echo "  observedManagedPolicyExists=$policy_exists"
echo "  currentStateHash=$current_state_hash"

if [[ "$ACTION" == "review" ]]; then
  echo "  reviewOnly=true (AWS reads only; no persistent filesystem writes)"
  jq . "$current_state_file"
  exit 0
fi

if [[ "$ACTION" == "apply" ]]; then
  if [[ -z "$EXPECTED_CURRENT_STATE_HASH" ]]; then
    echo "ACTION=apply requires EXPECTED_CURRENT_STATE_HASH from ACTION=review." >&2
    exit 4
  fi
  if [[ "$EXPECTED_CURRENT_STATE_HASH" != "$current_state_hash" ]]; then
    echo "IAM state hash is $current_state_hash; expected $EXPECTED_CURRENT_STATE_HASH. Refusing to mutate IAM." >&2
    exit 4
  fi
fi

delete_inline_policy_if_present() {
  local role_name="$1"
  local policy_name="$2"
  local lookup
  if lookup="$(aws iam get-role-policy \
      --role-name "$role_name" \
      --policy-name "$policy_name" \
      --output json 2>&1)"; then
    aws iam delete-role-policy \
      --role-name "$role_name" \
      --policy-name "$policy_name" >/dev/null
  elif grep -q 'NoSuchEntity' <<<"$lookup"; then
    return 0
  else
    printf '%s\n' "$lookup" >&2
    return 1
  fi
}

delete_user_inline_policy_if_present() {
  local user_name="$1"
  local policy_name="$2"
  local lookup
  if lookup="$(aws iam get-user-policy \
      --user-name "$user_name" \
      --policy-name "$policy_name" \
      --output json 2>&1)"; then
    aws iam delete-user-policy \
      --user-name "$user_name" \
      --policy-name "$policy_name" >/dev/null
  elif grep -q 'NoSuchEntity' <<<"$lookup"; then
    return 0
  else
    printf '%s\n' "$lookup" >&2
    return 1
  fi
}

if [[ "$ACTION" == "delete" ]]; then
  if [[ "$CONFIRM_DELETE" != "$ROLE_NAME" ]]; then
    echo "Deletion requires CONFIRM_DELETE=$ROLE_NAME" >&2
    exit 5
  fi
  if ! role_lookup="$(aws iam get-role --role-name "$ROLE_NAME" --output json 2>&1)"; then
    if grep -q 'NoSuchEntity' <<<"$role_lookup"; then
      delete_user_inline_policy_if_present "$TRUSTED_USER_NAME" "$ASSUME_USER_POLICY_NAME"
      echo "Role $ROLE_NAME does not exist; removed its source-user assume policy if present."
      exit 0
    fi
    printf '%s\n' "$role_lookup" >&2
    exit 6
  fi

  other_inline="$(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames' --output json \
    | jq -r --arg managed "$POLICY_NAME" '[.[] | select(. != $managed)] | length')"
  attached_count="$(aws iam list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies' --output json | jq 'length')"
  profile_count="$(aws iam list-instance-profiles-for-role --role-name "$ROLE_NAME" --query 'InstanceProfiles' --output json | jq 'length')"
  if [[ "$other_inline" != "0" || "$attached_count" != "0" || "$profile_count" != "0" ]]; then
    echo "Refusing to delete $ROLE_NAME: it has unmanaged policies or instance profiles." >&2
    exit 6
  fi
  delete_user_inline_policy_if_present "$TRUSTED_USER_NAME" "$ASSUME_USER_POLICY_NAME"
  delete_inline_policy_if_present "$ROLE_NAME" "$POLICY_NAME"
  aws iam delete-role --role-name "$ROLE_NAME" >/dev/null
  echo "Deleted $ROLE_NAME and its narrowly scoped source-user assume policy; the source user and Amplify role remain."
  exit 0
fi

install -d -m 0700 "$BACKUP_ROOT"
backup_dir="${BACKUP_ROOT%/}/${ROLE_NAME}-$(date -u +%Y%m%dT%H%M%SZ)-${current_state_hash:0:12}"
install -d -m 0700 "$backup_dir"
for evidence_file in "${state_dir}"/*; do
  install -m 0600 "$evidence_file" "${backup_dir}/$(basename "$evidence_file")"
done
printf '%s\n' "$current_state_hash" >"${backup_dir}/current-state.sha256"
chmod 0600 "${backup_dir}/current-state.sha256"

if [[ "$role_exists" == "true" ]]; then
  aws iam update-assume-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-document "file://${TRUST_POLICY_FILE}" >/dev/null
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --description "Dedicated least-privilege V2X API/Lambda deployment role" \
    --assume-role-policy-document "file://${TRUST_POLICY_FILE}" \
    --tags Key=managed-by,Value=v2x-backend Key=purpose,Value=api-lambda-deploy >/dev/null
fi

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "file://${PERMISSIONS_POLICY_FILE}" >/dev/null
aws iam put-user-policy \
  --user-name "$TRUSTED_USER_NAME" \
  --policy-name "$ASSUME_USER_POLICY_NAME" \
  --policy-document "file://${ASSUME_POLICY_FILE}" >/dev/null
aws iam tag-role \
  --role-name "$ROLE_NAME" \
  --tags Key=managed-by,Value=v2x-backend Key=purpose,Value=api-lambda-deploy >/dev/null

echo "Reconciled $ROLE_NAME. Rollback evidence: $backup_dir"
echo "Use API_ID=$API_ID RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false with provision-read-api.sh."

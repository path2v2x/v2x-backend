#!/usr/bin/env bash
set -euo pipefail
umask 077

# Reconnects only the Amplify repository. It never reads, infers, attaches, or
# updates an IAM service role. Repository authorization is proven before any
# separate IAM recovery is considered.

ACTION="${ACTION:-plan}"
AWS_REGION="${AWS_REGION:-us-west-2}"
AMPLIFY_APP_ID="${AMPLIFY_APP_ID:-d1ugco1rmb7yjj}"
AMPLIFY_BRANCH="${AMPLIFY_BRANCH:-main}"
EXPECTED_CURRENT_REPOSITORY="${EXPECTED_CURRENT_REPOSITORY:-https://github.com/michaelvu1207/v2x-backend}"
CANONICAL_REPOSITORY="${CANONICAL_REPOSITORY:-https://github.com/path2v2x/v2x-backend}"
ROLLBACK_METADATA_FILE="${ROLLBACK_METADATA_FILE:-}"
TOKEN_FROM_STDIN="${TOKEN_FROM_STDIN:-false}"
START_RELEASE="${START_RELEASE:-false}"
WAIT_FOR_RELEASE="${WAIT_FOR_RELEASE:-true}"
EXPECTED_CURRENT_HASH="${EXPECTED_CURRENT_HASH:-}"
BACKUP_DIR="${BACKUP_DIR:-/home/path/V2XCarla/v2x-backend-backups/amplify-repository}"

case "$ACTION" in
  plan|apply|rollback) ;;
  *)
    echo "ACTION must be plan, apply, or rollback" >&2
    exit 2
    ;;
esac
for boolean_name in TOKEN_FROM_STDIN START_RELEASE WAIT_FOR_RELEASE; do
  value="${!boolean_name}"
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "$boolean_name must be true or false" >&2
    exit 2
  fi
done
for dependency in aws jq sha256sum; do
  command -v "$dependency" >/dev/null 2>&1 || {
    echo "Missing dependency: $dependency" >&2
    exit 1
  }
done

export AWS_REGION
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
current_file="$WORKDIR/current-app.json"

aws amplify get-app --app-id "$AMPLIFY_APP_ID" --output json >"$current_file"
current_repository="$(jq -er '.app.repository | select(type == "string" and length > 0)' "$current_file")"
current_update_time="$(jq -r '.app.updateTime // "unknown"' "$current_file")"
current_hash="$(jq -Sc '.app' "$current_file" | sha256sum | awk '{print $1}')"

if [[ -n "$EXPECTED_CURRENT_HASH" && "$EXPECTED_CURRENT_HASH" != "$current_hash" ]]; then
  echo "Amplify app metadata hash is $current_hash; expected $EXPECTED_CURRENT_HASH. Refusing to continue." >&2
  exit 3
fi

desired_repository="$CANONICAL_REPOSITORY"
if [[ "$ACTION" == "rollback" ]]; then
  if [[ -z "$ROLLBACK_METADATA_FILE" || ! -r "$ROLLBACK_METADATA_FILE" ]]; then
    echo "ACTION=rollback requires a readable ROLLBACK_METADATA_FILE" >&2
    exit 4
  fi
  desired_repository="$(jq -er '.app.repository | select(type == "string" and length > 0)' "$ROLLBACK_METADATA_FILE")"
  if [[ "$desired_repository" != "$EXPECTED_CURRENT_REPOSITORY" ]]; then
    echo "Rollback metadata repository is $desired_repository, expected reviewed old repository $EXPECTED_CURRENT_REPOSITORY" >&2
    exit 4
  fi
  if [[ "$current_repository" != "$CANONICAL_REPOSITORY" ]]; then
    echo "Rollback expects current repository $CANONICAL_REPOSITORY; observed $current_repository" >&2
    exit 4
  fi
elif [[ "$current_repository" != "$EXPECTED_CURRENT_REPOSITORY" && \
        "$current_repository" != "$CANONICAL_REPOSITORY" ]]; then
  echo "Current repository is $current_repository; expected old $EXPECTED_CURRENT_REPOSITORY or canonical $CANONICAL_REPOSITORY." >&2
  exit 3
fi

echo "Amplify repository reconciliation:"
echo "  action=$ACTION"
echo "  app=$AMPLIFY_APP_ID branch=$AMPLIFY_BRANCH region=$AWS_REGION"
echo "  currentRepository=$current_repository"
echo "  desiredRepository=$desired_repository"
echo "  currentMetadataHash=$current_hash"
echo "  currentUpdateTime=$current_update_time"
echo "  serviceRoleMutation=none"
echo "  startRelease=$START_RELEASE"

if [[ "$ACTION" == "plan" ]]; then
  echo "  planOnly=true (AWS reads only; no token required)"
  exit 0
fi

token=""
if [[ "$current_repository" != "$desired_repository" ]]; then
  if [[ "$TOKEN_FROM_STDIN" == "true" ]]; then
    IFS= read -r token
  else
    token="${AMPLIFY_GITHUB_ACCESS_TOKEN:-}"
  fi
  unset AMPLIFY_GITHUB_ACCESS_TOKEN
  if [[ -z "$token" ]]; then
    echo "Repository update requires AMPLIFY_GITHUB_ACCESS_TOKEN or TOKEN_FROM_STDIN=true." >&2
    exit 5
  fi
fi

install -d -m 0700 "$BACKUP_DIR"
backup_file="${BACKUP_DIR%/}/${AMPLIFY_APP_ID}-$(date -u +%Y%m%dT%H%M%SZ)-${current_hash}.json"
install -m 0600 "$current_file" "$backup_file"
branch_backup="${backup_file%.json}-branch.json"
aws amplify get-branch \
  --app-id "$AMPLIFY_APP_ID" \
  --branch-name "$AMPLIFY_BRANCH" \
  --output json >"$branch_backup"
chmod 0600 "$branch_backup"

if [[ "$current_repository" != "$desired_repository" ]]; then
  update_input="$WORKDIR/update-app.json"
  jq -n \
    --arg AppId "$AMPLIFY_APP_ID" \
    --arg Repository "$desired_repository" \
    --arg AccessToken "$token" \
    '{$AppId, $Repository, $AccessToken}' >"$update_input"
  chmod 0600 "$update_input"
  token=""
  aws amplify update-app --cli-input-json "file://${update_input}" >/dev/null
  rm -f "$update_input"
fi
token=""

verified_repository="$(aws amplify get-app \
  --app-id "$AMPLIFY_APP_ID" \
  --query 'app.repository' \
  --output text)"
if [[ "$verified_repository" != "$desired_repository" ]]; then
  echo "Repository verification failed: observed $verified_repository" >&2
  echo "Rollback metadata: $backup_file" >&2
  exit 6
fi
echo "Verified Amplify repository metadata: $verified_repository"

if [[ "$START_RELEASE" != "true" ]]; then
  echo "Repository metadata updated, but clone/build authorization remains unproven until an explicit release succeeds."
  echo "Rollback metadata: $backup_file"
  exit 0
fi

job_id="$(aws amplify start-job \
  --app-id "$AMPLIFY_APP_ID" \
  --branch-name "$AMPLIFY_BRANCH" \
  --job-type RELEASE \
  --query 'jobSummary.jobId' \
  --output text)"
echo "Started repository authorization/release job: $job_id"
if [[ "$WAIT_FOR_RELEASE" != "true" ]]; then
  echo "Rollback metadata: $backup_file"
  exit 0
fi

for _ in $(seq 1 90); do
  status="$(aws amplify get-job \
    --app-id "$AMPLIFY_APP_ID" \
    --branch-name "$AMPLIFY_BRANCH" \
    --job-id "$job_id" \
    --query 'job.summary.status' \
    --output text 2>/dev/null || true)"
  echo "Amplify job $job_id: $status"
  case "$status" in
    SUCCEED)
      echo "Canonical repository clone/build/release succeeded."
      echo "Rollback metadata: $backup_file"
      exit 0
      ;;
    FAILED|CANCELLED)
      echo "Repository authorization/release failed with $status; rollback metadata: $backup_file" >&2
      exit 7
      ;;
  esac
  sleep 10
done

echo "Timed out waiting for repository authorization/release; rollback metadata: $backup_file" >&2
exit 124

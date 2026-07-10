#!/usr/bin/env bash
set -euo pipefail

# Compares the supervised perception Quick Tunnel with public Amplify runtime
# config. Repair is double-gated and rate-limited because it starts a production
# Amplify release; the safe default is observational failure only.

FRONTEND_CONFIG_URL="${FRONTEND_CONFIG_URL:-https://path2v2x.net/config.json}"
PERCEPTION_LOG_FILE="${PERCEPTION_LOG_FILE:-${LOG_FILE:-/tmp/v2x-perception-cloudflared.log}}"
PERCEPTION_PUBLIC_URL="${PERCEPTION_PUBLIC_URL:-${PUBLIC_HOSTNAME:+https://${PUBLIC_HOSTNAME}}}"
PERCEPTION_STREAM_PATH_TEMPLATE="${PERCEPTION_STREAM_PATH_TEMPLATE:-/streams/{camera_id}.mjpg}"
PERCEPTION_LINK_HEALTH_REPAIR="${PERCEPTION_LINK_HEALTH_REPAIR:-false}"
AMPLIFY_RELEASE_ENABLED="${AMPLIFY_RELEASE_ENABLED:-false}"
MIN_RELEASE_INTERVAL_SECONDS="${MIN_RELEASE_INTERVAL_SECONDS:-1800}"
STATE_DIR="${STATE_DIR:-/var/lib/v2x-perception-link-health}"
PUBLISHER="${PUBLISHER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/publish-amplify-runtime-config.sh}"

for boolean_name in PERCEPTION_LINK_HEALTH_REPAIR AMPLIFY_RELEASE_ENABLED; do
  value="${!boolean_name}"
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "$boolean_name must be true or false" >&2
    exit 2
  fi
done
if ! [[ "$MIN_RELEASE_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIN_RELEASE_INTERVAL_SECONDS must be a positive integer" >&2
  exit 2
fi
for dependency in curl jq flock; do
  command -v "$dependency" >/dev/null 2>&1 || {
    echo "Missing dependency: $dependency" >&2
    exit 1
  }
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
config_file="$WORKDIR/config.json"
cache_buster="$(date +%s)"
separator='?'
[[ "$FRONTEND_CONFIG_URL" == *\?* ]] && separator='&'
curl -fsSL --connect-timeout 10 --max-time 20 \
  "${FRONTEND_CONFIG_URL}${separator}_perception_link_check=${cache_buster}" \
  -o "$config_file"

public_url="$(jq -r '.perceptionStreamBaseUrl // ""' "$config_file")"
public_url="${public_url%/}"
candidate_url="$PERCEPTION_PUBLIC_URL"
if [[ -z "$candidate_url" ]]; then
  if [[ ! -f "$PERCEPTION_LOG_FILE" ]]; then
    echo "Perception tunnel log is absent: $PERCEPTION_LOG_FILE" >&2
    exit 1
  fi
  candidate_url="$(grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$PERCEPTION_LOG_FILE" | tail -n 1 || true)"
fi
candidate_url="${candidate_url%/}"
if [[ -z "$candidate_url" ]]; then
  echo "No current Quick Tunnel URL found in $PERCEPTION_LOG_FILE" >&2
  exit 1
fi
if [[ "$candidate_url" == *"@"* || "$candidate_url" == *"?"* || "$candidate_url" == *"#"* || \
      ! "$candidate_url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
  echo "Candidate perception endpoint must be a credential-free HTTPS origin URL." >&2
  exit 1
fi

health_file="$WORKDIR/health.json"
curl -fsS --connect-timeout 10 --max-time 20 "${candidate_url}/health" >"$health_file"
if ! jq -e '
  .status == "ok"
  and .ready == true
  and ([.cameras.ch1, .cameras.ch2, .cameras.ch3, .cameras.ch4]
       | all(.fresh == true and .state == "streaming"))' "$health_file" >/dev/null; then
  jq '{status,ready,cameras,frames}' "$health_file" >&2 || true
  echo "Supervised perception endpoint is not fresh/ready on all four cameras; refusing parity or publication." >&2
  exit 1
fi
for camera_id in ch1 ch2 ch3 ch4; do
  stream_path="${PERCEPTION_STREAM_PATH_TEMPLATE/\{camera_id\}/$camera_id}"
  curl -fsSI --connect-timeout 10 --max-time 20 "${candidate_url}${stream_path}" >/dev/null
done

if [[ "$public_url" == "$candidate_url" ]]; then
  echo "Perception public endpoint matches the supervised tunnel and all four feeds are fresh."
  exit 0
fi

echo "Perception public endpoint differs from the healthy supervised Quick Tunnel."
if [[ "$PERCEPTION_LINK_HEALTH_REPAIR" != "true" || "$AMPLIFY_RELEASE_ENABLED" != "true" ]]; then
  echo "Automatic release is disabled; enable both gates only after canonical Amplify repository/IAM acceptance." >&2
  exit 1
fi

install -d -m 0700 "$STATE_DIR"
exec 9>"$STATE_DIR/repair.lock"
if ! flock -n 9; then
  echo "Another perception endpoint repair is already running."
  exit 0
fi

now_epoch="$(date +%s)"
last_attempt=0
if [[ -r "$STATE_DIR/last-attempt-epoch" ]]; then
  read -r last_attempt <"$STATE_DIR/last-attempt-epoch" || last_attempt=0
fi
if [[ "$last_attempt" =~ ^[0-9]+$ ]] && \
   (( now_epoch - last_attempt < MIN_RELEASE_INTERVAL_SECONDS )); then
  remaining=$((MIN_RELEASE_INTERVAL_SECONDS - (now_epoch - last_attempt)))
  echo "Perception release cooldown is active for another ${remaining}s; refusing a release storm." >&2
  exit 1
fi

plan_file="$WORKDIR/publisher-plan.txt"
ACTION=plan \
UPDATE_DRIVE=false \
UPDATE_PERCEPTION=true \
PERCEPTION_STREAM_BASE_URL="$candidate_url" \
PERCEPTION_STREAM_PATH_TEMPLATE="$PERCEPTION_STREAM_PATH_TEMPLATE" \
VALIDATE_PERCEPTION_ENDPOINT=true \
START_RELEASE=false \
"$PUBLISHER" >"$plan_file"
current_hash="$(sed -n 's/^[[:space:]]*currentHash=//p' "$plan_file" | head -n 1)"
if [[ -z "$current_hash" ]]; then
  echo "Publisher plan did not return currentHash" >&2
  exit 1
fi

# Record attempts, including failed releases, before mutation so a broken repo
# cannot trigger a release every five minutes. Publisher rollback is preserved.
printf '%s\n' "$now_epoch" >"$STATE_DIR/last-attempt-epoch"
ACTION=publish \
UPDATE_DRIVE=false \
UPDATE_PERCEPTION=true \
PERCEPTION_STREAM_BASE_URL="$candidate_url" \
PERCEPTION_STREAM_PATH_TEMPLATE="$PERCEPTION_STREAM_PATH_TEMPLATE" \
VALIDATE_PERCEPTION_ENDPOINT=true \
EXPECTED_CURRENT_HASH="$current_hash" \
START_RELEASE=true \
WAIT_FOR_DEPLOY=true \
FORCE_RELEASE=true \
"$PUBLISHER"

published=false
for _ in $(seq 1 12); do
  observed="$(curl -fsSL --connect-timeout 10 --max-time 20 \
    "${FRONTEND_CONFIG_URL}${separator}_perception_publish_verify=$(date +%s)" \
    | jq -r '.perceptionStreamBaseUrl // ""' || true)"
  if [[ "${observed%/}" == "$candidate_url" ]]; then
    published=true
    break
  fi
  sleep 5
done
if [[ "$published" != "true" ]]; then
  echo "Amplify job succeeded but public config did not converge to the supervised perception endpoint." >&2
  exit 1
fi
printf '%s\n' "$(date +%s)" >"$STATE_DIR/last-success-epoch"
echo "Published and publicly verified the supervised perception endpoint through Amplify."

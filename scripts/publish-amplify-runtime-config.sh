#!/usr/bin/env bash
set -euo pipefail

# Reconciles the runtime endpoint variables consumed by the Amplify build. The
# safe default is a read-only plan. A publish/rollback preserves the complete
# existing environment map and writes a mode-0600 rollback snapshot first.

AMPLIFY_REGION="${AMPLIFY_REGION:-us-west-2}"
AMPLIFY_APP_ID="${AMPLIFY_APP_ID:-d1ugco1rmb7yjj}"
AMPLIFY_BRANCH="${AMPLIFY_BRANCH:-main}"
ACTION="${ACTION:-plan}"
UPDATE_DRIVE="${UPDATE_DRIVE:-true}"
UPDATE_PERCEPTION="${UPDATE_PERCEPTION:-true}"
DRIVE_LOG_FILE="${DRIVE_LOG_FILE:-/tmp/v2x-cloudflared.log}"
PERCEPTION_LOG_FILE="${PERCEPTION_LOG_FILE:-${LOG_FILE:-/tmp/v2x-perception-cloudflared.log}}"
DRIVE_WS_URL="${DRIVE_WS_URL:-}"
TAILSCALE_DRIVE_WS_URL="${TAILSCALE_DRIVE_WS_URL:-wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net}"
PERCEPTION_STREAM_BASE_URL="${PERCEPTION_STREAM_BASE_URL:-}"
PERCEPTION_STREAM_PATH_TEMPLATE="${PERCEPTION_STREAM_PATH_TEMPLATE:-/streams/{camera_id}.mjpg}"
BACKUP_DIR="${BACKUP_DIR:-/home/path/V2XCarla/v2x-backend-backups/amplify-runtime-config}"
ROLLBACK_ENV_FILE="${ROLLBACK_ENV_FILE:-}"
ROLLBACK_ENDPOINT_MODE="${ROLLBACK_ENDPOINT_MODE:-preserve-current}"
EXPECTED_CURRENT_HASH="${EXPECTED_CURRENT_HASH:-}"
# Updating branch variables is separable from releasing connected-repository
# source. Keep releases opt-in until canonical repository/IAM parity is proven.
START_RELEASE="${START_RELEASE:-false}"
WAIT_FOR_DEPLOY="${WAIT_FOR_DEPLOY:-true}"
FORCE_RELEASE="${FORCE_RELEASE:-false}"
VALIDATE_PERCEPTION_ENDPOINT="${VALIDATE_PERCEPTION_ENDPOINT:-true}"
VALIDATE_DRIVE_ENDPOINT="${VALIDATE_DRIVE_ENDPOINT:-true}"
PYTHON_BIN="${PYTHON_BIN:-/home/path/venvs/vw-scenario/bin/python}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need aws
need jq
need sha256sum
need curl

case "$ACTION" in
  plan|publish|rollback) ;;
  *)
    echo "ACTION must be plan, publish, or rollback" >&2
    exit 2
    ;;
esac
case "$ROLLBACK_ENDPOINT_MODE" in
  preserve-current|exact-named) ;;
  *)
    echo "ROLLBACK_ENDPOINT_MODE must be preserve-current or exact-named" >&2
    exit 2
    ;;
esac

for boolean_name in UPDATE_DRIVE UPDATE_PERCEPTION START_RELEASE WAIT_FOR_DEPLOY FORCE_RELEASE \
    VALIDATE_PERCEPTION_ENDPOINT VALIDATE_DRIVE_ENDPOINT; do
  boolean_value="${!boolean_name}"
  if [[ "$boolean_value" != "true" && "$boolean_value" != "false" ]]; then
    echo "$boolean_name must be true or false (got: $boolean_value)" >&2
    exit 2
  fi
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
current_file="$WORKDIR/current.json"
desired_file="$WORKDIR/desired.json"

extract_latest_quick_tunnel_url() {
  local log_file="$1"
  [[ -f "$log_file" ]] || return 1
  grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$log_file" | tail -n 1
}

normalize_ws_url() {
  local url="$1"
  url="${url/#https:/wss:}"
  url="${url/#http:/ws:}"
  printf '%s\n' "${url%/}"
}

normalize_http_url() {
  local url="$1"
  url="${url/#wss:/https:}"
  url="${url/#ws:/http:}"
  printf '%s\n' "${url%/}"
}

validate_public_url() {
  local label="$1"
  local url="$2"
  local scheme="$3"
  if [[ "$url" == *"@"* || "$url" == *"?"* || "$url" == *"#"* || \
      "$url" == *[[:space:]]* || ! "$url" =~ ^${scheme}://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
    echo "$label is not a credential-free ${scheme} URL: $url" >&2
    exit 3
  fi
}

canonical_hash() {
  jq -Sc . "$1" | sha256sum | awk '{print $1}'
}

AWS_REGION="$AMPLIFY_REGION" aws amplify get-branch \
  --app-id "$AMPLIFY_APP_ID" \
  --branch-name "$AMPLIFY_BRANCH" \
  --output json \
  | jq '.branch.environmentVariables // {}' >"$current_file"

if ! jq -e 'type == "object"' "$current_file" >/dev/null; then
  echo "Amplify branch environmentVariables is not an object" >&2
  exit 3
fi

current_hash="$(canonical_hash "$current_file")"
if [[ -n "$EXPECTED_CURRENT_HASH" && "$EXPECTED_CURRENT_HASH" != "$current_hash" ]]; then
  echo "Amplify environment hash is $current_hash; expected $EXPECTED_CURRENT_HASH. Refusing to continue." >&2
  exit 4
fi

if [[ "$ACTION" == "rollback" ]]; then
  if [[ -z "$ROLLBACK_ENV_FILE" || ! -r "$ROLLBACK_ENV_FILE" ]]; then
    echo "ACTION=rollback requires a readable ROLLBACK_ENV_FILE" >&2
    exit 5
  fi
  rollback_file="$WORKDIR/rollback.json"
  jq -e 'select(type == "object")' "$ROLLBACK_ENV_FILE" >"$rollback_file"
  if [[ "$ROLLBACK_ENDPOINT_MODE" == "preserve-current" ]]; then
    jq -s '
      .[0] + (.[1] | with_entries(select(
        (.key == "VITE_CLOUDFLARE_DRIVE_WS_URL")
        or (.key == "VITE_TAILSCALE_DRIVE_WS_URL")
        or (.key == "PERCEPTION_STREAM_BASE_URL")
        or (.key == "PERCEPTION_STREAM_URLS")
      )))' \
      "$rollback_file" "$current_file" >"$desired_file"
  else
    if jq -e '[
        .VITE_CLOUDFLARE_DRIVE_WS_URL,
        .PERCEPTION_STREAM_BASE_URL,
        (.PERCEPTION_STREAM_URLS // {} | .[])
      ] | any(strings | contains(".trycloudflare.com"))' \
      "$rollback_file" >/dev/null; then
      echo "exact-named rollback contains a process-scoped Quick Tunnel URL; refusing to restore a dead endpoint." >&2
      exit 5
    fi
    cp "$rollback_file" "$desired_file"
  fi
else
  cp "$current_file" "$desired_file"

  if [[ "$UPDATE_DRIVE" == "true" ]]; then
    if [[ -z "$DRIVE_WS_URL" ]]; then
      DRIVE_WS_URL="$(extract_latest_quick_tunnel_url "$DRIVE_LOG_FILE" || true)"
    fi
    if [[ -z "$DRIVE_WS_URL" ]]; then
      echo "No active Drive Quick Tunnel URL found in $DRIVE_LOG_FILE; set DRIVE_WS_URL explicitly." >&2
      exit 6
    fi
    DRIVE_WS_URL="$(normalize_ws_url "$DRIVE_WS_URL")"
    TAILSCALE_DRIVE_WS_URL="$(normalize_ws_url "$TAILSCALE_DRIVE_WS_URL")"
    validate_public_url DRIVE_WS_URL "$DRIVE_WS_URL" wss
    validate_public_url TAILSCALE_DRIVE_WS_URL "$TAILSCALE_DRIVE_WS_URL" wss
    if [[ "$VALIDATE_DRIVE_ENDPOINT" == "true" ]]; then
      if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "Drive endpoint validation requires executable PYTHON_BIN=$PYTHON_BIN" >&2
        exit 6
      fi
      CANDIDATE_DRIVE_WS_URL="$DRIVE_WS_URL" "$PYTHON_BIN" <<'PY'
import base64
import hashlib
import os
import socket
import ssl
from urllib.parse import urlparse

parsed = urlparse(os.environ["CANDIDATE_DRIVE_WS_URL"])
if parsed.scheme != "wss" or not parsed.hostname:
    raise SystemExit("candidate Drive endpoint is not a wss URL")
port = parsed.port or 443
path = parsed.path or "/"
if parsed.query:
    path += "?" + parsed.query
key = base64.b64encode(os.urandom(16)).decode("ascii")
request = (
    f"GET {path} HTTP/1.1\r\n"
    f"Host: {parsed.netloc}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "Origin: https://path2v2x.net\r\n\r\n"
).encode("ascii")
context = ssl.create_default_context()
with socket.create_connection((parsed.hostname, port), timeout=15) as raw:
    with context.wrap_socket(raw, server_hostname=parsed.hostname) as sock:
        sock.settimeout(15)
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
headers = response.decode("iso-8859-1", errors="replace").split("\r\n")
if not headers or " 101 " not in headers[0]:
    raise SystemExit("candidate Drive endpoint rejected the WebSocket upgrade")
accept = next((line.split(":", 1)[1].strip() for line in headers[1:]
               if line.lower().startswith("sec-websocket-accept:")), "")
expected = base64.b64encode(hashlib.sha1(
    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
).digest()).decode("ascii")
if accept != expected:
    raise SystemExit("candidate Drive endpoint returned an invalid WebSocket accept key")
PY
    fi
    jq \
      --arg drive "$DRIVE_WS_URL" \
      --arg tailscale "$TAILSCALE_DRIVE_WS_URL" \
      '. + {
        VITE_CLOUDFLARE_DRIVE_WS_URL: $drive,
        VITE_TAILSCALE_DRIVE_WS_URL: $tailscale
      }' "$desired_file" >"$WORKDIR/next.json"
    mv "$WORKDIR/next.json" "$desired_file"
  fi

  if [[ "$UPDATE_PERCEPTION" == "true" ]]; then
    if [[ -z "$PERCEPTION_STREAM_BASE_URL" ]]; then
      PERCEPTION_STREAM_BASE_URL="$(extract_latest_quick_tunnel_url "$PERCEPTION_LOG_FILE" || true)"
    fi
    if [[ -z "$PERCEPTION_STREAM_BASE_URL" ]]; then
      echo "No active Perception Quick Tunnel URL found in $PERCEPTION_LOG_FILE; set PERCEPTION_STREAM_BASE_URL explicitly." >&2
      exit 6
    fi
    PERCEPTION_STREAM_BASE_URL="$(normalize_http_url "$PERCEPTION_STREAM_BASE_URL")"
    validate_public_url PERCEPTION_STREAM_BASE_URL "$PERCEPTION_STREAM_BASE_URL" https
    if [[ "$PERCEPTION_STREAM_PATH_TEMPLATE" != /* || "$PERCEPTION_STREAM_PATH_TEMPLATE" == *[[:space:]]* ]]; then
      echo "PERCEPTION_STREAM_PATH_TEMPLATE must be an absolute whitespace-free path" >&2
      exit 3
    fi
    if [[ "$VALIDATE_PERCEPTION_ENDPOINT" == "true" ]]; then
      health_file="$WORKDIR/perception-health.json"
      curl -fsS --connect-timeout 10 --max-time 20 \
        "${PERCEPTION_STREAM_BASE_URL}/health" >"$health_file"
      if ! jq -e '
        .status == "ok"
        and .ready == true
        and ([.cameras.ch1, .cameras.ch2, .cameras.ch3, .cameras.ch4]
             | all(.fresh == true and .state == "streaming"))' \
        "$health_file" >/dev/null; then
        echo "Perception endpoint health is not fresh/ready for all four cameras; refusing to publish it." >&2
        exit 6
      fi
      for camera_id in ch1 ch2 ch3 ch4; do
        curl -fsSI --connect-timeout 10 --max-time 20 \
          "${PERCEPTION_STREAM_BASE_URL}${PERCEPTION_STREAM_PATH_TEMPLATE/\{camera_id\}/$camera_id}" \
          >/dev/null
      done
    fi
    jq \
      --arg base "$PERCEPTION_STREAM_BASE_URL" \
      --arg template "$PERCEPTION_STREAM_PATH_TEMPLATE" \
      '. + {
        PERCEPTION_STREAM_BASE_URL: $base,
        PERCEPTION_STREAM_PATH_TEMPLATE: $template
      }' "$desired_file" >"$WORKDIR/next.json"
    mv "$WORKDIR/next.json" "$desired_file"
  fi
fi

desired_hash="$(canonical_hash "$desired_file")"
echo "Amplify runtime config reconciliation:"
echo "  app=${AMPLIFY_APP_ID} branch=${AMPLIFY_BRANCH} region=${AMPLIFY_REGION}"
echo "  action=${ACTION}"
echo "  currentHash=${current_hash}"
echo "  desiredHash=${desired_hash}"
echo "  updateDrive=${UPDATE_DRIVE} updatePerception=${UPDATE_PERCEPTION}"
if [[ "$ACTION" == "plan" ]]; then
  echo "  planOnly=true (no Amplify or filesystem writes)"
  exit 0
fi

if [[ "$desired_hash" == "$current_hash" && "$FORCE_RELEASE" != "true" ]]; then
  echo "Amplify environment already matches; no branch update or release was started."
  exit 0
fi

install -d -m 0700 "$BACKUP_DIR"
backup_file="${BACKUP_DIR%/}/${AMPLIFY_APP_ID}-${AMPLIFY_BRANCH}-$(date -u +%Y%m%dT%H%M%SZ)-${current_hash}.json"
install -m 0600 "$current_file" "$backup_file"
echo "Saved rollback environment: $backup_file"

if [[ "$desired_hash" != "$current_hash" ]]; then
  AWS_REGION="$AMPLIFY_REGION" aws amplify update-branch \
    --app-id "$AMPLIFY_APP_ID" \
    --branch-name "$AMPLIFY_BRANCH" \
    --environment-variables "file://${desired_file}" >/dev/null
fi

if [[ "$START_RELEASE" != "true" ]]; then
  echo "Amplify environment updated; START_RELEASE=false, so no release was started."
  exit 0
fi

job_id="$(AWS_REGION="$AMPLIFY_REGION" aws amplify start-job \
  --app-id "$AMPLIFY_APP_ID" \
  --branch-name "$AMPLIFY_BRANCH" \
  --job-type RELEASE \
  --query 'jobSummary.jobId' \
  --output text)"
echo "Started Amplify release job: $job_id"

if [[ "$WAIT_FOR_DEPLOY" != "true" ]]; then
  exit 0
fi

for _ in $(seq 1 90); do
  status="$(AWS_REGION="$AMPLIFY_REGION" aws amplify get-job \
    --app-id "$AMPLIFY_APP_ID" \
    --branch-name "$AMPLIFY_BRANCH" \
    --job-id "$job_id" \
    --query 'job.summary.status' \
    --output text 2>/dev/null || true)"
  echo "Amplify job $job_id: $status"
  case "$status" in
    SUCCEED)
      echo "Amplify runtime config release succeeded."
      exit 0
      ;;
    FAILED|CANCELLED)
      echo "Amplify release $job_id ended with $status; rollback environment is $backup_file" >&2
      exit 7
      ;;
  esac
  sleep 10
done

echo "Timed out waiting for Amplify release $job_id; rollback environment is $backup_file" >&2
exit 124

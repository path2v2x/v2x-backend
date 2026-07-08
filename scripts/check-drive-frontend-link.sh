#!/usr/bin/env bash
set -euo pipefail

# Checks the public Drive frontend config and verifies the advertised WebSocket URL.
# With DRIVE_LINK_HEALTH_REPAIR=true, republishes the newest Quick Tunnel URL and retries.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FRONTEND_CONFIG_URL="${FRONTEND_CONFIG_URL:-https://path2v2x.net/config.json}"
DRIVE_LINK_HEALTH_REPAIR="${DRIVE_LINK_HEALTH_REPAIR:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT="${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT:-${REPO_ROOT}/scripts/publish-drive-amplify-config.sh}"
PYTHON_BIN="${PYTHON_BIN:-}"
DRIVE_WS_INSECURE_SSL="${DRIVE_WS_INSECURE_SSL:-true}"
CHECK_TIMEOUT_SECONDS="${CHECK_TIMEOUT_SECONDS:-15}"

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

pick_python() {
    if [[ -n "$PYTHON_BIN" ]]; then
        printf '%s\n' "$PYTHON_BIN"
        return
    fi

    if [[ -x /home/path/venvs/vw-scenario/bin/python ]]; then
        printf '%s\n' /home/path/venvs/vw-scenario/bin/python
        return
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return
    fi

    log "ERROR: python3 is not available and PYTHON_BIN is not set."
    exit 2
}

fetch_config() {
    local output_file="$1"
    local cache_buster
    local separator
    cache_buster="$(date +%s)"
    separator="?"
    if [[ "$FRONTEND_CONFIG_URL" == *\?* ]]; then
        separator="&"
    fi

    curl -fsSL --connect-timeout 10 --max-time 20 \
        "${FRONTEND_CONFIG_URL}${separator}_drive_link_check=${cache_buster}" \
        -o "$output_file"
}

extract_ws_url() {
    local config_file="$1"
    "$PY" - "$config_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    config = json.load(fh)

url = config.get("cloudflareDriveWsUrl") or ""
print(url.strip())
PY
}

check_ws_url() {
    local ws_url="$1"
    DRIVE_WS_URL="$ws_url" \
    DRIVE_WS_INSECURE_SSL="$DRIVE_WS_INSECURE_SSL" \
    CHECK_TIMEOUT_SECONDS="$CHECK_TIMEOUT_SECONDS" \
    "$PY" <<'PY'
import asyncio
import os
import ssl
import sys

try:
    import websockets
except Exception as exc:
    print(f"ERROR: Python dependency 'websockets' is unavailable: {exc}", file=sys.stderr)
    sys.exit(2)

url = os.environ["DRIVE_WS_URL"]
timeout = float(os.environ.get("CHECK_TIMEOUT_SECONDS", "15"))
insecure = os.environ.get("DRIVE_WS_INSECURE_SSL", "true").lower() == "true"

ssl_context = None
if url.startswith("wss://") and insecure:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

async def main():
    async with websockets.connect(
        url,
        open_timeout=timeout,
        close_timeout=5,
        ssl=ssl_context,
    ):
        print("WS_OK")

try:
    asyncio.run(main())
except Exception as exc:
    print(f"ERROR: WebSocket probe failed for {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

run_check_once() {
    local config_file="$1"

    log "Fetching Drive frontend config: ${FRONTEND_CONFIG_URL}"
    fetch_config "$config_file"

    ws_url="$(extract_ws_url "$config_file")"
    if [[ -z "$ws_url" ]]; then
        log "ERROR: cloudflareDriveWsUrl is empty in ${FRONTEND_CONFIG_URL}"
        return 1
    fi

    log "Checking frontend-advertised Drive WebSocket: ${ws_url}"
    check_ws_url "$ws_url"
}

PY="$(pick_python)"
tmp_config="$(mktemp)"
trap 'rm -f "$tmp_config"' EXIT

if run_check_once "$tmp_config"; then
    log "Drive frontend link is healthy."
    exit 0
fi

if [[ "$DRIVE_LINK_HEALTH_REPAIR" != "true" ]]; then
    log "Drive frontend link is unhealthy; set DRIVE_LINK_HEALTH_REPAIR=true to republish the latest tunnel URL."
    exit 1
fi

log "Drive frontend link is unhealthy; attempting repair via ${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT}"
"$PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT"

if run_check_once "$tmp_config"; then
    log "Drive frontend link repair succeeded."
    exit 0
fi

log "ERROR: Drive frontend link repair did not restore WebSocket reachability."
exit 1

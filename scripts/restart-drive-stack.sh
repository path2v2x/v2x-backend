#!/usr/bin/env bash
set -euo pipefail

# Restart the Path PC CARLA simulator container and the drive bridge service.
# Intended to be run by systemd as root, but safe to run manually with sudo.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CARLA_CONTAINER="${CARLA_CONTAINER:-carla-custommaps}"
CARLA_IMAGE="${CARLA_IMAGE:-}"
CARLA_COMMAND="${CARLA_COMMAND:-./CarlaUE4.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2000}"
DRIVE_SERVICE="${DRIVE_SERVICE:-v2x-drive.service}"
WS_PORT="${WS_PORT:-8765}"
CARLA_PYTHON="${CARLA_PYTHON:-/home/path/V2XCarla/carla-venv/bin/python}"
SKIP_RESTART_IF_ACTIVE_SESSION="${SKIP_RESTART_IF_ACTIVE_SESSION:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG="${PUBLISH_DRIVE_FRONTEND_CONFIG:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED="${PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT="${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT:-${REPO_ROOT}/scripts/publish-drive-amplify-config.sh}"
WAIT_SCRIPT="${WAIT_SCRIPT:-${REPO_ROOT}/scripts/wait-for-carla.sh}"
CARLA_WAIT_TIMEOUT="${CARLA_WAIT_TIMEOUT:-600}"
CARLA_WAIT_USER="${CARLA_WAIT_USER:-path}"

log() {
    printf '%s %s\n' "$(date -Is)" "$*"
}

if ! command -v docker >/dev/null 2>&1; then
    log "ERROR: docker is not available."
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    log "ERROR: systemctl is not available."
    exit 1
fi

create_carla_container() {
    if [[ -z "$CARLA_IMAGE" ]]; then
        log "ERROR: CARLA container '$CARLA_CONTAINER' does not exist and CARLA_IMAGE is not set."
        exit 1
    fi

    log "Creating CARLA container: $CARLA_CONTAINER from $CARLA_IMAGE"
    docker run -d \
        --name "$CARLA_CONTAINER" \
        --runtime=nvidia \
        --publish 2000:2000 \
        --publish 2001:2001 \
        --publish 2002:2002 \
        --env NVIDIA_VISIBLE_DEVICES=all \
        --env NVIDIA_DRIVER_CAPABILITIES=all \
        "$CARLA_IMAGE" \
        /bin/bash -lc "$CARLA_COMMAND" >/dev/null
}

active_drive_session_count() {
    "$CARLA_PYTHON" - "$WS_PORT" <<'PY'
import asyncio
import json
import sys

import websockets

async def main():
    port = int(sys.argv[1])
    async with websockets.connect(f"ws://127.0.0.1:{port}", open_timeout=3, close_timeout=1) as ws:
        await ws.send(json.dumps({"type": "server_status"}))
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        print(int(response.get("active_sessions", 0)))

asyncio.run(main())
PY
}

if [[ "$SKIP_RESTART_IF_ACTIVE_SESSION" == "true" ]]; then
    session_count="$(active_drive_session_count 2>/dev/null || true)"
    if [[ "$session_count" =~ ^[0-9]+$ ]] && (( session_count > 0 )); then
        log "Skipping drive stack restart because $session_count active drive session(s) are connected."
        exit 0
    fi
fi

log "Stopping drive service: $DRIVE_SERVICE"
systemctl stop "$DRIVE_SERVICE" || true

# Clean up manually-started bridge processes so the service can bind :8765.
if pgrep -f 'python -m digital_twin_bridge.drive_main' >/dev/null 2>&1; then
    log "Stopping unmanaged drive_main process(es)"
    pkill -TERM -f 'python -m digital_twin_bridge.drive_main' || true
    sleep 3
    pkill -KILL -f 'python -m digital_twin_bridge.drive_main' || true
fi

if ! docker inspect "$CARLA_CONTAINER" >/dev/null 2>&1; then
    create_carla_container
elif [[ -n "$CARLA_IMAGE" ]] && [[ "$(docker inspect -f '{{.Config.Image}}' "$CARLA_CONTAINER")" != "$CARLA_IMAGE" ]]; then
    log "Recreating CARLA container $CARLA_CONTAINER for image $CARLA_IMAGE"
    docker rm -f "$CARLA_CONTAINER" >/dev/null
    create_carla_container
elif docker inspect -f '{{.State.Running}}' "$CARLA_CONTAINER" 2>/dev/null | grep -qx true; then
    log "Restarting CARLA container: $CARLA_CONTAINER"
    docker restart "$CARLA_CONTAINER" >/dev/null
else
    log "Starting CARLA container: $CARLA_CONTAINER"
    docker start "$CARLA_CONTAINER" >/dev/null
fi

log "Waiting for CARLA RPC readiness"
if [ "$(id -u)" -eq 0 ] && id "$CARLA_WAIT_USER" >/dev/null 2>&1; then
    runuser -u "$CARLA_WAIT_USER" -- env \
        CARLA_CONTAINER="$CARLA_CONTAINER" \
        CARLA_WAIT_TIMEOUT="$CARLA_WAIT_TIMEOUT" \
        CARLA_HOST="${CARLA_HOST:-localhost}" \
        CARLA_PORT="${CARLA_PORT:-2000}" \
        CARLA_PYTHON="$CARLA_PYTHON" \
        "$WAIT_SCRIPT"
else
    CARLA_CONTAINER="$CARLA_CONTAINER" \
    CARLA_WAIT_TIMEOUT="$CARLA_WAIT_TIMEOUT" \
    CARLA_HOST="${CARLA_HOST:-localhost}" \
    CARLA_PORT="${CARLA_PORT:-2000}" \
    CARLA_PYTHON="$CARLA_PYTHON" \
    "$WAIT_SCRIPT"
fi

log "Starting drive service: $DRIVE_SERVICE"
systemctl reset-failed "$DRIVE_SERVICE" || true
systemctl start "$DRIVE_SERVICE"

if [[ "$PUBLISH_DRIVE_FRONTEND_CONFIG" == "true" ]]; then
    log "Publishing current drive frontend tunnel config"
    if ! "$PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT"; then
        if [[ "$PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED" == "true" ]]; then
            log "ERROR: drive frontend tunnel config publish failed."
            exit 1
        fi
        log "WARN: drive frontend tunnel config publish failed; leaving existing public config in place."
    fi
fi

log "Drive stack restart complete"

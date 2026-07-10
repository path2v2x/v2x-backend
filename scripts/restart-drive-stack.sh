#!/usr/bin/env bash
set -euo pipefail

# Restart the Path PC CARLA simulator container and the drive bridge service.
# Intended to be run by systemd as root, but safe to run manually with sudo.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CARLA_CONTAINER="${CARLA_CONTAINER:-carla-rr-maps}"
CARLA_IMAGE="${CARLA_IMAGE:-ghcr.io/simforgeinc/carla-rr-maps:0.10.0}"
CARLA_COMMAND="${CARLA_COMMAND:-./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2000}"
CARLA_SERVICE="${CARLA_SERVICE:-v2x-carla-rr.service}"
DRIVE_SERVICE="${DRIVE_SERVICE:-v2x-drive.service}"
WS_PORT="${WS_PORT:-8765}"
CARLA_PYTHON="${CARLA_PYTHON:-/home/path/V2XCarla/carla-venv-310/bin/python}"
ALLOW_CARLA_CREATE="${ALLOW_CARLA_CREATE:-false}"
ALLOW_CARLA_RECREATE="${ALLOW_CARLA_RECREATE:-false}"
SKIP_RESTART_IF_ACTIVE_SESSION="${SKIP_RESTART_IF_ACTIVE_SESSION:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG="${PUBLISH_DRIVE_FRONTEND_CONFIG:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED="${PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT="${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT:-${REPO_ROOT}/scripts/publish-drive-tunnel-config.sh}"
WAIT_SCRIPT="${WAIT_SCRIPT:-${REPO_ROOT}/scripts/wait-for-carla.sh}"
CARLA_WAIT_TIMEOUT="${CARLA_WAIT_TIMEOUT:-600}"
CARLA_WAIT_USER="${CARLA_WAIT_USER:-path}"

for boolean_name in ALLOW_CARLA_CREATE ALLOW_CARLA_RECREATE SKIP_RESTART_IF_ACTIVE_SESSION \
    PUBLISH_DRIVE_FRONTEND_CONFIG PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED; do
    boolean_value="${!boolean_name}"
    if [[ "$boolean_value" != "true" && "$boolean_value" != "false" ]]; then
        printf '%s must be true or false (got: %s)\n' "$boolean_name" "$boolean_value" >&2
        exit 2
    fi
done

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
    local -a command_parts=()
    read -r -a command_parts <<<"$CARLA_COMMAND"
    if (( ${#command_parts[@]} == 0 )); then
        log "ERROR: CARLA_COMMAND is empty."
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
        --label com.simforge.v2x.managed-by=v2x-backend \
        "$CARLA_IMAGE" \
        "${command_parts[@]}" >/dev/null
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

carla_rpc_healthy() {
    "$CARLA_PYTHON" - "${CARLA_HOST:-localhost}" "${CARLA_PORT:-2000}" <<'PY'
import sys
import carla

host = sys.argv[1]
port = int(sys.argv[2])
client = carla.Client(host, port)
client.set_timeout(5.0)
world = client.get_world()
world.get_map()
world.get_blueprint_library()
PY
}

if [[ "$SKIP_RESTART_IF_ACTIVE_SESSION" == "true" ]]; then
    session_count="$(active_drive_session_count 2>/dev/null || true)"
    if [[ "$session_count" =~ ^[0-9]+$ ]] && (( session_count > 0 )); then
        if carla_rpc_healthy >/dev/null 2>&1; then
            log "Skipping drive stack restart because $session_count active drive session(s) are connected and CARLA is healthy."
            exit 0
        fi
        log "CARLA health probe failed with $session_count active drive session(s); restarting the stack anyway."
    fi
fi

container_exists=false
image_mismatch=false
if docker inspect "$CARLA_CONTAINER" >/dev/null 2>&1; then
    container_exists=true
    current_image="$(docker inspect -f '{{.Config.Image}}' "$CARLA_CONTAINER")"
    if [[ "$current_image" != "$CARLA_IMAGE" ]]; then
        image_mismatch=true
    fi
fi

if [[ "$container_exists" != "true" && "$ALLOW_CARLA_CREATE" != "true" ]]; then
    log "ERROR: CARLA container '$CARLA_CONTAINER' is absent. Set ALLOW_CARLA_CREATE=true only in a controlled deployment window."
    exit 1
fi
if [[ "$image_mismatch" == "true" && "$ALLOW_CARLA_RECREATE" != "true" ]]; then
    log "ERROR: $CARLA_CONTAINER uses '$current_image', expected '$CARLA_IMAGE'. Refusing an automatic replacement; set ALLOW_CARLA_RECREATE=true only after rollback capture."
    exit 1
fi

carla_service_installed=false
if [[ -n "$CARLA_SERVICE" ]] && systemctl cat "$CARLA_SERVICE" >/dev/null 2>&1; then
    carla_service_installed=true
fi

log "Stopping drive service: $DRIVE_SERVICE"
systemctl stop "$DRIVE_SERVICE"

# Clean up manually-started bridge processes so the service can bind :8765.
if pgrep -f 'python -m digital_twin_bridge.drive_main' >/dev/null 2>&1; then
    log "Stopping unmanaged drive_main process(es)"
    pkill -TERM -f 'python -m digital_twin_bridge.drive_main' || true
    sleep 3
    pkill -KILL -f 'python -m digital_twin_bridge.drive_main' || true
fi

if [[ "$container_exists" != "true" ]]; then
    create_carla_container
elif [[ "$image_mismatch" == "true" ]]; then
    log "Recreating CARLA container $CARLA_CONTAINER for image $CARLA_IMAGE"
    if [[ "$carla_service_installed" == "true" ]]; then
        systemctl stop "$CARLA_SERVICE"
    fi
    docker rm -f "$CARLA_CONTAINER" >/dev/null
    create_carla_container
fi

if [[ "$carla_service_installed" == "true" ]]; then
    log "Restarting CARLA supervisor: $CARLA_SERVICE"
    systemctl restart "$CARLA_SERVICE"
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
    if ! ACTION=publish "$PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT"; then
        if [[ "$PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED" == "true" ]]; then
            log "ERROR: drive frontend tunnel config publish failed."
            exit 1
        fi
        log "WARN: drive frontend tunnel config publish failed; leaving existing public config in place."
    fi
fi

log "Drive stack restart complete"

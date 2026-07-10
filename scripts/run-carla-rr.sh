#!/usr/bin/env bash
set -euo pipefail

# Supervise the pre-provisioned RR/CARLA 0.10 container. Container creation or
# replacement is deliberately handled by restart-drive-stack.sh behind explicit
# deployment flags; a boot-time service must never destroy simulator state.

CARLA_CONTAINER="${CARLA_CONTAINER:-carla-rr-maps}"
CARLA_IMAGE="${CARLA_IMAGE:-ghcr.io/simforgeinc/carla-rr-maps:0.10.0}"
CARLA_COMMAND="${CARLA_COMMAND:-./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2000}"
ALLOW_CARLA_CONFIG_DRIFT="${ALLOW_CARLA_CONFIG_DRIFT:-false}"
ACTION="${1:-run}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

if [[ "$ALLOW_CARLA_CONFIG_DRIFT" != "true" && "$ALLOW_CARLA_CONFIG_DRIFT" != "false" ]]; then
    echo "ALLOW_CARLA_CONFIG_DRIFT must be true or false" >&2
    exit 2
fi
if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
    echo "Docker command is unavailable: $DOCKER_BIN" >&2
    exit 127
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "Missing dependency: jq" >&2
    exit 127
fi

case "$ACTION" in
    run|validate) ;;
    stop)
        if "$DOCKER_BIN" inspect "$CARLA_CONTAINER" >/dev/null 2>&1; then
            exec "$DOCKER_BIN" stop --time 60 "$CARLA_CONTAINER"
        fi
        exit 0
        ;;
    *)
        echo "Usage: $0 [run|validate|stop]" >&2
        exit 2
        ;;
esac

if ! inspect_json="$("$DOCKER_BIN" inspect "$CARLA_CONTAINER" 2>/dev/null)"; then
    echo "RR CARLA container '$CARLA_CONTAINER' is absent; provision it in a controlled deployment window." >&2
    exit 1
fi

read -r -a command_parts <<<"$CARLA_COMMAND"
expected_command_json="$(jq -cn --args '$ARGS.positional' -- "${command_parts[@]}")"
expected_shell_command_json="$(jq -cn --arg command "$CARLA_COMMAND" '["/bin/bash", "-lc", $command]')"

config_drift=false
if ! jq -e \
    --arg image "$CARLA_IMAGE" \
    --argjson command "$expected_command_json" \
    --argjson shell_command "$expected_shell_command_json" \
    '.[0]
     | (.Config.Image == $image)
       and ((.Config.Cmd == $command) or (.Config.Cmd == $shell_command))
       and (.HostConfig.Runtime == "nvidia")
       and (.HostConfig.NetworkMode == "bridge")
       and (((.HostConfig.PortBindings["2000/tcp"] // []) | map(.HostPort) | index("2000")) != null)
       and (((.HostConfig.PortBindings["2001/tcp"] // []) | map(.HostPort) | index("2001")) != null)
       and (((.HostConfig.PortBindings["2002/tcp"] // []) | map(.HostPort) | index("2002")) != null)' \
    <<<"$inspect_json" >/dev/null; then
    actual="$(jq -c '.[0] | {
        image: .Config.Image,
        command: .Config.Cmd,
        runtime: .HostConfig.Runtime,
        network: .HostConfig.NetworkMode,
        ports: .HostConfig.PortBindings
    }' <<<"$inspect_json")"
    config_drift=true
    if [[ "$ALLOW_CARLA_CONFIG_DRIFT" != "true" ]]; then
        echo "RR CARLA container configuration drifted from the tracked 0.10 definition: $actual" >&2
        echo "Refusing to start it; reconcile under the deployment/rollback gate." >&2
        exit 1
    fi
    echo "WARNING: starting RR CARLA with explicitly allowed configuration drift: $actual" >&2
fi

if [[ "$ACTION" == "validate" ]]; then
    if [[ "$config_drift" == "true" ]]; then
        echo "RR CARLA container '$CARLA_CONTAINER' has explicitly allowed configuration drift."
    else
        echo "RR CARLA container '$CARLA_CONTAINER' matches the tracked 0.10 runtime definition."
    fi
    exit 0
fi

if jq -e '.[0].State.Running == true' <<<"$inspect_json" >/dev/null; then
    echo "Adopting already-running RR CARLA container '$CARLA_CONTAINER' without restarting it."
    exec "$DOCKER_BIN" wait "$CARLA_CONTAINER"
fi

exec "$DOCKER_BIN" start --attach "$CARLA_CONTAINER"

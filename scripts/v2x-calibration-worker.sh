#!/usr/bin/env bash
set -euo pipefail

# Manage an isolated, non-restarting copy of the approved UE5.5 V2X worker.
# This container is never a production service and binds only to loopback.

ACTION="${1:-status}"
CONTAINER="${V2X_CALIBRATION_CONTAINER:-v2x-calibration-ue5}"
IMAGE="${V2X_CALIBRATION_IMAGE:-ghcr.io/simforgeinc/carla-rr-maps:0.10.0}"
EXPECTED_IMAGE_ID="sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d\
5087709504118e"
RPC_PORT=2300
SCOPE_LABEL="com.path2v2x.scope=calibration"

if [[ "$CONTAINER" == "carla-rr-maps" ]]; then
    echo "Refusing to operate the production CARLA container" >&2
    exit 2
fi

verify_image() {
    local image_id
    image_id="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
    if [[ "$image_id" != "$EXPECTED_IMAGE_ID" ]]; then
        echo "Approved calibration worker image ID is unavailable" >&2
        exit 1
    fi
}

inspect_worker() {
    docker inspect "$CONTAINER" | jq -e --arg image "$IMAGE" '
        .[0] as $root
        | ($root.Config.Image == $image)
          and ($root.Config.Labels["com.path2v2x.scope"] == "calibration")
          and ($root.HostConfig.Runtime == "nvidia")
          and ($root.HostConfig.RestartPolicy.Name == "no")
          and ($root.HostConfig.NetworkMode == "bridge")
          and ([2300, 2301, 2302] | all(. as $port |
            ($root.HostConfig.PortBindings[($port|tostring) + "/tcp"] // [])
            | any(
                .HostIp == "127.0.0.1"
                and .HostPort == ($port|tostring)
              )
          ))
          and (($root.Config.Cmd | join(" "))
            | contains("-carla-rpc-port=2300"))
          and (($root.Config.Cmd | join(" "))
            | contains("-RenderOffScreen"))
    ' >/dev/null
}

case "$ACTION" in
    start)
        verify_image
        if docker inspect "$CONTAINER" >/dev/null 2>&1; then
            inspect_worker || {
                echo "Existing calibration container does not match the tracked "\
"definition" \
                    >&2
                exit 1
            }
            if [[ "$(docker inspect -f '{{.State.Running}}' \
                "$CONTAINER")" == true ]]; then
                echo "$CONTAINER already running"
                exit 0
            fi
            docker start "$CONTAINER" >/dev/null
        else
            docker run -d \
                --name "$CONTAINER" \
                --runtime=nvidia \
                --restart=no \
                --publish 127.0.0.1:2300:2300 \
                --publish 127.0.0.1:2301:2301 \
                --publish 127.0.0.1:2302:2302 \
                --env NVIDIA_VISIBLE_DEVICES=all \
                --env NVIDIA_DRIVER_CAPABILITIES=all \
                --label "$SCOPE_LABEL" \
                "$IMAGE" \
                ./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound \
                "-carla-rpc-port=$RPC_PORT" >/dev/null
        fi
        inspect_worker
        echo "$CONTAINER started on 127.0.0.1:$RPC_PORT"
        ;;
    status)
        inspect_worker
        docker inspect "$CONTAINER" --format \
            'name={{.Name}} running={{.State.Running}}'\
' started={{.State.StartedAt}} image={{.Image}}'
        ;;
    stop)
        inspect_worker
        if [[ "$(docker inspect -f '{{.State.Running}}' \
            "$CONTAINER")" == true ]]; then
            timeout 20 docker stop --time 10 "$CONTAINER" >/dev/null || true
            if [[ "$(docker inspect -f '{{.State.Running}}' \
                "$CONTAINER")" == true ]]; then
                docker kill "$CONTAINER" >/dev/null
            fi
        fi
        echo "$CONTAINER stopped"
        ;;
    remove)
        inspect_worker
        if [[ "$(docker inspect -f '{{.State.Running}}' \
            "$CONTAINER")" == true ]]; then
            echo "Stop the owned calibration worker before removing it" >&2
            exit 1
        fi
        docker rm "$CONTAINER" >/dev/null
        echo "$CONTAINER removed"
        ;;
    *)
        echo "Usage: $0 {start|status|stop|remove}" >&2
        exit 2
        ;;
esac

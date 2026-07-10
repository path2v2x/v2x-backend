#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CARLA_CONTAINER:-carla-rr-maps}"
CARLA_HOST="${CARLA_HOST:-localhost}"
CARLA_PORT="${CARLA_PORT:-2000}"
TIMEOUT_SECONDS="${CARLA_WAIT_TIMEOUT:-600}"
PYTHON_BIN="${CARLA_PYTHON:-/home/path/V2XCarla/carla-venv-310/bin/python}"

deadline=$((SECONDS + TIMEOUT_SECONDS))

while [ "$SECONDS" -lt "$deadline" ]; do
    if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
        if "$PYTHON_BIN" - "$CARLA_HOST" "$CARLA_PORT" >/dev/null 2>&1 <<'PY'
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
        then
            echo "CARLA is ready on ${CARLA_HOST}:${CARLA_PORT} (${CONTAINER})."
            exit 0
        fi
    fi

    sleep 5
done

echo "Timed out waiting for CARLA on ${CARLA_HOST}:${CARLA_PORT} (${CONTAINER})." >&2
exit 1

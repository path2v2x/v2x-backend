---
name: path-pc-carla
description: Operate and diagnose the Path PC CARLA/V2X stack at path@100.72.252.40, including the production Unreal Engine 5.5 RR/CARLA 0.10 worker container, drive WebSocket bridge, Vite dashboard, perception/HLS pipeline, Cloudflare and Tailscale transport, systemd supervision, and controlled deployment/rollback gates. Use for any work that reads, tests, changes, deploys, or recovers the Path PC V2X environment; exclude Unreal Engine 6 experiments, which belong to a separate task and runtime namespace.
---

# Path PC CARLA/V2X

Treat this file as an operating procedure, not proof of current state. Re-run the read-only baseline before every intervention.

## Safety boundaries

- Work locally when already on `path-B860I-AORUS-PRO-ICE`; do not SSH back into the same host.
- From another host, use the configured SSH/Tailscale connection to `path@100.72.252.40`. Do not embed credentials in commands or source.
- Make source changes only in a clean Codex worktree. Treat `/home/path/V2XCarla/v2x-backend-dev` as a reference candidate, not as proof that it is clean; verify its status and fail the deployment gate if it is dirty.
- Do not overwrite `/home/path/V2XCarla/v2x-backend` until a controlled deployment gate. It may run active services and contain live-only work.
- Preserve `/home/path/V2XCarla/v2x-backend-backups/` and take a fresh rollback snapshot before deployment.
- Use only the packaged Unreal Engine 5.5 worker container `carla-rr-maps` for production V2X simulator work. The accepted image is RR/CARLA 0.10 and its runtime reports `5.5.0-0+UE5`.
- Never use the retired `carla-rfs`/CARLA 0.9.16 restart recipe.
- Do not build, launch, debug, authorize, retry, coordinate, or accept evidence from `/home/path/V2XCarla/CarlaUE6`, `/home/path/V2XCarla/UnrealEngine_6`, `ue6-*` user units, or ports `2100-2102` in a V2X task. A separate UE6 task owns those paths, processes, changes, and acceptance criteria.
- UE6 work must not stop, hold, delay, restart, or reconfigure V2X services or timers. V2X work must likewise remain independent: do not inspect, poll, gate on, coordinate, or operate UE6 paths, units, processes, listeners, or evidence. Validate only the V2X-owned UE5.5 resources below. Any cross-runtime contention is owned by the separate UE6 task, which must stop itself rather than asking V2X to change state.
- Before service or tunnel changes, stop every mutation-capable timer in the maintenance window, snapshot its state, and restore it only after validation:

```bash
sudo systemctl stop \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer
```

The link-health units can repair/publish public runtime configuration when their independent repair and release gates are enabled. The hourly unit restarts CARLA/drive and may publish tunnel configuration when explicitly enabled.

## Revalidate the live topology

Observed on 2026-07-10 UTC; verify rather than assume:

| Layer | Expected live value |
|---|---|
| Simulator engine | packaged Unreal Engine `5.5.0-0+UE5`; never UE6 |
| CARLA container | `carla-rr-maps` |
| CARLA image | `ghcr.io/simforgeinc/carla-rr-maps:0.10.0` |
| CARLA image ID | `sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e` |
| Shipping binary SHA-256 | `d9d8cafc10def42557cdfc2897f9581da45c4900dc82c3ff37f2c5e2e7b98b23` |
| CARLA command | `./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2000` |
| CARLA runtime/network | NVIDIA runtime, Docker bridge, host ports `2000-2002` |
| Map | `Richmond_Field_Station_Richmond_CA` |
| CARLA Python | `/home/path/V2XCarla/carla-venv-310/bin/python` |
| Drive WebSocket | `0.0.0.0:8765`, `v2x-drive.service` |
| Frontend | Vite on `0.0.0.0:5173`, `v2x-web.service`; do not inject browser-local `VITE_DRIVE_WS_URL` |
| Perception | `0.0.0.0:8090`, `v2x-perception.service` |
| Perception Python | `/home/path/V2XCarla/perception-venv/bin/python` (observed Python 3.12.3) |
| Perception assets | ignored live `apps/perception/yolov8n.pt` plus pinned `~/.cache/torch/hub/checkpoints/mobilenet_v3_small-047dcff4.pth` and `convnext_base-6075fbad.pth`; hash and preserve all three |
| Drive tunnel | `v2x-cloudflared-drive.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Perception tunnel | `v2x-cloudflared-perception.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Public API | `https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com` |
| AWS deploy caller | `arn:aws:iam::147229569658:user/rfs-v2x-service`; API writes require the dedicated least-privilege deploy role |
| Amplify repository | preferred `path2v2x/v2x-backend`; temporary `michaelvu1207/v2x-backend-amplify` mirror is acceptable only with exact main-SHA parity, active successful sync workflow, active push webhook, and explicit release gate |

Collect a non-mutating baseline:

```bash
hostname
date -u +%Y-%m-%dT%H:%M:%SZ
git -C /home/path/V2XCarla/v2x-backend status --short --branch
git -C /home/path/V2XCarla/v2x-backend-dev status --short --branch
test -z "$(git -C /home/path/V2XCarla/v2x-backend-dev status --porcelain=v1)" || {
  echo "Clean-reference candidate is dirty; stop and reconcile it." >&2
  exit 1
}
docker ps -a --filter name=carla-rr-maps --no-trunc
docker inspect carla-rr-maps --format \
  'image={{.Config.Image}} runtime={{.HostConfig.Runtime}} network={{.HostConfig.NetworkMode}} restart={{.HostConfig.RestartPolicy.Name}} ports={{json .HostConfig.PortBindings}} cmd={{json .Config.Cmd}}'
expected_image_id='sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e'
actual_image_id="$(docker inspect -f '{{.Image}}' carla-rr-maps)"
test "$actual_image_id" = "$expected_image_id" || {
  echo "Production CARLA image ID drifted: $actual_image_id" >&2
  exit 1
}
container_pid="$(docker inspect -f '{{.State.Pid}}' carla-rr-maps)"
ue5_binary="/proc/$container_pid/root/home/carla/CarlaUnreal/Binaries/Linux/CarlaUnreal-Linux-Shipping"
expected_binary_sha256='d9d8cafc10def42557cdfc2897f9581da45c4900dc82c3ff37f2c5e2e7b98b23'
actual_binary_sha256="$(sudo sha256sum "$ue5_binary" | awk '{print $1}')"
test "$actual_binary_sha256" = "$expected_binary_sha256" || {
  echo "Production CARLA UE5 worker binary drifted: $actual_binary_sha256" >&2
  exit 1
}
sudo strings -a "$ue5_binary" \
  | awk 'index($0, "/UnrealEngine5/") {found=1} END {exit !found}' || {
  echo 'Production CARLA binary lacks the UnrealEngine5 marker; stop.' >&2
  exit 1
}
if systemctl cat v2x-carla-rr.service \
  | grep -Eqi 'CarlaUE6|UnrealEngine_6|carla-rpc-port=2100'; then
  echo 'Production V2X service references the separate UE6 runtime; stop.' >&2
  exit 1
fi
if find /home/path/V2XCarla/v2x-backend \
  \( -path '*/.git' -o -path '*/node_modules' -o -path '*/.svelte-kit' \) \
  -prune -o -iname '*ue6*' -print -quit | grep -q .; then
  echo 'A UE6 artifact exists inside the production V2X checkout; stop.' >&2
  exit 1
fi
ss -ltnp | awk 'NR==1 || /:(2000|2001|2002|8765|5173|8090)( |$)/'
ps -eo pid=,ppid=,lstart=,args= | awk '/[c]loudflared/'
systemctl show \
  v2x-carla-rr.service \
  v2x-drive.service \
  v2x-perception.service \
  v2x-web.service \
  v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer \
  --property=Id,ActiveState,SubState,UnitFileState,FragmentPath,MainPID,ExecMainStartTimestamp,NextElapseUSecRealtime
for unit in \
  v2x-carla-rr.service v2x-drive.service v2x-perception.service \
  v2x-web.service v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service v2x-drive-link-health.timer \
  v2x-perception-link-health.timer v2x-hourly-drive-restart.timer; do
  printf '%s=' "$unit"
  systemctl is-enabled "$unit" 2>&1 || true
done
```

The Path PC AWS files have named profiles rather than a default profile. Use
`AWS_PROFILE=path AWS_REGION=us-west-2` for Amplify reads and the scoped
`AWS_PROFILE=v2x-backend-deploy AWS_REGION=us-west-1` role only for reviewed
exact-API deployment work. Never print credential values. A temporary Amplify
mirror does not satisfy the preferred canonical-repository endpoint merely
because a job succeeded; separately prove canonical/mirror `main` SHA parity,
the tracked `Sync Amplify mirror` workflow, and the active Amplify push webhook.

Inspect installed definitions before trusting tracked units:

```bash
systemctl cat \
  v2x-carla-rr.service \
  v2x-drive.service \
  v2x-perception.service \
  v2x-web.service \
  v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service \
  v2x-drive-link-health.service \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.service \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.service \
  v2x-hourly-drive-restart.timer
```

Print only allowlisted, non-secret safety gates. Calculate the installed
last-declaration-wins values, then compare active services with their actual
process environments; a mismatch means the process has not consumed the new
configuration. Never dump a complete unit or process environment.

```bash
gate_keys='^(ALLOW_CARLA_CONFIG_DRIFT|ALLOW_CARLA_CREATE|ALLOW_CARLA_RECREATE|AMPLIFY_RELEASE_ENABLED|DRIVE_CONFIG_REQUIRED|DRIVE_LINK_HEALTH_REPAIR|DRIVE_TUNNEL_MODE|DRIVE_WS_INSECURE_SSL|PERCEPTION_LINK_HEALTH_REPAIR|PUBLISH_DRIVE_FRONTEND_CONFIG|PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED|SKIP_RESTART_IF_ACTIVE_SESSION|V2X_PERCEPTION_UPLOAD)$'
show_declared_gates() {
  unit="$1"; shift
  {
    systemctl show "$unit" --property=Environment --value | tr ' ' '\n'
    for file in "$@"; do
      sudo awk -F= -v keys="$gate_keys" '$1 ~ keys {print}' "$file" 2>/dev/null || true
    done
  } | awk -F= -v keys="$gate_keys" '$1 ~ keys {value[$1]=$2} END {for (key in value) print key "=" value[key]}' | sort
}
show_declared_gates v2x-carla-rr.service /etc/v2x-carla-rr.env
show_declared_gates v2x-perception.service /etc/v2x-perception.env
show_declared_gates v2x-cloudflared-drive.service /etc/v2x-drive-tunnel.env
show_declared_gates v2x-cloudflared-perception.service /etc/v2x-perception-tunnel.env
show_declared_gates v2x-drive-link-health.service /etc/v2x-drive-tunnel.env /etc/v2x-drive-link-health.env
show_declared_gates v2x-perception-link-health.service /etc/v2x-perception-tunnel.env /etc/v2x-perception-link-health.env
show_declared_gates v2x-hourly-drive-restart.service /etc/v2x-drive-restart.env

for unit in v2x-carla-rr.service v2x-perception.service \
  v2x-cloudflared-drive.service v2x-cloudflared-perception.service; do
  pid="$(systemctl show "$unit" --property=MainPID --value)"
  printf '[%s pid=%s effective]\n' "$unit" "$pid"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    sudo sh -c 'tr "\0" "\n" < "/proc/$1/environ"' sh "$pid" \
      | awk -F= -v keys="$gate_keys" '$1 ~ keys {print}' | sort
  else
    echo inactive
  fi
done
```

## Mental model

Keep these layers separate:

1. The UE5.5 `carla-rr-maps` worker container and CARLA RPC on `2000`.
2. Drive WebSocket bridge on `8765`.
3. Supervised frontend dev server on `5173`.
4. Perception health/MJPEG on `8090`.
5. Independently supervised Drive and perception tunnels.
6. Cloudflare or Tailscale transport.
7. Public runtime configuration and API routes.

A healthy CARLA container does not prove a healthy bridge, tunnel, frontend, or perception pipeline.

## Computer Use companion

- Use CLI/API probes for infrastructure facts and Computer Use for visible `/drive`, `/live`, and `/timeline` behavior, screenshots, browser console, network requests, and WebSocket frames. Hard-refresh after each state-changing action before recording evidence.
- If `node_repl` with `@oai/sky` is unavailable on the Path PC task, continue the stable companion task on `remote-ssh-codex-managed:simforgelaptop` with `send_message_to_thread`; create a new task only when explicitly requested. Do not ask the user to operate the browser.
- Include the public URL, expected deployed commit/config version, exact page flows, read-only or mutation boundary, cleanup requirement, and a request to debug within scope until each acceptance check works. Require `node_repl`/`@oai/sky`, refreshed screenshots, console and network evidence, and explicit cleanup of any Drive session.
- Immediately create an `automation_update` heartbeat that calls `read_thread` every minute, reports terminal completion, and disables itself after success/failure. If that runtime is unavailable, dedicate a collaboration agent to poll the task every 30 seconds with bounded waits. Preserve the task when the selected account is usage-limited and resume it when capacity returns.
- Record the companion task ID and heartbeat ID in the phase evidence. Stop/archive the heartbeat only after consuming the final result; do not infer completion from silence.

## Drive diagnosis order

1. Confirm `carla-rr-maps` is running with the expected image/command and reports `5.5.0-0+UE5`.
2. Confirm RR/CARLA 0.10 in that UE5.5 worker accepts a client and has the Richmond map loaded.
3. Confirm `v2x-drive.service` and listener `8765`.
4. Perform a WebSocket handshake/protocol health check.
5. Inspect the tunnel process and its local origin.
6. Compare public `/config.json` and `/drive-config` with the active tunnel.
7. Refresh `/drive` and capture visible state, console, network, and WebSocket evidence.

CARLA client probe:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import carla

client = carla.Client("127.0.0.1", 2000)
client.set_timeout(20.0)
print("client", client.get_client_version())
print("server", client.get_server_version())
print("map", client.get_world().get_map().name)
PY
```

WebSocket handshake probe:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import asyncio
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765", open_timeout=10):
        print("WS_OK")

asyncio.run(main())
PY
```

Useful logs:

```bash
journalctl -u v2x-drive.service --utc -n 200 --no-pager
journalctl -u v2x-cloudflared-drive.service --utc -n 200 --no-pager
journalctl -u v2x-drive-link-health.service --utc -n 200 --no-pager
tail -n 200 /tmp/v2x-cloudflared.log
```

## Tunnel and runtime configuration

- The current installed drive unit may launch a process-scoped Quick Tunnel to `http://localhost:8765`.
- A named hostname such as `wss://drive.path2v2x.net` is valid only after its credential, DNS, unit, and WebSocket handshake are independently proven.
- Never hardcode a newly observed `*.trycloudflare.com` URL in source.
- Never roll back to a saved Quick-Tunnel URL after that process has stopped; it is dead. Preserve the old tunnel during a blue/green cutover, publish the newly proven endpoint, verify public convergence, and only then stop the old process.
- Treat Tailscale and Cloudflare as separate transports. Validate the endpoint the browser actually selected.
- Treat an enabled-but-inactive `v2x-cloudflared-perception.service` and a `cloudflared` process with PPID 1 as separate facts. `enable` does not adopt that unmanaged process; starting the unit creates a second tunnel. Record both PID/PPID/command/URL tuples, keep the PPID-1 tunnel alive during blue/green validation, and stop only the exact old PID after public convergence.

Read-only checks:

```bash
pgrep -af cloudflared
curl -fsS https://path2v2x.net/config.json | jq .
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/drive-config
```

Accept `/drive-config` only when it returns HTTP `200`; `version` is a positive,
nondecreasing integer; `updatedAt`/`expiresAt` are fresh and within the browser's
24-hour TTL bound; and the selected WebSocket URL equals the endpoint of the
still-running tunnel. For a Quick Tunnel, compare it directly:

```bash
(
body="$(mktemp)"; trap 'rm -f "$body"' EXIT
code="$(curl -sS -o "$body" -w '%{http_code}' \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/drive-config)"
test "$code" = 200
: "${PREVIOUS_DRIVE_CONFIG_VERSION:=0}"
jq -e --argjson previous "$PREVIOUS_DRIVE_CONFIG_VERSION" \
  --argjson now "$(date -u +%s)" '
  .version as $v
  | (.updatedAt | fromdateiso8601) as $updated
  | (.expiresAt | fromdateiso8601) as $expires
  | ($v | type == "number") and ($v >= 1) and ($v == ($v | floor))
    and ($v >= $previous) and ($updated <= ($now + 300))
    and ($expires > $now) and (($expires - $updated) > 0)
    and (($expires - $updated) <= 86400)' "$body"
pgrep -af 'cloudflared.*(localhost|127\.0\.0\.1):8765'
active_drive_ws="$(grep -Eo 'https://[A-Za-z0-9-]+\.trycloudflare\.com' \
  /tmp/v2x-cloudflared.log | tail -n 1 | sed 's#^https:#wss:#')"
published_drive_ws="$(jq -er '.cloudflareDriveWsUrl' "$body")"
test "$published_drive_ws" = "$active_drive_ws"
DRIVE_WS_URL="$published_drive_ws" /home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import asyncio, os, websockets
async def main():
    async with websockets.connect(os.environ["DRIVE_WS_URL"], open_timeout=10):
        print("PUBLIC_WS_OK")
asyncio.run(main())
PY
)
```

Also require Computer Use network evidence that `/drive-config` returned that
version and `/drive` opened its WebSocket against the same endpoint after a
hard refresh. An HTTP `426` from the tunnel root can be expected for a reachable
WebSocket-only origin; require a real WebSocket `101`/handshake for acceptance.

## Perception diagnosis

Do not use HTTP `200`, MJPEG byte flow, or a rising republished-frame counter as freshness proof. The service can replay `last_valid_frames` while an upstream camera is frozen. Legacy detections created before timestamp schema v2 used Path-PC decode-receipt time and are not valid archive-correlation evidence. Accept a new record for replay proof only when `timestamp_schema_version=2`, `media_time_trusted=true`, `timestamp_utc == media_timestamp_utc`, and `media_clock.source=hls_ext_x_program_date_time` with schema version 1. `decode_received_at_utc` and `decode_latency_ms` must remain separate diagnostics.

Check producer timestamps twice and require all four channels to advance:

```bash
curl -fsS http://127.0.0.1:8090/health | jq .
curl -fsS http://127.0.0.1:8090/detections/latest | jq .
sleep 5
curl -fsS http://127.0.0.1:8090/detections/latest | jq .
```

Expected endpoints:

- `/health`
- `/detections/latest`
- `/streams/ch1.mjpg` through `/streams/ch4.mjpg`

Validate upstream Kinesis separately through the read API:

```bash
for camera in ch1 ch2 ch3 ch4; do
  curl -fsS \
    "https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/video/session/${camera}" \
    | jq '{cameraId,playbackMode,expiresIn,hlsUrlPresent:(.hlsUrl|length>0)}'
done
```

Never print or retain signed HLS query strings. For acceptance, require:

- ch1-ch4 decoded-frame capture timestamps remain recent and advance;
- real frames change, not only response bytes;
- `/health.media_clock_ready` is true and every channel reports a trusted matched media clock with bounded decode latency;
- event timestamps are monotonic and close to DynamoDB ingestion time;
- forced HLS expiry/reconnect recovers within the agreed bound;
- socket counts do not accumulate `CLOSE_WAIT`;
- a new DynamoDB record proves current media, decode-receipt, and ingestion timestamps plus schema-v2 provenance.

For an archived vehicle/bbox acceptance gate, use the tracked read-only verifier
with one exact persisted detection JSON and the local model. It keeps signed HLS
URLs internal, requires trusted persisted provenance, selects the nearest actual
fMP4 frame, and exits nonzero for timing, bbox, or semantic mismatch:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_historical_correlation.py \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com \
  --detection-json /path/to/one-sanitized-detection.json \
  --output /tmp/v2x-correlation-frame.jpg \
  --yolo-model /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt \
  --require-yolo
```

Do not treat a legacy row, a CLI-supplied timestamp, a merely nonblank bbox, or
a changed twin JPEG as same-object proof. Require the selected `object_id` in a
`twin_status` response to carry the strict schema-v2 HLS clock provenance and
map to an `actor_present=true` UE5 CARLA `actor_id`, type, role, and transform.
Require three status samples spanning at least two replay seconds, one stable
actor ID, and at least 0.25 m of movement; validate the actor directly in CARLA.

A shared database `object_id` across cameras is not itself identity proof. For
vehicles, require two independently passing historical reports, exact archived
frame hashes, different cameras, bounded transit time, and the pinned ConvNeXt
appearance gate before replay acceptance:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_cross_camera_identity.py \
  --left-report /tmp/ch4-report.json --left-frame /tmp/ch4-frame.jpg \
  --right-report /tmp/ch1-report.json --right-frame /tmp/ch1-frame.jpg \
  --output /tmp/cross-camera-identity.json --device cuda
```

Production association must compute pinned ConvNeXt embeddings for vehicles,
require similarity at least `0.60` for every slow-path vehicle reattachment
(same or cross camera), never share a cache entry when `track_id` is absent,
and persist the association method, similarity, threshold, devices, time, and
distance. Missing appearance evidence fails closed rather than falling back to
proximity alone.
Live vehicle association must also require exact trusted schema-v2 HLS media
time and reject missing, non-finite, or combined localization uncertainty above
2.0 m. Never clamp a large uncertainty into the accepted association radius,
and record rather than silently overwrite a car/truck/bus class conflict.
When two vehicle candidates have insufficient spatial/appearance separation,
reject association, persist bounded ambiguity evidence, and start a distinct
track. Never let greedy input order choose between adjacent plausible cars.
Every tracked camera must provide finite measured `localization.pixel_sigma`
and `localization.calibration_uncertainty_m` no greater than 2.0 m. Missing
values block perception startup; never fill them from rejected exploratory or
matcher-generated calibration rows.

The 24-hour persistence gate is paginated and fail-closed. Require every
camera to have trusted schema-v2 events spanning at least 23 hours and a recent
upload; a query over a 24-hour window is not itself proof of 24-hour history:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_detection_persistence.py \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com
```

Twin camera alignment is a separate gate from channel wiring. The existing
perception CSVs contain only 4-7 local-XZ points per channel, no independent
holdouts, no global landmark IDs, and internally inconsistent shared points;
treat the current camera verifier as diagnostic only. Do not deploy pose, pole,
FOV, or lens changes fitted from those rows. To create acceptance evidence,
survey one shared pole pose and at least 12 globally identified CARLA-XYZ (or
GPS) correspondences per channel, pre-split into at least eight fit points and
four untouched holdouts spanning 50% of image width and 30% of height. Record
the source frame hash and measured intrinsics/distortion. Then require held-out
RMSE/P95/max of 75/125/175 pixels at 1280x960 and all four retained renders.

Fit, deploy, and verify must all call the same tracked camera-transform and
optical-model functions. A missing translation offset means zero; never hide a
default pole displacement in one path. Resolve candidate landmarks directly
from the UE5 map/depth buffer with `build_twin_camera_landmarks.py`; legacy
camera-local XZ converted through the heading under test is circular evidence.
Reject sparse, collinear, clustered, or non-global datasets before fitting.

Feature matchers (SIFT, LoFTR, RoMa, or successors) may propose landmarks but
cannot themselves certify held-out truth. Repeated lane/crosswalk markings can
produce a low numerical loss for the wrong correspondence. Retain the real and
twin source frames, manually/geometrically identify each held-out landmark, and
require an independent road-geometry gate for road edges, lane markings,
horizon, vanishing points, curb/crosswalk topology, and stable map landmarks.
If the retained render visibly contradicts the real view, fail the candidate
even when a point-only threshold passes; do not weaken thresholds or relabel
matcher-generated points to make it green.

Manual four-camera evidence must use one JSON annotation artifact per channel.
Points require `provenance="manually_verified_unique"`, at least eight frozen
train and four untouched holdouts. Road edges, lane markings, and crosswalk
geometry require `provenance="manually_traced_geometry"`, at least three train
and two holdout polylines. Include the exact real/twin frame SHA-256 values;
never translate `manual_verified_static`, matcher proposals, or vague repeated
line points into the accepted provenance labels.

Only in an authorized mutation window with zero Drive sessions, resolve those
twin pixels through a temporary UE5 depth sensor into one optimizer manifest:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/build_twin_calibration_manifest.py \
  /path/to/ch1-annotations.json /tmp/ch1-calibration-manifest.json \
  --camera ch1 \
  --real-frame /path/to/real-ch1.jpg \
  --twin-frame /path/to/twin-ch1.jpg \
  --cameras-json /home/path/V2XCarla/v2x-backend/config/cameras.json
```

The builder must destroy its owned depth sensor in `finally`. Preserve the
manifest's annotation, camera-file, per-camera, real-frame, twin-frame, depth
frame, and map fingerprints. Run `optimize_twin_road_geometry.py` only on this
generated manifest; a hand-converted CSV is not acceptance evidence.

Actor visual proof must also be reproducible across bridge restarts: choose UE5
blueprints with a stable digest rather than Python's randomized `hash()`. For a
same-car gate, require the projected actor bbox/centroid in the matched twin
camera over multiple replay timestamps, not merely `actor_present=true`.

Useful logs:

```bash
journalctl -u v2x-perception.service --utc -n 300 --no-pager
journalctl -u v2x-cloudflared-perception.service --utc -n 200 --no-pager
journalctl -u v2x-perception-link-health.service --utc -n 200 --no-pager
ss -tanp | awk 'NR==1 || /python/ || /CLOSE-WAIT/'
```

Use the tracked dependency-light verifier for the four-feed gate, locally and
again through the browser-selected public perception origin. It requires two
advancing health/detection samples and two different complete JPEG hashes from
each feed, and rejects query-bearing endpoint input:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_live_feeds.py \
  http://127.0.0.1:8090 --max-decode-latency-ms 10000
```

For a bounded Drive/twin/replay regression, run the tracked verifier
observationally first. Never pass `--apply` during planning, read-only diagnosis,
or observational validation: it mutates simulator state by creating sessions
and actors. Omit it entirely from read-only evidence.

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py
```

Only inside an authorized mutation window, after the observational command
reports zero active sessions, run apply mode. It creates two isolated sessions,
verifies correlated Teleport, exercises replay, restores live mode, and cleans
up owned actors in `finally`:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py --apply
```

After one post-schema-v2 persisted detection has passed the historical frame
verifier, use its exact run-scoped object, replay start, and camera for the
same-object twin gate without creating a Drive session:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py \
  --apply --skip-drive \
  --twin-object-id global_car_RUN_ID_TRACK \
  --twin-replay-start 2026-07-10T00:00:00.000Z \
  --twin-camera ch1 \
  --twin-yolo-model \
  /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt
```

The exact-object gate must retain one CARLA actor ID over at least three replay
samples and require a compatible YOLO detection to overlap that actor's
projected 3-D bounding box in each corresponding twin JPEG. The stream's
`twin_hello` must carry the exact UE5 camera actor ID, transform, dimensions,
FOV, all six CARLA lens attributes, and camera-config SHA-256 used for
projection. Configure every lens attribute explicitly per camera so a shared
blueprint cannot leak one channel's optical settings into another. Until the
tracked projection model supports a measured nonzero CARLA `lens_k` or
`lens_kcube`, fail closed rather than treating pinhole projection as equivalent.
Each JPEG must also be preceded by hash-matching `twin_frame` metadata with an
advancing UE5 frame ID and a replay clock no more than 250 ms after the sampled
object clock. Pin the stream fingerprint to the tracked channel config and the
advertised camera actor to the live `sensor.camera.*` transform and optical
attributes. Require before/after capture projection overlap with the same YOLO
bbox, at least 0.50 matched confidence, 0.15 IoU, 0.50 actor coverage, 75% of
the raw actor projection in frame, and an allowlisted YOLO model hash. Project
all live vehicles/walkers and reject foreground occlusion or a neighboring
actor that explains the detection within the fixed exclusivity margin. Apply
target-grade size/visibility thresholds only to the target; retain small or
partly clipped projected actors as potential confounders. Lane lookup may
provide vehicle height and yaw but must never replace the GPS-derived planar
position. Require the reported and independently recomputed raw-to-actor
planar error to remain at or below 0.10 m. Across
the three samples, require distinct JPEGs and image-space detection motion that
agrees with the target actor's projected direction and displacement. CLI
overrides may tighten these floors but must never weaken them.

## Controlled deployment gate

Before changing live services:

1. Confirm the clean worktree commit and successful web/bridge/perception tests.
2. Confirm all simulator operations target only the UE5.5 `carla-rr-maps` worker on ports `2000-2002`. Do not inspect or depend on UE6 paths, units, ports, processes, or evidence.
3. Confirm no active drive session.
4. Stop both repair timers and the hourly restart timer.
5. Capture installed unit hashes, process commands, container image ID, live Git status, tunnel/runtime config, ignored-model/cache hashes, perception Python/pip state, and service logs.
6. Preserve rollback copies of installed units, ignored runtime assets, and the live repository changes.
   Use `scripts/capture-v2x-rollback.sh` in its default plan mode first. After
   the timers are stopped, run `ACTION=capture`, then require `ACTION=verify`
   against the new bundle; verification rehearses tracked, staged, unstaged,
   and untracked repository restoration in an isolated clone without changing
   the live checkout.
7. Let `v2x-carla-rr.service` adopt an already-running validated container through `docker wait`; do not restart or recreate it merely to add supervision.
8. Install one layer at a time and refresh UI/API evidence after each action.
9. Start perception with `/etc/v2x-perception.env` keeping `V2X_PERCEPTION_UPLOAD=false`; require four fresh/changing feeds before enabling production uploads and proving a current DynamoDB record.
10. Restore the previous artifact immediately when its acceptance gate fails. For Quick Tunnels, restore variables around the currently healthy endpoint, never a dead saved URL.
11. Re-enable timers only after the final public/runtime checks pass.

API route reconciliation is plan-first and exact-resource only. The normal service user cannot write API Gateway directly. A separately authorized principal must apply `infra/aws-cli/bootstrap-v2x-deploy-role.sh`; then assume `V2XBackendDeployRole` and run `provision-read-api.sh` with the reviewed API ID, `RECONCILE_LAMBDA=false`, IAM attachment disabled, explicit `PLAN_ONLY=false`, and the plan's `EXPECTED_CURRENT_STATE_HASH`. Do not add API privileges to the Amplify service role.

Prefer source-controlled scripts and systemd units over one-off `nohup` or manual Docker commands. Do not leave source that exists only in the live checkout.

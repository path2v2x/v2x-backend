---
name: path-pc-carla
description: Operate and diagnose the Path PC CARLA/V2X stack at path@100.72.252.40, including the production Unreal Engine 5.5 RR/CARLA 0.10 worker container, drive WebSocket bridge, Vite dashboard, perception/HLS pipeline, Cloudflare and Tailscale transport, systemd supervision, and controlled deployment/rollback gates. Use for any work that reads, tests, changes, deploys, or recovers the Path PC V2X environment; exclude Unreal Engine 6 experiments, which belong to a separate task and runtime namespace.
---

# Path PC CARLA/V2X

Treat this file as an operating procedure, not proof of current state. Re-run the read-only baseline before every intervention.

## Current deployed state and integration hold

Observed through 2026-07-12 12:45 UTC; verify rather than assume:

- Canonical `origin/main`, the clean live checkout, the Amplify mirror, and
  successful production Amplify job 202 are exact commit
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`.
- Live CARLA/Drive use the packaged UE5.5 RR/CARLA 0.10 worker. After the
  scheduled 23:08 PDT restart both services held `NRestarts=0`, all expected
  listeners remained bound, and the all-channel metadata plus local/public
  four-feed verifiers passed.
- A fresh read-only audit at 12:14–12:15 UTC found every V2X service and all
  three mutation-capable timers active with `NRestarts=0`; the expected image,
  shipping-binary, UE5 marker, Richmond OpenDRIVE, six listeners, LIVE twin
  mode, zero active sessions, and four advancing twin camera counters matched.
  The corrected local four-feed verifier passed with two advancing timestamps
  and two distinct JPEG hashes for ch1–ch4. Re-run rather than inheriting this
  result after any service, config, image, or source change.
- Replay synchronization, tick-bound scene snapshots, exact actor-observed
  default lens acceptance, and cleanup are deployed. A bounded replay for
  `global_car_4db7ffc8_2` remained crash-free and returned to LIVE with zero
  sessions, but failed the unchanged final visual gate: no compatible visual
  detection overlapped the projected UE5 actor. Treat this as a genuine
  calibration/localization failure; do not lower thresholds or rerun replay
  before a new accepted candidate exists.
- Durable replay evidence is at
  `/home/path/V2XCarla/v2x-evidence/twin-replay/20260711T0546Z-default-lens-canary/`.
  The verified rollback bundle is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260711T054633Z-default-lens`.
- The HLS producer proactively rotates signed sessions at 240 seconds. The
  deployed candidate passed 660 one-second samples across two rotations with
  no health outage and per-channel maximum latency below 5.75 seconds. Re-run
  that complete gate after any merged perception deployment; old evidence does
  not transfer to a new fingerprint.
- Public `/timeline` is not currently an acceptance pass. The earlier
  Playwright evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T063328Z-current-baseline/`
  shows only CH2 visible while CH1/CH3/CH4 remain black and the header reports
  zero cameras/FPS. A first proxy canary then broke perception when the legacy
  `/video/session/{camera_id}` contract was changed in place; it was rolled back
  exactly and perception recovered without a restart. Commit `141a317` fixes
  the contract split: backend perception retains direct signed Kinesis delivery
  at `/video/session/{camera_id}`, while browsers use opaque same-origin delivery
  at `/video/browser-session/{camera_id}` and child resources remain under
  `/video/proxy/{token}/{resource_id}`. The split Lambda/routes, prefix-scoped
  IAM, one-day state expiry, and independent route throttles were deployed at
  12:40 UTC from reviewed state hash
  `8aa9f567c48dcc4c3bc708d89040e3ef25b50a320cee5b78828f5b49f67b5396`.
  Rollback evidence is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T124017Z-8aa9f567c48d/`.
  Real API proof passed direct ch1 plus opaque proxy master/media playlist reads
  for ch1-ch4. A local production build passed Playwright with four simultaneous
  2560x1920 videos at `readyState=4`, no media errors, all four browser-session
  calls returning 200, and 138 events / 3,806 detections in 24 hours; evidence
  is `/home/path/V2XCarla/v2x-evidence/playwright/20260712T124500Z-local-hls-split/`.
  At 12:46 UTC, more than six minutes after the API apply and therefore beyond
  the producer's 240-second renewal interval, the local four-feed verifier again
  passed with advancing timestamps and distinct JPEGs on every channel;
  `v2x-perception.service` remained active with `NRestarts=0` and current schema-v2
  uploads continued. This specifically closes the regression that forced the
  first proxy canary rollback, but it does not replace the 30-minute/24-hour
  production watch gates.
  This is a candidate-browser pass, not a public-production pass: Amplify is
  still connected to `michaelvu1207/v2x-backend-amplify` at main commit
  `d54f5df`, so the public app has not yet received the browser-route change.
  Require a clean source-controlled Amplify release and repeat the public gate.
- The clean integration worktree is
  `/home/path/.codex/worktrees/v2x-calibration-integration` on
  `codex/v2x-calibration-integration`. It layers the fail-closed calibration,
  physical-intrinsics, identity, persistence, rollback, and GPS-planar-placement
  gates onto current `origin/main` while preserving the newer replay protocol.
  It is not deployed. Never deploy from the dirty recovery worktree.
- The integration candidate also contains a read-only rolling detection-corpus
  exporter, hash-bound observation/contact/tracklet curation, a shared WGS84
  OpenDRIVE projection, honest independent placement metrics, and a bounded
  detection-assisted trajectory fit. The fit is diagnostic by construction;
  it cannot authorize deployment without measured per-camera intrinsics,
  surveyed static/lane evidence, locked whole-track holdouts, bootstrap, RTK,
  and UE5 visual proof. Read
  [references/calibration.md](references/calibration.md) before calibration,
  historical-frame, mapping, or same-car acceptance work.
- The active completion contract is
  `docs/v2x-calibration-completion-contract.md`. Its Fable-reviewed additions
  include clock drift, independent map survey, one-use holdouts, fixed eligible
  denominators/minimum sample counts, blind identity adjudication, per-axis
  pixel scaling, and 30-minute plus 24-hour deployment watches. Do not use an
  older plan as authority.
- Current static evidence still fails. Clean, vehicle-resistant fit/dev/holdout
  composites from three independent KVS windows per camera are retained at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T072000Z-temporal-static-targets-v3/`.
  They are proposal-only, not annotation truth. `build_temporal_static_targets.py`
  now makes window IDs path-independent and defaults to at least three valid
  samples; never opt down to one sample for an acceptance-labelled workflow.
  The latest completed bounded isolated UE5 search is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104045Z-inverse-render-search-v6/`.
  It retained 896 candidates (224 per camera except 223 for ch3) across broad
  pose/FOV ranges. Every selected candidate fails the fixed geometry gate and
  visual review; ch1/ch2/ch3/ch4 road-surface scores are approximately
  0.702/0.540/0.470/0.508 and all remain below the contract. A subsequent cold
  Richmond load did not become ready within ten minutes, so no v7 render corpus
  exists. Do not re-enter a maintenance window until the map loader has a true
  outer process deadline and rollback remains independently armed.
- The decisive static-topology diagnostic is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-ch4-crosswalk-planar-consistency-v1/report.json`.
  A homography fitted to one physical/map crosswalk reproduces that crosswalk,
  but projects the other visible Richmond crosswalks tens to hundreds of pixels
  away. CARLA exposes the paint as eight large aggregate `RoadLines` objects,
  not independently controllable crosswalk objects, so hiding one bad marking
  at runtime is not available and hiding all eight removes the full road-marking
  layer. Camera pose alone cannot repair this map inconsistency. Require the
  actual complete UE5.5 Richmond source map/dependency graph (or an authorized,
  independently surveyed complete road-marking replacement), a fingerprinted
  full cook, and fresh untouched holdouts before production calibration.
- A joint-rig diagnostic that forbids independent per-camera translation is at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T121500Z-joint-visual-selfcal-v1/`.
  The 19 visual parameters are full-rank with condition about 1.5e3, but five
  axes hit bounds and frozen ch1/ch2 road holdouts remain about 98/541 px at
  640-wide. All four overlays were visually inspected; local fits break other
  roads or landmarks. The candidate is rejected and must not be rendered,
  deployed, or used for actor placement.
- The map-source/capacity audit and accepted recovery routes are frozen in
  `docs/v2x-map-correction-recovery-plan.md`. The private
  `SimForgeinc/RFS_Reconstruction` main revision contains April Richmond
  editor assets but remains UE4.26 and has no raw RoadRunner/FBX/OBJ/USD/GIS
  source. The production image is cooked-only. The only local comparison
  workspace with UE5.5 source belongs to the separate UE6 comparison task and
  is ineligible for V2X. A dedicated clean V2X UE5.5 source/engine build needs
  roughly 250 GB; the Path PC root currently has about 6 GB free. A read-only
  audit found 877 GB free on the unmounted Windows NTFS volume, but no Richmond
  source; it was unmounted unchanged and is not a Linux build volume without a
  separate storage decision. Do not delete or reuse another task's workspace
  to create capacity. The UE4 import metadata names the missing authoring file
  as `D:/Work/Simforge/Berkley/Road Runner/28012026/Richmond.fbx`. A Drive
  inventory records a 158 GB Richmond export dated 2026-03-30, but its linked
  folder now returns 404 and read-only Drive/Slack searches found no replacement.
- Exact source-frame evidence for `global_car_4db7ffc8_138` is retained at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T100128Z-object-138-exact/`.
  One representative persisted event per camera is bound to the exact fMP4
  frame at 0 ms media-time error; independent YOLO detection IoU is about
  0.951/0.963/0.870/0.943 for ch1/ch2/ch3/ch4. The four views visually support
  the same white Toyota Camry, but this remains identity proposal evidence, not
  blind-adjudicated identity or world-placement acceptance.
- Cross-model segmentation contact consensus for that exact-frame sample has
  three accepted proposals (ch1/ch2/ch4) with median mask IoU about 0.983 and
  maximum native contact disagreement 1.5 px x / 1.75 px y. Ch3 is correctly
  rejected as clipped. The report is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T100128Z-object-138-exact/contact-consensus.json`.
  It is useful proposal evidence but fails four-camera coverage and independent
  contact review. The full frozen observation ledger contains 369 trusted
  vehicle rows and zero acceptance-eligible rows because reviewed contacts,
  static calibration, and independently adjudicated identity are absent.
  Consensus schema v2 must load the
  hash-bound capture report, cover its entire fixed denominator, validate masks
  and covariance, and apply native x/y disagreement limits independently.
  Producer-time samples that formerly appeared to show 0 ms phase/drift are
  now rejected as a shared zero-residual ingest timestamp grid. The fail-closed
  report is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T101000Z-kvs-timestamp-drift-v3/report.json`;
  it also has only two windows/4.43 hours versus four windows/12 hours and lacks
  independent exposure/UTC truth.
- The recovery worktree contains rejected exploratory camera CSVs and a dirty
  `config/cameras.json`. Preserve them as user-owned diagnostics, but never
  stage, glob, fit, promote, or deploy them.
- The integration candidate now shares one complete actor-observed CARLA
  default lens tuple across rig, manifest builder, optimizer, and replay
  verifier. Configured lens overrides remain a hard safety hold and no lens
  attributes are written at runtime.
- Proposal-only SIFT diagnostics for the retained source pairs found distributed
  proposal counts ch1/ch2/ch3/ch4 = 1/1/6/1. None reaches the 12-point manual
  evidence minimum. Outputs at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260711T064950Z-acquisition-deficit/proposals/`
  are `acceptance_eligible=false` and must not be promoted.
- The clean `path2v2x/co-perception` reference commit
  `c4ec4730bbabd915d62fad7f4acecc8488be4533` has been re-audited with every
  preserved channel CSV at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-legacy-co-perception-audit-v2/audit.json`.
  It contains only 7/5/4/4 camera-local points, no measured-intrinsics artifact,
  frame hashes, global landmark IDs, survey provenance, or frozen holdouts.
  Leave-one-out RMSE is about 1.49/7.60/4.84/1.85 m for ch1/ch2/ch3/ch4;
  ch1/ch2/ch4 geometry is collinear, and the active script's “Channel 4” comment
  actually matches the ch1 CSV. Use its nominal K and transforms only as a
  derived diagnostic baseline, never as physical or held-out calibration truth.
- Playwright CLI evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260711T073035Z/` proves the
  `/timeline` archive workflow, four HTTP-200 video sessions, replay control,
  a visible physical ch4 car, and cleanup to LIVE/zero sessions. It also proves
  the strict geometric gate fails: the corresponding twin actor is not visibly
  placed and road/crosswalk geometry is misregistered. Treat this as
  counter-evidence, not acceptance. Refresh the browser evidence after any
  deployed candidate; prior screenshots never transfer to a new fingerprint.
- Fresh local Playwright CLI baseline at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T122000Z-calibration-baseline/`
  shows all four `/live` physical feeds visible, but `/timeline` still renders
  only ch2 while ch1/ch3/ch4 are black, the global header reports zero cameras
  and zero FPS, and the object table is stale. This is current counter-evidence;
  infrastructure HTTP success and the passing four-feed CLI verifier do not
  make the timeline UI an acceptance pass.

## Safety boundaries

- Work locally when already on `path-B860I-AORUS-PRO-ICE`; do not SSH back into the same host.
- From another host, use the configured SSH/Tailscale connection to `path@100.72.252.40`. Do not embed credentials in commands or source.
- Make source changes only in a clean Codex worktree. Treat `/home/path/V2XCarla/v2x-backend-dev` as a reference candidate, not as proof that it is clean; verify its status and fail the deployment gate if it is dirty.
- Do not overwrite `/home/path/V2XCarla/v2x-backend` until a controlled deployment gate. It may run active services and contain live-only work.
- Preserve `/home/path/V2XCarla/v2x-backend-backups/` and take a fresh rollback snapshot before deployment.
- Use only the packaged Unreal Engine 5.5 worker container `carla-rr-maps` for production V2X simulator work. The accepted image is RR/CARLA 0.10 and its runtime reports `5.5.0-0+UE5`.
- Calibration batch rendering may use the same approved image in the tracked,
  non-restarting `v2x-calibration-ue5` container on loopback ports 2300-2302.
  This Path PC has a 16 GiB GPU and cannot safely run that worker alongside the
  production worker: a dual-worker Richmond load produced a bounded Vulkan OOM
  while production fingerprints remained stable. Use a rollback-captured,
  zero-session maintenance window, hold all three mutation-capable timers, stop
  Drive and the production UE5 worker, run one isolated batch, then stop the
  owned calibration container and fully restore production/timers. Never point
  an optimizer at production ports 2000-2002.
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

Observed through 2026-07-12 UTC; verify rather than assume:

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
| Amplify repository | temporary `michaelvu1207/v2x-backend-amplify` mirror at exact canonical SHA with successful sync workflow/webhook; preferred direct `path2v2x/v2x-backend` attachment still requires an organization-owner policy decision |

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

Validate video delivery separately through both read-API contracts. Perception
must receive direct Kinesis delivery and must never create proxy state; the
browser-only contract must report `delivery=SAME_ORIGIN_PROXY` without exposing
the Kinesis origin or signed query. Never point perception at the browser route:

```bash
api=https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com
curl -fsS "${api}/video/session/ch1?max_fragments=2" \
  | jq '{cameraId,playbackMode,delivery,maxMediaPlaylistFragmentResults}'
for camera in ch1 ch2 ch3 ch4; do
  curl -fsS "${api}/video/browser-session/${camera}?max_fragments=2" \
    | jq '{cameraId,playbackMode,delivery,expiresIn,
           proxyOrigin:(.hlsUrl|split("/video/proxy/")[0]),
           leaksSignedOrigin:(.hlsUrl|contains("kinesisvideo") or contains("SessionToken"))}'
done
```

Never print or retain direct signed HLS query strings or opaque proxy token
paths. The tracked proxy design stores the Kinesis token only in encrypted,
short-lived `hls-proxy/v1/` state, recursively rewrites playlists, HMAC-binds
child resources, restricts the exact Kinesis origin/basenames, rejects redirects,
and caps raw fragments at 4 MiB. A larger real fragment fails the Lambda/API
Gateway transport gate; do not raise the bound. For acceptance, require:

- ch1-ch4 decoded-frame capture timestamps remain recent and advance;
- real frames change, not only response bytes;
- `/health.media_clock_ready` is true and every channel reports a trusted matched media clock with bounded decode latency;
- event timestamps are monotonic and close to DynamoDB ingestion time;
- forced HLS expiry/reconnect recovers within the agreed bound;
- socket counts do not accumulate `CLOSE_WAIT`;
- a new DynamoDB record proves current media, decode-receipt, and ingestion timestamps plus schema-v2 provenance.

For an archived vehicle/bbox acceptance gate, use the tracked read-only verifier
with one exact persisted detection JSON and the local model. It keeps HLS
session/proxy URLs internal, requires trusted persisted provenance, selects the nearest actual
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
the source frame hash and measured intrinsics/distortion. The precision gate at
1280x960 is held-out point RMSE/P95/max no worse than 10/16/24 pixels and road
geometry RMSE/max no worse than 6/12 pixels, plus all four retained renders.
Scale these limits by `manifest.width/1280` when residuals use the native
real-camera pixel space; do not apply 1280-wide limits unchanged to 2560-wide
evidence.
The former 75/125/175 point limits were framing diagnostics and must never be
used as calibration acceptance.

The historical UE4.26 Richmond `Roads_Marking_Layer0` asset is not a UE5.5
shortcut. A hash-locked, read-only isolated mount caused the approved worker to
exit 139 with `Signal 11` before RPC readiness; evidence is at
`/home/path/V2XCarla/v2x-evidence/calibration/20260712T091606Z-road-overlay-maintenance/`.
A second attempt resaved the mesh and all 13 copied dependencies with UE5.5,
duplicated it through the Asset Tools API into the map's exact CARLA package
path, and completed a Linux cook with zero warnings. The resulting
`sha256:035ce69294773fb703b143f9e4d99d7ef194e65db5558b0a691b13359d65e4e1`
isolated image still timed out loading Richmond and exited 139. Evidence and
the verified rollback bundle are at
`/home/path/V2XCarla/v2x-evidence/calibration/20260712T092752Z-ue55-road-marking-conversion-v1/`
and
`/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T094404Z-mapfix`.
Never mount, inject, or retry either transplanted asset. Any marking correction
must be reimported or edited inside the actual CARLA UE5.5 source project and
cooked as a complete map build. Then compare it offline and accept it against
independent surveyed/held-out geometry before it can enter a deployment gate.

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
Use `propose_twin_calibration_annotations.py` only as a bounded manual-review
aid. It hashes both frames, requires mutual Lowe-ratio SIFT matches, distributes
proposals across configurable cells, bounds encoded and decoded image sizes,
and atomically refuses output overwrite. Its schema intentionally lacks
acceptance point/road fields and every proposal carries
`provenance="matcher_proposal_only"` plus `acceptance_eligible=false`:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/propose_twin_calibration_annotations.py \
  --camera ch1 --real-frame /path/to/ch1-real.jpg \
  --twin-frame /path/to/ch1-twin.jpg \
  --output /path/to/ch1-proposals.json
```

Never translate this file mechanically into manifest annotations. A human must
independently establish a unique semantic/world identity, freeze the split, and
write accepted provenance; the manifest builder must continue to reject the
proposal schema directly.

Semantic inverse rendering is the preferred diagnostic search before manual
acceptance annotation. Use `scripts/v2x-calibration-worker.sh` and
`render_semantic_calibration_candidate.py` to produce hash-bound, synchronized
RGB, raw semantic, raw instance, and metric-depth buffers for one explicit
candidate. The renderer must reject the production container/port, verify the
approved image and Richmond OpenDRIVE fingerprints, use one CARLA frame for all
buffers, destroy all owned sensors, and keep every result
`acceptance_eligible=false`.

For a bounded multi-candidate fit/dev search, use
`apps/bridge/tools/optimize_inverse_render_camera.py` only against loopback
`127.0.0.1:2300` after the isolated worker launcher has loaded and verified
`/Game/Carla/Maps/Richmond_Field_Station_Richmond_CA`, the approved image, the
exact OpenDRIVE hash, and zero pre-existing sensors. The optimizer rejects
holdout input, constrains physical translation/rotation/FOV, captures
synchronized RGB plus metric depth, scores reviewed road traces and road
surface/near-field visibility, retains every render/score, and never edits
`cameras.json`. Its numerical minimum is a diagnostic proposal. Inspect every
overlay and run the immutable held-out manifest gate before considering a
candidate. A visually wrong horizon, vanishing direction, road edge, landmark,
or required paint topology is a failure regardless of the scalar objective.
`scripts/v2x-calibration-worker.sh` accepts a non-default candidate only when
both `V2X_CALIBRATION_IMAGE` and its exact
`V2X_CALIBRATION_EXPECTED_IMAGE_ID` are supplied. This is a fingerprint gate,
not authorization to use a transplanted asset or an unreviewed image.

The current Richmond RR/UE5 build was observed to emit semantic tag 11 for 100%
of static pixels and instance sentinel 65535 for 100% of static pixels, even
while RGB and depth were valid. Retain those buffers as failure evidence; never
claim class-aware alignment from them. Build diagnostic road-paint/curb targets
from retained RGB, depth, reviewed topology, and class-specific masks instead.
Score robust symmetric contour distance, P95/max, tolerance precision/recall,
and topology separately. A lower clipped mean does not promote a candidate when
P95, tolerance F1, a required semantic class, or the retained visual overlay
regresses. Missing/mismatched map paint is a map blocker, not a reason to mask a
required held-out class or weaken the road-geometry gate.

Freeze the static camera before using vehicles. For a real vehicle with no close
UE5 mesh, treat blueprint family and dimensions as nuisance variables and use a
robust silhouette centroid/midpoint only as a lower-weight cue alongside visible
contour, wheel/road contact, projected 3-D extent, multi-camera timing, road
legality, and temporal smoothness. Never let midpoint agreement alone recalibrate
the camera or certify same-car placement.

Manual four-camera evidence must use one JSON annotation artifact per channel.
Capture observational pairs with `capture_twin_calibration_pairs.py` only
against a twin protocol that sends a `twin_frame` JSON packet immediately
before each binary JPEG. Pass the exact active `--cameras-json`; the capture
must bind the JPEG hash, channel ID, LIVE mode, CARLA frame/timestamp, capture
gap, whole config hash, and selected camera hash. The older binary-only/twin
clock sequence is diagnostic and must fail this capture gate.
Points require `provenance="manually_verified_unique"`, at least eight frozen
train and four untouched holdouts. Road edges, lane markings, and crosswalk
geometry require `provenance="manually_traced_geometry"`, at least three train
and two holdout polylines; infinite-line evidence is not accepted. Include the
exact real/twin frame SHA-256 values and decode both retained images to verify
their actual dimensions. Also freeze the exact `cameras.json` SHA-256 that
produced the annotated twin render and reject a manifest build against any
other config. Reject duplicate real/twin point pixels across train and holdout,
zero-length or duplicate/resampled train/holdout polylines, collinear/clustered
point sets, holdouts copied from fit geometry, and blank/duplicate semantic
landmark descriptions. Reject annotated twin pixels whose 3x3 depth
neighborhood crosses a geometry discontinuity; a plausible center depth alone
does not establish frozen world truth. Retain the numeric 3x3 neighborhood
range/deviation evidence for every point and polyline vertex. Never translate
`manual_verified_static`, matcher proposals, or vague repeated line points into
the accepted provenance labels.

Do not treat repeated nominal `fx/fy/cx/cy` values in `cameras.json` as
measured intrinsics. Each camera requires an `intrinsics_calibration` block
backed by a retained checkerboard or ChArUco JSON result artifact: exact
SHA-256, one unique SHA-256 per accepted source image, at least 10 accepted
calibration images, no more than 2 px calibration RMS, matching image
resolution and camera matrix, and finite Brown-Conrady `k1/k2/p1/p2/k3`. Pass
the actual result with `--intrinsics-artifact` and repeat
`--intrinsics-source-image` for every declared source hash; the manifest
builder must decode and hash every retained calibration image, then parse and
compare the normalized result to `cameras.json` before it connects to CARLA or
spawns a depth sensor. Quantify the full measured physical
model against the deployed UE5 centered-pinhole render over the image; an
optical mismatch above 0.25 px keeps deployment closed until a shared render
distortion or physical-feed undistortion path is implemented and verified.

Generate a dimensioned board with the tracked acquisition tool, print it at
100% scale, and verify one square with a physical ruler before capture:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/calibrate_camera_intrinsics.py generate-board \
  --output /path/to/checkerboard-9x6-25mm.svg \
  --inner-columns 9 --inner-rows 6 --square-mm 25
```

For each fixed physical camera, acquire at least 10 sharp, unique fit images
and two untouched holdouts at its native resolution. Bind the camera/channel,
resolution, crop, focus/zoom state, board hash, capture times, and source
hashes. Move and tilt the board across every image edge/corner with at least 15
degrees of tilt spread and 1.3x distance variation; do not crop, resize,
digitally warp, or reuse frames. Obtain site-access and traffic-safety
authorization before roadside capture, and re-capture frozen landmarks after
the session to prove the camera mount did not move. Calibrate with one
`--image` argument per fit image and one `--holdout-image` per holdout:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/calibrate_camera_intrinsics.py calibrate \
  --image /path/to/ch1-board-01.png \
  --image /path/to/ch1-board-02.png \
  `# repeat for at least 10 unique accepted images` \
  --holdout-image /path/to/ch1-board-holdout-01.png \
  --holdout-image /path/to/ch1-board-holdout-02.png \
  --output /path/to/ch1-intrinsics.json \
  --report /path/to/ch1-intrinsics-report.json \
  --inner-columns 9 --inner-rows 6 --square-mm 25
```

The tool must reject decode failures, mixed resolutions, duplicate source
hashes, fewer than 10 accepted fits or two disjoint holdouts, poor edge/corner,
tilt, or distance coverage, non-finite output, fit or holdout RMS above 2 px,
and held-out per-corner max error above 5 px. Preserve the board hash, every
source image, artifact, report, and the artifact SHA-256 copied into
`cameras.json`.

Only in an authorized mutation window with zero Drive sessions, resolve those
twin pixels through a temporary UE5 depth sensor into one optimizer manifest:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/build_twin_calibration_manifest.py \
  /path/to/ch1-annotations.json /tmp/ch1-calibration-manifest.json \
  --camera ch1 \
  --real-frame /path/to/real-ch1.jpg \
  --twin-frame /path/to/twin-ch1.jpg \
  --intrinsics-artifact /path/to/ch1-intrinsics.json \
  --intrinsics-source-image /path/to/ch1-charuco-01.png \
  --intrinsics-source-image /path/to/ch1-charuco-02.png \
  --depth-frame-output /path/to/ch1-depth.bgra \
  --cameras-json /home/path/V2XCarla/v2x-backend/config/cameras.json
```

The builder must destroy its owned depth sensor in `finally`. Preserve the
manifest's annotation, camera-file, per-camera, real-frame, twin-frame, depth
frame, map, and deployment-model fingerprints. The deployment model must freeze
the surveyed anchor, unadjusted pitch/yaw/roll/FOV, and all six UE5 lens
attributes so the fitted absolute camera can be translated back into tracked
`twin_pose` fields without relying on live state. Run
`optimize_twin_road_geometry.py` only on this generated manifest; a
hand-converted CSV is not acceptance evidence.
Retain the exact raw BGRA depth buffer, including its SHA-256 and byte count;
the manifest alone is insufficient evidence for depth-derived world points.

At optimization time, pass the exact retained annotations, real frame, twin
frame, cameras file, intrinsics artifact, and every calibration source image
again. The optimizer must re-hash all inputs, decode and match every declared
source image, re-hash the selected canonical camera object, and compare the
calibration block with both `cameras.json` and the parsed artifact; a direct
`optimize_manifest()` call without this binding is non-acceptable:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/optimize_twin_road_geometry.py \
  /path/to/ch1-calibration-manifest.json \
  --output /path/to/ch1-calibration-report.json \
  --annotations /path/to/ch1-annotations.json \
  --real-frame /path/to/real-ch1.jpg \
  --twin-frame /path/to/twin-ch1.jpg \
  --cameras-json /path/to/cameras.json \
  --intrinsics-artifact /path/to/ch1-intrinsics.json \
  --intrinsics-source-image /path/to/ch1-charuco-01.png \
  --intrinsics-source-image /path/to/ch1-charuco-02.png \
  --depth-frame /path/to/ch1-depth.bgra
```

The optimizer must reconnect read-only to the UE5.5 worker, verify the active
map name and OpenDRIVE SHA-256, record the host/port endpoint, and recompute the
absolute camera anchor from the verified config. In the same authorized
zero-session mutation window, spawn one temporary depth sensor at that exact
transform, compare fresh and retained depth at every annotated pixel, derive
world truth from the fresh render, and destroy the sensor in `finally`. Any
baseline, deployment-model, map-content, retained-depth, or feature-world
mismatch is a hard failure. No other actor or service mutation is allowed.

The optimizer must fit true 6-DoF extrinsics (CARLA x/y/z plus
pitch/yaw/roll), FOV, principal point, and radial distortion. Preserve the
fully unconstrained solution as diagnostic optical evidence, but never deploy
it directly: pose, principal point, and distortion are partly degenerate. Run
a second bounded optimization through the exact production UE5/verifier model
(currently centered principal point and zero modeled radial distortion), emit
the candidate `twin_pose`, and prove a sub-pixel optical plus exact transform
round trip. If the independently measured principal point or distortion cannot
be represented by that shared model, keep deployment closed; never copy the
optimizer's `k1` into CARLA `lens_k`, because those coefficients are not known
to be equivalent. A parameter at its search bound, an underconstrained fit, or
a green unconstrained fit paired with a failing deployable held-out fit is a
failure, not a calibration result.

Actor visual proof must also be reproducible across bridge restarts: choose UE5
blueprints with a stable digest rather than Python's randomized `hash()`. For a
same-car gate, require the projected actor bbox/centroid in the matched twin
camera over multiple replay timestamps, not merely `actor_present=true`.

A shared persisted object ID across cameras is diagnostic, not identity proof.
Run `apps/perception/tools/verify_cross_camera_persistence.py` against the
public API and require trusted schema-v2 media clocks, matching perception run,
finite GPS/bbox, localization uncertainty no greater than 2 m, plausible
transit time/speed, and a persisted
`identity_association.method="cross_camera_spatiotemporal_convnext"` with the
previous device, ConvNeXt similarity at least 0.60, and consistent distance.
Current production records that omit this association evidence must fail even
when their object IDs happen to match; visual same-car proof remains a separate
required gate.

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
  http://127.0.0.1:8090
```

For a bounded Drive/twin/replay regression, run the tracked verifier
observationally first. Never pass `--apply` during planning, read-only diagnosis,
or observational validation: it mutates simulator state by creating sessions
and actors. Omit it entirely from read-only evidence.

```bash
PYTHONPATH=/home/path/V2XCarla/v2x-backend/apps/bridge \
  /home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py
```

Only inside an authorized mutation window, after the observational command
reports zero active sessions, run apply mode. It creates two isolated sessions,
verifies correlated Teleport, exercises replay, restores live mode, and cleans
up owned actors in `finally`:

```bash
PYTHONPATH=/home/path/V2XCarla/v2x-backend/apps/bridge \
  /home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py --apply
```

After one post-schema-v2 persisted detection has passed the historical frame
verifier, use its exact run-scoped object, replay start, and camera for the
same-object twin gate without creating a Drive session:

```bash
PYTHONPATH=/home/path/V2XCarla/v2x-backend/apps/bridge \
  /home/path/V2XCarla/carla-venv-310/bin/python \
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
FOV, lens values, and camera-config SHA-256 used for projection. Until the
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
actor that explains the detection within the fixed exclusivity margin. Across
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

API route reconciliation is plan-first and exact-resource only. The normal
service user cannot write API Gateway or execution-role IAM directly. A route-
only repair may still use `RECONCILE_LAMBDA=false` with IAM attachment disabled,
but it cannot deploy the HLS proxy. For that candidate, a separately authorized
IAM principal must first review the generated plan, then run
`provision-read-api.sh` with the reviewed API ID, `RECONCILE_LAMBDA=true`,
`ATTACH_DDB_READ_POLICY=true`, explicit `PLAN_ONLY=false`, and the plan's
`EXPECTED_CURRENT_STATE_HASH`. Despite the legacy variable name, that reviewed
inline policy also adds prefix-scoped S3 get/put/delete for opaque proxy state.
Require exact `/video/proxy/{token}/{resource_id}` route parity, retained prior
inline-policy rollback evidence, generated-Lambda tests, a real fragment below
4 MiB, and refreshed browser network/playback proof. Do not add API privileges
to the Amplify service role.

Prefer source-controlled scripts and systemd units over one-off `nohup` or manual Docker commands. Do not leave source that exists only in the live checkout.

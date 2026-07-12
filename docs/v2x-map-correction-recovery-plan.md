# V2X Richmond UE5.5 map-correction recovery plan

Status: acceptance-blocking, source-only plan. No production or live service
mutation is authorized by this document.

## Proven failure

- The fixed completion contract is
  `docs/v2x-calibration-completion-contract.md`.
- The camera-independent planar test at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-ch4-crosswalk-planar-consistency-v1/`
  fits one coplanar Richmond crosswalk exactly and projects other visible
  crosswalks tens to hundreds of pixels from their physical locations.
- The bounded v6 inverse-render corpus at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104045Z-inverse-render-search-v6/`
  has 896 candidates and no passing camera.
- The shared-cluster 19-parameter diagnostic at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T121500Z-joint-visual-selfcal-v1/`
  is full-rank, but five parameters hit bounds and the frozen ch1/ch2 road
  holdouts remain approximately 98/541 px at 640-wide. It is rejected.
- Runtime inspection exposes eight aggregate `RoadLines` objects. Individual
  bad crosswalks cannot be disabled; hiding all aggregates removes the whole
  road-marking layer.

## Source and capacity audit

- `SimForgeinc/RFS_Reconstruction` main at
  `d14da5b57bbe4356930a2b9a926a675692e18547` is an Unreal 4.26 project. It has
  updated April Richmond `.uasset`/`.umap` files, including
  `New_RFS/Richmond_Field_Station_Richmond_CA.uasset`, but its complete Git tree
  has no `.rrscene`, FBX, OBJ, USD, glTF, OpenDRIVE, GIS, Blender, Maya, or 3ds
  Max source for Richmond.
- The retained UE4 import metadata names the missing original export exactly:
  `D:/Work/Simforge/Berkley/Road Runner/28012026/Richmond.fbx`. The file is not
  present in the repository, Path PC V2X-owned directories, or the Path PC's
  Windows volume.
- The connected `Reconstruction Map Overview` sheet records a completed 158 GB
  Richmond Unreal export dated 2026-03-30, but its linked Drive folder
  `1wWDuWUV6wSuFE3MnPTfTXljezI6xajiI` is no longer accessible to the connected
  account (Drive API 404). Drive and Slack searches found no replacement FBX,
  RoadRunner project, or accessible export archive.
- The approved 42 GB `ghcr.io/simforgeinc/carla-rr-maps:0.10.0` image is a
  cooked runtime. It has no source label or editable CARLA project.
- The local `/home/path/V2XCarla/Carla` checkout is a dirty UE4 development
  project and is not an eligible UE5.5 map source.
- A local UE5.5 comparison workspace belongs to the separate UE6 comparison
  task and uses cross-task content. It is excluded from V2X inspection,
  modification, build, and evidence.
- The Path PC root filesystem currently has about 6 GB free. Its second NVMe
  has an unmounted Windows NTFS volume with about 877 GB free; a read-only audit
  found no Richmond source and left it unmounted. A dedicated clean CARLA UE5.5
  source plus engine/content build needs roughly 250 GB on a Linux-compatible
  filesystem. Reserving a large loop-backed ext4 image or repartitioning the
  Windows volume is a separate storage authorization; do not assume it, and do
  not reuse, delete, or mutate another task's workspace to manufacture capacity.

## Accepted recovery routes

### Route A — recover the original map-authoring source (preferred)

Acquire the exact Richmond RoadRunner project/export or equivalent raw
geometry with materials and georeference, plus source revision/provenance. It
must contain the physical road edges, lane markings, and every visible
crosswalk. Preserve the current map origin and the accepted OpenDRIVE hash
unless an independent survey explicitly selects and versions a replacement.

### Route B — independently reauthor the complete road layer

If the original source no longer exists, acquire an independently surveyed
site control network: at least six stable landmarks and ten non-collinear
distances with datum/elevation uncertainty, plus surveyed road edges, lane
center/edge lines, and crosswalk vertices. Rebuild the complete road/marking
layer—not a camera-specific overlay—from that common world geometry.

Neither route may infer world truth from persisted detection GPS, current
CARLA actor positions, lane snapping, proposal camera poses, or per-camera
homographies.

## Implementation gate

1. Provision at least 250 GB of dedicated Linux-compatible free space without
   deleting another task's data. The unused Windows volume is a capacity option
   only after explicit authorization of its storage impact.
2. Create a dedicated clean V2X CARLA `ue5-dev` worktree and dedicated CARLA
   Unreal Engine 5.5 build path. It may not share content, build products,
   processes, ports, or evidence with UE6.
3. Import the complete source/dependency graph into the actual CARLA UE5.5
   project. Keep the map, static ground, road-line materials, traffic controls,
   semantic tags, OpenDRIVE, and package manifest versioned together.
4. Cook a complete fingerprinted CARLA package/image. A loose mount, isolated
   `.uasset`, debug drawing, or partial package transplant is an automatic
   failure.
5. In a rollback-captured zero-session V2X maintenance window, boot only the
   approved isolated UE5.5 worker on ports 2300–2302. Apply the 180-second hard
   map-load deadline and fail on crash, Vulkan/OOM signature, map/hash drift,
   unexpected sensors, or any production restart-counter change.
6. Render fit/dev views for all four cameras, then evaluate one newly frozen,
   untouched time-disjoint holdout. Burn that holdout after first inspection.
7. Pass at 1280×960: road-polyline RMSE/max ≤6/12 px and static landmark
   RMSE/P95/max ≤10/16/24 px for every camera, with no parameter bound hit,
   full data Jacobian rank, condition ≤1e8, and independent map survey
   RMSE/max ≤0.25/0.50 m.
8. Only after the static gate passes may the existing exact same-car/contact
   corpus enter world localization, identity, UE5 actor placement, replay,
   deployment, Playwright, and 30-minute/24-hour stability gates.

## Current executable action

Remain fail-closed. Recover the exact original `Richmond.fbx`/RoadRunner source
or Route B survey control, restore access to the recorded 158 GB export if it
contains that source, and provision dedicated disk capacity. Until both source
truth and capacity exist, continue only hash-bound offline evidence/tooling and
regression work; do not deploy a camera pose or place acceptance actors.

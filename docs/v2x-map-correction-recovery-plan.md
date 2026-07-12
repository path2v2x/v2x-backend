# V2X Richmond UE5.5 map-correction recovery plan

Status: acceptance-blocking, isolated UE5.5 migration in progress. No
production or live service mutation is authorized by this document.

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
- The latest retained `Richmond_NR.umap` and
  `New_RFS/Richmond_Field_Station_Richmond_CA.uasset` are materially distinct
  from the older deployed import. The scene asset preserves a newer source
  filename (`C:/Users/123/Downloads/Unreal_Exports/` followed by
  `Richmond_Field_Station_Richmond_CA.fbx`) and explicit `Terrain_Marking` and
  `Roads_Marking` hierarchies. This is enough to justify an isolated editor
  migration/visual test, but it is not raw authoring source, survey truth, or
  acceptance evidence.
- The hash-bound road-core materialization at
  `/mnt/v2x-ue5/evidence/april-road-core-dependencies/` verifies 29/29 LFS
  objects from `d14da5b` with zero primary material/texture import gaps for the
  level, scene, road, curb, gutter, sidewalk, and two marking layers. This is
  sufficient for the first UE5 topology rejection test. It is not a complete
  Richmond dependency graph, cooked package, or survey proof; thousands of
  unrelated prop assets remain unmaterialized and cannot be omitted from a
  production candidate.
- `scripts/migrate-richmond-road-core.sh` now performs a dry-run-first,
  path-confined, hash-gated migration of that exact 29-object set. Its fixture
  test proves tamper rejection and rollback capture. The first isolated
  migration passed at
  `/mnt/v2x-ue5/evidence/april-road-core-dependencies/migration-20260712T151559Z/`:
  all 29 objects were newly created in the clean CARLA `ue5-dev` project's
  `Content/Berkley` tree and the retained evidence manifest verifies. This is
  source staging only; it is not an editor conversion, topology render, cook,
  or acceptance result.
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
- The root filesystem remains intentionally unused for the build. The approved
  secondary-volume route is now implemented as one removable 500 GB sparse
  ext4 image at `/mnt/v2x-capacity/v2x-ue5-build.ext4`, mounted at
  `/mnt/v2x-ue5` through an `ntfs-3g` outer mount. The normal ext4 format has
  about 32 million inodes and currently leaves more than 400 GB free. The first
  kernel-`ntfs3` attempt and a second low-inode `largefile4` attempt were
  discarded without touching pre-existing Windows data. No persistent mount
  entry exists; both mounts and physical sparse allocation must be verified
  after reboot.

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
   deleting another task's data. **Satisfied for isolated build work** by the
   removable `/mnt/v2x-ue5` image above; this does not authorize production.
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

Finish the clean `CarlaUnreal/UnrealEngine` `ue5-dev-carla` dependencies and
bounded build plus the official CARLA `ue5-dev` content materialization. Open
the already hash-staged 29-file road core in that actual project and save its
UE5 conversion only inside the isolated build workspace. First create an
isolated road-only topology probe, render the four fixed camera regions, and
test crosswalk/road topology on fit/development imagery. Reject the retained
asset route immediately if the same camera-independent contradiction remains.
Only if that probe improves may the full Richmond dependency graph be
materialized, migrated, and cooked as a complete fingerprinted CARLA package.
Even then, production remains closed until independent survey and
measured-intrinsics gates are supplied and pass. Do not deploy a camera pose or
place acceptance actors before those gates.

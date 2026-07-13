# Map/LiDAR development registration plan

## Scope and safety

Implement an offline, immutable-evidence registration tool. It reads only a
hash-bound raw LAS/LAZ, its independent validation and authoritative metadata,
the exact OpenDRIVE document, a map-geometry export, and manual feature
annotations. It never connects to CARLA, Unreal, AWS, or a running service and
never writes deployment configuration. Unreal Engine 6 and the untouched
future holdout vault are out of scope.

## Evidence contract

1. Decode the entire LAS/LAZ and verify its byte hash, point count, bounds,
   quantization scales, projected CRS, and acquisition metadata against an
   independent validation artifact. Require projected horizontal and explicit
   vertical CRS axes in metres, and reject non-metre OpenDRIVE georeferences.
2. Require a manual annotation artifact whose hashes bind the raw cloud,
   validation, metadata, OpenDRIVE, and geometry export. Every feature has a
   globally unique identity, approach identity, immutable `fit` or `holdout`
   split, stable map-polyline reference, and manually selected raw-cloud point
   indices whose recorded XYZ values reproduce the decoded points.
3. Recompute the geometry export's full content provenance: exporter source,
   pair/camera manifests, all retained real/twin frame bytes and dimensions,
   camera-object hashes, stable feature identities, and a separate retained raw
   CARLA map-source export. Rebuild every lane boundary, crosswalk, and
   environment object from that raw export, rebind exact OpenDRIVE road-mark
   ranges, recompute camera projections, rerender the overlays, and require the
   geometry JSON and overlay bytes to match the retained evidence exactly.
4. Reject split leakage at the feature, source-feature, raw-point,
   physical-control, or geometric/resampled-polyline level, plus missing
   approaches, non-finite/zero-length polylines, coarse coordinate resolution,
   CRS disagreement, stale OpenDRIVE/geometry mismatch, or insufficient
   geometric rank before optimization. Exact endpoint/coordinate reuse is a
   hard failure, and fit/holdout geometry must remain more than 0.50 m apart in
   both map and LiDAR/survey coordinates after finite-segment resampling;
   distinct self-declared IDs cannot bypass this gate.
5. Bind the current survey to three actual retained files: raw observation CSV,
   surveyor-license PDF, and instrument-calibration PDF. Require exact byte
   hashes, sizes, roles, types, licensed-surveyor identity, instrument identity,
   provider/project/source identity, and observation-column schema. Read
   coordinates only from the CSV, resolve each control independently from the
   rebuilt map geometry, and require at least six distinct eligible stable
   landmarks (traffic lights, traffic signs, or explicit stable landmarks).
6. Compare OpenDRIVE, LiDAR, and survey horizontal CRS by WKT/EPSG, datum,
   metre units, and coordinate epoch. If they differ, require a separate
   hash-bound reconciliation artifact with the exact PROJ pipeline,
   source/target WKT and epochs, licensed source-deliverable identity, and at
   least six non-collinear survey-bound checkpoints. Execute the pipeline and
   recompute its residuals; a mismatched CRS cannot silently pass.

## Model and evaluation

1. Fit exactly one site-wide SE(2) transform `(tx, ty, yaw)` and one additive
   Z bias. No per-camera, per-road, per-approach, nonlinear, or local-warp
   degrees of freedom are allowed.
2. Score each finite polyline symmetrically in both map-to-LiDAR and
   LiDAR-to-map directions with point-to-nearest-segment normal residuals.
   Normalize each direction and feature so density and polyline length do not
   dominate the fit.
3. Keep holdouts outside the objective. Report fit and holdout identities,
   global/per-approach/per-feature horizontal RMSE/max, symmetric Hausdorff,
   and vertical RMSE/P95/max, plus before/after regression deltas.
4. Run deterministic multi-start optimization using center, near-bound seeds
   on every parameter axis, and deterministic low-discrepancy interior seeds.
   Cluster converged solutions into basins and run leave-one-approach-out
   refits. Report seed-bound coverage, Jacobian rank/condition/covariance,
   bound proximity, near-optimal separated modes, fold transform spread, and
   every failed gate.

## Fixed gates

- horizontal RMSE <= 0.25 m and max <= 0.50 m;
- symmetric Hausdorff <= 0.50 m;
- vertical RMSE <= 0.10 m, P95 <= 0.20 m, max <= 0.30 m;
- leave-one-approach-out translation/yaw spread <= 0.10 m / 0.10 deg;
- full four-parameter rank, Jacobian condition <= 1e8, no bound hit;
- no per-feature absolute-gate failure or transformed-vs-baseline regression;
- no materially separated near-optimal mode.

The 2018 QL2 artifact is always reported with `acceptance_eligible=false` and
is development control only. A deployment artifact is refused unless a
separate, current, licensed-deliverable-bound horizontal survey supplies raw
controls with exact projected CRS WKT/EPSG/datum/coordinate epoch/metre units
and per-control uncertainty.
The tool independently resolves every map control against the bound geometry,
requires at least 10 fit and 4 held-out controls with full two-dimensional
rank, rejects source-feature/physical-control/geometric leakage including the
0.50 m spatial exclusion buffer, re-fits the survey SE(2), and recomputes every
fit and holdout residual. Summary-only claims, JSON coordinate literals,
generic environment objects presented as stable landmarks, or unbound files
cannot pass. This implementation does not modify deployment state.

## Map export and comparison corrections

Preserve stable road/section/lane identities and every sampled contiguous
left/right road-mark interval instead of overwriting a lane with its final
marking. Segment and bind markings by exact OpenDRIVE range, type, color,
weight, lane-change, material, width, height, sway coefficients, nested
`type/line` length, space, lateral/longitudinal offsets, rule and width, and
explicit line records; bind sampled world geometry to that complete range.
Give crosswalks a content-derived stable identity rather than an
enumeration index, and namespace environment-object identities. Extend the
offline OpenDRIVE comparator with elevation, lane-offset, superelevation,
lateral-shape, lane-width, road-mark, road-link, explicit road-junction
assignment, and junction/lane-link signatures so old-vs-live topology drift
is explicit.

## Verification and exit gate

Add tests for a synthetic known transform, a local warp that one global model
must reject, exact and near-buffer split leakage despite distinct IDs, raw
deliverable and identity tampering, ineligible landmark categories, retained
CARLA-source and overlay-pixel tampering, complete nested road-mark drift,
unreconciled EPSG:3857/EPSG:26910, malformed and valid independently executed
PROJ pipelines, every hash binding, degenerate geometry, optimizer-bound
contact, coarse coordinate resolution, and old-vs-live map mismatch. Run
focused and full bridge tests with Python warnings treated as errors and an
explicit asyncio fixture-loop scope. A non-acceptable report exits nonzero by
default; a numeric-only development report may exit zero only with the explicit
`--development-numeric-ok` override and still cannot produce a deployment
artifact. Commit only a clean source/test/doc change. Do not push or deploy.

## Review status

The required Claude Fable high-effort review was attempted five times on
2026-07-13 with read-only tools. All attempts failed before reading the plan
because the Claude OAuth session was expired and could not be refreshed. This
is recorded as an unmet independent-model review, not treated as a pass or
replaced by the unit-test results.

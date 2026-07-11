# Calibration, archived detections, and same-car evidence

Use this workflow for camera calibration, detection localization, mapping, or
same-car twin proof. It is intentionally offline until every deployment gate
passes.

## Non-circular evidence contract

- Never fit to persisted GPS, camera-local XZ, current CARLA actor positions,
  lane-snapped poses, or model-generated object IDs. They were derived from the
  nominal camera model and are baselines only.
- Fit inputs are exact archived frames, manually reviewed wheel/road contact
  pixels with covariance, trusted schema-v2 HLS media time, measured per-camera
  intrinsics/distortion, surveyed ENU static/lane geometry, and reviewed
  whole-object track/association evidence.
- Keep the OpenDRIVE site transform fixed unless independent survey evidence
  selects a different model. Use `v2x_common/geodesy.py`; do not reintroduce
  degree-per-meter constants.
- Detection-assisted camera changes remain diagnostic even when their numerical
  objective improves. Do not write `config/cameras.json`, restart perception,
  or spawn candidate actors from that output.

## Freeze and curate detections

1. Export a rolling 24-hour corpus with
   `apps/perception/tools/export_detection_corpus.py`. Reconcile every page with
   `/detections/timeline`, preserve `SHA256SUMS`, and never persist signed URL
   queries. The systemd service/timer templates are opt-in deployment artifacts;
   do not enable them during observational work.
2. Build the pixel-only ledger with
   `apps/perception/tools/build_detection_observation_ledger.py`.
   `derived_baseline` must remain quarantined and forbidden as an optimizer
   target.
3. Recover each source frame with
   `apps/perception/tools/verify_historical_correlation.py` using the exact
   persisted detection JSON. Require trusted HLS time, at most 100 ms nearest
   frame error, a decodable native-resolution frame, hash-bound report, and no
   signed URL in output.
4. Apply wheel/road contacts with
   `apps/perception/tools/apply_ground_contact_reviews.py`. Acceptance requires
   a named human and the exact retained frame plus verifier-report hashes.
   Model-only contact proposals can guide review but can never pass this tool.
5. Generate proposals with `propose_detection_tracklets.py`, apply named-human
   motion/occlusion/optical-flow/lane review with
   `apply_tracklet_reviews.py`, and freeze whole evidence groups with
   `freeze_track_split.py`. A later UTC day must be holdout. Never split frames
   from one physical car across partitions.

## Fit and evaluate

Run `fit_detection_factor_graph.py --preflight-only` first. Per camera require
at least 30 reviewed moving tracklets, three lane paths including a turn,
near/mid/far fractions of at least 20/50/20 percent, 60 percent image-width and
40 percent image-height coverage, valid measured intrinsics, and no association
split leakage.

The diagnostic fit additionally requires:

- `v2x-static-camera-solution/v1` bound to `cameras.json`, surveyed ENU truth,
  a passing independent static holdout, tight pose priors/bounds, and a frozen
  site-to-map transform;
- `v2x-surveyed-lane-map/v1` independent of detections and no worse than 0.25 m
  survey accuracy;
- reviewed simultaneous cross-camera pairs before estimating clock offsets.
  Otherwise fix each offset to zero/unobservable.

The optimizer holds measured intrinsics and the site transform fixed, uses weak
lane distance without snapping, excludes pose priors from its Jacobian rank
test, and always reports `acceptance_eligible=false`. A successful synthetic or
diagnostic fit is not production proof.

## Production acceptance

Do not promote a candidate until all of these pass without relaxed thresholds:

- per-camera checkerboard/ChArUco fit RMS at most 2 px and untouched holdout max
  at most 5 px;
- held-out static geometry at 1280x960: landmark RMSE/P95/max at most 10/16/24
  px and road-polyline RMSE/max at most 6/12 px, scaled by width;
- whole-track bootstrap pose spread at most 0.2 degrees and 0.10 m, full data
  Jacobian rank, condition at most 1e8, no parameter at a bound, and no camera
  degraded on validation/holdout;
- independent RTK/reference actor median/P95 planar error at most 0.5/1.5 m,
  correct lane at least 95 percent, cross-camera same-car P95 agreement at most
  1.0 m, with raw unsnapped positions used for scoring;
- 24-hour shadow replay, then zero-session canary proof for all four cameras:
  stable same actor across at least three frames, centroid at most 16 px at
  1280-wide, bbox IoU at least 0.50, world error at most 2.0 m, correct motion,
  visible road geometry, cleanup to LIVE and zero sessions.

If physical target images, surveyed landmarks/lane geometry, or RTK truth do not
exist, record the exact acquisition deficit and stop at diagnostic status. Do
not fabricate labels or lower a threshold to turn missing evidence into a pass.

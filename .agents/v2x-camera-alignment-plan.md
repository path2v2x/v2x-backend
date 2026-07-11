# V2X four-camera alignment and same-car acceptance plan

## Scope and immutable boundaries

- Use only the production Unreal Engine 5.5 RR/CARLA 0.10 V2X worker. Unreal
  Engine 6 is a separate task and runtime namespace.
- Keep the live checkout, services, timers, tunnels, AWS, and production
  deployment unchanged until a recorded zero-session deployment gate.
- Never accept legacy local-XZ CSVs, matcher-only proposals, shared object IDs,
  actor existence, or visual approximation as geometric or same-car proof.
- Retain hashes, raw evidence, reports, source versions, and an executable
  rollback for every accepted mutation.

## Current evidence and honest status

- Source contains fail-closed manifest, optimizer, runtime-depth revalidation,
  stable actor-blueprint, trusted-clock, and cross-camera identity gates.
- Trusted physical frames and fresh direct UE5 renders are retained at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260711T0228Z-source-pairs`.
- The similarly named UTC artifacts correctly cross the local midnight
  boundary: NTP was synchronized at `2026-07-10T19:31:07-07:00`.
- The eight exploratory landmark/match CSVs are untracked, regular-grid legacy
  diagnostics. They are explicitly rejected, must not be staged, and will not
  be used as annotations or truth.
- All four current camera registrations fail. Legacy diagnostic point RMSE is
  about 642/133/199/50 px for ch1/ch2/ch3/ch4; none is acceptance evidence.
- Production stores trusted schema-v2 detections, but no accepted record has
  the required explicit cross-camera ConvNeXt association evidence.
- At the 2026-07-11T02:54:49Z persistence audit, trusted spans were
  13.78/14.35/12.34/16.31 hours for ch1/ch2/ch3/ch4; none passes the 23-hour
  minimum inside the 24-hour query window.
- All four feeds were fresh and clock-matched, but decode latency was roughly
  14.8–19.7 seconds and failed the 10-second gate. Source commit `0c989fe`
  adds a bounded two-fragment perception-only HLS request; it is tested but
  not deployed. Five-minute read endings align with signed-session expiry and
  reconnect successfully.
- A dimensioned 9x6-inner-corner, 25 mm checkerboard is retained at
  `/home/path/V2XCarla/v2x-evidence/intrinsics/board/` with SHA-256
  `9fc88b316e318068d46e2bfa267ae22609b2f47c78b30fdd7ef0907ce00dde08`.
- The latest native Computer Use run is durably retained at
  `/home/path/V2XCarla/v2x-evidence/computer-use/20260711T023533Z/`; Dia access
  was denied before UI state, so it proves no visual acceptance claim.
- Canonical Amplify attachment is blocked by the organization policy disabling
  deploy keys; the mirror remains the unchanged production source.

## Phase gates

1. **Freeze source evidence.** Capture hash-bound real and UE5 frames for all
   channels, record service/source/map fingerprints and zero Drive sessions,
   and reject protocol versions that cannot bind each binary twin frame to its
   metadata. Copy accepted artifacts from volatile storage into immutable,
   hash-manifested durable storage before exit. For static registration, record
   both capture instants and prove mounts are fixed; dynamic same-car evidence
   later requires a trusted replay-clock match, not sequential capture.
2. **Measure physical intrinsics.** For each physical camera, retain at least
   ten unique checkerboard or ChArUco fit images plus at least two untouched
   board holdouts. Bind channel/camera identity, native resolution, crop,
   focus/zoom state, board hash, source hashes, and capture times. Require board
   coverage at every image edge/corner, at least 15 degrees of pose-tilt spread,
   and at least 1.3x distance spread. Require fit and holdout RMS no worse than
   2 px and held-out per-corner max error no worse than 5 px. Obtain explicit
   site-access and traffic-safety authorization before placing the board in a
   roadside FOV. Re-capture frozen landmarks afterward to prove no mount moved.
   Exit only when the full measured lens
   model round-trips through the deployable render/undistortion path within
   0.25 px. Existing repeated nominal intrinsics do not pass this phase.
3. **Build independent static truth.** For each channel, manually/geometrically
   identify at least eight fit and four frozen holdout global landmarks spanning
   at least 50% of image width and 30% of height. Trace at least three fit and
   two holdout finite polylines covering road edges, lane markings, crosswalk
   topology, horizon/vanishing-point constraints, and stable unique landmarks.
   Resolve UE5 pixels with retained raw depth, reject discontinuities, and hash
   the frames, annotations, intrinsics, config, map, and depth. Every landmark
   must have a unique semantic description and depth-neighborhood evidence.
   Require a non-degenerate convex-hull area and bounded design/Jacobian
   condition number; holdouts must not lie only on fit polylines.
4. **Fit and reject weak models.** Run bounded multi-start true 6-DoF plus FOV
   diagnostics and a separate exact production-model fit. Require identifiable
   parameters, no search-bound solution, fresh UE5 depth agreement, exact
   transform round-trip, and deployable-model optical error at most 0.25 px.
   At 1280x960 require held-out landmark RMSE/P95/max at most 10/16/24 px and
   road-polyline RMSE/max at most 6/12 px, scaled only by native width/1280.
5. **Controlled UE5 deployment.** With zero Drive sessions, preserve config,
   source, units, endpoints, and service fingerprints; pause only the required
   mutation-capable supervisors; deploy source-controlled calibration; restart
   minimally. Exit only with all four fresh trusted feeds, LIVE mode, zero
   leaked actors/sessions, route/systemd/publisher/tunnel parity, and a tested
   immediate rollback. Canary one channel at a time when the protocol permits;
   automatically roll back on any feed/clock/session failure or more than five
   minutes of unavailable service. Restore all supervisors in success and
   failure paths.
   The independent HLS low-latency change uses the same snapshot/canary/
   rollback discipline before calibration deployment: canary one channel,
   verify route/Lambda/source fingerprints, then require trusted decode latency
   at most 10 seconds on all channels continuously across at least two complete
   five-minute session-expiry/reconnect boundaries. Any upstream rejection,
   feed drop, legacy-latency fallback, or persistence regression fails and
   rolls back.
6. **Detection, localization, and tracking.** Persist schema-v2 trusted clocks,
   localization uncertainty at most 2.0 m, deterministic tracking, explicit
   `cross_camera_spatiotemporal_convnext` association evidence, plausible
   transit speed/distance, and stale-record cleanup. Require fresh four-feed
   and DynamoDB evidence, multi-camera candidates, and regression tests.
7. **Held-out same-car replay.** Select cars and timestamps excluded from fit
   and calibration work. For multiple timestamps and camera transitions,
   independently verify appearance identity, physical bbox/centroid, world
   localization, tracked identity, UE5 actor pose, and projected twin
   bbox/centroid. Require at least three frames per camera transition, zero
   identity switches, world-centroid error at most 2.0 m, projected centroid
   error at most 16 px at 1280-wide, and projected bbox IoU at least 0.50.
   Require actor cleanup, LIVE restoration, and multi-session isolation;
   existence alone fails.
8. **Visible release proof and closeout.** Use normal-task Computer Use on
   simforgelaptop for refreshed `/live`, `/drive`, and `/timeline` screenshots,
   console and network/WebSocket evidence. Repair canonical GitHub/Amplify IAM
   only with organization-owner authorization, release the verified commit,
   then record deployed versions, rollback, remaining debt, and prove no
   live-only source remains.
   Computer Use artifacts must be timestamped, hash-manifested, bound in the
   same session to a deployed-version endpoint, and free of cookies, tokens,
   signed URLs, or other secrets. It must not bypass GitHub organization policy.
   If direct Dia state remains denied, keep this phase blocked and move the
   exact native validation to a fresh normal user-owned task with the approval
   bridge; do not substitute non-native automation.
9. **Drift monitoring.** Reproject the frozen semantic holdouts on a schedule
   and after any mount/config/map change. Invalidate calibration and alert when
   any Phase 4 held-out threshold fails; never silently keep a stale pose.

## Immediate executable sequence

1. Finish and test the hash-bound capture helper, including explicit failure
   against the deployed legacy twin WebSocket protocol.
2. Determine whether every retained real/twin pair contains enough unique,
   depth-safe static features. If any channel does not, acquire a better
   source frame or map render; do not fabricate repeated-marking identities.
3. Implement an annotation-assistance report that proposes features while
   preserving manual/geometric holdouts, feature uniqueness, spatial coverage,
   and train/holdout separation.
4. Obtain real measured intrinsics evidence. This is a hard dependency for an
   accepted calibration and cannot be synthesized from the current frames.
   The acquisition task is to place a measured ChArUco/checkerboard target
   throughout each fixed camera's image and retain at least ten accepted source
   frames per channel; vendor nominal values or self-calibration are diagnostic
   only and do not satisfy this gate.
5. Once phases 1-4 pass, enter the controlled deployment gate, then collect new
   explicit identity associations and execute the held-out same-car replay.
6. In parallel, request the `path2v2x` organization owner decision required to
   enable canonical-repository Amplify attachment; do not weaken repository
   policy or use personal credentials to bypass that decision.
7. Diagnose persistence gaps before re-auditing. Any perception/session-source
   deployment resets the evidence window: require a new post-deploy 24-hour
   query with at least 23 hours trusted span per camera, latest event age at
   most 6 hours, no single trusted-event gap above 30 minutes during periods
   where the feed health reports streaming, and consume the pass within six
   hours of the later release gate.
8. Before staging, delete or quarantine the rejected exploratory CSVs and
   reconcile the dirty `config/cameras.json` against its known rejected origin;
   never add ignore rules that could hide future calibration evidence drift.

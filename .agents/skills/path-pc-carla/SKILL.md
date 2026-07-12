---
name: path-pc-carla
description: Operate and diagnose the Path PC CARLA/V2X stack at path@100.72.252.40, including the production Unreal Engine 5.5 RR/CARLA 0.10 worker container, drive WebSocket bridge, Vite dashboard, perception/HLS pipeline, Cloudflare and Tailscale transport, systemd supervision, and controlled deployment/rollback gates. Use for any work that reads, tests, changes, deploys, or recovers the Path PC V2X environment; exclude Unreal Engine 6 experiments, which belong to a separate task and runtime namespace.
---

# Path PC CARLA/V2X

Treat this file as an operating procedure, not proof of current state. Re-run the read-only baseline before every intervention.

## Newest perception release chronology

Observed through 2026-07-12 19:27 UTC; verify rather than assume. This section
overrides older perception candidate and deployment statements below.

- PR 30 merged as canonical
  `ec3cd60f639e7d591607474d2302b48f73f2fcfe`. Its controlled deployment
  used verified rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T191545Z/`.
  Upload-disabled startup, five strict samples, three exact feed verifiers, and
  LIVE/zero-session observation passed. Upload-enabled startup and five more
  strict samples also passed, followed by two exact feed verifiers; the third
  rejected ch1 because its capture timestamp did not advance across two
  seconds. The candidate was explicitly rolled back to `d54f5df`; the prior
  unit/environment and all timers are restored, while CARLA/Drive/web remain
  unchanged. Do not redeploy PR 30 unchanged.
- The remaining feed failure was an architecture seam: MJPEG publication
  waited behind four sequential YOLO calls even when the trusted reader was
  healthy. Candidate `7193bfd8ad1162a33afb7cf535ca8b56bad5f952`
  publishes a rate-limited raw physical frame directly from each accepted
  reader callback, with unchanged media-clock assessment, while detection and
  annotated/offline outputs stay separate. Callback failures cannot reconnect
  a trusted reader; health becomes stale naturally if publication actually
  stops. All 97 perception tests pass. An upload-disabled four-camera canary
  passed 30/30 consecutive exact feed verifiers and 120/120 strict one-second
  samples across 30-second renewals, with zero reconnects/errors/stale samples,
  latency maxima ch1/ch2/ch3/ch4 = 7.055/8.376/9.275/8.553 seconds, and
  publication-age maxima = 3.075/6.296/6.368/5.774 seconds. Cleanup passed.
  Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T192226Z-raw-feed-decoupling-canary/`.
  This is not live acceptance. Require canonical merge, another fresh verified
  rollback bundle, upload-disabled then upload-enabled startup, repeated exact
  feed verifiers, LIVE/zero sessions, a ten-minute renewal watch, and the
  attended 30-minute plus automated 24-hour gates. Roll back immediately on
  any unchanged gate.

- PR 29 merged as canonical
  `9d541d2cdc2dfd3fcf86dfbb867129c94c38d12b`. Its controlled deployment
  used the verified rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T184900Z/`
  and started with uploads disabled. Five strict all-camera health samples
  passed, but the independent four-feed verifier rejected ch1 because its
  capture timestamp did not advance across the fixed two-sample interval. The
  deployment automatically restored `d54f5df`, the prior unit and environment,
  perception, and all timers; CARLA/Drive/web were unchanged. Do not redeploy
  PR 29 unchanged.
- A live-loop defect then proved that reader sequences were advanced before a
  global every-other-iteration inference throttle. A camera frame arriving on
  a throttled iteration was permanently discarded and repeated phase alignment
  could starve one channel. Commit `36e0f065da7ab3346cc66a507bca4518fa8c8b35`
  preserves those sequences until an inference iteration actually consumes
  them. Ten consecutive feed verifiers passed, but the stronger accelerated
  watch still failed ch3 stale at sample 47; retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T185552Z-live-throttle-sequence-canary/`.
- Reducing the exact-clock fragment-match pool from three to two capped peak
  decoder sessions at seven and improved normal cadence. That intermediate
  candidate still failed at sample 96 when ch4 reported
  `proactive capture preparation failed` and reconnected while ch2 aged to
  14.95 seconds. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T190257Z-nvdec-pool2-five-minute-canary/`.
- Candidate `28c223006bdca95b826a60dca8f1686f63565547` combines the sequence
  preservation, seven-session cap, immediate hot preparation on the first
  invalid trusted-clock sample, and retryable off-path preparation failure.
  A failed replacement no longer tears down a still-readable active session;
  real active-reader failures remain reconnecting/stale and the -1/+10-second
  clock plus 15-second freshness thresholds are unchanged. All 96 perception
  tests pass. The upload-disabled four-camera canary passed ten consecutive
  exact feed verifiers and 300/300 strict one-second samples across repeated
  30-second accelerated renewals, with zero reconnects/errors/stale samples,
  latency maxima ch1/ch2/ch3/ch4 = 7.989/8.324/8.023/9.662 seconds, and
  publication-age maxima = 4.935/6.474/3.072/3.602 seconds. Every owned process
  was cleaned up. Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T190843Z-prep-retry-five-minute-canary/`.
  This remains isolated candidate evidence. Require a canonical merge, a fresh
  verified rollback bundle, controlled upload-disabled then upload-enabled
  startup gates, four changing feeds, LIVE/zero sessions, a full ten-minute
  renewal watch, then attended 30-minute and automated 24-hour watches. Roll
  back immediately on any unchanged gate.
- The normal 12:00 PDT hourly UE5/Drive restart completed successfully at
  19:01:16 UTC. It produced fresh CARLA/Drive PIDs with `NRestarts=0`, LIVE
  mode, zero sessions, and zero actors. The current live perception checkout
  remains the deliberate rolled-back `d54f5df` baseline until a newer canonical
  candidate passes its controlled deployment.

- PR 28 merged as canonical
  `0bb596b227cad420c77e12516fb4dc77a11af5e0`. Its read Lambda code-only
  deployment is backed up at
  `/home/path/V2XCarla/v2x-backend-backups/read-api-code-only/v2x-backend-read-20260712T182245Z-0bb596b2/`.
  The real API reports `DIRECT_KINESIS` plus `ON_DISCONTINUITY` for direct
  perception sessions and `SAME_ORIGIN_PROXY` plus `ALWAYS` for browser
  sessions; the browser response does not expose the signed Kinesis origin.
- The controlled PR 28 perception deployment passed five strict startup
  samples, the independent four-feed verifier, LIVE/zero-session twin
  observation, and preserved CARLA/Drive/web fingerprints. Its ten-minute
  watch then failed at sample 266: ch3's FFmpeg child stayed alive but stopped
  publishing frames, so the last trusted frame aged from 8.43 to 15.43 seconds
  and health correctly changed to `stale`. No reconnect or sanitized error was
  surfaced. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T183001Z-continuous-pts-live-watch-10m/`.
  The live checkout and installed perception unit were immediately restored to
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`; only perception restarted, all
  three timers were restored, and CARLA/Drive/web PIDs and restart counters
  remained unchanged. Do not redeploy PR 28 unchanged.
- Candidate `7dafc4e361f1a55f8512c5ffff283dcd2bb93b69` adds a seven-second
  FFmpeg network-I/O timeout, a three-refresh nonadvancing-HLS bound, and a
  renewable-session-only hot replacement when a previously trusted clock
  becomes persistently invalid. It does not change the fixed -1/+10-second
  clock or 15-second stale thresholds. All 94 perception tests pass. An
  upload-disabled four-camera canary in an owned systemd cgroup passed 120
  strict one-second samples with 30-second accelerated renewals, zero stale or
  reconnect samples, and maxima ch1/ch2/ch3/ch4 =
  9.134/8.614/8.771/9.999 seconds; every canary process was cleaned up.
  Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T184344Z-stalled-reader-recovery-canary/`.
  The ch4 margin is only about 1 ms, so this is not a live pass. Require a
  canonical merge, a new verified rollback bundle, a controlled zero-session
  deployment, five strict startup samples, four changing feeds, LIVE/zero
  sessions, a full ten-minute renewal watch, then attended 30-minute and
  automated 24-hour watches. Roll back immediately on any unchanged gate.
- The current rolled-back CPU-decoder service can again show missing clock
  readiness and latency above ten seconds. This is retained baseline debt, not
  evidence that the rejected PR 28 deployment should remain live. Do not claim
  perception acceptance until a newer candidate passes every watch and a fresh
  schema-v2 persisted vehicle is proven.

## Current deployed state and integration hold

Observed through 2026-07-12 17:35 UTC; verify rather than assume. The following
newest chronology overrides older candidate/deployment statements later in this
section:

- Canonical `origin/main` is
  `e64b9abe559853bfe6b186822996d47778c655c9` after merged PRs 22-25. The public
  browser release was independently proven at merged PR 22 commit
  `d9a6ad8e7d83acad25c315b1f41e7b80cbb4f2d8`: Amplify job 203 succeeded at that
  exact commit and refreshed Playwright CLI evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T155049Z-public-release/`
  shows HTTP 200 on `/drive`, `/live`, and `/timeline`, four playing native
  2560x1920 timeline videos, four current live snapshots, and zero browser
  warnings or errors. This is browser-release evidence, not static calibration
  or same-car placement acceptance.
- PR 23 (`5513c8e890d8836c1b142fa6d7d31664c851c4e5`) separated the short live
  capture playlist from the five-fragment exact-clock playlist and restored
  proactive 240-second renewal. Its isolated canary passed, but the strict live
  watch failed immediately at 10.127 seconds on ch2 and 10.585 seconds on ch3.
  The live checkout and perception unit were rolled back without restarting
  CARLA, Drive, or web. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T162000Z-live-edge-renewal-watch/failure.json`.
- PR 24 (`828bd341d3b700bd9186587317a4589233e43aad`) reduced only the direct
  perception capture session to one fragment while keeping the exact-clock
  session at five. The Lambda code-only apply is backed up at
  `/home/path/V2XCarla/v2x-backend-backups/read-api-code-only/v2x-backend-read-20260712T163322Z-148b13bbdcad/`;
  its isolated four-camera canary stayed below 7.64 seconds. The subsequent
  strict live watch nevertheless failed at sample 132 when ch1 reached
  10.075919 seconds. It was rolled back exactly to
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`; all three timers were restored and
  the CARLA/Drive/web PIDs and restart counters remained unchanged. The current
  clean live checkout is therefore deliberately behind canonical main at
  `d54f5df`; do not redeploy PR 23 or PR 24 unchanged. Retain the failure at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T164000Z-one-fragment-renewal-watch/failure.json`
  and rollback bundle at
  `/home/path/V2XCarla/v2x-backend-backups/perception-one-fragment/20260712T163710Z-828bd341d3b7/`.
- Playlist timestamps prove Kinesis is normally within roughly one completed
  two-second fragment of wall time; the remaining 10-second tail is local CPU
  decode backlog from four 2560x1920 streams plus inference. A source candidate
  uses host `/usr/bin/ffmpeg` NVDEC through an anonymous memfd master playlist
  and timestamped local NUT FIFO, keeps signed URLs out of command lines and
  disk, and uses the same NVDEC pixels for exact fragment matching. A real
  upload-disabled four-camera diagnostic ran 90 seconds with zero reconnects
  and maxima ch1/ch2/ch3/ch4 = 5.838/7.559/8.024/6.673 seconds. A second
  120-second diagnostic completed one staggered, pre-clocked handover per
  camera with zero untrusted/stale samples after initial acquisition, zero
  reconnects, and maxima 5.936/5.665/5.939/8.494 seconds. Treat this as a
  full detector rerun then passed 120 consecutive strict samples across all
  four staggered handovers with maxima 8.950/7.431/6.240/5.608 seconds and
  seven upload-disabled objects. PR 25 merged that candidate as canonical
  `e64b9abe`.
  Its controlled live deployment passed startup and the four-feed/zero-session
  gates, but the strict watch rejected one ch3 frame whose exact media mapping
  was 3.044 seconds ahead of receipt, outside the existing -1 second trust
  floor. The deployment was immediately rolled back to `d54f5df`; only
  perception restarted and CARLA/Drive/web fingerprints plus all timers were
  preserved. Do not redeploy `e64b9abe` unchanged. Retain the failure at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T172900Z-nvdec-live-renewal-watch/failure.json`
  and rollback bundle at
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T172427Z-perception-nvdec-e64b9abe/`.
  Kinesis `ListFragments` over 30 minutes showed producer timestamps 0.65-0.76
  seconds behind AWS server time, while host chrony offset was below 0.3 ms;
  the rejected -3 second value is therefore an intermittent decoder PTS mapping
  fault, not a stable physical-camera clock offset. The next candidate must
  validate every resolved clock before publication/handover, discard and
  re-anchor an out-of-window mapping, and still pass full detector, controlled
  live, 30-minute, and 24-hour gates without weakening -1/+10 second bounds.
  PR 26 merged that validation as
  `f9a966ddc5f07411969666efda328647dbdc0e3b`. Its controlled deployment again
  passed startup/four-feed/LIVE gates, then the strict watch proved a second
  fail-closed defect: after ch1 rejected a bad PTS mapping, ordinary unclocked
  frames were still published during re-anchor, dropping global clock readiness
  at sample 5. It was rolled back exactly; retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T174500Z-clock-validated-live-watch/failure.json`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T174335Z-perception-clock-gate-f9a966dd/`.
  The next candidate freezes publication on the last trusted frame during
  re-anchor; that frame still ages normally, so a recovery longer than 15
  seconds fails stale rather than hiding an outage. Its upload-disabled full
  detector canary passed 120 strict accelerated-renewal samples with accepted
  minima 2.778/2.674/1.499/1.739 seconds and maxima
  7.028/7.771/8.221/7.654 seconds. This is not live acceptance; require a new
  canonical commit and repeat the controlled watches.
  PR 27 merged the publication freeze as
  `f2499bfd28a6cb9a9171798afdb768f4f087790c`. Its live watch kept clocks trusted
  but correctly failed when ch2 could not re-anchor before the last trusted
  frame became stale at 15.12 seconds. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T175400Z-clock-hold-live-watch/failure.json`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T175248Z-perception-clock-hold-f2499bfd/`;
  live was again restored to `d54f5df` without CARLA/Drive/web changes.
  The root cause is the direct HLS session's `DiscontinuityMode=ALWAYS`, which
  inserts a synthetic discontinuity at every normal two-second fragment. A
  read-only 180-second ch1 session with `ON_DISCONTINUITY` stayed trusted at
  3.19-9.57 seconds. A four-camera upload-disabled full detector canary using
  direct AWS `ON_DISCONTINUITY` sessions then passed 120 strict accelerated
  renewal samples with minima 1.932/2.579/0.657/2.457 seconds and maxima
  8.865/7.905/7.025/8.690 seconds. The next release must change only direct
  perception sessions to `ON_DISCONTINUITY`, leave the already-proven browser
  proxy on `ALWAYS`, expose and verify the selected mode, and repeat API,
  detector, controlled live, 30-minute, and 24-hour gates.

- The post-hourly clean HLS watch is complete at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T150759Z-post-hourly-clean/`.
  It ran from 15:07:59.841 through 15:38:48 UTC with 36,964 50-ms samples,
  7/7/7/6 completed staggered session handoffs for ch1-ch4, a 1.75-second
  worst availability gap, no active final outage, all four selected videos at
  2560x1920/`readyState=4`/playing with no media error, and zero console
  warnings or errors. A second final sample proved every video clock advanced.
  The sanitized resource ledger retained only HTTP 200 for ten browser-session,
  fifteen coverage, and 1,744 proxy responses, plus five status-0 proxy reads
  bounded within successful double-buffer handoffs; the latter are retained as
  retired-session abort candidates, not deleted. Direct and opaque browser
  master canaries returned 200 for all four channels without persisting signed
  or opaque URLs. This closes the attended browser-HLS seam only.
- Do not promote that HLS result to the complete perception/product gate. The
  final four-feed verifier proved two advancing trusted frames and distinct
  hashes for every camera, but ch3 decode latency was 13.94/10.84 seconds,
  exceeding the fixed 10-second limit; a later health sample had ch4 at 12.39
  seconds. The inspected timeline visibly reported `Objects DB STALE` and its
  newest persisted schema-v2 car was 14:33:40.087 UTC, while current live
  frames at 15:42 UTC were fresh but contained zero detections. This may be an
  empty-road interval rather than an upload failure, but a new eligible
  schema-v2 persistence proof is still required before end-to-end acceptance.
- Draft PR 22 now includes the tested HLS/API release and least-privilege
  observability policy at `69b1f3a`. The policy adds read-only CloudWatch
  metrics and read-only access to the exact read-Lambda log group. Its
  state-hash bootstrap refused before mutation because the only V2X-account
  source credential lacks `iam:GetUser`; do not bypass the administrator gate
  or claim those observability permissions are deployed.
- The isolated UE5.5 recovery workspace remains outside production. The
  official engine dependency fetch and official CARLA `ue5-dev` content/LFS
  materialization are in progress on `/mnt/v2x-ue5`. The exact 29-object
  Richmond road core was hash-gated into the isolated CARLA project with
  rollback evidence at
  `/mnt/v2x-ue5/evidence/april-road-core-dependencies/migration-20260712T151559Z/`;
  this is staging only, not conversion, render, cook, or calibration proof.

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
- Newest browser/API state overrides older HLS chronology below. The read
  Lambda was updated at 14:28 UTC from state hash
  `5958dd663b35bc3dbaf9ffd9d64181892efa957f695b3bb69e053fc2e1cdcb6e`;
  its narrow upstream retry covers only HTTP 429/5xx and network timeouts,
  while 4xx, invalid content, redirects, bounds, and auth/config failures still
  fail immediately. The verified rollback bundle is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T142822Z-5958dd663b35/`.
  A real playlist 502 and a measured 6.70-second all-camera simultaneous-renew
  outage remain retained failures at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T142114Z-seamless-failed-upstream502/`
  and
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T142909Z-post-retry-failed-simultaneous-renewal/`.
  The current web candidate double-buffers each HLS session and staggers ch1–ch4
  renewals by ten seconds. By 14:59 UTC its attended Playwright probe had five
  renewal rounds per camera, zero console errors, all session calls 200, and a
  worst measured availability gap of 1.45 seconds. Do not call the run complete
  before its 30-minute endpoint or before retaining final network/screenshot
  evidence. The candidate is isolated in commit `748d02e` on
  `codex/v2x-hls-release`, draft PR 22; 132 web tests, clean build/type checks,
  22 generated-Lambda tests, and recovery-infrastructure tests pass.
  Direct Amplify attachment to `path2v2x/v2x-backend` failed before metadata
  mutation because the organization disables deploy keys; do not weaken that
  policy. The current owner-controlled fallback
  `michaelvu1207/v2x-backend-amplify` is proven exact to canonical `main`, has
  an active push webhook and sync workflow, and Amplify jobs 193–202 succeeded.
  Repository-apply rollback metadata is
  `/home/path/V2XCarla/v2x-backend-backups/amplify-repository/d1ugco1rmb7yjj-20260712T145830Z-289164f0714ea21fb4b5a419fd11c60218dec0498eb7b487345db2a3b7530a1c.json`;
  the failed canonical update left Amplify repository metadata unchanged.
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
  production watch gates. The same browser watch later exposed two transient
  CH2 `/video/coverage` 502s because the page issued six ListFragments windows
  concurrently for each stream. A follow-up watch proved the connection limit
  is account-wide when overlapping ch2/ch3 calls still returned 502. The next
  source candidate serializes all coverage chunks globally and refuses
  overlapping refreshes. Also retain the deployment-compatibility fix: new HLS
  proxy settings have safe defaults and existing Lambda configuration is
  reconciled before new code. The 12:40 apply briefly produced two
  `HLS_PROXY_PREFIX` import failures while code preceded environment; do not
  call that canary a clean 30-minute pass or repeat the unsafe order.
  A subsequent local watch found Chromium 145 claiming native HLS support but
  leaving ch1 paused with `DEMUXER_ERROR_COULD_NOT_PARSE` after an opaque media
  playlist was blocked by ORB. The accepted browser candidate must prefer
  hls.js whenever Media Source Extensions are supported and reserve native HLS
  for Safari/fallback. Require all four video elements to remain playing with
  no media error across the renewed clean watch.
  The globally serialized browser candidate later exposed two isolated
  `/video/coverage` 502s at 13:31 UTC. Both responses had the exact 100-byte
  `video_coverage_unavailable` shape for
  `ClientLimitExceededException`, after 4.81/3.74-second requests. Commit
  `fda0d41` adds a narrow, jittered three-attempt retry for only transient
  Kinesis conditions; authorization/configuration errors still fail
  immediately. It was deployed at 13:40 UTC from reviewed state hash
  `dff605961d94b4b7616f752f41a74baee66451dc1f12f6c96420f1998c6f04de`.
  Rollback evidence, including the prior Lambda zip and prior IAM/API state, is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T134008Z-dff605961d94/`.
  Immediate post-apply canaries passed all four direct sessions, all four
  browser sessions, and all four four-hour coverage calls. Commit `b25720c`
  removes the underlying continuous-load pattern by aligning four-hour windows,
  caching complete historical chunks, re-fetching only the live-edge chunk,
  and refreshing optional coverage every five minutes. Web tests are 130/130,
  the production build is clean, and `svelte-check` reports zero errors and
  zero warnings. A fresh Playwright CLI watch started at 13:44:42 UTC; its
  initial 28 aligned historical/live-edge calls all returned 200 and all four
  2560x1920 videos played without media error. Do not call it a 30-minute pass
  until the full interval and renewal/refresh cycles complete, and still require
  the separate 24-hour watch.
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
  Fresh high-effort Fable review attempts after the 13:48 UTC map-plan update
  still fail before reading the file because Claude CLI reports an expired,
  non-refreshable OAuth session. Do not describe the updated map plan as newly
  Fable-reviewed until that external authentication works; retain the fixed
  contract and continue only work that cannot weaken its gates.
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
  is ineligible for V2X. Do not delete, inspect, or reuse it. A dedicated clean
  V2X UE5.5 migration workspace now exists at `/mnt/v2x-ue5` on a 500 GB
  loop-backed ext4 image stored as the single removable file
  `/mnt/v2x-capacity/v2x-ue5-build.ext4` on the secondary Windows volume. The
  outer NTFS mount intentionally uses `ntfs-3g`; the kernel `ntfs3` driver
  materialized the whole sparse file during the first bounded attempt, which
  was stopped and removed before retrying. A second `largefile4` format was
  also rejected after clean checkout proved its low inode count unsuitable for
  UE's 183,000+ source files; only partial V2X clones existed and were discarded.
  The current normal ext4 image exposes about 32 million inodes, initially used
  about 10 MB physical space, and left existing Windows data unchanged. Clean
  CARLA `ue5-dev` commit `6279162d1836024488474d3e5b2a5737ce57bb63`
  is complete. The engine checkout has been corrected from stock Epic 5.5.4 to
  CARLA's required `CarlaUnreal/UnrealEngine` `ue5-dev-carla` commit
  `2ac0528831e08e80784df2759db9a2c592d3bd4d`; fork dependencies are downloading
  at background CPU/disk priority and the checkout must eventually be named
  `/mnt/v2x-ue5/src/UnrealEngine5_carla`. The clean Richmond source checkout is
  fixed at `d14da5b57bbe4356930a2b9a926a675692e18547`. The complete 29-file April
  road-core subset—level, scene, road/curb/gutter/sidewalk and both marking
  layers plus their primary materials/textures—matches every recorded LFS SHA
  and has zero missing material/texture imports in UE Viewer. Retained evidence
  is `/mnt/v2x-ue5/evidence/april-road-core-dependencies/`. Thousands of
  unrelated prop assets remain pointers and are not acceptance-ready; do not
  mislabel the road-core subset as a complete final map package. Engine fork
  dependencies were about 31% complete at 14:52 UTC and continue at background
  priority. A probe proved the latest retained
  `Richmond_NR.umap` references
  `New_RFS/Richmond_Field_Station_Richmond_CA.uasset`; that scene asset retains
  a distinct newer source path,
  `C:/Users/123/Downloads/Unreal_Exports/Richmond_Field_Station_Richmond_CA.fbx`,
  plus `Terrain_Marking` and `Roads_Marking` scene hierarchies. This is a real
  candidate map revision, not accepted geometry and not a substitute for the
  independent survey gate. A clean 64-bit UE Viewer build at source commit
  `a0bfb468d42be831b126632fd8a0ae6b3614f981` can enumerate the UE4.26 assets,
  bounds, imports, and source models, but reports zero cooked/render LODs for
  the editor-only road meshes and therefore cannot export their vertices. Do
  not mislabel that as an empty map; it proves the CARLA UE5.5 editor migration
  remains necessary. All inputs and build products remain only in this
  V2X mount. The root filesystem and all UE6/comparison paths
  remain excluded. After any reboot, verify both mounts and the image allocation
  before resuming; no persistent mount entry has been installed yet. The UE4
  import metadata names the missing authoring file
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
- The superseded local Playwright CLI baseline at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T122000Z-calibration-baseline/`
  shows all four `/live` physical feeds visible, but `/timeline` still renders
  only ch2 while ch1/ch3/ch4 are black, the global header reports zero cameras
  and zero FPS, and the object table is stale. Infrastructure HTTP success and
  the passing four-feed CLI verifier did not make that timeline UI an
  acceptance pass. Use the newer 13:44 UTC candidate watch described above;
  retain this black-frame result as before-state evidence.

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
| Perception assets | ignored live `apps/perception/yolov8n.pt` plus `~/.cache/torch/hub/checkpoints/mobilenet_v3_small-047dcff4.pth`; hash and preserve both |
| Drive tunnel | `v2x-cloudflared-drive.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Perception tunnel | `v2x-cloudflared-perception.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Public API | `https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com` |
| AWS deploy caller | `arn:aws:iam::147229569658:user/rfs-v2x-service`; API writes require the dedicated least-privilege deploy role |
| Amplify repository | reconcile stale `michaelvu1207/v2x-backend` to canonical `path2v2x/v2x-backend` with a fresh token and explicit release gate |

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
7. Let `v2x-carla-rr.service` adopt an already-running validated container through `docker wait`; do not restart or recreate it merely to add supervision.
8. Install one layer at a time and refresh UI/API evidence after each action.
9. Start perception with `/etc/v2x-perception.env` keeping `V2X_PERCEPTION_UPLOAD=false`; require four fresh/changing feeds before enabling production uploads and proving a current DynamoDB record.
10. Restore the previous artifact immediately when its acceptance gate fails. For Quick Tunnels, restore variables around the currently healthy endpoint, never a dead saved URL.
11. Re-enable timers only after the final public/runtime checks pass.

API route reconciliation is plan-first and exact-resource only. The normal service user cannot write API Gateway directly. A separately authorized principal must apply `infra/aws-cli/bootstrap-v2x-deploy-role.sh`; then assume `V2XBackendDeployRole` and run `provision-read-api.sh` with the reviewed API ID, `RECONCILE_LAMBDA=false`, IAM attachment disabled, explicit `PLAN_ONLY=false`, and the plan's `EXPECTED_CURRENT_STATE_HASH`. Do not add API privileges to the Amplify service role.

Prefer source-controlled scripts and systemd units over one-off `nohup` or manual Docker commands. Do not leave source that exists only in the live checkout.

# Reviewed Vehicle Localization and UE5 Placement Contract

Status: source-only design and tests. This work does **not** establish that the
static camera calibration gate has passed and must not be deployed without the
separate controlled live gate.

Fable review status (2026-07-13): attempted with `--model fable --effort high`
and read-only tools, but Claude CLI exited 1 because its OAuth session was expired
and could not be refreshed. No model review was available to incorporate; this is
an external review blocker, not evidence that the plan was approved.

Independent implementation review status: changes required after commit
`e986ab6`. The remediation must add authority verification, semantic evidence
linkage, measured optical/static gates, trajectory dynamics and appearance gates,
eigenvalue-correct covariance bounds, transaction-safe actor updates/cleanup,
strict-live freshness, and exact blueprint/geometry binding. The prior commit is
not an acceptance candidate.

Fable remediation review status: retried after the independent findings, but the
same Claude CLI OAuth refresh failure returned exit 1. No Fable remediation
review was available; the independent findings below are therefore the governing
review record.

Remediation verification (2026-07-13): the isolated source worktree passes 498
bridge tests and 499 perception tests, including the new authority-forgery,
semantic-linkage, correlated-covariance, dynamics, freshness/replay, blueprint
geometry, transform rollback, and cleanup-ownership adversarial cases. Both JSON
schemas pass Draft 2020-12 metaschema validation. This is source evidence only;
it is not a live calibration, holdout, UE5, or deployment acceptance result.

## Safety and migration boundary

- Keep `DTB_TWIN_REVIEWED_PLACEMENT=off` as the default. Off mode preserves the
  existing GPS-derived placement behavior byte-for-byte.
- Strict mode accepts only a versioned, self-hashed reviewed localization sample
  whose event, identity, exact native frame, mask, detector, camera config,
  intrinsics, map, timing, consensus, reviewer, covariance, and trajectory
  fingerprints match the running bridge and detection record.
- Strict rejection is terminal for that sample: never fall back to baseline GPS,
  bbox-bottom-centre diagnostics, a lane-snapped position, or an earlier identity.
- No service, AWS, UE worker, future holdout, or production checkout mutation is
  part of this change.

## Implementation phases

1. Add a dependency-light reviewed-localization validator and canonical hashing
   helper. Require finite PSD pixel/world covariance, <=2 m uncertainty, reviewed
   footprint midpoint (not bbox centre), exact-same-session PTS provenance,
   unambiguous global identity, dimensions, heading, blueprint family, and complete
   SHA-256 bindings. Reject unknown keys that could create unsigned semantics.
2. Add an offline attachment tool for already-reviewed consensus/trajectory
   artifacts. It must verify every bound file and source hash before embedding a
   self-hashed per-event contract; diagnostic factor-graph or baseline GPS output
   remains ineligible.
3. Add the opt-in bridge feature gate. In strict mode, validate against the active
   map name/OpenDRIVE hash and exact cameras JSON/per-camera hashes, place the
   reviewed UE5 actor-centre coordinates directly, use the reviewed heading, and
   avoid all lateral/vertical lane snapping. Use a stable SHA-256 placement key for
   deterministic blueprint-family selection across process restarts.
4. Expose strict-mode status, accepted provenance hashes, exact reviewed target,
   raw-to-actor placement error, blueprint-selection digest, and bounded rejection
   diagnostics. Preserve existing cleanup, replay generation isolation, actor
   ownership, and despawn behavior.
5. Add adversarial tests for artifact tampering/spoofing, stale/missing hashes,
   map/config mismatch, ambiguous identity, non-finite/non-PSD covariance,
   excessive uncertainty, timestamp/session violations, exact footprint midpoint
   versus bbox centre, multi-camera trajectory identity, strict off/on/no-fallback,
   exact coordinates, cleanup, and deterministic blueprint selection.
6. Run the intended bridge and perception suites in their pinned environments,
   inspect the source-only diff, and commit the clean isolated branch. Any missing
   surveyed static truth or accepted trajectory artifact remains an explicit
   deployment blocker, never a test threshold adjustment.
7. Remediate independent-review findings without relaxing gates: verify an
   allowlisted keyed reviewer authority; semantically bind every accepted event,
   contact, mask, factor-graph placement, identity pair, appearance result,
   measured intrinsics, and passing static holdout result; enforce strict
   trajectory time/dynamics/covariance/transit constraints; make UE5 actor updates
   and cleanup transactional; gate strict live freshness separately from replay;
   and bind the selected blueprint catalog/pool/item plus measured actor dimensions
   to reviewed geometry and independent placement-error evidence.

## Exit criteria

- Default/off mode passes the pre-existing bridge tests without behavior changes.
- Strict mode accepts a fully bound reviewed sample and places its exact CARLA
  world coordinates; every listed malformed or mismatched variant is rejected with
  no GPS fallback and a visible reason.
- Identical global identity plus blueprint family yields the same selection digest
  and blueprint across fresh `TwinSync` instances; different/ambiguous identities
  cannot alias silently.
- A multi-camera trajectory keeps one global track and actor while timestamps and
  sample order advance monotonically; replay/session cleanup remains unchanged.
- Offline tooling round-trips a valid reviewed trajectory and detects any mutation
  of detections, frame, mask, consensus, factor-graph, config, intrinsics, or map
  bindings.
- No runtime/deployment files outside this worktree are changed, and no statement
  claims static calibration acceptance.

# V2X four-camera alignment execution plan

1. Preserve the production UE5 stack and inspect the current camera coordinate transforms, calibration CSV provenance, saved real frames, exact replay actor transforms, and existing uncommitted calibration work. Do not use UE6.
2. Implement a repeatable bounded optimizer rather than hand-tuned offsets. Search camera position, yaw/pitch/roll, FOV, and lens terms against static road/landmark image evidence, retain all inputs and objective metrics, and separate fit inputs from untouched validation evidence.
3. Produce one candidate per channel and require road geometry alignment plus held-out landmark thresholds at 1280x960. Reject any candidate based only on changed images, training-point fit, or actor metadata.
4. Test camera math, optimizer determinism, replay placement, and cleanup. Before deployment, require zero Drive sessions, stop mutation-capable timers, snapshot source/config/units/runtime, and preserve immediate rollback.
5. Deploy only the source-controlled candidate to the UE5 V2X bridge. Re-enable timers only after four fresh feeds, LIVE restoration, zero sessions, and service/tunnel checks pass.
6. For end-to-end acceptance, select trusted schema-v2 cars from every channel, independently verify their physical archived frames, replay the exact timestamps, and require the corresponding UE5 vehicle to be visibly detected in the matched twin camera. Capture refreshed /timeline and /drive UI screenshots, console/network/WebSocket evidence, and exact CLI/API artifacts.

Open risk: the legacy 4-7 local-XZ points per camera lack global IDs and holdouts. If image-driven registration cannot generate defensible independent validation, collect or derive additional static globally identified correspondences from the real frames and CARLA map before accepting/deploying offsets.

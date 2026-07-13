"""
Twin Sync — mirrors live real-world detections into CARLA actors.

Polls the perception service's local /detections/latest endpoint (both
services run on the Path PC, so no cloud round-trip) and keeps one CARLA
actor per global track: cars/trucks become vehicles snapped to the
nearest driving lane, people become walkers. Positions are lerped on the
bridge tick so actors glide between 1 Hz GPS fixes instead of teleporting.

All actors are spawned with physics off and role_name="twin_object" so
drive sessions, scenario runs, and the actor audit can tell them apart.
Disable entirely with DTB_TWIN_SYNC=off.
"""

import asyncio
from collections import Counter, deque
import hashlib
import logging
import math
import time
from typing import Dict, List, Optional

import requests

from digital_twin_bridge.detection_pages import fetch_all_detection_pages
from digital_twin_bridge.geo_utils import gps_to_carla
from digital_twin_bridge.reviewed_localization import (
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    build_runtime_context,
    validate_contract,
)

logger = logging.getLogger(__name__)

VEHICLE_TYPES = {"car", "truck", "bus"}
# Never mirror detections into blueprints with gameplay side effects
# (the firetruck triggers EVA pull-over alerts on drive sessions).
BLUEPRINT_BLOCKLIST = ("firetruck", "ambulance", "police")

# Spawn coordinates are only a bounded allocation bootstrap.  Successful
# actors are moved to the exact detection-derived transform before they are
# tracked, so none of these offsets may leak into placement evidence.  Keep
# this sequence fixed across polls and releases: retry drift would make a
# blocked track's eventual spawn depend on how long it had been observed.
SPAWN_BOOTSTRAP_MAX_OFFSET_M = 2.0
SPAWN_BOOTSTRAP_OFFSETS = (
    (0.0, 0.0, 0.0),
    (0.0, 0.0, 0.75),
    (0.0, 0.0, 1.5),
    (0.0, 0.0, 2.0),
    (1.25, 0.0, 0.75),
    (-1.25, 0.0, 0.75),
    (0.0, 1.25, 0.75),
    (0.0, -1.25, 0.75),
)


def _parse_utc_epoch(value) -> Optional[float]:
    """ISO-8601 (optionally with trailing Z) -> epoch seconds, or None."""
    if not value:
        return None
    from datetime import datetime, timezone

    v = str(value).strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _epoch_to_iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


class TwinTrack:
    """One mirrored real-world object and its CARLA actor."""

    __slots__ = (
        "object_id", "object_type", "actor_id", "last_seen",
        "current", "target", "lerp_start", "lerp_duration", "yaw",
        "event_id", "detection_timestamp_utc", "media_timestamp_utc",
        "timestamp_schema_version", "media_time_trusted", "media_clock",
        "device_id", "track_id", "bbox", "gps_location",
        "raw_carla_location", "lane_snap_distance_m",
        "raw_to_target_planar_m", "placement_planar_error_m",
        "reviewed_localization", "trajectory_id", "sample_index",
        "reviewed_media_epoch", "blueprint_family", "placement_key_sha256",
        "vehicle_dimensions_m",
    )

    def __init__(self, object_id: str, object_type: str) -> None:
        self.object_id = object_id
        self.object_type = object_type
        self.actor_id: Optional[int] = None
        self.last_seen = 0.0
        self.current = None  # carla.Location
        self.target = None  # carla.Location
        self.lerp_start = 0.0
        self.lerp_duration = 1.0
        self.yaw = 0.0
        self.event_id = None
        self.detection_timestamp_utc = None
        self.media_timestamp_utc = None
        self.timestamp_schema_version = None
        self.media_time_trusted = False
        self.media_clock = None
        self.device_id = None
        self.track_id = None
        self.bbox = None
        self.gps_location = None
        self.raw_carla_location = None
        self.lane_snap_distance_m = None
        self.raw_to_target_planar_m = None
        self.placement_planar_error_m = None
        self.reviewed_localization = None
        self.trajectory_id = None
        self.sample_index = None
        self.reviewed_media_epoch = None
        self.blueprint_family = None
        self.placement_key_sha256 = None
        self.vehicle_dimensions_m = None


class TwinSync:
    """Poll detections and upsert twin actors.

    Polling runs as an asyncio task on the server loop (HTTP in an
    executor); all CARLA actor operations happen on the loop thread,
    same as the drive-session handlers.
    """

    def __init__(
        self,
        world,
        carla_map,
        detections_url: str = "http://127.0.0.1:8090/detections/latest",
        poll_interval: float = 1.0,
        despawn_after: float = 12.0,
        detection_max_age: float = 8.0,
        detection_future_tolerance: float = 5.0,
        range_fetcher=None,
        reviewed_placement: str = "off",
        reviewed_context: Optional[ReviewedPlacementContext] = None,
        cameras_json_path: str = "",
    ) -> None:
        self._world = world
        self._map = carla_map
        self._detections_url = detections_url
        self._poll_interval = poll_interval
        self._despawn_after = despawn_after
        self._detection_max_age = detection_max_age
        self._detection_future_tolerance = max(
            0.0, float(detection_future_tolerance)
        )
        # Callable (start_iso, end_iso, limit) -> {"items": [...]} against the
        # detections DB; enables replaying the twin at past timestamps.
        self._range_fetcher = range_fetcher
        placement_mode = str(reviewed_placement).strip().lower()
        if placement_mode not in {"off", "strict"}:
            raise ValueError("reviewed_placement must be 'off' or 'strict'")
        self._reviewed_placement = placement_mode
        if placement_mode == "strict":
            self._reviewed_context = reviewed_context or build_runtime_context(
                carla_map, cameras_json_path
            )
        else:
            self._reviewed_context = None
        self._strict_rejections = Counter()
        self._recent_strict_rejections = deque(maxlen=20)
        self._tracks: Dict[str, TwinTrack] = {}
        self._vehicle_blueprints: List[object] = []
        self._truck_blueprints: List[object] = []
        self._walker_blueprints: List[object] = []
        self._blueprints_loaded = False
        self._stopped = False
        self._poll_failures = 0
        self._mode = "live"
        self._replay: Optional[dict] = None
        self._pending_replay = None
        # Results from a previous replay request must never be applied after a
        # second replay (or go-live) supersedes it while HTTP is still in the
        # executor.  The generation travels with every fetched chunk.
        self._replay_generation = 0

    # ------------------------------------------------------------------
    # Blueprint selection
    # ------------------------------------------------------------------

    def _load_blueprints(self) -> None:
        if self._blueprints_loaded:
            return
        bp_lib = self._world.get_blueprint_library()

        def usable(bp) -> bool:
            return not any(token in bp.id for token in BLUEPRINT_BLOCKLIST)

        vehicles = sorted((bp for bp in bp_lib.filter("vehicle.*") if usable(bp)), key=lambda b: b.id)
        for bp in vehicles:
            wheels = 4
            try:
                if bp.has_attribute("number_of_wheels"):
                    wheels = int(bp.get_attribute("number_of_wheels").as_int())
            except (AttributeError, ValueError, RuntimeError):
                pass
            if wheels < 4:
                continue
            if any(token in bp.id for token in ("truck", "van", "sprinter", "cybertruck")):
                self._truck_blueprints.append(bp)
            else:
                self._vehicle_blueprints.append(bp)
        if not self._truck_blueprints:
            self._truck_blueprints = list(self._vehicle_blueprints)

        self._walker_blueprints = sorted(bp_lib.filter("walker.pedestrian.*"), key=lambda b: b.id)
        self._blueprints_loaded = True
        logger.info(
            "Twin sync blueprints: %d vehicles, %d trucks, %d walkers",
            len(self._vehicle_blueprints), len(self._truck_blueprints), len(self._walker_blueprints),
        )

    def _blueprint_for(self, track: TwinTrack):
        if track.object_type == "person":
            pool = self._walker_blueprints
        elif track.blueprint_family in {"truck", "bus"} or track.object_type in {"truck", "bus"}:
            pool = self._truck_blueprints
        else:
            pool = self._vehicle_blueprints
        if not pool:
            return None
        # Stable per-track pick so a track keeps its car across updates.
        # Python's hash() is randomized per process.  A stable digest keeps a
        # replayed physical track on the same UE5 blueprint across bounded
        # retries and service restarts, making visual evidence reproducible.
        if track.placement_key_sha256 is not None:
            digest = bytes.fromhex(track.placement_key_sha256)
        else:
            digest = hashlib.sha256(track.object_id.encode("utf-8")).digest()
        bp = pool[int.from_bytes(digest[:8], "big") % len(pool)]
        try:
            bp.set_attribute("role_name", "twin_object")
        except (IndexError, RuntimeError):
            pass
        return bp

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def _location_for(self, track: TwinTrack, lat: float, lon: float):
        """GPS -> CARLA location, snapped to a plausible surface."""
        import carla

        location = gps_to_carla(self._map, lat, lon)
        track.raw_carla_location = {
            "x": float(location.x),
            "y": float(location.y),
            "z": float(location.z),
        }
        track.lane_snap_distance_m = None
        track.raw_to_target_planar_m = None
        track.placement_planar_error_m = None
        if track.object_type in VEHICLE_TYPES:
            waypoint = self._map.get_waypoint(location, project_to_road=True)
            if waypoint is not None:
                snapped = waypoint.transform.location
                track.lane_snap_distance_m = math.hypot(
                    float(snapped.x) - float(location.x),
                    float(snapped.y) - float(location.y),
                )
                # Keep the real-world position along the lane, only adopt the
                # lane height/yaw when the detection is near the road.
                if track.lane_snap_distance_m < 4.0:
                    track.yaw = waypoint.transform.rotation.yaw
                    location = carla.Location(
                        x=location.x,
                        y=location.y,
                        z=snapped.z,
                    )
                else:
                    logger.warning(
                        "Twin placement rejected for %s: %.2fm from driving lane",
                        track.object_id,
                        track.lane_snap_distance_m,
                    )
                    return None
            else:
                logger.warning(
                    "Twin placement rejected for %s: no driving waypoint",
                    track.object_id,
                )
                return None
        else:
            try:
                sidewalk = self._map.get_waypoint(
                    location, project_to_road=True, lane_type=carla.LaneType.Sidewalk
                )
            except Exception:
                sidewalk = None
            if sidewalk is not None and sidewalk.transform.location.distance(location) < 5.0:
                location.z = sidewalk.transform.location.z
        # Lift above the surface or try_spawn_actor fails on ground collision;
        # walkers are placed by their capsule centre so they need ~1 m.
        location.z += 1.1 if track.object_type == "person" else 0.3
        track.raw_to_target_planar_m = math.hypot(
            float(location.x) - float(track.raw_carla_location["x"]),
            float(location.y) - float(track.raw_carla_location["y"]),
        )
        return location

    def _reviewed_location_for(self, reviewed: dict):
        """Return the exact reviewed UE5 actor-centre coordinate.

        Strict reviewed placement never calls map waypoint projection and never
        adds a surface/collision offset.  The artifact is explicitly required to
        carry actor-centre semantics, so any adjustment here would destroy the
        reviewed world-coordinate evidence.
        """
        import carla

        position = reviewed["position_m"]
        return carla.Location(
            x=position["x"], y=position["y"], z=position["z"]
        )

    def _commit_reviewed_track(
        self, track: TwinTrack, reviewed: dict, location
    ) -> None:
        position = reviewed["position_m"]
        track.raw_carla_location = dict(position)
        track.lane_snap_distance_m = None
        track.raw_to_target_planar_m = 0.0
        track.placement_planar_error_m = 0.0
        track.yaw = reviewed["heading_deg"]
        track.reviewed_localization = reviewed
        track.trajectory_id = reviewed["trajectory_id"]
        track.sample_index = reviewed["sample_index"]
        track.reviewed_media_epoch = reviewed["media_epoch"]
        track.blueprint_family = reviewed["blueprint_family"]
        track.placement_key_sha256 = reviewed["placement_key_sha256"]
        track.vehicle_dimensions_m = reviewed["dimensions_m"]
        track.current = location
        track.target = location

    def _reject_strict(self, detection: dict, reason: str) -> None:
        self._strict_rejections[reason] += 1
        self._recent_strict_rejections.append({
            "event_id": detection.get("event_id"),
            "object_id": detection.get("object_id"),
            "reason": reason,
        })

    def _strict_sequence_is_valid(
        self, track: TwinTrack, reviewed: dict, object_type: str
    ) -> Optional[str]:
        if track.object_type != object_type:
            return "trajectory_object_type_changed"
        if track.trajectory_id is not None and track.trajectory_id != reviewed["trajectory_id"]:
            return "trajectory_identity_changed"
        if track.sample_index is not None and reviewed["sample_index"] <= track.sample_index:
            return "trajectory_sample_not_monotonic"
        if (
            track.reviewed_media_epoch is not None
            and reviewed["media_epoch"] < track.reviewed_media_epoch
        ):
            return "trajectory_timestamp_regressed"
        return None

    def _commit_detection_metadata(
        self,
        track: TwinTrack,
        detection: dict,
        now: float,
        use_detection_ts: bool,
    ) -> None:
        track.event_id = detection.get("event_id")
        track.detection_timestamp_utc = detection.get("timestamp_utc")
        track.media_timestamp_utc = detection.get("media_timestamp_utc")
        track.timestamp_schema_version = detection.get("timestamp_schema_version")
        track.media_time_trusted = detection.get("media_time_trusted") is True
        track.media_clock = detection.get("media_clock")
        track.device_id = detection.get("device_id")
        track.track_id = detection.get("track_id")
        track.bbox = detection.get("bbox") or (
            (detection.get("camera_data") or {})
            .get("bifocal_metadata", {})
            .get("bbox")
        )
        gps = detection.get("gps_location") or {}
        lat, lon = gps.get("latitude"), gps.get("longitude")
        track.gps_location = (
            {"latitude": float(lat), "longitude": float(lon)}
            if lat is not None and lon is not None
            else None
        )
        if use_detection_ts:
            detection_epoch = _parse_utc_epoch(detection.get("timestamp_utc"))
            track.last_seen = detection_epoch if detection_epoch is not None else now
        else:
            track.last_seen = now

    # ------------------------------------------------------------------
    # Poll + apply
    # ------------------------------------------------------------------

    def _fetch_detections(self) -> Optional[list]:
        """Fetch and flatten per-camera detection summaries (blocking)."""
        resp = requests.get(self._detections_url, timeout=5)
        resp.raise_for_status()
        payload = resp.json()
        detections = []
        now = time.time()
        for camera in (payload.get("cameras") or {}).values():
            if not isinstance(camera, dict):
                continue
            # A camera summary is usable only when its producer time is
            # trustworthy and current.  Missing/malformed timestamps used to
            # fail open and could resurrect a frozen feed indefinitely.
            updated_epoch = _parse_utc_epoch(camera.get("updated_at"))
            if updated_epoch is None:
                continue
            age = now - updated_epoch
            if (
                age < -self._detection_future_tolerance
                or age > self._detection_max_age
            ):
                continue
            camera_detections = camera.get("detections") or []
            if isinstance(camera_detections, list):
                detections.extend(camera_detections)
        return detections

    def _apply(self, detections: list, now: Optional[float] = None,
               use_detection_ts: bool = False) -> None:
        import carla

        self._load_blueprints()
        if now is None:
            now = time.time()

        if self._reviewed_placement == "strict":
            detections = sorted(
                detections,
                key=lambda item: (
                    str((item.get("reviewed_localization") or {}).get("trajectory_id", "")),
                    (item.get("reviewed_localization") or {}).get("sample_index", -1)
                    if isinstance((item.get("reviewed_localization") or {}).get("sample_index"), int)
                    else -1,
                    str(item.get("event_id", "")),
                ),
            )

        for det in detections:
            object_id = det.get("object_id")
            object_type = det.get("object_type") or "car"
            reviewed = None
            if self._reviewed_placement == "strict":
                try:
                    reviewed = validate_contract(
                        det.get("reviewed_localization"),
                        det,
                        self._reviewed_context,
                    )
                except ReviewedLocalizationError as exc:
                    self._reject_strict(det, exc.reason)
                    continue
                object_id = reviewed["global_track_id"]
                if object_type not in VEHICLE_TYPES:
                    self._reject_strict(det, "strict_mode_vehicle_only")
                    continue
            else:
                gps = det.get("gps_location") or {}
                lat, lon = gps.get("latitude"), gps.get("longitude")
                if not object_id or lat is None or lon is None:
                    continue
            if object_type not in VEHICLE_TYPES and object_type != "person":
                continue

            track = self._tracks.get(object_id)
            if reviewed is not None and track is not None:
                sequence_error = self._strict_sequence_is_valid(
                    track, reviewed, object_type
                )
                if sequence_error is not None:
                    self._reject_strict(det, sequence_error)
                    continue
            if track is None:
                track = TwinTrack(object_id, object_type)
                self._tracks[object_id] = track
            gps = det.get("gps_location") or {}
            lat, lon = gps.get("latitude"), gps.get("longitude")
            if reviewed is None:
                self._commit_detection_metadata(track, det, now, use_detection_ts)

            if reviewed is not None:
                # The family/key are needed for deterministic blueprint choice
                # before a first spawn.  The reviewed sample itself is committed
                # only after the exact transform succeeds.
                track.blueprint_family = reviewed["blueprint_family"]
                track.placement_key_sha256 = reviewed["placement_key_sha256"]
                location = self._reviewed_location_for(reviewed)
            else:
                location = self._location_for(track, float(lat), float(lon))
            if location is None:
                continue

            if track.actor_id is None:
                bp = self._blueprint_for(track)
                if bp is None:
                    continue
                intended_transform = carla.Transform(
                    location,
                    carla.Rotation(
                        yaw=(reviewed["heading_deg"] if reviewed else track.yaw)
                    ),
                )
                actor = None
                for dx, dy, dz in SPAWN_BOOTSTRAP_OFFSETS:
                    candidate_transform = carla.Transform(
                        carla.Location(
                            x=location.x + dx,
                            y=location.y + dy,
                            z=location.z + dz,
                        ),
                        carla.Rotation(
                            yaw=(reviewed["heading_deg"] if reviewed else track.yaw)
                        ),
                    )
                    actor = self._world.try_spawn_actor(bp, candidate_transform)
                    if actor is None:
                        continue
                    try:
                        actor.set_simulate_physics(False)
                        actor.set_transform(intended_transform)
                    except Exception:
                        logger.warning(
                            "Twin spawn setup failed for %s (%s); "
                            "destroying provisional actor",
                            object_id,
                            bp.id,
                            exc_info=True,
                        )
                        try:
                            actor.destroy()
                        except Exception:
                            logger.error(
                                "Twin provisional actor cleanup failed for %s",
                                object_id,
                                exc_info=True,
                            )
                        actor = None
                        continue
                    break
                if actor is None:
                    logger.info(
                        "Twin spawn blocked for %s (%s) at "
                        "(%.1f, %.1f, %.1f) after %d bounded candidates "
                        "within %.1fm; retrying next poll",
                        object_id,
                        bp.id,
                        location.x,
                        location.y,
                        location.z,
                        len(SPAWN_BOOTSTRAP_OFFSETS),
                        SPAWN_BOOTSTRAP_MAX_OFFSET_M,
                    )
                    continue
                track.actor_id = actor.id
                if reviewed is not None:
                    self._commit_reviewed_track(track, reviewed, location)
                    self._commit_detection_metadata(
                        track, det, now, use_detection_ts
                    )
                else:
                    track.current = location
                    track.target = location
                logger.info(
                    "Twin spawn: %s (%s) as %s at (%.1f, %.1f)",
                    object_id, object_type, bp.id, location.x, location.y,
                )
            else:
                # New GPS fix: update motion yaw, retarget the lerp.
                if reviewed is None and track.current is not None:
                    dx = location.x - track.current.x
                    dy = location.y - track.current.y
                    if math.hypot(dx, dy) > 1.5:
                        track.yaw = math.degrees(math.atan2(dy, dx))
                if reviewed is not None:
                    actor = self._world.get_actor(track.actor_id)
                    if actor is None:
                        track.actor_id = None
                        self._reject_strict(det, "strict_actor_vanished")
                        continue
                    try:
                        actor.set_transform(
                            carla.Transform(
                                location,
                                carla.Rotation(yaw=reviewed["heading_deg"]),
                            )
                        )
                    except Exception:
                        self._reject_strict(det, "strict_exact_transform_failed")
                        logger.warning(
                            "Strict twin transform failed for %s",
                            object_id,
                            exc_info=True,
                        )
                        continue
                    self._commit_reviewed_track(track, reviewed, location)
                    self._commit_detection_metadata(
                        track, det, now, use_detection_ts
                    )
                    track.lerp_start = time.time()
                    track.lerp_duration = 0.0
                else:
                    track.target = location
                    # Lerp progress always runs on the wall clock, even when
                    # `now` is a replay clock.
                    track.lerp_start = time.time()
                    track.lerp_duration = self._poll_interval

        self._despawn_stale(now)

    def _despawn_stale(self, now: float) -> None:
        for object_id in list(self._tracks):
            track = self._tracks[object_id]
            if now - track.last_seen <= self._despawn_after:
                continue
            self._destroy_track(track)
            del self._tracks[object_id]
            logger.info("Twin despawn: %s (unseen for %.0fs)", object_id, now - track.last_seen)

    def _destroy_track(self, track: TwinTrack) -> None:
        if track.actor_id is None:
            return
        try:
            actor = self._world.get_actor(track.actor_id)
            if actor is not None:
                actor.destroy()
        except Exception:
            logger.debug("Twin actor %s already gone", track.actor_id)
        track.actor_id = None

    # ------------------------------------------------------------------
    # Replay (drive the twin from recorded detections)
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def replay_supported(self) -> bool:
        return self._range_fetcher is not None

    def replay_clock(self) -> Optional[float]:
        """Current virtual time of the replay (epoch seconds)."""
        if self._replay is None:
            return None
        r = self._replay
        return r["start"] + (time.time() - r["wall0"]) * r["speed"]

    def start_replay(self, start_epoch: float, speed: float = 1.0) -> None:
        """Switch the twin to replaying recorded detections from a timestamp.

        The detections DB keeps seven days (TTL), so any timestamp in that window
        replays; the rig cameras then render the past scene live.
        """
        if self._range_fetcher is None:
            raise RuntimeError("Replay unavailable: no detections range fetcher")
        self.clear()
        self._replay_generation += 1
        self._pending_replay = None
        self._mode = "replay"
        self._replay = {
            "start": start_epoch,
            "wall0": time.time(),
            "speed": max(0.25, min(float(speed), 8.0)),
            "cursor": start_epoch,
        }
        logger.info("Twin replay started at %s (speed %.2fx)",
                    _epoch_to_iso(start_epoch), self._replay["speed"])

    def go_live(self) -> None:
        """Return the twin to mirroring live detections."""
        if self._mode != "live":
            logger.info("Twin returning to live mode")
        self.clear()
        self._replay_generation += 1
        self._pending_replay = None
        self._mode = "live"
        self._replay = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Poll loop; HTTP happens in an executor, actor ops on the loop."""
        loop = asyncio.get_running_loop()
        logger.info("Twin sync polling %s every %.1fs", self._detections_url, self._poll_interval)
        while not self._stopped:
            try:
                if self._mode == "replay":
                    await loop.run_in_executor(None, self._fetch_replay_chunk)
                    self._apply_pending_replay()
                else:
                    detections = await loop.run_in_executor(None, self._fetch_detections)
                    self._poll_failures = 0
                    if detections is not None and self._mode == "live":
                        self._apply(detections)
            except requests.RequestException as exc:
                self._poll_failures += 1
                if self._poll_failures in (1, 10) or self._poll_failures % 60 == 0:
                    logger.warning(
                        "Twin sync poll failed (%d in a row): %s", self._poll_failures, exc
                    )
                self._despawn_stale(time.time())
            except Exception:
                logger.error("Twin sync apply error", exc_info=True)
            await asyncio.sleep(self._poll_interval)

    def _fetch_replay_chunk(self) -> None:
        """Blocking part of a replay step: fetch the next detections chunk."""
        replay = self._replay
        generation = self._replay_generation
        if replay is None:
            return
        clock = self.replay_clock()
        cursor = replay["cursor"]
        if clock is None or clock <= cursor:
            if self._replay is replay and self._replay_generation == generation:
                self._pending_replay = (generation, [], clock)
            return
        chunk_end = min(clock, cursor + 30.0)
        result = fetch_all_detection_pages(
            self._range_fetcher,
            _epoch_to_iso(cursor),
            _epoch_to_iso(chunk_end),
            page_size=200,
        )
        items = result.get("items", []) or []
        items.sort(key=lambda item: item.get("timestamp_utc") or "")
        # The event loop may have accepted a newer replay command while this
        # blocking fetch was in flight.  Discard rather than contaminating the
        # new replay with the old range.
        if self._replay is not replay or self._replay_generation != generation:
            return
        self._pending_replay = (generation, items, clock)
        replay["cursor"] = chunk_end

    def _apply_pending_replay(self) -> None:
        """Actor mutations for a replay step (runs on the event loop)."""
        pending = getattr(self, "_pending_replay", None)
        self._pending_replay = None
        if pending is None or self._replay is None:
            return
        generation, items, clock = pending
        if generation != self._replay_generation:
            return
        if clock is None:
            return
        self._apply(items, now=clock, use_detection_ts=True)

    def tick(self) -> None:
        """Advance position lerps; called from the bridge tick loop."""
        import carla

        now = time.time()
        for track in self._tracks.values():
            if track.actor_id is None or track.target is None:
                continue
            if track.current is None:
                track.current = track.target
            t = 1.0
            if track.lerp_duration > 0:
                t = min((now - track.lerp_start) / track.lerp_duration, 1.0)
            x = track.current.x + (track.target.x - track.current.x) * t
            y = track.current.y + (track.target.y - track.current.y) * t
            z = track.current.z + (track.target.z - track.current.z) * t
            if t >= 1.0:
                track.current = track.target
            try:
                actor = self._world.get_actor(track.actor_id)
                if actor is None:
                    track.actor_id = None
                    continue
                actor.set_transform(
                    carla.Transform(carla.Location(x=x, y=y, z=z), carla.Rotation(yaw=track.yaw))
                )
            except Exception:
                logger.debug("Twin tick transform failed for %s", track.object_id)

    def actor_ids(self) -> set:
        return {t.actor_id for t in self._tracks.values() if t.actor_id is not None}

    def _track_status(self, track: TwinTrack) -> dict:
        """Return a JSON-safe detection-to-CARLA actor evidence record."""
        tracked_actor_id = track.actor_id
        resolved_actor_id = None
        actor_present = False
        actor_type = None
        transform_payload = None
        raw_to_actor_planar_m = None
        if track.actor_id is not None:
            try:
                actor = self._world.get_actor(track.actor_id)
                if actor is not None:
                    resolved_actor_id = int(actor.id)
                    actor_present = True
                    actor_type = getattr(actor, "type_id", None)
                    transform = actor.get_transform()
                    transform_payload = {
                        "location": {
                            "x": float(transform.location.x),
                            "y": float(transform.location.y),
                            "z": float(transform.location.z),
                        },
                        "rotation": {
                            "pitch": float(transform.rotation.pitch),
                            "yaw": float(transform.rotation.yaw),
                            "roll": float(transform.rotation.roll),
                        },
                    }
                    if isinstance(track.raw_carla_location, dict):
                        raw_to_actor_planar_m = math.hypot(
                            float(transform.location.x)
                            - float(track.raw_carla_location["x"]),
                            float(transform.location.y)
                            - float(track.raw_carla_location["y"]),
                        )
            except Exception:
                logger.debug(
                    "Twin status transform unavailable for %s", track.object_id
                )

        payload = {
            "object_id": track.object_id,
            "object_type": track.object_type,
            "event_id": track.event_id,
            "detection_timestamp_utc": track.detection_timestamp_utc,
            "media_timestamp_utc": track.media_timestamp_utc,
            "timestamp_schema_version": track.timestamp_schema_version,
            "media_time_trusted": track.media_time_trusted,
            "media_clock": track.media_clock,
            "device_id": track.device_id,
            "track_id": track.track_id,
            "bbox": track.bbox,
            "gps_location": track.gps_location,
            "raw_carla_location": track.raw_carla_location,
            "target_carla_location": (
                {
                    "x": float(track.target.x),
                    "y": float(track.target.y),
                    "z": float(track.target.z),
                }
                if track.target is not None else None
            ),
            "lane_snap_distance_m": track.lane_snap_distance_m,
            "raw_to_target_planar_m": track.raw_to_target_planar_m,
            "raw_to_actor_planar_m": raw_to_actor_planar_m,
            "reference_to_actor_planar_m": None,
            "placement_planar_error_m": track.placement_planar_error_m,
            "placement_metric_status": "independent_reference_missing",
            # ``actor_id`` is acceptance evidence, so expose it only after
            # resolving the actor and its transform from the live UE5 world.
            # Keep the track's last ID separately for diagnostics.
            "tracked_actor_id": tracked_actor_id,
            "actor_id": resolved_actor_id,
            "actor_present": actor_present and transform_payload is not None,
            "actor_type": actor_type,
            "carla_transform": transform_payload,
        }
        if self._reviewed_placement == "strict":
            reviewed = track.reviewed_localization
            reviewed_target = reviewed.get("position_m") if reviewed else None
            reviewed_to_actor_planar_m = None
            if reviewed_target is not None and transform_payload is not None:
                actor_location = transform_payload["location"]
                reviewed_to_actor_planar_m = math.hypot(
                    actor_location["x"] - reviewed_target["x"],
                    actor_location["y"] - reviewed_target["y"],
                )
            payload.update({
                "reviewed_placement_mode": "strict",
                "reviewed_localization_schema": (
                    reviewed.get("schema") if reviewed else None
                ),
                "reviewed_contract_sha256": (
                    reviewed.get("contract_sha256") if reviewed else None
                ),
                "trajectory_id": track.trajectory_id,
                "trajectory_sample_index": track.sample_index,
                "reviewed_world_location": reviewed_target,
                "reviewed_to_actor_planar_m": reviewed_to_actor_planar_m,
                "placement_planar_error_m": reviewed_to_actor_planar_m,
                "placement_metric_status": "reviewed_world_exact",
                "blueprint_family": track.blueprint_family,
                "blueprint_selection_digest": track.placement_key_sha256,
                "vehicle_dimensions_m": track.vehicle_dimensions_m,
                "placement_provenance": (
                    {
                        key: reviewed[key]
                        for key in (
                            "frame_sha256",
                            "mask_sha256",
                            "detector_model_sha256",
                            "detector_config_sha256",
                            "cameras_json_sha256",
                            "camera_config_sha256",
                            "intrinsics_artifact_sha256",
                            "opendrive_sha256",
                            "consensus_sha256",
                            "factor_graph_sha256",
                            "identity_evidence_sha256",
                            "uncertainty_m",
                        )
                    }
                    if reviewed else None
                ),
            })
        return payload

    def status(self) -> dict:
        clock = self.replay_clock()
        objects = [
            self._track_status(self._tracks[object_id])
            for object_id in sorted(self._tracks)
        ]
        payload = {
            "tracks": len(self._tracks),
            "actors": sum(1 for item in objects if item["actor_present"]),
            "poll_failures": self._poll_failures,
            "detections_url": self._detections_url,
            "mode": self._mode,
            "replay_supported": self.replay_supported,
            "replay_clock": _epoch_to_iso(clock) if clock is not None else None,
            "objects": objects,
        }
        if self._reviewed_placement == "strict":
            payload.update({
                "reviewed_placement_mode": "strict",
                "strict_rejections": dict(sorted(self._strict_rejections.items())),
                "recent_strict_rejections": list(self._recent_strict_rejections),
                "strict_context": {
                    "map_name": self._reviewed_context.map_name,
                    "opendrive_sha256": self._reviewed_context.opendrive_sha256,
                    "cameras_json_sha256": self._reviewed_context.cameras_json_sha256,
                },
            })
        return payload

    def clear(self) -> None:
        """Destroy all twin actors and forget tracks (keeps polling)."""
        for track in self._tracks.values():
            self._destroy_track(track)
        self._tracks.clear()

    def stop(self) -> None:
        """Stop polling and destroy all twin actors."""
        self._stopped = True
        self._replay_generation += 1
        self._pending_replay = None
        self._replay = None
        self.clear()
        logger.info("Twin sync stopped")

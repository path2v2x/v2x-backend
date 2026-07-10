"""
Twin Camera Rig — CARLA RGB sensors mirroring the real street cameras.

Spawns one static camera per real camera at the exact real-world pose
(shared pole GPS + per-channel height/pitch/yaw/heading from
config/cameras.json) so the digital twin renders the same view the
physical camera sees. Frames are kept as latest-JPEG buffers for the
/twin WebSocket stream.

Pose conversion notes:
- The RFS map is georeferenced (tmerc): CARLA x = easting, y = -northing
  (UE left-handed). ``gps_to_carla`` handles the latitude mirror.
- A compass bearing H (clockwise from true north) therefore maps to
  CARLA yaw = H - 90. The camera's optical-axis bearing is
  heading_deg + yaw_deg (mounting pan on top of the pole heading),
  matching how the perception pipeline composes them.
- Perception pitch is negative-down in its OpenCV model, which is the
  same sign convention as CARLA's Rotation.pitch (negative = down).
"""

import hashlib
import json
import logging
import math
import os
from copy import deepcopy
import threading
from pathlib import Path
from typing import Dict, List, Optional

from digital_twin_bridge.frame_encoder import encode_jpeg
from digital_twin_bridge.geo_utils import gps_to_carla

logger = logging.getLogger(__name__)

DEFAULT_CAMERAS_JSON = Path(__file__).resolve().parents[3] / "config" / "cameras.json"

TWIN_SUPPORTED_MAP_LEAVES = {"richmond_field_station_richmond_ca"}


def load_cameras_config(path: Optional[str] = None) -> Optional[dict]:
    """Load config/cameras.json (override with DTB_CAMERAS_JSON)."""
    config_path = path or os.environ.get("DTB_CAMERAS_JSON") or str(DEFAULT_CAMERAS_JSON)
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cameras config unavailable (%s): %s", config_path, exc)
        return None
    if not config.get("cameras") or not config.get("site"):
        logger.warning("Cameras config %s missing 'cameras' or 'site'.", config_path)
        return None
    return config


def is_twin_supported_map(carla_map_name: str) -> bool:
    """The rig only makes sense on the georeferenced RFS map."""
    leaf = carla_map_name.rsplit("/", 1)[-1].lower()
    return leaf in TWIN_SUPPORTED_MAP_LEAVES


def heading_to_carla_yaw(heading_deg: float, yaw_deg: float = 0.0) -> float:
    """Convert a compass bearing (+ mounting pan) to CARLA yaw degrees.

    With the map's x = easting / y = -northing axes, north is CARLA yaw
    -90 and east is 0, so yaw = bearing - 90 (normalised to [-180, 180]).
    """
    yaw = (heading_deg + yaw_deg) - 90.0
    while yaw > 180.0:
        yaw -= 360.0
    while yaw < -180.0:
        yaw += 360.0
    return yaw


def horizontal_fov_deg(intrinsics: dict) -> float:
    """Horizontal FOV from pinhole intrinsics: 2*atan((W/2)/fx)."""
    return math.degrees(2.0 * math.atan((intrinsics["width"] / 2.0) / intrinsics["fx"]))


def twin_horizontal_fov_deg(camera: dict) -> float:
    """Configured twin FOV, including an auditable calibration offset."""
    twin_pose = camera.get("twin_pose") or {}
    return horizontal_fov_deg(camera["intrinsics"]) + float(
        twin_pose.get("fov_offset_deg", 0.0)
    )


def camera_with_twin_pose(camera: dict, overrides: dict) -> dict:
    """Return a candidate camera without mutating the shared configuration."""
    candidate = deepcopy(camera)
    pose = dict(candidate.get("twin_pose") or {})
    pose.update({key: float(value) for key, value in overrides.items()})
    candidate["twin_pose"] = pose
    return candidate


def configure_twin_camera_blueprint(
    camera_bp,
    camera: dict,
    image_width: int,
    image_height: int,
    fps: Optional[float] = None,
) -> None:
    """Apply the same explicit optical model in the rig and verifier.

    CARLA's RGB camera defaults to ``lens_k=-1`` (barrel distortion), while
    the surveyed calibration and reprojection harness use a pinhole model.
    Default the twin to zero distortion unless measured coefficients are
    recorded in ``twin_lens``; otherwise the rendered image and numerical
    verifier are testing different cameras.
    """
    camera_bp.set_attribute("image_size_x", str(int(image_width)))
    camera_bp.set_attribute("image_size_y", str(int(image_height)))
    camera_bp.set_attribute("fov", f"{twin_horizontal_fov_deg(camera):.6f}")
    if fps is not None:
        camera_bp.set_attribute("sensor_tick", f"{1.0 / float(fps):.6f}")

    lens = {"lens_k": 0.0, "lens_kcube": 0.0}
    lens.update(camera.get("twin_lens") or {})
    for key in (
        "lens_k",
        "lens_kcube",
        "lens_circle_falloff",
        "lens_circle_multiplier",
        "lens_x_size",
        "lens_y_size",
    ):
        if key not in lens:
            continue
        try:
            camera_bp.set_attribute(key, str(float(lens[key])))
        except (IndexError, RuntimeError, TypeError, ValueError):
            logger.warning("Twin camera %s has invalid %s", camera.get("id"), key)


def compute_twin_camera_transform(carla_map, site: dict, camera: dict):
    """CARLA Transform for a real camera: pole GPS + height, mirrored pose.

    Optional per-camera ``twin_pose`` overrides in cameras.json refine the
    twin against the modelled map (fitted with tools/fit_twin_camera_poses.py):
    ``yaw_offset_deg``, ``pitch_offset_deg``, ``roll_offset_deg``,
    ``height_offset_m``, ``forward_offset_m``, and ``right_offset_m``. The
    translations move the virtual camera away from modelled pole/tree meshes
    and allow a full 6-DOF physical mounting calibration.
    """
    import carla

    twin_pose = camera.get("twin_pose") or {}
    yaw = heading_to_carla_yaw(
        float(camera["heading_deg"]),
        float(camera["yaw_deg"]) + float(twin_pose.get("yaw_offset_deg", 0.0)),
    )
    pitch = float(camera["pitch_deg"]) + float(twin_pose.get("pitch_offset_deg", 0.0))
    roll = float(camera.get("roll_deg", 0.0)) + float(
        twin_pose.get("roll_offset_deg", 0.0)
    )

    location = gps_to_carla(carla_map, site["lat"], site["lon"])
    # gps_to_carla snaps z to the road surface; the camera sits on the
    # pole `height_m` above that.
    location.z += float(camera["height_m"]) + float(twin_pose.get("height_offset_m", 0.0))

    # A missing translation must mean the surveyed pole location.  A hidden
    # forward offset made fitted, deployed, and verified cameras disagree.
    forward = float(twin_pose.get("forward_offset_m", 0.0))
    right = float(twin_pose.get("right_offset_m", 0.0))
    if forward:
        yaw_rad = math.radians(yaw)
        location.x += forward * math.cos(yaw_rad)
        location.y += forward * math.sin(yaw_rad)
    if right:
        yaw_rad = math.radians(yaw)
        location.x -= right * math.sin(yaw_rad)
        location.y += right * math.cos(yaw_rad)

    rotation = carla.Rotation(pitch=pitch, yaw=yaw, roll=roll)
    return carla.Transform(location, rotation)


class TwinCameraRig:
    """Fixed CARLA cameras at the real street-camera poses.

    Server-owned (spawned once at boot, survives drive sessions). Actors
    carry role_name="twin_rig" so session cleanup and the actor audit
    leave them alone.
    """

    def __init__(
        self,
        world,
        carla_map,
        config: dict,
        image_width: int = 1280,
        image_height: int = 960,
        fps: float = 12.0,
        jpeg_quality: int = 70,
        frame_context_provider=None,
    ) -> None:
        self._world = world
        self._map = carla_map
        self._config = config
        self._camera_config = {
            str(camera["id"]): deepcopy(camera)
            for camera in config.get("cameras", [])
        }
        self._image_width = int(image_width)
        self._image_height = int(image_height)
        self._fps = float(fps)
        self._jpeg_quality = int(jpeg_quality)
        self._frame_context_provider = frame_context_provider
        self._cameras: Dict[str, object] = {}
        self._frames: Dict[str, bytes] = {}
        self._frame_metadata: Dict[str, dict] = {}
        self._frame_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._accepting_frames = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spawn(self) -> int:
        """Spawn one camera per configured channel. Returns spawn count."""
        bp_lib = self._world.get_blueprint_library()
        camera_bp = bp_lib.find("sensor.camera.rgb")
        if camera_bp is None:
            logger.warning("sensor.camera.rgb blueprint not found; twin rig disabled")
            return 0

        site = self._config["site"]
        self._accepting_frames = True
        for camera in self._config["cameras"]:
            camera_id = camera["id"]
            configure_twin_camera_blueprint(
                camera_bp,
                camera,
                self._image_width,
                self._image_height,
                self._fps,
            )
            try:
                camera_bp.set_attribute("role_name", "twin_rig")
            except (IndexError, RuntimeError):
                pass  # role_name attribute is optional on sensors

            transform = compute_twin_camera_transform(self._map, site, camera)
            try:
                actor = self._world.spawn_actor(camera_bp, transform)
            except Exception as exc:
                logger.warning("Twin camera %s spawn failed: %s", camera_id, exc)
                continue

            actor.listen(self._make_listener(camera_id))
            self._cameras[camera_id] = actor
            self._frame_counts[camera_id] = 0
            logger.info(
                "Twin camera %s spawned at (%.1f, %.1f, %.1f) yaw=%.1f pitch=%.1f fov=%.1f",
                camera_id,
                transform.location.x,
                transform.location.y,
                transform.location.z,
                transform.rotation.yaw,
                transform.rotation.pitch,
                twin_horizontal_fov_deg(camera),
            )

        logger.info("Twin camera rig ready: %d cameras", len(self._cameras))
        return len(self._cameras)

    def _make_listener(self, camera_id: str):
        def _on_frame(image):
            if not self._accepting_frames:
                return
            try:
                jpeg = encode_jpeg(image, quality=self._jpeg_quality)
                carla_frame = int(image.frame)
                sensor_timestamp = float(image.timestamp)
            except Exception as exc:
                logger.debug("Twin frame encode error (%s): %s", camera_id, exc)
                return
            context = {}
            if self._frame_context_provider is not None:
                try:
                    context = self._frame_context_provider() or {}
                except Exception:
                    logger.debug("Twin frame context unavailable (%s)", camera_id)
            with self._lock:
                self._frames[camera_id] = jpeg
                count = self._frame_counts.get(camera_id, 0) + 1
                self._frame_counts[camera_id] = count
                self._frame_metadata[camera_id] = {
                    "camera_id": camera_id,
                    "frame_count": count,
                    "carla_frame": carla_frame,
                    "sensor_timestamp": sensor_timestamp,
                    "jpeg_sha256": hashlib.sha256(jpeg).hexdigest(),
                    "mode": context.get("mode"),
                    "replay_clock": context.get("replay_clock"),
                }

        return _on_frame

    def destroy(self) -> None:
        self._accepting_frames = False
        for camera_id, actor in self._cameras.items():
            try:
                actor.stop()
            except Exception:
                pass
            try:
                actor.destroy()
            except Exception:
                logger.debug("Twin camera %s already gone", camera_id)
        self._cameras.clear()
        with self._lock:
            self._frames.clear()
            self._frame_metadata.clear()
        logger.info("Twin camera rig destroyed")

    # ------------------------------------------------------------------
    # Frames / status
    # ------------------------------------------------------------------

    @property
    def camera_ids(self) -> List[str]:
        return list(self._cameras.keys())

    def actor_ids(self) -> set:
        return {actor.id for actor in self._cameras.values()}

    def has_camera(self, camera_id: str) -> bool:
        return camera_id in self._cameras

    def get_latest_frame(self, camera_id: str) -> Optional[bytes]:
        with self._lock:
            return self._frames.get(camera_id)

    def get_latest_frame_packet(self, camera_id: str):
        """Return an atomically paired JPEG and render/replay metadata."""
        with self._lock:
            frame = self._frames.get(camera_id)
            metadata = self._frame_metadata.get(camera_id)
            if frame is None or metadata is None:
                return None
            return frame, dict(metadata)

    def camera_model(self, camera_id: str) -> Optional[dict]:
        """Return the exact, fingerprinted UE5 camera model behind a stream.

        Acceptance tooling must be able to project a replay actor through the
        same transform and optical settings that produced the JPEG.  Returning
        only a channel name lets stale or differently configured cameras look
        equivalent, so the hello protocol carries this immutable evidence.
        """
        actor = self._cameras.get(camera_id)
        camera = self._camera_config.get(camera_id)
        if actor is None or camera is None:
            return None
        transform = actor.get_transform()
        lens = {"lens_k": 0.0, "lens_kcube": 0.0}
        lens.update(camera.get("twin_lens") or {})
        canonical = json.dumps(
            camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return {
            "camera_id": camera_id,
            "actor_id": int(actor.id),
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
            "transform": {
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
            },
            "image": {
                "width": self._image_width,
                "height": self._image_height,
                "horizontal_fov_deg": float(twin_horizontal_fov_deg(camera)),
            },
            "lens": {
                key: float(value)
                for key, value in lens.items()
                if key in {"lens_k", "lens_kcube"}
            },
        }

    def status(self) -> dict:
        with self._lock:
            frame_counts = dict(self._frame_counts)
        return {
            "cameras": self.camera_ids,
            "frame_counts": frame_counts,
            "width": self._image_width,
            "height": self._image_height,
            "fps": self._fps,
        }

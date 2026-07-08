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

import json
import logging
import math
import os
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


def compute_twin_camera_transform(carla_map, site: dict, camera: dict):
    """CARLA Transform for a real camera: pole GPS + height, mirrored pose.

    Optional per-camera ``twin_pose`` overrides in cameras.json refine the
    twin against the modelled map (fitted with tools/fit_twin_camera_poses.py):
    ``yaw_offset_deg``, ``pitch_offset_deg``, ``height_offset_m``, and
    ``forward_offset_m`` (moves the camera off the modelled pole mesh so it
    doesn't occlude the view).
    """
    import carla

    twin_pose = camera.get("twin_pose") or {}
    yaw = heading_to_carla_yaw(
        float(camera["heading_deg"]),
        float(camera["yaw_deg"]) + float(twin_pose.get("yaw_offset_deg", 0.0)),
    )
    pitch = float(camera["pitch_deg"]) + float(twin_pose.get("pitch_offset_deg", 0.0))

    location = gps_to_carla(carla_map, site["lat"], site["lon"])
    # gps_to_carla snaps z to the road surface; the camera sits on the
    # pole `height_m` above that.
    location.z += float(camera["height_m"]) + float(twin_pose.get("height_offset_m", 0.0))

    forward = float(twin_pose.get("forward_offset_m", 0.5))
    if forward:
        yaw_rad = math.radians(yaw)
        location.x += forward * math.cos(yaw_rad)
        location.y += forward * math.sin(yaw_rad)

    rotation = carla.Rotation(pitch=pitch, yaw=yaw, roll=0.0)
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
    ) -> None:
        self._world = world
        self._map = carla_map
        self._config = config
        self._image_width = int(image_width)
        self._image_height = int(image_height)
        self._fps = float(fps)
        self._jpeg_quality = int(jpeg_quality)
        self._cameras: Dict[str, object] = {}
        self._frames: Dict[str, bytes] = {}
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
            camera_bp.set_attribute("image_size_x", str(self._image_width))
            camera_bp.set_attribute("image_size_y", str(self._image_height))
            camera_bp.set_attribute("fov", f"{horizontal_fov_deg(camera['intrinsics']):.2f}")
            camera_bp.set_attribute("sensor_tick", f"{1.0 / self._fps:.4f}")
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
                horizontal_fov_deg(camera["intrinsics"]),
            )

        logger.info("Twin camera rig ready: %d cameras", len(self._cameras))
        return len(self._cameras)

    def _make_listener(self, camera_id: str):
        def _on_frame(image):
            if not self._accepting_frames:
                return
            try:
                jpeg = encode_jpeg(image, quality=self._jpeg_quality)
            except Exception as exc:
                logger.debug("Twin frame encode error (%s): %s", camera_id, exc)
                return
            with self._lock:
                self._frames[camera_id] = jpeg
                self._frame_counts[camera_id] = self._frame_counts.get(camera_id, 0) + 1

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

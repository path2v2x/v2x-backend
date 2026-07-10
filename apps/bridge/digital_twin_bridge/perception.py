"""
Perception module — Tesla-style 8-camera sensor stack on the ego.

Attaches paired semantic-segmentation and depth cameras at 8 positions
mimicking a real Tesla. Each pair fires at 10 Hz; we buffer the latest
frame per camera in a thread-safe dict. Every telemetry tick the bridge
calls ``scan()`` which:
  - finds connected blobs per class in each camera's semantic mask
  - reads median depth inside each blob
  - projects pixel + depth → 3D ego-frame coords
  - merges duplicates across overlapping cameras
  - id-tracks across ticks for stable WarningStack dedup

Output: ``list[Detection]`` returned to the caller, also exposable as
plain dicts via ``Detection.to_dict()`` for the WebSocket telemetry.

CARLA semantic class IDs (0.9.13+ Cityscapes mapping)::

    4  pedestrian   →  pedestrian
    10 vehicles     →  vehicle  (cars, trucks, buses, motorbikes, bikes)
    12 trafficsign  →  traffic_sign
    18 trafficlight →  traffic_light
    19 static       →  cone     (cones, barriers, mailboxes; best fit)
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ── CARLA semantic-seg class IDs we care about ──

SEM_CLASSES: dict[int, str] = {
    4: "pedestrian",
    10: "vehicle",
    12: "traffic_sign",
    18: "traffic_light",
    19: "cone",
}

TRACKED_CLASSES = ("vehicle", "pedestrian", "cone", "traffic_sign", "traffic_light")


# ── Detection ──

@dataclass
class Detection:
    """A single perceived object expressed in the ego's local frame."""

    id: str                              # stable across ticks (e.g. "vehicle-7")
    class_name: str                      # one of TRACKED_CLASSES
    pos: tuple[float, float]             # (forward_m, right_m) ego frame
    distance_m: float
    bbox_dim: tuple[float, float]        # (length_m, width_m) — crude estimate
    velocity: Optional[tuple[float, float]] = None
    in_path: bool = False
    alert_level: str = "none"            # none / info / warn / critical
    source_camera: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "class": self.class_name,
            "pos": [round(self.pos[0], 2), round(self.pos[1], 2)],
            "distance": round(self.distance_m, 2),
            "bbox_dim": [round(self.bbox_dim[0], 2), round(self.bbox_dim[1], 2)],
            "in_path": self.in_path,
            "alert": self.alert_level,
        }
        if self.velocity is not None:
            out["velocity"] = [round(self.velocity[0], 2), round(self.velocity[1], 2)]
        return out


# ── Camera mount layout (Tesla-ish, 8 cameras for full 360° coverage) ──

@dataclass(frozen=True)
class CameraConfig:
    name: str
    x_m: float           # mount x in ego frame (+forward)
    y_m: float           # mount y in ego frame (+right)
    z_m: float           # mount z in ego frame (+up)
    yaw_deg: float       # rotation around z (0 = looking forward)
    fov_deg: float
    width: int = 640
    height: int = 480


TESLA_LAYOUT: tuple[CameraConfig, ...] = (
    CameraConfig("front_main",     +1.5,  0.0, 1.3,    0.0, 50.0),
    CameraConfig("front_wide",     +1.7,  0.0, 0.6,    0.0, 120.0),
    CameraConfig("front_narrow",   +1.5,  0.0, 1.3,    0.0, 35.0),
    CameraConfig("pillar_left",    +0.3, -0.9, 1.1,  -45.0, 90.0),
    CameraConfig("pillar_right",   +0.3, +0.9, 1.1,  +45.0, 90.0),
    CameraConfig("repeater_left",  +0.3, -0.9, 1.0, -135.0, 90.0),
    CameraConfig("repeater_right", +0.3, +0.9, 1.0, +135.0, 90.0),
    CameraConfig("rear",           -1.9,  0.0, 1.0,  180.0, 50.0),
)


# ── Projection math ──

def fov_to_focal_px(fov_deg: float, image_width: int) -> float:
    """Horizontal FOV → focal length in pixels (pinhole model)."""
    return image_width / (2.0 * math.tan(math.radians(fov_deg) / 2.0))


def pixel_to_camera_frame(
    px: float,
    py: float,
    depth_m: float,
    fx: float,
    fy: float,
    cx_px: float,
    cy_px: float,
) -> tuple[float, float, float]:
    """Unproject a (pixel, depth) sample to camera-frame 3D coords.

    Camera frame uses OpenCV convention: +x right, +y down, +z forward.
    """
    x = (px - cx_px) * depth_m / fx
    y = (py - cy_px) * depth_m / fy
    z = depth_m
    return x, y, z


def camera_frame_to_ego(
    cam_xyz: tuple[float, float, float], cfg: CameraConfig
) -> tuple[float, float]:
    """Transform a camera-frame point (x_right, y_down, z_forward) to ego
    frame (x_forward, y_right). Only horizontal (XY in ego frame) — we
    drop the vertical axis since the dashboard is top-down.
    """
    x_right, _y_down, z_fwd = cam_xyz
    yaw_rad = math.radians(cfg.yaw_deg)
    cos = math.cos(yaw_rad)
    sin = math.sin(yaw_rad)
    # The camera's forward (camera +z) is the ego direction (cos, sin).
    # The camera's right (camera +x) is the ego direction (-sin, cos).
    ego_x = z_fwd * cos - x_right * sin + cfg.x_m
    ego_y = z_fwd * sin + x_right * cos + cfg.y_m
    return ego_x, ego_y


# ── Perception service ──

class PerceptionService:
    """Tesla-style 8-camera perception stack on the ego vehicle.

    Lifecycle (called by ``DriveSession``)::

        attach(world, ego)    # session start
        scan()                # every telemetry tick → list[Detection]
        detach()              # session end
    """

    MIN_BLOB_PX = 30
    MAX_DETECTION_RANGE_M = 80.0
    DEDUP_RADIUS_M = 1.5
    TRACK_GATE_M = 4.0
    DEPTH_MAX_M = 1000.0   # CARLA depth far plane

    def __init__(self, layout: tuple[CameraConfig, ...] = TESLA_LAYOUT) -> None:
        self._layout = layout
        self._sensors: list = []
        self._frames: dict[str, dict[str, Any]] = {}
        self._frames_lock = threading.Lock()
        self._tracked: list[Detection] = []
        self._next_track_ids: dict[str, int] = {}
        self._world = None
        self._ego = None
        self._attached = False

    # ─── lifecycle ─────────────────────────────────────────

    def attach(self, world, ego) -> None:
        """Spawn 16 sensor actors (8 sem-seg + 8 depth) attached to ego."""
        if self._attached:
            logger.warning("PerceptionService already attached; ignoring re-attach")
            return
        import carla
        self._world = world
        self._ego = ego
        bp_lib = world.get_blueprint_library()
        spawn_count = 0

        for cfg in self._layout:
            transform = carla.Transform(
                carla.Location(x=cfg.x_m, y=cfg.y_m, z=cfg.z_m),
                carla.Rotation(yaw=cfg.yaw_deg),
            )

            try:
                sem_bp = bp_lib.find("sensor.camera.semantic_segmentation")
                sem_bp.set_attribute("image_size_x", str(cfg.width))
                sem_bp.set_attribute("image_size_y", str(cfg.height))
                sem_bp.set_attribute("fov", str(cfg.fov_deg))
                sem_bp.set_attribute("sensor_tick", "0.1")
                sem_sensor = world.spawn_actor(
                    sem_bp,
                    transform,
                    attach_to=ego,
                    attachment_type=carla.AttachmentType.Rigid,
                )
                sem_sensor.listen(
                    lambda image, name=cfg.name: self._on_sem(name, image)
                )
                self._sensors.append(sem_sensor)
                spawn_count += 1

                depth_bp = bp_lib.find("sensor.camera.depth")
                depth_bp.set_attribute("image_size_x", str(cfg.width))
                depth_bp.set_attribute("image_size_y", str(cfg.height))
                depth_bp.set_attribute("fov", str(cfg.fov_deg))
                depth_bp.set_attribute("sensor_tick", "0.1")
                depth_sensor = world.spawn_actor(
                    depth_bp,
                    transform,
                    attach_to=ego,
                    attachment_type=carla.AttachmentType.Rigid,
                )
                depth_sensor.listen(
                    lambda image, name=cfg.name: self._on_depth(name, image)
                )
                self._sensors.append(depth_sensor)
                spawn_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to attach perception sensors for camera %s: %s",
                    cfg.name, e, exc_info=True,
                )
                continue

            self._frames[cfg.name] = {"sem": None, "depth": None}

        self._attached = True
        logger.info(
            "PerceptionService attached: %d cameras, %d sensor actors spawned",
            len(self._layout), spawn_count,
        )

    def detach(self) -> None:
        """Stop and destroy all attached sensor actors."""
        if not self._attached:
            return
        for s in self._sensors:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.destroy()
            except Exception:
                pass
        self._sensors.clear()
        with self._frames_lock:
            self._frames.clear()
        self._tracked.clear()
        self._next_track_ids.clear()
        self._world = None
        self._ego = None
        self._attached = False
        logger.info("PerceptionService detached")

    def actor_ids(self) -> list[int]:
        """Return the sensor actor IDs currently owned by this service."""
        return [
            actor_id
            for sensor in self._sensors
            if isinstance((actor_id := getattr(sensor, "id", None)), int)
        ]

    # ─── sensor callbacks ──────────────────────────────────

    def _on_sem(self, cam_name: str, image) -> None:
        try:
            arr = np.frombuffer(image.raw_data, dtype=np.uint8).reshape(
                (image.height, image.width, 4)
            )
            # CARLA semantic-seg encodes the class id in the R channel (BGRA layout).
            label = arr[:, :, 2].copy()
            with self._frames_lock:
                if cam_name in self._frames:
                    self._frames[cam_name]["sem"] = label
        except Exception:
            logger.debug("Sem callback failed for %s", cam_name, exc_info=True)

    def _on_depth(self, cam_name: str, image) -> None:
        try:
            arr = np.frombuffer(image.raw_data, dtype=np.uint8).reshape(
                (image.height, image.width, 4)
            ).astype(np.float32)
            # CARLA depth packing: normalized = (R + G*256 + B*256²) / (256³ - 1)
            b, g, r = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            normalized = (r + g * 256.0 + b * 65536.0) / 16777215.0
            depth_m = (normalized * self.DEPTH_MAX_M).astype(np.float32)
            with self._frames_lock:
                if cam_name in self._frames:
                    self._frames[cam_name]["depth"] = depth_m
        except Exception:
            logger.debug("Depth callback failed for %s", cam_name, exc_info=True)

    # ─── scan ──────────────────────────────────────────────

    def capture_scan_snapshot(self) -> dict[str, tuple[Any, Any, CameraConfig]]:
        """Capture immutable frame references for data-only worker analysis.

        Sensor callbacks replace whole numpy arrays rather than mutating arrays
        already stored in ``_frames``.  Holding these references is therefore
        safe after the lock is released and, importantly, requires no CARLA
        calls in the worker thread.
        """
        if not self._attached:
            return {}

        with self._frames_lock:
            frames = {
                name: (frame.get("sem"), frame.get("depth"))
                for name, frame in self._frames.items()
            }
        cfg_by_name = {config.name: config for config in self._layout}
        return {
            name: (sem, depth, cfg_by_name[name])
            for name, (sem, depth) in frames.items()
            if name in cfg_by_name and sem is not None and depth is not None
        }

    def analyze_scan_snapshot(
        self,
        snapshot: dict[str, tuple[Any, Any, CameraConfig]],
    ) -> list[Detection]:
        """Perform CPU-only extraction/dedup for a captured frame snapshot."""
        all_dets: list[Detection] = []
        for cam_name, (sem, depth, config) in snapshot.items():
            try:
                all_dets.extend(self._extract_detections(sem, depth, config))
            except Exception:
                logger.debug(
                    "Extraction failed for camera %s", cam_name, exc_info=True
                )
        return self._dedup_across_cameras(all_dets)

    def finalize_scan(self, detections: list[Detection]) -> list[Detection]:
        """Assign stable IDs on the owning event-loop thread."""
        tracked = self._assign_ids(detections)
        self._tracked = tracked
        return tracked

    def scan(self) -> list[Detection]:
        """Synchronous compatibility wrapper for offline/unit callers."""
        snapshot = self.capture_scan_snapshot()
        if not snapshot:
            return []
        return self.finalize_scan(self.analyze_scan_snapshot(snapshot))

    # ─── per-camera blob extraction ────────────────────────

    def _extract_detections(
        self, sem: np.ndarray, depth: np.ndarray, cfg: CameraConfig
    ) -> list[Detection]:
        if sem.shape != depth.shape:
            return []
        height, width = sem.shape
        fx = fov_to_focal_px(cfg.fov_deg, width)
        fy = fx  # square pixels
        cx_px = width / 2.0
        cy_px = height / 2.0

        detections: list[Detection] = []
        for sem_id, class_name in SEM_CLASSES.items():
            if class_name not in TRACKED_CLASSES:
                continue
            mask = (sem == sem_id).astype(np.uint8)
            if not mask.any():
                continue
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
                mask, connectivity=8
            )
            # Label 0 is the background.
            for i in range(1, num_labels):
                area = int(stats[i, cv2.CC_STAT_AREA])
                if area < self.MIN_BLOB_PX:
                    continue
                x0 = int(stats[i, cv2.CC_STAT_LEFT])
                y0 = int(stats[i, cv2.CC_STAT_TOP])
                w_px = int(stats[i, cv2.CC_STAT_WIDTH])
                h_px = int(stats[i, cv2.CC_STAT_HEIGHT])

                blob_mask = labels[y0:y0 + h_px, x0:x0 + w_px] == i
                depth_patch = depth[y0:y0 + h_px, x0:x0 + w_px]
                depths = depth_patch[blob_mask]
                if depths.size == 0:
                    continue
                depth_m = float(np.median(depths))
                if depth_m < 0.5 or depth_m > self.MAX_DETECTION_RANGE_M:
                    continue

                px_c, py_c = float(centroids[i, 0]), float(centroids[i, 1])
                cam_xyz = pixel_to_camera_frame(
                    px_c, py_c, depth_m, fx, fy, cx_px, cy_px
                )
                ego_fwd, ego_right = camera_frame_to_ego(cam_xyz, cfg)
                distance = math.hypot(ego_fwd, ego_right)
                if distance > self.MAX_DETECTION_RANGE_M:
                    continue

                # Crude bbox dim estimate: pixel extent × depth / focal length.
                world_w = (w_px * depth_m) / fx
                world_h = (h_px * depth_m) / fy
                detections.append(
                    Detection(
                        id="",
                        class_name=class_name,
                        pos=(ego_fwd, ego_right),
                        distance_m=distance,
                        bbox_dim=(max(0.4, world_h), max(0.3, world_w)),
                        in_path=self._is_in_path(ego_fwd, ego_right),
                        source_camera=cfg.name,
                    )
                )
        return detections

    @staticmethod
    def _is_in_path(ego_fwd: float, ego_right: float) -> bool:
        """Crude in-path check: ahead of ego and within a lane-width corridor."""
        return ego_fwd > 1.0 and abs(ego_right) < 1.6

    # ─── cross-camera dedup ────────────────────────────────

    def _dedup_across_cameras(self, dets: list[Detection]) -> list[Detection]:
        """Greedy dedup: merge same-class detections within DEDUP_RADIUS_M.
        Keeps the closer-to-ego one (more reliable depth)."""
        if not dets:
            return []
        ordered = sorted(dets, key=lambda d: d.distance_m)
        kept: list[Detection] = []
        for d in ordered:
            dup = False
            for k in kept:
                if k.class_name != d.class_name:
                    continue
                dx = d.pos[0] - k.pos[0]
                dy = d.pos[1] - k.pos[1]
                if dx * dx + dy * dy < self.DEDUP_RADIUS_M ** 2:
                    dup = True
                    break
            if not dup:
                kept.append(d)
        return kept

    # ─── stable-id tracking across ticks ───────────────────

    def _assign_ids(self, dets: list[Detection]) -> list[Detection]:
        """Assign stable ids by greedy nearest-match to the previous tick."""
        unmatched = list(self._tracked)
        for d in dets:
            best_idx = -1
            best_dist = self.TRACK_GATE_M
            for i, prev in enumerate(unmatched):
                if prev.class_name != d.class_name:
                    continue
                dx = d.pos[0] - prev.pos[0]
                dy = d.pos[1] - prev.pos[1]
                dist = math.hypot(dx, dy)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = i
            if best_idx >= 0:
                prev = unmatched.pop(best_idx)
                d.id = prev.id
                # Crude velocity carry-over (full filter is overkill here).
                d.velocity = (d.pos[0] - prev.pos[0], d.pos[1] - prev.pos[1])
            else:
                counter = self._next_track_ids.get(d.class_name, 0)
                d.id = f"{d.class_name}-{counter}"
                self._next_track_ids[d.class_name] = counter + 1
        return dets

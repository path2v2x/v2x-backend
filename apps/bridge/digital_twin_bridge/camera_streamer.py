"""
Camera Streamer — manages spectator cameras and produces frames.

Supports multiple camera views: chase, hood, bird's eye, free-look.
Frame encoding to JPEG for MJPEG fallback or WebRTC source.
"""

import io
import math
import logging
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

CAMERA_CONFIGS = {
    "chase": {"dx": -8.0, "dy": 0.0, "dz": 4.0, "pitch": -15.0},
    "hood":  {"dx": 0.2,  "dy": -0.38, "dz": 1.15, "pitch": 0.0},
    "bird":  {"dx": 0.0,  "dy": 0.0, "dz": 25.0, "pitch": -90.0},
    "free":  {"dx": -5.0, "dy": 0.0, "dz": 3.0, "pitch": -10.0},
}


def compute_camera_transform(view: str, vehicle_transform) -> object:
    """
    Compute the camera transform for a given view relative to the vehicle.
    Works with both real carla.Transform and MockTransform.
    """
    if view not in CAMERA_CONFIGS:
        raise ValueError(f"Invalid camera view: {view}. Must be one of {set(CAMERA_CONFIGS.keys())}")

    config = CAMERA_CONFIGS[view]
    vt = vehicle_transform

    yaw_rad = math.radians(vt.rotation.yaw)
    forward_x = math.cos(yaw_rad)
    forward_y = math.sin(yaw_rad)

    right_x = -forward_y
    right_y = forward_x

    cam_x = vt.location.x + config["dx"] * forward_x + config["dy"] * right_x
    cam_y = vt.location.y + config["dx"] * forward_y + config["dy"] * right_y
    cam_z = vt.location.z + config["dz"]

    cam_pitch = config["pitch"]
    cam_yaw = vt.rotation.yaw
    cam_roll = 0.0

    try:
        import carla
        return carla.Transform(
            carla.Location(x=cam_x, y=cam_y, z=cam_z),
            carla.Rotation(pitch=cam_pitch, yaw=cam_yaw, roll=cam_roll),
        )
    except ImportError:
        from tests.conftest import MockTransform, MockLocation, MockRotation
        return MockTransform(
            MockLocation(x=cam_x, y=cam_y, z=cam_z),
            MockRotation(pitch=cam_pitch, yaw=cam_yaw, roll=cam_roll),
        )


def encode_frame_jpeg(frame: np.ndarray, quality: int = 85) -> bytes:
    """Encode a numpy RGB array to JPEG bytes."""
    image = Image.fromarray(frame, mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


class CameraManager:
    """Manages CARLA spectator camera for a driving session."""

    def __init__(self, world, vehicle):
        self._world = world
        self._vehicle = vehicle
        self._active_view = "chase"
        self._spectator = world.get_spectator()

    def update(self, view: Optional[str] = None) -> None:
        """Update the spectator camera position based on current view."""
        if view is not None:
            self._active_view = view
        vehicle_transform = self._vehicle.get_transform()
        cam_transform = compute_camera_transform(self._active_view, vehicle_transform)
        self._spectator.set_transform(cam_transform)

    @property
    def active_view(self) -> str:
        return self._active_view

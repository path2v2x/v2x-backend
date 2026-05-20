"""Unit tests for the perception module's projection + dedup + tracking.

These tests don't touch CARLA at all — the perception module imports
``carla`` lazily inside ``attach()``, so the projection math and the
dedup / tracking pure-Python paths are testable without the simulator.
"""

import math

import pytest

from digital_twin_bridge.perception import (
    CameraConfig,
    Detection,
    PerceptionService,
    camera_frame_to_ego,
    fov_to_focal_px,
    pixel_to_camera_frame,
)


def mk_detection(
    class_name: str = "vehicle",
    pos: tuple[float, float] = (10.0, 0.0),
    id: str = "",
) -> Detection:
    return Detection(
        id=id,
        class_name=class_name,
        pos=pos,
        distance_m=math.hypot(*pos),
        bbox_dim=(4.5, 1.8),
    )


@pytest.mark.unit
class TestFovToFocal:
    def test_known_fov(self):
        # FOV 90° on a 640px-wide image: tan(45°) = 1 → focal = 320
        assert fov_to_focal_px(90.0, 640) == pytest.approx(320.0, abs=1e-3)

    def test_narrow_fov_higher_focal(self):
        # Narrower FOV should give larger focal length.
        assert fov_to_focal_px(30.0, 640) > fov_to_focal_px(90.0, 640)


@pytest.mark.unit
class TestPixelProjection:
    def test_center_pixel_at_depth_maps_to_optical_axis(self):
        # A pixel at the principal point with depth d should project to (0, 0, d).
        x, y, z = pixel_to_camera_frame(320, 240, 25.0, fx=320, fy=320, cx_px=320, cy_px=240)
        assert x == pytest.approx(0.0, abs=1e-6)
        assert y == pytest.approx(0.0, abs=1e-6)
        assert z == pytest.approx(25.0, abs=1e-6)

    def test_off_axis_pixel_projects_with_correct_x(self):
        # Pixel 100 to the right of center, depth 10m, fx=400
        # x = (100) * 10 / 400 = 2.5 m to the right of optical axis
        x, y, z = pixel_to_camera_frame(420, 240, 10.0, fx=400, fy=400, cx_px=320, cy_px=240)
        assert x == pytest.approx(2.5, abs=1e-6)
        assert y == pytest.approx(0.0, abs=1e-6)
        assert z == pytest.approx(10.0, abs=1e-6)


@pytest.mark.unit
class TestCameraFrameToEgo:
    def test_forward_camera_zero_yaw(self):
        # Front-main: yaw=0, mount at (1.5, 0). Camera-frame point at depth=20m,
        # offset 0 horizontally → ego frame should be (1.5 + 20, 0) = (21.5, 0).
        cfg = CameraConfig("test", x_m=1.5, y_m=0.0, z_m=1.0, yaw_deg=0.0, fov_deg=90)
        fx, fy = 0.0, 0.0  # straight ahead → cam_x=0
        ego_fwd, ego_right = camera_frame_to_ego((0.0, 0.0, 20.0), cfg)
        assert ego_fwd == pytest.approx(21.5, abs=1e-6)
        assert ego_right == pytest.approx(0.0, abs=1e-6)

    def test_rear_camera_yaw_180(self):
        # Rear camera (yaw=180, mount at -1.9). Camera-frame point 20m forward
        # (in camera coords = behind the ego) should map to ego (−21.9, 0).
        cfg = CameraConfig("test", x_m=-1.9, y_m=0.0, z_m=1.0, yaw_deg=180.0, fov_deg=90)
        ego_fwd, ego_right = camera_frame_to_ego((0.0, 0.0, 20.0), cfg)
        assert ego_fwd == pytest.approx(-21.9, abs=1e-6)
        assert ego_right == pytest.approx(0.0, abs=1e-6)

    def test_right_facing_camera_yaw_90(self):
        # A camera facing pure right (yaw=90, mount at +0.9 to the right).
        # 20m "forward" in camera coords → 20m to the ego's right.
        cfg = CameraConfig("test", x_m=0.0, y_m=0.9, z_m=1.0, yaw_deg=90.0, fov_deg=90)
        ego_fwd, ego_right = camera_frame_to_ego((0.0, 0.0, 20.0), cfg)
        assert ego_fwd == pytest.approx(0.0, abs=1e-6)
        assert ego_right == pytest.approx(20.9, abs=1e-6)

    def test_camera_x_component_offsets_perpendicular(self):
        # Forward camera, point shifted to the right in the camera frame (cam_x = 2),
        # at depth 10. Ego frame: forward 10, right 2.
        cfg = CameraConfig("test", x_m=0.0, y_m=0.0, z_m=1.0, yaw_deg=0.0, fov_deg=90)
        ego_fwd, ego_right = camera_frame_to_ego((2.0, 0.0, 10.0), cfg)
        assert ego_fwd == pytest.approx(10.0, abs=1e-6)
        assert ego_right == pytest.approx(2.0, abs=1e-6)


@pytest.mark.unit
class TestCrossCameraDedup:
    def test_identical_class_within_radius_merged(self):
        p = PerceptionService()
        dets = [
            mk_detection("vehicle", (10.0, 0.0)),
            mk_detection("vehicle", (10.5, 0.2)),  # 0.54m away → merged
        ]
        kept = p._dedup_across_cameras(dets)
        assert len(kept) == 1

    def test_same_class_far_apart_not_merged(self):
        p = PerceptionService()
        dets = [
            mk_detection("vehicle", (10.0, 0.0)),
            mk_detection("vehicle", (10.0, 5.0)),
        ]
        kept = p._dedup_across_cameras(dets)
        assert len(kept) == 2

    def test_different_classes_at_same_spot_not_merged(self):
        p = PerceptionService()
        dets = [
            mk_detection("vehicle", (10.0, 0.0)),
            mk_detection("pedestrian", (10.0, 0.0)),
        ]
        kept = p._dedup_across_cameras(dets)
        assert len(kept) == 2

    def test_dedup_keeps_closer_detection(self):
        p = PerceptionService()
        dets = [
            mk_detection("vehicle", (15.0, 0.0)),  # farther
            mk_detection("vehicle", (15.4, 0.2)),  # closer? distance ~15.4
            # Pre-sort by distance — closer kept, both have similar distance,
            # we just verify deduper keeps exactly one.
        ]
        kept = p._dedup_across_cameras(dets)
        assert len(kept) == 1


@pytest.mark.unit
class TestStableIdTracking:
    def test_new_detection_gets_new_id(self):
        p = PerceptionService()
        dets = [mk_detection("vehicle", (10.0, 0.0))]
        out = p._assign_ids(dets)
        assert out[0].id == "vehicle-0"

    def test_small_movement_preserves_id(self):
        p = PerceptionService()
        # First scan
        p._tracked = p._assign_ids([mk_detection("vehicle", (10.0, 0.0))])
        first_id = p._tracked[0].id
        # Second scan — moved 0.5m (still within gate)
        new = p._assign_ids([mk_detection("vehicle", (10.5, 0.1))])
        assert new[0].id == first_id

    def test_big_jump_issues_new_id(self):
        p = PerceptionService()
        # First scan
        p._tracked = p._assign_ids([mk_detection("vehicle", (10.0, 0.0))])
        first_id = p._tracked[0].id
        # Second scan — moved 10m (way outside gate)
        new = p._assign_ids([mk_detection("vehicle", (20.0, 0.0))])
        assert new[0].id != first_id

    def test_velocity_carries_delta_between_ticks(self):
        p = PerceptionService()
        p._tracked = p._assign_ids([mk_detection("vehicle", (10.0, 0.0))])
        new = p._assign_ids([mk_detection("vehicle", (12.0, 0.5))])
        assert new[0].velocity is not None
        assert new[0].velocity[0] == pytest.approx(2.0, abs=1e-6)
        assert new[0].velocity[1] == pytest.approx(0.5, abs=1e-6)

    def test_id_uniqueness_per_class(self):
        p = PerceptionService()
        out = p._assign_ids([
            mk_detection("vehicle", (10.0, 0.0)),
            mk_detection("pedestrian", (10.0, 0.0)),
        ])
        # Class-namespaced counters: vehicle-0 and pedestrian-0 (not vehicle-1)
        ids = {d.id for d in out}
        assert "vehicle-0" in ids
        assert "pedestrian-0" in ids


@pytest.mark.unit
class TestDetectionToDict:
    def test_basic_serialization(self):
        d = Detection(
            id="vehicle-3",
            class_name="vehicle",
            pos=(12.345, -1.234),
            distance_m=12.4,
            bbox_dim=(4.7, 1.8),
            in_path=True,
            alert_level="warn",
        )
        out = d.to_dict()
        assert out["id"] == "vehicle-3"
        assert out["class"] == "vehicle"
        assert out["pos"] == [12.35, -1.23]
        assert out["distance"] == 12.4
        assert out["bbox_dim"] == [4.7, 1.8]
        assert out["in_path"] is True
        assert out["alert"] == "warn"
        assert "velocity" not in out

    def test_serialization_includes_velocity_when_present(self):
        d = Detection(
            id="vehicle-3",
            class_name="vehicle",
            pos=(10.0, 0.0),
            distance_m=10.0,
            bbox_dim=(4.5, 1.8),
            velocity=(2.5, -0.3),
        )
        out = d.to_dict()
        assert out["velocity"] == [2.5, -0.3]


@pytest.mark.unit
class TestInPath:
    def test_directly_ahead_is_in_path(self):
        assert PerceptionService._is_in_path(15.0, 0.0) is True

    def test_off_to_the_side_is_not_in_path(self):
        assert PerceptionService._is_in_path(15.0, 4.0) is False

    def test_behind_ego_is_not_in_path(self):
        assert PerceptionService._is_in_path(-5.0, 0.0) is False

    def test_too_close_is_not_in_path(self):
        # The corridor starts at 1m forward (excludes the ego itself).
        assert PerceptionService._is_in_path(0.5, 0.0) is False

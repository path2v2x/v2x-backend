"""Tests for the twin camera rig: pose conversion + rig lifecycle."""

import math

import pytest

from digital_twin_bridge import twin_camera_rig
from digital_twin_bridge.twin_camera_rig import (
    TwinCameraRig,
    camera_with_twin_pose,
    configure_twin_camera_blueprint,
    compute_twin_camera_transform,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    is_twin_supported_map,
    load_cameras_config,
    twin_horizontal_fov_deg,
)

from tests.conftest import MockLocation


SITE = {"lat": 37.91560117034595, "lon": -122.33478756387032}

CAMERA = {
    "id": "ch1",
    "height_m": 7.0,
    "pitch_deg": -39.2,
    "yaw_deg": -46.06,
    "heading_deg": 200.0,
    "intrinsics": {"fx": 1325.4, "fy": 1325.4, "cx": 1280.0, "cy": 960.0, "width": 2560, "height": 1920},
}


def make_config(cameras=None):
    return {"site": dict(SITE), "cameras": cameras or [dict(CAMERA)]}


class TestHeadingToCarlaYaw:
    """CARLA x = easting / y = -northing, so bearing H -> yaw H - 90."""

    @pytest.mark.parametrize(
        "heading,expected",
        [(0.0, -90.0), (90.0, 0.0), (180.0, 90.0), (270.0, 180.0)],
    )
    def test_cardinal_bearings(self, heading, expected):
        assert heading_to_carla_yaw(heading) == pytest.approx(expected)

    def test_mounting_yaw_composes_with_heading(self):
        assert heading_to_carla_yaw(200.0, -46.06) == pytest.approx(63.94)

    def test_normalised_to_half_open_range(self):
        yaw = heading_to_carla_yaw(350.0, 50.0)
        assert -180.0 <= yaw <= 180.0
        assert yaw == pytest.approx(-50.0)


class TestIntrinsics:
    def test_horizontal_fov_matches_real_camera(self):
        fov = horizontal_fov_deg(CAMERA["intrinsics"])
        assert fov == pytest.approx(2 * math.degrees(math.atan(1280 / 1325.4)))
        assert 87.0 < fov < 89.0

    def test_twin_fov_applies_explicit_calibration_offset(self):
        camera = {**CAMERA, "twin_pose": {"fov_offset_deg": -1.25}}
        assert twin_horizontal_fov_deg(camera) == pytest.approx(
            horizontal_fov_deg(CAMERA["intrinsics"]) - 1.25
        )

    def test_blueprint_uses_pinhole_lens_unless_measured(self, mock_world):
        blueprint = mock_world.get_blueprint_library().find("sensor.camera.rgb")
        configure_twin_camera_blueprint(blueprint, CAMERA, 1280, 960, 12.0)
        assert str(blueprint.get_attribute("lens_k")) == "0.0"
        assert str(blueprint.get_attribute("lens_kcube")) == "0.0"
        assert float(str(blueprint.get_attribute("sensor_tick"))) == pytest.approx(
            1 / 12, abs=1e-6
        )

        measured = {**CAMERA, "twin_lens": {"lens_k": -0.2, "lens_kcube": 0.03}}
        configure_twin_camera_blueprint(blueprint, measured, 1280, 960)
        assert str(blueprint.get_attribute("lens_k")) == "-0.2"
        assert str(blueprint.get_attribute("lens_kcube")) == "0.03"


class TestMapGate:
    def test_rfs_map_supported(self):
        assert is_twin_supported_map("Carla/Maps/Richmond_Field_Station_Richmond_CA")
        assert is_twin_supported_map("Richmond_Field_Station_Richmond_CA")

    def test_other_maps_rejected(self):
        assert not is_twin_supported_map("San_Ramon_P1_Roads")
        assert not is_twin_supported_map("Carla/Maps/Town10HD")


class TestCamerasConfig:
    def test_repo_config_loads_all_channels(self):
        config = load_cameras_config()
        assert config is not None
        assert [c["id"] for c in config["cameras"]] == ["ch1", "ch2", "ch3", "ch4"]
        assert config["site"]["lat"] == pytest.approx(37.9156, abs=1e-3)

    def test_missing_file_returns_none(self):
        assert load_cameras_config("/nonexistent/cameras.json") is None


class TestComputeTransform:
    def test_candidate_pose_is_isolated_from_source(self):
        source = {**CAMERA, "twin_pose": {"forward_offset_m": 0.5}}
        candidate = camera_with_twin_pose(source, {"yaw_offset_deg": 2.0})
        assert candidate["twin_pose"]["yaw_offset_deg"] == 2.0
        assert "yaw_offset_deg" not in source["twin_pose"]

    def test_pole_height_and_rotation(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5)
        )
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, CAMERA)
        assert transform.location.z == pytest.approx(1.5 + 7.0)
        assert transform.rotation.pitch == pytest.approx(-39.2)
        assert transform.rotation.yaw == pytest.approx(200.0 - 46.06 - 90.0)
        assert transform.rotation.roll == 0.0

    def test_full_pose_offsets_include_right_translation_and_roll(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5)
        )
        camera = {
            **CAMERA,
            "heading_deg": 90.0,
            "yaw_deg": 0.0,
            "twin_pose": {
                "forward_offset_m": 2.0,
                "right_offset_m": 1.0,
                "height_offset_m": 0.5,
                "roll_offset_deg": 3.0,
            },
        }
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, camera)
        assert transform.location.x == pytest.approx(12.0)
        assert transform.location.y == pytest.approx(21.0)
        assert transform.location.z == pytest.approx(9.0)
        assert transform.rotation.roll == pytest.approx(3.0)

    def test_missing_forward_offset_has_no_hidden_translation(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig,
            "gps_to_carla",
            lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5),
        )
        camera = {**CAMERA, "twin_pose": {}}
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, camera)
        assert transform.location.x == pytest.approx(10.0)
        assert transform.location.y == pytest.approx(20.0)


class TestTwinCameraRig:
    def test_spawn_frames_destroy(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(0.0, 0.0, 0.0)
        )
        rig = TwinCameraRig(mock_world, mock_world.get_map(), make_config())
        assert rig.spawn() == 1
        assert rig.camera_ids == ["ch1"]
        assert rig.has_camera("ch1")
        assert not rig.has_camera("ch9")
        assert len(rig.actor_ids()) == 1

        # No frame yet
        assert rig.get_latest_frame("ch1") is None

        # Push a frame through the listener path (bypassing JPEG encoding)
        monkeypatch.setattr(twin_camera_rig, "encode_jpeg", lambda image, quality=70: b"jpeg")
        listener = rig._cameras["ch1"]._listener
        listener(object())
        assert rig.get_latest_frame("ch1") == b"jpeg"
        assert rig.status()["frame_counts"]["ch1"] == 1

        rig.destroy()
        assert rig.camera_ids == []
        assert rig.get_latest_frame("ch1") is None

    def test_frames_ignored_after_destroy(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(0.0, 0.0, 0.0)
        )
        monkeypatch.setattr(twin_camera_rig, "encode_jpeg", lambda image, quality=70: b"jpeg")
        rig = TwinCameraRig(mock_world, mock_world.get_map(), make_config())
        rig.spawn()
        listener = rig._cameras["ch1"]._listener
        rig.destroy()
        listener(object())
        assert rig.get_latest_frame("ch1") is None

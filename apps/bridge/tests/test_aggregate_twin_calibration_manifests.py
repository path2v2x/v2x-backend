import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.aggregate_twin_calibration_manifests import (
    SiteManifestError,
    aggregate_site_manifests,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cameras_hash = "c" * 64
    map_name = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
    opendrive_hash = "d" * 64
    landmarks = []
    for index in range(12):
        landmarks.append({
            "global_landmark_id": f"rfs-landmark-{index:02d}",
            "split": "train" if index < 8 else "holdout",
            "surveyed_world": [float(index), float(index * 2), 0.5],
            "survey_record_sha256": hashlib.sha256(
                f"survey-{index}".encode()
            ).hexdigest(),
        })
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": "v2x-site-landmark-registry/v1",
        "cameras_file_sha256": cameras_hash,
        "landmarks": landmarks,
    }))
    manifests = []
    for camera_id in CAMERAS:
        path = tmp_path / f"{camera_id}.json"
        point_features = [
            {
                "id": f"{camera_id}-feature-{index}",
                "type": "point",
                **copy.deepcopy(landmark),
                "world": copy.deepcopy(landmark["surveyed_world"]),
                "image": [200.0 + index * 20.0, 300.0 + index * 20.0],
                "twin": [100.0 + index * 10.0, 150.0 + index * 10.0],
                "provenance": "manually_verified_unique",
                "category": "static_landmark",
                "description": f"Unique surveyed static landmark {index}",
                "depth_neighborhood": {"center_depth_m": 10.0},
            }
            for index, landmark in enumerate(landmarks)
        ]
        road_features = [
            {
                "id": f"{camera_id}-road-{index}",
                "type": "polyline",
                "split": "train" if index < 3 else "holdout",
                "world": [[0.0, float(index), 0.0], [1.0, float(index), 0.0]],
                "twin_polyline": [
                    [100.0, 500.0 + index * 10.0],
                    [1100.0, 400.0 + index * 10.0],
                ],
                "image_polyline": [
                    [200.0, 1000.0 + index * 20.0],
                    [2200.0, 800.0 + index * 20.0],
                ],
                "provenance": "manually_traced_geometry",
                "category": "road_edge",
                "description": f"Unique manually traced road edge {index}",
                "depth_neighborhoods": [
                    {"center_depth_m": 10.0},
                    {"center_depth_m": 10.0},
                ],
            }
            for index in range(5)
        ]
        path.write_text(json.dumps({
            "schema_version": 1,
            "camera_id": camera_id,
            "width": 2560,
            "height": 1920,
            "source_frame_sha256": "1" * 64,
            "twin_frame_sha256": "2" * 64,
            "annotation_sha256": "3" * 64,
            "cameras_file_sha256": cameras_hash,
            "camera_config_sha256": "4" * 64,
            "ue5_map": map_name,
            "ue5_map_opendrive_sha256": opendrive_hash,
            "projection": {
                "source": "opendrive_georeference",
                "strict": True,
                "map_origin_error_m": 0.1,
                "map_name": map_name,
                "opendrive_sha256": opendrive_hash,
                "georeference_sha256": "e" * 64,
            },
            "depth_frame": {
                "carla_frame": 123,
                "sensor_timestamp": 45.5,
                "width": 1280,
                "height": 960,
                "raw_data_sha256": "5" * 64,
                "raw_data_size": 1280 * 960 * 4,
            },
            "baseline": {
                "location": [0.0, 0.0, 8.0],
                "pitch_deg": -35.0,
                "yaw_deg": 90.0,
                "roll_deg": 0.0,
                "fov_deg": 90.0,
                "cx": 1280.0,
                "cy": 960.0,
                "k1": 0.0,
            },
            "deployment_model": {
                "type": "twin_camera_rig_v1",
                "anchor_location": [0.0, 0.0, 8.0],
                "base": {
                    "pitch_deg": -35.0,
                    "yaw_deg": 90.0,
                    "roll_deg": 0.0,
                    "fov_deg": 90.0,
                },
                "lens": {
                    "lens_k": -1.0,
                    "lens_kcube": 0.0,
                    "lens_circle_falloff": 5.0,
                    "lens_circle_multiplier": 0.0,
                    "lens_x_size": 0.08,
                    "lens_y_size": 0.08,
                },
            },
            "intrinsics_calibration": {
                "method": "charuco",
                "artifact_sha256": "6" * 64,
                "image_count": 10,
                "source_images_sha256": [
                    hashlib.sha256(f"source-{index}".encode()).hexdigest()
                    for index in range(10)
                ],
                "rms_reprojection_error_px": 0.5,
                "resolution": [2560, 1920],
                "camera_matrix": [
                    [1325.0, 0.0, 1280.0],
                    [0.0, 1325.0, 960.0],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": {
                    "k1": 0.0,
                    "k2": 0.0,
                    "p1": 0.0,
                    "p2": 0.0,
                    "k3": 0.0,
                },
            },
            "features": point_features + road_features,
        }))
        manifests.append(path)
    return registry, manifests


def test_four_camera_registry_allows_canonical_cross_camera_reuse(tmp_path):
    registry, manifests = fixture(tmp_path)

    report = aggregate_site_manifests(registry, list(reversed(manifests)))

    assert report["gate_passed"] is True
    assert report["acceptance_eligible"] is False
    assert set(report["manifests"]) == set(CAMERAS)
    assert all(
        landmark["cameras"] == list(CAMERAS)
        for landmark in report["landmarks"].values()
    )
    assert report["site_landmark_registry"]["sha256"] == hashlib.sha256(
        registry.read_bytes()
    ).hexdigest()
    for camera_id, manifest in report["manifests"].items():
        path = tmp_path / f"{camera_id}.json"
        assert manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cross_camera_reuse_cannot_change_the_global_split(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[1].read_text())
    value["features"][0]["split"] = "holdout"
    value["features"][8]["split"] = "train"
    manifests[1].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="canonical split"):
        aggregate_site_manifests(registry, manifests)


def test_renamed_near_duplicate_landmark_is_rejected_site_wide(tmp_path):
    registry, manifests = fixture(tmp_path)
    registry_value = json.loads(registry.read_text())
    renamed = copy.deepcopy(registry_value["landmarks"][0])
    renamed["global_landmark_id"] = "caller-renamed-landmark"
    renamed["surveyed_world"][0] += 0.01
    renamed["survey_record_sha256"] = "d" * 64
    registry_value["landmarks"].append(renamed)
    registry.write_text(json.dumps(registry_value))
    manifest_value = json.loads(manifests[0].read_text())
    manifest_value["features"][0].update(copy.deepcopy(renamed))
    manifests[0].write_text(json.dumps(manifest_value))

    with pytest.raises(SiteManifestError, match="renamed near-duplicate"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize(
    ("separation", "accepted"),
    [(0.249, False), (0.25, True)],
)
def test_renamed_landmark_separation_boundary_is_fail_closed(
    tmp_path, separation, accepted
):
    registry, manifests = fixture(tmp_path)
    registry_value = json.loads(registry.read_text())
    registry_value["landmarks"][1]["surveyed_world"] = [
        separation,
        0.0,
        0.5,
    ]
    registry.write_text(json.dumps(registry_value))
    for path in manifests:
        value = json.loads(path.read_text())
        value["features"][1]["surveyed_world"] = [separation, 0.0, 0.5]
        value["features"][1]["world"] = [separation, 0.0, 0.5]
        path.write_text(json.dumps(value))

    if accepted:
        assert aggregate_site_manifests(registry, manifests)["gate_passed"]
    else:
        with pytest.raises(SiteManifestError, match="renamed near-duplicate"):
            aggregate_site_manifests(registry, manifests)


def test_survey_world_or_record_hash_disagreement_is_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[2].read_text())
    value["features"][3]["surveyed_world"][0] += 1.0
    value["features"][3]["survey_record_sha256"] = "e" * 64
    manifests[2].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="surveyed world identity"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize("malformed", [None, [], "not-an-object"])
def test_malformed_manifest_annotation_entries_fail_controlled(tmp_path, malformed):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value["features"].append(malformed)
    manifests[0].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="feature is malformed"):
        aggregate_site_manifests(registry, manifests)


def test_requires_exactly_one_manifest_per_camera(tmp_path):
    registry, manifests = fixture(tmp_path)

    with pytest.raises(SiteManifestError, match="exactly four"):
        aggregate_site_manifests(registry, manifests[:3])


def test_all_cameras_must_share_one_map_opendrive_fingerprint(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[3].read_text())
    value["ue5_map_opendrive_sha256"] = "f" * 64
    value["projection"]["opendrive_sha256"] = "f" * 64
    manifests[3].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="one map/OpenDRIVE"):
        aggregate_site_manifests(registry, manifests)


def test_same_global_landmark_requires_cross_camera_resolved_world_identity(
    tmp_path,
):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[2].read_text())
    value["features"][4]["world"][0] += 0.251
    manifests[2].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="resolved world disagrees"):
        aggregate_site_manifests(registry, manifests)


def test_incomplete_builder_contract_or_fallback_projection_is_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value.pop("depth_frame")
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="depth identity"):
        aggregate_site_manifests(registry, manifests)

    registry, manifests = fixture(tmp_path / "fallback")
    value = json.loads(manifests[0].read_text())
    value["projection"].update(
        source="origin_centered_fallback", strict=False
    )
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="strict OpenDRIVE projection"):
        aggregate_site_manifests(registry, manifests)


def test_builder_counts_and_measured_intrinsics_must_be_complete(tmp_path):
    registry, manifests = fixture(tmp_path / "counts")
    value = json.loads(manifests[0].read_text())
    value["features"] = [
        feature for feature in value["features"]
        if feature["id"] != "ch1-road-4"
    ]
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="feature counts are incomplete"):
        aggregate_site_manifests(registry, manifests)

    registry, manifests = fixture(tmp_path / "intrinsics")
    value = json.loads(manifests[0].read_text())
    value["intrinsics_calibration"]["source_images_sha256"] = ["a" * 64] * 10
    manifests[0].write_text(json.dumps(value))
    with pytest.raises(SiteManifestError, match="measured-intrinsics contract"):
        aggregate_site_manifests(registry, manifests)

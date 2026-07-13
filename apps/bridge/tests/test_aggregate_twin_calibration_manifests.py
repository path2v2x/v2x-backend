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


def rewrite_registry_from_manifests(registry, manifests):
    original = json.loads(registry.read_text())
    landmarks = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text())
        for feature in manifest["features"]:
            if feature.get("type") != "point":
                continue
            landmarks.setdefault(feature["global_landmark_id"], {
                "global_landmark_id": feature["global_landmark_id"],
                "split": feature["split"],
                "surveyed_world": feature["surveyed_world"],
                "survey_record_sha256": feature["survey_record_sha256"],
                "survey_record_path": feature["survey_record_path"],
                "survey_record_size_bytes": feature["survey_record"]["size_bytes"],
            })
    original["landmarks"] = list(landmarks.values())
    registry.write_text(json.dumps(original))


def fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    def artifact(name, payload):
        path = (tmp_path / name).resolve()
        path.write_bytes(payload)
        return {
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }

    cameras_artifact = artifact("cameras.json", b"frozen-cameras")
    cameras_hash = cameras_artifact["sha256"]
    map_name = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
    opendrive_hash = "d" * 64
    landmarks = []
    for index in range(12):
        survey_record = artifact(
            f"survey-{index}.json", f"survey-{index}".encode()
        )
        landmarks.append({
            "global_landmark_id": f"rfs-landmark-{index:02d}",
            "split": "train" if index < 8 else "holdout",
            "surveyed_world": [float(index), float(index * 2), 0.5],
            "survey_record_sha256": survey_record["sha256"],
            "survey_record_path": survey_record["path"],
            "survey_record_size_bytes": survey_record["size_bytes"],
        })
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": "v2x-site-landmark-registry/v1",
        "cameras_file_sha256": cameras_hash,
        "landmarks": landmarks,
    }))
    intrinsics_artifact = artifact("intrinsics.json", b"frozen-intrinsics")
    intrinsics_sources = [
        artifact(f"intrinsics-source-{index}.png", f"source-{index}".encode())
        for index in range(10)
    ]
    depth_artifact = artifact("depth.bgra", b"\0" * (1280 * 960 * 4))
    manifests = []
    for camera_id in CAMERAS:
        path = tmp_path / f"{camera_id}.json"
        point_features = [
            {
                "id": f"{camera_id}-feature-{index}",
                "type": "point",
                **copy.deepcopy(landmark),
                "survey_record": {
                    "path": landmark["survey_record_path"],
                    "sha256": landmark["survey_record_sha256"],
                    "size_bytes": landmark["survey_record_size_bytes"],
                },
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
        annotation_artifact = artifact(
            f"{camera_id}-annotations.json", f"annotations-{camera_id}".encode()
        )
        real_artifact = artifact(
            f"{camera_id}-real.jpg", f"real-{camera_id}".encode()
        )
        twin_artifact = artifact(
            f"{camera_id}-twin.jpg", f"twin-{camera_id}".encode()
        )
        path.write_text(json.dumps({
            "schema_version": 1,
            "camera_id": camera_id,
            "width": 2560,
            "height": 1920,
            "source_frame_sha256": real_artifact["sha256"],
            "twin_frame_sha256": twin_artifact["sha256"],
            "annotation_sha256": annotation_artifact["sha256"],
            "cameras_file_sha256": cameras_hash,
            "camera_config_sha256": "4" * 64,
            "source_artifacts": {
                "annotations": annotation_artifact,
                "real_frame": real_artifact,
                "twin_frame": twin_artifact,
                "cameras_file": cameras_artifact,
                "intrinsics_artifact": intrinsics_artifact,
                "intrinsics_source_images": intrinsics_sources,
            },
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
                "raw_data_sha256": depth_artifact["sha256"],
                "raw_data_size": 1280 * 960 * 4,
                "path": depth_artifact["path"],
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
                "artifact_sha256": intrinsics_artifact["sha256"],
                "image_count": 10,
                "source_images_sha256": [
                    item["sha256"] for item in intrinsics_sources
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
    assert report["contract"]["shared_landmark_count"] == 12
    assert report["contract"]["connected_camera_count"] == 4
    assert report["contract"]["cross_camera_edge_count"] == 6
    assert report["contract"]["shared_landmarks_per_camera"] == {
        camera: 12 for camera in CAMERAS
    }
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


def test_zero_shared_landmarks_are_rejected_even_with_four_complete_cameras(tmp_path):
    registry, manifests = fixture(tmp_path)
    for camera_index, path in enumerate(manifests):
        value = json.loads(path.read_text())
        for feature in value["features"]:
            if feature.get("type") != "point":
                continue
            feature["global_landmark_id"] = (
                f"{value['camera_id']}-{feature['global_landmark_id']}"
            )
            feature["surveyed_world"][0] += 100.0 * camera_index
            feature["world"][0] += 100.0 * camera_index
        path.write_text(json.dumps(value))
    rewrite_registry_from_manifests(registry, manifests)

    with pytest.raises(SiteManifestError, match="every camera must participate"):
        aggregate_site_manifests(registry, manifests)


def test_two_disconnected_shared_camera_islands_are_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    for path in manifests[2:]:
        value = json.loads(path.read_text())
        for feature in value["features"]:
            if feature.get("type") != "point":
                continue
            feature["global_landmark_id"] = "east-" + feature["global_landmark_id"]
            feature["surveyed_world"][0] += 200.0
            feature["world"][0] += 200.0
        path.write_text(json.dumps(value))
    rewrite_registry_from_manifests(registry, manifests)

    with pytest.raises(SiteManifestError, match="disconnected camera islands"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize(
    "artifact_kind",
    ["annotations", "real_frame", "twin_frame", "intrinsics", "depth", "survey"],
)
def test_missing_or_tampered_retained_artifact_cannot_pass_by_hash_string(
    tmp_path, artifact_kind
):
    registry, manifests = fixture(tmp_path)
    manifest = json.loads(manifests[0].read_text())
    if artifact_kind in {"annotations", "real_frame", "twin_frame"}:
        path = Path(manifest["source_artifacts"][artifact_kind]["path"])
    elif artifact_kind == "intrinsics":
        path = Path(
            manifest["source_artifacts"]["intrinsics_source_images"][0]["path"]
        )
    elif artifact_kind == "depth":
        path = Path(manifest["depth_frame"]["path"])
    else:
        path = Path(manifest["features"][0]["survey_record_path"])
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(SiteManifestError, match="artifact"):
        aggregate_site_manifests(registry, manifests)


def test_nonexistent_artifact_path_is_rejected_not_accepted_from_declared_hash(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value["source_artifacts"]["annotations"]["path"] = str(
        (tmp_path / "does-not-exist.json").resolve()
    )
    manifests[0].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="artifact is missing"):
        aggregate_site_manifests(registry, manifests)

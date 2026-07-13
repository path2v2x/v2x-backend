"""Raw retained four-camera calibration evidence for strict-contract tests."""

import copy
import hashlib
import json
from pathlib import Path

from digital_twin_bridge.reviewed_localization import (
    canonical_object_sha256,
    seal_authenticated_artifact,
)
from tests.test_aggregate_twin_calibration_manifests import (
    fixture as aggregate_fixture,
)
from tools.aggregate_twin_calibration_manifests import aggregate_site_manifests


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_reviewed_static_calibration(
    root,
    cameras_path,
    cameras,
    map_name,
    opendrive_sha256,
    authority_key_id,
    authority_key,
):
    """Create signed evidence whose acceptance is recomputed from raw inputs."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    registry_path, manifest_paths = aggregate_fixture(root / "retained")
    cameras_raw = Path(cameras_path).read_bytes()
    cameras_sha256 = hashlib.sha256(cameras_raw).hexdigest()
    retained_cameras_path = root / "retained" / "cameras.json"
    retained_cameras_path.write_bytes(cameras_raw)
    cameras_identity = {
        "path": str(retained_cameras_path.resolve()),
        "sha256": cameras_sha256,
        "size_bytes": len(cameras_raw),
    }
    registry = json.loads(registry_path.read_text())
    registry["cameras_file_sha256"] = cameras_sha256
    camera_index = {camera["id"]: camera for camera in cameras}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        camera = camera_index[manifest["camera_id"]]
        width = int(camera["intrinsics"]["width"])
        height = int(camera["intrinsics"]["height"])
        prior_width = float(manifest["width"])
        prior_height = float(manifest["height"])
        scale_x = width / prior_width
        scale_y = height / prior_height
        for feature in manifest["features"]:
            if feature["type"] == "point":
                feature["image"] = [
                    feature["image"][0] * scale_x,
                    feature["image"][1] * scale_y,
                ]
            else:
                feature["image_polyline"] = [
                    [pixel[0] * scale_x, pixel[1] * scale_y]
                    for pixel in feature["image_polyline"]
                ]
        manifest["width"] = width
        manifest["height"] = height
        manifest["cameras_file_sha256"] = cameras_sha256
        manifest["camera_config_sha256"] = canonical_object_sha256(camera)
        manifest["source_artifacts"]["cameras_file"] = cameras_identity
        manifest["ue5_map"] = map_name
        manifest["ue5_map_opendrive_sha256"] = opendrive_sha256
        manifest["projection"]["map_name"] = map_name
        manifest["projection"]["opendrive_sha256"] = opendrive_sha256
        manifest["baseline"]["location"] = [0.0, 0.0, 0.0]
        manifest["baseline"]["pitch_deg"] = 0.0
        manifest["baseline"]["yaw_deg"] = 0.0
        manifest["baseline"]["roll_deg"] = 0.0
        manifest["baseline"]["cx"] = camera["intrinsics"]["cx"]
        manifest["baseline"]["cy"] = camera["intrinsics"]["cy"]
        manifest["deployment_model"]["anchor_location"] = [0.0, 0.0, 0.0]
        manifest["deployment_model"]["base"]["pitch_deg"] = 0.0
        manifest["deployment_model"]["base"]["yaw_deg"] = 0.0
        manifest["deployment_model"]["base"]["roll_deg"] = 0.0
        manifest["intrinsics_calibration"]["resolution"] = [width, height]
        manifest["intrinsics_calibration"]["camera_matrix"] = copy.deepcopy(
            camera["intrinsics_calibration"]["camera_matrix"]
        )
        manifest["intrinsics_calibration"]["distortion"] = copy.deepcopy(
            camera["intrinsics_calibration"]["distortion"]
        )
        fx = float(camera["intrinsics"]["fx"])
        fy = float(camera["intrinsics"]["fy"])
        cx = float(camera["intrinsics"]["cx"])
        cy = float(camera["intrinsics"]["cy"])

        def world_for_pixel(pixel, depth=20.0):
            return [
                depth,
                (float(pixel[0]) - cx) * depth / fx,
                -(float(pixel[1]) - cy) * depth / fy,
            ]

        for feature in manifest["features"]:
            if feature["type"] == "point":
                projected_world = world_for_pixel(feature["image"])
                feature["world"] = projected_world
                feature["surveyed_world"] = projected_world
            else:
                feature["world"] = [
                    world_for_pixel(pixel)
                    for pixel in feature["image_polyline"]
                ]
        _write_json(manifest_path, manifest)
    landmark_world = {}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        for feature in manifest["features"]:
            if feature["type"] == "point":
                landmark_world.setdefault(
                    feature["global_landmark_id"], feature["surveyed_world"]
                )
    for landmark in registry["landmarks"]:
        landmark["surveyed_world"] = landmark_world[
            landmark["global_landmark_id"]
        ]
    _write_json(registry_path, registry)
    aggregate = aggregate_site_manifests(registry_path, manifest_paths)
    aggregate_path = root / "site-aggregation.json"
    aggregate_sha256 = _write_json(aggregate_path, aggregate)
    reprojection = {}
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        camera_id = manifest["camera_id"]
        points = []
        roads = []
        for feature in manifest["features"]:
            if feature.get("split") != "holdout":
                continue
            if feature["type"] == "point":
                points.append({
                    "feature_id": feature["id"],
                    "observed_pixel": feature["image"],
                })
            else:
                roads.append({
                    "feature_id": feature["id"],
                    "observed_polyline": feature["image_polyline"],
                })
        evidence = seal_authenticated_artifact({
            "schema": "v2x-camera-heldout-reprojection/v1",
            "camera_id": camera_id,
            "camera_config_sha256": manifest["camera_config_sha256"],
            "camera_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "native_resolution": [manifest["width"], manifest["height"]],
            "points": points,
            "roads": roads,
        }, authority_key_id, authority_key)
        evidence_path = root / f"{camera_id}-heldout-reprojection.json"
        evidence_sha256 = _write_json(evidence_path, evidence)
        reprojection[camera_id] = {
            "path": str(evidence_path),
            "sha256": evidence_sha256,
            "schema": "v2x-camera-heldout-reprojection/v1",
        }
    static = seal_authenticated_artifact({
        "schema": "v2x-static-camera-survey-manifest/v1",
        "source_cameras_json_sha256": cameras_sha256,
        "map": {"name": map_name, "opendrive_sha256": opendrive_sha256},
        "site_aggregation": {
            "path": str(aggregate_path),
            "sha256": aggregate_sha256,
            "schema": "v2x-site-calibration-aggregation/v1",
        },
        "heldout_reprojection": reprojection,
    }, authority_key_id, authority_key)
    static_path = root / "static-calibration.json"
    _write_json(static_path, static)
    return static_path

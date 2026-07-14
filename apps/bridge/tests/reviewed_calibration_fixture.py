"""Raw retained four-camera calibration evidence for strict-contract tests."""

import copy
import hashlib
import json
import math
from pathlib import Path

from digital_twin_bridge.reviewed_localization import (
    canonical_object_sha256,
    seal_authenticated_artifact,
)
from digital_twin_bridge.twin_camera_rig import (
    absolute_twin_model,
    heading_to_carla_yaw,
    horizontal_fov_deg,
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
        anchor = [0.0, 0.0, 0.0]
        base = {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(camera["intrinsics"]),
        }
        baseline = absolute_twin_model(
            anchor, base, camera.get("twin_pose") or {}
        )
        manifest["baseline"]["location"] = baseline["location"]
        manifest["baseline"]["pitch_deg"] = baseline["pitch_deg"]
        manifest["baseline"]["yaw_deg"] = baseline["yaw_deg"]
        manifest["baseline"]["roll_deg"] = baseline["roll_deg"]
        manifest["baseline"]["fov_deg"] = baseline["fov_deg"]
        manifest["baseline"]["cx"] = camera["intrinsics"]["cx"]
        manifest["baseline"]["cy"] = camera["intrinsics"]["cy"]
        manifest["deployment_model"]["anchor_location"] = anchor
        manifest["deployment_model"]["base"].update(base)
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

        pitch = math.radians(baseline["pitch_deg"])
        yaw = math.radians(baseline["yaw_deg"])
        roll = math.radians(baseline["roll_deg"])
        cp, sp = math.cos(pitch), math.sin(pitch)
        cyaw, syaw = math.cos(yaw), math.sin(yaw)
        cr, sr = math.cos(roll), math.sin(roll)
        forward_axis = (cp * cyaw, cp * syaw, sp)
        zero_roll_right = (-syaw, cyaw, 0.0)
        zero_roll_up = (-sp * cyaw, -sp * syaw, cp)
        right_axis = tuple(
            cr * zero_roll_right[index] - sr * zero_roll_up[index]
            for index in range(3)
        )
        up_axis = tuple(
            sr * zero_roll_right[index] + cr * zero_roll_up[index]
            for index in range(3)
        )

        def world_for_pixel(pixel, depth=20.0):
            normalized_x = (float(pixel[0]) - cx) / fx
            normalized_y = (float(pixel[1]) - cy) / fy
            return [
                baseline["location"][index]
                + depth * forward_axis[index]
                + depth * normalized_x * right_axis[index]
                - depth * normalized_y * up_axis[index]
                for index in range(3)
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

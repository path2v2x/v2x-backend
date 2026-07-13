#!/usr/bin/env python3
"""Bind four camera manifests to one surveyed site-landmark registry."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

try:
    from tools.build_twin_calibration_manifest import (
        validate_strict_projection_provenance,
    )
except ModuleNotFoundError:
    from build_twin_calibration_manifest import (
        validate_strict_projection_provenance,
    )


CAMERAS = frozenset({"ch1", "ch2", "ch3", "ch4"})
SPLITS = frozenset({"train", "holdout"})
MIN_DISTINCT_LANDMARK_SEPARATION_M = 0.25
WORLD_IDENTITY_TOLERANCE_M = 1e-6
RESOLVED_WORLD_IDENTITY_TOLERANCE_M = 0.25
DISTORTION_KEYS = ("k1", "k2", "p1", "p2", "k3")
LENS_KEYS = (
    "lens_k",
    "lens_kcube",
    "lens_circle_falloff",
    "lens_circle_multiplier",
    "lens_x_size",
    "lens_y_size",
)


class SiteManifestError(RuntimeError):
    pass


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _world(value, label):
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(float(component))
            for component in value
        )
    ):
        raise SiteManifestError(f"{label} world coordinate is invalid")
    return tuple(float(component) for component in value)


def _finite_vector(value, length):
    return (
        isinstance(value, list)
        and len(value) == length
        and all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(float(component))
            for component in value
        )
    )


def _pixel(value, width, height):
    return (
        _finite_vector(value, 2)
        and 0.0 <= float(value[0]) < float(width)
        and 0.0 <= float(value[1]) < float(height)
    )


def _builder_contract(manifest):
    """Validate the complete immutable builder envelope used by optimization."""
    required_hashes = (
        "source_frame_sha256",
        "twin_frame_sha256",
        "annotation_sha256",
        "cameras_file_sha256",
        "camera_config_sha256",
    )
    if any(not _valid_sha256(manifest.get(field)) for field in required_hashes):
        raise SiteManifestError("camera manifest lacks builder source fingerprints")
    map_name = manifest.get("ue5_map")
    opendrive_sha256 = manifest.get("ue5_map_opendrive_sha256")
    if (
        not isinstance(map_name, str)
        or not map_name.lower().endswith("richmond_field_station_richmond_ca")
        or not _valid_sha256(opendrive_sha256)
    ):
        raise SiteManifestError("camera manifest map identity is invalid")
    try:
        projection = validate_strict_projection_provenance(
            manifest.get("projection"),
            map_name=map_name,
            opendrive_sha256=opendrive_sha256,
        )
    except ValueError as exc:
        raise SiteManifestError(str(exc)) from exc
    width, height = manifest.get("width"), manifest.get("height")
    baseline = manifest.get("baseline")
    deployment = manifest.get("deployment_model")
    calibration = manifest.get("intrinsics_calibration")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(baseline, dict)
        or not _finite_vector(baseline.get("location"), 3)
        or any(
            isinstance(baseline.get(field), bool)
            or not isinstance(baseline.get(field), (int, float))
            or not math.isfinite(float(baseline[field]))
            for field in (
                "pitch_deg", "yaw_deg", "roll_deg", "fov_deg", "cx", "cy", "k1"
            )
        )
        or not isinstance(deployment, dict)
        or deployment.get("type") != "twin_camera_rig_v1"
        or not _finite_vector(deployment.get("anchor_location"), 3)
        or not isinstance(deployment.get("base"), dict)
        or any(
            isinstance(deployment["base"].get(field), bool)
            or not isinstance(deployment["base"].get(field), (int, float))
            or not math.isfinite(float(deployment["base"][field]))
            for field in ("pitch_deg", "yaw_deg", "roll_deg", "fov_deg")
        )
        or not isinstance(deployment.get("lens"), dict)
        or set(deployment["lens"]) != set(LENS_KEYS)
        or any(
            isinstance(deployment["lens"].get(field), bool)
            or not isinstance(deployment["lens"].get(field), (int, float))
            or not math.isfinite(float(deployment["lens"][field]))
            for field in LENS_KEYS
        )
        or not isinstance(calibration, dict)
    ):
        raise SiteManifestError("camera manifest builder model contract is invalid")
    matrix = calibration.get("camera_matrix")
    distortion = calibration.get("distortion")
    source_hashes = calibration.get("source_images_sha256")
    image_count = calibration.get("image_count")
    rms = calibration.get("rms_reprojection_error_px")
    try:
        matrix_values = [float(value) for row in matrix for value in row]
    except (TypeError, ValueError):
        matrix_values = []
    if (
        calibration.get("method") not in {"checkerboard", "charuco"}
        or not _valid_sha256(calibration.get("artifact_sha256"))
        or isinstance(image_count, bool)
        or not isinstance(image_count, int)
        or image_count < 10
        or not isinstance(source_hashes, list)
        or len(source_hashes) != image_count
        or len(set(source_hashes)) != len(source_hashes)
        or any(not _valid_sha256(value) for value in source_hashes)
        or isinstance(rms, bool)
        or not isinstance(rms, (int, float))
        or not math.isfinite(float(rms))
        or not 0.0 <= float(rms) <= 2.0
        or calibration.get("resolution") != [width, height]
        or not isinstance(matrix, list)
        or len(matrix) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in matrix)
        or len(matrix_values) != 9
        or not all(math.isfinite(value) for value in matrix_values)
        or not isinstance(distortion, dict)
        or any(
            isinstance(distortion.get(key), bool)
            or not isinstance(distortion.get(key), (int, float))
            or not math.isfinite(float(distortion[key]))
            for key in DISTORTION_KEYS
        )
    ):
        raise SiteManifestError(
            "camera manifest measured-intrinsics contract is invalid"
        )
    depth = manifest.get("depth_frame")
    if (
        not isinstance(depth, dict)
        or not _valid_sha256(depth.get("raw_data_sha256"))
        or isinstance(depth.get("carla_frame"), bool)
        or not isinstance(depth.get("carla_frame"), int)
        or depth.get("carla_frame", 0) <= 0
        or isinstance(depth.get("sensor_timestamp"), bool)
        or not isinstance(depth.get("sensor_timestamp"), (int, float))
        or not math.isfinite(float(depth["sensor_timestamp"]))
        or float(depth["sensor_timestamp"]) < 0.0
        or isinstance(depth.get("width"), bool)
        or not isinstance(depth.get("width"), int)
        or depth.get("width", 0) <= 0
        or isinstance(depth.get("height"), bool)
        or not isinstance(depth.get("height"), int)
        or depth.get("height", 0) <= 0
        or isinstance(depth.get("raw_data_size"), bool)
        or not isinstance(depth.get("raw_data_size"), int)
        or depth.get("raw_data_size")
        != depth.get("width") * depth.get("height") * 4
    ):
        raise SiteManifestError("camera manifest depth identity is invalid")
    features = manifest.get("features")
    if not isinstance(features, list):
        raise SiteManifestError("camera manifest feature contract is invalid")
    counts = {
        "train_points": 0,
        "holdout_points": 0,
        "train_polylines": 0,
        "holdout_polylines": 0,
    }
    seen_ids = set()
    for feature in features:
        if not isinstance(feature, dict):
            raise SiteManifestError("camera manifest feature is malformed")
        feature_id = feature.get("id")
        split = feature.get("split")
        feature_type = feature.get("type")
        if (
            not isinstance(feature_id, str)
            or not feature_id
            or feature_id.strip() != feature_id
            or feature_id in seen_ids
            or split not in SPLITS
            or feature_type not in {"point", "polyline"}
        ):
            raise SiteManifestError("camera manifest feature contract is invalid")
        seen_ids.add(feature_id)
        if feature_type == "point":
            counts[f"{split}_points"] += 1
            _world(feature.get("world"), f"{feature_id}:resolved")
            if (
                feature.get("provenance") != "manually_verified_unique"
                or not _pixel(feature.get("image"), width, height)
                or not _pixel(
                    feature.get("twin"), depth["width"], depth["height"]
                )
                or not isinstance(feature.get("depth_neighborhood"), dict)
            ):
                raise SiteManifestError("camera point lacks depth evidence")
        else:
            counts[f"{split}_polylines"] += 1
            worlds = feature.get("world")
            if not isinstance(worlds, list) or len(worlds) < 2:
                raise SiteManifestError("camera polyline world geometry is invalid")
            for index, world in enumerate(worlds):
                _world(world, f"{feature_id}:resolved:{index}")
            neighborhoods = feature.get("depth_neighborhoods")
            twin_polyline = feature.get("twin_polyline")
            image_polyline = feature.get("image_polyline")
            if (
                feature.get("provenance") != "manually_traced_geometry"
                or not isinstance(twin_polyline, list)
                or len(twin_polyline) != len(worlds)
                or any(
                    not _pixel(pixel, depth["width"], depth["height"])
                    for pixel in twin_polyline
                )
                or not isinstance(image_polyline, list)
                or len(image_polyline) < 2
                or any(not _pixel(pixel, width, height) for pixel in image_polyline)
                or not isinstance(neighborhoods, list)
                or len(neighborhoods) != len(worlds)
                or any(not isinstance(item, dict) for item in neighborhoods)
            ):
                raise SiteManifestError("camera polyline lacks complete depth evidence")
    if (
        counts["train_points"] < 8
        or counts["holdout_points"] < 4
        or counts["train_polylines"] < 3
        or counts["holdout_polylines"] < 2
    ):
        raise SiteManifestError("camera manifest builder feature counts are incomplete")
    return {
        "ue5_map": map_name,
        "ue5_map_opendrive_sha256": opendrive_sha256,
        "georeference_sha256": projection["georeference_sha256"],
        "counts": counts,
    }


def _load(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SiteManifestError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise SiteManifestError(f"{label} must be a JSON object")
    return path, raw, value


def aggregate_site_manifests(registry_path, manifest_paths):
    """Validate and hash-bind exactly one manifest for every site camera."""
    registry_file, registry_raw, registry = _load(
        registry_path, "site landmark registry"
    )
    cameras_file_sha256 = registry.get("cameras_file_sha256")
    entries = registry.get("landmarks")
    if (
        registry.get("schema") != "v2x-site-landmark-registry/v1"
        or not _valid_sha256(cameras_file_sha256)
        or not isinstance(entries, list)
        or not entries
    ):
        raise SiteManifestError("site landmark registry contract is invalid")

    landmark_index = {}
    for entry in entries:
        landmark_id = (
            entry.get("global_landmark_id") if isinstance(entry, dict) else None
        )
        split = entry.get("split") if isinstance(entry, dict) else None
        survey_record_sha256 = (
            entry.get("survey_record_sha256")
            if isinstance(entry, dict)
            else None
        )
        if (
            not isinstance(landmark_id, str)
            or not landmark_id
            or landmark_id.strip() != landmark_id
            or landmark_id in landmark_index
            or split not in SPLITS
            or not _valid_sha256(survey_record_sha256)
        ):
            raise SiteManifestError("site landmark registry entry is malformed")
        landmark_index[landmark_id] = {
            "split": split,
            "surveyed_world": _world(entry.get("surveyed_world"), landmark_id),
            "survey_record_sha256": survey_record_sha256,
        }
    ordered_landmarks = sorted(landmark_index.items())
    for index, (left_id, left) in enumerate(ordered_landmarks):
        for right_id, right in ordered_landmarks[index + 1 :]:
            distance = math.dist(
                left["surveyed_world"], right["surveyed_world"]
            )
            if distance < MIN_DISTINCT_LANDMARK_SEPARATION_M:
                raise SiteManifestError(
                    "distinct landmark IDs are a renamed near-duplicate: "
                    f"{left_id} / {right_id} ({distance:.6f} m)"
                )

    manifest_paths = list(manifest_paths)
    if len(manifest_paths) != len(CAMERAS):
        raise SiteManifestError("aggregation requires exactly four manifests")
    manifests = {}
    occurrences = {landmark_id: [] for landmark_id in landmark_index}
    resolved_worlds = {}
    map_identity = None
    for manifest_path in manifest_paths:
        path, raw, manifest = _load(manifest_path, "camera manifest")
        camera_id = manifest.get("camera_id")
        features = manifest.get("features")
        if (
            manifest.get("schema_version") != 1
            or camera_id not in CAMERAS
            or camera_id in manifests
            or manifest.get("cameras_file_sha256") != cameras_file_sha256
            or not isinstance(features, list)
        ):
            raise SiteManifestError("camera manifest contract is invalid")
        builder = _builder_contract(manifest)
        candidate_map_identity = {
            key: builder[key]
            for key in (
                "ue5_map",
                "ue5_map_opendrive_sha256",
                "georeference_sha256",
            )
        }
        if map_identity is None:
            map_identity = candidate_map_identity
        elif candidate_map_identity != map_identity:
            raise SiteManifestError(
                "camera manifests do not share one map/OpenDRIVE fingerprint"
            )
        seen_camera_landmarks = set()
        for feature in features:
            if not isinstance(feature, dict):
                raise SiteManifestError("camera manifest feature is malformed")
            if feature.get("type") != "point":
                continue
            landmark_id = feature.get("global_landmark_id")
            split = feature.get("split")
            survey_record_sha256 = feature.get("survey_record_sha256")
            if (
                not isinstance(landmark_id, str)
                or not landmark_id
                or landmark_id.strip() != landmark_id
                or landmark_id in seen_camera_landmarks
                or landmark_id not in landmark_index
                or split not in SPLITS
                or not _valid_sha256(survey_record_sha256)
            ):
                raise SiteManifestError("camera point landmark identity is malformed")
            seen_camera_landmarks.add(landmark_id)
            canonical = landmark_index[landmark_id]
            surveyed_world = _world(
                feature.get("surveyed_world"),
                f"{camera_id}:{landmark_id}",
            )
            if (
                split != canonical["split"]
                or survey_record_sha256 != canonical["survey_record_sha256"]
                or math.dist(surveyed_world, canonical["surveyed_world"])
                > WORLD_IDENTITY_TOLERANCE_M
            ):
                raise SiteManifestError(
                    f"{camera_id}:{landmark_id} disagrees with canonical "
                    "split or surveyed world identity"
                )
            resolved_world = _world(
                feature.get("world"),
                f"{camera_id}:{landmark_id}:resolved",
            )
            prior_resolved_world = resolved_worlds.get(landmark_id)
            if (
                prior_resolved_world is not None
                and math.dist(resolved_world, prior_resolved_world)
                > RESOLVED_WORLD_IDENTITY_TOLERANCE_M
            ):
                raise SiteManifestError(
                    f"{camera_id}:{landmark_id} resolved world disagrees "
                    "across cameras"
                )
            resolved_worlds.setdefault(landmark_id, resolved_world)
            occurrences[landmark_id].append(camera_id)
        manifests[camera_id] = {
            "path": str(path),
            "sha256": _sha256(raw),
            "point_landmarks": len(seen_camera_landmarks),
            "builder_contract_complete": True,
            "counts": builder["counts"],
            "ue5_map": builder["ue5_map"],
            "ue5_map_opendrive_sha256": builder[
                "ue5_map_opendrive_sha256"
            ],
            "projection_georeference_sha256": builder[
                "georeference_sha256"
            ],
        }
    if set(manifests) != CAMERAS:
        raise SiteManifestError("aggregation does not contain all four cameras")
    unused = sorted(
        landmark_id for landmark_id, cameras in occurrences.items() if not cameras
    )
    if unused:
        raise SiteManifestError("registry contains landmarks absent from all manifests")

    return {
        "schema": "v2x-site-calibration-aggregation/v1",
        "gate_passed": True,
        "acceptance_eligible": False,
        "site_landmark_registry": {
            "path": str(registry_file),
            "sha256": _sha256(registry_raw),
            "cameras_file_sha256": cameras_file_sha256,
        },
        "map_identity": map_identity,
        "manifests": dict(sorted(manifests.items())),
        "landmarks": {
            landmark_id: {
                "split": landmark_index[landmark_id]["split"],
                "surveyed_world": list(
                    landmark_index[landmark_id]["surveyed_world"]
                ),
                "survey_record_sha256": landmark_index[landmark_id][
                    "survey_record_sha256"
                ],
                "resolved_world": list(resolved_worlds[landmark_id]),
                "cameras": sorted(cameras),
            }
            for landmark_id, cameras in sorted(occurrences.items())
        },
        "contract": {
            "four_camera_complete": True,
            "global_landmark_split_frozen": True,
            "surveyed_world_identity_consistent": True,
            "resolved_world_identity_tolerance_m": (
                RESOLVED_WORLD_IDENTITY_TOLERANCE_M
            ),
            "one_map_opendrive_fingerprint": True,
            "complete_builder_contracts": True,
            "renamed_near_duplicates_rejected_below_m": (
                MIN_DISTINCT_LANDMARK_SEPARATION_M
            ),
            "deployment_authorized": False,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output_path = Path(args.output).expanduser().resolve()
    if output_path.exists():
        print("aggregation failed: output already exists", file=sys.stderr)
        return 1
    try:
        report = aggregate_site_manifests(args.registry, args.manifest)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, SiteManifestError) as exc:
        print(f"aggregation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

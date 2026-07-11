#!/usr/bin/env python3
"""Bounded multi-start calibration from independent road/landmark geometry.

This optimizer is intentionally independent of CARLA runtime APIs.  A manifest
contains globally anchored CARLA XYZ points/lines, real-image observations, a
frozen train/holdout split, and the baseline camera transform captured by the
UE5-side builder.  It solves pose plus pinhole/radial intrinsics, then reports
held-out metrics without modifying cameras.json.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares


# At the 1280x960 acceptance render, errors above roughly one percent of the
# image width can put a vehicle in the wrong lane or beyond a road edge.  The
# former 75/125/175 point limits were diagnostic framing checks, not precision
# calibration gates.
POINT_RMSE_MAX_PX = 10.0
POINT_P95_MAX_PX = 16.0
POINT_MAX_ERROR_PX = 24.0
LINE_RMSE_MAX_PX = 6.0
LINE_MAX_ERROR_PX = 12.0
DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX = 0.25
DEPLOYMENT_TRANSFORM_ROUNDTRIP_MAX = 1e-6
DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION = 0.01
DEPLOYMENT_JACOBIAN_CONDITION_MAX = 100_000.0

PARAMETER_NAMES = (
    "location_x",
    "location_y",
    "location_z",
    "pitch_deg",
    "yaw_deg",
    "roll_deg",
    "fov_deg",
    "cx",
    "cy",
    "k1",
)
DISTORTION_KEYS = ("k1", "k2", "p1", "p2", "k3")


def carla_rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def project_world_points(world_points, location, params, width, height):
    """Project CARLA XYZ with [pitch,yaw,roll,fov,cx,cy,k1]."""
    pitch, yaw, roll, fov, cx, cy, k1 = [float(value) for value in params]
    rotation = carla_rotation_matrix(pitch, yaw, roll)
    local = (rotation.T @ (np.asarray(world_points) - np.asarray(location)).T).T
    depth = local[:, 0]
    focal = (float(width) / 2.0) / math.tan(math.radians(fov) / 2.0)
    x = local[:, 1] / depth
    y = -local[:, 2] / depth
    radial = 1.0 + k1 * (x * x + y * y)
    pixels = np.column_stack((cx + focal * x * radial, cy + focal * y * radial))
    return pixels, depth


def project_calibration_points(world_points, params, width, height):
    """Project with the full fitted 6-DoF pose plus optical parameters."""
    return project_world_points(world_points, params[:3], params[3:], width, height)


def normalize_degrees(value):
    """Normalize an angular delta without losing a legitimate 180 degree value."""
    normalized = (float(value) + 180.0) % 360.0 - 180.0
    return 180.0 if normalized == -180.0 and value > 0 else normalized


def deployment_candidate(manifest, params):
    """Translate an absolute fit into the exact tracked twin_pose representation."""
    model = manifest["deployment_model"]
    anchor = np.asarray(model["anchor_location"], dtype=float)
    base = model["base"]
    location = np.asarray(params[:3], dtype=float)
    pitch, yaw, roll, fov = (float(value) for value in params[3:7])
    yaw_radians = math.radians(yaw)
    delta_x, delta_y = location[:2] - anchor[:2]
    return {
        "forward_offset_m": float(
            delta_x * math.cos(yaw_radians) + delta_y * math.sin(yaw_radians)
        ),
        "right_offset_m": float(
            -delta_x * math.sin(yaw_radians) + delta_y * math.cos(yaw_radians)
        ),
        "height_offset_m": float(location[2] - anchor[2]),
        "pitch_offset_deg": float(pitch - float(base["pitch_deg"])),
        "yaw_offset_deg": normalize_degrees(yaw - float(base["yaw_deg"])),
        "roll_offset_deg": float(roll - float(base["roll_deg"])),
        "fov_offset_deg": float(fov - float(base["fov_deg"])),
    }


def deployment_roundtrip(manifest, params):
    """Prove that the fit survives conversion to the production rig contract.

    UE5's tracked rig can represent a full translated/rotated pinhole camera,
    but it cannot move the principal point.  CARLA lens_k is also not the same
    calibrated radial model used by this optimizer.  We therefore quantify the
    optical mismatch over the whole image and fail closed above a sub-pixel
    bound instead of silently copying k1 into an unrelated blueprint field.
    """
    model = manifest["deployment_model"]
    candidate = deployment_candidate(manifest, params)
    anchor = np.asarray(model["anchor_location"], dtype=float)
    base = model["base"]
    yaw = float(base["yaw_deg"]) + candidate["yaw_offset_deg"]
    yaw_radians = math.radians(yaw)
    location = np.array([
        anchor[0]
        + candidate["forward_offset_m"] * math.cos(yaw_radians)
        - candidate["right_offset_m"] * math.sin(yaw_radians),
        anchor[1]
        + candidate["forward_offset_m"] * math.sin(yaw_radians)
        + candidate["right_offset_m"] * math.cos(yaw_radians),
        anchor[2] + candidate["height_offset_m"],
    ])
    roundtrip = np.array([
        *location,
        float(base["pitch_deg"]) + candidate["pitch_offset_deg"],
        yaw,
        float(base["roll_deg"]) + candidate["roll_offset_deg"],
        float(base["fov_deg"]) + candidate["fov_offset_deg"],
    ])
    expected = np.asarray(params[:7], dtype=float)
    transform_errors = np.abs(roundtrip - expected)
    transform_errors[4] = abs(normalize_degrees(roundtrip[4] - expected[4]))

    width, height = float(manifest["width"]), float(manifest["height"])
    fov, cx, cy, k1 = (float(value) for value in params[6:10])
    focal = (width / 2.0) / math.tan(math.radians(fov) / 2.0)
    half_x = math.tan(math.radians(fov) / 2.0)
    half_y = half_x * height / width
    optical_errors = []
    for x in np.linspace(-half_x, half_x, 5):
        for y in np.linspace(-half_y, half_y, 5):
            radial = 1.0 + k1 * (x * x + y * y)
            fitted = np.array([cx + focal * x * radial, cy + focal * y * radial])
            deployed = np.array([width / 2.0 + focal * x, height / 2.0 + focal * y])
            optical_errors.append(float(np.linalg.norm(fitted - deployed)))
    optical_max = max(optical_errors)
    calibration = manifest["intrinsics_calibration"]
    matrix = calibration["camera_matrix"]
    distortion = calibration["distortion"]
    fx, fy = float(matrix[0][0]), float(matrix[1][1])
    measured_cx, measured_cy = float(matrix[0][2]), float(matrix[1][2])
    k1_m, k2_m, p1_m, p2_m, k3_m = (
        float(distortion[key]) for key in DISTORTION_KEYS
    )
    measured_errors = []
    for x in np.linspace(-half_x, half_x, 9):
        for y in np.linspace(-half_y, half_y, 9):
            radius2 = x * x + y * y
            radial = 1.0 + k1_m * radius2 + k2_m * radius2**2 + k3_m * radius2**3
            distorted_x = x * radial + 2.0 * p1_m * x * y + p2_m * (radius2 + 2.0 * x * x)
            distorted_y = y * radial + p1_m * (radius2 + 2.0 * y * y) + 2.0 * p2_m * x * y
            measured = np.array([
                measured_cx + fx * distorted_x,
                measured_cy + fy * distorted_y,
            ])
            deployed = np.array([width / 2.0 + focal * x, height / 2.0 + focal * y])
            measured_errors.append(float(np.linalg.norm(measured - deployed)))
    measured_optical_max = max(measured_errors)
    reasons = []
    if float(np.max(transform_errors)) > DEPLOYMENT_TRANSFORM_ROUNDTRIP_MAX:
        reasons.append("deployment_transform_roundtrip")
    if optical_max > DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX:
        reasons.append("unrepresentable_principal_point_or_radial_distortion")
    if measured_optical_max > DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX:
        reasons.append("measured_physical_optics_not_representable_in_ue5")
    lens = model.get("lens") or {}
    if any(abs(float(lens.get(key, 0.0))) > 1e-12 for key in (
        "lens_k", "lens_kcube", "lens_circle_multiplier"
    )):
        reasons.append("unsupported_existing_ue5_lens_model")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "candidate_twin_pose": candidate,
        "transform_roundtrip_max": float(np.max(transform_errors)),
        "optical_roundtrip_max_px": optical_max,
        "optical_roundtrip_limit_px": DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX,
        "measured_optical_roundtrip_max_px": measured_optical_max,
        "unsupported_fitted_optics": {
            "principal_point_offset_px": [cx - width / 2.0, cy - height / 2.0],
            "radial_k1": k1,
        },
    }


def deployment_identifiability(manifest, params, parameter_scales):
    """Measure whether frozen training geometry constrains all 6-DoF+FOV axes."""
    params = np.asarray(params, dtype=float)
    scales = np.asarray(parameter_scales[:7], dtype=float)
    steps = np.array([0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001])
    columns = []
    for index, step in enumerate(steps):
        above, below = params.copy(), params.copy()
        above[index] += step
        below[index] -= step
        derivative = (
            feature_residuals(manifest, above, "train")
            - feature_residuals(manifest, below, "train")
        ) / (2.0 * step)
        columns.append(derivative * scales[index])
    jacobian = np.column_stack(columns)
    singular_values = np.linalg.svd(jacobian, compute_uv=False)
    if not len(singular_values) or singular_values[0] <= 0.0:
        rank, condition = 0, None
    else:
        tolerance = singular_values[0] * 1e-8
        rank = int(np.sum(singular_values > tolerance))
        condition = (
            None if singular_values[-1] <= tolerance
            else float(singular_values[0] / singular_values[-1])
        )
    return {
        "passed": (
            rank == 7
            and condition is not None
            and condition <= DEPLOYMENT_JACOBIAN_CONDITION_MAX
        ),
        "rank": rank,
        "required_rank": 7,
        "condition": condition,
        "condition_limit": DEPLOYMENT_JACOBIAN_CONDITION_MAX,
        "scaled_singular_values": [float(value) for value in singular_values],
    }


def normalized_line(image_line):
    x1, y1, x2, y2 = [float(value) for value in image_line]
    line = np.cross([x1, y1, 1.0], [x2, y2, 1.0])
    norm = math.hypot(line[0], line[1])
    if norm < 1e-6:
        raise ValueError("image line endpoints coincide")
    return line / norm


def point_to_polyline_distances(points, polyline):
    """Shortest Euclidean distance from each point to polyline segments."""
    points = np.asarray(points, dtype=float)
    polyline = np.asarray(polyline, dtype=float)
    if len(polyline) < 2:
        raise ValueError("polyline requires at least two vertices")
    starts, vectors = polyline[:-1], polyline[1:] - polyline[:-1]
    lengths2 = np.sum(vectors * vectors, axis=1)
    if np.any(lengths2 < 1e-9):
        raise ValueError("polyline contains duplicate adjacent vertices")
    delta = points[:, None, :] - starts[None, :, :]
    along = np.clip(
        np.sum(delta * vectors[None, :, :], axis=2) / lengths2[None, :],
        0.0, 1.0,
    )
    closest = starts[None, :, :] + along[:, :, None] * vectors[None, :, :]
    return np.sqrt(np.min(np.sum((points[:, None, :] - closest) ** 2, axis=2), axis=1))


def point_metrics(errors):
    values = np.asarray(errors, dtype=float)
    if not len(values):
        return None
    return {
        "count": int(len(values)),
        "rmse_px": float(np.sqrt(np.mean(values * values))),
        "p95_px": float(np.percentile(values, 95)),
        "max_px": float(np.max(values)),
    }


def manifest_gate(manifest):
    reasons = []
    if manifest.get("schema_version") != 1:
        reasons.append("invalid_manifest_schema")
    if manifest.get("camera_id") not in {"ch1", "ch2", "ch3", "ch4"}:
        reasons.append("invalid_camera_id")
    source_hash = str(manifest.get("source_frame_sha256") or "")
    if not (len(source_hash) == 64 and all(ch in "0123456789abcdef" for ch in source_hash)):
        reasons.append("missing_source_frame_hash")
    for field in (
        "twin_frame_sha256",
        "annotation_sha256",
        "cameras_file_sha256",
        "camera_config_sha256",
    ):
        value = str(manifest.get(field) or "")
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            reasons.append(f"missing_{field}")
    if not str(manifest.get("ue5_map") or "").lower().endswith(
        "richmond_field_station_richmond_ca"
    ):
        reasons.append("invalid_ue5_map")
    deployment = manifest.get("deployment_model")
    if not isinstance(deployment, dict) or deployment.get("type") != "twin_camera_rig_v1":
        reasons.append("missing_deployment_model")
    else:
        anchor = deployment.get("anchor_location")
        base = deployment.get("base")
        numeric_base = ("pitch_deg", "yaw_deg", "roll_deg", "fov_deg")
        if (
            not isinstance(anchor, list)
            or len(anchor) != 3
            or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in anchor)
            or not isinstance(base, dict)
            or not all(
                isinstance(base.get(key), (int, float))
                and math.isfinite(float(base[key]))
                for key in numeric_base
            )
        ):
            reasons.append("invalid_deployment_model")
    calibration = manifest.get("intrinsics_calibration")
    if not isinstance(calibration, dict):
        reasons.append("missing_measured_intrinsics_calibration")
    else:
        matrix = calibration.get("camera_matrix")
        distortion = calibration.get("distortion")
        artifact_hash = str(calibration.get("artifact_sha256") or "")
        image_count = calibration.get("image_count")
        source_hashes = calibration.get("source_images_sha256")
        rms = calibration.get("rms_reprojection_error_px")
        try:
            optical_values = [
                float(matrix[0][0]), float(matrix[0][2]),
                float(matrix[1][1]), float(matrix[1][2]),
                *(float(distortion[key]) for key in DISTORTION_KEYS),
            ]
        except (KeyError, TypeError, ValueError, IndexError):
            optical_values = []
        if (
            calibration.get("method") not in {"checkerboard", "charuco"}
            or len(artifact_hash) != 64
            or any(char not in "0123456789abcdef" for char in artifact_hash)
            or calibration.get("resolution") != [manifest.get("width"), manifest.get("height")]
            or isinstance(image_count, bool)
            or not isinstance(image_count, int)
            or image_count < 10
            or not isinstance(source_hashes, list)
            or len(source_hashes) != image_count
            or len(set(source_hashes)) != len(source_hashes)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in source_hashes
            )
            or isinstance(rms, bool)
            or not isinstance(rms, (int, float))
            or not math.isfinite(float(rms))
            or not 0.0 <= float(rms) <= 2.0
            or len(optical_values) != 9
            or not all(math.isfinite(value) for value in optical_values)
        ):
            reasons.append("invalid_measured_intrinsics_calibration")
    depth = manifest.get("depth_frame")
    if not isinstance(depth, dict):
        reasons.append("missing_depth_frame_identity")
    else:
        carla_frame = depth.get("carla_frame")
        timestamp = depth.get("sensor_timestamp")
        depth_width, depth_height = depth.get("width"), depth.get("height")
        if (
            isinstance(carla_frame, bool)
            or not isinstance(carla_frame, int)
            or carla_frame <= 0
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0.0
            or depth_width != 1280
            or depth_height != 960
        ):
            reasons.append("invalid_depth_frame_identity")
    features = manifest.get("features") or []
    ids = [feature.get("id") for feature in features]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        reasons.append("invalid_or_duplicate_feature_id")
    train_points = [f for f in features if f.get("type") == "point" and f.get("split") == "train"]
    held_points = [f for f in features if f.get("type") == "point" and f.get("split") == "holdout"]
    train_lines = [f for f in features if f.get("type") in {"line", "polyline"} and f.get("split") == "train"]
    held_lines = [f for f in features if f.get("type") in {"line", "polyline"} and f.get("split") == "holdout"]
    if len(train_points) < 8:
        reasons.append("insufficient_train_points")
    if len(held_points) < 4:
        reasons.append("insufficient_heldout_points")
    if len(train_lines) < 3:
        reasons.append("insufficient_train_lines")
    if len(held_lines) < 2:
        reasons.append("insufficient_heldout_lines")
    if any(
        feature.get("provenance") != "manually_verified_unique"
        for feature in train_points + held_points
    ):
        reasons.append("unverified_unique_landmark_provenance")
    if any(
        feature.get("provenance") != "manually_traced_geometry"
        for feature in train_lines + held_lines
    ):
        reasons.append("unverified_road_geometry_provenance")

    width, height = float(manifest.get("width", 0)), float(manifest.get("height", 0))
    for label, points in (("train", train_points), ("heldout", held_points)):
        pixels = [feature.get("image") for feature in points]
        if pixels and width > 0 and height > 0:
            horizontal = (max(p[0] for p in pixels) - min(p[0] for p in pixels)) / width
            vertical = (max(p[1] for p in pixels) - min(p[1] for p in pixels)) / height
        else:
            horizontal = vertical = 0.0
        if horizontal < 0.5 or vertical < 0.3:
            reasons.append(f"{label}_image_coverage")

    angles = []
    for feature in train_lines:
        if feature.get("type") == "polyline":
            x1, y1 = feature.get("image_polyline", [[0, 0]])[0]
            x2, y2 = feature.get("image_polyline", [[0, 0]])[-1]
        else:
            x1, y1, x2, y2 = feature.get("image_line", [0, 0, 0, 0])
        angles.append(math.atan2(y2 - y1, x2 - x1) % math.pi)
    diverse = any(
        min(abs(a - b), math.pi - abs(a - b)) >= math.radians(20)
        for index, a in enumerate(angles) for b in angles[index + 1:]
    )
    if not diverse:
        reasons.append("insufficient_line_direction_diversity")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "train_points": len(train_points),
        "heldout_points": len(held_points),
        "train_lines": len(train_lines),
        "heldout_lines": len(held_lines),
    }


def feature_residuals(manifest, params, split):
    width, height = manifest["width"], manifest["height"]
    residuals = []
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        if feature["type"] == "point":
            pixels, depth = project_calibration_points(
                [feature["world"]], params, width, height
            )
            if depth[0] <= 0.1:
                residuals.extend([5000.0, 5000.0])
            else:
                residuals.extend(pixels[0] - np.asarray(feature["image"], dtype=float))
        elif feature["type"] == "line":
            pixels, depth = project_calibration_points(
                feature["world"], params, width, height
            )
            line = normalized_line(feature["image_line"])
            for pixel, point_depth in zip(pixels, depth):
                residuals.append(
                    5000.0 if point_depth <= 0.1 else float(line @ [pixel[0], pixel[1], 1.0])
                )
        elif feature["type"] == "polyline":
            pixels, depth = project_calibration_points(
                feature["world"], params, width, height
            )
            target = np.asarray(feature["image_polyline"], dtype=float)
            forward = point_to_polyline_distances(pixels, target)
            backward = point_to_polyline_distances(target, pixels)
            residuals.extend(
                5000.0 if d <= 0.1 else float(error)
                for error, d in zip(forward, depth)
            )
            residuals.extend(float(error) for error in backward)
        else:
            raise ValueError(f"unsupported feature type {feature['type']!r}")
    return np.asarray(residuals, dtype=float)


def evaluate_split(manifest, params, split):
    point_errors, line_errors = [], []
    width, height = manifest["width"], manifest["height"]
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        pixels, depth = project_calibration_points(
            [feature["world"]] if feature["type"] == "point" else feature["world"],
            params, width, height,
        )
        if feature["type"] == "point":
            point_errors.append(
                5000.0 if depth[0] <= 0.1
                else float(np.linalg.norm(pixels[0] - np.asarray(feature["image"])))
            )
        elif feature["type"] == "line":
            line = normalized_line(feature["image_line"])
            line_errors.extend(
                5000.0 if d <= 0.1 else abs(float(line @ [p[0], p[1], 1.0]))
                for p, d in zip(pixels, depth)
            )
        elif feature["type"] == "polyline":
            target = np.asarray(feature["image_polyline"], dtype=float)
            line_errors.extend(point_to_polyline_distances(pixels, target))
            line_errors.extend(point_to_polyline_distances(target, pixels))
        else:
            raise ValueError(f"unsupported feature type {feature['type']!r}")
    return {"points": point_metrics(point_errors), "lines": point_metrics(line_errors)}


def optimize_manifest(manifest, allow_incomplete=False):
    gate = manifest_gate(manifest)
    if not gate["passed"] and not allow_incomplete:
        return {"passed": False, "dataset_gate": gate, "reason": "dataset_gate"}
    baseline = manifest["baseline"]
    x0 = np.array([
        *baseline["location"],
        baseline["pitch_deg"], baseline["yaw_deg"], baseline.get("roll_deg", 0.0),
        baseline["fov_deg"], baseline.get("cx", manifest["width"] / 2.0),
        baseline.get("cy", manifest["height"] / 2.0), baseline.get("k1", 0.0),
    ], dtype=float)
    lower = x0 + np.array([-3, -3, -3, -15, -15, -8, -20, -80, -80, -0.30])
    upper = x0 + np.array([3, 3, 3, 15, 15, 8, 20, 80, 80, 0.30])
    scales = np.array([1, 1, 1, 5, 5, 3, 8, 30, 30, 0.10])

    def objective(params):
        evidence = feature_residuals(manifest, params, "train")
        priors = 0.25 * (params - x0) / scales
        return np.concatenate((evidence, priors))

    def multistart(seed0, seed_lower, seed_upper, seed_objective):
        candidates = []
        for pitch_delta in (-6.0, 0.0, 6.0):
            for yaw_delta in (-6.0, 0.0, 6.0):
                for fov_delta in (-8.0, 0.0, 8.0):
                    seed = seed0.copy()
                    seed[[3, 4, 6]] += [pitch_delta, yaw_delta, fov_delta]
                    candidates.append(least_squares(
                        seed_objective,
                        seed,
                        bounds=(seed_lower, seed_upper),
                        loss="soft_l1",
                        f_scale=50.0,
                        max_nfev=10_000,
                    ))
        return min(
            candidates,
            key=lambda result: float(np.sum(seed_objective(result.x) ** 2)),
        )

    # First retain the fully unconstrained solution as measured diagnostic
    # evidence.  Pose/principal-point/distortion are partly degenerate, so it
    # must never be treated as production-deployable merely because it has a
    # small residual.
    unconstrained = multistart(x0, lower, upper, objective)

    # Then solve the exact optical model the tracked UE5 rig and replay
    # verifier share: centered principal point and zero radial distortion.
    # A camera whose measured optics materially require cx/cy/k1 will fail its
    # held-out evidence here instead of being silently approximated.
    def deployable_params(core):
        return np.concatenate((core, [manifest["width"] / 2.0, manifest["height"] / 2.0, 0.0]))

    def deployable_objective(core):
        params = deployable_params(core)
        evidence = feature_residuals(manifest, params, "train")
        # Bounds carry the physical safety constraint.  This light prior only
        # breaks exact degeneracies; it must not bias a perfect geometric fit
        # several pixels away from its observations.
        priors = 0.25 * (core - x0[:7]) / scales[:7]
        return np.concatenate((evidence, priors))

    deployable = multistart(x0[:7], lower[:7], upper[:7], deployable_objective)
    best_params = deployable_params(deployable.x)
    train = evaluate_split(manifest, best_params, "train")
    heldout = evaluate_split(manifest, best_params, "holdout")
    unconstrained_train = evaluate_split(manifest, unconstrained.x, "train")
    unconstrained_heldout = evaluate_split(manifest, unconstrained.x, "holdout")
    reasons = [] if gate["passed"] else ["dataset_gate"]
    point = heldout["points"]
    line = heldout["lines"]
    if not point or point["rmse_px"] > POINT_RMSE_MAX_PX:
        reasons.append("heldout_point_rmse")
    if not point or point["p95_px"] > POINT_P95_MAX_PX:
        reasons.append("heldout_point_p95")
    if not point or point["max_px"] > POINT_MAX_ERROR_PX:
        reasons.append("heldout_point_max")
    if not line or line["rmse_px"] > LINE_RMSE_MAX_PX:
        reasons.append("heldout_line_rmse")
    if not line or line["max_px"] > LINE_MAX_ERROR_PX:
        reasons.append("heldout_line_max")
    deployability = deployment_roundtrip(manifest, best_params) if gate["passed"] else {
        "passed": False,
        "reasons": ["dataset_gate"],
    }
    reasons.extend(deployability["reasons"])
    identifiability = deployment_identifiability(manifest, best_params, scales)
    if not identifiability["passed"]:
        reasons.append("underconstrained_deployable_fit")
    bound_margin_fraction = {
        name: float(min(value - low, high - value) / (high - low))
        for name, value, low, high in zip(
            PARAMETER_NAMES[:7], best_params[:7], lower[:7], upper[:7]
        )
    }
    if min(bound_margin_fraction.values()) < DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION:
        reasons.append("deployable_parameter_at_bound")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "dataset_gate": gate,
        "parameters": dict(zip(PARAMETER_NAMES, (float(value) for value in best_params))),
        "deployability": deployability,
        "identifiability": identifiability,
        "deployable_bound_margin_fraction": bound_margin_fraction,
        "deployable_bound_margin_min_fraction": DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION,
        "unconstrained_diagnostic": {
            "parameters": dict(zip(
                PARAMETER_NAMES, (float(value) for value in unconstrained.x)
            )),
            "train": unconstrained_train,
            "heldout": unconstrained_heldout,
            "deployability": deployment_roundtrip(manifest, unconstrained.x),
        },
        "train": train,
        "heldout": heldout,
        "bound_margin": {
            name: float(min(value - low, high - value))
            for name, value, low, high in zip(PARAMETER_NAMES, best_params, lower, upper)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--diagnostic-incomplete",
        action="store_true",
        help="produce a non-acceptable candidate while preserving dataset failure",
    )
    args = parser.parse_args()
    manifest_bytes = Path(args.manifest).read_bytes()
    manifest = json.loads(manifest_bytes)
    report = optimize_manifest(manifest, allow_incomplete=args.diagnostic_incomplete)
    report["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

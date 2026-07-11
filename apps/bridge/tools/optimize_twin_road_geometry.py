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


POINT_RMSE_MAX_PX = 75.0
POINT_P95_MAX_PX = 125.0
POINT_MAX_ERROR_PX = 175.0
LINE_RMSE_MAX_PX = 25.0
LINE_MAX_ERROR_PX = 50.0


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
    location = manifest["baseline"]["location"]
    residuals = []
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        if feature["type"] == "point":
            pixels, depth = project_world_points(
                [feature["world"]], location, params, width, height
            )
            if depth[0] <= 0.1:
                residuals.extend([5000.0, 5000.0])
            else:
                residuals.extend(pixels[0] - np.asarray(feature["image"], dtype=float))
        elif feature["type"] == "line":
            pixels, depth = project_world_points(
                feature["world"], location, params, width, height
            )
            line = normalized_line(feature["image_line"])
            for pixel, point_depth in zip(pixels, depth):
                residuals.append(
                    5000.0 if point_depth <= 0.1 else float(line @ [pixel[0], pixel[1], 1.0])
                )
        elif feature["type"] == "polyline":
            pixels, depth = project_world_points(
                feature["world"], location, params, width, height
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
    location = manifest["baseline"]["location"]
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        pixels, depth = project_world_points(
            [feature["world"]] if feature["type"] == "point" else feature["world"],
            location, params, width, height,
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
        baseline["pitch_deg"], baseline["yaw_deg"], baseline.get("roll_deg", 0.0),
        baseline["fov_deg"], baseline.get("cx", manifest["width"] / 2.0),
        baseline.get("cy", manifest["height"] / 2.0), baseline.get("k1", 0.0),
    ], dtype=float)
    lower = x0 + np.array([-15, -15, -8, -20, -80, -80, -0.30])
    upper = x0 + np.array([15, 15, 8, 20, 80, 80, 0.30])
    scales = np.array([5, 5, 3, 8, 30, 30, 0.10])

    def objective(params):
        evidence = feature_residuals(manifest, params, "train")
        priors = 5.0 * (params - x0) / scales
        return np.concatenate((evidence, priors))

    candidates = []
    for pitch_delta in (-6.0, 0.0, 6.0):
        for yaw_delta in (-6.0, 0.0, 6.0):
            for fov_delta in (-8.0, 0.0, 8.0):
                seed = x0.copy()
                seed[[0, 1, 3]] += [pitch_delta, yaw_delta, fov_delta]
                result = least_squares(
                    objective, seed, bounds=(lower, upper), loss="soft_l1",
                    f_scale=50.0, max_nfev=10_000,
                )
                candidates.append(result)
    best = min(candidates, key=lambda result: float(np.sum(objective(result.x) ** 2)))
    train = evaluate_split(manifest, best.x, "train")
    heldout = evaluate_split(manifest, best.x, "holdout")
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
    names = ("pitch_deg", "yaw_deg", "roll_deg", "fov_deg", "cx", "cy", "k1")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "dataset_gate": gate,
        "parameters": dict(zip(names, (float(value) for value in best.x))),
        "train": train,
        "heldout": heldout,
        "bound_margin": {
            name: float(min(value - low, high - value))
            for name, value, low, high in zip(names, best.x, lower, upper)
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

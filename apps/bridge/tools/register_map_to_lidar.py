#!/usr/bin/env python3
"""Register immutable map polylines to authoritative LiDAR development control.

The executable is deliberately offline.  It fits one site-wide SE(2) transform
and one additive Z bias from manually identified, hash-bound finite polylines.
Fit and holdout identities are disjoint, every direction is scored, and old
USGS QL2 data remains development-only even when its numerical fit is good.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from scipy.optimize import least_squares


ANNOTATION_SCHEMA = "v2x-map-lidar-registration-annotations/v1"
REPORT_SCHEMA = "v2x-map-lidar-registration-report/v1"
GEOMETRY_SCHEMA = "v2x-map-calibration-geometry/v1"
SURVEY_SCHEMA = "v2x-current-horizontal-survey/v1"

HORIZONTAL_RMSE_MAX_M = 0.25
HORIZONTAL_MAX_M = 0.50
HAUSDORFF_MAX_M = 0.50
VERTICAL_RMSE_MAX_M = 0.10
VERTICAL_P95_MAX_M = 0.20
VERTICAL_MAX_M = 0.30
FOLD_TRANSLATION_SPREAD_MAX_M = 0.10
FOLD_YAW_SPREAD_MAX_DEG = 0.10
JACOBIAN_CONDITION_MAX = 1e8
FEATURE_REGRESSION_TOLERANCE_M = 0.01
MAX_HORIZONTAL_QUANTIZATION_M = 0.05
MAX_VERTICAL_QUANTIZATION_M = 0.05
RAW_POINT_REPRODUCTION_TOLERANCE_M = 1e-6
EVALUATION_SPACING_M = 0.10
MIN_APPROACHES = 4
MIN_FIT_FEATURES = 4
MIN_HOLDOUT_FEATURES = 4
TRANSLATION_BOUND_RADIUS_M = 25.0
YAW_BOUND_RADIUS_DEG = 15.0
Z_BIAS_BOUND_RADIUS_M = 10.0
BOUND_PROXIMITY_FRACTION = 1e-5
NEAR_OPTIMAL_COST_FRACTION = 0.05
CURRENT_SURVEY_MAX_AGE_DAYS = 90.0
MANUAL_PROVENANCE = "manually_verified_map_lidar_polyline"
FEATURE_KINDS = {"road_edge", "lane_marking", "crosswalk_edge", "stable_landmark"}


class RegistrationError(ValueError):
    """An immutable input or strict registration precondition failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = fraction * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def write_json_exclusive(path: Path | str, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finite_xyz(value, label: str, minimum_points: int = 2) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.ndim != 2 or array.shape[1] != 3 or len(array) < minimum_points:
        raise RegistrationError(f"{label} must contain at least {minimum_points} XYZ rows")
    if not np.isfinite(array).all():
        raise RegistrationError(f"{label} contains non-finite coordinates")
    if np.any(np.linalg.norm(np.diff(array[:, :2], axis=0), axis=1) <= 1e-9):
        raise RegistrationError(f"{label} contains a zero-length segment")
    return array


def crs_components(crs) -> tuple[int | None, int | None]:
    if crs is None:
        return None, None
    horizontal, vertical = None, None
    components = list(getattr(crs, "sub_crs_list", ()) or ())
    if not components:
        components = [crs]
    for component in components:
        epsg = component.to_epsg()
        if getattr(component, "is_vertical", False):
            vertical = epsg
        elif getattr(component, "is_projected", False):
            horizontal = epsg
    return horizontal, vertical


def _semantic_crs_equal(left, right) -> bool:
    try:
        from pyproj import CRS

        return CRS.from_user_input(left).equals(CRS.from_user_input(right))
    except Exception as exc:
        raise RegistrationError(f"unable to parse declared LiDAR CRS: {exc}") from exc


def load_lidar_tile(lidar_path: Path, validation_path: Path) -> dict:
    try:
        import laspy
    except ImportError as exc:
        raise RegistrationError(
            "laspy with a LAZ backend is required to decode the complete raw cloud"
        ) from exc

    if lidar_path.suffix.lower() not in {".las", ".laz"}:
        raise RegistrationError("raw LiDAR inputs must be LAS or LAZ files")
    validation = json.loads(validation_path.read_text())
    lidar_hash, validation_hash = sha256(lidar_path), sha256(validation_path)
    cloud = laspy.read(lidar_path)
    points = np.column_stack((cloud.x, cloud.y, cloud.z)).astype(float, copy=False)
    if len(points) == 0 or not np.isfinite(points).all():
        raise RegistrationError(f"{lidar_path}: decoded cloud is empty or non-finite")
    header = cloud.header
    crs = header.parse_crs()
    if crs is None:
        raise RegistrationError(f"{lidar_path}: LAS/LAZ contains no parseable CRS")
    horizontal_epsg, vertical_epsg = crs_components(crs)
    if horizontal_epsg is None or not getattr(crs, "is_projected", False) and not any(
        getattr(part, "is_projected", False) for part in getattr(crs, "sub_crs_list", ())
    ):
        raise RegistrationError(f"{lidar_path}: horizontal CRS is not projected")
    scales = np.asarray(header.scales, dtype=float)
    if (
        len(scales) != 3
        or not np.isfinite(scales).all()
        or np.any(scales <= 0)
        or max(scales[:2]) > MAX_HORIZONTAL_QUANTIZATION_M
        or scales[2] > MAX_VERTICAL_QUANTIZATION_M
    ):
        raise RegistrationError(
            f"{lidar_path}: coordinate quantization is too coarse for fixed gates"
        )

    expected_hash = validation.get("sha256") or validation.get("lidar_sha256")
    if expected_hash is not None and expected_hash != lidar_hash:
        raise RegistrationError(f"{lidar_path}: validation raw hash mismatch")
    checks = {
        "bytes": (validation.get("bytes"), lidar_path.stat().st_size),
        "points": (validation.get("points"), len(points)),
    }
    for name, (declared, actual) in checks.items():
        if declared is None or int(declared) != int(actual):
            raise RegistrationError(f"{lidar_path}: validation {name} mismatch")
    for name, actual in (("mins", np.min(points, axis=0)), ("maxs", np.max(points, axis=0))):
        declared = np.asarray(validation.get(name), dtype=float)
        if declared.shape != (3,) or not np.allclose(
            declared, actual, atol=np.maximum(scales, 1e-6), rtol=0
        ):
            raise RegistrationError(f"{lidar_path}: validation {name} mismatch")
    declared_crs = validation.get("crs") or validation.get("crs_wkt")
    if not declared_crs or not _semantic_crs_equal(declared_crs, crs):
        raise RegistrationError(f"{lidar_path}: validation CRS mismatch")
    return {
        "path": str(lidar_path.resolve()),
        "sha256": lidar_hash,
        "validation_path": str(validation_path.resolve()),
        "validation_sha256": validation_hash,
        "points": points,
        "point_count": len(points),
        "bytes": lidar_path.stat().st_size,
        "bounds": {"min": np.min(points, axis=0).tolist(), "max": np.max(points, axis=0).tolist()},
        "scales": scales.tolist(),
        "crs_wkt": crs.to_wkt(),
        "horizontal_epsg": horizontal_epsg,
        "vertical_epsg": vertical_epsg,
    }


def select_metadata_record(metadata: dict, selector: dict) -> dict:
    if metadata.get("schema") == "v2x-lidar-authoritative-metadata/v1":
        record = metadata.get("project")
        if not isinstance(record, dict):
            raise RegistrationError("authoritative metadata project record is missing")
        return record
    features = metadata.get("features")
    if not isinstance(features, list):
        raise RegistrationError("authoritative metadata format is unsupported")
    required = {key: selector.get(key) for key in ("project_id", "workunit")}
    if any(value in (None, "") for value in required.values()):
        raise RegistrationError("metadata selector requires project_id and workunit")
    matches = [
        feature.get("attributes", {})
        for feature in features
        if all(feature.get("attributes", {}).get(key) == value for key, value in required.items())
    ]
    if len(matches) != 1:
        raise RegistrationError("metadata selector did not identify exactly one project")
    return matches[0]


def _metadata_year(value, label: str) -> int:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).year
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).year
        except ValueError as exc:
            raise RegistrationError(f"metadata {label} is malformed") from exc
    raise RegistrationError(f"metadata {label} is missing")


def parse_acquisition_years(record: dict) -> list[int]:
    start = _metadata_year(
        record.get("collect_start") or record.get("acquisition_start"), "acquisition start"
    )
    end_value = record.get("collect_end") or record.get("acquisition_end")
    end = _metadata_year(end_value, "acquisition end") if end_value is not None else start
    if end < start or end - start > 5:
        raise RegistrationError("metadata acquisition year range is invalid")
    return list(range(start, end + 1))


def parse_acquisition_year(record: dict) -> int:
    return parse_acquisition_years(record)[0]


def parse_opendrive(path: Path) -> dict:
    root = ET.parse(path).getroot()
    if root.tag != "OpenDRIVE":
        raise RegistrationError("map file is not an OpenDRIVE document")
    georeference = (root.findtext("./header/geoReference") or "").strip()
    if not georeference:
        raise RegistrationError("OpenDRIVE georeference is missing")
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "georeference": georeference,
        "georeference_sha256": hashlib.sha256(georeference.encode()).hexdigest(),
    }


def verify_artifact_bindings(annotation: dict, tiles: dict, metadata_path: Path,
                             opendrive: dict, geometry_path: Path, geometry: dict) -> None:
    if annotation.get("schema") != ANNOTATION_SCHEMA:
        raise RegistrationError("manual annotation schema is unsupported")
    bindings = annotation.get("bindings")
    if not isinstance(bindings, dict):
        raise RegistrationError("manual annotations have no immutable bindings")
    declared_tiles = bindings.get("lidar_tiles")
    expected_tiles = sorted(
        (item["sha256"], item["validation_sha256"]) for item in tiles.values()
    )
    actual_tiles = sorted(
        (item.get("lidar_sha256"), item.get("validation_sha256"))
        for item in declared_tiles or []
    )
    if actual_tiles != expected_tiles or len(actual_tiles) != len(set(actual_tiles)):
        raise RegistrationError("manual annotations do not bind every raw/validation tile")
    exact = {
        "metadata_sha256": sha256(metadata_path),
        "opendrive_sha256": opendrive["sha256"],
        "opendrive_georeference_sha256": opendrive["georeference_sha256"],
        "geometry_sha256": sha256(geometry_path),
    }
    for key, expected in exact.items():
        if bindings.get(key) != expected:
            raise RegistrationError(f"manual annotation {key} mismatch")
    if geometry.get("schema") != GEOMETRY_SCHEMA:
        raise RegistrationError("map geometry schema is unsupported")
    if geometry.get("opendrive_sha256") != opendrive["sha256"]:
        raise RegistrationError("map geometry was exported from a different OpenDRIVE")


def resolve_map_polyline(geometry: dict, reference: dict, label: str) -> np.ndarray:
    collection = reference.get("collection")
    feature_id = reference.get("feature_id")
    field = reference.get("polyline_field")
    allowed = {"lanes", "crosswalks", "road_mark_segments"}
    if collection not in allowed or not isinstance(feature_id, str) or not feature_id:
        raise RegistrationError(f"{label}: stable map feature reference is invalid")
    features = geometry.get("geometry", {}).get(collection)
    if not isinstance(features, list):
        raise RegistrationError(f"{label}: map collection {collection} is unavailable")
    matches = [item for item in features if item.get("id") == feature_id]
    if len(matches) != 1:
        raise RegistrationError(f"{label}: map feature identity is missing or ambiguous")
    if not isinstance(field, str) or field not in matches[0]:
        raise RegistrationError(f"{label}: map polyline field is unavailable")
    points = finite_xyz(matches[0][field], f"{label} map polyline")
    indices = reference.get("vertex_indices")
    if indices is not None:
        if (
            not isinstance(indices, list)
            or len(indices) < 2
            or len(indices) != len(set(indices))
            or any(not isinstance(index, int) or index < 0 or index >= len(points) for index in indices)
        ):
            raise RegistrationError(f"{label}: map vertex indices are invalid")
        points = finite_xyz(points[indices], f"{label} selected map polyline")
    return points


def load_features(annotation: dict, geometry: dict, tiles: dict) -> list[dict]:
    raw_features = annotation.get("features")
    if not isinstance(raw_features, list):
        raise RegistrationError("manual annotations have no feature list")
    features, identities, raw_identities, map_identities = [], set(), {}, {}
    for raw in raw_features:
        identity = raw.get("id")
        approach = raw.get("approach_id")
        split = raw.get("split")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise RegistrationError("feature identities must be unique and nonblank")
        if not isinstance(approach, str) or not approach:
            raise RegistrationError(f"{identity}: approach identity is missing")
        if split not in {"fit", "holdout"}:
            raise RegistrationError(f"{identity}: split must be fit or holdout")
        if raw.get("provenance") != MANUAL_PROVENANCE:
            raise RegistrationError(f"{identity}: manual feature provenance is not accepted")
        if raw.get("kind") not in FEATURE_KINDS:
            raise RegistrationError(f"{identity}: feature kind is not accepted")
        identities.add(identity)
        map_reference = raw.get("map", {})
        map_points = resolve_map_polyline(geometry, map_reference, identity)
        map_identity = (
            map_reference.get("collection"), map_reference.get("feature_id"),
            map_reference.get("polyline_field"),
            tuple(map_reference.get("vertex_indices") or range(len(map_points))),
        )
        previous_map = map_identities.get(map_identity)
        if previous_map is not None:
            raise RegistrationError(
                f"map polyline identity leaks between {previous_map[0]}:{previous_map[1]} "
                f"and {identity}:{split}"
            )
        map_identities[map_identity] = (identity, split)
        lidar = raw.get("lidar", {})
        tile_hash = lidar.get("tile_sha256")
        tile = tiles.get(tile_hash)
        if tile is None:
            raise RegistrationError(f"{identity}: LiDAR tile binding is unavailable")
        point_indices = lidar.get("point_indices")
        recorded = finite_xyz(lidar.get("xyz"), f"{identity} recorded LiDAR polyline")
        if (
            not isinstance(point_indices, list)
            or len(point_indices) != len(recorded)
            or len(point_indices) != len(set(point_indices))
            or any(not isinstance(index, int) or index < 0 or index >= tile["point_count"] for index in point_indices)
        ):
            raise RegistrationError(f"{identity}: raw LiDAR point indices are invalid")
        decoded = tile["points"][point_indices]
        tolerance = max(RAW_POINT_REPRODUCTION_TOLERANCE_M, max(tile["scales"]) / 2.0 + 1e-9)
        if not np.allclose(recorded, decoded, atol=tolerance, rtol=0):
            raise RegistrationError(f"{identity}: recorded LiDAR XYZ does not reproduce raw points")
        for point_index in point_indices:
            key = (tile_hash, point_index)
            previous = raw_identities.get(key)
            if previous is not None:
                raise RegistrationError(
                    f"raw LiDAR point identity leaks between {previous[0]}:{previous[1]} "
                    f"and {identity}:{split}"
                )
            raw_identities[key] = (identity, split)
        features.append({
            "id": identity,
            "approach_id": approach,
            "split": split,
            "kind": raw.get("kind"),
            "map_reference": map_reference,
            "map_points": map_points,
            "lidar_tile_sha256": tile_hash,
            "lidar_point_indices": list(point_indices),
            "lidar_points": recorded,
        })
    fit = [item for item in features if item["split"] == "fit"]
    holdout = [item for item in features if item["split"] == "holdout"]
    if len(fit) < MIN_FIT_FEATURES or len(holdout) < MIN_HOLDOUT_FEATURES:
        raise RegistrationError("insufficient fit or holdout feature identities")
    approaches = sorted({item["approach_id"] for item in features})
    if len(approaches) < MIN_APPROACHES:
        raise RegistrationError(f"at least {MIN_APPROACHES} approaches are required")
    for approach in approaches:
        splits = {item["split"] for item in features if item["approach_id"] == approach}
        if splits != {"fit", "holdout"}:
            raise RegistrationError(f"approach {approach} lacks disjoint fit/holdout truth")
    for label, selected in (("fit", fit), ("holdout", holdout)):
        for side in ("map_points", "lidar_points"):
            xy = np.vstack([item[side][:, :2] for item in selected])
            if np.linalg.matrix_rank(xy - np.mean(xy, axis=0), tol=1e-6) != 2:
                raise RegistrationError(f"{label} {side} geometry is rank deficient")
    return features


def resample_polyline(points: np.ndarray, spacing_m: float = EVALUATION_SPACING_M) -> np.ndarray:
    distances = np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(distances)))
    if cumulative[-1] <= 1e-9:
        raise RegistrationError("polyline has no finite horizontal extent")
    samples = np.linspace(0.0, cumulative[-1], max(2, int(math.ceil(cumulative[-1] / spacing_m)) + 1))
    output = np.column_stack([
        np.interp(samples, cumulative, points[:, axis]) for axis in range(3)
    ])
    return output


def transform_points(points: np.ndarray, parameters: np.ndarray) -> np.ndarray:
    tx, ty, yaw, z_bias = parameters
    cosine, sine = math.cos(yaw), math.sin(yaw)
    output = points.copy()
    output[:, 0] = cosine * points[:, 0] - sine * points[:, 1] + tx
    output[:, 1] = sine * points[:, 0] + cosine * points[:, 1] + ty
    output[:, 2] = points[:, 2] + z_bias
    return output


def nearest_segments(source: np.ndarray, target: np.ndarray) -> dict:
    starts, vectors = target[:-1], np.diff(target, axis=0)
    horizontal = vectors[:, :2]
    lengths_squared = np.sum(horizontal * horizontal, axis=1)
    if np.any(lengths_squared <= 1e-18):
        raise RegistrationError("nearest-segment target contains zero-length geometry")
    delta = source[:, None, :2] - starts[None, :, :2]
    fractions = np.clip(
        np.sum(delta * horizontal[None, :, :], axis=2) / lengths_squared[None, :],
        0.0,
        1.0,
    )
    closest_xy = starts[None, :, :2] + fractions[:, :, None] * horizontal[None, :, :]
    squared = np.sum((source[:, None, :2] - closest_xy) ** 2, axis=2)
    selected = np.argmin(squared, axis=1)
    rows = np.arange(len(source))
    chosen_fraction = fractions[rows, selected]
    chosen_xy = closest_xy[rows, selected]
    chosen_z = starts[selected, 2] + chosen_fraction * vectors[selected, 2]
    tangents = horizontal[selected]
    tangents = tangents / np.linalg.norm(tangents, axis=1)[:, None]
    normals = np.column_stack((-tangents[:, 1], tangents[:, 0]))
    differences = source[:, :2] - chosen_xy
    return {
        "normal": np.sum(differences * normals, axis=1),
        "horizontal": np.linalg.norm(differences, axis=1),
        "vertical": source[:, 2] - chosen_z,
    }


def sampled_feature(feature: dict) -> tuple[np.ndarray, np.ndarray]:
    return resample_polyline(feature["map_points"]), resample_polyline(feature["lidar_points"])


def balanced_residuals(parameters: np.ndarray, features: list[dict]) -> np.ndarray:
    approaches = sorted({item["approach_id"] for item in features})
    per_approach = Counter(item["approach_id"] for item in features)
    residuals = []
    for feature in features:
        map_points, lidar_points = sampled_feature(feature)
        transformed = transform_points(map_points, parameters)
        forward = nearest_segments(transformed, lidar_points)
        reverse = nearest_segments(lidar_points, transformed)
        feature_weight = 1.0 / math.sqrt(len(approaches) * per_approach[feature["approach_id"]])
        for direction in (forward, reverse):
            count_weight = feature_weight / math.sqrt(len(direction["normal"]))
            residuals.extend(direction["normal"] * count_weight)
            residuals.extend(direction["vertical"] * count_weight)
    return np.asarray(residuals, dtype=float)


def parameter_bounds(initial: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    radii = np.asarray([
        TRANSLATION_BOUND_RADIUS_M,
        TRANSLATION_BOUND_RADIUS_M,
        math.radians(YAW_BOUND_RADIUS_DEG),
        Z_BIAS_BOUND_RADIUS_M,
    ])
    return initial - radii, initial + radii


def deterministic_seeds(initial: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> list[np.ndarray]:
    offsets = [
        (0, 0, 0, 0),
        (1, 0, 0, 0), (-1, 0, 0, 0),
        (0, 1, 0, 0), (0, -1, 0, 0),
        (0, 0, math.radians(2), 0), (0, 0, math.radians(-2), 0),
        (0, 0, 0, 0.25), (0, 0, 0, -0.25),
    ]
    return [np.clip(initial + np.asarray(offset), lower + 1e-8, upper - 1e-8) for offset in offsets]


def solve(features: list[dict], initial: np.ndarray, multi_start: bool = True) -> dict:
    lower, upper = parameter_bounds(initial)
    seeds = deterministic_seeds(initial, lower, upper) if multi_start else [initial]
    solutions = []
    for seed in seeds:
        result = least_squares(
            balanced_residuals,
            seed,
            args=(features,),
            bounds=(lower, upper),
            method="trf",
            loss="soft_l1",
            f_scale=0.10,
            x_scale=np.asarray([1.0, 1.0, math.radians(1.0), 1.0]),
            max_nfev=500,
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        solutions.append(result)
    successful = [item for item in solutions if item.success and np.isfinite(item.cost)]
    if not successful:
        raise RegistrationError("all deterministic registration starts failed")
    best = min(successful, key=lambda item: item.cost)
    singular = np.linalg.svd(best.jac, compute_uv=False)
    rank = int(np.linalg.matrix_rank(best.jac))
    condition = float(singular[0] / singular[-1]) if len(singular) and singular[-1] > 0 else None
    dof = max(1, len(best.fun) - len(best.x))
    covariance = None
    if rank == len(best.x):
        covariance = (np.linalg.pinv(best.jac.T @ best.jac) * (2.0 * best.cost / dof)).tolist()
    span = upper - lower
    bound_hits = [
        name for name, value, low, high, width in zip(
            ("tx_m", "ty_m", "yaw_rad", "z_bias_m"), best.x, lower, upper, span
        ) if min(value - low, high - value) <= BOUND_PROXIMITY_FRACTION * width
    ]
    alternatives = []
    cost_limit = best.cost * (1.0 + NEAR_OPTIMAL_COST_FRACTION) + 1e-12
    for candidate in successful:
        if candidate is best or candidate.cost > cost_limit:
            continue
        translation = math.hypot(candidate.x[0] - best.x[0], candidate.x[1] - best.x[1])
        yaw = abs(math.degrees(math.atan2(
            math.sin(candidate.x[2] - best.x[2]), math.cos(candidate.x[2] - best.x[2])
        )))
        if translation > FOLD_TRANSLATION_SPREAD_MAX_M or yaw > FOLD_YAW_SPREAD_MAX_DEG:
            alternatives.append({
                "cost": float(candidate.cost),
                "translation_separation_m": translation,
                "yaw_separation_deg": yaw,
                "parameters": candidate.x.tolist(),
            })
    return {
        "x": best.x,
        "cost": float(best.cost),
        "success": bool(best.success),
        "message": str(best.message),
        "nfev": int(best.nfev),
        "jacobian_rank": rank,
        "jacobian_singular_values": singular.tolist(),
        "jacobian_condition": condition,
        "covariance": covariance,
        "bound_hits": bound_hits,
        "near_optimal_separated_modes": alternatives,
        "starts": [
            {"cost": float(item.cost), "success": bool(item.success), "parameters": item.x.tolist()}
            for item in solutions
        ],
    }


def metric_summary(horizontal: list[float], vertical: list[float]) -> dict:
    return {
        "sample_count": len(horizontal),
        "horizontal_rmse_m": math.sqrt(float(np.mean(np.square(horizontal)))) if horizontal else None,
        "horizontal_max_m": max(horizontal) if horizontal else None,
        "symmetric_hausdorff_m": max(horizontal) if horizontal else None,
        "vertical_rmse_m": math.sqrt(float(np.mean(np.square(vertical)))) if vertical else None,
        "vertical_p95_m": percentile([abs(value) for value in vertical], 0.95),
        "vertical_max_m": max((abs(value) for value in vertical), default=None),
    }


def feature_distances(feature: dict, parameters: np.ndarray) -> tuple[list[float], list[float]]:
    map_points, lidar_points = sampled_feature(feature)
    transformed = transform_points(map_points, parameters)
    forward, reverse = nearest_segments(transformed, lidar_points), nearest_segments(lidar_points, transformed)
    return (
        list(forward["horizontal"]) + list(reverse["horizontal"]),
        list(forward["vertical"]) + list(reverse["vertical"]),
    )


def metrics_for_features(features: list[dict], parameters: np.ndarray,
                         initial: np.ndarray) -> dict:
    horizontal, vertical = [], []
    per_feature, per_approach_raw = {}, defaultdict(lambda: ([], []))
    for feature in features:
        h_after, v_after = feature_distances(feature, parameters)
        h_before, v_before = feature_distances(feature, initial)
        horizontal.extend(h_after)
        vertical.extend(v_after)
        approach_h, approach_v = per_approach_raw[feature["approach_id"]]
        approach_h.extend(h_after)
        approach_v.extend(v_after)
        after, before = metric_summary(h_after, v_after), metric_summary(h_before, v_before)
        per_feature[feature["id"]] = {
            "approach_id": feature["approach_id"],
            "split": feature["split"],
            "kind": feature["kind"],
            "map_reference": feature["map_reference"],
            "after": after,
            "before": before,
            "horizontal_rmse_delta_m": after["horizontal_rmse_m"] - before["horizontal_rmse_m"],
            "vertical_rmse_delta_m": after["vertical_rmse_m"] - before["vertical_rmse_m"],
        }
    return {
        "global": metric_summary(horizontal, vertical),
        "per_feature": per_feature,
        "per_approach": {
            key: metric_summary(values[0], values[1]) for key, values in sorted(per_approach_raw.items())
        },
    }


def absolute_metric_failures(prefix: str, metrics: dict) -> list[str]:
    checks = (
        ("horizontal_rmse", metrics["horizontal_rmse_m"], HORIZONTAL_RMSE_MAX_M),
        ("horizontal_max", metrics["horizontal_max_m"], HORIZONTAL_MAX_M),
        ("symmetric_hausdorff", metrics["symmetric_hausdorff_m"], HAUSDORFF_MAX_M),
        ("vertical_rmse", metrics["vertical_rmse_m"], VERTICAL_RMSE_MAX_M),
        ("vertical_p95", metrics["vertical_p95_m"], VERTICAL_P95_MAX_M),
        ("vertical_max", metrics["vertical_max_m"], VERTICAL_MAX_M),
    )
    return [f"{prefix}_{name}" for name, value, limit in checks if value is None or value > limit]


def leave_one_approach_out(fit: list[dict], initial: np.ndarray,
                           full_parameters: np.ndarray) -> dict:
    folds, failures = [], []
    for approach in sorted({item["approach_id"] for item in fit}):
        training = [item for item in fit if item["approach_id"] != approach]
        omitted = [item for item in fit if item["approach_id"] == approach]
        try:
            solution = solve(training, initial, multi_start=False)
            parameters = solution["x"]
            translation_delta = math.hypot(
                parameters[0] - full_parameters[0], parameters[1] - full_parameters[1]
            )
            yaw_delta = abs(math.degrees(math.atan2(
                math.sin(parameters[2] - full_parameters[2]),
                math.cos(parameters[2] - full_parameters[2]),
            )))
            folds.append({
                "omitted_approach_id": approach,
                "training_feature_ids": [item["id"] for item in training],
                "evaluation_feature_ids": [item["id"] for item in omitted],
                "parameters": parameters.tolist(),
                "translation_delta_m": translation_delta,
                "yaw_delta_deg": yaw_delta,
                "omitted_metrics": metrics_for_features(omitted, parameters, initial)["global"],
                "jacobian_rank": solution["jacobian_rank"],
                "jacobian_condition": solution["jacobian_condition"],
                "bound_hits": solution["bound_hits"],
            })
        except (RegistrationError, ValueError) as exc:
            failures.append({"omitted_approach_id": approach, "error": str(exc)})
    return {
        "folds": folds,
        "failures": failures,
        "translation_spread_m": max((item["translation_delta_m"] for item in folds), default=None),
        "yaw_spread_deg": max((item["yaw_delta_deg"] for item in folds), default=None),
    }


def validate_current_survey(path: Path | None, geometry_hash: str, opendrive_hash: str,
                            horizontal_epsg: int | None) -> dict:
    if path is None:
        return {"present": False, "passed": False, "reasons": ["current_horizontal_survey_missing"]}
    survey = json.loads(path.read_text())
    reasons = []
    if survey.get("schema") != SURVEY_SCHEMA:
        reasons.append("current_horizontal_survey_schema")
    if survey.get("geometry_sha256") != geometry_hash:
        reasons.append("current_horizontal_survey_geometry_hash")
    if survey.get("opendrive_sha256") != opendrive_hash:
        reasons.append("current_horizontal_survey_opendrive_hash")
    if survey.get("horizontal_epsg") != horizontal_epsg:
        reasons.append("current_horizontal_survey_crs")
    try:
        observed = datetime.fromisoformat(str(survey.get("observed_at_utc")).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("survey timestamp must include an explicit UTC offset")
        age_days = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds() / 86400
        if age_days < -1 or age_days > CURRENT_SURVEY_MAX_AGE_DAYS:
            reasons.append("current_horizontal_survey_age")
    except (TypeError, ValueError):
        age_days = None
        reasons.append("current_horizontal_survey_timestamp")
    if int(survey.get("control_point_count", 0)) < 6:
        reasons.append("current_horizontal_survey_control_count")
    if int(survey.get("independent_holdout_count", 0)) < 3:
        reasons.append("current_horizontal_survey_holdout_count")
    survey_rmse = float(survey.get("horizontal_rmse_m", math.inf))
    survey_max = float(survey.get("horizontal_max_m", math.inf))
    if not math.isfinite(survey_rmse) or survey_rmse < 0 or survey_rmse > HORIZONTAL_RMSE_MAX_M:
        reasons.append("current_horizontal_survey_rmse")
    if not math.isfinite(survey_max) or survey_max < 0 or survey_max > HORIZONTAL_MAX_M:
        reasons.append("current_horizontal_survey_max")
    return {
        "present": True,
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "age_days": age_days,
        "passed": not reasons,
        "reasons": reasons,
    }


def register(annotation: dict, geometry: dict, tiles: dict, metadata_record: dict,
             metadata_summary: dict, survey: dict) -> dict:
    features = load_features(annotation, geometry, tiles)
    initial_raw = annotation.get("initial_transform")
    if not isinstance(initial_raw, dict):
        raise RegistrationError("manual annotations require one initial site transform")
    initial = np.asarray([
        float(initial_raw["tx_m"]), float(initial_raw["ty_m"]),
        math.radians(float(initial_raw["yaw_deg"])), float(initial_raw["z_bias_m"]),
    ])
    if not np.isfinite(initial).all():
        raise RegistrationError("initial site transform is non-finite")
    fit = [item for item in features if item["split"] == "fit"]
    holdout = [item for item in features if item["split"] == "holdout"]
    solution = solve(fit, initial, multi_start=True)
    fit_metrics = metrics_for_features(fit, solution["x"], initial)
    holdout_metrics = metrics_for_features(holdout, solution["x"], initial)
    folds = leave_one_approach_out(fit, initial, solution["x"])

    reasons = []
    reasons.extend(absolute_metric_failures("fit", fit_metrics["global"]))
    reasons.extend(absolute_metric_failures("holdout", holdout_metrics["global"]))
    for split_name, group in (("fit", fit_metrics), ("holdout", holdout_metrics)):
        for identity, item in group["per_feature"].items():
            reasons.extend(absolute_metric_failures(f"{split_name}_feature_{identity}", item["after"]))
            if item["horizontal_rmse_delta_m"] > FEATURE_REGRESSION_TOLERANCE_M:
                reasons.append(f"{split_name}_feature_{identity}_horizontal_regression")
            if item["vertical_rmse_delta_m"] > FEATURE_REGRESSION_TOLERANCE_M:
                reasons.append(f"{split_name}_feature_{identity}_vertical_regression")
    if solution["jacobian_rank"] != 4:
        reasons.append("fit_jacobian_not_full_rank")
    if solution["jacobian_condition"] is None or solution["jacobian_condition"] > JACOBIAN_CONDITION_MAX:
        reasons.append("fit_jacobian_condition")
    if solution["bound_hits"]:
        reasons.append("fit_parameter_bound_hit")
    if solution["near_optimal_separated_modes"]:
        reasons.append("fit_multimodal")
    if folds["failures"]:
        reasons.append("leave_one_approach_out_failure")
    if folds["translation_spread_m"] is None or folds["translation_spread_m"] > FOLD_TRANSLATION_SPREAD_MAX_M:
        reasons.append("leave_one_approach_out_translation_spread")
    if folds["yaw_spread_deg"] is None or folds["yaw_spread_deg"] > FOLD_YAW_SPREAD_MAX_DEG:
        reasons.append("leave_one_approach_out_yaw_spread")
    for fold in folds["folds"]:
        if fold["jacobian_rank"] != 4:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_rank")
        if fold["jacobian_condition"] is None or fold["jacobian_condition"] > JACOBIAN_CONDITION_MAX:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_condition")
        if fold["bound_hits"]:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_bound")

    acquisition_years = parse_acquisition_years(metadata_record)
    quality_level = str(metadata_record.get("ql") or metadata_record.get("quality_level") or "")
    old_ql2 = 2018 in acquisition_years and quality_level.lower().replace(" ", "") == "ql2"
    if old_ql2:
        reasons.append("2018_ql2_is_development_control_only")
    reasons.extend(survey["reasons"])
    reasons = sorted(set(reasons))
    parameters = solution["x"]
    numerical_passed = not [reason for reason in reasons if reason not in {
        "2018_ql2_is_development_control_only", "current_horizontal_survey_missing"
    } and not reason.startswith("current_horizontal_survey_")]
    return {
        "schema": REPORT_SCHEMA,
        "acceptance_eligible": False if old_ql2 else not reasons,
        "deployment_eligible": False if old_ql2 else not reasons,
        "numerical_registration_passed": numerical_passed,
        "created_at_utc": utc_now(),
        "model": {
            "degrees_of_freedom": ["tx_m", "ty_m", "yaw_deg", "z_bias_m"],
            "forbidden_degrees_of_freedom": [
                "per_approach_transform", "per_feature_transform", "scale", "shear", "local_warp"
            ],
            "transform": {
                "tx_m": float(parameters[0]), "ty_m": float(parameters[1]),
                "yaw_deg": math.degrees(float(parameters[2])), "z_bias_m": float(parameters[3]),
            },
            "initial_transform": initial_raw,
            "bounds_relative_to_initial": {
                "translation_m": TRANSLATION_BOUND_RADIUS_M,
                "yaw_deg": YAW_BOUND_RADIUS_DEG,
                "z_bias_m": Z_BIAS_BOUND_RADIUS_M,
            },
            "objective": {
                "horizontal": "symmetric_point_to_nearest_finite_segment_normal_residual",
                "vertical": "symmetric_nearest_finite_segment_interpolated_z_residual",
                "polyline_resample_spacing_m": EVALUATION_SPACING_M,
                "balancing": "equal_approach_then_equal_feature_then_equal_direction_sample",
                "robust_loss": "soft_l1",
                "robust_scale_m": 0.10,
            },
        },
        "fixed_gates": {
            "horizontal_rmse_max_m": HORIZONTAL_RMSE_MAX_M,
            "horizontal_max_m": HORIZONTAL_MAX_M,
            "symmetric_hausdorff_max_m": HAUSDORFF_MAX_M,
            "vertical_rmse_max_m": VERTICAL_RMSE_MAX_M,
            "vertical_p95_max_m": VERTICAL_P95_MAX_M,
            "vertical_max_m": VERTICAL_MAX_M,
            "fold_translation_spread_max_m": FOLD_TRANSLATION_SPREAD_MAX_M,
            "fold_yaw_spread_max_deg": FOLD_YAW_SPREAD_MAX_DEG,
            "jacobian_condition_max": JACOBIAN_CONDITION_MAX,
        },
        "evidence": metadata_summary,
        "feature_identities": {
            "fit": [item["id"] for item in fit],
            "holdout": [item["id"] for item in holdout],
            "approaches": sorted({item["approach_id"] for item in features}),
        },
        "fit_metrics": fit_metrics,
        "holdout_metrics": holdout_metrics,
        "optimizer": {key: value for key, value in solution.items() if key != "x"},
        "leave_one_approach_out": folds,
        "current_horizontal_survey": survey,
        "reasons": reasons,
        "limitations": [
            "manual_polyline_identity_is_not_a_current_horizontal_survey",
            *(["2018_USGS_QL2_does_not_certify_current_horizontal_site_alignment"] if old_ql2 else []),
            "report_never_modifies_or_deploys_map_or_camera_configuration",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lidar", action="append", type=Path, required=True)
    parser.add_argument("--lidar-validation", action="append", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--opendrive", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--current-horizontal-survey", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment-output", type=Path)
    return parser.parse_args()


def write_registration_outputs(report: dict, survey: dict, output: Path,
                               deployment_output: Path | None = None) -> None:
    write_json_exclusive(output, report)
    if deployment_output is not None:
        if not survey["passed"]:
            raise RegistrationError(
                "refusing deployment output without a passing current horizontal survey"
            )
        if not report["deployment_eligible"]:
            raise RegistrationError(
                "refusing deployment output because strict registration gates did not pass"
            )
        write_json_exclusive(deployment_output, {
            "schema": "v2x-map-lidar-deployment-candidate/v1",
            "registration_report_sha256": canonical_hash(report),
            "transform": report["model"]["transform"],
        })


def main() -> int:
    args = parse_args()
    if len(args.lidar) != len(args.lidar_validation):
        raise SystemExit("one --lidar-validation is required for each --lidar")
    tile_list = [load_lidar_tile(path.resolve(), validation.resolve()) for path, validation in zip(
        args.lidar, args.lidar_validation
    )]
    tiles = {item["sha256"]: item for item in tile_list}
    if len(tiles) != len(tile_list):
        raise SystemExit("duplicate raw LiDAR tiles are not allowed")
    crs_identities = {(item["horizontal_epsg"], item["vertical_epsg"]) for item in tile_list}
    if len(crs_identities) != 1:
        raise SystemExit("raw LiDAR tiles do not share one horizontal/vertical CRS")
    horizontal_epsg, vertical_epsg = next(iter(crs_identities))

    metadata_path, opendrive_path = args.metadata.resolve(), args.opendrive.resolve()
    geometry_path, annotation_path = args.geometry.resolve(), args.annotations.resolve()
    metadata, geometry, annotation = (
        json.loads(metadata_path.read_text()), json.loads(geometry_path.read_text()),
        json.loads(annotation_path.read_text()),
    )
    opendrive = parse_opendrive(opendrive_path)
    verify_artifact_bindings(annotation, tiles, metadata_path, opendrive, geometry_path, geometry)
    metadata_record = select_metadata_record(metadata, annotation.get("metadata_selector", {}))
    if int(metadata_record.get("horiz_crs") or metadata_record.get("horizontal_epsg")) != horizontal_epsg:
        raise RegistrationError("authoritative metadata horizontal CRS differs from raw LiDAR")
    if int(metadata_record.get("vert_crs") or metadata_record.get("vertical_epsg")) != vertical_epsg:
        raise RegistrationError("authoritative metadata vertical CRS differs from raw LiDAR")
    survey = validate_current_survey(
        args.current_horizontal_survey.resolve() if args.current_horizontal_survey else None,
        sha256(geometry_path), opendrive["sha256"], horizontal_epsg,
    )
    metadata_summary = {
        "annotations": {"path": str(annotation_path), "sha256": sha256(annotation_path)},
        "geometry": {"path": str(geometry_path), "sha256": sha256(geometry_path)},
        "opendrive": opendrive,
        "metadata": {
            "path": str(metadata_path), "sha256": sha256(metadata_path),
            "selected_project_id": metadata_record.get("project_id"),
            "selected_workunit": metadata_record.get("workunit"),
            "quality_level": metadata_record.get("ql") or metadata_record.get("quality_level"),
            "acquisition_years": parse_acquisition_years(metadata_record),
        },
        "lidar_tiles": [
            {key: item[key] for key in (
                "path", "sha256", "validation_path", "validation_sha256", "point_count",
                "bytes", "bounds", "scales", "horizontal_epsg", "vertical_epsg"
            )} for item in tile_list
        ],
    }
    report = register(annotation, geometry, tiles, metadata_record, metadata_summary, survey)
    write_registration_outputs(report, survey, args.output, args.deployment_output)
    print(json.dumps({
        "output": str(args.output),
        "acceptance_eligible": report["acceptance_eligible"],
        "numerical_registration_passed": report["numerical_registration_passed"],
        "reasons": report["reasons"],
    }, sort_keys=True))
    return 0 if report["numerical_registration_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resolve manual real/twin annotations into one frozen calibration manifest.

The input JSON keeps point and road-polyline truth separate from generated UE5
world coordinates. Twin pixels are back-projected through a temporary depth
sensor at the baseline rig pose. The output is accepted directly by
``optimize_twin_road_geometry.py`` and never edits cameras.json.
"""

import argparse
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import sys

from PIL import Image, UnidentifiedImageError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_twin_camera_landmarks import (  # noqa: E402
    depth_pixel_to_world,
    encoded_depth_meters,
    wait_for_frame,
)
from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    load_cameras_config,
    twin_horizontal_fov_deg,
)


CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
INTRINSICS_METHODS = {"checkerboard", "charuco"}
DISTORTION_KEYS = ("k1", "k2", "p1", "p2", "k3")


def decoded_image_size(image_bytes, label):
    """Return retained image dimensions, rejecting corrupt/truncated evidence."""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            return tuple(int(value) for value in image.size)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"{label} frame is not a valid retained image") from exc


def stable_depth_meters(raw_data, width, height, u, v):
    """Reject annotations on depth discontinuities before freezing world truth."""
    center_u, center_v = int(round(float(u))), int(round(float(v)))
    if not (1 <= center_u < width - 1 and 1 <= center_v < height - 1):
        raise ValueError("annotated twin pixel lacks a full depth neighborhood")
    samples = [
        encoded_depth_meters(raw_data, width, center_u + du, center_v + dv)
        for dv in (-1, 0, 1)
        for du in (-1, 0, 1)
    ]
    if any(not 0.25 <= value <= 250.0 for value in samples):
        raise ValueError("annotated twin pixel has implausible neighborhood depth")
    median = sorted(samples)[len(samples) // 2]
    tolerance = max(0.25, 0.02 * median)
    if max(abs(value - median) for value in samples) > tolerance:
        raise ValueError("annotated twin pixel lies on a depth discontinuity")
    return samples[4]


def validate_intrinsics_calibration(camera):
    """Validate independently measured physical-camera optical evidence."""
    calibration = camera.get("intrinsics_calibration")
    intrinsics = camera.get("intrinsics") or {}
    if not isinstance(calibration, dict):
        raise ValueError("camera lacks measured intrinsics_calibration evidence")
    if calibration.get("method") not in INTRINSICS_METHODS:
        raise ValueError("intrinsics calibration method must be checkerboard or charuco")
    artifact_hash = str(calibration.get("artifact_sha256") or "")
    if len(artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in artifact_hash):
        raise ValueError("intrinsics calibration artifact SHA-256 is invalid")
    image_count = calibration.get("image_count")
    rms = calibration.get("rms_reprojection_error_px")
    if isinstance(image_count, bool) or not isinstance(image_count, int) or image_count < 10:
        raise ValueError("intrinsics calibration requires at least 10 accepted images")
    source_hashes = calibration.get("source_images_sha256")
    if (
        not isinstance(source_hashes, list)
        or len(source_hashes) != image_count
        or len(set(source_hashes)) != len(source_hashes)
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in source_hashes
        )
    ):
        raise ValueError("intrinsics calibration requires one unique SHA-256 per accepted image")
    if (
        isinstance(rms, bool)
        or not isinstance(rms, (int, float))
        or not math.isfinite(float(rms))
        or not 0.0 <= float(rms) <= 2.0
    ):
        raise ValueError("intrinsics calibration RMS must be finite and no worse than 2 px")
    resolution = calibration.get("resolution")
    if resolution != [intrinsics.get("width"), intrinsics.get("height")]:
        raise ValueError("intrinsics calibration resolution does not match camera intrinsics")
    matrix = calibration.get("camera_matrix")
    expected = [
        [intrinsics.get("fx"), 0.0, intrinsics.get("cx")],
        [0.0, intrinsics.get("fy"), intrinsics.get("cy")],
        [0.0, 0.0, 1.0],
    ]
    try:
        matrix_matches = all(
            math.isclose(float(actual), float(wanted), rel_tol=0.0, abs_tol=1e-6)
            for actual_row, expected_row in zip(matrix, expected)
            for actual, wanted in zip(actual_row, expected_row)
        ) and len(matrix) == 3 and all(len(row) == 3 for row in matrix)
    except (TypeError, ValueError):
        matrix_matches = False
    if not matrix_matches:
        raise ValueError("intrinsics calibration matrix does not match camera intrinsics")
    distortion = calibration.get("distortion")
    if not isinstance(distortion, dict) or any(key not in distortion for key in DISTORTION_KEYS):
        raise ValueError("intrinsics calibration lacks Brown-Conrady distortion coefficients")
    if not all(
        isinstance(distortion[key], (int, float))
        and not isinstance(distortion[key], bool)
        and math.isfinite(float(distortion[key]))
        for key in DISTORTION_KEYS
    ):
        raise ValueError("intrinsics calibration distortion coefficients are invalid")
    return {
        "method": calibration["method"],
        "artifact_sha256": artifact_hash,
        "image_count": image_count,
        "source_images_sha256": list(source_hashes),
        "rms_reprojection_error_px": float(rms),
        "resolution": [int(value) for value in resolution],
        "camera_matrix": [[float(value) for value in row] for row in matrix],
        "distortion": {key: float(distortion[key]) for key in DISTORTION_KEYS},
    }


def validate_intrinsics_artifact(camera, artifact_bytes):
    """Bind the declared calibration values to a real, retained JSON artifact."""
    normalized = validate_intrinsics_calibration(camera)
    actual_hash = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_hash != normalized["artifact_sha256"]:
        raise ValueError("intrinsics calibration artifact hash does not match camera config")
    try:
        payload = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("intrinsics calibration artifact is not valid JSON") from exc
    declared_payload = {
        key: value for key, value in normalized.items() if key != "artifact_sha256"
    }
    if payload != declared_payload:
        raise ValueError("intrinsics calibration artifact contents do not match camera config")
    return normalized


def build_deployment_model(camera, transform):
    """Freeze the exact rig anchor/base needed to round-trip an absolute fit."""
    intrinsics = camera["intrinsics"]
    twin_pose = camera.get("twin_pose") or {}
    final_yaw = math.radians(float(transform.rotation.yaw))
    forward = float(twin_pose.get("forward_offset_m", 0.0))
    right = float(twin_pose.get("right_offset_m", 0.0))
    anchor_location = [
        float(transform.location.x)
        - forward * math.cos(final_yaw)
        + right * math.sin(final_yaw),
        float(transform.location.y)
        - forward * math.sin(final_yaw)
        - right * math.cos(final_yaw),
        float(transform.location.z) - float(twin_pose.get("height_offset_m", 0.0)),
    ]
    lens = {
        "lens_k": 0.0,
        "lens_kcube": 0.0,
        "lens_circle_falloff": 5.0,
        "lens_circle_multiplier": 0.0,
        "lens_x_size": 0.08,
        "lens_y_size": 0.08,
    }
    lens.update(camera.get("twin_lens") or {})
    return {
        "type": "twin_camera_rig_v1",
        "anchor_location": anchor_location,
        "base": {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(intrinsics),
        },
        "lens": {key: float(value) for key, value in lens.items()},
    }


def _pixel(value, label, width, height):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must be a two-number pixel")
    try:
        u, v = (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a two-number pixel") from exc
    if not all(math.isfinite(number) for number in (u, v)):
        raise ValueError(f"{label} contains a non-finite coordinate")
    if not (0.0 <= u < width and 0.0 <= v < height):
        raise ValueError(f"{label} lies outside its source image")
    return [u, v]


def validate_annotations(payload, camera_id, real_size, twin_size):
    """Return normalized annotations while preserving the frozen split."""
    if payload.get("camera_id") != camera_id or camera_id not in CAMERAS:
        raise ValueError("annotation camera_id does not match the requested channel")
    source_hash = str(payload.get("real_frame_sha256") or "")
    twin_hash = str(payload.get("twin_frame_sha256") or "")
    for value, label in ((source_hash, "real"), (twin_hash, "twin")):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"annotation {label} frame hash is invalid")
    points = payload.get("points")
    roads = payload.get("roads")
    if not isinstance(points, list) or not isinstance(roads, list):
        raise ValueError("annotations require point and road lists")
    normalized = []
    identifiers = set()
    for feature in points:
        identifier = str(feature.get("id") or "").strip()
        if not identifier or identifier in identifiers:
            raise ValueError("annotation feature IDs must be nonblank and unique")
        identifiers.add(identifier)
        split = feature.get("split")
        if split not in {"train", "holdout"}:
            raise ValueError(f"{identifier}: split must be frozen train or holdout")
        if feature.get("provenance") != "manually_verified_unique":
            raise ValueError(f"{identifier}: point provenance is not independently verified")
        normalized.append({
            "id": identifier,
            "type": "point",
            "split": split,
            "provenance": "manually_verified_unique",
            "category": str(feature.get("category") or "").strip(),
            "twin": _pixel(feature.get("twin"), f"{identifier}.twin", *twin_size),
            "image": _pixel(feature.get("image"), f"{identifier}.image", *real_size),
        })
    for feature in roads:
        identifier = str(feature.get("id") or "").strip()
        if not identifier or identifier in identifiers:
            raise ValueError("annotation feature IDs must be nonblank and unique")
        identifiers.add(identifier)
        split = feature.get("split")
        if split not in {"train", "holdout"}:
            raise ValueError(f"{identifier}: split must be frozen train or holdout")
        if feature.get("provenance") != "manually_traced_geometry":
            raise ValueError(f"{identifier}: road provenance is not manually traced")
        twin_polyline = feature.get("twin_polyline")
        image_polyline = feature.get("image_polyline")
        if (
            not isinstance(twin_polyline, list)
            or len(twin_polyline) < 2
            or not isinstance(image_polyline, list)
            or len(image_polyline) < 2
        ):
            raise ValueError(f"{identifier}: road polylines require at least two vertices")
        normalized.append({
            "id": identifier,
            "type": "polyline",
            "split": split,
            "provenance": "manually_traced_geometry",
            "category": str(feature.get("category") or "").strip(),
            "twin_polyline": [
                _pixel(pixel, f"{identifier}.twin_polyline", *twin_size)
                for pixel in twin_polyline
            ],
            "image_polyline": [
                _pixel(pixel, f"{identifier}.image_polyline", *real_size)
                for pixel in image_polyline
            ],
        })
    counts = {
        (kind, split): sum(
            feature["type"] == kind and feature["split"] == split
            for feature in normalized
        )
        for kind in ("point", "polyline")
        for split in ("train", "holdout")
    }
    if counts[("point", "train")] < 8 or counts[("point", "holdout")] < 4:
        raise ValueError("annotations require at least 8 train and 4 holdout points")
    if counts[("polyline", "train")] < 3 or counts[("polyline", "holdout")] < 2:
        raise ValueError("annotations require at least 3 train and 2 holdout roads")
    return normalized


def resolve_manifest(
    annotations,
    *,
    camera_id,
    camera,
    transform,
    depth_image,
    real_frame_sha256,
    twin_frame_sha256,
    annotation_sha256,
    cameras_file_sha256,
    camera_config_sha256,
    depth_raw_sha256,
):
    """Back-project validated annotations using one frozen UE5 depth frame."""
    fov = twin_horizontal_fov_deg(camera)

    def world_at(pixel):
        depth = stable_depth_meters(
            depth_image.raw_data,
            depth_image.width,
            depth_image.height,
            pixel[0],
            pixel[1],
        )
        if not 0.25 <= depth <= 250.0:
            raise ValueError(f"implausible UE5 depth {depth:.3f}m")
        location = depth_pixel_to_world(
            transform,
            pixel[0],
            pixel[1],
            depth,
            fov,
            depth_image.width,
            depth_image.height,
        )
        return [float(location.x), float(location.y), float(location.z)]

    features = []
    for feature in annotations:
        base = {
            key: feature[key]
            for key in ("id", "type", "split", "provenance", "category")
        }
        if feature["type"] == "point":
            base.update({
                "world": world_at(feature["twin"]),
                "twin": feature["twin"],
                "image": feature["image"],
            })
        else:
            base.update({
                "world": [world_at(pixel) for pixel in feature["twin_polyline"]],
                "twin_polyline": feature["twin_polyline"],
                "image_polyline": feature["image_polyline"],
            })
        features.append(base)
    intrinsics = camera["intrinsics"]
    intrinsics_calibration = validate_intrinsics_calibration(camera)
    return {
        "schema_version": 1,
        "camera_id": camera_id,
        "width": int(intrinsics["width"]),
        "height": int(intrinsics["height"]),
        "source_frame_sha256": real_frame_sha256,
        "twin_frame_sha256": twin_frame_sha256,
        "annotation_sha256": annotation_sha256,
        "cameras_file_sha256": cameras_file_sha256,
        "camera_config_sha256": camera_config_sha256,
        "depth_frame": {
            "carla_frame": int(depth_image.frame),
            "sensor_timestamp": float(depth_image.timestamp),
            "width": int(depth_image.width),
            "height": int(depth_image.height),
            "raw_data_sha256": depth_raw_sha256,
            "raw_data_size": len(depth_image.raw_data),
        },
        "baseline": {
            "location": [
                float(transform.location.x),
                float(transform.location.y),
                float(transform.location.z),
            ],
            "pitch_deg": float(transform.rotation.pitch),
            "yaw_deg": float(transform.rotation.yaw),
            "roll_deg": float(transform.rotation.roll),
            "fov_deg": float(fov),
            "cx": float(intrinsics["cx"]),
            "cy": float(intrinsics["cy"]),
            "k1": 0.0,
        },
        "deployment_model": build_deployment_model(camera, transform),
        "intrinsics_calibration": intrinsics_calibration,
        "features": features,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations")
    parser.add_argument("output")
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--twin-frame", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--twin-width", type=int, default=1280)
    parser.add_argument("--twin-height", type=int, default=960)
    parser.add_argument("--cameras-json")
    parser.add_argument(
        "--intrinsics-artifact",
        required=True,
        help="retained measured intrinsics JSON whose SHA-256 is frozen in cameras.json",
    )
    parser.add_argument(
        "--depth-frame-output",
        required=True,
        help="retained raw BGRA depth buffer used to resolve every twin annotation",
    )
    args = parser.parse_args()

    annotation_bytes = Path(args.annotations).read_bytes()
    payload = json.loads(annotation_bytes)
    real_bytes = Path(args.real_frame).read_bytes()
    twin_bytes = Path(args.twin_frame).read_bytes()
    real_hash = hashlib.sha256(real_bytes).hexdigest()
    twin_hash = hashlib.sha256(twin_bytes).hexdigest()
    if payload.get("real_frame_sha256") != real_hash:
        raise SystemExit("annotation real frame hash does not match input")
    if payload.get("twin_frame_sha256") != twin_hash:
        raise SystemExit("annotation twin frame hash does not match input")

    cameras_path = Path(args.cameras_json) if args.cameras_json else (
        Path(__file__).resolve().parents[3] / "config" / "cameras.json"
    )
    cameras_bytes = cameras_path.read_bytes()
    config = load_cameras_config(str(cameras_path))
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    # Fail before connecting to or spawning anything in UE5 if the physical
    # camera's optical model has no independent measurement evidence.
    validate_intrinsics_artifact(camera, Path(args.intrinsics_artifact).read_bytes())
    real_size = decoded_image_size(real_bytes, "real")
    twin_size = decoded_image_size(twin_bytes, "twin")
    expected_real_size = (
        int(camera["intrinsics"]["width"]),
        int(camera["intrinsics"]["height"]),
    )
    if real_size != expected_real_size:
        raise SystemExit(
            f"real frame dimensions {real_size} do not match intrinsics {expected_real_size}"
        )
    if twin_size != (args.twin_width, args.twin_height):
        raise SystemExit(
            f"twin frame dimensions {twin_size} do not match depth render "
            f"{(args.twin_width, args.twin_height)}"
        )
    annotations = validate_annotations(
        payload,
        args.camera,
        (int(camera["intrinsics"]["width"]), int(camera["intrinsics"]["height"])),
        (args.twin_width, args.twin_height),
    )

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    transform = compute_twin_camera_transform(world.get_map(), config["site"], camera)
    blueprint = world.get_blueprint_library().find("sensor.camera.depth")
    configure_twin_camera_blueprint(
        blueprint, camera, args.twin_width, args.twin_height
    )
    frames = []
    actor = world.spawn_actor(blueprint, transform)
    try:
        actor.listen(frames.append)
        depth_image = wait_for_frame(world, frames)
        if depth_image is None:
            raise RuntimeError("no UE5 depth frame received")
        depth_raw = bytes(depth_image.raw_data)
        manifest = resolve_manifest(
            annotations,
            camera_id=args.camera,
            camera=camera,
            transform=transform,
            depth_image=depth_image,
            real_frame_sha256=real_hash,
            twin_frame_sha256=twin_hash,
            annotation_sha256=hashlib.sha256(annotation_bytes).hexdigest(),
            cameras_file_sha256=hashlib.sha256(cameras_bytes).hexdigest(),
            camera_config_sha256=hashlib.sha256(json.dumps(
                camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")).hexdigest(),
            depth_raw_sha256=hashlib.sha256(depth_raw).hexdigest(),
        )
        manifest["ue5_map"] = str(world.get_map().name)
        manifest["ue5_map_opendrive_sha256"] = hashlib.sha256(
            world.get_map().to_opendrive().encode("utf-8")
        ).hexdigest()
        Path(args.depth_frame_output).write_bytes(depth_raw)
    finally:
        try:
            actor.stop()
        finally:
            actor.destroy()
    Path(args.output).write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(manifest['features'])} frozen features to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

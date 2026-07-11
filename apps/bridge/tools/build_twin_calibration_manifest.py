#!/usr/bin/env python3
"""Resolve manual real/twin annotations into one frozen calibration manifest.

The input JSON keeps point and road-polyline truth separate from generated UE5
world coordinates. Twin pixels are back-projected through a temporary depth
sensor at the baseline rig pose. The output is accepted directly by
``optimize_twin_road_geometry.py`` and never edits cameras.json.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

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
    load_cameras_config,
    twin_horizontal_fov_deg,
)


CAMERAS = {"ch1", "ch2", "ch3", "ch4"}


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
):
    """Back-project validated annotations using one frozen UE5 depth frame."""
    fov = twin_horizontal_fov_deg(camera)

    def world_at(pixel):
        depth = encoded_depth_meters(
            depth_image.raw_data, depth_image.width, pixel[0], pixel[1]
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
            base.update({"world": world_at(feature["twin"]), "image": feature["image"]})
        else:
            base.update({
                "world": [world_at(pixel) for pixel in feature["twin_polyline"]],
                "image_polyline": feature["image_polyline"],
            })
        features.append(base)
    intrinsics = camera["intrinsics"]
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
        )
        manifest["ue5_map"] = str(world.get_map().name)
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

#!/usr/bin/env python3
"""Export and project globally identified UE5 map calibration geometry.

This is a read-only proposal builder.  It binds the active OpenDRIVE map,
camera configuration, and retained real/twin frames, then exports nearby
crosswalk polygons, lane center/boundary polylines, and static traffic-control
objects in CARLA world coordinates.  It also renders labeled overlays using
the current camera model.  The output is map truth, but the real-image feature
identity still requires topology review before it can become acceptance input.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    compute_twin_camera_transform,
    load_cameras_config,
    twin_horizontal_fov_deg,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def project(points, transform, fov_deg, width, height):
    if not points:
        return [], []
    world = np.asarray(points, dtype=float)
    location = np.asarray([
        transform.location.x, transform.location.y, transform.location.z
    ])
    rotation = rotation_matrix(
        transform.rotation.pitch, transform.rotation.yaw, transform.rotation.roll
    )
    local = (rotation.T @ (world - location).T).T
    depth = local[:, 0]
    focal = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        pixels = np.column_stack((
            width / 2.0 + focal * local[:, 1] / depth,
            height / 2.0 - focal * local[:, 2] / depth,
        ))
    return pixels.tolist(), depth.tolist()


def distance_xy(left, right):
    return math.hypot(left.x - right.x, left.y - right.y)


def split_crosswalk_polygons(locations):
    polygons, current = [], []
    for location in locations:
        point = [float(location.x), float(location.y), float(location.z)]
        if not current:
            current = [point]
            continue
        current.append(point)
        if len(current) >= 4 and np.linalg.norm(
            np.asarray(current[-1]) - np.asarray(current[0])
        ) <= 0.05:
            polygons.append(current)
            current = []
    if current:
        raise RuntimeError("OpenDRIVE crosswalk list ended with an open polygon")
    return polygons


def nearby_lane_polylines(carla_map, anchor, radius_m, spacing_m):
    import carla

    groups = {}
    for waypoint in carla_map.generate_waypoints(spacing_m):
        location = waypoint.transform.location
        if distance_xy(location, anchor) > radius_m:
            continue
        if waypoint.lane_type != carla.LaneType.Driving:
            continue
        key = (int(waypoint.road_id), int(waypoint.section_id), int(waypoint.lane_id))
        groups.setdefault(key, []).append(waypoint)

    output = []
    for key, waypoints in sorted(groups.items()):
        waypoints.sort(key=lambda item: float(item.s))
        if len(waypoints) < 3:
            continue
        center, left, right = [], [], []
        marking = None
        for waypoint in waypoints:
            transform = waypoint.transform
            location = transform.location
            yaw = math.radians(transform.rotation.yaw)
            right_x, right_y = -math.sin(yaw), math.cos(yaw)
            half_width = float(waypoint.lane_width) / 2.0
            center.append([location.x, location.y, location.z + 0.03])
            left.append([
                location.x - right_x * half_width,
                location.y - right_y * half_width,
                location.z + 0.04,
            ])
            right.append([
                location.x + right_x * half_width,
                location.y + right_y * half_width,
                location.z + 0.04,
            ])
            marking = {
                "left": str(waypoint.left_lane_marking.type),
                "right": str(waypoint.right_lane_marking.type),
            }
        output.append({
            "id": f"road-{key[0]}-section-{key[1]}-lane-{key[2]}",
            "road_id": key[0],
            "section_id": key[1],
            "lane_id": key[2],
            "lane_width_m": float(np.median([item.lane_width for item in waypoints])),
            "marking_types": marking,
            "center_world": center,
            "left_boundary_world": left,
            "right_boundary_world": right,
        })
    return output


def static_objects(world, label_name, anchor, radius_m):
    import carla

    label = getattr(carla.CityObjectLabel, label_name)
    output = []
    for item in world.get_environment_objects(label):
        location = item.bounding_box.location
        if distance_xy(location, anchor) > radius_m:
            continue
        output.append({
            "id": str(item.id),
            "name": str(item.name),
            "category": label_name,
            "center_world": [location.x, location.y, location.z],
            "extent": [
                item.bounding_box.extent.x,
                item.bounding_box.extent.y,
                item.bounding_box.extent.z,
            ],
        })
    return sorted(output, key=lambda item: item["id"])


def visible_polyline(points, depths, width, height):
    visible = []
    for point, depth in zip(points, depths):
        if (
            depth > 0.2 and math.isfinite(depth)
            and math.isfinite(point[0]) and math.isfinite(point[1])
            and -width <= point[0] <= 2 * width
            and -height <= point[1] <= 2 * height
        ):
            visible.append(tuple(point))
        else:
            visible.append(None)
    return visible


def draw_segments(draw, points, color, width):
    previous = None
    for point in points:
        if point is not None:
            if previous is not None:
                draw.line((previous, point), fill=color, width=width)
            previous = point
        else:
            previous = None


def render_overlay(image_path, projection, output_path):
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for lane in projection["lanes"]:
        draw_segments(draw, lane["left"], (0, 180, 255), 2)
        draw_segments(draw, lane["right"], (0, 180, 255), 2)
        draw_segments(draw, lane["center"], (255, 210, 0), 1)
    for crosswalk in projection["crosswalks"]:
        draw_segments(draw, crosswalk["pixels"], (255, 40, 40), 4)
        valid = [point for point in crosswalk["pixels"] if point is not None]
        if valid:
            x, y = valid[0]
            draw.text((max(0, x + 3), max(0, y + 3)), crosswalk["id"], fill=(255, 255, 255))
    for item in projection["objects"]:
        point = item["pixel"]
        if point is None:
            continue
        x, y = point
        if 0 <= x < width and 0 <= y < height:
            radius = 5 if item["category"] == "TrafficLight" else 3
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=(255, 0, 255), width=2)
            draw.text((x + 4, y + 3), item["short_id"], fill=(255, 255, 255))
    image.save(output_path, quality=94)


def projected_geometry(geometry, transform, fov, width, height):
    lanes = []
    for lane in geometry["lanes"]:
        entry = {"id": lane["id"]}
        any_visible = False
        for source_key, output_key in (
            ("center_world", "center"),
            ("left_boundary_world", "left"),
            ("right_boundary_world", "right"),
        ):
            pixels, depths = project(lane[source_key], transform, fov, width, height)
            visible = visible_polyline(pixels, depths, width, height)
            entry[output_key] = visible
            any_visible |= sum(point is not None for point in visible) >= 2
        if any_visible:
            lanes.append(entry)
    crosswalks = []
    for item in geometry["crosswalks"]:
        pixels, depths = project(item["world"], transform, fov, width, height)
        visible = visible_polyline(pixels, depths, width, height)
        if sum(point is not None for point in visible) >= 2:
            crosswalks.append({"id": item["id"], "pixels": visible})
    objects = []
    for item in geometry["objects"]:
        pixels, depths = project([item["center_world"]], transform, fov, width, height)
        pixel = None
        if depths and depths[0] > 0.2 and all(math.isfinite(value) for value in pixels[0]):
            pixel = pixels[0]
        objects.append({
            "id": item["id"],
            "short_id": item["id"][-4:],
            "category": item["category"],
            "pixel": pixel,
            "depth_m": depths[0] if depths else None,
        })
    return {"lanes": lanes, "crosswalks": crosswalks, "objects": objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--pair-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--radius-m", type=float, default=80.0)
    parser.add_argument("--lane-spacing-m", type=float, default=0.5)
    args = parser.parse_args()

    if not 10 <= args.radius_m <= 250:
        parser.error("--radius-m must be in [10, 250]")
    if not 0.1 <= args.lane_spacing_m <= 2.0:
        parser.error("--lane-spacing-m must be in [0.1, 2.0]")
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite a nonempty geometry directory")

    cameras_path = Path(args.cameras_json).resolve()
    pair_path = Path(args.pair_manifest).resolve()
    cameras_bytes, pair_bytes = cameras_path.read_bytes(), pair_path.read_bytes()
    config, pair = load_cameras_config(str(cameras_path)), json.loads(pair_bytes)
    if pair.get("schema") != "v2x-observational-calibration-pairs/v1":
        raise SystemExit("pair manifest schema is unsupported")
    if hashlib.sha256(cameras_bytes).hexdigest() != pair.get("cameras_file_sha256"):
        raise SystemExit("cameras file does not match the retained pair manifest")

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive = carla_map.to_opendrive().encode("utf-8")
    transforms, computed_transforms = {}, {}
    for camera in config["cameras"]:
        camera_id = camera["id"]
        observed = pair["cameras"][camera_id]["twin"]["camera_model"]["transform"]
        transforms[camera_id] = carla.Transform(
            carla.Location(
                x=float(observed["location"]["x"]),
                y=float(observed["location"]["y"]),
                z=float(observed["location"]["z"]),
            ),
            carla.Rotation(
                pitch=float(observed["rotation"]["pitch"]),
                yaw=float(observed["rotation"]["yaw"]),
                roll=float(observed["rotation"]["roll"]),
            ),
        )
        computed_transforms[camera_id] = compute_twin_camera_transform(
            carla_map, config["site"], camera
        )
    anchor = transforms["ch1"].location
    polygons = []
    for index, polygon in enumerate(split_crosswalk_polygons(carla_map.get_crosswalks())):
        if min(math.hypot(point[0] - anchor.x, point[1] - anchor.y) for point in polygon) <= args.radius_m:
            polygons.append({"id": f"crosswalk-{index}", "world": polygon})
    lanes = nearby_lane_polylines(carla_map, anchor, args.radius_m, args.lane_spacing_m)
    objects = []
    for label in ("TrafficLight", "TrafficSigns"):
        objects.extend(static_objects(world, label, anchor, args.radius_m))
    # The custom Richmond asset classifies poles, cabinets, and signal arms as
    # ``Other`` rather than ``Poles``.  Keep only the immediate intersection
    # neighborhood so those globally identified objects remain reviewable.
    objects.extend(static_objects(world, "Other", anchor, min(args.radius_m, 30.0)))
    geometry = {"crosswalks": polygons, "lanes": lanes, "objects": objects}

    output_dir.mkdir(parents=True, exist_ok=True)
    camera_reports = {}
    for camera_id in CAMERAS:
        camera = next(item for item in config["cameras"] if item["id"] == camera_id)
        twin = pair["cameras"][camera_id]["twin"]
        if canonical_hash(camera) != twin["camera_config_sha256"]:
            raise SystemExit(f"{camera_id}: camera config hash mismatch")
        fov = float(twin["camera_model"]["image"]["horizontal_fov_deg"])
        reports = {}
        for kind in ("real", "twin"):
            frame = pair["cameras"][camera_id][kind]
            image_path = pair_path.parent / frame["file"]
            if sha256(image_path) != frame["sha256"]:
                raise SystemExit(f"{camera_id}: {kind} frame hash mismatch")
            with Image.open(image_path) as image:
                width, height = image.size
            projection = projected_geometry(
                geometry, transforms[camera_id], fov, width, height
            )
            overlay = output_dir / f"{camera_id}-{kind}-map-overlay.jpg"
            render_overlay(image_path, projection, overlay)
            reports[kind] = {
                "frame": str(image_path),
                "frame_sha256": frame["sha256"],
                "width": width,
                "height": height,
                "overlay": overlay.name,
                "overlay_sha256": sha256(overlay),
                "projection": projection,
            }
        transform = transforms[camera_id]
        computed = computed_transforms[camera_id]
        camera_reports[camera_id] = {
            "camera_config_sha256": twin["camera_config_sha256"],
            "horizontal_fov_deg": fov,
            "baseline_transform": {
                "location": [transform.location.x, transform.location.y, transform.location.z],
                "rotation": [
                    transform.rotation.pitch,
                    transform.rotation.yaw,
                    transform.rotation.roll,
                ],
            },
            "baseline_source": "retained_twin_actor_metadata",
            "tracked_helper_transform": {
                "location": [
                    computed.location.x, computed.location.y, computed.location.z
                ],
                "rotation": [
                    computed.rotation.pitch,
                    computed.rotation.yaw,
                    computed.rotation.roll,
                ],
            },
            "tracked_helper_delta": {
                "location_m": [
                    computed.location.x - transform.location.x,
                    computed.location.y - transform.location.y,
                    computed.location.z - transform.location.z,
                ],
                "rotation_deg": [
                    computed.rotation.pitch - transform.rotation.pitch,
                    computed.rotation.yaw - transform.rotation.yaw,
                    computed.rotation.roll - transform.rotation.roll,
                ],
                "fov_deg": twin_horizontal_fov_deg(camera) - fov,
            },
            **reports,
        }

    report = {
        "schema": "v2x-map-calibration-geometry/v1",
        "acceptance_eligible": False,
        "created_at_utc": utc_now(),
        "warning": "map-bound proposal overlay; real feature identities require frozen topology review",
        "map": carla_map.name,
        "opendrive_sha256": hashlib.sha256(opendrive).hexdigest(),
        "pair_manifest_sha256": hashlib.sha256(pair_bytes).hexdigest(),
        "cameras_file_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
        "radius_m": args.radius_m,
        "lane_spacing_m": args.lane_spacing_m,
        "geometry": geometry,
        "cameras": camera_reports,
    }
    report_path = output_dir / "map-calibration-geometry.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

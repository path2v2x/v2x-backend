#!/usr/bin/env python3
"""
Fit per-channel twin_pose overrides against the calibration ground truth.

The perception model's calibrated pitch/yaw fit ITS pinhole model to the
real (distorted) camera, so residual pose error remains when mirroring the
pose into CARLA. This tool optimises small offsets (yaw, pitch, height)
per channel by minimising the reprojection error of the surveyed
calibration points through the twin camera, using the live map for
terrain heights. Prints a cameras.json `twin_pose` block per channel.

Run on the Path PC:
    ~/V2XCarla/carla-venv-310/bin/python tools/fit_twin_camera_poses.py
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.geo_utils import gps_to_carla
from digital_twin_bridge.twin_camera_rig import (
    heading_to_carla_yaw,
    horizontal_fov_deg,
    load_cameras_config,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_twin_camera import load_calibration_points, project_world_point, xz_to_gps


def nelder_mead(f, x0, step=1.0, max_iter=300, tol=1e-3):
    """Minimal Nelder-Mead for a handful of parameters (no scipy needed)."""
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += step if isinstance(step, (int, float)) else step[i]
        simplex.append(p)
    scores = [f(p) for p in simplex]

    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: scores[i])
        simplex = [simplex[i] for i in order]
        scores = [scores[i] for i in order]
        if abs(scores[-1] - scores[0]) < tol:
            break
        centroid = [sum(p[i] for p in simplex[:-1]) / n for i in range(n)]
        worst = simplex[-1]
        reflect = [centroid[i] + (centroid[i] - worst[i]) for i in range(n)]
        r_score = f(reflect)
        if r_score < scores[0]:
            expand = [centroid[i] + 2 * (centroid[i] - worst[i]) for i in range(n)]
            e_score = f(expand)
            if e_score < r_score:
                simplex[-1], scores[-1] = expand, e_score
            else:
                simplex[-1], scores[-1] = reflect, r_score
        elif r_score < scores[-2]:
            simplex[-1], scores[-1] = reflect, r_score
        else:
            contract = [centroid[i] + 0.5 * (worst[i] - centroid[i]) for i in range(n)]
            c_score = f(contract)
            if c_score < scores[-1]:
                simplex[-1], scores[-1] = contract, c_score
            else:
                best = simplex[0]
                for i in range(1, n + 1):
                    simplex[i] = [best[j] + 0.5 * (simplex[i][j] - best[j]) for j in range(n)]
                    scores[i] = f(simplex[i])
    order = sorted(range(n + 1), key=lambda i: scores[i])
    return simplex[order[0]], scores[order[0]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--cameras-json", default=None)
    args = parser.parse_args()

    import carla

    config = load_cameras_config(args.cameras_json)
    if config is None:
        print("ERROR: cameras config not found", file=sys.stderr)
        return 1

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    carla_map = client.get_world().get_map()
    print(f"Connected. Map: {carla_map.name}")

    site = config["site"]
    base_location = gps_to_carla(carla_map, site["lat"], site["lon"])

    suggestions = {}
    for camera in config["cameras"]:
        camera_id = camera["id"]
        points = load_calibration_points(camera_id)
        if len(points) < 3:
            print(f"{camera_id}: only {len(points)} calibration points, skipping")
            continue

        fov = horizontal_fov_deg(camera["intrinsics"])
        real_w, real_h = camera["intrinsics"]["width"], camera["intrinsics"]["height"]
        scale_u, scale_v = args.width / real_w, args.height / real_h

        # Resolve each true point's CARLA location once (z from the map).
        world_points = []
        for point in points:
            lat, lon = xz_to_gps(
                point["x"], point["z"], site["lat"], site["lon"], camera["heading_deg"]
            )
            world_points.append((gps_to_carla(carla_map, lat, lon), point))

        base_yaw_input = float(camera["yaw_deg"])

        def objective(params, _camera=camera, _world_points=world_points, _fov=fov,
                      _scale=(scale_u, scale_v), _base_yaw=base_yaw_input):
            yaw_off, pitch_off, dz = params
            # Regularise so tiny point sets don't wander to implausible poses.
            penalty = 0.02 * (yaw_off ** 2 + pitch_off ** 2) + 0.5 * dz ** 2
            yaw = heading_to_carla_yaw(float(_camera["heading_deg"]), _base_yaw + yaw_off)
            location = carla.Location(
                x=base_location.x, y=base_location.y,
                z=base_location.z + float(_camera["height_m"]) + dz,
            )
            transform = carla.Transform(
                location,
                carla.Rotation(pitch=float(_camera["pitch_deg"]) + pitch_off, yaw=yaw, roll=0.0),
            )
            total = 0.0
            for world_loc, point in _world_points:
                projected = project_world_point(transform, world_loc, _fov, args.width, args.height)
                if projected is None:
                    total += 5000.0
                    continue
                pu, pv, _depth = projected
                total += math.hypot(
                    pu - point["u"] * _scale[0], pv - point["v"] * _scale[1]
                )
            return total / len(_world_points) + penalty

        before = objective([0.0, 0.0, 0.0])
        best, score = nelder_mead(objective, [0.0, 0.0, 0.0], step=2.0)
        yaw_off, pitch_off, dz = best
        print(
            f"{camera_id}: err {before:.0f}px -> {score:.0f}px "
            f"(yaw {yaw_off:+.2f} deg, pitch {pitch_off:+.2f} deg, dz {dz:+.2f} m)"
        )
        suggestions[camera_id] = {
            "yaw_offset_deg": round(yaw_off, 2),
            "pitch_offset_deg": round(pitch_off, 2),
            "height_offset_m": round(dz, 2),
            "forward_offset_m": 0.5,
        }

    print("\ntwin_pose blocks for config/cameras.json:")
    print(json.dumps(suggestions, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

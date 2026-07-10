"""Synthetic recovery and fail-closed tests for the road-geometry optimizer."""

import hashlib
import importlib.util
from pathlib import Path
import unittest

import numpy as np

TOOL = Path(__file__).resolve().parents[2] / "bridge" / "tools" / "optimize_twin_road_geometry.py"
SPEC = importlib.util.spec_from_file_location("optimize_twin_road_geometry", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RoadGeometryOptimizerTests(unittest.TestCase):
    def synthetic_manifest(self):
        width, height = 1280, 960
        truth = [-35.0, 90.0, 2.0, 90.0, 640.0, 480.0, -0.03]
        location = [0.0, 0.0, 8.0]
        worlds = [
            [-10, 12, 0], [10, 12, 0], [-10, 50, 0], [10, 50, 0],
            [-8, 20, 0], [8, 20, 0], [-8, 35, 0], [8, 35, 0],
            [-12, 12, 0], [12, 12, 0], [-12, 50, 0], [12, 50, 0],
        ]
        pixels, depth = MODULE.project_world_points(worlds, location, truth, width, height)
        self.assertTrue(np.all(depth > 0.1))
        features = []
        for index, (world, pixel) in enumerate(zip(worlds[:12], pixels[:12])):
            features.append({
                "id": f"point-{index}", "type": "point",
                "split": "train" if index < 8 else "holdout",
                "world": world, "image": pixel.tolist(),
                "provenance": "manually_verified_unique",
            })
        line_specs = [
            ("a", [[-10, 12, 0], [0, 12, 0], [10, 12, 0]]),
            ("b", [[-10, 12, 0], [-10, 30, 0], [-10, 50, 0]]),
            ("c", [[-10, 12, 0], [0, 31, 0], [10, 50, 0]]),
            ("d", [[-10, 50, 0], [0, 50, 0], [10, 50, 0]]),
            ("e", [[10, 12, 0], [10, 30, 0], [10, 50, 0]]),
        ]
        for index, (name, world) in enumerate(line_specs):
            projected, _ = MODULE.project_world_points(world, location, truth, width, height)
            features.append({
                "id": f"line-{name}", "type": "line",
                "split": "train" if index < 3 else "holdout",
                "world": world,
                "image_line": [*projected[0], *projected[-1]],
                "provenance": "manually_traced_geometry",
            })
        return {
            "camera_id": "synthetic", "width": width, "height": height,
            "source_frame_sha256": hashlib.sha256(b"frame").hexdigest(),
            "baseline": {
                "location": location, "pitch_deg": -33.0, "yaw_deg": 88.0,
                "roll_deg": 0.0, "fov_deg": 92.0, "cx": 640.0,
                "cy": 480.0, "k1": 0.0,
            },
            "features": features,
        }

    def test_recovers_synthetic_camera_and_passes_holdout(self):
        report = MODULE.optimize_manifest(self.synthetic_manifest())
        self.assertTrue(report["passed"], report)
        self.assertLess(report["heldout"]["points"]["rmse_px"], 1.0)
        self.assertLess(report["heldout"]["lines"]["rmse_px"], 1.0)

    def test_rejects_missing_independent_evidence(self):
        manifest = self.synthetic_manifest()
        manifest["features"] = manifest["features"][:4]
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "dataset_gate")

    def test_polyline_distance_follows_segments_not_infinite_extension(self):
        points = np.array([[0.5, 1.0], [3.0, 0.0]])
        polyline = np.array([[0.0, 0.0], [1.0, 0.0]])
        distances = MODULE.point_to_polyline_distances(points, polyline)
        self.assertTrue(np.allclose(distances, [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()

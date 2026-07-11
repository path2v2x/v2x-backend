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
        truth = [-35.0, 90.0, 2.0, 90.0, 640.0, 480.0, 0.0]
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
            "schema_version": 1,
            "camera_id": "ch1", "width": width, "height": height,
            "source_frame_sha256": hashlib.sha256(b"frame").hexdigest(),
            "twin_frame_sha256": hashlib.sha256(b"twin").hexdigest(),
            "annotation_sha256": hashlib.sha256(b"annotations").hexdigest(),
            "cameras_file_sha256": hashlib.sha256(b"cameras").hexdigest(),
            "camera_config_sha256": hashlib.sha256(b"camera").hexdigest(),
            "ue5_map": "Carla/Maps/Richmond_Field_Station_Richmond_CA",
            "depth_frame": {
                "carla_frame": 123,
                "sensor_timestamp": 45.5,
                "width": 1280,
                "height": 960,
            },
            "baseline": {
                "location": location, "pitch_deg": -33.0, "yaw_deg": 88.0,
                "roll_deg": 0.0, "fov_deg": 92.0, "cx": 640.0,
                "cy": 480.0, "k1": 0.0,
            },
            "deployment_model": {
                "type": "twin_camera_rig_v1",
                "anchor_location": location,
                "base": {
                    "pitch_deg": -33.0,
                    "yaw_deg": 88.0,
                    "roll_deg": 0.0,
                    "fov_deg": 92.0,
                },
                "lens": {
                    "lens_k": 0.0,
                    "lens_kcube": 0.0,
                    "lens_circle_multiplier": 0.0,
                },
            },
            "intrinsics_calibration": {
                "method": "charuco",
                "artifact_sha256": hashlib.sha256(b"intrinsics").hexdigest(),
                "image_count": 24,
                "source_images_sha256": [
                    hashlib.sha256(f"intrinsics-{index}".encode()).hexdigest()
                    for index in range(24)
                ],
                "rms_reprojection_error_px": 0.3,
                "resolution": [width, height],
                "camera_matrix": [
                    [640.0, 0.0, 640.0],
                    [0.0, 640.0, 480.0],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": {
                    "k1": 0.0,
                    "k2": 0.0,
                    "p1": 0.0,
                    "p2": 0.0,
                    "k3": 0.0,
                },
            },
            "features": features,
        }

    def test_recovers_synthetic_camera_and_passes_holdout(self):
        report = MODULE.optimize_manifest(self.synthetic_manifest())
        self.assertTrue(report["passed"], report)
        self.assertLess(report["heldout"]["points"]["rmse_px"], 1.0)
        self.assertLess(report["heldout"]["lines"]["rmse_px"], 1.0)
        self.assertTrue(report["deployability"]["passed"], report)
        self.assertTrue(report["identifiability"]["passed"], report)
        self.assertLessEqual(
            report["deployability"]["optical_roundtrip_max_px"], 0.25
        )

    def test_recovers_translated_camera_and_roundtrips_to_twin_pose(self):
        manifest = self.synthetic_manifest()
        truth = np.array([0.7, -0.4, 8.3, -35.0, 90.0, 2.0, 90.0, 640.0, 480.0, 0.0])
        for feature in manifest["features"]:
            world = [feature["world"]] if feature["type"] == "point" else feature["world"]
            pixels, depth = MODULE.project_calibration_points(
                world, truth, manifest["width"], manifest["height"]
            )
            self.assertTrue(np.all(depth > 0.1))
            if feature["type"] == "point":
                feature["image"] = pixels[0].tolist()
            else:
                feature["image_line"] = [*pixels[0], *pixels[-1]]
        report = MODULE.optimize_manifest(manifest)
        self.assertTrue(report["passed"], report)
        self.assertLess(report["deployability"]["transform_roundtrip_max"], 1e-6)
        fitted = report["parameters"]
        self.assertAlmostEqual(fitted["location_x"], truth[0], delta=0.1)
        self.assertAlmostEqual(fitted["location_y"], truth[1], delta=0.1)
        self.assertAlmostEqual(fitted["location_z"], truth[2], delta=0.1)

    def test_good_fit_with_unrepresentable_optics_cannot_pass_deployment(self):
        manifest = self.synthetic_manifest()
        truth = [-35.0, 90.0, 2.0, 90.0, 700.0, 430.0, -0.25]
        location = manifest["baseline"]["location"]
        for feature in manifest["features"]:
            world = [feature["world"]] if feature["type"] == "point" else feature["world"]
            pixels, _ = MODULE.project_world_points(
                world, location, truth, manifest["width"], manifest["height"]
            )
            if feature["type"] == "point":
                feature["image"] = pixels[0].tolist()
            else:
                feature["image_line"] = [*pixels[0], *pixels[-1]]
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"], report)
        self.assertFalse(report["deployability"]["passed"])
        self.assertIn(
            "measured_physical_optics_not_representable_in_ue5",
            report["deployability"]["reasons"],
        )
        unconstrained = report["unconstrained_diagnostic"]
        self.assertFalse(unconstrained["deployability"]["passed"])
        self.assertIn(
            "unrepresentable_principal_point_or_radial_distortion",
            unconstrained["deployability"]["reasons"],
        )
        self.assertLess(unconstrained["heldout"]["points"]["rmse_px"], 5.0)
        self.assertTrue(any(reason.startswith("heldout_") for reason in report["reasons"]))

    def test_rejects_missing_independent_evidence(self):
        manifest = self.synthetic_manifest()
        manifest["features"] = manifest["features"][:4]
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "dataset_gate")

    def test_rejects_manifest_without_frozen_deployment_model(self):
        manifest = self.synthetic_manifest()
        manifest.pop("deployment_model")
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertIn("missing_deployment_model", report["dataset_gate"]["reasons"])

    def test_rejects_manifest_without_measured_intrinsics(self):
        manifest = self.synthetic_manifest()
        manifest.pop("intrinsics_calibration")
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertIn(
            "missing_measured_intrinsics_calibration",
            report["dataset_gate"]["reasons"],
        )

    def test_rejects_untraceable_intrinsics_source_images(self):
        manifest = self.synthetic_manifest()
        manifest["intrinsics_calibration"]["source_images_sha256"] = ["a" * 64] * 24
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertIn(
            "invalid_measured_intrinsics_calibration",
            report["dataset_gate"]["reasons"],
        )

    def test_measured_distortion_blocks_otherwise_deployable_fit(self):
        manifest = self.synthetic_manifest()
        manifest["intrinsics_calibration"]["distortion"]["k1"] = -0.1
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"], report)
        self.assertIn(
            "measured_physical_optics_not_representable_in_ue5",
            report["reasons"],
        )

    def test_identifiability_rejects_rank_deficient_geometry(self):
        manifest = self.synthetic_manifest()
        report = MODULE.optimize_manifest(manifest)
        params = np.array([report["parameters"][key] for key in MODULE.PARAMETER_NAMES])
        for feature in manifest["features"]:
            if feature["split"] != "train":
                continue
            if feature["type"] == "point":
                feature["world"] = [0.0, 25.0, 0.0]
            else:
                feature["world"] = [[-1.0, 25.0, 0.0], [1.0, 25.0, 0.0]]
        metrics = MODULE.deployment_identifiability(
            manifest, params, np.array([1, 1, 1, 5, 5, 3, 8, 30, 30, 0.1])
        )
        self.assertFalse(metrics["passed"])
        self.assertLess(metrics["rank"], metrics["required_rank"])

    def test_rejects_hand_authored_manifest_without_builder_fingerprints(self):
        manifest = self.synthetic_manifest()
        manifest.pop("annotation_sha256")
        manifest.pop("depth_frame")
        report = MODULE.optimize_manifest(manifest)
        self.assertFalse(report["passed"])
        self.assertIn(
            "missing_annotation_sha256", report["dataset_gate"]["reasons"]
        )
        self.assertIn(
            "missing_depth_frame_identity", report["dataset_gate"]["reasons"]
        )

    def test_polyline_distance_follows_segments_not_infinite_extension(self):
        points = np.array([[0.5, 1.0], [3.0, 0.0]])
        polyline = np.array([[0.0, 0.0], [1.0, 0.0]])
        distances = MODULE.point_to_polyline_distances(points, polyline)
        self.assertTrue(np.allclose(distances, [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()

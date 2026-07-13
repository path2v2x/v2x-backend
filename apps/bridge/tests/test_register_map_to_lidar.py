import copy
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "register_map_to_lidar.py"
SPEC = importlib.util.spec_from_file_location("register_map_to_lidar", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def apply_transform(points, tx=100.0, ty=-50.0, yaw_deg=2.0, z_bias=5.0):
    yaw = math.radians(yaw_deg)
    cosine, sine = math.cos(yaw), math.sin(yaw)
    points = np.asarray(points, dtype=float)
    output = points.copy()
    output[:, 0] = cosine * points[:, 0] - sine * points[:, 1] + tx
    output[:, 1] = sine * points[:, 0] + cosine * points[:, 1] + ty
    output[:, 2] += z_bias
    return output


def synthetic_evidence(warp_holdout=None, initial=None):
    definitions = {
        "north": ([[0, 0, 0], [0, 8, 0], [0, 16, 0]], [[2, 0, 0], [2, 8, 0], [2, 16, 0]]),
        "east": ([[0, 20, 1], [8, 20, 1], [16, 20, 1]], [[0, 22, 1], [8, 22, 1], [16, 22, 1]]),
        "south": ([[20, 0, 2], [20, 8, 2], [20, 16, 2]], [[22, 0, 2], [22, 8, 2], [22, 16, 2]]),
        "west": ([[4, 4, 3], [10, 10, 3], [16, 16, 3]], [[4, 6, 3], [10, 12, 3], [16, 18, 3]]),
    }
    geometry_items, annotation_features, raw_points = [], [], []
    for approach, split_lines in definitions.items():
        for split, map_points in zip(("fit", "holdout"), split_lines):
            feature_id = f"{approach}-{split}"
            geometry_items.append({"id": feature_id, "left_boundary_world": map_points})
            lidar_points = apply_transform(map_points)
            if split == "holdout" and warp_holdout == approach:
                lidar_points[:, 0] += 0.8
            start = len(raw_points)
            raw_points.extend(lidar_points.tolist())
            annotation_features.append({
                "id": feature_id,
                "approach_id": approach,
                "split": split,
                "kind": "road_edge",
                "provenance": tool.MANUAL_PROVENANCE,
                "map": {
                    "collection": "lanes",
                    "feature_id": feature_id,
                    "polyline_field": "left_boundary_world",
                },
                "lidar": {
                    "tile_sha256": "tile-hash",
                    "point_indices": list(range(start, start + len(lidar_points))),
                    "xyz": lidar_points.tolist(),
                },
            })
    raw_points = np.asarray(raw_points)
    geometry = {
        "schema": tool.GEOMETRY_SCHEMA,
        "opendrive_sha256": "xodr-hash",
        "geometry": {"lanes": geometry_items, "crosswalks": [], "road_mark_segments": []},
    }
    annotation = {
        "schema": tool.ANNOTATION_SCHEMA,
        "initial_transform": initial or {
            "tx_m": 100.2, "ty_m": -50.2, "yaw_deg": 2.2, "z_bias_m": 5.1,
        },
        "features": annotation_features,
    }
    tile = {
        "sha256": "tile-hash",
        "validation_sha256": "validation-hash",
        "points": raw_points,
        "point_count": len(raw_points),
        "scales": [0.01, 0.01, 0.01],
    }
    metadata = {
        "collect_start": int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        "ql": "QL 2",
    }
    survey = {
        "present": False, "passed": False,
        "reasons": ["current_horizontal_survey_missing"],
    }
    return annotation, geometry, {"tile-hash": tile}, metadata, survey


def run_synthetic(**kwargs):
    annotation, geometry, tiles, metadata, survey = synthetic_evidence(**kwargs)
    return tool.register(annotation, geometry, tiles, metadata, {"source": "synthetic"}, survey)


def test_known_site_transform_passes_numerical_gates_but_2018_ql2_stays_ineligible():
    report = run_synthetic()
    transform = report["model"]["transform"]
    assert transform["tx_m"] == pytest.approx(100.0, abs=2e-3)
    assert transform["ty_m"] == pytest.approx(-50.0, abs=2e-3)
    assert transform["yaw_deg"] == pytest.approx(2.0, abs=2e-3)
    assert transform["z_bias_m"] == pytest.approx(5.0, abs=2e-3)
    assert report["numerical_registration_passed"] is True
    assert report["acceptance_eligible"] is False
    assert "2018_ql2_is_development_control_only" in report["reasons"]
    assert "current_horizontal_survey_missing" in report["reasons"]
    assert report["optimizer"]["jacobian_rank"] == 4
    assert report["leave_one_approach_out"]["translation_spread_m"] <= 0.10
    assert report["leave_one_approach_out"]["yaw_spread_deg"] <= 0.10


def test_ql2_collection_spanning_december_2017_into_2018_is_development_only():
    annotation, geometry, tiles, metadata, survey = synthetic_evidence()
    metadata["collect_start"] = int(datetime(2017, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
    metadata["collect_end"] = int(datetime(2018, 4, 24, tzinfo=timezone.utc).timestamp() * 1000)
    report = tool.register(annotation, geometry, tiles, metadata, {}, survey)
    assert report["acceptance_eligible"] is False
    assert "2018_ql2_is_development_control_only" in report["reasons"]


def test_current_survey_does_not_promote_2018_ql2_to_acceptance():
    annotation, geometry, tiles, metadata, _ = synthetic_evidence()
    survey = {"present": True, "passed": True, "reasons": [], "sha256": "survey"}
    report = tool.register(annotation, geometry, tiles, metadata, {}, survey)
    assert report["numerical_registration_passed"] is True
    assert report["acceptance_eligible"] is False
    assert report["deployment_eligible"] is False


def test_local_holdout_warp_is_rejected_by_one_global_model():
    report = run_synthetic(warp_holdout="east")
    assert report["numerical_registration_passed"] is False
    assert any(reason.startswith("holdout") for reason in report["reasons"])
    assert report["model"]["forbidden_degrees_of_freedom"][-1] == "local_warp"


def test_distance_uses_finite_segment_endpoint_not_an_infinite_line():
    source = np.asarray([[2.0, 1.0, 0.0]])
    target = np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    distance = tool.nearest_segments(source, target)
    assert distance["horizontal"][0] == pytest.approx(math.sqrt(2.0))
    assert abs(distance["normal"][0]) == pytest.approx(1.0)


def test_raw_point_identity_cannot_leak_between_fit_and_holdout():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["lidar"]["point_indices"][0] = annotation["features"][0]["lidar"]["point_indices"][0]
    annotation["features"][1]["lidar"]["xyz"][0] = annotation["features"][0]["lidar"]["xyz"][0]
    with pytest.raises(tool.RegistrationError, match="leaks between"):
        tool.load_features(annotation, geometry, tiles)


def test_map_polyline_identity_cannot_leak_between_fit_and_holdout():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["map"] = copy.deepcopy(annotation["features"][0]["map"])
    with pytest.raises(tool.RegistrationError, match="map polyline identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_automatic_or_unspecified_feature_provenance_is_rejected():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][0]["provenance"] = "automatic_matcher"
    with pytest.raises(tool.RegistrationError, match="provenance"):
        tool.load_features(annotation, geometry, tiles)


def test_rank_deficient_manual_geometry_is_rejected_before_fit():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    for feature in geometry["geometry"]["lanes"]:
        feature["left_boundary_world"] = [[0, 0, 0], [4, 0, 0], [8, 0, 0]]
    for feature in annotation["features"]:
        indices = feature["lidar"]["point_indices"]
        replacement = apply_transform([[0, 0, 0], [4, 0, 0], [8, 0, 0]])
        tiles["tile-hash"]["points"][indices] = replacement
        feature["lidar"]["xyz"] = replacement.tolist()
    with pytest.raises(tool.RegistrationError, match="rank deficient"):
        tool.load_features(annotation, geometry, tiles)


def test_solution_at_fixed_optimizer_bound_is_not_accepted():
    report = run_synthetic(initial={
        "tx_m": 60.0, "ty_m": -50.0, "yaw_deg": 2.0, "z_bias_m": 5.0,
    })
    assert report["numerical_registration_passed"] is False
    assert "fit_parameter_bound_hit" in report["reasons"]


def binding_fixture(tmp_path):
    metadata = tmp_path / "metadata.json"
    xodr = tmp_path / "map.xodr"
    geometry_path = tmp_path / "geometry.json"
    metadata.write_text('{"features":[]}')
    xodr.write_text("<OpenDRIVE/>")
    opendrive = {
        "sha256": tool.sha256(xodr),
        "georeference_sha256": "georef-hash",
    }
    geometry = {"schema": tool.GEOMETRY_SCHEMA, "opendrive_sha256": opendrive["sha256"]}
    geometry_path.write_text(json.dumps(geometry))
    tiles = {"tile": {"sha256": "tile", "validation_sha256": "validation"}}
    annotation = {
        "schema": tool.ANNOTATION_SCHEMA,
        "bindings": {
            "lidar_tiles": [{"lidar_sha256": "tile", "validation_sha256": "validation"}],
            "metadata_sha256": tool.sha256(metadata),
            "opendrive_sha256": opendrive["sha256"],
            "opendrive_georeference_sha256": "georef-hash",
            "geometry_sha256": tool.sha256(geometry_path),
        },
    }
    return annotation, tiles, metadata, opendrive, geometry_path, geometry


@pytest.mark.parametrize("binding", [
    "metadata_sha256", "opendrive_sha256", "opendrive_georeference_sha256", "geometry_sha256"
])
def test_every_manual_artifact_hash_is_fail_closed(tmp_path, binding):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    annotation["bindings"][binding] = "wrong"
    with pytest.raises(tool.RegistrationError, match="mismatch"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def test_raw_lidar_and_validation_hash_pair_is_fail_closed(tmp_path):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    annotation["bindings"]["lidar_tiles"][0]["validation_sha256"] = "wrong"
    with pytest.raises(tool.RegistrationError, match="raw/validation"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def test_old_geometry_cannot_be_combined_with_live_opendrive(tmp_path):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    geometry["opendrive_sha256"] = "old-map-hash"
    with pytest.raises(tool.RegistrationError, match="different OpenDRIVE"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def write_las_and_validation(tmp_path, scales=(0.01, 0.01, 0.01), crs_epsg=26910):
    import laspy
    from pyproj import CRS

    path = tmp_path / "control.las"
    cloud = laspy.create(point_format=3, file_version="1.2")
    cloud.header.scales = np.asarray(scales)
    cloud.header.offsets = np.asarray([500000.0, 4200000.0, 0.0])
    cloud.header.add_crs(CRS.from_epsg(crs_epsg))
    cloud.x = [500000.0, 500001.0, 500002.0]
    cloud.y = [4200000.0, 4200001.0, 4200002.0]
    cloud.z = [10.0, 10.5, 11.0]
    cloud.write(path)
    decoded = laspy.read(path)
    points = np.column_stack((decoded.x, decoded.y, decoded.z))
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({
        "bytes": path.stat().st_size,
        "points": len(points),
        "mins": np.min(points, axis=0).tolist(),
        "maxs": np.max(points, axis=0).tolist(),
        "crs": decoded.header.parse_crs().to_wkt(),
        "sha256": tool.sha256(path),
    }))
    return path, validation


def test_raw_las_crs_mismatch_is_rejected(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    value = json.loads(validation.read_text())
    value["crs"] = "EPSG:4326"
    validation.write_text(json.dumps(value))
    with pytest.raises(tool.RegistrationError, match="validation CRS mismatch"):
        tool.load_lidar_tile(path, validation)


def test_coarse_raw_las_resolution_is_rejected(tmp_path):
    path, validation = write_las_and_validation(tmp_path, scales=(0.1, 0.1, 0.1))
    with pytest.raises(tool.RegistrationError, match="quantization is too coarse"):
        tool.load_lidar_tile(path, validation)


def test_deployment_output_requires_current_survey(tmp_path):
    survey = tool.validate_current_survey(None, "geometry", "opendrive", 26910)
    assert survey == {
        "present": False, "passed": False,
        "reasons": ["current_horizontal_survey_missing"],
    }
    report = {
        "deployment_eligible": True,
        "model": {"transform": {"tx_m": 1, "ty_m": 2, "yaw_deg": 3, "z_bias_m": 4}},
    }
    with pytest.raises(tool.RegistrationError, match="without a passing current horizontal survey"):
        tool.write_registration_outputs(
            report, survey, tmp_path / "report.json", tmp_path / "deployment.json"
        )
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_2018_ql2_cannot_emit_deployment_even_with_current_survey(tmp_path):
    report = run_synthetic()
    survey = {"passed": True, "reasons": []}
    with pytest.raises(tool.RegistrationError, match="strict registration gates"):
        tool.write_registration_outputs(
            report, survey, tmp_path / "report.json", tmp_path / "deployment.json"
        )
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_nonfinite_current_survey_metrics_fail_closed(tmp_path):
    path = tmp_path / "survey.json"
    path.write_text(json.dumps({
        "schema": tool.SURVEY_SCHEMA,
        "geometry_sha256": "geometry",
        "opendrive_sha256": "opendrive",
        "horizontal_epsg": 26910,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "control_point_count": 6,
        "independent_holdout_count": 3,
        "horizontal_rmse_m": float("nan"),
        "horizontal_max_m": 0.1,
    }))
    survey = tool.validate_current_survey(path, "geometry", "opendrive", 26910)
    assert survey["passed"] is False
    assert "current_horizontal_survey_rmse" in survey["reasons"]

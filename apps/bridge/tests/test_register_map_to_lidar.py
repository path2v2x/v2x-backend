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
                    "physical_control_ids": [
                        f"physical-{feature_id}-{index}" for index in range(len(lidar_points))
                    ],
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


def write_current_survey(tmp_path, geometry, geometry_hash="geometry", opendrive_hash="opendrive",
                         corrupt_summary=False):
    from pyproj import CRS

    fit_features = [item for item in geometry["geometry"]["lanes"] if item["id"].endswith("-fit")]
    holdout_features = [item for item in geometry["geometry"]["lanes"] if item["id"].endswith("-holdout")]
    selected = []
    for feature in fit_features:
        for vertex_index, point in enumerate(feature["left_boundary_world"]):
            selected.append(("fit", feature, vertex_index, point))
    selected = selected[:10] + [
        ("holdout", feature, 0, feature["left_boundary_world"][0])
        for feature in holdout_features
    ]
    controls = []
    for index, (split, feature, vertex_index, point) in enumerate(selected):
        controls.append({
            "id": f"survey-{split}-{index}",
            "physical_control_id": f"monument-{index}",
            "split": split,
            "provenance": "licensed_survey_raw_control",
            "map": {
                "collection": "lanes", "feature_id": feature["id"],
                "point_field": "left_boundary_world", "vertex_index": vertex_index,
            },
            "map_xyz": point,
            "survey_xy": apply_transform([point])[0, :2].tolist(),
            "horizontal_uncertainty_m": 0.02,
        })
    crs = CRS.from_epsg(26910)
    value = {
        "schema": tool.SURVEY_SCHEMA,
        "geometry_sha256": geometry_hash,
        "opendrive_sha256": opendrive_hash,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "horizontal_crs": {
            "epsg": 26910,
            "wkt": crs.to_wkt(),
            "linear_units": crs.axis_info[0].unit_name,
            "datum": crs.datum.name,
        },
        "controls": controls,
    }
    if corrupt_summary:
        value.update({"horizontal_rmse_m": 0.0, "horizontal_max_m": 0.0})
    path = tmp_path / "survey.json"
    path.write_text(json.dumps(value))
    return path


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


def test_source_feature_identity_cannot_leak_through_different_vertex_slices():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][0]["map"]["vertex_indices"] = [0, 1]
    annotation["features"][1]["map"] = copy.deepcopy(annotation["features"][0]["map"])
    annotation["features"][1]["map"]["vertex_indices"] = [1, 2]
    with pytest.raises(tool.RegistrationError, match="map source feature identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_physical_control_identity_cannot_be_renamed_across_splits():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["lidar"]["physical_control_ids"][0] = (
        annotation["features"][0]["lidar"]["physical_control_ids"][0]
    )
    with pytest.raises(tool.RegistrationError, match="physical control identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_geometric_resampling_duplicate_with_new_source_id_is_rejected():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    original = geometry["geometry"]["lanes"][0]["left_boundary_world"]
    duplicate = {
        "id": "renamed-resample",
        "left_boundary_world": [[0, 0, 0], [0, 4, 0], [0, 8, 0], [0, 12, 0], [0, 16, 0]],
    }
    geometry["geometry"]["lanes"].append(duplicate)
    annotation["features"][1]["map"] = {
        "collection": "lanes", "feature_id": duplicate["id"],
        "polyline_field": "left_boundary_world",
    }
    assert duplicate["left_boundary_world"] != original
    with pytest.raises(tool.RegistrationError, match="geometric duplicate/resampling"):
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


def test_deterministic_seeds_cover_every_declared_parameter_bound():
    initial = np.asarray([10.0, 20.0, math.radians(3.0), 4.0])
    lower, upper = tool.parameter_bounds(initial)
    seeds = np.asarray(tool.deterministic_seeds(initial, lower, upper))
    span = upper - lower
    assert len(seeds) >= 17
    assert np.all(np.min(seeds, axis=0) - lower <= span * 2e-8)
    assert np.all(upper - np.max(seeds, axis=0) <= span * 2e-8)


def test_near_optimal_solutions_are_clustered_into_separate_basins():
    class Result:
        def __init__(self, cost, values):
            self.cost = cost
            self.x = np.asarray(values, dtype=float)

    clusters = tool.cluster_solution_basins([
        Result(1.0, [0, 0, 0, 0]),
        Result(1.01, [0.01, 0.01, math.radians(0.01), 0.01]),
        Result(1.02, [1.0, 0, math.radians(1.0), 0]),
    ])
    assert len(clusters) == 2
    assert sorted(item["member_count"] for item in clusters) == [1, 2]


def test_cli_fails_nonacceptance_by_default_and_requires_explicit_dev_override():
    report = {"acceptance_eligible": False, "numerical_registration_passed": True}
    assert tool.report_exit_code(report) == 2
    assert tool.report_exit_code(report, development_numeric_ok=True) == 0
    assert tool.report_exit_code({"acceptance_eligible": True}) == 0


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


def strict_geometry_fixture(tmp_path):
    from PIL import Image
    from types import SimpleNamespace

    tmp_path.mkdir(parents=True, exist_ok=True)
    exporter_path = TOOL_PATH.with_name("export_map_calibration_geometry.py")
    exporter_spec = importlib.util.spec_from_file_location("exporter_fixture", exporter_path)
    exporter = importlib.util.module_from_spec(exporter_spec)
    exporter_spec.loader.exec_module(exporter)
    xodr = tmp_path / "map.xodr"
    xodr.write_text("""<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header>
<road id="7" length="4"><lanes><laneSection s="0">
<center><lane id="0"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></center>
<right><lane id="-1"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></right>
</laneSection></lanes></road></OpenDRIVE>""")
    opendrive = tool.parse_opendrive(xodr)
    cameras_value = {"cameras": [{"id": camera, "value": index} for index, camera in enumerate(tool.CAMERA_IDS)]}
    cameras = tmp_path / "cameras.json"
    cameras.write_text(json.dumps(cameras_value))
    pair_cameras, report_cameras = {}, {}
    for camera in tool.CAMERA_IDS:
        camera_object = next(item for item in cameras_value["cameras"] if item["id"] == camera)
        camera_hash = tool.canonical_hash(camera_object)
        camera_model = {
            "transform": {
                "location": {"x": 1.0, "y": 2.0, "z": 3.0},
                "rotation": {"pitch": -5.0, "yaw": 10.0, "roll": 0.0},
            },
            "image": {"horizontal_fov_deg": 90.0},
        }
        pair_camera, report_camera = {}, {
            "camera_config_sha256": camera_hash,
            "horizontal_fov_deg": 90.0,
            "baseline_source": "retained_twin_actor_metadata",
            "baseline_transform": {
                "location": [1.0, 2.0, 3.0],
                "rotation": [-5.0, 10.0, 0.0],
            },
        }
        for kind, color in (("real", "red"), ("twin", "blue")):
            image_path = tmp_path / f"{camera}-{kind}.jpg"
            Image.new("RGB", (16, 12), color=color).save(image_path)
            frame = {"file": image_path.name, "sha256": tool.sha256(image_path)}
            if kind == "twin":
                frame["camera_config_sha256"] = camera_hash
                frame["camera_model"] = camera_model
            pair_camera[kind] = frame
            overlay_path = tmp_path / f"{camera}-{kind}-overlay.jpg"
            Image.new("RGB", (16, 12), color=color).save(overlay_path)
            report_camera[kind] = {
                "frame_sha256": frame["sha256"], "width": 16, "height": 12,
                "overlay": overlay_path.name,
                "overlay_sha256": tool.sha256(overlay_path),
                "projection": {"lanes": [], "crosswalks": [], "objects": []},
            }
        pair_cameras[camera] = pair_camera
        report_cameras[camera] = report_camera
    pair_value = {
        "schema": "v2x-observational-calibration-pairs/v1",
        "cameras_file_sha256": tool.sha256(cameras),
        "cameras": pair_cameras,
    }
    pair = tmp_path / "pairs.json"
    pair.write_text(json.dumps(pair_value))
    exact_ranges = exporter.opendrive_road_mark_ranges(xodr.read_bytes())
    boundary = [[float(value), 0.0, 0.0] for value in range(5)]
    sampled = [{
        "id": f"unbound-{side}", "road_id": 7, "section_id": 0, "lane_id": -1,
        "side": side, "type": "solid", "color": "white", "width_m": 0.15,
        "lane_change": "both", "start_s_m": 0.0, "end_s_m": 4.0,
        "sample_count": 5, "boundary_world": boundary, "usable_as_polyline": True,
    } for side in ("left", "right")]
    lane = {
        "id": "road-7-section-0-lane--1", "road_id": 7, "section_id": 0,
        "lane_id": -1, "s_range_m": [0.0, 4.0], "lane_width_m": 4.0,
        "lane_width_range_m": [4.0, 4.0], "center_world": boundary,
        "left_boundary_world": [[point[0], -2.0, point[2]] for point in boundary],
        "right_boundary_world": [[point[0], 2.0, point[2]] for point in boundary],
        "road_mark_segment_ids": [item["id"] for item in sampled],
        "road_mark_segments": sampled,
    }
    road_mark_segments = exporter.bind_sampled_road_marks([lane], exact_ranges, 1.0)
    payload = {
        "crosswalks": [], "lanes": [lane], "road_mark_segments": road_mark_segments,
        "opendrive_road_mark_ranges": exact_ranges, "objects": [],
    }
    transform = SimpleNamespace(
        location=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        rotation=SimpleNamespace(pitch=-5.0, yaw=10.0, roll=0.0),
    )
    for report_camera in report_cameras.values():
        for kind in ("real", "twin"):
            report_camera[kind]["projection"] = exporter.projected_geometry(
                payload, transform, 90.0, 16, 12
            )
    geometry_value = {
        "schema": tool.GEOMETRY_SCHEMA,
        "map": "SyntheticMap",
        "opendrive_sha256": opendrive["sha256"],
        "pair_manifest_sha256": tool.sha256(pair),
        "cameras_file_sha256": tool.sha256(cameras),
        "radius_m": 80.0,
        "lane_spacing_m": 0.5,
        "geometry": payload,
        "cameras": report_cameras,
    }
    geometry_value["geometry_provenance"] = {
        "schema": "v2x-map-geometry-provenance/v1",
        "exporter_sha256": tool.sha256(exporter_path),
        "map": "SyntheticMap",
        "opendrive_sha256": opendrive["sha256"],
        "opendrive_georeference_sha256": opendrive["georeference_sha256"],
        "pair_manifest_sha256": tool.sha256(pair),
        "cameras_file_sha256": tool.sha256(cameras),
        "radius_m": 80.0,
        "lane_spacing_m": 0.5,
        "geometry_payload_sha256": tool.canonical_hash(payload),
        "exact_road_mark_ranges_sha256": tool.canonical_hash(exact_ranges),
    }
    geometry = tmp_path / "geometry-strict.json"
    geometry.write_text(json.dumps(geometry_value))
    return geometry, geometry_value, xodr, opendrive, pair, cameras


def test_geometry_provenance_recomputes_exporter_pair_frames_and_payload(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path)
    result = tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)
    assert result["geometry_payload_sha256"] == tool.canonical_hash(value["geometry"])
    assert result["exporter_sha256"] == tool.sha256(TOOL_PATH.with_name("export_map_calibration_geometry.py"))


def test_schema_and_xodr_hash_without_full_geometry_provenance_is_rejected(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path)
    del value["geometry_provenance"]
    with pytest.raises(tool.RegistrationError, match="no strict exporter provenance"):
        tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)


def test_geometry_payload_or_retained_frame_tamper_is_rejected(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path)
    value["geometry"]["objects"].append({"id": "fabricated"})
    with pytest.raises(tool.RegistrationError, match="geometry_payload_sha256 mismatch"):
        tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path / "frames")
    (pair.parent / "ch1-real.jpg").write_bytes(b"tampered")
    with pytest.raises(tool.RegistrationError, match="source frame hash mismatch"):
        tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)


def test_geometry_camera_projection_is_independently_recomputed(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path)
    value["cameras"]["ch1"]["real"]["projection"]["objects"].append({"fabricated": True})
    with pytest.raises(tool.RegistrationError, match="projection cannot be reproduced"):
        tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)


def test_geometry_road_mark_binding_is_independently_recomputed(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras = strict_geometry_fixture(tmp_path)
    value["geometry"]["road_mark_segments"][0]["opendrive_source_lane_id"] = 999
    value["geometry_provenance"]["geometry_payload_sha256"] = tool.canonical_hash(
        value["geometry"]
    )
    with pytest.raises(tool.RegistrationError, match="does not reproduce exporter output"):
        tool.validate_geometry_provenance(value, geometry, xodr, opendrive, pair, cameras)


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


def write_las_and_validation(tmp_path, scales=(0.01, 0.01, 0.01), crs_epsg=26910,
                             vertical_epsg=5703):
    import laspy
    from pyproj import CRS

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "control.las"
    cloud = laspy.create(point_format=6, file_version="1.4")
    cloud.header.scales = np.asarray(scales)
    cloud.header.offsets = np.asarray([500000.0, 4200000.0, 0.0])
    cloud.header.add_crs(CRS.from_user_input(f"EPSG:{crs_epsg}+{vertical_epsg}"))
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


def test_non_metre_horizontal_and_vertical_las_crs_are_rejected(tmp_path):
    horizontal_path, horizontal_validation = write_las_and_validation(
        tmp_path / "horizontal", crs_epsg=2227, vertical_epsg=5703
    )
    with pytest.raises(tool.RegistrationError, match="horizontal CRS coordinate axes are not metres"):
        tool.load_lidar_tile(horizontal_path, horizontal_validation)
    vertical_path, vertical_validation = write_las_and_validation(
        tmp_path / "vertical", crs_epsg=26910, vertical_epsg=6360
    )
    with pytest.raises(tool.RegistrationError, match="vertical CRS coordinate axes are not metres"):
        tool.load_lidar_tile(vertical_path, vertical_validation)


def test_opendrive_georeference_must_be_projected_metres(tmp_path):
    path = tmp_path / "feet.xodr"
    path.write_text("<OpenDRIVE><header><geoReference>EPSG:2227</geoReference></header></OpenDRIVE>")
    with pytest.raises(tool.RegistrationError, match="not metres"):
        tool.parse_opendrive(path)


def test_deployment_output_requires_current_survey(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    survey = tool.validate_current_survey(None, geometry, "geometry", "opendrive", 26910)
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
    survey = {"passed": True, "raw_controls_recomputed": True, "reasons": []}
    with pytest.raises(tool.RegistrationError, match="strict registration gates"):
        tool.write_registration_outputs(
            report, survey, tmp_path / "report.json", tmp_path / "deployment.json"
        )
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_summary_only_current_survey_cannot_pass(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path = tmp_path / "survey.json"
    path.write_text(json.dumps({
        "schema": tool.SURVEY_SCHEMA,
        "geometry_sha256": "geometry",
        "opendrive_sha256": "opendrive",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "control_point_count": 6,
        "independent_holdout_count": 3,
        "horizontal_rmse_m": 0.0,
        "horizontal_max_m": 0.1,
    }))
    survey = tool.validate_current_survey(path, geometry, "geometry", "opendrive", 26910)
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_controls" in survey["reasons"]


def test_current_survey_recomputes_raw_fit_and_holdout_controls(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path = write_current_survey(tmp_path, geometry, corrupt_summary=True)
    survey = tool.validate_current_survey(path, geometry, "geometry", "opendrive", 26910)
    assert survey["passed"] is True
    assert survey["raw_controls_recomputed"] is True
    assert survey["raw_control_count"] == 14
    assert survey["fit_nonzero_pairwise_distance_count"] >= 10
    assert survey["recomputed_fit_metrics"]["horizontal_rmse_m"] < 1e-8
    assert survey["recomputed_holdout_metrics"]["horizontal_max_m"] < 1e-8
    assert survey["recomputed_transform"]["tx_m"] == pytest.approx(100.0)


def test_survey_raw_control_uncertainty_and_datum_fail_closed(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path = write_current_survey(tmp_path, geometry)
    value = json.loads(path.read_text())
    value["horizontal_crs"]["datum"] = "fabricated datum"
    value["controls"][0]["horizontal_uncertainty_m"] = 0.5
    path.write_text(json.dumps(value))
    survey = tool.validate_current_survey(path, geometry, "geometry", "opendrive", 26910)
    assert survey["passed"] is False
    assert "current_horizontal_survey_crs" in survey["reasons"]
    assert "current_horizontal_survey_raw_controls" in survey["reasons"]


def test_survey_geometric_control_duplicate_across_splits_is_rejected(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    lanes = geometry["geometry"]["lanes"]
    fit = next(item for item in lanes if item["id"].endswith("-fit"))
    holdout = next(item for item in lanes if item["id"].endswith("-holdout"))
    holdout["left_boundary_world"][0] = list(fit["left_boundary_world"][0])
    path = write_current_survey(tmp_path, geometry)
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910
    )
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_controls" in survey["reasons"]

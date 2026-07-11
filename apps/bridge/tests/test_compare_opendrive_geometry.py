import importlib.util
import math
from pathlib import Path


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "compare_opendrive_geometry.py"
SPEC = importlib.util.spec_from_file_location("compare_opendrive_geometry", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def geometry(shape, length=10.0, heading=0.0):
    return {
        "s": 0.0,
        "x": 1.0,
        "y": 2.0,
        "hdg": heading,
        "length": length,
        "shape": shape,
    }


def test_evaluate_line_and_arc():
    import xml.etree.ElementTree as ET

    x, y, heading = tool.evaluate_geometry(geometry(ET.Element("line")), 3.0)
    assert (x, y, heading) == (4.0, 2.0, 0.0)

    x, y, heading = tool.evaluate_geometry(
        geometry(ET.Element("arc", curvature="0.1")), 10.0
    )
    assert math.isclose(x, 1.0 + math.sin(1.0) / 0.1, abs_tol=1e-9)
    assert math.isclose(y, 2.0 + (1.0 - math.cos(1.0)) / 0.1, abs_tol=1e-9)
    assert math.isclose(heading, 1.0, abs_tol=1e-9)


def test_spiral_with_constant_curvature_matches_arc():
    import xml.etree.ElementTree as ET

    spiral = geometry(ET.Element("spiral", curvStart="0.1", curvEnd="0.1"))
    arc = geometry(ET.Element("arc", curvature="0.1"))
    spiral_pose = tool.evaluate_geometry(spiral, 10.0)
    arc_pose = tool.evaluate_geometry(arc, 10.0)
    for left, right in zip(spiral_pose, arc_pose):
        assert math.isclose(left, right, abs_tol=1e-7)


def test_parse_and_match_crosswalks(tmp_path):
    source = tmp_path / "source.xodr"
    source.write_text(
        """<OpenDRIVE>
<header revMajor="1" revMinor="4"><geoReference>same</geoReference></header>
<road id="1" length="20" junction="-1">
  <planView><geometry s="0" x="0" y="0" hdg="0" length="20"><line/></geometry></planView>
  <objects><object id="cw" name="LadderCrosswalk" type="crosswalk" s="10" t="2" hdg="0">
    <outline><cornerLocal u="-1" v="-1"/><cornerLocal u="1" v="-1"/>
      <cornerLocal u="1" v="1"/><cornerLocal u="-1" v="1"/></outline>
  </object></objects>
  <signals><signal id="sig" s="8" t="-1" type="1000001" subtype="0"/></signals>
</road><junction id="1"/></OpenDRIVE>"""
    )
    model = tool.parse_map(source)
    assert model["crosswalks"][0]["center_xy"] == [10.0, 2.0]
    assert model["crosswalks"][0]["outline_xy"][0] == [9.0, 1.0]
    assert model["signals"][0]["center_xy"] == [8.0, -1.0]

    report = tool.compare_maps(model, model, road_spacing_m=1.0)
    assert report["georeference_equal"] is True
    assert report["crosswalks"]["distance_m"]["max"] == 0.0
    assert report["signals"]["distance_m"]["max"] == 0.0
    assert report["road_reference_line"]["deployed_to_candidate"]["coverage"]["0.25"] == 1.0


def test_greedy_feature_match_is_one_to_one():
    left = [
        {"id": "a", "center_xy": [0.0, 0.0]},
        {"id": "b", "center_xy": [0.2, 0.0]},
    ]
    right = [{"id": "c", "center_xy": [0.1, 0.0]}]
    result = tool.match_features(left, right, 1.0)
    assert len(result["matches"]) == 1
    assert len(result["unmatched_deployed"]) == 1
    assert result["unmatched_candidate"] == []


def test_signal_match_rejects_different_feature_classes():
    left = [{"id": "vehicle", "feature_class": "vehicle_signal", "center_xy": [0, 0]}]
    right = [{"id": "pedestrian", "feature_class": "pedestrian_signal", "center_xy": [0, 0]}]
    result = tool.match_features(left, right, 1.0, require_same_class=True)
    assert result["matches"] == []
    assert len(result["unmatched_deployed"]) == 1
    assert len(result["unmatched_candidate"]) == 1


def test_site_anchor_uses_bound_map_georeference(tmp_path):
    config = tmp_path / "cameras.json"
    georeference = (
        "+proj=tmerc +lat_0=37 +lon_0=-122 +k=1 +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m"
    )
    config.write_text(
        '{"site":{"lat":37.0,"lon":-122.0,"map_georeference":'
        + repr(georeference).replace("'", '"')
        + "}}"
    )
    result = tool.site_anchor_from_config(config, georeference)
    x, y = result["anchor_xy"]
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, 0.0, abs_tol=1e-9)


def test_outline_distance_metrics_are_symmetric():
    left = [[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]]
    right = [[1, 0], [3, 0], [3, 1], [1, 1], [1, 0]]
    metrics = tool.outline_distance_metrics(left, right)
    assert math.isclose(metrics["symmetric_hausdorff_m"], 1.0, abs_tol=1e-9)
    assert math.isclose(metrics["deployed_area_m2"], 2.0, abs_tol=1e-9)
    assert math.isclose(metrics["candidate_area_m2"], 2.0, abs_tol=1e-9)


def test_assignment_is_global_and_reports_ambiguity():
    # Greedy takes row0->col0 (1.0), forcing row1->col1 (100). The global
    # solution is row0->col1 (2.0), row1->col0 (1.1).
    assert tool.minimum_cost_assignment([[1.0, 2.0], [1.1, 100.0]]) == [1, 0]


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "report.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("writer overwrote immutable evidence")

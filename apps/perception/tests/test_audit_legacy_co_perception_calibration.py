import importlib.util
import math
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_legacy_co_perception_calibration.py"
)
SPEC = importlib.util.spec_from_file_location("legacy_calibration_audit", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_parse_legacy_inputs(tmp_path):
    source = tmp_path / "calibration.py"
    source.write_text(
        """import numpy as np
def run():
    K = np.array([[100, 0, 50], [0, 101, 40], [0, 0, 1]], dtype=float)
    calibration_points = [
      {'u': 10, 'v': 20, 'true_X': 1, 'true_Z': 2},
      {'u': 30, 'v': 40, 'true_X': 3, 'true_Z': 4},
    ]
"""
    )
    matrix, points = tool.parse_legacy_calibration(source)
    assert matrix.tolist() == [[100, 0, 50], [0, 101, 40], [0, 0, 1]]
    assert len(points) == 2


def test_parse_legacy_inputs_rejects_invalid_intrinsics(tmp_path):
    source = tmp_path / "calibration.py"
    source.write_text(
        "K = np.array([[0,0,50],[0,100,40],[0,0,1]])\n"
        "calibration_points = ["
        "{'u':1,'v':2,'true_X':3,'true_Z':4},"
        "{'u':2,'v':3,'true_X':4,'true_Z':5}]\n"
    )
    try:
        tool.parse_legacy_calibration(source)
    except ValueError as error:
        assert "positive fx/fy" in str(error)
    else:
        raise AssertionError("invalid legacy intrinsics were accepted")


def test_parse_runtime_camera_calls(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text(
        "cam4 = VideoObjectDetector(model, .5, K, None, 7, -43.48, -22.63, 260, "
        "'cam-001-ch4', lat, lon)\n"
    )
    cameras = tool.parse_runtime_camera_calls(source)
    assert cameras["ch4"]["pitch_deg"] == -43.48
    assert cameras["ch4"]["heading_deg"] == 260


def test_data_geometry_detects_collinearity():
    points = [
        {"u": index, "v": 2 * index, "true_X": index, "true_Z": 3 * index}
        for index in range(1, 8)
    ]
    geometry = tool.data_geometry(points, 100, 100)
    assert geometry["pixels"]["collinear_at_0_05_ratio"] is True
    assert geometry["local_xz_m"]["collinear_at_0_05_ratio"] is True


def test_fit_pitch_yaw_reproduces_zero_pose():
    matrix = np.asarray([[100, 0, 50], [0, 100, 50], [0, 0, 1]], dtype=float)
    # For pitch=yaw=0 and H=2, ray y=(v-cy)/fy, so these map exactly.
    points = [
        {"u": 50, "v": 150, "true_X": 0, "true_Z": 2},
        {"u": 100, "v": 150, "true_X": 1, "true_Z": 2},
        {"u": 0, "v": 150, "true_X": -1, "true_Z": 2},
    ]
    result = tool.fit_pitch_yaw(matrix, points, 2.0)
    assert result["success"] is True
    assert math.isclose(result["pitch_deg"], 0.0, abs_tol=1e-3)
    assert math.isclose(result["yaw_deg"], 0.0, abs_tol=1e-3)
    assert result["in_sample_reproduction_loss_m"]["mean"] < 1e-5


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "audit.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("writer overwrote immutable evidence")

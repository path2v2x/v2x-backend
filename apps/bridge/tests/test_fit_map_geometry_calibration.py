import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fit_map_geometry_calibration import (  # noqa: E402
    canonical_pixels,
    canonical_world_ref,
)


def test_crosswalk_edge_identity_is_direction_independent():
    forward = {
        "kind": "crosswalk_edge", "crosswalk_id": "crosswalk-1",
        "start_vertex": 0, "end_vertex": 3,
    }
    reverse = {**forward, "start_vertex": 3, "end_vertex": 0}
    assert canonical_world_ref(forward) == canonical_world_ref(reverse)


def test_polyline_pixel_identity_is_direction_independent():
    assert canonical_pixels([[1.0, 2.0], [3.0, 4.0]]) == canonical_pixels(
        [[3.0, 4.0], [1.0, 2.0]]
    )

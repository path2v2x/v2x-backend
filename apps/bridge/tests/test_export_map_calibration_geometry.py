import sys
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from export_map_calibration_geometry import (  # noqa: E402
    canonical_hash,
    lane_geometry_from_waypoints,
    opendrive_road_mark_ranges,
    split_crosswalk_polygons,
    stable_crosswalk_id,
)


class Location:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Value:
    def __init__(self, value):
        self.type = value


class Rotation:
    yaw = 0


class Transform:
    def __init__(self, x):
        self.location = Location(x, 0, 0)
        self.rotation = Rotation()


class Waypoint:
    def __init__(self, s, left, right, width=4.0):
        self.s = s
        self.transform = Transform(s)
        self.lane_width = width
        self.left_lane_marking = Value(left)
        self.right_lane_marking = Value(right)


class ExportMapCalibrationGeometryTests(unittest.TestCase):
    def test_canonical_hash_ignores_dictionary_key_order(self):
        self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))

    def test_crosswalk_list_splits_only_closed_polygons(self):
        values = [
            Location(0, 0, 0), Location(1, 0, 0),
            Location(1, 1, 0), Location(0, 0, 0),
            Location(2, 2, 0), Location(3, 2, 0),
            Location(3, 3, 0), Location(2, 2, 0),
        ]
        polygons = split_crosswalk_polygons(values)
        self.assertEqual(len(polygons), 2)
        self.assertEqual(polygons[1][0], [2.0, 2.0, 0.0])

    def test_open_crosswalk_list_fails_closed(self):
        with self.assertRaises(RuntimeError):
            split_crosswalk_polygons([
                Location(0, 0, 0), Location(1, 0, 0), Location(1, 1, 0)
            ])

    def test_crosswalk_identity_is_stable_across_start_and_direction(self):
        polygon = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0], [0, 0, 0]]
        rotated = [[2, 1, 0], [0, 1, 0], [0, 0, 0], [2, 0, 0], [2, 1, 0]]
        reversed_polygon = list(reversed(polygon))
        self.assertEqual(stable_crosswalk_id(polygon), stable_crosswalk_id(rotated))
        self.assertEqual(stable_crosswalk_id(polygon), stable_crosswalk_id(reversed_polygon))

    def test_lane_export_preserves_every_contiguous_road_mark_range(self):
        waypoints = [
            Waypoint(0, "Solid", "Broken"),
            Waypoint(1, "Solid", "Broken"),
            Waypoint(2, "Broken", "Broken"),
            Waypoint(3, "Broken", "Solid"),
            Waypoint(4, "Broken", "Solid"),
        ]
        lane = lane_geometry_from_waypoints((12, 0, -1), waypoints)
        ranges = [
            (item["side"], item["marking_type"], item["start_s_m"], item["end_s_m"])
            for item in lane["road_mark_segments"]
        ]
        self.assertEqual(ranges, [
            ("left", "Solid", 0.0, 1.0),
            ("left", "Broken", 2.0, 4.0),
            ("right", "Broken", 0.0, 2.0),
            ("right", "Solid", 3.0, 4.0),
        ])
        self.assertEqual(len({item["id"] for item in lane["road_mark_segments"]}), 4)
        self.assertNotIn("marking_types", lane)

    def test_exact_opendrive_road_mark_ranges_are_not_collapsed(self):
        ranges = opendrive_road_mark_ranges(b"""<OpenDRIVE>
<road id="7" length="20"><lanes><laneSection s="2"><right>
<lane id="-1"><roadMark sOffset="0" type="solid"/><roadMark sOffset="5" type="broken"/>
<roadMark sOffset="12" type="solid"/></lane></right></laneSection></lanes></road>
</OpenDRIVE>""")
        ordered = sorted(ranges, key=lambda item: item["start_s_m"])
        self.assertEqual(
            [(item["type"], item["start_s_m"], item["end_s_m"]) for item in ordered],
            [("solid", 2.0, 7.0), ("broken", 7.0, 14.0), ("solid", 14.0, 20.0)],
        )
        self.assertEqual(len({item["id"] for item in ranges}), 3)


if __name__ == "__main__":
    unittest.main()

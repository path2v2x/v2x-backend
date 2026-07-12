import sys
from pathlib import Path
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from export_map_calibration_geometry import (  # noqa: E402
    canonical_hash,
    split_crosswalk_polygons,
)


class Location:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


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


if __name__ == "__main__":
    unittest.main()

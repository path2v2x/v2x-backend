from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from verify_detection_persistence import (  # noqa: E402
    CAMERA_IDS,
    VerificationError,
    evaluate_persistence,
    fetch_detection_window,
    normalize_api_base_url,
)

NOW = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)


def item(camera_id, timestamp):
    media = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    decode = timestamp + timedelta(seconds=2.5)
    return {
        "device_id": f"cam-001-{camera_id}",
        "timestamp_schema_version": 2,
        "media_time_trusted": True,
        "media_clock_status": "matched",
        "media_clock": {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
        },
        "timestamp_utc": media,
        "media_timestamp_utc": media,
        "decode_received_at_utc": decode.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "decode_latency_ms": 2_500.0,
        "ingested_at_epoch": int(decode.timestamp()),
    }


class DetectionPersistenceTests(unittest.TestCase):
    @patch("verify_detection_persistence._fetch_json")
    def test_repeated_pagination_token_fails_closed(self, fetch_json):
        fetch_json.side_effect = [
            {"items": [], "next": "repeat-token"},
            {"items": [], "next": "repeat-token"},
        ]
        with self.assertRaisesRegex(VerificationError, "repeated a token"):
            fetch_detection_window(
                "https://api.example.test",
                NOW - timedelta(hours=24),
                NOW,
            )

    def test_accepts_near_full_day_and_recent_upload_for_every_camera(self):
        start = NOW - timedelta(hours=24)
        rows = []
        for camera_id in CAMERA_IDS:
            rows.extend(
                [
                    item(camera_id, start + timedelta(minutes=15)),
                    item(camera_id, NOW - timedelta(minutes=15)),
                ]
            )
        result = evaluate_persistence(rows, start, NOW, pages=2)
        self.assertTrue(result["gate_passed"])
        self.assertTrue(all(c["gate_passed"] for c in result["cameras"].values()))

    def test_one_short_camera_span_fails_the_global_gate(self):
        start = NOW - timedelta(hours=24)
        rows = []
        for camera_id in CAMERA_IDS:
            first = start + timedelta(minutes=15)
            if camera_id == "ch3":
                first = NOW - timedelta(hours=10)
            rows.extend([item(camera_id, first), item(camera_id, NOW)])
        result = evaluate_persistence(rows, start, NOW)
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["cameras"]["ch3"]["gate_passed"])

    def test_spoofed_or_inconsistent_clock_rows_are_rejected(self):
        start = NOW - timedelta(hours=24)
        rows = []
        for camera_id in CAMERA_IDS:
            first = item(camera_id, start + timedelta(minutes=15))
            last = item(camera_id, NOW)
            if camera_id == "ch2":
                first["media_clock"]["source"] = "decode_receipt"
            rows.extend([first, last])
        result = evaluate_persistence(rows, start, NOW)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["cameras"]["ch2"]["rejected_items"], 1)

    def test_signed_or_query_bearing_api_input_is_rejected(self):
        with self.assertRaises(VerificationError) as raised:
            normalize_api_base_url(
                "https://api.example.test?SessionToken=secret&signature=hidden"
            )
        message = str(raised.exception)
        self.assertNotIn("SessionToken", message)
        self.assertNotIn("secret", message)


if __name__ == "__main__":
    unittest.main()

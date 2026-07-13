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


_DEFAULT_EVENT_ID = object()


def item(camera_id, timestamp, event_id=_DEFAULT_EVENT_ID):
    media = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    decode = timestamp + timedelta(seconds=2.5)
    if event_id is _DEFAULT_EVENT_ID:
        event_id = f"{camera_id}-{int(timestamp.timestamp() * 1000)}"
    return {
        "event_id": event_id,
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
        self.assertIn("ch2 has 1 rejected item(s)", result["reasons"])

    def test_rejected_row_fails_even_when_camera_has_otherwise_valid_evidence(self):
        start = NOW - timedelta(hours=24)
        rows = []
        for camera_id in CAMERA_IDS:
            rows.extend(
                [
                    item(camera_id, start + timedelta(minutes=15)),
                    item(camera_id, NOW - timedelta(minutes=15)),
                ]
            )
        rejected = item("ch2", NOW - timedelta(hours=12), "rejected-ch2")
        rejected["media_time_trusted"] = False
        rows.append(rejected)

        result = evaluate_persistence(rows, start, NOW)

        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["cameras"]["ch2"]["gate_passed"])
        self.assertEqual(result["cameras"]["ch2"]["trusted_items"], 2)
        self.assertEqual(result["cameras"]["ch2"]["rejected_items"], 1)
        self.assertIn("ch2 has 1 rejected item(s)", result["reasons"])

    def test_unknown_device_row_fails_an_otherwise_valid_gate(self):
        start = NOW - timedelta(hours=24)
        rows = []
        for camera_id in CAMERA_IDS:
            rows.extend(
                [
                    item(camera_id, start + timedelta(minutes=15)),
                    item(camera_id, NOW - timedelta(minutes=15)),
                ]
            )
        unknown = item("ch1", NOW - timedelta(hours=12), "unknown-device")
        unknown["device_id"] = "cam-001-unknown"
        rows.append(unknown)

        result = evaluate_persistence(rows, start, NOW)

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["unknown_device_items"], 1)
        self.assertIn("1 item(s) have an unknown camera device", result["reasons"])

    def test_missing_and_blank_event_ids_fail_closed(self):
        start = NOW - timedelta(hours=24)
        for invalid_event_id in (None, "", "   "):
            with self.subTest(event_id=invalid_event_id):
                rows = []
                for camera_id in CAMERA_IDS:
                    rows.extend(
                        [
                            item(camera_id, start + timedelta(minutes=15)),
                            item(camera_id, NOW - timedelta(minutes=15)),
                        ]
                    )
                invalid = item(
                    "ch3", NOW - timedelta(hours=12), invalid_event_id
                )
                if invalid_event_id is None:
                    invalid.pop("event_id")
                rows.append(invalid)

                result = evaluate_persistence(rows, start, NOW)

                self.assertFalse(result["gate_passed"])
                self.assertEqual(result["invalid_event_id_items"], 1)
                self.assertEqual(result["cameras"]["ch3"]["rejected_items"], 1)
                self.assertIn(
                    "1 item(s) have a missing or blank event_id",
                    result["reasons"],
                )

    @patch("verify_detection_persistence._fetch_json")
    def test_duplicate_event_across_pages_never_counts(self, fetch_json):
        start = NOW - timedelta(hours=24)
        duplicate = item(
            "ch1", start + timedelta(minutes=15), "duplicate-across-pages"
        )
        first_page = [duplicate]
        second_page = [dict(duplicate)]
        for camera_id in CAMERA_IDS:
            if camera_id != "ch1":
                second_page.append(item(camera_id, start + timedelta(minutes=15)))
            second_page.append(item(camera_id, NOW - timedelta(minutes=15)))
        fetch_json.side_effect = [
            {"items": first_page, "next": "page-two"},
            {"items": second_page},
        ]

        rows, pages = fetch_detection_window(
            "https://api.example.test", start, NOW
        )
        result = evaluate_persistence(rows, start, NOW, pages=pages)
        reverse_result = evaluate_persistence(
            list(reversed(rows)), start, NOW, pages=pages
        )

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result, reverse_result)
        self.assertEqual(result["duplicate_event_ids"], 1)
        self.assertEqual(result["duplicate_event_id_items"], 2)
        self.assertEqual(result["cameras"]["ch1"]["trusted_items"], 1)
        self.assertEqual(result["cameras"]["ch1"]["rejected_items"], 2)

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

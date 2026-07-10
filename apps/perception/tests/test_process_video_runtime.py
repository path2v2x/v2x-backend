import sys
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import numpy as np


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from live_capture import bounded_frame_identity  # noqa: E402
from process_video import (  # noqa: E402
    FrameBroadcaster,
    MultiCameraPipeline,
    VideoObjectDetector,
    attach_media_clock_metadata,
    assess_media_clock,
    records_ready_for_upload,
)


class FrameIdentityTests(unittest.TestCase):
    def test_sparse_identity_is_stable_for_copy_and_changes_for_content(self):
        frame = np.zeros((128, 192, 3), dtype=np.uint8)
        identity = bounded_frame_identity(frame)
        self.assertEqual(identity, bounded_frame_identity(frame.copy()))

        changed = frame.copy()
        changed[61:68, 93:100] = 255
        self.assertNotEqual(identity, bounded_frame_identity(changed))


class FrameBroadcasterTests(unittest.TestCase):
    def setUp(self):
        self.broadcaster = FrameBroadcaster(["ch1", "ch2"], stale_seconds=1.0)
        self.frame = np.zeros((8, 8, 3), dtype=np.uint8)

    def test_health_requires_a_fresh_real_frame_from_every_camera(self):
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])

        self.broadcaster.publish("ch1", self.frame, "2026-07-10T00:00:00.000Z")
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])

        self.broadcaster.publish("ch2", self.frame, "2026-07-10T00:00:00.000Z")
        health = self.broadcaster.snapshot_health()
        self.assertTrue(health["ready"])
        self.assertEqual(health["status"], "ok")

    def test_stale_and_reconnecting_states_are_visible(self):
        self.broadcaster.publish("ch1", self.frame)
        last_frame = self.broadcaster.camera_health["ch1"]["last_frame_monotonic"]
        stale = self.broadcaster.snapshot_health(now_monotonic=last_frame + 1.1)
        self.assertEqual(stale["cameras"]["ch1"]["state"], "stale")
        self.assertFalse(stale["cameras"]["ch1"]["fresh"])

        self.broadcaster.mark_reconnecting("ch1", "frame read failed", 3)
        reconnecting = self.broadcaster.snapshot_health()
        self.assertEqual(reconnecting["cameras"]["ch1"]["state"], "reconnecting")
        self.assertEqual(reconnecting["cameras"]["ch1"]["reconnect_attempts"], 3)

    def test_last_real_frame_does_not_erase_newer_reconnect_state(self):
        self.broadcaster.mark_reconnecting("ch1", "frame read failed", 1)
        self.broadcaster.publish(
            "ch1",
            self.frame,
            "2026-07-10T00:00:00.000Z",
            source_monotonic=100.0,
        )
        health = self.broadcaster.snapshot_health(now_monotonic=100.1)
        self.assertEqual(health["cameras"]["ch1"]["state"], "reconnecting")
        self.assertTrue(health["cameras"]["ch1"]["fresh"])
        self.assertFalse(health["ready"])
        frame, count = self.broadcaster.wait_for_frame("ch1", -1, timeout=0.0)
        self.assertIsNone(frame)
        self.assertEqual(count, -1)

    def test_health_age_uses_capture_time_not_inference_completion_time(self):
        self.broadcaster.mark_connected("ch1")
        self.broadcaster.publish(
            "ch1",
            self.frame,
            "2026-07-10T00:00:00.000Z",
            source_monotonic=100.0,
        )
        health = self.broadcaster.snapshot_health(now_monotonic=101.1)
        self.assertEqual(health["cameras"]["ch1"]["state"], "stale")
        self.assertFalse(health["cameras"]["ch1"]["fresh"])

    def test_public_health_never_exposes_signed_source_errors(self):
        self.broadcaster.mark_reconnecting(
            "ch1",
            "failed https://video.example/live.m3u8?SessionToken=secret-value",
            2,
        )
        health = self.broadcaster.snapshot_health()
        last_error = health["cameras"]["ch1"]["last_error"]
        self.assertIn("details redacted", last_error)
        self.assertNotIn("https://", last_error)
        self.assertNotIn("video.example", last_error)
        self.assertNotIn("SessionToken", last_error)
        self.assertNotIn("secret-value", last_error)

        # Snapshot sanitization is a second boundary even if legacy/internal
        # state somehow contains an unsanitized value.
        self.broadcaster.camera_health["ch1"]["last_error"] = (
            "https://other.example/hls?token=another-secret"
        )
        last_error = self.broadcaster.snapshot_health()["cameras"]["ch1"][
            "last_error"
        ]
        self.assertNotIn("other.example", last_error)
        self.assertNotIn("another-secret", last_error)

    def test_latest_detection_exposes_media_clock_for_correlation(self):
        media_clock = {
            "source": "hls_ext_x_program_date_time",
            "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
            "position_milliseconds": 250.5,
        }
        self.broadcaster.publish_detections(
            "ch1",
            [{
                "timestamp_utc": "2026-07-10T03:57:27.000Z",
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": media_clock,
            }],
        )
        detection = self.broadcaster.snapshot_detections()["cameras"]["ch1"][
            "detections"
        ][0]
        self.assertEqual(
            detection["media_timestamp_utc"],
            "2026-07-10T03:57:23.388Z",
        )
        self.assertEqual(detection["media_clock"], media_clock)


class MediaClockPersistenceTests(unittest.TestCase):
    def test_media_time_becomes_replay_index_and_receipt_is_preserved(self):
        record = {
            "event_id": "event-1",
            "timestamp_utc": "2026-07-10T03:57:27.000Z",
            "ingested_at_epoch": 1_783_655_847.0,
        }
        attach_media_clock_metadata(
            [record],
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
                    "anchor_fragment_id": "frag-123",
                    "position_milliseconds": 250.5,
                    "signed_url": "https://example.invalid/?SessionToken=secret",
                },
            },
        )

        self.assertEqual(record["timestamp_utc"], "2026-07-10T03:57:23.388Z")
        self.assertEqual(
            record["decode_received_at_utc"], "2026-07-10T03:57:27.000Z"
        )
        self.assertEqual(record["decode_received_at_epoch"], 1_783_655_847.0)
        self.assertEqual(
            record["media_timestamp_utc"], "2026-07-10T03:57:23.388Z"
        )
        self.assertEqual(
            record["ts_event"], "2026-07-10T03:57:23.388Z#event-1"
        )
        self.assertEqual(record["media_clock_status"], "matched")
        self.assertNotIn("signed_url", record["media_clock"])

    def test_missing_exact_match_is_marked_unavailable(self):
        record = {"timestamp_utc": "2026-07-10T03:57:27.000Z"}
        attach_media_clock_metadata([record], None)
        self.assertEqual(record["media_clock_status"], "unavailable")
        self.assertNotIn("media_timestamp_utc", record)

    def test_wrong_schema_or_implausible_latency_is_not_trusted(self):
        base = {
            "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "schema_version": 1,
                "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
                "position_milliseconds": 250.0,
            },
        }
        wrong_schema = copy.deepcopy(base)
        wrong_schema["media_clock"]["schema_version"] = 2
        self.assertEqual(
            assess_media_clock(wrong_schema, 1_783_655_847.0)["status"],
            "unsupported_schema",
        )
        implausible = assess_media_clock(
            base,
            1_783_655_847.0 + 121.0,
        )
        self.assertFalse(implausible["trusted"])
        self.assertEqual(implausible["status"], "latency_out_of_bounds")

    def test_anchor_position_must_reconstruct_media_timestamp(self):
        assessment = assess_media_clock(
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "anchor_program_date_time_utc": "2026-07-10T03:57:20.000Z",
                    "position_milliseconds": 100.0,
                },
            },
            1_783_655_847.0,
        )
        self.assertFalse(assessment["trusted"])
        self.assertEqual(assessment["status"], "inconsistent_provenance")

    def test_live_uploads_fail_closed_without_trusted_media_schema(self):
        trusted = {
            "event_id": "trusted",
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
        }
        unavailable = {
            "event_id": "unavailable",
            "timestamp_schema_version": 2,
            "media_time_trusted": False,
        }
        legacy = {"event_id": "legacy"}
        records = [trusted, unavailable, legacy]
        self.assertEqual(records_ready_for_upload(records, True), [trusted])
        self.assertEqual(records_ready_for_upload(records, False), records)


class RunScopedIdentityTests(unittest.TestCase):
    @staticmethod
    def pipeline(run_id):
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.perception_run_id = run_id
        pipeline.perception_run_prefix = run_id.replace("-", "")[:8]
        return pipeline

    @staticmethod
    def detection(camera="ch1", confidence=0.8, media_timestamp="first"):
        return {
            "event_id": f"event-{camera}",
            "object_id": f"car_{camera}_7",
            "object_type": "car",
            "confidence_score": confidence,
            "gps_location": {"latitude": 37.0, "longitude": -122.0},
            "device_id": camera,
            "track_id": 7,
            "embedding": None,
            "timestamp_utc": media_timestamp,
            "media_timestamp_utc": media_timestamp,
            "media_clock": {"source": "hls_ext_x_program_date_time"},
            "camera_data": {"bifocal_metadata": {"bbox": {}}},
        }

    def test_same_local_track_in_different_runs_gets_different_global_id(self):
        run_one = "123e4567-e89b-12d3-a456-426614174000"
        run_two = "abcdef01-e89b-12d3-a456-426614174000"
        first = self.pipeline(run_one).deduplicate(
            [self.detection()], 1_000.0
        )[0]
        second = self.pipeline(run_two).deduplicate(
            [self.detection()], 1_000.0
        )[0]

        self.assertEqual(first["object_id"], "global_car_123e4567_1")
        self.assertEqual(second["object_id"], "global_car_abcdef01_1")
        self.assertNotEqual(first["object_id"], second["object_id"])
        self.assertEqual(first["perception_run_id"], run_one)
        self.assertEqual(first["track_id"], 7)

    def test_cross_camera_winner_keeps_one_consistent_media_observation(self):
        run_id = "123e4567-e89b-12d3-a456-426614174000"
        older = self.detection("ch1", 0.7, "older")
        winner = self.detection("ch2", 0.9, "winner")
        result = self.pipeline(run_id).deduplicate(
            [copy.deepcopy(older), copy.deepcopy(winner)], 1_000.0
        )[0]

        self.assertEqual(result["device_id"], "ch2")
        self.assertEqual(result["timestamp_utc"], "winner")
        self.assertEqual(result["media_timestamp_utc"], "winner")
        self.assertEqual(result["event_id"], "event-ch2")

    def test_distinct_vehicles_seven_meters_apart_are_not_merged(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        second = self.detection("ch2", 0.9, "2026-07-10T00:00:00.100Z")
        second["gps_location"]["latitude"] += 7.0 / 111_320.0
        result = pipeline.deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)
        self.assertNotEqual(result[0]["object_id"], result[1]["object_id"])

    def test_cross_camera_observations_outside_media_window_are_not_merged(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        second = self.detection("ch2", 0.9, "2026-07-10T00:00:04.000Z")
        result = pipeline.deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)

    def test_stale_tracks_and_local_aliases_are_pruned(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        pipeline.deduplicate([self.detection()], 1_000.0)
        self.assertTrue(pipeline.global_tracks)
        pipeline.deduplicate([], 1_000.0 + pipeline.TRACK_MAX_IDLE_SEC + 0.1)
        self.assertEqual(pipeline.global_tracks, {})
        self.assertEqual(pipeline.local_to_global, {})


class BatchUploadTests(unittest.TestCase):
    def setUp(self):
        self.detector = object.__new__(VideoObjectDetector)
        self.detector.v2x_endpoint = "https://example.invalid/detections"
        self.records = [{"event_id": "one"}, {"event_id": "two"}]

    @patch("process_video.requests.post")
    def test_batch_upload_returns_true_for_complete_item_level_success(self, post):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "ok": True,
            "inserted": 2,
            "failed": 0,
            "results": [{"ok": True}, {"ok": True}],
        }
        post.return_value = response
        self.assertTrue(self.detector.upload_batch(self.records))

    @patch("process_video.requests.post")
    def test_batch_upload_returns_false_for_partial_http_200(self, post):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "ok": False,
            "inserted": 1,
            "failed": 1,
            "results": [{"ok": True}, {"ok": False}],
        }
        post.return_value = response
        self.assertFalse(self.detector.upload_batch(self.records))


class LivePipelineTimestampTests(unittest.TestCase):
    class StopPipeline(Exception):
        pass

    class FakeModel:
        def track(self, *_args, **_kwargs):
            return [object()]

    class FakeDetector:
        def __init__(self):
            self.model = LivePipelineTimestampTests.FakeModel()
            self.conf = 0.4
            self.event_times = []

        def extract_detections(self, _result, _frame_count):
            return []

        def compute_3d_detections(self, _detections, timestamp, epoch):
            self.event_times.append((timestamp, epoch))
            return []

        def draw_detections_3d(self, frame, _detections):
            return frame

    class FakeReader:
        def __init__(self, **_kwargs):
            self.snapshot_calls = 0

        def start(self):
            return None

        def snapshot(self, _after_sequence):
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return {
                    "sequence": 1,
                    "frame": np.zeros((8, 8, 3), dtype=np.uint8),
                    "source_epoch": 1_000.25,
                    "source_monotonic": 500.0,
                }
            raise LivePipelineTimestampTests.StopPipeline()

        def request_stop(self):
            return None

        def join(self, _timeout):
            return None

    @patch("process_video.LiveStreamReader", FakeReader)
    def test_pipeline_uses_per_camera_capture_time_and_source_age(self):
        detector = self.FakeDetector()
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [detector]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        broadcaster = FrameBroadcaster(["ch1"], stale_seconds=1.0)

        with self.assertRaises(self.StopPipeline):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=broadcaster,
                camera_ids=["ch1"],
            )

        self.assertEqual(detector.event_times[0][1], 1_000.25)
        health = broadcaster.snapshot_health(now_monotonic=500.1)
        self.assertEqual(
            health["cameras"]["ch1"]["source_updated_at"],
            detector.event_times[0][0],
        )
        self.assertAlmostEqual(health["cameras"]["ch1"]["age_seconds"], 0.1)


if __name__ == "__main__":
    unittest.main()

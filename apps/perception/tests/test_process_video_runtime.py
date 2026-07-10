import sys
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

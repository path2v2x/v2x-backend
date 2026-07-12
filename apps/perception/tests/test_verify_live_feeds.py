import contextlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from verify_live_feeds import (  # noqa: E402
    CAMERA_IDS,
    DEFAULT_CAPTURE_PROGRESS_TIMEOUT_SECONDS,
    DEFAULT_INFERENCE_PROGRESS_TIMEOUT_SECONDS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    VerificationError,
    main,
    normalize_base_url,
    verify_live_feeds,
)


class LiveFeedVerifierTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "health_samples": 0,
            "event_samples": 0,
            "identical_frames": False,
            "advance_timestamps": True,
            "inference_hold_samples": 0,
            "capture_hold_samples": 0,
            "decode_latency_ms": 500.0,
            "media_time_trusted": True,
            "base_time": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            @staticmethod
            def timestamp(sample_number):
                offset = sample_number - 1 if state["advance_timestamps"] else 0
                return (state["base_time"] + timedelta(seconds=offset)).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")

            def send_json(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    state["health_samples"] += 1
                    sample = state["health_samples"]
                    capture_sample = max(
                        1,
                        sample - state["capture_hold_samples"],
                    )
                    timestamp = self.timestamp(capture_sample)
                    self.send_json({
                        "status": "ok",
                        "ready": True,
                        "cameras": {
                            camera_id: {
                                "state": "streaming",
                                "fresh": True,
                                "source_updated_at": timestamp,
                                "frame_count": 100 + capture_sample,
                                "inference_frame_count": 100 + max(
                                    1,
                                    sample - state["inference_hold_samples"],
                                ),
                                "inference_fresh": True,
                                "media_clock_status": "matched",
                                "media_time_trusted": state[
                                    "media_time_trusted"
                                ],
                                "decode_latency_ms": state[
                                    "decode_latency_ms"
                                ],
                            }
                            for camera_id in CAMERA_IDS
                        },
                    })
                    return

                if self.path == "/detections/latest":
                    state["event_samples"] += 1
                    sample = state["event_samples"]
                    timestamp = self.timestamp(max(
                        1,
                        sample - state["inference_hold_samples"],
                    ))
                    self.send_json({
                        "cameras": {
                            camera_id: {"updated_at": timestamp}
                            for camera_id in CAMERA_IDS
                        }
                    })
                    return

                if self.path.startswith("/streams/") and self.path.endswith(
                    ".mjpg"
                ):
                    camera_id = self.path.rsplit("/", 1)[1].split(".", 1)[0]
                    first = b"\xff\xd8first-" + camera_id.encode() + b"\xff\xd9"
                    second_payload = (
                        b"first" if state["identical_frames"] else b"second"
                    )
                    second = (
                        b"\xff\xd8"
                        + second_payload
                        + b"-"
                        + camera_id.encode()
                        + b"\xff\xd9"
                    )
                    body = (
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + first
                        + b"\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + second
                        + b"\r\n--frame--\r\n"
                    )
                    self.send_response(200)
                    self.send_header(
                        "content-type",
                        "multipart/x-mixed-replace; boundary=frame",
                    )
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_verifies_advancing_times_and_two_changed_frames_per_camera(self):
        self.assertEqual(DEFAULT_SAMPLE_INTERVAL_SECONDS, 3.0)
        self.assertEqual(DEFAULT_CAPTURE_PROGRESS_TIMEOUT_SECONDS, 10.0)
        self.assertEqual(DEFAULT_INFERENCE_PROGRESS_TIMEOUT_SECONDS, 10.0)
        result = verify_live_feeds(
            self.base_url,
            sample_interval_seconds=0,
            max_age_seconds=10,
            timeout_seconds=2,
        )
        self.assertEqual(set(result), set(CAMERA_IDS))
        for camera_id in CAMERA_IDS:
            camera = result[camera_id]
            self.assertEqual(len(camera["capture_times"]), 2)
            self.assertEqual(len(camera["event_times"]), 2)
            self.assertEqual(len(camera["inference_frame_counts"]), 2)
            self.assertEqual(len(camera["frame_sha256"]), 2)
            self.assertNotEqual(
                camera["frame_sha256"][0], camera["frame_sha256"][1]
            )

    def test_identical_mjpeg_frames_fail_the_whole_gate(self):
        self.state["identical_frames"] = True
        with self.assertRaisesRegex(
            VerificationError, "identical content"
        ):
            verify_live_feeds(
                self.base_url,
                sample_interval_seconds=0,
                max_age_seconds=10,
                timeout_seconds=2,
            )

    def test_non_advancing_capture_and_event_times_fail(self):
        self.state["advance_timestamps"] = False
        with self.assertRaisesRegex(VerificationError, "did not advance"):
            verify_live_feeds(
                self.base_url,
                sample_interval_seconds=0,
                max_age_seconds=10,
                timeout_seconds=2,
                capture_progress_timeout_seconds=0.01,
                inference_progress_timeout_seconds=0.01,
                inference_poll_interval_seconds=0.001,
            )

    def test_polls_through_a_phase_aliased_inference_sample(self):
        self.state["inference_hold_samples"] = 2
        result = verify_live_feeds(
            self.base_url,
            sample_interval_seconds=0,
            max_age_seconds=10,
            timeout_seconds=2,
            inference_progress_timeout_seconds=1,
            inference_poll_interval_seconds=0,
        )
        for camera in result.values():
            self.assertGreater(
                camera["inference_frame_counts"][1],
                camera["inference_frame_counts"][0],
            )

    def test_polls_through_a_phase_aliased_capture_sample(self):
        self.state["capture_hold_samples"] = 2
        result = verify_live_feeds(
            self.base_url,
            sample_interval_seconds=0,
            max_age_seconds=10,
            timeout_seconds=2,
            capture_progress_timeout_seconds=1,
            inference_progress_timeout_seconds=1,
            inference_poll_interval_seconds=0,
        )
        for camera in result.values():
            self.assertGreater(camera["capture_times"][1], camera["capture_times"][0])

    def test_capture_progress_deadline_fails_closed(self):
        self.state["capture_hold_samples"] = 1_000_000
        with self.assertRaisesRegex(
            VerificationError, "capture did not advance within deadline"
        ):
            verify_live_feeds(
                self.base_url,
                sample_interval_seconds=0,
                max_age_seconds=10,
                timeout_seconds=2,
                capture_progress_timeout_seconds=0.005,
                inference_progress_timeout_seconds=1,
                inference_poll_interval_seconds=0.001,
            )

    def test_inference_progress_deadline_fails_closed(self):
        self.state["inference_hold_samples"] = 1_000_000
        with self.assertRaisesRegex(
            VerificationError, "inference did not advance within deadline"
        ):
            verify_live_feeds(
                self.base_url,
                sample_interval_seconds=0,
                max_age_seconds=10,
                timeout_seconds=2,
                inference_progress_timeout_seconds=0.005,
                inference_poll_interval_seconds=0.001,
            )

    def test_untrusted_clock_or_out_of_bounds_latency_fails(self):
        for field, value, message in (
            ("media_time_trusted", False, "media clock is not trusted"),
            ("decode_latency_ms", -1000.01, "decode latency is out of bounds"),
            ("decode_latency_ms", 10000.01, "decode latency is out of bounds"),
        ):
            with self.subTest(field=field, value=value):
                self.state[field] = value
                with self.assertRaisesRegex(VerificationError, message):
                    verify_live_feeds(
                        self.base_url,
                        sample_interval_seconds=0,
                        max_age_seconds=10,
                        timeout_seconds=2,
                    )
                self.state[field] = (
                    True if field == "media_time_trusted" else 500.0
                )

    def test_decode_latency_boundaries_are_inclusive(self):
        for value in (-1000.0, 10000.0):
            with self.subTest(value=value):
                self.state["decode_latency_ms"] = value
                result = verify_live_feeds(
                    self.base_url,
                    sample_interval_seconds=0,
                    max_age_seconds=10,
                    timeout_seconds=2,
                )
                self.assertEqual(set(result), set(CAMERA_IDS))

    def test_query_or_signed_input_is_rejected_without_echoing_it(self):
        secret_url = (
            "https://video.example/live?SessionToken=top-secret&"
            "X-Amz-Signature=signature-secret"
        )
        with self.assertRaises(VerificationError) as raised:
            normalize_base_url(secret_url)
        message = str(raised.exception)
        for forbidden in (
            "video.example",
            "SessionToken",
            "top-secret",
            "X-Amz-Signature",
            "signature-secret",
        ):
            self.assertNotIn(forbidden, message)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main([secret_url]), 1)
        emitted = stderr.getvalue()
        for forbidden in ("video.example", "top-secret", "signature-secret"):
            self.assertNotIn(forbidden, emitted)


if __name__ == "__main__":
    unittest.main()

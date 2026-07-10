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
                    timestamp = self.timestamp(sample)
                    self.send_json({
                        "status": "ok",
                        "ready": True,
                        "cameras": {
                            camera_id: {
                                "state": "streaming",
                                "fresh": True,
                                "source_updated_at": timestamp,
                                "frame_count": 100 + sample,
                            }
                            for camera_id in CAMERA_IDS
                        },
                    })
                    return

                if self.path == "/detections/latest":
                    state["event_samples"] += 1
                    sample = state["event_samples"]
                    timestamp = self.timestamp(sample)
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
            )

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

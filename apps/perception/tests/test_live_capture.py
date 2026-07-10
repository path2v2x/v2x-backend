import sys
from pathlib import Path
import threading
import time
import unittest


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from live_capture import LiveStreamReader  # noqa: E402
from runtime_health import StreamRecovery  # noqa: E402


class ScriptedCapture:
    def __init__(self, frames, block_after_frames=None):
        self.frames = list(frames)
        self.block_after_frames = block_after_frames
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.frames:
            return True, self.frames.pop(0)
        if self.block_after_frames is not None:
            self.block_after_frames.wait(1.0)
        return False, None

    def release(self):
        self.released = True


class LiveStreamReaderTests(unittest.TestCase):
    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_reconnect_renews_source_and_delivers_a_new_sequence(self):
        source_calls = []
        capture_sources = []
        states = []

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture_sources.append(source)
            return ScriptedCapture([f"frame-{len(capture_sources)}"])

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            second = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(second)
            self.assertGreater(second["sequence"], first["sequence"])
            self.assertGreaterEqual(len(source_calls), 2)
            self.assertNotEqual(capture_sources[0], capture_sources[1])
            self.assertTrue(any(event["state"] == "reconnecting" for event in states))
        finally:
            reader.stop(timeout=2.0)

    def test_snapshot_never_relabels_the_same_frame_as_new(self):
        release_read = threading.Event()
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["frame-1"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNone(reader.snapshot(first["sequence"]))
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_blocked_camera_does_not_delay_a_healthy_camera(self):
        release_blocked = threading.Event()
        release_healthy = threading.Event()
        blocked = LiveStreamReader(
            source_factory=lambda: "blocked-session",
            capture_factory=lambda _source: ScriptedCapture(
                [], block_after_frames=release_blocked
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        healthy = LiveStreamReader(
            source_factory=lambda: "healthy-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["healthy-frame"], block_after_frames=release_healthy
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        blocked.start()
        healthy.start()
        try:
            frame = healthy.wait_for_frame(0, timeout=0.5)
            self.assertIsNotNone(frame)
            self.assertEqual(frame["frame"], "healthy-frame")
            self.assertIsNone(blocked.snapshot(0))
        finally:
            release_blocked.set()
            release_healthy.set()
            blocked.stop(timeout=2.0)
            healthy.stop(timeout=2.0)

    def test_identical_content_after_reconnect_does_not_advance_freshness(self):
        release_reads = [threading.Event() for _ in range(3)]
        captures = []
        states = []
        wall_time_calls = []

        def source_factory():
            return f"renewed-session-{len(captures) + 1}"

        def capture_factory(_source):
            index = len(captures)
            frame = "terminal-frame" if index < 2 else "changed-frame"
            capture = ScriptedCapture(
                [frame], block_after_frames=release_reads[index]
            )
            captures.append(capture)
            return capture

        event_times = iter((100.0, 200.0))

        def wall_time():
            value = next(event_times)
            wall_time_calls.append(value)
            return value

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            wall_time=wall_time,
            duplicate_frame_limit=5,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertEqual(first["source_epoch"], 100.0)

            release_reads[0].set()
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            # The first frame from the renewed session is byte-identical to the
            # terminal frame from the prior session. It must not get a sequence
            # number or consume a new event timestamp.
            self.assertIsNone(reader.snapshot(first["sequence"]))
            self.assertEqual(wall_time_calls, [100.0])

            release_reads[1].set()
            changed = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(changed)
            self.assertEqual(changed["sequence"], first["sequence"] + 1)
            self.assertEqual(changed["source_epoch"], 200.0)
            self.assertEqual(wall_time_calls, [100.0, 200.0])
            self.assertGreaterEqual(
                sum(event["state"] == "reconnecting" for event in states), 2
            )
        finally:
            for event in release_reads:
                event.set()
            reader.stop(timeout=2.0)

    def test_terminal_repeats_force_reconnect_without_new_sequence(self):
        release_changed = threading.Event()
        captures = []
        states = []

        def capture_factory(_source):
            if not captures:
                capture = ScriptedCapture(["same", "same", "same"])
            else:
                capture = ScriptedCapture(
                    ["different"], block_after_frames=release_changed
                )
            captures.append(capture)
            return capture

        reader = LiveStreamReader(
            source_factory=lambda: "renewed-session",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            duplicate_frame_limit=2,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            changed = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(changed)
            self.assertEqual(changed["sequence"], first["sequence"] + 1)
            reconnect_errors = [
                event["error"]
                for event in states
                if event["state"] == "reconnecting"
            ]
            self.assertIn(
                "RuntimeError: repeated frame content", reconnect_errors
            )
        finally:
            release_changed.set()
            reader.stop(timeout=2.0)

    def test_source_exception_is_sanitized_before_callback(self):
        state_event = threading.Event()
        states = []

        def state_callback(**event):
            states.append(event)
            state_event.set()

        def source_factory():
            raise RuntimeError(
                "expired https://video.example/live.m3u8?"
                "SessionToken=secret&X-Amz-Signature=also-secret"
            )

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=state_callback,
        )
        reader.start()
        try:
            self.assertTrue(state_event.wait(1.0))
            error = states[0]["error"]
            self.assertIn("details redacted", error)
            for forbidden in (
                "https://",
                "video.example",
                "SessionToken",
                "secret",
                "X-Amz-Signature",
            ):
                self.assertNotIn(forbidden, error)
        finally:
            reader.stop(timeout=2.0)


if __name__ == "__main__":
    unittest.main()

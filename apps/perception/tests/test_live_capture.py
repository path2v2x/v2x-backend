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


class FakeMediaClock:
    def __init__(self, timestamp="2026-07-10T03:57:23.388Z"):
        self.positions = []
        self.timestamp = timestamp

    def metadata_at(self, position_milliseconds):
        self.positions.append(position_milliseconds)
        return {
            "media_timestamp_utc": self.timestamp,
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "position_milliseconds": position_milliseconds,
            },
        }


class SequencedMediaClock:
    def __init__(self, timestamps):
        self.timestamps = list(timestamps)

    def metadata_at(self, position_milliseconds):
        timestamp = self.timestamps.pop(0) if len(self.timestamps) > 1 else self.timestamps[0]
        return {
            "media_timestamp_utc": timestamp,
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "position_milliseconds": position_milliseconds,
            },
        }


class GatedPositionCapture:
    def __init__(self, frames, positions, next_frame_gate):
        self.frames = list(frames)
        self.positions = list(positions)
        self.next_frame_gate = next_frame_gate
        self.index = 0
        self.current_position = None
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.index >= len(self.frames):
            self.next_frame_gate.wait(1.0)
            return False, None
        if self.index > 0:
            self.next_frame_gate.wait(1.0)
        frame = self.frames[self.index]
        self.current_position = self.positions[self.index]
        self.index += 1
        return True, frame

    def get(self, _property):
        return self.current_position

    def release(self):
        self.released = True


class ContinuousCapture:
    def __init__(self, prefix):
        self.prefix = prefix
        self.count = 0
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        if self.released:
            return False, None
        time.sleep(0.002)
        self.count += 1
        return True, f"{self.prefix}-frame-{self.count}"

    def get(self, _property):
        return self.count * 50.0

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

    def test_proactive_renewal_rotates_source_without_reconnecting_state(self):
        source_calls = []
        states = []
        monotonic_value = [0.0]

        def monotonic():
            value = monotonic_value[0]
            monotonic_value[0] += 1.0
            return value

        def source_factory():
            source_calls.append(f"signed-session-{len(source_calls) + 1}")
            return source_calls[-1]

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=lambda source: ScriptedCapture(
                [f"{source}-frame-1", f"{source}-frame-2"]
            ),
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            monotonic=monotonic,
            connection_max_age_seconds=2.0,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            self.assertGreaterEqual(len(source_calls), 2)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
            self.assertEqual(sum(
                event["state"] == "connected" for event in states
            ), 1)
        finally:
            reader.stop(timeout=2.0)

    def test_proactive_renewal_age_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "must be positive"):
            LiveStreamReader(
                source_factory=lambda: "signed-session",
                capture_factory=lambda _source: ScriptedCapture([]),
                recovery=StreamRecovery(0.1, 0.1),
                connection_max_age_seconds=0,
            )

    def test_proactive_handover_drains_replacement_until_clock_is_ready(self):
        source_calls = []
        captures = []
        states = []
        release_replacement_clock = threading.Event()

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(source, *_args):
            if source == "signed-session-2":
                release_replacement_clock.wait(1.0)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
            connection_max_age_seconds=0.15,
            connection_renewal_lead_seconds=0.05,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            self.assertTrue(self.wait_until(lambda: captures[1].count >= 5))
            release_replacement_clock.set()
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            snapshot = reader.snapshot(0)
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(snapshot["media_clock"])
            replacement_frame_number = int(
                snapshot["frame"].rsplit("-", 1)[1]
            )
            self.assertGreaterEqual(replacement_frame_number, 5)
            self.assertTrue(captures[0].released)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            release_replacement_clock.set()
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

    def test_snapshot_keeps_media_time_separate_from_decode_receipt_time(self):
        release_read = threading.Event()
        clock = FakeMediaClock()
        capture = ScriptedCapture(
            ["frame-1"], block_after_frames=release_read
        )
        capture.get = lambda _property: 250.5
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            wall_time=lambda: 1_000.25,
            media_clock_factory=(
                lambda _source, _frame, _position, _identity: clock
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["source_epoch"], 1_000.25)
            self.assertEqual(
                snapshot["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.388Z",
            )
            self.assertEqual(clock.positions, [250.5])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_invalid_clock_mapping_is_discarded_and_reanchored(self):
        capture = ContinuousCapture("clock-validation")
        clock_calls = []

        def media_clock_factory(*_args):
            timestamp = (
                "2026-07-10T03:57:30.000Z"
                if not clock_calls
                else "2026-07-10T03:57:23.388Z"
            )
            clock_calls.append(timestamp)
            return FakeMediaClock(timestamp=timestamp)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"]
                == "2026-07-10T03:57:23.388Z"
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
            media_clock_invalid_grace_seconds=0.0,
        )
        reader.start()
        try:
            sequence = 0
            snapshot = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                candidate = reader.wait_for_frame(sequence, timeout=0.2)
                if candidate is None:
                    continue
                sequence = candidate["sequence"]
                if candidate.get("media_clock") is not None:
                    snapshot = candidate
                    break
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                snapshot["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.388Z",
            )
            self.assertGreaterEqual(len(clock_calls), 2)
        finally:
            reader.stop(timeout=2.0)

    def test_reanchor_retains_last_trusted_frame_until_valid_clock(self):
        capture = ContinuousCapture("clock-hold")
        reanchor_started = threading.Event()
        release_reanchor = threading.Event()
        calls = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def media_clock_factory(*_args):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return SequencedMediaClock([valid, invalid])
            reanchor_started.set()
            release_reanchor.wait(1.0)
            return FakeMediaClock(timestamp=valid)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
            media_clock_invalid_grace_seconds=0.0,
        )
        reader.start()
        try:
            sequence = 0
            trusted = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                candidate = reader.wait_for_frame(sequence, timeout=0.2)
                if candidate is None:
                    continue
                sequence = candidate["sequence"]
                if candidate.get("media_clock") is not None:
                    trusted = candidate
                    break
            self.assertIsNotNone(trusted)
            self.assertTrue(reanchor_started.wait(1.0))
            time.sleep(0.05)
            self.assertIsNone(reader.snapshot(trusted["sequence"]))

            release_reanchor.set()
            replacement = reader.wait_for_frame(
                trusted["sequence"], timeout=2.0
            )
            self.assertIsNotNone(replacement)
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"], valid
            )
        finally:
            release_reanchor.set()
            reader.stop(timeout=2.0)

    def test_transient_invalid_clock_is_discarded_without_reanchor(self):
        capture = ContinuousCapture("clock-glitch")
        calls = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def media_clock_factory(*_args):
            calls.append(1)
            return SequencedMediaClock([valid, invalid, valid])

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_invalid_grace_seconds=1.0,
        )
        reader.start()
        try:
            sequence = 0
            trusted_sequences = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(trusted_sequences) < 2:
                snapshot = reader.wait_for_frame(sequence, timeout=0.2)
                if snapshot is None:
                    continue
                sequence = snapshot["sequence"]
                if snapshot.get("media_clock") is not None:
                    trusted_sequences.append(sequence)
            self.assertEqual(len(trusted_sequences), 2)
            self.assertEqual(calls, [1])
            self.assertGreater(trusted_sequences[1], trusted_sequences[0])
        finally:
            reader.stop(timeout=2.0)

    def test_persistent_invalid_clock_hot_prepares_a_fresh_session(self):
        source_calls = []
        captures = []
        states = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(source, *_args):
            if source == "signed-session-1":
                return SequencedMediaClock([valid, invalid])
            return FakeMediaClock(timestamp=valid)

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_invalid_grace_seconds=0.0,
            connection_max_age_seconds=60.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIn("signed-session-1", first["frame"])
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            replacement = reader.wait_for_frame(
                first["sequence"], timeout=2.0
            )
            self.assertIsNotNone(replacement)
            self.assertIn("signed-session-2", replacement["frame"])
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"], valid
            )
            self.assertTrue(captures[0].released)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            reader.stop(timeout=2.0)

    def test_clock_resolution_uses_independent_connection_local_source(self):
        release_read = threading.Event()
        observed_sources = []

        def resolve(source, _frame, _position, _identity):
            observed_sources.append(source)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "capture-session",
            capture_factory=lambda source: ScriptedCapture(
                [f"{source}-frame"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=resolve,
            media_clock_source_factory=lambda: "clock-session",
            capture_position_milliseconds=lambda _cap: 0.0,
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(snapshot["media_clock"])
            self.assertEqual(observed_sources, ["clock-session"])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_media_clock_failure_does_not_hide_a_decodable_frame(self):
        release_read = threading.Event()
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["frame-1"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=(
                lambda _source, _frame, _position, _identity: (
                    _ for _ in ()
                ).throw(RuntimeError("playlist unavailable"))
            ),
            capture_position_milliseconds=lambda _cap: 0.0,
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertIsNone(snapshot["media_clock"])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_slow_media_clock_resolution_does_not_pause_live_capture(self):
        allow_next = threading.Event()
        release_clock = threading.Event()
        capture = GatedPositionCapture(
            ["frame-1", "frame-2"], [0.0, 100.0], allow_next
        )

        def slow_clock(*_args):
            release_clock.wait(1.0)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=slow_clock,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_initial_wait_seconds=0.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNone(first["media_clock"])
            allow_next.set()
            second = reader.wait_for_frame(first["sequence"], timeout=0.5)
            self.assertIsNotNone(second)
            self.assertIsNone(second["media_clock"])
        finally:
            release_clock.set()
            allow_next.set()
            reader.stop(timeout=2.0)

    def test_unmatched_clock_retries_on_a_later_frame(self):
        allow_next = threading.Event()
        capture = GatedPositionCapture(
            ["frame-1", "frame-2"], [0.0, 100.0], allow_next
        )
        calls = []
        recovered_clock = FakeMediaClock("2026-07-10T03:57:24.000Z")

        def media_clock_factory(*_args):
            calls.append(len(calls) + 1)
            return None if len(calls) == 1 else recovered_clock

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNone(first["media_clock"])
            time.sleep(0.11)
            allow_next.set()
            second = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                second["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:24.000Z",
            )
        finally:
            allow_next.set()
            reader.stop(timeout=2.0)

    def test_capture_position_reset_forces_an_exact_reanchor(self):
        allow_next = threading.Event()
        capture = GatedPositionCapture(
            ["before-reset", "after-reset"], [1000.0, 0.0], allow_next
        )
        clocks = [
            FakeMediaClock("2026-07-10T03:57:23.000Z"),
            FakeMediaClock("2026-07-10T03:57:25.000Z"),
        ]

        def media_clock_factory(*_args):
            return clocks.pop(0)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertEqual(
                first["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.000Z",
            )
            allow_next.set()
            second = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertEqual(
                second["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:25.000Z",
            )
            self.assertEqual(clocks, [])
        finally:
            allow_next.set()
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

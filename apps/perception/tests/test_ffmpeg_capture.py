import sys
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

import numpy as np


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from ffmpeg_capture import (  # noqa: E402
    FfmpegNvdecCapture,
    FragmentFrameSequenceMatch,
    NvdecCaptureError,
    build_nvdec_frame_identity,
    build_nvdec_command,
    match_fragment_frame_nvdec,
    rewrite_hls_master,
)


class FakeCapture:
    def __init__(self, frames, positions):
        self.frames = list(frames)
        self.positions = list(positions)
        self.position = 0.0
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if not self.frames:
            return False, None
        self.position = self.positions.pop(0)
        return True, self.frames.pop(0)

    def get(self, _property):
        return self.position

    def release(self):
        self.released = True


class FfmpegCaptureTests(unittest.TestCase):
    def test_release_fails_closed_and_retains_a_surviving_process(self):
        class SurvivingProcess:
            pid = 12345

            def __init__(self):
                self.waits = []

            def poll(self):
                return None

            def wait(self, timeout):
                self.waits.append(timeout)
                raise subprocess.TimeoutExpired("ffmpeg", timeout)

        class TemporaryDirectory:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        process = SurvivingProcess()
        temporary = TemporaryDirectory()
        capture = object.__new__(FfmpegNvdecCapture)
        capture._cancel_watcher_stop = threading.Event()
        capture._opened = True
        capture._capture = None
        capture._process_lock = threading.RLock()
        capture._process = process
        capture._memfd = None
        capture._temporary_directory = temporary
        capture._cancel_watcher = None

        with patch("ffmpeg_capture.os.killpg") as killpg:
            with self.assertRaisesRegex(
                NvdecCaptureError, "process did not exit"
            ):
                capture.release()

        self.assertIs(capture._process, process)
        self.assertFalse(temporary.cleaned)
        self.assertEqual(process.waits, [3, 3])
        self.assertEqual(killpg.call_count, 2)

    def test_capture_release_error_does_not_skip_child_termination(self):
        class BrokenCapture:
            def release(self):
                raise RuntimeError("synthetic OpenCV release failure")

        class TerminatingProcess:
            pid = 12345

            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = 0
                return 0

        class TemporaryDirectory:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        process = TerminatingProcess()
        temporary = TemporaryDirectory()
        capture = object.__new__(FfmpegNvdecCapture)
        capture._cancel_watcher_stop = threading.Event()
        capture._opened = True
        capture._capture = BrokenCapture()
        capture._process_lock = threading.RLock()
        capture._process = process
        capture._memfd = None
        capture._temporary_directory = temporary
        capture._cancel_watcher = None

        with patch("ffmpeg_capture.os.killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "OpenCV release"):
                capture.release()

        killpg.assert_called_once()
        self.assertIsNone(capture._process)
        self.assertTrue(temporary.cleaned)

    def test_master_is_rewritten_same_origin_without_command_line_url(self):
        source = "https://example.test/master.m3u8?token=secret"
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmedia.m3u8?child=secret\n"
        rewritten = rewrite_hls_master(source, master)
        self.assertIn(
            b"https://example.test/media.m3u8?child=secret", rewritten
        )
        command = build_nvdec_command(
            "/usr/bin/ffmpeg", "/proc/self/fd/9", "/tmp/frames.nut", hls=True
        )
        rendered = " ".join(command)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("example.test", rendered)
        self.assertIn("/proc/self/fd/9", rendered)
        self.assertIn("-rw_timeout 7000000", rendered)
        self.assertIn("-m3u8_hold_counters 3", rendered)

        file_command = build_nvdec_command(
            "/usr/bin/ffmpeg", "/tmp/input.mp4", "/tmp/frames.nut", hls=False
        )
        file_rendered = " ".join(file_command)
        self.assertNotIn("-rw_timeout", file_rendered)
        self.assertNotIn("-m3u8_hold_counters", file_rendered)

    def test_rejects_media_playlist_and_cross_origin_variant(self):
        with self.assertRaisesRegex(NvdecCaptureError, "variant playlist"):
            rewrite_hls_master(
                "https://example.test/master.m3u8",
                "#EXTM3U\n#EXTINF:2,\nsegment.mp4\n",
            )
        with self.assertRaisesRegex(NvdecCaptureError, "same-origin"):
            rewrite_hls_master(
                "https://example.test/master.m3u8",
                "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
                "https://other.test/media.m3u8\n",
            )

    def test_fragment_match_requires_one_exact_frame_and_releases(self):
        captures = []

        def factory(_path):
            capture = FakeCapture([b"left", b"target", b"right"], [0, 50, 100])
            captures.append(capture)
            return capture

        result = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            b"target",
            lambda frame: frame,
            capture_factory=factory,
        )
        self.assertEqual(result, 50.0)
        self.assertTrue(captures[0].released)

        result = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            b"duplicate",
            lambda _frame: b"duplicate",
            capture_factory=lambda _path: FakeCapture([b"a", b"b"], [0, 50]),
        )
        self.assertIsNone(result)

    def test_fragment_match_requires_one_unique_contiguous_sequence(self):
        result = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            (b"target-1", b"target-2", b"target-3"),
            lambda frame: frame,
            capture_factory=lambda _path: FakeCapture(
                [b"left", b"target-1", b"target-2", b"target-3", b"right"],
                [0, 50, 100, 150, 200],
            ),
        )
        self.assertEqual(result, FragmentFrameSequenceMatch(
            frame_offset_milliseconds=150.0,
            frame_positions_milliseconds=(50.0, 100.0, 150.0),
        ))

        duplicate = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            (b"target-1", b"target-2"),
            lambda frame: frame,
            capture_factory=lambda _path: FakeCapture(
                [b"target-1", b"target-2", b"gap", b"target-1", b"target-2"],
                [0, 50, 100, 150, 200],
            ),
        )
        self.assertIsNone(duplicate)

        non_monotonic = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            (b"target-1", b"target-2", b"target-3"),
            lambda frame: frame,
            capture_factory=lambda _path: FakeCapture(
                [b"target-1", b"target-2", b"target-3"],
                [100, 50, 150],
            ),
        )
        self.assertIsNone(non_monotonic)

    def test_nvdec_sequence_hashes_exact_pixels_only_for_quick_candidates(self):
        frames = [
            np.full((32, 32, 3), value, dtype=np.uint8)
            for value in (0, 1, 2, 3)
        ]
        exact_calls = []

        def exact(frame):
            exact_calls.append(int(frame[0, 0, 0]))
            return frame.tobytes()

        targets = tuple(
            build_nvdec_frame_identity(frame, exact)
            for frame in frames[1:3]
        )
        exact_calls.clear()
        result = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            targets,
            exact,
            capture_factory=lambda _path: FakeCapture(
                frames, [0, 50, 100, 150]
            ),
        )
        self.assertEqual(result, FragmentFrameSequenceMatch(
            frame_offset_milliseconds=100.0,
            frame_positions_milliseconds=(50.0, 100.0),
        ))
        self.assertEqual(exact_calls, [1, 2])

    def test_quick_identity_only_runs_exact_hash_for_candidates(self):
        frames = [
            np.full((32, 32, 3), value, dtype=np.uint8)
            for value in (0, 1, 2)
        ]
        exact_calls = []

        def exact(frame):
            exact_calls.append(int(frame[0, 0, 0]))
            return frame.tobytes()

        target = build_nvdec_frame_identity(frames[1], exact)
        exact_calls.clear()
        result = match_fragment_frame_nvdec(
            b"init",
            b"segment",
            target,
            exact,
            capture_factory=lambda _path: FakeCapture(frames, [0, 50, 100]),
        )
        self.assertEqual(result, 50.0)
        self.assertEqual(exact_calls, [1])

    def test_cancelled_fragment_match_never_opens_a_decoder(self):
        cancelled = threading.Event()
        cancelled.set()

        def forbidden_factory(_path):
            raise AssertionError("cancelled match opened a decoder")

        self.assertIsNone(match_fragment_frame_nvdec(
            b"init",
            b"segment",
            b"target",
            lambda frame: frame,
            capture_factory=forbidden_factory,
            cancel_event=cancelled,
        ))


if __name__ == "__main__":
    unittest.main()

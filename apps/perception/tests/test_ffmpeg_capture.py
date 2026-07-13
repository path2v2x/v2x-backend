import sys
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import requests

import numpy as np


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from ffmpeg_capture import (  # noqa: E402
    FfmpegNvdecCapture,
    FragmentFrameSequenceMatch,
    NvdecCaptureError,
    SameSessionTransportClock,
    _FramePtsSidecar,
    _fetch_bounded,
    _LoopbackHlsMediator,
    _MediatedFragment,
    _PacketSample,
    _parse_media_playlist,
    _probe_fragment_packets,
    _run_bounded_command,
    build_nvdec_frame_identity,
    build_nvdec_command,
    match_fragment_frame_nvdec,
    rewrite_hls_master,
)


class Response:
    def __init__(self, *, content=b"", text=None, url=None, status=200):
        self.content = (
            text.encode("utf-8") if text is not None else bytes(content)
        )
        self.text = (
            text if text is not None else self.content.decode("utf-8")
        )
        self.url = url
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(
                "failed https://example.test/?SessionToken=secret-value"
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
        protocol_index = command.index("-protocol_whitelist")
        self.assertEqual(command[protocol_index + 1], "file,http,tcp")
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

    def test_sidecar_command_preserves_pts_without_exposing_source(self):
        command = build_nvdec_command(
            "/usr/bin/ffmpeg",
            "/proc/self/fd/9",
            "/tmp/frames.nut",
            hls=True,
            pts_fd=11,
        )
        rendered = " ".join(command)
        self.assertIn("-copyts", command)
        self.assertIn("-copytb 1", rendered)
        self.assertEqual(rendered.count("-enc_time_base -1"), 1)
        self.assertIn("-stats_enc_pre pipe:11", rendered)
        self.assertIn(
            "v2xpts1,{fidx},{sidx},{n},{ni},{tb},{pts},{tbi},{ptsi}",
            command,
        )
        self.assertNotIn("split=2", rendered)
        self.assertNotIn("scale=2:2", rendered)
        self.assertNotIn("framecrc", rendered)
        self.assertNotIn("showinfo", rendered)
        self.assertIn("-fps_mode passthrough", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("SessionToken", rendered)
        with self.assertRaisesRegex(ValueError, "descriptor"):
            build_nvdec_command(
                "/usr/bin/ffmpeg", "/proc/self/fd/9", "/tmp/frames.nut",
                hls=True, pts_fd=True,
            )

    def test_preencode_sidecar_preserves_backward_source_pts(self):
        read_fd, write_fd = __import__("os").pipe()
        sidecar = _FramePtsSidecar(read_fd)
        try:
            __import__("os").write(write_fd, (
                b"v2xpts1,0,0,0,0,1/20,2,1/20,2\n"
                b"v2xpts1,0,0,1,1,1/20,3,1/20,3\n"
                b"v2xpts1,0,0,2,2,1/20,2,1/20,2\n"
            ))
            __import__("os").close(write_fd)
            self.assertEqual(sidecar.take(1.0), Fraction(1, 10))
            self.assertEqual(sidecar.take(1.0), Fraction(3, 20))
            self.assertEqual(sidecar.take(1.0), Fraction(1, 10))
        finally:
            try:
                __import__("os").close(write_fd)
            except OSError:
                pass
            sidecar.close()

    def test_malformed_or_overflowed_sidecar_fails_closed(self):
        for body in (
            b"v2xpts1,0,0,0,0,1/20,2,1/20,3\n",
            b"v2xpts1,0,0,1,1,1/20,2,1/20,2\n",
            b"v2xpts1,0,0,0,0,1/20,2,0/1,2\n",
            (
                b"v2xpts1,0,0,0,0,1/20,"
                b"9223372036854775807,1/20,9223372036854775807\n"
            ),
            b"https://example.invalid/?SessionToken=secret\n",
            b"\xff\n",
        ):
            with self.subTest(body=body):
                read_fd, write_fd = __import__("os").pipe()
                sidecar = _FramePtsSidecar(read_fd, queue_limit=4)
                try:
                    __import__("os").write(write_fd, body)
                    __import__("os").close(write_fd)
                    self.assertIsNone(sidecar.take(1.0))
                    self.assertIn(
                        sidecar.diagnostic(),
                        {
                            "sidecar_record_invalid",
                            "sidecar_index_nonsequential",
                            "sidecar_non_ascii",
                        },
                    )
                finally:
                    try:
                        __import__("os").close(write_fd)
                    except OSError:
                        pass
                    sidecar.close()

        read_fd, write_fd = os.pipe()
        sidecar = _FramePtsSidecar(read_fd, queue_limit=1)
        try:
            os.write(write_fd, (
                b"v2xpts1,0,0,0,0,1/20,2,1/20,2\n"
                b"v2xpts1,0,0,1,1,1/20,3,1/20,3\n"
            ))
            os.close(write_fd)
            self.assertIsNone(sidecar.take(1.0))
            self.assertEqual(sidecar.diagnostic(), "sidecar_queue_overflow")
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            sidecar.close()

        read_fd, write_fd = os.pipe()
        sidecar = _FramePtsSidecar(read_fd)
        try:
            os.write(write_fd, (
                b"v2xpts1,0,0,0,0,1/20,2,1/20,2\n"
                b"v2xpts1,0,0,1,1,1/25,3,1/25,3\n"
            ))
            os.close(write_fd)
            self.assertIsNone(sidecar.take(1.0))
            self.assertEqual(
                sidecar.diagnostic(), "sidecar_time_base_changed"
            )
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            sidecar.close()

        read_fd, write_fd = os.pipe()
        sidecar = _FramePtsSidecar(read_fd)
        writer = threading.Thread(target=lambda: (
            os.write(write_fd, b"invalid\n" + (
                b"v2xpts1,0,0,0,0,1/20,2,1/20,2\n" * 4096
            )),
            os.close(write_fd),
        ))
        try:
            writer.start()
            writer.join(2.0)
            self.assertFalse(writer.is_alive())
            self.assertIsNone(sidecar.take(1.0))
            self.assertEqual(sidecar.diagnostic(), "sidecar_record_invalid")
        finally:
            if writer.is_alive():
                os.close(write_fd)
                writer.join(1.0)
            sidecar.close()

    def test_host_preencode_stats_preserve_backward_input_pts(self):
        if not Path("/usr/bin/ffmpeg").is_file():
            self.skipTest("host FFmpeg is unavailable")
        read_fd, write_fd = os.pipe()
        sidecar = _FramePtsSidecar(read_fd)
        process = None
        with tempfile.TemporaryDirectory(prefix="v2x-stats-test-") as temp:
            output = Path(temp) / "frames.nut"
            command = [
                "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
                "-nostdin", "-f", "lavfi", "-i",
                "testsrc2=size=64x48:rate=1:duration=6,"
                "setpts='if(gte(N,3),PTS-2/TB,PTS)'",
                "-vf", "format=bgr24", "-map", "0:v:0",
                "-fps_mode", "passthrough", "-enc_time_base", "-1",
                "-c:v", "rawvideo", "-stats_enc_pre", f"pipe:{write_fd}",
                "-stats_enc_pre_fmt",
                "v2xpts1,{fidx},{sidx},{n},{ni},{tb},{pts},{tbi},{ptsi}",
                "-f", "nut", "-y", str(output),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                os.close(write_fd)
                write_fd = None
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(
                    [sidecar.take(1.0) for _ in range(6)],
                    [
                        Fraction(0), Fraction(1), Fraction(2),
                        Fraction(1), Fraction(2), Fraction(3),
                    ],
                )
            finally:
                if write_fd is not None:
                    os.close(write_fd)
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                sidecar.close()

    def test_sidecar_thread_is_the_only_descriptor_owner(self):
        read_fd, write_fd = os.pipe()
        sidecar = _FramePtsSidecar(read_fd)
        os.close(write_fd)
        sidecar.close()
        self.assertIsNone(sidecar._descriptor)

        # Linux normally reuses the just-closed descriptor. A second sidecar
        # close must never close whichever unrelated resource now owns it.
        replacement = os.open("/dev/null", os.O_RDONLY)
        try:
            sidecar.close()
            os.fstat(replacement)
        finally:
            os.close(replacement)

    def test_transport_clock_is_piecewise_exact_and_rejects_collisions(self):
        fragment = _MediatedFragment(
            program_date_time_epoch=1_783_655_843.0,
            program_date_time_utc="2026-07-10T03:57:23.000Z",
            duration_seconds=2.0,
            media_sequence=17,
            fragment_id="frag-123",
            init_url="https://example.test/init.mp4?SessionToken=secret",
            segment_url=(
                "https://example.test/media.mp4?FragmentNumber=frag-123&"
                "SessionToken=secret"
            ),
        )
        samples = (
            _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
            _PacketSample(1, Fraction(3, 20), Fraction(1, 20)),
        )
        clock = SameSessionTransportClock()
        clock.add_fragment(fragment, samples)
        self.assertEqual(clock.evidence_method, "exact_same_session_pts")
        metadata = clock.metadata_at(150.0)
        self.assertEqual(
            metadata["media_timestamp_utc"], "2026-07-10T03:57:23.050Z"
        )
        safe = metadata["media_clock"]
        self.assertEqual(safe["evidence_method"], "exact_same_session_pts")
        self.assertEqual(safe["anchor_fragment_id"], "frag-123")
        self.assertEqual(safe["fragment_sample_index"], 1)
        self.assertEqual(safe["source_pts"], 3)
        self.assertEqual(safe["source_time_base_numerator"], 1)
        self.assertEqual(safe["source_time_base_denominator"], 20)
        rendered = repr(metadata)
        self.assertNotIn("SessionToken", rendered)
        self.assertNotIn("example.test", rendered)
        self.assertIsNone(clock.metadata_at(200.0))
        self.assertEqual(clock.classify_position(150.0), "matched")
        self.assertEqual(
            clock.classify_position(200.0), "position_after_window"
        )
        self.assertEqual(
            clock.classify_position(50.0), "position_before_window"
        )

        collision = _MediatedFragment(
            program_date_time_epoch=1_783_655_845.0,
            program_date_time_utc="2026-07-10T03:57:25.000Z",
            duration_seconds=2.0,
            media_sequence=18,
            fragment_id="frag-124",
            init_url=fragment.init_url,
            segment_url=fragment.segment_url.replace("frag-123", "frag-124"),
        )
        with self.assertRaisesRegex(NvdecCaptureError, "timeline"):
            clock.add_fragment(collision, samples)

    def test_transport_clock_rejects_duplicates_and_sorts_b_frame_pts(self):
        fragment = _MediatedFragment(
            0.0, "1970-01-01T00:00:00.000Z", 1.0, 1, "fragment",
            "https://example.test/init", (
                "https://example.test/segment?FragmentNumber=fragment"
            ),
        )
        clock = SameSessionTransportClock()
        with self.assertRaisesRegex(NvdecCaptureError, "ambiguous"):
            clock.add_fragment(fragment, (
                _PacketSample(0, Fraction(1), Fraction(1, 20)),
                _PacketSample(1, Fraction(1), Fraction(1, 20)),
            ))
        with self.assertRaisesRegex(NvdecCaptureError, "not integral"):
            clock.add_fragment(fragment, (
                _PacketSample(0, Fraction(1, 3), Fraction(1, 20)),
            ))
        clock.add_fragment(fragment, (
            _PacketSample(0, Fraction(1), Fraction(1, 20)),
            _PacketSample(1, Fraction(1, 2), Fraction(1, 20)),
        ))
        self.assertEqual(
            clock.metadata_at(500.0)["media_timestamp_utc"],
            "1970-01-01T00:00:00.000Z",
        )
        self.assertEqual(
            clock.metadata_at(500.0)["media_clock"]["fragment_sample_index"],
            0,
        )
        self.assertEqual(
            clock.metadata_at(1000.0)["media_timestamp_utc"],
            "1970-01-01T00:00:00.500Z",
        )

    def test_transport_clock_accepts_affine_preroll_overlap(self):
        def fragment(sequence, fragment_id, epoch):
            return _MediatedFragment(
                epoch,
                "1970-01-01T00:00:00.000Z",
                2.0,
                sequence,
                fragment_id,
                "https://example.test/init",
                "https://example.test/segment?FragmentNumber=" + fragment_id,
            )

        clock = SameSessionTransportClock()
        clock.add_fragment(fragment(1, "f1", 1000.0), (
            _PacketSample(0, Fraction(0), Fraction(1, 1000)),
            _PacketSample(1, Fraction(1), Fraction(1, 1000)),
        ))
        clock.add_fragment(fragment(2, "f2", 1002.0), (
            _PacketSample(0, Fraction(2), Fraction(1, 1000)),
            _PacketSample(1, Fraction(3), Fraction(1, 1000)),
        ))
        clock.add_fragment(fragment(3, "overlap", 1003.0), (
            _PacketSample(0, Fraction(3), Fraction(1, 1000)),
            _PacketSample(1, Fraction(4), Fraction(1, 1000)),
        ))
        self.assertEqual(
            clock.metadata_at(3000.0)["media_timestamp_utc"],
            "1970-01-01T00:16:43.000Z",
        )
        self.assertEqual(
            clock.metadata_at(3000.0)["media_clock"]["anchor_fragment_id"],
            "f2",
        )

        real_shaped = SameSessionTransportClock()
        real_shaped.add_fragment(fragment(10, "kvs-a", 2000.0), (
            _PacketSample(0, Fraction(6366, 1000), Fraction(1, 1000)),
            _PacketSample(1, Fraction(7569, 1000), Fraction(1, 1000)),
            _PacketSample(2, Fraction(8330, 1000), Fraction(1, 1000)),
        ))
        real_shaped.add_fragment(fragment(11, "kvs-b", 2001.203), (
            _PacketSample(0, Fraction(7569, 1000), Fraction(1, 1000)),
            _PacketSample(1, Fraction(8330, 1000), Fraction(1, 1000)),
            _PacketSample(2, Fraction(9543, 1000), Fraction(1, 1000)),
        ))
        for pts in (6366.0, 7569.0, 8330.0, 9543.0):
            self.assertEqual(real_shaped.classify_position(pts), "matched")
        self.assertEqual(
            real_shaped.metadata_at(8330.0)["media_timestamp_utc"],
            "1970-01-01T00:33:21.964Z",
        )

    def test_transport_clock_rejects_non_affine_or_backward_fragments(self):
        def fragment(sequence, fragment_id, epoch):
            return _MediatedFragment(
                epoch,
                "1970-01-01T00:00:00.000Z",
                2.0,
                sequence,
                fragment_id,
                "https://example.test/init",
                "https://example.test/segment?FragmentNumber=" + fragment_id,
            )

        def seeded_clock():
            clock = SameSessionTransportClock()
            clock.add_fragment(fragment(1, "f1", 1000.0), (
                _PacketSample(0, Fraction(0), Fraction(1, 1000)),
                _PacketSample(1, Fraction(1), Fraction(1, 1000)),
            ))
            clock.add_fragment(fragment(2, "f2", 1002.0), (
                _PacketSample(0, Fraction(2), Fraction(1, 1000)),
                _PacketSample(1, Fraction(3), Fraction(1, 1000)),
            ))
            return clock

        with self.assertRaisesRegex(NvdecCaptureError, "PDT timeline"):
            seeded_clock().add_fragment(fragment(3, "shifted", 1003.002), (
                _PacketSample(0, Fraction(3), Fraction(1, 1000)),
            ))
        with self.assertRaisesRegex(NvdecCaptureError, "PDT timeline"):
            seeded_clock().add_fragment(fragment(3, "backward-pdt", 999.0), (
                _PacketSample(0, Fraction(4), Fraction(1, 1000)),
            ))
        with self.assertRaisesRegex(NvdecCaptureError, "PDT timeline"):
            seeded_clock().add_fragment(fragment(3, "backward-pts", 1003.0), (
                _PacketSample(0, Fraction(1), Fraction(1, 1000)),
            ))

        drifting = SameSessionTransportClock()
        drifting.add_fragment(fragment(1, "drift-1", 1000.0), (
            _PacketSample(0, Fraction(0), Fraction(1, 1000)),
        ))
        drifting.add_fragment(fragment(2, "drift-2", 1002.00075), (
            _PacketSample(0, Fraction(2), Fraction(1, 1000)),
        ))
        with self.assertRaisesRegex(NvdecCaptureError, "PDT timeline"):
            drifting.add_fragment(fragment(3, "drift-3", 1004.0015), (
                _PacketSample(0, Fraction(4), Fraction(1, 1000)),
            ))

    def test_transport_clock_prunes_fifo_and_clear_drops_retained_evidence(self):
        fragment = _MediatedFragment(
            0.0, "1970-01-01T00:00:00.000Z", 4.0, 1, "fragment",
            "https://example.test/init", (
                "https://example.test/segment?FragmentNumber=fragment"
            ),
        )
        clock = SameSessionTransportClock(max_positions=2)
        clock.add_fragment(fragment, (
            _PacketSample(0, Fraction(0), Fraction(1)),
            _PacketSample(1, Fraction(1), Fraction(1)),
            _PacketSample(2, Fraction(2), Fraction(1)),
        ))
        self.assertIsNone(clock.metadata_at(0.0))
        self.assertIsNotNone(clock.metadata_at(1000.0))
        self.assertIsNotNone(clock.metadata_at(2000.0))
        clock.clear()
        self.assertIsNone(clock.metadata_at(1000.0))
        self.assertIsNone(clock.metadata_at(2000.0))

    def test_transport_clock_affine_origin_survives_fragment_pruning(self):
        clock = SameSessionTransportClock(max_positions=2)

        def add(sequence, origin_shift=0.0):
            pts = Fraction(sequence - 1)
            fragment = _MediatedFragment(
                1000.0 + float(pts) + origin_shift,
                "1970-01-01T00:00:00.000Z",
                1.0,
                sequence,
                f"fragment-{sequence}",
                "https://example.test/init",
                "https://example.test/segment?FragmentNumber="
                f"fragment-{sequence}",
            )
            clock.add_fragment(fragment, (
                _PacketSample(0, pts, Fraction(1, 1000)),
            ))

        for sequence in range(1, 131):
            add(sequence)
        with self.assertRaisesRegex(NvdecCaptureError, "PDT timeline"):
            add(131, origin_shift=0.002)
        clock.clear()
        add(1, origin_shift=1.0)
        self.assertEqual(clock.classify_position(0.0), "matched")

    def test_capture_exposes_source_pts_and_only_an_exact_current_clock(self):
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        capture = object.__new__(FfmpegNvdecCapture)
        capture._opened = True
        capture._capture = FakeCapture([frame, frame], [0.0, 50.0])
        capture._process = SimpleNamespace(poll=lambda: None)
        capture._source_pts_timeout_seconds = 0.1
        capture._last_source_position_ms = None
        capture._last_transport_exact = False
        capture._transport_diagnostic = "starting"
        capture._last_emitted_source_pts = None
        capture._consecutive_overlap_frames = 0
        capture._overlap_frames_dropped = 0

        fragment = _MediatedFragment(
            0.0, "1970-01-01T00:00:00.000Z", 1.0, 1, "fragment",
            "https://example.test/init", (
                "https://example.test/segment?FragmentNumber=fragment"
            ),
        )
        clock = SameSessionTransportClock()
        clock.add_fragment(fragment, (
            _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
        ))
        capture._mediator = SimpleNamespace(
            clock=clock,
            transport_diagnostic=clock.classify_position,
        )
        positions = iter((Fraction(1, 10), Fraction(1, 5)))
        capture._pts_sidecar = SimpleNamespace(
            take=lambda _timeout: next(positions),
            diagnostic=lambda: "sidecar_ready",
        )

        self.assertTrue(capture.read()[0])
        self.assertEqual(capture.get(0), 100.0)
        self.assertIs(capture.transport_media_clock(), clock)
        self.assertTrue(capture.read()[0])
        self.assertEqual(capture.get(0), 200.0)
        self.assertIsNone(capture.transport_media_clock())

    def test_capture_drops_exact_preroll_before_returning_newer_frame(self):
        frames = [
            np.full((2, 2, 3), value, dtype=np.uint8)
            for value in range(4)
        ]
        capture = object.__new__(FfmpegNvdecCapture)
        capture._opened = True
        capture._capture = FakeCapture(frames, [0.0] * 4)
        capture._process = SimpleNamespace(poll=lambda: None)
        capture._source_pts_timeout_seconds = 0.1
        capture._last_source_position_ms = None
        capture._last_transport_exact = False
        capture._transport_diagnostic = "starting"
        capture._last_emitted_source_pts = None
        capture._consecutive_overlap_frames = 0
        capture._overlap_frames_dropped = 0

        fragment = _MediatedFragment(
            0.0, "1970-01-01T00:00:00.000Z", 1.0, 1, "fragment",
            "https://example.test/init",
            "https://example.test/segment?FragmentNumber=fragment",
        )
        clock = SameSessionTransportClock()
        clock.add_fragment(fragment, (
            _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
            _PacketSample(1, Fraction(3, 20), Fraction(1, 20)),
            _PacketSample(2, Fraction(1, 5), Fraction(1, 20)),
            _PacketSample(3, Fraction(1, 4), Fraction(1, 20)),
        ))
        capture._mediator = SimpleNamespace(
            clock=clock,
            transport_diagnostic=clock.classify_position,
        )
        positions = iter((
            Fraction(1, 10),
            Fraction(1, 5),
            Fraction(3, 20),
            Fraction(1, 4),
        ))
        capture._pts_sidecar = SimpleNamespace(
            take=lambda _timeout: next(positions),
            diagnostic=lambda: "sidecar_ready",
        )

        ok, first = capture.read()
        self.assertTrue(ok)
        self.assertTrue(np.array_equal(first, frames[0]))
        ok, second = capture.read()
        self.assertTrue(ok)
        self.assertTrue(np.array_equal(second, frames[1]))
        ok, after_overlap = capture.read()
        self.assertTrue(ok)
        self.assertTrue(np.array_equal(after_overlap, frames[3]))
        self.assertEqual(capture.get(0), 250.0)
        self.assertEqual(capture._overlap_frames_dropped, 1)
        self.assertEqual(capture.transport_clock_diagnostic(), "matched")
        self.assertIs(capture.transport_media_clock(), clock)

    def test_capture_bounds_continuous_overlap_replay(self):
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        capture = object.__new__(FfmpegNvdecCapture)
        capture._opened = True
        capture._capture = FakeCapture([frame] * 122, [0.0] * 122)
        capture._process = SimpleNamespace(poll=lambda: None)
        capture._source_pts_timeout_seconds = 0.1
        capture._last_source_position_ms = None
        capture._last_transport_exact = False
        capture._transport_diagnostic = "starting"
        capture._last_emitted_source_pts = None
        capture._consecutive_overlap_frames = 0
        capture._overlap_frames_dropped = 0

        fragment = _MediatedFragment(
            0.0, "1970-01-01T00:00:00.000Z", 1.0, 1, "fragment",
            "https://example.test/init",
            "https://example.test/segment?FragmentNumber=fragment",
        )
        clock = SameSessionTransportClock()
        clock.add_fragment(fragment, (
            _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
            _PacketSample(1, Fraction(1, 5), Fraction(1, 20)),
        ))
        capture._mediator = SimpleNamespace(
            clock=clock,
            transport_diagnostic=clock.classify_position,
        )
        positions = iter((Fraction(1, 5),) + (Fraction(1, 10),) * 121)
        capture._pts_sidecar = SimpleNamespace(
            take=lambda _timeout: next(positions),
            diagnostic=lambda: "sidecar_ready",
        )

        self.assertTrue(capture.read()[0])
        self.assertTrue(capture.read()[0])
        self.assertEqual(capture._overlap_frames_dropped, 120)
        self.assertEqual(
            capture.transport_clock_diagnostic(),
            "overlap_replay_exceeded",
        )
        self.assertIsNone(capture.transport_media_clock())

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

    def test_master_rejects_every_uri_bearing_tag(self):
        source = "https://example.test/master.m3u8?SessionToken=secret"
        base = (
            "#EXTM3U\n{tag}\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child\n"
        )
        for tag in (
            '#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"',
            '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"',
            '#EXT-X-SESSION-DATA:DATA-ID="x",URI = "data.json"',
            '#EXT-X-IMAGE-STREAM-INF:BANDWIDTH=1,uri="images.m3u8"',
            '#EXT-X-CONTENT-STEERING:SERVER-URI="steering.json"',
        ):
            with self.subTest(tag=tag):
                playlist = base.format(tag=tag)
                with self.assertRaisesRegex(
                    NvdecCaptureError, "URI-bearing"
                ):
                    rewrite_hls_master(source, playlist)
                with self.assertRaisesRegex(
                    NvdecCaptureError, "URI-bearing"
                ):
                    _LoopbackHlsMediator(
                        source,
                        playlist,
                        http_get=lambda *_args, **_kwargs: None,
                    )

    def test_media_playlist_requires_exact_safe_fmp4_provenance(self):
        media_url = (
            "https://example.test/media.m3u8?SessionToken=playlist-secret"
        )
        valid = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:17\n"
            "#EXT-X-MAP:URI=\"init.mp4?SessionToken=init-secret\"\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.000Z\n"
            "#EXTINF:2.0,\n"
            "segment.mp4?FragmentNumber=frag-123&SessionToken=media-secret\n"
        )
        fragment = _parse_media_playlist(media_url, valid)[0]
        self.assertEqual(fragment.fragment_id, "frag-123")
        self.assertEqual(fragment.media_sequence, 17)
        for text, expected in (
            (valid.replace("#EXT-X-PROGRAM-DATE-TIME", "#MISSING"),
             "provenance"),
            (valid.replace("#EXTINF:2.0,", "#EXT-X-BYTERANGE:10@0\n#EXTINF:2.0,"),
             "byte ranges"),
            (valid.replace("segment.mp4?", "https://other.test/segment.mp4?"),
             "same-origin"),
            (valid.replace("FragmentNumber=frag-123&", ""), "identity"),
            (valid.replace(
                "#EXT-X-MEDIA-SEQUENCE:17",
                '#EXT-X-SESSION-DATA:URI="https://example.test/data"',
            ), "URI-bearing"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(NvdecCaptureError, expected):
                    _parse_media_playlist(media_url, text)

    def test_ffprobe_packet_parser_is_bounded_url_free_and_exact(self):
        payload = (
            b'{"streams":[{"index":0,"codec_type":"video",'
            b'"time_base":"1/20"}],"packets":['
            b'{"stream_index":0,"pts":2,"dts":0,"duration":1,"flags":"K_"},'
            b'{"stream_index":0,"pts":3,"dts":1,"duration":1,"flags":"__"}]}'
        )
        with patch(
            "ffmpeg_capture._run_bounded_command", return_value=payload
        ) as run:
            samples = _probe_fragment_packets(b"init", b"segment")
        self.assertEqual([sample.pts for sample in samples], [
            Fraction(1, 10), Fraction(3, 20)
        ])
        command = " ".join(run.call_args.args[0])
        self.assertIn("/proc/self/fd/", command)
        self.assertNotIn("https://", command)
        self.assertNotIn("SessionToken", command)
        self.assertEqual(
            run.call_args.kwargs["output_limit"], 2 * 1024 * 1024
        )

        bad = b'{"streams":[],"packets":[]}'
        with patch("ffmpeg_capture._run_bounded_command", return_value=bad):
            with self.assertRaisesRegex(NvdecCaptureError, "track") as error:
                _probe_fragment_packets(b"init", b"segment")
        self.assertNotIn("secret", str(error.exception))

        multiple = (
            b'{"streams":['
            b'{"index":0,"codec_type":"video","time_base":"1/20"},'
            b'{"index":1,"codec_type":"video","time_base":"1/20"}],'
            b'"packets":[{"stream_index":0,"pts":1}]}'
        )
        with patch(
            "ffmpeg_capture._run_bounded_command", return_value=multiple
        ):
            with self.assertRaisesRegex(NvdecCaptureError, "ambiguous"):
                _probe_fragment_packets(b"init", b"segment")

        for codec_type in ("audio", "subtitle", "data", "attachment"):
            auxiliary = (
                '{"streams":['
                '{"index":0,"codec_type":"video","time_base":"1/20"},'
                f'{{"index":1,"codec_type":"{codec_type}",'
                '"time_base":"1/20"}],'
                '"packets":[{"stream_index":0,"pts":1}]}'
            ).encode()
            with self.subTest(codec_type=codec_type), patch(
                "ffmpeg_capture._run_bounded_command",
                return_value=auxiliary,
            ):
                with self.assertRaisesRegex(NvdecCaptureError, "ambiguous"):
                    _probe_fragment_packets(b"init", b"segment")

    def test_bounded_probe_runner_rejects_output_overflow(self):
        with self.assertRaisesRegex(NvdecCaptureError, "inspection failed"):
            _run_bounded_command(
                ["/bin/sh", "-c", "head -c 4096 /dev/zero"],
                pass_fds=(),
                timeout=1.0,
                output_limit=64,
            )

    def test_fetch_has_one_monotonic_deadline_and_disables_redirects(self):
        requested = {}

        def redirecting_get(url, **kwargs):
            requested.update(kwargs)
            return Response(content=b"redirect", url=url, status=302)

        with self.assertRaisesRegex(NvdecCaptureError, "redirect"):
            _fetch_bounded(
                redirecting_get,
                "https://example.test/master.m3u8?SessionToken=secret",
                timeout=1.0,
                origin_url="https://example.test/master.m3u8",
                limit=1024,
                label="HLS test",
            )
        self.assertIs(requested["allow_redirects"], False)
        self.assertIs(requested["stream"], True)

        class SlowResponse:
            url = "https://example.test/media.m3u8"
            headers = {}

            def __init__(self):
                self.closed = False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                time.sleep(0.03)
                yield b"late"

            def close(self):
                self.closed = True

        slow = SlowResponse()
        with self.assertRaisesRegex(NvdecCaptureError, "deadline"):
            _fetch_bounded(
                lambda _url, **_kwargs: slow,
                slow.url,
                timeout=0.01,
                origin_url=slow.url,
                limit=1024,
                label="HLS slow test",
            )
        self.assertTrue(slow.closed)

        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(NvdecCaptureError, "cancelled"):
            _fetch_bounded(
                lambda *_args, **_kwargs: self.fail("cancelled fetch ran"),
                slow.url,
                timeout=1.0,
                origin_url=slow.url,
                limit=1024,
                label="HLS cancelled test",
                cancel_event=cancelled,
            )

    def test_production_fetch_is_killable_and_keeps_url_in_memfd(self):
        source = (
            "https://example.test/master.m3u8?SessionToken=top-secret"
        )
        observed = {}

        def inspect_helper(command, **kwargs):
            rendered = " ".join(command)
            self.assertNotIn("top-secret", rendered)
            self.assertNotIn("example.test", rendered)
            descriptor = kwargs["pass_fds"][0]
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed.update(json.loads(os.read(descriptor, 256 * 1024)))
            self.assertLessEqual(kwargs["absolute_deadline"], time.monotonic() + 1.0)
            return b"playlist"

        with patch(
            "ffmpeg_capture._run_bounded_command", side_effect=inspect_helper
        ):
            body = _fetch_bounded(
                requests.get,
                source,
                timeout=1.0,
                origin_url=source,
                limit=1024,
                label="HLS production test",
            )
        self.assertEqual(body, b"playlist")
        self.assertEqual(observed["url"], source)
        self.assertEqual(observed["limit"], 1024)

        started = time.monotonic()
        with self.assertRaisesRegex(NvdecCaptureError, "bounded fetch"):
            _run_bounded_command(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                pass_fds=(),
                timeout=0.05,
                output_limit=64,
                error_message="bounded fetch",
            )
        self.assertLess(time.monotonic() - started, 0.75)

        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(NvdecCaptureError, "cancelled fetch"):
                _run_bounded_command(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    pass_fds=(),
                    timeout=5.0,
                    output_limit=64,
                    error_message="cancelled fetch",
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 0.75)

    def test_loopback_mediator_binds_actual_served_fragment_and_hides_tokens(self):
        source = (
            "https://example.test/master.m3u8?SessionToken=master-secret"
        )
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child-secret\n"
        )
        media = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:17\n"
            "#EXT-X-MAP:URI=\"init.mp4?SessionToken=init-secret\"\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.000Z\n"
            "#EXTINF:2.0,\n"
            "segment.mp4?FragmentNumber=frag-123&SessionToken=media-secret\n"
        )
        upstream_calls = []

        def get(url, timeout):
            upstream_calls.append((url, timeout))
            if "media.m3u8" in url:
                return Response(text=media, url=url)
            if "init.mp4" in url:
                return Response(content=b"exact-init", url=url)
            if "segment.mp4" in url:
                return Response(content=b"exact-segment", url=url)
            raise AssertionError("unexpected upstream URL")

        def probe(init, segment):
            self.assertEqual((init, segment), (b"exact-init", b"exact-segment"))
            return (
                _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
                _PacketSample(1, Fraction(3, 20), Fraction(1, 20)),
            )

        mediator = _LoopbackHlsMediator(
            source, master, http_get=get, packet_probe=probe
        )
        retained_clock = mediator.clock
        try:
            rendered_master = mediator.master.decode("utf-8")
            self.assertIn("http://127.0.0.1:", rendered_master)
            for secret in (
                "master-secret", "child-secret", "init-secret",
                "media-secret", "example.test", "SessionToken",
            ):
                self.assertNotIn(secret, rendered_master)
            local_media = next(
                line for line in rendered_master.splitlines()
                if line.startswith("http://")
            )
            media_response = requests.get(local_media, timeout=2)
            self.assertEqual(media_response.status_code, 200)
            rewritten = media_response.text
            self.assertNotIn("SessionToken", rewritten)
            self.assertNotIn("example.test", rewritten)
            init_url = re.search(r'URI="([^"]+)"', rewritten).group(1)
            segment_url = next(
                line for line in rewritten.splitlines()
                if line.startswith("http://") and line != init_url
            )
            self.assertEqual(requests.get(init_url, timeout=2).content, b"exact-init")
            self.assertEqual(
                requests.get(segment_url, timeout=2).content, b"exact-segment"
            )
            metadata = mediator.clock.metadata_at(150.0)
            self.assertEqual(
                metadata["media_timestamp_utc"],
                "2026-07-10T03:57:23.050Z",
            )
            rendered = repr(metadata)
            self.assertNotIn("secret", rendered.lower())
            self.assertEqual(len(upstream_calls), 3)
        finally:
            mediator.close()
        self.assertFalse(mediator.is_alive())
        self.assertIsNone(retained_clock.metadata_at(150.0))
        self.assertIsNone(mediator._media_url)
        self.assertIsNone(mediator._origin_url)
        self.assertIsNone(mediator._http_get)
        self.assertIsNone(mediator._packet_probe)
        self.assertIsNone(mediator._token)
        self.assertEqual(mediator.master, b"")

    def test_packet_or_clock_failure_keeps_exact_segment_pixel_fallback(self):
        source = "https://example.test/master.m3u8?SessionToken=master"
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child\n"
        )
        media = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:17\n"
            "#EXT-X-MAP:URI=\"init.mp4?SessionToken=init\"\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.000Z\n"
            "#EXTINF:2.0,\n"
            "segment.mp4?FragmentNumber=frag-123&SessionToken=segment\n"
        )

        def get(url, timeout):
            if "media.m3u8" in url:
                return Response(text=media, url=url)
            if "init.mp4" in url:
                return Response(content=b"exact-init", url=url)
            if "segment.mp4" in url:
                return Response(content=b"exact-segment", url=url)
            raise AssertionError("unexpected URL")

        probes = (
            (lambda *_args: (_ for _ in ()).throw(
                NvdecCaptureError("synthetic probe failure")
            ), "packet_probe_failed"),
            (lambda *_args: (
                _PacketSample(0, Fraction(1, 10), Fraction(1, 20)),
                _PacketSample(1, Fraction(1, 10), Fraction(1, 20)),
            ), "fragment_clock_rejected"),
        )
        for probe, expected_diagnostic in probes:
            with self.subTest(probe=probe):
                mediator = _LoopbackHlsMediator(
                    source, master, http_get=get, packet_probe=probe
                )
                try:
                    local_media = next(
                        line for line in mediator.master.decode().splitlines()
                        if line.startswith("http://")
                    )
                    rewritten = requests.get(local_media, timeout=2).text
                    init_url = re.search(
                        r'URI="([^"]+)"', rewritten
                    ).group(1)
                    segment_url = next(
                        line for line in rewritten.splitlines()
                        if line.startswith("http://") and line != init_url
                    )
                    self.assertEqual(
                        requests.get(init_url, timeout=2).content,
                        b"exact-init",
                    )
                    segment = requests.get(segment_url, timeout=2)
                    self.assertEqual(segment.status_code, 200)
                    self.assertEqual(segment.content, b"exact-segment")
                    self.assertFalse(mediator._transport_evidence_enabled)
                    self.assertEqual(
                        mediator.transport_diagnostic(0.0),
                        expected_diagnostic,
                    )
                    self.assertIsNone(mediator.clock.metadata_at(100.0))
                finally:
                    mediator.close()

    def test_transport_diagnostic_is_linearized_with_failure_reason(self):
        entered = threading.Event()
        release = threading.Event()
        disabled = threading.Event()
        result = {}

        class BlockingClock:
            def classify_position(self, _position):
                entered.set()
                self_test.assertTrue(release.wait(1.0))
                return "matched"

            def clear(self):
                return None

        self_test = self
        mediator = _LoopbackHlsMediator(
            "https://example.test/master.m3u8",
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmedia.m3u8\n",
            http_get=lambda *_args, **_kwargs: None,
            packet_probe=None,
        )
        mediator.clock = BlockingClock()

        def classify():
            result["diagnostic"] = mediator.transport_diagnostic(0.0)

        def disable():
            mediator._disable_transport_evidence("packet_probe_failed")
            disabled.set()

        classify_thread = threading.Thread(target=classify)
        disable_thread = threading.Thread(target=disable)
        try:
            classify_thread.start()
            self.assertTrue(entered.wait(1.0))
            disable_thread.start()
            self.assertFalse(disabled.wait(0.05))
            release.set()
            classify_thread.join(1.0)
            disable_thread.join(1.0)
            self.assertFalse(classify_thread.is_alive())
            self.assertFalse(disable_thread.is_alive())
            self.assertEqual(result["diagnostic"], "matched")
            self.assertEqual(
                mediator.transport_diagnostic(0.0),
                "packet_probe_failed",
            )
        finally:
            release.set()
            classify_thread.join(1.0)
            disable_thread.join(1.0)
            mediator.close()

    def test_discontinuity_disables_evidence_without_blocking_decode(self):
        source = "https://example.test/master.m3u8?SessionToken=master"
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child\n"
        )
        media = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:17\n"
            "#EXT-X-MAP:URI=\"init.mp4?SessionToken=init\"\n"
            "#EXT-X-DISCONTINUITY\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.000Z\n"
            "#EXTINF:2.0,\n"
            "segment.mp4?FragmentNumber=frag-123&SessionToken=segment\n"
        )

        def get(url, timeout):
            if "media.m3u8" in url:
                return Response(text=media, url=url)
            if "init.mp4" in url:
                return Response(content=b"exact-init", url=url)
            if "segment.mp4" in url:
                return Response(content=b"exact-segment", url=url)
            raise AssertionError("unexpected URL")

        def forbidden_probe(*_args):
            raise AssertionError("discontinuous transport was probed")

        mediator = _LoopbackHlsMediator(
            source, master, http_get=get, packet_probe=forbidden_probe
        )
        try:
            local_media = next(
                line for line in mediator.master.decode().splitlines()
                if line.startswith("http://")
            )
            rewritten = requests.get(local_media, timeout=2).text
            self.assertIn("#EXT-X-DISCONTINUITY", rewritten)
            init_url = re.search(r'URI="([^"]+)"', rewritten).group(1)
            segment_url = next(
                line for line in rewritten.splitlines()
                if line.startswith("http://") and line != init_url
            )
            self.assertEqual(requests.get(init_url, timeout=2).status_code, 200)
            segment = requests.get(segment_url, timeout=2)
            self.assertEqual(segment.status_code, 200)
            self.assertEqual(segment.content, b"exact-segment")
            self.assertFalse(mediator._transport_evidence_enabled)
            self.assertIsNone(mediator.clock.metadata_at(100.0))
        finally:
            mediator.close()

    def test_loopback_mediator_returns_only_fixed_secret_free_errors(self):
        source = "https://example.test/master.m3u8?SessionToken=secret"
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child-secret\n"
        )
        mediator = _LoopbackHlsMediator(
            source,
            master,
            http_get=lambda url, timeout: Response(
                text="secret", url=url, status=500
            ),
            packet_probe=lambda *_args: (),
        )
        try:
            local_media = next(
                line for line in mediator.master.decode().splitlines()
                if line.startswith("http://")
            )
            response = requests.get(local_media, timeout=2)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.text, "HLS mediation failed")
            self.assertNotIn("secret", response.text.lower())
        finally:
            close_started = time.monotonic()
            mediator.close()
            self.assertLess(time.monotonic() - close_started, 0.5)

    def test_failed_close_still_scrubs_every_signed_reference(self):
        source = "https://example.test/master.m3u8?SessionToken=secret"
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child-secret\n"
        )
        mediator = _LoopbackHlsMediator(
            source,
            master,
            http_get=lambda *_args, **_kwargs: Response(content=b"unused"),
        )
        with mediator._active_condition:
            mediator._active_requests = 1
        with self.assertRaisesRegex(NvdecCaptureError, "quiesce"):
            mediator.close(timeout=0.0)
        self.assertIsNone(mediator._media_url)
        self.assertIsNone(mediator._origin_url)
        self.assertIsNone(mediator._http_get)
        self.assertIsNone(mediator._packet_probe)
        self.assertIsNone(mediator._token)
        self.assertEqual(mediator.master, b"")
        with mediator._routes_lock:
            self.assertEqual(mediator._routes, {})
            self.assertEqual(mediator._init_bytes, {})

    def test_capture_cancellation_closes_the_mediator(self):
        class Process:
            pid = 12345

            @staticmethod
            def poll():
                return None

        class Mediator:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        capture = object.__new__(FfmpegNvdecCapture)
        capture._cancel_watcher_stop = threading.Event()
        capture._cancel_event = threading.Event()
        capture._cancel_event.set()
        capture._process_lock = threading.RLock()
        capture._process = Process()
        capture._mediator = Mediator()
        with patch("ffmpeg_capture.os.killpg") as killpg:
            capture._watch_for_cancel()
        killpg.assert_called_once_with(12345, __import__("signal").SIGTERM)
        self.assertTrue(capture._mediator.closed)

    def test_loopback_mediator_keeps_four_bounded_playlist_generations(self):
        source = "https://example.test/master.m3u8?SessionToken=secret"
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "media.m3u8?SessionToken=child\n"
        )
        media = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:17\n"
            "#EXT-X-MAP:URI=\"init.mp4?SessionToken=init\"\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.000Z\n"
            "#EXTINF:2.0,\n"
            "segment.mp4?FragmentNumber=frag-123&SessionToken=media\n"
        )

        def get(url, timeout):
            if "media.m3u8" in url:
                return Response(text=media, url=url)
            if "init.mp4" in url:
                return Response(content=b"init", url=url)
            raise AssertionError("unexpected fetch")

        mediator = _LoopbackHlsMediator(
            source, master, http_get=get, packet_probe=lambda *_args: ()
        )
        try:
            local_media = next(
                line for line in mediator.master.decode().splitlines()
                if line.startswith("http://")
            )
            generations = [requests.get(local_media, timeout=2).text for _ in range(4)]
            first_init = re.search(r'URI="([^"]+)"', generations[0]).group(1)
            first_segment = next(
                line for line in generations[0].splitlines()
                if line.startswith("http://") and line != first_init
            )
            self.assertEqual(requests.get(first_init, timeout=2).status_code, 200)
            requests.get(local_media, timeout=2)
            self.assertEqual(requests.get(first_segment, timeout=2).status_code, 404)
            with mediator._routes_lock:
                self.assertLessEqual(len(mediator._route_generations), 4)
                self.assertLessEqual(len(mediator._routes), 24)
        finally:
            mediator.close()

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

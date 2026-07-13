import json
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from kinesis_utils import (  # noqa: E402
    _run_nvdec_fragment_match,
    get_kvs_hls_url,
    get_video_session_hls_url,
    resolve_hls_media_clock,
)


class Response:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        return None


class HlsMediaClockTests(unittest.TestCase):
    def test_nvdec_fragment_admission_caps_normal_and_urgent_work_together(self):
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def matcher(value):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.05)
                return value
            finally:
                with lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(
                lambda value: _run_nvdec_fragment_match(
                    matcher, (value,), {}, cancel_event=threading.Event()
                ),
                range(6),
            ))

        self.assertEqual(results, list(range(6)))
        self.assertEqual(maximum_active, 2)

    def test_nvdec_fragment_admission_cancels_while_waiting(self):
        start_lock = threading.Lock()
        started = 0
        both_started = threading.Event()
        release_first = threading.Event()

        def blocker(value):
            nonlocal started
            with start_lock:
                started += 1
                if started == 2:
                    both_started.set()
            release_first.wait(2.0)
            return value

        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(
                _run_nvdec_fragment_match,
                blocker,
                ("one",),
                {},
            )
            second = executor.submit(
                _run_nvdec_fragment_match,
                blocker,
                ("two",),
                {},
            )
            self.assertTrue(both_started.wait(1.0))
            cancelled = threading.Event()
            third = executor.submit(
                _run_nvdec_fragment_match,
                lambda: self.fail("cancelled matcher ran"),
                (),
                {},
                cancelled,
            )
            cancelled.set()
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                third.result(timeout=1.0)
            release_first.set()
            self.assertEqual(first.result(timeout=1.0), "one")
            self.assertEqual(second.result(timeout=1.0), "two")

    def test_executor_shutdown_completes_in_fresh_subprocess(self):
        code = """
import sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, sys.argv[1])
import kinesis_utils
kinesis_utils.shutdown_media_clock_executors()
kinesis_utils._NVDEC_FRAGMENT_MATCH_EXECUTOR = ThreadPoolExecutor(max_workers=1)
kinesis_utils._NVDEC_URGENT_FRAGMENT_MATCH_EXECUTOR = ThreadPoolExecutor(max_workers=1)
kinesis_utils._NVDEC_FRAGMENT_MATCH_EXECUTOR.submit(lambda: 1)
kinesis_utils._NVDEC_URGENT_FRAGMENT_MATCH_EXECUTOR.submit(lambda: 2)
kinesis_utils.shutdown_media_clock_executors()
print("clean")
"""
        completed = subprocess.run(
            [sys.executable, "-c", code, str(PERCEPTION_DIR)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        self.assertEqual(completed.stdout.strip(), "clean")

    def test_cancelled_resolution_does_not_request_a_signed_url(self):
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            resolve_hls_media_clock(
                "https://example.invalid/media.m3u8?token=secret",
                reference_frame="frame",
                capture_position_milliseconds=0.0,
                frame_identity=lambda frame: frame,
                http_get=lambda *_args, **_kwargs: self.fail(
                    "cancelled resolver issued a request"
                ),
                cancel_event=cancelled,
            )

    def test_resolves_pdt_and_persists_no_signed_urls(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000
media.m3u8?SessionToken=master-secret
"""
        media = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:17
#EXT-X-MAP:URI="init.mp4?SessionToken=init-secret"
#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23.138Z
#EXTINF:2.001,
getMP4MediaFragment.mp4?FragmentNumber=frag-123&SessionToken=media-secret
"""
        calls = []

        def get(url, timeout):
            calls.append((url, timeout))
            if len(calls) == 1:
                return Response(text=master)
            if len(calls) == 2:
                return Response(text=media)
            if len(calls) == 3:
                return Response(content=b"init")
            return Response(content=b"segment")

        clock = resolve_hls_media_clock(
            "https://example.invalid/master.m3u8?SessionToken=session-secret",
            reference_frame="target-frame",
            capture_position_milliseconds=0.0,
            frame_identity=lambda frame: frame,
            http_get=get,
            fragment_matcher=(
                lambda init, segment, target, identity: 1968.0
                if (init, segment, target) == (
                    b"init", b"segment", "target-frame"
                )
                else None
            ),
        )
        metadata = clock.metadata_at(250.5)

        self.assertEqual(metadata["media_timestamp_utc"], "2026-07-10T03:57:25.356Z")
        self.assertEqual(
            metadata["media_clock"]["anchor_program_date_time_utc"],
            "2026-07-10T03:57:23.138Z",
        )
        self.assertEqual(metadata["media_clock"]["anchor_fragment_id"], "frag-123")
        self.assertEqual(metadata["media_clock"]["anchor_media_sequence"], 17)
        self.assertEqual(metadata["media_clock"]["schema_version"], 1)
        self.assertEqual(metadata["media_clock"]["position_milliseconds"], 2218.5)
        self.assertEqual(
            metadata["media_clock"]["capture_position_milliseconds"], 250.5
        )
        self.assertEqual(
            metadata["media_clock"]["anchor_fragment_frame_offset_milliseconds"],
            1968.0,
        )

        restarted = clock.reanchor_from_exact_match(250.5, 0.0)
        self.assertEqual(
            restarted.metadata_at(0.0)["media_timestamp_utc"],
            metadata["media_timestamp_utc"],
        )

        serialized = json.dumps(metadata)
        rendered_clock = repr(clock)
        for secret in (
            "session-secret",
            "master-secret",
            "init-secret",
            "media-secret",
            "SessionToken",
            "https://",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, rendered_clock)

    def test_playlist_without_program_date_time_has_no_media_clock(self):
        response = Response(text="#EXTM3U\n#EXTINF:2.0,\nsegment.mp4\n")
        self.assertIsNone(
            resolve_hls_media_clock(
                "https://example.invalid/media.m3u8",
                reference_frame="frame",
                capture_position_milliseconds=0.0,
                frame_identity=lambda frame: frame,
                http_get=lambda _url, timeout: response,
            )
        )

    def test_rejects_invalid_capture_positions(self):
        responses = iter((
            Response(text=(
                "#EXTM3U\n"
                "#EXT-X-MAP:URI=init.mp4\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23Z\n"
                "#EXTINF:2.0,\nsegment.mp4\n"
            )),
            Response(content=b"init"),
            Response(content=b"segment"),
        ))
        clock = resolve_hls_media_clock(
            "https://example.invalid/media.m3u8",
            reference_frame="frame",
            capture_position_milliseconds=0.0,
            frame_identity=lambda frame: frame,
            http_get=lambda _url, timeout: next(responses),
            fragment_matcher=lambda *_args: 0.0,
        )
        self.assertIsNone(clock.metadata_at(-1))
        self.assertIsNone(clock.metadata_at(float("nan")))

    def test_no_exact_frame_match_returns_no_clock(self):
        responses = iter((
            Response(text=(
                "#EXTM3U\n"
                "#EXT-X-MAP:URI=init.mp4\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23Z\n"
                "#EXTINF:2.0,\nsegment.mp4\n"
            )),
            Response(content=b"init"),
            Response(content=b"segment"),
        ))
        self.assertIsNone(resolve_hls_media_clock(
            "https://example.invalid/media.m3u8",
            reference_frame="frame-not-in-fragment",
            capture_position_milliseconds=0.0,
            frame_identity=lambda frame: frame,
            http_get=lambda _url, timeout: next(responses),
            fragment_matcher=lambda *_args: None,
        ))

    def test_same_frame_in_two_fragments_is_ambiguous(self):
        responses = iter((
            Response(text=(
                "#EXTM3U\n"
                "#EXT-X-MAP:URI=init.mp4\n"
                "#EXT-X-MEDIA-SEQUENCE:10\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:23Z\n"
                "#EXTINF:2.0,\nsegment-1.mp4\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:25Z\n"
                "#EXTINF:2.0,\nsegment-2.mp4\n"
            )),
            Response(content=b"init"),
            Response(content=b"segment-1"),
            Response(content=b"segment-2"),
        ))
        self.assertIsNone(resolve_hls_media_clock(
            "https://example.invalid/media.m3u8",
            reference_frame="static-frame",
            capture_position_milliseconds=0.0,
            frame_identity=lambda frame: frame,
            http_get=lambda _url, timeout: next(responses),
            fragment_matcher=lambda *_args: 0.0,
        ))

    def test_trusted_lower_bound_prunes_only_obsolete_complete_fragments(self):
        media = (
            "#EXTM3U\n"
            "#EXT-X-MAP:URI=init.mp4\n"
            "#EXT-X-MEDIA-SEQUENCE:10\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:20Z\n"
            "#EXTINF:2.0,\nsegment-old.mp4\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:22Z\n"
            "#EXTINF:2.0,\nsegment-boundary.mp4\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T03:57:24Z\n"
            "#EXTINF:2.0,\nsegment-new.mp4\n"
        )
        requested = []

        def get(url, timeout):
            requested.append(url)
            if len(requested) == 1:
                return Response(text=media)
            if url.endswith("init.mp4"):
                return Response(content=b"init")
            return Response(content=url.rsplit("/", 1)[-1].encode())

        clock = resolve_hls_media_clock(
            "https://example.invalid/media.m3u8",
            reference_frame="target",
            capture_position_milliseconds=0.0,
            frame_identity=lambda frame: frame,
            http_get=get,
            fragment_matcher=lambda _init, segment, *_args: (
                250.0 if segment == b"segment-new.mp4" else None
            ),
            not_before_media_time_utc="2026-07-10T03:57:23.500Z",
        )

        self.assertIsNotNone(clock)
        self.assertFalse(any("segment-old.mp4" in url for url in requested))
        self.assertTrue(any("segment-boundary.mp4" in url for url in requested))
        self.assertTrue(any("segment-new.mp4" in url for url in requested))

    @patch("kinesis_utils.get_video_session_hls_url", return_value=None)
    @patch("kinesis_utils.boto3.client")
    def test_direct_kinesis_fallback_requests_pdt_fmp4_playlist(
        self, client, _api_session
    ):
        endpoint_client = Mock()
        endpoint_client.get_data_endpoint.return_value = {
            "DataEndpoint": "https://endpoint.invalid"
        }
        media_client = Mock()
        media_client.get_hls_streaming_session_url.return_value = {
            "HLSStreamingSessionURL": "signed-session"
        }
        client.side_effect = [endpoint_client, media_client]

        self.assertEqual(get_kvs_hls_url("stream-ch1"), "signed-session")
        media_client.get_hls_streaming_session_url.assert_called_once_with(
            StreamName="stream-ch1",
            PlaybackMode="LIVE",
            ContainerFormat="FRAGMENTED_MP4",
            DiscontinuityMode="ON_DISCONTINUITY",
            DisplayFragmentTimestamp="ALWAYS",
            MaxMediaPlaylistFragmentResults=4,
        )

    @patch("kinesis_utils.requests.get")
    def test_read_api_live_session_requests_bounded_low_latency_playlist(self, get):
        get.return_value.json.return_value = {
            "hlsUrl": "signed-session",
            "discontinuityMode": "ON_DISCONTINUITY",
        }
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_VIDEO_SESSION_API_BASE_URL": "https://api.invalid"},
            clear=False,
        ):
            self.assertEqual(
                get_video_session_hls_url("v2x-backend-cam-ch1", 4),
                "signed-session",
            )
        get.assert_called_once_with(
            "https://api.invalid/video/session/ch1",
            params={"max_fragments": "4"},
            headers={"accept": "application/json"},
            timeout=10,
        )
        get.return_value.raise_for_status.assert_called_once_with()

    @patch("kinesis_utils.requests.get")
    def test_read_api_rejects_unsafe_direct_discontinuity_mode(self, get):
        get.return_value.json.return_value = {
            "hlsUrl": "signed-session",
            "discontinuityMode": "ALWAYS",
        }
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_VIDEO_SESSION_API_BASE_URL": "https://api.invalid"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "discontinuity mode"):
                get_video_session_hls_url("v2x-backend-cam-ch1", 4)

    def test_live_fragment_count_rejects_latency_weakening(self):
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_VIDEO_SESSION_API_BASE_URL": "https://api.invalid"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "between 1 and 5"):
                get_video_session_hls_url("v2x-backend-cam-ch1", 0)

    @patch("kinesis_utils.get_video_session_hls_url")
    def test_perception_rejects_fragment_counts_outside_api_bound(self, api):
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_PERCEPTION_LIVE_HLS_FRAGMENTS": "0"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "between 1 and 5"):
                get_kvs_hls_url("v2x-backend-cam-ch1")
        api.assert_not_called()

    @patch("kinesis_utils.get_video_session_hls_url")
    def test_explicit_capture_and_clock_fragment_windows(self, api):
        api.side_effect = ["capture-session", "clock-session"]
        self.assertEqual(
            get_kvs_hls_url("v2x-backend-cam-ch1", max_fragments=1),
            "capture-session",
        )
        self.assertEqual(
            get_kvs_hls_url("v2x-backend-cam-ch1", max_fragments=5),
            "clock-session",
        )
        self.assertEqual(
            [call.args[1] for call in api.call_args_list],
            [1, 5],
        )


if __name__ == "__main__":
    unittest.main()

import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from kinesis_utils import (  # noqa: E402
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
            DiscontinuityMode="ALWAYS",
            DisplayFragmentTimestamp="ALWAYS",
            MaxMediaPlaylistFragmentResults=4,
        )

    @patch("kinesis_utils.requests.get")
    def test_read_api_live_session_requests_bounded_low_latency_playlist(self, get):
        get.return_value.json.return_value = {"hlsUrl": "signed-session"}
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

    def test_live_fragment_count_rejects_latency_weakening(self):
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_VIDEO_SESSION_API_BASE_URL": "https://api.invalid"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "between 2 and 5"):
                get_video_session_hls_url("v2x-backend-cam-ch1", 1)

    @patch("kinesis_utils.get_video_session_hls_url")
    def test_perception_rejects_fragment_counts_outside_api_bound(self, api):
        with patch.dict(
            "kinesis_utils.os.environ",
            {"V2X_PERCEPTION_LIVE_HLS_FRAGMENTS": "1"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "between 2 and 5"):
                get_kvs_hls_url("v2x-backend-cam-ch1")
        api.assert_not_called()

    @patch("kinesis_utils.get_video_session_hls_url")
    def test_explicit_capture_and_clock_fragment_windows(self, api):
        api.side_effect = ["capture-session", "clock-session"]
        self.assertEqual(
            get_kvs_hls_url("v2x-backend-cam-ch1", max_fragments=2),
            "capture-session",
        )
        self.assertEqual(
            get_kvs_hls_url("v2x-backend-cam-ch1", max_fragments=5),
            "clock-session",
        )
        self.assertEqual(
            [call.args[1] for call in api.call_args_list],
            [2, 5],
        )


if __name__ == "__main__":
    unittest.main()

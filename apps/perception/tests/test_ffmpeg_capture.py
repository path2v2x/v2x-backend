import sys
from pathlib import Path
import unittest


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from ffmpeg_capture import (  # noqa: E402
    NvdecCaptureError,
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


if __name__ == "__main__":
    unittest.main()

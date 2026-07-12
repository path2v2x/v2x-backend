"""Low-latency FFmpeg/NVDEC capture without exposing signed HLS URLs.

The pip OpenCV wheel bundles an FFmpeg build without NVIDIA decoders.  The
host FFmpeg does provide ``h264_cuvid``.  This adapter lets the host process
decode into a timestamped NUT/rawvideo FIFO and keeps the existing OpenCV
capture interface for the rest of the perception pipeline.

Signed Kinesis URLs are never placed in a child command line or on disk.  A
validated HLS master playlist is rewritten to absolute same-origin URLs and
held in an anonymous memfd inherited by the FFmpeg child.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from urllib.parse import urljoin, urlparse

import cv2
import requests


_PROTOCOL_WHITELIST = "file,crypto,data,http,https,tcp,tls"
_MASTER_PLAYLIST_LIMIT = 64 * 1024


class NvdecCaptureError(RuntimeError):
    """A deliberately sanitized capture setup failure."""


@dataclass(frozen=True)
class NvdecFrameIdentity:
    """Fast reject key plus the authoritative full decoded-frame digest."""

    quick: bytes
    exact: bytes


def quick_frame_identity(frame, axis_samples=64):
    """Hash a distributed bounded sample before an authoritative exact hash."""
    digest = hashlib.blake2b(digest_size=16)
    shape = tuple(getattr(frame, "shape", ()) or ())
    dtype = str(getattr(frame, "dtype", ""))
    digest.update(repr((shape, dtype)).encode("ascii", errors="replace"))
    if len(shape) >= 2 and hasattr(frame, "__getitem__"):
        height, width = max(1, int(shape[0])), max(1, int(shape[1]))
        y_step = max(1, (height + axis_samples - 1) // axis_samples)
        x_step = max(1, (width + axis_samples - 1) // axis_samples)
        try:
            digest.update(frame[::y_step, ::x_step].tobytes())
            for y, x in (
                (0, 0),
                (0, width - 1),
                (height - 1, 0),
                (height - 1, width - 1),
                (height // 2, width // 2),
            ):
                digest.update(
                    frame[
                        max(0, y - 2):min(height, y + 3),
                        max(0, x - 2):min(width, x + 3),
                    ].tobytes()
                )
            return digest.digest()
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
    raw = frame if isinstance(frame, bytes) else repr(frame).encode("utf-8")
    digest.update(raw[:32_768])
    return digest.digest()


def build_nvdec_frame_identity(frame, exact_identity):
    return NvdecFrameIdentity(
        quick=quick_frame_identity(frame),
        exact=exact_identity(frame),
    )


def rewrite_hls_master(source_url, playlist_text):
    """Return a bounded, absolute, same-origin HLS variant playlist.

    A media playlist cannot safely be materialized once into a memfd because
    FFmpeg must poll its live URL for new fragments.  Requiring a master
    playlist preserves that refresh behavior while keeping its signed child
    URL out of the process arguments and filesystem.
    """
    if not isinstance(playlist_text, str):
        raise NvdecCaptureError("HLS master response is not text")
    encoded = playlist_text.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > _MASTER_PLAYLIST_LIMIT:
        raise NvdecCaptureError("HLS master response is outside the safe bound")

    source = urlparse(str(source_url))
    if source.scheme != "https" or not source.hostname:
        raise NvdecCaptureError("HLS master source must be HTTPS")
    if source.username or source.password or source.fragment:
        raise NvdecCaptureError("HLS master source has forbidden URL components")

    lines = playlist_text.splitlines()
    if not any(line.strip().startswith("#EXT-X-STREAM-INF:") for line in lines):
        raise NvdecCaptureError("HLS source is not a live variant playlist")

    uri_count = 0
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            rewritten.append(line)
            continue
        absolute = urljoin(str(source_url), stripped)
        child = urlparse(absolute)
        if (
            child.scheme != "https"
            or child.hostname != source.hostname
            or child.port != source.port
            or child.username
            or child.password
            or child.fragment
        ):
            raise NvdecCaptureError("HLS child playlist is not same-origin HTTPS")
        uri_count += 1
        rewritten.append(absolute)

    if uri_count < 1 or uri_count > 4:
        raise NvdecCaptureError("HLS master has an unsupported variant count")
    return ("\n".join(rewritten) + "\n").encode("utf-8")


def build_nvdec_command(ffmpeg_binary, input_reference, fifo_path, *, hls):
    """Build a command containing only local input references, never URLs."""
    command = [
        str(ffmpeg_binary),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if hls:
        command.extend(
            [
                "-protocol_whitelist",
                _PROTOCOL_WHITELIST,
                "-f",
                "hls",
            ]
        )
    command.extend(
        [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            str(input_reference),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-fps_mode",
            "passthrough",
            "-vf",
            "hwdownload,format=nv12,format=bgr24",
            "-c:v",
            "rawvideo",
            "-f",
            "nut",
            "-y",
            str(fifo_path),
        ]
    )
    return command


class FfmpegNvdecCapture:
    """A small subset of ``cv2.VideoCapture`` backed by host NVDEC."""

    def __init__(
        self,
        source_url=None,
        *,
        file_path=None,
        open_timeout_ms=10_000,
        read_timeout_ms=10_000,
        ffmpeg_binary="/usr/bin/ffmpeg",
        http_get=requests.get,
    ):
        if (source_url is None) == (file_path is None):
            raise ValueError("provide exactly one of source_url or file_path")
        self._capture = None
        self._process = None
        self._memfd = None
        self._temporary_directory = None
        self._opened = False

        binary = Path(ffmpeg_binary)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise NvdecCaptureError("NVDEC FFmpeg binary is unavailable")
        if shutil.which(str(binary)) is None:
            raise NvdecCaptureError("NVDEC FFmpeg binary is not executable")

        try:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="v2x-nvdec-"
            )
            fifo_path = Path(self._temporary_directory.name) / "frames.nut"
            os.mkfifo(fifo_path, 0o600)

            pass_fds = ()
            if source_url is not None:
                response = http_get(str(source_url), timeout=10)
                response.raise_for_status()
                master = rewrite_hls_master(str(source_url), response.text)
                self._memfd = os.memfd_create("v2x-hls-master", 0)
                os.write(self._memfd, master)
                os.lseek(self._memfd, 0, os.SEEK_SET)
                input_reference = f"/proc/self/fd/{self._memfd}"
                pass_fds = (self._memfd,)
                is_hls = True
                # Drop the Python reference before launching the child.  The
                # URL itself is not retained on this object.
                source_url = None
            else:
                input_file = Path(file_path).resolve(strict=True)
                if not input_file.is_file():
                    raise NvdecCaptureError("NVDEC input is not a regular file")
                input_reference = str(input_file)
                is_hls = False

            command = build_nvdec_command(
                binary, input_reference, fifo_path, hls=is_hls
            )
            self._process = subprocess.Popen(
                command,
                pass_fds=pass_fds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                # FFmpeg errors can contain signed child URLs. Never retain or
                # publish its raw stderr; health receives a fixed safe error.
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            params = []
            open_timeout = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
            read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
            if open_timeout is not None:
                params.extend([open_timeout, int(open_timeout_ms)])
            if read_timeout is not None:
                params.extend([read_timeout, int(read_timeout_ms)])
            self._capture = cv2.VideoCapture(
                str(fifo_path), cv2.CAP_FFMPEG, params
            )
            self._opened = bool(
                self._capture is not None
                and self._capture.isOpened()
                and self._process.poll() is None
            )
            if not self._opened:
                raise NvdecCaptureError("NVDEC capture open failed")
        except Exception:
            self.release()
            raise
        finally:
            if self._memfd is not None:
                os.close(self._memfd)
                self._memfd = None

    @classmethod
    def from_file(cls, file_path, **kwargs):
        return cls(file_path=file_path, **kwargs)

    def isOpened(self):
        return bool(
            self._opened
            and self._capture is not None
            and self._capture.isOpened()
            and self._process is not None
            and self._process.poll() is None
        )

    def read(self):
        if not self.isOpened():
            return False, None
        return self._capture.read()

    def get(self, property_id):
        if self._capture is None:
            return 0.0
        return self._capture.get(property_id)

    def release(self):
        self._opened = False
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()

        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass

        if self._memfd is not None:
            try:
                os.close(self._memfd)
            except OSError:
                pass
            self._memfd = None
        temporary, self._temporary_directory = self._temporary_directory, None
        if temporary is not None:
            temporary.cleanup()


def match_fragment_frame_nvdec(
    init_bytes,
    segment_bytes,
    target_identity,
    frame_identity,
    *,
    capture_factory=None,
):
    """Match exactly one decoded frame using the same NVDEC pixel path."""
    capture_factory = capture_factory or FfmpegNvdecCapture.from_file
    capture = None
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="v2x-hls-clock-nvdec-", suffix=".mp4", delete=False
        ) as fragment_file:
            path = fragment_file.name
            fragment_file.write(init_bytes)
            fragment_file.write(segment_bytes)

        capture = capture_factory(path)
        if capture is None or not capture.isOpened():
            return None
        matches = []
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            position = capture.get(cv2.CAP_PROP_POS_MSEC)
            if isinstance(target_identity, NvdecFrameIdentity):
                matches_target = (
                    quick_frame_identity(frame) == target_identity.quick
                    and frame_identity(frame) == target_identity.exact
                )
            else:
                matches_target = frame_identity(frame) == target_identity
            if matches_target:
                matches.append(float(position))
        return matches[0] if len(matches) == 1 else None
    finally:
        if capture is not None:
            capture.release()
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

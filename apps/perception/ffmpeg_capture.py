"""Low-latency FFmpeg/NVDEC capture without exposing signed HLS URLs.

The pip OpenCV wheel bundles an FFmpeg build without NVIDIA decoders.  The
host FFmpeg does provide ``h264_cuvid``.  This adapter lets the host process
decode into a timestamped NUT/rawvideo FIFO and keeps the existing OpenCV
capture interface for the rest of the perception pipeline.

Signed Kinesis URLs are never placed in a child command line or on disk.  A
validated HLS session is mediated through a bounded loopback server, and the
FFmpeg child inherits only a local master playlist through an anonymous memfd.
"""

from __future__ import annotations

from collections import deque, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urljoin, urlparse

import cv2
import requests


_PROTOCOL_WHITELIST = "file,http,tcp"
_MASTER_PLAYLIST_LIMIT = 64 * 1024
_MEDIA_PLAYLIST_LIMIT = 128 * 1024
_HLS_RESOURCE_LIMIT = 32 * 1024 * 1024
_FFPROBE_OUTPUT_LIMIT = 2 * 1024 * 1024
_MAX_FRAGMENT_PACKETS = 10_000
_SIDECAR_LINE_LIMIT = 512
_SIDECAR_QUEUE_LIMIT = 64
_HLS_IO_TIMEOUT_MICROSECONDS = 7_000_000
_HLS_HOLD_COUNTERS = 3
_HLS_FETCH_TIMEOUT_SECONDS = 7.0
_HLS_PACKET_PROBE_TIMEOUT_SECONDS = 5.0
_HLS_MEDIATOR_CLOSE_TIMEOUT_SECONDS = 13.0


class NvdecCaptureError(RuntimeError):
    """A deliberately sanitized capture setup failure."""


@dataclass(frozen=True)
class NvdecFrameIdentity:
    """Fast reject key plus the authoritative full decoded-frame digest."""

    quick: bytes
    exact: bytes


@dataclass(frozen=True)
class FragmentFrameSequenceMatch:
    """Exact contiguous match plus every decoded fragment-frame position."""

    frame_offset_milliseconds: float
    frame_positions_milliseconds: tuple


@dataclass(frozen=True)
class _PacketSample:
    index: int
    pts: Fraction
    time_base: Fraction


@dataclass(frozen=True)
class _MediatedFragment:
    program_date_time_epoch: float
    program_date_time_utc: str
    duration_seconds: float
    media_sequence: int
    fragment_id: str
    init_url: str
    segment_url: str
    discontinuity: bool = False


@dataclass(frozen=True)
class _TransportPoint:
    source_pts: Fraction
    media_epoch: float
    program_date_time_utc: str
    fragment_offset: Fraction
    fragment_id: str
    media_sequence: int
    duration_seconds: float
    sample_index: int
    sample_time_base: Fraction


@dataclass(frozen=True)
class _FragmentTimeline:
    fragment_id: str
    media_sequence: int
    program_date_time_epoch: float
    first_pts: Fraction
    last_pts: Fraction
    tick: Fraction


def _utc_iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


class SameSessionTransportClock:
    """Exact, piecewise UTC mapping from one mediated HLS capture session."""

    evidence_method = "exact_same_session_pts"

    def __init__(self, max_positions=8192):
        if (
            isinstance(max_positions, bool)
            or not isinstance(max_positions, int)
            or max_positions < 1
        ):
            raise ValueError("transport clock position bound is invalid")
        self._lock = threading.RLock()
        self._max_positions = max_positions
        self._positions = OrderedDict()
        self._float_positions = {}
        self._fragments = OrderedDict()

    @staticmethod
    def _position_key(position_milliseconds):
        try:
            position = float(position_milliseconds)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(position):
            return None
        return position

    def add_fragment(self, fragment, samples):
        samples = tuple(samples)
        if not samples:
            raise NvdecCaptureError("HLS fragment has no timestamped video packets")
        if len({sample.pts for sample in samples}) != len(samples):
            raise NvdecCaptureError("HLS fragment packet timestamps are ambiguous")
        samples = tuple(sorted(samples, key=lambda sample: sample.pts))
        if any(sample.pts < 0 or sample.time_base <= 0 for sample in samples):
            raise NvdecCaptureError("HLS fragment packet PTS is invalid")
        if any(
            (sample.pts / sample.time_base).denominator != 1
            for sample in samples
        ):
            raise NvdecCaptureError("HLS fragment packet PTS is not integral")
        first_pts = samples[0].pts
        if float(samples[-1].pts - first_pts) > (
            fragment.duration_seconds
            + float(max(sample.time_base for sample in samples))
        ):
            raise NvdecCaptureError("HLS fragment packet span is invalid")
        timeline = _FragmentTimeline(
            fragment_id=fragment.fragment_id,
            media_sequence=fragment.media_sequence,
            program_date_time_epoch=fragment.program_date_time_epoch,
            first_pts=first_pts,
            last_pts=samples[-1].pts,
            tick=max(sample.time_base for sample in samples),
        )
        with self._lock:
            existing = self._fragments.get(fragment.fragment_id)
            if existing is not None and existing != timeline:
                raise NvdecCaptureError("HLS fragment timeline changed")
            if existing is None:
                candidate_timelines = tuple(self._fragments.values()) + (
                    timeline,
                )
                if (
                    self._fragments
                    and timeline.media_sequence
                    < min(
                        item.media_sequence
                        for item in self._fragments.values()
                    )
                ):
                    raise NvdecCaptureError("HLS fragment timeline moved backward")
                ordered = sorted(
                    candidate_timelines,
                    key=lambda item: item.media_sequence,
                )
                if len({item.media_sequence for item in ordered}) != len(ordered):
                    raise NvdecCaptureError("HLS media sequence is ambiguous")
                for earlier, later in zip(ordered, ordered[1:]):
                    pts_delta = later.first_pts - earlier.first_pts
                    pdt_delta = (
                        later.program_date_time_epoch
                        - earlier.program_date_time_epoch
                    )
                    tolerance = max(
                        0.001,
                        float(earlier.tick),
                        float(later.tick),
                    )
                    if (
                        pts_delta <= 0
                        or later.first_pts <= earlier.last_pts
                        or pdt_delta <= 0.0
                        or abs(float(pts_delta) - pdt_delta) > tolerance
                    ):
                        raise NvdecCaptureError(
                            "HLS fragment PDT timeline is inconsistent"
                        )
                self._fragments[fragment.fragment_id] = timeline
                self._fragments.move_to_end(fragment.fragment_id)
                while len(self._fragments) > 128:
                    self._fragments.popitem(last=False)
            for presentation_index, sample in enumerate(samples):
                offset = sample.pts - first_pts
                point = _TransportPoint(
                    source_pts=sample.pts,
                    media_epoch=(
                        fragment.program_date_time_epoch + float(offset)
                    ),
                    program_date_time_utc=fragment.program_date_time_utc,
                    fragment_offset=offset,
                    fragment_id=fragment.fragment_id,
                    media_sequence=fragment.media_sequence,
                    duration_seconds=fragment.duration_seconds,
                    sample_index=presentation_index,
                    sample_time_base=sample.time_base,
                )
                key = sample.pts
                missing = object()
                prior = self._positions.get(key, missing)
                if prior is not missing and prior != point:
                    self._positions[key] = None
                else:
                    self._positions[key] = point
                self._positions.move_to_end(key)
                while len(self._positions) > self._max_positions:
                    self._positions.popitem(last=False)
            self._float_positions = {}
            for rational_position in self._positions:
                float_key = self._position_key(
                    float(rational_position * 1000)
                )
                prior = self._float_positions.get(float_key, missing)
                if prior is not missing and prior != rational_position:
                    self._float_positions[float_key] = None
                else:
                    self._float_positions[float_key] = rational_position

    def has_position(self, position_milliseconds):
        key = self._position_key(position_milliseconds)
        with self._lock:
            rational_position = self._float_positions.get(key)
            return (
                key is not None
                and rational_position is not None
                and self._positions.get(rational_position) is not None
            )

    def metadata_at(self, position_milliseconds):
        key = self._position_key(position_milliseconds)
        with self._lock:
            rational_position = self._float_positions.get(key)
            point = (
                None
                if rational_position is None
                else self._positions.get(rational_position)
            )
        if point is None:
            return None
        offset_milliseconds = float(point.fragment_offset * 1000)
        position = float(point.source_pts * 1000)
        return {
            "media_timestamp_utc": _utc_iso(point.media_epoch),
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "schema_version": 1,
                "evidence_method": self.evidence_method,
                "anchor_program_date_time_utc": (
                    point.program_date_time_utc
                ),
                "anchor_fragment_frame_offset_milliseconds": round(
                    offset_milliseconds, 3
                ),
                "anchor_capture_position_milliseconds": round(position, 3),
                "position_milliseconds": round(offset_milliseconds, 3),
                "capture_position_milliseconds": round(position, 3),
                "anchor_fragment_id": point.fragment_id,
                "anchor_media_sequence": point.media_sequence,
                "segment_duration_seconds": point.duration_seconds,
                "source_pts": int(point.source_pts / point.sample_time_base),
                "source_time_base_numerator": (
                    point.sample_time_base.numerator
                ),
                "source_time_base_denominator": (
                    point.sample_time_base.denominator
                ),
                "fragment_sample_index": point.sample_index,
            },
        }

    def clear(self):
        with self._lock:
            self._positions.clear()
            self._float_positions.clear()
            self._fragments.clear()


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


def _url_origin(value):
    parsed = urlparse(str(value))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise NvdecCaptureError("HLS URL has forbidden components")
    port = parsed.port or 443
    return parsed.hostname.lower(), port


def _same_origin_url(base_url, value):
    absolute = urljoin(str(base_url), str(value))
    if _url_origin(absolute) != _url_origin(base_url):
        raise NvdecCaptureError("HLS resource is not same-origin HTTPS")
    return absolute


def _has_uri_attribute(line):
    return bool(re.search(
        r"(?:^|[:,])\s*[A-Z0-9-]*URI\s*=",
        str(line),
        re.IGNORECASE,
    ))


def _master_media_url(source_url, playlist_text):
    if not isinstance(playlist_text, str):
        raise NvdecCaptureError("HLS master response is not text")
    encoded = playlist_text.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > _MASTER_PLAYLIST_LIMIT:
        raise NvdecCaptureError("HLS master response is outside the safe bound")
    _url_origin(source_url)
    lines = playlist_text.splitlines()
    if any(
        line.strip().startswith("#") and _has_uri_attribute(line.strip())
        for line in lines
    ):
        raise NvdecCaptureError("HLS master URI-bearing tag is unsupported")
    if not any(line.strip().startswith("#EXT-X-STREAM-INF:") for line in lines):
        raise NvdecCaptureError("HLS source is not a live variant playlist")
    children = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(children) != 1:
        raise NvdecCaptureError("HLS master must contain exactly one variant")
    return _same_origin_url(source_url, children[0])


def _parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise NvdecCaptureError("HLS program date time is invalid") from exc
    if parsed.tzinfo is None:
        raise NvdecCaptureError("HLS program date time lacks a timezone")
    return parsed.timestamp(), _utc_iso(parsed.timestamp())


def _map_uri(line):
    match = re.search(r'(?:^|[:,])URI=(?:"([^"]+)"|([^,]+))', line)
    if match is None:
        raise NvdecCaptureError("HLS initialization map has no URI")
    return (match.group(1) or match.group(2)).strip()


def _safe_fragment_id(segment_url):
    values = parse_qs(urlparse(segment_url).query).get("FragmentNumber") or []
    if len(values) != 1 or not re.fullmatch(
        r"[A-Za-z0-9._~-]{1,256}", values[0]
    ):
        raise NvdecCaptureError("HLS fragment identity is unavailable")
    return values[0]


def _parse_media_playlist(media_url, playlist_text):
    if not isinstance(playlist_text, str):
        raise NvdecCaptureError("HLS media response is not text")
    encoded = playlist_text.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > _MEDIA_PLAYLIST_LIMIT:
        raise NvdecCaptureError("HLS media response is outside the safe bound")
    if not playlist_text.lstrip().startswith("#EXTM3U"):
        raise NvdecCaptureError("HLS media playlist is invalid")
    if "#EXT-X-BYTERANGE" in playlist_text:
        raise NvdecCaptureError("HLS byte ranges are unsupported")
    if "#EXT-X-KEY" in playlist_text:
        raise NvdecCaptureError("HLS encrypted resources are unsupported")

    media_sequence = None
    segment_index = 0
    init_url = None
    pending_pdt = None
    pending_duration = None
    pending_discontinuity = False
    fragments = []
    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError as exc:
                raise NvdecCaptureError("HLS media sequence is invalid") from exc
            if media_sequence < 0:
                raise NvdecCaptureError("HLS media sequence is invalid")
            continue
        if line.startswith("#EXT-X-MAP:"):
            if "BYTERANGE=" in line:
                raise NvdecCaptureError("HLS byte ranges are unsupported")
            init_url = _same_origin_url(media_url, _map_uri(line))
            continue
        if line.startswith("#") and _has_uri_attribute(line):
            raise NvdecCaptureError("HLS URI-bearing tag is unsupported")
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = _parse_datetime(line.split(":", 1)[1])
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(
                    line.split(":", 1)[1].split(",", 1)[0]
                )
            except ValueError as exc:
                raise NvdecCaptureError("HLS fragment duration is invalid") from exc
            if not math.isfinite(pending_duration) or pending_duration <= 0.0:
                raise NvdecCaptureError("HLS fragment duration is invalid")
            continue
        if line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
            continue
        if line.startswith("#"):
            continue
        if (
            media_sequence is None
            or init_url is None
            or pending_pdt is None
            or pending_duration is None
        ):
            raise NvdecCaptureError("HLS fragment provenance is incomplete")
        segment_url = _same_origin_url(media_url, line)
        fragments.append(_MediatedFragment(
            program_date_time_epoch=pending_pdt[0],
            program_date_time_utc=pending_pdt[1],
            duration_seconds=pending_duration,
            media_sequence=media_sequence + segment_index,
            fragment_id=_safe_fragment_id(segment_url),
            init_url=init_url,
            segment_url=segment_url,
            discontinuity=pending_discontinuity,
        ))
        segment_index += 1
        pending_pdt = None
        pending_duration = None
        pending_discontinuity = False
    if pending_duration is not None:
        raise NvdecCaptureError("HLS fragment URI is missing")
    if not fragments or len(fragments) > 5:
        raise NvdecCaptureError("HLS media fragment count is unsafe")
    return tuple(fragments)


def _remaining_fetch_time(deadline, label, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise NvdecCaptureError(f"{label} request cancelled")
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0.0:
        raise NvdecCaptureError(f"{label} request deadline exceeded")
    return remaining


def _bounded_response(
    response,
    origin_url,
    limit,
    label,
    *,
    deadline,
    cancel_event=None,
):
    _remaining_fetch_time(deadline, label, cancel_event)
    status = getattr(response, "status_code", getattr(response, "status", None))
    if isinstance(status, int) and 300 <= status < 400:
        raise NvdecCaptureError(f"{label} redirect is unsupported")
    if bool(getattr(response, "is_redirect", False)) or bool(
        getattr(response, "is_permanent_redirect", False)
    ):
        raise NvdecCaptureError(f"{label} redirect is unsupported")
    if getattr(response, "history", None):
        raise NvdecCaptureError(f"{label} redirect is unsupported")
    try:
        response.raise_for_status()
    except Exception:
        raise NvdecCaptureError(f"{label} request failed") from None
    _remaining_fetch_time(deadline, label, cancel_event)
    final_url = getattr(response, "url", None)
    if final_url and _url_origin(final_url) != _url_origin(origin_url):
        raise NvdecCaptureError(f"{label} redirect is not same-origin")
    headers = getattr(response, "headers", {})
    try:
        declared_length = int(headers.get("Content-Length", 0))
    except (AttributeError, TypeError, ValueError):
        declared_length = 0
    if declared_length < 0 or declared_length > limit:
        raise NvdecCaptureError(f"{label} response is outside the safe bound")
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        content = bytearray()
        try:
            chunks = iter(iterator(chunk_size=64 * 1024))
            while True:
                _remaining_fetch_time(deadline, label, cancel_event)
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                _remaining_fetch_time(deadline, label, cancel_event)
                if not isinstance(chunk, bytes):
                    raise TypeError
                content.extend(chunk)
                if len(content) > limit:
                    raise NvdecCaptureError(
                        f"{label} response is outside the safe bound"
                    )
        except NvdecCaptureError:
            raise
        except Exception:
            raise NvdecCaptureError(f"{label} response is invalid") from None
        content = bytes(content)
    else:
        content = getattr(response, "content", None)
        _remaining_fetch_time(deadline, label, cancel_event)
    if content is None:
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise NvdecCaptureError(f"{label} response is invalid")
        content = text.encode("utf-8", errors="strict")
    if not isinstance(content, bytes) or not content or len(content) > limit:
        raise NvdecCaptureError(f"{label} response is outside the safe bound")
    _remaining_fetch_time(deadline, label, cancel_event)
    return content


_FETCH_HELPER_SOURCE = r'''
import json
import os
import sys
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def write_all(descriptor, body):
    view = memoryview(body)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]

try:
    descriptor = int(sys.argv[1])
    os.lseek(descriptor, 0, os.SEEK_SET)
    config = json.loads(os.read(descriptor, 256 * 1024))
    limit = int(config["limit"])
    timeout = float(config["timeout"])
    request = urllib.request.Request(
        config["url"],
        headers={
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "v2x-hls-fetch/1",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        status = int(response.getcode())
        if status < 200 or status >= 300:
            raise RuntimeError("unexpected response")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > limit:
            raise RuntimeError("response too large")
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RuntimeError("response too large")
            write_all(1, chunk)
    if total <= 0:
        raise RuntimeError("empty response")
except BaseException:
    os._exit(2)
'''


def _fetch_bounded_requests_process(
    url,
    *,
    deadline,
    limit,
    label,
    cancel_event=None,
):
    if cancel_event is not None and cancel_event.is_set():
        raise NvdecCaptureError(f"{label} request cancelled")
    descriptor = os.memfd_create("v2x-hls-fetch", 0)
    try:
        remaining = _remaining_fetch_time(deadline, label, cancel_event)
        config = json.dumps({
            "url": str(url),
            "timeout": remaining,
            "limit": int(limit),
        }, separators=(",", ":")).encode("utf-8")
        view = memoryview(config)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise NvdecCaptureError(f"{label} request failed")
            view = view[written:]
        command = [
            sys.executable,
            "-I",
            "-c",
            _FETCH_HELPER_SOURCE,
            str(descriptor),
        ]
        body = _run_bounded_command(
            command,
            pass_fds=(descriptor,),
            timeout=_remaining_fetch_time(deadline, label, cancel_event),
            output_limit=int(limit),
            error_message=f"{label} request failed",
            cancel_event=cancel_event,
            absolute_deadline=deadline,
        )
        if not body:
            raise NvdecCaptureError(f"{label} response is outside the safe bound")
        return body
    finally:
        try:
            os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)


def _fetch_bounded(
    http_get,
    url,
    *,
    timeout,
    origin_url,
    limit,
    label,
    cancel_event=None,
):
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as exc:
        raise NvdecCaptureError(f"{label} request deadline is invalid") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise NvdecCaptureError(f"{label} request deadline is invalid")
    deadline = time.monotonic() + timeout_seconds
    if _url_origin(url) != _url_origin(origin_url):
        raise NvdecCaptureError(f"{label} request is not same-origin")
    if http_get is requests.get:
        return _fetch_bounded_requests_process(
            url,
            deadline=deadline,
            limit=limit,
            label=label,
            cancel_event=cancel_event,
        )
    try:
        parameters = inspect.signature(http_get).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_keywords = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = {
        "timeout": _remaining_fetch_time(deadline, label, cancel_event),
    }
    if accepts_keywords or "stream" in parameters:
        kwargs["stream"] = True
    if accepts_keywords or "allow_redirects" in parameters:
        kwargs["allow_redirects"] = False
    try:
        response = http_get(url, **kwargs)
    except Exception:
        _remaining_fetch_time(deadline, label, cancel_event)
        raise NvdecCaptureError(f"{label} request failed") from None
    try:
        _remaining_fetch_time(deadline, label, cancel_event)
        return _bounded_response(
            response,
            origin_url,
            limit,
            label,
            deadline=deadline,
            cancel_event=cancel_event,
        )
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _parse_fraction(value, label):
    try:
        result = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise NvdecCaptureError(f"{label} is invalid") from exc
    if result <= 0:
        raise NvdecCaptureError(f"{label} is invalid")
    return result


def _run_bounded_command(
    command,
    *,
    pass_fds,
    timeout,
    output_limit,
    error_message="FFprobe packet inspection failed",
    cancel_event=None,
    absolute_deadline=None,
):
    deadline = (
        time.monotonic() + max(0.1, float(timeout))
        if absolute_deadline is None
        else float(absolute_deadline)
    )
    if (
        deadline <= time.monotonic()
        or (cancel_event is not None and cancel_event.is_set())
    ):
        raise NvdecCaptureError(error_message)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except OSError as exc:
        raise NvdecCaptureError(error_message) from exc
    output = bytearray()
    try:
        descriptor = process.stdout.fileno()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise NvdecCaptureError(error_message)
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise NvdecCaptureError(error_message)
            readable, _, _ = select.select(
                (descriptor,), (), (), min(0.05, remaining)
            )
            if not readable:
                continue
            chunk = os.read(descriptor, min(65_536, output_limit + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > output_limit:
                raise NvdecCaptureError(error_message)
        remaining = deadline - time.monotonic()
        if (
            remaining <= 0.0
            or (cancel_event is not None and cancel_event.is_set())
        ):
            raise NvdecCaptureError(error_message)
        if process.wait(timeout=remaining) != 0:
            raise NvdecCaptureError(error_message)
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired, NvdecCaptureError):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        raise NvdecCaptureError(error_message)
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _probe_fragment_packets(
    init_bytes,
    segment_bytes,
    *,
    ffprobe_binary="/usr/bin/ffprobe",
    timeout_seconds=5.0,
):
    if (
        not isinstance(init_bytes, bytes)
        or not isinstance(segment_bytes, bytes)
        or not init_bytes
        or not segment_bytes
        or len(init_bytes) > _HLS_RESOURCE_LIMIT
        or len(segment_bytes) > _HLS_RESOURCE_LIMIT
        or len(init_bytes) + len(segment_bytes) > _HLS_RESOURCE_LIMIT
    ):
        raise NvdecCaptureError("HLS fMP4 bytes are outside the safe bound")
    binary = Path(ffprobe_binary)
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or not os.access(binary, os.X_OK)
    ):
        raise NvdecCaptureError("FFprobe binary is unavailable")
    descriptor = os.memfd_create("v2x-hls-fragment", 0)
    try:
        os.write(descriptor, init_bytes)
        os.write(descriptor, segment_bytes)
        os.lseek(descriptor, 0, os.SEEK_SET)
        command = [
            str(binary),
            "-v", "error",
            "-show_packets",
            "-show_entries",
            "stream=index,codec_type,time_base:"
            "packet=stream_index,pts,dts,duration,flags",
            "-of", "json",
            f"/proc/self/fd/{descriptor}",
        ]
        output = _run_bounded_command(
            command,
            pass_fds=(descriptor,),
            timeout=max(0.1, float(timeout_seconds)),
            output_limit=_FFPROBE_OUTPUT_LIMIT,
        )
        try:
            payload = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NvdecCaptureError("FFprobe packet metadata is invalid") from exc
    finally:
        os.close(descriptor)

    streams = payload.get("streams") if isinstance(payload, dict) else None
    packets = payload.get("packets") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise NvdecCaptureError("HLS fragment video track is ambiguous")
    if (
        len(streams) != 1
        or not isinstance(streams[0], dict)
        or streams[0].get("codec_type") != "video"
    ):
        raise NvdecCaptureError("HLS fragment video track is ambiguous")
    stream = streams[0]
    time_base = _parse_fraction(stream.get("time_base"), "video time base")
    stream_index = stream.get("index")
    if (
        isinstance(stream_index, bool)
        or not isinstance(stream_index, int)
        or not isinstance(packets, list)
        or len(packets) > _MAX_FRAGMENT_PACKETS
    ):
        raise NvdecCaptureError("HLS fragment packet metadata is invalid")
    samples = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise NvdecCaptureError("HLS fragment packet metadata is invalid")
        if packet.get("stream_index") != stream_index:
            continue
        try:
            pts = int(packet["pts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NvdecCaptureError("HLS fragment packet PTS is invalid") from exc
        samples.append(_PacketSample(
            index=len(samples),
            pts=Fraction(pts) * time_base,
            time_base=time_base,
        ))
    if not samples:
        raise NvdecCaptureError("HLS fragment packet metadata is invalid")
    return tuple(samples)


class _FramePtsSidecar:
    """Drain strict URL-free framecrc records and expose source frame PTS."""

    def __init__(self, descriptor, queue_limit=_SIDECAR_QUEUE_LIMIT):
        self._descriptor = descriptor
        self._descriptor_lock = threading.Lock()
        self._queue_limit = int(queue_limit)
        self._condition = threading.Condition()
        self._records = deque()
        self._time_base = None
        self._last_pts = None
        self._failed = False
        self._done = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _fail(self):
        with self._condition:
            self._failed = True
            self._records.clear()
            self._condition.notify_all()

    def _parse_line(self, raw):
        if len(raw) > _SIDECAR_LINE_LIMIT:
            self._fail()
            return
        try:
            line = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            self._fail()
            return
        if not line:
            return
        if line.startswith("#tb 0:"):
            try:
                time_base = _parse_fraction(
                    line.split(":", 1)[1].strip(), "frame PTS time base"
                )
            except NvdecCaptureError:
                self._fail()
                return
            with self._condition:
                if self._time_base is not None and self._time_base != time_base:
                    self._failed = True
                    self._records.clear()
                self._time_base = time_base
                self._condition.notify_all()
            return
        if line.startswith("#"):
            return
        fields = [value.strip() for value in line.split(",")]
        try:
            if len(fields) < 6 or int(fields[0]) != 0:
                raise ValueError
            pts = int(fields[2])
            int(fields[1])
            int(fields[3])
            if int(fields[4]) <= 0:
                raise ValueError
            if not re.fullmatch(r"0x[0-9A-Fa-f]{8}", fields[5]):
                raise ValueError
        except ValueError:
            self._fail()
            return
        with self._condition:
            if self._failed or self._time_base is None:
                self._failed = True
                self._records.clear()
            elif len(self._records) >= self._queue_limit:
                self._failed = True
                self._records.clear()
            else:
                source_pts = Fraction(pts) * self._time_base
                if self._last_pts is not None and source_pts <= self._last_pts:
                    self._failed = True
                    self._records.clear()
                else:
                    self._last_pts = source_pts
                    self._records.append(source_pts)
            self._condition.notify_all()

    def _run(self):
        pending = b""
        with self._descriptor_lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            self._fail()
            with self._condition:
                self._done = True
                self._condition.notify_all()
            return
        try:
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > _SIDECAR_LINE_LIMIT and b"\n" not in pending:
                    self._fail()
                    pending = b""
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self._parse_line(line)
            if pending:
                self._parse_line(pending)
        except OSError:
            self._fail()
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            with self._condition:
                self._done = True
                self._condition.notify_all()

    def take(self, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while not self._records and not self._failed and not self._done:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    self._failed = True
                    break
                self._condition.wait(remaining)
            if self._failed or not self._records:
                return None
            return self._records.popleft()

    def close(self, timeout=1.0):
        self._thread.join(max(0.0, float(timeout)))
        if self._thread.is_alive():
            raise NvdecCaptureError("frame PTS sidecar did not exit")

    def is_alive(self):
        return self._thread.is_alive()


class _LoopbackHlsMediator:
    """Serve only the exact same-session HLS objects observed by capture."""

    def __init__(
        self,
        source_url,
        master_text,
        *,
        http_get=requests.get,
        packet_probe=_probe_fragment_packets,
        request_timeout=_HLS_FETCH_TIMEOUT_SECONDS,
        packet_probe_timeout=_HLS_PACKET_PROBE_TIMEOUT_SECONDS,
    ):
        self._origin_url = str(source_url)
        self._media_url = _master_media_url(source_url, master_text)
        self._http_get = http_get
        self._packet_probe = packet_probe
        self._request_timeout = max(0.1, float(request_timeout))
        self._packet_probe_timeout = max(0.1, float(packet_probe_timeout))
        self._token = secrets.token_urlsafe(32)
        self._routes_lock = threading.RLock()
        self._routes = {}
        self._route_generations = deque()
        self._init_bytes = {}
        self._stopping = threading.Event()
        self._closed = threading.Event()
        self._close_lock = threading.RLock()
        self._active_condition = threading.Condition()
        self._active_requests = 0
        self._transport_evidence_lock = threading.RLock()
        self._transport_evidence_enabled = True
        self.clock = SameSessionTransportClock()

        mediator = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                if not mediator._begin_request():
                    body = b"HLS mediator unavailable"
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                try:
                    try:
                        status, content_type, body = mediator._handle_get(
                            self.path
                        )
                    except Exception:
                        status, content_type, body = (
                            502,
                            "text/plain; charset=utf-8",
                            b"HLS mediation failed",
                        )
                    try:
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                finally:
                    mediator._end_request()

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False
            request_queue_size = 4

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()
        host, port = self._server.server_address
        local_media = f"http://{host}:{port}/{self._token}/media.m3u8"
        self.master = self._rewrite_master(master_text, local_media)

    @staticmethod
    def _rewrite_master(master_text, local_media):
        rewritten = []
        replaced = 0
        for line in master_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and _has_uri_attribute(stripped):
                raise NvdecCaptureError(
                    "HLS master URI-bearing tag is unsupported"
                )
            if stripped and not stripped.startswith("#"):
                rewritten.append(local_media)
                replaced += 1
            else:
                rewritten.append(line)
        if replaced != 1:
            raise NvdecCaptureError("HLS master must contain exactly one variant")
        return ("\n".join(rewritten) + "\n").encode("utf-8")

    def _begin_request(self):
        with self._active_condition:
            if self._stopping.is_set() or self._active_requests >= 2:
                return False
            self._active_requests += 1
            return True

    def _end_request(self):
        with self._active_condition:
            self._active_requests -= 1
            self._active_condition.notify_all()

    def _fetch(self, url, limit, label):
        if self._stopping.is_set():
            raise NvdecCaptureError("HLS mediator is stopping")
        return _fetch_bounded(
            self._http_get,
            url,
            timeout=self._request_timeout,
            origin_url=self._origin_url,
            limit=limit,
            label=label,
            cancel_event=self._stopping,
        )

    def _disable_transport_evidence(self):
        with self._transport_evidence_lock:
            self._transport_evidence_enabled = False
            self.clock.clear()

    def _probe_packets_bounded(self, init_bytes, segment_bytes):
        with self._transport_evidence_lock:
            if not self._transport_evidence_enabled:
                return None
            packet_probe = self._packet_probe
        if packet_probe is None:
            return None
        result = {}
        finished = threading.Event()

        def run_probe():
            try:
                result["samples"] = packet_probe(init_bytes, segment_bytes)
            except Exception as exc:
                result["error"] = exc
            finally:
                finished.set()

        threading.Thread(target=run_probe, daemon=True).start()
        if not finished.wait(self._packet_probe_timeout):
            raise NvdecCaptureError("HLS packet evidence inspection timed out")
        if "error" in result:
            raise NvdecCaptureError("HLS packet evidence inspection failed")
        return result.get("samples")

    def _record_transport_evidence(self, fragment, init_bytes, segment_bytes):
        try:
            samples = self._probe_packets_bounded(init_bytes, segment_bytes)
            if samples is None:
                return
            with self._transport_evidence_lock:
                if not self._transport_evidence_enabled:
                    return
                self.clock.add_fragment(fragment, samples)
        except Exception:
            # Packet metadata is optional evidence. The exact fMP4 body still
            # belongs to this mediated decode session and must remain usable by
            # the legacy pixel matcher, while transport evidence fails closed.
            self._disable_transport_evidence()

    def _local_route(self, kind, payload, generation):
        route = secrets.token_urlsafe(24)
        with self._routes_lock:
            if len(self._routes) >= 32:
                raise NvdecCaptureError("HLS mediator route bound exceeded")
            self._routes[route] = (kind, payload)
            generation.add(route)
        host, port = self._server.server_address
        return f"http://{host}:{port}/{self._token}/resource/{route}"

    def _rewrite_media(self, fragments):
        generation = set()
        init_routes = {}
        segment_routes = []
        for fragment in fragments:
            if fragment.init_url not in init_routes:
                init_routes[fragment.init_url] = self._local_route(
                    "init", fragment.init_url, generation
                )
            segment_routes.append((
                fragment,
                self._local_route("segment", fragment, generation),
            ))
        with self._routes_lock:
            self._route_generations.append(generation)
            while len(self._route_generations) > 4:
                expired = self._route_generations.popleft()
                for route in expired:
                    self._routes.pop(route, None)
        target_duration = max(
            1, int(math.ceil(max(item.duration_seconds for item in fragments)))
        )
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            f"#EXT-X-MEDIA-SEQUENCE:{fragments[0].media_sequence}",
        ]
        active_init = None
        for fragment, segment_route in segment_routes:
            if fragment.init_url != active_init:
                lines.append(
                    f'#EXT-X-MAP:URI="{init_routes[fragment.init_url]}"'
                )
                active_init = fragment.init_url
            if fragment.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend((
                f"#EXT-X-PROGRAM-DATE-TIME:"
                f"{fragment.program_date_time_utc}",
                f"#EXTINF:{fragment.duration_seconds:.6f},",
                segment_route,
            ))
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _handle_get(self, path):
        prefix = f"/{self._token}/"
        if not isinstance(path, str) or not path.startswith(prefix):
            return 404, "text/plain; charset=utf-8", b"not found"
        if path == f"/{self._token}/media.m3u8":
            body = self._fetch(
                self._media_url,
                _MEDIA_PLAYLIST_LIMIT,
                "HLS media playlist",
            )
            try:
                text = body.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise NvdecCaptureError("HLS media response is not UTF-8") from exc
            fragments = _parse_media_playlist(self._media_url, text)
            if any(
                line.strip() == "#EXT-X-DISCONTINUITY"
                for line in text.splitlines()
            ):
                self._disable_transport_evidence()
            return 200, "application/vnd.apple.mpegurl", self._rewrite_media(
                fragments
            )
        resource_prefix = f"/{self._token}/resource/"
        if not path.startswith(resource_prefix):
            return 404, "text/plain; charset=utf-8", b"not found"
        route = path[len(resource_prefix):]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", route):
            return 404, "text/plain; charset=utf-8", b"not found"
        with self._routes_lock:
            entry = self._routes.pop(route, None)
        if entry is None:
            return 404, "text/plain; charset=utf-8", b"not found"
        kind, payload = entry
        if kind == "init":
            body = self._fetch(
                payload, _HLS_RESOURCE_LIMIT, "HLS initialization fragment"
            )
            with self._routes_lock:
                if payload not in self._init_bytes and len(self._init_bytes) >= 8:
                    raise NvdecCaptureError(
                        "HLS initialization cache bound exceeded"
                    )
                self._init_bytes[payload] = body
            return 200, "video/mp4", body
        if kind != "segment" or not isinstance(payload, _MediatedFragment):
            raise NvdecCaptureError("HLS mediator route is invalid")
        with self._routes_lock:
            init_bytes = self._init_bytes.get(payload.init_url)
        if init_bytes is None:
            raise NvdecCaptureError("HLS initialization fragment was not served")
        segment_bytes = self._fetch(
            payload.segment_url, _HLS_RESOURCE_LIMIT, "HLS media fragment"
        )
        self._record_transport_evidence(payload, init_bytes, segment_bytes)
        return 200, "video/mp4", segment_bytes

    def _clear_retained_state(self):
        with self._routes_lock:
            self._routes.clear()
            self._route_generations.clear()
            self._init_bytes.clear()
        self._disable_transport_evidence()
        self._media_url = None
        self._origin_url = None
        self._http_get = None
        self._packet_probe = None
        self._packet_probe_timeout = 0.0
        self._token = None
        self.master = b""

    def close(self, timeout=_HLS_MEDIATOR_CLOSE_TIMEOUT_SECONDS):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._stopping.set()
            deadline = time.monotonic() + max(0.0, float(timeout))
            cleanup_error = None
            try:
                self._server.shutdown()
                self._server.server_close()
                self._thread.join(max(0.0, deadline - time.monotonic()))
                with self._active_condition:
                    while self._active_requests and time.monotonic() < deadline:
                        self._active_condition.wait(
                            max(0.0, deadline - time.monotonic())
                        )
                    active = self._active_requests
                if self._thread.is_alive() or active:
                    cleanup_error = NvdecCaptureError(
                        "HLS mediator did not quiesce"
                    )
            except Exception as exc:
                cleanup_error = exc
            finally:
                # Scrub signed upstream references even when quiescence fails.
                self._clear_retained_state()
                self._closed.set()
            if cleanup_error is not None:
                raise cleanup_error

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())


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
        if stripped.startswith("#") and _has_uri_attribute(stripped):
            raise NvdecCaptureError(
                "HLS master URI-bearing tag is unsupported"
            )
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


def build_nvdec_command(
    ffmpeg_binary, input_reference, fifo_path, *, hls, pts_fd=None
):
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
                # OpenCV reads a local FIFO and cannot reliably interrupt an
                # upstream HLS socket still owned by the child. Bound both a
                # blocked network operation and a live playlist that reloads
                # without advancing so the FIFO closes before freshness fails.
                "-rw_timeout",
                str(_HLS_IO_TIMEOUT_MICROSECONDS),
                "-f",
                "hls",
                "-m3u8_hold_counters",
                str(_HLS_HOLD_COUNTERS),
            ]
        )
    if pts_fd is not None:
        if isinstance(pts_fd, bool) or not isinstance(pts_fd, int) or pts_fd < 0:
            raise ValueError("frame PTS descriptor is invalid")
        command.extend(["-copyts", "-copytb", "1"])
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
            "-an",
            "-sn",
            "-dn",
        ]
    )
    if pts_fd is None:
        command.extend(
            [
            "-map",
            "0:v:0",
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
    else:
        command.extend(
            [
                "-filter_complex",
                "[0:v:0]hwdownload,format=nv12,split=2[full][timing];"
                "[full]format=bgr24[frames];"
                "[timing]scale=2:2,format=gray[timing_out]",
                "-map", "[frames]",
                "-fps_mode", "passthrough",
                "-enc_time_base", "-1",
                "-c:v", "rawvideo",
                "-f", "nut",
                "-y", str(fifo_path),
                "-map", "[timing_out]",
                "-fps_mode", "passthrough",
                "-enc_time_base", "-1",
                "-c:v", "rawvideo",
                "-f", "framecrc",
                f"pipe:{pts_fd}",
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
        packet_probe=_probe_fragment_packets,
        source_pts_timeout_ms=1_000,
        cancel_event=None,
    ):
        if (source_url is None) == (file_path is None):
            raise ValueError("provide exactly one of source_url or file_path")
        self._capture = None
        self._process = None
        self._memfd = None
        self._mediator = None
        self._pts_sidecar = None
        self._pts_write_fd = None
        self._last_source_position_ms = None
        self._last_transport_exact = False
        self._source_pts_timeout_seconds = max(
            0.0, float(source_pts_timeout_ms) / 1000.0
        )
        self._temporary_directory = None
        self._opened = False
        self._cancel_event = cancel_event
        self._cancel_watcher_stop = threading.Event()
        self._cancel_watcher = None
        self._process_lock = threading.RLock()

        def raise_if_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise NvdecCaptureError("NVDEC capture cancelled")

        binary = Path(ffmpeg_binary)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise NvdecCaptureError("NVDEC FFmpeg binary is unavailable")
        if shutil.which(str(binary)) is None:
            raise NvdecCaptureError("NVDEC FFmpeg binary is not executable")

        try:
            raise_if_cancelled()
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="v2x-nvdec-"
            )
            fifo_path = Path(self._temporary_directory.name) / "frames.nut"
            os.mkfifo(fifo_path, 0o600)

            pass_fds = ()
            pts_read_fd = None
            if source_url is not None:
                master_bytes = _fetch_bounded(
                    http_get,
                    str(source_url),
                    timeout=_HLS_FETCH_TIMEOUT_SECONDS,
                    origin_url=str(source_url),
                    limit=_MASTER_PLAYLIST_LIMIT,
                    label="HLS master playlist",
                    cancel_event=cancel_event,
                )
                raise_if_cancelled()
                try:
                    master_text = master_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise NvdecCaptureError(
                        "HLS master response is not UTF-8"
                    ) from exc
                self._mediator = _LoopbackHlsMediator(
                    str(source_url),
                    master_text,
                    http_get=http_get,
                    packet_probe=packet_probe,
                )
                self._memfd = os.memfd_create("v2x-hls-master", 0)
                os.write(self._memfd, self._mediator.master)
                os.lseek(self._memfd, 0, os.SEEK_SET)
                input_reference = f"/proc/self/fd/{self._memfd}"
                pts_read_fd, self._pts_write_fd = os.pipe()
                pass_fds = (self._memfd, self._pts_write_fd)
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
                binary,
                input_reference,
                fifo_path,
                hls=is_hls,
                pts_fd=self._pts_write_fd,
            )
            process = subprocess.Popen(
                command,
                pass_fds=pass_fds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                # FFmpeg errors can contain signed child URLs. Never retain or
                # publish its raw stderr; health receives a fixed safe error.
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with self._process_lock:
                self._process = process
            if self._pts_write_fd is not None:
                os.close(self._pts_write_fd)
                self._pts_write_fd = None
            if pts_read_fd is not None:
                self._pts_sidecar = _FramePtsSidecar(pts_read_fd)
                pts_read_fd = None
            if cancel_event is not None:
                self._cancel_watcher = threading.Thread(
                    target=self._watch_for_cancel,
                    daemon=True,
                )
                self._cancel_watcher.start()
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
            raise_if_cancelled()
            self._opened = bool(
                self._capture is not None
                and self._capture.isOpened()
                and self._process.poll() is None
            )
            if not self._opened:
                raise NvdecCaptureError("NVDEC capture open failed")
        except Exception:
            if pts_read_fd is not None:
                try:
                    os.close(pts_read_fd)
                except OSError:
                    pass
            self.release()
            raise
        finally:
            if self._pts_write_fd is not None:
                try:
                    os.close(self._pts_write_fd)
                except OSError:
                    pass
                self._pts_write_fd = None
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
        ok, frame = self._capture.read()
        if not ok or frame is None:
            return ok, frame
        self._last_source_position_ms = None
        self._last_transport_exact = False
        sidecar = self._pts_sidecar
        if sidecar is not None:
            source_pts = sidecar.take(self._source_pts_timeout_seconds)
            if source_pts is not None:
                self._last_source_position_ms = float(source_pts * 1000)
                mediator = self._mediator
                self._last_transport_exact = bool(
                    mediator is not None
                    and mediator.clock.has_position(
                        self._last_source_position_ms
                    )
                )
        return ok, frame

    def get(self, property_id):
        if self._capture is None:
            return 0.0
        if (
            property_id == cv2.CAP_PROP_POS_MSEC
            and self._last_source_position_ms is not None
        ):
            return self._last_source_position_ms
        return self._capture.get(property_id)

    def transport_media_clock(self):
        """Return exact same-session transport evidence for the latest frame."""
        mediator = self._mediator
        if (
            mediator is None
            or not self._last_transport_exact
            or self._last_source_position_ms is None
        ):
            return None
        if not mediator.clock.has_position(self._last_source_position_ms):
            return None
        return mediator.clock

    def release(self):
        self._cancel_watcher_stop.set()
        self._opened = False
        cleanup_error = None
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception as exc:
                cleanup_error = exc

        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        if process.poll() is None:
                            cleanup_error = NvdecCaptureError(
                                "NVDEC FFmpeg process did not exit"
                            )

        process_dead = process is None or process.poll() is not None
        if process_dead:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        if process_dead and self._memfd is not None:
            try:
                os.close(self._memfd)
            except OSError:
                pass
            self._memfd = None
        if process_dead:
            sidecar = getattr(self, "_pts_sidecar", None)
            if sidecar is not None:
                try:
                    sidecar.close(timeout=1.0)
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                else:
                    self._pts_sidecar = None
            mediator = getattr(self, "_mediator", None)
            if mediator is not None:
                try:
                    mediator.close()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                else:
                    self._mediator = None
            write_fd = getattr(self, "_pts_write_fd", None)
            if write_fd is not None:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
                self._pts_write_fd = None
        if process_dead:
            temporary, self._temporary_directory = (
                self._temporary_directory,
                None,
            )
            if temporary is not None:
                temporary.cleanup()
        self._last_source_position_ms = None
        self._last_transport_exact = False
        watcher, self._cancel_watcher = self._cancel_watcher, None
        if (
            watcher is not None
            and watcher is not threading.current_thread()
            and watcher.is_alive()
        ):
            watcher.join(timeout=0.2)
        if cleanup_error is not None:
            raise cleanup_error

    def _watch_for_cancel(self):
        while not self._cancel_watcher_stop.is_set():
            if self._cancel_event.wait(0.05):
                with self._process_lock:
                    process = self._process
                if process is not None and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                mediator = getattr(self, "_mediator", None)
                if mediator is not None:
                    try:
                        mediator.close()
                    except Exception:
                        # release() retains and reports failed cleanup; the
                        # cancellation watcher must not leak a traceback.
                        pass
                return


def match_fragment_frame_nvdec(
    init_bytes,
    segment_bytes,
    target_identity,
    frame_identity,
    *,
    capture_factory=None,
    cancel_event=None,
):
    """Match one exact frame or contiguous frame sequence on the NVDEC path."""
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

        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            parameters = inspect.signature(capture_factory).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs = (
            {"cancel_event": cancel_event}
            if "cancel_event" in parameters
            or any(
                value.kind == inspect.Parameter.VAR_KEYWORD
                for value in parameters.values()
            )
            else {}
        )
        capture = capture_factory(path, **kwargs)
        if capture is None or not capture.isOpened():
            return None
        targets = (
            tuple(target_identity)
            if isinstance(target_identity, (tuple, list))
            else (target_identity,)
        )
        if not targets:
            return None
        window = deque(maxlen=len(targets))
        matches = []
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            try:
                position = float(capture.get(cv2.CAP_PROP_POS_MSEC))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(position) or position < 0.0:
                return None
            if all(isinstance(target, NvdecFrameIdentity) for target in targets):
                window.append((
                    frame,
                    position,
                    quick_frame_identity(frame),
                ))
                if len(window) < len(targets):
                    continue
                matches_target = all(
                    item[2] == target.quick
                    for item, target in zip(window, targets)
                ) and all(
                    frame_identity(item[0]) == target.exact
                    for item, target in zip(window, targets)
                )
            else:
                window.append((frame_identity(frame), position))
                if len(window) < len(targets):
                    continue
                matches_target = all(
                    item[0] == target
                    for item, target in zip(window, targets)
                )
            positions_increase = all(
                later[1] > earlier[1]
                for earlier, later in zip(window, tuple(window)[1:])
            )
            if matches_target and positions_increase:
                matches.append(tuple(float(item[1]) for item in window))
        if len(matches) != 1:
            return None
        if len(targets) == 1:
            return matches[0][-1]
        return FragmentFrameSequenceMatch(
            frame_offset_milliseconds=matches[0][-1],
            frame_positions_milliseconds=matches[0],
        )
    finally:
        if capture is not None:
            capture.release()
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

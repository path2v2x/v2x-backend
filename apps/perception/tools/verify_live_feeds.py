#!/usr/bin/env python3
"""Verify four-feed perception freshness and real MJPEG frame changes."""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))

from runtime_health import sanitize_source_error  # noqa: E402


CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")


class VerificationError(RuntimeError):
    pass


def normalize_base_url(value):
    """Accept only a credential-free HTTP(S) base without query material."""
    parts = urlsplit(str(value).strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise VerificationError(
            "base URL must be credential-free HTTP(S) without query or fragment"
        )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_utc_timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise VerificationError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _fetch_json(url, timeout_seconds):
    request = Request(
        url,
        headers={
            "accept": "application/json",
            "cache-control": "no-cache",
            "user-agent": "v2x-perception-verifier/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        safe_error = sanitize_source_error(exc)
        exc.close()
        raise VerificationError(f"JSON request failed: {safe_error}") from None
    except Exception as exc:
        raise VerificationError(
            f"JSON request failed: {sanitize_source_error(exc)}"
        ) from None
    if len(payload) > 2 * 1024 * 1024:
        raise VerificationError("JSON response exceeds the bounded size limit")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("JSON response is invalid") from exc
    if not isinstance(decoded, dict):
        raise VerificationError("JSON response is not an object")
    return decoded


def _collect_timestamp_sample(
    base_url,
    timeout_seconds,
    min_decode_latency_ms=-1_000.0,
    max_decode_latency_ms=120_000.0,
):
    health = _fetch_json(f"{base_url}/health", timeout_seconds)
    detections = _fetch_json(f"{base_url}/detections/latest", timeout_seconds)
    if health.get("status") != "ok" or health.get("ready") is not True:
        raise VerificationError("perception health is not ready")
    if health.get("media_clock_ready") is not True:
        raise VerificationError("perception media clock is not ready")

    min_decode_latency_ms = float(min_decode_latency_ms)
    max_decode_latency_ms = float(max_decode_latency_ms)
    if (
        not math.isfinite(min_decode_latency_ms)
        or not math.isfinite(max_decode_latency_ms)
        or min_decode_latency_ms > max_decode_latency_ms
    ):
        raise VerificationError("decode latency bounds are invalid")

    health_cameras = health.get("cameras")
    detection_cameras = detections.get("cameras")
    if not isinstance(health_cameras, dict) or not isinstance(
        detection_cameras, dict
    ):
        raise VerificationError("camera timestamp maps are missing")

    sample = {}
    for camera_id in CAMERA_IDS:
        health_camera = health_cameras.get(camera_id)
        detection_camera = detection_cameras.get(camera_id)
        if not isinstance(health_camera, dict) or not isinstance(
            detection_camera, dict
        ):
            raise VerificationError(f"{camera_id} timestamp state is missing")
        if (
            health_camera.get("state") != "streaming"
            or health_camera.get("fresh") is not True
        ):
            raise VerificationError(f"{camera_id} is not fresh and streaming")
        if (
            health_camera.get("media_clock_status") != "matched"
            or health_camera.get("media_time_trusted") is not True
        ):
            raise VerificationError(
                f"{camera_id} does not have a trusted matched media clock"
            )
        decode_latency_ms = health_camera.get("decode_latency_ms")
        if (
            not isinstance(decode_latency_ms, (int, float))
            or isinstance(decode_latency_ms, bool)
            or not math.isfinite(float(decode_latency_ms))
            or not (
                min_decode_latency_ms
                <= float(decode_latency_ms)
                <= max_decode_latency_ms
            )
        ):
            raise VerificationError(
                f"{camera_id} decode latency is outside the accepted bounds"
            )
        frame_count = health_camera.get("frame_count")
        if not isinstance(frame_count, int) or isinstance(frame_count, bool):
            raise VerificationError(f"{camera_id} frame count is invalid")
        sample[camera_id] = {
            "capture": parse_utc_timestamp(
                health_camera.get("source_updated_at"),
                f"{camera_id} capture timestamp",
            ),
            "event": parse_utc_timestamp(
                detection_camera.get("updated_at"),
                f"{camera_id} event timestamp",
            ),
            "frame_count": frame_count,
            "decode_latency_ms": float(decode_latency_ms),
        }
    return sample


class JpegFrameParser:
    """Incrementally extract complete JPEG images from an MJPEG byte stream."""

    def __init__(self, max_frame_bytes=12 * 1024 * 1024):
        self.max_frame_bytes = int(max_frame_bytes)
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                if len(self.buffer) > 1:
                    del self.buffer[:-1]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > self.max_frame_bytes:
                    raise VerificationError("MJPEG frame exceeds size limit")
                break
            end += 2
            frames.append(bytes(self.buffer[:end]))
            del self.buffer[:end]
        return frames


def _read_mjpeg_hashes(url, timeout_seconds):
    request = Request(
        url,
        headers={
            "accept": "multipart/x-mixed-replace",
            "cache-control": "no-cache",
            "user-agent": "v2x-perception-verifier/1",
        },
    )
    parser = JpegFrameParser()
    hashes = []
    total_bytes = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if content_type != "multipart/x-mixed-replace":
                raise VerificationError("stream response is not MJPEG multipart")
            while len(hashes) < 2:
                if time.monotonic() >= deadline:
                    raise VerificationError("MJPEG frame deadline expired")
                chunk = response.read(64 * 1024)
                if not chunk:
                    raise VerificationError("MJPEG stream ended before two frames")
                total_bytes += len(chunk)
                if total_bytes > 32 * 1024 * 1024:
                    raise VerificationError("MJPEG verification byte limit exceeded")
                for frame in parser.feed(chunk):
                    hashes.append(hashlib.sha256(frame).hexdigest())
                    if len(hashes) == 2:
                        break
    except VerificationError:
        raise
    except HTTPError as exc:
        safe_error = sanitize_source_error(exc)
        exc.close()
        raise VerificationError(f"MJPEG request failed: {safe_error}") from None
    except Exception as exc:
        raise VerificationError(
            f"MJPEG request failed: {sanitize_source_error(exc)}"
        ) from None
    if hashes[0] == hashes[1]:
        raise VerificationError("two complete MJPEG frames have identical content")
    return hashes


def _validate_timestamp_samples(first, second, max_age_seconds, now=None):
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    max_age_seconds = float(max_age_seconds)
    if max_age_seconds <= 0:
        raise VerificationError("max age must be positive")

    output = {}
    for camera_id in CAMERA_IDS:
        first_camera = first[camera_id]
        second_camera = second[camera_id]
        for timestamp_name in ("capture", "event"):
            before = first_camera[timestamp_name]
            after = second_camera[timestamp_name]
            if after <= before:
                raise VerificationError(
                    f"{camera_id} {timestamp_name} timestamp did not advance"
                )
            for timestamp in (before, after):
                age = (now - timestamp).total_seconds()
                if age < -5.0 or age > max_age_seconds:
                    raise VerificationError(
                        f"{camera_id} {timestamp_name} timestamp is outside max age"
                    )
        if second_camera["frame_count"] <= first_camera["frame_count"]:
            raise VerificationError(f"{camera_id} frame count did not advance")
        output[camera_id] = {
            "capture_times": [
                first_camera["capture"].isoformat().replace("+00:00", "Z"),
                second_camera["capture"].isoformat().replace("+00:00", "Z"),
            ],
            "event_times": [
                first_camera["event"].isoformat().replace("+00:00", "Z"),
                second_camera["event"].isoformat().replace("+00:00", "Z"),
            ],
            "decode_latency_ms": [
                first_camera["decode_latency_ms"],
                second_camera["decode_latency_ms"],
            ],
        }
    return output


def verify_live_feeds(
    base_url,
    sample_interval_seconds=5.0,
    max_age_seconds=15.0,
    timeout_seconds=20.0,
    min_decode_latency_ms=-1_000.0,
    max_decode_latency_ms=120_000.0,
    stream_path_template="/streams/{camera_id}.mjpg",
):
    base_url = normalize_base_url(base_url)
    timeout_seconds = float(timeout_seconds)
    if timeout_seconds <= 0:
        raise VerificationError("timeout must be positive")
    if (
        not stream_path_template.startswith("/")
        or "{camera_id}" not in stream_path_template
        or "?" in stream_path_template
        or "#" in stream_path_template
    ):
        raise VerificationError("stream path template is invalid")

    first = _collect_timestamp_sample(
        base_url,
        timeout_seconds,
        min_decode_latency_ms,
        max_decode_latency_ms,
    )
    time.sleep(max(0.0, float(sample_interval_seconds)))
    second = _collect_timestamp_sample(
        base_url,
        timeout_seconds,
        min_decode_latency_ms,
        max_decode_latency_ms,
    )
    output = _validate_timestamp_samples(first, second, max_age_seconds)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            camera_id: executor.submit(
                _read_mjpeg_hashes,
                f"{base_url}{stream_path_template.format(camera_id=camera_id)}",
                timeout_seconds,
            )
            for camera_id in CAMERA_IDS
        }
        for camera_id, future in futures.items():
            try:
                output[camera_id]["frame_sha256"] = future.result()
            except Exception as exc:
                safe_error = sanitize_source_error(exc)
                raise VerificationError(
                    f"{camera_id} frame verification failed: {safe_error}"
                ) from None
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify fresh, changing ch1-ch4 perception feeds."
    )
    parser.add_argument("base_url")
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--max-age", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--min-decode-latency-ms", type=float, default=-1_000.0)
    parser.add_argument("--max-decode-latency-ms", type=float, default=120_000.0)
    parser.add_argument(
        "--stream-path-template", default="/streams/{camera_id}.mjpg"
    )
    args = parser.parse_args(argv)
    try:
        result = verify_live_feeds(
            args.base_url,
            sample_interval_seconds=args.sample_interval,
            max_age_seconds=args.max_age,
            timeout_seconds=args.timeout,
            min_decode_latency_ms=args.min_decode_latency_ms,
            max_decode_latency_ms=args.max_decode_latency_ms,
            stream_path_template=args.stream_path_template,
        )
    except Exception as exc:
        print(
            f"verification failed: {sanitize_source_error(exc)}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

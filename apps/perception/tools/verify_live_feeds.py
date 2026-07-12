#!/usr/bin/env python3
"""Verify four-feed perception freshness and real MJPEG frame changes."""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))

from runtime_health import sanitize_source_error  # noqa: E402


CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")
# Kinesis fragments at this site are about 2.002 seconds long. Sampling at
# exactly two seconds aliases the producer boundary and can see the same last
# completed frame twice. This is an observation window, not a freshness gate;
# the independent max-age and per-second health watches remain unchanged.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 3.0
DEFAULT_CAPTURE_PROGRESS_TIMEOUT_SECONDS = 5.0
DEFAULT_INFERENCE_PROGRESS_TIMEOUT_SECONDS = 10.0
INFERENCE_POLL_INTERVAL_SECONDS = 0.25


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
            "user-agent": "v2x-perception-verifier/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
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


def _collect_timestamp_sample(base_url, timeout_seconds):
    health = _fetch_json(f"{base_url}/health", timeout_seconds)
    detections = _fetch_json(f"{base_url}/detections/latest", timeout_seconds)
    if health.get("status") != "ok" or health.get("ready") is not True:
        raise VerificationError("perception health is not ready")

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
            raise VerificationError(f"{camera_id} media clock is not trusted")
        decode_latency_ms = health_camera.get("decode_latency_ms")
        if (
            isinstance(decode_latency_ms, bool)
            or not isinstance(decode_latency_ms, (int, float))
            or not -1_000.0 <= float(decode_latency_ms) <= 10_000.0
        ):
            raise VerificationError(f"{camera_id} decode latency is out of bounds")
        frame_count = health_camera.get("frame_count")
        if not isinstance(frame_count, int) or isinstance(frame_count, bool):
            raise VerificationError(f"{camera_id} frame count is invalid")
        inference_frame_count = health_camera.get("inference_frame_count")
        if (
            not isinstance(inference_frame_count, int)
            or isinstance(inference_frame_count, bool)
            or inference_frame_count < 1
            or health_camera.get("inference_fresh") is not True
        ):
            raise VerificationError(f"{camera_id} inference state is not fresh")
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
            "inference_frame_count": inference_frame_count,
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
    except Exception as exc:
        raise VerificationError(
            f"MJPEG request failed: {sanitize_source_error(exc)}"
        ) from None
    if hashes[0] == hashes[1]:
        raise VerificationError("two complete MJPEG frames have identical content")
    return hashes


def _validate_timestamp_samples(
    first,
    second,
    max_age_seconds,
    now=None,
    require_inference_progress=True,
    require_capture_progress=True,
):
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
            if (
                timestamp_name == "capture"
                and (
                    after < before
                    or (require_capture_progress and after == before)
                )
            ):
                raise VerificationError(
                    f"{camera_id} {timestamp_name} timestamp "
                    + ("regressed" if after < before else "did not advance")
                )
            if timestamp_name == "event" and after < before:
                raise VerificationError(
                    f"{camera_id} {timestamp_name} timestamp regressed"
                )
            for timestamp in (before, after):
                age = (now - timestamp).total_seconds()
                if age < -5.0 or age > max_age_seconds:
                    raise VerificationError(
                        f"{camera_id} {timestamp_name} timestamp is outside max age"
                    )
        if second_camera["frame_count"] < first_camera["frame_count"]:
            raise VerificationError(f"{camera_id} frame count regressed")
        if (
            require_capture_progress
            and second_camera["frame_count"] == first_camera["frame_count"]
        ):
            raise VerificationError(f"{camera_id} frame count did not advance")
        inference_advanced = (
            second_camera["inference_frame_count"]
            > first_camera["inference_frame_count"]
            and second_camera["event"] > first_camera["event"]
        )
        if require_inference_progress and not inference_advanced:
            raise VerificationError(
                f"{camera_id} inference did not advance within deadline"
            )
        output[camera_id] = {
            "capture_times": [
                first_camera["capture"].isoformat().replace("+00:00", "Z"),
                second_camera["capture"].isoformat().replace("+00:00", "Z"),
            ],
            "event_times": [
                first_camera["event"].isoformat().replace("+00:00", "Z"),
                second_camera["event"].isoformat().replace("+00:00", "Z"),
            ],
            "inference_frame_counts": [
                first_camera["inference_frame_count"],
                second_camera["inference_frame_count"],
            ],
        }
    return output


def verify_live_feeds(
    base_url,
    sample_interval_seconds=DEFAULT_SAMPLE_INTERVAL_SECONDS,
    max_age_seconds=15.0,
    timeout_seconds=20.0,
    stream_path_template="/streams/{camera_id}.mjpg",
    capture_progress_timeout_seconds=(
        DEFAULT_CAPTURE_PROGRESS_TIMEOUT_SECONDS
    ),
    inference_progress_timeout_seconds=(
        DEFAULT_INFERENCE_PROGRESS_TIMEOUT_SECONDS
    ),
    inference_poll_interval_seconds=INFERENCE_POLL_INTERVAL_SECONDS,
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

    capture_progress_timeout_seconds = float(
        capture_progress_timeout_seconds
    )
    inference_progress_timeout_seconds = float(
        inference_progress_timeout_seconds
    )
    inference_poll_interval_seconds = float(inference_poll_interval_seconds)
    if capture_progress_timeout_seconds <= 0:
        raise VerificationError("capture progress timeout must be positive")
    if inference_progress_timeout_seconds <= 0:
        raise VerificationError("inference progress timeout must be positive")
    if inference_poll_interval_seconds < 0:
        raise VerificationError("inference poll interval cannot be negative")

    first = _collect_timestamp_sample(base_url, timeout_seconds)
    capture_deadline = time.monotonic() + capture_progress_timeout_seconds
    inference_deadline = (
        time.monotonic() + inference_progress_timeout_seconds
    )
    time.sleep(max(0.0, float(sample_interval_seconds)))
    second = _collect_timestamp_sample(base_url, timeout_seconds)
    _validate_timestamp_samples(
        first,
        second,
        max_age_seconds,
        require_inference_progress=False,
        require_capture_progress=False,
    )

    capture_progressed = {
        camera_id: second[camera_id]
        for camera_id in CAMERA_IDS
        if (
            second[camera_id]["frame_count"]
            > first[camera_id]["frame_count"]
            and second[camera_id]["capture"] > first[camera_id]["capture"]
        )
    }
    inference_progressed = {
        camera_id: second[camera_id]
        for camera_id in CAMERA_IDS
        if (
            second[camera_id]["inference_frame_count"]
            > first[camera_id]["inference_frame_count"]
            and second[camera_id]["event"] > first[camera_id]["event"]
        )
    }
    previous = second
    while (
        len(capture_progressed) != len(CAMERA_IDS)
        or len(inference_progressed) != len(CAMERA_IDS)
    ):
        now_monotonic = time.monotonic()
        if (
            len(capture_progressed) != len(CAMERA_IDS)
            and now_monotonic >= capture_deadline
        ):
            missing = sorted(set(CAMERA_IDS) - set(capture_progressed))
            raise VerificationError(
                "capture did not advance within deadline for "
                + ",".join(missing)
            )
        if (
            len(inference_progressed) != len(CAMERA_IDS)
            and now_monotonic >= inference_deadline
        ):
            missing = sorted(set(CAMERA_IDS) - set(inference_progressed))
            raise VerificationError(
                "inference did not advance within deadline for "
                + ",".join(missing)
            )
        remaining_deadlines = []
        if len(capture_progressed) != len(CAMERA_IDS):
            remaining_deadlines.append(capture_deadline - now_monotonic)
        if len(inference_progressed) != len(CAMERA_IDS):
            remaining_deadlines.append(inference_deadline - now_monotonic)
        time.sleep(min(
            inference_poll_interval_seconds,
            max(0.0, min(remaining_deadlines)),
        ))
        current = _collect_timestamp_sample(base_url, timeout_seconds)
        _validate_timestamp_samples(
            previous,
            current,
            max_age_seconds,
            require_inference_progress=False,
            require_capture_progress=False,
        )
        for camera_id in CAMERA_IDS:
            if (
                camera_id not in capture_progressed
                and current[camera_id]["frame_count"]
                > first[camera_id]["frame_count"]
                and current[camera_id]["capture"]
                > first[camera_id]["capture"]
            ):
                capture_progressed[camera_id] = current[camera_id]
            if camera_id in inference_progressed:
                continue
            if (
                current[camera_id]["inference_frame_count"]
                > first[camera_id]["inference_frame_count"]
                and current[camera_id]["event"] > first[camera_id]["event"]
            ):
                inference_progressed[camera_id] = current[camera_id]
        previous = current

    final = {}
    for camera_id in CAMERA_IDS:
        final[camera_id] = dict(inference_progressed[camera_id])
        final[camera_id]["capture"] = capture_progressed[camera_id][
            "capture"
        ]
        final[camera_id]["frame_count"] = capture_progressed[camera_id][
            "frame_count"
        ]
    output = _validate_timestamp_samples(
        first,
        final,
        max_age_seconds,
        require_inference_progress=True,
    )

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
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help=(
            "seconds between timestamp samples; defaults above the measured "
            "2.002-second fragment cadence"
        ),
    )
    parser.add_argument("--max-age", type=float, default=15.0)
    parser.add_argument(
        "--capture-progress-timeout",
        type=float,
        default=DEFAULT_CAPTURE_PROGRESS_TIMEOUT_SECONDS,
        help="maximum seconds for every raw capture counter to advance",
    )
    parser.add_argument(
        "--inference-progress-timeout",
        type=float,
        default=DEFAULT_INFERENCE_PROGRESS_TIMEOUT_SECONDS,
        help="maximum seconds for every camera inference counter to advance",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
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
            stream_path_template=args.stream_path_template,
            capture_progress_timeout_seconds=args.capture_progress_timeout,
            inference_progress_timeout_seconds=(
                args.inference_progress_timeout
            ),
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

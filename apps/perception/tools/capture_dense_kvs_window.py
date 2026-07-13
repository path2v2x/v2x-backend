#!/usr/bin/env python3
"""Capture a hash-bound dense KVS image window around retained events.

This command is read-only against Kinesis Video Streams.  It derives the
window from an existing event-frame capture report, requests producer-time
JPEGs, and writes immutable frame/hash evidence without persisting signed
endpoints or credentials.
"""

import argparse
import base64
import binascii
import ctypes
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import cv2
import numpy as np


INPUT_SCHEMAS = {
    "v2x-detection-event-frame-capture/v1",
    "v2x-detection-event-frame-capture/v2",
}
OUTPUT_SCHEMA = "v2x-dense-kvs-window/v1"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
MAX_IMAGES_PER_RESPONSE = 25
MAX_REQUEST_SPAN_SECONDS = 4
MAX_REQUEST_COUNT = 100
FETCH_SAMPLING_MS = 200
MAX_TARGET_OFFSET_MS = 200
RENAME_NOREPLACE = 1


class DenseCaptureError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DenseCaptureError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DenseCaptureError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timedelta(0):
        raise DenseCaptureError(f"{label} is not UTC")
    return parsed


def canonical_utc(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def load_source_report(path, camera_id, object_id):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DenseCaptureError("source event report is unreadable or invalid") from exc
    if report.get("schema") not in INPUT_SCHEMAS:
        raise DenseCaptureError("source event report schema is unsupported")
    events = report.get("events")
    if not isinstance(events, list):
        raise DenseCaptureError("source event report has no event list")
    selected = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("camera_id") == camera_id
        and event.get("object_id") == object_id
    ]
    if len(selected) < 2:
        raise DenseCaptureError(
            "fewer than two source events bind the requested window"
        )
    event_ids = []
    timestamps = []
    for event in selected:
        event_id = event.get("event_id")
        frame = event.get("frame")
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in event_ids
            or not isinstance(frame, dict)
            or not isinstance(frame.get("sha256"), str)
            or len(frame["sha256"]) != 64
        ):
            raise DenseCaptureError("source event identity or frame binding is invalid")
        frame_path = Path(frame.get("path", "")).expanduser().resolve()
        try:
            frame_raw = frame_path.read_bytes()
        except OSError as exc:
            raise DenseCaptureError("source event frame is unreadable") from exc
        if sha256_bytes(frame_raw) != frame["sha256"]:
            raise DenseCaptureError("source event frame hash does not match")
        event_ids.append(event_id)
        timestamps.append(
            parse_utc(event.get("selected_frame_timestamp_utc"), "event timestamp")
        )
    selected.sort(key=lambda value: value["selected_frame_timestamp_utc"])
    return path, raw, selected, min(timestamps), max(timestamps)


def validate_parameters(camera_id, object_id, padding_seconds, sampling_ms):
    if camera_id not in CAMERAS:
        raise DenseCaptureError("camera must be ch1 through ch4")
    if not isinstance(object_id, str) or not object_id.strip():
        raise DenseCaptureError("object ID is missing")
    if (
        not isinstance(padding_seconds, (int, float))
        or isinstance(padding_seconds, bool)
        or not math.isfinite(float(padding_seconds))
        or not 0.0 <= float(padding_seconds) <= 10.0
    ):
        raise DenseCaptureError("padding must be between zero and ten seconds")
    if (
        not isinstance(sampling_ms, int)
        or isinstance(sampling_ms, bool)
        or not 200 <= sampling_ms <= 20000
    ):
        raise DenseCaptureError("sampling interval must be 200 through 20000 ms")


def sampling_targets(start, end, sampling_ms):
    """Return one whole-request sampling grid clipped to the exact window."""
    if end <= start:
        raise DenseCaptureError("dense KVS request has no positive time range")
    origin = start.replace(microsecond=0)
    step_us = sampling_ms * 1000
    offset_us = start.microsecond
    first_step = (offset_us + step_us - 1) // step_us
    target = origin + timedelta(microseconds=first_step * step_us)
    targets = []
    while target <= end:
        targets.append(target)
        target += timedelta(microseconds=step_us)
    if not targets:
        raise DenseCaptureError("dense KVS request contains no sampling target")
    if len(targets) > 100:
        raise DenseCaptureError(
            "requested dense window exceeds the 100-image API bound"
        )
    return origin, targets


def request_windows(start, end, sampling_ms):
    """Cover one sampling grid with deterministic whole-second fetch windows.

    GetImages currently returns at most 25 images per response even when a
    larger MaxResults value is requested.  Continuation-token requests have
    also produced intermittent timestamp-validation failures in the service.
    Fetching at 200 ms in at-most-four-second, whole-second windows keeps every
    request below that effective page size.  The response candidates are then
    matched to one declared whole-request target grid, so a chunk boundary
    never resets the sampling phase.
    """
    _origin, targets = sampling_targets(start, end, sampling_ms)
    windows = []
    for target in targets:
        bucket_start = target.replace(microsecond=0)
        bucket_end = bucket_start + timedelta(seconds=1)
        if not windows:
            windows.append((bucket_start, bucket_end))
            continue
        left, right = windows[-1]
        merged_right = max(right, bucket_end)
        if (
            bucket_start <= right
            and (merged_right - left).total_seconds()
            <= MAX_REQUEST_SPAN_SECONDS
        ):
            windows[-1] = (left, merged_right)
        else:
            windows.append((bucket_start, bucket_end))
    if len(windows) > MAX_REQUEST_COUNT:
        raise DenseCaptureError("dense KVS capture exceeds 100 bounded requests")
    return windows


def match_sampling_targets(targets, candidates):
    """Maximize ordered target coverage, then minimize total timing error."""
    tolerance = timedelta(milliseconds=MAX_TARGET_OFFSET_MS)
    row_count = len(targets) + 1
    column_count = len(candidates) + 1
    scores = [[(0, 0)] * column_count for _ in range(row_count)]
    decisions = [[None] * column_count for _ in range(row_count)]

    def better(left, right):
        return (left[0], -left[1]) > (right[0], -right[1])

    for target_index in range(1, row_count):
        decisions[target_index][0] = "skip_target"
    for candidate_index in range(1, column_count):
        decisions[0][candidate_index] = "skip_candidate"
    for target_index in range(1, row_count):
        target = targets[target_index - 1]
        for candidate_index in range(1, column_count):
            candidate_timestamp = candidates[candidate_index - 1][0]
            best_score = scores[target_index - 1][candidate_index]
            best_decision = "skip_target"
            skip_candidate = scores[target_index][candidate_index - 1]
            if better(skip_candidate, best_score):
                best_score = skip_candidate
                best_decision = "skip_candidate"
            offset = abs(candidate_timestamp - target)
            if offset <= tolerance:
                previous = scores[target_index - 1][candidate_index - 1]
                matched_score = (
                    previous[0] + 1,
                    previous[1] + offset // timedelta(microseconds=1),
                )
                if better(matched_score, best_score) or matched_score == best_score:
                    best_score = matched_score
                    best_decision = "match"
            scores[target_index][candidate_index] = best_score
            decisions[target_index][candidate_index] = best_decision

    matched = []
    target_index = len(targets)
    candidate_index = len(candidates)
    while target_index > 0 or candidate_index > 0:
        decision = decisions[target_index][candidate_index]
        if decision == "match":
            target = targets[target_index - 1]
            timestamp, content, image = candidates[candidate_index - 1]
            matched.append((target, timestamp, content, image))
            target_index -= 1
            candidate_index -= 1
        elif decision == "skip_target":
            target_index -= 1
        elif decision == "skip_candidate":
            candidate_index -= 1
        else:
            break
    matched.reverse()
    return matched


def publish_directory_no_replace(source, destination):
    """Atomically publish one staged directory without replacing any entry."""
    source = Path(source)
    destination = Path(destination)
    if source.parent != destination.parent:
        raise DenseCaptureError("staging and output directories must share a parent")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise DenseCaptureError(
            "atomic no-replace publication is unavailable"
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        ctypes.set_errno(0)
        result = renameat2(
            parent_fd,
            os.fsencode(source.name),
            parent_fd,
            os.fsencode(destination.name),
            RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(
                error_number,
                "dense capture output already exists",
                str(destination),
            )
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )
    finally:
        os.close(parent_fd)


def decode_image(item):
    if not isinstance(item, dict):
        return None
    timestamp = item.get("TimeStamp") if isinstance(item, dict) else None
    content = item.get("ImageContent") if isinstance(item, dict) else None
    if isinstance(content, str):
        try:
            content = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error):
            content = None
    if (
        not isinstance(timestamp, datetime)
        or not isinstance(content, (bytes, bytearray))
        or not content
        or item.get("Error")
    ):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    content = bytes(content)
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise DenseCaptureError("KVS returned an undecodable JPEG")
    return timestamp, content, image


def capture(
    source_report,
    camera_id,
    object_id,
    output_dir,
    profile_name,
    region_name="us-west-2",
    padding_seconds=1.0,
    sampling_ms=200,
    *,
    session_factory=None,
):
    validate_parameters(camera_id, object_id, padding_seconds, sampling_ms)
    source_path, source_raw, events, first, last = load_source_report(
        source_report, camera_id, object_id
    )
    start = first - timedelta(seconds=float(padding_seconds))
    end = last + timedelta(seconds=float(padding_seconds))
    target_origin, targets = sampling_targets(start, end, sampling_ms)

    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise DenseCaptureError("dense capture output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    if session_factory is None:
        import boto3

        session_factory = boto3.Session
    session = session_factory(profile_name=profile_name, region_name=region_name)
    kvs = session.client("kinesisvideo")
    stream_name = f"v2x-backend-cam-{camera_id}"
    endpoint = kvs.get_data_endpoint(
        StreamName=stream_name, APIName="GET_IMAGES"
    )["DataEndpoint"]
    archived = session.client(
        "kinesis-video-archived-media", endpoint_url=endpoint
    )
    image_items = []
    windows = request_windows(start, end, sampling_ms)
    for chunk_start, chunk_end in windows:
        request = {
            "StreamName": stream_name,
            "ImageSelectorType": "PRODUCER_TIMESTAMP",
            "StartTimestamp": chunk_start,
            "EndTimestamp": chunk_end,
            "SamplingInterval": FETCH_SAMPLING_MS,
            "Format": "JPEG",
            # The aligned request span is sized below the service's effective
            # 25-image response page, so any token is a fail-closed anomaly.
            "MaxResults": MAX_IMAGES_PER_RESPONSE,
        }
        try:
            response = archived.get_images(**request)
        except Exception as exc:
            raise DenseCaptureError("bounded KVS GetImages request failed") from exc
        if response.get("NextToken"):
            raise DenseCaptureError(
                "bounded KVS GetImages response unexpectedly requires pagination"
            )
        values = response.get("Images", [])
        if not isinstance(values, list) or len(values) > MAX_IMAGES_PER_RESPONSE:
            raise DenseCaptureError("KVS returned an invalid bounded image page")
        image_items.extend(values)
    page_count = len(windows)
    decoded_all = []
    discarded_error_count = 0
    for item in image_items:
        value = decode_image(item)
        if value is None:
            discarded_error_count += 1
        else:
            decoded_all.append(value)
    in_window = [value for value in decoded_all if start <= value[0] <= end]
    discarded_out_of_window_count = len(decoded_all) - len(in_window)
    by_timestamp = {}
    duplicate_timestamp_count = 0
    for value in in_window:
        timestamp, content, _image = value
        previous = by_timestamp.get(timestamp)
        if previous is not None:
            if previous[1] != content:
                raise DenseCaptureError(
                    "KVS returned conflicting images at one producer timestamp"
                )
            duplicate_timestamp_count += 1
            continue
        by_timestamp[timestamp] = value
    decoded = list(by_timestamp.values())
    decoded.sort(key=lambda value: value[0])
    matched = match_sampling_targets(targets, decoded)
    if len(matched) < 3:
        raise DenseCaptureError("KVS returned fewer than three usable dense frames")
    timestamps = [value[1] for value in matched]
    if len(set(timestamps)) != len(timestamps):
        raise DenseCaptureError("KVS returned duplicate producer timestamps")
    dimensions = {
        (image.shape[1], image.shape[0]) for _, _, _, image in matched
    }
    if len(dimensions) != 1:
        raise DenseCaptureError("dense KVS frames have mixed resolutions")
    width, height = next(iter(dimensions))

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent
        )
    )
    rows = []
    try:
        (temporary / "frames").mkdir()
        for index, (target, timestamp, content, _image) in enumerate(matched):
            name = f"frame-{index:03d}.jpg"
            path = temporary / "frames" / name
            path.write_bytes(content)
            rows.append({
                "index": index,
                "target_timestamp_utc": canonical_utc(target),
                "target_offset_ms": (
                    (timestamp - target).total_seconds() * 1000.0
                ),
                "producer_timestamp_utc": canonical_utc(timestamp),
                "path": f"frames/{name}",
                "sha256": sha256_bytes(content),
                "byte_count": len(content),
                "width": width,
                "height": height,
            })
        gaps_ms = [
            (right - left).total_seconds() * 1000.0
            for left, right in zip(timestamps, timestamps[1:])
        ]
        report = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "source_event_report": {
                "path": str(source_path),
                "sha256": sha256_bytes(source_raw),
            },
            "source_events": [
                {
                    "event_id": event["event_id"],
                    "selected_frame_timestamp_utc": event[
                        "selected_frame_timestamp_utc"
                    ],
                    "frame_sha256": event["frame"]["sha256"],
                }
                for event in events
            ],
            "camera_id": camera_id,
            "object_id": object_id,
            "stream_name": stream_name,
            "region": region_name,
            "requested_window": {
                "start_utc": canonical_utc(start),
                "end_utc": canonical_utc(end),
                "sampling_interval_ms": sampling_ms,
                "padding_seconds": float(padding_seconds),
            },
            "resolution": [width, height],
            "frames": rows,
            "frame_count": len(rows),
            "target_count": len(targets),
            "missing_target_count": len(targets) - len(rows),
            "response_page_count": page_count,
            "discarded_error_count": discarded_error_count,
            "discarded_out_of_window_count": discarded_out_of_window_count,
            "duplicate_timestamp_count": duplicate_timestamp_count,
            "unused_in_window_count": len(decoded) - len(rows),
            "request_strategy": {
                "whole_second_aligned": True,
                "maximum_images_per_response": MAX_IMAGES_PER_RESPONSE,
                "maximum_request_span_seconds": max(
                    (right - left).total_seconds() for left, right in windows
                ),
                "continuation_tokens_accepted": False,
                "exact_requested_window_post_filter": True,
                "fetch_sampling_interval_ms": FETCH_SAMPLING_MS,
                "target_grid_origin_utc": canonical_utc(target_origin),
                "target_sampling_interval_ms": sampling_ms,
                "maximum_target_offset_ms": MAX_TARGET_OFFSET_MS,
                "single_target_phase_across_requests": True,
            },
            "maximum_interframe_gap_ms": max(gaps_ms) if gaps_ms else None,
            "acceptance_failures": [
                "dense_frames_are_unreviewed_tracking_inputs",
                "model_object_id_is_not_independent_identity_truth",
                "camera_intrinsics_are_not_measured",
            ],
            "safety": {
                "read_only_kinesis_calls": True,
                "signed_endpoints_persisted": False,
                "credentials_persisted": False,
            },
        }
        (temporary / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            publish_directory_no_replace(temporary, output_dir)
        except FileExistsError as exc:
            raise DenseCaptureError("dense capture output already exists") from exc
        except OSError as exc:
            raise DenseCaptureError("atomic dense capture publication failed") from exc
    except DenseCaptureError:
        raise
    except OSError as exc:
        raise DenseCaptureError("dense capture staging failed") from exc
    finally:
        try:
            shutil.rmtree(temporary)
        except FileNotFoundError:
            pass
    return output_dir / "capture-report.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--padding-seconds", type=float, default=1.0)
    parser.add_argument("--sampling-ms", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        output = capture(
            args.source_report,
            args.camera,
            args.object_id,
            args.output_dir,
            args.aws_profile,
            args.region,
            args.padding_seconds,
            args.sampling_ms,
        )
    except DenseCaptureError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

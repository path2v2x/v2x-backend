from ultralytics import YOLO
import cv2
import hashlib
import math
import os
import re
import threading
from pathlib import Path
import sys
import numpy as np
import json
import uuid
import time
import requests
import tracking_utils
import kinesis_utils
from live_capture import LiveStreamReader
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import radians, cos, sin, asin, sqrt
from urllib.parse import urlparse
from runtime_health import (
    AttemptRateLimiter,
    MonotonicEventClock,
    StreamRecovery,
    sanitize_source_error,
    utc_iso,
    validate_batch_response,
)
from tracking_utils import AppearanceExtractor, KalmanTracker, VehicleAppearanceExtractor

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from v2x_common.geodesy import local_xz_to_geodetic  # noqa: E402


TIMESTAMP_SCHEMA_VERSION = 2
MAX_VEHICLE_LOCALIZATION_UNCERTAINTY_M = 2.0


def assess_media_clock(
    frame_media_clock,
    decode_received_epoch,
    minimum_latency_ms=-1_000.0,
    maximum_latency_ms=120_000.0,
):
    result = {
        "status": "unavailable",
        "trusted": False,
        "media_timestamp_utc": None,
        "media_clock": None,
        "decode_latency_ms": None,
        "media_epoch": None,
    }
    if not isinstance(frame_media_clock, dict):
        return result

    media_timestamp = frame_media_clock.get("media_timestamp_utc")
    raw_clock = frame_media_clock.get("media_clock")
    if not isinstance(media_timestamp, str) or not isinstance(raw_clock, dict):
        result["status"] = "invalid_metadata"
        return result
    if raw_clock.get("source") != "hls_ext_x_program_date_time":
        result["status"] = "unsupported_source"
        return result
    if raw_clock.get("schema_version") != 1:
        result["status"] = "unsupported_schema"
        return result

    try:
        parsed_media = datetime.fromisoformat(
            media_timestamp.replace("Z", "+00:00")
        )
        if parsed_media.tzinfo is None:
            raise ValueError("media timestamp lacks timezone")
        media_epoch = parsed_media.timestamp()
        receipt_epoch = float(decode_received_epoch)
    except (TypeError, ValueError, OverflowError):
        result["status"] = "invalid_timestamp"
        return result
    if not math.isfinite(media_epoch) or not math.isfinite(receipt_epoch):
        result["status"] = "invalid_timestamp"
        return result

    decode_latency_ms = (receipt_epoch - media_epoch) * 1000.0
    if not (
        float(minimum_latency_ms)
        <= decode_latency_ms
        <= float(maximum_latency_ms)
    ):
        result["status"] = "latency_out_of_bounds"
        result["decode_latency_ms"] = round(decode_latency_ms, 3)
        return result

    safe_clock = {
        "source": "hls_ext_x_program_date_time",
        "schema_version": 1,
    }
    anchor_timestamp = raw_clock.get("anchor_program_date_time_utc")
    if isinstance(anchor_timestamp, str):
        try:
            parsed_anchor = datetime.fromisoformat(
                anchor_timestamp.replace("Z", "+00:00")
            )
            if parsed_anchor.tzinfo is not None:
                safe_clock["anchor_program_date_time_utc"] = anchor_timestamp
        except ValueError:
            pass
    fragment_id = raw_clock.get("anchor_fragment_id")
    if isinstance(fragment_id, str) and re.fullmatch(
        r"[A-Za-z0-9._~-]{1,256}", fragment_id
    ):
        safe_clock["anchor_fragment_id"] = fragment_id
    for key in (
        "anchor_fragment_frame_offset_milliseconds",
        "anchor_capture_position_milliseconds",
        "anchor_media_sequence",
        "position_milliseconds",
        "capture_position_milliseconds",
        "segment_duration_seconds",
    ):
        value = raw_clock.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ):
            safe_clock[key] = value

    try:
        anchor_epoch = datetime.fromisoformat(
            safe_clock["anchor_program_date_time_utc"].replace("Z", "+00:00")
        ).timestamp()
        reconstructed_epoch = (
            anchor_epoch + float(safe_clock["position_milliseconds"]) / 1000.0
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        result["status"] = "incomplete_provenance"
        return result
    if abs(reconstructed_epoch - media_epoch) * 1000.0 > 5.0:
        result["status"] = "inconsistent_provenance"
        return result

    result.update({
        "status": "matched",
        "trusted": True,
        "media_timestamp_utc": media_timestamp,
        "media_clock": safe_clock,
        "decode_latency_ms": round(decode_latency_ms, 3),
        "media_epoch": media_epoch,
    })
    return result


def attach_media_clock_metadata(
    records,
    frame_media_clock,
    minimum_latency_ms=-1_000.0,
    maximum_latency_ms=120_000.0,
):
    """Use exact HLS media time while preserving decode-receipt time."""
    for record in records:
        assessment = assess_media_clock(
            frame_media_clock,
            record.get("ingested_at_epoch"),
            minimum_latency_ms,
            maximum_latency_ms,
        )
        record["timestamp_schema_version"] = TIMESTAMP_SCHEMA_VERSION
        record["media_time_trusted"] = assessment["trusted"]
        record["media_clock_status"] = assessment["status"]
        if not assessment["trusted"]:
            if assessment["decode_latency_ms"] is not None:
                record["decode_latency_ms"] = assessment["decode_latency_ms"]
            continue

        media_timestamp = assessment["media_timestamp_utc"]
        receipt_timestamp = record.get("timestamp_utc")
        receipt_epoch = record.get("ingested_at_epoch")
        record["decode_received_at_utc"] = receipt_timestamp
        record["decode_received_at_epoch"] = receipt_epoch
        record["timestamp_utc"] = media_timestamp
        record["media_timestamp_utc"] = media_timestamp
        record["media_clock"] = dict(assessment["media_clock"])
        record["decode_latency_ms"] = assessment["decode_latency_ms"]
        record["expires_at"] = int(assessment["media_epoch"]) + 86400
        event_id = record.get("event_id")
        if event_id:
            record["ts_event"] = f"{media_timestamp}#{event_id}"
    return records


def records_ready_for_upload(records, live):
    if not live:
        return list(records)
    return [
        record
        for record in records
        if (
            record.get("timestamp_schema_version") == TIMESTAMP_SCHEMA_VERSION
            and record.get("media_time_trusted") is True
            and vehicle_localization_acceptable(record)
        )
    ]


def vehicle_localization_acceptable(record):
    if record.get("object_type") not in {"car", "truck", "bus"}:
        return True
    value = (
        record.get("camera_data", {})
        .get("bifocal_metadata", {})
        .get("world_position", {})
        .get("uncertainty_meters")
    )
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(value)
        and 0.0 <= value <= MAX_VEHICLE_LOCALIZATION_UNCERTAINTY_M
    )

def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)

def env_optional(name):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value

DEFAULT_CAMERAS_JSON = Path(__file__).resolve().parents[2] / "config" / "cameras.json"

def load_cameras_config():
    """Load the shared camera-pose config used by perception and the twin rig.

    Path override via V2X_CAMERAS_JSON. Production fails closed. The legacy
    nominal model is available only through an explicit development-only flag
    and can never upload detections.
    """
    path = os.getenv("V2X_CAMERAS_JSON") or str(DEFAULT_CAMERAS_JSON)
    try:
        with open(path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        if env_bool("V2X_ALLOW_LEGACY_CAMERA_CONFIG", False):
            return None
        raise RuntimeError(f"required cameras config is unavailable: {path}") from exc
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("cameras"), list)
        or not config["cameras"]
    ):
        if env_bool("V2X_ALLOW_LEGACY_CAMERA_CONFIG", False):
            return None
        raise RuntimeError("required cameras config has no camera definitions")
    return config


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_object_sha256(value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def camera_config_fingerprints(config, path=None):
    """Bind emitted observations to the exact file and selected camera."""
    config_path = Path(
        path
        or os.getenv("V2X_CAMERAS_JSON")
        or str(DEFAULT_CAMERAS_JSON)
    )
    file_hash = sha256_file(config_path)
    camera_hashes = {
        camera["id"]: canonical_object_sha256(camera)
        for camera in config.get("cameras", [])
    }
    return file_hash, camera_hashes


def _resolve_calibration_evidence_path(root, value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} path is unreadable") from exc


def _verify_intrinsics_artifacts(calibration, evidence_root):
    artifact = _resolve_calibration_evidence_path(
        evidence_root, calibration.get("artifact_path"), "intrinsics artifact"
    )
    report_path = _resolve_calibration_evidence_path(
        evidence_root, calibration.get("report_path"), "intrinsics report"
    )
    source_values = calibration.get("source_image_paths")
    if not isinstance(source_values, list) or not source_values:
        raise ValueError("intrinsics source image paths are missing")
    source_paths = [
        _resolve_calibration_evidence_path(
            evidence_root, value, "intrinsics source image"
        )
        for value in source_values
    ]
    if sha256_file(artifact) != calibration["artifact_sha256"]:
        raise ValueError("intrinsics artifact hash does not match")
    report_hash = calibration.get("report_sha256")
    if (
        not isinstance(report_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", report_hash) is None
        or sha256_file(report_path) != report_hash
    ):
        raise ValueError("intrinsics report hash does not match")
    try:
        artifact_value = json.loads(artifact.read_bytes())
        report = json.loads(report_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("intrinsics artifact/report is invalid JSON") from exc
    declared_artifact = {
        key: calibration[key]
        for key in (
            "method",
            "image_count",
            "source_images_sha256",
            "rms_reprojection_error_px",
            "resolution",
            "camera_matrix",
            "distortion",
        )
    }
    if artifact_value != declared_artifact:
        raise ValueError("intrinsics artifact contents do not match config")
    accepted = report.get("accepted") if isinstance(report, dict) else None
    holdouts = report.get("holdouts") if isinstance(report, dict) else None
    metrics = report.get("holdout_metrics") if isinstance(report, dict) else None
    try:
        holdout_rmse = float(metrics["rmse_px"])
        holdout_max = float(metrics["max_error_px"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("intrinsics holdout metrics are invalid") from exc
    expected_report_schema = {
        "checkerboard": "v2x-checkerboard-calibration-report/v1",
        "charuco": "v2x-charuco-calibration-report/v1",
    }.get(calibration.get("method"))
    if (
        report.get("schema") != expected_report_schema
        or not isinstance(accepted, list)
        or len(accepted) < 10
        or not isinstance(holdouts, list)
        or len(holdouts) < 2
        or not math.isfinite(holdout_rmse)
        or not math.isfinite(holdout_max)
        or holdout_rmse > 2.0
        or holdout_max > 5.0
    ):
        raise ValueError("intrinsics untouched holdout gate did not pass")
    expected_hashes = calibration["source_images_sha256"]
    actual_hashes = []
    for path in source_paths:
        raw = path.read_bytes()
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("intrinsics source image is not decodable")
        actual_hashes.append(hashlib.sha256(raw).hexdigest())
    if (
        len(actual_hashes) != len(expected_hashes)
        or len(set(actual_hashes)) != len(actual_hashes)
        or set(actual_hashes) != set(expected_hashes)
    ):
        raise ValueError("intrinsics source image hashes do not match")
    report_hashes = {
        item.get("sha256")
        for item in accepted + holdouts
        if isinstance(item, dict)
    }
    if report_hashes != set(expected_hashes):
        raise ValueError("intrinsics report/source hashes do not match")


def camera_intrinsics_evidence(camera, *, evidence_root=None,
                               require_artifacts=False):
    """Return measured Brown-Conrady coefficients or block startup."""
    intrinsics = camera.get("intrinsics")
    calibration = camera.get("intrinsics_calibration")
    if not isinstance(intrinsics, dict) or not isinstance(calibration, dict):
        raise ValueError(
            f"camera {camera.get('id')!r} has no measured intrinsics evidence"
        )
    try:
        matrix = calibration["camera_matrix"]
        resolution = calibration["resolution"]
        distortion = calibration["distortion"]
        rms = float(calibration["rms_reprojection_error_px"])
        image_count = int(calibration["image_count"])
        source_hashes = calibration["source_images_sha256"]
        artifact_hash = calibration["artifact_sha256"]
        values = np.array(
            [
                distortion["k1"],
                distortion["k2"],
                distortion["p1"],
                distortion["p2"],
                distortion["k3"],
            ],
            dtype=np.float64,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"camera {camera.get('id')!r} intrinsics evidence is incomplete"
        ) from exc
    expected_matrix = np.array(
        [
            [intrinsics["fx"], 0.0, intrinsics["cx"]],
            [0.0, intrinsics["fy"], intrinsics["cy"]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    try:
        observed_matrix = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"camera {camera.get('id')!r} intrinsics matrix is invalid"
        ) from exc
    valid_hashes = (
        isinstance(artifact_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact_hash) is not None
        and isinstance(source_hashes, list)
        and len(source_hashes) >= 10
        and len(set(source_hashes)) == len(source_hashes)
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            for value in source_hashes
        )
    )
    valid = (
        calibration.get("method") in {"checkerboard", "charuco"}
        and valid_hashes
        and image_count == len(source_hashes)
        and image_count >= 10
        and math.isfinite(rms)
        and 0.0 <= rms <= 2.0
        and resolution == [intrinsics["width"], intrinsics["height"]]
        and observed_matrix.shape == (3, 3)
        and np.all(np.isfinite(observed_matrix))
        and np.allclose(observed_matrix, expected_matrix, rtol=0.0, atol=1e-9)
        and values.shape == (5,)
        and np.all(np.isfinite(values))
    )
    if not valid:
        raise ValueError(
            f"camera {camera.get('id')!r} intrinsics evidence is invalid"
        )
    if require_artifacts:
        if evidence_root is None:
            raise ValueError("intrinsics evidence root is required")
        _verify_intrinsics_artifacts(calibration, evidence_root)
    return values


def camera_localization_parameters(camera):
    """Return measured localization uncertainty or block deployment startup."""
    localization = camera.get("localization")
    if not isinstance(localization, dict):
        raise ValueError(
            f"camera {camera.get('id')!r} has no measured localization block"
        )
    try:
        pixel_sigma = float(localization["pixel_sigma"])
        calibration_uncertainty_m = float(
            localization["calibration_uncertainty_m"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"camera {camera.get('id')!r} localization evidence is incomplete"
        ) from exc
    if (
        not math.isfinite(pixel_sigma)
        or pixel_sigma < 0.1
        or not math.isfinite(calibration_uncertainty_m)
        or not 0.0
        <= calibration_uncertainty_m
        <= MAX_VEHICLE_LOCALIZATION_UNCERTAINTY_M
    ):
        raise ValueError(
            f"camera {camera.get('id')!r} localization evidence is invalid"
        )
    return pixel_sigma, calibration_uncertainty_m

def parse_video_paths():
    value = os.getenv("V2X_PERCEPTION_VIDEO_PATHS", "").strip()
    if value:
        if value.startswith("["):
            return json.loads(value)
        return [item.strip() for item in value.split(",") if item.strip()]
    return [
        "v2x-backend-cam-ch1",
        "v2x-backend-cam-ch2",
        "v2x-backend-cam-ch3",
        "v2x-backend-cam-ch4",
    ]

def parse_camera_ids(video_paths):
    value = os.getenv("V2X_PERCEPTION_CAMERA_IDS", "").strip()
    if value:
        if value.startswith("["):
            return json.loads(value)
        return [item.strip() for item in value.split(",") if item.strip()]

    camera_ids = []
    for path in video_paths:
        match = re.search(r"(ch\d+)", str(path))
        camera_ids.append(match.group(1) if match else f"cam{len(camera_ids) + 1}")
    return camera_ids

class FrameBroadcaster:
    def __init__(self, camera_ids, jpeg_quality=80, stale_seconds=15.0):
        self.camera_ids = list(camera_ids)
        self.jpeg_quality = int(jpeg_quality)
        self.stale_seconds = float(stale_seconds)
        self.frames = {}
        self.frame_counts = {camera_id: 0 for camera_id in self.camera_ids}
        self.camera_health = {
            camera_id: {
                "state": "starting",
                "source_updated_at": None,
                "last_frame_monotonic": None,
                "last_error": None,
                "reconnect_attempts": 0,
                "media_clock_status": "unavailable",
                "media_time_trusted": False,
                "decode_latency_ms": None,
            }
            for camera_id in self.camera_ids
        }
        self.latest_detections = {
            camera_id: {
                "updated_at": None,
                "frame_count": 0,
                "detections": [],
            }
            for camera_id in self.camera_ids
        }
        self.condition = threading.Condition()

    def publish(
        self,
        camera_id,
        frame,
        source_updated_at=None,
        source_monotonic=None,
        media_clock_health=None,
    ):
        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
        )
        if not ok:
            return

        with self.condition:
            self.frames[camera_id] = encoded.tobytes()
            self.frame_counts[camera_id] = self.frame_counts.get(camera_id, 0) + 1
            camera_health = self.camera_health[camera_id]
            was_reconnecting = camera_health["state"] == "reconnecting"
            camera_health.update({
                "source_updated_at": source_updated_at or utc_iso(),
                "last_frame_monotonic": (
                    time.monotonic()
                    if source_monotonic is None
                    else float(source_monotonic)
                ),
            })
            if isinstance(media_clock_health, dict):
                camera_health.update({
                    "media_clock_status": media_clock_health.get(
                        "status", "unavailable"
                    ),
                    "media_time_trusted": bool(
                        media_clock_health.get("trusted")
                    ),
                    "decode_latency_ms": media_clock_health.get(
                        "decode_latency_ms"
                    ),
                })
            # A final genuine buffered frame may finish inference after the
            # reader has already reported its next read failure. Publishing it
            # must not erase the newer reconnecting state or claim readiness.
            if not was_reconnecting:
                camera_health.update({
                    "state": "streaming",
                    "last_error": None,
                    "reconnect_attempts": 0,
                })
            self.condition.notify_all()

    def publish_detections(self, camera_id, detections, source_updated_at=None):
        summary = []
        for det in detections:
            metadata = det.get("camera_data", {}).get("bifocal_metadata", {})
            summary.append({
                "object_id": det.get("object_id"),
                "object_type": det.get("object_type"),
                "confidence_score": det.get("confidence_score"),
                "timestamp_utc": det.get("timestamp_utc"),
                "media_timestamp_utc": det.get("media_timestamp_utc"),
                "media_clock": det.get("media_clock"),
                "media_clock_status": det.get("media_clock_status"),
                "decode_received_at_utc": det.get("decode_received_at_utc"),
                "decode_latency_ms": det.get("decode_latency_ms"),
                "timestamp_schema_version": det.get("timestamp_schema_version"),
                "media_time_trusted": det.get("media_time_trusted"),
                "perception_run_id": det.get("perception_run_id"),
                "device_id": det.get("device_id"),
                "track_id": det.get("track_id"),
                "bbox": metadata.get("bbox"),
                "gps_location": det.get("gps_location"),
            })

        with self.condition:
            self.latest_detections[camera_id] = {
                "updated_at": source_updated_at or utc_iso(),
                "frame_count": self.frame_counts.get(camera_id, 0),
                "detections": summary,
            }
            self.condition.notify_all()

    def mark_reconnecting(self, camera_id, error, reconnect_attempts):
        with self.condition:
            self.camera_health[camera_id].update({
                "state": "reconnecting",
                "last_error": sanitize_source_error(error),
                "reconnect_attempts": int(reconnect_attempts),
            })
            self.condition.notify_all()

    def mark_connected(self, camera_id):
        """Record source recovery while waiting for its first processed frame."""
        with self.condition:
            self.camera_health[camera_id].update({
                "state": "connected",
                "last_error": None,
                "reconnect_attempts": 0,
            })
            self.condition.notify_all()

    def snapshot_health(self, now_monotonic=None):
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        with self.condition:
            cameras = {}
            ready = True
            media_clock_ready = True
            for camera_id in self.camera_ids:
                entry = self.camera_health[camera_id]
                last_frame = entry["last_frame_monotonic"]
                age_seconds = None if last_frame is None else max(0.0, now - last_frame)
                fresh = age_seconds is not None and age_seconds <= self.stale_seconds
                state = entry["state"]
                if state == "streaming" and not fresh:
                    state = "stale"
                ready = ready and fresh and state == "streaming"
                media_clock_ready = (
                    media_clock_ready and entry["media_time_trusted"] is True
                )
                cameras[camera_id] = {
                    "state": state,
                    "fresh": fresh,
                    "age_seconds": None if age_seconds is None else round(age_seconds, 3),
                    "source_updated_at": entry["source_updated_at"],
                    "frame_count": self.frame_counts.get(camera_id, 0),
                    "last_error": sanitize_source_error(entry["last_error"]),
                    "reconnect_attempts": entry["reconnect_attempts"],
                    "media_clock_status": entry["media_clock_status"],
                    "media_time_trusted": entry["media_time_trusted"],
                    "decode_latency_ms": entry["decode_latency_ms"],
                }
            return {
                "status": "ok" if ready else "degraded",
                "ready": ready,
                "media_clock_ready": media_clock_ready,
                "generated_at": utc_iso(),
                "stale_after_seconds": self.stale_seconds,
                "cameras": cameras,
                "frames": dict(self.frame_counts),
            }

    def snapshot_detections(self):
        with self.condition:
            return json.loads(json.dumps({
                "generated_at": utc_iso(),
                "cameras": self.latest_detections,
            }))

    def wait_for_frame(self, camera_id, last_count, timeout=5.0):
        with self.condition:
            available = self.condition.wait_for(
                lambda: (
                    self.frame_counts.get(camera_id, 0) != last_count
                    and self.camera_health[camera_id]["state"] == "streaming"
                    and self.camera_health[camera_id]["last_frame_monotonic"]
                    is not None
                    and time.monotonic()
                    - self.camera_health[camera_id]["last_frame_monotonic"]
                    <= self.stale_seconds
                ),
                timeout=timeout,
            )
            if not available:
                return None, last_count
            return self.frames.get(camera_id), self.frame_counts.get(camera_id, last_count)

class PerceptionHttpServer:
    def __init__(self, host, port, broadcaster):
        self.host = host
        self.port = int(port)
        self.broadcaster = broadcaster
        self.httpd = None
        self.thread = None

    def start(self):
        broadcaster = self.broadcaster

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _set_cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET,HEAD,OPTIONS")

            def _send_json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self._set_cors()
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._set_cors()
                self.end_headers()

            def do_HEAD(self):
                path = urlparse(self.path).path
                if path == "/health":
                    self._send_json(200, broadcaster.snapshot_health())
                    return

                if path == "/detections/latest":
                    self._send_json(200, broadcaster.snapshot_detections())
                    return

                match = re.match(r"^/streams/([^/.]+)\.(mjpg|mjpeg)$", path)
                if match and match.group(1) in broadcaster.camera_ids:
                    self.send_response(200)
                    self._set_cors()
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    return

                self.send_response(404)
                self._set_cors()
                self.end_headers()

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/health":
                    self._send_json(200, broadcaster.snapshot_health())
                    return

                if path == "/detections/latest":
                    self._send_json(200, broadcaster.snapshot_detections())
                    return

                match = re.match(r"^/streams/([^/.]+)\.(mjpg|mjpeg)$", path)
                if not match:
                    self.send_response(404)
                    self._set_cors()
                    self.end_headers()
                    return

                camera_id = match.group(1)
                if camera_id not in broadcaster.camera_ids:
                    self.send_response(404)
                    self._set_cors()
                    self.end_headers()
                    return

                self.send_response(200)
                self._set_cors()
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()

                last_count = -1
                while True:
                    frame, last_count = broadcaster.wait_for_frame(camera_id, last_count)
                    if frame is None:
                        continue
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        break

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        print(f"Perception MJPEG server listening on http://{self.host}:{self.port}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

def xy_to_gps(
    X, Z, origin_lat, origin_lon, heading_deg, map_georeference=None
):
        """
        Convert local camera XZ coordinates (meters) to GPS lat/lon.
        Uses a simple flat-earth approximation (accurate within ~10km).

        Args:
            X: Right offset in meters from camera
            Z: Forward offset in meters from camera
            origin_lat: Camera GPS latitude
            origin_lon: Camera GPS longitude

        Returns:
            (latitude, longitude)
        """
        projection = map_georeference or (
            f"+proj=tmerc +lat_0={float(origin_lat):.15g} "
            f"+lon_0={float(origin_lon):.15g} +k=1 +x_0=0 +y_0=0 "
            "+datum=WGS84 +units=m +no_defs"
        )
        lat, lon = local_xz_to_geodetic(
            float(X),
            float(Z),
            float(origin_lat),
            float(origin_lon),
            float(heading_deg),
            projection,
        )
        return float(lat), float(lon)

def compute_geohash(lat, lon, precision=5):
    """
    Encode lat/lon to a geohash string

    Args:
        lat: Latitude
        lon: Longitude
        precision: Geohash length (5 = ~5km x 5km cell)

    Returns:
        Geohash string
    """
    BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    geohash = []
    bits = [16, 8, 4, 2, 1]
    bit_idx = 0
    char_val = 0
    is_lon = True

    while len(geohash) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                char_val |= bits[bit_idx]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                char_val |= bits[bit_idx]
                lat_range[0] = mid
            else:
                lat_range[1] = mid

        is_lon = not is_lon
        if bit_idx < 4:
            bit_idx += 1
        else:
            geohash.append(BASE32[char_val])
            bit_idx = 0
            char_val = 0

    return "".join(geohash)

class MultiCameraPipeline:
    VEHICLE_CLASSES = {'car', 'truck', 'bus'}
    CROSS_CAMERA_MAX_TIME_DELTA_SEC = 2.5
    TRACK_MAX_IDLE_SEC = 15.0
    TRACK_CLOSE_DISTANCE_M = 3.0
    TRACK_MAX_DISTANCE_M = 8.0
    TRACK_MIN_APPEARANCE_SIMILARITY = 0.65
    VEHICLE_MIN_APPEARANCE_SIMILARITY = 0.60
    VEHICLE_AMBIGUITY_DISTANCE_MARGIN_M = 1.5
    VEHICLE_AMBIGUITY_APPEARANCE_MARGIN = 0.08
    MAX_VEHICLE_ASSOCIATION_UNCERTAINTY_M = (
        MAX_VEHICLE_LOCALIZATION_UNCERTAINTY_M
    )
    VEHICLE_EMBEDDING_REFRESH_FRAMES = 5
    VEHICLE_EMBEDDING_CACHE_MAX = 1024
    def __init__(self, detectors, perception_run_id=None):
        """
        Initialize the MultiCameraPipeline.

        Args:
            detectors: List of VideoObjectDetector instances.

        Returns:
            None
        """
        self.detectors = detectors
        self.all_clean_detections = []
        self.global_tracks = {} # Store global tracks
        self.local_to_global = {} # "device_id_local_track_id" -> global_id
        self.next_global_id = 0
        self.perception_run_id = str(
            uuid.UUID(str(perception_run_id or uuid.uuid4()))
        )
        self.perception_run_prefix = self.perception_run_id.replace("-", "")[:8]
        self.extractor = AppearanceExtractor()
        self.vehicle_extractor = VehicleAppearanceExtractor()
        self.vehicle_embedding_cache = {}

    @staticmethod
    def _appearance_similarity(left, right):
        if left is None or right is None:
            return None
        value = float(np.dot(left, right))
        return value if math.isfinite(value) else None

    def _vehicle_embedding(self, frame, detection, frame_count):
        track_id = detection.get('track_id')
        key = (
            (detection.get('device_id'), track_id)
            if track_id is not None else None
        )
        cached = self.vehicle_embedding_cache.get(key) if key is not None else None
        cache_age = (
            frame_count - cached['frame_count'] if cached is not None else None
        )
        if (
            cached is not None
            and cache_age < self.VEHICLE_EMBEDDING_REFRESH_FRAMES
        ):
            return cached['embedding']
        embedding = self.vehicle_extractor.extract(
            frame, detection['camera_data']['bifocal_metadata']['bbox']
        )
        if embedding is None:
            return cached['embedding'] if cached is not None else None
        if embedding is not None and key is not None:
            self.vehicle_embedding_cache[key] = {
                'embedding': embedding,
                'frame_count': frame_count,
            }
            if len(self.vehicle_embedding_cache) > self.VEHICLE_EMBEDDING_CACHE_MAX:
                oldest = min(
                    self.vehicle_embedding_cache,
                    key=lambda item: self.vehicle_embedding_cache[item]['frame_count'],
                )
                if oldest != key:
                    self.vehicle_embedding_cache.pop(oldest, None)
        return embedding

    @staticmethod
    def haversine_distance_meters(lat1, lon1, lat2, lon2):
        """
        Calculate the great circle distance in meters between two GPS points.

        Args:
            lat1: Latitude of the first point.
            lon1: Longitude of the first point.
            lat2: Latitude of the second point.
            lon2: Longitude of the second point.

        Returns:
            Distance in meters between the two points.
        """
        R = 6371000.0  # Earth radius in meters
        dLat = radians(lat2 - lat1)
        dLon = radians(lon2 - lon1)
        lat1 = radians(lat1)
        lat2 = radians(lat2)

        a = sin(dLat/2)**2 + cos(lat1)*cos(lat2)*sin(dLon/2)**2
        c = 2 * asin(sqrt(a))
        return R * c

    @staticmethod
    def _event_epoch(detection):
        value = detection.get('media_timestamp_utc') or detection.get('timestamp_utc')
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed.timestamp() if parsed.tzinfo is not None else None

    @classmethod
    def _trusted_event_epoch(cls, detection):
        """Return the schema-v2 HLS media epoch or fail closed."""
        schema = detection.get('timestamp_schema_version')
        clock = detection.get('media_clock')
        media_timestamp = detection.get('media_timestamp_utc')
        if (
            schema != TIMESTAMP_SCHEMA_VERSION
            or isinstance(schema, bool)
            or detection.get('media_time_trusted') is not True
            or detection.get('media_clock_status') != 'matched'
            or not isinstance(clock, dict)
            or clock.get('source') != 'hls_ext_x_program_date_time'
            or clock.get('schema_version') != 1
            or isinstance(clock.get('schema_version'), bool)
            or detection.get('timestamp_utc') != media_timestamp
        ):
            return None
        return cls._event_epoch(detection)

    @staticmethod
    def _localization_uncertainty(detection):
        value = (
            detection.get('camera_data', {})
            .get('bifocal_metadata', {})
            .get('world_position', {})
            .get('uncertainty_meters')
        )
        try:
            value = float(value)
        except (TypeError, ValueError):
            return float('inf')
        return max(0.0, value) if math.isfinite(value) else float('inf')

    @classmethod
    def _vehicle_association_uncertainty(cls, *detections_or_values):
        total = 0.0
        for item in detections_or_values:
            value = (
                cls._localization_uncertainty(item)
                if isinstance(item, dict)
                else float(item)
            )
            if not math.isfinite(value):
                return None
            total += value
        if total > cls.MAX_VEHICLE_ASSOCIATION_UNCERTAINTY_M:
            return None
        return total

    def _same_frame_vehicle_candidate(
        self, new_det, existing_det, require_trusted_time
    ):
        if (
            existing_det.get('object_type') not in self.VEHICLE_CLASSES
            or new_det.get('device_id') == existing_det.get('device_id')
        ):
            return None
        epoch_reader = (
            self._trusted_event_epoch
            if require_trusted_time else self._event_epoch
        )
        new_epoch = epoch_reader(new_det)
        existing_epoch = epoch_reader(existing_det)
        if (
            new_epoch is None
            or existing_epoch is None
            or abs(new_epoch - existing_epoch)
            > self.CROSS_CAMERA_MAX_TIME_DELTA_SEC
        ):
            return None
        appearance = self._appearance_similarity(
            new_det.get('embedding'), existing_det.get('embedding')
        )
        if (
            appearance is None
            or appearance < self.VEHICLE_MIN_APPEARANCE_SIMILARITY
        ):
            return None
        uncertainty = self._vehicle_association_uncertainty(
            new_det, existing_det
        )
        if uncertainty is None:
            return None
        distance = self.haversine_distance_meters(
            new_det['gps_location']['latitude'],
            new_det['gps_location']['longitude'],
            existing_det['gps_location']['latitude'],
            existing_det['gps_location']['longitude'],
        )
        if distance >= 2.0 + uncertainty:
            return None
        return {
            'record': existing_det,
            'distance_meters': float(distance),
            'appearance_similarity': float(appearance),
            'media_delta_seconds': abs(new_epoch - existing_epoch),
        }

    def _select_unambiguous_vehicle_candidate(self, candidates):
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                candidate['distance_meters'],
                -candidate['appearance_similarity'],
            ),
        )
        if len(ordered) == 1:
            return ordered[0], None
        best = ordered[0]
        ambiguous = any(
            (
                candidate['distance_meters'] - best['distance_meters']
                < self.VEHICLE_AMBIGUITY_DISTANCE_MARGIN_M
                and abs(
                    candidate['appearance_similarity']
                    - best['appearance_similarity']
                ) < self.VEHICLE_AMBIGUITY_APPEARANCE_MARGIN
            )
            or (
                candidate['appearance_similarity']
                > best['appearance_similarity']
                + self.VEHICLE_AMBIGUITY_APPEARANCE_MARGIN
            )
            for candidate in ordered[1:]
        )
        if not ambiguous:
            return best, None
        return None, {
            'method': 'ambiguous_spatiotemporal_convnext',
            'distance_margin_meters': self.VEHICLE_AMBIGUITY_DISTANCE_MARGIN_M,
            'appearance_margin': self.VEHICLE_AMBIGUITY_APPEARANCE_MARGIN,
            'candidates': [
                {
                    'distance_meters': round(item['distance_meters'], 3),
                    'appearance_similarity': round(
                        item['appearance_similarity'], 4
                    ),
                    'device_id': item['record'].get('device_id'),
                    'object_id': item['record'].get('object_id'),
                }
                for item in ordered[:4]
            ],
        }

    def _prune_tracks(self, current_time_epoch):
        stale = {
            gid for gid, track in self.global_tracks.items()
            if current_time_epoch - track['last_seen'] > self.TRACK_MAX_IDLE_SEC
        }
        for gid in stale:
            self.global_tracks.pop(gid, None)
        if stale:
            self.local_to_global = {
                key: gid for key, gid in self.local_to_global.items() if gid not in stale
            }

    def deduplicate(
        self,
        raw_buffer,
        current_time_epoch,
        merge_radius_meters=1.5,
        require_trusted_time=True,
    ):
        """
        Takes a list of V2X JSON records and removes duplicates that are
        physically too close together (overlapping camera seams).

        Args:
            raw_buffer: List of raw detection records.
            current_time_epoch: Current time in epoch seconds.
            merge_radius_meters: Radius in meters to consider detections as duplicates.

        Returns:
            List of deduplicated and tracked detection records.
        """
        self._prune_tracks(current_time_epoch)
        clean_buffer = []

        for new_det in raw_buffer:
            if new_det['object_type'] in self.VEHICLE_CLASSES:
                candidates = [
                    candidate
                    for existing_det in clean_buffer
                    if (
                        candidate := self._same_frame_vehicle_candidate(
                            new_det, existing_det, require_trusted_time
                        )
                    ) is not None
                ]
                selected, ambiguity = self._select_unambiguous_vehicle_candidate(
                    candidates
                ) if candidates else (None, None)
                if ambiguity is not None:
                    new_det['identity_ambiguity'] = ambiguity
                    clean_buffer.append(new_det)
                    continue
                if selected is None:
                    clean_buffer.append(new_det)
                    continue
                existing_det = selected['record']
                evidence = {
                    'method': 'spatiotemporal_convnext',
                    'appearance_similarity': round(
                        selected['appearance_similarity'], 4
                    ),
                    'appearance_threshold': self.VEHICLE_MIN_APPEARANCE_SIMILARITY,
                    'distance_meters': round(selected['distance_meters'], 3),
                    'media_delta_seconds': round(
                        selected['media_delta_seconds'], 3
                    ),
                    'devices': sorted([
                        str(new_det['device_id']),
                        str(existing_det['device_id']),
                    ]),
                }
                if new_det['confidence_score'] > existing_det['confidence_score']:
                    existing_det.clear()
                    existing_det.update(new_det)
                existing_det['cross_camera_dedup'] = evidence
                continue

            is_duplicate = False

            for existing_det in clean_buffer:
                if (
                    new_det['object_type'] != existing_det['object_type']
                    and not (
                        new_det['object_type'] in self.VEHICLE_CLASSES
                        and existing_det['object_type'] in self.VEHICLE_CLASSES
                    )
                ):
                    continue

                if new_det['device_id'] == existing_det['device_id']:
                    continue

                dist = self.haversine_distance_meters(
                    new_det['gps_location']['latitude'],
                    new_det['gps_location']['longitude'],
                    existing_det['gps_location']['latitude'],
                    existing_det['gps_location']['longitude']
                )

                new_epoch = self._event_epoch(new_det)
                existing_epoch = self._event_epoch(existing_det)
                radius = float(merge_radius_meters)

                if dist < radius:
                    is_duplicate = True
                    if new_det['confidence_score'] > existing_det['confidence_score']:
                        # Select one internally consistent observation. Partial
                        # field replacement used to pair one camera's bbox with
                        # another camera's timestamp/media clock.
                        existing_det.clear()
                        existing_det.update(new_det)
                    break

            if not is_duplicate:
                clean_buffer.append(new_det)

        # 2. Temporal Tracking (Cross frames)
        tracked_buffer = []
        claimed_gids = set() # Prevent multiple detections in the same frame from claiming the same track
        vehicle_classes = self.VEHICLE_CLASSES
        for det in clean_buffer:
            best_match_id = None
            min_dist = float('inf')
            match_evidence = None
            temporal_vehicle_candidates = []
            local_key = f"{det['device_id']}_{det['track_id']}"
            vehicle_detection = det['object_type'] in vehicle_classes
            event_epoch = (
                self._trusted_event_epoch(det)
                if require_trusted_time and vehicle_detection
                else self._event_epoch(det)
            )

            # 1. Fast Path: Use visual local tracker ID
            if (
                local_key in self.local_to_global
                and not (vehicle_detection and det.get('identity_ambiguity'))
            ):
                gid = self.local_to_global[local_key]
                if gid in self.global_tracks and gid not in claimed_gids:
                    track = self.global_tracks[gid]
                    compatible_class = (
                        track['type'] == det['object_type']
                        or (
                            track['type'] in vehicle_classes
                            and det['object_type'] in vehicle_classes
                        )
                    )
                    trusted_vehicle = (
                        not vehicle_detection
                        or not require_trusted_time
                        or event_epoch is not None
                    )
                    bounded_vehicle_uncertainty = (
                        not vehicle_detection
                        or self._vehicle_association_uncertainty(det) is not None
                    )
                    if (
                        compatible_class
                        and trusted_vehicle
                        and bounded_vehicle_uncertainty
                        and current_time_epoch - track['last_seen']
                        <= self.TRACK_MAX_IDLE_SEC
                    ):
                        best_match_id = gid
                        match_evidence = {
                            'method': 'same_camera_local_tracker',
                            'device_id': det.get('device_id'),
                            'local_track_id': det.get('track_id'),
                        }

            # 2. Slow Path: Spatial Math Search
            if best_match_id is None and not (
                vehicle_detection and det.get('identity_ambiguity')
            ):
                for gid, track in self.global_tracks.items():
                    if gid in claimed_gids:
                        continue

                    t_type = track['type']
                    d_type = det['object_type']
                    if t_type != d_type:
                        # Allow matches between vehicle types
                        if not (t_type in vehicle_classes and d_type in vehicle_classes):
                            continue

                    dt = current_time_epoch - track['last_seen']
                    if dt > self.TRACK_MAX_IDLE_SEC:
                        continue

                    pred_lat, pred_lon = track['kf'].get_prediction(dt=dt if dt > 0 else 0.1)
                    last_lat, last_lon = track['kf'].x[0], track['kf'].x[1]

                    dist_pred = self.haversine_distance_meters(
                        det['gps_location']['latitude'], det['gps_location']['longitude'],
                        pred_lat, pred_lon
                    )
                    dist_last = self.haversine_distance_meters(
                        det['gps_location']['latitude'], det['gps_location']['longitude'],
                        last_lat, last_lon
                    )
                    dist = min(dist_pred, dist_last)

                    emb_sim = 0.0
                    appearance = self._appearance_similarity(
                        track.get('embedding'), det.get('embedding')
                    )
                    if appearance is not None:
                        emb_sim = appearance

                    vehicle_reassociation = d_type in vehicle_classes
                    cross_camera_vehicle = (
                        vehicle_reassociation
                        and track.get('last_device_id') != det.get('device_id')
                    )
                    if vehicle_reassociation and (
                        appearance is None
                        or appearance < self.VEHICLE_MIN_APPEARANCE_SIMILARITY
                    ):
                        continue

                    if vehicle_reassociation:
                        track_epoch = track.get('event_epoch')
                        if (
                            (require_trusted_time and (
                                event_epoch is None or track_epoch is None
                            ))
                            or (
                                event_epoch is not None
                                and track_epoch is not None
                                and abs(event_epoch - track_epoch)
                                > self.TRACK_MAX_IDLE_SEC
                            )
                        ):
                            continue

                    if vehicle_reassociation:
                        uncertainty = self._vehicle_association_uncertainty(
                            det, track.get('uncertainty_meters', float('inf'))
                        )
                        if uncertainty is None:
                            continue
                    else:
                        uncertainty = min(
                            2.0,
                            self._localization_uncertainty(det)
                            + float(track.get('uncertainty_meters', 0.0)),
                        )
                    close_gate = self.TRACK_CLOSE_DISTANCE_M + uncertainty
                    max_gate = self.TRACK_MAX_DISTANCE_M + uncertainty
                    if dist < max_gate and dist < min_dist:
                        if (
                            dist < close_gate
                            or emb_sim >= self.TRACK_MIN_APPEARANCE_SIMILARITY
                        ):
                            evidence = {
                                'method': (
                                    'cross_camera_spatiotemporal_convnext'
                                    if cross_camera_vehicle
                                    else (
                                        'same_camera_spatiotemporal_convnext'
                                        if vehicle_reassociation
                                        else 'same_camera_spatiotemporal'
                                    )
                                ),
                                'distance_meters': round(float(dist), 3),
                                'appearance_similarity': (
                                    round(float(appearance), 4)
                                    if appearance is not None else None
                                ),
                                'appearance_threshold': (
                                    self.VEHICLE_MIN_APPEARANCE_SIMILARITY
                                    if vehicle_reassociation else None
                                ),
                                'previous_device_id': track.get('last_device_id'),
                                'device_id': det.get('device_id'),
                            }
                            if vehicle_reassociation:
                                temporal_vehicle_candidates.append({
                                    'gid': gid,
                                    'record': {
                                        'device_id': track.get('last_device_id'),
                                        'object_id': (
                                            f"global_{track['type']}_"
                                            f"{self.perception_run_prefix}_{gid}"
                                        ),
                                    },
                                    'distance_meters': float(dist),
                                    'appearance_similarity': float(appearance),
                                    'match_evidence': evidence,
                                })
                            else:
                                best_match_id = gid
                                min_dist = dist
                                match_evidence = evidence

            if best_match_id is None and temporal_vehicle_candidates:
                selected, ambiguity = self._select_unambiguous_vehicle_candidate(
                    temporal_vehicle_candidates
                )
                if ambiguity is not None:
                    ambiguity['method'] = 'ambiguous_track_reattachment'
                    det['identity_ambiguity'] = ambiguity
                elif selected is not None:
                    best_match_id = selected['gid']
                    min_dist = selected['distance_meters']
                    match_evidence = selected['match_evidence']

            if best_match_id is not None:
                claimed_gids.add(best_match_id)
                dt = current_time_epoch - self.global_tracks[best_match_id]['last_seen']
                self.global_tracks[best_match_id]['kf'].predict(dt=dt if dt > 0 else 0.1)
                self.global_tracks[best_match_id]['kf'].update([det['gps_location']['latitude'], det['gps_location']['longitude']])

                if det.get('embedding') is not None:
                    old_emb = self.global_tracks[best_match_id].get('embedding')
                    if old_emb is not None:
                        new_emb = 0.8 * old_emb + 0.2 * det['embedding']
                        self.global_tracks[best_match_id]['embedding'] = new_emb / np.linalg.norm(new_emb)
                    else:
                        self.global_tracks[best_match_id]['embedding'] = det['embedding']

                self.global_tracks[best_match_id]['last_seen'] = current_time_epoch
                self.global_tracks[best_match_id]['event_epoch'] = event_epoch
                self.global_tracks[best_match_id]['uncertainty_meters'] = self._localization_uncertainty(det)
                self.global_tracks[best_match_id]['last_device_id'] = det.get('device_id')
                det['object_id'] = (
                    f"global_{self.global_tracks[best_match_id]['type']}_"
                    f"{self.perception_run_prefix}_{best_match_id}"
                )
                self.local_to_global[local_key] = best_match_id
                det['identity_association'] = match_evidence
                track_type = self.global_tracks[best_match_id]['type']
                if det['object_type'] != track_type:
                    det['identity_association']['class_conflict'] = {
                        'track_type': track_type,
                        'observed_type': det['object_type'],
                    }
            else:
                self.next_global_id += 1
                new_gid = self.next_global_id
                self.global_tracks[new_gid] = {
                    'type': det['object_type'],
                    'kf': KalmanTracker(det['gps_location']['latitude'], det['gps_location']['longitude']),
                    'embedding': det.get('embedding'),
                    'last_seen': current_time_epoch
                    , 'event_epoch': event_epoch
                    , 'uncertainty_meters': self._localization_uncertainty(det),
                    'last_device_id': det.get('device_id')
                }
                claimed_gids.add(new_gid)
                det['object_id'] = (
                    f"global_{det['object_type']}_"
                    f"{self.perception_run_prefix}_{new_gid}"
                )
                self.local_to_global[local_key] = new_gid
                det['identity_association'] = {
                    'method': 'new_track',
                    'device_id': det.get('device_id'),
                    'local_track_id': det.get('track_id'),
                }

            det['perception_run_id'] = self.perception_run_id
            tracked_buffer.append(det)

        return tracked_buffer

    def process_streams(self, video_paths, show_live=True, upload=False, output_json=None, output_video=None, output_image=None, output_validate=False, stream_broadcaster=None, camera_ids=None, upload_min_interval_sec=0.0):
        """
        Processes multiple videos in parallel, running YOLO, 3D math, and deduplication.

        Args:
            video_paths: List of file paths to the input videos.
            show_live: Boolean to display the live processing grid.
            upload: Boolean to upload detections to V2X API.
            output_json: Path to save the detections JSON.
            output_video: Path to save the annotated output video.
            output_image: Path to save a final annotated image frame.
            output_validate: Boolean to enable validation output.
            stream_broadcaster: Optional FrameBroadcaster for per-camera MJPEG output.
            camera_ids: Camera IDs corresponding to video_paths.
            upload_min_interval_sec: Minimum time between detection batch uploads.

        Returns:
            None
        """
        if len(self.detectors) != len(video_paths):
            print("Error: Number of detectors must match number of video paths.")
            return
        if camera_ids is None:
            camera_ids = parse_camera_ids(video_paths)

        is_kinesis = [
            "v2x-backend-cam" in str(path)
            or str(path).startswith(("http://", "https://"))
            for path in video_paths
        ]
        live_mode = bool(is_kinesis) and all(is_kinesis)
        if any(is_kinesis) and not live_mode:
            raise ValueError("live HLS streams and recorded files cannot be mixed")

        reconnect_initial = env_float("V2X_PERCEPTION_RECONNECT_INITIAL_SEC", 1.0)
        reconnect_max = env_float("V2X_PERCEPTION_RECONNECT_MAX_SEC", 30.0)
        open_timeout_ms = int(env_float("V2X_PERCEPTION_OPEN_TIMEOUT_MS", 10_000))
        read_timeout_ms = int(env_float("V2X_PERCEPTION_READ_TIMEOUT_MS", 10_000))
        frame_identity_history_size = int(env_float(
            "V2X_PERCEPTION_FRAME_IDENTITY_HISTORY", 256
        ))
        duplicate_frame_limit = int(env_float(
            "V2X_PERCEPTION_DUPLICATE_FRAME_LIMIT", 90
        ))
        media_clock_min_latency_ms = env_float(
            "V2X_PERCEPTION_MEDIA_CLOCK_MIN_LATENCY_MS", -1_000.0
        )
        media_clock_max_latency_ms = env_float(
            "V2X_PERCEPTION_MEDIA_CLOCK_MAX_LATENCY_MS", 120_000.0
        )
        caps = [None] * len(video_paths)
        buffered_frames = [None] * len(video_paths)
        buffered_msecs = [-1.0] * len(video_paths)
        live_readers = []
        live_sequences = [0] * len(video_paths)

        def _source_for(index):
            path = str(video_paths[index])
            if "v2x-backend-cam" in path:
                return kinesis_utils.get_kvs_hls_url(path)
            return path

        def _open_capture(source, live):
            if not live:
                return cv2.VideoCapture(source)

            params = []
            open_timeout_property = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
            read_timeout_property = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
            if open_timeout_property is not None:
                params.extend([open_timeout_property, open_timeout_ms])
            if read_timeout_property is not None:
                params.extend([read_timeout_property, read_timeout_ms])
            if params:
                return cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
            return cv2.VideoCapture(source)

        if live_mode:
            def _state_callback(index):
                def callback(state, error, failures, delay_seconds):
                    if state == "connected":
                        if stream_broadcaster:
                            stream_broadcaster.mark_connected(camera_ids[index])
                        return
                    if stream_broadcaster:
                        stream_broadcaster.mark_reconnecting(
                            camera_ids[index], error, failures
                        )
                    print(
                        f"Camera {camera_ids[index]} unavailable; retrying in "
                        f"{delay_seconds:.1f}s ({error})."
                    )
                return callback

            for index in range(len(video_paths)):
                recovery = StreamRecovery(reconnect_initial, reconnect_max)
                reader = LiveStreamReader(
                    source_factory=lambda index=index: _source_for(index),
                    capture_factory=lambda source: _open_capture(source, True),
                    recovery=recovery,
                    state_callback=_state_callback(index),
                    media_clock_factory=kinesis_utils.resolve_hls_media_clock,
                    capture_position_milliseconds=lambda cap: cap.get(
                        cv2.CAP_PROP_POS_MSEC
                    ),
                    frame_identity_history_size=frame_identity_history_size,
                    duplicate_frame_limit=duplicate_frame_limit,
                )
                live_readers.append(reader)
        else:
            for index, path in enumerate(video_paths):
                cap = _open_capture(str(path), False)
                caps[index] = cap
                if cap is None or not cap.isOpened():
                    continue
                ret, frame = cap.read()
                if ret and frame is not None:
                    buffered_frames[index] = frame
                    buffered_msecs[index] = cap.get(cv2.CAP_PROP_POS_MSEC)

        frame_count = 0
        upload_rate_limiter = AttemptRateLimiter(upload_min_interval_sec)
        recorded_start_epoch = time.time()
        event_clocks = [
            MonotonicEventClock(
                live=live_mode,
                start_epoch=recorded_start_epoch,
            )
            for _ in video_paths
        ]
        fps = 30
        first_open_cap = next((cap for cap in caps if cap is not None), None)
        if first_open_cap is not None:
            fps = int(first_open_cap.get(cv2.CAP_PROP_FPS)) or 30

        num_cams = len(video_paths)
        if num_cams == 1:
            out_size = (640, 480)
        elif num_cams == 4:
            out_size = (1280, 960) # 2x2 grid
        else:
            # Default horizontal concatenation for 2 or 3 cameras
            out_size = (640 * num_cams, 480)

        # --- NEW: Initialize the Video Writer ---
        writer = None
        if output_video and num_cams > 0:
            # We skip 9/10 frames, so adjust the output framerate so it doesn't play at 10x speed
            out_fps = max(1, fps // 10)

            # Use mp4v codec for standard .mp4 output
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(output_video, fourcc, out_fps, out_size)

        print(f"Starting Multi-Stream Pipeline for {num_cams} cameras...")

        try:
            for reader in live_readers:
                reader.start()
            last_valid_frames = [None] * len(caps)
            for i, f in enumerate(buffered_frames):
                if f is not None:
                    last_valid_frames[i] = f.copy()

            while True:
                frames_to_process = [None] * num_cams
                source_epochs = [None] * num_cams
                source_monotonic = [None] * num_cams
                frame_media_clocks = [None] * num_cams
                source_media_msecs = [None] * num_cams
                if live_mode:
                    for i, reader in enumerate(live_readers):
                        snapshot = reader.snapshot(live_sequences[i])
                        if snapshot is None:
                            continue
                        live_sequences[i] = snapshot["sequence"]
                        frames_to_process[i] = snapshot["frame"]
                        source_epochs[i] = snapshot["source_epoch"]
                        source_monotonic[i] = snapshot["source_monotonic"]
                        frame_media_clocks[i] = snapshot.get("media_clock")

                    if not any(frame is not None for frame in frames_to_process):
                        time.sleep(0.02)
                        continue
                else:
                    valid_msecs = [m for m in buffered_msecs if m >= 0]
                    if not valid_msecs:
                        break
                    global_msec = min(valid_msecs)

                    for i in range(len(caps)):
                        if (
                            buffered_msecs[i] >= 0
                            and buffered_msecs[i] <= global_msec + 35.0
                        ):
                            frames_to_process[i] = buffered_frames[i]
                            source_media_msecs[i] = buffered_msecs[i]
                            ret, frame = caps[i].read()
                            if ret and frame is not None:
                                buffered_frames[i] = frame
                                buffered_msecs[i] = caps[i].get(
                                    cv2.CAP_PROP_POS_MSEC
                                )
                            else:
                                buffered_frames[i] = None
                                buffered_msecs[i] = -1.0

                frame_count += 1

                if frame_count != 1 and frame_count % 2 != 0:
                    continue

                raw_buffer = []
                annotated_frames = []
                batch_event_epochs = []

                for i, frame in enumerate(frames_to_process):
                    detector = self.detectors[i]
                    if frame is None:
                        if last_valid_frames[i] is not None:
                            fallback = cv2.resize(last_valid_frames[i], (640, 480))
                            if show_live or writer or output_image:
                                annotated_frames.append(fallback)
                        continue

                    last_valid_frames[i] = frame.copy()

                    if live_mode:
                        frame_epoch, frame_utc_str = event_clocks[i].next(
                            now_epoch=source_epochs[i]
                        )
                    else:
                        frame_epoch, frame_utc_str = event_clocks[i].next(
                            media_msec=source_media_msecs[i]
                        )
                    batch_event_epochs.append(frame_epoch)

                    results = detector.model.track(frame, persist=True, conf=detector.conf, tracker="botsort.yaml", verbose=False)

                    det_2d = detector.extract_detections(results[0], frame_count)
                    det_3d = detector.compute_3d_detections(
                        det_2d, frame_utc_str, frame_epoch
                    )
                    if live_mode:
                        attach_media_clock_metadata(
                            det_3d,
                            frame_media_clocks[i],
                            media_clock_min_latency_ms,
                            media_clock_max_latency_ms,
                        )

                    for det in det_3d:
                        if det['object_type'] in self.VEHICLE_CLASSES:
                            det['embedding'] = self._vehicle_embedding(
                                frame, det, frame_count
                            )
                        elif det['object_type'] == 'person':
                            emb = self.extractor.extract(frame, det['camera_data']['bifocal_metadata']['bbox'])
                            det['embedding'] = emb
                        else:
                            det['embedding'] = None

                    raw_buffer.extend(det_3d)

                    if show_live or writer or output_image or stream_broadcaster:
                        annotated = detector.draw_detections_3d(frame, det_3d)
                        annotated = cv2.resize(annotated, (640, 480))
                        if stream_broadcaster:
                            media_clock_health = assess_media_clock(
                                frame_media_clocks[i],
                                source_epochs[i],
                                media_clock_min_latency_ms,
                                media_clock_max_latency_ms,
                            )
                            stream_broadcaster.publish(
                                camera_ids[i],
                                annotated,
                                frame_utc_str,
                                source_monotonic=source_monotonic[i],
                                media_clock_health=media_clock_health,
                            )
                        if show_live or writer or output_image:
                            annotated_frames.append(annotated)
                    if stream_broadcaster:
                        stream_broadcaster.publish_detections(
                            camera_ids[i], det_3d, frame_utc_str
                        )

                # Deduplicate objects crossing the seams
                # Using a smaller radius (1.5m) so we don't accidentally merge multiple people in the same frame
                batch_event_epoch = (
                    max(batch_event_epochs)
                    if batch_event_epochs
                    else time.time()
                )
                clean_batch = self.deduplicate(
                    raw_buffer,
                    batch_event_epoch,
                    merge_radius_meters=3.0,
                    require_trusted_time=live_mode,
                )
                self.all_clean_detections.extend(clean_batch)

                # Batch Upload
                uploadable_batch = records_ready_for_upload(
                    clean_batch, live_mode
                )
                if (
                    upload
                    and uploadable_batch
                    and upload_rate_limiter.allow()
                ):
                    if self.detectors[0].upload_batch(uploadable_batch):
                        print(f"Frame {frame_count}: Uploaded {len(uploadable_batch)} unique objects (merged from {len(raw_buffer)} raw detections).")

                if annotated_frames:
                    if len(annotated_frames) == 1:
                        grid = annotated_frames[0]
                    elif len(annotated_frames) == 4:
                        top_row = cv2.hconcat([annotated_frames[0], annotated_frames[1]])
                        bottom_row = cv2.hconcat([annotated_frames[2], annotated_frames[3]])
                        grid = cv2.vconcat([top_row, bottom_row])
                    else:
                        grid = cv2.hconcat(annotated_frames)

                    # Save to file if output_video was provided
                    if writer:
                        writer.write(grid)

                    if output_image:
                        cv2.imwrite(output_image, grid)

                    # Show on screen if requested
                    if show_live:
                        cv2.imshow('V2X Multi-Camera Feed', grid)
                        # wait key was 1
                        if cv2.waitKey(1) & 0xFF == ord('q'):
                            break

        finally:
            for reader in live_readers:
                reader.request_stop()
            stop_deadline = time.monotonic() + max(
                1.0, (open_timeout_ms + read_timeout_ms) / 1000.0 + 1.0
            )
            for reader in live_readers:
                reader.join(max(0.0, stop_deadline - time.monotonic()))
            for cap in caps:
                if cap is not None:
                    cap.release()
            cv2.destroyAllWindows()
            print(f"Multi-Stream complete. Processed {frame_count} frames, found {len(self.all_clean_detections)} total unique objects.")

            if writer:
                writer.release()
                print(f"Video saved to: {output_video}")

            if output_image:
                print(f"Image saved to: {output_image}")

            if output_json:
                for det in self.all_clean_detections:
                    if 'embedding' in det:
                        del det['embedding']
                with open(output_json, 'w') as f:
                    json.dump(self.all_clean_detections, f, indent=2)
                print(f"JSON saved to: {output_json}")

            if output_validate:
                first_person=None
                for det in self.all_clean_detections:
                    if det.get('object_type') == 'person':
                        first_person = det
                        break

                if first_person:
                    metadata = first_person['camera_data']['bifocal_metadata']
                    u_val = metadata['pixel_centroid']['x']
                    v_val = metadata['bbox']['y2']

                    validation_output = {
                        "u": u_val,
                        "v": v_val
                    }
                    print(json.dumps(validation_output, indent=2))

class VideoObjectDetector:
    def __init__(self, model_path, conf=0.25, K=np.eye(3,3), dist_coeffs=None, camera_height=5.0, pitch_deg=0.0, yaw_deg=0.0, heading_deg=0.0, device_id="cam-001", origin_lat=0.0, origin_lon=0.0,
                 city="", state="", country="", localization_pixel_sigma=4.0,
                 calibration_uncertainty_m=float("inf"), image_width=None,
                 image_height=None, cameras_json_sha256=None,
                 camera_config_sha256=None, detector_model_sha256=None,
                 map_georeference=None):

        """
        Args:
            model_path:      Path to YOLO model weights
            conf:            Detection confidence threshold
            K:               3x3 camera intrinsic matrix
            dist_coeffs:     Lens distortion coefficients [k1,k2,p1,p2,k3]
            camera_height:   Camera height above ground in meters
            device_id:       Unique identifier for this camera device
            origin_lat/lon:  GPS coordinates of the camera (used for XZ → GPS)
            city/state/country: Global context metadata
        """

        self.v2x_endpoint = os.getenv("V2X_DETECTIONS_ENDPOINT", self.V2X_ENDPOINT).rstrip("/")
        self.model = YOLO(model_path)
        self.conf = conf
        self.class_names = self.model.names
        self.K = K
        self.dist_coeffs = dist_coeffs if dist_coeffs is not None else np.zeros(5)
        self.camera_height = camera_height
        self.fx = self.K[0, 0]
        self.fy = self.K[1, 1]
        self.cx = self.K[0, 2]
        self.cy = self.K[1, 2]
        self.localization_pixel_sigma = max(
            0.1, float(localization_pixel_sigma)
        )
        self.calibration_uncertainty_m = float(calibration_uncertainty_m)
        self.image_width = int(image_width) if image_width is not None else None
        self.image_height = int(image_height) if image_height is not None else None
        self.cameras_json_sha256 = cameras_json_sha256
        self.camera_config_sha256 = camera_config_sha256
        self.detector_model_sha256 = detector_model_sha256
        self.map_georeference = map_georeference

        self.pitch_deg = pitch_deg
        self.yaw_deg = yaw_deg
        self.heading_deg = heading_deg

        pitch = np.radians(self.pitch_deg)
        yaw = np.radians(self.yaw_deg)

        Rx = np.array([
            [1, 0, 0],
            [0, np.cos(pitch), -np.sin(pitch)],
            [0, np.sin(pitch), np.cos(pitch)]
        ])

        Ry = np.array([
            [np.cos(yaw), 0, np.sin(yaw)],
            [0, 1, 0],
            [-np.sin(yaw), 0, np.cos(yaw)]
        ])

        self.R = Ry @ Rx

        # Metadata
        self.device_id = device_id
        self.origin_lat = origin_lat
        self.origin_lon = origin_lon
        self.city = city
        self.state = state
        self.country = country

        self.all_detections_3d = []
        print(f"Camera parameters:")
        print(f"  Intrinsics: fx={self.fx:.1f}, fy={self.fy:.1f}, cx={self.cx:.1f}, cy={self.cy:.1f}")
        print(f"  Height: {self.camera_height}m")

    def extract_detections(self, result, frame_num):
        """
        Extract 2D bounding boxes and track IDs from YOLO results.

        Args:
            result: YOLO inference result object.
            frame_num: Current frame number.

        Returns:
            List of 2D detection dictionaries.
        """
        detections = []

        # Check if any tracks were actually found
        if result.boxes.id is not None:
            # Get IDs as an array of integers
            track_ids = result.boxes.id.int().cpu().tolist()

            for box, track_id in zip(result.boxes, track_ids):
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                conf = float(box.conf[0])
                cls = int(box.cls[0])
                class_name = self.class_names.get(cls, 'unknown')

                allowed_classes = {'car', 'person', 'truck'} #, 'bus', 'person', 'bike', 'bicycle', 'motor', 'motorcycle', 'rider', 'traffic light', 'traffic sign', 'train'}
                if class_name not in allowed_classes:
                    continue

                detections.append({
                    'frame': frame_num,
                    'track_id': track_id,
                    'class_name': class_name,
                    'confidence': conf,
                    'bbox': {'x1': float(x1), 'y1': float(y1), 'x2': float(x2), 'y2': float(y2)},
                    'center': {'x': float((x1 + x2) / 2), 'y': float((y1 + y2) / 2)}
                })
        return detections

    def get_class_color(self, class_id):
        """
        Get color for each class for visualization.

        Args:
            class_id: Integer ID of the object class.

        Returns:
            RGB color tuple (B, G, R).
        """
        colors = {
            0: (0, 255, 0),      # car - green
            1: (0, 255, 255),    # truck - yellow
            2: (255, 0, 255),    # bus - magenta
            3: (255, 0, 0),      # person - blue
            4: (0, 128, 255),    # bike - orange
            5: (128, 0, 255),    # motor - purple
            6: (255, 128, 0),    # rider - cyan
            7: (0, 0, 255),      # traffic light - red
            8: (128, 128, 0),    # traffic sign - teal
            9: (255, 255, 0),    # train - cyan
        }
        return colors.get(class_id, (255, 255, 255))

    def _ground_intersection(self, u, v):
        pixel = np.array([[u, v]], dtype=np.float32)
        undistorted = cv2.undistortPoints(
            pixel, self.K, self.dist_coeffs, P=self.K
        )
        u_u, v_u = undistorted[0][0]
        ray_cam = np.array([
            (u_u - self.cx) / self.fx,
            (v_u - self.cy) / self.fy,
            1.0,
        ])
        dx, dy, dz = self.R @ ray_cam
        if dy <= 1e-6:
            return None
        scale = self.camera_height / dy
        return np.array([scale * dx, scale * dz], dtype=np.float64)

    def compute_world_coordinates(self, u, v):
        """
        Compute 3D world coordinates (X, Y, Z) from 2D pixel coordinates (u, v).

        Args:
            u: X pixel coordinate.
            v: Y pixel coordinate.

        Returns:
            Dictionary containing X, Y, Z, distance, and angle if valid, else None.
        """
        point = self._ground_intersection(u, v)
        if point is None:
            return None
        X, Z = point

        theta = np.arctan2(X, Z)
        distance = np.sqrt(X**2 + Z**2)

        samples = (
            self._ground_intersection(u + 1.0, v),
            self._ground_intersection(u - 1.0, v),
            self._ground_intersection(u, v + 1.0),
            self._ground_intersection(u, v - 1.0),
        )
        if any(sample is None for sample in samples):
            pixel_uncertainty_m = float("inf")
        else:
            du = (samples[0] - samples[1]) / 2.0
            dv = (samples[2] - samples[3]) / 2.0
            pixel_uncertainty_m = self.localization_pixel_sigma * math.sqrt(
                float(np.dot(du, du) + np.dot(dv, dv))
            )
        calibration_uncertainty_m = self.calibration_uncertainty_m
        if (
            math.isfinite(pixel_uncertainty_m)
            and math.isfinite(calibration_uncertainty_m)
            and calibration_uncertainty_m >= 0.0
        ):
            uncertainty_meters = math.hypot(
                pixel_uncertainty_m, calibration_uncertainty_m
            )
        else:
            uncertainty_meters = float("inf")

        return {
            "X": float(X),
            "Y": 0.0,
            "Z": float(Z),
            "theta_rad": float(theta),
            "theta_deg": float(np.degrees(theta)),
            "distance": float(distance),
            "uncertainty_meters": (
                float(uncertainty_meters)
                if math.isfinite(uncertainty_meters) else None
            ),
            "uncertainty_components": {
                "pixel_sigma": float(self.localization_pixel_sigma),
                "pixel_meters": (
                    float(pixel_uncertainty_m)
                    if math.isfinite(pixel_uncertainty_m) else None
                ),
                "calibration_meters": (
                    float(calibration_uncertainty_m)
                    if math.isfinite(calibration_uncertainty_m) else None
                ),
            },
        }

    def compute_3d_detections(self, detections_2d, current_utc_str=None, current_epoch=None):
        """
        Convert 2D detections to V2X-schema dicts with 3D world coordinates.

        Args:
            detections_2d: List of 2D detection dictionaries.
            current_utc_str: Current timestamp in UTC string format.
            current_epoch: Current time in epoch seconds.

        Returns:
            List of 3D detection records formatted for V2X schema.
        """
        records = []
        if current_utc_str is None or current_epoch is None:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            epoch_now = int(time.time())
        else:
            now_utc = current_utc_str
            epoch_now = current_epoch

        for det in detections_2d:
            # Ground-contact pixel: bottom-centre of bbox
            u = det['center']['x']
            v = det['bbox']['y2']
            world = self.compute_world_coordinates(u, v)
            if world is None:
                continue

            # Convert XZ → GPS
            lat, lon = xy_to_gps(
                world['X'],
                world['Z'],
                self.origin_lat,
                self.origin_lon,
                self.heading_deg,
                getattr(self, "map_georeference", None),
            )
            geohash = compute_geohash(lat, lon, precision=5)

            event_id = str(uuid.uuid4())

            record = {
                # --- V2X schema fields ---
                "event_id": event_id,
                "object_id": f"{det['class_name']}_{self.device_id}_{det['track_id']}",
                "object_type": det['class_name'],
                "timestamp_utc": now_utc, # TODO: Take a look here
                "confidence_score": round(det['confidence'], 4),
                "gps_location": {
                    "latitude": round(lat, 8),
                    "longitude": round(lon, 8)
                },
                "geohash": geohash,
                "street_name_normalized": "",
                "global_context": {
                    "city": self.city,
                    "state": self.state,
                    "country": self.country
                },
                "camera_data": {
                    "image_reference_url": "",
                    "svo2_reference_url": "",
                    "bifocal_metadata": {
                        "frame": det['frame'],
                        "bbox": det['bbox'],
                        "pixel_centroid": det['center'],
                        "world_position": world   # X, Y, Z, theta, distance
                    }
                },
                "notes": (f"theta={world['theta_deg']:.1f}deg "
                          f"dist={world['distance']:.1f}m"),
                "device_id": self.device_id,
                "ts_event": f"{now_utc}#{event_id}",
                "expires_at": epoch_now + 86400,   # expire in 24 h
                "ingested_at_epoch": epoch_now,
                "track_id": det.get('track_id')
            }
            pixel_sigma_sq = float(self.localization_pixel_sigma) ** 2
            record["raw_observation"] = {
                "schema": "v2x-raw-detection-observation/v1",
                "native_resolution": (
                    [
                        getattr(self, "image_width", None),
                        getattr(self, "image_height", None),
                    ]
                    if getattr(self, "image_width", None) is not None
                    and getattr(self, "image_height", None) is not None
                    else None
                ),
                "bbox": dict(det['bbox']),
                "ground_contact": {
                    "method": "bbox_bottom_center_diagnostic",
                    "pixel": [float(u), float(v)],
                    "covariance_px2": [
                        [pixel_sigma_sq, 0.0],
                        [0.0, pixel_sigma_sq],
                    ],
                    "reviewed": False,
                },
                "fingerprints": {
                    "cameras_json_sha256": getattr(
                        self, "cameras_json_sha256", None
                    ),
                    "camera_config_sha256": getattr(
                        self, "camera_config_sha256", None
                    ),
                    "detector_model_sha256": getattr(
                        self, "detector_model_sha256", None
                    ),
                },
                "optimizer_contract": {
                    "pixel_observation_is_input": True,
                    "gps_location_is_derived_baseline": True,
                    "acceptance_eligible": False,
                    "reason": "ground_contact_not_reviewed",
                },
            }
            records.append(record)
        return records

    V2X_ENDPOINT = "https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/detections"

    def upload_detection(self, record):
        """
        POST a single V2X record to the API.

        Args:
            record: Dictionary containing the detection record.

        Returns:
            None
        """
        try:
            r = requests.post(self.v2x_endpoint,
                              headers={"content-type": "application/json"},
                              data=json.dumps(record),
                              timeout=5)
            if r.status_code not in (200, 201):
                print(f"  ⚠️  Upload failed ({r.status_code}): {r.text[:120]}")
        except Exception as e:
            print(f"  ❌ Upload error: {e}")

    def upload_batch(self, records):
        """
        POST a list of V2X records to the API in a single request.

        Args:
            records: List of detection record dictionaries.

        Returns:
            True only when every requested item was accepted.
        """
        if not records:
            return True

        # Prepare payload: strip internal non-serializable fields (like embeddings)
        payload = []
        for r in records:
            clean_r = r.copy()
            if 'embedding' in clean_r:
                del clean_r['embedding']
            payload.append(clean_r)

        try:
            # Wrap array in the "items" object as per the API documentation
            r = requests.post(self.v2x_endpoint,
                            headers={"content-type": "application/json"},
                            data=json.dumps({"items": payload}),
                            timeout=5)

            if r.status_code not in (200, 201):
                print(f"  ⚠️  Batch upload failed ({r.status_code}): {r.text[:120]}")
                return False

            try:
                validate_batch_response(r.json(), len(payload))
            except (ValueError, requests.exceptions.JSONDecodeError) as exc:
                print(f"  ⚠️  Batch upload rejected: {exc}")
                return False

            print(f"  ✅ Uploaded batch of {len(records)} detections.")
            return True

        except Exception as e:
            print(f"  ❌ Batch upload error: {e}")
            return False

    def upload_all(self):
        """
        Upload all accumulated detections to the V2X API.

        Args:
            None

        Returns:
            None
        """
        print(f"\nUploading {len(self.all_detections_3d)} detections to V2X API...")
        for i, det in enumerate(self.all_detections_3d):
            self.upload_detection(det)
            if (i + 1) % 20 == 0:
                print(f"  Uploaded {i + 1}/{len(self.all_detections_3d)}")
        print("✅ Upload complete")

    def draw_detections_3d(self, frame, detections_3d):
        """
        Draw 3D bounding boxes, metadata, and labels on a video frame.

        Args:
            frame: The input video frame as a NumPy array.
            detections_3d: List of 3D detection records.

        Returns:
            Annotated image as a NumPy array.
        """
        annotated = frame.copy()
        for det in detections_3d:
            x1, y1 = int(det['camera_data']['bifocal_metadata']['bbox']['x1']), \
                     int(det['camera_data']['bifocal_metadata']['bbox']['y1'])
            x2, y2 = int(det['camera_data']['bifocal_metadata']['bbox']['x2']), \
                     int(det['camera_data']['bifocal_metadata']['bbox']['y2'])
            world = det['camera_data']['bifocal_metadata']['world_position']
            cls_id = next((k for k, v in self.class_names.items()
                           if v == det['object_type']), 0)
            color = self.get_class_color(cls_id)

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.circle(annotated, (int((x1 + x2) / 2), y2), 5, color, -1)

            lines = [
                f"{det['object_type']} {det['confidence_score']:.2f}",
                f"GPS: ({det['gps_location']['latitude']:.5f}, {det['gps_location']['longitude']:.5f})",
                f"Angle: {world['theta_deg']:.1f}°  Dist: {world['distance']:.1f}m"
            ]
            y_off = y1 - 10
            for i, txt in enumerate(lines):
                (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                yp = y_off - (len(lines) - i - 1) * (th + 5)
                cv2.rectangle(annotated, (x1, yp - th - 4), (x1 + tw + 4, yp + 2), color, -1)
                cv2.putText(annotated, txt, (x1 + 2, yp - 1),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

        cv2.putText(annotated, f"Detections: {len(detections_3d)}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        return annotated

if __name__ == "__main__":
    model_path = os.getenv("V2X_PERCEPTION_MODEL_PATH", "yolov8n.pt")
    conf = env_float("V2X_PERCEPTION_CONFIDENCE", 0.5)

    cameras_config = load_cameras_config()
    detectors = []
    if cameras_config:
        cameras_json_sha256, camera_hashes = camera_config_fingerprints(
            cameras_config
        )
        detector_model_sha256 = sha256_file(model_path)
        site = cameras_config.get("site", {})
        for cam in cameras_config["cameras"]:
            pixel_sigma, calibration_uncertainty_m = (
                camera_localization_parameters(cam)
            )
            distortion = camera_intrinsics_evidence(
                cam,
                evidence_root=Path(
                    os.getenv("V2X_CAMERAS_JSON") or str(DEFAULT_CAMERAS_JSON)
                ).expanduser().resolve().parent,
                require_artifacts=True,
            )
            intr = cam["intrinsics"]
            K = np.array([
                [intr["fx"], 0, intr["cx"]],
                [0, intr["fy"], intr["cy"]],
                [0, 0, 1]
            ], dtype=np.float64)
            detectors.append(VideoObjectDetector(
                model_path, conf, K, distortion,
                cam["height_m"], cam["pitch_deg"], cam["yaw_deg"], cam["heading_deg"],
                cam["device_id"],
                site.get("lat", 0.0), site.get("lon", 0.0),
                site.get("city", ""), site.get("state", ""), site.get("country", ""),
                localization_pixel_sigma=pixel_sigma,
                calibration_uncertainty_m=calibration_uncertainty_m,
                image_width=intr["width"],
                image_height=intr["height"],
                cameras_json_sha256=cameras_json_sha256,
                camera_config_sha256=camera_hashes[cam["id"]],
                detector_model_sha256=detector_model_sha256,
                map_georeference=site.get("map_georeference"),
            ))
    else:
        # Explicit development-only legacy fallback. It is deliberately
        # incompatible with uploads and never acceptance eligible.
        if env_bool("V2X_PERCEPTION_UPLOAD", False):
            raise RuntimeError("legacy camera fallback cannot upload detections")
        K = np.array([
            [1325.4,      0, 1280.0],  # fx=1325.4, cx=1280
            [     0, 1325.4,  960.0],  # fy=1325.4, cy=960
            [     0,      0,      1]
        ], dtype=np.float64)

        base_lat = 37.91560117034595
        base_lon = -122.33478756387032

        detectors = [
            VideoObjectDetector(model_path, conf, K, None, 7.0, -39.20, -46.06, 200.0, "cam-001-ch1", base_lat, base_lon, "Richmond", "CA", "USA"),
            VideoObjectDetector(model_path, conf, K, None, 7.0, -40.52, 71.25, 300.0, "cam-001-ch2", base_lat, base_lon, "Richmond", "CA", "USA"),
            VideoObjectDetector(model_path, conf, K, None, 7.0, -30.42, 14.58, 315.0, "cam-001-ch3", base_lat, base_lon, "Richmond", "CA", "USA"),
            VideoObjectDetector(model_path, conf, K, None, 7.0, -43.48, -22.63, 260.0, "cam-001-ch4", base_lat, base_lon, "Richmond", "CA", "USA"),
        ]

    pipeline = MultiCameraPipeline(detectors=detectors)

    video_paths = parse_video_paths()
    camera_ids = parse_camera_ids(video_paths)
    upload = env_bool("V2X_PERCEPTION_UPLOAD", False)
    show_live = env_bool("V2X_PERCEPTION_SHOW_LIVE", False)
    output_json = env_optional("V2X_PERCEPTION_OUTPUT_JSON")
    output_video = env_optional("V2X_PERCEPTION_OUTPUT_VIDEO")
    output_image = env_optional("V2X_PERCEPTION_OUTPUT_IMAGE")
    output_validate = env_bool("V2X_PERCEPTION_OUTPUT_VALIDATE", False)
    upload_min_interval_sec = env_float("V2X_PERCEPTION_UPLOAD_MIN_INTERVAL_SEC", 1.0)
    stream_port = env_optional("V2X_PERCEPTION_STREAM_PORT")
    stream_host = os.getenv("V2X_PERCEPTION_STREAM_HOST", "0.0.0.0")
    stream_server = None
    stream_broadcaster = None

    if stream_port:
        stream_broadcaster = FrameBroadcaster(
            camera_ids,
            jpeg_quality=env_float("V2X_PERCEPTION_JPEG_QUALITY", 80),
            stale_seconds=env_float("V2X_PERCEPTION_STALE_SECONDS", 15.0),
        )
        stream_server = PerceptionHttpServer(stream_host, int(stream_port), stream_broadcaster)
        stream_server.start()

    try:
        pipeline.process_streams(
            video_paths=video_paths,
            show_live=show_live,
            upload=upload,
            output_json=output_json,
            output_video=output_video,
            output_image=output_image,
            output_validate=output_validate,
            stream_broadcaster=stream_broadcaster,
            camera_ids=camera_ids,
            upload_min_interval_sec=upload_min_interval_sec,
        )
    finally:
        if stream_server:
            stream_server.stop()

    # Or upload all at once after processing:
    # detector.upload_all()

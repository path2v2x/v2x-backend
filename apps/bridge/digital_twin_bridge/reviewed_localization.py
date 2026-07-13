"""Versioned, fail-closed reviewed vehicle localization contract.

The live producer's GPS and bbox-bottom-centre projection remain diagnostics.
This module validates only independently reviewed, hash-bound CARLA-world actor
placements.  It intentionally has no CARLA or NumPy dependency so the same
contract can be exercised by offline tools and bridge unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Optional


SCHEMA = "v2x-reviewed-vehicle-localization/v1"
TRAJECTORY_SCHEMA = "v2x-reviewed-vehicle-trajectory/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VEHICLE_FAMILIES = {
    "car": "passenger_car",
    "truck": "truck",
    "bus": "bus",
}


class ReviewedLocalizationError(ValueError):
    """A stable rejection reason for a reviewed localization contract."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CameraPlacementContext:
    camera_config_sha256: str
    intrinsics_artifact_sha256: str


@dataclass(frozen=True)
class ReviewedPlacementContext:
    map_name: str
    opendrive_sha256: str
    cameras_json_sha256: str
    cameras: Mapping[str, CameraPlacementContext]


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewedLocalizationError("contract_not_canonical_json") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_object_sha256(value: Any) -> str:
    """Match the producer's per-camera canonical object fingerprint."""
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewedLocalizationError("object_not_canonical_json") from exc
    return sha256_bytes(payload)


def contract_sha256(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("contract_sha256", None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def seal_contract(value: Mapping[str, Any]) -> dict:
    sealed = dict(value)
    sealed.pop("contract_sha256", None)
    sealed["contract_sha256"] = contract_sha256(sealed)
    return sealed


def placement_key_sha256(global_track_id: str, blueprint_family: str) -> str:
    payload = f"{global_track_id}\0{blueprint_family}".encode("utf-8")
    return sha256_bytes(payload)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReviewedLocalizationError(reason)
    return value


def _object(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReviewedLocalizationError(reason)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], reason: str) -> None:
    if set(value) != expected:
        raise ReviewedLocalizationError(reason)


def _text(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewedLocalizationError(reason)
    return value


def _vector(value: Any, size: int, reason: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not _finite(item) for item in value)
    ):
        raise ReviewedLocalizationError(reason)
    return [float(item) for item in value]


def _matrix(value: Any, size: int, reason: str) -> list[list[float]]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not isinstance(row, list) or len(row) != size for row in value)
        or any(not _finite(item) for row in value for item in row)
    ):
        raise ReviewedLocalizationError(reason)
    matrix = [[float(item) for item in row] for row in value]
    for row in range(size):
        for column in range(size):
            if abs(matrix[row][column] - matrix[column][row]) > 1e-9:
                raise ReviewedLocalizationError(reason)
    tolerance = 1e-9
    if any(matrix[index][index] < -tolerance for index in range(size)):
        raise ReviewedLocalizationError(reason)
    for left in range(size):
        for right in range(left + 1, size):
            minor = (
                matrix[left][left] * matrix[right][right]
                - matrix[left][right] * matrix[right][left]
            )
            if minor < -tolerance:
                raise ReviewedLocalizationError(reason)
    if size == 3:
        a, b, c = matrix[0]
        d, e, f = matrix[1]
        g, h, i = matrix[2]
        determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
        if determinant < -tolerance:
            raise ReviewedLocalizationError(reason)
    return matrix


def _utc(value: Any, reason: str) -> tuple[str, float]:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReviewedLocalizationError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReviewedLocalizationError(reason) from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReviewedLocalizationError(reason)
    return value, parsed.timestamp()


def _camera_id_for_detection(detection: Mapping[str, Any]) -> Optional[str]:
    camera_id = detection.get("camera_id")
    if isinstance(camera_id, str) and camera_id:
        return camera_id
    device_id = detection.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        return None
    return device_id.rsplit("-", 1)[-1]


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def build_runtime_context(carla_map, cameras_json_path: str) -> ReviewedPlacementContext:
    """Freeze the exact active map and camera inputs for strict validation."""

    path = Path(cameras_json_path).expanduser()
    try:
        path = path.resolve(strict=True)
        cameras_raw = path.read_bytes()
        config = json.loads(cameras_raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewedLocalizationError("active_camera_config_unavailable") from exc
    cameras = config.get("cameras") if isinstance(config, dict) else None
    if not isinstance(cameras, list) or not cameras:
        raise ReviewedLocalizationError("active_camera_config_invalid")
    indexed: dict[str, CameraPlacementContext] = {}
    for camera in cameras:
        if not isinstance(camera, dict):
            raise ReviewedLocalizationError("active_camera_config_invalid")
        camera_id = _text(camera.get("id"), "active_camera_id_invalid")
        if camera_id in indexed:
            raise ReviewedLocalizationError("active_camera_id_duplicate")
        calibration = _object(
            camera.get("intrinsics_calibration"),
            "active_intrinsics_missing",
        )
        artifact_hash = _sha(
            calibration.get("artifact_sha256"),
            "active_intrinsics_hash_invalid",
        )
        artifact_value = calibration.get("artifact_path")
        if not isinstance(artifact_value, str) or not artifact_value.strip():
            raise ReviewedLocalizationError("active_intrinsics_artifact_missing")
        artifact_path = Path(artifact_value).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = path.parent / artifact_path
        try:
            artifact_raw = artifact_path.resolve(strict=True).read_bytes()
        except OSError as exc:
            raise ReviewedLocalizationError(
                "active_intrinsics_artifact_unavailable"
            ) from exc
        if sha256_bytes(artifact_raw) != artifact_hash:
            raise ReviewedLocalizationError("active_intrinsics_artifact_mismatch")
        indexed[camera_id] = CameraPlacementContext(
            camera_config_sha256=canonical_object_sha256(camera),
            intrinsics_artifact_sha256=artifact_hash,
        )
    try:
        opendrive = carla_map.to_opendrive()
    except Exception as exc:
        raise ReviewedLocalizationError("active_opendrive_unavailable") from exc
    if not isinstance(opendrive, str) or not opendrive:
        raise ReviewedLocalizationError("active_opendrive_unavailable")
    return ReviewedPlacementContext(
        map_name=_text(getattr(carla_map, "name", None), "active_map_name_invalid"),
        opendrive_sha256=sha256_bytes(opendrive.encode("utf-8")),
        cameras_json_sha256=sha256_bytes(cameras_raw),
        cameras=indexed,
    )


def validate_contract(
    contract: Any,
    detection: Mapping[str, Any],
    context: ReviewedPlacementContext,
) -> dict:
    """Validate and normalize one embedded reviewed localization sample."""

    value = _object(contract, "reviewed_localization_missing")
    _exact_keys(value, {
        "schema", "event_id", "camera_id", "global_track_id",
        "trajectory_id", "sample_index", "source", "contact", "timing",
        "review", "identity", "placement", "contract_sha256",
    }, "contract_fields_invalid")
    if value.get("schema") != SCHEMA:
        raise ReviewedLocalizationError("reviewed_localization_schema")
    supplied_hash = _sha(value.get("contract_sha256"), "contract_hash_missing")
    if supplied_hash != contract_sha256(value):
        raise ReviewedLocalizationError("contract_hash_mismatch")

    event_id = _text(value.get("event_id"), "contract_event_id_missing")
    if event_id != detection.get("event_id"):
        raise ReviewedLocalizationError("contract_event_id_mismatch")
    camera_id = _text(value.get("camera_id"), "contract_camera_id_missing")
    if camera_id != _camera_id_for_detection(detection):
        raise ReviewedLocalizationError("contract_camera_id_mismatch")
    camera_context = context.cameras.get(camera_id)
    if camera_context is None:
        raise ReviewedLocalizationError("contract_camera_not_active")

    global_track_id = _text(
        value.get("global_track_id"), "contract_global_track_id_missing"
    )
    if global_track_id != detection.get("object_id"):
        raise ReviewedLocalizationError("contract_global_track_id_mismatch")
    trajectory_id = _text(value.get("trajectory_id"), "trajectory_id_missing")
    sample_index = value.get("sample_index")
    if not isinstance(sample_index, int) or isinstance(sample_index, bool) or sample_index < 0:
        raise ReviewedLocalizationError("trajectory_sample_index_invalid")

    source = _object(value.get("source"), "contract_source_missing")
    _exact_keys(source, {"frame", "detector", "camera", "map"}, "source_fields_invalid")
    frame = _object(source.get("frame"), "native_frame_binding_missing")
    _exact_keys(frame, {"sha256", "mask_sha256", "native_resolution", "frame_number"}, "native_frame_fields_invalid")
    frame_sha256 = _sha(frame.get("sha256"), "native_frame_hash_invalid")
    mask_sha256 = _sha(frame.get("mask_sha256"), "native_mask_hash_invalid")
    resolution = frame.get("native_resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in resolution)
    ):
        raise ReviewedLocalizationError("native_frame_resolution_invalid")
    emitted_resolution = _nested(detection, "raw_observation", "native_resolution")
    if emitted_resolution != resolution:
        raise ReviewedLocalizationError("native_frame_resolution_mismatch")
    frame_number = frame.get("frame_number")
    if not isinstance(frame_number, int) or isinstance(frame_number, bool) or frame_number < 0:
        raise ReviewedLocalizationError("native_frame_number_invalid")
    if frame_number != _nested(detection, "camera_data", "bifocal_metadata", "frame"):
        raise ReviewedLocalizationError("native_frame_number_mismatch")

    detector = _object(source.get("detector"), "detector_binding_missing")
    _exact_keys(detector, {"model_sha256", "config_sha256"}, "detector_fields_invalid")
    detector_model_sha256 = _sha(
        detector.get("model_sha256"), "detector_model_hash_invalid"
    )
    detector_config_sha256 = _sha(
        detector.get("config_sha256"), "detector_config_hash_invalid"
    )
    emitted_fingerprints = _nested(detection, "raw_observation", "fingerprints")
    if not isinstance(emitted_fingerprints, dict):
        raise ReviewedLocalizationError("emitted_fingerprints_missing")
    if detector_model_sha256 != emitted_fingerprints.get("detector_model_sha256"):
        raise ReviewedLocalizationError("detector_model_hash_mismatch")
    if detector_config_sha256 != emitted_fingerprints.get("detector_config_sha256"):
        raise ReviewedLocalizationError("detector_config_hash_mismatch")

    camera = _object(source.get("camera"), "camera_binding_missing")
    _exact_keys(camera, {"cameras_json_sha256", "camera_config_sha256", "intrinsics_artifact_sha256"}, "camera_fields_invalid")
    cameras_json_sha256 = _sha(
        camera.get("cameras_json_sha256"), "cameras_json_hash_invalid"
    )
    camera_config_sha256 = _sha(
        camera.get("camera_config_sha256"), "camera_config_hash_invalid"
    )
    intrinsics_artifact_sha256 = _sha(
        camera.get("intrinsics_artifact_sha256"),
        "intrinsics_artifact_hash_invalid",
    )
    if cameras_json_sha256 != context.cameras_json_sha256:
        raise ReviewedLocalizationError("active_cameras_json_mismatch")
    if camera_config_sha256 != camera_context.camera_config_sha256:
        raise ReviewedLocalizationError("active_camera_config_mismatch")
    if intrinsics_artifact_sha256 != camera_context.intrinsics_artifact_sha256:
        raise ReviewedLocalizationError("active_intrinsics_mismatch")
    if cameras_json_sha256 != emitted_fingerprints.get("cameras_json_sha256"):
        raise ReviewedLocalizationError("emitted_cameras_json_mismatch")
    if camera_config_sha256 != emitted_fingerprints.get("camera_config_sha256"):
        raise ReviewedLocalizationError("emitted_camera_config_mismatch")

    map_binding = _object(source.get("map"), "map_binding_missing")
    _exact_keys(map_binding, {"name", "opendrive_sha256"}, "map_fields_invalid")
    if map_binding.get("name") != context.map_name:
        raise ReviewedLocalizationError("active_map_name_mismatch")
    opendrive_sha256 = _sha(
        map_binding.get("opendrive_sha256"), "opendrive_hash_invalid"
    )
    if opendrive_sha256 != context.opendrive_sha256:
        raise ReviewedLocalizationError("active_opendrive_mismatch")

    contact = _object(value.get("contact"), "reviewed_contact_missing")
    _exact_keys(contact, {"method", "left_ground_pixel", "right_ground_pixel", "footprint_midpoint_pixel", "covariance_px2"}, "contact_fields_invalid")
    if contact.get("method") != "reviewed_vehicle_footprint_midpoint":
        raise ReviewedLocalizationError("reviewed_contact_method_invalid")
    left_pixel = _vector(contact.get("left_ground_pixel"), 2, "left_contact_invalid")
    right_pixel = _vector(contact.get("right_ground_pixel"), 2, "right_contact_invalid")
    midpoint = _vector(
        contact.get("footprint_midpoint_pixel"), 2, "footprint_midpoint_invalid"
    )
    expected_midpoint = [
        (left_pixel[index] + right_pixel[index]) / 2.0 for index in range(2)
    ]
    if any(abs(midpoint[index] - expected_midpoint[index]) > 1e-6 for index in range(2)):
        raise ReviewedLocalizationError("footprint_midpoint_mismatch")
    if not (0.0 <= midpoint[0] < resolution[0] and 0.0 <= midpoint[1] < resolution[1]):
        raise ReviewedLocalizationError("footprint_midpoint_outside_frame")
    covariance_px2 = _matrix(
        contact.get("covariance_px2"), 2, "contact_covariance_not_psd"
    )

    timing = _object(value.get("timing"), "timing_binding_missing")
    _exact_keys(timing, {"method", "trusted", "session_id", "pts_seconds", "media_timestamp_utc", "timestamp_error_ms"}, "timing_fields_invalid")
    if timing.get("method") != "exact_same_session_pts" or timing.get("trusted") is not True:
        raise ReviewedLocalizationError("timing_not_exact_same_session_pts")
    session_id = _text(timing.get("session_id"), "timing_session_id_missing")
    pts_seconds = timing.get("pts_seconds")
    timestamp_error_ms = timing.get("timestamp_error_ms")
    if not _finite(pts_seconds) or float(pts_seconds) < 0.0:
        raise ReviewedLocalizationError("timing_pts_invalid")
    if not _finite(timestamp_error_ms) or not 0.0 <= float(timestamp_error_ms) <= 1.0:
        raise ReviewedLocalizationError("timing_error_exceeds_exact_gate")
    media_timestamp_utc, media_epoch = _utc(
        timing.get("media_timestamp_utc"), "timing_media_timestamp_invalid"
    )
    detection_media = detection.get("media_timestamp_utc")
    if media_timestamp_utc != detection_media or detection.get("timestamp_utc") != detection_media:
        raise ReviewedLocalizationError("timing_detection_timestamp_mismatch")
    if detection.get("timestamp_schema_version") != 2 or detection.get("media_time_trusted") is not True:
        raise ReviewedLocalizationError("timing_detection_not_trusted_v2")
    media_clock = detection.get("media_clock")
    if (
        not isinstance(media_clock, dict)
        or media_clock.get("schema_version") != 1
        or media_clock.get("source") != "hls_ext_x_program_date_time"
        or media_clock.get("matching_method") != "exact_same_session_pts"
        or media_clock.get("session_id") != session_id
        or not _finite(media_clock.get("position_milliseconds"))
        or abs(float(media_clock["position_milliseconds"]) - float(pts_seconds) * 1000.0) > 1.0
    ):
        raise ReviewedLocalizationError("timing_media_clock_mismatch")

    review = _object(value.get("review"), "review_provenance_missing")
    _exact_keys(review, {"decision", "reviewer", "consensus", "factor_graph"}, "review_fields_invalid")
    if review.get("decision") != "accepted":
        raise ReviewedLocalizationError("review_not_accepted")
    reviewer = _object(review.get("reviewer"), "reviewer_missing")
    _exact_keys(reviewer, {"kind", "id"}, "reviewer_fields_invalid")
    if reviewer.get("kind") != "human":
        raise ReviewedLocalizationError("reviewer_not_human")
    reviewer_id = _text(reviewer.get("id"), "reviewer_id_missing")
    consensus = _object(review.get("consensus"), "consensus_provenance_missing")
    _exact_keys(consensus, {"method", "artifact_sha256", "reviewer_ids"}, "consensus_fields_invalid")
    if consensus.get("method") != "independent_review_consensus":
        raise ReviewedLocalizationError("consensus_method_invalid")
    consensus_sha256 = _sha(
        consensus.get("artifact_sha256"), "consensus_hash_invalid"
    )
    reviewer_ids = consensus.get("reviewer_ids")
    if (
        not isinstance(reviewer_ids, list)
        or len(set(reviewer_ids)) < 2
        or reviewer_id not in reviewer_ids
        or any(not isinstance(item, str) or not item.strip() for item in reviewer_ids)
    ):
        raise ReviewedLocalizationError("consensus_reviewers_invalid")
    factor_graph = _object(review.get("factor_graph"), "factor_graph_provenance_missing")
    _exact_keys(factor_graph, {"artifact_sha256", "acceptance_eligible"}, "factor_graph_fields_invalid")
    factor_graph_sha256 = _sha(
        factor_graph.get("artifact_sha256"), "factor_graph_hash_invalid"
    )
    if factor_graph.get("acceptance_eligible") is not True:
        raise ReviewedLocalizationError("factor_graph_not_acceptance_eligible")

    identity = _object(value.get("identity"), "identity_provenance_missing")
    _exact_keys(identity, {"status", "global_track_id", "trajectory_id", "association_method", "evidence_sha256", "camera_ids"}, "identity_fields_invalid")
    if identity.get("status") != "unambiguous":
        raise ReviewedLocalizationError("identity_ambiguous")
    if identity.get("global_track_id") != global_track_id:
        raise ReviewedLocalizationError("identity_global_track_mismatch")
    if identity.get("trajectory_id") != trajectory_id:
        raise ReviewedLocalizationError("identity_trajectory_mismatch")
    if identity.get("association_method") != "reviewed_multicamera_trajectory":
        raise ReviewedLocalizationError("identity_association_method_invalid")
    identity_evidence_sha256 = _sha(
        identity.get("evidence_sha256"), "identity_evidence_hash_invalid"
    )
    camera_ids = identity.get("camera_ids")
    if (
        not isinstance(camera_ids, list)
        or camera_id not in camera_ids
        or len(camera_ids) != len(set(camera_ids))
        or any(not isinstance(item, str) or not item for item in camera_ids)
    ):
        raise ReviewedLocalizationError("identity_camera_set_invalid")

    placement = _object(value.get("placement"), "world_placement_missing")
    _exact_keys(placement, {"coordinate_frame", "position_semantics", "position_m", "covariance_m2", "uncertainty_m", "heading_deg", "dimensions_m", "blueprint_family"}, "placement_fields_invalid")
    if placement.get("coordinate_frame") != "carla_world":
        raise ReviewedLocalizationError("placement_coordinate_frame_invalid")
    if placement.get("position_semantics") != "ue5_actor_center":
        raise ReviewedLocalizationError("placement_position_semantics_invalid")
    position = _object(placement.get("position_m"), "placement_position_missing")
    _exact_keys(position, {"x", "y", "z"}, "placement_position_fields_invalid")
    if any(not _finite(position.get(axis)) for axis in ("x", "y", "z")):
        raise ReviewedLocalizationError("placement_position_nonfinite")
    covariance_m2 = _matrix(
        placement.get("covariance_m2"), 3, "placement_covariance_not_psd"
    )
    uncertainty_m = placement.get("uncertainty_m")
    if not _finite(uncertainty_m) or not 0.0 <= float(uncertainty_m) <= 2.0:
        raise ReviewedLocalizationError("placement_uncertainty_exceeds_2m")
    if max(math.sqrt(max(0.0, covariance_m2[index][index])) for index in range(3)) > float(uncertainty_m) + 1e-9:
        raise ReviewedLocalizationError("placement_uncertainty_understates_covariance")
    heading_deg = placement.get("heading_deg")
    if not _finite(heading_deg):
        raise ReviewedLocalizationError("placement_heading_invalid")
    dimensions = _object(placement.get("dimensions_m"), "vehicle_dimensions_missing")
    _exact_keys(dimensions, {"length", "width", "height"}, "vehicle_dimension_fields_invalid")
    normalized_dimensions = {}
    for key, upper in (("length", 30.0), ("width", 5.0), ("height", 6.0)):
        item = dimensions.get(key)
        if not _finite(item) or not 0.1 <= float(item) <= upper:
            raise ReviewedLocalizationError("vehicle_dimensions_invalid")
        normalized_dimensions[key] = float(item)
    object_type = str(detection.get("object_type") or "").lower()
    blueprint_family = placement.get("blueprint_family")
    if VEHICLE_FAMILIES.get(object_type) != blueprint_family:
        raise ReviewedLocalizationError("blueprint_family_object_type_mismatch")

    return {
        "schema": SCHEMA,
        "contract_sha256": supplied_hash,
        "event_id": event_id,
        "camera_id": camera_id,
        "global_track_id": global_track_id,
        "trajectory_id": trajectory_id,
        "sample_index": sample_index,
        "media_timestamp_utc": media_timestamp_utc,
        "media_epoch": media_epoch,
        "session_id": session_id,
        "pts_seconds": float(pts_seconds),
        "frame_sha256": frame_sha256,
        "mask_sha256": mask_sha256,
        "detector_model_sha256": detector_model_sha256,
        "detector_config_sha256": detector_config_sha256,
        "cameras_json_sha256": cameras_json_sha256,
        "camera_config_sha256": camera_config_sha256,
        "intrinsics_artifact_sha256": intrinsics_artifact_sha256,
        "map_name": context.map_name,
        "opendrive_sha256": opendrive_sha256,
        "footprint_midpoint_pixel": midpoint,
        "contact_covariance_px2": covariance_px2,
        "consensus_sha256": consensus_sha256,
        "factor_graph_sha256": factor_graph_sha256,
        "identity_evidence_sha256": identity_evidence_sha256,
        "identity_camera_ids": list(camera_ids),
        "position_m": {axis: float(position[axis]) for axis in ("x", "y", "z")},
        "covariance_m2": covariance_m2,
        "uncertainty_m": float(uncertainty_m),
        "heading_deg": float(heading_deg) % 360.0,
        "dimensions_m": normalized_dimensions,
        "blueprint_family": blueprint_family,
        "placement_key_sha256": placement_key_sha256(
            global_track_id, blueprint_family
        ),
    }

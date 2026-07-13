#!/usr/bin/env python3
"""Attach accepted, hash-bound trajectory samples to persisted detections.

This is an offline adapter, not a reviewer or optimizer.  It accepts only
already acceptance-eligible consensus, factor-graph, and identity artifacts and
will not convert the current diagnostic factor-graph report or baseline GPS into
reviewed placement truth.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import uuid

import cv2
import numpy as np


BRIDGE_APP = Path(__file__).resolve().parents[2] / "bridge"
if str(BRIDGE_APP) not in sys.path:
    sys.path.insert(0, str(BRIDGE_APP))

from digital_twin_bridge.reviewed_localization import (  # noqa: E402
    MAX_INDEPENDENT_REFERENCE_ERROR_M,
    MAX_LOCALIZATION_UNCERTAINTY_M,
    MAX_TRANSIT_SECONDS,
    MAX_VEHICLE_ACCELERATION_MPS2,
    MAX_VEHICLE_SPEED_MPS,
    MIN_APPEARANCE_SIMILARITY,
    CameraPlacementContext,
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    SCHEMA,
    SHA256_RE,
    TRAJECTORY_SCHEMA,
    canonical_json_bytes,
    canonical_object_sha256,
    load_authority_keys,
    placement_key_sha256,
    seal_contract,
    sha256_bytes,
    validate_measured_intrinsics,
    validate_static_calibration,
    validate_contract,
)


ATTACHMENT_SCHEMA = "v2x-reviewed-localization-attachment/v1"
CONSENSUS_SCHEMA = "v2x-reviewed-footprint-consensus/v1"
FACTOR_SCHEMA = "v2x-reviewed-detection-factor-graph/v1"
IDENTITY_SCHEMA = "v2x-reviewed-trajectory-identity/v1"
REFERENCE_SCHEMA = "v2x-independent-vehicle-reference/v1"
BLUEPRINT_CATALOG_SCHEMA = "v2x-reviewed-ue5-blueprint-catalog/v1"


class AttachmentError(RuntimeError):
    pass


def load_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AttachmentError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise AttachmentError(f"{label} is not an object")
    return path, raw, value


def load_bound_json(base, descriptor, label, expected_schema=None):
    if not isinstance(descriptor, dict):
        raise AttachmentError(f"{label} descriptor is missing")
    value = descriptor.get("path")
    if not isinstance(value, str) or not value:
        raise AttachmentError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    path, raw, parsed = load_json(path, label)
    if sha256_bytes(raw) != descriptor.get("sha256"):
        raise AttachmentError(f"{label} hash does not match")
    declared_schema = descriptor.get("schema")
    if expected_schema is not None and declared_schema != expected_schema:
        raise AttachmentError(f"{label} descriptor schema is not allowlisted")
    if declared_schema is not None and parsed.get("schema") != declared_schema:
        raise AttachmentError(f"{label} schema does not match")
    return path, raw, parsed


def load_bound_file(base, descriptor, label):
    if not isinstance(descriptor, dict):
        raise AttachmentError(f"{label} descriptor is missing")
    value = descriptor.get("path")
    if not isinstance(value, str) or not value:
        raise AttachmentError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    try:
        path = path.resolve(strict=True)
        raw = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"{label} is unreadable") from exc
    if sha256_bytes(raw) != descriptor.get("sha256"):
        raise AttachmentError(f"{label} hash does not match")
    return path, raw


def verify_bound_image(raw, resolution, label, *, require_nonempty=False):
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in resolution)
    ):
        raise AttachmentError(f"{label} native resolution is invalid")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or [image.shape[1], image.shape[0]] != resolution:
        raise AttachmentError(f"{label} is not a matching native-resolution image")
    if require_nonempty:
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if int(np.count_nonzero(image)) == 0:
            raise AttachmentError(f"{label} is empty")


def load_detections(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AttachmentError("detection NDJSON is unreadable") from exc
    rows = []
    by_event = {}
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AttachmentError(f"detection line {number} is invalid") from exc
        event_id = value.get("event_id") if isinstance(value, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in by_event:
            raise AttachmentError("detection event IDs are missing or duplicated")
        rows.append(value)
        by_event[event_id] = value
    if not rows:
        raise AttachmentError("detection NDJSON is empty")
    return path, raw, rows, by_event


def _accepted_event_index(artifact, label):
    events = artifact.get("events")
    if artifact.get("acceptance_eligible") is not True or not isinstance(events, list):
        raise AttachmentError(f"{label} is not acceptance eligible")
    output = {}
    for event in events:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in output
            or event.get("accepted") is not True
            or event.get("ambiguity") is not False
        ):
            raise AttachmentError(f"{label} event result is invalid or ambiguous")
        output[event_id] = event
    if not output:
        raise AttachmentError(f"{label} has no accepted event results")
    return output


def _finite_vector(value, size, label):
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise AttachmentError(f"{label} is invalid")
    return [float(item) for item in value]


def _position(value, label):
    if not isinstance(value, dict) or any(
        isinstance(value.get(axis), bool)
        or not isinstance(value.get(axis), (int, float))
        or not math.isfinite(float(value[axis]))
        for axis in ("x", "y", "z")
    ):
        raise AttachmentError(f"{label} is invalid")
    return {axis: float(value[axis]) for axis in ("x", "y", "z")}


def _matrix_psd(value, size, label):
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise AttachmentError(f"{label} is invalid") from exc
    if (
        matrix.shape != (size, size)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix, matrix.T, rtol=0.0, atol=1e-9)
        or float(np.linalg.eigvalsh(matrix).min()) < -1e-9
    ):
        raise AttachmentError(f"{label} is not finite PSD")
    return matrix


def _parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AttachmentError(f"{label} is invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except ValueError as exc:
        raise AttachmentError(f"{label} is invalid") from exc


def _camera_context(cameras_path, cameras_raw, cameras_config):
    cameras = cameras_config.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise AttachmentError("cameras JSON has no camera definitions")
    indexed = {}
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("id"), str):
            raise AttachmentError("cameras JSON contains an invalid camera")
        try:
            artifact_hash, report_hash = validate_measured_intrinsics(
                camera, cameras_path.parent
            )
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"camera measured intrinsics rejected: {exc.reason}"
            ) from exc
        indexed[camera["id"]] = CameraPlacementContext(
            camera_config_sha256=canonical_object_sha256(camera),
            intrinsics_artifact_sha256=artifact_hash,
            intrinsics_report_sha256=report_hash,
            native_resolution=(
                int(camera["intrinsics"]["width"]),
                int(camera["intrinsics"]["height"]),
            ),
        )
    return sha256_bytes(cameras_raw), indexed


def _mask_contact_gate(mask_raw, resolution, contact, detection, label):
    mask = cv2.imdecode(np.frombuffer(mask_raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if mask is None or [mask.shape[1], mask.shape[0]] != resolution:
        raise AttachmentError(f"{label} mask is invalid")
    mask = mask > 0
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        raise AttachmentError(f"{label} mask is empty")
    bbox = detection.get("bbox") or (
        (detection.get("camera_data") or {}).get("bifocal_metadata", {}).get("bbox")
    )
    try:
        detection_box = np.asarray(
            [bbox[key] for key in ("x1", "y1", "x2", "y2")], dtype=float
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AttachmentError(f"{label} detection bbox is invalid") from exc
    mask_box = np.asarray([xs.min(), ys.min(), xs.max() + 1, ys.max() + 1], dtype=float)
    intersection = max(0.0, min(detection_box[2], mask_box[2]) - max(detection_box[0], mask_box[0])) * max(
        0.0, min(detection_box[3], mask_box[3]) - max(detection_box[1], mask_box[1])
    )
    union = (
        (detection_box[2] - detection_box[0]) * (detection_box[3] - detection_box[1])
        + (mask_box[2] - mask_box[0]) * (mask_box[3] - mask_box[1])
        - intersection
    )
    if union <= 0.0 or intersection / union < 0.50:
        raise AttachmentError(f"{label} mask does not match the detection bbox")
    pixels = [
        _finite_vector(contact.get(key), 2, f"{label} {key}")
        for key in ("left_ground_pixel", "right_ground_pixel", "footprint_midpoint_pixel")
    ]
    support = cv2.dilate(mask.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    bottom_tolerance = 24.0 * resolution[1] / 960.0
    for x, y in pixels:
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < resolution[0] and 0 <= yi < resolution[1]) or not support[yi, xi]:
            raise AttachmentError(f"{label} contact is not supported by the instance mask")
        column = np.nonzero(mask[:, max(0, xi - 2):min(resolution[0], xi + 3)])[0]
        if column.size == 0 or float(column.max()) - y > bottom_tolerance or y - float(column.max()) > 3.0:
            raise AttachmentError(f"{label} contact is not on the mask ground boundary")


def _blueprint_catalog(value):
    families = value.get("families") if isinstance(value, dict) else None
    if value.get("acceptance_eligible") is not True or not isinstance(families, dict):
        raise AttachmentError("blueprint catalog is not acceptance eligible")
    expected_families = {"passenger_car", "truck", "bus"}
    if set(families) != expected_families:
        raise AttachmentError("blueprint catalog family set is invalid")
    ids_by_family, entries = {}, {}
    for family, items in families.items():
        if not isinstance(items, list) or not items:
            raise AttachmentError("blueprint catalog family is empty")
        ids = []
        for item in items:
            bp_id = item.get("id") if isinstance(item, dict) else None
            dimensions = item.get("dimensions_m") if isinstance(item, dict) else None
            if not isinstance(bp_id, str) or not bp_id or bp_id in entries:
                raise AttachmentError("blueprint catalog IDs are invalid or duplicated")
            normalized_dimensions = _position(
                {
                    "x": dimensions.get("length") if isinstance(dimensions, dict) else None,
                    "y": dimensions.get("width") if isinstance(dimensions, dict) else None,
                    "z": dimensions.get("height") if isinstance(dimensions, dict) else None,
                },
                "blueprint catalog dimensions",
            )
            normalized_dimensions = {
                "length": normalized_dimensions["x"],
                "width": normalized_dimensions["y"],
                "height": normalized_dimensions["z"],
            }
            if any(value <= 0.0 for value in normalized_dimensions.values()):
                raise AttachmentError("blueprint catalog dimensions are invalid")
            ids.append(bp_id)
            entries[(family, bp_id)] = normalized_dimensions
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise AttachmentError("blueprint catalog pools must be sorted and unique")
        ids_by_family[family] = ids
    return ids_by_family, entries


def attach(detections_path, trajectory_path, output_path, authority_key_file):
    detections_path, detections_raw, rows, by_event = load_detections(
        detections_path
    )
    trajectory_path, trajectory_raw, trajectory = load_json(
        trajectory_path, "reviewed trajectory"
    )
    if (
        trajectory.get("schema") != TRAJECTORY_SCHEMA
        or trajectory.get("acceptance_eligible") is not True
    ):
        raise AttachmentError("reviewed trajectory is not acceptance eligible")
    source = trajectory.get("source")
    if not isinstance(source, dict):
        raise AttachmentError("reviewed trajectory source bindings are missing")
    if source.get("detections_ndjson_sha256") != sha256_bytes(detections_raw):
        raise AttachmentError("trajectory detection source hash does not match")
    base = trajectory_path.parent
    _consensus_path, consensus_raw, consensus = load_bound_json(
        base, source.get("consensus"), "reviewed contact consensus", CONSENSUS_SCHEMA
    )
    _factor_path, factor_raw, factor = load_bound_json(
        base, source.get("factor_graph"), "factor-graph result", FACTOR_SCHEMA
    )
    _identity_path, identity_raw, identity = load_bound_json(
        base, source.get("identity"), "trajectory identity result", IDENTITY_SCHEMA
    )
    _reference_path, reference_raw, reference = load_bound_json(
        base, source.get("independent_reference"), "independent reference", REFERENCE_SCHEMA
    )
    _catalog_path, catalog_raw, catalog = load_bound_json(
        base, source.get("blueprint_catalog"), "UE5 blueprint catalog", BLUEPRINT_CATALOG_SCHEMA
    )
    static_path, static_raw, _static = load_bound_json(
        base,
        source.get("static_calibration"),
        "static calibration manifest",
        "v2x-static-camera-survey-manifest/v1",
    )
    cameras_path, cameras_raw, cameras = load_bound_json(
        base, source.get("cameras_json"), "cameras JSON"
    )
    _opendrive_path, opendrive_raw = load_bound_file(
        base, source.get("opendrive"), "OpenDRIVE map"
    )
    if (
        factor.get("gate_passed") is not True
        or factor.get("acceptance_eligible") is not True
        or (factor.get("optimizer_contract") or {}).get(
            "diagnostic_until_independent_truth"
        ) is not False
    ):
        raise AttachmentError("diagnostic factor-graph output cannot be attached")
    consensus_events = _accepted_event_index(consensus, "reviewed contact consensus")
    factor_events = _accepted_event_index(factor, "factor-graph result")
    identity_events = _accepted_event_index(identity, "trajectory identity result")
    reference_events = _accepted_event_index(reference, "independent reference")
    reviewer = trajectory.get("reviewer")
    reviewer_ids = consensus.get("reviewer_ids")
    authority_key_id = trajectory.get("authority_key_id")
    try:
        authority_keys = load_authority_keys(authority_key_file)
    except ReviewedLocalizationError as exc:
        raise AttachmentError(f"review authority rejected: {exc.reason}") from exc
    authority_key = authority_keys.get(authority_key_id)
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") != "human"
        or not isinstance(reviewer.get("id"), str)
        or not isinstance(reviewer_ids, list)
        or any(not isinstance(item, str) or not item for item in reviewer_ids)
        or len(reviewer_ids) != len(set(reviewer_ids))
        or len(set(reviewer_ids)) < 2
        or reviewer["id"] not in reviewer_ids
        or reviewer["id"] != authority_key_id
        or authority_key is None
    ):
        raise AttachmentError("reviewer authority/consensus provenance is incomplete")
    for label, artifact in (
        ("reviewed contact consensus", consensus),
        ("factor-graph result", factor),
        ("trajectory identity result", identity),
        ("independent reference", reference),
        ("UE5 blueprint catalog", catalog),
    ):
        if artifact.get("authority_key_id") != authority_key_id:
            raise AttachmentError(f"{label} is not bound to the review authority")
    global_track_id = trajectory.get("global_track_id")
    trajectory_id = trajectory.get("trajectory_id")
    if (
        identity.get("status") != "unambiguous"
        or identity.get("global_track_id") != global_track_id
        or identity.get("trajectory_id") != trajectory_id
        or identity.get("acceptance_eligible") is not True
        or identity.get("ambiguity_count") != 0
    ):
        raise AttachmentError("trajectory identity is ambiguous or mismatched")
    camera_ids = identity.get("camera_ids")
    if (
        not isinstance(camera_ids, list)
        or not camera_ids
        or any(not isinstance(item, str) or not item for item in camera_ids)
        or len(camera_ids) != len(set(camera_ids))
    ):
        raise AttachmentError("trajectory identity camera set is missing")

    cameras_json_sha256, camera_context = _camera_context(
        cameras_path, cameras_raw, cameras
    )
    opendrive_hash = sha256_bytes(opendrive_raw)
    map_name = source.get("opendrive", {}).get("map_name")
    if not isinstance(map_name, str) or not map_name:
        raise AttachmentError("OpenDRIVE map name is missing")
    camera_hashes = {
        camera_id: value.camera_config_sha256
        for camera_id, value in camera_context.items()
    }
    try:
        static_hash = validate_static_calibration(
            str(static_path),
            cameras_json_sha256,
            camera_hashes,
            map_name,
            opendrive_hash,
        )
    except ReviewedLocalizationError as exc:
        raise AttachmentError(f"static calibration rejected: {exc.reason}") from exc
    if static_hash != sha256_bytes(static_raw):
        raise AttachmentError("static calibration manifest hash drifted")
    catalog_ids, catalog_entries = _blueprint_catalog(catalog)
    runtime_catalog_hash = sha256_bytes(canonical_json_bytes(catalog_ids))
    context = ReviewedPlacementContext(
        map_name=map_name,
        opendrive_sha256=opendrive_hash,
        cameras_json_sha256=cameras_json_sha256,
        cameras=camera_context,
        static_calibration_sha256=static_hash,
        authority_keys=authority_keys,
    )

    samples = trajectory.get("samples")
    if not isinstance(samples, list) or not samples:
        raise AttachmentError("reviewed trajectory has no samples")
    if any(not isinstance(sample, dict) for sample in samples):
        raise AttachmentError("reviewed trajectory sample is invalid")
    sample_indexes = [sample.get("sample_index") for sample in samples]
    if any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in sample_indexes
    ):
        raise AttachmentError("trajectory sample indexes are invalid")
    samples = sorted(samples, key=lambda item: item.get("sample_index", -1))
    if [sample.get("sample_index") for sample in samples] != list(range(len(samples))):
        raise AttachmentError("trajectory sample indexes must be contiguous from zero")
    sample_events = [sample.get("event_id") for sample in samples]
    if len(sample_events) != len(set(sample_events)) or any(
        not isinstance(event_id, str) or event_id not in by_event
        for event_id in sample_events
    ):
        raise AttachmentError("trajectory event IDs are unknown or duplicated")
    sample_camera_values = [sample.get("camera_id") for sample in samples]
    if any(
        not isinstance(camera_id, str) or not camera_id
        for camera_id in sample_camera_values
    ):
        raise AttachmentError("trajectory sample camera IDs are invalid")
    sample_camera_ids = set(sample_camera_values)
    if (
        set(camera_ids) != sample_camera_ids
        or any(camera_id not in camera_context for camera_id in sample_camera_ids)
    ):
        raise AttachmentError("trajectory identity camera set does not exactly match samples")
    denominators = [
        set(consensus_events), set(factor_events), set(identity_events),
        set(reference_events),
    ]
    if any(set(sample_events) != denominator for denominator in denominators):
        raise AttachmentError("semantic artifact event denominators do not match samples")
    pairs = identity.get("pairs")
    if not isinstance(pairs, list):
        raise AttachmentError("identity pair results are missing")
    pair_index = {}
    for pair in pairs:
        key = (
            pair.get("previous_event_id"), pair.get("event_id")
        ) if isinstance(pair, dict) else None
        if (
            key is None
            or any(not isinstance(event_id, str) or not event_id for event_id in key)
            or key in pair_index
        ):
            raise AttachmentError("identity pair results are invalid or duplicated")
        pair_index[key] = pair
    expected_pairs = set(zip(sample_events, sample_events[1:]))
    if set(pair_index) != expected_pairs:
        raise AttachmentError("identity pair evidence is not complete and exact")
    appearance_threshold = identity.get("minimum_appearance_similarity")
    if (
        not isinstance(identity.get("appearance_model_sha256"), str)
        or SHA256_RE.fullmatch(identity["appearance_model_sha256"]) is None
        or not isinstance(appearance_threshold, (int, float))
        or isinstance(appearance_threshold, bool)
        or not math.isfinite(float(appearance_threshold))
        or float(appearance_threshold) < MIN_APPEARANCE_SIMILARITY
        or float(appearance_threshold) > 1.0
    ):
        raise AttachmentError("identity appearance authority is invalid")
    contracts = {}
    previous_epoch = None
    previous_position = None
    previous_speed = 0.0
    previous_event_id = None
    for sample in samples:
        event_id = sample.get("event_id")
        detection = by_event.get(event_id)
        camera_id = sample.get("camera_id")
        if camera_id not in camera_ids or camera_id not in camera_context:
            raise AttachmentError("trajectory sample camera is not identity-bound")
        frame_path, frame_raw = load_bound_file(
            base, sample.get("frame"), f"{event_id} native frame"
        )
        mask_path, mask_raw = load_bound_file(
            base, sample.get("mask"), f"{event_id} native mask"
        )
        resolution = sample.get("native_resolution")
        verify_bound_image(frame_raw, resolution, f"{event_id} native frame")
        verify_bound_image(
            mask_raw,
            resolution,
            f"{event_id} native mask",
            require_nonempty=True,
        )
        frame_hash = sha256_bytes(frame_raw)
        mask_hash = sha256_bytes(mask_raw)
        consensus_event = consensus_events[event_id]
        factor_event = factor_events[event_id]
        identity_event = identity_events[event_id]
        reference_event = reference_events[event_id]
        contact = sample.get("contact")
        if (
            consensus_event.get("camera_id") != camera_id
            or consensus_event.get("frame_sha256") != frame_hash
            or consensus_event.get("mask_sha256") != mask_hash
            or consensus_event.get("contact") != contact
        ):
            raise AttachmentError("reviewed contact result is not linked to the exact frame/mask/sample")
        _mask_contact_gate(mask_raw, resolution, contact, detection, event_id)
        placement = sample.get("placement")
        if (
            factor_event.get("trajectory_id") != trajectory_id
            or factor_event.get("global_track_id") != global_track_id
            or factor_event.get("camera_id") != camera_id
            or factor_event.get("sample_index") != sample.get("sample_index")
            or factor_event.get("placement") != placement
        ):
            raise AttachmentError("factor-graph result is not linked to the exact placement sample")
        if (
            identity_event.get("global_track_id") != global_track_id
            or identity_event.get("trajectory_id") != trajectory_id
            or identity_event.get("camera_id") != camera_id
            or identity_event.get("sample_index") != sample.get("sample_index")
        ):
            raise AttachmentError("identity event result is not linked to the exact sample")
        if (
            reference_event.get("global_track_id") != global_track_id
            or reference_event.get("trajectory_id") != trajectory_id
            or reference_event.get("camera_id") != camera_id
            or reference_event.get("sample_index") != sample.get("sample_index")
        ):
            raise AttachmentError(
                "independent reference is not linked to the exact sample"
            )
        reference_position = _position(
            reference_event.get("position_m"), f"{event_id} independent reference"
        )
        reference_covariance = _matrix_psd(
            reference_event.get("covariance_m2"), 3, f"{event_id} reference covariance"
        )
        reference_uncertainty = reference_event.get("uncertainty_m")
        if (
            reference_event.get("source_kind") not in {
                "independent_rtk", "independent_surveyed_trajectory"
            }
            or not isinstance(reference_uncertainty, (int, float))
            or isinstance(reference_uncertainty, bool)
            or not 0.0 <= float(reference_uncertainty) <= 0.50
            or math.sqrt(max(0.0, float(np.linalg.eigvalsh(reference_covariance).max())))
            > float(reference_uncertainty)
        ):
            raise AttachmentError("independent reference uncertainty/source gate failed")
        position = _position(
            placement.get("position_m") if isinstance(placement, dict) else None,
            f"{event_id} placement position",
        )
        placement_covariance = _matrix_psd(
            placement.get("covariance_m2") if isinstance(placement, dict) else None,
            3,
            f"{event_id} placement covariance",
        )
        uncertainty = placement.get("uncertainty_m") if isinstance(placement, dict) else None
        if (
            not isinstance(uncertainty, (int, float))
            or isinstance(uncertainty, bool)
            or not 0.0 <= float(uncertainty) <= MAX_LOCALIZATION_UNCERTAINTY_M
            or math.sqrt(max(0.0, float(np.linalg.eigvalsh(placement_covariance).max())))
            > float(uncertainty)
        ):
            raise AttachmentError("placement covariance/uncertainty gate failed")
        reference_error = math.sqrt(sum(
            (position[axis] - reference_position[axis]) ** 2
            for axis in ("x", "y", "z")
        ))
        if reference_error > MAX_INDEPENDENT_REFERENCE_ERROR_M:
            raise AttachmentError("placement exceeds independent reference error gate")
        family = placement.get("blueprint_family")
        pool = catalog_ids.get(family)
        if not pool:
            raise AttachmentError("placement blueprint family is not cataloged")
        placement_key = placement_key_sha256(global_track_id, family)
        chosen_id = pool[int.from_bytes(bytes.fromhex(placement_key)[:8], "big") % len(pool)]
        catalog_dimensions = catalog_entries[(family, chosen_id)]
        if placement.get("dimensions_m") != catalog_dimensions:
            raise AttachmentError("reviewed geometry does not match the selected blueprint dimensions")
        dimension_tolerance = trajectory.get("blueprint_dimension_tolerance_m")
        if (
            not isinstance(dimension_tolerance, (int, float))
            or isinstance(dimension_tolerance, bool)
            or not 0.0 <= float(dimension_tolerance) <= 0.25
        ):
            raise AttachmentError("blueprint dimension tolerance is invalid")
        media_epoch = _parse_utc(
            sample.get("timing", {}).get("media_timestamp_utc"),
            f"{event_id} media timestamp",
        )
        transition = None
        if previous_epoch is not None:
            delta = media_epoch - previous_epoch
            if not 0.0 < delta <= MAX_TRANSIT_SECONDS:
                raise AttachmentError("trajectory timestamps are not strictly increasing/plausible")
            distance = math.sqrt(sum(
                (position[axis] - previous_position[axis]) ** 2
                for axis in ("x", "y", "z")
            ))
            speed = distance / delta
            acceleration = (speed - previous_speed) / delta
            pair = pair_index[(previous_event_id, event_id)]
            pair_covariance = _matrix_psd(
                pair.get("trajectory_covariance_m2"), 3, "identity trajectory covariance"
            )
            similarity = pair.get("appearance_similarity")
            declared_transit = pair.get("transit_seconds")
            declared_distance = pair.get("distance_m")
            if (
                pair.get("accepted") is not True
                or pair.get("ambiguity") is not False
                or pair.get("global_track_id") != global_track_id
                or pair.get("trajectory_id") != trajectory_id
                or not isinstance(similarity, (int, float))
                or isinstance(similarity, bool)
                or not isinstance(declared_transit, (int, float))
                or isinstance(declared_transit, bool)
                or not isinstance(declared_distance, (int, float))
                or isinstance(declared_distance, bool)
                or not all(
                    math.isfinite(float(value))
                    for value in (similarity, declared_transit, declared_distance)
                )
                or not 0.0 <= float(similarity) <= 1.0
                or float(declared_transit) <= 0.0
                or float(declared_distance) < 0.0
                or float(similarity) < float(appearance_threshold)
                or speed > MAX_VEHICLE_SPEED_MPS
                or abs(acceleration) > MAX_VEHICLE_ACCELERATION_MPS2
                or math.sqrt(max(0.0, float(np.linalg.eigvalsh(pair_covariance).max())))
                > MAX_LOCALIZATION_UNCERTAINTY_M
                or abs(float(declared_transit) - delta) > 1e-6
                or abs(float(declared_distance) - distance) > 1e-6
            ):
                raise AttachmentError("identity pair appearance/transit/dynamics gate failed")
            transition = {
                "previous_event_id": previous_event_id,
                "accepted": True,
                "ambiguity": False,
                "appearance_similarity": float(similarity),
                "transit_seconds": delta,
                "distance_m": distance,
                "speed_mps": speed,
                "acceleration_mps2": acceleration,
                "trajectory_covariance_m2": pair_covariance.tolist(),
                "pair_evidence_sha256": sha256_bytes(canonical_json_bytes(pair)),
            }
        del frame_path, mask_path
        raw_observation = detection.get("raw_observation")
        fingerprints = raw_observation.get("fingerprints") if isinstance(raw_observation, dict) else None
        if not isinstance(fingerprints, dict):
            raise AttachmentError("detection lacks emitted source fingerprints")
        contract = seal_contract({
            "schema": SCHEMA,
            "event_id": event_id,
            "camera_id": camera_id,
            "global_track_id": global_track_id,
            "trajectory_id": trajectory_id,
            "sample_index": sample.get("sample_index"),
            "source": {
                "frame": {
                    "source_kind": "persisted_native_frame_and_instance_mask",
                    "sha256": frame_hash,
                    "mask_sha256": mask_hash,
                    "native_resolution": resolution,
                    "frame_number": sample.get("frame_number"),
                },
                "detector": {
                    "model_sha256": fingerprints.get("detector_model_sha256"),
                    "config_sha256": fingerprints.get("detector_config_sha256"),
                },
                "camera": {
                    "cameras_json_sha256": cameras_json_sha256,
                    "camera_config_sha256": camera_context[camera_id].camera_config_sha256,
                    "intrinsics_artifact_sha256": camera_context[camera_id].intrinsics_artifact_sha256,
                    "intrinsics_report_sha256": camera_context[camera_id].intrinsics_report_sha256,
                    "static_calibration_sha256": static_hash,
                },
                "map": {"name": map_name, "opendrive_sha256": opendrive_hash},
            },
            "contact": contact,
            "timing": sample.get("timing"),
            "review": {
                "decision": "accepted",
                "reviewer": reviewer,
                "consensus": {
                    "method": "independent_review_consensus",
                    "artifact_sha256": sha256_bytes(consensus_raw),
                    "reviewer_ids": reviewer_ids,
                },
                "factor_graph": {
                    "artifact_sha256": sha256_bytes(factor_raw),
                    "acceptance_eligible": True,
                },
                "independent_reference": {
                    "artifact_sha256": sha256_bytes(reference_raw),
                    "acceptance_eligible": True,
                },
            },
            "identity": {
                "status": "unambiguous",
                "global_track_id": global_track_id,
                "trajectory_id": trajectory_id,
                "association_method": "reviewed_multicamera_trajectory",
                "evidence_sha256": sha256_bytes(identity_raw),
                "camera_ids": camera_ids,
                "transition": transition,
            },
            "placement": {
                **placement,
                "independent_reference": {
                    "position_m": reference_position,
                    "error_m": reference_error,
                },
                "blueprint": {
                    "catalog_sha256": runtime_catalog_hash,
                    "pool_sha256": sha256_bytes(canonical_json_bytes(pool)),
                    "selected_blueprint_id": chosen_id,
                    "expected_dimensions_m": catalog_dimensions,
                    "dimension_tolerance_m": float(dimension_tolerance),
                },
            },
        }, authority_key_id, authority_key)
        try:
            validate_contract(contract, detection, context)
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"{event_id} reviewed contract rejected: {exc.reason}"
            ) from exc
        contracts[event_id] = contract
        previous_epoch = media_epoch
        previous_position = position
        previous_speed = transition["speed_mps"] if transition is not None else 0.0
        previous_event_id = event_id

    updated = []
    for row in rows:
        value = dict(row)
        contract = contracts.get(row["event_id"])
        if contract is not None:
            value["reviewed_localization"] = contract
        updated.append(value)
    body = b"".join(canonical_json_bytes(value) for value in updated)
    output_path = Path(output_path).expanduser().resolve()
    manifest_path = output_path.with_name(output_path.name + ".manifest.json")
    if output_path.exists() or manifest_path.exists():
        raise AttachmentError("output or output manifest already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.parent / f".{output_path.name}.tmp-{uuid.uuid4().hex}"
    temporary_manifest = temporary.with_name(temporary.name + ".manifest.json")
    manifest = {
        "schema": ATTACHMENT_SCHEMA,
        "source_detections": str(detections_path),
        "source_detections_sha256": sha256_bytes(detections_raw),
        "reviewed_trajectory": str(trajectory_path),
        "reviewed_trajectory_sha256": sha256_bytes(trajectory_raw),
        "authority_key_id": authority_key_id,
        "static_calibration_manifest_sha256": static_hash,
        "independent_reference_sha256": sha256_bytes(reference_raw),
        "blueprint_catalog_sha256": sha256_bytes(catalog_raw),
        "output_sha256": sha256_bytes(body),
        "counts": {
            "detections": len(updated),
            "reviewed_localizations": len(contracts),
        },
        "static_camera_calibration_passed": False,
        "deployment_eligible": False,
    }
    try:
        temporary.write_bytes(body)
        temporary_manifest.write_bytes(canonical_json_bytes(manifest))
        os.rename(temporary, output_path)
        os.rename(temporary_manifest, manifest_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        raise
    return output_path, manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detections", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--authority-key-file", required=True)
    args = parser.parse_args(argv)
    try:
        output, manifest = attach(
            args.detections,
            args.trajectory,
            args.output,
            args.authority_key_file,
        )
    except AttachmentError as exc:
        print(f"reviewed localization attachment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "manifest": str(manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

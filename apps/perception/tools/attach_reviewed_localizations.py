#!/usr/bin/env python3
"""Attach accepted, hash-bound trajectory samples to persisted detections.

This is an offline adapter, not a reviewer or optimizer.  It accepts only
already acceptance-eligible consensus, factor-graph, and identity artifacts and
will not convert the current diagnostic factor-graph report or baseline GPS into
reviewed placement truth.
"""

from __future__ import annotations

import argparse
import json
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
    CameraPlacementContext,
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    SCHEMA,
    TRAJECTORY_SCHEMA,
    canonical_json_bytes,
    canonical_object_sha256,
    seal_contract,
    sha256_bytes,
    validate_contract,
)


ATTACHMENT_SCHEMA = "v2x-reviewed-localization-attachment/v1"


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


def load_bound_json(base, descriptor, label):
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
    expected_schema = descriptor.get("schema")
    if expected_schema is not None and parsed.get("schema") != expected_schema:
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


def _accepted_event_ids(artifact, label):
    values = artifact.get("accepted_event_ids")
    if (
        artifact.get("acceptance_eligible") is not True
        or not isinstance(values, list)
        or not values
        or len(values) != len(set(values))
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise AttachmentError(f"{label} is not acceptance eligible")
    return set(values)


def _camera_context(cameras_path, cameras_raw, cameras_config):
    cameras = cameras_config.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise AttachmentError("cameras JSON has no camera definitions")
    indexed = {}
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("id"), str):
            raise AttachmentError("cameras JSON contains an invalid camera")
        calibration = camera.get("intrinsics_calibration")
        artifact_hash = calibration.get("artifact_sha256") if isinstance(calibration, dict) else None
        if not isinstance(artifact_hash, str) or len(artifact_hash) != 64:
            raise AttachmentError("camera lacks a measured intrinsics hash")
        artifact_value = calibration.get("artifact_path")
        if not isinstance(artifact_value, str) or not artifact_value:
            raise AttachmentError("camera lacks a measured intrinsics artifact")
        artifact_path = Path(artifact_value).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = cameras_path.parent / artifact_path
        try:
            artifact_raw = artifact_path.resolve(strict=True).read_bytes()
        except OSError as exc:
            raise AttachmentError("camera intrinsics artifact is unreadable") from exc
        if sha256_bytes(artifact_raw) != artifact_hash:
            raise AttachmentError("camera intrinsics artifact hash does not match")
        indexed[camera["id"]] = CameraPlacementContext(
            camera_config_sha256=canonical_object_sha256(camera),
            intrinsics_artifact_sha256=artifact_hash,
        )
    return sha256_bytes(cameras_raw), indexed


def attach(detections_path, trajectory_path, output_path):
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
        base, source.get("consensus"), "reviewed contact consensus"
    )
    _factor_path, factor_raw, factor = load_bound_json(
        base, source.get("factor_graph"), "factor-graph result"
    )
    _identity_path, identity_raw, identity = load_bound_json(
        base, source.get("identity"), "trajectory identity result"
    )
    cameras_path, cameras_raw, cameras = load_bound_json(
        base, source.get("cameras_json"), "cameras JSON"
    )
    _opendrive_path, opendrive_raw = load_bound_file(
        base, source.get("opendrive"), "OpenDRIVE map"
    )
    if factor.get("gate_passed") is not True:
        raise AttachmentError("factor-graph gate did not pass")
    if factor.get("acceptance_eligible") is not True:
        raise AttachmentError("diagnostic factor-graph output cannot be attached")
    if (factor.get("optimizer_contract") or {}).get(
        "diagnostic_until_independent_truth"
    ) is True:
        raise AttachmentError("diagnostic factor-graph output cannot be attached")
    accepted_sets = [
        _accepted_event_ids(consensus, "reviewed contact consensus"),
        _accepted_event_ids(factor, "factor-graph result"),
        _accepted_event_ids(identity, "trajectory identity result"),
    ]
    reviewer = trajectory.get("reviewer")
    reviewer_ids = consensus.get("reviewer_ids")
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") != "human"
        or not isinstance(reviewer.get("id"), str)
        or not isinstance(reviewer_ids, list)
        or len(set(reviewer_ids)) < 2
        or reviewer["id"] not in reviewer_ids
    ):
        raise AttachmentError("reviewer/consensus provenance is incomplete")
    global_track_id = trajectory.get("global_track_id")
    trajectory_id = trajectory.get("trajectory_id")
    if (
        identity.get("status") != "unambiguous"
        or identity.get("global_track_id") != global_track_id
        or identity.get("trajectory_id") != trajectory_id
    ):
        raise AttachmentError("trajectory identity is ambiguous or mismatched")
    camera_ids = identity.get("camera_ids")
    if not isinstance(camera_ids, list) or not camera_ids:
        raise AttachmentError("trajectory identity camera set is missing")

    cameras_json_sha256, camera_context = _camera_context(
        cameras_path, cameras_raw, cameras
    )
    opendrive_hash = sha256_bytes(opendrive_raw)
    map_name = source.get("opendrive", {}).get("map_name")
    if not isinstance(map_name, str) or not map_name:
        raise AttachmentError("OpenDRIVE map name is missing")
    context = ReviewedPlacementContext(
        map_name=map_name,
        opendrive_sha256=opendrive_hash,
        cameras_json_sha256=cameras_json_sha256,
        cameras=camera_context,
    )

    samples = trajectory.get("samples")
    if not isinstance(samples, list) or not samples:
        raise AttachmentError("reviewed trajectory has no samples")
    sample_events = []
    sample_indexes = []
    contracts = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise AttachmentError("reviewed trajectory sample is invalid")
        event_id = sample.get("event_id")
        detection = by_event.get(event_id)
        if detection is None or event_id in contracts:
            raise AttachmentError("trajectory event is unknown or duplicated")
        if any(event_id not in accepted for accepted in accepted_sets):
            raise AttachmentError("trajectory event lacks accepted source evidence")
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
                    "sha256": sha256_bytes(frame_raw),
                    "mask_sha256": sha256_bytes(mask_raw),
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
                },
                "map": {"name": map_name, "opendrive_sha256": opendrive_hash},
            },
            "contact": sample.get("contact"),
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
            },
            "identity": {
                "status": "unambiguous",
                "global_track_id": global_track_id,
                "trajectory_id": trajectory_id,
                "association_method": "reviewed_multicamera_trajectory",
                "evidence_sha256": sha256_bytes(identity_raw),
                "camera_ids": camera_ids,
            },
            "placement": sample.get("placement"),
        })
        try:
            validate_contract(contract, detection, context)
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"{event_id} reviewed contract rejected: {exc.reason}"
            ) from exc
        contracts[event_id] = contract
        sample_events.append(event_id)
        sample_indexes.append(sample.get("sample_index"))
    if (
        any(not isinstance(index, int) or isinstance(index, bool) for index in sample_indexes)
        or sample_indexes != sorted(sample_indexes)
        or len(sample_indexes) != len(set(sample_indexes))
    ):
        raise AttachmentError("trajectory sample indexes are not strictly ordered")
    if set(sample_events) != set().union(*accepted_sets):
        raise AttachmentError("accepted artifact denominators do not match samples")

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
    args = parser.parse_args(argv)
    try:
        output, manifest = attach(args.detections, args.trajectory, args.output)
    except AttachmentError as exc:
        print(f"reviewed localization attachment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "manifest": str(manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

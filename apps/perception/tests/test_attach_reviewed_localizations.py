import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(BRIDGE))

from attach_reviewed_localizations import AttachmentError, attach  # noqa: E402
from digital_twin_bridge.reviewed_localization import (  # noqa: E402
    canonical_json_bytes,
    canonical_object_sha256,
    sha256_bytes,
)


HASH = {letter: letter * 64 for letter in "abcdef1234567890"}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schema": value.get("schema"),
    }


def build_inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ch1_intrinsics = tmp_path / "ch1-intrinsics.json"
    ch2_intrinsics = tmp_path / "ch2-intrinsics.json"
    ch1_intrinsics.write_text('{"camera":"ch1"}\n')
    ch2_intrinsics.write_text('{"camera":"ch2"}\n')
    cameras = {
        "cameras": [
            {
                "id": "ch1",
                "intrinsics_calibration": {
                    "artifact_path": str(ch1_intrinsics),
                    "artifact_sha256": sha256_bytes(ch1_intrinsics.read_bytes()),
                },
            },
            {
                "id": "ch2",
                "intrinsics_calibration": {
                    "artifact_path": str(ch2_intrinsics),
                    "artifact_sha256": sha256_bytes(ch2_intrinsics.read_bytes()),
                },
            },
        ]
    }
    cameras_path = tmp_path / "cameras.json"
    cameras_path.write_text(json.dumps(cameras, indent=2) + "\n")
    cameras_descriptor = {
        "path": str(cameras_path),
        "sha256": sha256_bytes(cameras_path.read_bytes()),
    }
    cameras_file_hash = sha256_bytes(cameras_path.read_bytes())
    camera_hashes = {
        item["id"]: canonical_object_sha256(item) for item in cameras["cameras"]
    }

    detections = []
    for index, camera_id in enumerate(("ch1", "ch2")):
        timestamp = f"2026-07-13T21:00:0{index}.000Z"
        detections.append({
            "event_id": f"event-{index}",
            "object_id": "global_car_reviewed",
            "object_type": "car",
            "timestamp_utc": timestamp,
            "media_timestamp_utc": timestamp,
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
            "media_clock": {
                "schema_version": 1,
                "source": "hls_ext_x_program_date_time",
                "matching_method": "exact_same_session_pts",
                "session_id": f"session-{camera_id}",
                "position_milliseconds": float(index * 1000),
            },
            "device_id": f"cam-001-{camera_id}",
            "gps_location": {"latitude": 37.0, "longitude": -122.0},
            "bbox": {"x1": 100.0, "y1": 200.0, "x2": 300.0, "y2": 500.0},
            "camera_data": {"bifocal_metadata": {"frame": 10 + index}},
            "raw_observation": {
                "native_resolution": [1280, 960],
                "fingerprints": {
                    "cameras_json_sha256": cameras_file_hash,
                    "camera_config_sha256": camera_hashes[camera_id],
                    "detector_model_sha256": HASH["c"],
                    "detector_config_sha256": HASH["d"],
                },
                "optimizer_contract": {
                    "gps_location_is_derived_baseline": True,
                    "acceptance_eligible": False,
                },
            },
        })
    detections_path = tmp_path / "detections.ndjson"
    detections_body = b"".join(canonical_json_bytes(item) for item in detections)
    detections_path.write_bytes(detections_body)

    event_ids = [item["event_id"] for item in detections]
    consensus_descriptor = write_json(tmp_path / "consensus.json", {
        "schema": "v2x-reviewed-footprint-consensus/v1",
        "acceptance_eligible": True,
        "reviewer_ids": ["reviewer-a", "reviewer-b"],
        "accepted_event_ids": event_ids,
    })
    factor_descriptor = write_json(tmp_path / "factor.json", {
        "schema": "v2x-reviewed-detection-factor-graph/v1",
        "acceptance_eligible": True,
        "gate_passed": True,
        "accepted_event_ids": event_ids,
        "optimizer_contract": {"diagnostic_until_independent_truth": False},
    })
    identity_descriptor = write_json(tmp_path / "identity.json", {
        "schema": "v2x-reviewed-trajectory-identity/v1",
        "acceptance_eligible": True,
        "accepted_event_ids": event_ids,
        "status": "unambiguous",
        "global_track_id": "global_car_reviewed",
        "trajectory_id": "trajectory-1",
        "camera_ids": ["ch1", "ch2"],
    })
    opendrive_path = tmp_path / "map.xodr"
    opendrive_path.write_text("<OpenDRIVE><road id='1'/></OpenDRIVE>\n")
    opendrive_descriptor = {
        "path": str(opendrive_path),
        "sha256": sha256_bytes(opendrive_path.read_bytes()),
        "map_name": "TestMap",
    }

    samples = []
    for index, detection in enumerate(detections):
        camera_id = detection["device_id"].rsplit("-", 1)[-1]
        frame = tmp_path / f"frame-{index}.jpg"
        mask = tmp_path / f"mask-{index}.png"
        image = np.full((960, 1280, 3), 40 + index, dtype=np.uint8)
        mask_image = np.zeros((960, 1280), dtype=np.uint8)
        mask_image[400:720, 450:750] = 255
        assert cv2.imwrite(str(frame), image)
        assert cv2.imwrite(str(mask), mask_image)
        samples.append({
            "event_id": detection["event_id"],
            "camera_id": camera_id,
            "sample_index": index,
            "frame": {"path": str(frame), "sha256": sha256_bytes(frame.read_bytes())},
            "mask": {"path": str(mask), "sha256": sha256_bytes(mask.read_bytes())},
            "native_resolution": [1280, 960],
            "frame_number": 10 + index,
            "contact": {
                "method": "reviewed_vehicle_footprint_midpoint",
                "left_ground_pixel": [500.0 + index, 700.0],
                "right_ground_pixel": [700.0 + index, 700.0],
                "footprint_midpoint_pixel": [600.0 + index, 700.0],
                "covariance_px2": [[4.0, 0.0], [0.0, 9.0]],
            },
            "timing": {
                "method": "exact_same_session_pts",
                "trusted": True,
                "session_id": f"session-{camera_id}",
                "pts_seconds": float(index),
                "media_timestamp_utc": detection["media_timestamp_utc"],
                "timestamp_error_ms": 0.0,
            },
            "placement": {
                "coordinate_frame": "carla_world",
                "position_semantics": "ue5_actor_center",
                "position_m": {"x": 10.0 + index, "y": 20.0, "z": 1.25},
                "covariance_m2": [[0.25, 0.0, 0.0], [0.0, 0.25, 0.0], [0.0, 0.0, 0.04]],
                "uncertainty_m": 0.75,
                "heading_deg": 30.0,
                "dimensions_m": {"length": 4.6, "width": 1.9, "height": 1.5},
                "blueprint_family": "passenger_car",
            },
        })
    trajectory = {
        "schema": "v2x-reviewed-vehicle-trajectory/v1",
        "acceptance_eligible": True,
        "global_track_id": "global_car_reviewed",
        "trajectory_id": "trajectory-1",
        "reviewer": {"kind": "human", "id": "reviewer-a"},
        "source": {
            "detections_ndjson_sha256": sha256_bytes(detections_body),
            "consensus": consensus_descriptor,
            "factor_graph": factor_descriptor,
            "identity": identity_descriptor,
            "cameras_json": cameras_descriptor,
            "opendrive": opendrive_descriptor,
        },
        "samples": samples,
    }
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps(trajectory, indent=2, sort_keys=True) + "\n")
    return detections_path, trajectory_path, trajectory


def test_attaches_two_camera_reviewed_trajectory_without_rewriting_baseline(tmp_path):
    detections, trajectory, _value = build_inputs(tmp_path)
    output = tmp_path / "attached.ndjson"

    output_path, manifest_path = attach(detections, trajectory, output)

    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["gps_location"] == {"latitude": 37.0, "longitude": -122.0}
    assert rows[0]["raw_observation"]["optimizer_contract"]["acceptance_eligible"] is False
    assert rows[0]["reviewed_localization"]["contact"]["footprint_midpoint_pixel"] == [600.0, 700.0]
    assert rows[0]["reviewed_localization"]["contact"]["footprint_midpoint_pixel"] != [200.0, 350.0]
    assert rows[1]["reviewed_localization"]["sample_index"] == 1
    manifest = json.loads(manifest_path.read_text())
    assert manifest["counts"] == {"detections": 2, "reviewed_localizations": 2}
    assert manifest["static_camera_calibration_passed"] is False
    assert manifest["deployment_eligible"] is False


def test_rejects_current_diagnostic_factor_graph(tmp_path):
    detections, trajectory_path, trajectory = build_inputs(tmp_path)
    factor_path = Path(trajectory["source"]["factor_graph"]["path"])
    factor = json.loads(factor_path.read_text())
    factor["acceptance_eligible"] = False
    factor["optimizer_contract"]["diagnostic_until_independent_truth"] = True
    factor_path.write_text(json.dumps(factor))
    trajectory["source"]["factor_graph"]["sha256"] = sha256_bytes(factor_path.read_bytes())
    trajectory_path.write_text(json.dumps(trajectory))
    with pytest.raises(AttachmentError, match="diagnostic factor-graph"):
        attach(detections, trajectory_path, tmp_path / "out.ndjson")


def test_rejects_source_detection_or_mask_tampering(tmp_path):
    detections, trajectory_path, trajectory = build_inputs(tmp_path)
    rows = [json.loads(line) for line in detections.read_text().splitlines()]
    rows[0]["gps_location"]["latitude"] = 38.0
    detections.write_bytes(b"".join(canonical_json_bytes(row) for row in rows))
    with pytest.raises(AttachmentError, match="source hash"):
        attach(detections, trajectory_path, tmp_path / "out-a.ndjson")

    detections, trajectory_path, trajectory = build_inputs(tmp_path / "second")
    mask = Path(trajectory["samples"][0]["mask"]["path"])
    mask.write_bytes(b"spoofed")
    with pytest.raises(AttachmentError, match="native mask hash"):
        attach(detections, trajectory_path, tmp_path / "out-b.ndjson")

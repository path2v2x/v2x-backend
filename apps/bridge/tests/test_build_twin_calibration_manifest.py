"""Fail-closed tests for manual calibration annotation manifests."""

import copy
import hashlib
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from build_twin_calibration_manifest import (  # noqa: E402
    build_deployment_model,
    validate_intrinsics_artifact,
    validate_intrinsics_calibration,
    validate_annotations,
)


def annotation_payload():
    points = []
    for index in range(12):
        points.append({
            "id": f"landmark-{index}",
            "split": "train" if index < 8 else "holdout",
            "provenance": "manually_verified_unique",
            "category": "signal_corner",
            "twin": [100.0 + index * 20.0, 100.0 + index * 10.0],
            "image": [200.0 + index * 40.0, 200.0 + index * 20.0],
        })
    roads = []
    for index in range(5):
        roads.append({
            "id": f"road-{index}",
            "split": "train" if index < 3 else "holdout",
            "provenance": "manually_traced_geometry",
            "category": "curb_edge",
            "twin_polyline": [[100, 500 + index * 10], [1100, 400 + index * 10]],
            "image_polyline": [[200, 1000 + index * 20], [2200, 800 + index * 20]],
        })
    return {
        "camera_id": "ch1",
        "real_frame_sha256": "a" * 64,
        "twin_frame_sha256": "b" * 64,
        "points": points,
        "roads": roads,
    }


def test_accepts_complete_frozen_manual_evidence():
    features = validate_annotations(
        annotation_payload(), "ch1", (2560, 1920), (1280, 960)
    )
    assert len(features) == 17
    assert sum(item["type"] == "point" for item in features) == 12
    assert sum(item["type"] == "polyline" for item in features) == 5
    assert sum(item["split"] == "holdout" for item in features) == 6


def test_deployment_model_reverses_existing_offsets_exactly():
    camera = {
        "pitch_deg": -30.0,
        "yaw_deg": 12.0,
        "heading_deg": 200.0,
        "roll_deg": 1.0,
        "intrinsics": {
            "fx": 640.0,
            "fy": 640.0,
            "cx": 640.0,
            "cy": 480.0,
            "width": 1280,
            "height": 960,
        },
        "twin_pose": {
            "forward_offset_m": 1.5,
            "right_offset_m": -0.4,
            "height_offset_m": 0.7,
            "yaw_offset_deg": 3.0,
        },
    }
    yaw = 200.0 + 12.0 + 3.0 - 90.0
    yaw_radians = math.radians(yaw)
    anchor = [10.0, 20.0, 8.0]
    transform = SimpleNamespace(
        location=SimpleNamespace(
            x=anchor[0] + 1.5 * math.cos(yaw_radians) - (-0.4) * math.sin(yaw_radians),
            y=anchor[1] + 1.5 * math.sin(yaw_radians) + (-0.4) * math.cos(yaw_radians),
            z=anchor[2] + 0.7,
        ),
        rotation=SimpleNamespace(yaw=yaw),
    )
    model = build_deployment_model(camera, transform)
    assert model["anchor_location"] == pytest.approx(anchor)
    assert model["base"]["yaw_deg"] == pytest.approx(122.0)
    assert model["base"]["pitch_deg"] == -30.0
    assert model["base"]["roll_deg"] == 1.0
    assert model["base"]["fov_deg"] == pytest.approx(90.0)
    assert model["lens"]["lens_k"] == 0.0


def measured_camera():
    camera = {
        "intrinsics": {
            "fx": 640.0,
            "fy": 639.5,
            "cx": 638.0,
            "cy": 481.0,
            "width": 1280,
            "height": 960,
        },
        "intrinsics_calibration": {
            "method": "charuco",
            "artifact_sha256": "c" * 64,
            "image_count": 24,
            "source_images_sha256": [
                hashlib.sha256(f"calibration-{index}".encode()).hexdigest()
                for index in range(24)
            ],
            "rms_reprojection_error_px": 0.4,
            "resolution": [1280, 960],
            "camera_matrix": [
                [640.0, 0.0, 638.0],
                [0.0, 639.5, 481.0],
                [0.0, 0.0, 1.0],
            ],
            "distortion": {
                "k1": -0.04,
                "k2": 0.01,
                "p1": 0.001,
                "p2": -0.001,
                "k3": 0.0,
            },
        },
    }
    payload = {
        key: value
        for key, value in camera["intrinsics_calibration"].items()
        if key != "artifact_sha256"
    }
    artifact = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    camera["intrinsics_calibration"]["artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
    return camera


def test_validates_measured_intrinsics_and_distortion_evidence():
    evidence = validate_intrinsics_calibration(measured_camera())
    assert evidence["method"] == "charuco"
    assert evidence["image_count"] == 24
    assert evidence["distortion"]["k1"] == -0.04


def test_artifact_hash_and_contents_are_bound_to_camera_config():
    camera = measured_camera()
    payload = {
        key: value
        for key, value in camera["intrinsics_calibration"].items()
        if key != "artifact_sha256"
    }
    artifact = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    evidence = validate_intrinsics_artifact(camera, artifact)
    assert evidence["artifact_sha256"] == hashlib.sha256(artifact).hexdigest()
    with pytest.raises(ValueError, match="hash does not match"):
        validate_intrinsics_artifact(camera, artifact + b" ")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda camera: camera.pop("intrinsics_calibration"), "lacks measured"),
        (
            lambda camera: camera["intrinsics_calibration"].update(image_count=3),
            "at least 10",
        ),
        (
            lambda camera: camera["intrinsics_calibration"].update(
                rms_reprojection_error_px=4.0
            ),
            "no worse than 2",
        ),
        (
            lambda camera: camera["intrinsics_calibration"]["camera_matrix"][0].__setitem__(0, 700.0),
            "does not match",
        ),
    ],
)
def test_rejects_missing_or_untrusted_intrinsics_evidence(mutate, message):
    camera = measured_camera()
    mutate(camera)
    with pytest.raises(ValueError, match=message):
        validate_intrinsics_calibration(camera)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["points"][0].update(
                provenance="manual_verified_static"
            ),
            "not independently verified",
        ),
        (
            lambda payload: payload["roads"][0].update(
                provenance="matcher_proposal"
            ),
            "not manually traced",
        ),
        (lambda payload: payload["points"].pop(), "8 train and 4 holdout"),
        (
            lambda payload: payload["points"][0].update(twin=[2000, 20]),
            "outside",
        ),
        (
            lambda payload: payload["roads"][0].update(
                id=payload["points"][0]["id"]
            ),
            "unique",
        ),
    ],
)
def test_rejects_unverified_sparse_or_malformed_evidence(mutate, message):
    payload = copy.deepcopy(annotation_payload())
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        validate_annotations(payload, "ch1", (2560, 1920), (1280, 960))

"""Fail-closed tests for manual calibration annotation manifests."""

import copy
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from build_twin_calibration_manifest import validate_annotations  # noqa: E402


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

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.aggregate_twin_calibration_manifests import (
    SiteManifestError,
    aggregate_site_manifests,
)


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def fixture(tmp_path):
    cameras_hash = "c" * 64
    landmarks = []
    for index in range(12):
        landmarks.append({
            "global_landmark_id": f"rfs-landmark-{index:02d}",
            "split": "train" if index < 8 else "holdout",
            "surveyed_world": [float(index), float(index * 2), 0.5],
            "survey_record_sha256": hashlib.sha256(
                f"survey-{index}".encode()
            ).hexdigest(),
        })
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema": "v2x-site-landmark-registry/v1",
        "cameras_file_sha256": cameras_hash,
        "landmarks": landmarks,
    }))
    manifests = []
    for camera_id in CAMERAS:
        path = tmp_path / f"{camera_id}.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "camera_id": camera_id,
            "cameras_file_sha256": cameras_hash,
            "features": [
                {
                    "id": f"{camera_id}-feature-{index}",
                    "type": "point",
                    **copy.deepcopy(landmark),
                }
                for index, landmark in enumerate(landmarks)
            ],
        }))
        manifests.append(path)
    return registry, manifests


def test_four_camera_registry_allows_canonical_cross_camera_reuse(tmp_path):
    registry, manifests = fixture(tmp_path)

    report = aggregate_site_manifests(registry, list(reversed(manifests)))

    assert report["gate_passed"] is True
    assert report["acceptance_eligible"] is False
    assert set(report["manifests"]) == set(CAMERAS)
    assert all(
        landmark["cameras"] == list(CAMERAS)
        for landmark in report["landmarks"].values()
    )
    assert report["site_landmark_registry"]["sha256"] == hashlib.sha256(
        registry.read_bytes()
    ).hexdigest()
    for camera_id, manifest in report["manifests"].items():
        path = tmp_path / f"{camera_id}.json"
        assert manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cross_camera_reuse_cannot_change_the_global_split(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[1].read_text())
    value["features"][0]["split"] = "holdout"
    manifests[1].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="canonical split"):
        aggregate_site_manifests(registry, manifests)


def test_renamed_near_duplicate_landmark_is_rejected_site_wide(tmp_path):
    registry, manifests = fixture(tmp_path)
    registry_value = json.loads(registry.read_text())
    renamed = copy.deepcopy(registry_value["landmarks"][0])
    renamed["global_landmark_id"] = "caller-renamed-landmark"
    renamed["surveyed_world"][0] += 0.01
    renamed["survey_record_sha256"] = "d" * 64
    registry_value["landmarks"].append(renamed)
    registry.write_text(json.dumps(registry_value))
    manifest_value = json.loads(manifests[0].read_text())
    manifest_value["features"][0].update(copy.deepcopy(renamed))
    manifests[0].write_text(json.dumps(manifest_value))

    with pytest.raises(SiteManifestError, match="renamed near-duplicate"):
        aggregate_site_manifests(registry, manifests)


def test_survey_world_or_record_hash_disagreement_is_rejected(tmp_path):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[2].read_text())
    value["features"][3]["surveyed_world"][0] += 1.0
    value["features"][3]["survey_record_sha256"] = "e" * 64
    manifests[2].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="surveyed world identity"):
        aggregate_site_manifests(registry, manifests)


@pytest.mark.parametrize("malformed", [None, [], "not-an-object"])
def test_malformed_manifest_annotation_entries_fail_controlled(tmp_path, malformed):
    registry, manifests = fixture(tmp_path)
    value = json.loads(manifests[0].read_text())
    value["features"].append(malformed)
    manifests[0].write_text(json.dumps(value))

    with pytest.raises(SiteManifestError, match="feature is malformed"):
        aggregate_site_manifests(registry, manifests)


def test_requires_exactly_one_manifest_per_camera(tmp_path):
    registry, manifests = fixture(tmp_path)

    with pytest.raises(SiteManifestError, match="exactly four"):
        aggregate_site_manifests(registry, manifests[:3])

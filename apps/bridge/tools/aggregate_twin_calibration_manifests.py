#!/usr/bin/env python3
"""Bind four camera manifests to one surveyed site-landmark registry."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys


CAMERAS = frozenset({"ch1", "ch2", "ch3", "ch4"})
SPLITS = frozenset({"train", "holdout"})
MIN_DISTINCT_LANDMARK_SEPARATION_M = 0.05
WORLD_IDENTITY_TOLERANCE_M = 1e-6


class SiteManifestError(RuntimeError):
    pass


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _world(value, label):
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(float(component))
            for component in value
        )
    ):
        raise SiteManifestError(f"{label} surveyed world coordinate is invalid")
    return tuple(float(component) for component in value)


def _load(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SiteManifestError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise SiteManifestError(f"{label} must be a JSON object")
    return path, raw, value


def aggregate_site_manifests(registry_path, manifest_paths):
    """Validate and hash-bind exactly one manifest for every site camera."""
    registry_file, registry_raw, registry = _load(
        registry_path, "site landmark registry"
    )
    cameras_file_sha256 = registry.get("cameras_file_sha256")
    entries = registry.get("landmarks")
    if (
        registry.get("schema") != "v2x-site-landmark-registry/v1"
        or not _valid_sha256(cameras_file_sha256)
        or not isinstance(entries, list)
        or not entries
    ):
        raise SiteManifestError("site landmark registry contract is invalid")

    landmark_index = {}
    for entry in entries:
        landmark_id = (
            entry.get("global_landmark_id") if isinstance(entry, dict) else None
        )
        split = entry.get("split") if isinstance(entry, dict) else None
        survey_record_sha256 = (
            entry.get("survey_record_sha256")
            if isinstance(entry, dict)
            else None
        )
        if (
            not isinstance(landmark_id, str)
            or not landmark_id
            or landmark_id.strip() != landmark_id
            or landmark_id in landmark_index
            or split not in SPLITS
            or not _valid_sha256(survey_record_sha256)
        ):
            raise SiteManifestError("site landmark registry entry is malformed")
        landmark_index[landmark_id] = {
            "split": split,
            "surveyed_world": _world(entry.get("surveyed_world"), landmark_id),
            "survey_record_sha256": survey_record_sha256,
        }
    ordered_landmarks = sorted(landmark_index.items())
    for index, (left_id, left) in enumerate(ordered_landmarks):
        for right_id, right in ordered_landmarks[index + 1 :]:
            distance = math.dist(
                left["surveyed_world"], right["surveyed_world"]
            )
            if distance < MIN_DISTINCT_LANDMARK_SEPARATION_M:
                raise SiteManifestError(
                    "distinct landmark IDs are a renamed near-duplicate: "
                    f"{left_id} / {right_id} ({distance:.6f} m)"
                )

    manifest_paths = list(manifest_paths)
    if len(manifest_paths) != len(CAMERAS):
        raise SiteManifestError("aggregation requires exactly four manifests")
    manifests = {}
    occurrences = {landmark_id: [] for landmark_id in landmark_index}
    for manifest_path in manifest_paths:
        path, raw, manifest = _load(manifest_path, "camera manifest")
        camera_id = manifest.get("camera_id")
        features = manifest.get("features")
        if (
            manifest.get("schema_version") != 1
            or camera_id not in CAMERAS
            or camera_id in manifests
            or manifest.get("cameras_file_sha256") != cameras_file_sha256
            or not isinstance(features, list)
        ):
            raise SiteManifestError("camera manifest contract is invalid")
        seen_camera_landmarks = set()
        for feature in features:
            if not isinstance(feature, dict):
                raise SiteManifestError("camera manifest feature is malformed")
            if feature.get("type") != "point":
                continue
            landmark_id = feature.get("global_landmark_id")
            split = feature.get("split")
            survey_record_sha256 = feature.get("survey_record_sha256")
            if (
                not isinstance(landmark_id, str)
                or not landmark_id
                or landmark_id.strip() != landmark_id
                or landmark_id in seen_camera_landmarks
                or landmark_id not in landmark_index
                or split not in SPLITS
                or not _valid_sha256(survey_record_sha256)
            ):
                raise SiteManifestError("camera point landmark identity is malformed")
            seen_camera_landmarks.add(landmark_id)
            canonical = landmark_index[landmark_id]
            surveyed_world = _world(
                feature.get("surveyed_world"),
                f"{camera_id}:{landmark_id}",
            )
            if (
                split != canonical["split"]
                or survey_record_sha256 != canonical["survey_record_sha256"]
                or math.dist(surveyed_world, canonical["surveyed_world"])
                > WORLD_IDENTITY_TOLERANCE_M
            ):
                raise SiteManifestError(
                    f"{camera_id}:{landmark_id} disagrees with canonical "
                    "split or surveyed world identity"
                )
            occurrences[landmark_id].append(camera_id)
        manifests[camera_id] = {
            "path": str(path),
            "sha256": _sha256(raw),
            "point_landmarks": len(seen_camera_landmarks),
        }
    if set(manifests) != CAMERAS:
        raise SiteManifestError("aggregation does not contain all four cameras")
    unused = sorted(
        landmark_id for landmark_id, cameras in occurrences.items() if not cameras
    )
    if unused:
        raise SiteManifestError("registry contains landmarks absent from all manifests")

    return {
        "schema": "v2x-site-calibration-aggregation/v1",
        "gate_passed": True,
        "acceptance_eligible": False,
        "site_landmark_registry": {
            "path": str(registry_file),
            "sha256": _sha256(registry_raw),
            "cameras_file_sha256": cameras_file_sha256,
        },
        "manifests": dict(sorted(manifests.items())),
        "landmarks": {
            landmark_id: {
                "split": landmark_index[landmark_id]["split"],
                "surveyed_world": list(
                    landmark_index[landmark_id]["surveyed_world"]
                ),
                "survey_record_sha256": landmark_index[landmark_id][
                    "survey_record_sha256"
                ],
                "cameras": sorted(cameras),
            }
            for landmark_id, cameras in sorted(occurrences.items())
        },
        "contract": {
            "four_camera_complete": True,
            "global_landmark_split_frozen": True,
            "surveyed_world_identity_consistent": True,
            "renamed_near_duplicates_rejected_below_m": (
                MIN_DISTINCT_LANDMARK_SEPARATION_M
            ),
            "deployment_authorized": False,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output_path = Path(args.output).expanduser().resolve()
    if output_path.exists():
        print("aggregation failed: output already exists", file=sys.stderr)
        return 1
    try:
        report = aggregate_site_manifests(args.registry, args.manifest)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, SiteManifestError) as exc:
        print(f"aggregation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

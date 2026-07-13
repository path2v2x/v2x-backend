import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import propose_dense_vehicle_tracks as tracks  # noqa: E402


def test_anchor_selection_requires_one_stable_tracker_id():
    tracked = [
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]}]},
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [20, 10, 40, 30]}]},
    ]
    anchors = [
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "t1"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "t2"},
    ]
    consensus = {
        ("ch1", "e1"): {"bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9},
        ("ch1", "e2"): {"bbox_xyxy": [20, 10, 40, 30], "frame_sha256": "b", "mask_iou": 0.9},
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target == 7
    assert len(matches) == 2
    assert reasons == []


def test_anchor_tracker_switch_rejects_sequence():
    tracked = [
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]}]},
        {"instances": [{"track_id": 8, "confidence": 0.9, "bbox_xyxy": [20, 10, 40, 30]}]},
    ]
    anchors = [
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "t1"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "t2"},
    ]
    consensus = {
        ("ch1", "e1"): {"bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9},
        ("ch1", "e2"): {"bbox_xyxy": [20, 10, 40, 30], "frame_sha256": "b", "mask_iou": 0.9},
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert len(matches) == 2
    assert reasons == ["anchor_tracker_identity_conflict"]


def test_unmatched_anchor_cannot_be_silently_promoted():
    tracked = [{"instances": [{
        "track_id": 2, "confidence": 0.9, "bbox_xyxy": [80, 80, 100, 100]
    }]}]
    anchors = [{
        "event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "t1"
    }]
    consensus = {
        ("ch1", "e1"): {
            "bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9
        }
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert matches == []
    assert reasons == ["anchor_unmatched:e1", "no_cross_model_anchor_matched"]


def test_retained_track_mask_is_largest_component_inside_bbox():
    mask = np.zeros((100, 100), dtype=np.float32)
    mask[30:70, 30:70] = 1.0
    mask[10:20, 10:20] = 1.0
    mask[75:82, 75:82] = 1.0

    class Data:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.asarray([mask])

    class Masks:
        data = Data()

    class Box:
        cls = np.asarray([2])
        conf = np.asarray([0.95])
        xyxy = np.asarray([[20.0, 20.0, 80.0, 80.0]])

    class Boxes:
        id = np.asarray([7.0])

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([Box()])

    class Result:
        boxes = Boxes()
        masks = Masks()
        names = {2: "car"}

    instances = tracks.model_instances_from_result(
        Result(), np.zeros((100, 100, 3), dtype=np.uint8)
    )

    assert len(instances) == 1
    clean = instances[0]["mask"]
    assert clean[35, 35]
    assert not clean[12, 12]
    assert not clean[77, 77]


def _jpeg(value):
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def write_dense_fixture(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}\n")
    frames = []
    for index, value in enumerate((20, 80, 140)):
        raw = _jpeg(value)
        path = tmp_path / "frames" / f"frame-{index:03d}.jpg"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        frames.append({
            "index": index,
            "path": f"frames/{path.name}",
            "sha256": _sha(raw),
            "byte_count": len(raw),
            "width": 64,
            "height": 48,
            "producer_timestamp_utc": f"2026-07-13T18:09:44.{index * 200:03d}Z",
        })
    report = {
        "schema": tracks.DENSE_SCHEMA,
        "camera_id": "ch1",
        "object_id": "proposal-1",
        "frame_count": len(frames),
        "frames": frames,
        "resolution": [64, 48],
        "acceptance_eligible": False,
        "source_event_report": {
            "path": str(source.resolve()),
            "sha256": _sha(source.read_bytes()),
        },
        "source_events": [{
            "event_id": "event-1",
            "frame_sha256": frames[1]["sha256"],
            "selected_frame_timestamp_utc": frames[1]["producer_timestamp_utc"],
        }],
    }
    path = tmp_path / "capture-report.json"
    path.write_text(json.dumps(report))
    return path, report


def test_dense_report_recomputes_every_frame_binding(tmp_path):
    path, report = write_dense_fixture(tmp_path)

    loaded = tracks.load_dense_report(path)

    assert loaded[2]["object_id"] == "proposal-1"
    assert len(loaded[3]) == 3
    assert loaded[4][0]["frame_index"] == 1

    (tmp_path / report["frames"][2]["path"]).write_bytes(b"tampered")
    with pytest.raises(tracks.DenseTrackError, match="byte identity"):
        tracks.load_dense_report(path)


def test_dense_report_rejects_path_escape(tmp_path):
    path, report = write_dense_fixture(tmp_path)
    report["frames"][0]["path"] = "../outside.jpg"
    path.write_text(json.dumps(report))

    with pytest.raises(tracks.DenseTrackError, match="escapes"):
        tracks.load_dense_report(path)

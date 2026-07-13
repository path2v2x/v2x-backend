import hashlib
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

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
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "2026-07-13T00:00:00.200Z"},
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
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "2026-07-13T00:00:00.200Z"},
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


def test_anchor_selection_rejects_overlapping_plausible_candidates():
    tracked = [{"instances": [
        {"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]},
        {"track_id": 8, "confidence": 0.8, "bbox_xyxy": [10, 10, 30, 30]},
    ]}]
    anchors = [{
        "event_id": "e1",
        "frame_index": 0,
        "frame_sha256": "a",
        "timestamp_utc": "2026-07-13T00:00:00.000Z",
    }]
    consensus = {("ch1", "e1"): {
        "bbox_xyxy": [10, 10, 30, 30],
        "frame_sha256": "a",
        "mask_iou": 0.9,
    }}

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert matches == []
    assert reasons == ["anchor_ambiguous:e1", "no_cross_model_anchor_matched"]


def test_two_event_ids_on_one_source_frame_do_not_count_as_two_anchors():
    tracked = [{"instances": [
        {"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]},
    ]}]
    anchors = [
        {
            "event_id": "e1", "frame_index": 0, "frame_sha256": "a",
            "timestamp_utc": "2026-07-13T00:00:00.000Z",
        },
        {
            "event_id": "e2", "frame_index": 0, "frame_sha256": "a",
            "timestamp_utc": "2026-07-13T00:00:00.000Z",
        },
    ]
    consensus = {
        ("ch1", event_id): {
            "bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9,
        }
        for event_id in ("e1", "e2")
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert len(matches) == 2
    assert reasons == ["duplicate_anchor_source_frame"]


def test_unmatched_anchor_cannot_be_silently_promoted():
    tracked = [{"instances": [{
        "track_id": 2, "confidence": 0.9, "bbox_xyxy": [80, 80, 100, 100]
    }]}]
    anchors = [{
        "event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"
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

    Result.boxes.id = np.asarray([7.5])
    with pytest.raises(tracks.DenseTrackError, match="non-integral"):
        tracks.model_instances_from_result(
            Result(), np.zeros((100, 100, 3), dtype=np.uint8)
        )


def _jpeg(value):
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def write_dense_fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    source = tmp_path / "source.json"
    source_events = [
        {
            "event_id": "event-1",
            "camera_id": "ch1",
            "object_id": "proposal-1",
            "selected_frame_timestamp_utc": frames[0]["producer_timestamp_utc"],
            "frame": {
                "path": str((tmp_path / frames[0]["path"]).resolve()),
                "sha256": frames[0]["sha256"],
            },
        },
        {
            "event_id": "event-2",
            "camera_id": "ch1",
            "object_id": "proposal-1",
            "selected_frame_timestamp_utc": frames[2]["producer_timestamp_utc"],
            "frame": {
                "path": str((tmp_path / frames[2]["path"]).resolve()),
                "sha256": frames[2]["sha256"],
            },
        },
    ]
    source.write_text(json.dumps({
        "schema": "v2x-detection-event-frame-capture/v2",
        "events": source_events,
    }))
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
        "source_events": [
            {
                "event_id": event["event_id"],
                "frame_sha256": event["frame"]["sha256"],
                "selected_frame_timestamp_utc": event[
                    "selected_frame_timestamp_utc"
                ],
            }
            for event in source_events
        ],
    }
    path = tmp_path / "capture-report.json"
    path.write_text(json.dumps(report))
    return path, report


def test_dense_report_recomputes_every_frame_binding(tmp_path):
    path, report = write_dense_fixture(tmp_path)

    loaded = tracks.load_dense_report(path)

    assert loaded[2]["object_id"] == "proposal-1"
    assert len(loaded[3]) == 3
    assert [anchor["frame_index"] for anchor in loaded[4]] == [0, 2]

    (tmp_path / report["frames"][1]["path"]).write_bytes(b"tampered")
    with pytest.raises(tracks.DenseTrackError, match="byte identity"):
        tracks.load_dense_report(path)


def test_dense_report_rejects_path_escape(tmp_path):
    path, report = write_dense_fixture(tmp_path)
    report["frames"][0]["path"] = "../outside.jpg"
    path.write_text(json.dumps(report))

    with pytest.raises(tracks.DenseTrackError, match="escapes"):
        tracks.load_dense_report(path)


def test_dense_report_rejects_unrelated_or_drifted_source_denominator(tmp_path):
    path, report = write_dense_fixture(tmp_path)
    source_path = Path(report["source_event_report"]["path"])
    source_path.write_text("{}\n")
    report["source_event_report"]["sha256"] = _sha(source_path.read_bytes())
    path.write_text(json.dumps(report))
    with pytest.raises(tracks.DenseTrackError, match="invalid or unrelated"):
        tracks.load_dense_report(path)

    path, report = write_dense_fixture(tmp_path / "fresh")
    report["source_events"][0]["event_id"] = "fabricated"
    path.write_text(json.dumps(report))
    with pytest.raises(tracks.DenseTrackError, match="drift"):
        tracks.load_dense_report(path)

    path, _report = write_dense_fixture(tmp_path / "denominator")
    with pytest.raises(tracks.DenseTrackError, match="consensus capture denominator"):
        tracks.load_dense_report(path, expected_source_report_sha256="f" * 64)


def write_consensus_fixture(tmp_path):
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "schema": "v2x-detection-event-frame-capture/v2",
        "events": [{"event_id": "event-1"}],
    }))
    reports = []
    models = []
    for side, shift in (("left", 0), ("right", 1)):
        model = tmp_path / f"{side}.pt"
        model.write_bytes(f"{side}-model".encode())
        models.append(model)
        mask = np.zeros((100, 160), dtype=np.uint8)
        mask[35:80, 40 + shift:120 + shift] = 255
        mask_path = tmp_path / f"{side}.png"
        assert cv2.imwrite(str(mask_path), mask)
        report = {
            "schema": "v2x-segmentation-ground-contact-proposals/v1",
            "acceptance_eligible": False,
            "capture_report": {"path": str(capture), "sha256": _sha(capture.read_bytes())},
            "model": {"path": str(model), "sha256": _sha(model.read_bytes())},
            "events": [{
                "event_id": "event-1",
                "camera_id": "ch1",
                "selected_frame_timestamp_utc": "2026-07-13T00:00:00.000Z",
                "frame": {"encoded_jpeg_sha256": "f" * 64, "width": 160, "height": 100},
                "matched_instance": {"bbox_xyxy": [40 + shift, 35, 120 + shift, 80]},
                "ground_contact_proposal": {
                    "pixel": [80 + shift, 79], "covariance_px2": [[4, 0], [0, 4]],
                },
                "mask": {"path": str(mask_path), "sha256": _sha(mask_path.read_bytes())},
            }],
        }
        report_path = tmp_path / f"{side}.json"
        report_path.write_text(json.dumps(report))
        reports.append(report_path)
    consensus = tracks.rebuild_segmentation_consensus(*reports)
    consensus_path = tmp_path / "consensus.json"
    consensus_path.write_text(json.dumps(consensus))
    return consensus_path, reports, models


def test_consensus_requires_both_exact_retained_model_artifacts(tmp_path):
    consensus, reports, models = write_consensus_fixture(tmp_path)
    selected_hash = _sha(models[0].read_bytes())
    loaded = tracks.load_consensus(consensus, selected_hash)
    assert loaded[4] == "left"

    models[1].unlink()
    with pytest.raises(tracks.DenseTrackError, match="model identity"):
        tracks.load_consensus(consensus, selected_hash)

    models[1].write_bytes(b"right-model")
    value = json.loads(reports[1].read_text())
    value["events"][0]["matched_instance"]["bbox_xyxy"] = [1, 1, 2, 2]
    reports[1].write_text(json.dumps(value))
    with pytest.raises(tracks.DenseTrackError, match="report or model identity"):
        tracks.load_consensus(consensus, selected_hash)


def _valid_track_instance(contact_x=31.5):
    mask = np.zeros((48, 64), dtype=bool)
    mask[12:32, 14:50] = True
    mask[30:39, 17:24] = True
    mask[30:39, 41:48] = True
    bbox = [10.0, 8.0, 54.0, 42.0]
    proposal = tracks.estimate_contact(mask, bbox)
    proposal["pixel"][0] = contact_x
    return {
        "track_id": 1,
        "label": "car",
        "confidence": 0.9,
        "bbox_xyxy": bbox,
        "mask": mask,
        "ground_contact_proposal": proposal,
        "rejection_reasons": [],
    }


def test_window_with_no_valid_contacts_is_never_review_ready(tmp_path, monkeypatch):
    capture, report = write_dense_fixture(tmp_path / "capture")
    source_hash = report["source_event_report"]["sha256"]
    instance = _valid_track_instance()
    instance["ground_contact_proposal"] = None
    instance["rejection_reasons"] = ["mask_has_no_supported_bottom_edge"]
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    consensus = {
        ("ch1", event["event_id"]): {
            "bbox_xyxy": instance["bbox_xyxy"],
            "frame_sha256": event["frame_sha256"],
            "mask_iou": 0.9,
        }
        for event in report["source_events"]
    }
    staged = tmp_path / "staged"
    staged.mkdir()
    row = tracks.process_window(
        capture, consensus, tmp_path / "model.pt", staged, lambda _path: Model(),
        0.2, 0.7, 1280, "cpu",
        expected_source_report_sha256=source_hash,
        model_sha256="a" * 64,
        consensus_sha256="b" * 64,
    )

    assert row["proposal_status"] == "rejected"
    assert row["summary"]["contact_proposal_count"] == 0
    assert "not_every_input_frame_has_a_valid_contact_and_mask" in row[
        "rejection_reasons"
    ]
    assert "mask_has_no_supported_bottom_edge" in row["rejection_reasons"]


def test_temporal_gate_rejects_343px_contact_flip_with_stationary_box_and_mask():
    states = []
    contacts = ([20.0, 40.0], [20.0, 40.0], [363.0, 40.0], [20.0, 40.0])
    for index, contact in enumerate(contacts):
        states.append({
            "frame_index": index,
            "timestamp_epoch": index * 0.2,
            "contact": np.asarray(contact),
            "bbox_center": np.asarray([32.0, 24.0]),
            "mask_centroid": np.asarray([32.0, 24.0]),
            "row": {"rejection_reasons": []},
        })

    diagnostics, reasons, gate = tracks.evaluate_contact_temporal_consistency(
        states, 2560, 1920
    )

    assert max(row["bbox_center_residual_jump_px"] for row in diagnostics) == 343.0
    assert gate["maximum_residual_jump_px"] == 48.0
    assert "contact_bbox_residual_jump_above_gate" in reasons
    assert "contact_mask_residual_acceleration_above_gate" in reasons


def test_hash_sequence_ids_separate_same_basenames_and_artifacts_never_clobber(tmp_path):
    left_root = tmp_path / "left" / "same"
    right_root = tmp_path / "right" / "same"
    left_root.mkdir(parents=True)
    right_root.mkdir(parents=True)
    left_path, left = write_dense_fixture(left_root)
    right_path, right = write_dense_fixture(right_root)
    left_id = tracks.dense_sequence_identity(
        left_path, left_path.read_bytes(), left, "a" * 64, "b" * 64
    )[0]
    right_id = tracks.dense_sequence_identity(
        right_path, right_path.read_bytes(), right, "a" * 64, "b" * 64
    )[0]
    assert left_path.parent.name == right_path.parent.name == "same"
    assert left_id != right_id

    destination = tmp_path / "exclusive.bin"

    def writer(value):
        try:
            tracks.write_bytes_exclusive(destination, value, "test artifact")
            return "written"
        except tracks.DenseTrackError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(writer, (b"left", b"right")))
    assert results == ["test artifact path collision", "written"]
    assert destination.read_bytes() in {b"left", b"right"}


def test_propose_publishes_one_verified_model_snapshot_and_bound_artifact_tree(
    tmp_path, monkeypatch
):
    capture, dense = write_dense_fixture(tmp_path / "capture")
    model = tmp_path / "model.pt"
    model.write_bytes(b"model-bytes")
    consensus_file = tmp_path / "consensus.json"
    consensus_raw = b'{"diagnostic":true}\n'
    consensus_file.write_bytes(consensus_raw)
    instance = _valid_track_instance()
    consensus_index = {
        ("ch1", event["event_id"]): {
            "bbox_xyxy": instance["bbox_xyxy"],
            "frame_sha256": event["frame_sha256"],
            "mask_iou": 0.9,
        }
        for event in dense["source_events"]
    }
    monkeypatch.setattr(
        tracks,
        "load_consensus",
        lambda _path, _hash: (
            consensus_file.resolve(), consensus_raw,
            {"capture_report_sha256": dense["source_event_report"]["sha256"]},
            consensus_index, "left",
        ),
    )
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    tracks.propose(
        [capture], consensus_file, model, output,
        model_factory=lambda _path: Model(), image_size=1280,
    )

    report = json.loads((output / "report.json").read_text())
    sequence = report["sequences"][0]
    assert sequence["proposal_status"] == "ready_for_independent_review"
    assert sequence["summary"]["contact_proposal_count"] == 3
    assert sequence["summary"]["temporal_contact_rejection_pair_count"] == 0
    snapshot = output / report["model"]["execution_snapshot"]["path"]
    assert snapshot.read_bytes() == model.read_bytes()
    for line in (output / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert _sha((output / relative).read_bytes()) == expected

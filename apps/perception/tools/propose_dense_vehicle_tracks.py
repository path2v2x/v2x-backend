#!/usr/bin/env python3
"""Build hash-bound, proposal-only vehicle tracks from dense KVS windows.

The persisted object ID and sparse detection box are hints, never identity or
geometry truth.  A pinned segmentation model is tracked over every retained
frame.  Exact-frame, cross-model segmentation consensus anchors select the
target tracker ID.  Any anchor disagreement, missing frame binding, hash drift,
or tracker-ID switch is retained as a rejection rather than hidden.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from capture_static_kvs_window import (  # noqa: E402
    StaticCaptureError,
    atomic_publish_directory,
)
from build_segmentation_contact_consensus import (  # noqa: E402
    ConsensusError,
    build as rebuild_segmentation_consensus,
)
from propose_segmentation_ground_contacts import (  # noqa: E402
    ContactProposalError,
    draw_overlay,
    estimate_contact,
    has_visibility_margin,
    largest_component,
)
from redetect_selected_capture_frames import (  # noqa: E402
    VEHICLE_LABELS,
    bbox_iou,
    sha256_bytes,
    touches_boundary,
)


SCHEMA = "v2x-dense-vehicle-track-proposals/v1"
DENSE_SCHEMA = "v2x-dense-kvs-window/v1"
CONSENSUS_SCHEMA = "v2x-segmentation-ground-contact-consensus/v2"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}


class DenseTrackError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DenseTrackError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DenseTrackError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def load_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DenseTrackError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise DenseTrackError(f"{label} must be a JSON object")
    return path, raw, value


def canonical_retained_path(report_path, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise DenseTrackError("dense frame path must be non-empty and relative")
    root = report_path.parent.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DenseTrackError("dense frame path escapes its capture directory") from exc
    if not path.is_file():
        raise DenseTrackError("dense frame is missing")
    return path


def load_dense_report(path):
    report_path, raw, report = load_json(path, "dense capture report")
    if report.get("schema") != DENSE_SCHEMA:
        raise DenseTrackError("dense capture schema is unsupported")
    camera_id = report.get("camera_id")
    object_id = report.get("object_id")
    frames = report.get("frames")
    if (
        camera_id not in CAMERAS
        or not isinstance(object_id, str)
        or not object_id.strip()
        or not isinstance(frames, list)
        or len(frames) < 3
        or report.get("frame_count") != len(frames)
        or report.get("acceptance_eligible") is not False
    ):
        raise DenseTrackError("dense capture contract is incomplete")
    source = report.get("source_event_report")
    if not isinstance(source, dict):
        raise DenseTrackError("dense source-event report identity is missing")
    source_path = Path(str(source.get("path", ""))).expanduser().resolve()
    if (
        not source_path.is_file()
        or str(source_path) != source.get("path")
        or sha256_file(source_path) != source.get("sha256")
    ):
        raise DenseTrackError("dense source-event report identity mismatches disk")

    decoded = []
    seen_indices = set()
    seen_hashes = set()
    previous_time = None
    resolution = report.get("resolution")
    for row in frames:
        if not isinstance(row, dict):
            raise DenseTrackError("dense frame descriptor is malformed")
        index = row.get("index")
        expected_hash = row.get("sha256")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index in seen_indices
            or index != len(decoded)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or expected_hash in seen_hashes
        ):
            raise DenseTrackError("dense frame indices or hashes are invalid")
        seen_indices.add(index)
        seen_hashes.add(expected_hash)
        timestamp = parse_utc(row.get("producer_timestamp_utc"), f"frame {index}")
        if previous_time is not None and timestamp <= previous_time:
            raise DenseTrackError("dense frame timestamps are not strictly increasing")
        previous_time = timestamp
        frame_path = canonical_retained_path(report_path, row.get("path"))
        encoded = frame_path.read_bytes()
        if len(encoded) != row.get("byte_count") or sha256_bytes(encoded) != expected_hash:
            raise DenseTrackError("dense frame byte identity mismatches report")
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DenseTrackError("dense frame is not a decodable JPEG")
        height, width = image.shape[:2]
        if [width, height] != resolution or [width, height] != [
            row.get("width"), row.get("height")
        ]:
            raise DenseTrackError("dense frame dimensions mismatch report")
        decoded.append({
            "index": index,
            "path": str(frame_path),
            "sha256": expected_hash,
            "timestamp_utc": row["producer_timestamp_utc"],
            "timestamp_epoch": timestamp.timestamp(),
            "image": image,
            "width": width,
            "height": height,
        })

    source_events = report.get("source_events")
    if not isinstance(source_events, list) or not source_events:
        raise DenseTrackError("dense report has no source event anchors")
    anchors = []
    event_ids = set()
    frame_index = {(row["sha256"], row["timestamp_utc"]): row for row in decoded}
    for event in source_events:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        key = (
            event.get("frame_sha256") if isinstance(event, dict) else None,
            event.get("selected_frame_timestamp_utc") if isinstance(event, dict) else None,
        )
        if not isinstance(event_id, str) or not event_id or event_id in event_ids:
            raise DenseTrackError("source event IDs are invalid or duplicated")
        event_ids.add(event_id)
        if key not in frame_index:
            raise DenseTrackError("source event does not bind an exact dense frame")
        anchors.append({
            "event_id": event_id,
            "frame_sha256": key[0],
            "timestamp_utc": key[1],
            "frame_index": frame_index[key]["index"],
        })
    return report_path, raw, report, decoded, anchors


def load_consensus(path, model_hash):
    consensus_path, raw, value = load_json(path, "segmentation consensus")
    if value.get("schema") != CONSENSUS_SCHEMA or value.get("acceptance_eligible") is not False:
        raise DenseTrackError("segmentation consensus contract is invalid")
    inputs = value.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2:
        raise DenseTrackError("segmentation consensus model bindings are incomplete")
    sides = ["left", "right"]
    selected_side = None
    input_paths = []
    for side, identity in zip(sides, inputs):
        model = identity.get("model") if isinstance(identity, dict) else None
        input_path = Path(str(identity.get("path", ""))).expanduser().resolve() if isinstance(identity, dict) else None
        if (
            not isinstance(model, dict)
            or not input_path
            or not input_path.is_file()
            or str(input_path) != identity.get("path")
            or sha256_file(input_path) != identity.get("sha256")
        ):
            raise DenseTrackError("segmentation consensus input identity mismatches disk")
        input_paths.append(input_path)
        if model.get("sha256") == model_hash:
            selected_side = side
    if selected_side is None:
        raise DenseTrackError("tracking model is not one of the consensus models")
    try:
        recomputed = rebuild_segmentation_consensus(*input_paths)
    except (ConsensusError, OSError, TypeError, ValueError) as exc:
        raise DenseTrackError("segmentation consensus cannot be recomputed") from exc
    for key in (
        "schema", "acceptance_eligible", "inputs", "capture_report_sha256",
        "gate", "events", "summary", "acceptance_failures",
    ):
        if value.get(key) != recomputed.get(key):
            raise DenseTrackError(
                f"segmentation consensus {key} mismatches retained source evidence"
            )
    index = {}
    events = recomputed.get("events")
    if not isinstance(events, list):
        raise DenseTrackError("segmentation consensus event list is missing")
    for event in events:
        if not isinstance(event, dict):
            raise DenseTrackError("segmentation consensus event is malformed")
        event_id = event.get("event_id")
        camera_id = event.get("camera_id")
        if not isinstance(event_id, str) or camera_id not in CAMERAS:
            raise DenseTrackError("segmentation consensus event identity is invalid")
        key = (camera_id, event_id)
        if key in index:
            raise DenseTrackError("segmentation consensus events are duplicated")
        side_value = event.get(selected_side)
        instance = side_value.get("matched_instance") if isinstance(side_value, dict) else None
        frame = event.get("frame")
        if event.get("consensus") is not None and isinstance(instance, dict):
            index[key] = {
                "bbox_xyxy": instance.get("bbox_xyxy"),
                "frame_sha256": frame.get("encoded_jpeg_sha256") if isinstance(frame, dict) else None,
                "consensus_pixel": event["consensus"].get("pixel"),
                "mask_iou": event.get("mask_iou"),
            }
    return consensus_path, raw, value, index, selected_side


def model_instances_from_result(result, image):
    if result.boxes is None or len(result.boxes) == 0:
        return []
    if result.masks is None:
        raise DenseTrackError("tracking model returned boxes without masks")
    ids = result.boxes.id
    masks = result.masks.data.detach().cpu().numpy()
    height, width = image.shape[:2]
    instances = []
    for index, box in enumerate(result.boxes):
        class_id = int(box.cls[0].item())
        label = str(result.names[class_id])
        if label not in VEHICLE_LABELS:
            continue
        if ids is None or not math.isfinite(float(ids[index].item())):
            continue
        mask = masks[index]
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        bbox = [float(value) for value in box.xyxy[0].tolist()]
        x1, y1, x2, y2 = bbox
        ix1, iy1 = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
        ix2, iy2 = min(width, int(math.ceil(x2))), min(height, int(math.ceil(y2)))
        clean_mask = np.zeros((height, width), dtype=bool)
        if ix2 > ix1 and iy2 > iy1:
            clean_mask[iy1:iy2, ix1:ix2] = largest_component(
                (mask[iy1:iy2, ix1:ix2] >= 0.5).astype(np.uint8)
            ).astype(bool)
        proposal = None
        reasons = []
        if touches_boundary(bbox, width, height) or not has_visibility_margin(
            bbox, width, height
        ):
            reasons.append("vehicle_instance_touches_frame_boundary")
        else:
            try:
                proposal = estimate_contact(clean_mask, bbox)
            except ContactProposalError as exc:
                reasons.append(str(exc).replace(" ", "_"))
        instances.append({
            "track_id": int(ids[index].item()),
            "label": label,
            "confidence": float(box.conf[0].item()),
            "bbox_xyxy": bbox,
            "mask": clean_mask,
            "ground_contact_proposal": proposal,
            "rejection_reasons": reasons,
        })
    return sorted(instances, key=lambda item: (item["track_id"], -item["confidence"]))


def select_target_track(tracked_frames, anchors, consensus_index, camera_id,
                        minimum_anchor_iou=0.50):
    matches = []
    reasons = []
    for anchor in anchors:
        truth = consensus_index.get((camera_id, anchor["event_id"]))
        if truth is None:
            continue
        if truth.get("frame_sha256") != anchor["frame_sha256"]:
            raise DenseTrackError("consensus anchor frame hash mismatches dense frame")
        candidates = tracked_frames[anchor["frame_index"]]["instances"]
        scored = sorted(
            ((bbox_iou(truth["bbox_xyxy"], item["bbox_xyxy"]), item) for item in candidates),
            key=lambda value: (value[0], value[1]["confidence"]),
            reverse=True,
        )
        if not scored or scored[0][0] < minimum_anchor_iou:
            reasons.append(f"anchor_unmatched:{anchor['event_id']}")
            continue
        score, instance = scored[0]
        matches.append({
            **anchor,
            "track_id": instance["track_id"],
            "bbox_iou": score,
            "model_bbox_xyxy": instance["bbox_xyxy"],
            "consensus_bbox_xyxy": truth["bbox_xyxy"],
            "consensus_mask_iou": truth["mask_iou"],
        })
    track_ids = sorted({item["track_id"] for item in matches})
    if not matches:
        reasons.append("no_cross_model_anchor_matched")
        target = None
    elif len(track_ids) != 1:
        reasons.append("anchor_tracker_identity_conflict")
        target = None
    else:
        target = track_ids[0]
    return target, matches, reasons


def make_review_sheet(entries, columns=4, tile_width=480, tile_height=390):
    rows = max(1, math.ceil(len(entries) / columns))
    sheet = np.full((rows * tile_height, columns * tile_width, 3), 28, dtype=np.uint8)
    for position, (label, encoded) in enumerate(entries):
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        available_height = tile_height - 30
        scale = min(tile_width / image.shape[1], available_height / image.shape[0])
        resized = cv2.resize(
            image,
            (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        row, column = divmod(position, columns)
        x = column * tile_width + (tile_width - resized.shape[1]) // 2
        y = row * tile_height + 30 + (available_height - resized.shape[0]) // 2
        sheet[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
        cv2.putText(sheet, label, (column * tile_width + 6, row * tile_height + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.50, (240, 240, 240), 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise DenseTrackError("failed to encode dense track review sheet")
    return encoded.tobytes()


def process_window(report_value, consensus_index, model_path, staged_root, model_factory,
                   confidence, iou_threshold, image_size, device):
    report_path, report_raw, report, frames, anchors = load_dense_report(report_value)
    camera_id = report["camera_id"]
    sequence_id = report_path.parent.name
    model = model_factory(str(model_path))
    tracked = []
    for frame in frames:
        result = model.track(
            frame["image"], persist=True, tracker="bytetrack.yaml",
            conf=confidence, iou=iou_threshold, imgsz=image_size, device=device,
            retina_masks=True, verbose=False,
        )[0]
        tracked.append({
            "frame": frame,
            "instances": model_instances_from_result(result, frame["image"]),
        })
    target_id, anchor_matches, rejection_reasons = select_target_track(
        tracked, anchors, consensus_index, camera_id
    )
    selected_rows = []
    review_candidates = []
    if target_id is not None:
        for item in tracked:
            frame = item["frame"]
            candidates = [value for value in item["instances"] if value["track_id"] == target_id]
            if len(candidates) > 1:
                raise DenseTrackError("tracker emitted duplicate target IDs in one frame")
            if not candidates:
                continue
            instance = candidates[0]
            proposal = instance["ground_contact_proposal"]
            mask_descriptor = None
            if proposal is not None:
                mask_relative = Path("masks") / sequence_id / f"frame-{frame['index']:03d}.png"
                mask_path = staged_root / mask_relative
                mask_path.parent.mkdir(parents=True, exist_ok=True)
                ok, encoded = cv2.imencode(
                    ".png", instance["mask"].astype(np.uint8) * 255,
                    [cv2.IMWRITE_PNG_COMPRESSION, 4],
                )
                if not ok:
                    raise DenseTrackError("failed to encode dense target mask")
                mask_path.write_bytes(encoded.tobytes())
                mask_descriptor = {
                    "path": mask_relative.as_posix(),
                    "sha256": sha256_file(mask_path),
                }
                overlay = draw_overlay(
                    frame["image"], instance["bbox_xyxy"], instance["mask"], proposal
                )
                review_candidates.append((frame, overlay))
            selected_rows.append({
                "frame_index": frame["index"],
                "producer_timestamp_utc": frame["timestamp_utc"],
                "frame_sha256": frame["sha256"],
                "track_id": target_id,
                "label": instance["label"],
                "confidence": instance["confidence"],
                "bbox_xyxy": instance["bbox_xyxy"],
                "ground_contact_proposal": proposal,
                "mask": mask_descriptor,
                "rejection_reasons": instance["rejection_reasons"],
                "acceptance_eligible": False,
            })
    coverage = len(selected_rows) / len(frames)
    timestamps = [parse_utc(row["producer_timestamp_utc"], "selected frame").timestamp()
                  for row in selected_rows]
    maximum_gap_ms = max(
        ((right - left) * 1000.0 for left, right in zip(timestamps, timestamps[1:])),
        default=None,
    )
    if target_id is not None and coverage < 0.75:
        rejection_reasons.append("target_track_coverage_below_75_percent")
    if target_id is not None and maximum_gap_ms is not None and maximum_gap_ms > 1000.0:
        rejection_reasons.append("target_track_gap_above_1000ms")
    if target_id is not None and len(anchor_matches) < 2:
        rejection_reasons.append("fewer_than_two_cross_model_anchors")

    review_descriptor = None
    if review_candidates:
        chosen_indices = np.linspace(
            0, len(review_candidates) - 1, min(12, len(review_candidates)), dtype=int
        )
        entries = []
        for index in sorted(set(int(value) for value in chosen_indices)):
            frame, overlay = review_candidates[index]
            entries.append((f"{camera_id} f{frame['index']} {frame['timestamp_utc']}", overlay))
        review_relative = Path("review-sheets") / f"{sequence_id}.jpg"
        review_path = staged_root / review_relative
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_bytes(make_review_sheet(entries))
        review_descriptor = {
            "path": review_relative.as_posix(),
            "sha256": sha256_file(review_path),
        }
    return {
        "sequence_id": sequence_id,
        "camera_id": camera_id,
        "model_object_id": report["object_id"],
        "model_object_id_is_identity_truth": False,
        "capture_report": {"path": str(report_path), "sha256": sha256_bytes(report_raw)},
        "source_event_report": report["source_event_report"],
        "target_track_id": target_id,
        "anchors": anchor_matches,
        "frames": selected_rows,
        "review_sheet": review_descriptor,
        "summary": {
            "input_frame_count": len(frames),
            "tracked_frame_count": len(selected_rows),
            "contact_proposal_count": sum(
                row["ground_contact_proposal"] is not None for row in selected_rows
            ),
            "coverage_fraction": coverage,
            "maximum_tracked_gap_ms": maximum_gap_ms,
            "matched_anchor_count": len(anchor_matches),
        },
        "rejection_reasons": sorted(set(rejection_reasons)),
        "proposal_status": "ready_for_independent_review" if not rejection_reasons else "rejected",
        "acceptance_eligible": False,
    }


def propose(capture_reports, consensus_path, model_path, output_directory, *,
            confidence=0.20, iou_threshold=0.70, image_size=1280, device="cpu",
            model_factory=None):
    if not capture_reports:
        raise DenseTrackError("at least one dense capture report is required")
    if not 0.0 < confidence <= 1.0 or not 0.0 < iou_threshold <= 1.0:
        raise DenseTrackError("confidence and IoU thresholds must be in (0, 1]")
    if image_size < 320 or image_size > 2560 or image_size % 32:
        raise DenseTrackError("image size must be a multiple of 32 in [320, 2560]")
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        raise DenseTrackError("segmentation tracking model is unavailable")
    model_hash = sha256_file(model_path)
    consensus_file, consensus_raw, _value, consensus_index, side = load_consensus(
        consensus_path, model_hash
    )
    if model_factory is None:
        try:
            import torch
            import ultralytics
            from ultralytics import YOLO
        except ImportError as exc:
            raise DenseTrackError("pinned segmentation runtime is unavailable") from exc
        model_factory = YOLO
        runtime = {
            "torch_version": torch.__version__,
            "ultralytics_version": ultralytics.__version__,
        }
    else:
        runtime = {"torch_version": "test-double", "ultralytics_version": "test-double"}
    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        raise DenseTrackError("dense track output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        sequences = [
            process_window(
                report, consensus_index, model_path, staged, model_factory,
                confidence, iou_threshold, image_size, device,
            )
            for report in capture_reports
        ]
        payload = {
            "schema": SCHEMA,
            "generated_at_utc": utc_now(),
            "acceptance_eligible": False,
            "acceptance_failures": [
                "tracker_identity_is_a_model_proposal_not_independent_same_car_truth",
                "ground_contacts_are_unreviewed_segmentation_midpoints",
                "static_camera_and_map_calibration_must_pass_before_backprojection",
                "development_sequences_are_not_untouched_holdouts",
            ],
            "consensus": {
                "path": str(consensus_file),
                "sha256": sha256_bytes(consensus_raw),
                "selected_model_side": side,
            },
            "model": {"path": str(model_path), "sha256": model_hash},
            "runtime": {
                **runtime,
                "python_version": platform.python_version(),
                "opencv_version": cv2.__version__,
                "device": device,
                "image_size": image_size,
                "confidence": confidence,
                "nms_iou": iou_threshold,
                "tracker": "bytetrack.yaml",
            },
            "sequences": sequences,
            "summary": {
                "sequence_count": len(sequences),
                "ready_for_independent_review_count": sum(
                    row["proposal_status"] == "ready_for_independent_review"
                    for row in sequences
                ),
                "rejected_count": sum(row["proposal_status"] == "rejected" for row in sequences),
                "input_frame_count": sum(row["summary"]["input_frame_count"] for row in sequences),
                "tracked_frame_count": sum(row["summary"]["tracked_frame_count"] for row in sequences),
                "contact_proposal_count": sum(
                    row["summary"]["contact_proposal_count"] for row in sequences
                ),
            },
        }
        report_path = staged / "report.json"
        report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        (staged / "SHA256SUMS").write_text("".join(
            f"{sha256_file(path)}  {path.relative_to(staged).as_posix()}\n"
            for path in sorted(item for item in staged.rglob("*") if item.is_file())
        ))
        atomic_publish_directory(staged, output)
    except StaticCaptureError as exc:
        raise DenseTrackError("atomic dense-track publication failed") from exc
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", action="append", required=True)
    parser.add_argument("--consensus", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--nms-iou", type=float, default=0.70)
    parser.add_argument("--image-size", type=int, default=1280)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    try:
        output = propose(
            args.capture_report, args.consensus, args.model, args.output_directory,
            confidence=args.confidence, iou_threshold=args.nms_iou,
            image_size=args.image_size, device=args.device,
        )
    except (DenseTrackError, OSError, ValueError) as exc:
        print(f"dense vehicle tracking failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

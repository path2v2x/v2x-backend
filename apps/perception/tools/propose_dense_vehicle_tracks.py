#!/usr/bin/env python3
"""Build hash-bound, proposal-only vehicle tracks from dense KVS windows.

The persisted object ID and sparse detection box are hints, never identity or
geometry truth.  A pinned segmentation model is tracked over every retained
frame.  Exact-frame, cross-model segmentation consensus anchors select the
target tracker ID.  Any anchor disagreement, missing frame binding, hash drift,
or tracker-ID switch is retained as a rejection rather than hidden.

Resource admission: do not run CPU model jobs while live perception is active
unless the job is isolated below a previously proven non-impact CPU and memory
cap.  This proposal tool does not establish that cap.

SIGINT and SIGTERM remove only this invocation's exact staging directory.
SIGKILL cannot be handled and can leave that directory behind.  Later runs do
not sweep similarly named directories: an operator must prove the owning
process is gone and the final output is absent before removing an orphan.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
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
from capture_dense_kvs_window import (  # noqa: E402
    DenseCaptureError,
    load_source_report as load_dense_source_report,
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
    validate_bbox,
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
MINIMUM_ANCHOR_IOU = 0.50
MINIMUM_PLAUSIBLE_RUNNER_UP_IOU = 0.20
MINIMUM_ANCHOR_IOU_MARGIN = 0.20
MINIMUM_DISTINCT_ANCHOR_FRAMES = 2
MINIMUM_ANCHOR_SPAN_MS = 200.0
REFERENCE_WIDTH = 1280.0
REFERENCE_HEIGHT = 960.0
MAXIMUM_CONTACT_RESIDUAL_JUMP_AT_REFERENCE_PX = 24.0
MAXIMUM_CONTACT_RESIDUAL_SPEED_AT_REFERENCE_PX_PER_SECOND = 160.0
MAXIMUM_CONTACT_RESIDUAL_ACCELERATION_AT_REFERENCE_PX_PER_SECOND2 = 1000.0
STAGING_OWNER_MARKER = ".v2x-dense-track-staging-owner.json"
HANDLED_TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class DenseTrackError(RuntimeError):
    pass


class DenseTrackInterrupted(DenseTrackError):
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


def canonical_json_sha256(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def write_bytes_exclusive(path, value, label):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DenseTrackError(f"{label} path collision") from exc


def write_text_exclusive(path, value, label):
    write_bytes_exclusive(path, value.encode("utf-8"), label)


def copy_file_exclusive(source, destination, label):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Path(source).open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except FileExistsError as exc:
        raise DenseTrackError(f"{label} path collision") from exc


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory_tree(root):
    root = Path(root)
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)


@contextmanager
def invocation_staging_directory(output):
    """Yield one owned staging directory with scoped, cleanup-safe signals."""
    if not hasattr(signal, "pthread_sigmask"):
        raise DenseTrackError("scoped staging requires POSIX signal masking")
    output = Path(output).resolve()
    handled = set(HANDLED_TERMINATION_SIGNALS)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
    previous_handlers = {}
    staged = None
    marker = None
    state = {"cleanup_started": False, "received": None}

    def interrupt(signum, _frame):
        # A repeated signal must not interrupt the exact-directory cleanup.
        if state["cleanup_started"] or state["received"] is not None:
            return
        state["received"] = signum
        raise DenseTrackInterrupted(
            f"dense vehicle tracking interrupted by {signal.Signals(signum).name}"
        )

    cleanup_error = None
    try:
        try:
            for handled_signal in HANDLED_TERMINATION_SIGNALS:
                previous_handler = signal.getsignal(handled_signal)
                signal.signal(handled_signal, interrupt)
                previous_handlers[handled_signal] = previous_handler
        except ValueError as exc:
            raise DenseTrackError(
                "scoped staging signal handlers require the Python main thread"
            ) from exc
        staged = Path(tempfile.mkdtemp(
            prefix=f".{output.name}.tmp-", dir=output.parent
        ))
        marker = staged / STAGING_OWNER_MARKER
        write_text_exclusive(
            marker,
            json.dumps({
                "schema": "v2x-dense-track-staging-owner/v1",
                "pid": os.getpid(),
                "created_at_utc": utc_now(),
                "nonce": os.urandom(16).hex(),
                "staging_directory": str(staged.resolve()),
                "final_output_directory": str(output),
            }, indent=2, sort_keys=True) + "\n",
            "dense track staging owner marker",
        )
        fsync_directory(staged)
        # The try/finally is active before pending signals are deliverable.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield staged, marker
    finally:
        state["cleanup_started"] = True
        signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        if staged is not None and staged.exists():
            try:
                # Never search for or sweep other similarly named directories.
                shutil.rmtree(staged)
            except OSError as exc:
                cleanup_error = exc
        try:
            for handled_signal, previous_handler in previous_handlers.items():
                signal.signal(handled_signal, previous_handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if cleanup_error is not None:
            raise DenseTrackError(
                f"failed to remove invocation staging directory {staged}"
            ) from cleanup_error


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


def load_dense_report(path, expected_source_report_sha256=None):
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
    declared_source_path = source.get("path")
    declared_source_hash = source.get("sha256")
    source_path = Path(str(declared_source_path or "")).expanduser().resolve()
    if (
        not source_path.is_file()
        or str(source_path) != declared_source_path
        or not valid_sha256(declared_source_hash)
    ):
        raise DenseTrackError("dense source-event report identity mismatches disk")
    try:
        (
            verified_source_path,
            source_raw,
            verified_source_events,
            _first_source_time,
            _last_source_time,
        ) = load_dense_source_report(source_path, camera_id, object_id)
    except (DenseCaptureError, OSError, TypeError, ValueError) as exc:
        raise DenseTrackError(
            "dense source-event report content is invalid or unrelated"
        ) from exc
    actual_source_hash = sha256_bytes(source_raw)
    if (
        verified_source_path != source_path
        or actual_source_hash != declared_source_hash
        or (
            expected_source_report_sha256 is not None
            and actual_source_hash != expected_source_report_sha256
        )
    ):
        raise DenseTrackError(
            "dense source-event report is not the consensus capture denominator"
        )

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
    expected_source_events = [
        {
            "event_id": event["event_id"],
            "selected_frame_timestamp_utc": event["selected_frame_timestamp_utc"],
            "frame_sha256": event["frame"]["sha256"],
        }
        for event in verified_source_events
    ]
    if source_events != expected_source_events:
        raise DenseTrackError(
            "dense source events drift from the verified source-report denominator"
        )
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
    retained_model_hashes = set()
    for side, identity in zip(sides, inputs):
        model = identity.get("model") if isinstance(identity, dict) else None
        input_path = Path(str(identity.get("path", ""))).expanduser().resolve() if isinstance(identity, dict) else None
        model_path = (
            Path(str(model.get("path", ""))).expanduser().resolve()
            if isinstance(model, dict)
            else None
        )
        retained_model_hash = model.get("sha256") if isinstance(model, dict) else None
        if (
            not isinstance(model, dict)
            or not input_path
            or not input_path.is_file()
            or str(input_path) != identity.get("path")
            or sha256_file(input_path) != identity.get("sha256")
            or not model_path
            or not model_path.is_file()
            or str(model_path) != model.get("path")
            or not valid_sha256(retained_model_hash)
            or sha256_file(model_path) != retained_model_hash
            or retained_model_hash in retained_model_hashes
        ):
            raise DenseTrackError(
                "segmentation consensus report or model identity mismatches disk"
            )
        retained_model_hashes.add(retained_model_hash)
        input_paths.append(input_path)
        if retained_model_hash == model_hash:
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
    box_count = len(result.boxes)
    if masks.ndim != 3 or masks.shape[0] != box_count:
        raise DenseTrackError("tracking masks are not ordered one-for-one with boxes")
    if ids is not None and len(ids) != box_count:
        raise DenseTrackError("tracking IDs are not ordered one-for-one with boxes")
    instances = []
    for index, box in enumerate(result.boxes):
        class_id = int(box.cls[0].item())
        label = str(result.names[class_id])
        if label not in VEHICLE_LABELS:
            continue
        if ids is None:
            continue
        track_id_value = float(ids[index].item())
        if (
            not math.isfinite(track_id_value)
            or not track_id_value.is_integer()
            or track_id_value < 0
        ):
            raise DenseTrackError("tracking model returned a non-integral tracker ID")
        confidence = float(box.conf[0].item())
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise DenseTrackError("tracking model returned an invalid confidence")
        mask = masks[index]
        if mask.ndim != 2 or not np.isfinite(mask).all():
            raise DenseTrackError("tracking model returned an invalid mask plane")
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        bbox = [float(value) for value in box.xyxy[0].tolist()]
        try:
            validate_bbox(bbox, width, height)
        except (ContactProposalError, TypeError, ValueError) as exc:
            raise DenseTrackError("tracking model returned an invalid bbox") from exc
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
            "track_id": int(track_id_value),
            "label": label,
            "confidence": confidence,
            "bbox_xyxy": bbox,
            "mask": clean_mask,
            "ground_contact_proposal": proposal,
            "rejection_reasons": reasons,
        })
    return sorted(instances, key=lambda item: (item["track_id"], -item["confidence"]))


def select_target_track(tracked_frames, anchors, consensus_index, camera_id):
    matches = []
    reasons = []
    fatal_identity_ambiguity = False
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
        if not scored or scored[0][0] < MINIMUM_ANCHOR_IOU:
            reasons.append(f"anchor_unmatched:{anchor['event_id']}")
            continue
        if len(scored) > 1:
            best_iou, runner_up_iou = scored[0][0], scored[1][0]
            if (
                runner_up_iou >= MINIMUM_ANCHOR_IOU
                or (
                    runner_up_iou >= MINIMUM_PLAUSIBLE_RUNNER_UP_IOU
                    and best_iou - runner_up_iou < MINIMUM_ANCHOR_IOU_MARGIN
                )
            ):
                reasons.append(f"anchor_ambiguous:{anchor['event_id']}")
                fatal_identity_ambiguity = True
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
    frame_keys = {
        (item["frame_sha256"], item["timestamp_utc"]) for item in matches
    }
    distinct_hashes = {item["frame_sha256"] for item in matches}
    distinct_timestamps = {item["timestamp_utc"] for item in matches}
    if len(frame_keys) != len(matches):
        reasons.append("duplicate_anchor_source_frame")
        fatal_identity_ambiguity = True
    if not matches:
        reasons.append("no_cross_model_anchor_matched")
        target = None
    elif len(track_ids) != 1:
        reasons.append("anchor_tracker_identity_conflict")
        target = None
    elif fatal_identity_ambiguity:
        target = None
    elif (
        len(frame_keys) < MINIMUM_DISTINCT_ANCHOR_FRAMES
        or len(distinct_hashes) < MINIMUM_DISTINCT_ANCHOR_FRAMES
        or len(distinct_timestamps) < MINIMUM_DISTINCT_ANCHOR_FRAMES
    ):
        reasons.append("fewer_than_two_distinct_cross_model_anchor_frames")
        target = None
    else:
        anchor_times = sorted(
            parse_utc(value, "anchor timestamp").timestamp()
            for value in distinct_timestamps
        )
        if (anchor_times[-1] - anchor_times[0]) * 1000.0 < MINIMUM_ANCHOR_SPAN_MS:
            reasons.append("cross_model_anchor_span_below_200ms")
            target = None
        else:
            target = track_ids[0]
    return target, matches, reasons


def dense_sequence_identity(
    report_path, report_raw, report, model_sha256, consensus_sha256
):
    identity = {
        "camera_id": report["camera_id"],
        "model_object_id": report["object_id"],
        "capture_report_path": str(Path(report_path).resolve()),
        "capture_report_sha256": sha256_bytes(report_raw),
        "source_event_report_path": report["source_event_report"]["path"],
        "source_event_report_sha256": report["source_event_report"]["sha256"],
        "tracking_model_sha256": model_sha256,
        "segmentation_consensus_sha256": consensus_sha256,
    }
    digest = canonical_json_sha256(identity)
    return f"{report['camera_id']}-{digest}", digest, identity


def finite_vector(value, size):
    return (
        isinstance(value, (list, tuple))
        and len(value) == size
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def validated_contact_state(instance, frame, row):
    proposal = instance.get("ground_contact_proposal")
    mask = np.asarray(instance.get("mask"))
    bbox = instance.get("bbox_xyxy")
    if not isinstance(proposal, dict):
        raise DenseTrackError("contact_proposal_missing")
    if proposal.get("method") != "segmentation_visible_support_midpoint_proposal":
        raise DenseTrackError("contact_proposal_method_invalid")
    if proposal.get("reviewed") is not False:
        raise DenseTrackError("contact_proposal_review_state_invalid")
    if mask.shape != (frame["height"], frame["width"]) or mask.dtype != np.bool_:
        raise DenseTrackError("contact_mask_shape_invalid")
    if not mask.any():
        raise DenseTrackError("contact_mask_empty")
    try:
        checked_bbox = validate_bbox(bbox, frame["width"], frame["height"])
    except (ContactProposalError, TypeError, ValueError) as exc:
        raise DenseTrackError("contact_bbox_invalid") from exc
    pixel = proposal.get("pixel")
    endpoints = proposal.get("support_endpoints")
    if not finite_vector(pixel, 2):
        raise DenseTrackError("contact_proposal_pixel_invalid")
    if (
        not isinstance(endpoints, (list, tuple))
        or len(endpoints) != 2
        or not all(finite_vector(endpoint, 2) for endpoint in endpoints)
    ):
        raise DenseTrackError("contact_proposal_support_invalid")
    pixel_array = np.asarray(pixel, dtype=float)
    endpoints_array = np.asarray(endpoints, dtype=float)
    x1, y1, x2, y2 = checked_bbox
    if not (
        x1 <= pixel_array[0] <= x2
        and y1 <= pixel_array[1] <= y2
        and np.all(endpoints_array[:, 0] >= x1)
        and np.all(endpoints_array[:, 0] <= x2)
        and np.all(endpoints_array[:, 1] >= y1)
        and np.all(endpoints_array[:, 1] <= y2)
    ):
        raise DenseTrackError("contact_proposal_outside_bbox")
    try:
        covariance = np.asarray(proposal.get("covariance_px2"), dtype=float)
    except (TypeError, ValueError) as exc:
        raise DenseTrackError("contact_proposal_covariance_invalid") from exc
    if (
        covariance.shape != (2, 2)
        or not np.isfinite(covariance).all()
        or not np.allclose(covariance, covariance.T, atol=1e-9)
        or float(np.linalg.eigvalsh(covariance).min()) < -1e-9
    ):
        raise DenseTrackError("contact_proposal_covariance_invalid")
    mask_area = int(mask.sum())
    if proposal.get("mask_area_px") != mask_area:
        raise DenseTrackError("contact_proposal_mask_area_mismatch")
    for key in ("mask_fraction_of_bbox", "support_span_fraction_of_bbox", "support_quantile"):
        value = proposal.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 < float(value) <= 1.0
        ):
            raise DenseTrackError(f"contact_proposal_{key}_invalid")
    support_count = proposal.get("support_column_count")
    if (
        not isinstance(support_count, int)
        or isinstance(support_count, bool)
        or support_count <= 0
    ):
        raise DenseTrackError("contact_proposal_support_count_invalid")
    y_coordinates, x_coordinates = np.nonzero(mask)
    mask_centroid = np.asarray(
        [float(np.mean(x_coordinates)), float(np.mean(y_coordinates))], dtype=float
    )
    bbox_center = np.asarray([(x1 + x2) / 2.0, (y1 + y2) / 2.0], dtype=float)
    return {
        "frame_index": frame["index"],
        "timestamp_epoch": frame["timestamp_epoch"],
        "contact": pixel_array,
        "bbox_center": bbox_center,
        "mask_centroid": mask_centroid,
        "row": row,
    }


def contact_temporal_gate(width, height):
    scale = max(float(width) / REFERENCE_WIDTH, float(height) / REFERENCE_HEIGHT)
    scale = max(scale, 0.25)
    return {
        "reference_canvas": {"width": int(REFERENCE_WIDTH), "height": int(REFERENCE_HEIGHT)},
        "native_scale": scale,
        "maximum_residual_jump_px": (
            MAXIMUM_CONTACT_RESIDUAL_JUMP_AT_REFERENCE_PX * scale
        ),
        "maximum_residual_speed_px_per_second": (
            MAXIMUM_CONTACT_RESIDUAL_SPEED_AT_REFERENCE_PX_PER_SECOND * scale
        ),
        "maximum_residual_acceleration_px_per_second2": (
            MAXIMUM_CONTACT_RESIDUAL_ACCELERATION_AT_REFERENCE_PX_PER_SECOND2 * scale
        ),
        "motion_references": ["bbox_center", "segmentation_mask_centroid"],
    }


def evaluate_contact_temporal_consistency(states, width, height):
    gate = contact_temporal_gate(width, height)
    diagnostics = []
    sequence_reasons = []
    previous_velocity = None
    previous_dt = None
    for left, right in zip(states, states[1:]):
        dt = right["timestamp_epoch"] - left["timestamp_epoch"]
        pair_reasons = []
        consecutive_frames = right["frame_index"] == left["frame_index"] + 1
        if not consecutive_frames:
            pair_reasons.append("contact_temporal_input_frame_gap")
            previous_velocity = None
            previous_dt = None
        if not math.isfinite(dt) or dt <= 0.0:
            pair_reasons.append("contact_temporal_timestamp_invalid")
            dt = math.nan
        contact_delta = right["contact"] - left["contact"]
        bbox_residual = contact_delta - (
            right["bbox_center"] - left["bbox_center"]
        )
        mask_residual = contact_delta - (
            right["mask_centroid"] - left["mask_centroid"]
        )
        bbox_jump = float(np.linalg.norm(bbox_residual))
        mask_jump = float(np.linalg.norm(mask_residual))
        bbox_speed = mask_speed = math.inf
        current_velocity = None
        if math.isfinite(dt) and consecutive_frames:
            bbox_velocity = bbox_residual / dt
            mask_velocity = mask_residual / dt
            bbox_speed = float(np.linalg.norm(bbox_velocity))
            mask_speed = float(np.linalg.norm(mask_velocity))
            current_velocity = (bbox_velocity, mask_velocity)
        if bbox_jump > gate["maximum_residual_jump_px"]:
            pair_reasons.append("contact_bbox_residual_jump_above_gate")
        if mask_jump > gate["maximum_residual_jump_px"]:
            pair_reasons.append("contact_mask_residual_jump_above_gate")
        if bbox_speed > gate["maximum_residual_speed_px_per_second"]:
            pair_reasons.append("contact_bbox_residual_speed_above_gate")
        if mask_speed > gate["maximum_residual_speed_px_per_second"]:
            pair_reasons.append("contact_mask_residual_speed_above_gate")
        bbox_acceleration = mask_acceleration = None
        if (
            current_velocity is not None
            and previous_velocity is not None
            and previous_dt is not None
        ):
            acceleration_dt = (dt + previous_dt) / 2.0
            bbox_acceleration = float(
                np.linalg.norm(current_velocity[0] - previous_velocity[0])
                / acceleration_dt
            )
            mask_acceleration = float(
                np.linalg.norm(current_velocity[1] - previous_velocity[1])
                / acceleration_dt
            )
            if (
                bbox_acceleration
                > gate["maximum_residual_acceleration_px_per_second2"]
            ):
                pair_reasons.append(
                    "contact_bbox_residual_acceleration_above_gate"
                )
            if (
                mask_acceleration
                > gate["maximum_residual_acceleration_px_per_second2"]
            ):
                pair_reasons.append(
                    "contact_mask_residual_acceleration_above_gate"
                )
        if current_velocity is not None:
            previous_velocity = current_velocity
            previous_dt = dt
        pair_reasons = sorted(set(pair_reasons))
        if pair_reasons:
            right["row"]["rejection_reasons"] = sorted(set(
                right["row"]["rejection_reasons"] + pair_reasons
            ))
            sequence_reasons.extend(pair_reasons)
        diagnostics.append({
            "left_frame_index": left["frame_index"],
            "right_frame_index": right["frame_index"],
            "delta_time_ms": None if not math.isfinite(dt) else dt * 1000.0,
            "contact_motion_px": float(np.linalg.norm(contact_delta)),
            "bbox_center_residual_jump_px": bbox_jump,
            "mask_centroid_residual_jump_px": mask_jump,
            "bbox_center_residual_speed_px_per_second": (
                None if not math.isfinite(bbox_speed) else bbox_speed
            ),
            "mask_centroid_residual_speed_px_per_second": (
                None if not math.isfinite(mask_speed) else mask_speed
            ),
            "bbox_center_residual_acceleration_px_per_second2": bbox_acceleration,
            "mask_centroid_residual_acceleration_px_per_second2": mask_acceleration,
            "rejection_reasons": pair_reasons,
        })
    return diagnostics, sorted(set(sequence_reasons)), gate


def verify_output_artifacts(staged_root, sequences):
    for sequence in sequences:
        descriptors = [sequence.get("review_sheet")]
        descriptors.extend(frame.get("mask") for frame in sequence.get("frames", []))
        for descriptor in descriptors:
            if descriptor is None:
                continue
            relative = descriptor.get("path") if isinstance(descriptor, dict) else None
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise DenseTrackError("dense-track output artifact path is invalid")
            path = (staged_root / relative).resolve()
            try:
                path.relative_to(staged_root.resolve())
            except ValueError as exc:
                raise DenseTrackError("dense-track output artifact path escapes stage") from exc
            if not path.is_file() or sha256_file(path) != descriptor.get("sha256"):
                raise DenseTrackError("dense-track output artifact hash binding failed")


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
                   confidence, iou_threshold, image_size, device,
                   expected_source_report_sha256=None, model_sha256=None,
                   consensus_sha256=None):
    report_path, report_raw, report, frames, anchors = load_dense_report(
        report_value,
        expected_source_report_sha256=expected_source_report_sha256,
    )
    camera_id = report["camera_id"]
    if not valid_sha256(model_sha256) or not valid_sha256(consensus_sha256):
        raise DenseTrackError("dense sequence model or consensus binding is invalid")
    sequence_id, sequence_identity_sha256, sequence_identity = dense_sequence_identity(
        report_path, report_raw, report, model_sha256, consensus_sha256
    )
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
    temporal_states = []
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
            frame_reasons = list(instance["rejection_reasons"])
            mask_descriptor = None
            row = {
                "frame_index": frame["index"],
                "producer_timestamp_utc": frame["timestamp_utc"],
                "frame_sha256": frame["sha256"],
                "track_id": target_id,
                "label": instance["label"],
                "confidence": instance["confidence"],
                "bbox_xyxy": instance["bbox_xyxy"],
                "ground_contact_proposal": proposal,
                "mask": None,
                "rejection_reasons": frame_reasons,
                "acceptance_eligible": False,
            }
            if proposal is not None:
                try:
                    state = validated_contact_state(instance, frame, row)
                except DenseTrackError as exc:
                    proposal = None
                    row["ground_contact_proposal"] = None
                    frame_reasons.append(str(exc))
                else:
                    mask_relative = (
                        Path("masks") / sequence_id / f"frame-{frame['index']:03d}.png"
                    )
                    mask_path = staged_root / mask_relative
                    ok, encoded = cv2.imencode(
                        ".png", instance["mask"].astype(np.uint8) * 255,
                        [cv2.IMWRITE_PNG_COMPRESSION, 4],
                    )
                    if not ok:
                        raise DenseTrackError("failed to encode dense target mask")
                    write_bytes_exclusive(
                        mask_path, encoded.tobytes(), "dense target mask"
                    )
                    mask_descriptor = {
                        "path": mask_relative.as_posix(),
                        "sha256": sha256_file(mask_path),
                    }
                    row["mask"] = mask_descriptor
                    overlay = draw_overlay(
                        frame["image"], instance["bbox_xyxy"], instance["mask"], proposal
                    )
                    review_candidates.append((frame, overlay))
                    temporal_states.append(state)
            if proposal is None:
                frame_reasons.append("target_frame_has_no_valid_contact_proposal")
            row["rejection_reasons"] = sorted(set(frame_reasons))
            selected_rows.append(row)
    coverage = len(selected_rows) / len(frames)
    timestamps = [parse_utc(row["producer_timestamp_utc"], "selected frame").timestamp()
                  for row in selected_rows]
    maximum_gap_ms = max(
        ((right - left) * 1000.0 for left, right in zip(timestamps, timestamps[1:])),
        default=None,
    )
    if target_id is not None and coverage < 0.75:
        rejection_reasons.append("target_track_coverage_below_75_percent")
    if target_id is not None and coverage < 1.0:
        rejection_reasons.append("target_track_not_present_in_every_frame")
    if target_id is not None and maximum_gap_ms is not None and maximum_gap_ms > 1000.0:
        rejection_reasons.append("target_track_gap_above_1000ms")
    distinct_anchor_frames = {
        (item["frame_sha256"], item["timestamp_utc"]) for item in anchor_matches
    }
    if target_id is not None and len(distinct_anchor_frames) < 2:
        rejection_reasons.append("fewer_than_two_cross_model_anchors")
    contact_count = sum(
        row["ground_contact_proposal"] is not None and row["mask"] is not None
        for row in selected_rows
    )
    if target_id is not None and contact_count != len(frames):
        rejection_reasons.append("not_every_input_frame_has_a_valid_contact_and_mask")
    for row in selected_rows:
        rejection_reasons.extend(row["rejection_reasons"])

    temporal_diagnostics = []
    temporal_gate = contact_temporal_gate(frames[0]["width"], frames[0]["height"])
    if temporal_states:
        (
            temporal_diagnostics,
            temporal_reasons,
            temporal_gate,
        ) = evaluate_contact_temporal_consistency(
            temporal_states, frames[0]["width"], frames[0]["height"]
        )
        rejection_reasons.extend(temporal_reasons)

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
        write_bytes_exclusive(
            review_path, make_review_sheet(entries), "dense track review sheet"
        )
        review_descriptor = {
            "path": review_relative.as_posix(),
            "sha256": sha256_file(review_path),
        }
    return {
        "sequence_id": sequence_id,
        "sequence_identity_sha256": sequence_identity_sha256,
        "sequence_identity": sequence_identity,
        "camera_id": camera_id,
        "model_object_id": report["object_id"],
        "model_object_id_is_identity_truth": False,
        "capture_report": {"path": str(report_path), "sha256": sha256_bytes(report_raw)},
        "source_event_report": report["source_event_report"],
        "target_track_id": target_id,
        "anchors": anchor_matches,
        "frames": selected_rows,
        "temporal_contact_gate": temporal_gate,
        "temporal_contact_diagnostics": temporal_diagnostics,
        "review_sheet": review_descriptor,
        "summary": {
            "input_frame_count": len(frames),
            "tracked_frame_count": len(selected_rows),
            "contact_proposal_count": contact_count,
            "coverage_fraction": coverage,
            "maximum_tracked_gap_ms": maximum_gap_ms,
            "matched_anchor_count": len(anchor_matches),
            "distinct_matched_anchor_frame_count": len(distinct_anchor_frames),
            "temporal_contact_rejection_pair_count": sum(
                bool(row["rejection_reasons"]) for row in temporal_diagnostics
            ),
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
    consensus_file, consensus_raw, consensus_value, consensus_index, side = load_consensus(
        consensus_path, model_hash
    )
    consensus_hash = sha256_bytes(consensus_raw)
    source_report_sha256 = consensus_value.get("capture_report_sha256")
    if not valid_sha256(source_report_sha256):
        raise DenseTrackError("segmentation consensus capture denominator is invalid")
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
    with invocation_staging_directory(output) as (staged, owner_marker):
        try:
            model_snapshot_relative = Path("inputs") / f"tracking-model-{model_hash}.pt"
            model_snapshot = staged / model_snapshot_relative
            copy_file_exclusive(model_path, model_snapshot, "tracking model snapshot")
            if sha256_file(model_snapshot) != model_hash:
                raise DenseTrackError("tracking model changed while it was snapshotted")
            sequences = [
                process_window(
                    report, consensus_index, model_snapshot, staged, model_factory,
                    confidence, iou_threshold, image_size, device,
                    expected_source_report_sha256=source_report_sha256,
                    model_sha256=model_hash,
                    consensus_sha256=consensus_hash,
                )
                for report in capture_reports
            ]
            sequence_ids = [sequence["sequence_id"] for sequence in sequences]
            if len(sequence_ids) != len(set(sequence_ids)):
                raise DenseTrackError(
                    "dense capture reports resolve to duplicate sequence IDs"
                )
            verify_output_artifacts(staged, sequences)
            if sha256_file(model_snapshot) != model_hash:
                raise DenseTrackError("tracking model snapshot changed during inference")
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
                    "sha256": consensus_hash,
                    "selected_model_side": side,
                    "capture_report_sha256": source_report_sha256,
                },
                "model": {
                    "path": str(model_path),
                    "sha256": model_hash,
                    "execution_snapshot": {
                        "path": model_snapshot_relative.as_posix(),
                        "sha256": model_hash,
                    },
                },
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
                    "rejected_count": sum(
                        row["proposal_status"] == "rejected" for row in sequences
                    ),
                    "input_frame_count": sum(
                        row["summary"]["input_frame_count"] for row in sequences
                    ),
                    "tracked_frame_count": sum(
                        row["summary"]["tracked_frame_count"] for row in sequences
                    ),
                    "contact_proposal_count": sum(
                        row["summary"]["contact_proposal_count"] for row in sequences
                    ),
                },
            }
            report_path = staged / "report.json"
            write_text_exclusive(
                report_path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                "dense track report",
            )
            owner_marker.unlink()
            fsync_directory(staged)
            write_text_exclusive(staged / "SHA256SUMS", "".join(
                f"{sha256_file(path)}  {path.relative_to(staged).as_posix()}\n"
                for path in sorted(item for item in staged.rglob("*") if item.is_file())
            ), "dense track checksum manifest")
            fsync_directory_tree(staged)
            atomic_publish_directory(staged, output)
            fsync_directory(output.parent)
        except StaticCaptureError as exc:
            raise DenseTrackError("atomic dense-track publication failed") from exc
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

#!/usr/bin/env python3
"""Bounded live verification for Drive session isolation and twin replay.

Without ``--apply`` this tool is observational: it reads server and twin status.
Mutation is explicit, refuses to run while another Drive session exists, always
restores twin live mode, and closes/ends any owned sessions in ``finally``.
``--skip-drive`` keeps Drive observational while replay acceptance runs.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import websockets


DEFAULT_TWIN_YOLO_PYTHON = Path("/home/path/V2XCarla/perception-venv/bin/python")
DEFAULT_TWIN_YOLO_DETECTOR = (
    Path(__file__).resolve().parents[2]
    / "perception"
    / "tools"
    / "detect_jpeg_objects.py"
)


class VerificationError(RuntimeError):
    pass


def utc_iso(value=None):
    value = value or datetime.now(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def websocket_url(base_url, path, query=""):
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


async def receive_json(websocket, expected_types, timeout, evidence=None):
    """Receive the next expected JSON message while skipping binary frames."""
    expected = set(expected_types)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise VerificationError(
                f"timed out waiting for message type {sorted(expected)}"
            )
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            if evidence is not None:
                evidence["binary_frames"] = evidence.get("binary_frames", 0) + 1
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if evidence is not None:
            evidence.setdefault("json_types", []).append(payload.get("type"))
        if payload.get("type") in expected:
            return payload


async def request_json(websocket, payload, expected_types, timeout, evidence=None):
    await websocket.send(json.dumps(payload))
    return await receive_json(
        websocket, expected_types, timeout=timeout, evidence=evidence
    )


def binary_digest(payload):
    return hashlib.sha256(payload).hexdigest()


async def receive_binary_frame(
    websocket,
    timeout,
    *,
    evidence=None,
    different_from=None,
):
    """Receive a binary frame, optionally requiring new rendered content."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            expectation = "changed binary frame" if different_from else "binary frame"
            raise VerificationError(f"timed out waiting for {expectation}")
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        if not isinstance(raw, bytes):
            if evidence is not None:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                evidence.setdefault("json_types", []).append(payload.get("type"))
            continue
        digest = binary_digest(raw)
        if evidence is not None:
            evidence["binary_frames"] = evidence.get("binary_frames", 0) + 1
        if different_from is None or digest != different_from:
            return digest


async def receive_binary_payload(websocket, timeout, *, evidence=None):
    """Receive one rendered JPEG while preserving only its digest in evidence."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise VerificationError("timed out waiting for twin JPEG evidence")
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        if not isinstance(raw, bytes):
            if evidence is not None:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                evidence.setdefault("json_types", []).append(payload.get("type"))
            continue
        if evidence is not None:
            evidence["binary_frames"] = evidence.get("binary_frames", 0) + 1
        return raw, binary_digest(raw)


def _carla_rotation_axes(rotation):
    pitch = math.radians(float(rotation["pitch"]))
    yaw = math.radians(float(rotation["yaw"]))
    roll = math.radians(float(rotation["roll"]))
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return (
        (cp * cy, cp * sy, sp),
        (cy * sp * sr - sy * cr, sy * sp * sr + cy * cr, -cp * sr),
        (-cy * sp * cr - sy * sr, -sy * sp * cr + cy * sr, cp * cr),
    )


def project_world_xyz(point, camera_model):
    """Project one CARLA XYZ through the exact zero-distortion twin model."""
    lens = camera_model["lens"]
    if abs(lens["lens_k"]) > 1e-9 or abs(lens["lens_kcube"]) > 1e-9:
        raise VerificationError(
            "twin actor projection does not support unmodeled nonzero CARLA lens distortion"
        )
    transform = camera_model["transform"]
    location = transform["location"]
    delta = tuple(float(point[index]) - location[axis] for index, axis in enumerate(("x", "y", "z")))
    forward, right, up = _carla_rotation_axes(transform["rotation"])
    depth = sum(left * right_value for left, right_value in zip(delta, forward))
    if depth <= 0.1:
        return None
    local_right = sum(left * right_value for left, right_value in zip(delta, right))
    local_up = sum(left * right_value for left, right_value in zip(delta, up))
    image = camera_model["image"]
    focal = (image["width"] / 2.0) / math.tan(
        math.radians(image["horizontal_fov_deg"]) / 2.0
    )
    return (
        image["width"] / 2.0 + focal * local_right / depth,
        image["height"] / 2.0 - focal * local_up / depth,
        depth,
    )


def project_actor_bbox(actor, camera_model):
    """Project a concrete UE5 actor's 3-D bounding box into its twin JPEG."""
    bounding_box = getattr(actor, "bounding_box", None)
    if bounding_box is None or not hasattr(bounding_box, "get_world_vertices"):
        raise VerificationError("mapped UE5 actor has no projectable bounding box")
    try:
        vertices = bounding_box.get_world_vertices(actor.get_transform())
    except Exception as exc:
        raise VerificationError("failed to obtain mapped UE5 actor bounding box") from exc
    projected = []
    for vertex in vertices:
        value = project_world_xyz((vertex.x, vertex.y, vertex.z), camera_model)
        if value is not None:
            projected.append(value)
    if len(projected) < 4:
        raise VerificationError("mapped UE5 actor is not sufficiently in front of twin camera")
    xs, ys = [value[0] for value in projected], [value[1] for value in projected]
    raw = (min(xs), min(ys), max(xs), max(ys))
    width, height = camera_model["image"]["width"], camera_model["image"]["height"]
    clipped = (
        max(0.0, min(float(width), raw[0])),
        max(0.0, min(float(height), raw[1])),
        max(0.0, min(float(width), raw[2])),
        max(0.0, min(float(height), raw[3])),
    )
    area = max(0.0, clipped[2] - clipped[0]) * max(0.0, clipped[3] - clipped[1])
    if area < 400.0:
        raise VerificationError("mapped UE5 actor projection is too small or outside the twin frame")
    return tuple(round(value, 3) for value in clipped)


def _bbox_iou(left, right):
    ix1, iy1 = max(left[0], right[0]), max(left[1], right[1])
    ix2, iy2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return (intersection / union if union > 0.0 else 0.0), (
        intersection / left_area if left_area > 0.0 else 0.0
    )


def validate_projected_actor_detection(
    projected_bbox,
    detections,
    object_type,
    *,
    minimum_iou=0.15,
    minimum_actor_coverage=0.50,
):
    """Require a compatible visual detection overlapping the exact actor projection."""
    expected = {
        "car": {"car"},
        "truck": {"truck"},
        "bus": {"bus"},
        "person": {"person"},
    }.get(object_type, set())
    candidates = []
    for detection in detections:
        if detection.get("label") not in expected:
            continue
        bbox = tuple(float(value) for value in detection["bbox"])
        iou, coverage = _bbox_iou(projected_bbox, bbox)
        candidate = {
            "label": detection["label"],
            "confidence": round(float(detection["confidence"]), 4),
            "bbox": [round(value, 3) for value in bbox],
            "iou_with_projected_actor": round(iou, 4),
            "projected_actor_coverage": round(coverage, 4),
        }
        candidate["compatible"] = (
            iou >= minimum_iou and coverage >= minimum_actor_coverage
        )
        candidates.append(candidate)
    matches = [candidate for candidate in candidates if candidate["compatible"]]
    if not matches:
        raise VerificationError(
            "no compatible visual detection overlaps the projected UE5 actor"
        )
    best = max(
        matches,
        key=lambda candidate: (
            candidate["iou_with_projected_actor"],
            candidate["projected_actor_coverage"],
            candidate["confidence"],
        ),
    )
    return {
        "projected_bbox": list(projected_bbox),
        "minimum_iou": minimum_iou,
        "minimum_actor_coverage": minimum_actor_coverage,
        "best_detection": best,
        "candidate_count": len(candidates),
    }


def file_sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def detect_twin_objects(jpeg, detector, *, confidence, device):
    """Run YOLO in its intended perception environment through bounded stdin."""
    command = [
        str(detector["python"]),
        str(detector["script"]),
        "--model",
        str(detector["model"]),
        "--confidence",
        str(float(confidence)),
        "--device",
        str(device),
    ]
    try:
        completed = subprocess.run(
            command,
            input=jpeg,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60.0,
            check=False,
        )
        payload = json.loads(completed.stdout)
    except Exception as exc:
        raise VerificationError("twin visual detection helper failed") from exc
    if completed.returncode != 0 or payload.get("ok") is not True:
        raise VerificationError("twin visual detection helper rejected the JPEG")
    detections = payload.get("detections")
    if not isinstance(detections, list):
        raise VerificationError("twin visual detection helper returned invalid evidence")
    return detections


def world_actor_inventory(world):
    """Snapshot actor identity fields needed to distinguish ownership."""
    inventory = {}
    for actor in world.get_actors():
        actor_id = getattr(actor, "id", None)
        if not isinstance(actor_id, int):
            continue
        attributes = getattr(actor, "attributes", None) or {}
        inventory[int(actor_id)] = {
            "type_id": str(getattr(actor, "type_id", "")),
            "role_name": str(attributes.get("role_name", "")),
        }
    return inventory


def synchronize_world(world, timeout):
    """Wait for a real CARLA snapshot before trusting actor enumeration.

    RR/CARLA 0.10 can return frame zero and an empty actor registry to a newly
    connected client until that client consumes its first world tick.
    """
    try:
        snapshot = world.wait_for_tick(float(timeout))
    except Exception as exc:
        raise VerificationError("timed out synchronizing the CARLA world") from exc
    frame = getattr(snapshot, "frame", None)
    if isinstance(frame, bool) or not isinstance(frame, int) or frame <= 0:
        raise VerificationError("CARLA world synchronization returned no real frame")
    return frame


def world_actor_ids(world):
    """Snapshot every current CARLA actor ID, not only declared ownership."""
    return set(world_actor_inventory(world))


def is_expected_non_session_actor(actor_identity):
    """Return whether an actor is owned by the map or live-twin runtime.

    The RR world can populate its spectator, traffic-control actors, and fixed
    twin camera rig after the first ``get_actors()`` response. Live twin
    detections may also appear or expire while a Drive session is running.
    These actors are intentionally outside every DriveSession manifest.
    """
    type_id = actor_identity.get("type_id", "")
    role_name = actor_identity.get("role_name", "")
    if type_id == "spectator" or type_id.startswith("traffic."):
        return True
    if role_name == "twin_rig" and type_id.startswith("sensor."):
        return True
    if role_name == "twin_object" and type_id.startswith(("vehicle.", "walker.")):
        return True
    return False


def session_candidate_actor_ids(actor_ids, actor_inventory):
    """Filter map/live-twin churn from a post-baseline actor delta."""
    return {
        actor_id
        for actor_id in actor_ids
        if actor_id not in actor_inventory
        or not is_expected_non_session_actor(actor_inventory[actor_id])
    }


def _manifest_id_set(values, label):
    if not isinstance(values, list):
        raise VerificationError(f"session_ready has no valid {label} manifest")
    parsed = set()
    for value in values:
        if isinstance(value, bool):
            raise VerificationError(f"session_ready {label} are invalid")
        try:
            actor_id = int(value)
        except (TypeError, ValueError) as exc:
            raise VerificationError(f"session_ready {label} are invalid") from exc
        if actor_id <= 0:
            raise VerificationError(f"session_ready {label} are invalid")
        parsed.add(actor_id)
    return parsed


def validate_session_actor_manifest(
    response,
    *,
    baseline_actor_ids,
    current_actor_ids,
    prior_owned_actor_ids,
):
    """Validate ownership categories against actors that actually exist."""
    try:
        vehicle_id = int(response["vehicle_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError("session_ready has no valid vehicle_id") from exc
    owned_ids = _manifest_id_set(response.get("owned_actor_ids"), "owned_actor_ids")
    sensor_ids = _manifest_id_set(response.get("sensor_actor_ids"), "sensor_actor_ids")
    scene_ids = _manifest_id_set(response.get("scene_actor_ids"), "scene_actor_ids")

    if sensor_ids & scene_ids:
        raise VerificationError("session sensor and scene actor manifests overlap")
    expected_owned = {vehicle_id} | sensor_ids | scene_ids
    if owned_ids != expected_owned:
        raise VerificationError(
            "session owned manifest is not exactly ego + sensors + scene actors"
        )
    missing = owned_ids - current_actor_ids
    if missing:
        raise VerificationError(
            f"session manifest contains actors that do not exist: {sorted(missing)}"
        )
    preexisting = owned_ids & baseline_actor_ids
    if preexisting:
        raise VerificationError(
            f"session claims pre-existing CARLA actors: {sorted(preexisting)}"
        )
    overlap = owned_ids & prior_owned_actor_ids
    if overlap:
        raise VerificationError(
            f"session actor ownership overlaps another session: {sorted(overlap)}"
        )
    return vehicle_id, owned_ids, sensor_ids, scene_ids


def validate_actor_delta(
    created_actor_ids,
    declared_actor_ids,
    actor_inventory=None,
):
    """Require every Drive-owned post-baseline actor to be declared.

    Map actors and server-owned live-twin actors are excluded only when their
    type/role pair proves that ownership. All vehicles, sensors, and props that
    could belong to a Drive session remain subject to the exact manifest gate.
    """
    created_actor_ids = set(created_actor_ids)
    declared_actor_ids = set(declared_actor_ids)
    actor_inventory = actor_inventory or {}
    session_candidates = session_candidate_actor_ids(
        created_actor_ids, actor_inventory
    )
    undeclared = session_candidates - declared_actor_ids
    missing_from_world = set(declared_actor_ids) - set(created_actor_ids)
    if undeclared or missing_from_world:
        raise VerificationError(
            "CARLA actor delta does not exactly match session manifests: "
            f"undeclared={sorted(undeclared)} missing={sorted(missing_from_world)}"
        )
    return created_actor_ids - session_candidates


def replay_clock_epoch(value):
    """Normalize the twin protocol's ISO replay clock for monotonic checks."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return None


def _finite_transform_payload(value, label):
    """Normalize a JSON/CARLA-style transform while rejecting partial data."""
    try:
        location = value["location"]
        rotation = value["rotation"]
        parsed = {
            "location": {
                axis: float(location[axis]) for axis in ("x", "y", "z")
            },
            "rotation": {
                axis: float(rotation[axis]) for axis in ("pitch", "yaw", "roll")
            },
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError(f"{label} has no valid transform") from exc
    if not all(
        math.isfinite(number)
        for group in parsed.values()
        for number in group.values()
    ):
        raise VerificationError(f"{label} has no valid transform")
    return parsed


def _actor_transform_payload(actor):
    transform = actor.get_transform()
    return _finite_transform_payload(
        {
            "location": {
                "x": transform.location.x,
                "y": transform.location.y,
                "z": transform.location.z,
            },
            "rotation": {
                "pitch": transform.rotation.pitch,
                "yaw": transform.rotation.yaw,
                "roll": transform.rotation.roll,
            },
        },
        "CARLA actor",
    )


def _angular_error(left, right):
    return abs(((float(left) - float(right) + 180.0) % 360.0) - 180.0)


def _object_from_twin_status(status, object_id):
    objects = status.get("objects")
    if not isinstance(objects, list):
        raise VerificationError("twin_status has no valid objects list")
    matches = [
        item
        for item in objects
        if isinstance(item, dict) and item.get("object_id") == object_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise VerificationError(
            f"twin_status contains duplicate object_id {object_id!r}"
        )
    return matches[0]


def validate_twin_camera_model(hello, expected_camera_id):
    """Validate the exact calibrated UE5 sensor that produced twin frames."""
    if hello.get("camera_id") != expected_camera_id:
        raise VerificationError("twin stream camera does not match the request")
    model = hello.get("camera_model")
    if not isinstance(model, dict) or model.get("camera_id") != expected_camera_id:
        raise VerificationError("twin stream has no matching camera model")
    actor_id = model.get("actor_id")
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id <= 0:
        raise VerificationError("twin camera model has no valid UE5 actor_id")
    fingerprint = str(model.get("config_sha256") or "")
    if not (
        len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
    ):
        raise VerificationError("twin camera model has no valid config fingerprint")
    transform = _finite_transform_payload(model.get("transform"), "twin camera model")
    image = model.get("image")
    if not isinstance(image, dict):
        raise VerificationError("twin camera model has no image geometry")
    width, height = image.get("width"), image.get("height")
    fov = image.get("horizontal_fov_deg")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or width != hello.get("width")
        or height != hello.get("height")
        or isinstance(fov, bool)
        or not isinstance(fov, (int, float))
        or not math.isfinite(float(fov))
        or not 10.0 <= float(fov) <= 170.0
    ):
        raise VerificationError("twin camera model image geometry is invalid")
    lens = model.get("lens")
    if not isinstance(lens, dict) or set(lens) != {"lens_k", "lens_kcube"}:
        raise VerificationError("twin camera model lens geometry is invalid")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in lens.values()
    ):
        raise VerificationError("twin camera model lens geometry is invalid")
    return {
        "camera_id": expected_camera_id,
        "actor_id": actor_id,
        "config_sha256": fingerprint,
        "transform": transform,
        "image": {
            "width": width,
            "height": height,
            "horizontal_fov_deg": float(fov),
        },
        "lens": {key: float(value) for key, value in lens.items()},
    }


def _exact_schema_version(value, expected):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value).is_integer()
        and int(value) == expected
    )


def _validate_trusted_twin_media(item, replay_epoch):
    """Require the persisted schema-v2 provenance used by archive proof."""
    if item.get("media_time_trusted") is not True:
        raise VerificationError("twin object media time is not trusted")
    if not _exact_schema_version(item.get("timestamp_schema_version"), 2):
        raise VerificationError("twin object timestamp schema is not version 2")
    detection_epoch = replay_clock_epoch(item.get("detection_timestamp_utc"))
    media_epoch = replay_clock_epoch(item.get("media_timestamp_utc"))
    if detection_epoch is None or media_epoch is None:
        raise VerificationError("twin object has no valid persisted media timestamp")
    if abs(detection_epoch - media_epoch) > 0.005:
        raise VerificationError("twin object detection/media timestamps disagree")
    clock = item.get("media_clock")
    if not isinstance(clock, dict):
        raise VerificationError("twin object has no persisted media clock")
    if clock.get("source") != "hls_ext_x_program_date_time":
        raise VerificationError("twin object has an untrusted media clock source")
    if not _exact_schema_version(clock.get("schema_version"), 1):
        raise VerificationError("twin object media clock schema is not version 1")
    anchor_epoch = replay_clock_epoch(clock.get("anchor_program_date_time_utc"))
    position_ms = clock.get("position_milliseconds")
    if (
        anchor_epoch is None
        or isinstance(position_ms, bool)
        or not isinstance(position_ms, (int, float))
        or not math.isfinite(float(position_ms))
        or float(position_ms) < 0.0
    ):
        raise VerificationError("twin object has invalid media clock provenance")
    if abs((anchor_epoch + float(position_ms) / 1000.0) - media_epoch) > 0.005:
        raise VerificationError("twin object media clock reconstruction disagrees")
    age = replay_epoch - media_epoch
    if age < -0.25 or age > 15.0:
        raise VerificationError(
            f"twin object media time is {age:.3f}s from the replay clock"
        )
    return media_epoch


def validate_twin_object_sample(
    status,
    object_id,
    world,
    *,
    position_tolerance_m,
    rotation_tolerance_deg,
):
    """Prove one protocol object maps to the same concrete UE5 CARLA actor."""
    if status.get("mode") != "replay":
        raise VerificationError("twin_status object evidence is not in replay mode")
    replay_epoch = replay_clock_epoch(status.get("replay_clock"))
    if replay_epoch is None:
        raise VerificationError("twin_status has no valid replay clock")

    item = _object_from_twin_status(status, object_id)
    if item is None:
        raise VerificationError(f"twin object {object_id!r} is not present")
    if item.get("actor_present") is not True:
        raise VerificationError("twin object does not report a present CARLA actor")
    event_id = str(item.get("event_id") or "").strip()
    if not event_id:
        raise VerificationError("twin object has no persisted event_id")
    media_epoch = _validate_trusted_twin_media(item, replay_epoch)
    actor_id = item.get("actor_id")
    if (
        isinstance(actor_id, bool)
        or not isinstance(actor_id, int)
        or actor_id <= 0
    ):
        raise VerificationError("twin object has no valid CARLA actor_id")

    actor = world.get_actor(actor_id)
    if actor is None:
        raise VerificationError(
            f"mapped UE5 CARLA actor {actor_id} does not exist"
        )
    actual_type = str(getattr(actor, "type_id", ""))
    reported_type = str(item.get("actor_type") or "")
    if not reported_type or reported_type != actual_type:
        raise VerificationError(
            "twin actor type does not match the mapped UE5 CARLA actor"
        )
    object_type = str(item.get("object_type") or "")
    expected_prefix = "walker." if object_type == "person" else "vehicle."
    if object_type not in {"car", "truck", "bus", "person"} or not actual_type.startswith(
        expected_prefix
    ):
        raise VerificationError(
            f"twin object type {object_type!r} is incompatible with {actual_type!r}"
        )
    attributes = getattr(actor, "attributes", None) or {}
    role_name = str(attributes.get("role_name", ""))
    if role_name != "twin_object":
        raise VerificationError(
            f"mapped UE5 CARLA actor has unexpected role {role_name!r}"
        )

    reported = _finite_transform_payload(
        item.get("carla_transform"), "twin_status object"
    )
    observed = _actor_transform_payload(actor)
    reported_location = reported["location"]
    observed_location = observed["location"]
    position_error = math.sqrt(
        sum(
            (reported_location[axis] - observed_location[axis]) ** 2
            for axis in ("x", "y", "z")
        )
    )
    rotation_errors = {
        axis: _angular_error(
            reported["rotation"][axis], observed["rotation"][axis]
        )
        for axis in ("pitch", "yaw", "roll")
    }
    rotation_error = max(rotation_errors.values())
    if position_error > position_tolerance_m or rotation_error > rotation_tolerance_deg:
        raise VerificationError(
            "twin_status transform does not match the mapped UE5 CARLA actor: "
            f"position={position_error:.3f}m rotation={rotation_error:.3f}deg"
        )

    return {
        "sampled_at": utc_iso(),
        "replay_clock": status.get("replay_clock"),
        "replay_clock_epoch": replay_epoch,
        "object_id": object_id,
        "object_type": object_type,
        "event_id": event_id,
        "media_timestamp_utc": item.get("media_timestamp_utc"),
        "media_timestamp_epoch": media_epoch,
        "actor_id": actor_id,
        "actor_type": actual_type,
        "role_name": role_name,
        "reported_transform": reported,
        "observed_transform": observed,
        "position_error_m": round(position_error, 3),
        "rotation_error_deg": round(rotation_error, 3),
    }


def validate_twin_object_samples(
    samples,
    *,
    min_samples=3,
    min_span_seconds=2.0,
    min_movement_m=0.25,
):
    """Require stable identity plus visible motion over distinct replay samples."""
    if len(samples) < min_samples:
        raise VerificationError(
            f"twin object has only {len(samples)} samples; need {min_samples}"
        )
    object_ids = {sample.get("object_id") for sample in samples}
    actor_ids = {sample.get("actor_id") for sample in samples}
    if len(object_ids) != 1 or len(actor_ids) != 1:
        raise VerificationError(
            "twin object samples do not retain one object_id and CARLA actor_id"
        )
    event_ids = [str(sample.get("event_id") or "").strip() for sample in samples]
    if any(not event_id for event_id in event_ids):
        raise VerificationError("twin object samples have missing event IDs")
    media_clocks = [sample.get("media_timestamp_epoch") for sample in samples]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in media_clocks
    ):
        raise VerificationError("twin object samples have invalid media timestamps")
    if any(right < left for left, right in zip(media_clocks, media_clocks[1:])):
        raise VerificationError("twin object media timestamps regressed")
    clocks = [sample.get("replay_clock_epoch") for sample in samples]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in clocks
    ):
        raise VerificationError("twin object samples have invalid replay clocks")
    if any(right <= left for left, right in zip(clocks, clocks[1:])):
        raise VerificationError("twin object sample replay clocks did not advance")
    span = float(clocks[-1] - clocks[0])
    if span < min_span_seconds:
        raise VerificationError(
            f"twin object samples span only {span:.3f}s; need {min_span_seconds:.3f}s"
        )

    locations = [
        [
            sample["reported_transform"]["location"][axis]
            for axis in ("x", "y", "z")
        ]
        for sample in samples
    ]
    displacements = [
        planar_distance(left, right)
        for index, left in enumerate(locations)
        for right in locations[index + 1 :]
    ]
    max_movement = max(displacements, default=0.0)
    path_length = sum(
        planar_distance(left, right)
        for left, right in zip(locations, locations[1:])
    )
    if max_movement < min_movement_m:
        raise VerificationError(
            f"twin object moved only {max_movement:.3f}m; need {min_movement_m:.3f}m"
        )
    visuals = [sample.get("visual") for sample in samples]
    if any(
        not isinstance(visual, dict)
        or not isinstance(visual.get("best_detection"), dict)
        or visual["best_detection"].get("compatible") is not True
        for visual in visuals
    ):
        raise VerificationError("twin object samples lack projected visual proof")
    frame_hashes = [str(visual.get("frame_sha256") or "") for visual in visuals]
    if any(len(value) != 64 for value in frame_hashes):
        raise VerificationError("twin object visual samples have invalid frame hashes")
    if len(set(frame_hashes)) != len(frame_hashes):
        raise VerificationError("twin object visual samples reused a rendered frame")
    return {
        "sample_count": len(samples),
        "object_id": next(iter(object_ids)),
        "actor_id": next(iter(actor_ids)),
        "event_ids": event_ids,
        "media_start": utc_iso(
            datetime.fromtimestamp(media_clocks[0], timezone.utc)
        ),
        "media_end": utc_iso(
            datetime.fromtimestamp(media_clocks[-1], timezone.utc)
        ),
        "replay_span_seconds": round(span, 3),
        "max_planar_movement_m": round(max_movement, 3),
        "planar_path_length_m": round(path_length, 3),
        "visual_frame_sha256": frame_hashes,
    }


def actor_snapshot(world, actor_ids):
    snapshots = {}
    for actor_id in actor_ids:
        actor = world.get_actor(int(actor_id))
        if actor is None:
            continue
        transform = actor.get_transform()
        snapshots[int(actor_id)] = {
            "role_name": actor.attributes.get("role_name", ""),
            "location": [
                float(transform.location.x),
                float(transform.location.y),
                float(transform.location.z),
            ],
            "yaw": float(transform.rotation.yaw),
        }
    return snapshots


def planar_distance(left, right):
    return math.hypot(left[0] - right[0], left[1] - right[1])


def teleport_pose_errors(position, yaw, target_position, target_yaw):
    """Return planar/yaw errors while rejecting malformed protocol poses."""
    try:
        values = [
            float(position[0]),
            float(position[1]),
            float(yaw),
            float(target_position[0]),
            float(target_position[1]),
            float(target_yaw),
        ]
    except (IndexError, TypeError, ValueError) as exc:
        raise VerificationError("Teleport response has no valid pose") from exc
    if not all(math.isfinite(value) for value in values):
        raise VerificationError("Teleport response has no valid pose")
    position_error = math.hypot(values[0] - values[3], values[1] - values[4])
    yaw_error = abs(((values[2] - values[5] + 180.0) % 360.0) - 180.0)
    return position_error, yaw_error


def validate_isolated_ego_roles(roles):
    """Require the per-session role prefix emitted by DriveSession."""
    if len(roles) != 2 or len(set(roles)) != 2:
        raise VerificationError(f"session ego roles are not isolated: {roles}")
    if not all(role.startswith("ego_vehicle_") for role in roles):
        raise VerificationError(f"session ego roles are not isolated: {roles}")


def choose_teleport_target(carla_map, occupied_locations):
    spawn_points = carla_map.get_spawn_points()
    if not spawn_points:
        raise VerificationError("CARLA map has no spawn points")

    def clearance(transform):
        point = [float(transform.location.x), float(transform.location.y)]
        return min(planar_distance(point, occupied[:2]) for occupied in occupied_locations)

    target = max(spawn_points, key=clearance)
    if clearance(target) < 20.0:
        raise VerificationError("no teleport target is safely separated by 20 metres")
    return target


async def observational_probe(args):
    evidence = {
        "mode": "observational",
        "checked_at": utc_iso(),
        "ws_url": args.ws_url,
    }
    async with websockets.connect(
        args.ws_url, open_timeout=args.timeout, max_size=args.max_message_bytes
    ) as websocket:
        evidence["server_status"] = await request_json(
            websocket,
            {"type": "server_status"},
            {"server_status"},
            args.timeout,
        )

    if not args.skip_twin:
        twin_url = websocket_url(args.ws_url, "/twin", "control=1")
        async with websockets.connect(
            twin_url, open_timeout=args.timeout, max_size=args.max_message_bytes
        ) as websocket:
            evidence["twin_hello"] = await receive_json(
                websocket, {"twin_hello"}, args.timeout
            )
            evidence["twin_status"] = await request_json(
                websocket,
                {"type": "twin_status"},
                {"twin_mode", "twin_error"},
                args.timeout,
            )
    return evidence


def validate_zero_active_sessions(status):
    active_sessions = status.get("active_sessions")
    if (
        isinstance(active_sessions, bool)
        or not isinstance(active_sessions, int)
        or active_sessions < 0
    ):
        raise VerificationError("server_status has no valid active_sessions count")
    if active_sessions != 0:
        raise VerificationError(
            "refusing mutation while another Drive session is active"
        )
    return status


async def verify_zero_active_sessions(args):
    """Read-only mutation preflight; never sends start_session."""
    async with websockets.connect(
        args.ws_url,
        open_timeout=args.timeout,
        max_size=args.max_message_bytes,
    ) as websocket:
        status = await request_json(
            websocket,
            {"type": "server_status"},
            {"server_status"},
            args.timeout,
        )
    return validate_zero_active_sessions(status)


async def collect_twin_object_samples(
    args,
    control_socket,
    stream_socket,
    world,
    camera_model,
    detector,
    evidence,
):
    """Collect three independently timestamped exact-object status samples."""
    samples = []
    deadline = asyncio.get_running_loop().time() + args.timeout
    next_sample_clock = None
    while len(samples) < 3:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise VerificationError(
                f"timed out collecting twin object {args.twin_object_id!r} samples"
            )
        # RR/CARLA can leave a separately connected client on frame zero (or
        # an older actor snapshot) until it consumes a real world tick.  The
        # Drive process reports the live actor transform from its own client,
        # so synchronize this verifier client immediately before comparing
        # the two views.  This is read-only and preserves the exact transform
        # and movement tolerances below.
        sync_frame = synchronize_world(world, min(1.0, remaining))
        evidence.setdefault("object_sync_frames", []).append(sync_frame)
        status = await request_json(
            control_socket,
            {"type": "twin_status"},
            {"twin_mode", "twin_error"},
            min(args.timeout, remaining),
            evidence,
        )
        if status.get("type") == "twin_error":
            raise VerificationError(status.get("message", "twin status failed"))
        item = _object_from_twin_status(status, args.twin_object_id)
        if item is None:
            await asyncio.sleep(min(0.2, max(0.0, remaining)))
            continue
        replay_epoch = replay_clock_epoch(status.get("replay_clock"))
        if replay_epoch is None:
            raise VerificationError("twin_status has no valid replay clock")
        if next_sample_clock is not None and replay_epoch < next_sample_clock:
            await asyncio.sleep(min(0.1, max(0.0, remaining)))
            continue
        sample = validate_twin_object_sample(
            status,
            args.twin_object_id,
            world,
            position_tolerance_m=args.position_tolerance_m,
            rotation_tolerance_deg=args.yaw_tolerance_deg,
        )
        actor = world.get_actor(sample["actor_id"])
        projected_bbox = project_actor_bbox(actor, camera_model)
        jpeg, jpeg_digest = await receive_binary_payload(
            stream_socket, min(args.timeout, remaining), evidence=evidence
        )
        detections = detect_twin_objects(
            jpeg,
            detector,
            confidence=args.twin_yolo_confidence,
            device=args.twin_yolo_device,
        )
        sample["visual"] = {
            "frame_sha256": jpeg_digest,
            **validate_projected_actor_detection(
                projected_bbox,
                detections,
                sample["object_type"],
                minimum_iou=args.twin_min_iou,
                minimum_actor_coverage=args.twin_min_actor_coverage,
            ),
        }
        samples.append(sample)
        next_sample_clock = replay_epoch + 1.0

    summary = validate_twin_object_samples(samples)
    return {"samples": samples, **summary}


async def verify_twin(args, world=None):
    evidence = {"binary_frames": 0, "json_types": []}
    detector = None
    if args.twin_object_id:
        detector = {
            "python": args.twin_yolo_python,
            "script": args.twin_yolo_detector,
            "model": args.twin_yolo_model,
        }
        evidence["twin_yolo_model_sha256"] = file_sha256(args.twin_yolo_model)
        evidence["twin_yolo_detector_sha256"] = file_sha256(args.twin_yolo_detector)
    control_url = websocket_url(args.ws_url, "/twin", "control=1")
    stream_url = websocket_url(
        args.ws_url, "/twin", f"cam={args.twin_camera}"
    )

    async with websockets.connect(
        stream_url, open_timeout=args.timeout, max_size=args.max_message_bytes
    ) as stream_socket:
        evidence["stream_hello"] = await receive_json(
            stream_socket, {"twin_hello", "twin_error"}, args.timeout, evidence
        )
        if evidence["stream_hello"]["type"] == "twin_error":
            raise VerificationError(evidence["stream_hello"].get("message", "twin error"))
        evidence["validated_camera_model"] = validate_twin_camera_model(
            evidence["stream_hello"], args.twin_camera
        )
        live_digest = await receive_binary_frame(
            stream_socket, args.timeout, evidence=evidence
        )
        evidence["live_frame_sha256"] = live_digest

        async with websockets.connect(
            control_url, open_timeout=args.timeout, max_size=args.max_message_bytes
        ) as control_socket:
            evidence["control_hello"] = await receive_json(
                control_socket, {"twin_hello"}, args.timeout, evidence
            )
            initial = await request_json(
                control_socket,
                {"type": "twin_status"},
                {"twin_mode", "twin_error"},
                args.timeout,
                evidence,
            )
            evidence["initial_mode"] = initial
            if initial["type"] == "twin_error":
                raise VerificationError(initial.get("message", "twin status failed"))
            if not initial.get("replay_supported"):
                raise VerificationError("twin replay is not supported by the live server")
            if initial.get("mode") != "live":
                raise VerificationError(
                    f"refusing to disturb pre-existing twin mode {initial.get('mode')!r}"
                )

            restored = False
            try:
                replay_start = args.twin_replay_start or utc_iso(
                    datetime.now(timezone.utc)
                    - timedelta(seconds=args.replay_age_seconds)
                )
                evidence["replay_start"] = replay_start
                replay = await request_json(
                    control_socket,
                    {"type": "twin_replay", "start": replay_start, "speed": 1.0},
                    {"twin_mode", "twin_error"},
                    args.timeout,
                    evidence,
                )
                evidence["replay_mode"] = replay
                if replay.get("mode") != "replay":
                    raise VerificationError(f"twin did not enter replay mode: {replay}")

                replay_digest = await receive_binary_frame(
                    stream_socket,
                    args.timeout,
                    evidence=evidence,
                    different_from=live_digest,
                )
                evidence["replay_frame_sha256"] = replay_digest
                evidence["replay_frame_changed"] = True

                if args.twin_object_id:
                    if world is None:
                        raise VerificationError(
                            "exact twin object verification requires a CARLA world"
                        )
                    evidence["object_correlation"] = (
                        await collect_twin_object_samples(
                            args,
                            control_socket,
                            stream_socket,
                            world,
                            evidence["validated_camera_model"],
                            detector,
                            evidence,
                        )
                    )

                first_clock = None
                second_clock = None
                deadline = asyncio.get_running_loop().time() + args.timeout
                while second_clock is None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise VerificationError("twin replay clock did not advance")
                    clock = await receive_json(
                        control_socket, {"twin_clock"}, remaining, evidence
                    )
                    value = replay_clock_epoch(clock.get("replay_clock"))
                    if value is None:
                        continue
                    if first_clock is None:
                        first_clock = value
                    elif value > first_clock:
                        second_clock = value
                evidence["replay_clock_delta_seconds"] = round(
                    second_clock - first_clock, 3
                )
            finally:
                live = await request_json(
                    control_socket,
                    {"type": "twin_live"},
                    {"twin_mode", "twin_error"},
                    args.timeout,
                    evidence,
                )
                evidence["restored_mode"] = live
                restored = live.get("mode") == "live"
            if not restored:
                raise VerificationError("failed to restore twin live mode")
    return evidence


async def verify_drive_sessions(args, world, carla_map):
    baseline_snapshot_frame = synchronize_world(world, args.timeout)
    baseline_actor_inventory = world_actor_inventory(world)
    baseline_actor_ids = set(baseline_actor_inventory)
    evidence = {
        "binary_frames": 0,
        "json_types": [],
        "started_actor_ids": [],
        "started_owned_actor_ids": [],
        "session_actor_manifests": [],
        "actor_snapshot_frames": [baseline_snapshot_frame],
        "baseline_actor_ids": sorted(baseline_actor_ids),
        "ignored_non_session_actor_ids": [],
    }
    sockets = []
    started = []
    observed_created_actor_ids = set()
    ignored_non_session_actor_ids = set()
    try:
        for _ in range(2):
            socket = await websockets.connect(
                args.ws_url,
                open_timeout=args.timeout,
                max_size=args.max_message_bytes,
            )
            sockets.append(socket)

        pre_status = await request_json(
            sockets[0],
            {"type": "server_status"},
            {"server_status"},
            args.timeout,
            evidence,
        )
        evidence["pre_status"] = pre_status
        validate_zero_active_sessions(pre_status)

        now = datetime.now(timezone.utc)
        start = args.start or utc_iso(now - timedelta(hours=1))
        end = args.end or utc_iso(now)
        for socket in sockets:
            response = await request_json(
                socket,
                {
                    "type": "start_session",
                    "start": start,
                    "end": end,
                    "vehicle": args.vehicle,
                },
                {"session_ready", "error"},
                args.session_start_timeout,
                evidence,
            )
            if response.get("type") != "session_ready":
                raise VerificationError(f"Drive session failed to start: {response}")
            evidence["actor_snapshot_frames"].append(
                synchronize_world(world, args.timeout)
            )
            current_actor_inventory = world_actor_inventory(world)
            current_actor_ids = set(current_actor_inventory)
            observed_created_actor_ids.update(current_actor_ids - baseline_actor_ids)
            prior_owned = set().union(
                *(previous_owned for _, _, previous_owned in started)
            ) if started else set()
            actor_id, owned_ids, sensor_ids, scene_ids = validate_session_actor_manifest(
                response,
                baseline_actor_ids=baseline_actor_ids,
                current_actor_ids=current_actor_ids,
                prior_owned_actor_ids=prior_owned,
            )
            ignored_non_session_actor_ids.update(
                validate_actor_delta(
                    current_actor_ids - baseline_actor_ids,
                    prior_owned | owned_ids,
                    current_actor_inventory,
                )
            )
            started.append((socket, actor_id, owned_ids))
            evidence["started_actor_ids"].append(actor_id)
            evidence["started_owned_actor_ids"].extend(sorted(owned_ids))
            evidence["session_actor_manifests"].append({
                "vehicle_id": actor_id,
                "sensor_actor_ids": sorted(sensor_ids),
                "scene_actor_ids": sorted(scene_ids),
                "owned_actor_ids": sorted(owned_ids),
            })

        evidence["actor_snapshot_frames"].append(
            synchronize_world(world, args.timeout)
        )
        post_start_actor_inventory = world_actor_inventory(world)
        post_start_actor_ids = set(post_start_actor_inventory)
        created_actor_ids = post_start_actor_ids - baseline_actor_ids
        observed_created_actor_ids.update(created_actor_ids)
        declared_actor_ids = set().union(
            *(owned_ids for _, _, owned_ids in started)
        )
        evidence["post_start_actor_ids"] = sorted(post_start_actor_ids)
        evidence["created_actor_ids"] = sorted(created_actor_ids)
        ignored_non_session_actor_ids.update(
            validate_actor_delta(
                created_actor_ids,
                declared_actor_ids,
                post_start_actor_inventory,
            )
        )
        evidence["ignored_non_session_actor_ids"] = sorted(
            ignored_non_session_actor_ids
        )

        actor_ids = [actor_id for _, actor_id, _ in started]
        before = actor_snapshot(world, actor_ids)
        if set(before) != set(actor_ids):
            raise VerificationError("one or more session ego actors are missing")
        roles = [before[actor_id]["role_name"] for actor_id in actor_ids]
        validate_isolated_ego_roles(roles)
        evidence["before"] = before

        occupied_locations = []
        for actor in world.get_actors().filter("vehicle.*"):
            location = actor.get_location()
            occupied_locations.append(
                [float(location.x), float(location.y), float(location.z)]
            )
        target = choose_teleport_target(carla_map, occupied_locations)
        request_id = f"phase4-{uuid.uuid4()}"
        acknowledgement = await request_json(
            sockets[0],
            {
                "type": "teleport",
                "request_id": request_id,
                "x": float(target.location.x),
                "y": float(target.location.y),
                "yaw": float(target.rotation.yaw),
            },
            {"teleported", "teleport_error"},
            args.timeout,
            evidence,
        )
        evidence["teleport_ack"] = acknowledgement
        if acknowledgement.get("type") != "teleported":
            raise VerificationError(f"Teleport failed: {acknowledgement}")
        if acknowledgement.get("request_id") != request_id:
            raise VerificationError("Teleport acknowledgement correlation mismatch")

        await asyncio.sleep(args.settle_seconds)
        ack_pos = acknowledgement.get("pos")
        if not isinstance(ack_pos, list) or len(ack_pos) != 3:
            raise VerificationError("Teleport acknowledgement has no valid position")
        target_position = [float(target.location.x), float(target.location.y)]
        target_yaw = float(target.rotation.yaw)
        ack_target_error, ack_target_yaw_error = teleport_pose_errors(
            ack_pos,
            acknowledgement.get("yaw"),
            target_position,
            target_yaw,
        )
        evidence["teleport_ack_target_error_m"] = round(ack_target_error, 3)
        evidence["teleport_ack_target_yaw_error_deg"] = round(
            ack_target_yaw_error, 3
        )
        if (
            ack_target_error > args.position_tolerance_m
            or ack_target_yaw_error > args.yaw_tolerance_deg
        ):
            raise VerificationError(
                "Teleport acknowledgement does not match the requested target"
            )

        # A separate RR/CARLA client can briefly observe a pre-Teleport actor
        # snapshot even after the bridge has acknowledged set_transform().
        # Consume bounded world ticks until this client's view converges.
        deadline = time.monotonic() + args.timeout
        after = {}
        a_error = math.inf
        a_yaw_error = math.inf
        poll_count = 0
        last_frame = None
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            last_frame = synchronize_world(world, min(1.0, remaining))
            poll_count += 1
            after = actor_snapshot(world, actor_ids)
            if set(after) != set(actor_ids):
                continue
            a_error, a_yaw_error = teleport_pose_errors(
                after[actor_ids[0]]["location"],
                after[actor_ids[0]]["yaw"],
                target_position,
                target_yaw,
            )
            if (
                a_error <= args.position_tolerance_m
                and a_yaw_error <= args.yaw_tolerance_deg
            ):
                break
        evidence["post_teleport_poll_count"] = poll_count
        evidence["actor_snapshot_frames"].append(last_frame)
        evidence["after"] = after
        if set(after) != set(actor_ids):
            raise VerificationError("session actor disappeared during Teleport")
        ack_observed_error, ack_observed_yaw_error = teleport_pose_errors(
            after[actor_ids[0]]["location"],
            after[actor_ids[0]]["yaw"],
            ack_pos,
            acknowledgement.get("yaw"),
        )
        b_displacement = planar_distance(
            after[actor_ids[1]]["location"], before[actor_ids[1]]["location"]
        )
        evidence["teleport_position_error_m"] = round(a_error, 3)
        evidence["teleport_yaw_error_deg"] = round(a_yaw_error, 3)
        evidence["teleport_ack_observed_error_m"] = round(
            ack_observed_error, 3
        )
        evidence["teleport_ack_observed_yaw_error_deg"] = round(
            ack_observed_yaw_error, 3
        )
        evidence["isolated_session_displacement_m"] = round(b_displacement, 3)
        if (
            a_error > args.position_tolerance_m
            or a_yaw_error > args.yaw_tolerance_deg
            or ack_observed_error > args.position_tolerance_m
            or ack_observed_yaw_error > args.yaw_tolerance_deg
        ):
            raise VerificationError(
                f"Teleported ego pose did not converge: position={a_error:.3f}m "
                f"yaw={a_yaw_error:.3f}deg "
                f"ack={ack_pos[:2]} observed={after[actor_ids[0]]['location'][:2]} "
                f"after {poll_count} world ticks"
            )
        if b_displacement > args.isolation_tolerance_m:
            raise VerificationError(
                f"other session moved {b_displacement:.3f} m during Teleport"
            )

        for socket, _, _ in started:
            ended = await request_json(
                socket,
                {"type": "end_session"},
                {"session_ended", "error"},
                args.timeout,
                evidence,
            )
            if ended.get("type") != "session_ended":
                raise VerificationError(f"session cleanup failed: {ended}")
        started.clear()
    finally:
        for socket, _, _ in list(started):
            try:
                await request_json(
                    socket,
                    {"type": "end_session"},
                    {"session_ended", "error"},
                    min(args.timeout, 10.0),
                )
            except Exception:
                pass
        for socket in sockets:
            try:
                await socket.close()
            except Exception:
                pass

        final_actor_inventory = world_actor_inventory(world)
        observed_created_actor_ids.update(
            set(final_actor_inventory) - baseline_actor_ids
        )
        if observed_created_actor_ids:
            deadline = time.monotonic() + args.cleanup_timeout
            remaining_ids = session_candidate_actor_ids(
                observed_created_actor_ids & set(final_actor_inventory),
                final_actor_inventory,
            )
            while time.monotonic() < deadline and remaining_ids:
                await asyncio.sleep(0.2)
                final_actor_inventory = world_actor_inventory(world)
                observed_created_actor_ids.update(
                    set(final_actor_inventory) - baseline_actor_ids
                )
                remaining_ids = session_candidate_actor_ids(
                    observed_created_actor_ids & set(final_actor_inventory),
                    final_actor_inventory,
                )
            final_actor_ids = set(final_actor_inventory)
            evidence["final_actor_ids"] = sorted(final_actor_ids)
            evidence["remaining_created_actor_ids"] = sorted(remaining_ids)
            if remaining_ids:
                raise VerificationError(
                    "actors created after the Phase 4 baseline remain after cleanup: "
                    f"{sorted(remaining_ids)}"
                )
    return evidence


async def apply_probe(args, carla_module=None):
    if carla_module is None:
        try:
            import carla as carla_module
        except ImportError as exc:
            raise VerificationError(
                "--apply requires the CARLA Python environment"
            ) from exc

    client = carla_module.Client(args.carla_host, args.carla_port)
    client.set_timeout(args.timeout)
    world = client.get_world()
    result = {
        "mode": "apply",
        "checked_at": utc_iso(),
        "map": world.get_map().name,
        "preflight_server_status": await verify_zero_active_sessions(args),
    }
    if args.skip_drive:
        result["drive"] = {
            "skipped": True,
            "reason": (
                "--skip-drive requested; no start_session request or "
                "Drive-owned CARLA actor was created"
            ),
        }
    else:
        result["drive"] = await verify_drive_sessions(
            args, world, world.get_map()
        )
    if not args.skip_twin:
        result["pre_twin_server_status"] = await verify_zero_active_sessions(args)
        result["twin"] = await verify_twin(args, world=world)
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765")
    parser.add_argument("--carla-host", default="127.0.0.1")
    parser.add_argument("--carla-port", type=int, default=2000)
    parser.add_argument("--vehicle", default="vehicle.tesla.model3")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--session-start-timeout", type=float, default=120.0)
    parser.add_argument("--cleanup-timeout", type=float, default=15.0)
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--position-tolerance-m", type=float, default=2.0)
    parser.add_argument("--yaw-tolerance-deg", type=float, default=3.0)
    parser.add_argument("--isolation-tolerance-m", type=float, default=3.0)
    parser.add_argument("--replay-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-message-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--skip-twin", action="store_true")
    parser.add_argument(
        "--skip-drive",
        action="store_true",
        help="do not create Drive sessions; still require zero active sessions",
    )
    parser.add_argument(
        "--twin-object-id",
        help="require exact replay object-to-CARLA actor motion evidence",
    )
    parser.add_argument(
        "--twin-replay-start",
        help="exact ISO replay start; defaults to --replay-age-seconds ago",
    )
    parser.add_argument(
        "--twin-camera",
        choices=("ch1", "ch2", "ch3", "ch4"),
        default="ch1",
        help="twin camera whose replay frames must visibly change",
    )
    parser.add_argument(
        "--twin-yolo-model",
        type=Path,
        help="local YOLO weights required for exact projected-actor visual proof",
    )
    parser.add_argument(
        "--twin-yolo-python",
        type=Path,
        default=DEFAULT_TWIN_YOLO_PYTHON,
        help="Python executable containing the pinned perception dependencies",
    )
    parser.add_argument(
        "--twin-yolo-detector",
        type=Path,
        default=DEFAULT_TWIN_YOLO_DETECTOR,
        help="tracked stdin-only JPEG detection helper",
    )
    parser.add_argument("--twin-yolo-device", default="cpu")
    parser.add_argument("--twin-yolo-confidence", type=float, default=0.25)
    parser.add_argument("--twin-min-iou", type=float, default=0.15)
    parser.add_argument("--twin-min-actor-coverage", type=float, default=0.50)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "exercise bounded acceptance; creates two Drive sessions unless "
            "--skip-drive is set; default is read-only"
        ),
    )
    return parser


def validate_cli_args(args):
    if args.skip_drive and not args.apply:
        raise VerificationError("--skip-drive is meaningful only with --apply")
    if args.twin_replay_start and not args.apply:
        raise VerificationError("--twin-replay-start requires --apply")
    if args.twin_replay_start and replay_clock_epoch(args.twin_replay_start) is None:
        raise VerificationError("--twin-replay-start must be a valid ISO timestamp")
    if args.twin_object_id is not None:
        args.twin_object_id = args.twin_object_id.strip()
        if not args.twin_object_id:
            raise VerificationError("--twin-object-id must not be blank")
        if not args.apply:
            raise VerificationError("--twin-object-id requires --apply")
        if args.skip_twin:
            raise VerificationError("--twin-object-id cannot be used with --skip-twin")
        if args.twin_yolo_model is None or not args.twin_yolo_model.is_file():
            raise VerificationError(
                "--twin-object-id requires an existing --twin-yolo-model"
            )
        if not args.twin_yolo_python.is_file():
            raise VerificationError("--twin-yolo-python does not exist")
        if not args.twin_yolo_detector.is_file():
            raise VerificationError("--twin-yolo-detector does not exist")
    elif args.twin_yolo_model is not None:
        raise VerificationError("--twin-yolo-model requires --twin-object-id")
    for value, label in (
        (args.twin_yolo_confidence, "--twin-yolo-confidence"),
        (args.twin_min_iou, "--twin-min-iou"),
        (args.twin_min_actor_coverage, "--twin-min-actor-coverage"),
    ):
        if not math.isfinite(value) or not 0.0 < value <= 1.0:
            raise VerificationError(f"{label} must be in (0, 1]")
    return args


def main():
    try:
        args = validate_cli_args(build_parser().parse_args())
        result = asyncio.run(apply_probe(args) if args.apply else observational_probe(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, "evidence": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

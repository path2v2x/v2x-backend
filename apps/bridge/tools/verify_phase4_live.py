#!/usr/bin/env python3
"""Bounded live verification for Drive session isolation and twin replay.

Without ``--apply`` this tool is observational: it reads server and twin status.
Mutation is explicit, refuses to run while another Drive session exists, and
always closes/ends sessions in ``finally`` so CARLA-owned actors are cleaned up.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import websockets


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


def world_actor_ids(world):
    """Snapshot every current CARLA actor ID, not only declared ownership."""
    return {
        int(actor.id)
        for actor in world.get_actors()
        if isinstance(getattr(actor, "id", None), int)
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


def validate_actor_delta(created_actor_ids, declared_actor_ids):
    """Require the complete post-baseline CARLA delta to be declared."""
    undeclared = set(created_actor_ids) - set(declared_actor_ids)
    missing_from_world = set(declared_actor_ids) - set(created_actor_ids)
    if undeclared or missing_from_world:
        raise VerificationError(
            "CARLA actor delta does not exactly match session manifests: "
            f"undeclared={sorted(undeclared)} missing={sorted(missing_from_world)}"
        )


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


async def verify_twin(args):
    evidence = {"binary_frames": 0, "json_types": []}
    control_url = websocket_url(args.ws_url, "/twin", "control=1")
    stream_url = websocket_url(args.ws_url, "/twin", "cam=ch1")

    async with websockets.connect(
        stream_url, open_timeout=args.timeout, max_size=args.max_message_bytes
    ) as stream_socket:
        evidence["stream_hello"] = await receive_json(
            stream_socket, {"twin_hello", "twin_error"}, args.timeout, evidence
        )
        if evidence["stream_hello"]["type"] == "twin_error":
            raise VerificationError(evidence["stream_hello"].get("message", "twin error"))
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
                replay_start = utc_iso(
                    datetime.now(timezone.utc) - timedelta(seconds=args.replay_age_seconds)
                )
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
    baseline_actor_ids = world_actor_ids(world)
    evidence = {
        "binary_frames": 0,
        "json_types": [],
        "started_actor_ids": [],
        "started_owned_actor_ids": [],
        "session_actor_manifests": [],
        "baseline_actor_ids": sorted(baseline_actor_ids),
    }
    sockets = []
    started = []
    observed_created_actor_ids = set()
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
        if pre_status.get("active_sessions") != 0:
            raise VerificationError(
                "refusing mutation while another Drive session is active"
            )

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
            current_actor_ids = world_actor_ids(world)
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
            validate_actor_delta(
                current_actor_ids - baseline_actor_ids,
                prior_owned | owned_ids,
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

        post_start_actor_ids = world_actor_ids(world)
        created_actor_ids = post_start_actor_ids - baseline_actor_ids
        observed_created_actor_ids.update(created_actor_ids)
        declared_actor_ids = set().union(
            *(owned_ids for _, _, owned_ids in started)
        )
        evidence["post_start_actor_ids"] = sorted(post_start_actor_ids)
        evidence["created_actor_ids"] = sorted(created_actor_ids)
        validate_actor_delta(created_actor_ids, declared_actor_ids)

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
        after = actor_snapshot(world, actor_ids)
        evidence["after"] = after
        if set(after) != set(actor_ids):
            raise VerificationError("session actor disappeared during Teleport")
        ack_pos = acknowledgement.get("pos")
        if not isinstance(ack_pos, list) or len(ack_pos) != 3:
            raise VerificationError("Teleport acknowledgement has no valid position")
        a_error = planar_distance(after[actor_ids[0]]["location"], ack_pos)
        b_displacement = planar_distance(
            after[actor_ids[1]]["location"], before[actor_ids[1]]["location"]
        )
        evidence["teleport_position_error_m"] = round(a_error, 3)
        evidence["isolated_session_displacement_m"] = round(b_displacement, 3)
        if a_error > args.position_tolerance_m:
            raise VerificationError(f"Teleported ego position error is {a_error:.3f} m")
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

        observed_created_actor_ids.update(world_actor_ids(world) - baseline_actor_ids)
        if observed_created_actor_ids:
            deadline = time.monotonic() + args.cleanup_timeout
            while (
                time.monotonic() < deadline
                and observed_created_actor_ids & world_actor_ids(world)
            ):
                await asyncio.sleep(0.2)
            final_actor_ids = world_actor_ids(world)
            remaining_ids = observed_created_actor_ids & final_actor_ids
            evidence["final_actor_ids"] = sorted(final_actor_ids)
            evidence["remaining_created_actor_ids"] = sorted(remaining_ids)
            if remaining_ids:
                raise VerificationError(
                    "actors created after the Phase 4 baseline remain after cleanup: "
                    f"{sorted(remaining_ids)}"
                )
    return evidence


async def apply_probe(args):
    try:
        import carla
    except ImportError as exc:
        raise VerificationError(
            "--apply requires the CARLA Python environment"
        ) from exc

    client = carla.Client(args.carla_host, args.carla_port)
    client.set_timeout(args.timeout)
    world = client.get_world()
    result = {
        "mode": "apply",
        "checked_at": utc_iso(),
        "map": world.get_map().name,
        "drive": await verify_drive_sessions(args, world, world.get_map()),
    }
    if not args.skip_twin:
        result["twin"] = await verify_twin(args)
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
    parser.add_argument("--isolation-tolerance-m", type=float, default=3.0)
    parser.add_argument("--replay-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-message-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--skip-twin", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create two bounded Drive sessions and exercise replay; default is read-only",
    )
    return parser


def main():
    args = build_parser().parse_args()
    try:
        result = asyncio.run(apply_probe(args) if args.apply else observational_probe(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, "evidence": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Render one proposal-only calibration candidate with a temporary UE5 camera."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    camera_with_twin_pose,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


async def query_server_status(url):
    async with websockets.connect(url, open_timeout=5.0, close_timeout=2.0) as socket:
        await socket.send(json.dumps({"type": "server_status"}))
        while True:
            message = await asyncio.wait_for(socket.recv(), timeout=5.0)
            if isinstance(message, str):
                payload = json.loads(message)
                if payload.get("type") == "server_status":
                    return payload


def verify_zero_active_sessions(url):
    status = asyncio.run(query_server_status(url))
    if status.get("active_sessions") != 0:
        raise RuntimeError(
            f"refusing candidate render with active_sessions={status.get('active_sessions')}"
        )


def wait_for_image(frames, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frames:
            return frames[-1]
        time.sleep(0.05)
    raise RuntimeError("temporary candidate camera produced no frame")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--report", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    # The live UE5 worker reliably accepts one extra 640x480 RGB sensor but
    # killed the same temporary actor at 1280x960 while the four production
    # sensors were active.  Keep the safe diagnostic default small.
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--drive-ws-url", default="ws://127.0.0.1:8765")
    parser.add_argument(
        "--authorized-zero-session",
        action="store_true",
        help="required acknowledgement for temporary CARLA sensor mutation",
    )
    args = parser.parse_args()

    if not args.authorized_zero_session:
        parser.error("--authorized-zero-session is required")
    report_path = Path(args.report).resolve()
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    if report.get("schema") != "v2x-diagnostic-visual-calibration/v1" or report.get("acceptance_eligible") is not False:
        raise SystemExit("refusing a report without the diagnostic contract")
    result = report.get("cameras", {}).get(args.camera, {})
    if result.get("candidate_recommendation") != "continue_offline_render_review":
        raise SystemExit("candidate is not eligible for offline render review")

    config_path = Path(args.cameras_json).resolve()
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != report.get("cameras_json_sha256"):
        raise SystemExit("cameras JSON does not match the fitted report")
    config = load_cameras_config(str(config_path))
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    candidate = camera_with_twin_pose(camera, result["candidate_twin_pose"])
    output = Path(args.output).resolve()
    if output.exists() or output.with_suffix(output.suffix + ".json").exists():
        raise SystemExit("refusing to overwrite an existing candidate render")
    verify_zero_active_sessions(args.drive_ws_url)

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    transform = compute_twin_camera_transform(world.get_map(), config["site"], candidate)
    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
    configure_twin_camera_blueprint(blueprint, candidate, args.width, args.height)
    frames, actor = [], None
    destroyed = False
    try:
        actor = world.spawn_actor(blueprint, transform)
        actor.listen(frames.append)
        frame = wait_for_image(frames)
        bgra = np.frombuffer(frame.raw_data, dtype=np.uint8).reshape(
            frame.height, frame.width, 4
        )
        rgb = bgra[:, :, :3][:, :, ::-1]
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(output, quality=94)
    finally:
        if actor is not None:
            try:
                actor.stop()
            except Exception:
                pass
            if actor.is_alive:
                result_destroy = actor.destroy()
                destroyed = result_destroy is not False and not actor.is_alive
            else:
                destroyed = True
    if not destroyed:
        raise RuntimeError("temporary candidate camera cleanup could not be verified")
    verify_zero_active_sessions(args.drive_ws_url)

    frame_bytes = output.read_bytes()
    metadata = {
        "schema": "v2x-diagnostic-candidate-render/v1",
        "acceptance_eligible": False,
        "camera": args.camera,
        "created_at_utc": utc_now(),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "cameras_json_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "render_sha256": hashlib.sha256(frame_bytes).hexdigest(),
        "carla_frame": int(frame.frame),
        "sensor_timestamp": float(frame.timestamp),
        "candidate_twin_pose": result["candidate_twin_pose"],
        "temporary_actor_destroyed": destroyed,
    }
    metadata_path = output.with_suffix(output.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print(metadata_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

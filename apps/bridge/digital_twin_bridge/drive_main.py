"""
Unified entry point for the Digital Twin Bridge.

Combines the drive server (WebSocket vehicle control + MJPEG streaming)
with V2X observation (object spawning, state publishing, map export).

Run with:  python -m digital_twin_bridge.drive_main

Architecture:

  ┌─────────────────────────────────────────────┐
  │  Unified Server (asyncio)                   │
  │                                             │
  │  CARLA tick loop ─── 20 Hz physics          │
  │  WebSocket server ── per-client drive sess  │
  │  V2X snapshot ────── props spawned at boot  │
  │  State publisher ─── state.json → S3        │
  │  Map exporter ────── road network → S3      │
  │  Actor audit ─────── orphan cleanup / 60s   │
  └─────────────────────────────────────────────┘
"""

import asyncio
import logging
import os
import sys
import time

import requests
import websockets

from digital_twin_bridge.config import Config
from digital_twin_bridge.carla_connection import CarlaConnection, drive_map_status
from digital_twin_bridge.drive_server import serve_drive, _active_sessions, active_session_count
from digital_twin_bridge.trajectory_player import TrajectoryPlayer
from digital_twin_bridge.openscenario_runner import OpenScenarioRunner

logger = logging.getLogger(__name__)


# ── V2X snapshot ────────────────────────────────────────────────────


def fetch_v2x_snapshot(config: Config) -> list[dict]:
    """Fetch current V2X detections from the read API (one-shot).

    The V2X data is treated as static — fetched once at startup and
    spawned as CARLA props. No continuous polling.
    """
    try:
        resp = requests.get(
            config.V2X_API_URL,
            params={"limit": config.V2X_LIMIT},
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        logger.info("Fetched %d V2X detections", len(items))
        return items
    except Exception as e:
        logger.warning("Failed to fetch V2X detections: %s", e)
        return []


# ── State publisher ─────────────────────────────────────────────────


async def state_publisher(config, registry, health, uplink, interval=5.0):
    """Periodically publish state.json to S3 so the dashboard stays live."""
    loop = asyncio.get_running_loop()

    while True:
        try:
            status = health.get_status()
            state_objects = []
            for obj in registry.get_all():
                state_objects.append({
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "lat": obj.lat,
                    "lon": obj.lon,
                    "confidence": obj.confidence,
                    "street_name": obj.street_name,
                    "timestamp_utc": obj.timestamp_utc,
                    "snapshot_url": getattr(obj, "snapshot_url", None),
                    "snapshot_timestamp": getattr(obj, "snapshot_timestamp", None),
                    "last_updated": int(time.time() * 1000),
                })
            bridge_status = {
                "status": "connected",
                "carla_fps": status.get("effective_fps", 0),
                "objects_tracked": registry.count,
                "cameras_active": 0,
                "last_heartbeat": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            }
            await loop.run_in_executor(
                None, uplink.publish_state, state_objects, bridge_status
            )
        except Exception as e:
            logger.debug("State publish failed: %s", e)

        await asyncio.sleep(interval)


# ── API fetcher (for per-session scene reconstruction) ──────────────


def make_api_fetcher(config: Config):
    """Create an API fetcher for SceneReconstructor (per-session use)."""
    base_url = config.V2X_API_URL.rsplit("/detections/", 1)[0]

    def fetch(start: str, end: str, limit: int = 500) -> dict:
        url = f"{base_url}/detections/range"
        params = {"start": start, "end": end, "limit": limit}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    return fetch


def cleanup_drive_world(world, shared_prop_pool: dict[str, int] | None = None, registry=None) -> None:
    """Remove drive-owned actors from the current world."""
    for actor in world.get_actors().filter("vehicle.*"):
        logger.info("Cleaning up leftover vehicle: %s (id=%d)", actor.type_id, actor.id)
        actor.destroy()
    for actor in world.get_actors().filter("sensor.*"):
        logger.info("Cleaning up leftover sensor: %s (id=%d)", actor.type_id, actor.id)
        actor.destroy()
    leftover_props = world.get_actors().filter("static.prop.*")
    if leftover_props:
        logger.info("Cleaning up %d leftover static prop(s)", len(leftover_props))
        for actor in leftover_props:
            try:
                actor.destroy()
            except Exception as e:
                logger.debug("Prop destroy failed (id=%d): %s", actor.id, e)
    if shared_prop_pool is not None:
        shared_prop_pool.clear()


def create_openscenario_runner(config: Config, world):
    return OpenScenarioRunner(
        scenario_runner_path=config.SCENARIO_RUNNER_PATH,
        carla_host=config.CARLA_HOST,
        carla_port=config.CARLA_PORT,
        python_executable=config.SCENARIO_RUNNER_PYTHON or None,
        pythonpath_prefix=config.SCENARIO_RUNNER_PYTHONPATH,
        world=world,
    )


def export_current_map_data(conn: CarlaConnection, config: Config, uplink) -> None:
    """Export the active CARLA map locally and to S3 when uplink is available."""
    if uplink is None:
        return
    from digital_twin_bridge.map_data import MapDataExporter

    map_exporter = MapDataExporter(conn)
    snapshot_dir = config.LOCAL_SNAPSHOT_DIR
    os.makedirs(snapshot_dir, exist_ok=True)
    map_data = map_exporter.export_to_json(
        os.path.join(snapshot_dir, "map_data.json")
    )
    uplink.upload_map_data(map_data)
    logger.info("Map data exported and uploaded to S3")


class DriveMapController:
    """Coordinates safe runtime switching between the two public drive maps."""

    def __init__(
        self,
        conn: CarlaConnection,
        config: Config,
        runtime: dict,
        shared_prop_pool: dict[str, int],
        uplink,
    ) -> None:
        self._conn = conn
        self._config = config
        self._runtime = runtime
        self._shared_prop_pool = shared_prop_pool
        self._uplink = uplink
        self._lock = asyncio.Lock()

    @property
    def world(self):
        return self._runtime["world"]

    @property
    def carla_map(self):
        return self._runtime["carla_map"]

    @property
    def trajectory_player(self):
        return self._runtime["trajectory_player"]

    @property
    def openscenario_runner(self):
        return self._runtime["openscenario_runner"]

    def status_payload(self) -> dict:
        return drive_map_status(self._conn.carla_map.name)

    async def switch_map(self, map_id: str) -> dict:
        async with self._lock:
            if active_session_count() > 0:
                raise RuntimeError("End the active drive session before switching maps")
            if self.trajectory_player.is_active():
                raise RuntimeError("Stop trajectory playback before switching maps")
            if self.openscenario_runner.is_running:
                raise RuntimeError("Stop OpenSCENARIO before switching maps")

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._conn.switch_drive_map, map_id)

            try:
                self.trajectory_player.stop()
            except Exception as e:
                logger.debug("Trajectory stop before map switch failed: %s", e)
            try:
                self.openscenario_runner.stop()
            except Exception as e:
                logger.debug("OpenSCENARIO stop before map switch failed: %s", e)

            cleanup_drive_world(self._conn.world, self._shared_prop_pool)
            self._runtime["world"] = self._conn.world
            self._runtime["carla_map"] = self._conn.carla_map
            self._runtime["trajectory_player"] = TrajectoryPlayer(
                self._conn.world,
                self._conn.carla_map,
            )
            self._runtime["openscenario_runner"] = create_openscenario_runner(
                self._config,
                self._conn.world,
            )

            try:
                await loop.run_in_executor(None, export_current_map_data, self._conn, self._config, self._uplink)
            except Exception:
                logger.warning("Map data export after map switch failed", exc_info=True)

            return {"type": "map_set", **result}


# ── Main ────────────────────────────────────────────────────────────


async def main():
    config = Config.from_env()
    config.setup_logging()

    if "--dry-run" in sys.argv:
        logger.info("Unified server dry-run OK")
        return

    # ── Connect to CARLA (CarlaConnection handles sync mode + restore) ──
    conn = CarlaConnection(config)
    conn.connect()

    world = conn.world
    carla_map = conn.carla_map

    cleanup_drive_world(world)

    # ── V2X props: boot spawn disabled ──
    # PropSpawner is intentionally not invoked here. Production V2X
    # detections were landing traffic cones on the road in the firetruck
    # scenarios' paths. To re-enable, restore the fetch_v2x_snapshot() +
    # PropSpawner(world, carla_map).sync(registry) block from git history;
    # the spawner module (prop_spawner.py) is still available unchanged.
    shared_prop_pool: dict[str, int] = {}
    registry = None

    # ── Map data: export road network to S3 ──
    uplink = None
    try:
        from digital_twin_bridge.uplink import Uplink

        uplink = Uplink(config)
        export_current_map_data(conn, config, uplink)
    except Exception:
        logger.warning("Map data export failed (non-fatal)", exc_info=True)

    # ── Health monitor ──
    from digital_twin_bridge.health import HealthMonitor

    health = HealthMonitor()

    # ── Trajectory player (singleton: one playback at a time, shared world) ──
    trajectory_player = TrajectoryPlayer(world, carla_map)

    # ── OpenSCENARIO runner (singleton: one .xosc at a time across all sessions) ──
    openscenario_runner = create_openscenario_runner(config, world)
    runtime = {
        "world": world,
        "carla_map": carla_map,
        "trajectory_player": trajectory_player,
        "openscenario_runner": openscenario_runner,
    }
    map_controller = DriveMapController(conn, config, runtime, shared_prop_pool, uplink)

    # ── Drive server setup ──
    api_fetcher = make_api_fetcher(config)

    _test_log_subscribers: list[asyncio.Queue] = []

    async def _serve_test(websocket):
        import json
        import os
        import re
        from datetime import datetime
        TEST_LOG = "/tmp/bridge-test.log"
        UPLOAD_DIR = "/tmp/bridge-uploads"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        def _broadcast(event):
            for q in list(_test_log_subscribers):
                try:
                    q.put_nowait(event)
                except Exception:
                    pass
        def _tlog(line):
            full = f"{datetime.now().isoformat(timespec='seconds')} {line}"
            try:
                with open(TEST_LOG, "a") as f:
                    f.write(full + "\n")
            except Exception:
                pass
            _broadcast({"type": "log_line", "line": full})
        def _file_entry(name):
            try:
                st = os.stat(os.path.join(UPLOAD_DIR, name))
                return {
                    "name": name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            except OSError:
                return None
        def _safe_name(name):
            base = os.path.basename((name or "upload.bin")).strip()
            base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload.bin"
            base = base.lstrip(".") or "upload.bin"
            return base[:120]
        addr = websocket.remote_address
        logger.info("Test connection from %s", addr)
        await websocket.send(json.dumps({
            "type": "test_hello",
            "remote": str(addr),
        }))

        try:
            with open(TEST_LOG) as f:
                history = f.read().splitlines()[-500:]
        except FileNotFoundError:
            history = []
        await websocket.send(json.dumps({"type": "log_history", "lines": history}))

        try:
            files = []
            for name in sorted(os.listdir(UPLOAD_DIR)):
                entry = _file_entry(name)
                if entry is not None:
                    files.append(entry)
        except OSError:
            files = []
        await websocket.send(json.dumps({"type": "uploads_listing", "files": files}))

        log_q: asyncio.Queue = asyncio.Queue()
        _test_log_subscribers.append(log_q)
        async def _log_pump():
            try:
                while True:
                    event = await log_q.get()
                    await websocket.send(json.dumps(event))
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                pass
            except Exception:
                pass
        pump_task = asyncio.create_task(_log_pump())

        _tlog(f"OPEN  {addr}")
        pending_upload = None
        try:
            async for msg in websocket:
                if isinstance(msg, bytes):
                    if pending_upload is not None:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                        saved_name = f"{ts}_{_safe_name(pending_upload.get('filename'))}"
                        saved_path = os.path.join(UPLOAD_DIR, saved_name)
                        try:
                            with open(saved_path, "wb") as f:
                                f.write(msg)
                            _tlog(
                                f"UPLOAD {addr} saved={saved_path} bytes={len(msg)} "
                                f"original={pending_upload.get('filename')!r} "
                                f"mime={pending_upload.get('mime')!r}"
                            )
                            entry = _file_entry(saved_name)
                            if entry is not None:
                                _broadcast({"type": "uploads_added", "file": entry})
                            await websocket.send(json.dumps({
                                "type": "upload_done",
                                "saved_as": saved_path,
                                "size": len(msg),
                                "original_filename": pending_upload.get("filename"),
                            }))
                        except Exception as e:
                            _tlog(f"UPLOAD_ERROR {addr} {e!r}")
                            await websocket.send(json.dumps({
                                "type": "upload_error",
                                "message": str(e),
                            }))
                        finally:
                            pending_upload = None
                    else:
                        hex_preview = msg[:32].hex(" ")
                        _tlog(f"IN    {addr} (binary, {len(msg)}B) {hex_preview}")
                        await websocket.send(msg)
                else:
                    _tlog(f"IN    {addr} (text) {msg!r}")
                    try:
                        parsed = json.loads(msg)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("type") == "upload_start":
                        pending_upload = parsed
                        _tlog(
                            f"UPLOAD_START {addr} filename={parsed.get('filename')!r} "
                            f"size={parsed.get('size')} mime={parsed.get('mime')!r}"
                        )
                        await websocket.send(json.dumps({
                            "type": "upload_ready",
                            "filename": parsed.get("filename"),
                        }))
                    else:
                        await websocket.send(json.dumps({"type": "echo", "data": msg}))
        except websockets.exceptions.ConnectionClosed:
            logger.info("Test connection closed")
            _tlog(f"CLOSE {addr}")
        finally:
            try:
                _test_log_subscribers.remove(log_q)
            except ValueError:
                pass
            pump_task.cancel()

    async def handler(websocket):
        if websocket.request.path == "/test":
            await _serve_test(websocket)
            return
        await serve_drive(
            websocket, runtime["world"], runtime["carla_map"], api_fetcher,
            shared_prop_pool, runtime["trajectory_player"], runtime["openscenario_runner"],
            eva_warning_distance_m=config.EVA_WARNING_DISTANCE_M,
            map_controller=map_controller,
        )

    async def tick_loop():
        """Advance CARLA physics at 20 Hz.

        In sync mode the world does not tick on its own; we drive it from
        here. While ScenarioRunner is running it owns the tick (launched
        with --sync, locally patched to pace at 20 Hz wall-time so user
        controls feel normal). The bridge yields its tick during that
        window. SR turns sync mode off on exit, so we re-arm it before
        resuming. After each bridge tick we step the trajectory player so
        its controller stays in lockstep with sim time.
        """
        loop = asyncio.get_running_loop()
        target_dt = 0.05
        was_running = False
        while True:
            world = runtime["world"]
            trajectory_player = runtime["trajectory_player"]
            openscenario_runner = runtime["openscenario_runner"]
            if openscenario_runner.is_running:
                was_running = True
                await asyncio.sleep(target_dt)
                continue
            if was_running:
                try:
                    settings = world.get_settings()
                    if not settings.synchronous_mode:
                        settings.synchronous_mode = True
                        settings.fixed_delta_seconds = target_dt
                        world.apply_settings(settings)
                        logger.info("Re-applied sync mode after scenario finished")
                except Exception as e:
                    logger.warning("Failed to restore sync mode after scenario: %s", e)
                was_running = False

            start = loop.time()
            try:
                await loop.run_in_executor(None, world.tick)
            except Exception as e:
                logger.warning("world.tick() failed: %s", e)
                await asyncio.sleep(target_dt)
                continue
            try:
                trajectory_player.tick()
            except Exception as e:
                logger.warning("trajectory_player.tick() failed: %s", e)
            elapsed = loop.time() - start
            await asyncio.sleep(max(0.0, target_dt - elapsed))

    async def periodic_actor_audit():
        """Every 60s, check for orphaned actors when no sessions are active."""
        while True:
            await asyncio.sleep(60)
            if active_session_count() > 0:
                continue
            try:
                from digital_twin_bridge.drive_server import _traffic_actor_ids
                world = runtime["world"]
                vehicles = [v for v in world.get_actors().filter("vehicle.*")
                            if v.id not in _traffic_actor_ids
                            and v.attributes.get("role_name") != "trajectory"]
                sensors = world.get_actors().filter("sensor.*")
                # Boot-time V2X props are intentional; only sweep if we find a
                # runaway count (>2x the tracked snapshot), which indicates
                # stacked spawns from prior sessions.
                props = list(world.get_actors().filter("static.prop.*"))
                baseline = len(registry.get_all()) if registry else 0
                props_to_clean = props if baseline and len(props) > baseline * 2 else []
                orphaned = len(vehicles) + len(sensors) + len(props_to_clean)
                if orphaned > 0:
                    logger.warning(
                        "Actor audit: %d orphaned actors (vehicles=%d, sensors=%d, props=%d). Cleaning up.",
                        orphaned, len(vehicles), len(sensors), len(props_to_clean),
                    )
                    for a in sensors:
                        try:
                            a.stop()
                        except Exception:
                            pass
                        try:
                            a.destroy()
                        except Exception:
                            pass
                    for a in vehicles:
                        try:
                            a.destroy()
                        except Exception:
                            pass
                    for a in props_to_clean:
                        try:
                            a.destroy()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Actor audit error: %s", e)

    port = config.WS_PORT

    logger.info("=" * 60)
    logger.info("  Digital Twin -- Unified Server")
    logger.info("=" * 60)
    logger.info("  CARLA       : %s:%d (sync mode, 20 Hz)", config.CARLA_HOST, config.CARLA_PORT)
    logger.info("  Drive WS    : ws://0.0.0.0:%d", port)
    logger.info("  V2X objects : %d tracked", len(shared_prop_pool))
    logger.info("  State pub   : %s", "active" if uplink else "disabled (no AWS)")
    logger.info("=" * 60)

    tick_task = None
    audit_task = None
    publish_task = None

    try:
        tick_task = asyncio.create_task(tick_loop())
        audit_task = asyncio.create_task(periodic_actor_audit())

        # Publish state.json to S3 so the web dashboard stays live
        if uplink is not None and registry is not None:
            publish_task = asyncio.create_task(
                state_publisher(config, registry, health, uplink)
            )

        async with websockets.serve(
            handler,
            "0.0.0.0",
            port,
            ping_interval=5,
            ping_timeout=15,
            close_timeout=5,
            max_size=128 * 1024 * 1024,
        ):
            logger.info("Unified server ready. Waiting for connections...")
            await asyncio.Future()
    finally:
        for task in [tick_task, audit_task, publish_task]:
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass

        # Stop any active trajectory playback (server-owned, not session-owned).
        try:
            runtime["trajectory_player"].stop()
        except Exception as e:
            logger.debug("Trajectory stop on shutdown failed: %s", e)

        # Stop any running OpenSCENARIO subprocess.
        try:
            runtime["openscenario_runner"].stop()
        except Exception as e:
            logger.debug("OpenSCENARIO stop on shutdown failed: %s", e)

        # Destroy shared V2X props (owned by the server, not any session).
        if shared_prop_pool:
            destroyed = 0
            for actor_id in list(shared_prop_pool.values()):
                try:
                    actor = runtime["world"].get_actor(actor_id)
                    if actor is not None:
                        actor.destroy()
                        destroyed += 1
                except Exception as e:
                    logger.debug("Shared prop destroy failed (id=%d): %s", actor_id, e)
            shared_prop_pool.clear()
            logger.info("Destroyed %d shared V2X props at shutdown", destroyed)

        conn.disconnect()
        logger.info("Unified server stopped")


if __name__ == "__main__":
    asyncio.run(main())

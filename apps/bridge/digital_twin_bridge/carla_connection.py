"""
Manages the connection to the CARLA simulator.

Provides a context-manager interface so the original world settings are
always restored on exit.
"""

import logging
from typing import Optional

import carla

from digital_twin_bridge.config import Config

logger = logging.getLogger(__name__)

DRIVE_MAP_OPTIONS = {
    "richmond": {
        "id": "richmond",
        "label": "Richmond",
        "map_name": "Richmond_Field_Station_Richmond_CA",
    },
    "san_ramon": {
        "id": "san_ramon",
        "label": "San Ramon",
        "map_name": "San_Ramon_P1_Roads",
    },
}


def _map_leaf(name: str) -> str:
    """Return the final path component for CARLA map identifiers."""
    return name.rsplit("/", 1)[-1]


def normalize_drive_map_id(value: str) -> str:
    """Resolve a public two-choice drive map id or CARLA map name."""
    raw = (value or "").strip()
    lowered = raw.lower().replace(" ", "_").replace("-", "_")
    if lowered in DRIVE_MAP_OPTIONS:
        return lowered

    leaf = _map_leaf(raw).lower()
    for map_id, option in DRIVE_MAP_OPTIONS.items():
        if leaf == option["map_name"].lower():
            return map_id
    raise ValueError("Unsupported drive map")


def drive_map_status(carla_map_name: str) -> dict:
    """Return the public map status payload for a CARLA map name."""
    current_id = None
    leaf = _map_leaf(carla_map_name).lower()
    for map_id, option in DRIVE_MAP_OPTIONS.items():
        if leaf == option["map_name"].lower():
            current_id = map_id
            break
    return {
        "current_map": current_id,
        "current_map_name": carla_map_name,
        "maps": list(DRIVE_MAP_OPTIONS.values()),
    }


class CarlaConnection:
    """Persistent connection to a CARLA simulator instance.

    Usage::

        with CarlaConnection(config) as conn:
            world = conn.world
            carla_map = conn.carla_map
            conn.tick()
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Optional[carla.Client] = None
        self._world: Optional[carla.World] = None
        self._map: Optional[carla.Map] = None
        self._original_settings: Optional[carla.WorldSettings] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to CARLA, retrieve the world/map, and enable sync mode."""
        logger.info(
            "Connecting to CARLA at %s:%d ...",
            self._config.CARLA_HOST,
            self._config.CARLA_PORT,
        )
        self._client = carla.Client(
            self._config.CARLA_HOST, self._config.CARLA_PORT
        )
        self._client.set_timeout(30.0)

        self._world = self._client.get_world()
        self._map = self._world.get_map()
        self._load_configured_map_if_available()

        # Save original settings so we can restore them later
        self._original_settings = self._world.get_settings()

        # If CARLA is stuck in sync mode from a previous crash, reset first
        if self._original_settings.synchronous_mode:
            logger.warning("CARLA was already in sync mode (previous crash?). Resetting...")
            reset = self._world.get_settings()
            reset.synchronous_mode = False
            self._world.apply_settings(reset)
            import time
            time.sleep(0.5)
            self._original_settings = self._world.get_settings()

        # Enable synchronous mode with a fixed delta.
        settings = self._world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05  # 20 Hz simulation
        self._world.apply_settings(settings)
        self._client.set_timeout(10.0)

        # Bright noon at connect — without this, get_weather() returns
        # WeatherParameters() defaults (sun_altitude_angle=0, dark). Once a
        # scenario runs, its <EnvironmentAction> overrides this and the new
        # weather persists past scenario end (no bridge-side restore).
        self._world.set_weather(carla.WeatherParameters(
            cloudiness=0.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=30.0,
            sun_azimuth_angle=180.0,
            sun_altitude_angle=75.0,
            fog_density=0.0,
            fog_distance=100000.0,
            wetness=0.0,
        ))

        logger.info(
            "Connected to CARLA. Map: %s | Sync mode enabled.",
            self._map.name,
        )

    def _load_configured_map_if_available(self) -> None:
        """Load the configured CARLA map when the simulator advertises it."""
        if self._client is None or self._world is None or self._map is None:
            raise RuntimeError("Not connected to CARLA.")

        requested_map = self._config.CARLA_MAP.strip()
        if not requested_map:
            return
        try:
            requested_map = DRIVE_MAP_OPTIONS[normalize_drive_map_id(requested_map)]["map_name"]
        except ValueError:
            pass

        current_map = self._map.name
        if current_map == requested_map or _map_leaf(current_map) == requested_map:
            logger.info("CARLA map already active: %s", current_map)
            return

        try:
            available_maps = list(self._client.get_available_maps())
        except Exception:
            logger.warning(
                "Failed to list CARLA maps; keeping current map %s.",
                current_map,
                exc_info=True,
            )
            return

        target_map = next(
            (
                candidate
                for candidate in available_maps
                if candidate == requested_map or _map_leaf(candidate) == requested_map
            ),
            None,
        )
        if target_map is None:
            logger.warning(
                "Requested CARLA map %s is not available; keeping current map %s. "
                "Available maps: %s",
                requested_map,
                current_map,
                ", ".join(available_maps),
            )
            return

        logger.info(
            "Loading configured CARLA map %s (current=%s, available=%s)",
            target_map,
            current_map,
            ", ".join(available_maps),
        )
        self._client.set_timeout(120.0)
        self._world = self._client.load_world(target_map)
        self._map = self._world.get_map()
        logger.info("Loaded CARLA map: %s", self._map.name)

    def switch_drive_map(self, map_id_or_name: str) -> dict:
        """Switch between the two supported public drive maps."""
        if self._client is None or self._world is None or self._map is None:
            raise RuntimeError("Not connected to CARLA.")

        map_id = normalize_drive_map_id(map_id_or_name)
        requested_map = DRIVE_MAP_OPTIONS[map_id]["map_name"]
        current_map = self._map.name
        if current_map == requested_map or _map_leaf(current_map) == requested_map:
            return {"changed": False, **drive_map_status(current_map)}

        available_maps = list(self._client.get_available_maps())
        target_map = next(
            (
                candidate
                for candidate in available_maps
                if candidate == requested_map or _map_leaf(candidate) == requested_map
            ),
            None,
        )
        if target_map is None:
            raise RuntimeError(
                f"Drive map '{DRIVE_MAP_OPTIONS[map_id]['label']}' is not available in CARLA"
            )

        logger.info("Switching drive map from %s to %s", current_map, target_map)
        self._client.set_timeout(120.0)
        self._world = self._client.load_world(target_map)
        self._map = self._world.get_map()
        self._original_settings = self._world.get_settings()

        settings = self._world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        self._world.apply_settings(settings)
        self._world.set_weather(carla.WeatherParameters(
            cloudiness=0.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=30.0,
            sun_azimuth_angle=180.0,
            sun_altitude_angle=75.0,
            fog_density=0.0,
            fog_distance=100000.0,
            wetness=0.0,
        ))
        self._client.set_timeout(10.0)
        logger.info("Switched drive map: %s", self._map.name)
        return {"changed": True, **drive_map_status(self._map.name)}

    def disconnect(self) -> None:
        """Restore original world settings and release references."""
        if self._world is not None and self._original_settings is not None:
            try:
                self._world.apply_settings(self._original_settings)
                logger.info("Restored original CARLA world settings.")
            except Exception:
                logger.warning(
                    "Failed to restore CARLA world settings.", exc_info=True
                )
        self._client = None
        self._world = None
        self._map = None
        self._original_settings = None

    def tick(self) -> int:
        """Advance the simulation by one step (synchronous mode).

        Returns:
            The frame id returned by :meth:`carla.World.tick`.
        """
        if self._world is None:
            raise RuntimeError("Not connected to CARLA.")
        return self._world.tick()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def world(self) -> carla.World:
        """The active CARLA world."""
        if self._world is None:
            raise RuntimeError("Not connected to CARLA.")
        return self._world

    @property
    def carla_map(self) -> carla.Map:
        """The active CARLA map."""
        if self._map is None:
            raise RuntimeError("Not connected to CARLA.")
        return self._map

    @property
    def client(self) -> carla.Client:
        """The underlying CARLA client."""
        if self._client is None:
            raise RuntimeError("Not connected to CARLA.")
        return self._client

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "CarlaConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[override]
        self.disconnect()
        return None

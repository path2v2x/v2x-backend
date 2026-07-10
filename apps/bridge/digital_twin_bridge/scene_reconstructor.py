"""
Scene Reconstructor — queries historical V2X detections and spawns
them as CARLA actors to recreate a past scene.

Reuses geo_utils for GPS-to-CARLA coordinate conversion.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from digital_twin_bridge.detection_pages import fetch_all_detection_pages
from digital_twin_bridge.geo_utils import gps_to_carla

logger = logging.getLogger(__name__)

OBJECT_TYPE_TO_BLUEPRINT = {
    "traffic_cone": "static.prop.trafficcone01",
}
DEFAULT_BLUEPRINT = "static.prop.trafficwarning"


@dataclass
class SpawnedActor:
    """Metadata for an actor spawned during scene reconstruction."""
    id: int
    object_id: str
    object_type: str
    lat: float
    lon: float


@dataclass
class ReconstructionResult:
    """Result of a scene reconstruction."""
    spawned_actors: list[SpawnedActor] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    total_detections: int = 0


class SceneReconstructor:
    """
    Fetches one historical V2X range and owns the CARLA actors created from it.

    ``fetch`` is deliberately free of CARLA calls so DriveSession may execute
    bounded HTTP pagination in a worker thread.  ``spawn`` and ``cleanup`` must
    run on the bridge event-loop/CARLA thread.  Actors are never shared by
    ``object_id`` across sessions because two historical ranges may describe
    the same object at different positions.
    """

    def __init__(
        self,
        world,
        carla_map,
        api_fetcher: Callable,
        *,
        max_pages: int = 20,
        max_items: int = 10_000,
    ):
        self._world = world
        self._map = carla_map
        self._api_fetcher = api_fetcher
        self._max_pages = max(1, int(max_pages))
        self._max_items = max(1, int(max_items))
        self._spawned_actors: list[SpawnedActor] = []

    def fetch(
        self,
        start: str,
        end: str,
        limit: int = 500,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> ReconstructionResult:
        """Fetch and deduplicate a range without touching CARLA state."""
        result = ReconstructionResult()

        api_response = fetch_all_detection_pages(
            self._api_fetcher,
            start,
            end,
            page_size=limit,
            max_pages=self._max_pages,
            max_items=self._max_items,
            should_stop=should_stop,
        )
        items = api_response.get("items", [])
        result.total_detections = len(items)

        if not items:
            logger.info("No detections found for %s to %s", start, end)
            return result

        # Deduplicate by object_id within this requested range only.
        deduped: dict[str, dict] = {}
        for item in items:
            oid = item["object_id"]
            if oid not in deduped or item["timestamp_utc"] > deduped[oid]["timestamp_utc"]:
                deduped[oid] = item

        result.objects = list(deduped.values())
        logger.info(
            "Fetched scene: %d unique objects from %d detections",
            len(deduped), len(items),
        )
        return result

    def spawn(self, result: ReconstructionResult) -> ReconstructionResult:
        """Spawn a fetched result on the caller's CARLA thread."""
        if self._spawned_actors:
            raise RuntimeError("scene reconstructor already owns spawned actors")

        bp_lib = self._world.get_blueprint_library()

        for obj in result.objects:
            oid = obj["object_id"]
            obj_type = obj.get("object_type", "unknown")

            bp_id = OBJECT_TYPE_TO_BLUEPRINT.get(obj_type, DEFAULT_BLUEPRINT)
            blueprints = bp_lib.filter(bp_id)
            if not blueprints:
                logger.warning("No blueprint found for %s (%s)", bp_id, obj_type)
                continue
            bp = blueprints[0]

            # GPS to CARLA coordinates via map geo-reference
            gps = obj.get("gps_location", {})
            lat = gps.get("latitude", 0.0)
            lon = gps.get("longitude", 0.0)
            transform = self._gps_to_transform(lat, lon)

            actor = self._world.try_spawn_actor(bp, transform)
            if actor is None:
                logger.warning("Failed to spawn %s at (%.6f, %.6f)", obj_type, lat, lon)
                continue

            spawned = SpawnedActor(
                id=actor.id,
                object_id=oid,
                object_type=obj_type,
                lat=lat,
                lon=lon,
            )
            self._spawned_actors.append(spawned)
            result.spawned_actors.append(spawned)

        logger.info(
            "Scene reconstruction complete: %d session-owned actors",
            len(result.spawned_actors),
        )
        return result

    def reconstruct(self, start: str, end: str, limit: int = 500) -> ReconstructionResult:
        """Synchronous compatibility wrapper used by offline/unit callers."""
        return self.spawn(self.fetch(start, end, limit))

    def actor_ids(self) -> list[int]:
        """Return the CARLA actor IDs owned by this historical scene."""
        return [spawned.id for spawned in self._spawned_actors]

    def cleanup(self) -> int:
        """Destroy every historical actor owned by this session."""
        destroyed = 0
        for spawned in self._spawned_actors:
            actor = self._world.get_actor(spawned.id)
            if actor is not None:
                try:
                    actor.destroy()
                    destroyed += 1
                except Exception:
                    logger.warning(
                        "Failed to destroy historical actor %d", spawned.id,
                        exc_info=True,
                    )
        self._spawned_actors.clear()
        return destroyed

    def _gps_to_transform(self, lat: float, lon: float):
        """Convert GPS coordinates through the version-aware shared helper.

        CARLA 0.10 removed ``Map.geolocation_to_transform``.  ``gps_to_carla``
        supports both the 0.9.x API and the 0.10 inverse-projection fallback,
        and already snaps the resulting location to the road surface.
        """
        import carla

        return carla.Transform(gps_to_carla(self._map, lat, lon))

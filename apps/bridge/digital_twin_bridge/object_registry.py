"""
Thread-safe registry of tracked V2X objects.

The registry is the single source of truth for what objects exist in the
scene.  The V2X poller writes into it; the camera scheduler reads from it.
"""

import time
import logging
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import carla

logger = logging.getLogger(__name__)
PRODUCER_FUTURE_TOLERANCE_SECONDS = 5.0


def _producer_epoch(value) -> Optional[float]:
    """Return a valid producer epoch, never a local receipt-time fallback."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 1_000_000_000_000:
            epoch /= 1000.0
        return epoch if 0.0 <= epoch < float("inf") else None

    text = str(value).strip()
    if not text:
        return None
    try:
        if text.replace(".", "", 1).isdigit():
            return _producer_epoch(float(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        epoch = parsed.timestamp()
        return epoch if 0.0 <= epoch < float("inf") else None
    except (OverflowError, ValueError):
        return None


@dataclass
class TrackedObject:
    """Represents a single V2X-detected object being tracked."""

    object_id: str
    object_type: str
    lat: float
    lon: float
    confidence: float
    street_name: str
    timestamp_utc: str
    carla_location: Optional[carla.Location] = None
    carla_actor_id: Optional[int] = None
    last_seen: float = field(default_factory=time.time)
    last_captured: float = 0.0
    capture_count: int = 0
    snapshot_url: Optional[str] = None
    snapshot_timestamp: Optional[str] = None


class ObjectRegistry:
    """Thread-safe store for :class:`TrackedObject` instances.

    All public methods acquire the internal lock so the registry can be
    safely shared between the V2X polling thread and the main capture loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: Dict[str, TrackedObject] = {}
        self._pending_destroy: List[int] = []  # CARLA actor IDs to destroy

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @staticmethod
    def _make_unique_id(base_id: str, lat: float, lon: float) -> str:
        """Generate a registry key from the API object_id + coordinates.

        This ensures every distinct detection is tracked separately, even
        when the API returns multiple items with the same ``object_id``.
        """
        return f"{base_id}_{lat:.6f}_{lon:.6f}"

    def update_from_v2x(self, detections: List[dict]) -> None:
        """Upsert objects from a list of V2X detection dicts.

        Each detection is treated as a distinct object — duplicates with
        the same ``object_id`` but different GPS positions are all kept.

        Each dict should contain at least ``object_id``, ``object_type``,
        ``gps_location.latitude``, ``gps_location.longitude``,
        ``confidence_score``, ``timestamp_utc``, and optionally
        ``street_name_normalized``.

        Fields that are populated externally (e.g. ``carla_location``)
        are preserved across updates so that they are not lost.
        """
        now = time.time()
        # `/detections/recent` is newest-first today, but registry correctness
        # must not depend on API order.  Collapse each exact registry key to its
        # newest valid producer record before touching shared state.
        newest: Dict[str, tuple[float, dict, float, float]] = {}
        for det in detections:
            if not isinstance(det, dict):
                continue
            gps = det.get("gps_location", {})
            if not isinstance(gps, dict):
                continue
            lat = gps.get("latitude")
            lon = gps.get("longitude")
            base_id = det.get("object_id", "")
            producer_epoch = _producer_epoch(det.get("timestamp_utc"))
            if (
                lat is None
                or lon is None
                or not base_id
                or producer_epoch is None
                or producer_epoch > now + PRODUCER_FUTURE_TOLERANCE_SECONDS
            ):
                continue
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (TypeError, ValueError):
                continue
            uid = self._make_unique_id(str(base_id), lat_f, lon_f)
            prior = newest.get(uid)
            if prior is None or producer_epoch > prior[0]:
                newest[uid] = (producer_epoch, det, lat_f, lon_f)

        with self._lock:
            seen_ids: set = set()
            for uid, (producer_epoch, det, lat_f, lon_f) in newest.items():
                seen_ids.add(uid)

                existing = self._objects.get(uid)
                if existing is not None:
                    existing_epoch = _producer_epoch(existing.timestamp_utc)
                    # Seeing the key still refreshes receipt-time liveness, but
                    # an older producer event can never roll its fields back.
                    existing.last_seen = now
                    if existing_epoch is not None and producer_epoch < existing_epoch:
                        continue
                    # Update mutable fields, keep camera state
                    existing.lat = lat_f
                    existing.lon = lon_f
                    existing.object_type = det.get("object_type", existing.object_type)
                    existing.confidence = float(det.get("confidence_score", existing.confidence))
                    existing.street_name = det.get("street_name_normalized", existing.street_name)
                    existing.timestamp_utc = det.get("timestamp_utc", existing.timestamp_utc)
                else:
                    self._objects[uid] = TrackedObject(
                        object_id=uid,
                        object_type=det.get("object_type", "unknown"),
                        lat=lat_f,
                        lon=lon_f,
                        confidence=float(det.get("confidence_score", 0.0)),
                        street_name=det.get("street_name_normalized", ""),
                        timestamp_utc=det.get("timestamp_utc", ""),
                        last_seen=now,
                    )

            logger.debug(
                "Registry update: %d detections processed, %d active objects.",
                len(seen_ids),
                len(self._objects),
            )

    def mark_captured(self, object_id: str) -> None:
        """Record that a snapshot was just taken for *object_id*."""
        with self._lock:
            obj = self._objects.get(object_id)
            if obj is not None:
                obj.last_captured = time.time()
                obj.capture_count += 1

    def remove_stale(self, max_age_seconds: float = 300.0) -> int:
        """Remove objects that have not been refreshed recently.

        Any associated CARLA actor IDs are queued for destruction on the
        main thread (see :meth:`drain_pending_destroy`).

        Args:
            max_age_seconds: Maximum time since last V2X update before
                an object is considered stale (default 5 minutes).

        Returns:
            The number of objects removed.
        """
        cutoff = time.time() - max_age_seconds
        with self._lock:
            stale_ids = [
                oid
                for oid, obj in self._objects.items()
                if obj.last_seen < cutoff
            ]
            for oid in stale_ids:
                obj = self._objects.pop(oid)
                if obj.carla_actor_id is not None:
                    self._pending_destroy.append(obj.carla_actor_id)
        if stale_ids:
            logger.info("Removed %d stale objects from registry.", len(stale_ids))
        return len(stale_ids)

    def drain_pending_destroy(self) -> List[int]:
        """Return and clear the list of CARLA actor IDs pending destruction.

        This should be called from the main thread, which can safely call
        ``world.get_actor(id).destroy()``.
        """
        with self._lock:
            ids = list(self._pending_destroy)
            self._pending_destroy.clear()
        return ids

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_all(self) -> List[TrackedObject]:
        """Return a snapshot list of all active tracked objects."""
        with self._lock:
            return list(self._objects.values())

    def get_by_id(self, object_id: str) -> Optional[TrackedObject]:
        """Return a single tracked object by ID, or ``None``."""
        with self._lock:
            return self._objects.get(object_id)

    @property
    def count(self) -> int:
        """Number of objects currently in the registry."""
        with self._lock:
            return len(self._objects)

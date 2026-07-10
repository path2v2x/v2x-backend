"""
Geographic utility functions for the Digital Twin Camera Bridge.

Handles conversions between GPS (WGS-84) coordinates and CARLA's UE4
coordinate system, correcting for the left-handed Y-axis inversion.
"""

import math
import logging
from typing import List, Tuple

import carla

logger = logging.getLogger(__name__)


def lateral_shift(transform: carla.Transform, shift: float) -> carla.Location:
    """Compute a lateral shift of a transform's forward vector.

    Rotates the transform's yaw by 90 degrees and moves ``shift`` metres
    along the resulting forward vector.  This is the standard technique
    used in CARLA's ``no_rendering_mode.py`` for computing lane edges.

    Args:
        transform: The reference transform (will be mutated -- pass a copy
            if you need to preserve the original).
        shift: Distance in metres.  Positive values shift to the right
            of the original forward direction.

    Returns:
        A :class:`carla.Location` offset from the original position.
    """
    transform.rotation.yaw += 90
    return transform.location + shift * transform.get_forward_vector()


def extract_road_network_gps(carla_map: carla.Map) -> List[List[List[float]]]:
    """Extract road edges as GPS polylines suitable for map rendering.

    Each polyline is a list of ``[longitude, latitude]`` pairs.  CARLA 0.9
    needs its historical UE4 latitude mirror, while CARLA 0.10 already emits
    correct WGS-84 coordinates and must be left unchanged.

    Args:
        carla_map: A :class:`carla.Map` obtained from the simulator.

    Returns:
        A list of polylines, each being a list of ``[lon, lat]`` pairs.
    """
    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude
    mirror_latitude = hasattr(carla_map, "geolocation_to_transform")

    def output_latitude(raw_latitude: float) -> float:
        if mirror_latitude:
            return 2 * origin_lat - raw_latitude
        return raw_latitude

    topology = [wp_pair[0] for wp_pair in carla_map.get_topology()]
    topology = sorted(topology, key=lambda w: w.transform.location.z)

    precision = 0.5  # metres between waypoints
    road_lines: List[List[List[float]]] = []

    for waypoint in topology:
        waypoints = [waypoint]
        nxt_list = waypoint.next(precision)
        if len(nxt_list) > 0:
            nxt = nxt_list[0]
            while nxt.road_id == waypoint.road_id:
                waypoints.append(nxt)
                nxt_list = nxt.next(precision)
                if len(nxt_list) > 0:
                    nxt = nxt_list[0]
                else:
                    break

        left_edge: List[List[float]] = []
        right_edge: List[List[float]] = []

        for w in waypoints:
            l_loc = lateral_shift(w.transform, -w.lane_width * 0.5)
            r_loc = lateral_shift(w.transform, w.lane_width * 0.5)

            l_geo = carla_map.transform_to_geolocation(l_loc)
            r_geo = carla_map.transform_to_geolocation(r_loc)

            left_edge.append([l_geo.longitude, output_latitude(l_geo.latitude)])
            right_edge.append([r_geo.longitude, output_latitude(r_geo.latitude)])

        if len(left_edge) > 1:
            road_lines.append(left_edge)
            road_lines.append(right_edge)

    logger.info("Extracted %d road-edge polylines from CARLA map.", len(road_lines))
    return road_lines


def gps_to_carla(
    carla_map: carla.Map, lat: float, lon: float
) -> carla.Location:
    """Convert GPS coordinates to a CARLA world Location.

    On CARLA 0.9, applies the UE4 Y-axis correction before calling
    :meth:`carla.Map.geolocation_to_transform`.  On CARLA 0.10, where that
    inverse API was removed, performs the local inverse projection from the
    map's georeference origin.

    The returned location is snapped to the nearest road surface when
    possible.

    Args:
        carla_map: The active CARLA map.
        lat: WGS-84 latitude in decimal degrees.
        lon: WGS-84 longitude in decimal degrees.

    Returns:
        A :class:`carla.Location` in CARLA world coordinates.
    """
    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude

    if hasattr(carla_map, "geolocation_to_transform"):
        # CARLA <= 0.9.x: geolocation_to_transform exists but mishandles the
        # UE left-handed Y-axis, so mirror latitude around the origin first.
        corrected_lat = 2 * origin_lat - lat

        geo = carla.GeoLocation(
            latitude=corrected_lat,
            longitude=lon,
            altitude=0.0,
        )
        projected = carla_map.geolocation_to_transform(geo)
        # CARLA 0.9.x returns a Transform here.  Some test doubles and older
        # PythonAPI builds expose the projected Location directly, so accept
        # both shapes at this version boundary.
        location = getattr(projected, "location", projected)
    else:
        # CARLA 0.10 dropped geolocation_to_transform, and its
        # transform_to_geolocation returns correct WGS-84 (x = easting,
        # y = -northing; verified empirically on the RFS map). Invert with a
        # flat-earth approximation around the map origin — accurate to
        # centimetres at site scale.
        meters_per_deg_lat = 111_320.0
        meters_per_deg_lon = 111_320.0 * math.cos(math.radians(origin_lat))
        location = carla.Location(
            x=(lon - origin_geo.longitude) * meters_per_deg_lon,
            y=-((lat - origin_lat) * meters_per_deg_lat),
            z=0.0,
        )

    # Snap Z to the road surface (coarse estimate from OpenDRIVE profile)
    wp = carla_map.get_waypoint(location, project_to_road=True)
    if wp is not None:
        location.z = wp.transform.location.z

    return location


def carla_to_gps(
    carla_map: carla.Map, location: carla.Location
) -> Tuple[float, float]:
    """Convert a CARLA world Location to GPS coordinates.

    Applies the inverse of the UE4 Y-axis correction so the returned
    latitude matches real-world WGS-84.

    Args:
        carla_map: The active CARLA map.
        location: A :class:`carla.Location` in CARLA world coordinates.

    Returns:
        A ``(latitude, longitude)`` tuple in decimal degrees.
    """
    geo = carla_map.transform_to_geolocation(location)

    if not hasattr(carla_map, "geolocation_to_transform"):
        # CARLA 0.10: transform_to_geolocation is already correct WGS-84.
        return geo.latitude, geo.longitude

    origin_geo = carla_map.transform_to_geolocation(carla.Location())
    origin_lat = origin_geo.latitude

    # Reverse the Y-axis mirror
    corrected_lat = 2 * origin_lat - geo.latitude

    return corrected_lat, geo.longitude


def compute_look_at_transform(
    target_location: carla.Location,
    offset_distance: float = 8.0,
    offset_height: float = 4.0,
) -> carla.Transform:
    """Compute a camera Transform that looks at a target location.

    The camera is placed ``offset_distance`` metres behind the target
    along the X-axis and ``offset_height`` metres above it.  Pitch and
    yaw are computed so the camera looks directly at the target.

    Args:
        target_location: The world location to look at.
        offset_distance: Horizontal offset in metres (default 8.0).
        offset_height: Vertical offset in metres (default 4.0).

    Returns:
        A :class:`carla.Transform` for the camera.
    """
    cam_loc = carla.Location(
        x=target_location.x - offset_distance,
        y=target_location.y,
        z=target_location.z + offset_height,
    )
    dx = target_location.x - cam_loc.x
    dy = target_location.y - cam_loc.y
    dz = target_location.z - cam_loc.z
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = math.degrees(math.atan2(dz, math.sqrt(dx * dx + dy * dy)))

    return carla.Transform(cam_loc, carla.Rotation(pitch=pitch, yaw=yaw))

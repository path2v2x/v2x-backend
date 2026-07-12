import math

import pytest

from v2x_common.geodesy import (
    GeodesyError,
    TransverseMercator,
    extract_opendrive_georeference,
    local_xz_to_geodetic,
)

GEOREFERENCE = (
    "+proj=tmerc +lat_0=37.9150891287087 +lon_0=-122.333308830857 "
    "+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +vunits=m +no_defs"
)


def test_matches_pyproj_reference_at_richmond_site():
    projection = TransverseMercator.from_proj_string(GEOREFERENCE)
    easting, northing = projection.forward(
        37.91560117034595, -122.33478756387032
    )
    assert easting == pytest.approx(-130.02947035053526, abs=1e-5)
    assert northing == pytest.approx(56.835028776906114, abs=1e-5)
    latitude, longitude = projection.inverse(easting, northing)
    assert latitude == pytest.approx(37.91560117034595, abs=2e-9)
    assert longitude == pytest.approx(-122.33478756387032, abs=2e-9)


def test_camera_local_offsets_share_the_map_projection():
    latitude, longitude = local_xz_to_geodetic(
        5.0,
        20.0,
        37.91560117034595,
        -122.33478756387032,
        0.0,
        GEOREFERENCE,
    )
    assert latitude == pytest.approx(37.91578135953516, abs=2e-9)
    assert longitude == pytest.approx(-122.33473070588238, abs=2e-9)


def test_roundtrip_is_centimetre_scale_over_site_extent():
    projection = TransverseMercator.from_proj_string(GEOREFERENCE)
    for easting in (-500.0, -130.0, 0.0, 400.0):
        for northing in (-400.0, 0.0, 56.0, 500.0):
            latitude, longitude = projection.inverse(easting, northing)
            roundtrip = projection.forward(latitude, longitude)
            assert math.hypot(roundtrip[0] - easting, roundtrip[1] - northing) < 0.01


def test_extracts_cdata_and_rejects_wrong_projection():
    opendrive = (
        "<OpenDRIVE><header><geoReference><![CDATA["
        + GEOREFERENCE
        + "]]></geoReference></header></OpenDRIVE>"
    )
    assert extract_opendrive_georeference(opendrive) == GEOREFERENCE
    with pytest.raises(GeodesyError, match="transverse-Mercator"):
        TransverseMercator.from_proj_string(
            "+proj=utm +zone=10 +datum=WGS84 +units=m"
        )

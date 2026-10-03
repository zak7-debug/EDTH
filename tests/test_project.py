"""geo/project.py: destination point and its inverse (README_audio_geolocation.md, Testing)."""
import math

import pytest

from backend.app.geo.project import R, bearing_deg, distance_m, finite_point, project


def test_known_answer_1000m_east_of_greenwich():
    # 1000 m along a great circle heading east from 51.5 N: about 0.014447 deg of longitude
    # (1000 / (R cos 51.5)), not 0.0143 as first written in the README.
    lat, lon = project(51.5, 0.0, 1000, 90)
    expected = (51.5, math.degrees(1000 / (R * math.cos(math.radians(51.5)))))
    assert expected[1] == pytest.approx(0.014447, abs=1e-6)
    assert distance_m((lat, lon), expected) < 2.0


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315, 17.5])
@pytest.mark.parametrize("dist", [50, 800, 5_000, 20_000])
def test_round_trip(bearing, dist):
    start = (47.62, 35.60)
    end = project(*start, dist, bearing)
    assert distance_m(start, end) == pytest.approx(dist, abs=0.01)
    assert bearing_deg(start, end) == pytest.approx(bearing, abs=1e-6)


def test_cardinal_directions_move_the_right_way():
    lat, lon = 47.62, 35.60
    assert project(lat, lon, 1000, 0)[0] > lat
    assert project(lat, lon, 1000, 180)[0] < lat
    assert project(lat, lon, 1000, 90)[1] > lon
    assert project(lat, lon, 1000, 270)[1] < lon


def test_zero_distance_is_the_same_point_and_longitude_wraps():
    assert project(47.62, 35.60, 0, 123) == pytest.approx((47.62, 35.60))
    assert -180 <= project(0.0, 179.999, 1000, 90)[1] < -179.99


def test_finite_point():
    assert finite_point((47.6, 35.6))
    assert not finite_point((float("nan"), 35.6))
    assert not finite_point((95.0, 35.6))
    assert not finite_point(("x", 1))

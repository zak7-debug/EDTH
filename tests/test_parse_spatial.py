"""geo/parse_spatial.py: Ukrainian and English compass words, azimuths, clock positions, distances."""
import pytest

from backend.app.geo.parse_spatial import parse_spatial


@pytest.mark.parametrize("text, bearing", [
    ("на північ", 0), ("на північний схід", 45), ("на схід", 90), ("на південний схід", 135),
    ("на південь", 180), ("на південний захід", 225), ("на захід", 270), ("на північний захід", 315),
    # case forms and adjectives a speaker actually uses
    ("на півночі", 0), ("північніше", 0), ("на північному сході", 45), ("північно-східніше", 45),
    ("на півдні", 180), ("південно-західний напрямок", 225), ("на сході", 90), ("із заходу", 270),
    ("north", 0), ("north-east", 45), ("northeast", 45), ("south west", 225), ("NW", 315),
])
def test_every_compass_word(text, bearing):
    p = parse_spatial(text + ", 300 метрів")
    assert p.bearing_deg == bearing and p.bearing_source == "compass"


def test_going_in_is_not_west():
    assert parse_spatial("не заходити, сто метрів").bearing_deg is None  # "заходити" = to go in


@pytest.mark.parametrize("text, metres", [
    ("двісті метрів", 200), ("два кілометри", 2000), ("вісімсот метрів", 800), ("800 м", 800),
    ("800м", 800), ("півтора кілометра", 1500), ("1,5 км", 1500), ("півкілометра", 500),
    ("пів кілометра", 500), ("двісті п'ятдесят метрів", 250), ("за двохсот метрів", 200),
    ("тисяча двісті метрів", 1200), ("дві тисячі метрів", 2000), ("за кілометр", 1000),
    ("two hundred and fifty meters", 250), ("a kilometre", 1000), ("half a kilometre", 500),
    ("3 km", 3000), ("сорок п'ять метрів", 45),
])
def test_spoken_distances(text, metres):
    assert parse_spatial(text + " на північ").distance_m == pytest.approx(metres)


def test_numeric_azimuth_and_degrees():
    assert parse_spatial("азимут 270, два кілометри").bearing_deg == 270
    assert parse_spatial("азимут двісті сімдесят, два кілометри").distance_m == 2000  # the pause splits them
    p = parse_spatial("на дев'яносто градусів, триста метрів")
    assert (p.bearing_deg, p.bearing_source, p.distance_m) == (90, "azimuth", 300)
    assert parse_spatial("bearing 045, 1 km").bearing_deg == 45


def test_clock_needs_a_heading():
    p = parse_spatial("на третій годині, триста метрів")
    assert p.bearing_deg is None and p.needs_confirmation and "heading" in p.reasons[0]
    p = parse_spatial("на третій годині, триста метрів", heading_deg=350)
    assert p.bearing_deg == pytest.approx(80) and p.bearing_source == "clock" and not p.needs_confirmation
    assert parse_spatial("at 9 o'clock, 400 m", heading_deg=0).bearing_deg == 270


def test_radius_is_kept_apart_from_distance():
    p = parse_spatial("Ворожий дрон, вісімсот метрів на північний схід. Закрити п'ятсот метрів.")
    assert (p.distance_m, p.bearing_deg, p.radius_m) == (800, 45, 500)
    p = parse_spatial("закрити п'ятсот метрів, дрон за 2 км на захід")
    assert (p.distance_m, p.radius_m) == (2000, 500)


def test_missing_distance_is_an_assumption_and_missing_direction_needs_confirmation():
    p = parse_spatial("ворожий дрон на сході")
    assert p.distance_m == 500 and p.assumptions
    p = parse_spatial("ворожий дрон, вісімсот метрів")
    assert p.bearing_deg is None and p.needs_confirmation and not p.located


def test_overhead_is_at_the_reporter():
    p = parse_spatial("ворожий дрон над нами")
    assert (p.distance_m, p.bearing_source) == (0, "here")

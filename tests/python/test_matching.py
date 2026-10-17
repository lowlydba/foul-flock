"""Camera-to-place matching and statutory exclusion screening."""

import pytest

from factories import make_cam, make_rule, place_at


def test_matching_heuristic_scans_wide(build_mod):
    # Non-statutory rule: candidates out to scan_max are kept even beyond
    # the default buffer, so the UI sliders work without a rebuild.
    cam = make_cam()
    places = {"school": [place_at(build_mod, 47.0, -122.0, 300)]}
    matches = build_mod.run_matching(
        [cam], places, [make_rule()], default_buffer=100, scan_max=400)
    assert len(matches) == 1
    assert matches[0]["distance_m"] == pytest.approx(300, rel=0.01)
    assert matches[0]["buffer_m"] == 100
    assert not matches[0]["buffer_specified"]


def test_matching_statutory_scans_exact(build_mod):
    # Statutory rule: scan stops at the statute's own number.
    cam = make_cam()
    places = {"school": [place_at(build_mod, 47.0, -122.0, 300)]}
    rule = make_rule(buffer_meters=150, buffer_specified=True)
    matches = build_mod.run_matching(
        [cam], places, [rule], default_buffer=100, scan_max=400)
    assert matches == []


def test_matching_cross_state_filtered(build_mod):
    cam = make_cam()
    places = {"school": [place_at(build_mod, 47.0, -122.0, 50, state="OR")]}
    matches = build_mod.run_matching(
        [cam], places, [make_rule()], default_buffer=100, scan_max=400)
    assert matches == []


def test_matching_caps_candidates_at_20_nearest(build_mod):
    cam = make_cam()
    places = {"school": [
        place_at(build_mod, 47.0, -122.0, 30 + i * 10, pid=f"p-{i}")
        for i in range(30)
    ]}
    matches = build_mod.run_matching(
        [cam], places, [make_rule()], default_buffer=100, scan_max=400)
    assert len(matches) == 20
    dists = [m["distance_m"] for m in matches]
    assert dists == sorted(dists)
    assert dists[0] == pytest.approx(30, rel=0.05)


def test_matching_skips_excluded_cameras(build_mod):
    cam = make_cam(possible_exclusion=True)
    places = {"school": [place_at(build_mod, 47.0, -122.0, 50)]}
    matches = build_mod.run_matching(
        [cam], places, [make_rule()], default_buffer=100, scan_max=400)
    assert matches == []


def test_mark_possible_exclusions(build_mod):
    rules = [make_rule(statutory_exclusions="speed cameras excluded")]
    doc = {"exclusion_manufacturer_watchlist": ["SpeedCo"]}
    cams = [
        make_cam(brand="SpeedCo Systems"),
        make_cam(brand="Flock Safety"),
        make_cam(brand="SpeedCo Systems", state="VA"),  # VA has no exclusions
        make_cam(brand=None),
    ]
    n = build_mod.mark_possible_exclusions(cams, rules, doc)
    assert n == 1
    assert cams[0]["possible_exclusion"] is True
    assert cams[1]["possible_exclusion"] is False
    assert cams[2]["possible_exclusion"] is False
    assert cams[3]["possible_exclusion"] is False

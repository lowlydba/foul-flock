"""Unit tests for build/build.py: geometry, taxonomy mapping, matching,
cache/refresh behavior, and artifact emission. Fully offline."""

import json

import pytest


# ------------------------------------------------------------ geometry ----

def test_haversine_one_degree_latitude(build_mod):
    # 1 degree of latitude is ~111.19 km on the sphere build.py uses.
    d = build_mod.haversine_m(47.0, -122.0, 48.0, -122.0)
    assert d == pytest.approx(111_195, rel=0.001)


def test_haversine_zero(build_mod):
    assert build_mod.haversine_m(47.5, -122.3, 47.5, -122.3) == 0


def test_grid_index_radius_filtering(build_mod):
    idx = build_mod.GridIndex(cell_deg=0.01)
    near = {"lat": 47.0005, "lon": -122.0, "name": "near"}
    far = {"lat": 47.02, "lon": -122.0, "name": "far"}
    idx.insert(near)
    idx.insert(far)
    hits = idx.query(47.0, -122.0, radius_m=200)
    assert [h[0]["name"] for h in hits] == ["near"]
    assert hits[0][1] == pytest.approx(55.6, rel=0.01)


def test_parse_directions_variants(build_mod):
    pd = build_mod.parse_directions
    assert pd(None) == []
    assert pd("90") == [90.0]
    assert pd(270) == [270.0]
    assert pd("NE") == [45.0]
    assert pd("90;180") == [90.0, 180.0]
    assert pd("N, S") == [0.0, 180.0]
    assert pd("garbage") == []
    assert pd("450") == [90.0]  # normalized mod 360


def test_cone_polygon_shape(build_mod):
    ring = build_mod.cone_polygon(47.0, -122.0, bearing=0.0)[0]
    # camera + 9 arc points + closing camera point
    assert len(ring) == 11
    assert ring[0] == ring[-1] == [-122.0, 47.0]
    # arc points sit ~CONE_RANGE_M away from the camera
    mid = ring[5]
    d = build_mod.haversine_m(47.0, -122.0, mid[1], mid[0])
    assert d == pytest.approx(build_mod.CONE_RANGE_M, rel=0.01)


# ------------------------------------------------- taxonomy and mapping ----

@pytest.fixture(scope="module")
def lookup(build_mod):
    rules_doc = build_mod.yaml.safe_load(
        build_mod.RULES_FILE.read_text(encoding="utf-8"))
    taxonomy = build_mod.load_taxonomy()
    return build_mod.build_category_lookup(
        rules_doc["category_overture_mapping"], taxonomy)


def test_taxonomy_descendants_match(lookup):
    assert lookup["school"] == "school"
    assert lookup["elementary_school"] == "school"
    assert lookup["montessori_school"] == "school"


def test_taxonomy_healthcare_scope(lookup):
    # kept: facilities and the pediatric subtree
    assert lookup["hospital"] == "healthcare_facility"
    assert lookup["childrens_hospital"] == "healthcare_facility"
    assert lookup["urgent_care_clinic"] == "healthcare_facility"
    assert lookup["pediatrician"] == "healthcare_facility"
    assert lookup["pediatric_cardiology"] == "healthcare_facility"
    # dropped on purpose: generic doctor offices and adjacent noise
    assert "doctor" not in lookup
    assert "dermatologist" not in lookup
    assert "dentist" not in lookup
    assert "clinical_laboratories" not in lookup


def test_taxonomy_worship_and_civic(lookup):
    assert lookup["mosque"] == "place_of_worship"
    assert lookup["synagogue"] == "place_of_worship"
    assert lookup["religious_organization"] == "place_of_worship"
    assert lookup["courthouse"] == "courthouse"
    assert lookup["food_banks"] == "food_bank"
    # higher ed is deliberately out of scope
    assert "college_university" not in lookup


def test_religion_by_category(build_mod):
    assert build_mod.RELIGION_BY_CATEGORY["church_cathedral"] == "christian"
    assert build_mod.RELIGION_BY_CATEGORY["mosque"] == "muslim"
    assert build_mod.RELIGION_BY_CATEGORY["synagogue"] == "jewish"
    assert "temple" not in build_mod.RELIGION_BY_CATEGORY


# ------------------------------------------------------------- matching ----

def make_rule(**over):
    rule = {
        "rule_id": "T-1",
        "state_code": "WA",
        "restricted_category": "school",
        "buffer_meters": None,
        "buffer_specified": False,
        "heuristic_buffer_meters": None,
    }
    rule.update(over)
    return rule


def make_cam(lat=47.0, lon=-122.0, state="WA", **over):
    cam = {"camera_id": "cam-1", "lat": lat, "lon": lon,
           "state_code": state, "possible_exclusion": False}
    cam.update(over)
    return cam


def place_at(build_mod, lat, lon, dist_m, bearing=90.0, state="WA",
             cat="school", pid="p-1"):
    plat, plon = build_mod.destination(lat, lon, bearing, dist_m)
    return {"place_id": pid, "lat": plat, "lon": plon, "name": pid,
            "category": cat, "state_code": state}


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


# --------------------------------------------------- cache and refresh ----

def test_overpass_cache_hit_and_refresh(build_mod, sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(build_mod, "http_post_overpass",
                        lambda q: calls.append(q) or b'{"elements": []}')
    monkeypatch.setattr(build_mod.time, "sleep", lambda s: None)

    out1 = build_mod.overpass_query("cameras_XX", "query", offline=False)
    assert out1 == {"elements": []}
    assert len(calls) == 1

    # Second call is served from cache
    build_mod.overpass_query("cameras_XX", "query", offline=False)
    assert len(calls) == 1

    # Camera refresh scope busts it
    monkeypatch.setattr(build_mod, "REFRESH_SCOPES", {"cameras"})
    build_mod.overpass_query("cameras_XX", "query", offline=False)
    assert len(calls) == 2


def test_overpass_offline_without_cache_exits(build_mod, sandbox):
    with pytest.raises(SystemExit):
        build_mod.overpass_query("cameras_YY", "query", offline=True)


def test_fetch_division_offline_cache(build_mod, sandbox):
    entry = {"state_code": "WA", "name": "Washington",
             "division_id": "gers-123",
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
    build_mod.CACHE_DIR.mkdir()
    (build_mod.CACHE_DIR / "overture_division_WA.json").write_text(
        json.dumps(entry), encoding="utf-8")
    assert build_mod.fetch_division("WA", offline=True) == entry
    with pytest.raises(SystemExit):
        build_mod.fetch_division("VA", offline=True)


def test_fetch_places_cache_and_mapping_invalidation(build_mod, sandbox):
    code_lookup = {"school": "school", "elementary_school": "school"}
    division = {"geometry": {"type": "Polygon",
                             "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
    places = [{"place_id": "overture-1", "lat": 0.5, "lon": 0.5,
               "name": "X", "category": "school", "state_code": "WA",
               "religion": None, "overture_category": "school"}]
    build_mod.CACHE_DIR.mkdir()
    cache_file = build_mod.CACHE_DIR / "overture_places_WA.json"
    cache_file.write_text(json.dumps(
        {"mapping_key": sorted(code_lookup), "places": places}),
        encoding="utf-8")

    got = build_mod.fetch_overture_places(
        "WA", {"school"}, code_lookup, division, offline=True)
    assert got == places

    # A different mapping invalidates the cache; offline can't refetch.
    cache_file.write_text(json.dumps(
        {"mapping_key": ["something_else"], "places": places}),
        encoding="utf-8")
    with pytest.raises(SystemExit):
        build_mod.fetch_overture_places(
            "WA", {"school"}, code_lookup, division, offline=True)


# ----------------------------------------------------------------- emit ----

def test_emit_artifacts(build_mod, sandbox):
    rule = make_rule(buffer_specified=False, heuristic_buffer_meters=None,
                     last_verified="2026-07-18")
    cam_flagged = make_cam(camera_id="cam-flagged", osm_id=1, operator=None,
                           brand=None, direction="90")
    cam_clear = make_cam(camera_id="cam-clear", osm_id=2, lat=48.5,
                         operator=None, brand=None, direction=None)
    place = place_at(build_mod, 47.0, -122.0, 80)
    matches = build_mod.run_matching(
        [cam_flagged, cam_clear], {"school": [place]}, [rule],
        default_buffer=100, scan_max=400)

    divisions = {
        "WA": {"state_code": "WA", "name": "Washington",
               "division_id": "gers-wa",
               "geometry": {"type": "Polygon",
                            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
        "OR": {"state_code": "OR", "name": "Oregon",
               "division_id": "gers-or",
               "geometry": {"type": "Polygon",
                            "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 2]]]}},
        "PR": {"state_code": "PR", "name": "Puerto Rico",
               "division_id": "gers-pr",
               "geometry": {"type": "Polygon",
                            "coordinates": [[[4, 4], [5, 4], [5, 5], [4, 4]]]}},
    }
    rules_doc = {
        "states_reviewed_no_siting_law": [
            {"state_code": "OR", "verdict": "no_siting_law",
             "note": "retention only", "last_verified": "2026-07-18"},
        ],
    }
    meta = build_mod.emit(
        [cam_flagged, cam_clear], {"school": [place]}, matches, [rule],
        rules_doc, divisions, default_buffer=100, rule_states=["WA"],
        scan_max=400)

    out = build_mod.OUT_DIR
    cams = json.loads((out / "cameras.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["camera_id"]: f["properties"]
             for f in cams["features"]}
    assert by_id["cam-flagged"]["status"] == "flagged"
    assert by_id["cam-flagged"]["match_count"] == 1
    assert by_id["cam-clear"]["status"] == "checked_clear"
    # matches ride along as a JSON string for the web app
    assert isinstance(by_id["cam-flagged"]["matches"], str)
    assert json.loads(by_id["cam-flagged"]["matches"])[0]["place_id"] == "p-1"

    cones = json.loads((out / "view_cones.geojson").read_text(encoding="utf-8"))
    assert [f["properties"]["camera_id"] for f in cones["features"]] == ["cam-flagged"]

    states = json.loads((out / "states.geojson").read_text(encoding="utf-8"))
    got = {f["properties"]["state_code"]: f["properties"]
           for f in states["features"]}
    # unreviewed PR emits no feature at all: no overlay = no claim
    assert set(got) == {"WA", "OR"}
    assert got["WA"]["law_status"] == "siting_law"
    assert got["WA"]["last_verified"] == "2026-07-18"
    assert got["WA"]["division_id"] == "gers-wa"
    assert got["OR"]["law_status"] == "reviewed_no_law"

    rules_json = json.loads((out / "rules.json").read_text(encoding="utf-8"))
    assert rules_json["sliders"]["close_by"]["default"] == 100
    assert rules_json["sliders"]["on_premises"]["default"] == 50
    assert rules_json["flagged_count"] == 1
    assert rules_json["camera_count"] == 2
    assert meta["flagged_count"] == 1

    # data license notice ships with the artifacts
    lic = (out / "DATA_LICENSE.txt").read_text(encoding="utf-8")
    assert "OpenStreetMap" in lic and "Overture" in lic

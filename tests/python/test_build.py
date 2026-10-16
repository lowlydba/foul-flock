"""Unit tests for build/build.py: geometry, taxonomy mapping, matching,
cache/refresh behavior, and artifact emission. Fully offline."""

import json
import sys
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
    assert lookup["kindergarten"] == "school"


def test_taxonomy_healthcare_scope(lookup):
    # kept: facilities, ERs and urgent care, and the pediatric subtree
    assert lookup["hospital"] == "healthcare_facility"
    assert lookup["childrens_hospital"] == "healthcare_facility"
    assert lookup["emergency_department"] == "healthcare_facility"
    assert lookup["urgent_care_clinic"] == "healthcare_facility"
    assert lookup["walk_in_clinic"] == "healthcare_facility"
    assert lookup["pediatric_clinic"] == "healthcare_facility"
    assert lookup["pediatric_cardiology"] == "healthcare_facility"
    # exact anchor: the generic node matches, its practices do not
    assert lookup["outpatient_care_facility"] == "healthcare_facility"
    assert "doctors_office" not in lookup
    assert "dental_clinic" not in lookup
    assert "dermatology" not in lookup
    assert "medical_billing" not in lookup
    assert "ambulance_or_ems_service" not in lookup


def test_taxonomy_worship_and_civic(lookup):
    assert lookup["muslim_place_of_worship"] == "place_of_worship"
    assert lookup["sunni_muslim_place_of_worship"] == "place_of_worship"
    assert lookup["jewish_place_of_worship"] == "place_of_worship"
    assert lookup["religious_organization"] == "place_of_worship"
    assert lookup["monastery"] == "place_of_worship"
    assert lookup["shinto_shrine"] == "place_of_worship"
    assert lookup["courthouse"] == "courthouse"
    assert lookup["food_bank"] == "food_bank"
    # higher ed is deliberately out of scope
    assert "college_university" not in lookup


def test_guideline_categories(lookup):
    assert lookup["abortion_clinic"] == "reproductive_health_clinic"
    assert lookup["reproductive_perinatal_and_womens_care"] == "reproductive_health_clinic"
    assert "obstetrics_and_gynecology" not in lookup
    assert "immigration_assistance_service" not in lookup
    assert "immigration_law" not in lookup


def test_name_pattern_anchor(build_mod):
    taxonomy = {"svc": ["svc"], "kid": ["svc", "kid"], "other": ["other"]}
    mapping = {"imm": [{"code": "svc", "name_pattern": "immigra|refugee"}]}
    assert build_mod.build_category_lookup(mapping, taxonomy) == {}
    names = build_mod.build_name_lookup(mapping, taxonomy)
    assert set(names) == {"svc", "kid"}
    cat, rx = names["kid"][0]
    assert cat == "imm"
    assert rx.search("Center for Immigrant Development")
    assert not rx.search("Town Community Center")


def test_exact_anchor_does_not_cover_descendants(build_mod):
    taxonomy = {"a": ["a"], "b": ["a", "b"], "c": ["x", "c"]}
    mapping = {"cat1": [{"code": "a", "descendants": False}], "cat2": ["c"]}
    assert build_mod.build_category_lookup(mapping, taxonomy) == {
        "a": "cat1", "c": "cat2"}


def test_taxonomy_loads_paths(build_mod):
    tax = build_mod.load_taxonomy()
    assert tax["elementary_school"] == [
        "education", "place_of_learning", "school", "elementary_school"]
    assert tax["school"][-1] == "school"
    assert len(tax) > 2000


def test_religion_from_hierarchy(build_mod):
    rh = build_mod.religion_from_hierarchy
    assert rh(["cultural_and_historic", "place_of_worship",
               "christian_place_of_worship", "roman_catholic_place_of_worship"]) == "christian"
    assert rh(["cultural_and_historic", "place_of_worship",
               "muslim_place_of_worship"]) == "muslim"
    assert rh(["cultural_and_historic", "place_of_worship",
               "jewish_place_of_worship", "reform_jewish_place_of_worship"]) == "jewish"
    assert rh(["cultural_and_historic", "religious_organization"]) is None
    assert rh(None) is None


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
                        lambda q, **kw: calls.append(q) or b'{"elements": []}')
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


def test_division_seeded_from_previous_build(build_mod, sandbox, monkeypatch):
    geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    build_mod.OUT_DIR.mkdir()
    (build_mod.OUT_DIR / "states.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": geom,
             "properties": {"state_code": "WA", "name": "Washington",
                            "division_id": "gers-wa"}}]}), encoding="utf-8")

    got = build_mod.fetch_division("WA", offline=True)
    assert got == {"state_code": "WA", "name": "Washington",
                   "division_id": "gers-wa", "geometry": geom}

    # a state missing from the previous build still needs a real fetch
    with pytest.raises(SystemExit):
        build_mod.fetch_division("OR", offline=True)

    # --refresh divisions bypasses the seed
    monkeypatch.setattr(build_mod, "REFRESH_SCOPES", {"divisions"})
    with pytest.raises(SystemExit):
        build_mod.fetch_division("WA", offline=True)


def test_fetch_places_reads_current_overture_schema(build_mod, sandbox,
                                                    monkeypatch, capsys):
    pa = pytest.importorskip("pyarrow")
    shapely = pytest.importorskip("shapely")

    tax_t = pa.struct([("primary", pa.string()),
                       ("hierarchy", pa.list_(pa.string()))])

    def tax(primary, hierarchy):
        return {"primary": primary, "hierarchy": hierarchy}

    pts = [(0.5, 0.5), (0.5, 0.6), (0.5, 0.7), (0.5, 0.8), (5.0, 5.0),
           (0.5, 0.9)]
    batch = pa.record_batch({
        "id": pa.array(["a", "b", "c", "d", "e", "f"]),
        "geometry": pa.array([shapely.to_wkb(shapely.Point(x, y))
                              for x, y in pts]),
        "names": pa.array([{"primary": n} for n in
                           ["Elm Elementary", "St Mark", "Doc Office",
                            "Mystery", "Outside School", None]],
                          type=pa.struct([("primary", pa.string())])),
        "taxonomy": pa.array([
            tax("elementary_school",
                ["education", "place_of_learning", "school", "elementary_school"]),
            tax("roman_catholic_place_of_worship",
                ["cultural_and_historic", "place_of_worship",
                 "christian_place_of_worship", "roman_catholic_place_of_worship"]),
            tax("doctors_office",
                ["health_care", "outpatient_care_facility", "doctors_office"]),
            tax("brand_new_code", ["x", "brand_new_code"]),
            tax("elementary_school",
                ["education", "place_of_learning", "school", "elementary_school"]),
            None,
        ], type=tax_t),
    })
    monkeypatch.setattr(build_mod, "_overture_reader",
                        lambda *a, **k: iter([batch]))

    taxonomy = build_mod.load_taxonomy()
    lookup = build_mod.build_category_lookup(
        build_mod.yaml.safe_load(build_mod.RULES_FILE.read_text(
            encoding="utf-8"))["category_overture_mapping"], taxonomy)
    division = {"geometry": {"type": "Polygon", "coordinates": [
        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}

    got = build_mod.fetch_overture_places(
        "XX", {"school", "place_of_worship", "healthcare_facility"}, lookup,
        division, offline=False, known_codes=set(taxonomy))

    assert [(p["place_id"], p["category"], p["religion"]) for p in got] == [
        ("overture-a", "school", None),
        ("overture-b", "place_of_worship", "christian"),
    ]
    assert got[0]["name"] == "Elm Elementary"
    # doctors_office is excluded; the unknown code is reported, not matched
    assert "brand_new_code" in capsys.readouterr().out


def test_fetch_places_rejects_pre_taxonomy_schema(build_mod, sandbox,
                                                  monkeypatch):
    pa = pytest.importorskip("pyarrow")
    pytest.importorskip("shapely")
    legacy = pa.record_batch({"id": pa.array(["a"]),
                              "categories": pa.array([None], type=pa.string())})
    monkeypatch.setattr(build_mod, "_overture_reader",
                        lambda *a, **k: iter([legacy]))
    division = {"geometry": {"type": "Polygon", "coordinates": [
        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
    with pytest.raises(SystemExit, match="taxonomy"):
        build_mod.fetch_overture_places(
            "XX", {"school"}, {"school": "school"}, division, offline=False)


def test_overpass_runtime_error_is_retried_not_cached(build_mod, sandbox,
                                                      monkeypatch):
    import io
    import urllib.request

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    bodies = [
        b'{"elements":[{"type":"node","id":9}],"remark":"runtime error: Query ran out of memory"}',
        b'{"version":0.6,"remark":"runtime error: Query timed out","elements":[]}',
        b'{"version":0.6,"elements":[{"type":"node","id":1}]}',
    ]
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: Resp(bodies.pop(0)))
    monkeypatch.setattr(build_mod.time, "sleep", lambda s: None)

    got = build_mod.overpass_query("roads_XX", "q", offline=False)
    assert got["elements"] == [{"type": "node", "id": 1}]
    cached = (build_mod.CACHE_DIR / "roads_XX.json").read_text(encoding="utf-8")
    assert "runtime error" not in cached


def test_roads_scope_refreshes_cached_road_chunks(build_mod, sandbox,
                                                 monkeypatch):
    calls = []
    monkeypatch.setattr(build_mod, "http_post_overpass",
                        lambda q, **k: calls.append(q) or b'{"elements":[]}')
    monkeypatch.setattr(build_mod.time, "sleep", lambda s: None)
    args = ("roads_XX", "q", False)
    kw = {"refresh_scope": ("cameras", "roads")}
    build_mod.overpass_query(*args, **kw)
    build_mod.overpass_query(*args, **kw)
    assert len(calls) == 1
    monkeypatch.setattr(build_mod, "REFRESH_SCOPES", {"roads"})
    build_mod.overpass_query(*args, **kw)
    assert len(calls) == 2


def test_refresh_roads_scope_bypasses_reuse(build_mod, sandbox, monkeypatch):
    seen = {}

    def fake_apply(cameras, rules, offline, previous=None):
        seen["previous"] = previous
        return 0

    monkeypatch.setattr(build_mod, "apply_road_guidelines", fake_apply)
    monkeypatch.setattr(build_mod, "load_previous_cameras",
                        lambda: {"cam": {}})
    monkeypatch.setattr(build_mod, "fetch_division",
                        lambda st, offline: {"geometry": {}, "division_id": "g",
                                             "name": st, "state_code": st})
    monkeypatch.setattr(build_mod, "fetch_cameras", lambda st, offline: [])
    monkeypatch.setattr(build_mod, "fetch_overture_places",
                        lambda *a, **k: [])
    monkeypatch.setattr(build_mod, "emit", lambda *a, **k: {
        "flagged_count": 0, "camera_count": 0})
    for scope, expected in (("cameras", {"cam": {}}), ("roads", {})):
        monkeypatch.setattr(sys, "argv", ["build.py", "--refresh", scope,
                                          "--states", "WA"])
        monkeypatch.setattr(build_mod, "REFRESH_SCOPES", set())
        build_mod.main()
        assert seen["previous"] == expected

    monkeypatch.setattr(sys, "argv", ["build.py", "--refresh", "bogus"])
    with pytest.raises(SystemExit, match="bogus"):
        build_mod.main()


def test_overture_reader_uses_stac_and_falls_back(build_mod, monkeypatch):
    import types

    calls = []

    def fake_reader(overture_type, bbox=None, stac=False, **kw):
        calls.append(stac)
        if stac and fake_reader.fail:
            raise RuntimeError("index down")
        return fake_reader.result

    core = types.SimpleNamespace(record_batch_reader=fake_reader)
    monkeypatch.setitem(sys.modules, "overturemaps",
                        types.SimpleNamespace(core=core))

    fake_reader.fail, fake_reader.result = False, iter(["batch"])
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) == ["batch"]
    assert calls == [True]

    # STAC says nothing intersects: an empty stream, not an error
    calls.clear()
    fake_reader.result = None
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) == []

    # index unreachable: retried without STAC
    calls.clear()
    fake_reader.fail, fake_reader.result = True, iter(["batch"])
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) == ["batch"]
    assert calls == [True, False]


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
        "states_reviewed": [
            {"state_code": "OR", "statute_status": "no_siting_law",
             "summary": "retention only.", "last_verified": "2026-07-18",
             "source_url": "https://example.org/or"},
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
    assert got["OR"]["summary"] == "retention only."
    assert got["OR"]["statute_status"] == "no_siting_law"
    assert got["OR"]["source_url"] == "https://example.org/or"

    rules_json = json.loads((out / "rules.json").read_text(encoding="utf-8"))
    assert rules_json["sliders"]["close_by"]["default"] == 100
    assert rules_json["sliders"]["on_premises"]["default"] == 50
    assert rules_json["flagged_count"] == 1
    assert rules_json["camera_count"] == 2
    assert meta["flagged_count"] == 1

    # data license notice ships with the artifacts
    lic = (out / "DATA_LICENSE.txt").read_text(encoding="utf-8")
    assert "OpenStreetMap" in lic and "Overture" in lic


# ------------------------------------------------ spirit-of guidelines ----

def road_way(ref="SR 50"):
    # east-west road at lat 28.0
    return {"type": "way", "tags": {"ref": ref, "highway": "primary"},
            "geometry": [{"lat": 28.0, "lon": -81.001},
                         {"lat": 28.0, "lon": -80.999}]}


def test_nearest_road_refs(build_mod):
    on = make_cam(camera_id="on", lat=28.0001, lon=-81.0, state="FL")
    off = make_cam(camera_id="off", lat=28.001, lon=-81.0, state="FL")
    got = build_mod.nearest_road_refs([on, off], [road_way()], 30)
    assert set(got) == {"on"}
    assert got["on"][0] == "SR 50"
    assert got["on"][1] == pytest.approx(11.1, rel=0.05)


def guideline_road_rule(**over):
    rule = {"rule_id": "G-1", "state_code": "TX",
            "authority": "executive_action", "mechanism": "road_class",
            "road_ref_regex": "x", "road_buffer_m": 30,
            "grandfather_cutoff": build_date()}
    rule.update(over)
    return rule


def build_date():
    from datetime import date
    return date(2026, 9, 8)


def test_road_guideline_phases(build_mod, monkeypatch):
    old = make_cam(camera_id="old", osm_id=1, lat=28.0001, lon=-81.0, state="TX")
    new = make_cam(camera_id="new", osm_id=2, lat=28.0001, lon=-81.0, state="TX")
    off = make_cam(camera_id="off", osm_id=3, lat=28.01, lon=-81.0, state="TX")
    monkeypatch.setattr(build_mod, "fetch_state_road_ways",
                        lambda *a, **k: [road_way("TX 71")])
    monkeypatch.setattr(build_mod, "fetch_camera_ids_asof",
                        lambda *a, **k: {1, 3})
    n = build_mod.apply_road_guidelines(
        [old, new, off], [guideline_road_rule()], offline=True)
    assert n == 1
    assert old["road_phase"] == "existing" and old["road_flag"] is False
    assert new["road_phase"] == "new" and new["road_flag"] is True
    assert "road_flag" not in off

    # no cutoff: everything on a state road is flagged
    fl = make_cam(camera_id="fl", osm_id=4, lat=28.0001, lon=-81.0, state="FL")
    rule = guideline_road_rule(state_code="FL", grandfather_cutoff=None)
    assert build_mod.apply_road_guidelines([fl], [rule], offline=True) == 1
    assert fl["road_phase"] == "all"

    # as-of snapshot unavailable: can't grandfather, flag as unknown
    monkeypatch.setattr(build_mod, "fetch_camera_ids_asof",
                        lambda *a, **k: None)
    unk = make_cam(camera_id="unk", osm_id=5, lat=28.0001, lon=-81.0, state="TX")
    build_mod.apply_road_guidelines([unk], [guideline_road_rule()], True)
    assert unk["road_phase"] == "unknown" and unk["road_flag"] is True


def test_road_lookup_reuses_previous_build(build_mod, monkeypatch):
    rule = guideline_road_rule(grandfather_cutoff=None)
    key = build_mod.road_eval_key(rule)

    def cam(cid, oid):
        return make_cam(camera_id=cid, osm_id=oid, lat=28.0001, lon=-81.0,
                        state="TX")

    kept, moved, stale, fresh, clear = (
        cam("kept", 1), cam("moved", 2), cam("stale", 3), cam("fresh", 4),
        cam("clear", 5))
    props = {"road_eval": key, "road_rule_id": rule["rule_id"],
             "road_ref": "TX 9"}
    previous = {
        "kept": {"lat": 28.0001, "lon": -81.0, "props": props},
        # same id but the node was moved
        "moved": {"lat": 28.5, "lon": -81.0, "props": props},
        # evaluated under a different rule configuration
        "stale": {"lat": 28.0001, "lon": -81.0,
                  "props": {**props, "road_eval": "other:00000000"}},
        # evaluated before and was not on a state road
        "clear": {"lat": 28.0001, "lon": -81.0,
                  "props": {"road_eval": key, "road_rule_id": None}},
    }
    queried = []
    monkeypatch.setattr(
        build_mod, "fetch_state_road_ways",
        lambda st, rx, buf, ids, off: queried.append(sorted(ids))
        or [road_way("TX 71")])
    n = build_mod.apply_road_guidelines(
        [kept, moved, stale, fresh, clear], [rule], True, previous=previous)

    assert queried == [[2, 3, 4]]            # only unknown cameras hit Overpass
    assert kept["road_ref"] == "TX 9" and kept["road_flag"] is True
    assert "road_rule_id" not in clear and clear["road_eval"] == key
    assert moved["road_ref"] == fresh["road_ref"] == "TX 71"
    assert n == 4


def test_road_guideline_skipped_without_road_data(build_mod, monkeypatch):
    cam = make_cam(camera_id="c", osm_id=1, state="TX")
    monkeypatch.setattr(build_mod, "fetch_state_road_ways",
                        lambda *a, **k: None)
    assert build_mod.apply_road_guidelines(
        [cam], [guideline_road_rule()], offline=True) == 0
    assert "road_flag" not in cam


def test_optional_overpass_miss_offline(build_mod, sandbox):
    assert build_mod.overpass_query(
        "nope", "q", offline=True, required=False) is None
    with pytest.raises(SystemExit):
        build_mod.overpass_query("nope", "q", offline=True)


def test_emit_guideline_status_and_poi(build_mod, sandbox):
    tx_new = make_cam(camera_id="tx-new", osm_id=1, state="TX",
                      operator=None, brand=None, direction=None,
                      road_flag=True, road_phase="new", road_ref="TX 71",
                      road_rule_id="G-1")
    tx_old = make_cam(camera_id="tx-old", osm_id=2, state="TX",
                      operator=None, brand=None, direction=None,
                      road_flag=False, road_phase="existing",
                      road_ref="TX 71", road_rule_id="G-1")
    ct = make_cam(camera_id="ct-1", osm_id=3, lat=41.6, lon=-72.7,
                  state="CT", operator=None, brand=None, direction=None)
    clinic = place_at(build_mod, 41.6, -72.7, 30, state="CT",
                      cat="reproductive_health_clinic", pid="clinic")
    far_clinic = place_at(build_mod, 41.9, -72.7, 30, state="CT",
                          cat="reproductive_health_clinic", pid="far")
    poi_rule = {"rule_id": "CT-1", "state_code": "CT",
                "authority": "statute_pending_distance",
                "mechanism": "proximity",
                "restricted_category": "reproductive_health_clinic",
                "heuristic_buffer_meters": 50, "distance_tbd": True,
                "title": "CT guideline", "last_verified": "2026-10-16",
                "maintainer_notes": "private"}
    road_rule = guideline_road_rule()
    road_rule.update(title="TX pause", last_verified="2026-10-16")
    matches = build_mod.run_matching(
        [ct], {"reproductive_health_clinic": [clinic, far_clinic]},
        [poi_rule], default_buffer=100, scan_max=400)
    assert matches[0]["guideline"] is True and matches[0]["distance_tbd"]

    div = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    divisions = {s: {"state_code": s, "name": s, "division_id": f"g-{s}",
                     "geometry": div} for s in ("TX", "CT")}
    meta = build_mod.emit(
        [tx_new, tx_old, ct], {"reproductive_health_clinic": [clinic, far_clinic]},
        matches, [], {"states_reviewed": [
            {"state_code": "TX", "statute_status": "no_alpr_statute",
             "summary": "n.", "last_verified": "2026-07-18"}]},
        divisions, default_buffer=100, rule_states=[], scan_max=400,
        guideline_rules=[poi_rule, road_rule])

    out = build_mod.OUT_DIR
    cams = {f["properties"]["camera_id"]: f["properties"] for f in json.loads(
        (out / "cameras.geojson").read_text(encoding="utf-8"))["features"]}
    assert cams["tx-new"]["status"] == "guideline"
    assert cams["tx-old"]["status"] == "checked_clear"   # grandfathered
    assert cams["tx-old"]["road_ref"] == "TX 71"
    assert cams["ct-1"]["status"] == "guideline"
    assert meta["flagged_count"] == 0 and meta["guideline_count"] == 2
    assert meta["guideline_states"] == ["CT", "TX"]
    exported = json.loads((out / "rules.json").read_text(encoding="utf-8"))
    assert "maintainer_notes" not in exported["guideline_rules"][0]

    states = {f["properties"]["state_code"]: f["properties"] for f in json.loads(
        (out / "states.geojson").read_text(encoding="utf-8"))["features"]}
    assert states["TX"]["law_status"] == "guideline"
    assert states["CT"]["guideline_title"] == "CT guideline"

    # guideline-category places are emitted even when no camera is near
    places = json.loads((out / "places.geojson").read_text(encoding="utf-8"))
    assert {f["properties"]["place_id"] for f in places["features"]} == {
        "clinic", "far"}

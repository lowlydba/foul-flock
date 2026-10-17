"""Overpass and Overture fetching: cache hits, --refresh scopes, offline
behavior, retries, and the camera and place parsers."""

import json
import sys

import pytest


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


def _stac_index_bytes():
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    def asset(key):
        return {"aws": {"alternate": {"s3": {"href": f"s3://bucket/{key}"}}}}

    table = pa.table({
        "collection": ["place", "place", "division_area"],
        "type": ["Feature"] * 3,
        "bbox": [{"xmin": 0.0, "ymin": 0.0, "xmax": 1.0, "ymax": 1.0},
                 {"xmin": 5.0, "ymin": 5.0, "xmax": 6.0, "ymax": 6.0},
                 {"xmin": 0.0, "ymin": 0.0, "xmax": 1.0, "ymax": 1.0}],
        "assets": [asset("p0"), asset("p5"), asset("d0")],
    })
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


@pytest.fixture()
def fake_overture(build_mod, monkeypatch):
    """overturemaps stub plus a fresh STAC index memo; file reads return
    the list of files the reader was asked to open."""
    import types

    monkeypatch.setattr(build_mod, "_STAC_INDEX", {})
    monkeypatch.setattr(build_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_mod, "_files_reader",
                        lambda files, bbox: iter([files]))
    core = types.SimpleNamespace(
        get_latest_release=lambda: "2026-01-01.0",
        record_batch_reader=lambda *a, **k: iter(["full scan"]))
    monkeypatch.setitem(sys.modules, "overturemaps",
                        types.SimpleNamespace(core=core))
    return core


def test_overture_reader_downloads_stac_index_once(build_mod, monkeypatch,
                                                   fake_overture):
    gets = []
    monkeypatch.setattr(build_mod, "_http_get",
                        lambda url: gets.append(url) or _stac_index_bytes())

    assert list(build_mod._overture_reader("place", (0.5, 0.5, 0.6, 0.6))) \
        == [["bucket/p0"]]
    assert list(build_mod._overture_reader("division_area", (0, 0, 2, 2))) \
        == [["bucket/d0"]]
    # no intersecting file: nothing to open, and no full-scan fallback
    assert list(build_mod._overture_reader("place", (9, 9, 10, 10))) == [[]]
    assert gets == ["https://stac.overturemaps.org/2026-01-01.0/"
                    "collections.parquet"]


def test_files_reader_with_no_files_is_empty(build_mod):
    assert list(build_mod._files_reader([], (0, 0, 1, 1))) == []


def test_stac_index_retries_rate_limits(build_mod, monkeypatch, fake_overture):
    import urllib.error

    sleeps, responses = [], []
    monkeypatch.setattr(build_mod.time, "sleep", sleeps.append)

    def flaky_get(url):
        if len(responses) < 2:
            hdrs = {"Retry-After": "45"} if not responses else {}
            responses.append(429)
            raise urllib.error.HTTPError(url, 429, "Too Many Requests",
                                         hdrs, None)
        return _stac_index_bytes()

    monkeypatch.setattr(build_mod, "_http_get", flaky_get)
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) \
        == [["bucket/p0"]]
    assert sleeps == [45, 30]


def test_overture_reader_falls_back_when_index_unreachable(
        build_mod, monkeypatch, fake_overture):
    import urllib.error

    gets = []

    def down(url):
        gets.append(url)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(build_mod, "_http_get", down)
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) \
        == ["full scan"]
    # the failure is remembered: later queries skip straight to the scan
    assert list(build_mod._overture_reader("place", (0, 0, 1, 1))) \
        == ["full scan"]
    assert len(gets) == len(build_mod.STAC_RETRY_WAITS) + 1

    # no reader from the fallback either: the build stops
    fake_overture.record_batch_reader = lambda *a, **k: None
    with pytest.raises(SystemExit, match="reader unavailable"):
        build_mod._overture_reader("place", (0, 0, 1, 1))


def test_stac_index_does_not_retry_client_errors(build_mod, monkeypatch,
                                                 fake_overture):
    import urllib.error

    gets = []

    def missing(url):
        gets.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(build_mod, "_http_get", missing)
    assert build_mod._stac_index("2026-01-01.0") is None
    assert len(gets) == 1


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


def test_fetch_cameras_reads_osm_last_edit_date(build_mod, monkeypatch):
    elements = [
        {"type": "node", "id": 7, "lat": 47.0, "lon": -122.0,
         "timestamp": "2025-03-14T18:22:05Z", "user": "someone", "uid": 5,
         "tags": {"brand": "Flock Safety", "direction": "90"}},
        # a cache from before `out meta` has no timestamp
        {"type": "node", "id": 8, "lat": 47.1, "lon": -122.1, "tags": {}},
    ]
    seen = {}

    def fake_query(name, q, offline, **kw):
        seen["q"] = q
        return {"elements": elements}

    monkeypatch.setattr(build_mod, "overpass_query", fake_query)
    cams = {c["osm_id"]: c for c in build_mod.fetch_cameras("WA", offline=True)}
    assert "out meta;" in seen["q"]
    assert cams[7]["last_edited"] == "2025-03-14"
    assert cams[8]["last_edited"] is None
    # only the edit date is kept; editor identity stays out of the data
    assert "user" not in cams[7] and "uid" not in cams[7]

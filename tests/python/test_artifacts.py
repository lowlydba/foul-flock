"""Artifact emission: core GeoJSON, PMTiles archives, states, places,
rules.json and the data license."""

import json

import pytest

import vectortiles as vt

from factories import guideline_road_rule, make_cam, make_rule, place_at


def test_emit_artifacts(build_mod, sandbox):
    rule = make_rule(buffer_specified=False, heuristic_buffer_meters=None,
                     last_verified="2026-07-18")
    cam_flagged = make_cam(camera_id="cam-flagged", osm_id=1, operator=None,
                           brand=None, direction="90", last_edited="2025-03-14")
    cam_clear = make_cam(camera_id="cam-clear", osm_id=2, lat=48.5,
                         operator=None, brand=None, direction="45",
                         last_edited="2026-01-05")
    # ~250 m from the place: clear at the default radius, but a wider
    # slider could flag it, so it must stay in the core file
    cam_near = make_cam(camera_id="cam-near", osm_id=3, lat=47.0025,
                        operator=None, brand=None, direction=None)
    place = place_at(build_mod, 47.0, -122.0, 80)
    matches = build_mod.run_matching(
        [cam_flagged, cam_clear, cam_near], {"school": [place]}, [rule],
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
    build_mod.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ("cameras_clear_ZZ.geojson", "places.geojson"):
        (build_mod.OUT_DIR / stale).write_text("{}")
    meta = build_mod.emit(
        [cam_flagged, cam_clear, cam_near], {"school": [place]}, matches,
        [rule], rules_doc, divisions, default_buffer=100, rule_states=["WA"],
        scan_max=400)

    out = build_mod.OUT_DIR
    cams = json.loads((out / "cameras.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["camera_id"]: f["properties"]
             for f in cams["features"]}
    assert by_id["cam-flagged"]["status"] == "flagged"
    assert by_id["cam-flagged"]["match_count"] == 1
    # candidates within the scan radius stay in the core file for the sliders
    assert by_id["cam-near"]["status"] == "checked_clear"
    assert json.loads(by_id["cam-near"]["matches"])
    # a camera with no candidate anywhere can never be flagged: static tiles
    assert "cam-clear" not in by_id
    tiles = {f["properties"]["camera_id"]: f for f in vt.read_features(
        out / "clear_cameras.pmtiles", "clear_cameras")}
    assert set(tiles) == {"cam-clear"}
    clear = tiles["cam-clear"]["properties"]
    assert clear["status"] == "checked_clear"
    # tile geometry is rounded, so exact coordinates ride along
    assert (clear["lat_e7"], clear["lon_e7"]) == (485_000_000, -1_220_000_000)
    assert meta["clear_cone_count"] == 1 and meta["place_count"] == 1
    # the OSM last-edit date rides along when known and is omitted when not
    assert clear["last_edited"] == "2026-01-05"
    assert by_id["cam-flagged"]["last_edited"] == "2025-03-14"
    assert "last_edited" not in by_id["cam-near"]
    lon, lat = tiles["cam-clear"]["geometry"]["coordinates"]
    assert (lon, lat) == pytest.approx((-122.0, 48.5), abs=2e-5)
    assert meta["clear_camera_count"] == 1
    cones_clear = vt.read_features(out / "clear_cones.pmtiles", "clear_cones")
    assert [f["properties"]["camera_id"] for f in cones_clear] == ["cam-clear"]
    assert not list(out.glob("*_clear_*.geojson")), "stale shards removed"
    assert not (out / "places.geojson").exists()
    # matches ride along as a JSON string for the web app
    assert isinstance(by_id["cam-flagged"]["matches"], str)
    assert json.loads(by_id["cam-flagged"]["matches"])[0]["place_id"] == "p-1"
    # empty/false/null values are omitted; provenance fields are always kept
    assert not {"matches", "match_count", "road_flag", "road_rule_id"} & set(clear)
    for props in [*by_id.values(), clear]:
        assert props["osm_url"].startswith("https://www.openstreetmap.org/node/")
    # a match's camera_id just repeats the feature's own
    assert "camera_id" not in json.loads(by_id["cam-flagged"]["matches"])[0]

    cones = json.loads((out / "view_cones.geojson").read_text(encoding="utf-8"))
    assert [f["properties"]["camera_id"] for f in cones["features"]] == ["cam-flagged"]

    places_tiles = vt.read_features(out / "places.pmtiles", "places", zoom=12)
    assert [f["properties"]["place_id"] for f in places_tiles] == ["p-1"]
    state_tiles = vt.read_features(out / "states.pmtiles", "states", zoom=0)
    assert {f["properties"]["state_code"] for f in state_tiles} == {"WA", "OR"}

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
    assert rules_json["camera_count"] == 3
    assert meta["flagged_count"] == 1

    # data license notice ships with the artifacts
    lic = (out / "DATA_LICENSE.txt").read_text(encoding="utf-8")
    assert "OpenStreetMap" in lic and "Overture" in lic


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
    tiles = {f["properties"]["camera_id"]: f["properties"] for f in vt.read_features(
        out / "clear_cameras.pmtiles", "clear_cameras")}
    assert cams["tx-new"]["status"] == "guideline"
    assert "tx-old" not in cams
    assert tiles["tx-old"]["status"] == "checked_clear"   # grandfathered
    assert tiles["tx-old"]["road_ref"] == "TX 71"
    # road lookups seed from the tiles too, or clear cameras would be re-queried
    prev = build_mod.load_previous_cameras()
    assert prev["tx-old"]["props"]["road_ref"] == "TX 71"
    assert prev["tx-old"]["lat"] == 47.0 and prev["tx-old"]["lon"] == -122.0
    assert set(prev) == {"tx-new", "tx-old", "ct-1"}
    # optional archives are absent, and rules.json says so, when empty
    assert meta["clear_camera_count"] == 1 and meta["clear_cone_count"] == 0
    assert not (out / "clear_cones.pmtiles").exists()
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
    places = vt.read_features(out / "places.pmtiles", "places", zoom=12)
    assert {f["properties"]["place_id"] for f in places} == {"clinic", "far"}
